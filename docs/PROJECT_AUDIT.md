# AEGIS — Engineering & Architecture Audit

Anomalous Event Graph Intelligence System — a cyber-physical risk analytics
platform for a simulated smart city. This review covers the full tree: the
FastAPI/Postgres backend, the Streamlit research engine, the Next.js
operations console, and CI.

Markdown transcription of the audit originally published as an HTML
artifact on 2026-09-05. Content, figures, and findings are unchanged from
that version — this is a format conversion, not a re-audit — so every
number below is "as of" the commit named here, not the current tip of the
branch.

| | |
|---|---|
| **Repository** | AEGIS_FINAL |
| **Branch reviewed** | FINAL-FINAL-BEST |
| **Commit** | `eb920b8` |
| **Date** | 2026-09-05 |

## Topline verdict

| | |
|---|---|
| **Test suite** | 738 / 15 passed / skipped, 0 failed — `pytest tests/ -q`. The 15 skips are the live-Postgres suite, gated on `AEGIS_TEST_LIVE_DB=1`. |
| **Lint (CI gate)** | Clean — `ruff check src/ backend/ --select E,F,W --ignore E501`, zero findings. |
| **Detection channels** | 6 → 1 — six independent detectors correlated into one fused decision by `HybridFusionEngine`. |
| **CII graph** | 45/63 assets / edges (raw) — 50 nodes / 75 edges once the mandatory gateway rewrite runs. Both figures independently re-counted for this audit. |
| **IPS posture** | Off — `ips_enabled=False`, `ips_dry_run=True` — advisory only, by design, out of the box. |
| **T-GNN signal, re-measured** | 1.7% / 28.5% — BENIGN / Bot firing rate on CIC-IDS2017 friday-morning, post-pivot — was 20.2% / 9.0% (inverted) one commit ago. Detail in §4. |

## Contents

1. [Scope & method](#01-scope--method)
2. [System overview](#02-system-overview)
3. [Detection layer](#03-detection-layer)
4. [T-GNN: a worked case study](#04-t-gnn-a-worked-case-study)
5. [Blast-radius engine (CII)](#05-blast-radius-engine-cii)
6. [Prevention layer (IPS)](#06-prevention-layer-ips)
7. [Operations console](#07-operations-console)
8. [CI & test discipline](#08-ci--test-discipline)
9. [Findings register](#09-findings-register)
10. [Recommendations](#10-recommendations)
11. [Appendix: stack & sources](#11-appendix-stack--sources)

---

## 01. Scope & method

A static, evidence-first review of the repository as committed at
`eb920b8` — not a penetration test, and not a load test.

Every claim below was checked against the tree rather than transcribed
from existing docs: line counts and settings defaults were read directly
from source, the CII graph's node/edge counts were produced by importing
`graph_manager.build_graph()` and counting, the fusion engine's branch
logic was read out of `fusion.py` rather than assumed from its docstring,
and the test/lint figures are from runs executed this session, not a
cached CI badge. Where code and documentation disagreed, the code was
treated as ground truth — the project's own stated convention (`CLAUDE.md`
§8) — and the disagreement is logged as a finding in §9 rather than
silently resolved.

Out of scope: the live Postgres-backed test path (requires
`AEGIS_TEST_LIVE_DB=1` against a real database), frontend visual/interaction
QA, and anything requiring a deployed instance.

## 02. System overview

One replay pipeline feeds two downstream consumers: a detection-and-fusion
path that produces the live operator view, and a blast-radius simulation
keyed off whichever asset the fusion layer names as compromised.

Raw CIC-IDS2017 capture files are parsed into a chronologically-ordered
flow stream (`backend/replay_reader.py`, 625 lines — it corrects a
12-hour clock with no AM/PM marker and re-sorts because CICFlowMeter emits
rows in completion order, not start order), then paced into batches in
real time or at a speed multiplier (`backend/replay_engine.py`, 795
lines). Each batch passes through `IngestPipeline`
(`backend/ingest.py`, 2,111 lines), which runs all six detectors, fuses
their verdicts, persists the result, and broadcasts it.

```
CIC-IDS2017     ReplayFlow-     ReplayEngine     IngestPipeline      HybridFusion-
capture CSVs ─► Reader      ─► (paced batches)─► (6 detectors   ─►  Engine
                                                   → verdicts)            │
      rows           batches          flows            verdicts×6        │
                                                                          │
        ┌─────────────────────┬──────────────────────┬──────────────────┤
        ▼                     ▼                       ▼          if asset
   Postgres              WebSocket               IPSPolicyEngine  named
   event_scores      → Operations Console        (dry-run by       anomalous
   (persist)              (broadcast)              default)             ▼
                                                  (policy check)  CII Monte Carlo
                                                                  (blast-radius, §5)
```

*Fig. 1 — one fused decision feeds four independent consumers: durable
storage, the live console, the (disabled-by-default) prevention layer, and
the blast-radius simulation.*

## 03. Detection layer

Six detectors, three of them measured against real attack traffic and
three still carrying unmeasured placeholder weights, correlated by one
fusion rule that refuses to average away a confirmed signal.

| Channel | Mechanism | Reliability | Certainty | Tests |
|---|---|---|---|---|
| Volumetric | Isolation Forest, 3 flow-volume features | 0.02 | Heuristic | `test_streaming_scorer.py` |
| Supervised | RandomForest, trained on labelled attacks | 0.90 | Heuristic | `test_detectors.py` |
| Tripwire | Honeytoken credential touch | 1.00 | Confirmed | `test_deception_tripwire.py` |
| Signature | 4 declarative metadata rules | 0.85 | Heuristic | `test_signature.py` |
| Beaconing | Inter-arrival coefficient of variation | 0.50† | Heuristic | `test_beaconing.py` |
| T-GNN | Self-temporal graph drift + edge novelty | 0.50† | Heuristic | `test_tgnn.py` |

† Both flagged as unmeasured placeholders in `docs/FEATURES.md` §3, not
evidence of quality — see §10.

The channels are not peers, and the fusion rule is built around that fact
rather than around it: the tripwire's precision is zero-by-construction (a
honeytoken credential has no legitimate use), while the volumetric channel
runs at roughly 2% precision on the same traffic. Averaging those two
would let hundreds of junk volumetric signals drag a confirmed compromise
back toward "maybe." `backend/detection/fusion.py:183` avoids that with a
precedence rule, not a weight: any fired `Certainty.CONFIRMED` verdict
short-circuits the decision outright.

```
Volumetric  Supervised  Tripwire  Signature  Beaconing  T-GNN
     └───────────┴──────────┴─────────┴──────────┴─────────┘
                             │
                Any fired verdict CONFIRMED?
                    ┌────yes────┴────no────┐
                    ▼                       ▼
        threat_score = 1.0        weighted noisy-OR over
        (escalate — never          fired heuristics only:
           averaged)          1 − Π(1 − scoreᵢ·reliabilityᵢ)
                    └───────────┬───────────┘
                                 ▼
                          Band → Action
```

*Fig. 2 — `HybridFusionEngine.fuse()`: a confirmed tripwire hit bypasses
the noisy-OR entirely rather than being blended in at weight 1.0, which
would still let it be pulled down by simultaneous noise.*

## 04. T-GNN: a worked case study

Included because it is the most recent, best-documented example of the
review process this audit is applying to everything else: a measured
claim, an inverted result, a diagnosed cause, and a re-measured fix.

The channel's first shipped version scored *pooled global centrality* —
weighted degree and PageRank — fit once across the whole traffic graph.
Replayed against CIC-IDS2017 friday-morning, that design's signal was
backwards:

| Label | Before (centrality) | After (temporal drift) | Target |
|---|---|---|---|
| BENIGN firing rate | 20.19% | **1.67%** | < 5% |
| Bot (Ares C2) firing rate | 9.00% | **28.48%** | > 25% |

The cause traced to what the features actually measured: high centrality
is a stable property of infrastructure *role* (a gateway or DNS server is
supposed to be a hub), not of attack behavior — so every legitimate hub
inflated the benign rate. Meanwhile the Ares bot's out-degree of 1–2
looked like a textbook inlier in a feature space built entirely from "how
central is this node." A quiet, low-degree attacker is invisible to a
detector that only asks whether a node is unusually connected.

The fix replaced pooled centrality with four features that compare a node
only to its own history — `unseen_peer_ratio`, `degree_expansion`,
`neighbor_drift` (Jaccard distance), `traffic_entropy_delta` — with a
global edge-novelty fallback for nodes with no history at all. The harder
problem this created (every such delta feature is trivially zero at the
instant a snapshot is compared to itself, so an Isolation Forest fit on
one snapshot cannot split on any of them) was solved by accumulating one
feature row per scorable node on *every* batch, starting before the
baseline is even considered ready, into a rolling training buffer — real
temporal spread instead of a single degenerate instant.

> **Why this belongs in an architecture audit.** Nothing here was a coding
> mistake — the original design compiled, ran, and had passing tests. The
> defect was a measurement one: the feature set was never checked against
> ground truth before being called done. `tests/test_tgnn.py` now encodes
> the specific failure modes directly (a stable high-degree hub must not
> fire; a low-degree node touching one brand-new peer must) rather than
> only checking that scores land in `[0,1]`.

## 05. Blast-radius engine (CII)

A Monte Carlo simulation over a hand-curated dependency graph, reported as
a distribution rather than a point estimate.

`src/cii_calculator.py` runs probabilistic BFS from a compromised asset
over a `networkx.DiGraph` built from `src/config.py`'s `DEPENDENCY_GRAPH`
— independently re-counted for this audit at **45 assets / 63 edges** raw.
`graph_manager.build_graph()` then rewrites every path to a
high-criticality asset through a mandatory Purdue-zone gateway, adding
gateway and grid nodes; re-running it for this audit confirmed the
rendered graph at exactly **50 nodes / 75 edges**, matching what the API
actually serves.

Impact is reported as a *fraction of the city's total criticality mass*
(median, p5, p95 across iterations), a deliberate replacement for an
earlier absolute-sum design that saturated at scale: on the 50-node graph,
that older scheme returned the clamp value for 18 of 50 origin assets and
exactly zero for 28 more, meaning a 30-asset cascade and a 26-asset
cascade scored identically. A median of 0.0 is common under the current
scheme and is treated as an honest result — most Monte Carlo iterations
propagating nothing is the correct answer for a weakly-coupled leaf node,
and the p5/p95 interval is where a rare severe tail shows up.

## 06. Prevention layer (IPS)

Built, tested, and switched off — the safer of the two possible defaults
for a system that has never seen production traffic.

`backend/ips/policy.py`'s `IPSPolicyEngine.decide()` is a pure function of
the fused decision plus asset criticality and CII median — it never
re-examines raw flows. It only recommends an active tier (rate-limit,
block, quarantine) when the fused decision carries a `Certainty.CONFIRMED`
verdict, or when at least `ips_min_corroborating_detectors` (default 2)
heuristic detectors fired together. Enforcement itself runs through
`SimulatedEnforcementAdapter` — there is no real network fabric behind it,
which is the honest state to be in rather than a gap to hide.

Two independent switches keep this observe-only out of the box:
`ips_enabled=False` (the layer doesn't run at all) and, one level up,
`hybrid_gates_alerts=False` (even the underlying Hybrid IDS fusion result
cannot yet raise an alert on its own authority — it observes and
persists). Both would need to flip, deliberately and separately, before
this system could act on anything.

## 07. Operations console

A Next.js 16 App Router frontend consuming one shared live stream, not one
per component.

The console (`frontend/src/app/page.tsx`, with `DetectionPreventionPanel.tsx`,
`SeverityGlyph.tsx`, `TelemetryRail.tsx` as its main working surfaces)
reads live events over a single WebSocket connection wrapped by
`stream-context.tsx` — a fix for an earlier state where each component
that wanted live data opened its own socket. REST endpoints
(`/api/topology`, `/api/events`, `/api/alerts`, `/api/ips/actions`) exist
for backfill on reconnect, keyed by a `since=` event id, and for actions
like acknowledging an alert — the WebSocket is the push channel, REST is
the catch-up and control-plane path.

## 08. CI & test discipline

33 test files, 12,539 lines, and one lint gate that quietly doesn't cover
the package it should.

`.github/workflows/ci.yml` runs on Python 3.11 and 3.12: install both
requirement files, `ruff check src/ backend/ --select E,F,W --ignore
E501`, a custom AST walk that fails the build on any duplicate
function/class definition, then `pytest tests/ --cov=src ...`. Coverage is
generated and uploaded as an artifact but nothing fails the build on a
coverage regression — there is no `--cov-fail-under`.

The duplicate-definition check — the guard behind this project's stated
"no duplicate definitions" convention — walks `src/*.py` only. `backend/`
is now the larger of the two Python packages (`ingest.py` alone is 2,111
lines) and has no equivalent guard; see F-6 below.

## 09. Findings register

Eight items. Six open, all low-to-medium severity and each independently
reproducible; one resolved and verified this session; one already known
and published rather than hidden.

| ID | Location | Severity | Finding | Status |
|---|---|---|---|---|
| F-1 | `src/aegis_demo.py:586` | Medium | Unpacks the legacy 4-tuple edge format; `DEPENDENCY_GRAPH` is now a list of dicts. Raises whenever an attack is active and `impacted_assets` is non-empty — the Cascading Impact Path overlay is broken exactly when it matters most. | Open |
| F-2 | `src/datasets/download_datasets.py` | Medium | Missing `urllib.request` import, TLS verification disabled (`CERT_NONE`), and the sample URL points at an unrelated dataset. Effectively dead code. | Open — manual dataset placement is the documented workaround |
| F-3 | `src/config.py` | Low | Page-chrome constants (`PAGE_TITLE`, `CUSTOM_CSS`, …) are dead — duplicated inline in `aegis_demo.py` instead of imported from here. | Open |
| F-4 | `aegis_demo.py` (sidebar) | Low | The "What-If" asset dropdown offers three names absent from `DEPENDENCY_GRAPH`; selecting one silently returns an all-zero `CIIResult()` rather than an error. | Open |
| F-5 | `CLAUDE.md` §8 | Low · doc | Cites `docs/ARCHITECTURE.md`, which does not exist anywhere in the tree. Confirmed by directory listing. | Open |
| F-6 | `.github/workflows/ci.yml` | Medium | The duplicate-definition guard scans `src/*.py` only. `backend/` — now the larger, actively-growing package — has no equivalent check. | Open |
| F-7 | `.github/workflows/ci.yml` | Low | Coverage is measured and reported but not gated — no `--cov-fail-under`. May be intentional; not documented as a decision either way. | Open |
| F-8 | `backend/detection/tgnn.py` | Resolved | Original design scored pooled global centrality, inverting the detection signal (BENIGN 20.19% vs. Bot 9.00%). Full detail in §4. | Fixed & verified — replay re-run + 18 tests, this session |

## 10. Recommendations

Ordered by effort, not by severity — the top of this list is an
afternoon, not a sprint.

- **Fix F-1** (`aegis_demo.py:586`) — a one-line change
  (`entry["src"] / entry["tgt"]` instead of the 4-tuple unpack), already
  diagnosed, currently breaking a UI overlay on every active attack.
- **Correct or remove the F-5 reference** — either write
  `docs/ARCHITECTURE.md` or repoint CLAUDE.md §8 at
  `docs/SYSTEM_REFERENCE.md`, which already serves that role in practice.
- **Extend F-6's duplicate-definition check to `backend/`** — the
  convention is stated project-wide in CLAUDE.md; the enforcement
  currently isn't.
- **Make F-7 a decision, not a gap** — either set a `--cov-fail-under`
  threshold or add one sentence to CLAUDE.md stating that coverage is
  observational by design.
- **Measure the two placeholder reliability weights** (beaconing, T-GNN —
  both `0.50`, both flagged unmeasured) the same way
  volumetric/supervised/tripwire already were, so all six numbers feeding
  the noisy-OR mean the same kind of thing.
- **Regenerate `AEGIS_Judge_Room_Dossier.docx`** from its `.md` source —
  the export is one edition behind (2026-09-03 vs. the source's
  2026-09-04) as of this audit.

## 11. Appendix: stack & sources

**Backend**

| | |
|---|---|
| FastAPI | 0.115+ |
| SQLAlchemy | 2.0+ |
| PostgreSQL | 16 |
| pydantic-settings | 2.6+ |

**Research engine**

| | |
|---|---|
| scikit-learn | 1.8+ |
| networkx | 3.6+ |
| Streamlit | 1.57+ |

**Frontend**

| | |
|---|---|
| Next.js | 16.3.3 |
| React | 19.2.8 |
| TypeScript | 5.x |
| Tailwind | 4.x |

All four version sets were checked directly against `requirements.txt`,
`requirements-backend.txt`, and `frontend/package.json` — no mismatch
against documented claims found.

---

*Prepared by Claude Code, in-session engineering review — not a
substitute for a third-party security assessment. `eb920b8` ·
`FINAL-FINAL-BEST`.*
