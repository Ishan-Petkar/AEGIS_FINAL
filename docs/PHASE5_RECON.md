# Phase 5 Reconnaissance — Findings and Execution Plan

**Status:** planning artifact, produced before any Ticket #1 code.
**Method:** read PLAN_MASTER.md, docs/PHASE5_BUILD_PLAN.md, CLAUDE.md, docs/DESIGN.md, docs/DATASETS.md, and the Phase 1–3 source under `src/`; verified claims against the actual filesystem and by executing the engine.

---

## 0. Blocking findings (read these first)

Two things in the authoritative plan cannot be built as written. Both are surfaced here rather than silently worked around, per the directive's conflict rule.

### FINDING 1 — CRITICAL: the dataset the plan assumes has no IPs and no timestamps

`docs/PHASE5_BUILD_PLAN.md` §2 specifies the replay engine reads *"real CIC-IDS2017 rows in timestamp order."* Section 10 of the directive requires *"real IP mapping through AssetRegistry"* and *"/24 cluster nodes."*

The existing `CICIDSAdapter` reads `datasets/MachineLearningCVE/`. Verified column inventory: **79 columns, none of which are Source IP, Destination IP, or Timestamp.** It is a pure numeric-flow-feature dataset.

Consequences in the current adapter (`src/datasets/cic_ids_adapter.py`), all verified by reading the code:

| Line | Behaviour | Phase 5 impact |
|---|---|---|
| 260 | `"timestamp": datetime(2017,7,3,9,0,0)` — **one hardcoded constant for every row** | No timestamp ordering is possible. Nothing to pace a 20× replay against. |
| 275 | `"observed_at": None` — never populated | Alert requirement #3 ("WHEN did it happen?") has no real answer. |
| 230–232 | Comment: *"CIC-IDS2017 doesn't include source/destination IPs"* | True of this variant only. |
| 239–251 | Source/dest assets **synthesised from a destination-port heuristic** | No real IPs exist → nothing for `/24` clustering to cluster. |
| 241–244 | `if action == ACTION_ALERT:` → source is set to a hardcoded external threat IP | **The graph topology would be derived from the ground-truth label.** |
| 200–212 | Rows are `.sample()`d | Order is randomised, not chronological. |

Line 241–244 deserves emphasis. It does not corrupt the ML (features are `duration_sec`/`packets`/`bytes`, not the asset name), so Phase 3's metrics remain valid. But if the Phase 5 graph is built from these asset assignments, **the visual topology encodes the answer** — attacker→asset edges exist *because* the row was labelled an attack. That is the same class of circularity Phase 1 removed from `data_generator.py`, and directive §19 forbids fabricated graph relationships.

**Resolution — the data we need is already on disk.** `datasets/TrafficLabelling /` (note the trailing space in the directory name) holds the same 8 capture files with **85 columns including `Flow ID`, `Source IP`, `Source Port`, `Destination IP`, `Destination Port`, `Protocol`, `Timestamp`.**

Verified by sampling 8,000 rows of the PortScan capture:
- Real IPs: 413 unique sources, 608 unique destinations
- Timestamps parse 8000/8000, monotonically increasing in file order, range `2017-07-07 01:00 → 01:19`
- Labels intact (`BENIGN` 7993, `PortScan` 7)
- Encoding is `latin-1`, **not** utf-8 (utf-8 raises)

**Caveat that must shape the design:** timestamp precision is **inconsistent across files**, and there is a clock defect affecting all of them. Fully investigated in §0.5 below — an earlier draft of this document claimed uniform minute-granularity, which was wrong.

**Recommendation:** add a *new, additive* `src/datasets/cic_replay_adapter.py` reading `TrafficLabelling `. Do **not** modify `CICIDSAdapter` — Phase 3's published evaluation numbers were produced with it, and changing it would invalidate them (Invariant A). This is a new ticket, **#5b**, justified in §E.

---

## 0.5 PCAP investigation and the timing decision

Directive: *"investigate whether the original CIC-IDS2017 PCAP captures are available locally or can be obtained from the official CIC/UNB source. Prefer genuine packet/flow timing whenever practical."*

### Is PCAP-based replay practical? **No.** Verdict: fall back — but the fallback is better than first proposed.

Evidence gathered, not assumed:

| Check | Result |
|---|---|
| PCAPs on this machine | **None.** Filesystem search found zero `.pcap`/`.pcapng`/`.pcap.gz`. |
| What we do have | `~/Downloads/CIC Datasets for AEGIS/` — `MachineLearningCSV.zip` and `GeneratedLabelledFlows.zip`. **Both are CSV distributions. Neither contains packets.** |
| Official source | UNB distributes the raw captures separately at ~**50 GB** for the 5-day set. |
| **Free disk space** | **2.5 GiB of 460 GiB — the volume is 100 % full.** |
| PCAP tooling | No `tshark`, no `editcap`, no `capinfos`. Only `tcpdump` (can read, cannot do flow extraction). |

Disk space alone is decisive: a ~50 GB download cannot physically fit, and it is not close. Even with space, PCAP replay would require downloading 50 GB, installing a capture toolchain, re-running CICFlowMeter to regenerate flows with sub-second timing, and re-aligning the regenerated flows against the existing labels — multi-day work, mid-sprint, for a timing refinement. It also cuts against Invariant F (an offline-reliable demo shouldn't depend on a 50 GB fetch).

**Decision: use CSV timestamps. Do not fabricate precision.**

### What the CSV timestamps actually contain (all 8 files audited)

| File | Sample | Seconds? | Hours seen |
|---|---|---|---|
| **Monday-WorkingHours** | `03/07/2017 08:55:58` | **YES** | 1–5, 8–12 |
| Tuesday-WorkingHours | `4/7/2017 8:54` | no | 1–5, 8–12 |
| Wednesday-workingHours | `5/7/2017 8:42` | no | 1–2, 8–12 |
| Thursday-Morning-WebAttacks | `6/7/2017 8:59` | no | 8–12 |
| Thursday-Afternoon-Infilteration | `6/7/2017 1:00` | no | 1–5 |
| Friday-Morning | `7/7/2017 8:59` | no | 8–12 |
| Friday-Afternoon-PortScan | `7/7/2017 1:00` | no | 1–3 |
| Friday-Afternoon-DDos | `7/7/2017 3:30` | no | 3–5 |

Two discoveries that change the design:

**(1) Monday has genuine second-level precision.** Verified against 300,000 rows: **all 60 distinct second values present, near-uniformly distributed** (`05`×6203, `07`×6125, `04`×6111…). Rounded or synthetic data would show a handful of values. This is real capture timing. Monday is also the all-benign baseline day — exactly what the landing experience and the model warmup want.

**(2) Every file uses a 12-hour clock with no AM/PM marker.** No file reports an hour above 12; afternoon captures show 1–5. **A naive chronological sort is therefore wrong** — `1:00` (13:00) would sort before `8:59` (08:59). This must be corrected or Invariant E's "timestamp order" is silently violated.

The correction is deterministic and documentable: hours **1–7 → PM (+12)**, hours **8–12 → AM**, justified by these being known working-hours captures (~08:00–17:00) with morning/afternoon split declared in the filenames. This *recovers* information the CSV encoded lossily; it does not invent any.

### Resulting tiered timing model

| Tier | Source | Timing | Use |
|---|---|---|---|
| **1 — genuine** | Monday | Real, second-resolution capture timestamps | **Default landing stream** + model warmup (all-benign) |
| **2 — interpolated** | Tue–Fri | Minute bucket; a bucket's N events distributed evenly across `60/speed` s | Attack scenarios (this is where the attacks live) |

Every persisted event carries a `timing_provenance` field (`"capture_seconds"` / `"interpolated_minute_bucket"`) so the distinction survives into the database, the API, and the UI. The Tier-2 interpolation preserves *real relative volume* — a genuinely busy minute emits proportionally more events — but the **sub-minute ordering within a bucket is synthetic and must never be presented as original arrival time.**

This satisfies "prefer genuine timing whenever practical": the demo opens on genuinely-timed real traffic, and interpolation is confined to the attack days where the source data has no finer resolution to offer.

### FINDING 2 — MINOR: `docs/ARCHITECTURE.md` no longer exists

The directive lists it as a source of truth. It was deleted earlier in this project (commit "Remove docs describing the superseded pre-gateway CII design") because it documented the pre-gateway BFS-with-decay CII model — superseded by the Monte Carlo engine and gateway topology. Phase 0's own audit had already flagged it as outdated.

**Resolution:** `PLAN_MASTER.md` §Architecture and this document carry that role. Ticket #18 should restore a current `docs/ARCHITECTURE.md`. No action needed now.

---

## A. Current architecture map (as built, verified)

```
datasets/  MachineLearningCVE/ ─► CICIDSAdapter ──┐
           PS_*.csv             ─► PaySimAdapter ──┤
           SWaT/                ─► SWaTAdapter   ──┤
           (none)               ─► data_generator ─┤
           (none)               ─► deception/adapter
                                                    │
                          AssetRegistry.resolve()   ▼
                          (IP/account → asset)   CanonicalBatch
                                                 schema v2.0, 14 cols
                                                 + signal_type, observed_at,
                                                   purdue_level
                                                    │
                        datasets.loader.load_dataset(name)
                                                    ▼
                    ┌───────────────────────────────────────────┐
                    │ core/pipeline.run_analysis()  ← C1         │
                    │  preprocess_features (FITS a scaler)       │
                    │  train_isolation_forest                    │
                    │  compute_anomaly_scores                    │
                    │  TripwireDetector fusion (OR + confidence) │
                    │  → AnalysisResult                          │
                    └───────────────────────────────────────────┘
                                     │
                    graph_manager.build_graph()  ← C2 (sole builder)
                      gateway rewrite: edges into criticality ≥ 0.85
                      re-routed through Gateway_L<purdue>
                                     ▼
                    cii_calculator.compute_cascading_impact_full()
                      Monte Carlo, → CIIResult(median, p5, p95,
                                     impacted_assets, hop_details)
                                     ▼
                    aegis_demo.py (Streamlit, 5 tabs)  +  evaluation/
```

**Measured:** `compute_cascading_impact_full` = **4.5 ms/call** (~220 calls/sec ceiling). Debounce is needed for storms but this is not a hard bottleneck.

**Detector registry** currently holds: `zscore`, `mad`, `tripwire`, plus Isolation Forest / OCSVM via `detectors/sklearn_wrappers.py`.

**Test baseline:** 229 passing, ruff clean, no duplicate top-level defs in `src/*.py`.

---

## B. Phase 5 target architecture

```
datasets/TrafficLabelling /  (real IPs + real timestamps)
        │
        ▼
src/datasets/cic_replay_adapter.py        ← NEW (#5b), additive
        │  canonical events w/ REAL timestamp + REAL src/dst IP
        ▼
backend/replay.py                          ← #6
        │  chronological, minute-bucket interpolated, speed×20
        ▼
backend/ingest.py                          ← #7
        │  StreamingScorer.score_event()   (transform-only)
        │  resolve identity → /24 cluster if unresolved
        │  fuse tripwire (reuse pipeline logic)
        │  CII on anomaly, debounced + cached
        ▼
   ┌────────────┬──────────────────────┐
   ▼            ▼                      ▼
PostgreSQL   WebSocket /ws/stream   REST /api/*
events       {type: event|alert|    topology, events,
event_scores  cii|stats, data:{}}   alerts, cii, stats,
cii_snapshots                       replay ctl, inject
alerts
   │
   ▼
frontend/  Next.js Operations Console
   live feed │ asset graph │ alerts + why + blast radius
```

**Invariant mapping:**
- **B (no refit):** `src/core/streaming.py` — fit once from benign warmup, `joblib`-persist, `transform()` only. The fitted `StandardScaler` already stores `mean_` and `scale_`, so `explain()` derives per-feature σ-deviation directly from it — no extra state, deterministic, reproducible.
- **C (no re-implementation):** ingest calls the existing tripwire/fusion path; it does not re-derive OR-fusion or confidence escalation.
- **D (one graph):** backend serves topology from `graph_manager.build_graph()`. Frontend renders only.
- **E (real landing):** replay of TrafficLabelling is the default startup source.
- **F (offline):** local Postgres, local CSVs, local `joblib` artifact. No network in the demo path.

---

## C. Existing code to reuse (do not re-implement)

| Need | Reuse |
|---|---|
| Graph + gateway topology | `graph_manager.build_graph()`, `gateway_nodes()`, `is_gateway_name()`, `gateway_target_assets()` |
| Blast radius | `cii_calculator.compute_cascading_impact_full()` → `CIIResult` |
| Identity resolution | `AssetRegistry.resolve()` (wrap for clustering, don't edit) |
| Canonical schema | `datasets/schema.py` v2.0, `CANONICAL_COLUMNS` |
| Tripwire | `deception/tripwire.TripwireDetector`, `deception/adapter.generate_tripwire_events()` |
| Attack injection | `data_generator.generate_scripted_attack()` + `ATTACK_RECON_GATEWAY` |
| Model + calibration | `ml_engine.train_isolation_forest`, sigmoid `1/(1+exp(5·raw))` — mirror exactly |
| Config | `settings.SETTINGS`, add a `StreamingSettings` / `BackendSettings` block in the same Pydantic style |
| Asset seed data | `config.SMART_CITY_ASSETS` (has `purdue_level`), `config.HONEYTOKEN_CREDENTIALS` |

---

## D. Do NOT touch (Invariant A)

Freeze list. Any change here requires escalation with justification.

- `src/ml_engine.py` — Phase 3 numbers depend on it
- `src/datasets/cic_ids_adapter.py` — **specifically frozen**; the replay path gets a new module instead
- `src/datasets/{paysim,swat}_adapter.py`, `asset_registry.py`, `schema.py`
- `src/cii_calculator.py`, `src/graph_manager.py`
- `src/deception/*`, `src/detectors/*`
- `src/evaluation/*`
- `src/aegis_demo.py` — remains the Research Console
- All 229 existing tests

Permitted: **additive** `SETTINGS` blocks, **additive** loader registration, new modules.

---

## E. Ticket dependency graph

One new ticket is proposed. Everything else keeps the planned order.

> **NEW #5b — CIC replay adapter (TrafficLabelling).** Required by Finding 1. Without it #6 has no chronological source, #11 has no IPs to cluster, and Invariant E cannot be satisfied. Sits on the critical path immediately before #6. Scope: one new file + tests; no existing file modified except an additive loader entry.

```
TRACK A (backend)     #1 ──► #2 ──────────────► #7 ──► #12 ──► #13 ──► #15
                              ▲                  ▲              └► #14
                      #5 ─────┘                  │       #16 ──► #12
                                            #8, #9 ──────┘
TRACK B (frontend)    #3 ──► #4 ──► #10 ──────► #12
                              └───► #11 ──────► #12
TRACK C (data)        #5b ──► #6 ─────────────► #7

CONVERGENCE           #12 (Day 4, all three)
FINISH                #17, #18, #19 ──► #20 ──► #21
```

Critical path: `#1 → #2 → #7 → #12 → #15 → #20`.
`#4` (mock WS) is the load-bearing unblocker — it must land Day 1 or Track B stalls.

---

## F. Risks and implementation traps

| # | Trap | Why it bites | Mitigation |
|---|---|---|---|
| **T1** | Reusing `preprocess_features()` in the stream path | It calls `fit_transform()`. The baseline silently chases the attack; scores stay plausible and become meaningless. **Nothing visibly breaks.** | `StreamingScorer.transform()` only. Pin with a test asserting `scaler.mean_` is byte-identical before and after scoring an extreme event. |
| **T2** | Graph hairball | `AssetRegistry` mints one `Unresolved_<ip>` node per unique IP; its subnet heuristic only covers `10.0.1.x`, so every real CIC IP (`192.168.10.x`, public) falls through. 413 unique sources in 8k rows → thousands of nodes. | Phase 5 resolution wrapper: if `ResolutionResult.is_known == False`, map to a `/24` cluster node with a count badge. **Wrap, never edit, AssetRegistry.** |
| **T3** | Fake precision in replay pacing | Precision differs per file (Monday has real seconds; the rest are minute-only). Pacing minute-only files purely on deltas emits a whole minute at once, then idles. | Tiered model (§0.5): Monday uses genuine timing; Tue–Fri interpolate within the minute bucket. Tag every event with `timing_provenance`. **Never present interpolated order as original arrival time.** |
| **T3b** | **12-hour clock — silent mis-ordering** | No file marks AM/PM; afternoon rows read `1:00`. A naive sort puts 13:00 *before* 08:59, breaking Invariant E's "timestamp order" with no visible error. | Deterministic correction: hours 1–7 → +12, 8–12 → as-is. Assert monotonicity per file in a test. |
| **T11** | **Disk exhaustion mid-sprint** | **2.5 GiB free of 460 GiB (100 % full).** Postgres data dir, `node_modules` (~300–500 MB), Next.js build cache, and the model artifact all land on this volume. Running out mid-sprint corrupts a Postgres cluster and burns a day. | Free space **before** Ticket #1. The `datasets.zip` archive (~774 MB, already gitignored and redundant with `datasets/`) is the obvious first candidate. Budget ≥ 10 GB headroom. |
| **T4** | CII recalculation storms | Every anomalous event triggering Monte Carlo. 4.5 ms/call → ~220/sec ceiling. | Debounce per origin asset + short TTL cache; reuse the last snapshot within the window. |
| **T5** | Unbounded memory | `AssetRegistry._discovered` and `criticality_map()` grow with every new IP, forever, in a long-running process. | Cap/evict in the Phase 5 wrapper; never let the raw registry back a long-lived stream unbounded. |
| **T6** | `latin-1` encoding | `TrafficLabelling ` CSVs raise on utf-8. Directory name has a **trailing space**. | Read with `encoding="latin-1"`; path-handle the trailing space explicitly. |
| **T7** | Duplicate alerts for one incident | Recon + exfil + N anomalous flows on one asset → alert spam, and it undermines the "detect before exfiltration" narrative. | Alert de-dup/correlation key on (asset, incident window). |
| **T8** | Demo trains at startup | Fitting on stage = dead air. | Warmup fitted at build time, `joblib` artifact committed or generated by a make target; service loads at boot and fails loudly if missing. |
| **T9** | Injection ordering faked | Directive §12: the recon→tripwire→alert→exfil ordering must be real. | Drive it through the existing `generate_scripted_attack()` two-stage timeline; assert `alert.ts < exfil_event.ts` in an integration test. |
| **T10** | CI breaks | CI installs `requirements.txt` on py3.11/3.12 and runs ruff + duplicate-def + pytest. New backend deps could break it. | Keep backend deps in a separate `requirements-backend.txt`, or add and verify CI still passes. Duplicate-def check only scans `src/*.py` top level — `backend/` is unaffected. |

---

## G. Day 0–1 execution plan

**Prereq (blocks #1):** Postgres is **not installed** — verified, no `psql`/`pg_ctl`/`postgres` on PATH and nothing in brew. `brew install postgresql@16` is the first action. Backend Python deps also absent (`fastapi`, `sqlalchemy`, `psycopg` missing; `uvicorn`, `websockets`, `joblib`, `pydantic` already present).

| Order | Ticket | Owner | Output |
|---|---|---|---|
| 1 | **#1** | C | `brew install postgresql@16`, service up, `aegis` DB + role, `.env.example`, `backend/config.py`, `requirements-backend.txt` |
| 2 | **#3** | B | Next.js scaffold + `docs/DESIGN.md` tokens (`#080c14`/`#00d4ff`/`#ff3355`), app shell |
| 3 | **#4** | B | Mock WS server emitting the real envelope shape at a set rate — **unblocks all of Track B** |
| 4 | **#2** | A | SQLAlchemy models, migration/init, seed `assets` from `SMART_CITY_ASSETS` + gateway nodes |
| 5 | **#5** | A | `src/core/streaming.py` + tests (incl. the T1 no-refit assertion) |
| 6 | **#5b** | C | `src/datasets/cic_replay_adapter.py` + tests (real ts/IPs, latin-1, chronological) |

Parallel from the start: C on #1, B on #3→#4, A on #2→#5.

---

## H. Ticket #1 implementation plan

**Objective:** a running local Postgres and a typed, validated configuration surface the backend reads — nothing else.

**Creates:** `backend/__init__.py`, `backend/config.py`, `requirements-backend.txt`, `docs/SETUP_BACKEND.md`, `scripts/init_db.sh`
**Modifies:** `.env.example` (additive), `.gitignore` (add `.env`, `*.joblib`, `frontend/node_modules`, `frontend/.next`)
**Touches no `src/` file.**

**Config contract** (Pydantic `BaseSettings`, mirroring the existing `SETTINGS` style):

| Var | Default | Purpose |
|---|---|---|
| `AEGIS_DB_URL` | `postgresql+psycopg://aegis:aegis@localhost:5432/aegis` | connection |
| `AEGIS_REPLAY_SPEED` | `20.0` | replay multiplier |
| `AEGIS_REPLAY_DATASET_DIR` | `datasets/TrafficLabelling ` | source (note trailing space) |
| `AEGIS_MODEL_PATH` | `artifacts/streaming_scorer.joblib` | warmup artifact |
| `AEGIS_CII_DEBOUNCE_SEC` | `5.0` | T4 |
| `AEGIS_EVENT_RETENTION` | `500000` | row cap |

**Acceptance:**
1. `brew services list` shows postgresql running
2. `psql -d aegis -c "SELECT 1"` returns 1
3. `python -c "from backend.config import settings; print(settings.db_url)"` works from repo root
4. Missing/invalid env → clear Pydantic validation error, not a stack trace at query time
5. No secret committed; `.env` gitignored
6. **229/229 still pass; ruff clean** (regression gate on every ticket)

**Rollback:** delete `backend/`, revert `.gitignore`/`.env.example`. Nothing in `src/` changed, so rollback is total.

**Risks:** brew install may need a password (user action — cannot be done unattended); port 5432 may conflict.

---

## I. Test strategy

**Regression gate (every ticket):** `PYTHONPATH=src pytest tests/ -q` → 229 passing, `ruff check src/ --select E,F,W --ignore E501` clean, duplicate-def check clean. A failure stops work — the test is not edited to make it pass.

**New unit tests:** `StreamingScorer` (fit/save/load round-trip, **no-refit assertion**, `explain()` σ math vs. hand-computed values, calibration parity with `ml_engine`); replay adapter (chronological order, real IP/timestamp presence, latin-1, minute-bucket interpolation); `/24` clustering (public IPs, malformed IPs, IPv6, known-asset passthrough); CII debounce; alert de-dup.

**Integration:** replay→score→persist→broadcast; API→WS fanout with multiple clients; **injection→recon→tripwire→alert→CII with an explicit `alert.ts < exfil.ts` assertion** (T9).

**Resilience:** empty DB; missing dataset; missing model artifact; malformed/duplicate/out-of-order events; WS disconnect→reconnect; backend restart with events surviving; Postgres restart; replay faster than consumer.

**Offline:** full demo path with networking disabled.

---

## J. Definition of "Phase 5 complete"

From a clean environment, the 32-step rehearsal in directive §18 passes end to end, including: real CIC-IDS2017 streaming on landing with **real timestamps and real IPs**; events persisted and surviving restart; readable 30–60 node graph; injection producing a tripwire alert **provably before exfiltration**; explanation numbers traceable to actual scaler baselines; CII blast radius rendered; acknowledgement persisted; Research Console intact; **229/229 engine tests passing**; and the whole demo repeated with the internet disabled.

Plus: no fabricated metrics, detections, explanations, telemetry, graph relationships, lead times, or attack outcomes anywhere in the deliverable.
