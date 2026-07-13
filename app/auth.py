from __future__ import annotations

import os
import re
from typing import Annotated

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.emailer import normalize_email
from app.models import User

SESSION_SECRET = os.getenv("DSA_SESSION_SECRET", "dsa-tracker-dev-secret-change-me")
USERNAME_FROM_EMAIL_RE = re.compile(r"[^a-zA-Z0-9_]+")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def validate_password(password: str) -> str:
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    return password


def _username_from_email(email: str) -> str:
    local = email.split("@", 1)[0].lower()
    cleaned = USERNAME_FROM_EMAIL_RE.sub("_", local).strip("_")
    if len(cleaned) < 3:
        cleaned = (cleaned + "user")[:32]
    return cleaned[:32]


def get_user_by_email(db: Session, email: str) -> User | None:
    return db.scalars(select(User).where(User.email == email.strip().lower())).first()


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.scalars(select(User).where(User.username == username)).first()


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.scalars(select(User).where(User.id == user_id)).first()


def _unique_username(db: Session, base: str) -> str:
    candidate = base[:32]
    if not get_user_by_username(db, candidate):
        return candidate
    for i in range(2, 1000):
        suffix = f"_{i}"
        candidate = f"{base[: 32 - len(suffix)]}{suffix}"
        if not get_user_by_username(db, candidate):
            return candidate
    raise ValueError("Could not create a unique account name. Try another email.")


def create_user(db: Session, email: str, password: str) -> User:
    email = normalize_email(email)
    password = validate_password(password)
    if get_user_by_email(db, email):
        raise ValueError("An account with that email already exists.")
    username = _unique_username(db, _username_from_email(email))
    user = User(
        username=username,
        email=email,
        password_hash=hash_password(password),
        email_reminders=True,
        email_send_time="09:00",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, email: str, password: str) -> User | None:
    try:
        email = normalize_email(email)
    except ValueError:
        return None
    user = get_user_by_email(db, email)
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


def login_user(request: Request, user: User) -> None:
    request.session.clear()
    request.session["user_id"] = user.id


def logout_user(request: Request) -> None:
    request.session.clear()


def get_optional_user(
    request: Request, db: Session = Depends(get_db)
) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return get_user_by_id(db, int(user_id))


def require_user(
    request: Request, db: Session = Depends(get_db)
) -> User:
    user = get_optional_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Login required")
    return user


CurrentUser = Annotated[User, Depends(require_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
