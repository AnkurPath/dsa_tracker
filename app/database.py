from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DB_PATH = Path(__file__).resolve().parent.parent / "dsa_tracker.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _needs_auth_migration() -> bool:
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    if "users" not in tables:
        return "problems" in tables
    if "problems" not in tables:
        return False
    cols = {c["name"] for c in inspector.get_columns("problems")}
    return "user_id" not in cols


def _ensure_difficulty_column() -> None:
    inspector = inspect(engine)
    if "problems" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("problems")}
    if "difficulty" in cols:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE problems ADD COLUMN difficulty VARCHAR(16)"))


def init_db() -> None:
    from app import models  # noqa: F401

    if _needs_auth_migration():
        # Old schema had no users — rebuild so problems are scoped per account
        Base.metadata.drop_all(bind=engine)

    Base.metadata.create_all(bind=engine)
    _ensure_difficulty_column()
