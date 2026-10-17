"""Database models. Postgres in Docker; SQLite (or any SQLAlchemy URL) elsewhere."""

from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, create_engine, func, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    projects: Mapped[list["Project"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class UserSession(Base):
    __tablename__ = "sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    base_url: Mapped[str] = mapped_column(String(500))
    headers_enc: Mapped[str] = mapped_column(Text, default="")  # encrypted JSON of header name -> value
    allow_writes: Mapped[bool] = mapped_column(Boolean, default=False)
    samples: Mapped[int] = mapped_column(Integer, default=5)
    source_path: Mapped[str] = mapped_column(String(500), default="")  # optional: app source, for raise sites
    login_enc: Mapped[str] = mapped_column(Text, default="")  # encrypted JSON: optional sign-in call
    query_enc: Mapped[str] = mapped_column(Text, default="")  # encrypted JSON: credentials carried in the URL
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user: Mapped[User] = relationship(back_populates="projects")
    runs: Mapped[list["Run"]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Run.id.desc()"
    )


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | done | failed
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    results: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    project: Mapped[Project] = relationship(back_populates="runs")


def database_url() -> str:
    return os.environ.get("GHOST_DATABASE_URL", "sqlite:///./ghost.db")


def make_session_factory(url: str | None = None) -> sessionmaker:
    url = url or database_url()
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
    if url.startswith("sqlite") and ":memory:" in url:
        from sqlalchemy.pool import StaticPool
        kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    _migrate(engine)
    _migrate_projects(engine)
    return sessionmaker(engine, expire_on_commit=False)


def _migrate(engine) -> None:
    """create_all never alters existing tables, so add columns introduced after a database was created."""
    columns = {c["name"] for c in inspect(engine).get_columns("users")}
    default = "0" if engine.dialect.name == "sqlite" else "FALSE"
    with engine.begin() as conn:
        if "must_change_password" not in columns:
            conn.execute(text(f"ALTER TABLE users ADD COLUMN must_change_password BOOLEAN NOT NULL DEFAULT {default}"))
        if "is_admin" not in columns:
            conn.execute(text(f"ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT {default}"))
            # An instance that predates roles: its first account becomes the admin so someone can manage users.
            conn.execute(text(f"UPDATE users SET is_admin = {'1' if default == '0' else 'TRUE'} WHERE id = (SELECT MIN(id) FROM users)"))


def _migrate_projects(engine) -> None:
    columns = {c["name"] for c in inspect(engine).get_columns("projects")}
    with engine.begin() as conn:
        if "source_path" not in columns:
            conn.execute(text("ALTER TABLE projects ADD COLUMN source_path VARCHAR(500) NOT NULL DEFAULT ''"))
        for name in ("login_enc", "query_enc"):
            if name not in columns:
                conn.execute(text(f"ALTER TABLE projects ADD COLUMN {name} TEXT NOT NULL DEFAULT ''"))


def ensure_admin(factory: sessionmaker) -> bool:
    """On a brand-new install (no users at all) create admin/admin and force a password change at first sign-in.

    GHOST_ADMIN_PASSWORD sets a different initial password and skips the forced change.
    Never runs when any user exists, so a default credential cannot appear on a live instance."""
    from .security import hash_password

    with factory() as db:
        if db.scalar(select(func.count()).select_from(User)):
            return False
        chosen = os.environ.get("GHOST_ADMIN_PASSWORD")
        db.add(User(email="admin", password_hash=hash_password(chosen or "admin"), must_change_password=not chosen, is_admin=True))
        db.commit()
        return True
