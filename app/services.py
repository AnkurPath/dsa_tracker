from __future__ import annotations

import random
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.leetcode import fetch_problem
from app.models import (
    FREQUENT_INTERVAL_DAYS,
    RANDOM_INTERVAL_MAX,
    RANDOM_INTERVAL_MIN,
    REVISION_INTERVALS,
    Attempt,
    Difficulty,
    Problem,
    SolveMethod,
    User,
)


def next_interval_days(solve_method: SolveMethod, revision_number: int) -> int:
    """Days until the next review after this attempt.

    - Not solved on your own → frequent (2 days)
    - Solved on your own before the 2nd revision → fixed spaced steps (2, then 4)
    - Solved on your own on/after the 2nd revision → random interval
    """
    if solve_method != SolveMethod.OWN:
        return FREQUENT_INTERVAL_DAYS

    if revision_number >= 2:
        return random.randint(RANDOM_INTERVAL_MIN, RANDOM_INTERVAL_MAX)

    return REVISION_INTERVALS[revision_number]


def _user_problems_query(user_id: int):
    return (
        select(Problem)
        .options(selectinload(Problem.attempts))
        .where(Problem.user_id == user_id)
    )


def list_problems(db: Session, user_id: int) -> list[Problem]:
    return list(
        db.scalars(
            _user_problems_query(user_id).order_by(
                Problem.next_revision_date.asc().nullslast(),
                Problem.created_at.desc(),
            )
        ).all()
    )


def get_due_problems(db: Session, user_id: int) -> list[Problem]:
    today = date.today()
    return list(
        db.scalars(
            _user_problems_query(user_id)
            .where(
                Problem.mastered.is_(False),
                Problem.next_revision_date.is_not(None),
                Problem.next_revision_date <= today,
            )
            .order_by(Problem.next_revision_date.asc())
        ).all()
    )


def get_upcoming_problems(db: Session, user_id: int) -> list[Problem]:
    today = date.today()
    return list(
        db.scalars(
            _user_problems_query(user_id)
            .where(
                Problem.mastered.is_(False),
                Problem.next_revision_date.is_not(None),
                Problem.next_revision_date > today,
            )
            .order_by(Problem.next_revision_date.asc())
        ).all()
    )


def get_mastered_problems(db: Session, user_id: int) -> list[Problem]:
    return list(
        db.scalars(
            _user_problems_query(user_id)
            .where(Problem.mastered.is_(True))
            .order_by(Problem.updated_at.desc())
        ).all()
    )


def get_problem(db: Session, user_id: int, problem_id: int) -> Problem | None:
    return db.scalars(
        _user_problems_query(user_id).where(Problem.id == problem_id)
    ).first()


def add_problem(
    db: Session,
    user: User,
    url: str,
    solve_method: SolveMethod,
    time_minutes: int,
    notes: str | None = None,
) -> Problem:
    info = fetch_problem(url)

    existing = db.scalars(
        select(Problem).where(Problem.user_id == user.id, Problem.slug == info.slug)
    ).first()
    if existing:
        raise ValueError(f'"{existing.title}" is already in your tracker.')

    difficulty = None
    if info.difficulty:
        try:
            difficulty = Difficulty(info.difficulty)
        except ValueError:
            difficulty = None

    interval = next_interval_days(solve_method, revision_number=0)
    problem = Problem(
        user_id=user.id,
        title=info.title,
        slug=info.slug,
        url=info.url,
        difficulty=difficulty,
        notes=notes or None,
        revision_count=0,
        next_revision_date=date.today() + timedelta(days=interval),
        mastered=False,
    )
    db.add(problem)
    db.flush()

    attempt = Attempt(
        problem_id=problem.id,
        revision_number=0,
        solve_method=solve_method,
        time_minutes=time_minutes,
        notes=notes or None,
    )
    db.add(attempt)
    db.commit()
    db.refresh(problem)
    return problem


def log_revision(
    db: Session,
    problem: Problem,
    solve_method: SolveMethod,
    time_minutes: int,
    notes: str | None = None,
) -> Problem:
    if problem.mastered:
        raise ValueError("This problem is already mastered.")

    revision_number = problem.revision_count + 1
    attempt = Attempt(
        problem_id=problem.id,
        revision_number=revision_number,
        solve_method=solve_method,
        time_minutes=time_minutes,
        notes=notes or None,
    )
    db.add(attempt)

    problem.revision_count = revision_number
    interval = next_interval_days(solve_method, revision_number)
    problem.next_revision_date = date.today() + timedelta(days=interval)
    problem.mastered = False

    db.commit()
    db.refresh(problem)
    return problem


def mark_mastered(db: Session, problem: Problem) -> Problem:
    problem.mastered = True
    problem.next_revision_date = None
    db.commit()
    db.refresh(problem)
    return problem


def delete_problem(db: Session, problem: Problem) -> None:
    db.delete(problem)
    db.commit()


def stats(db: Session, user_id: int) -> dict[str, int]:
    problems = list_problems(db, user_id)
    return {
        "total": len(problems),
        "due": sum(1 for p in problems if p.is_due),
        "upcoming": sum(
            1
            for p in problems
            if not p.mastered and not p.is_due and p.next_revision_date
        ),
        "mastered": sum(1 for p in problems if p.mastered),
    }
