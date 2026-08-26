"""Voice recording cog — per-worker voice receive with per-user tracks."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import struct
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

import discord
from discord.ext import commands, tasks
from discord.ext.voice_recv import AudioSink, VoiceRecvClient, VoiceData

from database import SessionLocal
from models.voice_recording import VoiceRecording, VoiceRecordingParticipant, VoiceRecordingTrack

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  # 16-bit


class PerUserWaveSink(AudioSink):
    """Sink that creates a separate WAV file per speaker (by SSRC/user)."""

    def __init__(self, recording_id: int, temp_dir: str):
        super().__init__()
        self.recording_id = recording_id
        self.temp_dir = temp_dir
        self._files: dict[int, io.BufferedRandom] = {}
        self._wav_headers: dict[int, bool] = {}
        self._user_map: dict[int, int] = {}  # ssrc -> user_discord_id
        self._username_map: dict[int, str] = {}  # ssrc -> username
        self._start_times: dict[int, float] = {}  # ssrc -> start time
        self._pcm_sizes: dict[int, int] = {}  # ssrc -> total PCM bytes

    def _get_file(self, ssrc: int) -> io.BufferedRandom:
        if ssrc not in self._files:
            path = os.path.join(self.temp_dir, f"track_{ssrc}.wav")
            f = open(path, "w+b")
            # Write placeholder WAV header (44 bytes)
            f.write(b'\x00' * 44)
            self._files[ssrc] = f
            self._wav_headers[ssrc] = False
            self._pcm_sizes[ssrc] = 0
            self._start_times[ssrc] = time.time()
        return self._files[ssrc]

    def wants_opus(self) -> bool:
        return False

    def write(self, user: discord.Member | None, data: VoiceData):
        if user is None:
            return
        ssrc = data.packet.ssrc
        if ssrc == 0:
            return

        if ssrc not in self._user_map and user:
            self._user_map[ssrc] = user.id
            self._username_map[ssrc] = user.display_name

        f = self._get_file(ssrc)
        f.write(data.pcm)
        self._pcm_sizes[ssrc] = self._pcm_sizes.get(ssrc, 0) + len(data.pcm)

    def _write_wav_header(self, f: io.BufferedRandom, data_size: int):
        f.seek(0)
        f.write(b'RIFF')
        f.write(struct.pack('<I', 36 + data_size))
        f.write(b'WAVE')
        f.write(b'fmt ')
        f.write(struct.pack('<I', 16))
        f.write(struct.pack('<H', 1))  # PCM
        f.write(struct.pack('<H', CHANNELS))
        f.write(struct.pack('<I', SAMPLE_RATE))
        f.write(struct.pack('<I', SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH))
        f.write(struct.pack('<H', CHANNELS * SAMPLE_WIDTH))
        f.write(struct.pack('<H', SAMPLE_WIDTH * 8))
        f.write(b'data')
        f.write(struct.pack('<I', data_size))

    def cleanup(self) -> list[dict[str, Any]]:
        """Finalize WAV files and return track info list."""
        tracks = []
        for ssrc, f in self._files.items():
            data_size = self._pcm_sizes.get(ssrc, 0)
            self._write_wav_header(f, data_size)
            f.close()

            duration = int(time.time() - self._start_times.get(ssrc, time.time()))
            path = os.path.join(self.temp_dir, f"track_{ssrc}.wav")
            file_size = os.path.getsize(path) if os.path.exists(path) else 0

            tracks.append({
                "ssrc": ssrc,
                "user_discord_id": self._user_map.get(ssrc, 0),
                "username": self._username_map.get(ssrc, f"unknown_{ssrc}"),
                "path": path,
                "file_size": file_size,
                "duration": duration,
            })
        return tracks


class VoiceRecorder:
    """Per-worker voice recorder with per-user tracks."""

    def __init__(self, worker_id: int, bot: commands.Bot):
        self.worker_id = worker_id
        self.bot = bot
        self.vc: VoiceRecvClient | None = None
        self.sink: PerUserWaveSink | None = None
        self._recording = False
        self._channel_id: int | None = None
        self._channel_name: str | None = None
        self._start_time: datetime | None = None
        self._db_recording_id: int | None = None
        self._temp_dir: str | None = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    async def start(self, channel_id: int, channel_name: str) -> dict[str, Any]:
        """Connect to voice channel and start per-user recording."""
        if self._recording:
            return {"success": False, "error": "Already recording"}

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return {"success": False, "error": f"Channel {channel_id} not found in bot cache"}

        try:
            self.vc = await channel.connect(cls=VoiceRecvClient)
        except Exception as e:
            logger.error(f"Worker {self.worker_id} failed to connect: {e}")
            return {"success": False, "error": str(e)}

        self._channel_id = channel_id
        self._channel_name = channel_name
        self._start_time = datetime.now(timezone.utc).replace(tzinfo=None)

        # Create temp dir for this recording
        self._temp_dir = tempfile.mkdtemp(prefix=f"rec_{self.worker_id}_")

        # Create DB record
        with SessionLocal() as db:
            recording = VoiceRecording(
                channel_id=channel_id,
                channel_name=channel_name,
                worker_bot_id=self.worker_id,
                started_at=self._start_time,
                status="recording",
            )
            db.add(recording)
            db.commit()
            db.refresh(recording)
            self._db_recording_id = recording.id

        self.sink = PerUserWaveSink(self._db_recording_id, self._temp_dir)
        self.vc.listen(self.sink)
        self._recording = True

        logger.info(f"Started recording in {channel_name} ({channel_id})")
        return {"success": True, "channel_name": channel_name, "recording_id": self._db_recording_id}

    async def stop(self) -> dict[str, Any] | None:
        """Stop recording, upload per-user tracks to S3."""
        if not self._recording:
            return None

        self._recording = False
        if self.vc:
            self.vc.stop_listening()
            await self.vc.disconnect()

        # Finalize WAV files
        tracks_info = []
        if self.sink:
            tracks_info = self.sink.cleanup()

        duration = 0
        if self._start_time:
            duration = int((datetime.now(timezone.utc).replace(tzinfo=None) - self._start_time).total_seconds())

        # Upload each track to S3 and save to DB
        from s3_client import S3Client
        s3 = S3Client()
        total_size = 0

        with SessionLocal() as db:
            for track in tracks_info:
                s3_key = f"recordings/{self._db_recording_id}/user_{track['user_discord_id']}_{track['username']}.wav"
                upload_ok = s3.upload_file(track["path"], s3_key)
                file_size = track["file_size"] if upload_ok else 0
                total_size += file_size

                db_track = VoiceRecordingTrack(
                    recording_id=self._db_recording_id,
                    user_discord_id=track["user_discord_id"],
                    username=track["username"],
                    s3_key=s3_key if upload_ok else "",
                    file_size_bytes=file_size,
                    duration_seconds=track["duration"],
                )
                db.add(db_track)

            # Update main recording
            recording = db.get(VoiceRecording, self._db_recording_id)
            if recording:
                recording.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
                recording.duration_seconds = duration
                recording.status = "completed"
                recording.file_size_bytes = total_size
                if tracks_info:
                    recording.s3_key = f"recordings/{self._db_recording_id}/"

            db.commit()

        # Cleanup temp files
        if self._temp_dir:
            for track in tracks_info:
                try:
                    os.remove(track["path"])
                except OSError:
                    pass
            try:
                os.rmdir(self._temp_dir)
            except OSError:
                pass

        result = {
            "channel_name": self._channel_name,
            "duration": duration,
            "tracks_count": len(tracks_info),
            "total_size": total_size,
        }

        self.sink = None
        self.vc = None
        self._channel_id = None
        self._channel_name = None
        self._start_time = None
        self._db_recording_id = None
        self._temp_dir = None

        logger.info(f"Stopped recording: {len(tracks_info)} tracks, {duration}s")
        return result


class WorkerBot:
    """Wrapper around a single worker Discord bot."""

    def __init__(self, token: str, worker_id: int = 1):
        self.worker_id = worker_id
        self.token = token
        self.bot: commands.Bot | None = None
        self.recorder: VoiceRecorder | None = None
        self.is_busy = False
        self._channel_id: int | None = None
        self._channel_name: str | None = None
        self._ready = asyncio.Event()

    @property
    def current_channel_id(self) -> int | None:
        return self._channel_id

    async def start(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        intents.members = True
        intents.message_content = False

        self.bot = commands.Bot(command_prefix="!", intents=intents)
        self.recorder = VoiceRecorder(self.worker_id, self.bot)

        @self.bot.event
        async def on_ready():
            logger.info(f"Worker {self.worker_id} ({self.bot.user}) is ready")
            self._ready.set()

        asyncio.create_task(self.bot.start(self.token))
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=30)
        except asyncio.TimeoutError:
            logger.error(f"Worker {self.worker_id} failed to start within 30s")

    async def start_recording(self, channel_id: int, channel_name: str) -> dict[str, Any]:
        if not self.recorder:
            return {"success": False, "error": "Worker not initialized"}
        result = await self.recorder.start(channel_id, channel_name)
        if result.get("success"):
            self.is_busy = True
            self._channel_id = channel_id
            self._channel_name = channel_name
        return result

    async def stop_recording(self) -> dict[str, Any] | None:
        if not self.recorder:
            return None
        result = await self.recorder.stop()
        self.is_busy = False
        self._channel_id = None
        self._channel_name = None
        return result

    def get_status(self) -> dict[str, Any]:
        elapsed = 0
        if self.recorder and self.recorder.start_time:
            elapsed = int((datetime.now(timezone.utc) - self.recorder.start_time).total_seconds())
        return {
            "worker_id": self.worker_id,
            "is_busy": self.is_busy,
            "channel_id": self._channel_id,
            "channel_name": self._channel_name,
            "elapsed_seconds": elapsed,
        }

    async def stop(self):
        if self.recorder and self.recorder.is_recording:
            await self.recorder.stop()
        if self.bot:
            await self.bot.close()


class BotManager:
    """Manages worker bots for voice recording."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.workers: list[WorkerBot] = []
        self.active_recordings: dict[int, WorkerBot] = {}  # channel_id -> worker
        self.queue: list[dict[str, Any]] = []
        self._initialized = False

    async def initialize(self, worker_tokens: list[str]):
        for i, token in enumerate(worker_tokens, 1):
            worker = WorkerBot(i, token)
            await worker.start()
            self.workers.append(worker)
            logger.info(f"Worker {i} started successfully")

        self._process_queue.start()
        self._initialized = True
        logger.info(f"BotManager initialized with {len(self.workers)} workers")

    async def assign_channel(self, channel_id: int, channel_name: str) -> WorkerBot | None:
        if channel_id in self.active_recordings:
            return self.active_recordings[channel_id]

        free_worker = None
        for w in self.workers:
            if not w.is_busy:
                free_worker = w
                break

        if not free_worker:
            self.queue.append({
                "channel_id": channel_id,
                "channel_name": channel_name,
                "queued_at": datetime.now(timezone.utc).replace(tzinfo=None),
                "expires_at": datetime.now(timezone.utc).replace(tzinfo=None),
            })
            logger.info(f"No free workers, queued {channel_name}")
            return None

        result = await free_worker.start_recording(channel_id, channel_name)
        if result.get("success"):
            self.active_recordings[channel_id] = free_worker
            logger.info(f"Assigned channel {channel_name} ({channel_id}) to worker {free_worker.worker_id}")
            return free_worker
        else:
            logger.error(f"Failed to start recording: {result.get('error')}")
            return None

    async def release_worker(self, worker: WorkerBot) -> dict[str, Any] | None:
        result = await worker.stop_recording()
        if worker._channel_id is None:
            for cid, w in list(self.active_recordings.items()):
                if w.worker_id == worker.worker_id:
                    del self.active_recordings[cid]
                    break
        else:
            self.active_recordings.pop(worker._channel_id, None)
        return result

    def get_worker_by_channel(self, channel_id: int) -> WorkerBot | None:
        return self.active_recordings.get(channel_id)

    def get_all_status(self) -> dict[str, Any]:
        workers_status = []
        for w in self.workers:
            status = {
                "worker_id": w.worker_id,
                "is_busy": w.is_busy,
                "channel_name": w._channel_name,
                "channel_id": w._channel_id,
                "is_recording": w.recorder.is_recording if w.recorder else False,
            }
            if w.recorder and w.recorder._start_time:
                elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - w.recorder._start_time).total_seconds()
                status["elapsed_seconds"] = int(elapsed)
            workers_status.append(status)

        return {
            "workers": workers_status,
            "queue_size": len(self.queue),
            "active_count": len(self.active_recordings),
        }

    @tasks.loop(seconds=30)
    async def _process_queue(self):
        if not self.queue:
            return

        free_workers = [w for w in self.workers if not w.is_busy]
        if not free_workers:
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.queue = [q for q in self.queue if (now - q["queued_at"]).total_seconds() < 1800]

        for queued in self.queue[:]:
            if not free_workers:
                break
            worker = free_workers.pop(0)
            result = await worker.start_recording(queued["channel_id"], queued["channel_name"])
            if result.get("success"):
                self.active_recordings[queued["channel_id"]] = worker
                self.queue.remove(queued)
                logger.info(f"Queue: assigned {queued['channel_name']} to worker {worker.worker_id}")

    async def shutdown(self):
        self._process_queue.stop()
        for w in self.workers:
            await w.stop()
        logger.info("BotManager shutdown complete")



