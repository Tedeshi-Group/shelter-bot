from __future__ import annotations

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, mapped_column

from database import Base


class VoiceCounter(Base):
    __tablename__ = "voice_counters"

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[int] = mapped_column(Integer)
    year: Mapped[int] = mapped_column(Integer)
    count: Mapped[int] = mapped_column(Integer, default=0)
