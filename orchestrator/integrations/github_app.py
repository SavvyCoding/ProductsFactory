"""
GitHub App authentication — JWT sign + installation-token minter.

Replaces the per-product SSH deploy-key model. The orchestrator now
authenticates to GitHub with a single App installed on a dedicated org
(see system_config.github_org). Every git push, PR API call, and repo
creation goes through a short-lived installation access token minted
from this module.

Token lifecycle:
    1. Sign a JWT with the App's PEM private key (iss=app_id, exp=10 min).
    2. POST /app/installations/{installation_id}/access_tokens with the JWT.
    3. GitHub returns an access token valid for 60 min.

Module-level cache holds (token, expires_at) per installation_id. A
fresh token is minted when the cached one has < 5 min remaining; that
buffer covers clock skew + a long-running session that has the token
in-flight when refresh time arrives.

Configuration is read from /api/system-config rather than env vars so
operators can rotate the App secret without restarting the poller — same
pattern github.py uses for the PAT.

Why centralized: every caller that previously read github_pat now goes
through `get_installation_token()`. That single chokepoint means the
fallback (PAT) lives in exactly one place during the transition release.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import jwt  # PyJWT

log = logging.getLogger("poller.github_app")

PM_API_URL = os.environ.get("PM_API_URL", "")

_JWT_LIFETIME_SECONDS  = 10 * 60   # GitHub max is 10 min
_REFRESH_BUFFER_SECONDS = 5 * 60   # mint a new token when < 5 min left


@dataclass(frozen=True)
class _CachedToken:
    token: str
    expires_at: float  # epoch seconds


_cache: dict[int, _CachedToken] = {}
_cache_lock = threading.Lock()


def _fetch_app_config() -> tuple[int | None, str | None, int | None]:
    """Read App ID, PEM, Installation ID from system_config via the PM API.

    Returns (app_id, pem, installation_id). Any may be None during the
    pre-migration transition window — callers should treat all-three-None
    as "App not configured" and fall back to whatever the legacy path was.
    """
    if not PM_API_URL:
        return None, None, None
    try:
        with httpx.Client(base_url=PM_API_URL, timeout=5) as client:
            resp = client.get("/api/system-config")
            resp.raise_for_status()
            data = resp.json()
        if not isinstance(data, dict):
            return None, None, None
        return (
            data.get("github_app_id"),
            data.get("github_app_private_key") or None,
            data.get("github_app_installation_id"),
        )
    except Exception as e:
        log.warning("github_app: failed to read App config from PM API: %s", e)
        return None, None, None


def _build_jwt(app_id: int, pem: str) -> str:
    """Sign a JWT proving 'I am this App'. Short-lived, used only to mint
    installation tokens — never sent to git or repo API calls directly."""
    now = int(time.time())
    payload = {
        "iat": now - 60,   # back-date to absorb clock skew vs GitHub
        "exp": now + _JWT_LIFETIME_SECONDS,
        # GitHub spec requires `iss` to be the App ID as a string. PyJWT
        # historically accepted ints here; current versions (and stricter
        # validators on the GitHub side) reject them with
        # "Issuer (iss) must be a string."
        "iss": str(app_id),
    }
    return jwt.encode(payload, pem, algorithm="RS256")


def _mint_via_api(app_jwt: str, installation_id: int) -> _CachedToken:
    """Exchange the App JWT for a 60-min installation access token."""
    url = f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    headers = {
        "Authorization": f"Bearer {app_jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    with httpx.Client(timeout=10) as client:
        resp = client.post(url, headers=headers)
    if resp.status_code != 201:
        raise RuntimeError(
            f"github_app: token mint failed for installation {installation_id} "
            f"(HTTP {resp.status_code}): {resp.text[:300]}"
        )
    body = resp.json()
    expires_iso = body["expires_at"]
    expires_dt = datetime.fromisoformat(expires_iso.replace("Z", "+00:00"))
    return _CachedToken(token=body["token"], expires_at=expires_dt.timestamp())


def get_installation_token_from_config(
    app_id: int | None,
    pem: str | None,
    installation_id: int | None,
) -> str | None:
    """Mint or return-cached an installation token from explicit config.

    Used by code paths that already have the App config loaded (e.g. the
    website running inside pm-api, where the HTTP self-loopback in
    ``_fetch_app_config`` would be wasteful). Shares the same module-level
    cache as ``get_installation_token``.
    """
    if not (app_id and pem and installation_id):
        return None

    now = time.time()
    with _cache_lock:
        cached = _cache.get(installation_id)
        if cached and cached.expires_at - now > _REFRESH_BUFFER_SECONDS:
            return cached.token

    try:
        app_jwt = _build_jwt(app_id, pem)
        fresh = _mint_via_api(app_jwt, installation_id)
    except Exception as e:
        log.error("github_app: token mint failed: %s", e)
        return None

    with _cache_lock:
        _cache[installation_id] = fresh
    return fresh.token


def get_installation_token() -> str | None:
    """Return a valid installation token, minting fresh if needed.

    Reads App config via ``/api/system-config`` — appropriate for callers
    outside the pm-api process (orchestrator, agent containers).
    For pm-api itself, use ``get_installation_token_from_config`` to skip
    the loopback.

    Returns None if the App isn't fully configured — caller decides
    whether to fall back to the legacy PAT path.
    """
    app_id, pem, installation_id = _fetch_app_config()
    return get_installation_token_from_config(app_id, pem, installation_id)


def probe() -> tuple[bool, str]:
    """Mint a fresh token (bypassing cache) to verify the App is healthy.

    Used by supervisor auto-heal as the canonical 'is git auth working'
    check. Returns (ok, message). On failure the message names the
    specific cause — bad PEM, revoked install, network — so the heal
    log explains *why* without grepping the orchestrator log.
    """
    app_id, pem, installation_id = _fetch_app_config()
    if not (app_id and pem and installation_id):
        return False, "App not configured: app_id / private_key / installation_id missing"
    try:
        app_jwt = _build_jwt(app_id, pem)
    except Exception as e:
        return False, f"JWT sign failed (bad PEM?): {e}"
    try:
        fresh = _mint_via_api(app_jwt, installation_id)
    except Exception as e:
        return False, f"installation token mint failed: {e}"

    with _cache_lock:
        _cache[installation_id] = fresh
    return True, f"token minted, expires {datetime.fromtimestamp(fresh.expires_at, tz=timezone.utc).isoformat()}"


