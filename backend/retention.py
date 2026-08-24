"""
backend/retention.py — events retention (Ticket #2).

Provides `prune_events()`, which keeps only the most recent
`db_event_retention_max_rows` events (by `ts`, event time) and deletes the
rest. Wiring this to a periodic call is Ticket #7 — this ticket only
provides and tests the function.

`_rows_to_prune()` is split out as a pure function (no DB access) so the
retention *policy* — "how many rows, if any, should be deleted, given the
current count and the cap" — is unit-testable with no live database,
mirroring the separation `backend/seed.py` uses for `compute_seed_rows()`.
`prune_events()` itself necessarily touches the database (it has to know
the current row count and issue the DELETE) and is exercised by the
opt-in live-DB tests.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select

from backend.config import BACKEND_SETTINGS
from backend.models import Event


def _rows_to_prune(total_count: int, max_rows: int) -> int:
    """How many of the oldest rows must be deleted so at most `max_rows`
    remain. Pure arithmetic — no I/O."""
    return max(0, total_count - max_rows)


def prune_events(session, max_rows: int | None = None) -> int:
    """Delete the oldest events beyond the most recent `max_rows`.

    Optional-override signature per CLAUDE.md: falls back to
    `BACKEND_SETTINGS.db_event_retention_max_rows` when `max_rows` is None.

    "Most recent" is by `ts` (event time), not `ingested_at` — retention
    keeps the events that actually happened most recently, independent of
    the order they were processed in.

    `event_scores` rows for deleted events are removed by the database via
    `ON DELETE CASCADE`; a `cii_snapshots.trigger_event_id` pointing at a
    deleted event is set to NULL via `ON DELETE SET NULL` (see
    backend/models.py for the rationale on both).

    Does not commit — the caller controls the transaction boundary (e.g.
    via `backend.db.session_scope`). Returns the number of rows deleted.
    """
    limit = max_rows if max_rows is not None else BACKEND_SETTINGS.db_event_retention_max_rows

    total = session.scalar(select(func.count()).select_from(Event)) or 0
    to_delete = _rows_to_prune(total, limit)
    if to_delete == 0:
        return 0

    oldest_ids = select(Event.id).order_by(Event.ts.asc()).limit(to_delete)
    stmt = delete(Event).where(Event.id.in_(oldest_ids))
    result = session.execute(stmt)
    return result.rowcount or 0
