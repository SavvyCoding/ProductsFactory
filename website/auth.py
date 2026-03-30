"""
HTTP Basic Auth for the PM website.
Credentials stored in environment variables — never hardcoded.
"""

import os
import secrets
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()

PM_USERNAME = os.environ.get("PM_USERNAME", "admin")
PM_PASSWORD = os.environ.get("PM_PASSWORD", "")  # must be set — empty = startup error


def require_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """FastAPI dependency — call on every protected route."""
    if not PM_PASSWORD:
        raise RuntimeError("PM_PASSWORD env var not set. Set it before starting the website.")

    username_ok = secrets.compare_digest(credentials.username.encode(), PM_USERNAME.encode())
    password_ok = secrets.compare_digest(credentials.password.encode(), PM_PASSWORD.encode())

    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username
