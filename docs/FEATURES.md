# AEGIS — Features & Technology Inventory

What's actually built and running, versus what's planned. Code is
authoritative over this document — if the two disagree, trust the code
(see `CLAUDE.md` §8). Compiled 2026-09-03, updated same day after the
Hybrid IDS landed — not carried forward from older planning docs.

---

## 1. Tech stack

### Backend / API (`backend/`)
| Tech | Version | Role |
|---|---|---|
| FastAPI | 0.115+ | REST API + WebSocket server |
| Uvicorn | 0.34+ | ASGI server |
| SQLAlchemy | 2.0+ | ORM (declarative models, typed) |
| PostgreSQL | 16 | Persistence (events, scores, alerts, CII snapshots) |
| psycopg | 3.2+ | Postgres driver |
| Pydantic + pydantic-settings | 2.x | Typed config from env / `.env` |
| joblib | 1.4+ | Model artifact persistence |
| httpx, pytest-asyncio | — | Test client / async test support |

### ML / detection engine (`src/`)
| Tech | Version | Role |
|---|---|---|
| scikit-learn | 1.8+ | IsolationForest, OneClassSVM, StandardScaler, RandomForest |
| pandas / numpy | 3.x / 2.4+ | Data pipeline |
| networkx | 3.6+ | Dependency graph, Monte Carlo traversal |
| Pydantic | 2.x | Frozen `SETTINGS` singleton (no magic numbers) |
| Streamlit + Plotly | 1.57+ / 6.7+ | Research Console (offline benchmark UI) |

### Frontend (`frontend/`)
| Tech | Version | Role |
|---|---|---|
| Next.js | 16.3.3 | App Router, dev server |
| React | 19.2.8 | UI |
| TypeScript | 5.x | Type safety |
| Tailwind CSS | v4 (Lightning CSS) | Styling, design tokens |
| react-force-graph-2d | 1.29+ | Canvas-based topology rendering |
| `ws` | 8.21+ | Dev-only mock WebSocket server |

### Dev tooling
| Tool | Role |
|---|---|
| ruff | Python lint (`src/`, `backend/`) |
| pytest + pytest-cov | 662+ tests, coverage |
| eslint (`eslint-config-next`) | Frontend lint |
| Custom AST duplicate-def checker | CI — no function/class defined twice in `src/*.py` |
| `scripts/dev-up.sh` / `dev-down.sh` / `dev-open.sh` | One-command local dev lifecycle |

---

## 2. Detection — three channels, reported honestly side by side

| Channel | Algorithm | Role | Measured performance |
|---|---|---|---|
| **Unsupervised (novel-threat)** | Isolation Forest (+ Z-Score / MAD / One-Class SVM baselines) | Flags statistical outliers in flow volume, no labels needed | ROC AUC 0.585–0.778, **precision ~0.006–0.02** on real Bot C2 traffic — honestly weak; C2 beacons are *smaller* than benign traffic, so a volume-based outlier detector looks the wrong way |
| **Supervised (known-threat)** | Random Forest, temporal train/test split | Learns the shape of attacks it has seen | **AUC 0.847, precision 0.998, recall 0.585** (honest, temporal split — never same-distribution self-test). Cross-day/novel-family test: **precision 0.000** — catches nothing it wasn't trained on |
| **Honeytoken tripwire** | Deception (planted credential) | Detects any use of a fake credential | **Perfect precision/recall by construction** — a credential with zero legitimate use cannot false-positive, works on the very first sighting of a novel attacker |

This three-channel split is the project's actual research contribution: an
honest, measured demonstration of *why* each paradigm alone is
insufficient, and why the deception layer exists (`docs/DETECTION_STUDY.md`).
A fourth and fifth detector (signature rules, temporal beaconing) now
correlate alongside these three rather than replacing the research
narrative — see §3.

### Evaluation methodology (the part that survives a careful second look)
- Segment-wise recall / row-wise precision for time-series (SWaT) — a
  deliberate rejection of "point-adjust" scoring, which the literature
  shows can make random noise look state-of-the-art
- Degenerate-split guard — refuses to report P/R/F1 on a split with <1%
  or >99% positive rate rather than silently returning meaningless zeros
- Train/eval scaler fit on the training split only (fixed 2026-09-03 —
  previously leaked eval-split statistics into the scaler)
- Cross-day, novel-attack-family test for the supervised channel (not just
  same-distribution self-test)

---

## 3. Hybrid IDS — correlating five detectors into one decision

Added 2026-09-03. Sits alongside the three-channel research narrative in
§2, not in place of it: every existing channel's own verdict feeds into
this layer unchanged, adapted rather than recomputed.

```
ReplayFlow ──► FlowFeatures (ground-truth fields excluded by construction)
                     │
        ┌────────────┼─────────────┬──────────────┬───────────────┐
        ▼            ▼             ▼              ▼               ▼
   volumetric     tripwire     supervised     signature       beaconing
  (adapted)      (adapted)     (adapted)      (new)            (new)
        └────────────┴─────────────┴──────────────┴───────────────┘
                              │
                    HybridFusionEngine
                              │
                        FusedDecision
                              │
              existing risk / CII / alerts / persistence / WS
```

**Contracts** (`backend/detection/contracts.py`) — `FlowFeatures` is a
label-free projection of a flow; it structurally cannot carry
`ReplayFlow.label`/`.is_attack`, so no detector fed through this layer can
leak ground truth even by accident. `Certainty.CONFIRMED` vs `HEURISTIC`
on every verdict is what lets the honeytoken tripwire escalate a fused
decision to `threat_score = 1.0` without being diluted by a
0.02-precision volumetric signal firing alongside it.

**Two new detectors**:
- **Signature engine** (`backend/detection/signature.py`) — 4 declarative
  rules over flow metadata only (this project has flow records, not
  payloads, so it is explicitly not a Snort/Suricata-style payload IDS):
  known-bad address, outbound small-payload-to-high-port (C2-shaped),
  high-risk admin port, external-to-database-port. Measured **0.56%**
  firing rate on 40,000 real friday-morning flows — tuned down from an
  initial 20.0% after finding the predicate matched ordinary service-port
  *responses*, not just client-initiated beacons.
- **Beaconing detector** (`backend/detection/beaconing.py`) — per-`(src,
  dst)` inter-arrival coefficient-of-variation, the direct answer to §2's
  volumetric blind spot (a beacon's signal is timing regularity, which no
  per-flow volumetric feature can carry). Stateful — one long-lived
  instance per pipeline, LRU-bounded per-pair history.

**Fusion** (`backend/detection/fusion.py`) — confirmed-signal precedence
first (any fired `CONFIRMED` verdict wins outright, `threat_score = 1.0`,
never averaged), otherwise weighted noisy-OR over fired heuristic
verdicts (`1 - Π(1 - score×reliability)`), banded against configured
thresholds. Reliability weights default to each channel's own *measured*
precision from `docs/DETECTION_STUDY.md` (volumetric 0.02, supervised
0.90, tripwire 1.0) — beaconing's weight (0.50) is explicitly flagged as
an unmeasured placeholder, not evidence of quality.

**Shipped posture — observable, not yet authoritative**:
`hybrid_enabled=True` (the layer runs on every batch and persists a
`hybrid` `event_scores` row + an additive `hybrid` key in the WebSocket
envelope), but `hybrid_gates_alerts=False` — it cannot yet create an
alert the existing tripwire/volumetric policy would not have created on
its own. Turning that on is a deliberate future policy change requiring
re-measurement, not a tuning knob. Live-verified: the existing tripwire
alert path (title, severity, debounce, risk index) is byte-for-byte
unchanged with the hybrid layer running underneath it.

**Deferred for IPS**: `ResponseAction.THROTTLE`/`.BLOCK` are declared in
the contract but never produced by the fusion engine and nothing consumes
them yet — this is detection and advisory alerting only, no active
prevention.

---

## 4. Cascading Impact Index (CII) — blast-radius engine

- **Monte Carlo simulation** over a hand-curated 45-asset / 63-edge
  dependency graph (rendered as 50 nodes / 75 edges after gateway
  synthesis)
- Edge semantics: `depends_on`, `controls`, `communicates_with`,
  `pays_through` (independent sampling), `backed_up_by` (redundancy — all
  backup paths must fail), `shares_provider` (**correlated common-mode
  failure** — fixed 2026-09-03, now draws one shared outcome per provider
  per iteration instead of sampling independently)
- Output is a **distribution** (median, p5, p95), never a point estimate —
  a median of 0.0 with p95 0.185 is reported honestly as "usually
  nothing, occasionally moderate"
- Normalized as a *fraction of the city's total criticality mass*, so
  scores are comparable across graphs of different sizes and don't
  saturate as the topology grows
- Mandatory access gateway model (Purdue-zone gateways) — a compromise
  must pass through instrumented gateway nodes, which are pinned to
  near-zero criticality so they can never inflate a score

---

## 5. Real-time Operations Console (backend + frontend)

### Data pipeline
- **Real captured traffic replay** — CIC-IDS2017 (2017 network capture,
  8 days, 3.2 GB), chronologically replayed at operator-selectable speed
  (1x/5x/20x/60x), never synthetic/generated data in the live path
- Tiered timing fidelity: genuine second-level timestamps where the
  capture supports it, honestly-tagged interpolated minute-buckets where
  it doesn't (`timing_provenance` column — never presented as more
  precise than it is)
- Micro-batched ingest (score → resolve asset → persist → CII → alert →
  broadcast) — batching was required to hit real-time throughput
  (measured: per-event insert cost was 2.4x over budget; batched cost is
  ~40x under budget)
- Asset resolution with graceful degradation: exact match → PaySim
  account-prefix heuristic → subnet proximity → auto-registered
  "unresolved" — no event is ever silently dropped

### Live streaming
- WebSocket broadcast (`/ws/stream`) with per-client bounded queues
  (drop-oldest on overflow, counted) — a slow browser tab can never
  become the replay engine's rate limiter
- Automatic reconnect with exponential backoff, **and** gap-free
  backfill via `GET /api/events?since=<id>` on reconnect
- Manual "restart stream" control (↻) — force-reconnects this browser
  tab's socket, for when the feed looks stuck but hasn't actually dropped

### Interactive city graph
- Two genuinely separate, honestly-labeled layers: the curated 50-asset
  city model, and observed `/24` traffic clusters from live capture IPs —
  **no invented edges** between them (the two data sources don't overlap,
  and the UI says so rather than fabricating a connection)
- **Stackable sector focus** — click multiple sector chips to expand
  several at once (Energy + Finance simultaneously, etc.), not a
  single-select toggle
- CII cascade animation driven strictly by the real Monte Carlo `impacted`
  array — no scripted path
- Maximise/collapse toggle for full-window viewing during a demo

### Operator controls (all live-verified end to end)
- **Replay speed** — `POST /api/replay/speed`
- **Inject** — replay *real* captured attack traffic (Bot C2, DDoS,
  PortScan, honeytoken-flagged) re-targeted onto any curated asset, for
  live what-if scenarios without fabricating flow data
- **Alert acknowledge** — clears an alert, visibly drops the risk index
- **Restart stream (↻)** — reconnects this tab's WebSocket
- **Restart replay** — rewinds the actual replay data to the top of the
  day (added 2026-09-03; distinct from ↻ — fixes "day ran to completion,
  Inject now 409s" rather than a stuck socket)

### Alerting policy
- Tripwire hits **always** alert (severity critical)
- Volumetric-only anomalies are suppressed by default (~0.02 precision
  would produce ~800 junk alerts per replay day and bury the one signal
  that matters) — still scored, persisted, and visible in the feed, just
  not paged
- Per-asset debounce window on both channels

### Security (Phase B)
- Optional bearer-token auth on all state-changing routes (`POST`), GET
  routes stay open for monitoring — off by default, on via env var
- Per-IP rate limiting (token bucket) on mutating routes
- Neither is a full auth system — deliberately the smallest viable fix for
  "don't let an open write surface get flooded," not enterprise auth

### Data retention
- Row-count cap (default 500k events) — always on
- **Age-based retention** (added 2026-09-03) — optional `max_age_days`,
  additive with the row cap (a row is deleted if it violates *either*
  bound), off by default to preserve existing behavior

---

## 6. Frontend UX details

- Freeze-on-hover/scroll telemetry feed with an honest "Paused · N new"
  badge — display only, the stream keeps receiving underneath
- Contradiction-proof connection state — one shared `StreamProvider`
  instance so the header counters and the feed rows can never show two
  different "connected" states from two different sockets
- Full error/retry states for every panel (backend down → visible error +
  Retry button, not an infinite spinner)
- **Request timeout** (added 2026-09-03, 15s) — a wedged/hung backend now
  fails over to a retryable error instead of leaving every panel loading
  forever with no way to recover except a page reload
- Dark, glass-panel visual design system with light/dark-aware tokens

---

## 7. Testing & CI discipline

- **662 passing / 15 skipped** in the default no-DB posture (skips are
  real-dataset / live-DB tests gated on actual data/DB presence, not
  silent failures) — **677 passing / 0 skipped** with a real Postgres
  present, see §10
- ruff clean, zero duplicate top-level definitions across `src/*.py`
  (CI-enforced)
- Every public function follows an optional-override signature
  (`param: T | None = None`, falls back to a typed `SETTINGS` value) —
  no hardcoded thresholds
- "Invariant A": the core `src/` engine is untouched by backend work
  (git-status-verified per ticket), so the Research Console never regresses
  while the Operations Console is built around it

---

## 8. Known, documented limitations (stated on purpose, not hidden)

- Unsupervised detector: ~0.02 precision on real traffic — a genuine,
  published finding, not a bug to be quietly patched over
- Supervised detector: 0.000 precision on attack families it wasn't
  trained on
- Only 2 of 4 scripted-attack demo gateways currently guard a real
  protected asset in the graph
- No containerization, no message queue, no CI/CD beyond lint+test, no
  monitoring stack, no multi-tenant support — single-process, single-city
  by design at this stage
- `events.source_asset` / `destination_asset` are plain strings, not
  foreign keys (deliberate denormalization for telemetry-log resilience)
- Scalability beyond one node is unmeasured and out of scope so far

---

## 9. Planned / not yet implemented

Ordered by how soon each would matter, not by difficulty.

### Near-term (would harden the current demo)
- [x] ~~Temporal/beaconing features for the unsupervised channel~~ —
  **done 2026-09-03**, see §3 (`backend/detection/beaconing.py`)
- [ ] Turn `hybrid_gates_alerts` on (currently `False` — the hybrid layer
  observes and persists but cannot yet create an alert on its own
  authority). Requires re-measuring precision/recall with the widened
  gate before flipping the default, per that setting's own docstring
- [ ] Fix the 2-of-4 scripted-attack gateway coverage gap (Camera
  Spoofing / Data Exfiltration currently recon against a gateway with no
  materialized protected asset)
- [ ] Resolve/clarify the "three channels" framing everywhere it's
  written — now sharper, not settled, now that §3 adds two more detector
  inputs (signature, beaconing) feeding the same fused decision. "Three
  channels, benchmarked" and "five detectors, fused" are both currently
  true statements about different parts of the system and need one
  consistent story in the pitch materials

### Research-grade upgrades (bigger lift, real differentiator)
- [ ] **Graph neural network** for novel-threat detection (Anomal-E /
  E-GraphSAGE style) — self-supervised, edge-aware, no labels required;
  would also give the "observed traffic doesn't map onto curated assets"
  gap a principled learned-topology answer instead of two disconnected
  layers. Complementary to, not superseded by, §3's signature/beaconing
  detectors: those catch specific known shapes (rules) and one specific
  temporal pattern (periodicity); a GNN would learn structure a
  hand-written rule or a single-pair statistic cannot express
- [ ] **Learned edge probabilities** for the CII dependency graph (Bayesian
  attack graph posterior updates from observed telemetry) instead of the
  current hand-assigned static probabilities
- [ ] Provenance-graph / MITRE ATT&CK-mapped behavioral baselining for the
  stated blind spot: an attacker already holding valid credentials, who
  never touches the honeytoken and doesn't look anomalous by volume —
  invisible to all three current channels. Needs weeks of baseline data
  CIC-IDS2017 alone can't provide; a real, not a quick, gap
- [ ] Open-set / zero-day generalization testing (leave-one-attack-family-out
  validation) as a standard part of the evaluation harness, not just a
  documented cross-day finding

### Path to production (explicitly deferred, not urgent)
- [ ] Multi-tenant / multi-city support (keyed registry of replay engines,
  pub/sub broadcaster instead of in-process)
- [ ] Alembic migrations (currently `Base.metadata.create_all()`)
- [ ] Structured (JSON) logging + a Prometheus-format `/metrics` endpoint
  exposing the counters that already exist internally
- [ ] Containerization (`docker-compose.yml` for Postgres + backend +
  frontend) — currently a multi-step manual setup

---

## 10. Where each of these came from

- Hybrid IDS: `backend/detection/` module docstrings; signature engine
  firing-rate measurements (20.0% -> 0.56%) run directly against real
  friday-morning flows during this pass, not estimated; full-suite counts
  (662 default posture / 677 with live Postgres) verified twice — the
  first live-DB run surfaced one more pre-existing row-count assumption
  (test_live_roundtrip_persists_events_and_scores, the same class of
  issue as 4 default-posture tests fixed earlier in this pass) that the
  default-posture run alone could not have caught, since that test is
  gated on AEGIS_TEST_LIVE_DB=1
- Detection numbers: `docs/DETECTION_STUDY.md`, `docs/EVALUATION.md`
- CII engine details: `src/cii_calculator.py`, `CLAUDE.md` §4
- Operations Console history: `docs/PHASE5_STATE.md` (per-ticket build log)
- Planned-features research pass: web research conducted 2026-09-03,
  cross-referenced against this project's own measured failure modes
  rather than generic "add more ML" suggestions
