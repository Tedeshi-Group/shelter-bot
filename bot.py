import os
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from sqlalchemy import select, func

from database import SessionLocal
from models import User, VoiceSession

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID', '1307622842048839731'))

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix='!', intents=intents)


@tasks.loop(minutes=10)
async def refresh_sessions():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    try:
        open_sessions = db.execute(
            select(VoiceSession).where(VoiceSession.left_at.is_(None))
        ).scalars().all()

        still_in_voice = {}

        for guild in bot.guilds:
            for voice_channel in guild.voice_channels:
                for member in voice_channel.members:
                    if not member.bot:
                        still_in_voice[member.id] = voice_channel.id

        for session in open_sessions:
            session.left_at = now
            session.duration_seconds = int((now - session.joined_at).total_seconds())

            if session.user_discord_id in still_in_voice:
                new_session = VoiceSession(
                    user_discord_id=session.user_discord_id,
                    channel_id=still_in_voice[session.user_discord_id],
                    joined_at=now,
                )
                db.add(new_session)

        db.commit()
    finally:
        db.close()


@refresh_sessions.before_loop
async def before_refresh_sessions():
    await bot.wait_until_ready()


@bot.event
async def on_ready():
    guild = discord.Object(id=GUILD_ID)
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)

    try:
        await bot.load_extension("cogs.voice_channels")
        voice_cog = bot.get_cog("VoiceChannels")
        if voice_cog:
            await voice_cog._ensure_new_voice_exists()
    except Exception:
        pass

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db = SessionLocal()
    try:
        for g in bot.guilds:
            for voice_channel in g.voice_channels:
                for member in voice_channel.members:
                    if member.bot:
                        continue
                    user = db.execute(
                        select(User).where(User.discord_id == member.id)
                    ).scalar_one_or_none()
                    if user is None:
                        user = User(discord_id=member.id, username=member.name)
                        db.add(user)
                    exists = db.execute(
                        select(VoiceSession)
                        .where(VoiceSession.user_discord_id == member.id)
                        .where(VoiceSession.left_at.is_(None))
                    ).scalars().first()
                    if not exists:
                        db.add(VoiceSession(
                            user_discord_id=member.id,
                            channel_id=voice_channel.id,
                            joined_at=now,
                        ))
        db.commit()
    finally:
        db.close()

    refresh_sessions.start()
    print(f'Бот {bot.user} запущен и готов к работе!')


@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    db = SessionLocal()
    try:
        user = db.execute(
            select(User).where(User.discord_id == member.id)
        ).scalar_one_or_none()

        if user is None:
            user = User(discord_id=member.id, username=member.name)
            db.add(user)
            db.commit()

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        if before.channel is None and after.channel is not None:
            session = VoiceSession(
                user_discord_id=member.id,
                channel_id=after.channel.id,
                joined_at=now,
            )
            db.add(session)
            db.commit()

        elif before.channel is not None and after.channel is None:
            open_sessions = db.execute(
                select(VoiceSession)
                .where(VoiceSession.user_discord_id == member.id)
                .where(VoiceSession.left_at.is_(None))
            ).scalars().all()

            for session in open_sessions:
                session.left_at = now
                session.duration_seconds = int((now - session.joined_at).total_seconds())
                db.commit()

        elif before.channel.id != after.channel.id:
            old_sessions = db.execute(
                select(VoiceSession)
                .where(VoiceSession.user_discord_id == member.id)
                .where(VoiceSession.left_at.is_(None))
            ).scalars().all()

            for old_session in old_sessions:
                old_session.left_at = now
                old_session.duration_seconds = int((now - old_session.joined_at).total_seconds())

            new_session = VoiceSession(
                user_discord_id=member.id,
                channel_id=after.channel.id,
                joined_at=now,
            )
            db.add(new_session)
            db.commit()
    finally:
        db.close()


@bot.command(name='ping')
async def ping(ctx):
    await ctx.send('Pong!')


@bot.tree.command(name='stats', description='Показать суммарное время в голосовых каналах')
async def stats(interaction: discord.Interaction):
    db = SessionLocal()
    try:
        results = (
            db.execute(
                select(
                    User.discord_id,
                    User.username,
                    func.coalesce(func.sum(VoiceSession.duration_seconds), 0).label('total_seconds'),
                )
                .join(VoiceSession, User.discord_id == VoiceSession.user_discord_id)
                .where(VoiceSession.duration_seconds.isnot(None))
                .group_by(User.discord_id)
                .order_by(func.sum(VoiceSession.duration_seconds).desc())
            )
            .all()
        )

        if not results:
            await interaction.response.send_message('Пока нет данных о голосовой активности.')
            return

        lines = ['**Статистика по голосовым каналам:**\n']
        for i, (discord_id, username, total_seconds) in enumerate(results, 1):
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            lines.append(f'{i}. **{username}** — {hours}ч {minutes}м')

        await interaction.response.send_message('\n'.join(lines))
    finally:
        db.close()


@bot.tree.command(name='time', description='Показать время пользователя в голосовых каналах')
async def time(interaction: discord.Interaction, user: discord.Member = None):
    if user is None:
        user = interaction.user

    db = SessionLocal()
    try:
        result = db.execute(
            select(
                func.coalesce(func.sum(VoiceSession.duration_seconds), 0).label('total_seconds'),
            )
            .where(VoiceSession.user_discord_id == user.id)
            .where(VoiceSession.duration_seconds.isnot(None))
        ).scalar()

        total_seconds = result or 0
        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        # Also add currently open session time
        open_session = db.execute(
            select(VoiceSession)
            .where(VoiceSession.user_discord_id == user.id)
            .where(VoiceSession.left_at.is_(None))
        ).scalar_one_or_none()

        if open_session:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            open_seconds = int((now - open_session.joined_at).total_seconds())
            total_seconds += open_seconds
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60

        await interaction.response.send_message(
            f'**{user.display_name}** провёл в голосовых каналах: **{hours}ч {minutes}м**',
            ephemeral=True
        )
    finally:
        db.close()


bot.run(TOKEN)
