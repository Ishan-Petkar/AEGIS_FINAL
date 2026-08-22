"""
graph_manager.py — Single authoritative graph builder for AEGIS (Phase 1, contract C2).

Every consumer of the asset dependency graph (the CII engine, and eventually the
dashboard topology view and Phase 4 graph-feature extraction) builds its graph by
calling build_graph() here — no other module constructs an nx.Graph/nx.DiGraph
from DEPENDENCY_GRAPH. One builder means a gateway node added here is guaranteed
visible to every consumer; two builders let them silently disagree.

Also implements the mandatory access gateway (Phase 1 Decisions #1, #4, #5 in
PLAN_MASTER.md): any edge whose target is a "protected" asset (criticality >=
SETTINGS.gateway.criticality_threshold) is rewritten to route through a single
gateway node per Purdue-level trust zone, so no protected asset is reachable
except through its zone's gateway. Gateway nodes are pure detection instruments —
callers must treat them as near-zero criticality (see gateway_nodes() below and
SETTINGS.gateway.gateway_node_criticality) so a gateway hit doesn't distort
Cascading Impact Index totals.
"""
from __future__ import annotations

import re

import networkx as nx

from config import DEPENDENCY_GRAPH, SMART_CITY_ASSETS
from settings import SETTINGS


# ---------------------------------------------------------------------------
# Edge normalization (supports both legacy tuples and new dicts)
# ---------------------------------------------------------------------------

def _normalize_edge(edge) -> dict:
    """Convert a DEPENDENCY_GRAPH entry to the canonical dict format.

    Supports:
    - New dict format: {"src": ..., "tgt": ..., "edge_type": ..., "prob": ..., ...}
    - Legacy tuple format: (src, tgt, edge_type, prob)
    """
    if isinstance(edge, dict):
        return edge
    src, tgt, edge_type, prob = edge
    return {
        "src": src,
        "tgt": tgt,
        "edge_type": edge_type,
        "prob": prob,
        "source": "legacy",
        "owner": "unknown",
        "rationale": "",
        "confidence": 1.0,
        "last_reviewed": "1970-01-01",
    }


# ---------------------------------------------------------------------------
# Gateway topology
# ---------------------------------------------------------------------------

GATEWAY_EDGE_TYPE = "gateway_relay"

_GATEWAY_NAME_RE = re.compile(r"^Gateway_L(\d+|unclassified)$")


def _gateway_name(purdue_level: int | None) -> str:
    zone = purdue_level if purdue_level is not None else "unclassified"
    return f"Gateway_L{zone}"


def is_gateway_name(name: str) -> bool:
    """True if `name` follows the gateway-node naming convention ('Gateway_LN'
    or 'Gateway_Lunclassified'), regardless of whether build_graph() has
    actually materialised a node with that name yet.

    Used by cii_calculator's tripwire-only path to tell "this is a gateway
    zone that just hasn't seen a DEPENDENCY_GRAPH edge yet" apart from "this
    is simply an unrecognised asset name" — the two must not be handled the
    same way (only the former should ever report a non-zero fallback CII).
    """
    return bool(_GATEWAY_NAME_RE.match(name))


def _protected_assets() -> dict[str, int | None]:
    """Map of asset_name -> purdue_level for every asset at/above the gateway threshold."""
    threshold = SETTINGS.gateway.criticality_threshold
    return {
        a["asset_name"]: a.get("purdue_level")
        for a in SMART_CITY_ASSETS
        if a.get("criticality", 0.0) >= threshold
    }


def gateway_target_assets(gateway_name: str) -> list[str]:
    """Return protected asset names guarded by `gateway_name` (e.g. 'Gateway_L4').

    Computed directly from the Purdue-level -> protected-asset mapping
    (SMART_CITY_ASSETS + SETTINGS.gateway.criticality_threshold), independent
    of whether any DEPENDENCY_GRAPH edge currently targets one of those
    assets. This lets a caller (e.g. cii_calculator's Phase 2 tripwire-only
    path) resolve "which asset(s) does this gateway protect" even for a zone
    whose gateway node build_graph() would not otherwise materialise (no
    edge in DEPENDENCY_GRAPH currently targets a protected asset there).
    """
    protected = _protected_assets()
    return [name for name, level in protected.items() if _gateway_name(level) == gateway_name]


def gateway_nodes() -> set[str]:
    """Return the set of gateway node names build_graph() inserts.

    Callers that maintain their own criticality map (rather than relying on
    cii_calculator's built-in gateway-aware lookup) should assign every name
    in this set SETTINGS.gateway.gateway_node_criticality.
    """
    return {_gateway_name(level) for level in _protected_assets().values()}


def _add_edge(G, src: str, tgt: str, edge: dict, is_gateway_edge: bool) -> None:
    G.add_edge(
        src,
        tgt,
        edge_type=edge.get("edge_type", "depends_on"),
        prob=edge["prob"],
        confidence=edge.get("confidence", 1.0),
        source=edge.get("source", "unknown"),
        owner=edge.get("owner", "unknown"),
        rationale=edge.get("rationale", ""),
        last_reviewed=edge.get("last_reviewed", ""),
        provider_id=edge.get("provider_id"),
        is_gateway_edge=is_gateway_edge,
    )


def build_graph(
    directed: bool = True,
    raw_graph=None,
    apply_gateway: bool = True,
) -> "nx.DiGraph | nx.Graph":
    """
    Build the AEGIS asset dependency graph — the single source of truth.

    Parameters
    ----------
    directed:
        True (default) returns a DiGraph, used by the CII Monte Carlo engine.
        False returns an undirected Graph, for topology visualisation.
    raw_graph:
        Override the edge list (defaults to config.DEPENDENCY_GRAPH). Used by
        tests and by callers that need to patch the topology.
    apply_gateway:
        If True (default), rewrite edges targeting a protected asset so they
        route through that asset's Purdue-zone gateway node instead of
        terminating on it directly. This is the mandatory chokepoint — leave
        True in production; tests using synthetic node names (not present in
        SMART_CITY_ASSETS) are unaffected either way.

    Returns
    -------
    The graph object. Every edge carries: edge_type, prob, confidence, source,
    owner, rationale, last_reviewed, provider_id, is_gateway_edge.
    """
    if raw_graph is None:
        raw_graph = DEPENDENCY_GRAPH

    G = nx.DiGraph() if directed else nx.Graph()
    protected = _protected_assets() if apply_gateway else {}

    # The gateway->target hop can be fed by several distinct original edges
    # (e.g. three different sources all targeting the same protected asset).
    # Adding it once per original edge would let the last-processed edge
    # silently overwrite the rest (nx.DiGraph.add_edge on an existing (u, v)
    # pair replaces its attributes). Instead, collect every original edge
    # feeding a given (gateway, target) pair and combine them below.
    gateway_hop_sources: dict[tuple[str, str], list[dict]] = {}

    for entry in raw_graph:
        edge = _normalize_edge(entry)
        src, tgt = edge["src"], edge["tgt"]

        if tgt in protected:
            gw = _gateway_name(protected[tgt])
            _add_edge(G, src, gw, edge, is_gateway_edge=True)
            gateway_hop_sources.setdefault((gw, tgt), []).append(edge)
        else:
            _add_edge(G, src, tgt, edge, is_gateway_edge=False)

    for (gw, tgt), edges in gateway_hop_sources.items():
        G.add_edge(
            gw,
            tgt,
            edge_type=GATEWAY_EDGE_TYPE,
            prob=_union_prob(e["prob"] for e in edges),
            confidence=min(e.get("confidence", 1.0) for e in edges),
            source="graph_manager.gateway_rewrite",
            owner="infra_ops",
            rationale=(
                f"Synthesised post-gateway compromise probability for {tgt}, "
                f"combining {len(edges)} original edge(s) via probabilistic union "
                f"(P(any fires) = 1 - product(1 - p_i))."
            ),
            last_reviewed="",
            provider_id=None,
            is_gateway_edge=True,
        )

    return G


def _union_prob(probs) -> float:
    """P(at least one of several independent events fires) = 1 - product(1 - p_i)."""
    remaining = 1.0
    for p in probs:
        remaining *= (1.0 - p)
    return 1.0 - remaining


def get_layout(G) -> dict:
    """Kamada-Kawai layout for a graph built by build_graph() — shared by the dashboard."""
    return nx.kamada_kawai_layout(G)
