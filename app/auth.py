from __future__ import annotations

import os
import re
from typing import Annotated

import bcrypt
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,32}$")
SESSION_SECRET = os.getenv("DSA_SESSION_SECRET", "dsa-tracker-dev-secret-change-me")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def validate_username(username: str) -> str:
    username = username.strip()
    if not USERNAME_RE.match(username):
        raise ValueError(
            "Username must be 3–32 characters: letters, numbers, underscores only."
        )
    return username


def validate_password(password: str) -> str:
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters.")
    return password


def get_user_by_username(db: Session, username: str) -> User | None:
    return db.scalars(select(User).where(User.username == username)).first()


def get_user_by_id(db: Session, user_id: int) -> User | None:
    return db.scalars(select(User).where(User.id == user_id)).first()


def create_user(db: Session, username: str, password: str) -> User:
    username = validate_username(username)
    password = validate_password(password)
    if get_user_by_username(db, username):
        raise ValueError("That username is already taken.")
    user = User(username=username, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, username: str, password: str) -> User | None:
    user = get_user_by_username(db, username.strip())
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
