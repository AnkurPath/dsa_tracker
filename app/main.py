from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

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
from app.models import SOLVE_METHOD_LABELS, SolveMethod, User

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="DSA Tracker", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, max_age=60 * 60 * 24 * 30)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


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


def _dashboard_context(db: Session, user: User) -> dict:
    return {
        "due": services.get_due_problems(db, user.id),
        "upcoming": services.get_upcoming_problems(db, user.id),
        "mastered": services.get_mastered_problems(db, user.id),
        "stats": services.stats(db, user.id),
    }


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
    username: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        if password != password_confirm:
            raise ValueError("Passwords do not match.")
        user = create_user(db, username, password)
        login_user(request, user)
        return RedirectResponse("/", status_code=303)
    except ValueError as exc:
        return _render(
            request,
            "register.html",
            {"error": str(exc), "username": username.strip()},
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
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    user = authenticate_user(db, username, password)
    if not user:
        return _render(
            request,
            "login.html",
            {"error": "Invalid username or password.", "username": username.strip()},
            status_code=400,
        )
    login_user(request, user)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout(request: Request) -> RedirectResponse:
    logout_user(request)
    return RedirectResponse("/login", status_code=303)


# —— App ——


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: OptionalUser,
    db: Session = Depends(get_db),
) -> HTMLResponse:
    if not user:
        return _login_redirect()
    return _render(
        request,
        "index.html",
        {**_dashboard_context(db, user), "error": None},
        user=user,
    )


@app.post("/problems", response_class=HTMLResponse)
def create_problem(
    request: Request,
    user: CurrentUser,
    url: str = Form(...),
    solve_method: str = Form(...),
    time_minutes: int = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    try:
        method = SolveMethod(solve_method)
        if time_minutes < 1:
            raise ValueError("Time must be at least 1 minute.")
        services.add_problem(
            db, user, url, method, time_minutes, notes.strip() or None
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
