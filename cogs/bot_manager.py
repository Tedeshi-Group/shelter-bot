import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

import discord
from sqlalchemy import select

from database import AsyncSessionLocal
from models.voice_recording import VoiceRecording, VoiceRecordingParticipant, VoiceRecordingQueue
from cogs.voice_recording import WorkerBot

logger = logging.getLogger(__name__)


class BotManager:
    """Manages a pool of worker bots for voice recording."""

    def __init__(self, max_workers: int = 5, max_queue_size: int = 10):
        self.workers: list[WorkerBot] = []
        self.queue: asyncio.Queue[VoiceChannel] = asyncio.Queue(maxsize=max_queue_size)
        self.current_index: int = 0
        self.max_workers = max_workers
        self.max_queue_size = max_queue_size
        self._initialized = False

    async def initialize(self):
        """Initialize worker bots from environment tokens."""
        if self._initialized:
            return

        # Load worker tokens from environment
        tokens = []
        for i in range(1, self.max_workers + 1):
            token = os.getenv(f"VOICE_WORKER_TOKEN_{i}")
            if token:
                tokens.append(token)

        if not tokens:
            logger.warning("No worker tokens found in environment")
            return

        # Create and start worker bots
        for i, token in enumerate(tokens, 1):
            worker = WorkerBot(token, worker_id=i)
            try:
                await worker.start()
                self.workers.append(worker)
                logger.info(f"Worker {i} started successfully")
            except Exception as e:
                logger.error(f"Failed to start worker {i}: {e}")

        self._initialized = True
        logger.info(f"BotManager initialized with {len(self.workers)} workers")

    async def shutdown(self):
        """Shutdown all worker bots."""
        for worker in self.workers:
            try:
                await worker.stop()
            except Exception as e:
                logger.error(f"Error stopping worker {worker.worker_id}: {e}")
        self.workers.clear()
        self._initialized = False

    async def assign_channel(self, channel: discord.VoiceChannel) -> WorkerBot | None:
        """
        Assign a voice channel to a free worker using round-robin.
        Returns the assigned worker or None if all are busy (channel queued).
        """
        # Find free workers
        free_workers = [w for w in self.workers if not w.is_busy]

        if free_workers:
            # Round-robin selection
            worker = free_workers[self.current_index % len(free_workers)]
            self.current_index += 1

            # Start recording
            success = await worker.start_recording(channel)
            if success:
                logger.info(f"Assigned channel {channel.name} to worker {worker.worker_id}")
                return worker
            else:
                logger.error(f"Failed to start recording on worker {worker.worker_id}")
                # Try next worker
                return await self._try_next_worker(channel, free_workers)

        # All workers busy - add to queue
        logger.info(f"All workers busy, queuing channel {channel.name}")
        await self._add_to_queue(channel)
        return None

    async def _try_next_worker(
        self, channel: discord.VoiceChannel, free_workers: list[WorkerBot]
    ) -> WorkerBot | None:
        """Try remaining free workers after a failure."""
        for worker in free_workers:
            if worker.is_busy:
                continue
            success = await worker.start_recording(channel)
            if success:
                logger.info(f"Assigned channel {channel.name} to worker {worker.worker_id} (retry)")
                return worker
        return None

    async def release_worker(self, worker: WorkerBot) -> dict[str, Any] | None:
        """
        Release a worker and return recording metadata.
        Also processes the queue to assign next waiting channel.
        """
        metadata = await worker.stop_recording()

        if metadata:
            # Save to database
            await self._save_recording(worker.worker_id, metadata)

        # Process queue
        await self._process_queue()

        return metadata

    async def _add_to_queue(self, channel: discord.VoiceChannel):
        """Add a channel to the recording queue."""
        try:
            async with AsyncSessionLocal() as db:
                expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)
                queue_entry = VoiceRecordingQueue(
                    channel_id=channel.id,
                    channel_name=channel.name,
                    expires_at=expires_at,
                )
                db.add(queue_entry)
                await db.commit()

            # Also add to in-memory queue
            try:
                self.queue.put_nowait(channel)
            except asyncio.QueueFull:
                logger.warning(f"Queue is full, dropping channel {channel.name}")

        except Exception as e:
            logger.error(f"Failed to add channel to queue: {e}")

    async def _process_queue(self):
        """Process the queue and assign waiting channels to free workers."""
        while not self.queue.empty():
            # Find free worker
            free_worker = None
            for worker in self.workers:
                if not worker.is_busy:
                    free_worker = worker
                    break

            if not free_worker:
                break

            # Get next channel from queue
            try:
                channel = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            # Check if channel still exists and has members
            if not channel.members:
                logger.info(f"Channel {channel.name} is empty, removing from queue")
                continue

            # Start recording
            success = await free_worker.start_recording(channel)
            if success:
                logger.info(f"Assigned queued channel {channel.name} to worker {free_worker.worker_id}")
                # Update queue entry in DB
                await self._update_queue_status(channel.id, "assigned", free_worker.worker_id)
            else:
                logger.error(f"Failed to start recording for queued channel {channel.name}")
                await self._update_queue_status(channel.id, "failed")

    async def _update_queue_status(
        self, channel_id: int, status: str, worker_id: int | None = None
    ):
        """Update queue entry status in database."""
        try:
            async with AsyncSessionLocal() as db:
                entry = (await db.execute(
                    select(VoiceRecordingQueue)
                    .where(VoiceRecordingQueue.channel_id == channel_id)
                    .where(VoiceRecordingQueue.status == "waiting")
                    .order_by(VoiceRecordingQueue.queued_at.desc())
                    .limit(1)
                )).scalar_one_or_none()

                if entry:
                    entry.status = status
                    if worker_id:
                        entry.assigned_worker_id = worker_id
                    await db.commit()
        except Exception as e:
            logger.error(f"Failed to update queue status: {e}")

    async def _save_recording(self, worker_id: int, metadata: dict[str, Any]):
        """Save recording metadata to database."""
        try:
            async with AsyncSessionLocal() as db:
                recording = VoiceRecording(
                    channel_id=metadata["channel_id"],
                    channel_name=metadata["channel_name"],
                    worker_bot_id=worker_id,
                    started_at=datetime.fromisoformat(metadata["started_at"]),
                    ended_at=datetime.fromisoformat(metadata["ended_at"]),
                    duration_seconds=metadata["duration_seconds"],
                    file_size_bytes=metadata["file_size_bytes"],
                    status="completed",
                )
                db.add(recording)
                await db.flush()

                # Save participants
                for participant in metadata.get("participants", []):
                    db.add(VoiceRecordingParticipant(
                        recording_id=recording.id,
                        user_discord_id=participant["discord_id"],
                        username=participant["username"],
                        joined_at=datetime.fromisoformat(participant["joined_at"]),
                        left_at=datetime.fromisoformat(participant["left_at"]) if participant["left_at"] else None,
                        duration_seconds=participant.get("duration_seconds"),
                    ))

                await db.commit()
                logger.info(f"Saved recording {recording.id} to database")

        except Exception as e:
            logger.error(f"Failed to save recording to database: {e}")

    def get_status(self) -> dict[str, Any]:
        """Get status of all workers and queue."""
        return {
            "workers": [w.get_status() for w in self.workers],
            "queue_size": self.queue.qsize(),
            "total_workers": len(self.workers),
            "busy_workers": sum(1 for w in self.workers if w.is_busy),
            "free_workers": sum(1 for w in self.workers if not w.is_busy),
        }

    def get_worker_by_channel(self, channel_id: int) -> WorkerBot | None:
        """Find worker recording a specific channel."""
        for worker in self.workers:
            if worker.current_channel and worker.current_channel.id == channel_id:
                return worker
        return None

    async def stop_recording_channel(self, channel_id: int) -> dict[str, Any] | None:
        """Stop recording a specific channel."""
        worker = self.get_worker_by_channel(channel_id)
        if worker:
            return await self.release_worker(worker)
        return None
