from collections.abc import Generator
import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

_default_db = Path(__file__).resolve().parent.parent / "dsa_tracker.db"
DB_PATH = Path(os.getenv("DSA_DB_PATH", str(_default_db))).expanduser()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
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


def _ensure_leetcode_sync_columns() -> None:
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("users")}
    with engine.begin() as conn:
        if "leetcode_username" not in cols:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN leetcode_username VARCHAR(64)")
            )
        if "leetcode_last_synced_at" not in cols:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN leetcode_last_synced_at DATETIME")
            )
        if "email" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(255)"))
        # Unique index for email login (ignore nulls / duplicates until cleaned)
        indexes = {idx["name"] for idx in inspector.get_indexes("users")}
        if "uq_users_email" not in indexes and "ix_users_email" not in indexes:
            try:
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email "
                        "ON users(email) WHERE email IS NOT NULL"
                    )
                )
            except Exception:
                # Older SQLite without partial indexes — best-effort unique index
                try:
                    conn.execute(
                        text(
                            "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_email "
                            "ON users(email)"
                        )
                    )
                except Exception:
                    pass
        if "email_reminders" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE users ADD COLUMN email_reminders BOOLEAN "
                    "DEFAULT 1 NOT NULL"
                )
            )
        if "email_last_reminder_date" not in cols:
            conn.execute(
                text("ALTER TABLE users ADD COLUMN email_last_reminder_date DATE")
            )
        if "email_send_time" not in cols:
            conn.execute(
                text(
                    "ALTER TABLE users ADD COLUMN email_send_time VARCHAR(5) "
                    "DEFAULT '09:00' NOT NULL"
                )
            )


def _ensure_attempt_time_nullable() -> None:
    """Allow attempts without time (filled later on revision)."""
    inspector = inspect(engine)
    if "attempts" not in inspector.get_table_names():
        return
    cols = {c["name"]: c for c in inspector.get_columns("attempts")}
    time_col = cols.get("time_minutes")
    if time_col is None or time_col.get("nullable"):
        return

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE attempts_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    problem_id INTEGER NOT NULL,
                    revision_number INTEGER NOT NULL,
                    solve_method VARCHAR(16) NOT NULL,
                    time_minutes INTEGER,
                    notes TEXT,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(problem_id) REFERENCES problems (id)
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO attempts_new
                    (id, problem_id, revision_number, solve_method,
                     time_minutes, notes, created_at)
                SELECT id, problem_id, revision_number, solve_method,
                       time_minutes, notes, created_at
                FROM attempts
                """
            )
        )
        conn.execute(text("DROP TABLE attempts"))
        conn.execute(text("ALTER TABLE attempts_new RENAME TO attempts"))


def init_db() -> None:
    from app import models  # noqa: F401

    if _needs_auth_migration():
        # Old schema had no users — rebuild so problems are scoped per account
        Base.metadata.drop_all(bind=engine)

    Base.metadata.create_all(bind=engine)
    _ensure_difficulty_column()
    _ensure_leetcode_sync_columns()
    _ensure_attempt_time_nullable()
