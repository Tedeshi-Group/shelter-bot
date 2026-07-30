from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.achievement import Achievement


class AchievementLevel(Base):
    __tablename__ = "achievement_levels"

    id: Mapped[int] = mapped_column(primary_key=True)
    achievement_id: Mapped[int] = mapped_column(Integer, ForeignKey("achievements.id"))
    level: Mapped[int] = mapped_column(Integer)
    threshold: Mapped[int] = mapped_column(Integer)
    role_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    name: Mapped[str] = mapped_column(String(100))

    achievement: Mapped[Achievement] = relationship(back_populates="levels")
