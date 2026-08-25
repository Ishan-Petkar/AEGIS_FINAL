"""
backend/streaming.py — Phase 5 Ticket #5: `StreamingScorer` + tripwire fusion.

Location deviation from PLAN_MASTER (docs/PHASE5_TICKET5_PLAN.md section 9)
----------------------------------------------------------------------------
PLAN_MASTER specifies `src/core/streaming.py`; this ticket places the class
at `backend/streaming.py` instead. Two independent, verified reasons force
this:

1. K2 (docs/PHASE5_STATE.md) requires reading
   `BACKEND_SETTINGS.model_artifact_path_resolved`. A `src/` module cannot
   import `backend.config`: `backend/__init__.py` establishes
   `backend -> src` (puts `src/` on `sys.path`), nothing establishes the
   reverse, and `PYTHONPATH=src python -c "import backend"` raises
   `ModuleNotFoundError` from any CWD other than the repo root. A
   `src/`-resident `StreamingScorer` literally cannot satisfy K2.
2. A new untracked file under `src/` would make `git status --short src/`
   non-empty — the one mechanical Invariant-A check every Phase 5 ticket
   has been accepted against.

See docs/PHASE5_TICKET5_PLAN.md section 9 for the full argument, including
why "the CI duplicate-def check protects src/" is explicitly NOT part of
this reasoning (section 12 item 2: that check globs `src/*.py` — top-level
only, not recursive — so `src/core/streaming.py` would never have been
scanned by it either way).

Invariant B — the whole point of this ticket
----------------------------------------------------------------------------
`ml_engine.preprocess_features()` ends in `scaler.fit_transform(X)`: a
FRESH `StandardScaler` on every call. Measured cost of naively reusing that
call per micro-batch (docs/PHASE5_TICKET5_PLAN.md section 1), on a real
500-flow friday-morning batch against the full-Monday warmup baseline: the
`packets` standard deviation collapses to 5.5% of the true baseline value.
Every z-score, every calibrated score, and every `explain()` sentence
computed from a refit scaler is silently wrong — nothing raises, nothing
logs, nothing looks broken.

`StreamingScorer` fits its scaler and model EXACTLY ONCE, in
`fit_from_warmup()`, on all-benign warmup data. Every other method
(`score_batch`, `score_event`, `explain`) calls `.transform()` /
`.decision_function()` only, never `.fit()` / `.fit_transform()`.
`ml_engine.preprocess_features` is imported for that ONE call site and must
never appear in a scoring code path — pinned by
`tests/test_streaming_scorer.py::test_invariant_b_no_fit_calls_in_scoring_path`,
which monkeypatches `fit`/`fit_transform` on both the scaler and the model
(plus `preprocess_features` itself) to raise, so a future refactor that
creates a *fresh* scaler object (which a naive "scaler.mean_ unchanged"
test would not catch — see the plan section 12 item 5) fails loudly.

Q2 note (feature alignment, docs/PHASE5_TICKET5_PLAN.md section 3): the
volumetric features (`duration_sec`, `packets`, `bytes`) are the SAME
quantities under the SAME microsecond-conversion semantics the frozen
`src/datasets/cic_ids_adapter.py` uses, so a warmup-fitted scorer measures
what the Phase 1-3 benchmark measured — the Phase 3 numbers remain a fair
reference point for this streaming path.

Q6 note (contamination): `streaming_contamination` defaults to 0.005
(`BACKEND_SETTINGS`), NOT `SETTINGS.ml.isolation_forest_contamination`
(0.08). The warmup slice is 100% benign by construction, so contamination
here is a stated FALSE-POSITIVE budget, not an anomaly-rate estimate — see
`backend/config.py`'s field docstring for the measured flag-rate table.
"""
from __future__ import annotations

import logging
import platform
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.exceptions import InconsistentVersionWarning

from backend.config import BACKEND_SETTINGS
from backend.replay_reader import ReplayFlow, ReplayFlowReader
from ml_engine import compute_anomaly_scores, preprocess_features, train_isolation_forest
from settings import SETTINGS

logger = logging.getLogger(__name__)

#: Bump only on a genuine, incompatible artifact-format change.
ARTIFACT_SCHEMA_VERSION = "1.0"
EXPLANATION_SCHEMA_VERSION = "1.0"

_ARTIFACT_REQUIRED_KEYS = {
    "artifact_schema_version",
    "model",
    "scaler",
    "feature_names",
    "baseline_degenerate",
    "warmup",
    "hyperparameters",
    "library_versions",
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class StreamingScorerError(RuntimeError):
    """Base class for StreamingScorer usage/artifact errors."""


class StreamingScorerNotFitted(StreamingScorerError):
    """Raised by score_batch/score_event/explain/baseline on an unfitted
    scorer. There is deliberately no implicit path from "unfitted" to
    "fitted on stream data" — call fit_from_warmup() (build time only) or
    load() (an existing artifact)."""


class StreamingScorerArtifactMissing(StreamingScorerError, FileNotFoundError):
    """Raised by load() when the artifact file does not exist. Deliberately
    a FileNotFoundError subclass too, so a caller catching either base
    still sees this. NEVER triggers an implicit fit — see load()'s
    docstring: no `fit_if_missing` parameter exists and none may be added."""


class StreamingScorerIncompatible(StreamingScorerError):
    """Raised by load() on artifact-schema, feature-name, or
    n_features_in_ mismatch. Hard failure, no fallback (docs/
    PHASE5_TICKET5_PLAN.md section 7) — a silently-wrong scorer is worse
    than a process that refuses to start."""


# ---------------------------------------------------------------------------
# ScoredFlow
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoredFlow:
    """One flow's volumetric verdict.

    Deliberately carries the scaled feature row (`z_scores`) so
    `explain()` costs no additional arithmetic: `(x - scaler.mean_) /
    scaler.scale_` is bit-identical to `scaler.transform(x)`'s output
    (verified over 191,033 real friday-morning rows,
    docs/PHASE5_TICKET5_PLAN.md section 4), which `score_batch` already
    computed to feed the model.
    """

    flow: ReplayFlow
    raw_score: float
    calibrated_score: float
    is_anomaly: bool
    z_scores: tuple[float, ...]  # aligned to StreamingScorer.feature_names


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _library_versions() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scikit-learn": sklearn.__version__,
        "joblib": joblib.__version__,
    }


# ---------------------------------------------------------------------------
# StreamingScorer
# ---------------------------------------------------------------------------


class StreamingScorer:
    """Fit-once, transform-only volumetric anomaly scorer for the Phase 5
    stream. See the module docstring for Invariant B.

    This is the NOVEL-THREAT channel (docs/DETECTION_STUDY.md): weak on
    the friday-morning Bot/C2 traffic actually present in the demo dataset
    (measured ROC AUC 0.600 — Bot beacons are smaller than benign traffic,
    the opposite direction an outlier detector over volume looks), but
    honestly so, and it needs no labelled attack data to operate. The
    KNOWN-THREAT channel is `backend.supervised_detector.SupervisedFlowDetector`.
    """

    def __init__(self, feature_names: Optional[list[str]] = None) -> None:
        """`feature_names` defaults to `SETTINGS.ml.default_features`
        (optional-override convention). Constructs an UNFITTED scorer:
        every scoring/explanation method raises `StreamingScorerNotFitted`
        until `fit_from_warmup()` or `load()` has run.
        """
        self.feature_names: list[str] = (
            list(feature_names) if feature_names is not None else list(SETTINGS.ml.default_features)
        )
        self._scaler = None
        self._model = None
        self._baseline_degenerate: Optional[tuple[bool, ...]] = None
        self._warmup_meta: Optional[dict] = None
        self._hyperparameters: Optional[dict] = None
        self._library_versions: Optional[dict] = None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def is_fitted(self) -> bool:
        return self._scaler is not None and self._model is not None

    def _require_fitted(self) -> None:
        if not self.is_fitted:
            raise StreamingScorerNotFitted(
                "StreamingScorer is not fitted. Call fit_from_warmup() "
                "(build time) or load() (an existing artifact) first — "
                "there is no automatic fit-from-stream-data path by design "
                "(Invariant B)."
            )

    @property
    def baseline(self) -> dict:
        """Provenance block: feature names, warmup-fitted mean_/scale_/var_,
        degeneracy flags, warmup metadata, hyperparameters, and library
        versions. Embedded in every `explain()` payload's `baseline` key
        (a subset) and intended for Ticket #16's `/api/stats`."""
        self._require_fitted()
        return {
            "feature_names": list(self.feature_names),
            "mean_": [float(v) for v in self._scaler.mean_],
            "scale_": [float(v) for v in self._scaler.scale_],
            "var_": [float(v) for v in self._scaler.var_],
            "baseline_degenerate": list(self._baseline_degenerate),
            "warmup": dict(self._warmup_meta),
            "hyperparameters": dict(self._hyperparameters),
            "library_versions": dict(self._library_versions),
        }

    # ------------------------------------------------------------------
    # Fitting (build time only)
    # ------------------------------------------------------------------

    def fit_from_warmup(
        self,
        flows: Optional[Sequence[ReplayFlow]] = None,
        day: Optional[str] = None,
        row_limit: Optional[int] = None,
        contamination: Optional[float] = None,
        n_estimators: Optional[int] = None,
        random_state: Optional[int] = None,
    ) -> "StreamingScorer":
        """Fit scaler + IsolationForest ONCE on benign warmup traffic.

        `flows` may be supplied directly (tests); otherwise the warmup day
        is read via `ReplayFlowReader`. `day` defaults to
        `BACKEND_SETTINGS.warmup_dataset_day` ("monday"), `row_limit` to
        `BACKEND_SETTINGS.warmup_row_limit` (None = all ~529,918 rows —
        docs/PHASE5_TICKET5_PLAN.md Q1: subsampling saves at most 14% of an
        already-cheap 4.72s build and was measured to make the `packets`
        baseline sigma wrong by up to 3.1x), and `contamination` to
        `BACKEND_SETTINGS.streaming_contamination` (0.005 — a stated
        FALSE-POSITIVE budget; the warmup set has zero attacks by
        construction).

        Delegates the fit to `ml_engine.preprocess_features()` (fit) and
        `ml_engine.train_isolation_forest()` — this is the ONE call site
        in the whole process where `fit_transform` is correct (Invariant
        B, module docstring).

        Raises `StreamingScorerError` if fewer than
        `BACKEND_SETTINGS.warmup_min_rows` rows survive (zero-variance
        baseline risk), or if any warmup row has `is_attack=True` (the
        false-positive-budget semantics of `contamination` only hold on
        known-benign data).
        """
        resolved_day = day if day is not None else BACKEND_SETTINGS.warmup_dataset_day
        resolved_row_limit = row_limit if row_limit is not None else BACKEND_SETTINGS.warmup_row_limit
        resolved_contamination = (
            contamination if contamination is not None else BACKEND_SETTINGS.streaming_contamination
        )
        resolved_n_estimators = (
            n_estimators if n_estimators is not None else BACKEND_SETTINGS.streaming_n_estimators
        )
        resolved_random_state = (
            random_state if random_state is not None else SETTINGS.ml.isolation_forest_random_state
        )

        source_file = None
        if flows is None:
            reader = ReplayFlowReader()
            flow_list = list(reader.iter_flows(day=resolved_day, limit=resolved_row_limit))
            if reader.last_read_stats is not None:
                source_file = reader.last_read_stats.source_file
        else:
            flow_list = list(flows)

        n_rows = len(flow_list)
        if n_rows < BACKEND_SETTINGS.warmup_min_rows:
            raise StreamingScorerError(
                f"Warmup slice for day={resolved_day!r} has {n_rows} row(s), "
                f"below BACKEND_SETTINGS.warmup_min_rows="
                f"{BACKEND_SETTINGS.warmup_min_rows}. A too-small warmup "
                "risks a zero-variance baseline (docs/PHASE5_TICKET5_PLAN.md "
                "Q3: head(1) yields 3 zero-variance columns, head(2) yields "
                "2) which would make explain()'s sigma numbers meaningless."
            )

        attack_rows = sum(1 for f in flow_list if f.is_attack)
        if attack_rows:
            raise StreamingScorerError(
                f"Warmup day {resolved_day!r} contains {attack_rows} attack "
                f"row(s) of {n_rows}. fit_from_warmup() requires an "
                "all-benign warmup slice — BACKEND_SETTINGS."
                "streaming_contamination is a stated false-positive budget "
                "on KNOWN-BENIGN data, not an anomaly-rate estimate; fitting "
                "on traffic that contains real attacks would silently "
                "invalidate that semantics."
            )

        df = pd.DataFrame(
            {name: [getattr(f, name) for f in flow_list] for name in self.feature_names},
            columns=self.feature_names,
        )
        # THE one call site where fit_transform is correct (Invariant B).
        X_scaled, scaler = preprocess_features(df, features=self.feature_names)
        model = train_isolation_forest(
            X_scaled,
            n_estimators=resolved_n_estimators,
            contamination=resolved_contamination,
            random_state=resolved_random_state,
        )

        ts_values = [f.ts for f in flow_list]
        self._scaler = scaler
        self._model = model
        self._baseline_degenerate = tuple(bool(v) for v in (scaler.var_ == 0))
        self._warmup_meta = {
            "day": resolved_day,
            "source_file": source_file,
            "source_dataset": flow_list[0].source_dataset,
            "rows_seen": n_rows,
            "rows_used": n_rows,
            "rows_skipped": 0,
            "attack_rows_in_warmup": attack_rows,
            "ts_min": min(ts_values).isoformat(),
            "ts_max": max(ts_values).isoformat(),
            "fitted_at": datetime.now(timezone.utc).isoformat(),
        }
        self._hyperparameters = {
            "n_estimators": int(model.n_estimators),
            "contamination": float(resolved_contamination),
            "random_state": resolved_random_state,
            "max_samples_": int(model.max_samples_),
        }
        self._library_versions = _library_versions()
        return self

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: Optional[Path] = None) -> Path:
        """joblib-dump to `BACKEND_SETTINGS.model_artifact_path_resolved`
        (K2 — never the CWD-relative `model_artifact_path`) unless an
        explicit `path` is given. Creates parent directories. Returns the
        absolute path written."""
        self._require_fitted()
        target = Path(path) if path is not None else BACKEND_SETTINGS.model_artifact_path_resolved
        target.parent.mkdir(parents=True, exist_ok=True)
        artifact = {
            "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
            "model": self._model,
            "scaler": self._scaler,
            "feature_names": list(self.feature_names),
            "baseline_degenerate": list(self._baseline_degenerate),
            "warmup": dict(self._warmup_meta),
            "hyperparameters": dict(self._hyperparameters),
            "library_versions": dict(self._library_versions),
        }
        joblib.dump(artifact, target)
        return target.resolve()

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "StreamingScorer":
        """Load a persisted scorer. Defaults to
        `BACKEND_SETTINGS.model_artifact_path_resolved` (K2).

        NEVER refits. There is deliberately no `fit_if_missing` parameter
        and none may be added: an implicit refit on stream data is
        precisely the Invariant B failure this class exists to prevent.

        Raises `StreamingScorerArtifactMissing` (naming the absolute path
        and the `python -m backend.warmup` build command) if the artifact
        file is absent. Raises `StreamingScorerIncompatible` on
        artifact-schema, feature-name, or `n_features_in_` mismatch, or if
        `joblib.load` itself raises. Logs (does not raise) on scikit-learn
        version drift (`InconsistentVersionWarning`) — Invariant F wants
        the demo to work offline on the demo machine; the mismatch is also
        surfaced in `library_versions` on every subsequent `baseline` /
        `explain()` call.
        """
        target = Path(path) if path is not None else BACKEND_SETTINGS.model_artifact_path_resolved
        if not target.exists():
            raise StreamingScorerArtifactMissing(
                f"No StreamingScorer artifact at {target}. Build it with: "
                "PYTHONPATH=src venv/bin/python -m backend.warmup"
            )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", category=InconsistentVersionWarning)
            try:
                artifact = joblib.load(target)
            except Exception as exc:  # noqa: BLE001 — any unpickle failure is fatal here
                raise StreamingScorerIncompatible(
                    f"Failed to load StreamingScorer artifact at {target}: {exc}"
                ) from exc

        for w in caught:
            if issubclass(w.category, InconsistentVersionWarning):
                logger.warning(
                    "StreamingScorer artifact at %s was pickled under a "
                    "different scikit-learn version (%s); scores may be "
                    "affected. Recorded library_versions: %s",
                    target,
                    w.message,
                    artifact.get("library_versions") if isinstance(artifact, dict) else None,
                )

        if not isinstance(artifact, dict):
            raise StreamingScorerIncompatible(
                f"Artifact at {target} is not a dict (got {type(artifact).__name__})."
            )
        missing_keys = _ARTIFACT_REQUIRED_KEYS - artifact.keys()
        if missing_keys:
            raise StreamingScorerIncompatible(
                f"Artifact at {target} is missing key(s) {sorted(missing_keys)}."
            )

        if artifact["artifact_schema_version"] != ARTIFACT_SCHEMA_VERSION:
            raise StreamingScorerIncompatible(
                f"Artifact schema version {artifact['artifact_schema_version']!r} "
                f"at {target} != expected {ARTIFACT_SCHEMA_VERSION!r}. Rebuild "
                "with: PYTHONPATH=src venv/bin/python -m backend.warmup"
            )

        expected_features = list(SETTINGS.ml.default_features)
        if list(artifact["feature_names"]) != expected_features:
            raise StreamingScorerIncompatible(
                f"Artifact feature_names {artifact['feature_names']!r} at "
                f"{target} != SETTINGS.ml.default_features {expected_features!r}. "
                "A default_features edit made this artifact stale; rebuild "
                "with: PYTHONPATH=src venv/bin/python -m backend.warmup"
            )

        scaler = artifact["scaler"]
        model = artifact["model"]
        n_features = len(artifact["feature_names"])
        scaler_n = getattr(scaler, "n_features_in_", None)
        model_n = getattr(model, "n_features_in_", None)
        if scaler_n != n_features or model_n != n_features:
            raise StreamingScorerIncompatible(
                f"Artifact at {target} has n_features_in_ mismatch: "
                f"scaler={scaler_n}, model={model_n}, expected {n_features}."
            )

        scorer = cls(feature_names=artifact["feature_names"])
        scorer._scaler = scaler
        scorer._model = model
        scorer._baseline_degenerate = tuple(artifact["baseline_degenerate"])
        scorer._warmup_meta = dict(artifact["warmup"])
        scorer._hyperparameters = dict(artifact["hyperparameters"])
        scorer._library_versions = dict(artifact["library_versions"])
        return scorer

    # ------------------------------------------------------------------
    # Scoring (hot path)
    # ------------------------------------------------------------------

    def _to_frame(self, flows: Sequence[ReplayFlow]) -> pd.DataFrame:
        """`self.feature_names`-ordered DataFrame — required so the
        fitted scaler's `feature_names_in_` check passes cleanly (no
        `UserWarning`, and a `ValueError` on accidental reordering, both
        free consequences of scaler.transform(DataFrame) — docs/
        PHASE5_TICKET5_PLAN.md section 3)."""
        return pd.DataFrame(
            {name: [getattr(f, name) for f in flows] for name in self.feature_names},
            columns=self.feature_names,
        )

    def score_batch(self, flows: Sequence[ReplayFlow]) -> list[ScoredFlow]:
        """Score a micro-batch. `transform()` only, NEVER `fit_transform()`
        (Invariant B). THE hot path — Ticket #6 emits batches of <=500
        (P5-12). `ml_engine.compute_anomaly_scores` itself measures
        2.73-5.42ms per 500-flow batch (docs/PHASE5_TICKET5_PLAN.md
        section 3, 0.0055-0.0108 ms/event); this method's own end-to-end
        measurement (DataFrame build + transform + compute_anomaly_scores
        + `ScoredFlow` construction) on a real 500-flow friday-morning
        batch, warmup-fitted on the full Monday day: ~6.3ms/batch
        (~0.0126 ms/event) — still ~59x under P5-10's 0.747 ms/event
        budget. Empty input returns `[]`."""
        self._require_fitted()
        if not flows:
            return []
        flows = list(flows)
        df = self._to_frame(flows)
        X_scaled = self._scaler.transform(df)
        scored_df = compute_anomaly_scores(self._model, X_scaled, df)

        # .to_numpy() once, then iterate over plain arrays — repeated
        # .iloc[i] in a Python loop is O(n) per call in pandas and was
        # measured to dominate this method's cost far more than the
        # sklearn calls above it.
        raw_scores = scored_df["raw_score"].to_numpy()
        calibrated_scores = scored_df["calibrated_score"].to_numpy()
        is_anomaly_arr = scored_df["is_anomaly"].to_numpy()

        results: list[ScoredFlow] = [
            ScoredFlow(
                flow=flow,
                raw_score=float(raw_scores[i]),
                calibrated_score=float(calibrated_scores[i]),
                is_anomaly=bool(is_anomaly_arr[i]),
                z_scores=tuple(float(v) for v in X_scaled[i]),
            )
            for i, flow in enumerate(flows)
        ]
        return results

    def score_event(self, flow: ReplayFlow) -> ScoredFlow:
        """Score one flow. CONVENIENCE/TEST API ONLY — measured ~1.99ms
        per event, ~366x worse per event than `score_batch(500)`. Calling
        this in a loop is a performance bug; Ticket #7 must call
        `score_batch()`."""
        return self.score_batch([flow])[0]

    # ------------------------------------------------------------------
    # Explanation
    # ------------------------------------------------------------------

    def explain(self, scored: ScoredFlow) -> dict:
        """Per-feature deviation vs. the warmup baseline, as the JSON dict
        persisted to `alerts.explanation` (`backend/models.py`).

        Deliberately NOT SHAP: the fitted `StandardScaler` already holds
        `mean_`/`scale_`, so `z = (x - mean_) / scale_` is bit-identical
        to `scaler.transform`'s output (verified over 191,033 real rows)
        — already computed by `score_batch` and cached on
        `ScoredFlow.z_scores`, so this costs no arithmetic at all.

        `features` is sorted by `|z|` descending (degenerate-baseline
        features sort last, since they carry no z magnitude) and always
        includes every feature — never truncated. A feature whose warmup
        variance was zero reports `z: None`, `degenerate_baseline: True`,
        and is described in raw units: sklearn substitutes `scale_=1.0`
        for a zero scale, so a naive z there would be a raw-unit number
        wearing a sigma label — a fabricated statistic. `top_feature`
        prefers a non-degenerate feature whenever one exists. Every value
        is a plain JSON scalar (`float`/`bool`/`str`/`None`), never a
        numpy type — psycopg cannot serialise numpy scalars into JSONB.

        Raises `TypeError` if `scored` is not a `ScoredFlow` — most
        commonly, passing a bare `ReplayFlow` by mistake (an easy
        confusion for a caller juggling both, e.g. Ticket #7). Without
        this guard the failure surfaces deep inside the method as an
        opaque `AttributeError: 'ReplayFlow' object has no attribute
        'flow'`, which leaks this method's internals instead of naming
        the actual mistake.
        """
        if not isinstance(scored, ScoredFlow):
            raise TypeError(
                f"StreamingScorer.explain() expects a ScoredFlow, got "
                f"{type(scored).__name__}. If you have a ReplayFlow, "
                "obtain a ScoredFlow first via score_batch()/score_event()."
            )
        self._require_fitted()
        features = []
        for i, name in enumerate(self.feature_names):
            value = float(getattr(scored.flow, name))
            mean = float(self._scaler.mean_[i])
            std = float(self._scaler.scale_[i])
            degenerate = bool(self._baseline_degenerate[i])
            if degenerate:
                z: Optional[float] = None
            else:
                z = float(scored.z_scores[i])
            if value > mean:
                direction = "above"
            elif value < mean:
                direction = "below"
            else:
                direction = "at"
            features.append(
                {
                    "name": name,
                    "value": value,
                    "baseline_mean": mean,
                    "baseline_std": std,
                    "z": z,
                    "direction": direction,
                    "degenerate_baseline": degenerate,
                }
            )

        sorted_features = sorted(
            features,
            key=lambda f: (
                1 if f["degenerate_baseline"] else 0,
                -(abs(f["z"]) if f["z"] is not None else 0.0),
            ),
        )
        non_degenerate = [f for f in sorted_features if not f["degenerate_baseline"]]
        top = non_degenerate[0] if non_degenerate else sorted_features[0]

        if top["degenerate_baseline"]:
            summary = (
                f"{top['name']} {top['value']:,.0f} vs constant warmup "
                f"baseline {top['baseline_mean']:,.0f} (no variance in "
                "warmup — sigma undefined)"
            )
        else:
            summary = f"{top['name']} {abs(top['z']):.1f} sigma {top['direction']} baseline"

        return {
            "schema_version": EXPLANATION_SCHEMA_VERSION,
            "method": "zscore_vs_warmup_baseline",
            "score": {
                "raw": float(scored.raw_score),
                "calibrated": float(scored.calibrated_score),
                "is_anomaly": bool(scored.is_anomaly),
                "threshold": 0.0,
                "detector": "isolation_forest",
            },
            "features": sorted_features,
            "top_feature": top["name"],
            "summary": summary,
            "baseline": {
                "warmup_day": self._warmup_meta["day"],
                "warmup_rows": self._warmup_meta["rows_used"],
                "artifact_schema_version": ARTIFACT_SCHEMA_VERSION,
                "contamination": self._hyperparameters["contamination"],
            },
        }


# ---------------------------------------------------------------------------
# Tripwire fusion (Invariant C — see docs/PHASE5_TICKET5_PLAN.md section 5)
# ---------------------------------------------------------------------------


def fuse_tripwire_confidence(
    volume_fired: np.ndarray, tripwire_fired: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """OR-fuse the volumetric and tripwire signals and escalate confidence.

    Mirrors `core.pipeline.run_analysis()`'s Phase 2 fusion block
    (`src/core/pipeline.py`, lines ~134-154) by importing the SAME
    `SETTINGS.deception` confidence constants; only the `np.select`
    combinator itself is restated here, because that block is inline in
    `run_analysis` (not an importable function — `dir(core.pipeline)`
    shows exactly one module-level callable) and extracting it would edit
    `src/` (Invariant A). This is a duplicated *expression*, not a
    duplicate *definition* in the CI-checked sense (no name collision, and
    the CI walk only globs `src/*.py` — see docs/PHASE5_TICKET5_PLAN.md
    section 12 item 2 for why "CI already protects this" would be a wrong
    argument to make here); it is pinned by
    `tests/test_streaming_scorer.py::test_fusion_matches_pipeline` instead.

    NOT a `StreamingScorer` method deliberately: `StreamingScorer`'s
    contract is "the fitted volumetric model, applied without refitting."
    Fusion is an ingest-policy concern (Ticket #7's job per Invariant C —
    this ticket must not reimplement tripwire detection or confidence
    escalation beyond restating this one combinator), shipped here as a
    free function so Ticket #7 imports it rather than hand-rolling a
    second copy.

    Parameters
    ----------
    volume_fired, tripwire_fired:
        Same-shape boolean arrays — the volumetric detector's `is_anomaly`
        flags and the tripwire detector's fired flags, aligned per-row.

    Returns
    -------
    (is_anomaly, confidence):
        `is_anomaly` is `volume_fired | tripwire_fired`. `confidence` is
        `SETTINGS.deception.confidence_both` where both fired,
        `confidence_tripwire_only` where only tripwire fired,
        `confidence_volume_only` where only volume fired, else
        `confidence_none` — read live from `SETTINGS.deception` so this
        function can never drift from the constants `core.pipeline` uses.
    """
    volume_fired = np.asarray(volume_fired, dtype=bool)
    tripwire_fired = np.asarray(tripwire_fired, dtype=bool)
    if volume_fired.shape != tripwire_fired.shape:
        raise ValueError(
            f"volume_fired shape {volume_fired.shape} != tripwire_fired "
            f"shape {tripwire_fired.shape}"
        )

    deception_cfg = SETTINGS.deception
    is_anomaly = volume_fired | tripwire_fired
    confidence = np.select(
        [
            volume_fired & tripwire_fired,
            tripwire_fired,
            volume_fired,
        ],
        [
            deception_cfg.confidence_both,
            deception_cfg.confidence_tripwire_only,
            deception_cfg.confidence_volume_only,
        ],
        default=deception_cfg.confidence_none,
    )
    return is_anomaly, confidence
