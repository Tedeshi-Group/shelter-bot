"""Recording commands cog — /record slash commands."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from database import SessionLocal
from models.voice_recording import VoiceRecording, VoiceRecordingTrack

if TYPE_CHECKING:
    from cogs.bot_manager import BotManager

logger = logging.getLogger(__name__)


class RecordingCommands(commands.Cog):
    """Voice recording management commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @property
    def bot_manager(self) -> "BotManager | None":
        return getattr(self.bot, 'bot_manager', None)

    @commands.slash_command(name="record", description="Voice recording management")
    @commands.has_permissions(administrator=True)
    async def record(self, ctx: discord.ApplicationContext):
        pass

    @record.command(name="start", description="Start recording in a voice channel")
    @commands.has_permissions(administrator=True)
    async def record_start(self, ctx: discord.ApplicationContext, channel: discord.VoiceChannel):
        if not self.bot_manager:
            await ctx.respond("BotManager not initialized", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)

        worker = await self.bot_manager.assign_channel(channel.id, channel.name)
        if worker:
            await ctx.respond(
                f"Recording started in {channel.name} (Worker {worker.worker_id})",
                ephemeral=True
            )
        else:
            await ctx.respond(
                f"All workers busy. Channel {channel.name} added to queue.",
                ephemeral=True
            )

    @record.command(name="stop", description="Stop recording in a voice channel")
    @commands.has_permissions(administrator=True)
    async def record_stop(self, ctx: discord.ApplicationContext, channel: discord.VoiceChannel):
        if not self.bot_manager:
            await ctx.respond("BotManager not initialized", ephemeral=True)
            return

        worker = self.bot_manager.get_worker_by_channel(channel.id)
        if not worker:
            await ctx.respond(f"No active recording in {channel.name}", ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        result = await self.bot_manager.release_worker(worker)

        if result:
            tracks = result.get("tracks_count", 0)
            size_kb = result.get("total_size", 0) / 1024
            await ctx.respond(
                f"Recording stopped in {channel.name}\n"
                f"Duration: {result['duration']}s\n"
                f"Tracks: {tracks} speakers\n"
                f"Size: {size_kb:.1f} KB",
                ephemeral=True
            )
        else:
            await ctx.respond("Error stopping recording", ephemeral=True)

    @record.command(name="status", description="Show recording status")
    async def record_status(self, ctx: discord.ApplicationContext):
        if not self.bot_manager:
            await ctx.respond("BotManager not initialized", ephemeral=True)
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

        await ctx.respond(embed=embed, ephemeral=True)

    @record.command(name="list", description="List recent recordings")
    async def record_list(self, ctx: discord.ApplicationContext):
        with SessionLocal() as db:
            recordings = (
                db.query(VoiceRecording)
                .order_by(VoiceRecording.created_at.desc())
                .limit(10)
                .all()
            )

            if not recordings:
                await ctx.respond("No recordings found", ephemeral=True)
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

            await ctx.respond(embed=embed, ephemeral=True)

    @record.command(name="play", description="Get download links for a recording")
    async def record_play(self, ctx: discord.ApplicationContext, recording_id: int):
        with SessionLocal() as db:
            recording = db.get(VoiceRecording, recording_id)
            if not recording:
                await ctx.respond(f"Recording #{recording_id} not found", ephemeral=True)
                return

            tracks = db.query(VoiceRecordingTrack).filter(
                VoiceRecordingTrack.recording_id == recording_id
            ).all()

            if not tracks:
                await ctx.respond(f"No tracks found for recording #{recording_id}", ephemeral=True)
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

            await ctx.respond(embed=embed, ephemeral=True)

    @record.command(name="delete", description="Delete a recording")
    @commands.has_permissions(administrator=True)
    async def record_delete(self, ctx: discord.ApplicationContext, recording_id: int):
        with SessionLocal() as db:
            recording = db.get(VoiceRecording, recording_id)
            if not recording:
                await ctx.respond(f"Recording #{recording_id} not found", ephemeral=True)
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

            await ctx.respond(f"Recording #{recording_id} deleted", ephemeral=True)


def setup(bot: commands.Bot):
    bot.add_cog(RecordingCommands(bot))
