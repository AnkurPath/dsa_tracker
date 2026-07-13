from __future__ import annotations

import enum
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Spaced steps when solved on your own (before the 2nd revision)
REVISION_INTERVALS = [2, 4]
# Until you can solve it yourself, keep reviewing often
FREQUENT_INTERVAL_DAYS = 2
# After an "own" solve on/after the 2nd revision, pick randomly in this range
RANDOM_INTERVAL_MIN = 4
RANDOM_INTERVAL_MAX = 60


class SolveMethod(str, enum.Enum):
    OWN = "own"
    CODE = "code"
    VIDEO = "video"


class Difficulty(str, enum.Enum):
    EASY = "Easy"
    MEDIUM = "Medium"
    HARD = "Hard"


SOLVE_METHOD_LABELS = {
    SolveMethod.OWN: "By your own",
    SolveMethod.CODE: "By looking at existing code",
    SolveMethod.VIDEO: "By watching solution video",
}

DIFFICULTY_LABELS = {
    Difficulty.EASY: "Easy",
    Difficulty.MEDIUM: "Medium",
    Difficulty.HARD: "Hard",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


problem_topics = Table(
    "problem_topics",
    Base.metadata,
    Column("problem_id", ForeignKey("problems.id", ondelete="CASCADE"), primary_key=True),
    Column("topic_id", ForeignKey("topics.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    email_reminders: Mapped[bool] = mapped_column(Boolean, default=True)
    email_send_time: Mapped[str] = mapped_column(String(5), default="09:00")  # HH:MM IST
    email_last_reminder_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    leetcode_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    leetcode_last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    problems: Mapped[list["Problem"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    @property
    def display_name(self) -> str:
        if self.email:
            return self.email.split("@", 1)[0]
        return self.username


class Topic(Base):
    __tablename__ = "topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)

    problems: Mapped[list[Problem]] = relationship(
        secondary=problem_topics,
        back_populates="topics",
    )


class Problem(Base):
    __tablename__ = "problems"
    __table_args__ = (UniqueConstraint("user_id", "slug", name="uq_user_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str] = mapped_column(String(512), nullable=False)
    difficulty: Mapped[Difficulty | None] = mapped_column(
        Enum(Difficulty, values_callable=lambda x: [e.value for e in x]),
        nullable=True,
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision_count: Mapped[int] = mapped_column(Integer, default=0)
    next_revision_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    mastered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    user: Mapped[User] = relationship(back_populates="problems")
    topics: Mapped[list[Topic]] = relationship(
        secondary=problem_topics,
        back_populates="problems",
    )
    attempts: Mapped[list[Attempt]] = relationship(
        back_populates="problem",
        cascade="all, delete-orphan",
        order_by="Attempt.created_at.desc()",
    )

    @property
    def last_attempt(self) -> Attempt | None:
        return self.attempts[0] if self.attempts else None

    @property
    def schedule_label(self) -> str:
        """How the next review was scheduled based on the latest attempt."""
        attempt = self.last_attempt
        if attempt is None:
            return "Frequent"
        if attempt.solve_method != SolveMethod.OWN:
            return "Frequent"
        if attempt.revision_number >= 2:
            return "Random"
        return "Spaced"

    @property
    def needs_help(self) -> bool:
        attempt = self.last_attempt
        return attempt is not None and attempt.solve_method != SolveMethod.OWN

    @property
    def is_due(self) -> bool:
        if self.mastered or self.next_revision_date is None:
            return False
        return self.next_revision_date <= date.today()

    @property
    def days_until_revision(self) -> int | None:
        if self.next_revision_date is None:
            return None
        return (self.next_revision_date - date.today()).days


class Attempt(Base):
    __tablename__ = "attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    problem_id: Mapped[int] = mapped_column(ForeignKey("problems.id"), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    solve_method: Mapped[SolveMethod] = mapped_column(
        Enum(SolveMethod, values_callable=lambda x: [e.value for e in x]),
        nullable=False,
    )
    time_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    problem: Mapped[Problem] = relationship(back_populates="attempts")

    @property
    def solve_method_label(self) -> str:
        return SOLVE_METHOD_LABELS[self.solve_method]

    @property
    def is_initial(self) -> bool:
        return self.revision_number == 0
