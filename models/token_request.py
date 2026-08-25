from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.user import User


class TokenRequest(Base):
    __tablename__ = "token_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    requester_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open, in_progress, confirmed, disputed, rejected, closed
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    channel_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    creation_bonus: Mapped[bool] = mapped_column(default=False)  # eligible for creation bonus
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    requester: Mapped[User] = relationship(foreign_keys=[requester_id])
    tokens: Mapped[list[TokenRequestItem]] = relationship(back_populates="request", cascade="all, delete-orphan")
    fulfillment: Mapped[TokenFulfillment | None] = relationship(back_populates="request", uselist=False)


class TokenRequestItem(Base):
    __tablename__ = "token_request_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(Integer, ForeignKey("token_requests.id"))
    token_id: Mapped[int] = mapped_column(Integer, ForeignKey("dota_tokens.id"))
    fulfilled: Mapped[bool] = mapped_column(default=False)
    fulfilled_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fulfilled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    request: Mapped[TokenRequest] = relationship(back_populates="tokens")


class TokenFulfillment(Base):
    __tablename__ = "token_fulfillments"

    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[int] = mapped_column(Integer, ForeignKey("token_requests.id"), unique=True)
    fulfiller_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )

    request: Mapped[TokenRequest] = relationship(back_populates="fulfillment")
