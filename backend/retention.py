"""
backend/retention.py — events retention (Ticket #2; age-based bound added
in the Phase C methodology-rigor pass).

Provides `prune_events()`, which deletes any event caught by EITHER of two
independent bounds: the row-count cap (`db_event_retention_max_rows`, keeps
only the most recent N events by `ts`) and an optional age cap
(`db_event_retention_max_age_days`, deletes anything older than N days
regardless of how many total rows remain). Wiring the row-count bound to a
periodic call was Ticket #7 — this module only provides and tests the
policy.

`_rows_to_prune()` and `_age_cutoff()` are split out as pure functions (no
DB access) so the retention *policy* — "how many rows, if any, should be
deleted, given the current count/age and the caps" — is unit-testable with
no live database, mirroring the separation `backend/seed.py` uses for
`compute_seed_rows()`. `prune_events()` itself necessarily touches the
database (it has to know the current row count and issue the DELETE) and
is exercised by the opt-in live-DB tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select

from backend.config import BACKEND_SETTINGS
from backend.models import Event


def _rows_to_prune(total_count: int, max_rows: int) -> int:
    """How many of the oldest rows must be deleted so at most `max_rows`
    remain. Pure arithmetic — no I/O."""
    return max(0, total_count - max_rows)


def _age_cutoff(now: datetime, max_age_days: int) -> datetime:
    """Events with `ts` older than the returned cutoff are stale. Pure — no
    I/O, no wall-clock read (the caller supplies `now`)."""
    return now - timedelta(days=max_age_days)


def prune_events(
    session,
    max_rows: int | None = None,
    max_age_days: int | None = None,
    now: datetime | None = None,
) -> int:
    """Delete events beyond the row-count cap OR older than the age cap.

    Optional-override signature per CLAUDE.md: `max_rows` falls back to
    `BACKEND_SETTINGS.db_event_retention_max_rows`, `max_age_days` to
    `BACKEND_SETTINGS.db_event_retention_max_age_days` (None disables the
    age bound entirely — the row-count bound alone still applies), `now`
    to the real wall clock (injectable for deterministic tests, same
    pattern as `backend.security.RateLimiter`).

    The two bounds are independent and additive: a row is deleted if it is
    among the oldest rows beyond the row cap, OR if its `ts` is older than
    the age cutoff — whichever bound catches it first. Both are measured
    against `ts` (event time), not `ingested_at` — retention reasons about
    when events actually happened, independent of processing order.

    `event_scores` rows for deleted events are removed by the database via
    `ON DELETE CASCADE`; a `cii_snapshots.trigger_event_id` pointing at a
    deleted event is set to NULL via `ON DELETE SET NULL` (see
    backend/models.py for the rationale on both).

    Does not commit — the caller controls the transaction boundary (e.g.
    via `backend.db.session_scope`). Returns the number of rows deleted.
    """
    limit = max_rows if max_rows is not None else BACKEND_SETTINGS.db_event_retention_max_rows
    age_days = (
        max_age_days if max_age_days is not None else BACKEND_SETTINGS.db_event_retention_max_age_days
    )

    total = session.scalar(select(func.count()).select_from(Event)) or 0
    to_delete_by_count = _rows_to_prune(total, limit)

    conditions = []
    if to_delete_by_count:
        oldest_ids = select(Event.id).order_by(Event.ts.asc()).limit(to_delete_by_count)
        conditions.append(Event.id.in_(oldest_ids))
    if age_days is not None:
        cutoff = _age_cutoff(now if now is not None else datetime.now(timezone.utc), age_days)
        conditions.append(Event.ts < cutoff)

    if not conditions:
        return 0

    stmt = delete(Event).where(or_(*conditions))
    result = session.execute(stmt)
    return result.rowcount or 0
