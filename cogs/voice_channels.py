import asyncio
from datetime import datetime

import discord
from discord.ext import commands

from database import SessionLocal
from models.voice_counter import VoiceCounter

VOICE_CATEGORY_ID = 1517577490368041200
ARCHIVE_CHANNEL_ID = 1429769594037600267
VOICE_CHANNEL_PREFIX = "голосовой #"
NEW_VOICE_NAME = "новый войс"
EMPTY_TIMEOUT_SECONDS = 10


class VoiceChannels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_timers: dict[int, asyncio.Task] = {}

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_new_voice_exists()
        self._restore_deletion_timers()

    def _restore_deletion_timers(self):
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild:
            return

        try:
            category = guild.get_channel(VOICE_CATEGORY_ID)
        except (discord.NotFound, discord.HTTPException):
            return

        if not category:
            return

        for channel in category.voice_channels:
            if channel.name.startswith(VOICE_CHANNEL_PREFIX) and not channel.members:
                self._start_deletion_timer(channel)

    async def _ensure_new_voice_exists(self):
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild:
            return

        category = guild.get_channel(VOICE_CATEGORY_ID)
        if not category:
            return

        for channel in category.voice_channels:
            if channel.name == NEW_VOICE_NAME:
                return

        await category.create_voice_channel(NEW_VOICE_NAME, user_limit=1)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return

        if before.channel and before.channel.category_id == VOICE_CATEGORY_ID:
            if before.channel.name == NEW_VOICE_NAME and not before.channel.members:
                return

            if before.channel.name.startswith(VOICE_CHANNEL_PREFIX):
                if not before.channel.members:
                    self._start_deletion_timer(before.channel)
                elif before.channel.id in self.active_timers:
                    self._cancel_deletion_timer(before.channel.id)

        if after.channel and after.channel.category_id == VOICE_CATEGORY_ID:
            if after.channel.name == NEW_VOICE_NAME:
                await self._handle_new_voice_join(after.channel)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        channel = message.channel
        if not isinstance(channel, discord.VoiceChannel):
            return

        if channel.category_id != VOICE_CATEGORY_ID:
            return

        if not channel.name.startswith(VOICE_CHANNEL_PREFIX):
            return

        archive_channel = self.bot.get_channel(ARCHIVE_CHANNEL_ID)
        if not archive_channel:
            return

        if not hasattr(self, '_voice_threads'):
            self._voice_threads = {}

        if channel.id not in self._voice_threads:
            thread = await archive_channel.create_thread(
                name=channel.name,
                type=discord.ChannelType.private_thread
            )
            self._voice_threads[channel.id] = thread.id

        thread_id = self._voice_threads[channel.id]
        thread = archive_channel.get_thread(thread_id)

        if not thread:
            thread = await archive_channel.create_thread(
                name=channel.name,
                type=discord.ChannelType.private_thread
            )
            self._voice_threads[channel.id] = thread.id

        try:
            embed = discord.Embed(
                description=message.content or "",
                color=discord.Color.greyple(),
            )
            embed.set_author(
                name=message.author.display_name,
                icon_url=message.author.display_avatar.url,
            )
            files = [await a.to_file() for a in message.attachments]
            await thread.send(embed=embed, files=files)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def _handle_new_voice_join(self, channel: discord.VoiceChannel):
        category = channel.category
        if not category:
            return

        now = datetime.utcnow()
        session = SessionLocal()
        try:
            counter = session.query(VoiceCounter).filter_by(
                month=now.month, year=now.year
            ).first()
            if not counter:
                counter = VoiceCounter(month=now.month, year=now.year, count=0)
                session.add(counter)
            counter.count += 1
            session.commit()
            new_name = f"{VOICE_CHANNEL_PREFIX}{counter.count}"
        finally:
            session.close()

        await channel.edit(name=new_name, user_limit=None)
        await category.create_voice_channel(NEW_VOICE_NAME, user_limit=1)

    def _start_deletion_timer(self, channel: discord.VoiceChannel):
        task = asyncio.create_task(self._delete_after_timeout(channel))
        self.active_timers[channel.id] = task

    def _cancel_deletion_timer(self, channel_id: int):
        if channel_id in self.active_timers:
            self.active_timers[channel_id].cancel()
            del self.active_timers[channel_id]

    async def _delete_after_timeout(self, channel: discord.VoiceChannel):
        await asyncio.sleep(EMPTY_TIMEOUT_SECONDS)

        if channel.members:
            return

        if hasattr(self, '_voice_threads') and channel.id in self._voice_threads:
            del self._voice_threads[channel.id]

        try:
            await channel.delete(reason="Voice channel empty for 10 seconds")
        except discord.NotFound:
            pass
        self.active_timers.pop(channel.id, None)



async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceChannels(bot))
