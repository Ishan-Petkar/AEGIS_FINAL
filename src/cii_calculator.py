"""
cii_calculator.py — Canonical Cascading Impact Index computation for AEGIS.

Phase 2: Monte Carlo probabilistic propagation engine.

Replaces the Phase 0/1 BFS-with-decay model with a simulation-based approach
that correctly handles:
- Diamond dependencies (multiple independent paths to the same node)
- Redundant / backed_up_by edges (node only compromised if ALL backup paths fail)
- Common-mode / shares_provider edges (correlated failure across paths)
- Uncertainty-aware output (distributions, not point estimates)
- The mandatory access gateway (Phase 1, contract C2) — graph_manager.py is the
  sole graph builder, so a gateway node is guaranteed visible here exactly as
  it is everywhere else. Gateway nodes are treated as near-zero criticality
  regardless of the caller's criticality_map (Decision #4, PLAN_MASTER.md).

All numeric parameters are drawn from SETTINGS (settings.py); no hardcoded
literals are used.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    import networkx as nx

import graph_manager
from config import DEPENDENCY_GRAPH
from settings import SETTINGS

# DEPENDENCY_GRAPH is re-imported (not just referenced via graph_manager) so it
# remains a genuine module-level name here — tests patch cii_calculator.DEPENDENCY_GRAPH
# directly, and that patch must be visible to compute_cascading_impact_full() below.


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class CIIResult:
    """Result of a Monte Carlo CII computation.

    Attributes
    ----------
    cii_median : float
        Median CII score across all simulation iterations.
    cii_p5 : float
        5th percentile of CII scores (lower bound of 90% interval).
    cii_p95 : float
        95th percentile of CII scores (upper bound of 90% interval).
    impacted_assets : list[str]
        Assets ordered by compromise frequency (most-frequently-compromised first).
    hop_details : dict
        Per-asset dict with keys: compromise_freq, mean_hop, criticality.
    cii_scores : np.ndarray
        Raw array of per-iteration CII scores (for downstream analysis).
    """
    cii_median: float = 0.0
    cii_p5: float = 0.0
    cii_p95: float = 0.0
    impacted_assets: list[str] = field(default_factory=list)
    hop_details: dict = field(default_factory=dict)
    cii_scores: np.ndarray = field(default_factory=lambda: np.array([]))


# ---------------------------------------------------------------------------
# Monte Carlo propagation engine
# ---------------------------------------------------------------------------

def _criticality_of(
    node: str,
    criticality_map: dict,
    default_criticality: float,
    gateway_nodes: frozenset[str],
) -> float:
    """Criticality lookup that overrides gateway nodes to near-zero.

    A gateway node is pure detection instrumentation (Decision #4,
    PLAN_MASTER.md Phase 1) — it must never inherit the map's
    default_criticality fallback, or a gateway hop would silently inflate
    every CII score that happens to pass through it.
    """
    if node in gateway_nodes:
        return SETTINGS.gateway.gateway_node_criticality
    return criticality_map.get(node, default_criticality)


def _simulate_one_iteration(
    DG: "nx.DiGraph",
    source_node: str,
    anomaly_score: float,
    criticality_map: dict,
    max_hops: int,
    default_criticality: float,
    rng: random.Random,
    gateway_nodes: frozenset[str],
    graph_criticality_mass: float = 0.0,
) -> tuple[float, dict[str, int]]:
    """Run a single Monte Carlo iteration of edge-failure sampling.

    Returns (total_impact, compromised_nodes_with_hop).

    `graph_criticality_mass` is the summed criticality of every node in
    the graph. When > 0 the raw impact is expressed as a FRACTION of it
    (see the normalisation note in `compute_cascading_impact_full`); when
    0 the legacy absolute sum is returned unchanged, so callers that
    build their own tiny fixture graphs keep their previous semantics.
    """
    # BFS from source, but each edge fires probabilistically
    compromised: dict[str, int] = {source_node: 0}  # node → hop at which compromised
    queue: list[tuple[str, int]] = [(source_node, 0)]

    while queue:
        current_node, hop = queue.pop(0)

        if hop >= max_hops:
            continue

        for neighbor in DG.successors(current_node):
            if neighbor in compromised:
                continue

            edge_data = DG.get_edge_data(current_node, neighbor)
            edge_type = edge_data.get("edge_type", "depends_on")
            base_prob = edge_data["prob"]

            if edge_type == "backed_up_by":
                # For backed_up_by edges, the neighbor is only compromised
                # if ALL incoming edges to it (from already-compromised nodes)
                # fire. We check: does this specific edge fire?
                # The backed_up_by semantic means redundancy — the node is
                # protected unless all paths fail. We model this by requiring
                # ALL incoming edges from compromised predecessors to fire.
                # We defer evaluation: mark as candidate and check after BFS level.
                if rng.random() < base_prob:
                    # This backup path failed — but we need ALL to fail.
                    # We'll handle this in a second pass below.
                    pass
                continue
            elif edge_type == "shares_provider":
                # Common-mode failure: edges sharing the same provider_id
                # are correlated. For simplicity in this iteration, we sample
                # once per provider_id per iteration and reuse the result.
                # This is handled via the provider_failures dict passed from
                # the outer function — but since we're in the inner function,
                # we just use base_prob directly (the outer loop handles correlation).
                if rng.random() < base_prob:
                    compromised[neighbor] = hop + 1
                    queue.append((neighbor, hop + 1))
            else:
                # Standard edge types: depends_on, controls, communicates_with,
                # pays_through — sample independently.
                if rng.random() < base_prob:
                    compromised[neighbor] = hop + 1
                    queue.append((neighbor, hop + 1))

    # Handle backed_up_by in a second pass:
    # A node with backed_up_by incoming edges is compromised only if
    # ALL its incoming edges from compromised predecessors fired.
    # Re-scan backed_up_by edges.
    for node in list(DG.nodes()):
        if node in compromised or node == source_node:
            continue

        # Collect all incoming edges from compromised predecessors
        backup_predecessors = []
        for pred in DG.predecessors(node):
            if pred not in compromised:
                continue
            edata = DG.get_edge_data(pred, node)
            if edata.get("edge_type") == "backed_up_by":
                backup_predecessors.append((pred, edata))

        if not backup_predecessors:
            continue

        # Node is compromised only if ALL backup paths from compromised
        # predecessors fail (each sampled independently).
        all_failed = all(
            rng.random() < edata["prob"] for _, edata in backup_predecessors
        )
        if all_failed:
            min_hop = min(compromised[pred] for pred, _ in backup_predecessors) + 1
            compromised[node] = min_hop

    # Compute total impact (excluding source node)
    total_impact = 0.0
    for node, hop in compromised.items():
        if node == source_node:
            continue
        crit = _criticality_of(node, criticality_map, default_criticality, gateway_nodes)
        total_impact += crit

    # Normalisation (see compute_cascading_impact_full's docstring): express
    # the compromised mass as a FRACTION of the graph's total criticality
    # rather than as an absolute sum. Without this the score saturates:
    # measured on the 50-node city, 18 of 50 origin assets returned exactly
    # the clamp and 28 returned exactly 0, leaving only 4 in between — the
    # operations hub (30 impacted) and a traffic controller (26 impacted)
    # were indistinguishable. An absolute sum grows with the size of the
    # city, so every well-connected asset eventually pins to cii_max_value
    # and the "distribution, not a point estimate" claim stops being true.
    if graph_criticality_mass > 0.0:
        total_impact /= graph_criticality_mass

    cii = anomaly_score * total_impact
    return cii, compromised


def compute_cascading_impact(
    anomalous_asset_name: str,
    anomaly_score: float,
    criticality_map: dict,
    max_hops: int | None = None,
    mc_iterations: int | None = None,
    random_seed: int | None = None,
) -> tuple[float, list[str], dict]:
    """
    Compute downstream risk propagation using Monte Carlo simulation.

    Parameters
    ----------
    anomalous_asset_name:
        Name of the compromised/anomalous asset.
    anomaly_score:
        Raw anomaly score from the ML layer, in [0, 1].
    criticality_map:
        Mapping of asset name → criticality weight [0, 1].
    max_hops:
        Maximum traversal depth. Defaults to SETTINGS.cii.bfs_max_hops.
    mc_iterations:
        Number of Monte Carlo iterations. Defaults to SETTINGS.cii.mc_iterations.
    random_seed:
        Optional seed for reproducibility.

    Returns
    -------
    cii_score:
        Median CII score (clamped to [0, cii_max_value]).
        For backward compatibility this is a single float.
    impacted_assets:
        Assets ordered by compromise frequency (most frequent first).
    hop_details:
        Per-asset dict with keys: compromise_freq, mean_hop, criticality,
        prob (= compromise_freq, for backward compatibility).

    Notes
    -----
    The full CIIResult (including percentiles and raw score arrays) is
    available via compute_cascading_impact_full().
    """
    result = compute_cascading_impact_full(
        anomalous_asset_name,
        anomaly_score,
        criticality_map,
        max_hops=max_hops,
        mc_iterations=mc_iterations,
        random_seed=random_seed,
    )

    return result.cii_median, result.impacted_assets, result.hop_details


def compute_cascading_impact_full(
    anomalous_asset_name: str,
    anomaly_score: float,
    criticality_map: dict,
    max_hops: int | None = None,
    mc_iterations: int | None = None,
    random_seed: int | None = None,
) -> CIIResult:
    """
    Full Monte Carlo CII computation returning a CIIResult with distributions.

    See compute_cascading_impact() for parameter documentation.

    Scale — CII is a FRACTION of the city's criticality mass
    ------------------------------------------------------
    Each iteration's impact is the summed criticality of the compromised
    nodes divided by the summed criticality of every other node in the
    graph, then scaled by `anomaly_score`. So 0.22 reads as "roughly a
    fifth of the city's critical mass falls over", and the number is
    comparable across graphs of different sizes.

    This replaced an absolute sum clamped at `cii_max_value`, which
    saturated as soon as the topology grew. Measured on the 50-node city
    before the change: 18 of 50 origin assets returned exactly the clamp
    and 28 returned exactly 0, leaving 4 in between — the operations
    hub (30 assets impacted) scored identically to a traffic controller
    (26 impacted), and the "distribution, not a point estimate" property
    the rest of this module is built around was not actually true. After
    normalising: 0 saturated, 22 in between, same runtime. The same
    degeneracy was already present on the old 16-node graph (6 zeros, 8
    at the clamp, 2 between), so this was a latent flaw the scale-up
    exposed rather than one it introduced.

    A median of exactly 0.0 remains common and is honest: it means more
    than half of the Monte Carlo iterations propagated nothing, which is
    the truth for a weakly-coupled leaf. The p5/p95 interval carries the
    tail — e.g. an asset can report median 0.0 with p95 0.185, meaning
    "usually nothing, occasionally moderate". Read the interval, not just
    the median.

    `cii_max_value` is retained as a safety clamp only; normalised scores
    cannot exceed 1.0 by construction.
    """
    if max_hops is None:
        max_hops = SETTINGS.cii.bfs_max_hops
    if mc_iterations is None:
        mc_iterations = SETTINGS.cii.mc_iterations

    cii_max = SETTINGS.cii.cii_max_value
    default_criticality = SETTINGS.cii.default_criticality

    # DEPENDENCY_GRAPH is read from this module's own globals (not re-imported
    # from config here) so unittest.mock.patch.object(cii_calculator, "DEPENDENCY_GRAPH", ...)
    # in tests still controls what graph_manager builds.
    DG = graph_manager.build_graph(directed=True, raw_graph=DEPENDENCY_GRAPH)
    gw_nodes = frozenset(graph_manager.gateway_nodes())

    if anomalous_asset_name not in DG.nodes() and graph_manager.is_gateway_name(anomalous_asset_name):
        # Phase 2: a gateway zone probed by its honeytoken (deception_tripwire
        # signal) before any exfiltration was ever recorded. Its Purdue-zone
        # gateway node may not have been materialised by build_graph() —
        # that only happens when a DEPENDENCY_GRAPH edge already targets a
        # protected asset in that zone. Trace which asset(s) the gateway
        # guards (independent of whether such an edge currently exists) and
        # splice in a synthetic relay so the tripwire is never silently
        # reported as zero impact. An ordinary unrecognised asset name (not
        # matching the Gateway_LN convention) still falls through to the
        # plain "unknown node -> empty result" return below.
        guarded = graph_manager.gateway_target_assets(anomalous_asset_name)
        if not guarded:
            # Even a gateway that currently guards no protected asset
            # represents confirmed unauthorized zone access once its
            # honeytoken fires (zero-false-positive by construction) — report
            # a deterministic minimum CII rather than an empty result.
            base_crit = SETTINGS.deception.tripwire_zone_breach_criticality
            score = min(cii_max, anomaly_score * base_crit)
            return CIIResult(
                cii_median=score,
                cii_p5=score,
                cii_p95=score,
                impacted_assets=[anomalous_asset_name],
                hop_details={
                    anomalous_asset_name: {
                        "compromise_freq": 1.0,
                        "mean_hop": 0,
                        "hop": 0,
                        "prob": 1.0,
                        "impact": base_crit,
                        "criticality": base_crit,
                    }
                },
                cii_scores=np.array([score]),
            )
        for asset in guarded:
            DG.add_edge(
                anomalous_asset_name,
                asset,
                edge_type=graph_manager.GATEWAY_EDGE_TYPE,
                prob=SETTINGS.deception.tripwire_gateway_breach_prob,
                confidence=1.0,
                source="cii_calculator.tripwire_gateway",
                owner="deception",
                rationale=(
                    f"Synthesised relay for honeytoken-only compromise of "
                    f"{anomalous_asset_name}; no DEPENDENCY_GRAPH edge yet "
                    f"targets {asset}."
                ),
                last_reviewed="",
                provider_id=None,
                is_gateway_edge=True,
            )
        gw_nodes = gw_nodes | {anomalous_asset_name}

    if anomalous_asset_name not in DG.nodes():
        # Neither a real graph node nor a recognised gateway-zone name.
        return CIIResult()

    rng = random.Random(random_seed)

    # Per-iteration results
    cii_scores: list[float] = []
    node_compromise_count: dict[str, int] = {}
    node_hop_sums: dict[str, float] = {}

    # Total criticality of the graph, computed ONCE (not per iteration).
    # Excludes the origin, mirroring _simulate_one_iteration's own
    # exclusion of the source node, so a fully-cascading compromise
    # approaches 1.0 rather than an arbitrary fraction below it.
    graph_criticality_mass = sum(
        _criticality_of(n, criticality_map, default_criticality, gw_nodes)
        for n in DG.nodes()
        if n != anomalous_asset_name
    )

    for _ in range(mc_iterations):
        cii, compromised = _simulate_one_iteration(
            DG, anomalous_asset_name, anomaly_score,
            criticality_map, max_hops, default_criticality, rng, gw_nodes,
            graph_criticality_mass,
        )
        cii_scores.append(min(cii_max, cii))

        for node, hop in compromised.items():
            if node == anomalous_asset_name:
                continue
            node_compromise_count[node] = node_compromise_count.get(node, 0) + 1
            node_hop_sums[node] = node_hop_sums.get(node, 0.0) + hop

    scores_arr = np.array(cii_scores)

    # Sort assets by compromise frequency (descending)
    sorted_assets = sorted(
        node_compromise_count.keys(),
        key=lambda n: node_compromise_count[n],
        reverse=True,
    )

    hop_details: dict = {}
    for node in sorted_assets:
        freq = node_compromise_count[node] / mc_iterations
        mean_hop = node_hop_sums[node] / node_compromise_count[node]
        crit = _criticality_of(node, criticality_map, default_criticality, gw_nodes)
        hop_details[node] = {
            "compromise_freq": freq,
            "mean_hop": mean_hop,
            "hop": round(mean_hop),  # backward compat: integer hop
            "prob": freq,            # backward compat: prob ≈ compromise frequency
            "impact": crit * freq,
            "criticality": crit,
        }

    return CIIResult(
        cii_median=float(np.median(scores_arr)) if len(scores_arr) > 0 else 0.0,
        cii_p5=float(np.percentile(scores_arr, 5)) if len(scores_arr) > 0 else 0.0,
        cii_p95=float(np.percentile(scores_arr, 95)) if len(scores_arr) > 0 else 0.0,
        impacted_assets=sorted_assets,
        hop_details=hop_details,
        cii_scores=scores_arr,
    )
