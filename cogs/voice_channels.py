import asyncio
import re

import discord
from discord.ext import commands

VOICE_CATEGORY_ID = 1517577490368041200
ARCHIVE_CHANNEL_ID = 1517446069192102003
VOICE_CHANNEL_PREFIX = "Голосовой #"
NEW_VOICE_NAME = "Новый войс"
EMPTY_TIMEOUT_SECONDS = 10

REGION_OPTIONS = [
    discord.SelectOption(label="Brazil", value="brazil", emoji="🇧🇷"),
    discord.SelectOption(label="Hong Kong", value="hongkong", emoji="🇭🇰"),
    discord.SelectOption(label="India", value="india", emoji="🇮🇳"),
    discord.SelectOption(label="Japan", value="japan", emoji="🇯🇵"),
    discord.SelectOption(label="Rotterdam", value="rotterdam", emoji="🇳🇱"),
    discord.SelectOption(label="Russia", value="russia", emoji="🇷🇺"),
    discord.SelectOption(label="Singapore", value="singapore", emoji="🇸🇬"),
    discord.SelectOption(label="South Africa", value="southafrica", emoji="🇿🇦"),
    discord.SelectOption(label="South Korea", value="southkorea", emoji="🇰🇷"),
    discord.SelectOption(label="Sydney", value="sydney", emoji="🇦🇺"),
    discord.SelectOption(label="US Central", value="us-central", emoji="🇺🇸"),
    discord.SelectOption(label="US East", value="us-east", emoji="🇺🇸"),
    discord.SelectOption(label="US South", value="us-south", emoji="🇺🇸"),
    discord.SelectOption(label="US West", value="us-west", emoji="🇺🇸"),
]


class RegionSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Выберите регион войса",
            options=REGION_OPTIONS,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("Это меню работает только в голосовых каналах.", ephemeral=True)
            return

        if interaction.user not in channel.members:
            await interaction.response.send_message("Вы должны быть в этом войсе чтобы менять регион.", ephemeral=True)
            return

        region = self.values[0]
        try:
            await channel.edit(rtc_region=region)
            await interaction.response.send_message(f"Регион изменён на **{region}**.", ephemeral=True)
        except Exception:
            await interaction.response.send_message("Не удалось изменить регион.", ephemeral=True)


class RegionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RegionSelect())


class VoiceChannels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_timers: dict[int, asyncio.Task] = {}

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_new_voice_exists()

    async def _ensure_new_voice_exists(self):
        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild:
            return

        try:
            category = await guild.fetch_channel(VOICE_CATEGORY_ID)
        except (discord.NotFound, discord.HTTPException):
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

                if after.channel is None or after.channel.id != before.channel.id:
                    await self._log_event(before.channel, f"🚪 **{member.display_name}** вышел из войса")

        if after.channel and after.channel.category_id == VOICE_CATEGORY_ID:
            if after.channel.name == NEW_VOICE_NAME:
                await self._handle_new_voice_join(after.channel)
            elif after.channel.name.startswith(VOICE_CHANNEL_PREFIX):
                if before.channel is None or before.channel.id != after.channel.id:
                    await self._log_event(after.channel, f"🚪 **{member.display_name}** вошёл в войс")

                if before.channel and before.channel.id == after.channel.id:
                    if before.rtc_region != after.rtc_region:
                        region_name = after.rtc_region or "auto"
                        await self._log_event(after.channel, f"🌍 Регион изменён на **{region_name}** (пользователь: **{member.display_name}**)")

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
                type=discord.ChannelType.public_thread
            )
            self._voice_threads[channel.id] = thread.id

        thread_id = self._voice_threads[channel.id]
        thread = archive_channel.get_thread(thread_id)

        if not thread:
            thread = await archive_channel.create_thread(
                name=channel.name,
                type=discord.ChannelType.public_thread
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

        max_number = 0
        for ch in category.voice_channels:
            match = re.match(r"Голосовой #(\d+)", ch.name)
            if match:
                num = int(match.group(1))
                if num > max_number:
                    max_number = num

        new_name = f"{VOICE_CHANNEL_PREFIX}{max_number + 1}"

        await channel.edit(name=new_name, user_limit=None)
        await category.create_voice_channel(NEW_VOICE_NAME, user_limit=1)

        embed = discord.Embed(
            title="Выбор региона",
            description="Используйте меню ниже чтобы изменить регион войса.",
            color=discord.Color.green(),
        )
        await channel.send(embed=embed, view=RegionView())

    async def _log_event(self, channel: discord.VoiceChannel, text: str):
        if not hasattr(self, '_voice_threads'):
            return

        thread_id = self._voice_threads.get(channel.id)
        if not thread_id:
            return

        archive_channel = self.bot.get_channel(ARCHIVE_CHANNEL_ID)
        if not archive_channel:
            return

        thread = archive_channel.get_thread(thread_id)
        if not thread:
            return

        try:
            await thread.send(text)
        except (discord.NotFound, discord.HTTPException):
            pass

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
