import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands
from sqlalchemy import select, text

from achievements import check_achievements
from database import AsyncSessionLocal
from models import MessageCounter, User, VoiceSession

logger = logging.getLogger(__name__)

VOICE_CATEGORY_ID = 1517577490368041200
ARCHIVE_CHANNEL_ID = 1429769594037600267
LOG_CHANNEL_ID = 1517446069192102003
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


class DeafToggleButton(discord.ui.Button):
    def __init__(self, cog: "VoiceChannels"):
        super().__init__(
            label="Переключить мне звук",
            style=discord.ButtonStyle.danger,
            emoji="🔇",
            custom_id="voice_deaf_toggle",
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction):
        channel = interaction.channel
        if not isinstance(channel, discord.VoiceChannel):
            await interaction.response.send_message("Эта кнопка работает только в голосовых каналах.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or member.voice.channel is None:
            await interaction.response.send_message("Вы должны быть в голосовом канале.", ephemeral=True)
            return

        ch_id = channel.id
        if ch_id not in self.cog.deaf_states:
            self.cog.deaf_states[ch_id] = set()

        is_deafned = member.voice.deaf
        await member.edit(deafen=not is_deafned)

        if is_deafned:
            self.cog.deaf_states[ch_id].discard(member.id)
        else:
            self.cog.deaf_states[ch_id].add(member.id)

        state = "отключён" if not is_deafned else "включён"
        await interaction.response.send_message(f"Ваш звук **{state}**.", ephemeral=True)


class DeafView(discord.ui.View):
    def __init__(self, cog: "VoiceChannels"):
        super().__init__(timeout=None)
        self.add_item(DeafToggleButton(cog))


class VoiceChannels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_timers: dict[int, asyncio.Task] = {}
        self.deaf_states: dict[int, set[int]] = {}  # channel_id -> set of member ids
        self.bot.add_view(DeafView(self))

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

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        # --- Voice session tracking ---
        async with AsyncSessionLocal() as db:
            user = (await db.execute(
                select(User).where(User.discord_id == member.id)
            )).scalar_one_or_none()

            if user is None:
                user = User(discord_id=member.id, username=member.name)
                db.add(user)
                await db.commit()

            if before.channel is None and after.channel is not None:
                # Joined a voice channel
                db.add(VoiceSession(
                    user_discord_id=member.id,
                    channel_id=after.channel.id,
                    joined_at=now,
                ))
                await db.commit()

                # Лог в архивный тред
                await self._log_voice_event(after.channel, member, "зашёл", discord.Color.green())

            elif before.channel is not None and after.channel is None:
                # Left voice channels
                open_sessions = (await db.execute(
                    select(VoiceSession)
                    .where(VoiceSession.user_discord_id == member.id)
                    .where(VoiceSession.left_at.is_(None))
                )).scalars().all()

                for session in open_sessions:
                    session.left_at = now
                    session.duration_seconds = int((now - session.joined_at).total_seconds())
                await db.commit()

                # Лог в архивный тред
                await self._log_voice_event(before.channel, member, "вышел", discord.Color.red())

                # Check voice achievements
                await self._check_and_notify_achievements(member, [
                    "voice_total",
                    "voice_longest_session",
                    "voice_streak",
                    "voice_lone_wolf",
                ])

            elif before.channel.id != after.channel.id:
                # Moved between channels
                old_sessions = (await db.execute(
                    select(VoiceSession)
                    .where(VoiceSession.user_discord_id == member.id)
                    .where(VoiceSession.left_at.is_(None))
                )).scalars().all()

                for old_session in old_sessions:
                    old_session.left_at = now
                    old_session.duration_seconds = int((now - old_session.joined_at).total_seconds())

                db.add(VoiceSession(
                    user_discord_id=member.id,
                    channel_id=after.channel.id,
                    joined_at=now,
                ))
                await db.commit()

                # Check voice achievements
                await self._check_and_notify_achievements(member, [
                    "voice_total",
                    "voice_longest_session",
                    "voice_lone_wolf",
                ])

        # --- Deaf state management (only on channel change, not on deaf toggle) ---
        channel_changed = before.channel != after.channel
        if channel_changed:
            if before.channel and before.channel.category_id == VOICE_CATEGORY_ID:
                ch_id = before.channel.id
                if ch_id in self.deaf_states and member.id in self.deaf_states[ch_id]:
                    try:
                        await member.edit(deafen=False)
                    except (discord.HTTPException, discord.Forbidden):
                        pass

            if after.channel and after.channel.category_id == VOICE_CATEGORY_ID:
                ch_id = after.channel.id
                if ch_id in self.deaf_states and member.id in self.deaf_states[ch_id]:
                    try:
                        await member.edit(deafen=True)
                    except (discord.HTTPException, discord.Forbidden):
                        pass

        # --- Temporary voice channel management ---
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

        if not hasattr(self, '_voice_threads') or channel.id not in self._voice_threads:
            return

        archive_channel = self.bot.get_channel(ARCHIVE_CHANNEL_ID)
        if not archive_channel:
            return

        thread = archive_channel.get_thread(self._voice_threads[channel.id])
        if not thread:
            return

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

        # Count message
        async with AsyncSessionLocal() as db:
            user = (await db.execute(
                select(User).where(User.discord_id == message.author.id)
            )).scalar_one_or_none()

            if user is None:
                user = User(discord_id=message.author.id, username=message.author.name)
                db.add(user)
                await db.commit()

            db.add(MessageCounter(
                user_discord_id=message.author.id,
                channel_id=message.channel.id,
            ))
            user.total_messages += 1
            await db.commit()

        # Check message achievements
        await self._check_and_notify_achievements(message.author, ["messages_total"])

    async def _handle_new_voice_join(self, channel: discord.VoiceChannel, creator: discord.Member):
        category = channel.category
        if not category:
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text("UPDATE voice_counters SET count = count + 1 WHERE month = :month AND year = :year RETURNING count"),
                {"month": now.month, "year": now.year},
            )
            row = result.fetchone()
            if row:
                new_count = row[0]
            else:
                await db.execute(
                    text("INSERT INTO voice_counters (month, year, count) VALUES (:month, :year, 1)"),
                    {"month": now.month, "year": now.year},
                )
                new_count = 1
            await db.commit()
            new_name = f"{VOICE_CHANNEL_PREFIX}{new_count}"

        await channel.edit(name=new_name, user_limit=None)
        await category.create_voice_channel(NEW_VOICE_NAME, user_limit=1)

        overwrite = discord.PermissionOverwrite(
            manage_channels=True,
            move_members=True,
            kick_members=True,
        )
        await channel.set_permissions(creator, overwrite=overwrite)

        # Создать архивный тред для этого войса
        if not hasattr(self, '_voice_threads'):
            self._voice_threads = {}

        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if log_channel:
            try:
                thread = await log_channel.create_thread(
                    name=new_name,
                    type=discord.ChannelType.private_thread,
                )
                self._voice_threads[channel.id] = thread.id

                create_embed = discord.Embed(
                    title="🔊 Голосовой канал создан",
                    description=f"Создатель: {creator.mention}",
                    color=discord.Color.green(),
                    timestamp=datetime.now(timezone.utc),
                )
                await thread.send(embed=create_embed)
            except (discord.NotFound, discord.HTTPException):
                pass

        embed = discord.Embed(
            description="Используйте меню ниже чтобы изменить регион войса.",
            color=discord.Color.green(),
        )
        await channel.send(embed=embed, view=RegionView())

        deaf_embed = discord.Embed(
            description="Нажмите кнопку чтобы отключить/включить себе звук.",
            color=discord.Color.orange(),
        )
        await channel.send(embed=deaf_embed, view=DeafView(self))

    async def _log_voice_event(self, channel: discord.VoiceChannel, member: discord.Member, action: str, color: discord.Color):
        if not hasattr(self, '_voice_threads') or channel.id not in self._voice_threads:
            return

        log_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if not log_channel:
            return

        thread = log_channel.get_thread(self._voice_threads[channel.id])
        if not thread:
            return

        embed = discord.Embed(
            description=f"{member.mention} {action} в голосовой канал",
            color=color,
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
        try:
            await thread.send(embed=embed)
        except (discord.NotFound, discord.HTTPException):
            pass

    async def _check_and_notify_achievements(self, member: discord.Member, achievement_names: list[str]):
        """Check achievements and notify user if any are unlocked."""
        for name in achievement_names:
            unlocked = await check_achievements(member.id, name)
            for item in unlocked:
                await self._notify_achievement(member, item["achievement"], item["level"])

    async def _notify_achievement(self, member: discord.Member, achievement, level):
        """Send DM notification and assign role if configured."""
        try:
            dm = await member.create_dm()
            embed = discord.Embed(
                title="🏆 Достижение получено!",
                description=f"**{level.name}**\n{achievement.description}",
                color=discord.Color.gold(),
            )
            if achievement.icon:
                embed.set_thumbnail(url=achievement.icon)
            await dm.send(embed=embed)
        except (discord.HTTPException, discord.Forbidden):
            pass

        if level.role_id:
            try:
                role = member.guild.get_role(level.role_id)
                if role and role not in member.roles:
                    await member.add_roles(role, reason="Achievement unlocked")
            except (discord.HTTPException, discord.Forbidden):
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
        self.deaf_states.pop(channel.id, None)
        self.active_timers.pop(channel.id, None)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceChannels(bot))
