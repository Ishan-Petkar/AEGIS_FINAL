"""
backend/inject.py — Phase 5 Ticket #13: real-attack scenario registry
backing `POST /api/inject`.

The constraint that shapes this module
----------------------------------------------------------------------
`src/data_generator.py`'s `generate_scripted_attack()` FABRICATES flows.
That is disallowed here (Invariant: no synthetic data anywhere). The
capture already contains real, labelled attack traffic in volume:

    day                          label     flow count
    friday-morning                Bot        1,966
    friday-afternoon-ddos         DDoS     128,027
    friday-afternoon-portscan     PortScan 158,930

So "injecting an attack" here means replaying REAL captured flows read
through `backend.replay_reader.ReplayFlowReader`, filtered to a real
`label`, handed to the existing `ReplayEngine.inject()` — never a second
injection path, never a fabricated row.

Decision D13-1 — real behaviour, operator-chosen target
----------------------------------------------------------------------
K8 (docs/PHASE5_RECON.md) established that real capture IPs
(192.168.10.x) resolve to nothing in `config.DEPENDENCY_GRAPH`, so CII
over them is all zeros and no cascade renders — the demo's second act
would do nothing. So this module replays real attack flows with their
`source_ip` RE-TARGETED to the operator-chosen curated asset's real
static IP (from `config.SMART_CITY_ASSETS`, the same identifier
`AssetRegistry.resolve()` maps to that asset name at confidence 1.0),
while preserving EVERY other real traffic characteristic — bytes,
packets, duration, protocol, ports, timing (`ts`), and the real `label`.

Why `source_ip`, not `destination_ip`: `backend.ingest.IngestPipeline.
_handle_anomalies` computes the blast radius from
`resolutions[i][0].asset_name` — the SOURCE resolution — treating the
source of an anomalous flow as "the compromised asset" (this is the
existing, Ticket #7 convention, not new here). Re-targeting `source_ip`
is therefore what actually makes the operator's chosen asset the CII
origin; `destination_ip` is left as the real captured value.

Only curated assets (an entry in `config.SMART_CITY_ASSETS`, exposed
here via `backend.seed.compute_seed_rows()`'s `ip` field) can be a valid
`target_asset` — gateway/synthesized graph nodes have no real static IP
identifier for `AssetRegistry` to resolve, so targeting one would silently
produce an event that resolves to something else entirely (an
`Unresolved_*` node, most likely) rather than the asset the operator
asked for. `build_criticality_map()` alone is not a strict-enough gate
for this endpoint — see `resolvable_target_assets()` below and decision
note in `routes.py`.

Every injected event is unmistakably a what-if, never observed capture
traffic: `ReplayEngine.inject()` already tags the batch
`origin=BATCH_ORIGIN_INJECTED`, which `IngestPipeline._persist_events`
already writes into `events.raw.batch_origin` — no change needed there
either (Ticket #7 built that seam; Ticket #9's WS envelopes carry it
through unchanged).

Decision D13-2 — the honeytoken scenario
----------------------------------------------------------------------
A honeytoken touch cannot exist in a 2017 public capture: the honeytoken
is AEGIS's own planted credential, part of its deception instrumentation,
not something an external dataset can contain. The `honeytoken` scenario
reuses the SAME real `bot_c2` flow pool (real Bot/C2 telemetry — bytes,
packets, timing, label all real and unmodified) and additionally sets
`ReplayFlow.is_honeytoken_use = True` via `dataclasses.replace()` (the
dataclass is frozen) on each flow — representing the attacker using the
planted credential during that real C2 session. The telemetry stays
real; only this deception-layer control flag, which
`backend.ingest.default_tripwire_signal` already reads via
`getattr(flow, "is_honeytoken_use", False)` (a seam Ticket #7 built for
exactly this), is synthesized. This is the system's OWN control signal,
not fabricated attacker data.

This matters: docs/DETECTION_STUDY.md section 5 measured the tripwire as
the ONLY channel that catches novel threats (unsupervised precision
~0.02; supervised precision 0.000 on unseen families; tripwire perfect
with zero training data) — the project's strongest empirical claim, and
the honeytoken scenario is what lets an operator actually witness it.

Caching (correctness requirement, section 5 of the ticket plan)
----------------------------------------------------------------------
`ReplayFlowReader.iter_flows()` reads and sorts an entire capture file
(75-280MB) before any row can be filtered by label — there's no cheaper
partial read (see `replay_reader.py`'s own docstring on this). This
module therefore caches the filtered, unmodified real-flow pool per
`(day, label)` pair the FIRST time a scenario needs it, keyed
independently of scenario name so `bot_c2` and `honeytoken` (which share
a `(day, label)`) only ever read the Friday-morning file once between
them. Re-targeting (`source_ip`, `source_row_id`, `is_honeytoken_use`)
is applied fresh on every call, AFTER the cached read — the cache holds
only real, unmodified `ReplayFlow` objects.
"""

from __future__ import annotations

import dataclasses
import threading
import uuid
from dataclasses import dataclass

from backend.replay_reader import ReplayFlow, ReplayFlowReader
from backend.seed import compute_seed_rows

# ---------------------------------------------------------------------------
# Scenario registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioSpec:
    """name -> (real dataset day, real label) plus the honeytoken flag and
    an operator-facing description. Never a fabrication knob."""

    name: str
    day: str
    label: str
    is_honeytoken: bool
    description: str


#: At minimum bot_c2, ddos, port_scan, plus one honeytoken variant
#: (ticket scope, section 2 item 2 / section 4). Every entry replays REAL
#: captured, labelled attack flows verified present on disk (module
#: docstring table) — `generate_scripted_attack()` is never used.
SCENARIOS: dict[str, ScenarioSpec] = {
    "bot_c2": ScenarioSpec(
        name="bot_c2",
        day="friday-morning",
        label="Bot",
        is_honeytoken=False,
        description=(
            "Real captured Bot/C2 traffic (friday-morning, label='Bot', "
            "1,966 real flows) re-targeted at the chosen asset."
        ),
    ),
    "ddos": ScenarioSpec(
        name="ddos",
        day="friday-afternoon-ddos",
        label="DDoS",
        is_honeytoken=False,
        description=(
            "Real captured DDoS traffic (friday-afternoon-ddos, "
            "label='DDoS', 128,027 real flows) re-targeted at the chosen "
            "asset."
        ),
    ),
    "port_scan": ScenarioSpec(
        name="port_scan",
        day="friday-afternoon-portscan",
        label="PortScan",
        is_honeytoken=False,
        description=(
            "Real captured port-scan traffic (friday-afternoon-portscan, "
            "label='PortScan', 158,930 real flows) re-targeted at the "
            "chosen asset."
        ),
    ),
    "honeytoken": ScenarioSpec(
        name="honeytoken",
        day="friday-morning",
        label="Bot",
        is_honeytoken=True,
        description=(
            "The SAME real Bot/C2 traffic as bot_c2, re-targeted at the "
            "chosen asset, with AEGIS's own planted-credential flag "
            "(is_honeytoken_use) additionally set on each flow to "
            "represent the attacker using the honeytoken during that real "
            "C2 session. Telemetry is real; only this deception-layer "
            "control flag is synthesized (decision D13-2) — a honeytoken "
            "touch cannot exist in a 2017 public capture."
        ),
    ),
}


class InjectionError(ValueError):
    """Raised for an unknown scenario or an unresolvable target_asset.
    Routes translate this to HTTP 422 (never a silent no-op or a
    fabricated-zero response)."""


# ---------------------------------------------------------------------------
# Target-asset resolution — curated assets only (see module docstring)
# ---------------------------------------------------------------------------


def resolvable_target_assets() -> dict[str, str]:
    """Asset name -> real static IP, for every CURATED asset (an entry in
    `config.SMART_CITY_ASSETS`, i.e. `compute_seed_rows()`'s `ip` field is
    not `None`). Gateway/synthesized graph nodes are excluded: they have
    no real static IP `AssetRegistry` resolves them from, so setting
    `source_ip` to anything for one of them would resolve to some OTHER
    asset (most likely an auto-registered `Unresolved_*` node under the
    10.0.1.x subnet-proximity heuristic), silently defeating the operator's
    choice rather than raising. `build_criticality_map()` alone (every
    graph node) is therefore not a strict-enough gate for `target_asset` —
    this function is the stricter one this module actually validates
    against.
    """
    return {
        row["name"]: row["ip"]
        for row in compute_seed_rows()
        if row["ip"] is not None
    }


# ---------------------------------------------------------------------------
# Real-flow pool cache — per (day, label), never per scenario name, so
# bot_c2 and honeytoken (same day/label) share one cached read.
# ---------------------------------------------------------------------------

_pool_cache: dict[tuple[str, str], list[ReplayFlow]] = {}
_pool_cache_lock = threading.Lock()

#: Upper bound on how many real flows are RETAINED in the per-(day,label)
#: cache. Never the limiting factor for a real demo burst: it comfortably
#: exceeds `BACKEND_SETTINGS.inject_max_flows`'s own ceiling (10,000) while
#: still bounding memory for the two 100k+-row labels (ddos, port_scan) —
#: there is no reason to hold every one of 158,930 PortScan flows in
#: memory when at most `inject_max_flows` are ever served from the pool
#: per request.
_POOL_CAP = 10_000


def _load_pool(day: str, label: str, reader: ReplayFlowReader) -> list[ReplayFlow]:
    """Read+filter (day, label) once; cache under a lock so concurrent
    first-callers don't both pay the full CSV read. Returns the REAL,
    UNMODIFIED flows (chronological order, per `ReplayFlowReader`) —
    re-targeting happens later, per-call, in `build_injection_flows()`.
    """
    key = (day, label)
    with _pool_cache_lock:
        cached = _pool_cache.get(key)
        if cached is not None:
            return cached

        pool: list[ReplayFlow] = []
        for flow in reader.iter_flows(day=day):
            if flow.label == label:
                pool.append(flow)
                if len(pool) >= _POOL_CAP:
                    break
        _pool_cache[key] = pool
        return pool


def clear_pool_cache() -> None:
    """Test-only reset of the module-level cache between test cases that
    use different fake readers for the same (day, label) key."""
    with _pool_cache_lock:
        _pool_cache.clear()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_injection_flows(
    scenario: str,
    target_asset: str,
    count: int,
    *,
    reader: ReplayFlowReader | None = None,
) -> list[ReplayFlow]:
    """Real captured flows for `scenario`, re-targeted at `target_asset`,
    ready for `ReplayEngine.inject()`.

    Raises `InjectionError` for an unknown `scenario` or an unresolvable
    `target_asset` (see `resolvable_target_assets()`) — 422 at the route,
    never a silent no-op and never a fabricated result.

    `count` is the caller's responsibility to have already bounded by
    `BACKEND_SETTINGS.inject_max_flows` (the route does this via Pydantic
    `Field(le=...)`, mirroring `/api/events`' `limit` pattern) — this
    function additionally clamps to however many real flows the pool
    actually holds, so a `count` larger than the real pool (only possible
    for `bot_c2`/`honeytoken`, whose real pool is 1,966) returns fewer
    flows rather than fabricating or cycling any.
    """
    spec = SCENARIOS.get(scenario)
    if spec is None:
        raise InjectionError(
            f"unknown scenario {scenario!r}; choose one of "
            f"{sorted(SCENARIOS)}"
        )

    targets = resolvable_target_assets()
    target_ip = targets.get(target_asset)
    if target_ip is None:
        raise InjectionError(
            f"target_asset {target_asset!r} is not a curated asset with a "
            f"real static IP identifier; choose one of {sorted(targets)}"
        )

    active_reader = reader if reader is not None else ReplayFlowReader()
    pool = _load_pool(spec.day, spec.label, active_reader)
    selected = pool[: max(count, 0)]

    injected: list[ReplayFlow] = []
    for flow in selected:
        injected.append(
            dataclasses.replace(
                flow,
                source_ip=target_ip,
                # Fresh, globally-unique row id: real flows' own
                # source_row_id ("<file>:<line>") is scoped to a replay of
                # that file under ITS OWN replay_session_id. An injected
                # batch shares whatever replay_session_id is currently
                # live (see backend/routes.py), so reusing the original
                # source_row_id risks colliding with the D4 unique
                # constraint (replay_session_id, source_row_id) against a
                # real row from that same file already ingested in this
                # session, or against a repeat injection of the same
                # scenario. A fresh uuid4 per injected flow guarantees no
                # collision, ever.
                source_row_id=f"injected:{scenario}:{uuid.uuid4().hex}",
                is_honeytoken_use=spec.is_honeytoken,
            )
        )
    return injected
