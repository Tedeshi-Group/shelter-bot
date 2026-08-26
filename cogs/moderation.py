from datetime import datetime, timezone

import discord
from discord.ext import commands

LOG_CHANNEL_ID = 1517446069192102003
DELETED_THREAD_NAME = "Удалённые"
EDITED_THREAD_NAME = "Изменённые"


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._deleted_thread_id: int | None = None
        self._edited_thread_id: int | None = None
        self._recently_logged_deletes: set[int] = set()

    @commands.Cog.listener()
    async def on_ready(self):
        await self._get_or_create_thread(DELETED_THREAD_NAME, "_deleted_thread_id")
        await self._get_or_create_thread(EDITED_THREAD_NAME, "_edited_thread_id")

    async def _get_or_create_thread(self, name: str, attr_name: str) -> discord.Thread | None:
        archive_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if not archive_channel:
            return None

        thread_id = getattr(self, attr_name)
        if thread_id:
            thread = archive_channel.get_thread(thread_id)
            if thread:
                return thread

        for thread in archive_channel.threads:
            if thread.name == name:
                setattr(self, attr_name, thread.id)
                return thread

        try:
            thread = await archive_channel.create_thread(
                name=name,
                type=discord.ChannelType.private_thread,
            )
            setattr(self, attr_name, thread.id)
            return thread
        except (discord.NotFound, discord.HTTPException):
            return None

    # --- Deleted messages ---

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.author.bot:
            return

        self._recently_logged_deletes.add(message.id)
        if len(self._recently_logged_deletes) > 500:
            self._recently_logged_deletes.pop()

        embed = self._build_delete_embed(message)
        files = []
        for a in message.attachments:
            try:
                files.append(await a.to_file())
            except (discord.HTTPException, discord.NotFound):
                pass
        await self._send_to_thread("_deleted_thread_id", embed, files)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.message_id in self._recently_logged_deletes:
            return

        embed = self._build_delete_partial_embed(payload)
        await self._send_to_thread("_deleted_thread_id", embed)

    # --- Edited messages ---

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.author.bot:
            return
        if before.content == after.content:
            return

        embed = self._build_edit_embed(before, after)
        await self._send_to_thread("_edited_thread_id", embed)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if payload.cached_message is not None:
            return

        embed = self._build_edit_partial_embed(payload)
        await self._send_to_thread("_edited_thread_id", embed)

    # --- Embed builders ---

    def _build_delete_embed(self, message: discord.Message) -> discord.Embed:
        embed = discord.Embed(
            title="🗑️ Сообщение удалено",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=message.author.display_name,
            icon_url=message.author.display_avatar.url,
        )
        embed.add_field(name="Канал", value=message.channel.mention, inline=True)
        embed.add_field(name="Автор", value=message.author.mention, inline=True)

        content = message.content or "*[нет текстового содержимого]*"
        if len(content) > 1024:
            content = content[:1021] + "..."
        embed.add_field(name="Содержимое", value=content, inline=False)

        if message.attachments:
            att_list = "\n".join(f"• [{a.filename}]({a.url})" for a in message.attachments)
            if len(att_list) > 1024:
                att_list = att_list[:1021] + "..."
            embed.add_field(name="Вложения", value=att_list, inline=False)

        embed.set_footer(text=f"Отправлено: {message.created_at.strftime('%d.%m.%Y %H:%M:%S')} UTC")
        return embed

    def _build_delete_partial_embed(self, payload: discord.RawMessageDeleteEvent) -> discord.Embed:
        embed = discord.Embed(
            title="🗑️ Сообщение удалено (не в кэше)",
            description=f"ID сообщения: `{payload.message_id}`",
            color=discord.Color.dark_red(),
            timestamp=datetime.now(timezone.utc),
        )
        channel = self.bot.get_channel(payload.channel_id)
        if channel:
            embed.add_field(name="Канал", value=channel.mention, inline=True)
        embed.set_footer(text="Сообщение не было в кэше бота — содержимое недоступно")
        return embed

    def _build_edit_embed(self, before: discord.Message, after: discord.Message) -> discord.Embed:
        embed = discord.Embed(
            title="✏️ Сообщение изменено",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_author(
            name=before.author.display_name,
            icon_url=before.author.display_avatar.url,
        )
        embed.add_field(name="Канал", value=before.channel.mention, inline=True)

        old_content = before.content or "*[пусто]*"
        new_content = after.content or "*[пусто]*"
        if len(old_content) > 1024:
            old_content = old_content[:1021] + "..."
        if len(new_content) > 1024:
            new_content = new_content[:1021] + "..."

        embed.add_field(name="До", value=old_content, inline=False)
        embed.add_field(name="После", value=new_content, inline=False)
        embed.set_footer(text=f"Сообщение: {before.id}")
        return embed

    def _build_edit_partial_embed(self, payload: discord.RawMessageUpdateEvent) -> discord.Embed:
        embed = discord.Embed(
            title="✏️ Сообщение изменено (не в кэше)",
            color=discord.Color.dark_orange(),
            timestamp=datetime.now(timezone.utc),
        )
        channel = self.bot.get_channel(payload.channel_id)
        if channel:
            embed.add_field(name="Канал", value=channel.mention, inline=True)

        new_content = payload.data.get("content", "")
        if new_content:
            if len(new_content) > 1024:
                new_content = new_content[:1021] + "..."
            embed.add_field(name="Новое содержимое", value=new_content, inline=False)

        embed.set_footer(text=f"Сообщение: {payload.message_id} | Старое содержимое недоступно")
        return embed

    # --- Send helper ---

    async def _send_to_thread(self, attr_name: str, embed: discord.Embed, files: list | None = None):
        thread_id = getattr(self, attr_name)
        if not thread_id:
            return

        archive_channel = self.bot.get_channel(LOG_CHANNEL_ID)
        if not archive_channel:
            return

        thread = archive_channel.get_thread(thread_id)
        if not thread:
            name = DELETED_THREAD_NAME if attr_name == "_deleted_thread_id" else EDITED_THREAD_NAME
            thread = await self._get_or_create_thread(name, attr_name)
            if not thread:
                return

        try:
            await thread.send(embed=embed, files=files or [])
        except (discord.NotFound, discord.HTTPException):
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
