import asyncio
import logging
import os
import subprocess
import sys
import traceback
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from cogs.bot_manager import BotManager
from cogs.recording_commands import RecordingCommands
from s3_client import S3Client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)-8s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

TOKEN = os.getenv('MASTER_BOT_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID', '1307622842048839731'))

intents = discord.Intents.default()
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)


@bot.event
async def setup_hook():
    """Load cogs before on_ready fires."""
    # Initialize BotManager and S3Client
    bot.bot_manager = BotManager(max_workers=5)
    bot.s3_client = S3Client()

    # Initialize bot manager (starts worker bots)
    await bot.bot_manager.initialize()

    # Load recording commands
    try:
        await bot.add_cog(RecordingCommands(bot, bot.bot_manager, bot.s3_client))
        logging.info("Loaded cog: recording_commands")
    except Exception:
        logging.error("Ошибка загрузки кога recording_commands:\n%s", traceback.format_exc())


@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

    print(f'Мастер-бот {bot.user} запущен и готов к работе!')
    print(f'Воркеров: {len(bot.bot_manager.workers)}')

    # Auto-join existing voice channels with people
    await _auto_join_channels()


async def _auto_join_channels():
    """Find all voice channels with people and assign workers."""
    await asyncio.sleep(3)  # Wait for workers to fully connect

    channels_to_record = []
    for guild in bot.guilds:
        for channel in guild.voice_channels:
            # Skip empty channels and AFK channels
            if not channel.members:
                continue
            if guild.afk_channel and channel.id == guild.afk_channel.id:
                continue
            # Skip channels that only have bots
            human_members = [m for m in channel.members if not m.bot]
            if not human_members:
                continue
            channels_to_record.append(channel)

    if not channels_to_record:
        logging.info("No voice channels with people found on startup")
        return

    logging.info(f"Found {len(channels_to_record)} voice channels with people, assigning workers...")

    assigned = 0
    for channel in channels_to_record:
        worker = await bot.bot_manager.assign_channel(channel)
        if worker:
            logging.info(f"Auto-joined: {channel.name} -> Worker {worker.worker_id}")
            assigned += 1
        else:
            logging.warning(f"No free worker for: {channel.name}")

    logging.info(f"Auto-join complete: {assigned}/{len(channels_to_record)} channels assigned")


@bot.event
async def on_close():
    """Cleanup on bot shutdown."""
    if hasattr(bot, 'bot_manager') and bot.bot_manager:
        await bot.bot_manager.shutdown()
        logging.info("BotManager shutdown complete")


@bot.command(name='ping')
async def ping(ctx):
    await ctx.send('Pong!')


@bot.command(name='workers')
async def workers_status(ctx):
    """Show status of all worker bots."""
    if not hasattr(bot, 'bot_manager'):
        await ctx.send('BotManager не инициализирован.')
        return

    status = bot.bot_manager.get_status()

    lines = ['**Статус воркеров:**\n']
    for worker in status['workers']:
        if worker['is_busy']:
            duration = worker.get('recording_duration_seconds', 0)
            minutes = duration // 60
            seconds = duration % 60
            lines.append(
                f"🟢 Воркер {worker['worker_id']}: записывает "
                f"**{worker['current_channel']}** ({minutes:02d}:{seconds:02d})"
            )
        else:
            lines.append(f"⚪ Воркер {worker['worker_id']}: свободен")

    lines.append(f"\n📋 Очередь: {status['queue_size']} каналов")
    lines.append(f"📊 Загрузка: {status['busy_workers']}/{status['total_workers']} занято")

    await ctx.send('\n'.join(lines))


bot.run(TOKEN)
