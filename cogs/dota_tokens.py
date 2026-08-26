import asyncio
import logging
import re
import uuid
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
    """Parse Steam profile page to extract nickname and avatar URL."""
    if not url.startswith('http'):
        url = 'https://' + url

    if not re.match(r'https?://steamcommunity\.com/(id|profiles)/[\w]+', url):
        return None

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return None
                html = await resp.text()

        nickname_match = re.search(r'<title>Steam Community :: (.+?)</title>', html)
        if not nickname_match:
            nickname_match = re.search(r'class="actual_persona_name">(.+?)<', html)
        nickname = nickname_match.group(1).strip() if nickname_match else None

        avatar_match = re.search(r'<link rel="image_src" href="(.+?)"', html)
        if not avatar_match:
            avatar_match = re.search(r'<img[^>]+class="playerAvatar[^"]*"[^>]+src="(.+?)"', html)
        avatar_url = avatar_match.group(1) if avatar_match else None

        if not nickname and not avatar_url:
            return None

        return {'nickname': nickname, 'avatar_url': avatar_url}
    except Exception as e:
        log.warning("Failed to parse Steam profile %s: %s", url, e)
        return None


# --- Views ---

class SteamUrlModal(discord.ui.Modal, title="Steam профиль"):
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
        self.selected_tokens: dict[int, list[int]] = {}

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

    @staticmethod
    def _is_persistent_view_message(message: discord.Message) -> bool:
        """Check if a message is the persistent view message by its component custom_ids."""
        for row in message.components:
            for component in row.children:
                if component.custom_id in ("token_select", "token_create"):
                    return True
        return False

    async def token_select_callback(self, interaction: discord.Interaction):
        token_ids = [int(v) for v in interaction.data.get("values", [])]
        self.selected_tokens[interaction.user.id] = token_ids
        await interaction.response.defer()

    async def create_request(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        selected = self.selected_tokens.get(user_id)
        if not selected:
            await interaction.response.send_message(
                "Сначала выберите жетоны из меню выше.",
                ephemeral=True,
            )
            return

        async with AsyncSessionLocal() as db:
            user = (await db.execute(
                select(User).where(User.discord_id == user_id)
            )).scalar_one_or_none()

        if not user or not user.steam_url:
            async def on_steam_submit(inter: discord.Interaction, steam_url: str):
                await self._process_steam_and_create(inter, user_id, selected, steam_url)

            modal = SteamUrlModal(on_steam_submit)
            await interaction.response.send_modal(modal)
            return

        await interaction.response.defer()
        await self._do_create_request(interaction, user_id, selected)

    async def _process_steam_and_create(
        self, interaction: discord.Interaction, user_id: int, selected: list[int], steam_url: str
    ):
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

        await self._do_create_request(interaction, user_id, selected)

    async def _do_create_request(
        self, interaction: discord.Interaction, user_id: int, selected: list[int]
    ):
        async with AsyncSessionLocal() as db:
            user = (await db.execute(
                select(User).where(User.discord_id == user_id)
            )).scalar_one_or_none()
            if user is None:
                await interaction.followup.send("Пользователь не найден.", ephemeral=True)
                return

            if user.blocked_creating:
                await interaction.followup.send("Вы заблокированы от создания сделок.", ephemeral=True)
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
                .where(TokenRequest.creation_bonus.is_(True))
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
            await db.refresh(user)

        embed = self._build_request_embed(request, tokens, interaction.user, "open", user)
        view = RequestView(request.id, tokens)

        channel = interaction.channel
        msg = await channel.send(embed=embed, view=view)

        # Create private thread for the requester immediately
        thread = await channel.create_thread(
            name=f"Сделка #{request.id}",
            auto_archive_duration=1440,
            type=discord.ChannelType.private_thread,
        )
        requester_member = interaction.guild.get_member(user_id)
        if requester_member:
            await thread.add_user(requester_member)

        thread_embed = discord.Embed(
            title=f"Сделка #{request.id}",
            description="Здесь вы будете получать уведомления о жетонах.\nНажмите **«Закрыть сделку»** чтобы отменить незаполненные жетоны.",
            color=discord.Color.blue(),
        )
        token_lines = [f"{t.emoji} {t.name}" for t in tokens]
        thread_embed.add_field(name="Нужные жетоны", value="\n".join(token_lines), inline=False)
        await thread.send(embed=thread_embed, view=ThreadCloseView(request.id))

        async with AsyncSessionLocal() as db2:
            req = await db2.get(TokenRequest, request.id)
            req.message_id = msg.id
            req.thread_id = thread.id
            await db2.commit()

        self.selected_tokens.pop(user_id, None)

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

        if db_user and db_user.steam_nickname:
            embed.add_field(name="Steam", value=db_user.steam_nickname, inline=True)
        if db_user and db_user.steam_avatar_url:
            embed.set_thumbnail(url=db_user.steam_avatar_url)

        token_lines = [f"{t.emoji} {t.name}" for t in tokens]
        embed.add_field(name="Нужные жетоны", value="\n".join(token_lines), inline=False)

        embed.set_footer(text=f"ID: {request.id} | {status_labels.get(status, status)}")
        return embed


class RequestView(discord.ui.View):
    """View on request embed: Select menu for fulfillers only."""

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

            # Check if user is blocked from sending
            fulfiller = (await db.execute(
                select(User).where(User.discord_id == interaction.user.id)
            )).scalar_one_or_none()
            if fulfiller and fulfiller.blocked_sending:
                await interaction.response.send_message("Вы заблокированы от отправки жетонов.", ephemeral=True)
                return

            item = None
            for t in request.tokens:
                if t.token_id == token_id and not t.fulfilled:
                    item = t
                    break

            if not item:
                await interaction.response.send_message("Этот жетон уже отправлен или не найден.", ephemeral=True)
                return

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
            expires_at = request.expires_at

        thread = await self._get_or_create_thread(interaction, request)

        confirm_view = TokenConfirmView(self.request_id, token_id)
        if expires_at:
            unix_ts = int(expires_at.replace(tzinfo=timezone.utc).timestamp())
            auto_text = f"Сделка автоматически одобрится <t:{unix_ts}:R>"
        else:
            auto_text = f"Подтвердите в течение {AUTO_CONFIRM_HOURS}ч."
        embed = discord.Embed(
            title="Отправка жетона",
            description=f"<@{interaction.user.id}> отправил жетон **{token_name}**.\n{auto_text}",
            color=discord.Color.yellow(),
        )
        await thread.send(
            content=f"<@{request.requester_id}>",
            embed=embed,
            view=confirm_view,
        )

        if all_fulfilled:
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass
            await interaction.response.defer()
        else:
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
            await interaction.response.defer()

    async def _get_or_create_thread(
        self, interaction: discord.Interaction, request: TokenRequest
    ) -> discord.Thread:
        if request.thread_id:
            thread = interaction.guild.get_thread(request.thread_id)
            if thread:
                return thread

        thread = await interaction.channel.create_thread(
            name=f"Сделка #{request.id}",
            auto_archive_duration=1440,
            type=discord.ChannelType.private_thread,
        )

        requester_member = interaction.guild.get_member(request.requester_id)
        if requester_member:
            await thread.add_user(requester_member)

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

        confirm_btn = discord.ui.Button(
            label="Подтвердить",
            style=discord.ButtonStyle.success,
            custom_id=f"token_confirm_{request_id}_{token_id}",
        )
        confirm_btn.callback = self.confirm_button
        self.add_item(confirm_btn)

        dispute_btn = discord.ui.Button(
            label="Спор",
            style=discord.ButtonStyle.danger,
            custom_id=f"token_dispute_{request_id}_{token_id}",
        )
        dispute_btn.callback = self.dispute_button
        self.add_item(dispute_btn)

    async def confirm_button(self, interaction: discord.Interaction):
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

            all_confirmed = all(t.fulfilled for t in request.tokens)

            if all_confirmed:
                request.status = "confirmed"
                if request.creation_bonus:
                    requester = await db.get(User, request.requester_id)
                    if requester:
                        requester.friendship_points += 1

            await db.commit()

        await interaction.message.edit(view=None)
        await interaction.response.defer()

        if all_confirmed:
            if interaction.message.thread:
                await interaction.message.thread.send("Все жетоны подтверждены. Тред будет удалён.")
                await asyncio.sleep(3)
                await interaction.message.thread.delete()

    async def dispute_button(self, interaction: discord.Interaction):
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

            fulfiller_id = None
            for t in request.tokens:
                if t.token_id == self.token_id:
                    fulfiller_id = t.fulfilled_by
                    break

            request.status = "disputed"
            await db.commit()

        parent_channel = interaction.channel.parent if isinstance(interaction.channel, discord.Thread) else interaction.channel
        thread = await parent_channel.create_thread(
            name=f"Спор #{self.request_id}",
            auto_archive_duration=1440,
            type=discord.ChannelType.private_thread,
        )

        requester_member = interaction.guild.get_member(request.requester_id)
        if requester_member:
            await thread.add_user(requester_member)

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


class ThreadCloseView(discord.ui.View):
    """Close button inside the private thread — only visible to requester."""

    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.request_id = request_id
        # Add button programmatically with unique custom_id
        btn = discord.ui.Button(
            label="Закрыть сделку",
            style=discord.ButtonStyle.danger,
            custom_id=f"thread_close_{request_id}",
        )
        btn.callback = self.close_button
        self.add_item(btn)

    async def close_button(self, interaction: discord.Interaction):
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
                await interaction.response.send_message("Только автор запроса может закрыть его.", ephemeral=True)
                return

            if request.status not in ("open", "in_progress"):
                await interaction.response.send_message("Этот запрос уже завершён.", ephemeral=True)
                return

            message_id = request.message_id
            channel_id = request.channel_id
            request.status = "closed"
            await db.commit()

        if message_id and channel_id:
            channel = interaction.guild.get_channel(channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(message_id)
                    await msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

        await interaction.response.send_message("Сделка закрыта. Тред будет удалён через 5 секунд.")
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete()
        except discord.HTTPException:
            pass


class DealsMenuView(discord.ui.View):
    """Ephemeral view for managing deals with pagination (up to 5 SelectMenus)."""

    def __init__(self, active: list[TokenRequest], closed: list[TokenRequest], admin_id: int, target_user_id: int):
        super().__init__(timeout=300)
        self.admin_id = admin_id
        self.target_user_id = target_user_id
        self._uid = uuid.uuid4().hex[:8]

        # Combine: active first, then closed
        all_requests = active + closed
        chunk_size = 25

        for i in range(0, min(len(all_requests), chunk_size * 5), chunk_size):
            chunk = all_requests[i:i + chunk_size]
            page_num = (i // chunk_size) + 1
            options = []
            for r in chunk:
                is_active = r.status in ("open", "in_progress")
                emoji = {"open": "🟢", "in_progress": "🟡", "confirmed": "✅", "closed": "⚫", "rejected": "❌", "disputed": "🔴"}.get(r.status, "⚪")
                prefix = "active" if is_active else "closed"
                options.append(discord.SelectOption(
                    label=f"#{r.id} — {r.status}",
                    value=f"{prefix}_{r.id}",
                    emoji=emoji,
                ))

            select = discord.ui.Select(
                placeholder=f"Сделки {i + 1}-{i + len(chunk)}...",
                options=options,
                custom_id=f"deals_sel_{self._uid}_{page_num}",
            )
            select.callback = self.deal_select_callback
            self.add_item(select)

    async def deal_select_callback(self, interaction: discord.Interaction):
        value = interaction.data["values"][0]
        prefix, request_id_str = value.split("_", 1)
        request_id = int(request_id_str)

        if prefix == "active":
            view = DealActionView(request_id, self.admin_id, self.target_user_id)
            await interaction.response.send_message(
                f"Сделка #{request_id}",
                view=view,
                ephemeral=True,
            )
        else:
            async with AsyncSessionLocal() as db:
                request = await db.get(TokenRequest, request_id)

            if request and request.thread_id:
                thread = interaction.guild.get_thread(request.thread_id)
                if thread:
                    await interaction.response.send_message(
                        f"Перейти к сделке: {thread.mention}",
                        ephemeral=True,
                    )
                    return

            await interaction.response.send_message(
                f"Сделка #{request_id} — тред не найден.",
                ephemeral=True,
            )


class DealActionView(discord.ui.View):
    """Buttons for active deal management."""

    def __init__(self, request_id: int, admin_id: int, target_user_id: int | None = None):
        super().__init__(timeout=120)
        self.request_id = request_id
        self.admin_id = admin_id
        self.target_user_id = target_user_id

        # Add buttons programmatically with unique custom_ids
        force_btn = discord.ui.Button(
            label="Закрыть принудительно",
            style=discord.ButtonStyle.danger,
            custom_id=f"deal_force_{request_id}",
        )
        force_btn.callback = self.force_close
        self.add_item(force_btn)

        manage_btn = discord.ui.Button(
            label="Управление пользователем",
            style=discord.ButtonStyle.primary,
            custom_id=f"deal_manage_{request_id}",
        )
        manage_btn.callback = self.manage_user
        self.add_item(manage_btn)

        back_btn = discord.ui.Button(
            label="Назад в меню",
            style=discord.ButtonStyle.secondary,
            custom_id=f"deal_back_{request_id}",
        )
        back_btn.callback = self.back_to_menu
        self.add_item(back_btn)

    async def force_close(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Только администратор может закрыть сделку.", ephemeral=True)
            return

        async with AsyncSessionLocal() as db:
            request = (await db.execute(
                select(TokenRequest)
                .options(selectinload(TokenRequest.tokens))
                .where(TokenRequest.id == self.request_id)
            )).scalar_one_or_none()

            if not request:
                await interaction.response.send_message("Сделка не найдена.", ephemeral=True)
                return

            thread_id = request.thread_id
            message_id = request.message_id
            channel_id = request.channel_id
            request.status = "closed"
            await db.commit()

        if message_id and channel_id:
            channel = interaction.guild.get_channel(channel_id)
            if channel:
                try:
                    msg = await channel.fetch_message(message_id)
                    await msg.delete()
                except (discord.NotFound, discord.HTTPException):
                    pass

        if thread_id:
            thread = interaction.guild.get_thread(thread_id)
            if thread:
                try:
                    await thread.delete()
                except discord.HTTPException:
                    pass

        await interaction.response.send_message(f"Сделка #{self.request_id} закрыта.", ephemeral=True)

    async def manage_user(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Только администратор может управлять пользователями.", ephemeral=True)
            return

        if not self.target_user_id:
            await interaction.response.send_message("Пользователь не определён.", ephemeral=True)
            return

        view = UserManageView(self.target_user_id, self.admin_id)
        await interaction.response.send_message(
            f"Управление <@{self.target_user_id}>",
            view=view,
            ephemeral=True,
        )

    async def back_to_menu(self, interaction: discord.Interaction):
        if not self.target_user_id:
            await interaction.response.send_message("Не удалось определить пользователя.", ephemeral=True)
            return

        async with AsyncSessionLocal() as db:
            active = (await db.execute(
                select(TokenRequest)
                .where(TokenRequest.requester_id == self.target_user_id)
                .where(TokenRequest.status.in_(["open", "in_progress"]))
                .order_by(TokenRequest.created_at.desc())
            )).scalars().all()

            closed = (await db.execute(
                select(TokenRequest)
                .where(TokenRequest.requester_id == self.target_user_id)
                .where(TokenRequest.status.in_(["confirmed", "closed", "rejected", "disputed"]))
                .order_by(TokenRequest.created_at.desc())
                .limit(100)
            )).scalars().all()

        member = interaction.guild.get_member(self.target_user_id)
        view = DealsMenuView(active, closed, self.admin_id, self.target_user_id)
        embed = DotaTokens._build_deals_embed(active, closed, member)
        await interaction.response.edit_message(embed=embed, view=view)


class UserManageView(discord.ui.View):
    """Buttons for user management in /deals."""

    def __init__(self, target_user_id: int, admin_id: int):
        super().__init__(timeout=120)
        self.target_user_id = target_user_id
        self.admin_id = admin_id

        uid = target_user_id
        buttons = [
            ("Заблокировать отправку", discord.ButtonStyle.danger, f"block_send_{uid}", self.block_sending),
            ("Разблокировать отправку", discord.ButtonStyle.success, f"unblock_send_{uid}", self.unblock_sending),
            ("Заблокировать создание", discord.ButtonStyle.danger, f"block_create_{uid}", self.block_creating),
            ("Разблокировать создание", discord.ButtonStyle.success, f"unblock_create_{uid}", self.unblock_creating),
        ]
        for label, style, custom_id, callback in buttons:
            btn = discord.ui.Button(label=label, style=style, custom_id=custom_id)
            btn.callback = callback
            self.add_item(btn)

    async def block_sending(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin_id:
            return
        async with AsyncSessionLocal() as db:
            user = await db.get(User, self.target_user_id)
            if user:
                user.blocked_sending = True
                await db.commit()
        await interaction.response.send_message(f"Пользователь <@{self.target_user_id}> заблокирован от отправки.", ephemeral=True)

    async def unblock_sending(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin_id:
            return
        async with AsyncSessionLocal() as db:
            user = await db.get(User, self.target_user_id)
            if user:
                user.blocked_sending = False
                await db.commit()
        await interaction.response.send_message(f"Пользователь <@{self.target_user_id}> разблокирован для отправки.", ephemeral=True)

    async def block_creating(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin_id:
            return
        async with AsyncSessionLocal() as db:
            user = await db.get(User, self.target_user_id)
            if user:
                user.blocked_creating = True
                await db.commit()
        await interaction.response.send_message(f"Пользователь <@{self.target_user_id}> заблокирован от создания сделок.", ephemeral=True)

    async def unblock_creating(self, interaction: discord.Interaction):
        if interaction.user.id != self.admin_id:
            return
        async with AsyncSessionLocal() as db:
            user = await db.get(User, self.target_user_id)
            if user:
                user.blocked_creating = False
                await db.commit()
        await interaction.response.send_message(f"Пользователь <@{self.target_user_id}> разблокирован для создания.", ephemeral=True)


# --- Cog ---

class DotaTokens(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._view_registered = False
        self._persistent_msg_id: int | None = None
        log.info("DotaTokens cog initialized")

    @commands.Cog.listener()
    async def on_ready(self):
        log.info("DotaTokens on_ready fired")
        if not self._view_registered:
            await self._setup_persistent_view()
            self._view_registered = True
        self.auto_confirm_loop.start()

    async def _setup_persistent_view(self):
        log.info("Setting up persistent view for channel %s", TOKEN_CHANNEL_ID)

        async with AsyncSessionLocal() as db:
            tokens = (await db.execute(
                select(DotaToken).order_by(DotaToken.name)
            )).scalars().all()

            active_requests = (await db.execute(
                select(TokenRequest)
                .options(selectinload(TokenRequest.tokens))
                .where(TokenRequest.status.in_(["open", "in_progress"]))
            )).scalars().all()

        if not tokens:
            log.info("No tokens in DB, skipping persistent view send")
            return

        view = TokenRequestView(tokens)
        self.bot.add_view(view)

        for req in active_requests:
            async with AsyncSessionLocal() as db:
                items = (await db.execute(
                    select(TokenRequestItem)
                    .where(TokenRequestItem.request_id == req.id)
                    .where(TokenRequestItem.fulfilled.is_(False))
                )).scalars().all()
                remaining_tokens = []
                for item in items:
                    t = await db.get(DotaToken, item.token_id)
                    if t:
                        remaining_tokens.append(t)

            rv = RequestView(req.id, remaining_tokens if remaining_tokens else None)
            self.bot.add_view(rv)

            # Register ThreadCloseView for each active request
            self.bot.add_view(ThreadCloseView(req.id))

            # Register TokenConfirmView for each token in active requests
            for item in req.tokens:
                self.bot.add_view(TokenConfirmView(req.id, item.token_id))

        channel = self.bot.get_channel(TOKEN_CHANNEL_ID)
        if not channel:
            log.warning("Channel %s not found!", TOKEN_CHANNEL_ID)
            return

        log.info("Found channel: %s (%s)", channel.name, channel.id)

        # Find the earliest bot message that IS the persistent view
        persistent_msg = None
        async for message in channel.history(limit=50, oldest_first=True):
            if message.author == self.bot.user and TokenRequestView._is_persistent_view_message(message):
                persistent_msg = message
                break

        if persistent_msg:
            embed = self._build_main_embed()
            await persistent_msg.edit(embed=embed, view=view)
            self._persistent_msg_id = persistent_msg.id
            log.info("Persistent view message found and updated (id=%s)", persistent_msg.id)
        else:
            embed = self._build_main_embed()
            msg = await channel.send(embed=embed, view=view)
            self._persistent_msg_id = msg.id
            log.info("Persistent view message created (id=%s)", msg.id)

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

    # --- Deals command (admin) ---

    @app_commands.command(name="deals", description="Управление сделками пользователя (только админы)")
    @app_commands.describe(member="Пользователь для просмотра сделок")
    async def deals(self, interaction: discord.Interaction, member: discord.Member):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Только администраторы могут использовать эту команду.", ephemeral=True)
            return

        async with AsyncSessionLocal() as db:
            active = (await db.execute(
                select(TokenRequest)
                .where(TokenRequest.requester_id == member.id)
                .where(TokenRequest.status.in_(["open", "in_progress"]))
                .order_by(TokenRequest.created_at.desc())
            )).scalars().all()

            closed = (await db.execute(
                select(TokenRequest)
                .where(TokenRequest.requester_id == member.id)
                .where(TokenRequest.status.in_(["confirmed", "closed", "rejected", "disputed"]))
                .order_by(TokenRequest.created_at.desc())
                .limit(100)
            )).scalars().all()

        view = DealsMenuView(active, closed, interaction.user.id, member.id)
        await interaction.response.send_message(
            embed=self._build_deals_embed(active, closed, member),
            view=view,
            ephemeral=True,
        )

    @staticmethod
    def _build_deals_embed(
        active: list[TokenRequest],
        closed: list[TokenRequest],
        member: discord.Member | None = None,
    ) -> discord.Embed:
        title = f"Сделки {member.display_name}" if member else "Управление сделками"
        embed = discord.Embed(title=title, color=discord.Color.blue())
        if member:
            embed.set_thumbnail(url=member.display_avatar.url)

        if active:
            active_lines = []
            for r in active:
                status_emoji = "🟢" if r.status == "open" else "🟡"
                active_lines.append(f"{status_emoji} #{r.id}")
            embed.add_field(name=f"Активные ({len(active)})", value="\n".join(active_lines), inline=False)
        else:
            embed.add_field(name="Активные", value="Нет активных сделок", inline=False)

        if closed:
            closed_lines = []
            for r in closed[:25]:
                status_emoji = {"confirmed": "✅", "closed": "⚫", "rejected": "❌", "disputed": "🔴"}.get(r.status, "⚪")
                closed_lines.append(f"{status_emoji} #{r.id}")
            embed.add_field(name=f"Завершённые ({len(closed)})", value="\n".join(closed_lines), inline=False)

        total = len(active) + len(closed)
        if total > 25:
            embed.set_footer(text=f"Всего: {total} сделок (показано до 125 в меню)")

        return embed

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

                if request.message_id and request.channel_id:
                    channel = self.bot.get_channel(request.channel_id)
                    if channel:
                        try:
                            message = await channel.fetch_message(request.message_id)
                            await message.delete()
                        except (discord.NotFound, discord.HTTPException):
                            pass

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
        async with AsyncSessionLocal() as db:
            tokens = (await db.execute(
                select(DotaToken).order_by(DotaToken.name)
            )).scalars().all()

        if not tokens:
            return

        if not hasattr(self, '_persistent_msg_id') or not self._persistent_msg_id:
            return

        channel = self.bot.get_channel(TOKEN_CHANNEL_ID)
        if not channel:
            return

        try:
            message = await channel.fetch_message(self._persistent_msg_id)
            view = TokenRequestView(tokens)
            await message.edit(view=view)
        except (discord.NotFound, discord.HTTPException) as e:
            log.warning("Failed to refresh persistent view: %s", e)
            self._persistent_msg_id = None


async def setup(bot: commands.Bot):
    await bot.add_cog(DotaTokens(bot))
