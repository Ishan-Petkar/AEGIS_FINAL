"""
backend/detection/tgnn.py — Temporal Graph Neural Network detector.

Graph-based structural anomaly detection inspired by E-GraphSAGE and Anomal-E,
using NetworkX + scikit-learn in place of PyTorch for lightweight deployment.

Why this detector exists
-------------------------------------------------------------------------
The existing five detection channels are signal-centric: volumetric features
(size, duration, count), timing patterns (inter-arrival periodicity), rule
matches (signatures), tripwires (honeytokens), and supervised features
(out-of-distribution classification). Together they cover TEMPORAL and FEATURE
anomalies, but NOT STRUCTURAL anomalies.

A structural anomaly is a deviation in who talks to whom and how traffic
distributes across an asset's peer set. An attacker whose behavior is:
  - Volumetrically quiet (passes the size/count floor)
  - Temporally regular (looks like a cron job or health check)
  - Syntactically normal (passes all signature rules)
... but TOPOLOGICALLY anomalous (talking to unusual peers, concentrating
traffic on new connections, suddenly centralizing traffic flow) is invisible
to all five existing channels.

The T-GNN detector closes this blind spot by learning normal STRUCTURAL
roles and flagging departures.

2026-09-04 pivot: pooled global centrality inverted the signal
-------------------------------------------------------------------------
The first version of this detector (kept in git history) scored nodes on
POOLED GLOBAL features — weighted in/out-degree, PageRank, clustering
coefficient — fit once across the whole graph. Offline replay against
CIC-IDS2017 friday-morning showed the signal was INVERTED: BENIGN fired at
20.19% and the Bot (Ares C2) label fired at only 9.00%, against
signature's 26.65% and beaconing's 22.69% on the same traffic. Root cause,
diagnosed from the same replay:

  1. Gateways and DNS servers are naturally global hubs. High weighted
     degree and PageRank are a property of that ROLE, not of attack
     behavior, so every busy legitimate server scored as a structural
     outlier and inflated the benign firing rate.
  2. The Ares bot in this capture has out-degree 1-2 — it looks like a
     textbook low-centrality INLIER in a feature space built entirely from
     "how central is this node globally." A quiet, low-degree attacker is
     invisible to a detector that only asks "is this node unusually
     connected," because it is, if anything, unusually UNconnected.

Both failures trace to the same design error: PageRank/degree centrality
measure a node's position in the network's HIERARCHY, which is a stable
property of infrastructure role, not a signal of compromise. This module
now scores nodes on SELF-TEMPORAL DRIFT instead — how a node's own
behavior has changed relative to its own history — which a hub cannot
fail merely by being a hub, and a low-degree attacker cannot evade merely
by staying quiet, since "suddenly talking to a peer it has never talked
to" is exactly as visible at out-degree 2 as at out-degree 200.

Algorithm — self-temporal drift + edge novelty
-------------------------------------------------------------------------
1. Maintain a sliding-window DiGraph (`_graph`) where nodes are IPs and
   edges are weighted by flow count/bytes in the window. Edges older than
   `tgnn_window_sec` are pruned — this is the node's CURRENT/live state.

2. Separately, maintain a never-pruned, LRU-bounded HISTORY of every
   node's out-neighbors and per-peer byte totals ever observed
   (`_history_out_peers`, `_history_weights`), plus a global set of every
   destination IP ever observed anywhere in the network
   (`_history_global_destinations`). This is the node's BASELINE — it
   grows continuously and is read *before* each batch is merged into it,
   so a batch is always scored against strictly prior history, never
   against itself.

3. Per scorable node (out-degree, in the LIVE window, at or above
   `tgnn_min_edges_to_score`), extract four temporal-delta features —
   see `_extract_node_features` for the exact formulas:
     - `unseen_peer_ratio`   — fraction of this window's out-peers absent
                               from the node's historical out-peer set.
     - `degree_expansion`    — live out-degree / (historical out-peer
                               count + 1): how much the node's current
                               fan-out exceeds its own established norm.
     - `neighbor_drift`      — Jaccard DISTANCE between the node's
                               historical and current out-peer sets.
     - `traffic_entropy_delta` — |current edge-weight entropy − historical
                               edge-weight entropy| for this node's
                               out-edges.
   Deliberately OUT-degree/OUT-neighbors only, never in-degree or
   predecessors: a node's popularity as a DESTINATION (many clients
   dialing into it) is exactly the hub-centrality signal that inverted
   the old design, and is excluded by construction rather than merely
   down-weighted.

   Cold start: a node with NO history at all (first time ever observed)
   has no per-node baseline to drift against. `unseen_peer_ratio` falls
   back to EDGE NOVELTY against the GLOBAL destination history instead —
   is this node's traffic going somewhere the network as a whole has
   never sent traffic to before — which is exactly the shape of a
   previously-unseen low-degree beacon reaching out to a fresh C2
   address, the case the pooled-centrality design was blind to.

4. Anomaly scoring via IsolationForest over the four-feature vector — no
   global centrality feature is fed to the forest at all.

Why the fitted forest doesn't degenerate on a single snapshot
-------------------------------------------------------------------------
Every one of the four features above is a DELTA between "current" and
"baseline." If both were read at the same instant — as the previous
design's single end-of-warmup snapshot did for its one delta feature,
`neighbor_drift` — every node's delta is exactly 0 (or exactly 1 for a
never-before-seen node) at that instant, by construction. A matrix with
zero variance in every column cannot be split by an IsolationTree: every
tree is depth 0 and `decision_function` returns 0.0 for everything,
including a genuine outlier.

This module avoids that trap structurally: features are computed and
appended to a rolling training buffer (`_training_rows`, capped at
`tgnn_max_training_rows`) on EVERY batch, starting from the first one —
not only once, at the moment the baseline is deemed ready. Different
batches catch different nodes at different points in their own history
(a node's first appearance contributes a high-drift row; the same node
ten batches later, once its footprint has accumulated, contributes a
near-zero one), so the accumulated buffer has genuine per-row variance
by the time `tgnn_baseline_batches` is reached, even though any single
instant does not. The buffer keeps rolling after the first fit, so a
periodic refit (`tgnn_refit_every_batches`) trains on the population's
*recent* temporal behavior rather than its entire session history.

Calibration
-------
IsolationForest's `score_samples()` has no fixed range — it depends on the
fitted forest's tree depths, which in turn depend on how many rows were in
the training buffer. A hardcoded linear map (e.g. assuming scores fall in
[-1, 0.2]) is unsound: on a small buffer, scores cluster far more negative
than that assumption, and a fixed map would flag nearly every node as
anomalous even on perfectly stable traffic.

A percentile rank against the baseline's own scores is equally unsound, for
the opposite reason: a percentile is UNIFORM over its reference population by
construction, so a threshold `t` fires on `(1 - t)` of perfectly normal nodes
whatever `t` is.

Instead, `calibrated_score` is anchored to the model's own **contamination
boundary**. `decision_function` is `score_samples - offset_`, with `offset_`
set by `contamination`, so its sign is the forest's inlier/outlier verdict
(`< 0` is exactly `predict == -1`). `_calibrate` maps `decision == 0` to
`fire_threshold` exactly, inliers into `[0, fire_threshold)` and outliers
into `[fire_threshold, 1.0]`, dividing by the empirical 95th-percentile
spread of the training population on each side of the boundary
(`_baseline_df_*_scale`, captured at fit time from the fitted model — not a
constant). Firing is then governed by `contamination`, the parameter that
exists to govern it.

`certainty` is always `Certainty.HEURISTIC`: topology drift alone cannot
confirm a compromise — it can also signal legitimate topology changes,
failovers, or new service deployments.
"""

from __future__ import annotations

import logging
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from typing import Optional, Sequence

import networkx as nx
import numpy as np
from sklearn.ensemble import IsolationForest

from backend.config import BACKEND_SETTINGS
from backend.detection.contracts import (
    DETECTOR_TGNN,
    Certainty,
    DetectorVerdict,
    FlowFeatures,
)

logger = logging.getLogger(__name__)

#: Feature vector column order — kept as a module constant so
#: `_extract_node_features`, `_fit_baseline` and `_score_nodes` cannot
#: silently drift out of sync with each other.
_FEATURE_NAMES = (
    "unseen_peer_ratio",
    "degree_expansion",
    "neighbor_drift",
    "traffic_entropy_delta",
)


class TGNNDetector:
    """Temporal Graph Neural Network detector — see the module docstring.

    Stateful and batch-oriented per the `FlowDetector` protocol: the graph
    and history accumulate over time and the baseline is fitted lazily on
    a rolling buffer of per-batch feature rows, so verdicts on later
    flows reflect prior observations.
    """

    #: Stable name written to `event_scores.detector`.
    name: str = DETECTOR_TGNN

    def __init__(
        self,
        window_sec: Optional[float] = None,
        max_nodes: Optional[int] = None,
        baseline_batches: Optional[int] = None,
        refit_every_batches: Optional[int] = None,
        min_baseline_nodes: Optional[int] = None,
        min_edges_to_score: Optional[int] = None,
        fire_threshold: Optional[float] = None,
        contamination: Optional[float] = None,
        reliability: Optional[float] = None,
        max_training_rows: Optional[int] = None,
    ) -> None:
        # Optional-override convention (CLAUDE.md section 5)
        self._window_sec = (
            window_sec if window_sec is not None else BACKEND_SETTINGS.tgnn_window_sec
        )
        self._max_nodes = (
            max_nodes if max_nodes is not None else BACKEND_SETTINGS.tgnn_max_nodes
        )
        self._baseline_batches = (
            baseline_batches if baseline_batches is not None else BACKEND_SETTINGS.tgnn_baseline_batches
        )
        self._refit_every_batches = (
            refit_every_batches
            if refit_every_batches is not None
            else BACKEND_SETTINGS.tgnn_refit_every_batches
        )
        self._min_baseline_nodes = (
            min_baseline_nodes
            if min_baseline_nodes is not None
            else BACKEND_SETTINGS.tgnn_min_baseline_nodes
        )
        self._min_edges_to_score = (
            min_edges_to_score
            if min_edges_to_score is not None
            else BACKEND_SETTINGS.tgnn_min_edges_to_score
        )
        self._fire_threshold = (
            fire_threshold if fire_threshold is not None else BACKEND_SETTINGS.tgnn_fire_threshold
        )
        self._contamination = (
            contamination if contamination is not None else BACKEND_SETTINGS.tgnn_contamination
        )
        self._reliability = (
            reliability if reliability is not None else BACKEND_SETTINGS.hybrid_weight_tgnn
        )
        self._max_training_rows = (
            max_training_rows
            if max_training_rows is not None
            else BACKEND_SETTINGS.tgnn_max_training_rows
        )

        # DiGraph: nodes are IPs, edges weighted by (count, bytes_sum).
        # Edges carry timestamp of last update for pruning. This is the
        # node's CURRENT/live state — see module docstring.
        self._graph: nx.DiGraph = nx.DiGraph()

        # LRU node tracking (same pattern as BeaconingDetector)
        self._node_created_at: OrderedDict[str, datetime] = OrderedDict()

        # Never-pruned HISTORY — the node's baseline. `_history_out_peers`
        # is the cumulative set of distinct out-neighbors ever observed
        # per node; `_history_weights` the cumulative bytes sent to each
        # of those peers. Both are purged only on LRU eviction (mirroring
        # `_graph`'s own eviction) or `reset()`.
        self._history_out_peers: dict[str, set[str]] = {}
        self._history_weights: dict[str, dict[str, float]] = {}
        #: LRU set (as an OrderedDict) of every destination IP ever seen
        #: anywhere in the network — the reference a node with NO history
        #: of its own falls back to for edge-novelty scoring. See module
        #: docstring "Cold start".
        self._history_global_destinations: OrderedDict[str, None] = OrderedDict()

        # Rolling training buffer — see module docstring "Why the fitted
        # forest doesn't degenerate on a single snapshot".
        self._training_rows: deque[np.ndarray] = deque(maxlen=self._max_training_rows)

        # Baseline fitting state
        self._batches_seen = 0
        self._baseline_fitted = False
        #: Batch index at which the baseline was last fitted — drives the
        #: periodic refit in `examine()`.
        self._fitted_at_batch = 0
        self._isolation_forest: Optional[IsolationForest] = None
        #: Empirical spread of the training population's `decision_function`
        #: output on each side of the contamination boundary. These are
        #: the scale factors the piecewise calibration divides by, so the
        #: map is anchored to the fitted model's own distribution rather
        #: than to any constant. `None` until fitted.
        self._baseline_df_pos_scale: Optional[float] = None
        self._baseline_df_neg_scale: Optional[float] = None

    # -----------------------------------------------------------------
    # FlowDetector protocol
    # -----------------------------------------------------------------

    def examine(self, flows: Sequence[FlowFeatures]) -> list[DetectorVerdict]:
        """Return exactly one verdict per input flow, in input order."""
        # Update the LIVE graph with this batch's flows.
        for flow in flows:
            self._add_flow_to_graph(flow)

        # Prune edges older than window_sec, so `_graph` reflects only the
        # CURRENT window.
        self._prune_old_edges()

        # Temporal-delta features are computed against HISTORY as it
        # stood BEFORE this batch is merged into it (see
        # `_merge_batch_into_history` below) — a batch is always scored
        # against strictly prior state, never against itself. This also
        # runs before the baseline exists, unlike the old whole-graph
        # PageRank/clustering pass it replaces: these features are cheap
        # (each is O(that node's own out-degree), not a whole-graph
        # centrality computation), and rows collected before the baseline
        # is ready are exactly what makes the training buffer have real
        # variance once it IS ready — see module docstring.
        features_dict = self._extract_node_features()
        self._accumulate_training_rows(features_dict)

        # Baseline: fit once after warmup, then REFIT periodically so the
        # reference distribution tracks the traffic's current normal.
        self._batches_seen += 1
        if not self._baseline_fitted:
            if self._batches_seen >= self._baseline_batches:
                self._fit_baseline()
        elif self._batches_seen - self._fitted_at_batch >= self._refit_every_batches:
            self._fit_baseline()

        # The anomaly score is a property of the NODE, not the flow —
        # every flow from the same source resolves to the same score.
        # Scoring each DISTINCT source node once, in a single vectorised
        # `decision_function` call, rather than once per flow, is the
        # same batching contract the previous design established (see
        # `tests/test_tgnn.py::test_scores_each_distinct_node_once_per_batch`).
        node_scores = self._score_nodes(flows, features_dict)

        # Score each flow. Verdicts are built from `features_dict` /
        # `self._history_*` as they stand BEFORE the merge below, so
        # evidence and history stay consistent with each other.
        verdicts: list[DetectorVerdict] = []
        for flow in flows:
            verdicts.append(self._verdict_for_flow(flow, features_dict, node_scores))

        # Only now fold this batch into history, so it becomes part of
        # every later batch's baseline but was no part of its own.
        self._merge_batch_into_history(flows)

        return verdicts

    def reset(self) -> None:
        """Clear all state. Used by tests and session boundaries."""
        self._graph.clear()
        self._node_created_at.clear()
        self._history_out_peers.clear()
        self._history_weights.clear()
        self._history_global_destinations.clear()
        self._training_rows.clear()
        self._batches_seen = 0
        self._baseline_fitted = False
        self._fitted_at_batch = 0
        self._isolation_forest = None
        self._baseline_df_pos_scale = None
        self._baseline_df_neg_scale = None

    @property
    def tracked_nodes(self) -> int:
        """Number of nodes currently in the graph."""
        return len(self._graph)

    # -----------------------------------------------------------------
    # Internals — graph / history bookkeeping
    # -----------------------------------------------------------------

    def _add_flow_to_graph(self, flow: FlowFeatures) -> None:
        """Add a flow as an edge to the LIVE graph, updating edge weights."""
        src = flow.source_ip
        tgt = flow.destination_ip

        # Add nodes if new, applying LRU cap
        for ip in [src, tgt]:
            if ip not in self._graph:
                self._graph.add_node(ip)
                self._node_created_at[ip] = flow.ts
                self._node_created_at.move_to_end(ip)
                # Evict oldest node if we've exceeded the cap — purge its
                # HISTORY too, so eviction actually bounds memory rather
                # than bounding only the live graph.
                while len(self._graph) > self._max_nodes:
                    old_ip = next(iter(self._node_created_at))
                    self._graph.remove_node(old_ip)
                    del self._node_created_at[old_ip]
                    self._history_out_peers.pop(old_ip, None)
                    self._history_weights.pop(old_ip, None)
            else:
                self._node_created_at.move_to_end(ip)

        # Add or update edge
        if self._graph.has_edge(src, tgt):
            edge_data = self._graph[src][tgt]
            edge_data["count"] = edge_data.get("count", 0) + 1
            edge_data["bytes_sum"] = edge_data.get("bytes_sum", 0) + flow.bytes
        else:
            self._graph.add_edge(src, tgt, count=1, bytes_sum=flow.bytes)

        # Record timestamp for pruning
        self._graph[src][tgt]["last_seen"] = flow.ts

    def _prune_old_edges(self) -> None:
        """Remove edges not seen in the last `window_sec` seconds."""
        if not self._graph.edges():
            return

        now = max(
            (self._graph[u][v]["last_seen"] for u, v in self._graph.edges()),
            default=datetime.now(),
        )
        window_start = now - timedelta(seconds=self._window_sec)

        edges_to_remove = [
            (u, v)
            for u, v in self._graph.edges()
            if self._graph[u][v]["last_seen"] < window_start
        ]
        for u, v in edges_to_remove:
            self._graph.remove_edge(u, v)

    def _merge_batch_into_history(self, flows: Sequence[FlowFeatures]) -> None:
        """Fold this batch's edges into the never-pruned HISTORY, so future
        batches see today's edges as baseline. Called AFTER scoring —
        never before — so a batch is never compared against itself."""
        for flow in flows:
            src, dst = flow.source_ip, flow.destination_ip
            # `src` may already have been LRU-evicted from `_graph` by a
            # later flow in this same batch (eviction happens as nodes
            # are added, in `_add_flow_to_graph`). Skip it here too, or
            # HISTORY grows unbounded despite the node cap.
            if src not in self._graph:
                continue
            self._history_out_peers.setdefault(src, set()).add(dst)
            weights = self._history_weights.setdefault(src, {})
            weights[dst] = weights.get(dst, 0.0) + flow.bytes

            if dst in self._history_global_destinations:
                self._history_global_destinations.move_to_end(dst)
            else:
                self._history_global_destinations[dst] = None
                while len(self._history_global_destinations) > self._max_nodes:
                    self._history_global_destinations.popitem(last=False)

    # -----------------------------------------------------------------
    # Internals — feature extraction
    # -----------------------------------------------------------------

    @staticmethod
    def _shannon_entropy(weights: Sequence[float]) -> float:
        """Normalised Shannon entropy, in [0, 1], of a weight distribution.

        0.0 for no weights, a single weight, or an all-zero total — all
        three are "no distribution to speak of," not an error. Real
        CIC-IDS2017 rows carry `bytes=0`, so an all-zero total is not
        hypothetical (see the zero-byte regression this guard has always
        existed for, first documented against the previous version of
        this method).
        """
        positive = [w for w in weights if w > 0]
        if len(positive) <= 1:
            return 0.0
        total = sum(positive)
        probs = np.array([w / total for w in positive])
        entropy = -np.sum(probs * np.log2(probs))
        max_entropy = np.log2(len(positive))
        return float(entropy / max_entropy) if max_entropy > 0 else 0.0

    def _extract_node_features(self) -> dict[str, np.ndarray]:
        """Extract the four temporal-delta features for every node
        currently in the LIVE graph, against HISTORY as it stands right
        now (i.e. strictly before the batch presently being examined is
        merged in — see `examine()`).

        Column order matches `_FEATURE_NAMES`. OUT-degree/OUT-neighbors
        only, by design — see module docstring on why in-degree/PageRank
        are excluded entirely rather than down-weighted.
        """
        features: dict[str, np.ndarray] = {}

        for node in self._graph.nodes():
            current_peers = frozenset(self._graph.successors(node))
            current_weights = [
                self._graph[node][peer].get("bytes_sum", 1) for peer in current_peers
            ]
            current_entropy = self._shannon_entropy(current_weights)

            baseline_peers = self._history_out_peers.get(node)
            has_own_baseline = bool(baseline_peers)

            if current_peers:
                if has_own_baseline:
                    reference = baseline_peers
                else:
                    # Cold start: no history for THIS node at all. Fall
                    # back to global edge novelty — see module docstring.
                    reference = self._history_global_destinations
                unseen_peer_ratio = len(
                    current_peers.difference(reference)
                ) / len(current_peers)
            else:
                unseen_peer_ratio = 0.0

            baseline_degree = len(baseline_peers) if baseline_peers else 0
            degree_expansion = self._graph.out_degree(node) / (baseline_degree + 1)

            if has_own_baseline or current_peers:
                union_size = len((baseline_peers or set()) | current_peers)
                intersection_size = len((baseline_peers or set()) & current_peers)
                jaccard = intersection_size / union_size if union_size else 1.0
                neighbor_drift = 1.0 - jaccard
            else:
                neighbor_drift = 0.0

            baseline_weights = self._history_weights.get(node, {})
            baseline_entropy = self._shannon_entropy(list(baseline_weights.values()))
            traffic_entropy_delta = abs(current_entropy - baseline_entropy)

            features[node] = np.array(
                [unseen_peer_ratio, degree_expansion, neighbor_drift, traffic_entropy_delta],
                dtype=np.float32,
            )

        return features

    def _accumulate_training_rows(self, features_dict: dict[str, np.ndarray]) -> None:
        """Append this batch's SCORABLE-node feature rows to the rolling
        training buffer. Runs every batch, including before the baseline
        exists — see module docstring "Why the fitted forest doesn't
        degenerate on a single snapshot"."""
        for node, feat in features_dict.items():
            if self._graph.out_degree(node) >= self._min_edges_to_score:
                self._training_rows.append(feat)

    # -----------------------------------------------------------------
    # Internals — fitting and scoring
    # -----------------------------------------------------------------

    def _fit_baseline(self) -> None:
        """Fit IsolationForest on the rolling training buffer.

        Fitted on EXACTLY the population that will later be scored —
        rows come only from nodes passing the `min_edges_to_score` gate
        (`_accumulate_training_rows`) — and not on the whole graph. This
        is the difference between a working channel and a permanently
        saturated one: fitting on everything and scoring only the small
        fraction of genuinely high-fan-out nodes asks the forest whether
        a busy node looks like an idle leaf, which it does not, so every
        busy node would score as a maximal outlier regardless of whether
        its behavior actually changed.
        """
        if len(self._training_rows) < self._min_baseline_nodes:
            logger.debug(
                "T-GNN baseline fitting deferred: %d training rows < %d required",
                len(self._training_rows),
                self._min_baseline_nodes,
            )
            return

        X = np.array(self._training_rows)

        try:
            self._isolation_forest = IsolationForest(
                contamination=self._contamination,
                random_state=42,
            )
            self._isolation_forest.fit(X)
            # Decision-boundary scales. `decision_function` is
            # `score_samples - offset_`, where `offset_` is set by
            # `contamination`, so its sign IS the model's own inlier /
            # outlier verdict: >= 0 inlier, < 0 outlier, and `predict`
            # returns -1 exactly when it is negative. Calibration is
            # anchored to that boundary and scaled by how far the
            # training population actually spreads on each side of it —
            # an empirical quantity from the fitted model, not a
            # constant. The 95th-percentile magnitude is used rather than
            # the max so a single extreme row cannot compress the whole
            # scale; the map clamps beyond it either way.
            baseline_df = self._isolation_forest.decision_function(X)
            positives = baseline_df[baseline_df > 0]
            negatives = -baseline_df[baseline_df < 0]
            self._baseline_df_pos_scale = (
                float(np.percentile(positives, 95)) if positives.size else None
            )
            self._baseline_df_neg_scale = (
                float(np.percentile(negatives, 95)) if negatives.size else None
            )
            self._baseline_fitted = True
            self._fitted_at_batch = self._batches_seen
            logger.info(
                f"T-GNN baseline fitted on {len(X)} training rows "
                f"(buffer cap {self._max_training_rows}), "
                f"{self._graph.number_of_nodes()} live nodes"
            )
        except Exception as e:
            logger.error(f"T-GNN baseline fitting failed: {e}")

    def _score_nodes(
        self,
        flows: Sequence[FlowFeatures],
        features_dict: dict[str, np.ndarray],
    ) -> dict[str, float]:
        """Anomaly-score every DISTINCT scorable source node in this batch
        in one vectorised `decision_function` call — see `examine()`'s
        comment for why this is batched rather than done per flow.

        Only source nodes that would actually pass `_verdict_for_flow`'s
        gates are scored, so a batch of flows from unknown or low-degree
        nodes costs nothing. Returns `{}` before the baseline is fitted,
        which is the state in which every verdict abstains anyway.
        """
        if not self._baseline_fitted or self._isolation_forest is None:
            return {}

        nodes = sorted(
            {
                flow.source_ip
                for flow in flows
                if flow.source_ip in features_dict
                and self._graph.out_degree(flow.source_ip) >= self._min_edges_to_score
            }
        )
        if not nodes:
            return {}

        X = np.array([features_dict[node] for node in nodes])
        scores = self._isolation_forest.decision_function(X)
        return {node: float(score) for node, score in zip(nodes, scores)}

    def _calibrate(self, decision: float) -> float:
        """Map a `decision_function` value onto [0, 1], anchored so that the
        contamination boundary (`decision == 0`) is exactly
        `fire_threshold`.

        Inliers (`decision > 0`) fall in `[0, fire_threshold)` and outliers
        (`decision < 0`) in `[fire_threshold, 1.0]`, each scaled by how far
        the *training population* actually spread on that side of the
        boundary (`_baseline_df_*_scale`, captured at fit time). Scores are
        therefore interpretable as a distance from the model's own
        decision boundary, and the firing rate is governed by
        `contamination` — the parameter that exists to govern it — rather
        than being uniform by construction.

        A side with no training observations (e.g. contamination so low the
        fit produced no negatives) has no empirical scale, so anything on
        that side saturates to its endpoint rather than being scaled by a
        made-up number.
        """
        threshold = self._fire_threshold
        if decision >= 0.0:
            scale = self._baseline_df_pos_scale
            if not scale:
                return threshold
            depth = min(decision / scale, 1.0)
            return float(threshold * (1.0 - depth))

        scale = self._baseline_df_neg_scale
        if not scale:
            return 1.0
        depth = min(-decision / scale, 1.0)
        return float(threshold + (1.0 - threshold) * depth)

    def _verdict_for_flow(
        self,
        flow: FlowFeatures,
        features_dict: dict[str, np.ndarray],
        node_scores: dict[str, float],
    ) -> DetectorVerdict:
        """Score a flow based on its source node's structural anomaly.

        `features_dict` is the batch-wide feature map extracted once by
        `examine()` — see the comment there for why it is not recomputed
        per flow.
        """
        # Before baseline is fitted, abstain
        if not self._baseline_fitted or self._isolation_forest is None:
            return DetectorVerdict(
                detector=self.name,
                fired=False,
                calibrated_score=0.0,
                reliability=self._reliability,
                certainty=Certainty.HEURISTIC,
                raw_score=None,
                evidence={"abstained": "baseline_not_fitted", "batches_seen": self._batches_seen},
            )

        # Check if source node has enough edges to score
        src = flow.source_ip
        if src not in self._graph or self._graph.out_degree(src) < self._min_edges_to_score:
            return DetectorVerdict(
                detector=self.name,
                fired=False,
                calibrated_score=0.0,
                reliability=self._reliability,
                certainty=Certainty.HEURISTIC,
                raw_score=None,
                evidence={
                    "abstained": "insufficient_edges",
                    "edges": self._graph.out_degree(src) if src in self._graph else 0,
                    "required_edges": self._min_edges_to_score,
                },
            )

        if src not in features_dict:
            return DetectorVerdict(
                detector=self.name,
                fired=False,
                calibrated_score=0.0,
                reliability=self._reliability,
                certainty=Certainty.HEURISTIC,
                raw_score=None,
                evidence={"abstained": "node_not_in_features"},
            )

        # Score precomputed once per distinct node by `_score_nodes`.
        if src not in node_scores:
            return DetectorVerdict(
                detector=self.name,
                fired=False,
                calibrated_score=0.0,
                reliability=self._reliability,
                certainty=Certainty.HEURISTIC,
                raw_score=None,
                evidence={"abstained": "node_not_scored"},
            )

        feat = features_dict[src]
        decision = node_scores[src]
        is_cold_start = not bool(self._history_out_peers.get(src))

        calibrated = self._calibrate(decision)
        # The model's own inlier/outlier call is the gate. `decision < 0`
        # is exactly `predict(x) == -1`, so this is the contamination
        # boundary the forest was fitted to, not a second threshold layered
        # on top of it. The score comparison is kept as well so
        # `fire_threshold` can still tighten (never loosen) the boundary.
        is_outlier = decision < 0.0
        fired = bool(is_outlier and calibrated >= self._fire_threshold)

        return DetectorVerdict(
            detector=self.name,
            fired=fired,
            calibrated_score=calibrated,
            reliability=self._reliability,
            certainty=Certainty.HEURISTIC,
            raw_score=decision,
            evidence={
                "node": src,
                "out_degree": int(self._graph.out_degree(src)),
                "unseen_peer_ratio": float(feat[0]),
                "degree_expansion": float(feat[1]),
                "neighbor_drift": float(feat[2]),
                "traffic_entropy_delta": float(feat[3]),
                "cold_start": is_cold_start,
                "decision_function": float(decision),
                "is_outlier": is_outlier,
                "calibrated_score": calibrated,
                "fire_threshold": self._fire_threshold,
                "comparison": f"calibrated={calibrated:.4f} {'≥' if fired else '<'} threshold={self._fire_threshold:.4f}",
            },
        )
