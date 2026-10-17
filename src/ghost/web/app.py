"""The Ghost web app: sign in, enroll a project, run it, read the report."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from .db import Project, Run, User, UserSession, make_session_factory, utcnow
from .runner import execute_run
from .security import (
    decrypt_headers, encrypt_headers, hash_password, hash_token,
    new_session_token, parse_headers, verify_password,
)

COOKIE = "ghost_session"
SESSION_DAYS = 14
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def create_app(factory: sessionmaker | None = None) -> FastAPI:
    factory = factory or make_session_factory()
    app = FastAPI(title="fastapi-ghost", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.factory = factory

    def get_db():
        with factory() as db:
            yield db

    def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        sess = db.get(UserSession, hash_token(token))
        if not sess or sess.expires_at.replace(tzinfo=sess.expires_at.tzinfo or utcnow().tzinfo) < utcnow():
            return None
        return db.get(User, sess.user_id)

    def require_user(user: User | None = Depends(current_user)) -> User:
        if not user:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return user

    def own_project(project_id: int, db: Session, user: User) -> Project:
        project = db.get(Project, project_id)
        if not project or project.user_id != user.id:
            raise HTTPException(404, "Project not found")
        return project

    def signup_open(db: Session) -> bool:
        no_users = db.scalar(select(func.count()).select_from(User)) == 0
        return no_users or os.environ.get("GHOST_ALLOW_SIGNUP", "1") == "1"

    def start_session(db: Session, user: User) -> RedirectResponse:
        token, hashed = new_session_token()
        db.add(UserSession(token_hash=hashed, user_id=user.id, expires_at=utcnow() + timedelta(days=SESSION_DAYS)))
        db.commit()
        resp = RedirectResponse("/projects", status_code=303)
        resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_DAYS * 86400)
        return resp

    @app.get("/")
    def index(user: User | None = Depends(current_user)):
        return RedirectResponse("/projects" if user else "/login", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/login")
    def login_page(request: Request):
        return templates.TemplateResponse(request, "login.html", {"user": None, "error": None})

    @app.post("/login")
    def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
        user = db.scalar(select(User).where(User.email == email.strip().lower()))
        if not user or not verify_password(password, user.password_hash):
            return templates.TemplateResponse(
                request, "login.html", {"user": None, "error": "Wrong email or password."}, status_code=401
            )
        return start_session(db, user)

    @app.get("/register")
    def register_page(request: Request, db: Session = Depends(get_db)):
        return templates.TemplateResponse(
            request, "register.html", {"user": None, "error": None, "open": signup_open(db)}
        )

    @app.post("/register")
    def register(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
        email = email.strip().lower()
        error = None
        if not signup_open(db):
            error = "Sign-up is disabled on this server."
        elif "@" not in email:
            error = "Enter a valid email."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif db.scalar(select(User).where(User.email == email)):
            error = "That email is already registered."
        if error:
            return templates.TemplateResponse(
                request, "register.html", {"user": None, "error": error, "open": signup_open(db)}, status_code=400
            )
        user = User(email=email, password_hash=hash_password(password))
        db.add(user)
        db.commit()
        return start_session(db, user)

    @app.post("/logout")
    def logout(request: Request, db: Session = Depends(get_db)):
        token = request.cookies.get(COOKIE)
        if token and (sess := db.get(UserSession, hash_token(token))):
            db.delete(sess)
            db.commit()
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/projects")
    def projects(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
        items = db.scalars(select(Project).where(Project.user_id == user.id).order_by(Project.id.desc())).all()
        return templates.TemplateResponse(request, "projects.html", {"user": user, "projects": items, "error": None})

    @app.post("/projects")
    def create_project(
        request: Request,
        name: str = Form(...),
        base_url: str = Form(...),
        headers: str = Form(""),
        samples: int = Form(5),
        allow_writes: str | None = Form(None),
        user: User = Depends(require_user),
        db: Session = Depends(get_db),
    ):
        base_url = base_url.strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            items = db.scalars(select(Project).where(Project.user_id == user.id)).all()
            return templates.TemplateResponse(
                request, "projects.html",
                {"user": user, "projects": items, "error": "URL must start with http:// or https://"},
                status_code=400,
            )
        project = Project(
            user_id=user.id, name=name.strip() or base_url, base_url=base_url,
            headers_enc=encrypt_headers(parse_headers(headers)),
            allow_writes=allow_writes is not None, samples=max(1, min(samples, 50)),
        )
        db.add(project)
        db.commit()
        return RedirectResponse(f"/projects/{project.id}", status_code=303)

    @app.get("/projects/{project_id}")
    def project_page(
        request: Request, project_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)
    ):
        project = own_project(project_id, db, user)
        header_names = list(decrypt_headers(project.headers_enc))
        return templates.TemplateResponse(
            request, "project.html", {"user": user, "project": project, "header_names": header_names}
        )

    @app.post("/projects/{project_id}/run")
    def run_project(
        project_id: int, tasks: BackgroundTasks,
        user: User = Depends(require_user), db: Session = Depends(get_db),
    ):
        project = own_project(project_id, db, user)
        run = Run(project_id=project.id)
        db.add(run)
        db.commit()
        tasks.add_task(execute_run, factory, run.id)
        return RedirectResponse(f"/runs/{run.id}", status_code=303)

    @app.post("/projects/{project_id}/delete")
    def delete_project(project_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
        db.delete(own_project(project_id, db, user))
        db.commit()
        return RedirectResponse("/projects", status_code=303)

    def own_run(run_id: int, db: Session, user: User) -> Run:
        run = db.get(Run, run_id)
        if not run or run.project.user_id != user.id:
            raise HTTPException(404, "Run not found")
        return run

    @app.get("/runs/{run_id}")
    def run_page(request: Request, run_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
        run = own_run(run_id, db, user)
        return templates.TemplateResponse(request, "run.html", {"user": user, "run": run, "project": run.project})

    @app.get("/runs/{run_id}/report.json")
    def run_json(run_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
        run = own_run(run_id, db, user)
        return JSONResponse({"status": run.status, "error": run.error, **(run.results or {})})

    @app.get("/runs/{run_id}/report.md")
    def run_markdown(run_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
        from ..report import render_markdown
        run = own_run(run_id, db, user)
        if not run.results:
            raise HTTPException(404, "Report not ready")
        return PlainTextResponse(render_markdown(run.results), media_type="text/markdown; charset=utf-8")

    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn ghost.web.app:app_factory --factory`."""
    return create_app()
