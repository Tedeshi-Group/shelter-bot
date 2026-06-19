# Voice Channels Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement temporary voice channels that auto-rename, auto-delete after 10 seconds of emptiness, and archive chat messages to threads.

**Architecture:** A new Cog `cogs/voice_channels.py` handles all voice channel lifecycle logic. Uses `on_voice_state_update` for channel management and `asyncio.Task` for 10-second deletion timers. Archives messages as embeds to threads in the archive channel.

**Tech Stack:** Python 3.10+, discord.py, SQLAlchemy, asyncio

---

### Task 1: Create Cog structure and constants

**Covers:** S1, S2

**Files:**
- Create: `cogs/__init__.py`
- Create: `cogs/voice_channels.py`

- [ ] **Step 1: Create `cogs/__init__.py`**

```python
# Empty file for package
```

- [ ] **Step 2: Create `cogs/voice_channels.py` with Cog class and constants**

```python
import asyncio
import re
from datetime import datetime, timezone

import discord
from discord.ext import commands

VOICE_CATEGORY_ID = 1517577490368041200
ARCHIVE_CHANNEL_ID = 1517446069192102003
VOICE_CHANNEL_PREFIX = "Голосовой #"
NEW_VOICE_NAME = "Новый войс"
EMPTY_TIMEOUT_SECONDS = 10


class VoiceChannels(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.active_timers: dict[int, asyncio.Task] = {}
        self.next_number: int = 0

    @commands.Cog.listener()
    async def on_ready(self):
        await self._ensure_new_voice_exists()

    async def _ensure_new_voice_exists(self):
        """Check for 'Новый войс' in category, create if missing."""
        guild = self.bot.get_guild(self.bot.guilds[0].id) if self.bot.guilds else None
        if not guild:
            return

        category = guild.get_channel(VOICE_CATEGORY_ID)
        if not category:
            return

        for channel in category.voice_channels:
            if channel.name == NEW_VOICE_NAME:
                return

        await category.create_voice_channel(NEW_VOICE_NAME)

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

    async def _handle_new_voice_join(self, channel: discord.VoiceChannel):
        """Rename 'Новый войс' to 'Голосовой #N' and create new 'Новый войс'."""
        self.next_number += 1
        new_name = f"{VOICE_CHANNEL_PREFIX}{self.next_number}"

        await channel.edit(name=new_name)

        category = channel.category
        if category:
            await category.create_voice_channel(NEW_VOICE_NAME)

    def _start_deletion_timer(self, channel: discord.VoiceChannel):
        """Start 10-second timer to delete empty channel."""
        task = asyncio.create_task(self._delete_after_timeout(channel))
        self.active_timers[channel.id] = task

    def _cancel_deletion_timer(self, channel_id: int):
        """Cancel deletion timer if someone joins."""
        if channel_id in self.active_timers:
            self.active_timers[channel_id].cancel()
            del self.active_timers[channel_id]

    async def _delete_after_timeout(self, channel: discord.VoiceChannel):
        """Wait 10 seconds, archive chat, then delete channel."""
        await asyncio.sleep(EMPTY_TIMEOUT_SECONDS)

        if channel.members:
            return

        await self._archive_chat(channel)
        await channel.delete(reason="Voice channel empty for 10 seconds")
        self.active_timers.pop(channel.id, None)

    async def _archive_chat(self, channel: discord.VoiceChannel):
        """Archive text messages from voice channel to thread."""
        archive_channel = self.bot.get_channel(ARCHIVE_CHANNEL_ID)
        if not archive_channel:
            return

        thread = await archive_channel.create_thread(
            name=channel.name,
            type=discord.ChannelType.public_thread
        )

        messages = [msg async for msg in channel.history(limit=100)]
        messages.reverse()

        for msg in messages:
            embed = discord.Embed(
                description=msg.content,
                color=discord.Color.blue(),
                timestamp=msg.created_at
            )
            embed.set_author(
                name=msg.author.display_name,
                icon_url=msg.author.display_avatar.url
            )

            if msg.attachments:
                attachment_text = "\n".join([a.url for a in msg.attachments])
                embed.add_field(name="Вложения", value=attachment_text, inline=False)

            await thread.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(VoiceChannels(bot))
```

- [ ] **Step 3: Register Cog in bot.py**

Modify `bot.py` line 1 to add Cog loading in `on_ready`:

```python
@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

    await bot.load_extension("cogs.voice_channels")  # Add this line

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # ... rest of existing code
```

- [ ] **Step 4: Test the bot starts without errors**

Run: `python bot.py`
Expected: Bot starts, 'Новый войс' channel appears in category

---

### Task 2: Handle number persistence and edge cases

**Covers:** S2, S3

**Files:**
- Modify: `cogs/voice_channels.py`

- [ ] **Step 1: Add number persistence using database or file**

Update `_handle_new_voice_join` to find highest existing number:

```python
async def _handle_new_voice_join(self, channel: discord.VoiceChannel):
    """Rename 'Новый войс' to 'Голосовой #N' and create new 'Новый войс'."""
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

    self.next_number = max_number + 1
    new_name = f"{VOICE_CHANNEL_PREFIX}{self.next_number}"

    await channel.edit(name=new_name)
    await category.create_voice_channel(NEW_VOICE_NAME)
```

- [ ] **Step 2: Test with existing channels**

Create test channels "Голосовой #3" and "Голосовой #5" manually, then join "Новый войс". Should create "Голосовой #6".

---

### Task 3: Handle edge cases for archive

**Covers:** S4, S5

**Files:**
- Modify: `cogs/voice_channels.py`

- [ ] **Step 1: Handle empty chat and attachment-only messages**

Update archive method to handle edge cases:

```python
async def _archive_chat(self, channel: discord.VoiceChannel):
    """Archive text messages from voice channel to thread."""
    archive_channel = self.bot.get_channel(ARCHIVE_CHANNEL_ID)
    if not archive_channel:
        return

    thread = await archive_channel.create_thread(
        name=channel.name,
        type=discord.ChannelType.public_thread
    )

    messages = [msg async for msg in channel.history(limit=100)]
    messages.reverse()

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
```

- [ ] **Step 2: Commit implementation**

```bash
git add cogs/__init__.py cogs/voice_channels.py bot.py
git commit -m "feat: add temporary voice channels with auto-archive"
```
