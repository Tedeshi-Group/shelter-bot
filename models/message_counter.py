from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.user import User


class MessageCounter(Base):
    __tablename__ = "message_counters"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_discord_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"))
    channel_id: Mapped[int] = mapped_column(BigInteger)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    user: Mapped[User] = relationship(back_populates="messages")
