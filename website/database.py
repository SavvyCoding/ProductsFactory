"""
Database connection and session management.
Uses SQLAlchemy async engine + asyncpg driver for website runtime.
Alembic uses psycopg2 (sync) — see db/migrations/env.py.
"""

import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase


def _make_async_url(raw: str) -> str:
    """
    Normalise any postgresql:// variant to postgresql+asyncpg://.
    Handles: postgresql://, postgresql+psycopg2://, postgresql+asyncpg:// (already correct).
    """
    if "+asyncpg" in raw:
        return raw
    # Strip any existing driver specifier, then add asyncpg
    base = raw.split("://", 1)[1]
    return f"postgresql+asyncpg://{base}"


_raw_url = os.environ.get("DATABASE_URL", "")
DATABASE_URL = _make_async_url(_raw_url) if _raw_url else ""

engine = create_async_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=False,
) if DATABASE_URL else None

AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
) if engine else None


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncSession:
    """FastAPI dependency — yields a committed async DB session per request."""
    if AsyncSessionLocal is None:
        raise RuntimeError("DATABASE_URL not configured")
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
