"""Authentication module for Enclave Web UI.

Users are stored in a JSON file at ~/.local/share/enclave/webui_users.json.
Passwords are bcrypt-hashed. JWTs are issued on login and validated on each request.

Initial user creation: `enclave-webui --create-user <username>`
This prompts for a password interactively.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from jose import JWTError, jwt

USERS_FILE = Path.home() / ".local" / "share" / "enclave" / "webui_users.json"
SECRET_FILE = Path.home() / ".local" / "share" / "enclave" / "webui_secret.key"
ALGORITHM = "HS256"
TOKEN_EXPIRE_HOURS = 72

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def _get_secret_key() -> str:
    """Load or generate the JWT signing key."""
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(48)
    SECRET_FILE.write_text(key)
    SECRET_FILE.chmod(0o600)
    return key


def _load_users() -> dict:
    """Load users dict from JSON file."""
    if not USERS_FILE.exists():
        return {}
    return json.loads(USERS_FILE.read_text())


def _save_users(users: dict) -> None:
    """Save users dict to JSON file."""
    USERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    USERS_FILE.write_text(json.dumps(users, indent=2))
    USERS_FILE.chmod(0o600)


def create_user(username: str, password: str, is_admin: bool = False) -> None:
    """Create or update a user with a bcrypt-hashed password."""
    users = _load_users()
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    users[username] = {
        "password_hash": hashed,
        "is_admin": is_admin,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    _save_users(users)


def verify_password(username: str, password: str) -> dict | None:
    """Verify credentials. Returns user dict or None."""
    users = _load_users()
    user = users.get(username)
    if not user:
        return None
    if bcrypt.checkpw(password.encode(), user["password_hash"].encode()):
        return {"username": username, **user}
    return None


def create_token(username: str) -> str:
    """Create a JWT access token."""
    expire = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {"sub": username, "exp": expire}
    return jwt.encode(payload, _get_secret_key(), algorithm=ALGORITHM)


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> dict:
    """FastAPI dependency: validate JWT and return user info."""
    return validate_token(token)


def validate_token(token: str) -> dict:
    """Validate a JWT token string and return user info.

    Raises HTTPException if token is invalid.  Used directly for WebSocket
    auth where OAuth2PasswordBearer is not available.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, _get_secret_key(), algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    users = _load_users()
    user = users.get(username)
    if user is None:
        raise credentials_exception
    return {"username": username, **user}


def user_count() -> int:
    """Return number of configured users."""
    return len(_load_users())
