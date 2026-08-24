"""
backend/db.py — SQLAlchemy engine + session factory (Ticket #2).

The engine is created lazily on first use, not at import time: importing
`backend.db` must succeed on a machine with no Postgres reachable at all
(CI has no database). `get_engine()` and `get_session_factory()` build and
cache a module-level singleton on first call; nothing here talks to a
socket until a caller actually asks for a connection.

Usage:
    from backend.db import session_scope

    with session_scope() as session:
        session.add(some_model_instance)
        # commits on clean exit, rolls back and re-raises on exception
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.config import BACKEND_SETTINGS

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Return the process-wide SQLAlchemy Engine, creating it on first use.

    Pool sizing/timeout/pre-ping all come from BACKEND_SETTINGS (db_pool_size,
    db_max_overflow, db_pool_timeout_sec, db_pool_pre_ping) rather than
    SQLAlchemy defaults, per the ticket's explicit requirement.
    """
    global _engine
    if _engine is None:
        _engine = create_engine(
            BACKEND_SETTINGS.database_url,
            pool_size=BACKEND_SETTINGS.db_pool_size,
            max_overflow=BACKEND_SETTINGS.db_max_overflow,
            pool_timeout=BACKEND_SETTINGS.db_pool_timeout_sec,
            pool_pre_ping=BACKEND_SETTINGS.db_pool_pre_ping,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Return the process-wide session factory, creating it (and the engine
    it binds to) on first use."""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed session: commits on clean exit, rolls back and
    re-raises on exception, always closes.

        with session_scope() as session:
            ...
    """
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def reset_engine_for_tests() -> None:
    """Dispose of and drop the cached engine/session factory.

    Only meaningful in tests that construct a fresh engine against a
    different URL (e.g. a live-DB test run) — production code never needs
    this, the singleton lives for the life of the process.
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
