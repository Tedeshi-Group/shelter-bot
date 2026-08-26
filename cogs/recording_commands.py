import logging
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from sqlalchemy import func, select

from database import AsyncSessionLocal
from models.voice_recording import VoiceRecording, VoiceRecordingQueue
from cogs.bot_manager import BotManager
from s3_client import S3Client

logger = logging.getLogger(__name__)


class RecordingCommands(commands.Cog):
    """Commands for managing voice recordings."""

    def __init__(self, bot: commands.Bot, bot_manager: BotManager, s3_client: S3Client):
        self.bot = bot
        self.bot_manager = bot_manager
        self.s3_client = s3_client

    @app_commands.command(name="record", description="Управление записью голосовых каналов")
    @app_commands.describe(
        action="Действие",
        channel="Голосовой канал (для start/stop)",
        id="ID записи (для play/delete)",
        page="Страница (для list)",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="start", value="start"),
        app_commands.Choice(name="stop", value="stop"),
        app_commands.Choice(name="status", value="status"),
        app_commands.Choice(name="list", value="list"),
        app_commands.Choice(name="play", value="play"),
        app_commands.Choice(name="delete", value="delete"),
        app_commands.Choice(name="queue", value="queue"),
    ])
    async def record(
        self,
        interaction: discord.Interaction,
        action: str,
        channel: Optional[discord.VoiceChannel] = None,
        id: Optional[int] = None,
        page: Optional[int] = 1,
    ):
        # Check permissions
        if action in ["start", "stop", "delete"] and not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Только администраторы могут использовать эту команду.",
                ephemeral=True,
            )
            return

        if action == "start":
            await self._handle_start(interaction, channel)
        elif action == "stop":
            await self._handle_stop(interaction, channel)
        elif action == "status":
            await self._handle_status(interaction)
        elif action == "list":
            await self._handle_list(interaction, page)
        elif action == "play":
            await self._handle_play(interaction, id)
        elif action == "delete":
            await self._handle_delete(interaction, id)
        elif action == "queue":
            await self._handle_queue(interaction)

    async def _handle_start(self, interaction: discord.Interaction, channel: Optional[discord.VoiceChannel]):
        """Start recording a voice channel."""
        if not channel:
            # Try to get user's current voice channel
            if interaction.user.voice and interaction.user.voice.channel:
                channel = interaction.user.voice.channel
            else:
                await interaction.response.send_message(
                    "Укажите голосовой канал или подключитесь к нему.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer()

        # Check if already recording
        existing_worker = self.bot_manager.get_worker_by_channel(channel.id)
        if existing_worker:
            await interaction.followup.send(
                f"Канал **{channel.name}** уже записывается.",
                ephemeral=True,
            )
            return

        # Assign to worker
        worker = await self.bot_manager.assign_channel(channel)

        if worker:
            embed = discord.Embed(
                title="🔴 Запись начата",
                description=f"Канал: **{channel.name}**\nВоркер: {worker.worker_id}",
                color=discord.Color.red(),
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        else:
            embed = discord.Embed(
                title="⏳ Канал добавлен в очередь",
                description=f"Канал: **{channel.name}**\nВсе воркеры заняты, запись начнётся когда освободится.",
                color=discord.Color.orange(),
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)

    async def _handle_stop(self, interaction: discord.Interaction, channel: Optional[discord.VoiceChannel]):
        """Stop recording a voice channel."""
        if not channel:
            if interaction.user.voice and interaction.user.voice.channel:
                channel = interaction.user.voice.channel
            else:
                await interaction.response.send_message(
                    "Укажите голосовой канал.",
                    ephemeral=True,
                )
                return

        await interaction.response.defer()

        # Find worker recording this channel
        worker = self.bot_manager.get_worker_by_channel(channel.id)
        if not worker:
            await interaction.followup.send(
                f"Канал **{channel.name}** не записывается.",
                ephemeral=True,
            )
            return

        # Stop recording
        metadata = await self.bot_manager.release_worker(worker)

        if metadata:
            # Upload to S3
            from pathlib import Path
            import tempfile

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"worker-{worker.worker_id}_{channel.name}_{timestamp}.ogg"
            audio_path = Path(tempfile.gettempdir()) / "shelter_recordings" / filename

            if audio_path.exists():
                s3_result = await self.s3_client.upload_recording(
                    audio_path=audio_path,
                    worker_id=worker.worker_id,
                    channel_name=channel.name,
                    metadata=metadata,
                )

                if s3_result:
                    # Update database with S3 key
                    async with AsyncSessionLocal() as db:
                        recording = (await db.execute(
                            select(VoiceRecording)
                            .where(VoiceRecording.channel_id == channel.id)
                            .where(VoiceRecording.status == "completed")
                            .order_by(VoiceRecording.created_at.desc())
                            .limit(1)
                        )).scalar_one_or_none()

                        if recording:
                            recording.s3_key = s3_result["s3_key"]
                            await db.commit()

                    # Clean up local file
                    audio_path.unlink(missing_ok=True)

            duration = metadata.get("duration_seconds", 0)
            hours = duration // 3600
            minutes = (duration % 3600) // 60
            seconds = duration % 60

            embed = discord.Embed(
                title="⏹️ Запись остановлена",
                description=(
                    f"Канал: **{channel.name}**\n"
                    f"Длительность: {hours:02d}:{minutes:02d}:{seconds:02d}\n"
                    f"Участников: {len(metadata.get('participants', []))}"
                ),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            await interaction.followup.send(embed=embed)
        else:
            await interaction.followup.send(
                f"Ошибка при остановке записи канала **{channel.name}**.",
                ephemeral=True,
            )

    async def _handle_status(self, interaction: discord.Interaction):
        """Show status of all workers and queue."""
        status = self.bot_manager.get_status()

        embed = discord.Embed(
            title="📊 Статус оркестра записи",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )

        workers_text = []
        for worker in status["workers"]:
            if worker["is_busy"]:
                duration = worker.get("recording_duration_seconds", 0)
                minutes = duration // 60
                seconds = duration % 60
                workers_text.append(
                    f"🟢 Воркер {worker['worker_id']}: записывает "
                    f"**{worker['current_channel']}** ({minutes:02d}:{seconds:02d})"
                )
            else:
                workers_text.append(f"⚪ Воркер {worker['worker_id']}: свободен")

        embed.add_field(
            name="Воркеры",
            value="\n".join(workers_text) or "Нет воркеров",
            inline=False,
        )

        embed.add_field(
            name="Очередь",
            value=f"{status['queue_size']} каналов ожидают",
            inline=True,
        )

        embed.add_field(
            name="Загрузка",
            value=f"{status['busy_workers']}/{status['total_workers']} занято",
            inline=True,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _handle_list(self, interaction: discord.Interaction, page: int):
        """List recordings with pagination."""
        per_page = 10
        offset = (page - 1) * per_page

        async with AsyncSessionLocal() as db:
            # Get total count
            total = (await db.execute(
                select(func.count(VoiceRecording.id))
            )).scalar()

            # Get recordings
            recordings = (await db.execute(
                select(VoiceRecording)
                .order_by(VoiceRecording.created_at.desc())
                .offset(offset)
                .limit(per_page)
            )).scalars().all()

        if not recordings:
            await interaction.response.send_message(
                "Нет записей." if page == 1 else f"Нет записей на странице {page}.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"📋 Записи (страница {page})",
            color=discord.Color.blue(),
            timestamp=datetime.now(timezone.utc),
        )

        for rec in recordings:
            duration = rec.duration_seconds or 0
            hours = duration // 3600
            minutes = (duration % 3600) // 60

            status_emoji = {
                "recording": "🔴",
                "uploading": "⏳",
                "completed": "✅",
                "failed": "❌",
            }.get(rec.status, "❓")

            embed.add_field(
                name=f"{status_emoji} #{rec.id} - {rec.channel_name}",
                value=(
                    f"Дата: {rec.started_at.strftime('%d.%m.%Y %H:%M')}\n"
                    f"Длительность: {hours}ч {minutes}м\n"
                    f"Статус: {rec.status}"
                ),
                inline=True,
            )

        total_pages = (total + per_page - 1) // per_page
        embed.set_footer(text=f"Страница {page}/{total_pages} | Всего: {total}")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _handle_play(self, interaction: discord.Interaction, recording_id: Optional[int]):
        """Get a link to play a recording."""
        if not recording_id:
            await interaction.response.send_message(
                "Укажите ID записи.",
                ephemeral=True,
            )
            return

        async with AsyncSessionLocal() as db:
            recording = (await db.execute(
                select(VoiceRecording).where(VoiceRecording.id == recording_id)
            )).scalar_one_or_none()

        if not recording:
            await interaction.response.send_message(
                f"Запись #{recording_id} не найдена.",
                ephemeral=True,
            )
            return

        if recording.status != "completed":
            await interaction.response.send_message(
                f"Запись #{recording_id} ещё не завершена (статус: {recording.status}).",
                ephemeral=True,
            )
            return

        if not recording.s3_key:
            await interaction.response.send_message(
                f"Запись #{recording_id} не загружена на S3.",
                ephemeral=True,
            )
            return

        # Generate presigned URL
        url = self.s3_client.get_presigned_url(recording.s3_key, expiration=3600)

        if url:
            embed = discord.Embed(
                title=f"🎵 Запись #{recording_id}",
                description=(
                    f"Канал: **{recording.channel_name}**\n"
                    f"Дата: {recording.started_at.strftime('%d.%m.%Y %H:%M')}\n"
                    f"Длительность: {recording.duration_seconds // 60}м {recording.duration_seconds % 60}с"
                ),
                color=discord.Color.green(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.add_field(
                name="Ссылка",
                value=f"[Скачать запись]({url})\nСсылка действительна 1 час.",
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(
                "Не удалось生成 ссылку на запись.",
                ephemeral=True,
            )

    async def _handle_delete(self, interaction: discord.Interaction, recording_id: Optional[int]):
        """Delete a recording."""
        if not recording_id:
            await interaction.response.send_message(
                "Укажите ID записи.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        async with AsyncSessionLocal() as db:
            recording = (await db.execute(
                select(VoiceRecording).where(VoiceRecording.id == recording_id)
            )).scalar_one_or_none()

            if not recording:
                await interaction.followup.send(
                    f"Запись #{recording_id} не найдена.",
                    ephemeral=True,
                )
                return

            # Delete from S3 if exists
            if recording.s3_key:
                await self.s3_client.delete_recording(recording.s3_key)

            # Delete from database
            await db.delete(recording)
            await db.commit()

        embed = discord.Embed(
            title="🗑️ Запись удалена",
            description=f"Запись #{recording_id} ({recording.channel_name}) удалена.",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _handle_queue(self, interaction: discord.Interaction):
        """Show the recording queue."""
        async with AsyncSessionLocal() as db:
            queue_entries = (await db.execute(
                select(VoiceRecordingQueue)
                .where(VoiceRecordingQueue.status == "waiting")
                .order_by(VoiceRecordingQueue.queued_at)
            )).scalars().all()

        if not queue_entries:
            await interaction.response.send_message(
                "Очередь пуста.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📋 Очередь записи",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )

        for i, entry in enumerate(queue_entries, 1):
            wait_time = (datetime.now(timezone.utc).replace(tzinfo=None) - entry.queued_at).total_seconds()
            minutes = int(wait_time // 60)

            embed.add_field(
                name=f"{i}. {entry.channel_name}",
                value=f"Ожидает: {minutes} мин\nID канала: {entry.channel_id}",
                inline=True,
            )

        embed.set_footer(text=f"В очереди: {len(queue_entries)} каналов")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    """Load the recording commands cog."""
    # This will be called from bot.py with the bot_manager and s3_client
    pass
