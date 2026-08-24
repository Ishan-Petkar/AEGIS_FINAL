# Phase 5 — Running Implementation State

Updated at each ticket acceptance. Authority: `PLAN_MASTER.md` (what/why) →
`docs/PHASE5_BUILD_PLAN.md` (how) → `docs/PHASE5_RECON.md` (findings, traps,
per-ticket plans). This file is the sprint's status board, not a design doc.

---

## Test status

| Gate | Value | Notes |
|---|---|---|
| Engine regression baseline | **229** | Phases 1–3, must never drop |
| Current suite | **357 passed, 10 skipped** | 229 engine + backend (Tickets #1/#2/#5b/#6); skips are real-dataset tests gated on `datasets/` presence. Ticket #6 (`tests/test_replay_engine.py`) has 34 tests, 0 skipped (its real-data smoke test runs — `datasets/TrafficLabelling ` is present on this machine); +2 over the original 32 from the MEDIUM-1/MEDIUM-2 review-fix pass (batch-cap no-loss/ordering test, cap-active chronological-order regression test) |
| Ruff (`src/ backend/`) | clean | `E,F,W`, ignore `E501` |
| Duplicate-def check (`src/*.py`) | clean | CI parity |
| Invariant A (`src/` untouched) | **holding** | verified per ticket via `git status --short src/` |

---

## Tickets

| # | Ticket | Owner lane | Status |
|---|---|---|---|
| 1 | Postgres + environment/config | A | ✅ **ACCEPTED** |
| 2 | Schema + models + asset seeding | A | ✅ **ACCEPTED** |
| 3 | Next.js scaffold | B | not started |
| 4 | Mock WebSocket server | B | not started |
| 5 | `StreamingScorer` + tests | A | not started |
| **5b** | **`ReplayFlowReader` (real IP/timestamp source)** | A/C | ✅ **ACCEPTED** |
| 6 | Replay engine | C | ✅ **ACCEPTED** — review found 2 MEDIUM findings (unbounded batch size scaling with speed; `emitted_count` not distinguishing consumer failures), both fixed and re-verified (P5-12). `backend/replay_engine.py` + `tests/test_replay_engine.py`; 34/34 tests pass; 357 passed / 10 skipped overall; ruff clean; `src/` untouched |
| 7–21 | — | — | not started |

---

## Environment (provisioned, verified)

- **PostgreSQL 16.15** (Homebrew), running as a service, auto-starts at login
- Role `aegis` / password `aegis` / database `aegis` on `127.0.0.1:5432`
- Binaries: `/opt/homebrew/opt/postgresql@16/bin` (on `PATH` via `~/.zshrc`)
- Verified: TCP connect as `aegis`, plus CREATE/INSERT/SELECT/DROP round-trip
- Disk: ~160 GB free (was 2.5 GB — resolved before Ticket #1)
- Backend deps installed into the existing `venv/`: FastAPI 0.141, SQLAlchemy 2.0.52,
  psycopg 3.3.4, pydantic-settings 2.15, uvicorn, httpx, pytest-asyncio, joblib

---

## Architectural decisions taken during implementation

| # | Decision | Rationale |
|---|---|---|
| P5-1 | `backend/` is a new top-level package, not inside `src/` | Keeps Invariant A mechanically checkable (`git status src/`) and keeps `backend/` outside the CI duplicate-def walk, which only scans `src/*.py` |
| P5-2 | `backend/__init__.py` puts `src/` on `sys.path` once | Matches the repo's existing flat-import convention (no `setup.py`/`pyproject.toml`); lets later tickets `from core.pipeline import run_analysis` without duplicating engine logic |
| P5-3 | `requirements-backend.txt` separate from `requirements.txt` | The engine's dependency set stays untouched; the Research Console keeps working even if backend deps change |
| P5-4 | Config split: `src/settings.py` = engine tuning (no env), `backend/config.py` = deployment (env + `.env`) | Preserves the existing "engine reads no environment" property while giving the backend a conventional 12-factor surface |
| P5-5 | Tiered replay timing: Monday = genuine seconds, Tue–Fri = interpolated minute buckets, every event tagged `timing_provenance` | PCAP replay ruled impractical (see recon §0.5); honours "prefer genuine timing whenever practical" without fabricating precision |
| P5-6 | `api_host` defaults to loopback | API/WebSocket have no auth (out of scope per PLAN_MASTER §15); binding publicly would expose replay + injection control on venue wifi |
| P5-7 | File order is not chronological (Monday 39.41% / Wednesday 23.66% inversions; CICFlowMeter emits in flow-completion order). The reader always sorts by corrected timestamp, stably, so equal timestamps keep original file order | The only defensible within-bucket ordering — minute-granularity files offer no finer ordering signal than file order for ties |
| P5-8 | Landing day is `friday-morning` (1.03% real Bot traffic, realistic); Monday is warmup-only (0.0% attacks, genuine second resolution); Friday-afternoon rejected (55–57% attack traffic is unrealistic) | A landing stream with zero real anomalies undercuts a live demo (Invariant E); 55–57% attacks is implausibly hostile for an operations console |
| P5-9 | `ReplayFlowReader` yields raw IPs and does not resolve assets | Resolution and `/24` clustering belong to Tickets #7/#11 — keeps this reader single-purpose and independently testable |
| P5-10 | `ReplayEngine` (`backend/replay_engine.py`) emits micro-batches (`consumer(batch: list[ReplayFlow], meta: BatchMeta)`), never single flows | Ticket #6 brief's Fact B, measured on this machine: per-event scoring+insert costs 1.78 ms/event against a 0.747 ms/event budget at friday-morning's peak bucket density (4,017 events/3s at 20x) — **2.4x over budget**. Batched scoring (500) + `executemany` insert costs ~0.019 ms/event, ~40x under budget. Batching also caps WebSocket frames/sec for Ticket #9 |
| P5-11 | Within-bucket pacing uses a separate `compute_virtual_times()` interpolation, purely for scheduling; `ReplayFlow.ts` is never mutated | Verified failure mode without this (T3): naive delta-pacing on friday-morning's densest bucket (4,017 events sharing one timestamp) fires all 4,017 in one instant, then idles ~3s. Measured proof (Ticket #6 verification run): naive burst = 4,017 vs. engine's actual max batch size = 134 at the documented speed=20x/tick=100ms. `timing_provenance` (P5-5, `backend/models.py`) remains the sole honest record of which timing tier a row came from; the interpolated virtual time is never written back into `ts` or exposed as an observed time |
| P5-12 | Emission batch size is capped (`BACKEND_SETTINGS.replay_max_batch_size`, default 500); overflow carries forward to the next tick and is never dropped | Review finding (MEDIUM-1): batch size scales linearly with the operator-controllable speed multiplier — `set_speed()` today, `POST /api/replay/speed` in Ticket #8/#13 — so an unbounded cap hands Ticket #7 an unbounded bulk insert and Ticket #9 an unbounded WebSocket frame. Measured speed→batch-size table (friday-morning, 20,000-flow runs): `speed=20x batches=858 max_batch=87` (unaffected by the cap — the demo path stays exactly as it was), `speed=200x batches=89 max_batch=500` (was 860 uncapped), `speed=2000x batches=41 max_batch=500` (was 3,955 uncapped). All three re-verified no-loss (`total_emitted == requested_limit`) and chronologically ordered with the cap active. The resulting schedule slippage is not hidden: at 2000x, `status().lag_seconds` reported 3.24s at completion — the honest trade of bounded frames plus visible lag instead of unbounded frames. `max_batch_size` is an optional-override constructor parameter on `ReplayEngine`, per the CLAUDE.md convention |

Also fixed in the same review pass (MEDIUM-2): `_emit_batch` previously incremented `emitted_count` unconditionally, so `status().emitted_count` did not distinguish flows the consumer successfully processed from flows in a batch whose consumer call raised. `ReplayStatus` now also exposes `consumer_failed_flow_count` (flows, incremented alongside the existing batch-level `consumer_error_count`), with the subtraction relationship (`emitted_count - consumer_failed_flow_count` = successfully processed) made explicit in the docstring.

---

## Known issues (recorded, not blocking)

| ID | Issue | Disposition |
|---|---|---|
| K1 | `_instance: ClassVar` in `backend/config.py` is declared but never used | Mirrors the same dead pattern at `src/settings.py:280`. Consistent with codebase; not worth sprint time. |
| K2 | `model_artifact_path` (unresolved) still exists alongside `model_artifact_path_resolved` | A caller could use the CWD-relative one by mistake. **Ticket #5 must use `model_artifact_path_resolved`.** |
| K3 | `docs/ARCHITECTURE.md` is referenced by the execution directive but was deleted in Phase 3 cleanup (described the superseded pre-gateway BFS design) | Recon §0 records this. Ticket #18 rewrites architecture docs. |
| K4 | `HONEYTOKEN_CREDENTIALS` defines 6 gateway zones (L0–L5) but `graph_manager.gateway_nodes()` materialises only 4 (L1/L3/L4/L5), because Traffic_Cam (0.2) and SCADA_Historian (0.6) fall below the 0.85 protection threshold, so L0/L2 guard nothing | Consequence: the **Camera Spoofing** and **Data Exfiltration** scripted attacks recon against gateways with no graph node and an empty protected set. **Payment Gateway Breach (L4) and Lateral Movement (L3) are unaffected — the headline demo path is sound.** Pre-existing Phase 2 drift; fixing it means touching `src/` (Invariant A), so it is recorded, not silently worked around. Phase 5 should demo the L4 path. |

---

## Timing / demo notes

- friday-morning spans 08:59–12:59 (191,033 rows). At 20× that is ~12 minutes of demo. First real attack lands at 09:34 — about **1.5 minutes into the stream** — and attack density peaks 10:30–11:00 at 4.31%. Useful for Ticket #6 if a start-offset is wanted.
- Full read+sort cost measured: wednesday (692,703 rows) **28.3 s, ~380 MiB**; friday-morning (191k) proportionally less. Ticket #6 must account for this at startup.

---

## Note for Ticket #8 (recorded by Ticket #6)

Because hundreds of events can share one `ts` on minute-granularity days
(friday-morning: median 629 events/bucket, max 4,017), any "recent events"
API query **must order by `ts DESC, id DESC`** — `ts` alone is ambiguous
within a bucket and would make the live feed appear jumbled. The serial
`id` primary key preserves true emission (insert) order within a tied
timestamp, since `ReplayEngine` emits flows within a bucket in the
deterministic order `compute_virtual_times()`/`ReplayFlowReader`'s stable
sort produced (P5-7), and Ticket #7's consumer is expected to insert them
in the order they arrive in each batch.

---

## Live risks carried forward

| ID | Risk | Current mitigation |
|---|---|---|
| T3/T3b | CIC timestamps: per-file precision differs; **all files use a 12-hour clock with no AM/PM**, so a naive sort mis-orders afternoon traffic | Ticket #5b must apply the deterministic AM/PM correction (hours 1–7 → +12) and assert per-file monotonicity in a test |
| T5 | `AssetRegistry` auto-registers one node per unique IP → thousands of nodes | `/24` cluster aggregation must live in the Phase 5 layer (Ticket #11), **not** by modifying `AssetRegistry` (Invariant A) |
| T-B | Accidental model refit in the streaming path | `StreamingScorer` (Ticket #5) must be fit-once/`transform`-only; pin with a test that asserts the scaler is never refit |
| T11 | Disk exhaustion | Resolved for now (~160 GB free); recheck before `node_modules` lands in Ticket #3 |

---

## Ticket #1 — acceptance record

**Verdict: PASS.** Verified independently by Opus, not accepted on the implementer's report.

Delivered: `backend/__init__.py`, `backend/config.py`, `backend/db_check.py`,
`requirements-backend.txt`, `tests/test_backend_config.py` (18 tests);
modified `.env.example`, `.gitignore`. Nothing under `src/`.

Review found 3 defects, all fixed and re-verified:

| Severity | Finding | Verified failure before fix | After fix |
|---|---|---|---|
| **HIGH** | `database_url` did not URL-encode credentials | `AEGIS_DB_PASSWORD='p@ss:w/rd'` → `make_url()` raised `ValueError: invalid literal for int() with base 10: 'w'` | `p@ss:w/rd#x?y` percent-encodes and round-trips to the exact original host/user/password/port/db |
| **HIGH** | `api_host` defaulted to `0.0.0.0` — unauthenticated API + WebSocket, incl. replay/injection control, exposed to the whole LAN | n/a (design defect) | defaults to `127.0.0.1`; `0.0.0.0` documented as deliberate opt-in |
| **MEDIUM** | `model_artifact_path` resolved against CWD | from `/tmp` → `/private/tmp/artifacts/streaming_scorer.joblib` | identical absolute path from repo root and `/tmp` |

Also hardened `db_check._redact` to parse via `make_url(...).render_as_string(hide_password=True)`
instead of a regex — verified no leak for a password containing `@`.

Acceptance criteria:

- [x] Postgres reachable from the backend's own config path (`python -m backend.db_check`)
- [x] Typed, validated, frozen settings singleton; env + `.env` overridable
- [x] Single-URL escape hatch (`AEGIS_DATABASE_URL`) takes precedence, passed through verbatim
- [x] No secrets committed; `.env` gitignored, only `.env.example` tracked
- [x] Backend tests pass with **no live database** (CI has none)
- [x] **247 passed, 0 failed** — engine baseline of 229 intact
- [x] Ruff clean; duplicate-def check clean
- [x] Invariant A: `src/` untouched
