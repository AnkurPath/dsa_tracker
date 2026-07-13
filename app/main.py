from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from app import services
from app.auth import (
    SESSION_SECRET,
    CurrentUser,
    OptionalUser,
    authenticate_user,
    create_user,
    login_user,
    logout_user,
)
from app.database import get_db, init_db
from app.emailer import smtp_configured
from app.models import SOLVE_METHOD_LABELS, SolveMethod, User

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000")
# Check often so IST send times are hit reliably
EMAIL_JOB_INTERVAL_SEC = int(os.getenv("EMAIL_JOB_INTERVAL_SEC", "60"))


async def _email_job_loop() -> None:
    """Periodically sync LeetCode + send scheduled IST emails."""
    while True:
        try:
            await asyncio.to_thread(
                services.process_daily_email_jobs, base_url=APP_BASE_URL
            )
        except Exception:
            pass
        await asyncio.sleep(EMAIL_JOB_INTERVAL_SEC)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    task = asyncio.create_task(_email_job_loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="DSA Tracker", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=60 * 60 * 24 * 30)
_static_dir = BASE_DIR / "static"
_static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    if exc.status_code == 401:
        if request.headers.get("HX-Request") == "true":
            response = HTMLResponse("", status_code=401)
            response.headers["HX-Redirect"] = "/login"
            return response
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(exc.detail or "Error", status_code=exc.status_code)


def _render(
    request: Request,
    name: str,
    context: dict | None = None,
    status_code: int = 200,
    user: User | None = None,
) -> HTMLResponse:
    ctx = {
        "request": request,
        "solve_methods": SOLVE_METHOD_LABELS,
        "user": user,
    }
    if context:
        ctx.update(context)
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _dashboard_context(
    db: Session, user: User, topic_slug: str | None = None
) -> dict:
    due = services.get_due_problems(db, user.id)
    upcoming = services.get_upcoming_problems(db, user.id)
    mastered = services.get_mastered_problems(db, user.id)
    if topic_slug:
        due = services.filter_problems_by_topic(due, topic_slug)
        upcoming = services.filter_problems_by_topic(upcoming, topic_slug)
        mastered = services.filter_problems_by_topic(mastered, topic_slug)

    insights = services.topic_insights(db, user.id)
    active_topic = next((t for t in insights if t.slug == topic_slug), None)
    return {
        "due": due,
        "upcoming": upcoming,
        "mastered": mastered,
        "stats": services.stats(db, user.id),
        "activity": services.activity_summary(db, user.id),
        "topic_insights": insights,
        "radar": services.weakness_radar(db, user.id),
        "topic_slug": topic_slug,
        "active_topic": active_topic,
        "session_topics": [item for item in insights if item.due_count > 0],
        "session_due_count": len(services.get_due_problems(db, user.id)),
        "sync_message": None,
        "sync_kind": None,
        "smtp_ready": smtp_configured(),
        "email_sent_today": services.email_sent_today(user),
    }


def _sync_flash(result: services.LeetCodeSyncResult) -> tuple[str | None, str | None]:
    if result.error:
        return result.error, "error"
    if result.imported:
        if result.imported == 1:
            return f'Imported "{result.titles[0]}" from LeetCode.', "ok"
        return f"Imported {result.imported} new solves from LeetCode.", "ok"
    return None, None


def _login_redirect() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


# —— Auth ——


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request, user: OptionalUser) -> HTMLResponse:
    if user:
        return RedirectResponse("/", status_code=303)
    return _render(request, "register.html", {"error": None})


@app.post("/register", response_class=HTMLResponse)
def register(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        if password != password_confirm:
            raise ValueError("Passwords do not match.")
        user = create_user(db, email, password)
        login_user(request, user)
        return RedirectResponse("/", status_code=303)
    except ValueError as exc:
        return _render(
            request,
            "register.html",
            {"error": str(exc), "email": email.strip()},
            status_code=400,
        )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: OptionalUser) -> HTMLResponse:
    if user:
        return RedirectResponse("/", status_code=303)
    return _render(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = authenticate_user(db, email, password)
    if not user:
        return _render(
            request,
            "login.html",
            {"error": "Invalid email or password.", "email": email.strip()},
            status_code=400,
        )
    login_user(request, user)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    logout_user(request)
    return RedirectResponse("/login", status_code=303)


# —— App ——


# —— Practice session ——


def _practice_state(request: Request) -> dict | None:
    state = request.session.get("practice")
    if not isinstance(state, dict) or "ids" not in state:
        return None
    return state


def _clear_practice(request: Request) -> None:
    request.session.pop("practice", None)


@app.post("/session/start")
def start_session(
    request: Request,
    user: CurrentUser,
    topic: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    topic_slug = topic.strip().lower() or None
    queue = services.build_session_queue(db, user.id, topic_slug)
    if not queue:
        target = "/?empty_session=1"
        if topic_slug:
            target = f"/?empty_session=1&topic={topic_slug}"
        return RedirectResponse(target, status_code=303)

    topic_name = None
    if topic_slug:
        insights = services.topic_insights(db, user.id)
        active_topic = next((item for item in insights if item.slug == topic_slug), None)
        topic_name = active_topic.name if active_topic else topic_slug

    request.session["practice"] = {
        "ids": [p.id for p in queue],
        "index": 0,
        "completed": 0,
        "own": 0,
        "help": 0,
        "topic_slug": topic_slug,
        "topic_name": topic_name,
    }
    return RedirectResponse("/session", status_code=303)


@app.post("/session/end")
def end_session(request: Request, user: CurrentUser) -> RedirectResponse:
    _clear_practice(request)
    return RedirectResponse("/", status_code=303)


@app.get("/session", response_class=HTMLResponse)
def practice_session(
    request: Request,
    user: OptionalUser,
    db: Session = Depends(get_db),
    topic: str | None = None,
) -> HTMLResponse:
    if not user:
        return _login_redirect()

    state = _practice_state(request)
    if not state:
        topic_slug = topic.strip().lower() if topic else None
        queue = services.build_session_queue(db, user.id, topic_slug)
        if not queue:
            return _render(
                request,
                "session_empty.html",
                {
                    "radar": services.weakness_radar(db, user.id),
                    "topic_slug": topic_slug,
                },
                user=user,
            )
        topic_name = None
        if topic_slug:
            insights = services.topic_insights(db, user.id)
            active_topic = next((item for item in insights if item.slug == topic_slug), None)
            topic_name = active_topic.name if active_topic else topic_slug
        state = {
            "ids": [p.id for p in queue],
            "index": 0,
            "completed": 0,
            "own": 0,
            "help": 0,
            "topic_slug": topic_slug,
            "topic_name": topic_name,
        }
        request.session["practice"] = state

    ids: list[int] = list(state["ids"])
    index = int(state.get("index", 0))

    if index >= len(ids):
        summary = {
            "completed": int(state.get("completed", 0)),
            "own": int(state.get("own", 0)),
            "help": int(state.get("help", 0)),
            "total": len(ids),
            "topic_name": state.get("topic_name"),
        }
        _clear_practice(request)
        return _render(
            request,
            "session_done.html",
            {"summary": summary, "radar": services.weakness_radar(db, user.id)},
            user=user,
        )

    problem = services.get_problem(db, user.id, ids[index])
    if not problem:
        # Skip missing/deleted problems
        state["index"] = index + 1
        request.session["practice"] = state
        return RedirectResponse("/session", status_code=303)

    return _render(
        request,
        "session.html",
        {
            "problem": problem,
            "position": index + 1,
            "total": len(ids),
            "completed": int(state.get("completed", 0)),
            "topic_name": state.get("topic_name"),
            "topic_slug": state.get("topic_slug"),
            "error": None,
        },
        user=user,
    )


@app.post("/session/skip")
def skip_session_problem(
    request: Request,
    user: CurrentUser,
) -> RedirectResponse:
    state = _practice_state(request)
    if state:
        state["index"] = int(state.get("index", 0)) + 1
        request.session["practice"] = state
    return RedirectResponse("/session", status_code=303)


@app.post("/session/snooze")
def snooze_session_problem(
    request: Request,
    user: CurrentUser,
    problem_id: int = Form(...),
    days: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    problem = services.get_problem(db, user.id, problem_id)
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")
    try:
        services.snooze_problem(db, problem, days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    state = _practice_state(request)
    if state:
        state["index"] = int(state.get("index", 0)) + 1
        request.session["practice"] = state
    return RedirectResponse("/session", status_code=303)


@app.post("/session/revise", response_class=HTMLResponse)
def session_revise(
    request: Request,
    user: CurrentUser,
    problem_id: int = Form(...),
    solve_method: str = Form(...),
    time_minutes: int = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    state = _practice_state(request)
    problem = services.get_problem(db, user.id, problem_id)
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")

    try:
        method = SolveMethod(solve_method)
        if time_minutes < 1:
            raise ValueError("Time must be at least 1 minute.")
        services.log_revision(
            db, problem, method, time_minutes, notes.strip() or None
        )
    except ValueError as exc:
        return _render(
            request,
            "session.html",
            {
                "problem": problem,
                "position": int((state or {}).get("index", 0)) + 1,
                "total": len((state or {}).get("ids", [problem_id])),
                "completed": int((state or {}).get("completed", 0)),
                "topic_name": (state or {}).get("topic_name"),
                "topic_slug": (state or {}).get("topic_slug"),
                "error": str(exc),
            },
            status_code=400,
            user=user,
        )

    if state:
        state["completed"] = int(state.get("completed", 0)) + 1
        if method == SolveMethod.OWN:
            state["own"] = int(state.get("own", 0)) + 1
        else:
            state["help"] = int(state.get("help", 0)) + 1
        state["index"] = int(state.get("index", 0)) + 1
        request.session["practice"] = state

    return RedirectResponse("/session", status_code=303)


@app.post("/topics/sync", response_class=HTMLResponse)
def sync_topics(
    request: Request,
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    services.backfill_missing_topics(db, user.id)
    return _render(
        request,
        "partials/dashboard_sections.html",
        _dashboard_context(db, user),
        user=user,
    )


@app.post("/settings/leetcode", response_class=HTMLResponse)
def save_leetcode_username(
    request: Request,
    user: CurrentUser,
    leetcode_username: str = Form(""),
    sync_mode: str = Form("import"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        services.set_leetcode_username(
            db, user, leetcode_username, sync_mode=sync_mode
        )
    except ValueError as exc:
        if _is_htmx(request):
            response = _render(
                request,
                "partials/flash.html",
                {"message": str(exc), "kind": "error"},
                status_code=400,
                user=user,
            )
            response.headers["HX-Retarget"] = "#flash"
            response.headers["HX-Reswap"] = "innerHTML"
            return response
        return _render(
            request,
            "index.html",
            {**_dashboard_context(db, user), "error": str(exc)},
            status_code=400,
            user=user,
        )

    sync_message = None
    sync_kind = None
    mode = (sync_mode or "import").strip().lower()
    if user.leetcode_username:
        result = services.sync_leetcode_solves(db, user, force=True)
        sync_message, sync_kind = _sync_flash(result)
        if not sync_message and not result.error:
            if mode == "fresh":
                sync_message = (
                    f"Connected @{user.leetcode_username} — fresh start. "
                    "Only new AC solves from now on will be imported."
                )
            else:
                sync_message = (
                    f"Connected @{user.leetcode_username}. "
                    "Recent ACs imported; new solves will auto-import."
                )
            sync_kind = "ok"
        elif mode == "fresh" and result.imported == 0 and not result.error:
            sync_message = (
                f"Connected @{user.leetcode_username} — fresh start. "
                "Older solves were skipped."
            )
            sync_kind = "ok"
    else:
        sync_message = "LeetCode sync disconnected."
        sync_kind = "ok"

    ctx = _dashboard_context(db, user)
    if _is_htmx(request):
        if sync_message:
            return _render(
                request,
                "partials/dashboard_sections.html",
                {**ctx, "sync_message": sync_message, "sync_kind": sync_kind},
                user=user,
            )
        return _render(
            request,
            "partials/dashboard_sections.html",
            ctx,
            user=user,
        )

    return RedirectResponse("/", status_code=303)


@app.post("/settings/email", response_class=HTMLResponse)
def save_email_settings(
    request: Request,
    user: CurrentUser,
    email_reminders: str = Form(""),
    email_send_time: str = Form("09:00"),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        services.set_email_settings(
            db,
            user,
            reminders=email_reminders.strip() in {"1", "on", "true", "yes"},
            send_time=email_send_time,
        )
    except ValueError as exc:
        if _is_htmx(request):
            response = _render(
                request,
                "partials/flash.html",
                {"message": str(exc), "kind": "error"},
                status_code=400,
                user=user,
            )
            response.headers["HX-Retarget"] = "#flash"
            response.headers["HX-Reswap"] = "innerHTML"
            return response
        return _render(
            request,
            "index.html",
            {**_dashboard_context(db, user), "error": str(exc)},
            status_code=400,
            user=user,
        )

    message = (
        f"Reminders on — daily at {user.email_send_time} IST."
        if user.email_reminders
        else "Email reminders turned off."
    )
    ctx = {
        **_dashboard_context(db, user),
        "sync_message": message,
        "sync_kind": "ok",
    }
    if _is_htmx(request):
        return _render(
            request, "partials/dashboard_sections.html", ctx, user=user
        )
    return RedirectResponse("/", status_code=303)


@app.post("/email/send-due", response_class=HTMLResponse)
def send_due_email_now(
    request: Request,
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    ok, message = services.send_due_reminder(
        db, user, base_url=APP_BASE_URL, force=True, sync_leetcode=True
    )
    ctx = {
        **_dashboard_context(db, user),
        "sync_message": message,
        "sync_kind": "ok" if ok else "error",
    }
    if _is_htmx(request):
        return _render(
            request, "partials/dashboard_sections.html", ctx, user=user
        )
    return _render(request, "index.html", {**ctx, "error": None}, user=user)


@app.post("/leetcode/sync", response_class=HTMLResponse)
def sync_leetcode(
    request: Request,
    user: CurrentUser,
    quiet: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    result = services.sync_leetcode_solves(db, user, force=True)
    message, kind = _sync_flash(result)
    is_quiet = quiet.strip() in {"1", "true", "yes"}

    if is_quiet and not result.imported and not result.error:
        # Background poll — only refresh UI when something changed
        return _render(
            request,
            "partials/dashboard_sections.html",
            _dashboard_context(db, user),
            user=user,
        )

    if not message and not result.error:
        message = "Already up to date — no new solves to import."
        kind = "ok"

    ctx = {
        **_dashboard_context(db, user),
        "sync_message": message,
        "sync_kind": kind,
    }
    if _is_htmx(request):
        return _render(
            request,
            "partials/dashboard_sections.html",
            ctx,
            user=user,
        )
    return _render(request, "index.html", {**ctx, "error": None}, user=user)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: OptionalUser,
    db: Session = Depends(get_db),
    topic: str | None = None,
) -> HTMLResponse:
    if not user:
        return _login_redirect()
    topic_slug = topic.strip().lower() if topic else None

    sync_message = None
    sync_kind = None
    if user.leetcode_username:
        result = services.sync_leetcode_solves(db, user, force=False)
        sync_message, sync_kind = _sync_flash(result)

    return _render(
        request,
        "index.html",
        {
            **_dashboard_context(db, user, topic_slug),
            "error": None,
            "sync_message": sync_message,
            "sync_kind": sync_kind,
        },
        user=user,
    )


@app.post("/problems", response_class=HTMLResponse)
def create_problem(
    request: Request,
    user: CurrentUser,
    url: str = Form(...),
    solve_method: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        method = SolveMethod(solve_method)
        services.add_problem(
            db, user, url, method, time_minutes=None, notes=notes.strip() or None
        )
    except ValueError as exc:
        if _is_htmx(request):
            response = _render(
                request,
                "partials/flash.html",
                {"message": str(exc), "kind": "error"},
                status_code=400,
                user=user,
            )
            response.headers["HX-Retarget"] = "#flash"
            response.headers["HX-Reswap"] = "innerHTML"
            return response
        return _render(
            request,
            "index.html",
            {**_dashboard_context(db, user), "error": str(exc)},
            status_code=400,
            user=user,
        )

    if _is_htmx(request):
        return _render(
            request,
            "partials/dashboard_sections.html",
            _dashboard_context(db, user),
            user=user,
        )

    return RedirectResponse("/", status_code=303)


@app.get("/problems/{problem_id}/revise", response_class=HTMLResponse)
def revise_form(
    request: Request,
    problem_id: int,
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    problem = services.get_problem(db, user.id, problem_id)
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")
    return _render(
        request, "partials/revise_form.html", {"problem": problem}, user=user
    )


@app.post("/problems/{problem_id}/revise", response_class=HTMLResponse)
def revise_problem(
    request: Request,
    problem_id: int,
    user: CurrentUser,
    solve_method: str = Form(...),
    time_minutes: int = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    problem = services.get_problem(db, user.id, problem_id)
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")

    try:
        method = SolveMethod(solve_method)
        if time_minutes < 1:
            raise ValueError("Time must be at least 1 minute.")
        services.log_revision(
            db, problem, method, time_minutes, notes.strip() or None
        )
    except ValueError as exc:
        response = _render(
            request,
            "partials/flash.html",
            {"message": str(exc), "kind": "error"},
            status_code=400,
            user=user,
        )
        response.headers["HX-Retarget"] = "#flash"
        response.headers["HX-Reswap"] = "innerHTML"
        return response

    referer = request.headers.get("referer", "")
    if f"/problems/{problem_id}" in referer:
        response = HTMLResponse("")
        response.headers["HX-Redirect"] = "/"
        return response

    return _render(
        request,
        "partials/dashboard_sections.html",
        _dashboard_context(db, user),
        user=user,
    )


@app.post("/problems/{problem_id}/master", response_class=HTMLResponse)
def master_problem(
    request: Request,
    problem_id: int,
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    problem = services.get_problem(db, user.id, problem_id)
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")
    services.mark_mastered(db, problem)
    return _render(
        request,
        "partials/dashboard_sections.html",
        _dashboard_context(db, user),
        user=user,
    )


@app.post("/problems/{problem_id}/snooze", response_class=HTMLResponse)
def snooze_problem(
    request: Request,
    problem_id: int,
    user: CurrentUser,
    days: int = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    problem = services.get_problem(db, user.id, problem_id)
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")
    try:
        services.snooze_problem(db, problem, days)
    except ValueError as exc:
        response = _render(
            request,
            "partials/flash.html",
            {"message": str(exc), "kind": "error"},
            status_code=400,
            user=user,
        )
        response.headers["HX-Retarget"] = "#flash"
        response.headers["HX-Reswap"] = "innerHTML"
        return response

    referer = request.headers.get("referer", "")
    if f"/problems/{problem_id}" in referer and not _is_htmx(request):
        return RedirectResponse(f"/problems/{problem_id}", status_code=303)

    return _render(
        request,
        "partials/dashboard_sections.html",
        _dashboard_context(db, user),
        user=user,
    )


@app.delete("/problems/{problem_id}", response_class=HTMLResponse)
def remove_problem(
    request: Request,
    problem_id: int,
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    problem = services.get_problem(db, user.id, problem_id)
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")
    services.delete_problem(db, problem)
    return _render(
        request,
        "partials/dashboard_sections.html",
        _dashboard_context(db, user),
        user=user,
    )


@app.get("/problems/{problem_id}", response_class=HTMLResponse)
def problem_detail(
    request: Request,
    problem_id: int,
    user: OptionalUser,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not user:
        return _login_redirect()
    problem = services.get_problem(db, user.id, problem_id)
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")
    return _render(
        request, "problem_detail.html", {"problem": problem}, user=user
    )


@app.post("/problems/{problem_id}/notes")
def update_problem_notes(
    problem_id: int,
    user: CurrentUser,
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    problem = services.get_problem(db, user.id, problem_id)
    if not problem:
        raise HTTPException(status_code=404, detail="Problem not found")
    services.update_problem_notes(db, problem, notes)
    return RedirectResponse(f"/problems/{problem_id}", status_code=303)
