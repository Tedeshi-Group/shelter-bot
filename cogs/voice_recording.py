import asyncio
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands
from discord.ext.voice_recv import VoiceRecvClient, WaveSink, FFmpegSink

logger = logging.getLogger(__name__)


class VoiceRecorder:
    """Records audio from a Discord voice channel using discord-ext-voice-recv."""

    def __init__(self, channel: discord.VoiceChannel, output_path: Path):
        self.channel = channel
        self.output_path = output_path
        self.vc: VoiceRecvClient | None = None
        self.sink: WaveSink | None = None
        self.started_at: datetime | None = None
        self.participants: dict[int, dict[str, Any]] = {}  # user_id -> {username, joined_at, left_at}
        self._recording = False

    async def start(self) -> bool:
        """Start recording. Returns True if successful."""
        try:
            # Connect to voice channel with VoiceRecvClient
            self.vc = await self.channel.connect(cls=VoiceRecvClient)
            self.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self._recording = True

            # Create sink for recording
            self.sink = WaveSink(str(self.output_path))

            # Start listening
            self.vc.listen(self.sink)

            logger.info(f"Started recording in {self.channel.name} ({self.channel.id})")
            return True

        except Exception as e:
            logger.error(f"Failed to start recording in {self.channel.name}: {e}")
            await self.stop()
            return False

    async def stop(self) -> dict[str, Any] | None:
        """Stop recording and return metadata."""
        if not self._recording:
            return None

        self._recording = False
        ended_at = datetime.now(timezone.utc).replace(tzinfo=None)

        # Stop listening and disconnect
        if self.vc:
            self.vc.stop_listening()
            await self.vc.disconnect()

        # Cleanup sink
        if self.sink:
            self.sink.cleanup()

        # Calculate duration
        duration_seconds = None
        if self.started_at:
            duration_seconds = int((ended_at - self.started_at).total_seconds())

        # Update participant durations
        for user_id, info in self.participants.items():
            if info["left_at"] is None:
                info["left_at"] = ended_at
            if info["joined_at"]:
                info["duration_seconds"] = int(
                    (info["left_at"] - info["joined_at"]).total_seconds()
                )

        metadata = {
            "channel_id": self.channel.id,
            "channel_name": self.channel.name,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": ended_at.isoformat(),
            "duration_seconds": duration_seconds,
            "participants": [
                {
                    "discord_id": uid,
                    "username": info["username"],
                    "joined_at": info["joined_at"].isoformat() if info["joined_at"] else None,
                    "left_at": info["left_at"].isoformat() if info["left_at"] else None,
                    "duration_seconds": info.get("duration_seconds"),
                }
                for uid, info in self.participants.items()
            ],
            "file_size_bytes": self.output_path.stat().st_size if self.output_path.exists() else 0,
        }

        logger.info(
            f"Stopped recording in {self.channel.name}. "
            f"Duration: {duration_seconds}s, Size: {metadata['file_size_bytes']} bytes"
        )

        return metadata

    def add_participant(self, user: discord.Member):
        """Track a participant joining."""
        if user.id not in self.participants:
            self.participants[user.id] = {
                "username": user.name,
                "joined_at": datetime.now(timezone.utc).replace(tzinfo=None),
                "left_at": None,
                "duration_seconds": None,
            }
        elif self.participants[user.id]["left_at"] is not None:
            # User rejoined
            self.participants[user.id]["joined_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
            self.participants[user.id]["left_at"] = None

    def remove_participant(self, user: discord.Member):
        """Track a participant leaving."""
        if user.id in self.participants and self.participants[user.id]["left_at"] is None:
            self.participants[user.id]["left_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
            if self.participants[user.id]["joined_at"]:
                self.participants[user.id]["duration_seconds"] = int(
                    (self.participants[user.id]["left_at"] - self.participants[user.id]["joined_at"]).total_seconds()
                )


class WorkerBot:
    """A single bot instance that can record one voice channel."""

    def __init__(self, token: str, worker_id: int):
        self.token = token
        self.worker_id = worker_id
        self.bot: commands.Bot | None = None
        self.recorder: VoiceRecorder | None = None
        self.is_busy = False
        self.current_channel: discord.VoiceChannel | None = None
        self._ready = asyncio.Event()

    async def start(self):
        """Start the worker bot."""
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True

        self.bot = commands.Bot(command_prefix=f"!worker{self.worker_id}_", intents=intents)

        @self.bot.event
        async def on_ready():
            logger.info(f"Worker {self.worker_id} ({self.bot.user}) is ready")
            self._ready.set()

        @self.bot.event
        async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
            """Track participant join/leave for recording."""
            if not self.recorder or not self._recording:
                return

            # User joined the channel we're recording
            if after.channel and after.channel.id == self.current_channel.id:
                self.recorder.add_participant(member)

            # User left the channel we're recording
            if before.channel and before.channel.id == self.current_channel.id:
                self.recorder.remove_participant(member)

        # Start bot in background
        asyncio.create_task(self.bot.start(self.token))
        await asyncio.wait_for(self._ready.wait(), timeout=30)

    async def stop(self):
        """Stop the worker bot."""
        if self.recorder:
            await self.recorder.stop()
        if self.bot:
            await self.bot.close()

    async def start_recording(self, channel: discord.VoiceChannel) -> bool:
        """Start recording a voice channel."""
        if self.is_busy:
            logger.warning(f"Worker {self.worker_id} is already busy")
            return False

        self.is_busy = True
        self.current_channel = channel

        # Create output file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"worker-{self.worker_id}_{channel.name}_{timestamp}.wav"
        output_path = Path(tempfile.gettempdir()) / "shelter_recordings" / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Create recorder
        self.recorder = VoiceRecorder(channel, output_path)
        success = await self.recorder.start()

        if not success:
            self.is_busy = False
            self.current_channel = None
            self.recorder = None
            return False

        return True

    async def stop_recording(self) -> dict[str, Any] | None:
        """Stop recording and return metadata."""
        if not self.recorder:
            return None

        metadata = await self.recorder.stop()
        self.is_busy = False
        self.current_channel = None
        self.recorder = None

        return metadata

    def get_status(self) -> dict[str, Any]:
        """Get current status of the worker."""
        status = {
            "worker_id": self.worker_id,
            "is_busy": self.is_busy,
            "current_channel": self.current_channel.name if self.current_channel else None,
            "bot_user": str(self.bot.user) if self.bot and self.bot.user else None,
            "is_connected": self.bot and self.bot.is_ready() if self.bot else False,
        }

        if self.recorder and self.recorder.started_at:
            duration = (datetime.now(timezone.utc).replace(tzinfo=None) - self.recorder.started_at).total_seconds()
            status["recording_duration_seconds"] = int(duration)
            status["participants_count"] = len(self.recorder.participants)

        return status
