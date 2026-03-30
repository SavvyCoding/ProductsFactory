"""
Shared pytest fixtures for ProductFactory tests.
Uses a real PostgreSQL DB (not mocks) — set TEST_DATABASE_URL before running.

  TEST_DATABASE_URL=postgresql://user:pass@localhost/productfactory_test pytest
"""

import os
import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from website.models import Base


@pytest.fixture(scope="session")
def db_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", os.environ.get("DATABASE_URL", ""))
    if not url:
        pytest.skip("TEST_DATABASE_URL not set — skipping DB tests")
    return (
        url
        .replace("postgresql+asyncpg://", "postgresql+psycopg2://")
        .replace("postgresql://", "postgresql+psycopg2://")
    )


@pytest.fixture(scope="session")
def engine(db_url):
    _engine = create_engine(db_url)
    Base.metadata.create_all(_engine)
    yield _engine
    Base.metadata.drop_all(_engine)
    _engine.dispose()


@pytest.fixture
def db_session(engine) -> Session:
    """Each test gets a rolled-back transaction — no cleanup needed."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    yield session
    session.close()
    transaction.rollback()
    connection.close()
