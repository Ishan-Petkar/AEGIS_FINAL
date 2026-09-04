# AEGIS — Features & Technology Inventory

What's actually built and running, versus what's planned. Code is
authoritative over this document — if the two disagree, trust the code
(see `CLAUDE.md` §8). Compiled 2026-09-03, updated same day after the
Hybrid IDS landed, updated again 2026-09-04 after the IPS layer landed,
and again same day after the T-GNN detector landed — not carried forward
from older planning docs.

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
| pytest + pytest-cov | 720+ tests, coverage |
| eslint (`eslint-config-next`) | Frontend lint |
| Custom AST duplicate-def checker | CI — no function/class defined twice in `src/*.py` |
| `scripts/dev-up.sh` / `dev-down.sh` / `dev-open.sh` | One-command local dev lifecycle |
| `scripts/demo.py` | Terminal-only showcase of all 6 detectors in ~1.5s — no Postgres, no frontend, no model artifact (2026-09-05) |

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

## 3. Hybrid IDS — correlating six detectors into one decision

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
  instance per pipeline, LRU-bounded per-pair history. A fired verdict's
  `evidence["summary"]` (added 2026-09-05, alongside T-GNN's own) states
  the destination and rhythm in one sentence — e.g. *"Periodic connection
  detected to 203.0.113.7:443 (mean interval 60.00s, jitter/CV 0.03)"* —
  next to the raw `cv`/`mean_interval_sec` figures, not instead of them.

**A third new detector, added 2026-09-04**: **T-GNN**
(`backend/detection/tgnn.py`) — graph-structural anomaly detection,
closing the blind spot none of the other five channels cover: an
attacker who is volumetrically quiet, temporally regular, and
rule-compliant, but *topologically* unusual (talking to peers it's never
talked to, concentrating traffic where it shouldn't). Named honestly —
it is "lightweight structural-embedding anomaly detection inspired by
E-GraphSAGE / Anomal-E, using NetworkX + IsolationForest rather than a
full GNN framework," deliberately not PyTorch/PyTorch-Geometric, and it
does not claim to be a real neural network.

Maintains a sliding-window `DiGraph` (nodes = IPs, edges weighted by
flow count/bytes, pruned after `tgnn_window_sec`, default 60s — see
below) as each node's CURRENT state, plus a separate, never-pruned,
LRU-bounded HISTORY of every node's out-neighbors and per-peer byte
totals ever observed, as each node's BASELINE. Per scorable node
(out-degree ≥ `tgnn_min_edges_to_score`), extracts four *self-temporal
drift* features — `unseen_peer_ratio`, `degree_expansion`,
`neighbor_drift` (Jaccard distance), `traffic_entropy_delta` — all
comparing the node's CURRENT window against its OWN history, and scored
by an Isolation Forest fit on a rolling buffer of these feature rows
collected every batch (`tgnn_max_training_rows`). A node with no history
at all (first-ever appearance) falls back to *edge novelty* against a
global "every destination ever seen" set, rather than trivially reading
maximal drift against nothing. A fired verdict's `evidence["summary"]`
(added 2026-09-05) states the finding in one sentence — novel-peer
count and fan-out multiplier, or the cold-start phrasing when there is
no per-node baseline to expand from — next to the raw feature floats,
not instead of them.

This replaced an earlier version (through 2026-09-03) that scored
*pooled global centrality* — weighted in/out-degree, PageRank, clustering
coefficient — fit once across the whole graph. Offline replay against
CIC-IDS2017 friday-morning showed that design's signal was INVERTED:
BENIGN fired at 20.19%, the Bot (Ares C2) label at only 9.00%, because
high centrality is a stable property of infrastructure ROLE (gateways,
DNS servers), not of attack behavior, while the low out-degree (1-2) Ares
bot looked like a textbook inlier in a feature space built entirely from
"how central is this node." The 2026-09-04 pivot to self-temporal drift +
edge novelty, above, measures the same replay at BENIGN 1.67% / Bot
28.48% — signal now correctly ordered and both past their respective
targets (<5% / >25%).

**Per-node history is LRU-bounded too (2026-09-05).** `tgnn_max_nodes`
caps how many NODES are tracked, but said nothing about how large any
one node's own peer history could grow — a long-lived hub surviving a
multi-day run could accumulate an unbounded peer set even with the node
count flat. `tgnn_max_history_peers_per_node` (default 2,000) closes
that: each node's `_history_out_peers` is now itself an LRU map, oldest
peer evicted first. The first value tried (200) silently reopened the
hub-inflation failure the pivot above exists to close — clipping a busy
node's historical degree at 200 when the real friday-morning replay's
busiest node reaches 1,301 inflates `degree_expansion` for perfectly
ordinary hubs, and measurably so: BENIGN firing rose from 1.67% to 9.65%
on the identical replay. 2,000 was chosen by measuring that real maximum
and re-verified to reproduce the exact 1.67%/28.48% figures unchanged.

Calibration is anchored to the fitted forest's own contamination boundary
(`decision_function`'s sign is its inlier/outlier verdict), scaled by the
empirical spread of the training population on each side of that
boundary — not a percentile rank, which is uniform over its reference
population by construction and so fires on a fixed fraction of NORMAL
traffic regardless of the threshold chosen. Certainty is always
`HEURISTIC` — topology drift alone cannot confirm compromise, since it
can equally mean a legitimate failover or a new service deployment.
Abstains (`fired=False`, `calibrated_score=0.0`) until its baseline is
fitted (first `tgnn_baseline_batches` batches, default 10) — and shares
the volumetric channel's known limitation that the baseline assumes
those first batches are benign; a demo that starts with an injection
immediately poisons it. `tests/test_tgnn.py` — 18 tests, all passing.

**Fusion** (`backend/detection/fusion.py`) — confirmed-signal precedence
first (any fired `CONFIRMED` verdict wins outright, `threat_score = 1.0`,
never averaged), otherwise weighted noisy-OR over fired heuristic
verdicts (`1 - Π(1 - score×reliability)`), banded against configured
thresholds. Reliability weights default to each channel's own *measured*
precision from `docs/DETECTION_STUDY.md` (volumetric 0.02, supervised
0.90, tripwire 1.0) and, as of the 2026-09-05 T-GNN self-temporal-drift
pivot, `hybrid_weight_tgnn` too (0.15 — precision = 560/(560+3166) Bot vs.
BENIGN fires on the same friday-morning replay, see that setting's
docstring). Beaconing's weight (0.50) is still an explicitly flagged
unmeasured placeholder, not evidence of quality.

**Shipped posture — observable, not yet authoritative**:
`hybrid_enabled=True` (the layer runs on every batch and persists a
`hybrid` `event_scores` row + an additive `hybrid` key in the WebSocket
envelope), but `hybrid_gates_alerts=False` — it cannot yet create an
alert the existing tripwire/volumetric policy would not have created on
its own. Turning that on is a deliberate future policy change requiring
re-measurement, not a tuning knob. Live-verified: the existing tripwire
alert path (title, severity, debounce, risk index) is byte-for-byte
unchanged with the hybrid layer running underneath it.

**This layer stays detection/advisory-only.** `ResponseAction.THROTTLE`/
`.BLOCK` remain declared in the contract but never produced by the fusion
engine — that has not changed. §5's IPS layer, added 2026-09-04, does now
add active prevention downstream of this one, but as its own action set
(`PreventionAction`) consuming `FusedDecision` read-only, not by finally
using these two reserved-but-unused values.

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

## 5. IPS — prevention layer (`backend/ips/`)

Added 2026-09-04. Sits one step downstream of §3's Hybrid IDS and §4's
CII engine, per the target architecture:

```
Traffic → Hybrid IDS → Detection Fusion → Risk + CII
        → IPS Policy Engine → Prevention Decision
        → Enforcement Adapter → Audit / Persistence / Alert / WS / UI
```

**Consumes, does not re-detect.** `IPSPolicyEngine.decide()`
(`backend/ips/policy.py`) is a pure function of an already-fused
`FusedDecision` plus asset criticality and CII median impact — it never
examines a raw flow or reimplements any of the six Hybrid IDS channels.

**Never blocks on a single weak signal.** Active prevention
(RATE_LIMIT/BLOCK/QUARANTINE) requires EITHER a `Certainty.CONFIRMED`
signal (the honeytoken tripwire, which cannot false-positive) OR at
least `ips_min_corroborating_detectors` (default 2) independently fired
detectors — a lone heuristic detector, however confident, can only ever
reach ALERT. Tier selection above that floor: RATE_LIMIT on any
corroborated signal past its threat-score floor; BLOCK additionally
needs sufficient target-asset criticality; QUARANTINE additionally needs
a real, currently-projected CII blast radius — high criticality alone is
not enough to isolate an asset that has nothing left downstream to
protect right now.

**Own action set**, deliberately not an extension of the pinned
`ResponseAction` enum (`backend/detection/contracts.py` still only ever
emits OBSERVE/ALERT — `THROTTLE`/`BLOCK` stay declared-but-unused there,
exactly as before): `PreventionAction` = observe / alert / rate_limit /
block / quarantine (`backend/ips/contracts.py`).

**Enforcement adapter** (`backend/ips/enforcement.py`) — a `Protocol`
so a future adapter that talks to a real firewall/SDN/security-group API
is a drop-in replacement, no change needed to the policy engine or
`IngestPipeline`. The shipped default, `SimulatedEnforcementAdapter`, is
the honest choice for this environment specifically: AEGIS has no real
network fabric to enforce against (same "no live ingestion... no
production deployment" scope CLAUDE.md §1 already states), so claiming a
real block here would be exactly the kind of overclaim this project's
other honesty trade-offs (§2's real 0.02 precision, §4's median-of-zero
reporting) already refuse to make elsewhere.

**Safety controls** (all requirement-driven, all configurable via
`BACKEND_SETTINGS.ips_*`, `backend/config.py`):
- `ips_enabled` (default **False**) — master switch, off by default
  unlike the Hybrid IDS layer's `hybrid_enabled=True`, since this layer
  can act on a decision (even in simulation) rather than only observe
- `ips_dry_run` (default **True**) — decisions are computed, persisted,
  and broadcast normally, but never mutate the pipeline's own active-
  mitigation state; independent of `ips_enabled`, so a layer can stay
  enabled-but-simulated indefinitely
- TTL/expiry on every active action (`ips_rate_limit_ttl_sec` 15m,
  `ips_block_ttl_sec` 30m, `ips_quarantine_ttl_sec` 1h) — swept once per
  batch against `IngestPipeline`'s in-memory registry (mirrors the CII
  debounce cache's own bounded-`OrderedDict` pattern), auto-expiring to
  `ActionStatus.EXPIRED` with a real rollback call
- Manual unblock/rollback: `POST /api/ips/actions/{id}/rollback` — 404
  unknown id, 409 if the action is not currently active (already
  terminal, or was only ever an ALERT-tier decision with nothing
  enforced to undo)
- Duplicate/conflicting-action protection: a repeat decision on an
  already-actioned asset at the same or lower severity is suppressed,
  never re-persisted; a strictly higher-severity decision supersedes the
  existing row (marked `SUPERSEDED`, not deleted — the audit trail keeps
  every approved decision)
- Fail-open on enforcement failure: an adapter exception is caught,
  logged, and recorded as `ActionStatus.FAILED` — never raised into the
  batch, which would otherwise abort ordinary ingest over an IPS bug

**Audit trail** — every approved decision (ALERT and above; OBSERVE is
never persisted, mirroring how a suppressed volumetric anomaly gets no
`alerts` row either) is a durable `ips_actions` row (`backend/models.py`
`IpsAction`): what (action), why (reason + full evidence snapshot —
threat_score, band, fired detectors, criticality, CII median), target,
timestamp, result (status), and rollback/expiry state. Surfaced via
`GET /api/ips/actions` (filterable by `active`/`target_asset`) and
`GET /api/ips/policy` (the live configured thresholds), broadcast live as
an additive `ips_action` WebSocket envelope, and shown in the console's
new **IPS Prevention** panel (active mitigations, dry-run badge,
confidence, TTL countdown, roll-back control).

**Verified under real load** — driven directly against the real pipeline
(real `StreamingScorer`/`SupervisedFlowScorer`, real signature/beaconing
detectors, real Postgres) with `ips_enabled=True` over 20,000 real
friday-afternoon-portscan flows: 3 approved decisions (2 rate-limit, 1
alert-only — the corroboration floor correctly withheld the other 11
hybrid-likely candidates that only had one detector fire), 11 duplicate
decisions correctly suppressed, 0 failures, full audit trail confirmed
end-to-end through the live REST API including a real rollback (200 →
409 on retry) and dry-run enforcement (0 real state changes recorded).

---

## 6. Real-time Operations Console (backend + frontend)

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

## 7. Frontend UX details

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

## 8. Testing & CI discipline

- **720 passing / 15 skipped** in the default no-DB posture (skips are
  real-dataset / live-DB tests gated on actual data/DB presence, not
  silent failures) — **735 passing / 0 skipped** with a real Postgres
  present, see §11
- ruff clean, zero duplicate top-level definitions across `src/*.py`
  (CI-enforced)
- Every public function follows an optional-override signature
  (`param: T | None = None`, falls back to a typed `SETTINGS` value) —
  no hardcoded thresholds
- "Invariant A": the core `src/` engine is untouched by backend work
  (git-status-verified per ticket), so the Research Console never regresses
  while the Operations Console is built around it

---

## 9. Known, documented limitations (stated on purpose, not hidden)

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

## 10. Planned / not yet implemented

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
  written — now sharper, not settled, now that §3 adds three more
  detector inputs (signature, beaconing, T-GNN) feeding the same fused
  decision. "Three channels, benchmarked" and "six detectors, fused" are
  both currently true statements about different parts of the system and
  need one consistent story in the pitch materials

### Research-grade upgrades (bigger lift, real differentiator)
- [x] ~~Graph neural network for novel-threat detection (Anomal-E /
  E-GraphSAGE style)~~ — **done 2026-09-04**, see §3
  (`backend/detection/tgnn.py`). Shipped as lightweight
  structural-embedding anomaly detection (NetworkX graph features +
  IsolationForest), not a full GNN framework — honestly labeled T-GNN
  rather than claimed as a real neural network. Closes the "topologically
  unusual but volumetrically/temporally unremarkable" gap that
  §3's signature/beaconing detectors don't reach: those catch specific
  known shapes (rules) and one specific temporal pattern (periodicity); a
  graph-structural detector learns peer/concentration structure neither
  can express. Its reliability weight (`hybrid_weight_tgnn`) was measured
  post-pivot at 0.15 precision (see §4 and the setting's own docstring),
  no longer the earlier 0.50 unmeasured placeholder. Its baseline still
  carries the same first-N-batches-are-benign assumption as the
  volumetric channel — a demo that opens with an injection poisons it
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
- [x] ~~A Prometheus-format `/metrics` endpoint exposing the counters
  that already exist internally~~ — **done 2026-09-05**
  (`GET /metrics`, `backend/routes.py`). Hand-rolled text exposition
  (no `prometheus_client` dependency — the format is three lines per
  metric family) over `IngestPipeline.stats()` and `event_scores`
  aggregates; per-detector fire counts are DB-sourced rather than from
  `IngestStats` so all six channels are covered, not only the three
  that counter tracks. Structured JSON logging is still open — the
  natural correlation key (`replay_session_id`) already exists in the
  domain model, so this is a formatter + a `logging.Filter`, not new
  instrumentation.
- [ ] Containerization (`docker-compose.yml` for Postgres + backend +
  frontend) — currently a multi-step manual setup

---

## 11. Where each of these came from

- Hybrid IDS: `backend/detection/` module docstrings; signature engine
  firing-rate measurements (20.0% -> 0.56%) run directly against real
  friday-morning flows during this pass, not estimated
- IPS layer: `backend/ips/` module docstrings; full-suite counts (720
  default posture / 735 with live Postgres) measured directly after this
  pass, both fully green — one pre-existing test in each of
  `tests/test_api.py` (`IngestCountersOut`'s hardcoded field dict) and
  `tests/test_backend_models.py` (the "five tables" assertion) needed
  updating for the new `ips_*` counters and the new `ips_actions` table,
  same class of maintenance CLAUDE.md §8 already documents as expected,
  not a regression
- Detection numbers: `docs/DETECTION_STUDY.md`, `docs/EVALUATION.md`
- CII engine details: `src/cii_calculator.py`, `CLAUDE.md` §4
- Operations Console history: `docs/PHASE5_STATE.md` (per-ticket build log)
- Planned-features research pass: web research conducted 2026-09-03,
  cross-referenced against this project's own measured failure modes
  rather than generic "add more ML" suggestions
