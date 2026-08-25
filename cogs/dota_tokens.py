import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from database import AsyncSessionLocal
from models import (
    DotaToken,
    TokenFulfillment,
    TokenRequest,
    TokenRequestItem,
    User,
    UserAchievement,
    VoiceSession,
)

log = logging.getLogger(__name__)

TOKEN_CHANNEL_ID = 1541914614588121109
MAX_ACTIVE_REQUESTS = 3
MAX_TOKENS_PER_REQUEST = 5
AUTO_CONFIRM_HOURS = 24


def _parse_emoji(emoji_str: str) -> discord.PartialEmoji | str:
    """Parse emoji string - returns PartialEmoji for custom, str for Unicode."""
    if emoji_str.startswith('<'):
        return discord.PartialEmoji.from_str(emoji_str)
    return emoji_str


async def _parse_steam_profile(url: str) -> dict | None:
    """Parse Steam profile page to extract nickname and avatar URL.

    Returns dict with 'nickname' and 'avatar_url' keys, or None on failure.
    """
    # Normalize URL
    if not url.startswith('http'):
        url = 'https://' + url

    # Validate Steam URL pattern
    if not re.match(r'https?://steamcommunity\.com/(id|profiles)/[\w]+', url):
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

        # Extract nickname from <title> or <span class="actual_persona_name">
        nickname_match = re.search(r'<title>Steam Community :: (.+?)</title>', html)
        if not nickname_match:
            nickname_match = re.search(r'class="actual_persona_name">(.+?)<', html)
        nickname = nickname_match.group(1).strip() if nickname_match else None

        # Extract avatar from <img src="...avatar...">
        avatar_match = re.search(r'<link rel="image_src" href="(.+?)"', html)
        if not avatar_match:
            avatar_match = re.search(r'<img[^>]+class="playerAvatar[^"]*"[^>]+src="(.+?)"', html)
        avatar_url = avatar_match.group(1) if avatar_match else None

        if not nickname and not avatar_url:
            return None

        return {
            'nickname': nickname,
            'avatar_url': avatar_url,
        }
    except Exception as e:
        log.warning("Failed to parse Steam profile %s: %s", url, e)
        return None


# --- Persistent Views ---

class SteamUrlModal(discord.ui.Modal, title="Steam профиль"):
    """Modal for entering Steam profile URL."""

    steam_url = discord.ui.TextInput(
        label="Ссылка на Steam профиль",
        placeholder="https://steamcommunity.com/id/yourname",
        style=discord.TextStyle.short,
        required=True,
    )

    def __init__(self, callback_after):
        super().__init__()
        self.callback_after = callback_after

    async def on_submit(self, interaction: discord.Interaction):
        url = self.steam_url.value.strip()
        if not url.startswith('http'):
            url = 'https://' + url

        if not re.match(r'https?://steamcommunity\.com/(id|profiles)/[\w]+', url):
            await interaction.response.send_message(
                "Некорректная ссылка. Пример: https://steamcommunity.com/id/yourname",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        await self.callback_after(interaction, url)


class TokenRequestView(discord.ui.View):
    """Persistent view in the token channel with select menu + create button."""

    def __init__(self, tokens: list[DotaToken] | None = None):
        super().__init__(timeout=None)
        self.selected_tokens: dict[int, list[int]] = {}  # user_id -> [token_ids]

        if tokens:
            options = [
                discord.SelectOption(label=t.name, value=str(t.id), emoji=_parse_emoji(t.emoji))
                for t in tokens
            ][:25]
            select = discord.ui.Select(
                placeholder="Выберите жетоны (макс 5)...",
                min_values=1,
                max_values=min(MAX_TOKENS_PER_REQUEST, len(options)),
                custom_id="token_select",
                options=options,
            )
            select.callback = self.token_select_callback
            self.add_item(select)

        button = discord.ui.Button(
            label="Создать запрос",
            style=discord.ButtonStyle.primary,
            custom_id="token_create",
        )
        button.callback = self.create_request
        self.add_item(button)

    async def token_select_callback(self, interaction: discord.Interaction):
        token_ids = [int(v) for v in interaction.data.get("values", [])]
        self.selected_tokens[interaction.user.id] = token_ids
        await interaction.response.send_message(
            f"Выбрано жетонов: {len(token_ids)}. Нажмите «Создать запрос».",
            ephemeral=True,
        )

    async def create_request(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        selected = self.selected_tokens.get(user_id)
        if not selected:
            await interaction.response.send_message(
                "Сначала выберите жетоны из меню выше.",
                ephemeral=True,
            )
            return

        # Check if user has Steam info
        async with AsyncSessionLocal() as db:
            user = (await db.execute(
                select(User).where(User.discord_id == user_id)
            )).scalar_one_or_none()

        if not user or not user.steam_url:
            # Show modal to enter Steam URL
            async def on_steam_submit(inter: discord.Interaction, steam_url: str):
                await self._process_steam_and_create(inter, user_id, selected, steam_url)

            modal = SteamUrlModal(on_steam_submit)
            await interaction.response.send_modal(modal)
            return

        # User has Steam info, proceed with creation
        await self._do_create_request(interaction, user_id, selected)

    async def _process_steam_and_create(
        self, interaction: discord.Interaction, user_id: int, selected: list[int], steam_url: str
    ):
        """Parse Steam profile and create request."""
        # Parse Steam profile
        steam_info = await _parse_steam_profile(steam_url)

        async with AsyncSessionLocal() as db:
            user = (await db.execute(
                select(User).where(User.discord_id == user_id)
            )).scalar_one_or_none()
            if user is None:
                user = User(discord_id=user_id, username=interaction.user.name)
                db.add(user)

            user.steam_url = steam_url
            if steam_info:
                user.steam_nickname = steam_info.get('nickname')
                user.steam_avatar_url = steam_info.get('avatar_url')
            await db.commit()

        if steam_info:
            await interaction.followup.send(
                f"Steam профиль сохранён! Ник: **{steam_info.get('nickname', 'неизвестно')}**",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "Не удалось распарсить Steam профиль, но ссылка сохранена.",
                ephemeral=True,
            )

        # Create the request
        await self._do_create_request(interaction, user_id, selected)

    async def _do_create_request(
        self, interaction: discord.Interaction, user_id: int, selected: list[int]
    ):
        """Actually create the token request."""
        async with AsyncSessionLocal() as db:
            user = (await db.execute(
                select(User).where(User.discord_id == user_id)
            )).scalar_one_or_none()
            if user is None:
                await interaction.followup.send("Пользователь не найден.", ephemeral=True)
                return

            active_count = (await db.execute(
                select(func.count(TokenRequest.id))
                .where(TokenRequest.requester_id == user_id)
                .where(TokenRequest.status.in_(["open", "in_progress"]))
            )).scalar()
            if active_count >= MAX_ACTIVE_REQUESTS:
                await interaction.followup.send(
                    f"У вас уже {active_count} активных запросов (максимум {MAX_ACTIVE_REQUESTS}).",
                    ephemeral=True,
                )
                return

            tokens = (await db.execute(
                select(DotaToken).where(DotaToken.id.in_(selected))
            )).scalars().all()
            if not tokens:
                await interaction.followup.send("Жетоны не найдены.", ephemeral=True)
                return

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

            bonus_today = (await db.execute(
                select(TokenRequest)
                .where(TokenRequest.requester_id == user_id)
                .where(TokenRequest.creation_bonus == True)
                .where(TokenRequest.created_at >= today_start)
            )).scalar_one_or_none()

            request = TokenRequest(
                requester_id=user_id,
                status="open",
                channel_id=TOKEN_CHANNEL_ID,
                created_at=now,
                creation_bonus=bonus_today is None,
            )
            db.add(request)
            await db.flush()

            for token in tokens:
                db.add(TokenRequestItem(request_id=request.id, token_id=token.id))

            await db.commit()

            # Reload user for steam fields
            await db.refresh(user)

        # Build embed with Select menu for fulfillers
        embed = self._build_request_embed(request, tokens, interaction.user, "open", user)
        view = RequestView(request.id, tokens)

        channel = interaction.channel
        msg = await channel.send(embed=embed, view=view)

        async with AsyncSessionLocal() as db2:
            req = await db2.get(TokenRequest, request.id)
            req.message_id = msg.id
            await db2.commit()

        self.selected_tokens.pop(user_id, None)

        await interaction.followup.send(
            f"Запрос #{request.id} создан!",
            ephemeral=True,
        )

    @staticmethod
    def _build_request_embed(
        request: TokenRequest,
        tokens: list[DotaToken],
        requester: discord.User | discord.Member,
        status: str,
        db_user: User | None = None,
    ) -> discord.Embed:
        colors = {
            "open": discord.Color.blue(),
            "in_progress": discord.Color.yellow(),
            "confirmed": discord.Color.green(),
            "disputed": discord.Color.red(),
            "rejected": discord.Color.dark_red(),
            "closed": discord.Color.dark_grey(),
        }
        status_labels = {
            "open": "Открыт",
            "in_progress": "В процессе",
            "confirmed": "Выполнен",
            "disputed": "Спор",
            "rejected": "Отклонён",
            "closed": "Закрыт",
        }

        embed = discord.Embed(
            title="Запрос жетонов",
            color=colors.get(status, discord.Color.blue()),
        )
        embed.set_author(name=requester.display_name, icon_url=requester.display_avatar.url)

        # Steam info
        if db_user and db_user.steam_nickname:
            embed.add_field(name="Steam", value=db_user.steam_nickname, inline=True)
        if db_user and db_user.steam_avatar_url:
            embed.set_thumbnail(url=db_user.steam_avatar_url)

        token_lines = [f"{t.emoji} {t.name}" for t in tokens]
        embed.add_field(name="Нужные жетоны", value="\n".join(token_lines), inline=False)

        embed.set_footer(text=f"ID: {request.id} | {status_labels.get(status, status)}")
        return embed


class RequestView(discord.ui.View):
    """View on request embed: Select menu for fulfillers + Close button for requester."""

    def __init__(self, request_id: int, tokens: list[DotaToken] | None = None):
        super().__init__(timeout=None)
        self.request_id = request_id

        if tokens:
            options = [
                discord.SelectOption(label=t.name, value=str(t.id), emoji=_parse_emoji(t.emoji))
                for t in tokens
            ][:25]
            select = discord.ui.Select(
                placeholder="Выберите жетон для отправки...",
                min_values=1,
                max_values=1,
                custom_id=f"token_fulfill_{request_id}",
                options=options,
            )
            select.callback = self.fulfill_callback
            self.add_item(select)

        self.add_item(CloseButton(request_id))

    async def fulfill_callback(self, interaction: discord.Interaction):
        token_id = int(interaction.data["values"][0])

        async with AsyncSessionLocal() as db:
            request = (await db.execute(
                select(TokenRequest)
                .options(selectinload(TokenRequest.tokens))
                .where(TokenRequest.id == self.request_id)
            )).scalar_one_or_none()

            if not request or request.status not in ("open", "in_progress"):
                await interaction.response.send_message("Этот запрос уже недоступен.", ephemeral=True)
                return

            if request.requester_id == interaction.user.id:
                await interaction.response.send_message("Нельзя выполнять свой собственный запрос.", ephemeral=True)
                return

            item = None
            for t in request.tokens:
                if t.token_id == token_id and not t.fulfilled:
                    item = t
                    break

            if not item:
                await interaction.response.send_message("Этот жетон уже отправлен или не найден.", ephemeral=True)
                return

            fulfiller = (await db.execute(
                select(User).where(User.discord_id == interaction.user.id)
            )).scalar_one_or_none()
            if fulfiller is None:
                fulfiller = User(discord_id=interaction.user.id, username=interaction.user.name)
                db.add(fulfiller)
                await db.flush()

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            item.fulfilled = True
            item.fulfilled_by = interaction.user.id
            item.fulfilled_at = now

            fulfiller.friendship_points += 1

            if request.status == "open":
                request.status = "in_progress"
                request.expires_at = now + timedelta(hours=AUTO_CONFIRM_HOURS)

            all_fulfilled = all(t.fulfilled for t in request.tokens)

            if all_fulfilled:
                request.status = "confirmed"
                if request.creation_bonus:
                    requester = await db.get(User, request.requester_id)
                    if requester:
                        requester.friendship_points += 1

            await db.commit()

            token = await db.get(DotaToken, token_id)
            token_name = token.name if token else "жетон"

        # Create or get private thread
        thread = await self._get_or_create_thread(interaction, request)

        # Send notification in thread
        confirm_view = TokenConfirmView(self.request_id, token_id)
        await thread.send(
            content=f"<@{request.requester_id}>, <@{interaction.user.id}> отправил жетон **{token_name}**. Подтвердите в течение {AUTO_CONFIRM_HOURS}ч.",
            view=confirm_view,
        )

        if all_fulfilled:
            # Delete main message
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass
            await interaction.response.send_message(
                f"Все жетоны отправлены! Запрос #{self.request_id} выполнен. +{len(request.tokens)} очков дружбы!",
            )
        else:
            # Update main embed with remaining tokens
            remaining = [t for t in request.tokens if not t.fulfilled]
            remaining_tokens = []
            db_user = None
            async with AsyncSessionLocal() as db:
                for item in remaining:
                    t = await db.get(DotaToken, item.token_id)
                    if t:
                        remaining_tokens.append(t)
                db_user = await db.get(User, request.requester_id)

            requester = interaction.guild.get_member(request.requester_id)
            embed = TokenRequestView._build_request_embed(request, remaining_tokens, requester, "in_progress", db_user)
            new_view = RequestView(self.request_id, remaining_tokens)
            await interaction.message.edit(embed=embed, view=new_view)

            await interaction.response.send_message(
                f"Жетон **{token_name}** отправлен! +1 очко дружбы.",
                ephemeral=True,
            )

    async def _get_or_create_thread(
        self, interaction: discord.Interaction, request: TokenRequest
    ) -> discord.Thread:
        """Get existing private thread or create new one (only for requester)."""
        if request.thread_id:
            thread = interaction.guild.get_thread(request.thread_id)
            if thread:
                return thread

        # Create new private thread
        thread = await interaction.channel.create_thread(
            name=f"Сделка #{request.id}",
            auto_archive_duration=1440,
            type=discord.ChannelType.private_thread,
        )

        # Add only requester
        requester_member = interaction.guild.get_member(request.requester_id)
        if requester_member:
            await thread.add_user(requester_member)

        # Save thread_id
        async with AsyncSessionLocal() as db:
            req = await db.get(TokenRequest, request.id)
            req.thread_id = thread.id
            await db.commit()

        return thread


class TokenConfirmView(discord.ui.View):
    """View in private thread for confirming/disputing individual tokens."""

    def __init__(self, request_id: int, token_id: int):
        super().__init__(timeout=None)
        self.request_id = request_id
        self.token_id = token_id

    @discord.ui.button(label="Подтвердить", style=discord.ButtonStyle.success, custom_id="token_item_confirm")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as db:
            request = (await db.execute(
                select(TokenRequest)
                .options(selectinload(TokenRequest.tokens))
                .where(TokenRequest.id == self.request_id)
            )).scalar_one_or_none()

            if not request:
                await interaction.response.send_message("Запрос не найден.", ephemeral=True)
                return

            if request.requester_id != interaction.user.id:
                await interaction.response.send_message("Только автор запроса может подтвердить.", ephemeral=True)
                return

            item = None
            for t in request.tokens:
                if t.token_id == self.token_id:
                    item = t
                    break

            if not item or not item.fulfilled:
                await interaction.response.send_message("Жетон не найден или не отправлен.", ephemeral=True)
                return

            # Mark as confirmed (we reuse fulfilled_at for confirmation time)
            # Check if all items are confirmed
            all_confirmed = all(t.fulfilled for t in request.tokens)

            if all_confirmed:
                request.status = "confirmed"
                if request.creation_bonus:
                    requester = await db.get(User, request.requester_id)
                    if requester:
                        requester.friendship_points += 1

            await db.commit()

        await interaction.message.edit(view=None)
        await interaction.response.send_message("Жетон подтверждён!")

        if all_confirmed:
            # Delete thread
            if interaction.message.thread:
                await interaction.message.thread.send("Все жетоны подтверждены. Тред будет удалён.")
                await asyncio.sleep(3)
                await interaction.message.thread.delete()

    @discord.ui.button(label="Спор", style=discord.ButtonStyle.danger, custom_id="token_item_dispute")
    async def dispute_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as db:
            request = (await db.execute(
                select(TokenRequest)
                .options(selectinload(TokenRequest.tokens))
                .where(TokenRequest.id == self.request_id)
            )).scalar_one_or_none()

            if not request:
                await interaction.response.send_message("Запрос не найден.", ephemeral=True)
                return

            if request.requester_id != interaction.user.id:
                await interaction.response.send_message("Только автор запроса может создать спор.", ephemeral=True)
                return

            # Find the fulfiller for this specific token
            fulfiller_id = None
            for t in request.tokens:
                if t.token_id == self.token_id:
                    fulfiller_id = t.fulfilled_by
                    break

            request.status = "disputed"
            await db.commit()

        # Create dispute thread (accessible to both parties)
        thread = await interaction.channel.create_thread(
            name=f"Спор #{self.request_id}",
            auto_archive_duration=1440,
            type=discord.ChannelType.private_thread,
        )

        # Add requester
        requester_member = interaction.guild.get_member(request.requester_id)
        if requester_member:
            await thread.add_user(requester_member)

        # Add fulfiller
        if fulfiller_id:
            fulfiller_member = interaction.guild.get_member(fulfiller_id)
            if fulfiller_member:
                try:
                    await thread.add_user(fulfiller_member)
                except discord.HTTPException:
                    pass

        await thread.send(
            f"Спор по запросу #{self.request_id}.\n"
            f"Заказчик: <@{request.requester_id}>\n"
            f"Исполнитель: <@{fulfiller_id}>\n\n"
            f"Админ может использовать `/token-resolve {self.request_id} approve` или `reject`."
        )

        await interaction.message.edit(view=None)
        await interaction.response.send_message(
            f"Создан спор по жетону. Тред: {thread.mention}",
        )


class CloseButton(discord.ui.Button):
    def __init__(self, request_id: int):
        super().__init__(label="Закрыть", style=discord.ButtonStyle.secondary, custom_id=f"token_close_{request_id}")
        self.request_id = request_id

    async def callback(self, interaction: discord.Interaction):
        async with AsyncSessionLocal() as db:
            request = (await db.execute(
                select(TokenRequest).where(TokenRequest.id == self.request_id)
            )).scalar_one_or_none()

            if not request:
                await interaction.response.send_message("Запрос не найден.", ephemeral=True)
                return

            if request.requester_id != interaction.user.id:
                await interaction.response.send_message("Только автор запроса может закрыть его.", ephemeral=True)
                return

            if request.status not in ("open", "in_progress"):
                await interaction.response.send_message("Этот запрос уже завершён.", ephemeral=True)
                return

            thread_id = request.thread_id
            request.status = "closed"
            await db.commit()

        # Delete main message
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass

        # Delete thread if exists
        if thread_id:
            thread = interaction.guild.get_thread(thread_id)
            if thread:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass

        await interaction.response.send_message("Запрос закрыт.", ephemeral=True)


# --- Cog ---

class DotaTokens(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._view_registered = False
        log.info("DotaTokens cog initialized")

    @commands.Cog.listener()
    async def on_ready(self):
        log.info("DotaTokens on_ready fired")
        if not self._view_registered:
            await self._setup_persistent_view()
            self._view_registered = True
        self.auto_confirm_loop.start()

    async def _setup_persistent_view(self):
        """Register persistent view and send it to the token channel if needed."""
        log.info("Setting up persistent view for channel %s", TOKEN_CHANNEL_ID)

        async with AsyncSessionLocal() as db:
            tokens = (await db.execute(
                select(DotaToken).order_by(DotaToken.name)
            )).scalars().all()

            active_requests = (await db.execute(
                select(TokenRequest).where(TokenRequest.status.in_(["open", "in_progress"]))
            )).scalars().all()

        if not tokens:
            log.info("No tokens in DB, skipping persistent view send")
            return

        view = TokenRequestView(tokens)
        self.bot.add_view(view)

        # Register active request views
        for req in active_requests:
            async with AsyncSessionLocal() as db:
                items = (await db.execute(
                    select(TokenRequestItem)
                    .where(TokenRequestItem.request_id == req.id)
                    .where(TokenRequestItem.fulfilled == False)
                )).scalars().all()
                remaining_tokens = []
                for item in items:
                    t = await db.get(DotaToken, item.token_id)
                    if t:
                        remaining_tokens.append(t)

            rv = RequestView(req.id, remaining_tokens if remaining_tokens else None)
            self.bot.add_view(rv)

        channel = self.bot.get_channel(TOKEN_CHANNEL_ID)
        if not channel:
            log.warning("Channel %s not found!", TOKEN_CHANNEL_ID)
            return

        log.info("Found channel: %s (%s)", channel.name, channel.id)

        async for message in channel.history(limit=10):
            if message.author == self.bot.user and message.components:
                embed = self._build_main_embed()
                await message.edit(embed=embed, view=view)
                log.info("Persistent view message updated")
                return

        embed = self._build_main_embed()
        await channel.send(embed=embed, view=view)
        log.info("Persistent view message sent to channel")

    @staticmethod
    def _build_main_embed() -> discord.Embed:
        embed = discord.Embed(
            title="Обмен жетонами Dota 2",
            description=(
                "Выберите нужные жетоны из меню и нажмите **«Создать запрос»**.\n\n"
                "**Как заработать очки:**\n"
                "- **+1** за каждый отправленный жетон\n"
                "- **+1** за создание запроса (раз в день)\n\n"
                "В конце события, лидерам по очкам выдадим:\n"
                "🏆 **Топ-3** получат Dota Plus на месяц или эквивалент в деньгах"
            ),
            color=discord.Color.blue(),
        )
        embed.set_image(url="https://clan.fastly.steamstatic.com/images/3703047/7942925df6ae43659acf60f2d2ff827461c02485.png")
        return embed

    # --- Token management commands ---

    @app_commands.command(name="token-add", description="Добавить жетон (только админы)")
    @app_commands.describe(name="Название жетона", emoji="Эмодзи жетона (юникод или кастомный)")
    async def token_add(self, interaction: discord.Interaction, name: str, emoji: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Только администраторы могут использовать эту команду.", ephemeral=True)
            return

        import re
        is_unicode = len(emoji) <= 10 and not emoji.isalnum()
        is_custom = bool(re.match(r'^<a?:\w+:\d+>$', emoji))
        if not is_unicode and not is_custom:
            await interaction.response.send_message(
                "Укажите корректный эмодзи. Пример: 🎯, ⚔️, или кастомный `<:name:id>`",
                ephemeral=True,
            )
            return

        async with AsyncSessionLocal() as db:
            existing = (await db.execute(
                select(DotaToken).where(DotaToken.name == name)
            )).scalar_one_or_none()
            if existing:
                await interaction.response.send_message(f"Жетон «{name}» уже существует.", ephemeral=True)
                return

            db.add(DotaToken(name=name, emoji=emoji))
            await db.commit()

        await interaction.response.send_message(f"Жетон {emoji} **{name}** добавлен.", ephemeral=True)
        await self._refresh_select_menu()

    @app_commands.command(name="token-remove", description="Удалить жетон (только админы)")
    @app_commands.describe(name="Название жетона")
    async def token_remove(self, interaction: discord.Interaction, name: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Только администраторы могут использовать эту команду.", ephemeral=True)
            return

        async with AsyncSessionLocal() as db:
            token = (await db.execute(
                select(DotaToken).where(DotaToken.name == name)
            )).scalar_one_or_none()
            if not token:
                await interaction.response.send_message(f"Жетон «{name}» не найден.", ephemeral=True)
                return

            await db.delete(token)
            await db.commit()

        await interaction.response.send_message(f"Жетон **{name}** удалён.", ephemeral=True)
        await self._refresh_select_menu()

    @app_commands.command(name="token-list", description="Показать все жетоны")
    async def token_list(self, interaction: discord.Interaction):
        async with AsyncSessionLocal() as db:
            tokens = (await db.execute(
                select(DotaToken).order_by(DotaToken.name)
            )).scalars().all()

        if not tokens:
            await interaction.response.send_message("Жетоны не добавлены.", ephemeral=True)
            return

        lines = [f"{t.emoji} **{t.name}**" for t in tokens]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # --- Admin resolve command ---

    @app_commands.command(name="token-resolve", description="Решить спор по запросу (только админы)")
    @app_commands.describe(request_id="ID запроса", action="approve или reject")
    @app_commands.choices(action=[
        app_commands.Choice(name="Подтвердить", value="approve"),
        app_commands.Choice(name="Отклонить", value="reject"),
    ])
    async def token_resolve(self, interaction: discord.Interaction, request_id: int, action: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Только администраторы могут использовать эту команду.", ephemeral=True)
            return

        async with AsyncSessionLocal() as db:
            request = (await db.execute(
                select(TokenRequest)
                .options(selectinload(TokenRequest.tokens))
                .where(TokenRequest.id == request_id)
            )).scalar_one_or_none()

            if not request:
                await interaction.response.send_message(f"Запрос #{request_id} не найден.", ephemeral=True)
                return

            if request.status != "disputed":
                await interaction.response.send_message(f"Запрос #{request_id} не в статусе «спор».", ephemeral=True)
                return

            if action == "approve":
                request.status = "confirmed"
                if request.creation_bonus:
                    requester = await db.get(User, request.requester_id)
                    if requester:
                        requester.friendship_points += 1
                await db.commit()
                await interaction.response.send_message(f"Запрос #{request_id} подтверждён. Очки дружбы начислены.", ephemeral=True)
            else:
                request.status = "rejected"
                await db.commit()
                await interaction.response.send_message(f"Запрос #{request_id} отклонён.", ephemeral=True)

        # Delete thread if exists
        if request.thread_id:
            thread = interaction.guild.get_thread(request.thread_id)
            if thread:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass

    # --- Profile command ---

    @app_commands.command(name="profile", description="Показать профиль пользователя на сервере")
    @app_commands.describe(user="Пользователь (по умолчанию вы)")
    async def profile(self, interaction: discord.Interaction, user: discord.Member = None):
        if user is None:
            user = interaction.user

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        async with AsyncSessionLocal() as db:
            db_user = (await db.execute(
                select(User).where(User.discord_id == user.id)
            )).scalar_one_or_none()

            if db_user is None:
                await interaction.response.send_message("Пользователь не найден в базе.", ephemeral=True)
                return

            total_seconds_result = (await db.execute(
                select(func.coalesce(func.sum(VoiceSession.duration_seconds), 0))
                .where(VoiceSession.user_discord_id == user.id)
                .where(VoiceSession.duration_seconds.isnot(None))
            )).scalar()
            total_seconds = total_seconds_result or 0

            open_session = (await db.execute(
                select(VoiceSession)
                .where(VoiceSession.user_discord_id == user.id)
                .where(VoiceSession.left_at.is_(None))
            )).scalar_one_or_none()
            if open_session:
                total_seconds += int((now - open_session.joined_at).total_seconds())

            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60

            user_achievements = (await db.execute(
                select(UserAchievement)
                .options(selectinload(UserAchievement.achievement))
                .where(UserAchievement.user_discord_id == user.id)
                .order_by(UserAchievement.unlocked_at.desc())
                .limit(5)
            )).scalars().all()

        embed = discord.Embed(color=discord.Color.blue())
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)

        embed.add_field(name="Время в голосовых", value=f"{hours}ч {minutes}м", inline=True)
        embed.add_field(name="Очки дружбы", value=str(db_user.friendship_points), inline=True)
        embed.add_field(name="Сообщений", value=str(db_user.total_messages), inline=True)

        if user_achievements:
            ach_lines = []
            for ua in user_achievements:
                ach_lines.append(f"{ua.achievement.icon or '🏆'} {ua.achievement.display_name}")
            embed.add_field(name="Последние достижения", value="\n".join(ach_lines), inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- Auto-confirm loop ---

    @tasks.loop(minutes=5)
    async def auto_confirm_loop(self):
        log.debug("Auto-confirm loop running")
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        async with AsyncSessionLocal() as db:
            expired = (await db.execute(
                select(TokenRequest)
                .options(selectinload(TokenRequest.tokens))
                .where(TokenRequest.status == "in_progress")
                .where(TokenRequest.expires_at.isnot(None))
                .where(TokenRequest.expires_at <= now)
            )).scalars().all()

            for request in expired:
                # Mark all unfulfilled tokens as fulfilled (auto-confirm)
                for item in request.tokens:
                    if not item.fulfilled:
                        item.fulfilled = True
                        item.fulfilled_at = now

                request.status = "confirmed"

                if request.creation_bonus:
                    requester = await db.get(User, request.requester_id)
                    if requester:
                        requester.friendship_points += 1

                await db.commit()

                # Delete main message
                if request.message_id and request.channel_id:
                    channel = self.bot.get_channel(request.channel_id)
                    if channel:
                        try:
                            message = await channel.fetch_message(request.message_id)
                            await message.delete()
                        except (discord.NotFound, discord.HTTPException):
                            pass

                # Delete thread
                if request.thread_id:
                    guild = self.bot.get_guild(1307622842048839731)
                    if guild:
                        thread = guild.get_thread(request.thread_id)
                        if thread:
                            try:
                                await thread.delete()
                            except discord.HTTPException:
                                pass

    @auto_confirm_loop.before_loop
    async def before_auto_confirm(self):
        await self.bot.wait_until_ready()
        log.info("Auto-confirm loop starting")

    async def _refresh_select_menu(self):
        """Refresh the select menu options in the persistent view."""
        async with AsyncSessionLocal() as db:
            tokens = (await db.execute(
                select(DotaToken).order_by(DotaToken.name)
            )).scalars().all()

        if not tokens:
            return

        channel = self.bot.get_channel(TOKEN_CHANNEL_ID)
        if not channel:
            return

        async for message in channel.history(limit=10):
            if message.author == self.bot.user and message.components:
                view = TokenRequestView(tokens)
                await message.edit(view=view)
                break


async def setup(bot: commands.Bot):
    await bot.add_cog(DotaTokens(bot))
