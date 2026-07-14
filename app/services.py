from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.leetcode import (
    LeetCodeTopic,
    fetch_problem,
    fetch_problem_by_slug,
    fetch_recent_ac_submissions,
    verify_leetcode_username,
)
from app.models import (
    FREQUENT_INTERVAL_DAYS,
    RANDOM_INTERVAL_MAX,
    RANDOM_INTERVAL_MIN,
    REVISION_INTERVALS,
    Attempt,
    Difficulty,
    Problem,
    SolveMethod,
    Topic,
    User,
    utcnow,
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
        .options(
            selectinload(Problem.attempts),
            selectinload(Problem.topics),
        )
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


def get_or_create_topics(
    db: Session, leetcode_topics: tuple[LeetCodeTopic, ...]
) -> list[Topic]:
    result: list[Topic] = []
    for item in leetcode_topics:
        topic = db.scalars(select(Topic).where(Topic.slug == item.slug)).first()
        if not topic:
            topic = Topic(name=item.name, slug=item.slug)
            db.add(topic)
            db.flush()
        elif topic.name != item.name:
            topic.name = item.name
        result.append(topic)
    return result


def add_problem(
    db: Session,
    user: User,
    url: str,
    solve_method: SolveMethod,
    time_minutes: int | None = None,
    notes: str | None = None,
) -> Problem:
    info = fetch_problem(url)
    problem = _create_problem_from_info(
        db,
        user,
        info,
        solve_method=solve_method,
        time_minutes=time_minutes,
        notes=notes,
    )
    assert problem is not None
    return problem


def _create_problem_from_info(
    db: Session,
    user: User,
    info,
    *,
    solve_method: SolveMethod,
    time_minutes: int | None = None,
    notes: str | None = None,
    skip_if_exists: bool = False,
) -> Problem | None:
    slug = (info.slug or "").strip().lower()
    existing = db.scalars(
        select(Problem).where(Problem.user_id == user.id, Problem.slug == slug)
    ).first()
    if existing:
        if skip_if_exists:
            return None
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
        slug=slug,
        url=info.url,
        difficulty=difficulty,
        notes=notes or None,
        revision_count=0,
        next_revision_date=date.today() + timedelta(days=interval),
        mastered=False,
        topics=get_or_create_topics(db, info.topics),
    )
    db.add(problem)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        if skip_if_exists:
            return None
        raise ValueError(f'"{info.title}" is already in your tracker.') from None

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


@dataclass
class LeetCodeSyncResult:
    imported: int = 0
    skipped: int = 0
    titles: list[str] | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if self.titles is None:
            self.titles = []


SYNC_COOLDOWN = timedelta(minutes=2)
AUTO_IMPORT_METHOD = SolveMethod.VIDEO


def set_leetcode_username(
    db: Session,
    user: User,
    username: str | None,
    *,
    sync_mode: str = "import",
) -> User:
    """Link or unlink a LeetCode profile.

    sync_mode:
      - \"import\" — import recent AC history (within LeetCode's public limit)
      - \"fresh\" — ignore older ACs; only sync solves from now on
    """
    if not username or not username.strip():
        user.leetcode_username = None
        user.leetcode_last_synced_at = None
        user.leetcode_sync_since = None
        db.commit()
        db.refresh(user)
        return user

    verified = verify_leetcode_username(username)
    user.leetcode_username = verified
    mode = (sync_mode or "import").strip().lower()
    if mode == "fresh":
        # Cutoff = now; submissions before this are ignored forever for this link
        user.leetcode_sync_since = int(utcnow().timestamp())
    else:
        user.leetcode_sync_since = None
    db.commit()
    db.refresh(user)
    return user


def sync_leetcode_solves(
    db: Session,
    user: User,
    *,
    force: bool = False,
    limit: int = 20,
) -> LeetCodeSyncResult:
    """Pull recent AC submissions and add any new ones to the revision queue."""
    if not user.leetcode_username:
        return LeetCodeSyncResult(error="Connect your LeetCode username first.")

    if not force and user.leetcode_last_synced_at is not None:
        last = user.leetcode_last_synced_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if utcnow() - last < SYNC_COOLDOWN:
            return LeetCodeSyncResult()

    try:
        submissions = fetch_recent_ac_submissions(user.leetcode_username, limit=limit)
    except ValueError as exc:
        return LeetCodeSyncResult(error=str(exc))

    since = user.leetcode_sync_since  # None = import all recent

    existing_slugs = {
        (slug or "").strip().lower()
        for slug in db.scalars(
            select(Problem.slug).where(Problem.user_id == user.id)
        ).all()
    }

    result = LeetCodeSyncResult()
    for submission in submissions:
        slug = (submission.slug or "").strip().lower()
        if not slug or slug in existing_slugs:
            result.skipped += 1
            continue
        if since is not None and submission.timestamp < since:
            result.skipped += 1
            continue
        info = fetch_problem_by_slug(slug, title_hint=submission.title)
        created = _create_problem_from_info(
            db,
            user,
            info,
            solve_method=AUTO_IMPORT_METHOD,
            time_minutes=None,
            notes="Auto-imported from LeetCode",
            skip_if_exists=True,
        )
        if created is None:
            result.skipped += 1
            existing_slugs.add(slug)
            continue
        existing_slugs.add(created.slug)
        result.imported += 1
        result.titles.append(created.title)

    # Session may have been rolled back on a duplicate race — re-load user
    user = db.get(User, user.id) or user
    user.leetcode_last_synced_at = utcnow()
    db.commit()
    db.refresh(user)
    return result


def log_revision(
    db: Session,
    problem: Problem,
    solve_method: SolveMethod,
    time_minutes: int,
    notes: str | None = None,
) -> Problem:
    if problem.mastered:
        raise ValueError("This problem is already mastered.")
    if time_minutes < 1:
        raise ValueError("Time must be at least 1 minute.")

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


def set_email_settings(
    db: Session,
    user: User,
    *,
    reminders: bool = True,
    send_time: str = "09:00",
) -> User:
    """Update reminder prefs for the account email (set at registration)."""
    if not user.email:
        raise ValueError("Your account has no email. Sign up again with an email address.")

    user.email_reminders = reminders
    user.email_send_time = normalize_ist_send_time(send_time)
    db.commit()
    db.refresh(user)
    return user


def normalize_ist_send_time(value: str) -> str:
    """Accept HH:MM (24h) and return normalized IST send time."""
    raw = (value or "").strip()
    try:
        hour_s, minute_s = raw.split(":", 1)
        hour = int(hour_s)
        minute = int(minute_s)
    except ValueError as exc:
        raise ValueError("Send time must look like 09:00 (24-hour IST).") from exc
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        raise ValueError("Send time must be a valid clock time in IST.")
    return f"{hour:02d}:{minute:02d}"


def ist_now() -> datetime:
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo("Asia/Kolkata"))


def user_ready_for_scheduled_email(user: User, *, now_ist: datetime | None = None) -> bool:
    """True when IST clock has reached the user's daily send time and not sent today."""
    now = now_ist or ist_now()
    today = now.date()
    if user.email_last_reminder_date == today:
        return False
    send_time = normalize_ist_send_time(user.email_send_time or "09:00")
    hour, minute = map(int, send_time.split(":"))
    preferred = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return now >= preferred


def build_daily_email(
    user: User, due: list[Problem], *, base_url: str
) -> tuple[str, str, str]:
    """Due list or congratulations. All links point at the product."""
    base = base_url.rstrip("/")
    home = f"{base}/"
    session_url = f"{base}/session"
    count = len(due)

    if count:
        subject = f"DSA Revision Helper: {count} problem{'s' if count != 1 else ''} due today"
        lines = [
            f"Hi {user.display_name},",
            "",
            f"You still have {count} revision{'s' if count != 1 else ''} due today:",
            "",
        ]
        for problem in due:
            product_link = f"{base}/problems/{problem.id}"
            lines.append(f"- {problem.title}")
            lines.append(f"  {product_link}")
        lines.extend(
            [
                "",
                f"Open your queue: {home}",
                f"Start a session: {session_url}",
                "",
                "— DSA Revision Helper",
            ]
        )
        text_body = "\n".join(lines)
        items = "".join(
            (
                '<li style="margin:0 0 10px">'
                f'<a href="{base}/problems/{problem.id}">{problem.title}</a>'
                "</li>"
            )
            for problem in due
        )
        html_body = f"""
        <p>Hi {user.display_name},</p>
        <p>You still have <strong>{count}</strong> revision{'s' if count != 1 else ''} due today:</p>
        <ul>{items}</ul>
        <p>
          <a href="{home}">Open DSA Revision Helper</a>
          · <a href="{session_url}">Start a session</a>
        </p>
        <p>— DSA Revision Helper</p>
        """
        return subject, text_body, html_body

    subject = "DSA Revision Helper: congratulations — you're clear for today"
    text_body = "\n".join(
        [
            f"Hi {user.display_name},",
            "",
            "Congratulations! You cleared today's revisions before reminder time.",
            "Nothing is due right now — keep the streak going.",
            "",
            f"Open DSA Revision Helper: {home}",
            "",
            "— DSA Revision Helper",
        ]
    )
    html_body = f"""
    <p>Hi {user.display_name},</p>
    <p><strong>Congratulations!</strong> You cleared today's revisions before reminder time.</p>
    <p>Nothing is due right now — keep the streak going.</p>
    <p><a href="{home}">Open DSA Revision Helper</a></p>
    <p>— DSA Revision Helper</p>
    """
    return subject, text_body, html_body


def email_sent_today(user: User, *, now_ist: datetime | None = None) -> bool:
    """True if a reminder was already sent for the current IST calendar day."""
    today = (now_ist or ist_now()).date()
    return (
        user.email_last_reminder_date is not None
        and user.email_last_reminder_date == today
    )


def send_due_reminder(
    db: Session,
    user: User,
    *,
    base_url: str,
    force: bool = False,
    sync_leetcode: bool = True,
    respect_schedule: bool = False,
) -> tuple[bool, str]:
    """Sync LeetCode, then send due or congratulations email. Returns (sent, message).

    At most one email per IST calendar day (including manual Send now).
    """
    from app.emailer import send_email, smtp_configured

    if not user.email:
        return False, "Add an email address first."
    if not user.email_reminders and not force:
        return False, "Email reminders are turned off."
    if not smtp_configured():
        return False, "SMTP is not configured on the server."

    now = ist_now()

    if respect_schedule and not force and not user_ready_for_scheduled_email(user, now_ist=now):
        return False, "Not time to send yet (IST schedule)."

    # Manual Send now and scheduled sends share the same once-per-day limit
    if email_sent_today(user, now_ist=now):
        return False, "You can only send one email every 24 hours. Try again tomorrow (IST)."

    if sync_leetcode and user.leetcode_username:
        sync_leetcode_solves(db, user, force=True)
        user = db.get(User, user.id) or user

    due = get_due_problems(db, user.id)
    subject, text_body, html_body = build_daily_email(user, due, base_url=base_url)
    try:
        send_email(
            to=user.email,
            subject=subject,
            text_body=text_body,
            html_body=html_body,
        )
    except ValueError as exc:
        return False, str(exc)

    user.email_last_reminder_date = now.date()
    db.commit()
    if due:
        return (
            True,
            f"Sent due list ({len(due)}) to {user.email}.",
        )
    return True, f"Sent congratulations email to {user.email}."


def process_daily_email_jobs(*, base_url: str) -> int:
    """Send scheduled IST emails once per day per user. Returns send count."""
    from app.database import SessionLocal

    sent = 0
    db = SessionLocal()
    try:
        users = list(
            db.scalars(
                select(User).where(
                    User.email.is_not(None),
                    User.email_reminders.is_(True),
                )
            ).all()
        )
        now = ist_now()
        for user in users:
            if not user_ready_for_scheduled_email(user, now_ist=now):
                # Quiet sync throughout the day so solves land before mail time
                if user.leetcode_username:
                    sync_leetcode_solves(db, user, force=False)
                continue
            ok, _ = send_due_reminder(
                db,
                user,
                base_url=base_url,
                force=False,
                sync_leetcode=True,
                respect_schedule=True,
            )
            if ok:
                sent += 1
    finally:
        db.close()
    return sent


def mark_mastered(db: Session, problem: Problem) -> Problem:
    problem.mastered = True
    problem.next_revision_date = None
    db.commit()
    db.refresh(problem)
    return problem


def snooze_problem(db: Session, problem: Problem, days: int) -> Problem:
    if problem.mastered:
        raise ValueError("Mastered problems cannot be snoozed.")
    if days < 1:
        raise ValueError("Snooze duration must be at least 1 day.")
    problem.next_revision_date = date.today() + timedelta(days=days)
    db.commit()
    db.refresh(problem)
    return problem


def update_problem_notes(db: Session, problem: Problem, notes: str | None) -> Problem:
    clean_notes = notes.strip() if notes else ""
    problem.notes = clean_notes or None
    db.commit()
    db.refresh(problem)
    return problem


def backfill_missing_topics(db: Session, user_id: int) -> int:
    """Fetch LeetCode topics for problems that don't have any yet."""
    problems = [
        p
        for p in list_problems(db, user_id)
        if not p.topics
    ]
    updated = 0
    for problem in problems:
        info = fetch_problem(problem.url)
        if info.topics:
            problem.topics = get_or_create_topics(db, info.topics)
            updated += 1
        if info.difficulty and problem.difficulty is None:
            try:
                problem.difficulty = Difficulty(info.difficulty)
            except ValueError:
                pass
        if info.title and problem.title != info.title:
            problem.title = info.title
    if updated or problems:
        db.commit()
    return updated


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


@dataclass
class ActivityDay:
    day: date
    count: int

    @property
    def intensity(self) -> int:
        if self.count <= 0:
            return 0
        if self.count == 1:
            return 1
        if self.count <= 3:
            return 2
        return 3


@dataclass
class ActivitySummary:
    current_streak: int
    best_streak: int
    active_days_30: int
    attempts_30: int
    attempts_total: int
    last_30_days: list[ActivityDay]


def activity_summary(db: Session, user_id: int) -> ActivitySummary:
    attempts = list(
        db.scalars(
            select(Attempt)
            .join(Problem, Attempt.problem_id == Problem.id)
            .where(Problem.user_id == user_id)
            .order_by(Attempt.created_at.asc())
        ).all()
    )

    counts_by_day: dict[date, int] = {}
    for attempt in attempts:
        attempt_day = attempt.created_at.date()
        counts_by_day[attempt_day] = counts_by_day.get(attempt_day, 0) + 1

    days = sorted(counts_by_day.keys())
    best_streak = 0
    running = 0
    prev: date | None = None
    for day in days:
        if prev and (day - prev).days == 1:
            running += 1
        else:
            running = 1
        best_streak = max(best_streak, running)
        prev = day

    today = date.today()
    current_streak = 0
    cursor = today
    while counts_by_day.get(cursor, 0) > 0:
        current_streak += 1
        cursor -= timedelta(days=1)

    window_days: list[ActivityDay] = []
    for days_back in range(29, -1, -1):
        day = today - timedelta(days=days_back)
        window_days.append(ActivityDay(day=day, count=counts_by_day.get(day, 0)))

    attempts_30 = sum(item.count for item in window_days)
    active_days_30 = sum(1 for item in window_days if item.count > 0)
    return ActivitySummary(
        current_streak=current_streak,
        best_streak=best_streak,
        active_days_30=active_days_30,
        attempts_30=attempts_30,
        attempts_total=len(attempts),
        last_30_days=window_days,
    )


@dataclass
class TopicInsight:
    name: str
    slug: str
    total: int
    own_count: int
    help_count: int
    frequent_count: int
    due_count: int
    own_rate: float
    behind: bool

    @property
    def own_pct(self) -> int:
        return round(self.own_rate * 100)


def topic_insights(db: Session, user_id: int) -> list[TopicInsight]:
    """Per-topic strength based on latest attempt method on active problems."""
    problems = [p for p in list_problems(db, user_id) if not p.mastered]
    buckets: dict[str, dict] = {}

    for problem in problems:
        last = problem.last_attempt
        solved_own = last is not None and last.solve_method == SolveMethod.OWN
        for topic in problem.topics:
            bucket = buckets.setdefault(
                topic.slug,
                {
                    "name": topic.name,
                    "slug": topic.slug,
                    "total": 0,
                    "own_count": 0,
                    "help_count": 0,
                    "frequent_count": 0,
                    "due_count": 0,
                },
            )
            bucket["total"] += 1
            if solved_own:
                bucket["own_count"] += 1
            else:
                bucket["help_count"] += 1
            if problem.schedule_label == "Frequent":
                bucket["frequent_count"] += 1
            if problem.is_due:
                bucket["due_count"] += 1

    insights: list[TopicInsight] = []
    for bucket in buckets.values():
        total = bucket["total"]
        own_rate = bucket["own_count"] / total if total else 0.0
        behind = (
            total >= 2
            and (
                own_rate < 0.5
                or bucket["help_count"] >= 2
                or bucket["frequent_count"] >= 2
            )
        )
        insights.append(
            TopicInsight(
                name=bucket["name"],
                slug=bucket["slug"],
                total=total,
                own_count=bucket["own_count"],
                help_count=bucket["help_count"],
                frequent_count=bucket["frequent_count"],
                due_count=bucket["due_count"],
                own_rate=own_rate,
                behind=behind,
            )
        )

    insights.sort(
        key=lambda t: (
            not t.behind,
            t.own_rate,
            -t.help_count,
            -t.frequent_count,
            t.name.lower(),
        )
    )
    return insights


@dataclass
class DifficultyInsight:
    name: str
    total: int
    own_count: int
    help_count: int

    @property
    def own_pct(self) -> int:
        if not self.total:
            return 0
        return round(100 * self.own_count / self.total)


@dataclass
class WeaknessRadar:
    total_active: int
    own_count: int
    help_count: int
    frequent_count: int
    due_count: int
    own_pct: int
    behind_topics: list[TopicInsight]
    strong_topics: list[TopicInsight]
    by_difficulty: list[DifficultyInsight]
    headlines: list[str]


def weakness_radar(db: Session, user_id: int) -> WeaknessRadar:
    """Coach-style summary of where the user is falling behind."""
    problems = [p for p in list_problems(db, user_id) if not p.mastered]
    topics = topic_insights(db, user_id)
    behind = [t for t in topics if t.behind]
    strong = [t for t in topics if not t.behind and t.own_rate >= 0.7 and t.total >= 2]

    own_count = 0
    help_count = 0
    frequent_count = 0
    due_count = 0
    diff_buckets: dict[str, dict[str, int]] = {
        "Easy": {"total": 0, "own": 0, "help": 0},
        "Medium": {"total": 0, "own": 0, "help": 0},
        "Hard": {"total": 0, "own": 0, "help": 0},
        "Unknown": {"total": 0, "own": 0, "help": 0},
    }

    for problem in problems:
        last = problem.last_attempt
        is_own = last is not None and last.solve_method == SolveMethod.OWN
        if is_own:
            own_count += 1
        else:
            help_count += 1
        if problem.schedule_label == "Frequent":
            frequent_count += 1
        if problem.is_due:
            due_count += 1

        label = problem.difficulty.value if problem.difficulty else "Unknown"
        bucket = diff_buckets[label]
        bucket["total"] += 1
        if is_own:
            bucket["own"] += 1
        else:
            bucket["help"] += 1

    total = len(problems)
    own_pct = round(100 * own_count / total) if total else 0

    by_difficulty = [
        DifficultyInsight(
            name=name,
            total=data["total"],
            own_count=data["own"],
            help_count=data["help"],
        )
        for name, data in diff_buckets.items()
        if data["total"]
    ]

    headlines: list[str] = []
    if not problems:
        headlines.append("Add problems to start building your weakness radar.")
    else:
        if behind:
            names = ", ".join(t.name for t in behind[:3])
            headlines.append(f"Falling behind on: {names}.")
        if frequent_count:
            headlines.append(
                f"{frequent_count} problem{'s' if frequent_count != 1 else ''} still on frequent review "
                "(not yet solved on your own)."
            )
        if due_count:
            headlines.append(
                f"{due_count} revision{'s' if due_count != 1 else ''} due — start a session to clear them."
            )
        if own_pct >= 70 and not behind:
            headlines.append(f"Strong overall — {own_pct}% latest attempts solved on your own.")
        elif own_pct < 40 and total >= 3:
            headlines.append(
                f"Own-solve rate is {own_pct}%. Prioritize weaker topics before adding new problems."
            )
        for diff in by_difficulty:
            if diff.total >= 2 and diff.own_pct < 40 and diff.name != "Unknown":
                headlines.append(
                    f"{diff.name} problems need work ({diff.own_pct}% own)."
                )

    if not headlines and problems:
        headlines.append("Keep logging honest attempts — patterns will show up here.")

    return WeaknessRadar(
        total_active=total,
        own_count=own_count,
        help_count=help_count,
        frequent_count=frequent_count,
        due_count=due_count,
        own_pct=own_pct,
        behind_topics=behind,
        strong_topics=strong[:3],
        by_difficulty=by_difficulty,
        headlines=headlines[:5],
    )


_DIFFICULTY_RANK = {"Hard": 0, "Medium": 1, "Easy": 2}


def build_session_queue(
    db: Session, user_id: int, topic_slug: str | None = None
) -> list[Problem]:
    """Due problems ordered for practice: overdue → weak topics → needs help → harder."""
    due = get_due_problems(db, user_id)
    due = filter_problems_by_topic(due, topic_slug)
    weak_slugs = {t.slug for t in topic_insights(db, user_id) if t.behind}
    today = date.today()

    def sort_key(problem: Problem) -> tuple:
        overdue = (
            (today - problem.next_revision_date).days
            if problem.next_revision_date
            else 0
        )
        weak = 0 if any(t.slug in weak_slugs for t in problem.topics) else 1
        help_first = 0 if problem.needs_help else 1
        diff = _DIFFICULTY_RANK.get(
            problem.difficulty.value if problem.difficulty else "", 3
        )
        return (-overdue, weak, help_first, diff, problem.title.lower())

    ordered = sorted(due, key=sort_key)
    # Never queue the same problem twice in one session
    seen: set[int] = set()
    unique: list[Problem] = []
    for problem in ordered:
        if problem.id in seen:
            continue
        seen.add(problem.id)
        unique.append(problem)
    return unique


def filter_problems_by_topic(
    problems: list[Problem], topic_slug: str | None
) -> list[Problem]:
    if not topic_slug:
        return problems
    return [p for p in problems if any(t.slug == topic_slug for t in p.topics)]
