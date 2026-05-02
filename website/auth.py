"""
HTTP Basic Auth for the PM website.

Priority:
  1. If any PMUser rows exist in the DB → validate against those (bcrypt hashes).
  2. Otherwise → fall back to PM_USERNAME / PM_PASSWORD env vars.

Includes an in-memory rate limiter: after AUTH_MAX_FAILS failed attempts for a
given username, that username is locked out for AUTH_LOCKOUT_SECONDS. Counters
reset on successful auth. State is per-process — a PM website restart clears
the lockout. For production we'd push this to Redis, but in-memory is
sufficient for the current single-worker uvicorn deployment.

Returns the authenticated username so routes can attribute actions to a PM.
"""

import os
import secrets
import threading
import time
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

# ── Rate limiter (per-username, in-memory) ────────────────────────────────────
# Configured from env so tests / ops can tune without code changes.
AUTH_MAX_FAILS       = int(os.environ.get("AUTH_MAX_FAILS", "5"))
AUTH_LOCKOUT_SECONDS = int(os.environ.get("AUTH_LOCKOUT_SECONDS", "900"))  # 15 min

_fail_state_lock = threading.Lock()
# username -> (fail_count, first_fail_ts, locked_until_ts)
_fail_state: dict[str, tuple[int, float, float]] = {}


def _check_lockout(username: str) -> None:
    """Raise 429 if the username is currently locked out. Otherwise no-op."""
    now = time.time()
    with _fail_state_lock:
        entry = _fail_state.get(username)
        if not entry:
            return
        _, _, locked_until = entry
        if locked_until and now < locked_until:
            retry_after = int(locked_until - now)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many failed attempts — try again in {retry_after}s",
                headers={
                    "Retry-After": str(retry_after),
                    "WWW-Authenticate": "Basic",
                },
            )


def _record_failure(username: str) -> None:
    """Increment the fail counter for username; lock out if threshold reached."""
    now = time.time()
    with _fail_state_lock:
        count, first_ts, _ = _fail_state.get(username, (0, now, 0.0))
        count += 1
        locked_until = now + AUTH_LOCKOUT_SECONDS if count >= AUTH_MAX_FAILS else 0.0
        _fail_state[username] = (count, first_ts, locked_until)


def _record_success(username: str) -> None:
    """Clear any lockout / fail count for username on successful auth."""
    with _fail_state_lock:
        _fail_state.pop(username, None)


def _reset_rate_limit_state_for_tests() -> None:
    """Test-only hook to clear all rate-limit state between tests."""
    with _fail_state_lock:
        _fail_state.clear()


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

    # Lockout check runs BEFORE any DB / bcrypt work, so a locked-out attacker
    # can't use us for timing oracles or cause DB load.
    _check_lockout(username)

    def _unauthorized() -> HTTPException:
        _record_failure(username)
        return HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    # ── DB mode: check against PM users table ─────────────────────────────────
    result = await db.execute(select(PMUser))
    db_users = result.scalars().all()

    if db_users:
        user = next((u for u in db_users if u.username == username), None)
        if user and _verify(password, user.password_hash):
            _record_success(username)
            return username
        raise _unauthorized()

    # ── Env-var fallback (no PM users in DB yet) ───────────────────────────────
    if not PM_PASSWORD:
        raise RuntimeError("PM_PASSWORD env var not set and no PM users in DB.")

    username_ok = secrets.compare_digest(username.encode(), PM_USERNAME.encode())
    password_ok = secrets.compare_digest(password.encode(), PM_PASSWORD.encode())

    if not (username_ok and password_ok):
        raise _unauthorized()
    _record_success(username)
    return username


# ── Internal API HMAC verification (Phase #9, opt-in) ────────────────────────
# When PF_INTERNAL_API_SECRET is set, write endpoints reachable from the
# orchestrator/agent network require an X-PF-Signature header containing
# `sha256=<hmac>` of the raw request body keyed by the secret. When the env
# var is unset, the dependency is a no-op (preserves current behavior, no
# operational risk on rollout).
#
# The orchestrator side computes the signature in deploy/orchestrator/tools.py
# (helper sign_internal_body) and includes it on every internal POST.
#
# Why opt-in: agent containers attach to productfactory-net and historically
# trust the network. Flipping enforcement on would require coordinated deploy
# of orchestrator + website + agent prompt updates. Operators can roll this
# out one product at a time, then enable globally.

import hashlib
import hmac as _hmac
from fastapi import Request

_INTERNAL_API_SECRET = os.environ.get("PF_INTERNAL_API_SECRET", "")


def _compute_internal_signature(body: bytes, secret: str) -> str:
    """Return `sha256=<hex>` HMAC of body using secret."""
    digest = _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def verify_internal_signature(request: Request) -> None:
    """FastAPI dependency: verify X-PF-Signature on internal write endpoints.

    No-op when PF_INTERNAL_API_SECRET is unset. When set, the request body is
    re-read and HMAC-SHA256 verified against the X-PF-Signature header. Uses
    timing-safe comparison so signature mismatches don't leak via timing.

    Raises 401 on missing or mismatched signature.
    """
    if not _INTERNAL_API_SECRET:
        return  # opt-in: no enforcement when env var unset
    sig = request.headers.get("X-PF-Signature", "")
    if not sig:
        raise HTTPException(
            status_code=401,
            detail="missing X-PF-Signature header (PF_INTERNAL_API_SECRET enforced)",
        )
    body = await request.body()
    expected = _compute_internal_signature(body, _INTERNAL_API_SECRET)
    if not _hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=401, detail="invalid X-PF-Signature")
