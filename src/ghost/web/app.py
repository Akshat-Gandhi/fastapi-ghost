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

from ..report import FLAG_INFO, OUTCOME_LABELS
from . import accounts
from .db import Project, Run, User, UserSession, ensure_admin, make_session_factory, utcnow
from .runner import execute_run
from .security import (
    decrypt_headers, encrypt_headers, hash_password, hash_token,
    new_session_token, parse_headers, verify_password,
)

COOKIE = "ghost_session"
WEAK_PASSWORDS = {"admin", "password", "12345678", "password1"}
SESSION_DAYS = 14
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals["FLAGS"] = FLAG_INFO
templates.env.globals["OUTCOMES"] = OUTCOME_LABELS


def create_app(factory: sessionmaker | None = None) -> FastAPI:
    factory = factory or make_session_factory()
    app = FastAPI(title="fastapi-ghost", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.factory = factory
    ensure_admin(factory)

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

    def require_user_any(user: User | None = Depends(current_user)) -> User:
        if not user:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return user

    def require_user(user: User = Depends(require_user_any)) -> User:
        if user.must_change_password:
            raise HTTPException(status_code=303, headers={"Location": "/change-password"})
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
        resp = RedirectResponse("/change-password" if user.must_change_password else "/projects", status_code=303)
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
                request, "login.html", {"user": None, "error": "Wrong email/username or password."}, status_code=401
            )
        if password in WEAK_PASSWORDS and not user.must_change_password:
            user.must_change_password = True  # an account still on a default password must change it
            db.commit()
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

    def require_admin(user: User = Depends(require_user)) -> User:
        if not user.is_admin:
            raise HTTPException(404, "Not found")  # do not reveal that the page exists
        return user

    def users_page(request: Request, admin: User, db: Session, error: str | None = None,
                   notice: str | None = None, status_code: int = 200):
        rows = db.scalars(select(User).order_by(User.id)).all()
        return templates.TemplateResponse(
            request, "users.html", {"user": admin, "users": rows, "error": error, "notice": notice}, status_code=status_code
        )

    @app.get("/admin/users")
    def admin_users(request: Request, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
        return users_page(request, admin, db)

    @app.post("/admin/users")
    def admin_create_user(
        request: Request, identifier: str = Form(...), password: str = Form(...),
        make_admin: str | None = Form(None),
        admin: User = Depends(require_admin), db: Session = Depends(get_db),
    ):
        identifier = identifier.strip().lower()
        error = None
        if not identifier or " " in identifier:
            error = "Enter an email or a username without spaces."
        elif len(password) < 8 or password in WEAK_PASSWORDS:
            error = "Initial password must be at least 8 characters and not a default."
        elif accounts.find_user(db, identifier):
            error = "That user already exists."
        if error:
            return users_page(request, admin, db, error=error, status_code=400)
        db.add(User(email=identifier, password_hash=hash_password(password), must_change_password=True,
                    is_admin=make_admin is not None))
        db.commit()
        return users_page(request, admin, db, notice=f"Created {identifier}. They must choose a new password at first sign-in.")

    @app.post("/admin/users/{user_id}/reset-password")
    def admin_reset_password(
        request: Request, user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db),
    ):
        target = db.get(User, user_id)
        if not target:
            raise HTTPException(404, "User not found")
        temp = accounts.reset_password(db, target)
        return users_page(
            request, admin, db,
            notice=f"Temporary password for {target.email}: {temp}  (shown once; they must change it at sign-in)",
        )

    @app.post("/admin/users/{user_id}/delete")
    def admin_delete_user(
        request: Request, user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db),
    ):
        target = db.get(User, user_id)
        if not target:
            raise HTTPException(404, "User not found")
        if target.id == admin.id:
            return users_page(request, admin, db, error="You cannot delete your own account.", status_code=400)
        if target.is_admin and accounts.admin_count(db) <= 1:
            return users_page(request, admin, db, error="You cannot delete the last admin.", status_code=400)
        db.delete(target)
        db.commit()
        return users_page(request, admin, db, notice=f"Deleted {target.email} and their projects.")

    @app.get("/change-password")
    def change_password_page(request: Request, user: User = Depends(require_user_any)):
        return templates.TemplateResponse(request, "change_password.html", {"user": user, "error": None})

    @app.post("/change-password")
    def change_password(
        request: Request,
        request_user: User = Depends(require_user_any),
        current_password: str = Form(...),
        new_password: str = Form(...),
        confirm_password: str = Form(...),
        db: Session = Depends(get_db),
    ):
        user = db.get(User, request_user.id)
        error = None
        if not verify_password(current_password, user.password_hash):
            error = "Current password is wrong."
        elif new_password != confirm_password:
            error = "The new passwords do not match."
        elif len(new_password) < 8:
            error = "New password must be at least 8 characters."
        elif new_password in WEAK_PASSWORDS or new_password == current_password:
            error = "Choose a password that is different from the current one and not a default."
        if error:
            return templates.TemplateResponse(
                request, "change_password.html", {"user": user, "error": error}, status_code=400
            )
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        keep = hash_token(request.cookies.get(COOKIE, ""))
        for sess in db.scalars(select(UserSession).where(UserSession.user_id == user.id)).all():
            if sess.token_hash != keep:
                db.delete(sess)  # sign out every other session
        db.commit()
        return RedirectResponse("/projects", status_code=303)

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
