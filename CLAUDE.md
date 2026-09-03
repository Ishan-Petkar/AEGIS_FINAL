# CLAUDE.md — AEGIS

Guidance for Claude Code when working in this repository.

## 1. What this project is

**AEGIS (Anomalous Event Graph Intelligence System)** is a cyber-physical risk
analytics platform for a simulated "smart city" (municipal infrastructure +
municipal financial systems). It answers two questions:

1. **Detection** — *which telemetry events are anomalous?* Unsupervised ML
   (Isolation Forest, with Z-Score / MAD / One-Class SVM baselines) over
   network-flow, financial-transaction, or ICS-sensor data.
2. **Blast radius** — *if this asset is compromised, what else falls over?*
   A **Cascading Impact Index (CII)** computed by Monte Carlo simulation over a
   hand-curated asset dependency graph.

The deliverable is a single-page Streamlit dashboard (`src/aegis_demo.py`) that
lets an operator pick a telemetry source, trigger a scripted attack (or a
"what-if" compromise of any node), and see anomaly scores, the propagation path,
and detector benchmark metrics.

This is a research/demo codebase, not a production security product: there is no
live ingestion, no persistence layer, no auth, and the asset topology is
hardcoded in `src/config.py`.

## 2. Tech stack

Python 3.11–3.13 (CI matrix: 3.11, 3.12; the local `venv/` is 3.13). No
`setup.py` / `pyproject.toml` — the project is run as loose modules with `src/`
on `PYTHONPATH`, not as an installed package.

| Concern | Library |
|---|---|
| Data | `pandas`, `numpy` |
| ML | `scikit-learn` (`IsolationForest`, `OneClassSVM`, `StandardScaler`) |
| Graph | `networkx` (`DiGraph` for CII, `Graph` for the topology plot) |
| Config | `pydantic` v2 (typed, frozen settings models) |
| UI | `streamlit` + `plotly` (`graph_objects` and `express`) |
| Test/lint | `pytest`, `pytest-cov`, `ruff` |

Deps live in `requirements.txt` only. `.env.example` is a placeholder — the
project reads **no** environment variables and needs no secrets.

## 3. Folder structure

```
aegis-project/
├── src/
│   ├── aegis_demo.py           # Streamlit app — the only entry point
│   ├── settings.py             # Pydantic SETTINGS singleton (all tunable numbers)
│   ├── config.py               # Static domain data: assets, dependency graph, CSS
│   ├── data_generator.py       # Synthetic network traffic generator
│   ├── ml_engine.py            # Preprocess / train / score + baseline detectors
│   ├── cii_calculator.py       # Monte Carlo cascading-impact engine
│   ├── evaluation/             # Phase 3 benchmark harness (also a CLI: python -m evaluation)
│   └── datasets/               # Phase 1 ingestion layer
│       ├── schema.py           #   CanonicalEvent + CanonicalBatch
│       ├── asset_registry.py   #   identifier → asset-name resolution
│       ├── loader.py           #   load_dataset() — the ONLY ingestion entry point
│       ├── cic_ids_adapter.py  #   CIC-IDS2017 network flows
│       ├── paysim_adapter.py   #   PaySim financial transactions
│       ├── swat_adapter.py     #   SWaT ICS/OT sensor telemetry
│       ├── download_datasets.py    # helper script (see Known issues)
│       └── generate_paysim_sample.py  # helper script
├── tests/                      # pytest, 88 tests, all currently passing
├── datasets/                   # gitignored — real CSVs live here
├── docs/                       # ARCHITECTURE / DATA_SCHEMA / DESIGN / SETUP / …
├── graphify-out/               # generated knowledge-graph artifacts (not source)
└── .github/workflows/ci.yml    # ruff + duplicate-def check + pytest w/ coverage
```

`venv/`, `datasets/`, `__pycache__/`, `.DS_Store` are gitignored. `datasets.zip`
(~800 MB) is an untracked local archive of the dataset directory — never commit it.

## 4. Architecture and data flow

```
                    ┌─────────────────────────────────────┐
  CIC-IDS2017 CSVs ─┤ cic_ids_adapter.CICIDSAdapter       │
  PaySim CSV       ─┤ paysim_adapter.PaySimAdapter        │──┐
  SWaT CSVs        ─┤ swat_adapter.SWaTAdapter            │  │
  (none)           ─┤ data_generator.generate_mock_*      │  │
                    └─────────────────────────────────────┘  │
                                    ▲                         ▼
                       AssetRegistry.resolve()        CanonicalBatch
                       (IP / account → asset name)    (schema.py, v1.0)
                                                              │
                          datasets.loader.load_dataset(name)  │
                                                              ▼
                    ┌─────────────────────────────────────────────────┐
                    │ ml_engine.preprocess_features → StandardScaler   │
                    │ ml_engine.train_isolation_forest                 │
                    │ ml_engine.compute_anomaly_scores                 │
                    │   → anomaly_score / raw_score / calibrated_score │
                    └─────────────────────────────────────────────────┘
                                                              │
                       anomalous asset name + score           ▼
                    ┌─────────────────────────────────────────────────┐
                    │ cii_calculator.compute_cascading_impact_full     │
                    │   DiGraph from config.DEPENDENCY_GRAPH           │
                    │   N Monte Carlo iterations of probabilistic BFS  │
                    │   → CIIResult(median, p5, p95, impacted, hops)   │
                    └─────────────────────────────────────────────────┘
                                                              │
                                                              ▼
                                          aegis_demo.py — 5 Streamlit tabs
```

### Key invariant: the canonical schema
Every ingestion path normalises into `CanonicalEvent` / `CanonicalBatch`
(`src/datasets/schema.py`, `SCHEMA_VERSION = "2.0"`, 17 columns in
`CANONICAL_COLUMNS` — v2.0 added `signal_type`, `observed_at`, and
`purdue_level` to the original 14). **No downstream component may read a dataset-specific
column directly**, and **nothing should call an adapter directly** — go through
`datasets.loader.load_dataset(name, limit=…)`. Adapters additionally attach
three non-canonical ML feature columns (`duration_sec`, `packets`, `bytes`) that
`CanonicalBatch.validate_schema()` tolerates because it only checks for
*missing* required columns.

Ground truth for evaluation is derived from the canonical `action` column:
`action == ACTION_ALERT` ⇒ anomaly.

### The CII engine (`cii_calculator.py`)
`DEPENDENCY_GRAPH` in `config.py` is a list of edge dicts with `src`, `tgt`,
`edge_type`, `prob`, plus provenance metadata (`source`, `owner`, `rationale`,
`confidence`, `last_reviewed`). Each Monte Carlo iteration runs a BFS from the
compromised node where each edge fires with probability `prob`, capped at
`max_hops`. Edge semantics:

- `depends_on`, `controls`, `communicates_with`, `pays_through` — independent
  sampling per edge.
- `backed_up_by` — redundancy: resolved in a **second pass** after the BFS; the
  target is compromised only if *all* backup paths from compromised predecessors
  fail.
- `shares_provider` — intended as correlated common-mode failure via
  `provider_id`; currently sampled independently (see Known issues).

Per-iteration impact = `anomaly_score × (Σ criticality(compromised nodes) /
Σ criticality(every other node))` — a **fraction of the city's criticality
mass**, so 0.22 reads as "about a fifth of the city falls over" and the
score is comparable across graphs of different sizes. Results are reported
as a **distribution** (median, p5, p95), not a point estimate.

This normalisation replaced an absolute sum clamped at
`SETTINGS.cii.cii_max_value`, which saturated once the topology grew: on
the 50-node city, 18 of 50 origin assets returned exactly the clamp and 28
returned exactly 0, so the operations hub (30 assets impacted) scored
identically to a traffic controller (26 impacted). The same degeneracy was
already latent on the old 16-node graph (6 zeros / 8 clamped / 2 between),
so the scale-up exposed it rather than caused it. `cii_max_value` is now a
safety clamp only — normalised scores cannot exceed 1.0 by construction.

Two correct, different node counts coexist — know which one you're citing.
`config.py`'s raw `SMART_CITY_ASSETS` list is **45 assets / 63 edges**
(`len(SMART_CITY_ASSETS)`, `len(DEPENDENCY_GRAPH)`). The **50-node city**
figure above is the *rendered* graph `GET /api/topology` actually serves:
`graph_manager.py`'s gateway rewrite adds 4 Purdue-zone gateway nodes plus
1 synthesized `City_Grid` node (45 + 4 + 1 = 50) and consolidates/adds
edges (63 → 75). Neither number is stale; they measure different stages
of the same pipeline.

**A median of exactly 0.0 is common and honest**: it means more than half
the Monte Carlo iterations propagated nothing, which is the truth for a
weakly-coupled leaf. The p5/p95 interval carries the tail (an asset may
report median 0.0 with p95 0.185 — "usually nothing, occasionally
moderate"). Read the interval, not just the median.

Two public entry points: `compute_cascading_impact()` returns the legacy
3-tuple; `compute_cascading_impact_full()` returns the full `CIIResult`. The
dashboard uses the `_full` variant.

### Asset resolution (`asset_registry.py`)
`AssetRegistry.resolve(identifier)` → `ResolutionResult(asset_name, criticality,
confidence, is_known)`, tried in order: exact static match (conf 1.0) → PaySim
`C…`/`M…` account-prefix heuristic (conf 0.85) → 10.0.1.x subnet proximity
within 5 host numbers (conf 0.4–1.0) → auto-registered `Unresolved_<id>`
(criticality 0.1, conf 0.3). **Events are never silently dropped.**

### Dashboard tabs (`aegis_demo.py`)
Top-to-bottom procedural script (Streamlit style), not functions:
page config → CSS → `st.session_state` init → sidebar controls → data pipeline →
metric cards → 5 tabs: *Interactive Network Topology*, *Traffic Threat
Analytics*, *Machine Learning Model Inspector* (runs `run_evaluation`),
*Raw Traffic Database*, *System Evolution & Roadmap*.

Session state keys: `active_attack`, `anomalous_asset`, `attack_edges`.
Four scripted attack presets (Payment Gateway Breach, Camera Spoofing, Data
Exfiltration, Lateral Movement) each inject a high-volume synthetic edge and set
the compromised asset; the "What-If" selectbox sets `anomalous_asset` directly.

## 5. Coding patterns and conventions

**Follow these — they are enforced by CI or by explicit design.**

- **No magic numbers.** Every tunable lives in `src/settings.py` as a Pydantic
  field with bounds and a docstring, reachable via the frozen module-level
  singleton `SETTINGS` (`SETTINGS.cii.*`, `SETTINGS.ml.*`, `SETTINGS.data_gen.*`).
  Import `SETTINGS`; never hardcode a threshold, decay rate, or hyperparameter.
- **Optional-override signature.** Public functions take
  `param: T | None = None` and fall back to the `SETTINGS` value inside the
  body. This lets tests and hyperparameter sweeps override without mutating
  global config. Preserve this shape when adding functions.
- **No duplicate definitions.** CI walks the AST of every `src/*.py` and fails
  if any function or class name is defined more than once across the directory.
  Import the canonical implementation instead of re-defining it — see the
  "do NOT redefine these here" comment at the top of `aegis_demo.py`.
- **Flat imports.** Modules import each other by bare name (`from settings
  import SETTINGS`, `from config import DEPENDENCY_GRAPH`) because `src/` is on
  `sys.path`, while the ingestion layer uses the `datasets.` package prefix
  (`from datasets.schema import …`). Match the surrounding style.
- **DataFrames are copied, not mutated.** `ml_engine` functions do
  `df = df.copy()` and return the new frame.
- **sklearn sign convention everywhere.** `predict` → `-1` anomaly / `1` normal;
  `score_samples` / `decision_function` → lower is more anomalous. The custom
  `ZScoreDetector` and `MADDetector` implement this contract deliberately so
  they are drop-in substitutes in `evaluation.py`.
- **Graceful degradation over hard failure.** Missing datasets raise the
  `DatasetNotAvailable` sentinel, which callers catch and fall back to synthetic
  data (`evaluation.run_evaluation`) or a `st.warning` (`aegis_demo.py`). Tests
  `skipTest()` on it rather than failing.
- **Module docstrings** state the file's role and its roadmap phase; sections
  are separated by `# ---- 72-dash rules ----`. Numpydoc-style parameter blocks
  on public functions.
- **Reproducibility.** Seeds default to 42 (`SETTINGS.*.random_state`/
  `random_seed`); sampling calls pass `random_state=42` explicitly.

The codebase is organised by "phases" that appear in docstrings and UI copy:
Phase 0/1 = canonical schema + adapters, Phase 2 = Monte Carlo CII,
Phase 3 = evaluation harness, Phase 4 = uncertainty visibility in the UI.

## 6. Running things

Everything needs `src/` on the path. From the project root:

```bash
streamlit run src/aegis_demo.py
```

```bash
PYTHONPATH=src python -m pytest tests/ -q
```

```bash
PYTHONPATH=src python -m evaluation --dataset swat --limit 20000 --no-ocsvm
```

```bash
ruff check src/ --select E,F,W --ignore E501
```

Datasets are gitignored and must be placed manually under `datasets/`:
`MachineLearningCVE/*.csv` (CIC-IDS2017), `PS_20174392719_1491204439457_log.csv`
(PaySim), `SWaT/merged.csv` or `SWaT/attack.csv`. See `docs/DATASETS.md`.
`datasets.loader.available_datasets()` reports which are present.

Tests skip rather than fail when data is absent, so a green local run does not
prove the adapter paths were exercised — check for `skipped` in the output.

## 7. Known issues (verified in the current tree)

Fix these if you touch the surrounding code; do not assume they are intentional.

- **`src/aegis_demo.py:586`** — `for src, tgt, _, _ in DEPENDENCY_GRAPH:` still
  unpacks the *legacy 4-tuple* format, but `config.DEPENDENCY_GRAPH` is now a
  list of dicts. This raises `ValueError` whenever an attack is active *and*
  `impacted_assets` is non-empty, i.e. the "Cascading Impact Path" overlay is
  broken. Use `entry["src"] / entry["tgt"]`.
- **`src/datasets/download_datasets.py`** — calls `urllib.request.Request` /
  `urlopen` but never imports `urllib.request`; it also disables TLS
  verification (`CERT_NONE`) and the `cic_ids2017_sample` URL points at an
  unrelated NSL-KDD file. The script is effectively dead; prefer manual dataset
  placement.
~~**`cii_calculator._simulate_one_iteration`** — the `shares_provider` branch
  documents correlated common-mode failure but samples independently, identical
  to the default branch. The `provider_id` edge attribute is plumbed through but
  unused.~~ — fixed 2026-09-03 (Phase C methodology-rigor pass): edges sharing a
  `provider_id` now draw one Bernoulli per provider per Monte Carlo iteration
  and reuse that outcome for every edge in the group, instead of sampling each
  edge independently; an edge with no `provider_id` still samples independently.
  Pinned by `tests/test_cii_calculator.py::TestSharesProviderEdge` (4 tests:
  same-provider-id edges never split into a single-node outcome, marginal
  compromise frequency still tracks the configured probability, and two
  control cases — different provider_id, no provider_id — confirm independent
  sampling still occurs where it should). No edge in `config.DEPENDENCY_GRAPH`
  currently uses `shares_provider` (or `backed_up_by`), so this has no effect
  on any currently-published CII number — only on the correctness of the code
  path if the graph grows to use it.
- **`src/config.py` is partly dead.** `PAGE_TITLE`, `PAGE_ICON`, `PAGE_LAYOUT`,
  `CUSTOM_CSS`, and `HEADER_HTML` are duplicated inline in `aegis_demo.py`
  rather than imported. Only `FINANCIAL_TYPES`, `DEPENDENCY_GRAPH`,
  `SMART_CITY_ASSETS`, and `EXTERNAL_THREAT_IPS` are actually consumed.
- **Sidebar "What-If" asset list** (`aegis_demo.py`) offers
  `Central_Bank_Interbank_Feed`, `Power_Substation_Alpha`, and
  `Water_Treatment_Plant`, none of which exist in `DEPENDENCY_GRAPH`. Selecting
  them returns an empty `CIIResult()` (all zeros) rather than an error.
~~`evaluation.run_evaluation` defaults to `dataset="swat"`~~ — fixed; it now
defaults to `"cic_ids2017"` (`src/evaluation/__init__.py:143`), matching its
docstring, printed banners, and `EvalResult.dataset`'s default
(`"CIC-IDS2017"`). ~~The stray `PS_20174392719_1491204439457_log copy.csv`
file~~ has also been removed from `src/datasets/`. Both verified fixed
2026-09-02; kept here (struck through, not deleted) since this file is the
audit trail for what's actually been checked, not just a live TODO list.

## 8. Where to look for more

`docs/SYSTEM_REFERENCE.md` is the intended definitive guide; `docs/ARCHITECTURE.md`,
`docs/DATA_SCHEMA.md`, `docs/DESIGN.md` (the dark-theme design tokens the
dashboard CSS implements), `docs/DATASETS.md` (sources and licences), and
`docs/SETUP.md` / `docs/DEVELOPMENT.md` cover the rest. Treat the code as
authoritative where docs disagree — several docs were rewritten across the phase
migrations and describe the superseded BFS-with-decay CII model.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
