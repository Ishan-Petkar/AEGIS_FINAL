"""
backend/init_db.py — create all tables, then seed assets (Ticket #2,
Decision D3: SQLAlchemy metadata.create_all(), no Alembic).

Idempotent: `CREATE TABLE IF NOT EXISTS` semantics via create_all(), and
seed_assets() is an upsert keyed on assets.name (backend/seed.py). Safe to
run any number of times against the same database.

    python -m backend.init_db

Column-default reconciliation (MEDIUM-1 review fix)
-----------------------------------------------------
Because Decision D3 uses `create_all()` with no Alembic, a table that was
already created *before* a `server_default` was added to its model
definition does NOT retroactively get that default — `create_all()` never
alters existing tables. `_reconcile_missing_column_defaults()` below is a
narrowly-scoped, idempotent reconciliation step (NOT a migration system —
it only ever touches the specific (table, column) pairs listed in
`_COLUMN_DEFAULTS_TO_RECONCILE`) that brings an already-created database in
line by issuing `ALTER TABLE ... ALTER COLUMN ... SET DEFAULT` for any of
those columns whose live catalog default is still missing. A column that
already has the default (including one created fresh by `create_all()`
after this fix landed) is left untouched.
"""

from __future__ import annotations

# (table_name, column_name, default_sql) — see docstring above. Keep this
# list limited to columns whose model definition just gained a
# server_default; do not grow this into a general migration mechanism.
_COLUMN_DEFAULTS_TO_RECONCILE = (
    ("alerts", "acknowledged", "false"),
    ("assets", "is_gateway", "false"),
)


def _reconcile_missing_column_defaults(engine) -> list[str]:
    """Apply any missing DB-level defaults from `_COLUMN_DEFAULTS_TO_RECONCILE`.

    Idempotent targeted reconciliation, not a migration framework: reads
    `information_schema.columns` for each known (table, column) pair and
    only issues `ALTER TABLE ... ALTER COLUMN ... SET DEFAULT` when the
    live column currently has no default. Returns the list of
    "table.column" strings that were actually altered (empty if the
    database was already up to date).
    """
    from sqlalchemy import text

    reconciled: list[str] = []
    with engine.begin() as conn:
        for table_name, column_name, default_sql in _COLUMN_DEFAULTS_TO_RECONCILE:
            current_default = conn.execute(
                text(
                    "SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table_name, "c": column_name},
            ).scalar()
            if current_default is None:
                conn.execute(
                    text(
                        f"ALTER TABLE {table_name} ALTER COLUMN {column_name} "
                        f"SET DEFAULT {default_sql}"
                    )
                )
                reconciled.append(f"{table_name}.{column_name}")
    return reconciled


def main() -> int:
    from backend.db import get_engine, session_scope
    from backend.models import Base
    from backend.seed import seed_assets

    engine = get_engine()
    table_names = sorted(Base.metadata.tables.keys())
    Base.metadata.create_all(engine)
    print(f"Ensured tables exist ({len(table_names)}): {', '.join(table_names)}")

    reconciled = _reconcile_missing_column_defaults(engine)
    if reconciled:
        print(f"Reconciled missing column defaults: {', '.join(reconciled)}")
    else:
        print("Column defaults already in place (no reconciliation needed).")

    with session_scope() as session:
        result = seed_assets(session)
    print(
        f"Seeded assets: {result['created']} created, {result['updated']} updated, "
        f"{result['total']} total (idempotent upsert keyed on assets.name)."
    )
    if result["stale"]:
        print(
            f"WARNING: {result['stale']} stale asset row(s) in the DB are not present "
            f"in backend.seed.compute_seed_rows() (Invariant D — the assets table may "
            f"have diverged from the authoritative graph): {', '.join(result['stale_names'])}"
        )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
