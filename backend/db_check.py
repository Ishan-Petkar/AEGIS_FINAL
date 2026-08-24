"""
backend/db_check.py — connectivity verification for the Phase 5 backend.

Run from the repo root with the project venv active:

    python -m backend.db_check

Connects using `BACKEND_SETTINGS.database_url`, runs `SELECT version()`,
and prints the resolved (password-redacted) connection target plus the
server version. Exits non-zero with an actionable message if it cannot
connect — this is the fast diagnostic for "is Postgres actually up and
configured the way the backend expects" during development, and the
manual verification for the acceptance criterion that a live database
round-trip works. It intentionally has no test-suite equivalent: the
default test suite must pass with no Postgres available (CI has none).
"""

import re
import sys


def _redact(url: str) -> str:
    """Replace the password segment of a SQLAlchemy URL with '***'.

    Parses with `sqlalchemy.engine.make_url` and renders via its own
    password-hiding facility (`render_as_string(hide_password=True)`)
    rather than a hand-rolled regex, so a password containing `@` (or
    other URL-special characters) is still redacted correctly. Falls back
    to a best-effort regex if the URL cannot be parsed (e.g. a malformed
    operator-supplied override), so this diagnostic never crashes.
    """
    try:
        from sqlalchemy.engine import make_url

        return make_url(url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 - best-effort fallback, never raise here
        return re.sub(r"://([^:/@]+):([^@]*)@", r"://\1:***@", url)


def main() -> int:
    from sqlalchemy import create_engine, text

    from backend.config import BACKEND_SETTINGS

    target = _redact(BACKEND_SETTINGS.database_url)
    print(f"Connecting to {target} ...")

    try:
        engine = create_engine(
            BACKEND_SETTINGS.database_url,
            pool_pre_ping=BACKEND_SETTINGS.db_pool_pre_ping,
        )
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar_one()
    except Exception as exc:  # noqa: BLE001 - top-level CLI, report and exit
        print(f"FAILED to connect to {target}", file=sys.stderr)
        print(f"  error: {exc}", file=sys.stderr)
        print(
            "  check: is Postgres running? (`brew services list`)\n"
            "  check: does the role/database from BACKEND_SETTINGS exist?\n"
            "  check: do AEGIS_DB_* env vars or .env match your environment?",
            file=sys.stderr,
        )
        return 1

    print(f"Connected OK. Server version: {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
