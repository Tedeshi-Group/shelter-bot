"""Seed achievements and their levels."""
from sqlalchemy import select
from database import SessionLocal
from models import Achievement, AchievementLevel


ACHIEVEMENTS = [
    {
        "name": "voice_total",
        "display_name": "Вояка",
        "description": "Провести в голосовых каналах суммарно N часов",
        "icon": "🎙️",
        "max_level": 7,
        "levels": [
            {"level": 1, "threshold": 7200, "name": "Вояка I"},      # 2h
            {"level": 2, "threshold": 14400, "name": "Вояка II"},     # 4h
            {"level": 3, "threshold": 28800, "name": "Вояка III"},    # 8h
            {"level": 4, "threshold": 57600, "name": "Вояка IV"},     # 16h
            {"level": 5, "threshold": 115200, "name": "Вояка V"},     # 32h
            {"level": 6, "threshold": 230400, "name": "Вояка VI"},    # 64h
            {"level": 7, "threshold": 460800, "name": "Вояка VII"},   # 128h
        ],
    },
    {
        "name": "voice_longest_session",
        "display_name": "Непрерывник",
        "description": "Провести в голосовом канале непрерывно N часов",
        "icon": "⏰",
        "max_level": 4,
        "levels": [
            {"level": 1, "threshold": 3600, "name": "Непрерывник I"},    # 1h
            {"level": 2, "threshold": 7200, "name": "Непрерывник II"},   # 2h
            {"level": 3, "threshold": 10800, "name": "Непрерывник III"}, # 3h
            {"level": 4, "threshold": 18000, "name": "Непрерывник IV"},  # 5h
        ],
    },
    {
        "name": "voice_streak",
        "display_name": "Марафонец",
        "description": "Заходить в голосовые каналы N дней подряд",
        "icon": "🔥",
        "max_level": 4,
        "levels": [
            {"level": 1, "threshold": 3, "name": "Марафонец I"},     # 3 days
            {"level": 2, "threshold": 7, "name": "Марафонец II"},    # 7 days
            {"level": 3, "threshold": 14, "name": "Марафонец III"},  # 14 days
            {"level": 4, "threshold": 30, "name": "Марафонец IV"},   # 30 days
        ],
    },
    {
        "name": "messages_total",
        "display_name": "Болтун",
        "description": "Написать суммарно N сообщений",
        "icon": "💬",
        "max_level": 5,
        "levels": [
            {"level": 1, "threshold": 100, "name": "Болтун I"},      # 100
            {"level": 2, "threshold": 500, "name": "Болтун II"},     # 500
            {"level": 3, "threshold": 1000, "name": "Болтун III"},   # 1000
            {"level": 4, "threshold": 5000, "name": "Болтун IV"},    # 5000
            {"level": 5, "threshold": 10000, "name": "Болтун V"},    # 10000
        ],
    },
    {
        "name": "voice_lone_wolf",
        "display_name": "Одинокий волк",
        "description": "Находиться в голосовом канале одному N минут",
        "icon": "🐺",
        "max_level": 3,
        "levels": [
            {"level": 1, "threshold": 1800, "name": "Одинокий волк I"},    # 30m
            {"level": 2, "threshold": 3600, "name": "Одинокий волк II"},   # 1h
            {"level": 3, "threshold": 7200, "name": "Одинокий волк III"},  # 2h
        ],
    },
]


def seed_achievements():
    with SessionLocal() as db:
        for achievement_data in ACHIEVEMENTS:
            existing = db.execute(
                select(Achievement).where(Achievement.name == achievement_data["name"])
            ).scalar_one_or_none()

            if existing:
                print(f"Achievement '{achievement_data['name']}' already exists, skipping")
                continue

            achievement = Achievement(
                name=achievement_data["name"],
                display_name=achievement_data["display_name"],
                description=achievement_data["description"],
                icon=achievement_data["icon"],
                max_level=achievement_data["max_level"],
            )
            db.add(achievement)
            db.flush()

            for level_data in achievement_data["levels"]:
                level = AchievementLevel(
                    achievement_id=achievement.id,
                    level=level_data["level"],
                    threshold=level_data["threshold"],
                    name=level_data["name"],
                    role_id=None,  # Roles will be configured manually
                )
                db.add(level)

            print(f"Created achievement: {achievement_data['display_name']}")

        db.commit()
        print("Seeding complete!")


if __name__ == "__main__":
    seed_achievements()
