from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.achievement import Achievement
    from models.user import User


class UserAchievement(Base):
    __tablename__ = "user_achievements"
    __table_args__ = (
        UniqueConstraint("user_discord_id", "achievement_id", name="uq_user_achievement"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_discord_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"))
    achievement_id: Mapped[int] = mapped_column(Integer, ForeignKey("achievements.id"))
    level: Mapped[int] = mapped_column(Integer)
    unlocked_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    user: Mapped[User] = relationship(back_populates="achievements")
    achievement: Mapped[Achievement] = relationship(back_populates="user_achievements")
