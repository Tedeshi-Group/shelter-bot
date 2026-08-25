import logging
import os
import traceback
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from database import AsyncSessionLocal
from models import User, VoiceSession, Achievement, AchievementLevel, UserAchievement

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
GUILD_ID = int(os.getenv('GUILD_ID', '1307622842048839731'))

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)


@tasks.loop(minutes=10)
async def refresh_sessions():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with AsyncSessionLocal() as db:
        open_sessions = (await db.execute(
            select(VoiceSession).where(VoiceSession.left_at.is_(None))
        )).scalars().all()

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

        await db.commit()


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
        logging.error("Ошибка загрузки кога voice_channels:\n%s", traceback.format_exc())

    try:
        await bot.load_extension("cogs.moderation")
    except Exception:
        logging.error("Ошибка загрузки кога moderation:\n%s", traceback.format_exc())

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with AsyncSessionLocal() as db:
        for g in bot.guilds:
            for voice_channel in g.voice_channels:
                for member in voice_channel.members:
                    if member.bot:
                        continue
                    user = (await db.execute(
                        select(User).where(User.discord_id == member.id)
                    )).scalar_one_or_none()
                    if user is None:
                        user = User(discord_id=member.id, username=member.name)
                        db.add(user)
                    exists = (await db.execute(
                        select(VoiceSession)
                        .where(VoiceSession.user_discord_id == member.id)
                        .where(VoiceSession.left_at.is_(None))
                    )).scalars().first()
                    if not exists:
                        db.add(VoiceSession(
                            user_discord_id=member.id,
                            channel_id=voice_channel.id,
                            joined_at=now,
                        ))
        await db.commit()

    refresh_sessions.start()
    print(f'Бот {bot.user} запущен и готов к работе!')


@bot.command(name='ping')
async def ping(ctx):
    await ctx.send('Pong!')


@bot.tree.command(name='stats', description='Показать суммарное время в голосовых каналах')
async def stats(interaction: discord.Interaction):
    async with AsyncSessionLocal() as db:
        results = (await db.execute(
            select(
                User.discord_id,
                User.username,
                func.coalesce(func.sum(VoiceSession.duration_seconds), 0).label('total_seconds'),
            )
            .join(VoiceSession, User.discord_id == VoiceSession.user_discord_id)
            .where(VoiceSession.duration_seconds.isnot(None))
            .group_by(User.discord_id)
            .order_by(func.sum(VoiceSession.duration_seconds).desc())
        )).all()

        if not results:
            await interaction.response.send_message('Пока нет данных о голосовой активности.')
            return

        lines = ['**Статистика по голосовым каналам:**\n']
        for i, (discord_id, username, total_seconds) in enumerate(results, 1):
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            lines.append(f'{i}. **{username}** — {hours}ч {minutes}м')

        await interaction.response.send_message('\n'.join(lines))


@bot.tree.command(name='time', description='Показать время пользователя в голосовых каналах')
@app_commands.describe(
    user='Пользователь (по умолчанию вы)',
    period='Период: day, week, month или all (по умолчанию all)'
)
@app_commands.choices(period=[
    app_commands.Choice(name='За день', value='day'),
    app_commands.Choice(name='За неделю', value='week'),
    app_commands.Choice(name='За месяц', value='month'),
    app_commands.Choice(name='Всё время', value='all'),
])
async def time(interaction: discord.Interaction, user: discord.Member = None, period: str = 'all'):
    if user is None:
        user = interaction.user

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    
    if period == 'day':
        cutoff = now - timedelta(days=1)
        period_text = 'за последний день'
    elif period == 'week':
        cutoff = now - timedelta(weeks=1)
        period_text = 'за последнюю неделю'
    elif period == 'month':
        cutoff = now - timedelta(days=30)
        period_text = 'за последний месяц'
    else:
        cutoff = None
        period_text = 'за всё время'

    async with AsyncSessionLocal() as db:
        query = select(
            func.coalesce(func.sum(VoiceSession.duration_seconds), 0).label('total_seconds'),
        ).where(VoiceSession.user_discord_id == user.id)
        
        if cutoff:
            query = query.where(VoiceSession.joined_at >= cutoff)
        
        query = query.where(VoiceSession.duration_seconds.isnot(None))
        
        result = (await db.execute(query)).scalar()
        total_seconds = result or 0

        if period == 'all':
            open_session = (await db.execute(
                select(VoiceSession)
                .where(VoiceSession.user_discord_id == user.id)
                .where(VoiceSession.left_at.is_(None))
            )).scalar_one_or_none()

            if open_session:
                open_seconds = int((now - open_session.joined_at).total_seconds())
                total_seconds += open_seconds

        hours = total_seconds // 3600
        minutes = (total_seconds % 3600) // 60

        await interaction.response.send_message(
            f'**{user.display_name}** провёл в голосовых каналах {period_text}: **{hours}ч {minutes}м**',
            ephemeral=True
        )


@bot.tree.command(name='monthly', description='Показать статистику за месяц')
@app_commands.describe(month='Месяц в формате YYYY-MM (по умолчанию текущий)')
async def monthly(interaction: discord.Interaction, month: str | None = None):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    
    if month:
        try:
            year, mon = map(int, month.split('-'))
            if not (1 <= mon <= 12):
                raise ValueError
        except ValueError:
            await interaction.response.send_message(
                'Неверный формат месяца. Используйте YYYY-MM (например, 2026-07).',
                ephemeral=True
            )
            return
    else:
        year = now.year
        mon = now.month
    
    month_start = datetime(year, mon, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    if mon == 12:
        month_end = datetime(year + 1, 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)
    else:
        month_end = datetime(year, mon + 1, 1, tzinfo=timezone.utc).replace(tzinfo=None)

    async with AsyncSessionLocal() as db:
        results = (await db.execute(
            select(
                User.discord_id,
                User.username,
                func.coalesce(func.sum(VoiceSession.duration_seconds), 0).label('total_seconds'),
            )
            .join(VoiceSession, User.discord_id == VoiceSession.user_discord_id)
            .where(VoiceSession.joined_at >= month_start)
            .where(VoiceSession.joined_at < month_end)
            .where(VoiceSession.duration_seconds.isnot(None))
            .group_by(User.discord_id)
            .order_by(func.sum(VoiceSession.duration_seconds).desc())
        )).all()

        if not results:
            await interaction.response.send_message(
                f'Нет данных за {mon:02d}.{year}.',
                ephemeral=True
            )
            return

        month_names = {
            1: 'Январь', 2: 'Февраль', 3: 'Март', 4: 'Апрель',
            5: 'Май', 6: 'Июнь', 7: 'Июль', 8: 'Август',
            9: 'Сентябрь', 10: 'Октябрь', 11: 'Ноябрь', 12: 'Декабрь'
        }
        
        lines = [f'**Статистика за {month_names[mon]} {year}:**\n']
        for i, (discord_id, username, total_seconds) in enumerate(results, 1):
            hours = total_seconds // 3600
            minutes = (total_seconds % 3600) // 60
            lines.append(f'{i}. **{username}** — {hours}ч {minutes}м')

        await interaction.response.send_message('\n'.join(lines), ephemeral=True)


@bot.tree.command(name='achievements', description='Показать достижения пользователя')
@app_commands.describe(user='Пользователь (по умолчанию вы)')
async def achievements(interaction: discord.Interaction, user: discord.Member = None):
    if user is None:
        user = interaction.user

    async with AsyncSessionLocal() as db:
        all_achievements = (await db.execute(
            select(Achievement).options(selectinload(Achievement.levels)).order_by(Achievement.id)
        )).scalars().all()

        user_achievements = (await db.execute(
            select(UserAchievement)
            .where(UserAchievement.user_discord_id == user.id)
        )).scalars().all()

        ua_map = {ua.achievement_id: ua for ua in user_achievements}

        lines = [f'**Достижения {user.display_name}:**\n']
        for ach in all_achievements:
            ua = ua_map.get(ach.id)
            if ua:
                level_name = next(
                    (l.name for l in ach.levels if l.level == ua.level),
                    f"Уровень {ua.level}"
                )
                lines.append(f'{ach.icon or "🏆"} **{level_name}** — {ach.description}')
            else:
                lines.append(f'{ach.icon or "🔒"} ~~{ach.display_name}~~ — не получено')

        await interaction.response.send_message('\n'.join(lines), ephemeral=True)


@bot.tree.command(name='setrole', description='Назначить роль для уровня достижения (только админы)')
@app_commands.describe(
    achievement='Название достижения',
    level='Уровень достижения',
    role='Роль для выдачи',
)
@app_commands.choices(achievement=[
    app_commands.Choice(name='Вояка', value='voice_total'),
    app_commands.Choice(name='Непрерывник', value='voice_longest_session'),
    app_commands.Choice(name='Марафонец', value='voice_streak'),
    app_commands.Choice(name='Болтун', value='messages_total'),
    app_commands.Choice(name='Одинокий волк', value='voice_lone_wolf'),
])
async def setrole(interaction: discord.Interaction, achievement: str, level: int, role: discord.Role):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message('Только администраторы могут использовать эту команду.', ephemeral=True)
        return

    async with AsyncSessionLocal() as db:
        ach = (await db.execute(
            select(Achievement).where(Achievement.name == achievement)
        )).scalar_one_or_none()

        if not ach:
            await interaction.response.send_message('Достижение не найдено.', ephemeral=True)
            return

        ach_level = (await db.execute(
            select(AchievementLevel)
            .where(AchievementLevel.achievement_id == ach.id)
            .where(AchievementLevel.level == level)
        )).scalar_one_or_none()

        if not ach_level:
            await interaction.response.send_message(f'Уровень {level} не найден для достижения "{ach.display_name}".', ephemeral=True)
            return

        ach_level.role_id = role.id
        await db.commit()

    # Retroactive role assignment
    assigned = 0
    async with AsyncSessionLocal() as db:
        holders = (await db.execute(
            select(UserAchievement.user_discord_id)
            .where(UserAchievement.achievement_id == ach.id)
            .where(UserAchievement.level >= level)
        )).scalars().all()

        guild = interaction.guild
        for uid in holders:
            member = guild.get_member(uid)
            if member and role not in member.roles:
                try:
                    await member.add_roles(role, reason=f"Achievement: {ach_level.name}")
                    assigned += 1
                except (discord.HTTPException, discord.Forbidden):
                    pass

    await interaction.response.send_message(
        f'Роль {role.mention} назначена для **{ach_level.name}**.\n'
        f'Выдана {assigned} участникам, которые уже получили это достижение.',
        ephemeral=True
    )


bot.run(TOKEN)
