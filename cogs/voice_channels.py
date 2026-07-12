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


def _build_deaf_options(channel: discord.VoiceChannel, invoker: discord.Member) -> list[discord.SelectOption]:
    options = [discord.SelectOption(label="ВСЕХ", value="__all__", emoji="🔇")]
    for member in channel.members:
        if member.bot:
            continue
        status = "🔇" if member.voice.deaf else "🔊"
        options.append(discord.SelectOption(
            label=member.display_name,
            value=str(member.id),
            emoji=status,
        ))
    return options


class DeafSelect(discord.ui.Select):
    def __init__(self, channel: discord.VoiceChannel, invoker: discord.Member, cog: "VoiceChannels"):
        super().__init__(
            placeholder="Кого отключить от звука?",
            options=_build_deaf_options(channel, invoker),
            min_values=1,
            max_values=1,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("Это меню работает только в голосовых каналах.", ephemeral=True)
            return

        if not (interaction.user.guild_permissions.manage_channels or interaction.user.guild_permissions.administrator):
            await interaction.response.send_message("У вас нет прав для управления звуком.", ephemeral=True)
            return

        target_id = self.values[0]
        ch_id = channel.id

        if ch_id not in self.cog.deaf_states:
            self.cog.deaf_states[ch_id] = set()

        if target_id == "__all__":
            humans = [m for m in channel.members if not m.bot]
            should_deaf = any(m.id not in self.cog.deaf_states[ch_id] for m in humans)
            for member in humans:
                await member.edit(deafen=should_deaf)
                if should_deaf:
                    self.cog.deaf_states[ch_id].add(member.id)
                else:
                    self.cog.deaf_states[ch_id].discard(member.id)
            state = "отключён" if should_deaf else "включён"
            await interaction.response.send_message(f"Звук **{state}** для всех участников.", ephemeral=True)
        else:
            member = channel.guild.get_member(int(target_id))
            if not member:
                await interaction.response.send_message("Участник не найден.", ephemeral=True)
                return
            is_deafned = member.id in self.cog.deaf_states[ch_id]
            await member.edit(deafen=not is_deafned)
            if not is_deafned:
                self.cog.deaf_states[ch_id].add(member.id)
            else:
                self.cog.deaf_states[ch_id].discard(member.id)
            state = "отключён" if not is_deafned else "включён"
            await interaction.response.send_message(f"Звук **{state}** для **{member.display_name}**.", ephemeral=True)

        self.options = _build_deaf_options(channel, interaction.user)
        await interaction.message.edit(view=self.view)


class DeafView(discord.ui.View):
    def __init__(self, channel: discord.VoiceChannel, invoker: discord.Member, cog: "VoiceChannels"):
        super().__init__(timeout=None)
        self.add_item(DeafSelect(channel, invoker, cog))


class VoiceChannels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_timers: dict[int, asyncio.Task] = {}
        self.deaf_states: dict[int, set[int]] = {}

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
            if channel.name != NEW_VOICE_NAME and not channel.members:
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
            if before.channel.name == NEW_VOICE_NAME:
                return

            if not before.channel.members:
                self._start_deletion_timer(before.channel)
            elif before.channel.id in self.active_timers:
                self._cancel_deletion_timer(before.channel.id)

        if after.channel and after.channel.category_id == VOICE_CATEGORY_ID:
            if after.channel.name == NEW_VOICE_NAME:
                await self._handle_new_voice_join(after.channel, member)

            ch_id = after.channel.id
            if ch_id in self.deaf_states and member.id in self.deaf_states[ch_id]:
                try:
                    await member.edit(deafen=True)
                except (discord.HTTPException, discord.Forbidden):
                    pass

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

    async def _handle_new_voice_join(self, channel: discord.VoiceChannel, creator: discord.Member):
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

        overwrite = discord.PermissionOverwrite(
            manage_channels=True,
            move_members=True,
            kick_members=True,
        )
        await channel.set_permissions(creator, overwrite=overwrite)

        embed = discord.Embed(
            description="Используйте меню ниже чтобы изменить регион войса.",
            color=discord.Color.green(),
        )
        await channel.send(embed=embed, view=RegionView())

        deaf_embed = discord.Embed(
            description="Отключите звук участнику войса.",
            color=discord.Color.orange(),
        )
        await channel.send(embed=deaf_embed, view=DeafView(channel, creator, self))

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
        self.deaf_states.pop(channel.id, None)
        self.active_timers.pop(channel.id, None)



async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceChannels(bot))
