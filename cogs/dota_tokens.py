import logging
from datetime import datetime, timedelta, timezone

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


# --- Persistent Views ---

class TokenRequestView(discord.ui.View):
    """Persistent view in the token channel with select menu + create button."""

    def __init__(self, tokens: list[DotaToken] | None = None):
        super().__init__(timeout=None)
        self.selected_tokens: dict[int, list[int]] = {}  # user_id -> [token_ids]

        if tokens:
            options = [
                discord.SelectOption(label=t.name, value=str(t.id), emoji=_parse_emoji(t.emoji))
                for t in tokens
            ][:25]  # Discord max 25 options
            select = discord.ui.Select(
                placeholder="Выберите жетоны (макс 5)...",
                min_values=1,
                max_values=min(MAX_TOKENS_PER_REQUEST, len(options)),
                custom_id="token_select",
                options=options,
            )
            select.callback = self.token_select_callback
            self.add_item(select)

    async def token_select_callback(self, interaction: discord.Interaction):
        select = interaction.data  # raw interaction data
        token_ids = [int(v) for v in interaction.data.get("values", [])]
        self.selected_tokens[interaction.user.id] = token_ids
        await interaction.response.send_message(
            f"Выбрано жетонов: {len(token_ids)}. Нажмите «Создать запрос».",
            ephemeral=True,
        )

    @discord.ui.button(label="Создать запрос", style=discord.ButtonStyle.primary, custom_id="token_create")
    async def create_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        selected = self.selected_tokens.get(user_id)
        if not selected:
            await interaction.response.send_message(
                "Сначала выберите жетоны из меню выше.",
                ephemeral=True,
            )
            return

        async with AsyncSessionLocal() as db:
            # Ensure user exists
            user = (await db.execute(
                select(User).where(User.discord_id == user_id)
            )).scalar_one_or_none()
            if user is None:
                user = User(discord_id=user_id, username=interaction.user.name)
                db.add(user)
                await db.flush()

            # Check active requests limit
            active_count = (await db.execute(
                select(func.count(TokenRequest.id))
                .where(TokenRequest.requester_id == user_id)
                .where(TokenRequest.status.in_(["open", "in_progress"]))
            )).scalar()
            if active_count >= MAX_ACTIVE_REQUESTS:
                await interaction.response.send_message(
                    f"У вас уже {active_count} активных запросов (максимум {MAX_ACTIVE_REQUESTS}).",
                    ephemeral=True,
                )
                return

            # Get tokens info
            tokens = (await db.execute(
                select(DotaToken).where(DotaToken.id.in_(selected))
            )).scalars().all()
            if not tokens:
                await interaction.response.send_message("Жетоны не найдены.", ephemeral=True)
                return

            # Create request
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            request = TokenRequest(
                requester_id=user_id,
                status="open",
                channel_id=TOKEN_CHANNEL_ID,
                created_at=now,
            )
            db.add(request)
            await db.flush()

            # Add token items
            for token in tokens:
                db.add(TokenRequestItem(request_id=request.id, token_id=token.id))

            await db.commit()

            # Build embed
            embed = self._build_request_embed(request, tokens, interaction.user, "open")
            view = RequestView(request.id)

            msg = await interaction.channel.send(embed=embed, view=view)

            # Update request with message_id
            async with AsyncSessionLocal() as db2:
                req = await db2.get(TokenRequest, request.id)
                req.message_id = msg.id
                await db2.commit()

            # Clear selection
            self.selected_tokens.pop(user_id, None)

            await interaction.response.send_message(
                f"Запрос #{request.id} создан!",
                ephemeral=True,
            )

    @staticmethod
    def _build_request_embed(
        request: TokenRequest,
        tokens: list[DotaToken],
        requester: discord.User | discord.Member,
        status: str,
        fulfiller: discord.User | discord.Member | None = None,
    ) -> discord.Embed:
        colors = {
            "open": discord.Color.blue(),
            "in_progress": discord.Color.yellow(),
            "confirmed": discord.Color.green(),
            "disputed": discord.Color.red(),
            "rejected": discord.Color.dark_red(),
        }
        status_labels = {
            "open": "Открыт",
            "in_progress": "В процессе",
            "confirmed": "Выполнен",
            "disputed": "Спор",
            "rejected": "Отклонён",
        }

        embed = discord.Embed(
            title="Запрос жетонов",
            color=colors.get(status, discord.Color.blue()),
        )
        embed.set_author(name=requester.display_name, icon_url=requester.display_avatar.url)

        token_lines = [f"{t.emoji} {t.name}" for t in tokens]
        embed.add_field(name="Нужные жетоны", value="\n".join(token_lines), inline=False)

        if fulfiller:
            embed.add_field(name="Исполнитель", value=fulfiller.mention, inline=True)

        embed.set_footer(text=f"ID: {request.id} | {status_labels.get(status, status)}")
        return embed


class RequestView(discord.ui.View):
    """Persistent view on each request embed with action buttons."""

    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.request_id = request_id

    @discord.ui.button(label="Выполнить", style=discord.ButtonStyle.success, custom_id="token_fulfill")
    async def fulfill_request(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as db:
            request = (await db.execute(
                select(TokenRequest)
                .options(selectinload(TokenRequest.tokens))
                .where(TokenRequest.id == self.request_id)
            )).scalar_one_or_none()

            if not request or request.status != "open":
                await interaction.response.send_message("Этот запрос уже недоступен.", ephemeral=True)
                return

            if request.requester_id == interaction.user.id:
                await interaction.response.send_message("Нельзя выполнять свой собственный запрос.", ephemeral=True)
                return

            # Ensure fulfiller exists
            fulfiller = (await db.execute(
                select(User).where(User.discord_id == interaction.user.id)
            )).scalar_one_or_none()
            if fulfiller is None:
                fulfiller = User(discord_id=interaction.user.id, username=interaction.user.name)
                db.add(fulfiller)
                await db.flush()

            now = datetime.now(timezone.utc).replace(tzinfo=None)
            request.status = "in_progress"
            request.expires_at = now + timedelta(hours=AUTO_CONFIRM_HOURS)

            db.add(TokenFulfillment(
                request_id=request.id,
                fulfiller_id=interaction.user.id,
                created_at=now,
            ))
            await db.commit()

        # Update embed
        await self._update_embed(interaction, "in_progress", interaction.user)

        # Replace buttons
        self.clear_items()
        self.add_item(ConfirmButton(self.request_id))
        self.add_item(DisputeButton(self.request_id))
        await interaction.message.edit(view=self)

        await interaction.response.send_message(
            f"Вы взялись выполнить запрос #{self.request_id}. У заказчика есть {AUTO_CONFIRM_HOURS}ч на подтверждение.",
            ephemeral=True,
        )

    async def _update_embed(self, interaction: discord.Interaction, status: str, fulfiller: discord.User | None = None):
        async with AsyncSessionLocal() as db:
            request = (await db.execute(
                select(TokenRequest)
                .options(selectinload(TokenRequest.tokens))
                .where(TokenRequest.id == self.request_id)
            )).scalar_one_or_none()
            if not request:
                return

            tokens = (await db.execute(
                select(DotaToken).where(DotaToken.id.in_([t.token_id for t in request.tokens]))
            )).scalars().all()

        requester = interaction.guild.get_member(request.requester_id) or await interaction.guild.fetch_member(request.requester_id)
        embed = TokenRequestView._build_request_embed(request, tokens, requester, status, fulfiller)
        await interaction.message.edit(embed=embed)


class ConfirmButton(discord.ui.Button):
    def __init__(self, request_id: int):
        super().__init__(label="Подтвердить", style=discord.ButtonStyle.success, custom_id=f"token_confirm_{request_id}")
        self.request_id = request_id

    async def callback(self, interaction: discord.Interaction):
        async with AsyncSessionLocal() as db:
            request = (await db.execute(
                select(TokenRequest).where(TokenRequest.id == self.request_id)
            )).scalar_one_or_none()

            if not request or request.status != "in_progress":
                await interaction.response.send_message("Запрос не в статусе «в процессе».", ephemeral=True)
                return

            if request.requester_id != interaction.user.id:
                await interaction.response.send_message("Только автор запроса может подтвердить.", ephemeral=True)
                return

            fulfillment = (await db.execute(
                select(TokenFulfillment).where(TokenFulfillment.request_id == self.request_id)
            )).scalar_one_or_none()

            if fulfillment:
                fulfiller = await db.get(User, fulfillment.fulfiller_id)
                if fulfiller:
                    fulfiller.friendship_points += 1

            request.status = "confirmed"
            await db.commit()

        # Update embed
        await self._update_embed(interaction, "confirmed")
        await interaction.message.edit(view=None)
        await interaction.response.send_message("Запрос подтверждён! Очки дружбы начислены.", ephemeral=True)

    async def _update_embed(self, interaction: discord.Interaction, status: str):
        async with AsyncSessionLocal() as db:
            request = (await db.execute(
                select(TokenRequest)
                .options(selectinload(TokenRequest.tokens))
                .where(TokenRequest.id == self.request_id)
            )).scalar_one_or_none()
            if not request:
                return

            tokens = (await db.execute(
                select(DotaToken).where(DotaToken.id.in_([t.token_id for t in request.tokens]))
            )).scalars().all()

            fulfillment = (await db.execute(
                select(TokenFulfillment).where(TokenFulfillment.request_id == self.request_id)
            )).scalar_one_or_none()

        requester = interaction.guild.get_member(request.requester_id) or await interaction.guild.fetch_member(request.requester_id)
        fulfiller = None
        if fulfillment:
            fulfiller = interaction.guild.get_member(fulfillment.fulfiller_id)
        embed = TokenRequestView._build_request_embed(request, tokens, requester, status, fulfiller)
        await interaction.message.edit(embed=embed)


class DisputeButton(discord.ui.Button):
    def __init__(self, request_id: int):
        super().__init__(label="Спор", style=discord.ButtonStyle.danger, custom_id=f"token_dispute_{request_id}")
        self.request_id = request_id

    async def callback(self, interaction: discord.Interaction):
        async with AsyncSessionLocal() as db:
            request = (await db.execute(
                select(TokenRequest).where(TokenRequest.id == self.request_id)
            )).scalar_one_or_none()

            if not request or request.status != "in_progress":
                await interaction.response.send_message("Запрос не в статусе «в процессе».", ephemeral=True)
                return

            if request.requester_id != interaction.user.id:
                await interaction.response.send_message("Только автор запроса может создать спор.", ephemeral=True)
                return

            request.status = "disputed"
            await db.commit()

        # Create thread for discussion
        thread = await interaction.message.create_thread(
            name=f"Спор по запросу #{self.request_id}",
            auto_archive_duration=1440,
        )
        await thread.send(
            f"Спор создан. Ожидайте решения администратора.\n"
            f"Заказчик: <@{request.requester_id}>\n"
            f"Админ может использовать `/token-resolve {self.request_id} approve` или `reject`."
        )

        # Update embed
        await self._update_embed(interaction, "disputed")
        await interaction.message.edit(view=None)
        await interaction.response.send_message("Спор создан. Администратор рассмотрит его.", ephemeral=True)

    async def _update_embed(self, interaction: discord.Interaction, status: str):
        async with AsyncSessionLocal() as db:
            request = (await db.execute(
                select(TokenRequest)
                .options(selectinload(TokenRequest.tokens))
                .where(TokenRequest.id == self.request_id)
            )).scalar_one_or_none()
            if not request:
                return

            tokens = (await db.execute(
                select(DotaToken).where(DotaToken.id.in_([t.token_id for t in request.tokens]))
            )).scalars().all()

        requester = interaction.guild.get_member(request.requester_id) or await interaction.guild.fetch_member(request.requester_id)
        embed = TokenRequestView._build_request_embed(request, tokens, requester, status)
        await interaction.message.edit(embed=embed)


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

        # Fetch tokens from DB
        async with AsyncSessionLocal() as db:
            tokens = (await db.execute(
                select(DotaToken).order_by(DotaToken.name)
            )).scalars().all()

            # Also register existing request views
            active_requests = (await db.execute(
                select(TokenRequest).where(TokenRequest.status.in_(["open", "in_progress"]))
            )).scalars().all()

        if not tokens:
            log.info("No tokens in DB, skipping persistent view send")
            return

        view = TokenRequestView(tokens)
        self.bot.add_view(view)

        for req in active_requests:
            if req.status == "open":
                rv = RequestView(req.id)
            else:
                rv = RequestView(req.id)
                rv.clear_items()
                rv.add_item(ConfirmButton(req.id))
                rv.add_item(DisputeButton(req.id))
            self.bot.add_view(rv)

        # Check if persistent view message exists in channel
        channel = self.bot.get_channel(TOKEN_CHANNEL_ID)
        if not channel:
            log.warning("Channel %s not found!", TOKEN_CHANNEL_ID)
            return

        log.info("Found channel: %s (%s)", channel.name, channel.id)

        # Try to find existing view message
        async for message in channel.history(limit=10):
            if message.author == self.bot.user and message.components:
                log.info("Persistent view message already exists")
                return  # View already exists

        # Send new persistent view
        embed = discord.Embed(
            title="Обмен жетонами Dota 2",
            description="Выберите нужные жетоны из меню и нажмите «Создать запрос».",
            color=discord.Color.blue(),
        )
        await channel.send(embed=embed, view=view)
        log.info("Persistent view message sent to channel")

    # --- Token management commands ---

    @app_commands.command(name="token-add", description="Добавить жетон (только админы)")
    @app_commands.describe(name="Название жетона", emoji="Эмодзи жетона (юникод или кастомный)")
    async def token_add(self, interaction: discord.Interaction, name: str, emoji: str):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Только администраторы могут использовать эту команду.", ephemeral=True)
            return

        # Validate emoji: Unicode emoji or custom Discord emoji <:name:id> / <a:name:id>
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
                select(TokenRequest).where(TokenRequest.id == request_id)
            )).scalar_one_or_none()

            if not request:
                await interaction.response.send_message(f"Запрос #{request_id} не найден.", ephemeral=True)
                return

            if request.status != "disputed":
                await interaction.response.send_message(f"Запрос #{request_id} не в статусе «спор».", ephemeral=True)
                return

            fulfillment = (await db.execute(
                select(TokenFulfillment).where(TokenFulfillment.request_id == request_id)
            )).scalar_one_or_none()

            if action == "approve":
                request.status = "confirmed"
                if fulfillment:
                    fulfiller = await db.get(User, fulfillment.fulfiller_id)
                    if fulfiller:
                        fulfiller.friendship_points += 1
                await db.commit()
                await interaction.response.send_message(f"Запрос #{request_id} подтверждён. Очки дружбы начислены.", ephemeral=True)
            else:
                request.status = "rejected"
                await db.commit()
                await interaction.response.send_message(f"Запрос #{request_id} отклонён.", ephemeral=True)

        # Update embed if message exists
        if request.message_id and request.channel_id:
            channel = self.bot.get_channel(request.channel_id)
            if channel:
                try:
                    message = await channel.fetch_message(request.message_id)
                    tokens = (await db.execute(
                        select(DotaToken)
                        .join(TokenRequestItem, TokenRequestItem.token_id == DotaToken.id)
                        .where(TokenRequestItem.request_id == request_id)
                    )).scalars().all()
                    requester = channel.guild.get_member(request.requester_id)
                    fulfiller_member = channel.guild.get_member(fulfillment.fulfiller_id) if fulfillment else None
                    embed = TokenRequestView._build_request_embed(
                        request, tokens, requester, request.status, fulfiller_member
                    )
                    await message.edit(embed=embed, view=None)
                except (discord.NotFound, discord.HTTPException):
                    pass

    # --- Profile command (replaces /time) ---

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

            # Voice time
            total_seconds_result = (await db.execute(
                select(func.coalesce(func.sum(VoiceSession.duration_seconds), 0))
                .where(VoiceSession.user_discord_id == user.id)
                .where(VoiceSession.duration_seconds.isnot(None))
            )).scalar()
            total_seconds = total_seconds_result or 0

            # Add open session time
            open_session = (await db.execute(
                select(VoiceSession)
                .where(VoiceSession.user_discord_id == user.id)
                .where(VoiceSession.left_at.is_(None))
            )).scalar_one_or_none()
            if open_session:
                total_seconds += int((now - open_session.joined_at).total_seconds())

            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60

            # Achievements
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
                .where(TokenRequest.status == "in_progress")
                .where(TokenRequest.expires_at.isnot(None))
                .where(TokenRequest.expires_at <= now)
            )).scalars().all()

            for request in expired:
                fulfillment = (await db.execute(
                    select(TokenFulfillment).where(TokenFulfillment.request_id == request.id)
                )).scalar_one_or_none()

                if fulfillment:
                    fulfiller = await db.get(User, fulfillment.fulfiller_id)
                    if fulfiller:
                        fulfiller.friendship_points += 1

                request.status = "confirmed"
                await db.commit()

                # Update embed
                if request.message_id and request.channel_id:
                    channel = self.bot.get_channel(request.channel_id)
                    if channel:
                        try:
                            message = await channel.fetch_message(request.message_id)
                            tokens = (await db.execute(
                                select(DotaToken)
                                .join(TokenRequestItem, TokenRequestItem.token_id == DotaToken.id)
                                .where(TokenRequestItem.request_id == request.id)
                            )).scalars().all()
                            requester = channel.guild.get_member(request.requester_id)
                            fulfiller_member = channel.guild.get_member(fulfillment.fulfiller_id) if fulfillment else None
                            embed = TokenRequestView._build_request_embed(
                                request, tokens, requester, "confirmed", fulfiller_member
                            )
                            await message.edit(embed=embed, view=None)
                        except (discord.NotFound, discord.HTTPException):
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

        # Find and update the view message
        async for message in channel.history(limit=10):
            if message.author == self.bot.user and message.components:
                view = TokenRequestView(tokens)
                await message.edit(view=view)
                break


async def setup(bot: commands.Bot):
    await bot.add_cog(DotaTokens(bot))
