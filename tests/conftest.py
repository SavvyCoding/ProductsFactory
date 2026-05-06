"""
Shared pytest fixtures for ProductFactory tests.

Hard rule (after 2× production-wipe incidents on 2026-05-03 and 2026-05-05):
TEST_DATABASE_URL MUST be explicitly set to a database whose name contains
the substring "test", and MUST NOT be the production database. There is NO
fallback to DATABASE_URL — that fallback is what wiped production both times.

Run:
    TEST_DATABASE_URL=postgresql://user:pass@localhost/productfactory_test pytest

One-time setup of the test database:
    docker exec ProductsFactoryDB createdb -U productfactory productfactory_test
    DATABASE_URL=postgresql://...:.../productfactory_test \\
        alembic -c alembic.ini upgrade head
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from website.models import Base


# Names that MUST never be used as a test database. Production lives at
# `productfactory`; the bare `postgres` admin DB and an empty name are also
# refused so a malformed URL doesn't slip through.
_BANNED_DB_NAMES = frozenset({"productfactory", "postgres", "", None})


def _validate_test_db_url(url_str: str) -> None:
    """
    Refuse to run pytest against the production database. Aborts the entire
    pytest session via ``pytest.exit`` (returncode 2) — not ``skip``, because
    skipping just one test isn't enough to prevent ``drop_all`` from firing
    on the session-scoped engine teardown of any OTHER DB-touching test.

    Two guards:
      1. DB name must not be in _BANNED_DB_NAMES (productfactory, postgres, "")
      2. DB name MUST contain "test" — forces the convention that test
         databases self-identify as ephemeral.
    """
    parsed = make_url(url_str)
    db_name = (parsed.database or "").lower()
    if db_name in _BANNED_DB_NAMES:
        pytest.exit(
            f"REFUSING to run pytest: TEST_DATABASE_URL points at database "
            f"'{db_name}' — this is a banned name (production / admin DB). "
            f"Set TEST_DATABASE_URL to a separate DB whose name contains "
            f"'test', e.g. postgresql://.../productfactory_test",
            returncode=2,
        )
    if "test" not in db_name:
        pytest.exit(
            f"REFUSING to run pytest: TEST_DATABASE_URL points at database "
            f"'{db_name}' — its name must contain the substring 'test'. "
            f"This guard exists because conftest used to call drop_all on "
            f"every session teardown and wiped production twice when "
            f"TEST_DATABASE_URL was misconfigured to the prod URL. The bar "
            f"is now: never run against any DB that doesn't self-identify "
            f"as ephemeral.",
            returncode=2,
        )


@pytest.fixture(scope="session")
def db_url() -> str:
    """
    Resolve TEST_DATABASE_URL or skip. NO fallback to DATABASE_URL.

    Two prior incidents both went through the old fallback chain
    ``os.environ.get("TEST_DATABASE_URL", os.environ.get("DATABASE_URL", ""))``
    — when TEST_DATABASE_URL was unset or misconfigured, conftest silently
    fell back to DATABASE_URL and the engine fixture's drop_all wiped prod.
    The fallback is gone for good.
    """
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set — skipping DB tests")
    _validate_test_db_url(url)
    return (
        url
        .replace("postgresql+asyncpg://", "postgresql+psycopg2://")
        .replace("postgresql://", "postgresql+psycopg2://")
    )


@pytest.fixture(scope="session")
def engine(db_url):
    """
    Session-scoped SQLAlchemy engine.

    NO drop_all on teardown. Per-test transactional rollback (via the
    ``db_session`` fixture below) is the only cleanup mechanism. Removing
    drop_all eliminates the destructive footgun that fired in both
    production-wipe incidents.

    ``create_all`` is kept as an idempotent safety net — only creates
    tables that don't already exist; never touches data. The test DB is
    expected to be pre-migrated:
        alembic -c alembic.ini upgrade head   (with TEST_DATABASE_URL set)
    """
    _engine = create_engine(db_url)
    Base.metadata.create_all(_engine)
    yield _engine
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
