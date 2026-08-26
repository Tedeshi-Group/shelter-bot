import asyncio
import logging
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands
from discord.ext.voice_recv import VoiceRecvClient, WaveSink

logger = logging.getLogger(__name__)


class VoiceRecorder:
    """Records audio from a Discord voice channel using discord-ext-voice-recv."""

    def __init__(self, channel: discord.VoiceChannel, output_path: Path):
        self.channel = channel
        self.output_path = output_path
        self.vc: VoiceRecvClient | None = None
        self.sink: WaveSink | None = None
        self.started_at: datetime | None = None
        self.participants: dict[int, dict[str, Any]] = {}
        self._recording = False

    async def start(self) -> bool:
        """Start recording. Returns True if successful."""
        try:
            self.vc = await self.channel.connect(cls=VoiceRecvClient)
            self.started_at = datetime.now(timezone.utc).replace(tzinfo=None)
            self._recording = True

            self.sink = WaveSink(str(self.output_path))
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

        if self.vc:
            self.vc.stop_listening()
            await self.vc.disconnect()

        if self.sink:
            self.sink.cleanup()

        duration_seconds = None
        if self.started_at:
            duration_seconds = int((ended_at - self.started_at).total_seconds())

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
        if user.id not in self.participants:
            self.participants[user.id] = {
                "username": user.name,
                "joined_at": datetime.now(timezone.utc).replace(tzinfo=None),
                "left_at": None,
                "duration_seconds": None,
            }
        elif self.participants[user.id]["left_at"] is not None:
            self.participants[user.id]["joined_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
            self.participants[user.id]["left_at"] = None

    def remove_participant(self, user: discord.Member):
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
        self.current_channel_id: int | None = None
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
            if not self.recorder or not self.current_channel_id:
                return
            if after.channel and after.channel.id == self.current_channel_id:
                self.recorder.add_participant(member)
            if before.channel and before.channel.id == self.current_channel_id:
                self.recorder.remove_participant(member)

        asyncio.create_task(self.bot.start(self.token))
        await asyncio.wait_for(self._ready.wait(), timeout=30)

    async def stop(self):
        if self.recorder:
            await self.recorder.stop()
        if self.bot:
            await self.bot.close()

    def _find_channel(self, channel_id: int) -> discord.VoiceChannel | None:
        """Find a voice channel in the worker bot's own guild cache."""
        for guild in self.bot.guilds:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.VoiceChannel):
                return channel
        return None

    async def start_recording(self, channel_id: int, channel_name: str) -> bool:
        """Start recording a voice channel by ID."""
        if self.is_busy:
            logger.warning(f"Worker {self.worker_id} is already busy")
            return False

        # Find channel in THIS worker's guild cache
        channel = self._find_channel(channel_id)
        if not channel:
            logger.error(f"Worker {self.worker_id}: channel {channel_id} not found in guild cache")
            return False

        self.is_busy = True
        self.current_channel_id = channel_id

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"worker-{self.worker_id}_{channel_name}_{timestamp}.wav"
        output_path = Path(tempfile.gettempdir()) / "shelter_recordings" / filename
        output_path.parent.mkdir(parents=True, exist_ok=True)

        self.recorder = VoiceRecorder(channel, output_path)
        success = await self.recorder.start()

        if not success:
            self.is_busy = False
            self.current_channel_id = None
            self.recorder = None
            return False

        return True

    async def stop_recording(self) -> dict[str, Any] | None:
        if not self.recorder:
            return None

        metadata = await self.recorder.stop()
        self.is_busy = False
        self.current_channel_id = None
        self.recorder = None

        return metadata

    def get_status(self) -> dict[str, Any]:
        channel_name = None
        if self.current_channel_id and self.bot:
            ch = self._find_channel(self.current_channel_id)
            if ch:
                channel_name = ch.name

        status = {
            "worker_id": self.worker_id,
            "is_busy": self.is_busy,
            "current_channel": channel_name,
            "bot_user": str(self.bot.user) if self.bot and self.bot.user else None,
            "is_connected": self.bot.is_ready() if self.bot else False,
        }

        if self.recorder and self.recorder.started_at:
            duration = (datetime.now(timezone.utc).replace(tzinfo=None) - self.recorder.started_at).total_seconds()
            status["recording_duration_seconds"] = int(duration)
            status["participants_count"] = len(self.recorder.participants)

        return status
