# Phase 5 — Ticket #5 Implementation Plan: `StreamingScorer`

**Status:** planning artifact. No production code written by this pass.
**Method:** every number below was measured on this machine with
`PYTHONPATH=src venv/bin/python` against the real
`datasets/TrafficLabelling ` capture, not taken from the plan documents.
Where a plan document is wrong, §12 says so explicitly.

**Environment measured on:** Python 3.13, scikit-learn 1.8.0, numpy 2.4.6,
joblib 1.5.3, pandas 3.x. Baseline suite before this ticket:
**357 passed, 10 skipped**; `git status --short src/` **empty** (Invariant A
holding).

---

## 1. Objective

Provide a **fit-once, transform-only** anomaly scorer for the Phase 5
streaming path, so that the live console's anomaly scores mean the same
thing at minute 12 of the demo as they did at minute 0.

### The invariant this ticket exists to protect (Invariant B)

`ml_engine.preprocess_features()` ends with `scaler.fit_transform(X)` — a
**new** `StandardScaler` on every call. Demonstrated cost of naively reusing
it per micro-batch, measured on a real 500-flow friday-morning batch against
the full-Monday warmup baseline:

| | `duration_sec` | `packets` | `bytes` |
|---|---|---|---|
| warmup `mean_` (529,918 rows) | 10.389 | 10.390 | 532.42 |
| per-batch `mean_` (500 rows) | 6.149 | 12.826 | 1576.30 |
| **`scale_` ratio batch/warmup** | **0.737** | **0.055** | **0.812** |

The `packets` standard deviation collapses to **5.5%** of the true baseline
on that one batch. Every z-score, every calibrated score, and every
`explain()` sentence computed from it is wrong — and nothing raises, nothing
logs, nothing looks broken. That is the failure mode this class exists to
make impossible.

**Required surface** (PLAN_MASTER "The one real engine change"):
`fit_from_warmup()`, `save()`, `load()`, `score_batch()`, `score_event()`,
`explain()`.

---

## 2. Q1 — How much warmup data?

**Recommendation: all 529,918 Monday rows. `warmup_row_limit` defaults to
`None`. Total build-time cost 4.72 s.**

### Measurement A — fit time barely depends on `n`

```
n         scaler.fit   IsolationForest.fit   score 191k friday rows
50,000      0.002 s          0.131 s               0.231 s
200,000     0.005 s          0.341 s               0.239 s
529,918     0.015 s          0.808 s               0.247 s
```

The reason matters for the implementer: sklearn's `max_samples="auto"` is
`min(256, n_samples)`. Verified — `clf.max_samples_ == 256` at both n=50,000
and n=529,918. **Each of the 100 trees is built from 256 rows regardless of
warmup size.** The only O(n) work in `fit` is computing `offset_`, the
contamination threshold, which scores every training row. Hence 0.81 s at
530k.

### Measurement B — end-to-end build cost is dominated by I/O, not fitting

```
read+sort (ReplayFlowReader, monday)   3.52 s
featurize (list -> ndarray)            0.38 s
StandardScaler.fit                     0.02 s
IsolationForest.fit                    0.79 s
joblib.dump                            0.01 s
------------------------------------------------
TOTAL                                  4.72 s     artifact 823 KiB
```

Subsampling saves **at most 0.68 s of 4.72 s (14%)** — and saves *nothing*
on the read, because `ReplayFlowReader.iter_flows(limit=N)` reads and sorts
the entire file before slicing (its own documented "limit semantics"). A
10-minute fit was never a risk here; 4.72 s is a non-issue for a build step.

### Measurement C — subsampling actively corrupts `explain()`

`explain()` is derived from `scaler.mean_` / `scaler.scale_`, so warmup size
changes the σ numbers printed in the operator console:

```
warmup slice        mean_                              scale_
head 50,000    [13.14,   47.07,   853.56]     [ 31.36, 2772.17, 17662.89]
random 50,000  [10.43,    6.29,   506.26]     [ 28.78,   84.34,  3169.51]
head 200,000   [11.14,   17.12,   610.91]     [ 29.08, 1451.05,  9588.87]
random 200,000 [10.39,   10.32,   534.03]     [ 28.72,  879.70,  6037.82]
FULL 529,918   [10.39,   10.39,   532.42]     [ 28.75,  892.41,  6228.64]
```

Chronologically-first 50k covers only the start of the capture day: its
`packets` σ is **3.1× too large** and its mean **4.5× too large**. Every
"N σ above baseline" the console prints would be off by ~3×. Random-50k
errs the other way — `packets` σ **10.6× too small**. Only the full day is
stable (random-200k is close, but has no cost advantage worth the risk).

The *ranking* is robust either way (friday-morning `raw_score` correlation
with the full-day model: 0.995 for head-50k, 0.998 for random-200k), so this
is specifically an explanation-honesty argument, not a detection-quality
argument. Since `explain()` is the deliverable's credibility story, take the
full day.

### Guard rails to implement

- `BACKEND_SETTINGS.warmup_row_limit: int | None = None` — optional override
  so tests and CI can cap it, per CLAUDE.md's optional-override convention.
- `BACKEND_SETTINGS.warmup_min_rows: int = 1000` — hard floor.
  `fit_from_warmup()` raises `StreamingScorerError` below it. Measured
  justification: `head(1)` yields 3 zero-variance columns, `head(2)` yields
  2; `head(10)` onward all three columns have non-zero variance and
  `head(1000)`'s `var_` is already within the right order of magnitude of
  the full day's. See Q3 for why zero variance is dangerous.
- `fit_from_warmup()` must assert the warmup slice is genuinely benign:
  verified `monday` has **0 attack rows of 529,918 (0.000%)**, `rows_seen ==
  rows_emitted == 529,918`, zero skipped rows of any kind. Record
  `attack_rows_in_warmup` in the artifact metadata and raise if it is not 0.

---

## 3. Q2 — Feature alignment and how to reuse `ml_engine` without duplicating it

### Alignment: exact, confirmed

```
SETTINGS.ml.default_features == ['duration_sec', 'packets', 'bytes']
ReplayFlow fields             ==  duration_sec,   packets,   bytes
```

Names match exactly, and so do the **semantics**. `backend/replay_reader.py`
derives them from `Flow Duration / 1_000_000`, `Total Fwd Packets`, and
`Total Length of Fwd Packets`; the frozen `src/datasets/cic_ids_adapter.py`
(lines 255–256, 278–280) uses the identical three source columns with the
identical microsecond conversion. So a warmup-fitted scorer is measuring the
same quantities the Phase 1–3 benchmark measured. Record this in the
docstring — it is the reason the Phase 3 numbers remain a fair reference
point for the streaming path.

### The mapping is reusable; the fitting is not

`preprocess_features()` does two separable things:

1. **column mapping / defaulting** — `payload_size → bytes`, and synthesising
   `duration_sec = 1.0` / `packets = 1` when absent.
2. **fitting** — `StandardScaler().fit_transform(X)`.

For `ReplayFlow` input, **(1) is a no-op**: all three columns are always
present under exactly the right names. There is no mapping logic worth
extracting.

**The correct split — and it needs no `src/` change at all:**

- **Warmup fit** — call `preprocess_features(warmup_df, features=self.feature_names)`
  **verbatim**. Fitting a fresh scaler is *exactly right* here; this is the
  one call site where `fit_transform` is correct. Genuine reuse, zero
  duplication.
- **Stream path** — call `self._scaler.transform(df)` on the persisted
  scaler. Never `preprocess_features`.

That single sentence is the whole of Invariant B: **`preprocess_features` is
called exactly once in the process's lifetime, inside `fit_from_warmup`, and
is not imported into any streaming code path.**

### Feature-name safety comes free — use it

`preprocess_features` passes a *DataFrame* to `fit_transform` (it does
`df[features].astype(float).copy()`), so the fitted scaler carries
`feature_names_in_ == ['duration_sec' 'packets' 'bytes']`. Verified
consequences:

| call | result |
|---|---|
| `scaler.transform(DataFrame, correct order)` | clean, no warning |
| `scaler.transform(ndarray)` | `UserWarning: X does not have valid feature names…` **on every call** |
| `scaler.transform(DataFrame, columns reordered)` | `ValueError: The feature names should match those that were passed during fit` |

So: **`score_batch` must build a `pandas.DataFrame` with columns in
`self.feature_names` order**, not an ndarray. This buys free, sklearn-native
protection against feature reordering, and avoids a `UserWarning` fired ten
times a second into the demo logs. Cost measured at 500 rows: DataFrame
construction 0.135 ms + `transform` 0.191 ms, against a 2.42 ms
`decision_function` — 13% of an already-negligible path (see Q7).

### Conversion path (`list[ReplayFlow]` → matrix)

```python
def _to_frame(self, flows: Sequence[ReplayFlow]) -> pd.DataFrame:
    return pd.DataFrame(
        {name: [getattr(f, name) for f in flows] for name in self.feature_names},
        columns=self.feature_names,
    )
```

Driving the comprehension off `self.feature_names` (which came from
`SETTINGS.ml.default_features` at fit time and is persisted in the artifact)
means the column list is declared once, not hardcoded — and a future
`default_features` change is caught by the artifact-compatibility check in
Q6 rather than silently producing garbage.

### Scoring: call `ml_engine.compute_anomaly_scores` — do not re-derive it

`compute_anomaly_scores(clf, X_scaled, df)` adds `anomaly_score`,
`raw_score`, `is_anomaly`, and `calibrated_score = 1/(1+exp(5·raw))`.

There is a tempting optimisation — call `clf.decision_function` once and
derive `is_anomaly = raw < 0` — because `predict()` internally recomputes
`decision_function`, so calling both doubles the dominant cost. Verified the
identity holds exactly: `(raw < 0) == (predict == -1)` on all 191,033
friday-morning rows, with zero exact-zero ties. Measured saving on a
500-flow batch: **5.42 ms → 2.73 ms**.

**Recommendation: take the reuse, not the optimisation. Call
`compute_anomaly_scores`.** Reasons:

- Both numbers are far under budget. P5-10's budget is 0.747 ms/event at
  friday-morning's peak density; `compute_anomaly_scores` costs
  **0.0108 ms/event** at batch 500 — **69× under budget**. The optimised
  path is 142× under. Neither is a bottleneck; the DB insert and WS
  broadcast will dominate.
- The sigmoid constant `5.0` is a bare literal inside `ml_engine`, not a
  `SETTINGS` field. RECON §C says to "mirror exactly" — better to not
  mirror at all. Re-deriving it in `backend/` creates a second copy of a
  magic number that CLAUDE.md forbids and that no test would catch drifting.
- Verified parity: the hand-rolled path and `compute_anomaly_scores` agree
  (`allclose` on `raw_score` and `calibrated_score`, exact on
  `anomaly_score`), so the optimisation remains available later if profiling
  ever justifies it. Record the measurement in the docstring so a future
  reader does not have to re-derive it.

---

## 4. Q3 — `explain()` design

### It is free, and it is exactly the scaler output

Verified: `(X - scaler.mean_) / scaler.scale_` is **identical** to
`scaler.transform(X)` (`np.allclose == True` over 191,033 rows). Since
`score_batch` already computes `X_scaled` to feed the model, `explain()`
requires **zero additional arithmetic** — it reads the row of `X_scaled` the
scorer already has. Cache `X_scaled` on the batch result; do not recompute.

Sanity check on real data — the five most anomalous friday-morning flows
under the full-Monday baseline:

```
raw=-0.1126  packets 207,964 vs mean 10.39 = +233.0σ | bytes 1,235,152 vs 532 = +198.2σ | duration 120.0s = +3.8σ
raw=-0.1188  packets  77,620 vs mean 10.39 =  +87.0σ | bytes   459,634 vs 532 =  +73.7σ | duration 110.3s = +3.5σ
raw=-0.1143  bytes   223,576 vs mean   532 =  +35.8σ | duration 107.2s = +3.4σ | packets 320 = +0.3σ
```

PLAN_MASTER's illustrative *"bytes 47σ above baseline"* is well within the
real range (max observed |z| = 233.0, all finite).

### Output structure (becomes `alerts.explanation` JSONB, `backend/models.py:287`)

```json
{
  "schema_version": "1.0",
  "method": "zscore_vs_warmup_baseline",
  "score": {
    "raw": -0.1126,
    "calibrated": 0.6376,
    "is_anomaly": true,
    "threshold": 0.0,
    "detector": "isolation_forest"
  },
  "features": [
    {"name": "packets",      "value": 207964.0, "baseline_mean": 10.39,
     "baseline_std": 892.41, "z": 233.02, "direction": "above",
     "degenerate_baseline": false},
    {"name": "bytes",        "value": 1235152.0, "baseline_mean": 532.42,
     "baseline_std": 6228.64, "z": 198.24, "direction": "above",
     "degenerate_baseline": false},
    {"name": "duration_sec", "value": 120.0, "baseline_mean": 10.39,
     "baseline_std": 28.75, "z": 3.81, "direction": "above",
     "degenerate_baseline": false}
  ],
  "top_feature": "packets",
  "summary": "packets 233.0 sigma above baseline",
  "baseline": {
    "warmup_day": "monday",
    "warmup_rows": 529918,
    "artifact_schema_version": "1.0",
    "contamination": 0.005
  }
}
```

Design notes:

- `features` is **sorted by `|z|` descending** and includes **all three**
  features, never truncated. There are only three; showing the ones that did
  *not* fire is part of an honest explanation.
- `direction` is `"above"` / `"below"` / `"at"`, derived from the sign of
  `z`. A negative z is a real signal (an unusually *small* flow) and must
  not be reported as "above".
- `summary` is a single sentence for the alert card, formatted from
  `top_feature`. Formatting only — the numbers are not recomputed.
- `baseline` block travels with every explanation so an operator (and a
  judge) can see which model produced it. This is what makes the
  explanation traceable to an actual fitted scaler rather than a plausible
  number, which is the acceptance bar in RECON §J.
- Every value is a plain JSON scalar (`float(...)` / `bool(...)`), never a
  `numpy.float64` — psycopg will not serialise numpy scalars into JSONB.

### Zero-variance features — verified, and the risk is NOT a division error

Measured behaviour of `StandardScaler` on a column with zero variance
(`Z = [[1,5,5],[2,5,7],[3,5,9]]`):

```
var_   = [0.667, 0.0,  2.667]
scale_ = [0.816, 1.0,  1.633]     <- sklearn substitutes 1.0
transform([[1, 99, 5]]) = [[-1.225, 94.0, -1.225]]
```

`sklearn.preprocessing._data._handle_zeros_in_scale` replaces a zero scale
with `1.0`. **There is no `ZeroDivisionError` and no `inf`.** The real
danger is worse and quieter: the "z" for that feature is then in **raw
units**, while the UI labels it σ. A flow with 99 packets against a constant
baseline of 5 would render as *"packets 94σ above baseline"* — a fabricated
statistic, precisely the class of thing the directive forbids.

Measured against real data:

```
full Monday (529,918):  var_ = [826.67, 795,899.09, 38,795,912.8]   zero-variance columns: 0
head(1):     3 zero-variance columns
head(2):     2 zero-variance columns
head(10):    0
head(100):   0
head(1000):  0
```

So the production path is safe, but the degenerate case is trivially
reachable from a test fixture or a mis-set `warmup_row_limit`.

**Required handling:**

1. `fit_from_warmup()` computes `baseline_degenerate = (scaler.var_ == 0)`
   and **persists it in the artifact** (do not recompute at score time —
   `var_` could be reconstructed differently by a future sklearn).
2. `fit_from_warmup()` raises `StreamingScorerError` if
   `n_rows < warmup_min_rows` (1000), which makes the degenerate case
   unreachable in practice.
3. If a feature is nevertheless degenerate, `explain()` emits
   `"z": null`, `"degenerate_baseline": true`, `"direction"` still derived
   from `value` vs `baseline_mean`, and the summary reads
   *"packets 207,964 vs constant warmup baseline 10 (no variance in warmup —
   sigma undefined)"*. **Never print a σ number derived from a substituted
   `scale_` of 1.0.**
4. `top_feature` selection skips degenerate features when any
   non-degenerate feature has a defined `z`.

---

## 5. Q4 — Tripwire fusion (Invariant C)

### `core.pipeline.run_analysis()` cannot be called from the stream path

Verified by source inspection. `run_analysis` calls, in order:
`generate_mock_network_data()` (fabricates synthetic nodes/edges),
`preprocess_features(edges_df)` (**`fit_transform` — a direct Invariant B
violation on every batch**), `train_isolation_forest(...)` (**refits the
model on the batch**), then `compute_cascading_impact_full()`. Calling it per
micro-batch would reintroduce exactly the failure this ticket exists to
prevent, plus 4.5 ms of Monte Carlo per call.

### The fusion is not extractable, because it is not a function

The fusion block lives inline at `src/core/pipeline.py:134–154`.
`dir(core.pipeline)` shows exactly one module-level callable: `run_analysis`.
There is **nothing importable to call**. Extracting it would mean editing
`src/core/pipeline.py` — forbidden by Invariant A and by RECON §D.

### What *is* importable, and must be reused

Verified working:

- `deception.tripwire.TripwireDetector` — the actual detection.
  `TripwireDetector.features_from_df(df)` returns all-zeros when the
  `is_honeytoken_use` column is absent (confirmed: `predict → [1, 1]` on a
  frame without the column), which is exactly the `ReplayFlow` case. So it
  is safe to run unconditionally on replay traffic.
- `SETTINGS.deception.confidence_both / _tripwire_only / _volume_only /
  _none` = `0.99 / 0.9 / 0.5 / 0.0` — the escalation constants.

The irreducible remainder is the three-branch `np.select` combinator itself.

### Minimal non-duplicating approach — and it touches no `src/` file

Define the combinator **exactly once in Phase 5**, as a module-level function
in `backend/streaming.py`:

```python
def fuse_tripwire_confidence(
    volume_fired: np.ndarray, tripwire_fired: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """OR-fuse the volumetric and tripwire signals and escalate confidence.

    Mirrors core/pipeline.run_analysis()'s Phase 2 fusion block
    (src/core/pipeline.py:134-154) *by importing the same detector and the
    same SETTINGS.deception constants*; only the np.select combinator is
    restated, because that block is inline in run_analysis and therefore
    not importable, and extracting it would modify src/ (Invariant A).
    Pinned to run_analysis by a parity test — see
    tests/test_streaming_scorer.py::test_fusion_matches_pipeline.
    """
```

This is **not** a duplicate definition in the CI sense (no name collision;
CI's AST walk is `pathlib.Path("src").glob("*.py")` — top-level `src/` only,
see §12), but it *is* a duplicated expression, so it is pinned by test, not
by comment.

### Where fusion belongs

**Not on `StreamingScorer`.** `StreamingScorer`'s contract is "the fitted
volumetric model, applied without refitting". Fusion is an ingest policy
concern (Ticket #7), which is also where PLAN_MASTER's architecture diagram
and RECON §B put it. Ship `fuse_tripwire_confidence` as a free function in
`backend/streaming.py` so Ticket #7 imports it rather than hand-rolling a
second copy, and keep `StreamingScorer` purely volumetric.

### Consequence Ticket #7 must know

`ReplayFlow` has no `is_honeytoken_use` field. Replayed CIC traffic
therefore always fuses with an all-`False` tripwire vector, so replay-path
confidence is always `confidence_volume_only` (0.5) or `confidence_none`
(0.0). Only Ticket #13's injected flows can produce 0.9 / 0.99. This is
correct — but it means the *demo's* headline confidence number comes from
injection, not from the recorded stream. See Q5, which makes the same point
from the detection side.

---

## 6. Q5 — Contamination, and an uncomfortable finding

### Contamination on all-benign data is a false-positive budget

Monday has **0 attack rows of 529,918**, verified. So `contamination` is not
"the expected anomaly rate" — it is a **stated false-positive rate**: the
fraction of *known-benign* traffic the model is instructed to flag. sklearn
implements it exactly that way (`offset_ = percentile(score_samples(X_train),
100 * contamination)`).

### Measured: warmup-fitted model applied to friday-morning (191,033 rows, 1,966 real Bot attacks = 1.029%)

| contamination | `offset_` | monday flag % | friday flag % | TP | FP | precision | recall | flagged flows/sec of demo |
|---|---|---|---|---|---|---|---|---|
| **0.08** (`SETTINGS.ml` default) | −0.578 | 8.000 | **8.706** | 46 | 16,586 | 0.28% | 2.34% | **23.10** |
| 0.05 | −0.605 | 5.000 | 5.633 | 45 | 10,715 | 0.42% | 2.29% | 14.88 |
| 0.02 | −0.669 | 2.000 | 2.373 | 44 | 4,490 | 0.97% | 2.24% | 6.29 |
| 0.01 | −0.721 | 1.000 | 1.229 | 44 | 2,303 | 1.87% | 2.24% | 3.26 |
| **0.005** | −0.752 | 0.499 | **0.651** | 43 | 1,200 | 3.46% | 2.19% | **1.73** |
| 0.002 | −0.785 | 0.199 | 0.278 | **0** | 532 | 0.00% | 0.00% | 0.74 |
| 0.001 | −0.803 | 0.100 | 0.136 | **0** | 260 | 0.00% | 0.00% | 0.36 |
| `"auto"` | −0.500 | 16.264 | 16.462 | 54 | 31,393 | 0.17% | 2.75% | 43.60 |

"flagged flows/sec of demo" assumes the documented operating point:
friday-morning spans 08:59–12:59 (14,400 s of capture) replayed at 20×, i.e.
a **720 s / 12-minute demo carrying ~265 events/sec**.

### Recommendation

**`streaming_contamination = 0.005`**, as a new
`BACKEND_SETTINGS` field.

- At 0.08 (the current `SETTINGS.ml` value) the console flags **23 flows per
  second**. That is a wall of red; it reads as a broken detector, not a
  vigilant one.
- At 0.005 it flags **1.73 flows/sec against a 265 flows/sec stream** — a
  visible, steady trickle that makes the console look alive without
  drowning the injected tripwire alert (confidence 0.99) that is the demo's
  actual payload.
- 0.002 (0.74/sec) is the fallback if the live feed still reads noisy.
  Do not go below it without accepting zero true positives (see below).

**Do not change `SETTINGS.ml.isolation_forest_contamination`.** It is 0.08,
it is what Phase 3's published evaluation numbers were produced with, and
changing it edits `src/` (Invariant A) and silently invalidates those
numbers.

### Implementation trap — `contamination=0.0` is silently ignored

`ml_engine.train_isolation_forest` does
`contamination=contamination or SETTINGS.ml.isolation_forest_contamination`.
Verified: passing `contamination=0.0` produces a model with
`clf.contamination == 0.08`, identical to the default. The falsy-`or`
pattern swallows it. Therefore the new Pydantic field **must** be
`gt=0.0, lt=0.5`, and `StreamingScorer` must never pass `0`, `0.0`, or
`None` expecting "no flagging".

### The finding the plan does not acknowledge

**The volumetric detector has essentially no discriminative power on
friday-morning's real attacks.** Measured with the full-Monday warmup model:

```
ROC AUC        = 0.600
Average precision = 0.0146   against a base rate of 0.0103  (lift ~1.4x)
```

At the recommended operating point that is 43 true positives against 1,200
false positives — 3.46% precision, 2.19% recall. Below contamination 0.002 it
catches **zero** attacks.

The reason is in the data, and it is not fixable by tuning:

```
                    n        median duration   median packets   median bytes   p95 bytes
BENIGN         189,067          0.031 s              2               70          1,838
Bot (attack)     1,966          0.071 s              3                6            213
```

friday-morning's only attack class is `Bot` (verified label census: `BENIGN`
189,067, `Bot` 1,966 — nothing else). Bot C2 beacons are *smaller* than
benign traffic. An isolation-forest outlier detector over
(duration, packets, bytes) flags **large** flows. It is looking in the
opposite direction from where the attack lives.

Cross-checked with the same warmup model on the other capture days:

| day | rows | attack % | ROC AUC | flag % @0.01 | precision | recall |
|---|---|---|---|---|---|---|
| friday-morning | 191,033 | 1.03 | 0.600 | 1.23 | 1.87% | 2.24% |
| friday-afternoon-ddos | 225,745 | 56.71 | 0.563 | 3.91 | ~0% | ~0% |
| friday-afternoon-portscan | 286,467 | 55.48 | **0.477** (worse than chance) | 0.57 | 0.06% | ~0% |
| wednesday | 692,703 | 36.48 | 0.752 | 1.23 | 0.13% | ~0% |

Verified corollary: **the five most anomalous friday-morning flows are all
`is_attack == False`** — genuine volumetric outliers in benign traffic
(207,964 packets / 1.24 MB, etc.).

**What this means, and what it does not:**

- It does **not** invalidate the ticket. `StreamingScorer` is still correct,
  still necessary, and Invariant B still matters — a drifting baseline would
  make even these numbers meaningless.
- It **does** invalidate PLAN_MASTER's demo-arc step 2 as currently worded:
  *"Anomalies surface naturally from the real data."* Anomalies do surface,
  and they are genuinely anomalous — but they are unusual **benign**
  transfers, not the labelled attacks. Saying on stage that the console
  "caught the attack in the recorded traffic" would be a fabricated
  detection claim, and the ground-truth labels are right there to contradict
  it.
- The defensible narrative is already in the plan and is unaffected: the
  volumetric detector surfaces *unusual* traffic (true, demonstrable, with a
  real σ breakdown), and the **detection** claim rests on the deterministic
  honeytoken tripwire fired by Ticket #13's injection, which provably
  precedes exfiltration.
- **Escalation, out of scope for this ticket:** P5-8 chose friday-morning on
  attack *realism* (1.03%) without measuring detectability. Wednesday scores
  best of the days measured (AUC 0.752) but is 36.5% attacks. The sprint
  owner should decide whether P5-8 is revisited; Ticket #5 should not
  silently change the landing day.

`fit_from_warmup()` should therefore expose the ground-truth label
(`ReplayFlow.is_attack`) in its return value / metadata so this measurement
is reproducible from the codebase rather than from this document.

---

## 7. Q6 — Persistence

### Path — K2

**Use `BACKEND_SETTINGS.model_artifact_path_resolved`, never
`model_artifact_path`.** The unresolved field is CWD-relative
(`artifacts/streaming_scorer.joblib`); the resolved property anchors it to
`_REPO_ROOT` derived from `__file__`. This is the exact defect the Ticket #1
review caught (from `/tmp`, the unresolved path became
`/private/tmp/artifacts/streaming_scorer.joblib`). `artifacts/` and
`*.joblib` are both already in `.gitignore` — no change needed there.

Pin it with a test that `monkeypatch.chdir(tmp_path)` and asserts `save()`
still writes to the repo-root path, mirroring
`tests/test_backend_config.py:277`.

### Artifact contents

```python
ARTIFACT_SCHEMA_VERSION = "1.0"

{
  "artifact_schema_version": "1.0",
  "model":   IsolationForest,          # fitted
  "scaler":  StandardScaler,           # fitted, carries feature_names_in_
  "feature_names": ["duration_sec", "packets", "bytes"],
  "baseline_degenerate": [False, False, False],   # (scaler.var_ == 0), see Q3
  "warmup": {
      "day": "monday",
      "source_file": "Monday-WorkingHours.pcap_ISCX.csv",
      "source_dataset": "CIC-IDS2017-TrafficLabelling",
      "rows_seen": 529918, "rows_used": 529918, "rows_skipped": 0,
      "attack_rows_in_warmup": 0,       # asserted == 0 at fit time
      "ts_min": "...", "ts_max": "...",
      "fitted_at": "<utc isoformat>",
  },
  "hyperparameters": {"n_estimators": 100, "contamination": 0.005,
                      "random_state": 42, "max_samples_": 256},
  "library_versions": {"python": "3.13.x", "numpy": "2.4.6",
                       "scikit-learn": "1.8.0", "joblib": "1.5.3"},
}
```

Measured: **823 KiB**, `joblib.dump` 0.01 s, `joblib.load` **7.8 ms**.
Round-trip verified **byte-identical**: scores from the loaded artifact
`== ` scores from the in-memory objects exactly (not merely `allclose`) over
191,033 rows.

### Compatibility detection on load — what fails hard vs. what warns

**Hard failure (`StreamingScorerError`, no fallback):**

1. `artifact["artifact_schema_version"] != ARTIFACT_SCHEMA_VERSION`.
2. `artifact["feature_names"] != list(SETTINGS.ml.default_features)`. This is
   the highest-value check: it catches a future `default_features` edit
   silently invalidating a stale artifact, which would otherwise produce
   confidently wrong scores.
3. `scaler.n_features_in_ != len(feature_names)` or
   `model.n_features_in_ != len(feature_names)`.
4. Any missing top-level key, or `joblib.load` raising.

**Warn, do not fail:** scikit-learn version drift. Verified that
`sklearn.exceptions.InconsistentVersionWarning` exists and is emitted at
unpickle time when the pickling version differs. Wrap `joblib.load` in
`warnings.catch_warnings(record=True)` and log any such warning at WARNING
level together with the recorded `library_versions` — but do not refuse to
start. Rationale: Invariant F wants the demo to work offline on the demo
machine; bricking it on a patch bump is a worse failure than a logged
mismatch, and the mismatch is surfaced in every `explain()` payload's
`baseline` block anyway. If unpickling actually raises, that is case (4)
above and it is fatal.

### Missing artifact at startup — the fallback must NOT refit

This is where Invariant B would sneak back in. Required behaviour:

- `StreamingScorer.load()` raises **`StreamingScorerArtifactMissing`** (a
  subclass of `StreamingScorerError` and of `FileNotFoundError`) naming the
  exact absolute path and the exact build command:
  `PYTHONPATH=src venv/bin/python -m backend.warmup`.
- **`load()` has no `fit_if_missing` / `auto_fit` parameter, and none may be
  added.** Make this an explicit line in the docstring so the next
  implementer does not "helpfully" add one.
- An un-fitted `StreamingScorer()` instance is unusable: `score_batch()`,
  `score_event()`, and `explain()` all raise `StreamingScorerNotFitted`.
  There is no code path from "artifact missing" to "a model got fitted on
  stream data".
- **Backend startup (Ticket #7 wiring, recommended here):** fail fast — the
  FastAPI lifespan should let the exception propagate so uvicorn refuses to
  start. Considered and **rejected**: degrading to a "scoring disabled" mode,
  because a console showing zero anomalies is visually indistinguishable
  from a working console during quiet traffic. That is a silent failure of
  exactly the kind this ticket exists to prevent. If a degraded mode is ever
  wanted, it must persist `is_anomaly = NULL` (not `false`) and render a
  permanent banner.
- Provide `backend/warmup.py` as a `python -m backend.warmup` CLI. Measured
  cost **4.72 s** — cheap enough to make a Makefile/README step, and it
  satisfies T8 ("demo trains at startup" → it does not).

---

## 8. Q7 — Throughput

All measurements: warm (3 untimed calls first), 30–200 timed repetitions,
real friday-morning rows, full-Monday warmup model.

### Batch scaling

| batch size | ms/batch | ms/event |
|---|---|---|
| 1 | 1.996 | **1.99589** |
| 10 | 1.991 | 0.19905 |
| 50 | 2.022 | 0.04044 |
| **87** (P5-12's measured max at demo speed 20×) | ~2.17 | **~0.0249** |
| 134 | 2.165 | 0.01616 |
| **500** (P5-12 cap) | 2.731 | **0.00546** |
| 2000 | 5.308 | 0.00265 |

The shape is `≈ 1.99 ms fixed + 1.5 µs/event`. The fixed cost is sklearn's
per-call tree-traversal/`Parallel` setup — which is precisely what batching
amortises.

### Component breakdown at batch 500

| step | ms | share |
|---|---|---|
| build DataFrame from `list[ReplayFlow]` | 0.135 | 5% |
| `scaler.transform(DataFrame)` | 0.191 | 7% |
| `clf.decision_function` | 2.418 | **89%** |
| (`clf.predict`, if also called) | +2.575 | — |
| **total, `decision_function` only** | **2.731** | |
| **total, via `compute_anomaly_scores`** | **5.421** | |

### Verdict

**The wrapper adds no per-event overhead that could erase the batching
advantage.** The conversion path costs 0.326 ms of a 2.73 ms batch (12%),
and it is essentially *fixed*, not per-event — it does not grow the
1.5 µs/event marginal cost. Ticket #6's 0.0072 ms/event figure is
reproduced and slightly beaten (0.00546 measured with `decision_function`
only). Even the recommended reuse path via `compute_anomaly_scores`
(0.0108 ms/event) is **69× under P5-10's 0.747 ms/event budget**, and at the
actual demo operating point (batch 87 at 20×) it is ~0.049 ms/event, **15×
under budget**.

### Two rules the implementer must follow

1. **`score_event()` is a convenience/test API only.** Measured
   1.996 ms/event — **366× worse** than batch-500. Implement it literally as
   `self.score_batch([flow])[0]` and say in its docstring that calling it in
   a loop is a performance bug, with the measured numbers. Ticket #7 must
   call `score_batch`.
2. **Never transform an ndarray against a name-fitted scaler.** It emits a
   `UserWarning` on *every* call (observed: 54 warnings from one test run) —
   at 10 batches/sec that is log spam that will bury a real warning during
   the demo. Always pass a DataFrame (see Q2).

---

## 9. Q8 — `backend/` or `src/`?

**Recommendation: `backend/streaming.py`.** PLAN_MASTER's `src/core/streaming.py`
should be corrected.

### Arguments, strongest first

1. **It would break the Invariant A verification mechanism.** Every Phase 5
   ticket has been accepted on `git status --short src/` being empty
   (verified empty right now). A new untracked file under `src/` makes that
   output non-empty, so the one mechanical, unarguable check of the sprint's
   core invariant becomes a judgement call requiring an allowlist. That is a
   real loss for zero gain.
2. **It breaks the P5-4 configuration boundary, and it does not import.**
   K2 requires `StreamingScorer` to read
   `BACKEND_SETTINGS.model_artifact_path_resolved`. P5-4 states `src/` reads
   no environment; `backend/config.py` does. A `src/` module importing
   `backend.config` inverts that. It also simply fails: verified —
   `PYTHONPATH=src python -c "import backend"` succeeds only when the CWD
   happens to be the repo root, and raises `ModuleNotFoundError: No module
   named 'backend'` from any other directory. `backend/__init__.py`
   establishes `backend → src`; nothing establishes `src → backend`.
3. **Dependency direction.** `StreamingScorer`'s input type is
   `backend.replay_reader.ReplayFlow` and its only consumer is Ticket #7 in
   `backend/`. Placing it in `src/` makes the frozen engine depend on the
   new backend, which is backwards.
4. **Dependencies.** `joblib` is in `requirements-backend.txt`, not
   `requirements.txt`. (Mitigating fact, checked: `joblib` is a transitive
   dependency of `scikit-learn`, so this alone would not break CI — but the
   declared dependency graph would be wrong.)

### One argument that is commonly made and is **factually wrong**

The claim that `src/` is protected by the CI duplicate-definition walk does
**not** apply to a subpackage file. The CI check is:

```python
src = pathlib.Path("src")
for py_file in src.glob("*.py"):   # NOT rglob — top level only
```

`src/core/streaming.py` would never be scanned. Neither are
`src/detectors/*`, `src/datasets/*`, `src/deception/*`, or
`src/evaluation/*` today. P5-1 cites this protection as a reason for
`backend/` being a top-level package; the reasoning is sound for
`backend/*.py` but should not be reused for this decision. (This is also
worth recording independently: CLAUDE.md §5 says CI "walks the AST of every
`src/*.py`", which readers reasonably interpret as recursive. It is not.)

### The trade-off, stated honestly

PLAN_MASTER frames `src/core/streaming.py` as *"the ONE new file in `src/`"*
— a narrative beat about the engine changing in exactly one place. Moving it
to `backend/` **strengthens** that beat rather than weakening it: the claim
becomes *"zero files in `src/` were changed or added; the entire engine is
byte-identical, and 229/229 of its tests still pass."* That is a better
sentence and a stronger, mechanically-verifiable claim.

The cost is a documentation inconsistency, which must be fixed in the same
PR (§10).

---

## 10. File list

### Create

| Path | Contents |
|---|---|
| `backend/streaming.py` | `StreamingScorer`, `ScoredFlow`, `fuse_tripwire_confidence`, `ARTIFACT_SCHEMA_VERSION`, exception hierarchy |
| `backend/warmup.py` | `python -m backend.warmup` build-time CLI: read warmup day → `fit_from_warmup` → `save()`; prints row counts, timings, resolved artifact path |
| `tests/test_streaming_scorer.py` | §13 |

### Modify

| Path | Change |
|---|---|
| `backend/config.py` | **additive fields only:** `streaming_contamination` (0.005, `gt=0.0, lt=0.5`), `streaming_n_estimators` (`None` → `SETTINGS.ml` default), `warmup_row_limit` (`None`), `warmup_min_rows` (1000) |
| `tests/test_backend_config.py` | additive tests for the four new fields (bounds, env override, defaults) |
| `.env.example` | document the four new `AEGIS_*` vars |
| `docs/PHASE5_STATE.md` | Ticket #5 → accepted; new decisions P5-13 (scorer lives in `backend/`, with the §9 reasoning), P5-14 (`streaming_contamination = 0.005` and the measured detectability finding); new known issue for the `contamination or` trap |
| `PLAN_MASTER.md` | Phase 5 architecture block + "The one real engine change": `src/core/streaming.py` → `backend/streaming.py`; demo-arc step 2 reworded per §6 |
| `docs/PHASE5_RECON.md` | §B invariant map and §G day-0/1 table: same path correction |

### Explicitly untouched

Everything under `src/` (Invariant A), every existing engine test, and every
test file belonging to Tickets #1/#2/#5b/#6.

---

## 11. Class surface

```python
ARTIFACT_SCHEMA_VERSION: str = "1.0"
EXPLANATION_SCHEMA_VERSION: str = "1.0"

class StreamingScorerError(RuntimeError): ...
class StreamingScorerNotFitted(StreamingScorerError): ...
class StreamingScorerArtifactMissing(StreamingScorerError, FileNotFoundError): ...
class StreamingScorerIncompatible(StreamingScorerError): ...


@dataclass(frozen=True)
class ScoredFlow:
    """One flow's volumetric verdict. Deliberately carries the scaled
    feature row so explain() costs no additional arithmetic (the z-scores
    ARE scaler.transform's output — verified allclose over 191,033 rows)."""
    flow: ReplayFlow
    raw_score: float
    calibrated_score: float
    is_anomaly: bool
    z_scores: tuple[float, ...]   # aligned to feature_names


class StreamingScorer:
    """Fit-once, transform-only anomaly scorer for the Phase 5 stream.

    Invariant B: the StandardScaler and IsolationForest are fitted exactly
    once, in fit_from_warmup(), on all-benign warmup data, and thereafter
    only transform()/decision_function() are called. ml_engine.
    preprocess_features() (which fit_transform()s) is imported for the
    warmup fit ONLY and must never appear in a scoring path — refitting per
    batch collapses the `packets` baseline sigma to 5.5% of its true value
    (measured) while nothing visibly breaks.
    """

    def __init__(self, feature_names: list[str] | None = None) -> None:
        """feature_names defaults to SETTINGS.ml.default_features
        (optional-override convention). Constructs an UNFITTED scorer:
        every scoring method raises StreamingScorerNotFitted until
        fit_from_warmup() or load() has run."""

    # ---- fitting (build time only) ----------------------------------
    def fit_from_warmup(
        self,
        flows: Sequence[ReplayFlow] | None = None,
        day: str | None = None,
        row_limit: int | None = None,
        contamination: float | None = None,
        n_estimators: int | None = None,
        random_state: int | None = None,
    ) -> "StreamingScorer":
        """Fit scaler + IsolationForest once on benign warmup traffic.

        `flows` may be supplied directly (tests); otherwise the warmup day
        is read via ReplayFlowReader. `day` defaults to
        BACKEND_SETTINGS.warmup_dataset_day ("monday"), `row_limit` to
        BACKEND_SETTINGS.warmup_row_limit (None = all 529,918 rows — see
        docs/PHASE5_TICKET5_PLAN.md Q1: subsampling saves 0.68s of 4.72s
        and makes the `packets` baseline sigma wrong by 3x), and
        `contamination` to BACKEND_SETTINGS.streaming_contamination
        (0.005 — a stated FALSE-POSITIVE budget, since the warmup set has
        zero attacks by construction).

        Delegates the fit to ml_engine.preprocess_features() and
        ml_engine.train_isolation_forest() — this is the ONE call site
        where fit_transform is correct.

        Raises StreamingScorerError if fewer than
        BACKEND_SETTINGS.warmup_min_rows rows survive (zero-variance
        baseline risk, Q3) or if any warmup row has is_attack=True.
        """

    # ---- persistence -------------------------------------------------
    def save(self, path: Path | None = None) -> Path:
        """joblib-dump to BACKEND_SETTINGS.model_artifact_path_resolved
        (K2 — never the CWD-relative model_artifact_path). Creates parent
        directories. Returns the absolute path written."""

    @classmethod
    def load(cls, path: Path | None = None) -> "StreamingScorer":
        """Load a persisted scorer. Defaults to
        model_artifact_path_resolved (K2).

        NEVER refits. There is deliberately no fit_if_missing parameter and
        none may be added: an implicit refit on a stream is precisely the
        Invariant B failure this class exists to prevent.

        Raises StreamingScorerArtifactMissing (naming the path and the
        `python -m backend.warmup` build command) if absent;
        StreamingScorerIncompatible on artifact-schema, feature-name, or
        n_features_in_ mismatch. Logs (does not raise) on scikit-learn
        version drift."""

    # ---- scoring (hot path) ------------------------------------------
    def score_batch(self, flows: Sequence[ReplayFlow]) -> list[ScoredFlow]:
        """Score a micro-batch. transform() only, never fit_transform().

        THE hot path (Ticket #6 emits batches of <=500, P5-12). Measured:
        2.73 ms per 500-flow batch = 0.0055 ms/event; cost is
        ~1.99 ms fixed + 1.5 us/event, so batching is what amortises the
        fixed cost. Empty input returns []."""

    def score_event(self, flow: ReplayFlow) -> ScoredFlow:
        """Score one flow. CONVENIENCE/TEST API ONLY — measured
        1.996 ms/event, 366x worse per event than score_batch(500).
        Calling this in a loop is a performance bug; Ticket #7 must use
        score_batch()."""

    # ---- explanation --------------------------------------------------
    def explain(self, scored: ScoredFlow) -> dict:
        """Per-feature deviation vs. the warmup baseline, as the JSON dict
        persisted to alerts.explanation (backend/models.py:287).

        Deliberately NOT SHAP: the fitted StandardScaler already holds
        mean_ and scale_, so z = (x - mean_)/scale_ is exactly
        scaler.transform's output (verified identical over 191,033 rows) —
        already computed by score_batch and cached on ScoredFlow.z_scores,
        so this costs no arithmetic at all.

        Features are sorted by |z| descending; all are included, never
        truncated. A feature whose warmup variance was zero reports
        z=None / degenerate_baseline=True and is described in raw units:
        sklearn substitutes scale_=1.0 for a zero scale, so a naive z would
        be a raw-unit number wearing a sigma label — a fabricated
        statistic. All values are plain JSON scalars, never numpy types
        (psycopg cannot serialise those into JSONB)."""

    # ---- introspection -------------------------------------------------
    @property
    def is_fitted(self) -> bool: ...
    @property
    def baseline(self) -> dict: ...
        # feature_names, mean_, scale_, var_, degenerate flags, warmup
        # metadata, hyperparameters, library_versions — the provenance
        # block embedded in every explain() payload and served by
        # Ticket #16's /api/stats.


def fuse_tripwire_confidence(
    volume_fired: np.ndarray, tripwire_fired: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """See §5. Free function, not a StreamingScorer method — fusion is an
    ingest policy concern (Ticket #7), not the scorer's contract."""
```

---

## 12. Errors in the existing plan documents

Stated plainly, with evidence, as requested.

| # | Document | Claim | Reality |
|---|---|---|---|
| **1** | CLAUDE.md §5; implied by P5-1 | CI "walks the AST of every `src/*.py`" and duplicate-def protection covers `src/` | The check is `pathlib.Path("src").glob("*.py")` — **top-level only, not recursive**. `src/core/`, `src/detectors/`, `src/datasets/`, `src/deception/`, `src/evaluation/` are all unscanned today. Do not use this as a reason for or against Q8. |
| **2** | PLAN_MASTER Phase 5 architecture; RECON §B, §G | `StreamingScorer` lives at `src/core/streaming.py` | Should be `backend/streaming.py` — see §9. A `src/` module cannot import `backend.config` (verified `ModuleNotFoundError` from any CWD other than the repo root), which K2 requires it to do; and it breaks the `git status --short src/` acceptance check. |
| **3** | PLAN_MASTER demo arc step 2 | "Anomalies surface naturally from the real data" (implying the recorded attacks are detected) | Measured ROC AUC **0.600**, AP **0.0146** at a 1.029% base rate; at contamination 0.005, 43 TP / 1,200 FP. The five most anomalous friday-morning flows are all benign. Anomalies surface; the *labelled attacks* do not. Reword before the rehearsal — the ground truth is in the CSV and contradicts the stronger claim. |
| **4** | RECON §C | "Model + calibration … sigmoid `1/(1+exp(5·raw))` — **mirror exactly**" | Do not mirror — **call** `ml_engine.compute_anomaly_scores`. Mirroring creates a second copy of the bare literal `5.0` that no test would catch drifting. Measured cost of calling it: 0.0108 ms/event, 69× under P5-10's budget. |
| **5** | RECON §F/T1, PHASE5_STATE T-B | "Pin with a test asserting `scaler.mean_` is byte-identical before and after scoring an extreme event" | Necessary but **not sufficient**: it passes trivially if the scorer merely holds a reference to a scaler it never touches, and it would not catch a `preprocess_features` import creeping into `score_batch` (which returns a *new* scaler, leaving the old one untouched). Add the stronger checks in §13. |
| **6** | `ml_engine.compute_anomaly_scores` docstring | "Returns the modified DataFrame (**also mutates in-place**)" | It does `edges_df = edges_df.copy()` first — the caller's frame is **not** mutated. Stale docstring. Cannot be fixed here (Invariant A); note it so Ticket #5's implementer does not rely on the in-place behaviour. |
| **7** | Ticket #1 acceptance, PHASE5_STATE | "Backend tests pass with no live database (CI has none)" | `backend/config.py` imports `pydantic_settings`, which is in `requirements-backend.txt` only. CI installs `requirements.txt` alone and it is not a transitive dependency of anything there (only of `fastapi[standard]`). So `tests/test_backend_config.py` — and any Ticket #5 test importing `BACKEND_SETTINGS` — will **fail collection in GitHub CI**, though it passes locally. (`joblib` is fine: it *is* a transitive dependency of scikit-learn.) Needs a CI step installing `requirements-backend.txt`, or an `importorskip` guard. Pre-existing; flagging because Ticket #5 makes it worse. |
| **8** | `ml_engine.train_isolation_forest` | `contamination` is a plain optional-override | `contamination or SETTINGS...` — verified that `contamination=0.0` silently yields `0.08`. Bound the new setting `gt=0.0`. |
| **9** | PHASE5_STATE "Timing / demo notes" | "wednesday (692,703 rows) **28.3 s**" read+sort | Measured **monday 529,918 rows in 3.52 s** and **friday-morning 191,033 in 1.24 s** on this machine. Either the 28.3 s figure predates an optimisation or it was measured under different conditions. Not blocking — the real number is better than the recorded one — but Ticket #5's build-time budget should quote the measured 4.72 s end-to-end, not extrapolate from 28.3 s. |

---

## 13. Test strategy

### Offline (no dataset, no DB) — must run in CI

These use synthetic `ReplayFlow` fixtures and cover the invariant.

1. **`test_invariant_b_scaler_identity`** — capture `id(scorer._scaler)`,
   `scaler.mean_.copy()`, `scaler.scale_.copy()`, `scaler.var_.copy()` after
   fit. Score 50 batches including a deliberately extreme flow
   (2,000,000 packets). Assert **object identity unchanged** and all three
   arrays **byte-identical** (`np.array_equal`, not `allclose`).
2. **`test_invariant_b_no_fit_calls_in_scoring_path`** — the strong one.
   Monkeypatch `StandardScaler.fit`, `StandardScaler.fit_transform`,
   `IsolationForest.fit`, and `ml_engine.preprocess_features` to raise
   `AssertionError`. Then run `score_batch`, `score_event`, and `explain`.
   Any refit anywhere in the call tree — including one introduced by a
   future refactor that creates a *fresh* scaler — fails loudly. This is the
   regression pin §12 item 5 says the documented test lacks.
3. **`test_score_batch_equals_score_event`** — `score_batch(flows)` element-wise
   `==` `[score_event(f) for f in flows]`.
4. **`test_score_batch_order_and_length`** — output length and ordering match
   input exactly; empty input returns `[]`.
5. **`test_explain_z_matches_hand_computed`** — with a hand-built scaler
   (`mean_=[10,10,500]`, `scale_=[2,4,100]`), assert each reported `z`
   equals `(x-mean)/scale` to 12 decimal places, `direction` signs are
   right, ordering is by `|z|` descending, and every value is a builtin
   `float`/`bool`/`str`/`None` (`json.dumps` round-trips) — no numpy
   scalars, which psycopg cannot write to JSONB.
6. **`test_explain_zero_variance_feature`** — fit on data with a constant
   feature (bypassing `warmup_min_rows`); assert `z is None`,
   `degenerate_baseline is True`, the summary contains no "sigma" claim for
   that feature, and `top_feature` prefers a non-degenerate feature.
7. **`test_fit_rejects_too_few_rows`** / **`test_fit_rejects_attack_rows`**.
8. **`test_save_load_round_trip`** — scores from a loaded scorer are
   **exactly** equal (`==`, verified achievable) to the pre-save scores;
   `feature_names`, `baseline_degenerate`, and warmup metadata survive.
9. **`test_save_uses_resolved_path`** — `monkeypatch.chdir(tmp_path)`; assert
   `save()` returns the repo-root-anchored absolute path and that nothing
   was written under `tmp_path`. (K2.)
10. **`test_load_missing_artifact_raises_and_does_not_fit`** — assert
    `StreamingScorerArtifactMissing`, that the message contains the absolute
    path and `backend.warmup`, and — with `IsolationForest.fit` monkeypatched
    to raise — that no fit was attempted.
11. **`test_load_rejects_incompatible_artifact`** — three cases: bumped
    `artifact_schema_version`, mutated `feature_names`, mismatched
    `n_features_in_`. Each raises `StreamingScorerIncompatible`.
12. **`test_unfitted_scorer_raises`** — `score_batch` / `score_event` /
    `explain` on a fresh instance raise `StreamingScorerNotFitted`.
13. **`test_fusion_matches_pipeline`** — truth table for
    `fuse_tripwire_confidence` against `SETTINGS.deception` constants
    (0.99 / 0.9 / 0.5 / 0.0, read from SETTINGS, not literals), plus a
    behavioural parity assertion against `core.pipeline.run_analysis`'s
    `confidence` column on a fixture carrying `is_honeytoken_use`.
14. **`test_contamination_never_zero`** — `BACKEND_SETTINGS` field rejects
    `0.0`; and a regression assert that
    `train_isolation_forest(X, contamination=0.0).contamination == 0.08`,
    documenting the upstream trap so it cannot silently change meaning.
15. **`test_no_feature_name_warnings`** — `warnings.simplefilter("error")`
    around `score_batch`; a `UserWarning` about feature names fails the test.

### Needs real data — `pytest.skip` on `DatasetNotAvailable`, mirroring the existing convention

16. **`test_warmup_day_is_all_benign`** — assert `monday` yields 529,918
    flows, `is_attack.sum() == 0`, `rows_seen == rows_emitted`.
17. **`test_warmup_baseline_has_no_degenerate_features`** — assert
    `(scaler.var_ > 0).all()` on the full Monday fit.
18. **`test_warmup_fit_under_time_budget`** — assert end-to-end
    `fit_from_warmup()` completes in < 30 s (measured 4.72 s; a 6× headroom
    catches an accidental O(n²), not machine variance).
19. **`test_flag_rate_on_landing_stream`** — score friday-morning with the
    warmup model at the configured contamination; assert the flag rate is
    within a documented band (measured 0.651% at 0.005; assert 0.3%–1.5%).
    This is the "does the demo cry wolf" regression.
20. **`test_detectability_is_recorded_not_assumed`** — compute and assert the
    measured ROC AUC on friday-morning is in `[0.55, 0.70]` (measured 0.600),
    with a comment pointing at §6. Purpose: if someone later changes the
    features or the landing day, this test forces them to look at the number
    rather than assume it improved.

### Regression gate (unchanged, every ticket)

`PYTHONPATH=src venv/bin/python -m pytest tests/ -q` → at least
**357 passed** (229 engine baseline intact); `ruff check src/ backend/
--select E,F,W --ignore E501` clean; duplicate-def check clean;
`git status --short src/` **empty**.

---

## 14. Acceptance criteria

- [ ] `backend/streaming.py` and `backend/warmup.py` exist; **nothing under
      `src/` is changed or added** (`git status --short src/` empty).
- [ ] `python -m backend.warmup` fits on all 529,918 Monday rows and writes
      `<repo>/artifacts/streaming_scorer.joblib` in under 30 s (measured
      4.72 s), printing rows used, attack rows (must be 0), contamination,
      and the absolute path.
- [ ] Loading the artifact and scoring reproduces the pre-save scores
      **exactly**.
- [ ] `score_batch(500)` measured under 10 ms (measured 2.7–5.4 ms).
- [ ] `explain()` output validates against §4's shape, `json.dumps`
      round-trips, and every σ number is reproducible by hand from the
      persisted `mean_`/`scale_`.
- [ ] Missing artifact → `StreamingScorerArtifactMissing` naming the path and
      the build command; **no fit is attempted** (asserted with a
      monkeypatched `fit`).
- [ ] Tests 1 and 2 in §13 both present and passing; test 2 fails if
      `preprocess_features` is reintroduced anywhere in a scoring path.
- [ ] Flag rate on friday-morning within 0.3%–1.5% at the shipped
      contamination.
- [ ] Full suite ≥ 357 passed, ruff clean, duplicate-def clean.
- [ ] `PHASE5_STATE.md` records P5-13/P5-14 and the §6 detectability finding
      verbatim, including the numbers.
- [ ] `PLAN_MASTER.md` and `PHASE5_RECON.md` path references corrected, and
      demo-arc step 2 reworded.

---

## 15. Risks

| # | Risk | Likelihood | Mitigation |
|---|---|---|---|
| **R-A** | A future refactor reintroduces `preprocess_features` into the scoring path. Scores stay plausible and become meaningless — the worst failure mode, because nothing breaks. | Medium | §13 test 2 (monkeypatched `fit`/`fit_transform`/`preprocess_features` raising) — the only check that survives a refactor creating a *fresh* scaler. |
| **R-B** | The demo console cries wolf or shows nothing. | Medium | `streaming_contamination` measured against the real landing stream (§6 table); §13 test 19 pins the flag rate in a band. |
| **R-C** | Someone claims on stage that the console detected the recorded attack. | **High** — the plan currently implies it | §6 and §12 item 3; PLAN_MASTER reworded; §13 test 20 keeps the real AUC in the test suite where it cannot be forgotten. |
| **R-D** | Artifact and code drift (features change, artifact does not). | Medium | `feature_names` compared against `SETTINGS.ml.default_features` on load; hard failure, no fallback (§7). |
| **R-E** | A stale artifact is silently regenerated on the demo machine with different data. | Low | `warmup` metadata (day, file, row counts, `fitted_at`) is embedded and surfaced in every `explain()` payload's `baseline` block, so a wrong artifact is visible in the UI. |
| **R-F** | `backend.config` import fails in GitHub CI (`pydantic_settings` absent). | **High** — will happen on the next push | §12 item 7. Add a CI step installing `requirements-backend.txt`, or guard with `pytest.importorskip`. Decide before Ticket #5 lands, since its tests need `BACKEND_SETTINGS`. |
| **R-G** | Ticket #7 calls `score_event` in a loop. | Medium | Docstring states the measured 366× penalty; §14 pins `score_batch` performance; call it out in the Ticket #7 brief. |
| **R-H** | Moving the file contradicts PLAN_MASTER and confuses a later reader. | Low | Doc corrections are in the same PR's file list (§10) and are acceptance criteria. |
| **R-I** | Monday warmup baseline does not represent friday-morning traffic (different day, different mix). | Medium | Accepted and *recorded*, not hidden: this is inherent to a fit-once design and is exactly the trade Invariant B demands. The `baseline` block in every explanation names the warmup day, so the assumption is visible to the operator rather than implicit. |

---

## 16. Measurement appendix — reproduction

Every number above came from scripts run as
`PYTHONPATH=src venv/bin/python <script>` against
`datasets/TrafficLabelling ` on this machine (Python 3.13 / scikit-learn
1.8.0 / numpy 2.4.6 / joblib 1.5.3). The dataset facts underpinning them:

```
monday          read+sort 3.34 s   529,918 rows   0 skipped   0 attacks (0.000%)
friday-morning  read+sort 1.24 s   191,033 rows   0 skipped   1,966 attacks (1.029%)
                label census: BENIGN 189,067 | Bot 1,966 | (nothing else)
```

The `warmup` day key in `backend/replay_reader.py` is an alias for
`monday`; `BACKEND_SETTINGS.warmup_dataset_day` is `"monday"`. Either
resolves to the same file — `fit_from_warmup` should default to the
**setting**, per P5-8 and the reader's own optional-override convention.
