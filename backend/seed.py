"""
backend/seed.py — idempotent asset seeding (Ticket #2, Decision D2).

Seeds the `assets` table from `graph_manager.build_graph()` — the single
graph authority (Invariant D, "one graph authority") — never from a
hardcoded node list. Every node build_graph() materialises falls into
exactly one of three categories, derived programmatically so a future
change to config.SMART_CITY_ASSETS or the gateway topology flows through
automatically:

  - curated      present in config.SMART_CITY_ASSETS: real ip/type/
                  criticality/purdue_level, is_gateway=False
  - gateway       graph_manager.gateway_nodes() /
                  graph_manager.is_gateway_name(): criticality =
                  SETTINGS.gateway.gateway_node_criticality (0.0 — must
                  NOT inherit a real criticality, Decision #4 in
                  PLAN_MASTER.md), is_gateway=True, purdue_level parsed
                  from the name, ip=None, type='gateway'
  - synthesized   everything else (currently only City_Grid, which
                  appears solely as a DEPENDENCY_GRAPH edge target):
                  criticality = SETTINGS.cii.default_criticality (0.5),
                  ip=None, type='synthesized', purdue_level=None,
                  is_gateway=False

`compute_seed_rows()` is a pure function — no DB access — returning plain
dicts. This is deliberate: it is the thing tests exercise to verify the
seeding *logic* without a live Postgres connection. `seed_assets()` is the
thin DB-writing wrapper around it (upsert keyed on `assets.name`, so
re-running never duplicates a row and drift in the source graph is
reconciled on the next run rather than accumulating).
"""

from __future__ import annotations

from typing import Any

from config import SMART_CITY_ASSETS
from graph_manager import build_graph, gateway_nodes, is_gateway_name
from settings import SETTINGS

from backend.models import ASSET_TYPE_GATEWAY, ASSET_TYPE_SYNTHESIZED, Asset

_MUTABLE_FIELDS = ("ip", "type", "criticality", "purdue_level", "is_gateway")


def _purdue_level_from_gateway_name(name: str) -> int | None:
    """Parse the Purdue zone out of a gateway node name.

    'Gateway_L4' -> 4. 'Gateway_Lunclassified' (a zone with no numeric
    Purdue level; see graph_manager._gateway_name) has no integer to
    parse -> None.
    """
    suffix = name.removeprefix("Gateway_L")
    return int(suffix) if suffix.isdigit() else None


def compute_seed_rows() -> list[dict[str, Any]]:
    """Compute the asset rows to seed. Pure — touches no database.

    Returns one dict per node in `graph_manager.build_graph()`, each with
    keys name/ip/type/criticality/purdue_level/is_gateway ready to become
    an `Asset(**row)`.
    """
    graph = build_graph(directed=True)
    curated_by_name = {a["asset_name"]: a for a in SMART_CITY_ASSETS}
    gateways = gateway_nodes()

    rows: list[dict[str, Any]] = []
    for node in graph.nodes():
        if node in curated_by_name:
            asset = curated_by_name[node]
            rows.append(
                {
                    "name": node,
                    "ip": asset["ip"],
                    "type": asset["type"],
                    "criticality": asset["criticality"],
                    "purdue_level": asset.get("purdue_level"),
                    "is_gateway": False,
                }
            )
        elif node in gateways or is_gateway_name(node):
            rows.append(
                {
                    "name": node,
                    "ip": None,
                    "type": ASSET_TYPE_GATEWAY,
                    "criticality": SETTINGS.gateway.gateway_node_criticality,
                    "purdue_level": _purdue_level_from_gateway_name(node),
                    "is_gateway": True,
                }
            )
        else:
            rows.append(
                {
                    "name": node,
                    "ip": None,
                    "type": ASSET_TYPE_SYNTHESIZED,
                    "criticality": SETTINGS.cii.default_criticality,
                    "purdue_level": None,
                    "is_gateway": False,
                }
            )
    return rows


def seed_assets(session) -> dict[str, Any]:
    """Upsert `compute_seed_rows()` into the `assets` table, keyed on `name`.

    Idempotent: a second call with an unchanged graph creates nothing and
    updates nothing. If the graph's topology changes (e.g. an asset's
    criticality is edited in config.py), the existing row is updated in
    place rather than a duplicate being inserted. Nothing is ever deleted
    here — a node that stops appearing in build_graph() leaves a stale row
    behind rather than being silently dropped, which is the conservative
    choice for a curated topology table.

    Stale-row detection (MEDIUM-2 review fix, Invariant D — "one graph
    authority", no silent divergence): any existing `assets` row whose
    `name` is not among the current `compute_seed_rows()` names is reported
    back to the caller via the `stale` count and `stale_names` list, but is
    deliberately NOT deleted — the correct resolution (re-add the node to
    the graph vs. intentionally retire the asset) depends on context this
    function doesn't have. Surfacing it is what closes the blind spot; the
    CLIs (`backend.seed.main`, `backend.init_db.main`) print a warning
    naming these rows so they can't silently drift out of sync with the
    topology endpoint.

    Does not commit; the caller controls the transaction (see
    backend.db.session_scope).
    """
    from sqlalchemy import select

    rows = compute_seed_rows()
    row_names = {row["name"] for row in rows}
    existing = {a.name: a for a in session.scalars(select(Asset)).all()}

    created = 0
    updated = 0
    for row in rows:
        asset = existing.get(row["name"])
        if asset is None:
            session.add(Asset(**row))
            created += 1
            continue
        changed = False
        for field in _MUTABLE_FIELDS:
            if getattr(asset, field) != row[field]:
                setattr(asset, field, row[field])
                changed = True
        if changed:
            updated += 1

    stale_names = sorted(name for name in existing if name not in row_names)

    session.flush()
    return {
        "created": created,
        "updated": updated,
        "total": len(rows),
        "stale": len(stale_names),
        "stale_names": stale_names,
    }


def main() -> int:
    from backend.db import session_scope

    with session_scope() as session:
        result = seed_assets(session)
    print(
        f"Seeded assets: {result['created']} created, {result['updated']} updated, "
        f"{result['total']} total (upsert keyed on assets.name)."
    )
    if result["stale"]:
        print(
            f"WARNING: {result['stale']} stale asset row(s) in the DB are not present "
            f"in compute_seed_rows() (Invariant D — the assets table may have diverged "
            f"from the authoritative graph): {', '.join(result['stale_names'])}"
        )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
