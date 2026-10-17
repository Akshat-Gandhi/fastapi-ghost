"""Account operations shared by the web app and the CLI."""

from __future__ import annotations

import secrets

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .db import User, UserSession
from .security import hash_password

TEMP_PASSWORD_BYTES = 9  # ~12 url-safe characters


def find_user(db: Session, identifier: str) -> User | None:
    return db.scalar(select(User).where(User.email == identifier.strip().lower()))


def reset_password(db: Session, user: User, new_password: str | None = None) -> str:
    """Set a temporary password, require a change at next sign-in, and sign the user out everywhere."""
    temp = new_password or secrets.token_urlsafe(TEMP_PASSWORD_BYTES)
    user.password_hash = hash_password(temp)
    user.must_change_password = True
    db.execute(delete(UserSession).where(UserSession.user_id == user.id))
    db.commit()
    return temp


def admin_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True))) or 0
