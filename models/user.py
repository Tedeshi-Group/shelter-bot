from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.message_counter import MessageCounter
    from models.user_achievement import UserAchievement
    from models.voice_session import VoiceSession


class User(Base):
    __tablename__ = "users"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    total_messages: Mapped[int] = mapped_column(Integer, default=0)
    friendship_points: Mapped[int] = mapped_column(Integer, default=0)
    steam_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    steam_nickname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    steam_avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    blocked_sending: Mapped[bool] = mapped_column(default=False)  # blocked from sending tokens
    blocked_creating: Mapped[bool] = mapped_column(default=False)  # blocked from creating requests

    sessions: Mapped[list[VoiceSession]] = relationship(back_populates="user")
    messages: Mapped[list[MessageCounter]] = relationship(back_populates="user")
    achievements: Mapped[list[UserAchievement]] = relationship(back_populates="user")
