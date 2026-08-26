"""Recording commands cog — /record slash commands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from database import SessionLocal
from models.voice_recording import VoiceRecording, VoiceRecordingTrack

if TYPE_CHECKING:
    from cogs.bot_manager import BotManager

logger = logging.getLogger(__name__)


class RecordingCommands(commands.Cog):
    """Voice recording management commands."""

    def __init__(self, bot: commands.Bot, bot_manager=None, s3_client=None):
        self.bot = bot
        self._bot_manager = bot_manager
        self._s3_client = s3_client

    @property
    def bot_manager(self) -> "BotManager | None":
        return self._bot_manager or getattr(self.bot, 'bot_manager', None)

    @app_commands.command(name="record_start", description="Start recording in a voice channel")
    @app_commands.describe(channel="Voice channel to record")
    @app_commands.checks.has_permissions(administrator=True)
    async def record_start(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        if not self.bot_manager:
            await interaction.response.send_message("BotManager not initialized", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        worker = await self.bot_manager.assign_channel(channel.id, channel.name)
        if worker:
            await interaction.followup.send(
                f"Recording started in {channel.name} (Worker {worker.worker_id})",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"All workers busy. Channel {channel.name} added to queue.",
                ephemeral=True
            )

    @app_commands.command(name="record_stop", description="Stop recording in a voice channel")
    @app_commands.describe(channel="Voice channel to stop recording")
    @app_commands.checks.has_permissions(administrator=True)
    async def record_stop(self, interaction: discord.Interaction, channel: discord.VoiceChannel):
        if not self.bot_manager:
            await interaction.response.send_message("BotManager not initialized", ephemeral=True)
            return

        worker = self.bot_manager.get_worker_by_channel(channel.id)
        if not worker:
            await interaction.response.send_message(
                f"No active recording in {channel.name}", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        result = await self.bot_manager.release_worker(worker)

        if result:
            tracks = result.get("tracks_count", 0)
            size_kb = result.get("total_size", 0) / 1024
            await interaction.followup.send(
                f"Recording stopped in {channel.name}\n"
                f"Duration: {result['duration']}s\n"
                f"Tracks: {tracks} speakers\n"
                f"Size: {size_kb:.1f} KB",
                ephemeral=True
            )
        else:
            await interaction.followup.send("Error stopping recording", ephemeral=True)

    @app_commands.command(name="record_status", description="Show recording status")
    async def record_status(self, interaction: discord.Interaction):
        if not self.bot_manager:
            await interaction.response.send_message("BotManager not initialized", ephemeral=True)
            return

        status = self.bot_manager.get_all_status()

        embed = discord.Embed(title="Recording Status", color=discord.Color.blue())

        workers_text = []
        for w in status["workers"]:
            if w["is_busy"]:
                elapsed = w.get("elapsed_seconds", 0)
                m, s = divmod(elapsed, 60)
                workers_text.append(
                    f"🔴 Worker {w['worker_id']}: recording **{w['channel_name']}** ({m:02d}:{s:02d})"
                )
            else:
                workers_text.append(f"⚪ Worker {w['worker_id']}: idle")

        embed.add_field(name="Workers", value="\n".join(workers_text), inline=False)
        embed.add_field(
            name="Queue",
            value=f"{status['queue_size']} channels waiting",
            inline=True
        )
        embed.add_field(
            name="Active",
            value=f"{status['active_count']}/{len(status['workers'])}",
            inline=True
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="record_list", description="List recent recordings")
    async def record_list(self, interaction: discord.Interaction):
        with SessionLocal() as db:
            recordings = (
                db.query(VoiceRecording)
                .order_by(VoiceRecording.created_at.desc())
                .limit(10)
                .all()
            )

            if not recordings:
                await interaction.response.send_message("No recordings found", ephemeral=True)
                return

            embed = discord.Embed(title="Recent Recordings", color=discord.Color.green())

            for r in recordings:
                dur = r.duration_seconds or 0
                m, s = divmod(dur, 60)
                size_kb = (r.file_size_bytes or 0) / 1024

                tracks = db.query(VoiceRecordingTrack).filter(
                    VoiceRecordingTrack.recording_id == r.id
                ).all()
                track_names = ", ".join(t.username for t in tracks) if tracks else "no tracks"

                embed.add_field(
                    name=f"#{r.id} — {r.channel_name}",
                    value=(
                        f"Duration: {m:02d}:{s:02d}\n"
                        f"Tracks: {track_names}\n"
                        f"Size: {size_kb:.1f} KB\n"
                        f"Status: {r.status}"
                    ),
                    inline=False
                )

            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="record_play", description="Get download links for a recording")
    @app_commands.describe(recording_id="Recording ID")
    async def record_play(self, interaction: discord.Interaction, recording_id: int):
        with SessionLocal() as db:
            recording = db.get(VoiceRecording, recording_id)
            if not recording:
                await interaction.response.send_message(
                    f"Recording #{recording_id} not found", ephemeral=True
                )
                return

            tracks = db.query(VoiceRecordingTrack).filter(
                VoiceRecordingTrack.recording_id == recording_id
            ).all()

            if not tracks:
                await interaction.response.send_message(
                    f"No tracks found for recording #{recording_id}", ephemeral=True
                )
                return

            from s3_client import S3Client
            s3 = S3Client()

            embed = discord.Embed(
                title=f"Recording #{recording_id} — {recording.channel_name}",
                color=discord.Color.green()
            )

            for track in tracks:
                url = s3.get_presigned_url(track.s3_key, expiration=3600)
                dur = track.duration_seconds or 0
                m, s = divmod(dur, 60)
                size_kb = (track.file_size_bytes or 0) / 1024

                embed.add_field(
                    name=track.username,
                    value=f"[Download WAV]({url})\n{m:02d}:{s:02d} | {size_kb:.1f} KB" if url else "Link expired",
                    inline=True
                )

            await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="record_delete", description="Delete a recording")
    @app_commands.describe(recording_id="Recording ID")
    @app_commands.checks.has_permissions(administrator=True)
    async def record_delete(self, interaction: discord.Interaction, recording_id: int):
        with SessionLocal() as db:
            recording = db.get(VoiceRecording, recording_id)
            if not recording:
                await interaction.response.send_message(
                    f"Recording #{recording_id} not found", ephemeral=True
                )
                return

            tracks = db.query(VoiceRecordingTrack).filter(
                VoiceRecordingTrack.recording_id == recording_id
            ).all()

            from s3_client import S3Client
            s3 = S3Client()

            for track in tracks:
                if track.s3_key:
                    await s3.delete_recording(track.s3_key)

            for track in tracks:
                db.delete(track)
            db.delete(recording)
            db.commit()

            await interaction.response.send_message(
                f"Recording #{recording_id} deleted", ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(RecordingCommands(bot))
