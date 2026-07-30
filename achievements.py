"""Achievement checking and unlocking logic."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text

from database import AsyncSessionLocal
from models import (
    Achievement,
    AchievementLevel,
    User,
    UserAchievement,
    VoiceSession,
)


async def check_achievements(user_id: int, achievement_name: str) -> list[dict]:
    """Check if user qualifies for new achievement levels.
    
    Returns list of newly unlocked achievements with their level info.
    """
    unlocked = []
    
    async with AsyncSessionLocal() as db:
        achievement = (await db.execute(
            select(Achievement).where(Achievement.name == achievement_name)
        )).scalar_one_or_none()
        
        if not achievement:
            return unlocked
        
        current = (await db.execute(
            select(UserAchievement)
            .where(UserAchievement.user_discord_id == user_id)
            .where(UserAchievement.achievement_id == achievement.id)
        )).scalar_one_or_none()
        
        current_level = current.level if current else 0
        
        if current_level >= achievement.max_level:
            return unlocked
        
        metric_value = await _compute_metric(db, user_id, achievement_name)
        
        next_level = (await db.execute(
            select(AchievementLevel)
            .where(AchievementLevel.achievement_id == achievement.id)
            .where(AchievementLevel.level == current_level + 1)
        )).scalar_one_or_none()
        
        if not next_level:
            return unlocked
        
        if metric_value >= next_level.threshold:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if current:
                current.level = next_level.level
                current.unlocked_at = now
            else:
                await db.execute(
                    text(
                        "INSERT INTO user_achievements (user_discord_id, achievement_id, level, unlocked_at) "
                        "VALUES (:uid, :aid, :level, :now) "
                        "ON CONFLICT (user_discord_id, achievement_id) DO UPDATE SET level=:level, unlocked_at=:now"
                    ),
                    {"uid": user_id, "aid": achievement.id, "level": next_level.level, "now": now},
                )
            
            await db.commit()
            
            unlocked.append({
                "achievement": achievement,
                "level": next_level,
                "metric_value": metric_value,
            })
    
    return unlocked


async def _compute_metric(db, user_id: int, achievement_name: str) -> int:
    """Compute the current metric value for an achievement."""
    if achievement_name == "voice_total":
        return await _get_total_voice_time(db, user_id)
    elif achievement_name == "voice_longest_session":
        return await _get_longest_session(db, user_id)
    elif achievement_name == "voice_streak":
        return await _get_voice_streak(db, user_id)
    elif achievement_name == "messages_total":
        return await _get_total_messages(db, user_id)
    elif achievement_name == "voice_lone_wolf":
        return await _get_lone_wolf_time(db, user_id)
    return 0


async def _get_total_voice_time(db, user_id: int) -> int:
    """Get total voice time in seconds for a user."""
    result = await db.execute(
        select(func.sum(VoiceSession.duration_seconds))
        .where(VoiceSession.user_discord_id == user_id)
        .where(VoiceSession.duration_seconds.isnot(None))
    )
    total = result.scalar()
    
    open_sessions = (await db.execute(
        select(VoiceSession)
        .where(VoiceSession.user_discord_id == user_id)
        .where(VoiceSession.left_at.is_(None))
    )).scalars().all()
    
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for session in open_sessions:
        total += int((now - session.joined_at).total_seconds())
    
    return total or 0


async def _get_longest_session(db, user_id: int) -> int:
    """Get longest single voice session in seconds."""
    result = await db.execute(
        select(func.max(VoiceSession.duration_seconds))
        .where(VoiceSession.user_discord_id == user_id)
        .where(VoiceSession.duration_seconds.isnot(None))
    )
    longest = result.scalar()
    
    open_sessions = (await db.execute(
        select(VoiceSession)
        .where(VoiceSession.user_discord_id == user_id)
        .where(VoiceSession.left_at.is_(None))
    )).scalars().all()
    
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for session in open_sessions:
        current_duration = int((now - session.joined_at).total_seconds())
        if current_duration > (longest or 0):
            longest = current_duration
    
    return longest or 0


async def _get_voice_streak(db, user_id: int) -> int:
    """Get current consecutive days with voice activity."""
    today = datetime.now(timezone.utc).replace(tzinfo=None).date()
    
    streak = 0
    current_date = today
    
    while True:
        day_start = datetime.combine(current_date, datetime.min.time())
        day_end = datetime.combine(current_date, datetime.max.time())
        
        has_session = (await db.execute(
            select(VoiceSession.id)
            .where(VoiceSession.user_discord_id == user_id)
            .where(VoiceSession.joined_at >= day_start)
            .where(VoiceSession.joined_at <= day_end)
            .limit(1)
        )).scalar_one_or_none()
        
        if has_session:
            streak += 1
            current_date -= timedelta(days=1)
        else:
            break
    
    return streak


async def _get_total_messages(db, user_id: int) -> int:
    """Get total message count for a user."""
    user = (await db.execute(
        select(User).where(User.discord_id == user_id)
    )).scalar_one_or_none()
    
    return user.total_messages if user else 0


async def _get_lone_wolf_time(db, user_id: int) -> int:
    """Get total time spent alone in voice channels."""
    sessions = (await db.execute(
        select(VoiceSession)
        .where(VoiceSession.user_discord_id == user_id)
        .where(VoiceSession.duration_seconds.isnot(None))
    )).scalars().all()
    
    total_alone_time = 0
    
    for session in sessions:
        if await _was_alone_in_channel(db, session.channel_id, session.joined_at, session.left_at):
            total_alone_time += session.duration_seconds
    
    return total_alone_time


async def _was_alone_in_channel(db, channel_id: int, start_time: datetime, end_time: datetime) -> bool:
    """Check if user was alone in a channel during a time period."""
    other_sessions = (await db.execute(
        select(VoiceSession)
        .where(VoiceSession.channel_id == channel_id)
        .where(VoiceSession.joined_at < end_time)
        .where(
            (VoiceSession.left_at.is_(None)) | (VoiceSession.left_at > start_time)
        )
    )).scalars().all()
    
    return len(other_sessions) <= 1


async def check_all_achievements(user_id: int) -> list[dict]:
    """Check all achievements for a user."""
    all_unlocked = []
    
    achievement_names = [
        "voice_total",
        "voice_longest_session",
        "voice_streak",
        "messages_total",
        "voice_lone_wolf",
    ]
    
    for name in achievement_names:
        unlocked = await check_achievements(user_id, name)
        all_unlocked.extend(unlocked)
    
    return all_unlocked
