"""
tests/test_tgnn.py — backend/detection/tgnn.py.

Plain pytest functions (mirrors tests/test_beaconing.py's style), no
fixtures beyond a small local flow-builder helper. Flows are constructed
directly as `FlowFeatures` — the contract this detector consumes.

2026-09-04 pivot: the detector was rewritten to score self-temporal drift
+ edge novelty instead of pooled global centrality (PageRank, weighted
degree) — see `backend/detection/tgnn.py`'s module docstring for why.
These tests were rewritten alongside it; they specifically probe the two
failure modes that motivated the pivot (a stable high-degree hub must NOT
fire; a low-degree node touching a brand-new peer MUST be visible),
because "some flow fires" is not evidence the original bug is fixed —
firing on the WRONG population is exactly what the bug was.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np

from backend.detection.contracts import DETECTOR_TGNN, Certainty, FlowFeatures
from backend.detection.tgnn import _FEATURE_NAMES, TGNNDetector

_T0 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def _flow(
    ts: datetime,
    src: str = "10.0.0.1",
    dst: str = "10.0.0.2",
    bytes_: int = 1000,
) -> FlowFeatures:
    return FlowFeatures(
        ts=ts,
        source_ip=src,
        source_port=54321,
        destination_ip=dst,
        destination_port=443,
        protocol="TCP",
        duration_sec=0.01,
        packets=2,
        bytes=bytes_,
    )


def _stable_batch(batch_idx: int, n: int = 60, n_src: int = 12, n_dst: int = 5):
    """A batch of benign, repeating traffic — same small set of source and
    destination IPs, cycling deterministically. No structural change from
    batch to batch: every source always reaches the exact same peers with
    the exact same weight distribution, batch after batch.

    Sources deliberately have DIFFERENT fan-outs (2..5 destinations,
    assigned deterministically by source index) rather than all reaching
    every destination, so the training population isn't a single
    identical row repeated — a benign population has a spread of
    structural roles.
    """
    pairs = [
        (f"10.0.0.{s}", f"10.0.1.{d}")
        for s in range(n_src)
        for d in range(min(2 + (s % 4), n_dst))
    ]
    return [
        _flow(
            _T0 + timedelta(seconds=batch_idx * n + i),
            src=pairs[i % len(pairs)][0],
            dst=pairs[i % len(pairs)][1],
        )
        for i in range(n)
    ]


def _fit_baseline(detector: TGNNDetector, batches: int = 6, **kwargs) -> None:
    """Feed enough stable batches to move the detector past baseline
    fitting, matching `baseline_batches` on the given detector.

    Defaults to 6 (not the minimum 3) because the very FIRST batch a node
    is ever seen in is necessarily a "cold start" row (no history yet
    exists to diff against) — see the module docstring's "Why the fitted
    forest doesn't degenerate on a single snapshot". Feeding several
    batches beyond that dilutes those cold-start rows to a minority of
    the training buffer, matching what real traffic looks like far more
    than a 1-of-3 split would.
    """
    for i in range(batches):
        detector.examine(_stable_batch(i, **kwargs))


# ---------------------------------------------------------------------------
# Contract compliance
# ---------------------------------------------------------------------------


def test_examine_returns_one_verdict_per_flow():
    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2)
    flows = _stable_batch(0)
    verdicts = detector.examine(flows)

    assert len(verdicts) == len(flows)
    for v in verdicts:
        assert v.detector == DETECTOR_TGNN
        assert v.certainty is Certainty.HEURISTIC
        assert 0.0 <= v.calibrated_score <= 1.0
        assert 0.0 <= v.reliability <= 1.0


def test_feature_vector_has_no_global_centrality_feature():
    """The 2026-09-04 pivot's whole point: PageRank / weighted-degree
    centrality must be gone from the scoring vector, replaced by
    self-temporal drift + edge novelty. Locks the feature contract down
    so a future change can't silently reintroduce a pooled-centrality
    column."""
    assert _FEATURE_NAMES == (
        "unseen_peer_ratio",
        "degree_expansion",
        "neighbor_drift",
        "traffic_entropy_delta",
    )
    for banned in ("pagerank", "centrality", "clustering", "in_degree"):
        assert banned not in _FEATURE_NAMES

    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2)
    detector.examine(_stable_batch(0))
    feat = detector._extract_node_features()["10.0.0.0"]
    assert feat.shape == (4,)


# ---------------------------------------------------------------------------
# Baseline warmup
# ---------------------------------------------------------------------------


def test_abstains_before_baseline_fitted():
    detector = TGNNDetector(baseline_batches=10, min_edges_to_score=2)
    # Only 2 batches, well short of the 10-batch baseline requirement.
    verdicts = detector.examine(_stable_batch(0))
    verdicts += detector.examine(_stable_batch(1))

    assert detector._baseline_fitted is False
    for v in verdicts:
        assert v.fired is False
        assert v.calibrated_score == 0.0
        assert v.evidence.get("abstained") == "baseline_not_fitted"


def test_training_rows_accumulate_before_baseline_is_fitted():
    """Rows must be collected from the FIRST batch onward, not only once
    the baseline is deemed ready — this is what gives the eventual fit
    matrix real variance instead of a single degenerate snapshot."""
    detector = TGNNDetector(baseline_batches=10, min_edges_to_score=2)
    detector.examine(_stable_batch(0))
    assert len(detector._training_rows) > 0
    assert detector._baseline_fitted is False


# ---------------------------------------------------------------------------
# Structural anomaly detection
# ---------------------------------------------------------------------------


def test_fires_on_structural_anomaly():
    """A genuine topological outlier must cross the model's own
    contamination boundary — `decision_function < 0`, i.e. `predict == -1` —
    and only then map to a score at or above `fire_threshold`."""
    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2, fire_threshold=0.7)
    _fit_baseline(detector, batches=6)
    assert detector._baseline_fitted is True

    # A previously stable source node suddenly fans out to many brand-new
    # destinations it has never talked to — a scan/exfil-shaped structural
    # change no volumetric or timing feature would catch, and (unlike the
    # pre-pivot design) not detected via raw degree/PageRank magnitude
    # either: it is caught because the node's OWN history says it never
    # goes there.
    anomalous_flows = [
        _flow(_T0 + timedelta(seconds=2000 + i), src="10.0.0.0", dst=f"10.9.9.{i}")
        for i in range(30)
    ]
    verdicts = detector.examine(anomalous_flows)

    fired = [v for v in verdicts if v.fired]
    assert fired, [v.evidence for v in verdicts[:3]]
    for v in fired:
        # Fires only where the fitted model itself says "outlier".
        assert v.evidence["is_outlier"] is True
        assert v.evidence["decision_function"] < 0.0
        assert v.calibrated_score >= 0.7
        # And the "why" is the temporal-drift story, not a magnitude one.
        assert v.evidence["unseen_peer_ratio"] > 0.9
        assert v.evidence["neighbor_drift"] > 0.9

    # Cross-check the gate against the estimator directly, rather than
    # trusting the detector's own bookkeeping. Built from the EVIDENCE
    # captured during `examine()`, not a fresh `_extract_node_features()`
    # call — that batch is already merged into history by now, so
    # recomputing afterward would compare the node against itself.
    v0 = next(v for v in verdicts if v.evidence.get("node") == "10.0.0.0")
    feat = np.array(
        [
            v0.evidence["unseen_peer_ratio"],
            v0.evidence["degree_expansion"],
            v0.evidence["neighbor_drift"],
            v0.evidence["traffic_entropy_delta"],
        ]
    ).reshape(1, -1)
    assert detector._isolation_forest.predict(feat)[0] == -1


def test_does_not_fire_on_stable_topology():
    """On unchanged topology, firing is bounded by `contamination` and
    confined to nodes the model itself calls outliers.

    Deliberately NOT asserting zero fires. `contamination` is the fraction
    of the training population the forest is instructed to place below
    its decision boundary, so some rate of firing on a stable population
    is a fitted-model property, not a defect — exactly the knob that
    governs this channel's rate. What must hold is that no INLIER ever
    fires and the rate stays at the contamination scale.
    """
    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2, fire_threshold=0.7)
    _fit_baseline(detector, batches=6)

    fired_nodes: set[str] = set()
    scored_nodes: set[str] = set()

    # More batches of the exact same stable pattern — nothing about the
    # topology has changed.
    for i in range(6, 12):
        for v in detector.examine(_stable_batch(i)):
            if "abstained" in v.evidence:
                continue
            scored_nodes.add(v.evidence["node"])
            if v.fired:
                fired_nodes.add(v.evidence["node"])
                # An inlier firing would mean the boundary gate is broken.
                assert v.evidence["is_outlier"] is True, v.evidence
            else:
                assert v.calibrated_score <= 0.7, v.evidence

    assert scored_nodes
    assert len(fired_nodes) / len(scored_nodes) <= 0.2, (fired_nodes, scored_nodes)


def test_score_is_below_threshold_for_every_inlier():
    """The piecewise map's core invariant, asserted directly on the
    detector's output rather than inferred from firing behaviour."""
    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2, fire_threshold=0.7)
    _fit_baseline(detector, batches=6)

    verdicts = detector.examine(_stable_batch(20))
    scored = [v for v in verdicts if "abstained" not in v.evidence]
    assert scored

    for v in scored:
        if v.evidence["decision_function"] >= 0.0:
            assert v.calibrated_score <= 0.7
        else:
            assert v.calibrated_score >= 0.7


def test_stable_high_degree_hub_does_not_fire():
    """The bug this detector was rewritten to fix, case 1: a legitimate
    high-volume hub (gateway/DNS-shaped — large, but perfectly consistent,
    fan-out) must not be flagged just for being busy. The old
    pooled-centrality design measured ~20% benign firing precisely because
    high weighted-degree/PageRank is a stable property of that ROLE, not
    of attack behavior."""
    hub_src = "10.0.0.200"
    hub_dsts = [f"10.0.9.{i}" for i in range(20)]

    def batch_with_hub(batch_idx: int):
        hub_flows = [
            _flow(_T0 + timedelta(seconds=batch_idx * 100 + i), src=hub_src, dst=d)
            for i, d in enumerate(hub_dsts)
        ]
        return _stable_batch(batch_idx, n=60) + hub_flows

    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2, fire_threshold=0.7)
    for i in range(6):
        detector.examine(batch_with_hub(i))
    assert detector._baseline_fitted is True

    hub_fired = False
    for i in range(6, 12):
        for v in detector.examine(batch_with_hub(i)):
            if v.evidence.get("node") == hub_src:
                assert v.evidence["unseen_peer_ratio"] == 0.0
                assert v.evidence["neighbor_drift"] == 0.0
                if v.fired:
                    hub_fired = True

    assert not hub_fired


def test_low_degree_node_new_peer_is_visible_via_edge_novelty():
    """The bug this detector was rewritten to fix, case 2: a LOW-degree
    node (out-degree 2 — exactly the shape of the Ares bot in the
    friday-morning replay, degree 1-2) that starts talking to peers it
    has never talked to must be visible even though its raw degree never
    changes. The old pooled-centrality design could not see this at all:
    a degree-2 node looks like a textbook inlier in a feature space built
    from "how central is this node," regardless of WHO it talks to.
    """
    quiet_src = "10.0.0.201"
    quiet_dsts = ["10.0.5.1", "10.0.5.2"]

    def batch_with_quiet_node(batch_idx: int):
        quiet_flows = [
            _flow(_T0 + timedelta(seconds=batch_idx * 100 + i), src=quiet_src, dst=d)
            for i, d in enumerate(quiet_dsts)
        ]
        return _stable_batch(batch_idx, n=60) + quiet_flows

    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2, fire_threshold=0.7)
    for i in range(6):
        detector.examine(batch_with_quiet_node(i))
    assert detector._baseline_fitted is True

    # Same out-degree (2) as every prior batch, but to two peers this
    # node — and the whole network — has never seen (TEST-NET-3, RFC
    # 5737, guaranteed not to collide with anything else in this fixture).
    novel_flows = [
        _flow(_T0 + timedelta(seconds=5000), src=quiet_src, dst="203.0.113.10"),
        _flow(_T0 + timedelta(seconds=5001), src=quiet_src, dst="203.0.113.11"),
    ]
    verdicts = detector.examine(novel_flows)

    by_node = {v.evidence.get("node"): v for v in verdicts if "abstained" not in v.evidence}
    assert quiet_src in by_node
    v = by_node[quiet_src]
    assert v.evidence["out_degree"] == 2  # unchanged magnitude
    assert v.evidence["unseen_peer_ratio"] == 1.0
    assert v.evidence["neighbor_drift"] == 1.0
    assert v.fired is True, v.evidence


def test_cold_start_node_falls_back_to_global_edge_novelty():
    """A source node with NO history at all (first appearance) has no
    per-node baseline to diff against. `unseen_peer_ratio` must fall back
    to whether the destination has ever been seen ANYWHERE in the
    network, not trivially read 1.0 for lack of a per-node reference."""
    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2)
    _fit_baseline(detector, batches=6)

    # A brand-new source, talking to a destination the stable baseline
    # traffic already uses heavily (10.0.1.0) — globally familiar, so a
    # cold-start node reaching it should NOT read as fully novel.
    # Evidence is read from the SAME `examine()` call that produced it —
    # not from a manual `_extract_node_features()` call afterward, which
    # would run after that batch's flows are already merged into history
    # and so trivially find the node "familiar" with itself.
    familiar_flows = [
        _flow(_T0 + timedelta(seconds=3000), src="10.0.0.222", dst="10.0.1.0"),
        _flow(_T0 + timedelta(seconds=3001), src="10.0.0.222", dst="10.0.1.1"),
    ]
    verdicts = detector.examine(familiar_flows)
    unseen_familiar = next(
        v.evidence["unseen_peer_ratio"] for v in verdicts if v.evidence.get("node") == "10.0.0.222"
    )
    assert unseen_familiar < 1.0

    # A brand-new source talking to destinations nothing has ever used.
    detector2 = TGNNDetector(baseline_batches=6, min_edges_to_score=2)
    _fit_baseline(detector2, batches=6)
    novel_flows = [
        _flow(_T0 + timedelta(seconds=3000), src="10.0.0.223", dst="198.51.100.1"),
        _flow(_T0 + timedelta(seconds=3001), src="10.0.0.223", dst="198.51.100.2"),
    ]
    verdicts2 = detector2.examine(novel_flows)
    unseen_novel = next(
        v.evidence["unseen_peer_ratio"] for v in verdicts2 if v.evidence.get("node") == "10.0.0.223"
    )
    assert unseen_novel == 1.0  # fully novel globally


# ---------------------------------------------------------------------------
# Window pruning
# ---------------------------------------------------------------------------


def test_window_prunes_old_edges():
    detector = TGNNDetector(window_sec=10.0, baseline_batches=100, min_edges_to_score=2)
    detector.examine([_flow(_T0, src="10.0.0.1", dst="10.0.0.2")])
    assert detector._graph.has_edge("10.0.0.1", "10.0.0.2")

    # A flow far outside the 10s window, between different nodes, should
    # trigger pruning of the old edge once its timestamp is stale relative
    # to the newest observed timestamp.
    detector.examine([_flow(_T0 + timedelta(seconds=100), src="10.0.0.3", dst="10.0.0.4")])
    assert not detector._graph.has_edge("10.0.0.1", "10.0.0.2")


# ---------------------------------------------------------------------------
# LRU bound
# ---------------------------------------------------------------------------


def test_lru_cap_bounds_memory():
    detector = TGNNDetector(max_nodes=10, baseline_batches=100, min_edges_to_score=2)
    # 50 unique source IPs, each talking to a fixed destination -> 51
    # candidate nodes, well over the cap of 10.
    flows = [
        _flow(_T0 + timedelta(seconds=i), src=f"10.0.0.{i}", dst="10.0.1.1")
        for i in range(50)
    ]
    detector.examine(flows)

    assert detector.tracked_nodes <= 10
    # Eviction must purge HISTORY too, not just the live graph, or memory
    # is unbounded despite the node cap.
    assert len(detector._history_out_peers) <= 10


def test_per_node_history_peer_set_is_lru_bounded():
    """`tgnn_max_nodes` bounds how many NODES are tracked; it says
    nothing about how large any single node's own peer history can grow.
    A long-lived node (a gateway) that survives the whole session while
    continuously reaching new destinations must still have its own
    history capped, or a multi-day run leaks memory one peer at a time
    even though the node count stays flat."""
    detector = TGNNDetector(
        baseline_batches=100, min_edges_to_score=2, max_history_peers_per_node=5
    )
    # One long-lived source, 20 distinct destinations across 20 batches —
    # the node itself is never evicted (only one node ever tracked).
    for i in range(20):
        detector.examine([_flow(_T0 + timedelta(seconds=i), src="10.0.0.1", dst=f"10.9.0.{i}")])

    assert len(detector._history_out_peers["10.0.0.1"]) <= 5


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


def test_reset_clears_state():
    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2)
    _fit_baseline(detector, batches=6)
    assert detector._baseline_fitted is True
    assert detector.tracked_nodes > 0
    assert len(detector._history_out_peers) > 0
    assert len(detector._training_rows) > 0

    detector.reset()

    assert detector._baseline_fitted is False
    assert detector.tracked_nodes == 0
    assert detector._isolation_forest is None
    assert detector._history_out_peers == {}
    assert len(detector._history_global_destinations) == 0
    assert len(detector._training_rows) == 0


# ---------------------------------------------------------------------------
# Real-data shapes
# ---------------------------------------------------------------------------


def test_zero_byte_flows_do_not_raise():
    """Zero-byte flows must not crash the detector.

    Regression test for a live failure, not a hypothetical: real
    CIC-IDS2017 rows carry `bytes=0`, so a node whose every outgoing edge
    is zero-byte made the entropy computation divide by a zero total and
    raise ZeroDivisionError out of `examine()`. Every other test in this
    file uses bytes=1000, which is exactly why none of them caught it.
    """
    detector = TGNNDetector(baseline_batches=2, min_edges_to_score=2)

    for batch_idx in range(6):
        flows = [
            _flow(
                _T0 + timedelta(seconds=batch_idx * 20 + i),
                src=f"10.0.0.{i % 5}",
                dst=f"10.0.1.{i % 3}",
                bytes_=0,
            )
            for i in range(20)
        ]
        verdicts = detector.examine(flows)  # must not raise
        assert len(verdicts) == 20


def test_mixed_zero_and_nonzero_byte_flows_do_not_raise():
    """The mixed case — some edges zero-byte, some not — must also hold,
    since that is what real traffic actually looks like."""
    detector = TGNNDetector(baseline_batches=2, min_edges_to_score=2)

    for batch_idx in range(6):
        flows = [
            _flow(
                _T0 + timedelta(seconds=batch_idx * 20 + i),
                src=f"10.0.0.{i % 5}",
                dst=f"10.0.1.{i % 3}",
                bytes_=0 if i % 2 else 1500,
            )
            for i in range(20)
        ]
        verdicts = detector.examine(flows)  # must not raise
        assert len(verdicts) == 20
        for v in verdicts:
            assert 0.0 <= v.calibrated_score <= 1.0


# ---------------------------------------------------------------------------
# Batching invariant
# ---------------------------------------------------------------------------


def test_scores_each_distinct_node_once_per_batch():
    """`decision_function` must be called ONCE per batch, not once per flow.

    This is a performance contract, not a stylistic one. sklearn pays a
    fixed joblib/validation cost per scoring call that dwarfs the
    tree traversal for a single row. Without this test, a later refactor
    could move scoring back inside the per-flow loop and reintroduce that
    regression silently — every other test here would still pass, because
    the OUTPUT is identical (verified: sklearn scores rows independently).
    """
    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2)
    _fit_baseline(detector, batches=6)

    forest = detector._isolation_forest
    calls: list[int] = []
    original = forest.decision_function

    def counting_decision_function(X):
        calls.append(len(X))
        return original(X)

    forest.decision_function = counting_decision_function

    # 20 flows spanning only 5 distinct source IPs.
    flows = _stable_batch(6, n=20, n_src=5, n_dst=3)
    verdicts = detector.examine(flows)

    assert len(verdicts) == 20
    # Exactly one vectorised call, covering at most the distinct sources —
    # never one call per flow.
    assert len(calls) == 1, f"expected 1 batched call, got {len(calls)}"
    assert calls[0] <= 5, f"expected <=5 rows (distinct sources), got {calls[0]}"


def test_same_source_node_scores_consistently_within_batch():
    """Every flow from one source resolves to that node's single score —
    the property that makes per-node batching equivalent to per-flow."""
    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2)
    _fit_baseline(detector, batches=6)

    flows = _stable_batch(6, n=20, n_src=5, n_dst=3)
    verdicts = detector.examine(flows)

    by_source: dict[str, set] = {}
    for flow, verdict in zip(flows, verdicts):
        if "abstained" not in verdict.evidence:
            by_source.setdefault(flow.source_ip, set()).add(verdict.calibrated_score)

    for src, scores in by_source.items():
        assert len(scores) == 1, f"{src} got inconsistent scores: {scores}"


# ---------------------------------------------------------------------------
# Evidence payload
# ---------------------------------------------------------------------------


def test_evidence_payload_shape():
    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2)
    _fit_baseline(detector, batches=6)

    verdicts = detector.examine(_stable_batch(6))
    scored = [v for v in verdicts if "abstained" not in v.evidence]
    assert scored
    v = scored[0]

    assert "node" in v.evidence
    assert "out_degree" in v.evidence
    assert "unseen_peer_ratio" in v.evidence
    assert "degree_expansion" in v.evidence
    assert "neighbor_drift" in v.evidence
    assert "traffic_entropy_delta" in v.evidence
    assert "cold_start" in v.evidence
    assert "calibrated_score" in v.evidence
    assert "fire_threshold" in v.evidence
    assert "decision_function" in v.evidence
    assert "is_outlier" in v.evidence


# ---------------------------------------------------------------------------
# Analyst-readable summary (SOC triage)
# ---------------------------------------------------------------------------


def test_fired_verdict_carries_plain_english_summary():
    """A fired verdict must explain itself in a sentence, not only in
    feature floats. `degree_expansion=7.5` is checkable but not readable;
    an analyst should not need to know this detector's feature space to
    see what it claims happened."""
    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2, fire_threshold=0.7)
    _fit_baseline(detector, batches=6)

    anomalous_flows = [
        _flow(_T0 + timedelta(seconds=2000 + i), src="10.0.0.0", dst=f"10.9.9.{i}")
        for i in range(30)
    ]
    fired = [v for v in detector.examine(anomalous_flows) if v.fired]
    assert fired

    summary = fired[0].evidence["summary"]
    assert "10.0.0.0" in summary
    assert "novel peer" in summary
    # The sentence must agree with the structured features beside it.
    assert f"{fired[0].evidence['degree_expansion']:.1f}x" in summary


def test_non_fired_verdict_has_no_summary():
    """`summary` is an alert-time artifact. Emitting one for every quiet
    flow would put a sentence claiming nothing happened on ~98% of
    traffic, which is noise in the evidence payload and in the DB."""
    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2, fire_threshold=0.7)
    _fit_baseline(detector, batches=6)

    for v in detector.examine(_stable_batch(9)):
        if not v.fired:
            assert "summary" not in v.evidence


def test_cold_start_fired_verdict_summary_says_cold_start():
    """The cold-start path has its own sentence: with no per-node history
    there is no 'expanded Nx baseline' to report, and claiming one would
    be inventing a baseline that does not exist."""
    detector = TGNNDetector(baseline_batches=6, min_edges_to_score=2, fire_threshold=0.7)
    _fit_baseline(detector, batches=6)

    # A never-before-seen host fanning out to globally-novel destinations.
    novel = [
        _flow(_T0 + timedelta(seconds=4000 + i), src="10.0.0.77", dst=f"203.0.113.{i}")
        for i in range(25)
    ]
    fired = [v for v in detector.examine(novel) if v.fired and v.evidence.get("cold_start")]
    assert fired, "expected the cold-start path to fire on 25 globally-novel destinations"

    summary = fired[0].evidence["summary"]
    assert "Cold-start host 10.0.0.77" in summary
    assert "novel external connection" in summary
    # No fan-out multiplier is claimed: with no prior history there is no
    # baseline to have expanded from, and inventing one would be a lie.
    assert "x baseline" not in summary and "x its baseline" not in summary
