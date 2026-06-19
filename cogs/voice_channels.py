import asyncio
import logging
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)

VOICE_CATEGORY_ID = 1517577490368041200
ARCHIVE_CHANNEL_ID = 1517446069192102003
VOICE_CHANNEL_PREFIX = "Голосовой #"
NEW_VOICE_NAME = "Новый войс"
EMPTY_TIMEOUT_SECONDS = 10


class VoiceChannels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_timers: dict[int, asyncio.Task] = {}

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_new_voice_exists()

    async def _ensure_new_voice_exists(self):
        logger.info("[VoiceChannels] Checking for '%s' in category %s", NEW_VOICE_NAME, VOICE_CATEGORY_ID)

        guild = self.bot.guilds[0] if self.bot.guilds else None
        if not guild:
            logger.warning("[VoiceChannels] No guilds found")
            return

        try:
            category = await guild.fetch_channel(VOICE_CATEGORY_ID)
        except (discord.NotFound, discord.HTTPException) as e:
            logger.error("[VoiceChannels] Failed to fetch category %s: %s", VOICE_CATEGORY_ID, e)
            return

        for channel in category.voice_channels:
            if channel.name == NEW_VOICE_NAME:
                logger.info("[VoiceChannels] '%s' already exists", NEW_VOICE_NAME)
                return

        logger.info("[VoiceChannels] Creating '%s'", NEW_VOICE_NAME)
        await category.create_voice_channel(NEW_VOICE_NAME)
        logger.info("[VoiceChannels] '%s' created", NEW_VOICE_NAME)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return

        logger.debug("[VoiceChannels] %s: %s -> %s", member.name, before.channel, after.channel)

        if before.channel and before.channel.category_id == VOICE_CATEGORY_ID:
            if before.channel.name == NEW_VOICE_NAME and not before.channel.members:
                return

            if before.channel.name.startswith(VOICE_CHANNEL_PREFIX):
                if not before.channel.members:
                    logger.info("[VoiceChannels] %s left empty channel '%s', starting timer", member.name, before.channel.name)
                    self._start_deletion_timer(before.channel)
                elif before.channel.id in self.active_timers:
                    logger.info("[VoiceChannels] %s joined '%s', cancelling timer", member.name, before.channel.name)
                    self._cancel_deletion_timer(before.channel.id)

        if after.channel and after.channel.category_id == VOICE_CATEGORY_ID:
            if after.channel.name == NEW_VOICE_NAME:
                logger.info("[VoiceChannels] %s joined '%s'", member.name, NEW_VOICE_NAME)
                await self._handle_new_voice_join(after.channel)

    async def _handle_new_voice_join(self, channel: discord.VoiceChannel):
        category = channel.category
        if not category:
            logger.error("[VoiceChannels] No category found for channel '%s'", channel.name)
            return

        max_number = 0
        for ch in category.voice_channels:
            match = re.match(r"Голосовой #(\d+)", ch.name)
            if match:
                num = int(match.group(1))
                if num > max_number:
                    max_number = num

        new_name = f"{VOICE_CHANNEL_PREFIX}{max_number + 1}"
        logger.info("[VoiceChannels] Renaming '%s' to '%s'", channel.name, new_name)

        await channel.edit(name=new_name)
        logger.info("[VoiceChannels] Creating new '%s'", NEW_VOICE_NAME)
        await category.create_voice_channel(NEW_VOICE_NAME)
        logger.info("[VoiceChannels] New '%s' created", NEW_VOICE_NAME)

    def _start_deletion_timer(self, channel: discord.VoiceChannel):
        logger.info("[VoiceChannels] Starting %ds timer for '%s'", EMPTY_TIMEOUT_SECONDS, channel.name)
        task = asyncio.create_task(self._delete_after_timeout(channel))
        self.active_timers[channel.id] = task

    def _cancel_deletion_timer(self, channel_id: int):
        if channel_id in self.active_timers:
            logger.info("[VoiceChannels] Cancelling timer for channel %s", channel_id)
            self.active_timers[channel_id].cancel()
            del self.active_timers[channel_id]

    async def _delete_after_timeout(self, channel: discord.VoiceChannel):
        await asyncio.sleep(EMPTY_TIMEOUT_SECONDS)

        if channel.members:
            logger.info("[VoiceChannels] Channel '%s' has members, skipping delete", channel.name)
            return

        logger.info("[VoiceChannels] Archiving chat for '%s'", channel.name)
        await self._archive_chat(channel)

        logger.info("[VoiceChannels] Deleting channel '%s'", channel.name)
        try:
            await channel.delete(reason="Voice channel empty for 10 seconds")
            logger.info("[VoiceChannels] Channel '%s' deleted", channel.name)
        except discord.NotFound:
            logger.warning("[VoiceChannels] Channel '%s' already deleted", channel.name)
        self.active_timers.pop(channel.id, None)

    async def _archive_chat(self, channel: discord.VoiceChannel):
        logger.info("[VoiceChannels] Archiving chat from '%s'", channel.name)

        archive_channel = self.bot.get_channel(ARCHIVE_CHANNEL_ID)
        if not archive_channel:
            logger.error("[VoiceChannels] Archive channel %s not found", ARCHIVE_CHANNEL_ID)
            return

        thread = await archive_channel.create_thread(
            name=channel.name,
            type=discord.ChannelType.public_thread
        )
        logger.info("[VoiceChannels] Created thread '%s' in archive channel", thread.name)

        messages = [msg async for msg in channel.history(limit=100)]
        messages.reverse()
        logger.info("[VoiceChannels] Found %d messages to archive", len(messages))

        if not messages:
            await thread.send("Нет сообщений для архивации.")
            return

        for msg in messages:
            if not msg.content and not msg.attachments:
                continue

            embed = discord.Embed(
                description=msg.content if msg.content else "*Нет текста*",
                color=discord.Color.blue(),
                timestamp=msg.created_at
            )
            embed.set_author(
                name=msg.author.display_name,
                icon_url=msg.author.display_avatar.url
            )

            if msg.attachments:
                attachment_urls = []
                for a in msg.attachments:
                    if a.url.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp')):
                        embed.set_image(url=a.url)
                    else:
                        attachment_urls.append(a.url)

                if attachment_urls:
                    attachment_text = "\n".join(attachment_urls)
                    embed.add_field(name="Вложения", value=attachment_text, inline=False)

            await thread.send(embed=embed)

        logger.info("[VoiceChannels] Archive complete for '%s'", channel.name)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceChannels(bot))
