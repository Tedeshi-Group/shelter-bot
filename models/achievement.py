from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.achievement_level import AchievementLevel
    from models.user_achievement import UserAchievement


class Achievement(Base):
    __tablename__ = "achievements"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    display_name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(String(500))
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)
    max_level: Mapped[int] = mapped_column(Integer)

    levels: Mapped[list[AchievementLevel]] = relationship(back_populates="achievement", order_by="AchievementLevel.level")
    user_achievements: Mapped[list[UserAchievement]] = relationship(back_populates="achievement")
