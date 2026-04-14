"""
HTTP Basic Auth for the PM website.

Priority:
  1. If any PMUser rows exist in the DB → validate against those (bcrypt hashes).
  2. Otherwise → fall back to PM_USERNAME / PM_PASSWORD env vars.

Returns the authenticated username so routes can attribute actions to a PM.
"""

import os
import secrets
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from website.database import get_db
from website.models import PMUser

security = HTTPBasic()

def _verify(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

PM_USERNAME = os.environ.get("PM_USERNAME", "admin")
PM_PASSWORD = os.environ.get("PM_PASSWORD", "")


async def require_auth(
    credentials: HTTPBasicCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> str:
    """
    FastAPI dependency — validates Basic Auth credentials.
    Returns the authenticated username.
    """
    username = credentials.username
    password = credentials.password

    # ── DB mode: check against PM users table ─────────────────────────────────
    result = await db.execute(select(PMUser))
    db_users = result.scalars().all()

    if db_users:
        user = next((u for u in db_users if u.username == username), None)
        if user and _verify(password, user.password_hash):
            return username
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    # ── Env-var fallback (no PM users in DB yet) ───────────────────────────────
    if not PM_PASSWORD:
        raise RuntimeError("PM_PASSWORD env var not set and no PM users in DB.")

    username_ok = secrets.compare_digest(username.encode(), PM_USERNAME.encode())
    password_ok = secrets.compare_digest(password.encode(), PM_PASSWORD.encode())

    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return username
