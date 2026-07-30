from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import ASYNC_DATABASE_URL, DATABASE_URL

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)

_async_session_factory = None


def get_async_session_factory():
    global _async_session_factory
    if _async_session_factory is None:
        async_eng = create_async_engine(ASYNC_DATABASE_URL)
        _async_session_factory = async_sessionmaker(async_eng, class_=AsyncSession, expire_on_commit=False)
    return _async_session_factory


class AsyncSessionLocal:
    def __init__(self):
        self._factory = get_async_session_factory()

    async def __aenter__(self):
        self._session = self._factory()
        return self._session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._session.close()


class Base(DeclarativeBase):
    pass
