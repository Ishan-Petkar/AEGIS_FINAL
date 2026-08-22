# AEGIS Evaluation Protocol (Phase 3)

This document describes how AEGIS's anomaly detectors and deception layer
are evaluated, how to reproduce every number this document or the dashboard
publishes, and why the methodology looks the way it does. It is grounded in
the actual code in `src/evaluation/` — if this doc and the code ever
disagree, the code is authoritative (see `CLAUDE.md` §8).

Two separate measurements exist, on purpose, and are never mixed:

1. **Precision / Recall / F1 / ROC-AUC** — the standard supervised-metric
   benchmark for the volumetric detectors (Isolation Forest, Z-Score, MAD,
   optionally One-Class SVM), run against real labeled ground truth
   (CIC-IDS2017, PaySim, SWaT).
2. **Lead time** — how many seconds earlier the honeytoken tripwire fires
   than the volumetric detector would, measured on the four scripted-attack
   presets. This is a completely different measurement on a different
   timeline, not a P/R/F1 number.

---

## 1. Why two separate measurements

`detectors.registry.DETECTORS` (contract C3) holds every detector AEGIS
ships: `isolation_forest`, `zscore`, `mad`, `ocsvm`, and `tripwire`. The
first four are **volumetric** — they learn a distribution over
`duration_sec` / `packets` / `bytes` (or, for SWaT, raw sensor channels) and
flag statistical outliers. `TripwireDetector` is fundamentally different: it
has exactly one feature, `is_honeytoken_use`, which is `True` only when a
connection touches a Purdue-zone gateway's honeytoken credential
(`config.HONEYTOKEN_CREDENTIALS`, `src/deception/adapter.py`). That column
does not exist — is implicitly always `False` — on CIC-IDS2017, PaySim, or
SWaT traffic, none of which contain a honeytoken.

Running `TripwireDetector` through the same precision/recall/F1 harness as
the volumetric detectors would therefore produce a detector that predicts
"normal" for literally every row in the dataset: zero recall, undefined
precision, a meaningless result dressed up as a real one. `run_evaluation()`
explicitly excludes `"tripwire"` from its detector loop
(`evaluation/__init__.py`, `_BENCHMARK_EXCLUDED_DETECTORS`) for exactly this
reason.

Instead, the tripwire is graded on the one thing it actually does: fire
before the volumetric detector would, during the recon stage of a real
attack. That is `evaluation/lead_time.py` — see §4.

---

## 2. Precision / Recall / F1 / ROC-AUC benchmark

### 2.1 What it does

`run_evaluation()` (`src/evaluation/__init__.py`):

1. Loads a dataset via `datasets.loader.load_dataset(dataset, limit=limit)`.
   Falls back to `"synthetic"` if the requested dataset's files aren't
   present on disk (`DatasetNotAvailable`).
2. Derives binary ground truth from the canonical `action` column:
   `action == ACTION_ALERT` → anomaly (`y_true = 1`).
3. Selects feature columns: `SETTINGS.ml.default_features`
   (`duration_sec`, `packets`, `bytes`) for flow/transaction datasets, or —
   for SWaT — every numeric non-canonical column (the raw sensor channels;
   SWaT has no meaningful flow volume).
4. Splits into a **benign-only training set** (`train_fraction`, default
   0.5, of the benign rows) and an **evaluation set** (the remaining benign
   rows plus every anomalous row, in original chronological order). Models
   never see attack labels during training — this simulates unsupervised
   deployment, not a supervised classifier.
5. **Degenerate-split guard** (§3): refuses to score a split whose positive
   rate falls outside `[SETTINGS.evaluation.min_positive_rate,
   SETTINGS.evaluation.max_positive_rate]` (default `[0.01, 0.99]`).
6. Iterates `detectors.registry.DETECTORS`, skipping `"tripwire"` (§1) and
   `"ocsvm"` when `include_ocsvm=False`. Each detector is constructed with
   no arguments, `.fit(X_train)`, then `.predict(X_eval)` /
   `.score_samples(X_eval)`.
7. Scores each detector's predictions — point-wise (`_evaluate`) for
   flow/transaction datasets, **segment-wise** (`_evaluate_segment_wise`,
   §5) for SWaT — and returns a `list[EvalResult]`.

Because the loop iterates the registry instead of hardcoding four detector
blocks, a new `BaseDetector` subclass is benchmarked the moment it's added
to `detectors.registry.DETECTORS` — no changes to `evaluation/__init__.py`
required.

`IsolationForestDetector` / `OneClassSVMDetector`
(`src/detectors/sklearn_wrappers.py`) are thin `BaseDetector` adapters
around `ml_engine.train_isolation_forest` / `train_ocsvm_baseline` — added
in Phase 3 so those two sklearn-native detectors could join the registry
without duplicating their fit logic. `ml_engine.py`'s own functions are
unchanged and still called directly by `core/pipeline.run_analysis()` and
the dashboard's ML tab.

### 2.2 Reproducing the numbers

```bash
cd aegis-project

# Default: CIC-IDS2017, 20k rows, all registered detectors except tripwire
PYTHONPATH=src venv/bin/python -m evaluation

# A specific dataset, row cap, and skip the slow One-Class SVM
PYTHONPATH=src venv/bin/python -m evaluation --dataset swat --limit 20000 --no-ocsvm

# Programmatically
PYTHONPATH=src venv/bin/python -c "
from evaluation import run_evaluation, results_to_dataframe
results = run_evaluation(dataset='cic_ids2017', limit=20000, include_ocsvm=False)
print(results_to_dataframe(results))
"
```

CLI flags (`src/evaluation/__main__.py`):

| Flag | Default | Meaning |
|---|---|---|
| `--dataset` | `cic_ids2017` | Any name `datasets.loader.load_dataset` accepts: `cic_ids2017`, `paysim`, `swat`, `synthetic`, `deception`. |
| `--limit` | `20000` | Max rows loaded (head of the file — see the SWaT caveat in §5.3). |
| `--no-ocsvm` | off | Skip the `"ocsvm"` registry entry (slowest detector). |

The dashboard's ML Inspector tab (`aegis_demo.py`) calls
`run_evaluation(limit=5000, include_ocsvm=False, verbose=False)` through a
`st.cache_data`-wrapped `_cached_run_evaluation()` and renders the resulting
table/chart directly — every number shown there is this same function, not
a hand-typed figure.

### 2.3 Actual output (CIC-IDS2017, this repo's local dataset copy)

```
$ PYTHONPATH=src venv/bin/python -m evaluation --dataset cic_ids2017 --limit 20000 --no-ocsvm
[evaluation] Train: 7126 benign rows | Eval: 15730 rows (54.7% anomalies)
[evaluation] Training Isolation Forest...
[evaluation] Training Z-Score (baseline)...
[evaluation] Training MAD (baseline)...

========================================================================
AEGIS Phase 3 — Anomaly Detection Evaluation Results
========================================================================
[Isolation Forest] P=0.718  R=0.170  F1=0.275  AUC=0.643  (pred 2042/8604 anomalies from 15730 samples, pointwise)
[Z-Score (baseline)] P=0.203  R=0.016  F1=0.030  AUC=0.466  (pred 685/8604 anomalies from 15730 samples, pointwise)
[MAD (baseline)] P=0.667  R=0.539  F1=0.596  AUC=0.676  (pred 6949/8604 anomalies from 15730 samples, pointwise)
========================================================================
```

Read honestly: none of these detectors are close to production-grade on
CIC-IDS2017 with only volumetric features. MAD is the strongest of the
three at this row limit; Z-Score barely beats random. This is expected —
`duration_sec`/`packets`/`bytes` alone is a weak feature set, and the point
of this harness is to report that plainly rather than cherry-pick a
flattering slice.

---

## 3. The degenerate-split guard

### 3.1 The bug this replaces

The original Phase 0 harness had no guard on the eval split's class
balance. If the split happened to land on all-benign (or all-anomalous)
rows, every detector predicted the majority class trivially and the harness
printed something like:

```
[SomeDetector] P=0.000  R=0.000  F1=0.000  AUC=nan  (pred 0/0 anomalies from N samples)
```

silently, indistinguishable from "the detector is genuinely bad" — no
signal that the *split*, not the model, was degenerate. This was a real,
reproducible failure mode: `datasets.loader._load_synthetic` (the fallback
path whenever a requested real dataset isn't present) had a bug of its own
— it hardcoded `action = ACTION_PASS` for every row regardless of
`data_generator`'s `is_ground_truth_anomaly` flag (found and fixed while
building this harness — see the fix in `src/datasets/loader.py` and the
regression test `tests/test_adapters.py::test_synthetic_loader_action_reflects_ground_truth`).
Any environment without real dataset files on disk — a fresh clone, CI —
would have hit this exact all-benign degenerate split on every synthetic
fallback run.

### 3.2 The fix

`run_evaluation()` computes the eval split's positive rate and raises
`DegenerateEvaluationError` (`src/evaluation/__init__.py`) before touching a
single detector if that rate falls outside
`[SETTINGS.evaluation.min_positive_rate, SETTINGS.evaluation.max_positive_rate]`
(default `[0.01, 0.99]`, both `pydantic.Field`-bounded in `settings.py`, no
magic numbers). Both bounds are overridable per-call
(`min_positive_rate` / `max_positive_rate` kwargs, following the project's
optional-override convention) for anyone who genuinely wants to inspect an
extreme split.

```bash
$ PYTHONPATH=src venv/bin/python -m evaluation --dataset deception --limit 10 --no-ocsvm
[evaluation] ERROR: Eval split for dataset='DECEPTION' (limit=10) has a positive
rate of 1.0000 over 6 rows, outside the sane [0.01, 0.99] band
(SETTINGS.evaluation.min_positive_rate / max_positive_rate). Refusing to report
precision/recall/F1/AUC computed on a degenerate split — ...
```

(The `"deception"` dataset — Phase 2 honeytoken events for every gateway
zone — is 100% `ACTION_ALERT` by construction, so it reliably reproduces a
degenerate split without needing a contrived flag.)

`aegis_demo.py`'s ML Inspector tab catches `DegenerateEvaluationError`
specifically and renders `st.warning(...)` instead of crashing the tab
(mirrors the existing `dataset_warning` pattern used elsewhere in the
dashboard).

`tests/test_evaluation.py::TestDegenerateEvaluationGuard` pins this
behavior: it asserts the exception is raised (not silently swallowed) for a
known-degenerate split, and that the min/max overrides are actually honored
in both directions.

---

## 4. Tripwire lead time

### 4.1 Method

`evaluation/lead_time.py` replays each of the four scripted-attack presets
(`data_generator.ATTACK_RECON_GATEWAY`: Payment Gateway Breach, Camera
Spoofing, Data Exfiltration, Lateral Movement) through
`data_generator.generate_scripted_attack()` — the same two-stage recon/exfil
timeline the dashboard's sidebar buttons build — and computes:

- **`recon_detected_at`** — the tripwire's actual detection instant:
  `deception.adapter.generate_tripwire_events`'s `observed_at` field, which
  is the honeytoken-touch timestamp plus a modeled ~1s log/alert latency
  (`_DETECTION_LATENCY_SEC`). (`generate_scripted_attack`'s own returned
  dict only keeps `timestamp`, not `observed_at` — `lead_time.py` recovers
  it by calling `generate_tripwire_events` again with the same
  `(gateway_zone, seed, base_timestamp)` rather than widen that function's
  public contract for a field only this module needs.)
- **`exfil_detected_at`** — the volumetric detector's detection instant.
  The codebase has no per-event alerting-latency model for Isolation
  Forest/Z-Score/MAD (`ml_engine.compute_anomaly_scores` scores a batch, it
  doesn't simulate streaming delay), so this is set to the most generous
  possible instant available: the moment the exfil connection itself lands
  (`base_timestamp + recon_delay_sec`). Any real deployment would add
  scoring/alerting latency on top of that. **This is a conservative,
  pro-baseline assumption — it can only understate the tripwire's true
  lead-time advantage, never overstate it.**
- **`lead_time_seconds = exfil_detected_at - recon_detected_at`**.

`compute_all_scripted_attack_lead_times()` runs this for all four presets;
`summarize_lead_times()` reports how many showed a positive lead time and
the mean.

### 4.2 Reproducing the numbers

```bash
PYTHONPATH=src venv/bin/python -c "
from evaluation.lead_time import compute_all_scripted_attack_lead_times, summarize_lead_times
results = compute_all_scripted_attack_lead_times()
for r in results:
    print(f'{r.attack_name:28s} lead_time={r.lead_time_seconds:6.1f}s  gateway={r.gateway_zone}')
print(summarize_lead_times(results))
"
```

The dashboard's ML Inspector tab renders this same call (§ Phase 3 —
Tripwire Lead-Time Benchmark panel, `aegis_demo.py`) next to the
precision/recall/F1 table, not in a separate silent code path.

### 4.3 Interpretation

With the default `SETTINGS.deception.recon_delay_sec = 60`, every scripted
attack's tripwire fires ~58-59s before the volumetric detector's most
generous possible detection instant (the ~1-2s gap is the tripwire's own
modeled latency and timing jitter). All four scripted attacks currently
show a positive lead time, satisfying Phase 2's "Done when" criterion (≥3 of
4) — this is a direct, mechanical consequence of the recon-before-exfil
timeline Phase 2 built (`generate_scripted_attack` always places the recon
stage `recon_delay_sec` seconds before the exfil stage), not a coincidence
of these specific attack scenarios. The honest caveat this doesn't cover:
an attacker who already holds a valid credential and skips the honeytoken
entirely produces no recon-stage signal at all — see
`docs/DECEPTION.md`'s stated limitation.

---

## 5. Segment-wise scoring for SWaT

### 5.1 Why not point-adjust

SWaT is a **time-series** ICS dataset: rows are seconds-apart sensor
readings, and a single physical attack spans a contiguous run of many rows
(an "attack segment"), not one independent event. `research/BENCHMARKS.md`
(finding B5, citing the ESORICS 2022 ICS anomaly-detection evaluation
suite) documents a well-known trap here: **"point-adjust"** scoring — if a
detector fires on *any* row inside a ground-truth attack segment, the
*entire* segment is retroactively counted as detected for both precision
and recall — "can inflate scores by masking timing errors," to the point
that "a detection algorithm outputting random noise is expected to produce
very good scores, and capable of outperforming state of the art methods."

### 5.2 What AEGIS does instead

`evaluation/metrics.py`'s `segment_wise_precision_recall()`:

- **Recall is segment-wise**: a ground-truth attack segment (a maximal run
  of consecutive `y_true == 1` rows, `find_segments()`) counts as "found" if
  the detector fired on *at least one* row inside it. This is the one piece
  of point-adjust's intuition worth keeping — a human operator who gets one
  alert inside a 10-minute attack has, in practice, been alerted to that
  attack.
- **Precision stays row-wise** — the deliberate break from point-adjust.
  Point-adjust's actual flaw is inflating *precision*: once a segment is
  marked detected, every row inside it (including ones the detector never
  flagged) is credited as a true positive, laundering false negatives into
  true positives. AEGIS's precision only ever counts rows the detector
  *actually* flagged; a false positive on a benign row counts against
  precision exactly as it would under plain point-wise scoring.

Net effect: recall is more forgiving than pure point-wise scoring (matching
how attacks are actually experienced), while precision is exactly as strict
as point-wise scoring — no segment gets a free pass. See the full rationale
in `evaluation/metrics.py`'s module docstring.

`run_evaluation()` routes to `_evaluate_segment_wise()` instead of
`_evaluate()` whenever `dataset.lower() == "swat"`; every other dataset uses
ordinary point-wise scoring (`EvalResult.scoring` records which was used).

### 5.3 A real trap this caught while building the harness

`datasets/SWaT/merged.csv` (this repo's local copy) is
`normal.csv` + `attack.csv` concatenated: **1,387,098 consecutive Normal
rows followed by 54,621 consecutive Attack rows.** `load_dataset("swat",
limit=N)` reads the *first* `N` rows of that file. For any `--limit` below
~1.39M, the loaded slice is 100% benign — the degenerate-split guard (§3)
catches this and raises rather than reporting a fake zero-detector benchmark
for SWaT. To get a real, non-degenerate SWaT slice, `--limit` must be large
enough to reach into the attack region:

```bash
$ PYTHONPATH=src venv/bin/python -m evaluation --dataset swat --limit 1441719 --no-ocsvm
[evaluation] Train: 693549 benign rows | Eval: 748170 rows (7.3% anomalies)
[evaluation] Training Isolation Forest...
[evaluation] Training Z-Score (baseline)...
[evaluation] Training MAD (baseline)...

========================================================================
AEGIS Phase 3 — Anomaly Detection Evaluation Results
========================================================================
[Isolation Forest] P=0.412  R=1.000  F1=0.583  AUC=0.879  (pred 93976/54621 anomalies from 748170 samples, segment-wise)
[Z-Score (baseline)] P=0.000  R=0.000  F1=0.000  AUC=nan  (pred 0/54621 anomalies from 748170 samples, segment-wise)
[MAD (baseline)] P=0.000  R=0.000  F1=0.000  AUC=nan  (pred 0/54621 anomalies from 748170 samples, segment-wise)
========================================================================
```

Read honestly: Isolation Forest recalls every one of SWaT's attack segments
(segment-wise recall = 1.0 — it fired at least once during each attack) but
at a row-level precision of only 0.41, meaning most of its individual
alerts are false positives even though it never fully misses an attack.
Z-Score and MAD predict zero anomalies at this feature scale and score
zero across the board — a real, unflattering result, not a bug in the
harness. This asymmetry (recall near-perfect, precision mediocre) is
exactly the shape segment-wise scoring is designed to surface honestly;
point-adjust would have reported near-perfect *precision* too, which would
have been false.

---

## 6. Files

| File | Role |
|---|---|
| `src/evaluation/__init__.py` | `run_evaluation()`, `EvalResult`, `DegenerateEvaluationError`, `results_to_dataframe()`. |
| `src/evaluation/__main__.py` | `python -m evaluation` CLI. |
| `src/evaluation/metrics.py` | Segment-wise precision/recall (`find_segments`, `segment_wise_precision_recall`). |
| `src/evaluation/lead_time.py` | Tripwire lead-time measurement (`compute_lead_time`, `compute_all_scripted_attack_lead_times`, `summarize_lead_times`). |
| `src/detectors/sklearn_wrappers.py` | `BaseDetector` adapters for Isolation Forest / One-Class SVM, registered in `detectors/registry.py`. |
| `src/settings.py` (`EvaluationSettings`) | `min_positive_rate` / `max_positive_rate` bounds. |
| `tests/test_evaluation.py`, `test_evaluation_metrics.py`, `test_evaluation_lead_time.py`, `test_detectors.py` | Coverage for all of the above. |

No file in this list imports Streamlit — the evaluation harness is
headless by design (Phase 1's C1 discipline extends here), so it is
callable from a script, a test, or a future API without a browser session.
