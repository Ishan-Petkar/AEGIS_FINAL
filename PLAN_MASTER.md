# PLAN_MASTER.md — AEGIS

**Status:** Authoritative. Supersedes all prior plan documents.
**Date:** 2026-08-22 (Phase 5 promoted to the critical path; Phases 1–3 complete)
**Research basis:** `research/BENCHMARKS.md`
**Companion doc:** `docs/PHASE5_BUILD_PLAN.md` — implementation detail for the active sprint (DDL, API routes, WS envelope, per-ticket acceptance criteria). This document is authoritative for *what and why*; that one for *how*.

**Current state:** Phases 0–3 built and independently verified — 229 tests passing, ruff clean, real-dataset evaluation with honest metrics. **Phase 5 (Live Operations Console) is the active 8-day sprint.** Phase 4 is deferred by deliberate choice, not neglect (see §14 and the Phase 4 section).

This document has two jobs: (1) record *how* we got to this plan, in order, so no decision gets silently lost again, and (2) lay out the actual build order. Section 1 is the history. Section 2 onward is the plan itself.

---

## 1. How This Plan Came Together (chronological)

**1. Audit.** A full pass over the Streamlit dashboard found 8 defects that made most of the app unreachable or made it lie about its own behavior — dict/tuple mismatch crashing every attack simulation, a `NameError` on What-If asset selection, an evaluation harness silently producing all-zero metrics, fabricated feature names in the UI, phantom dropdown entries, SWaT never exposed despite being fully ingestible, false roadmap claims, and a dead, insecure download script. All 8 fixed and verified live in-browser. 88/88 tests passing, ruff clean. Two follow-up bugs (`border_color` `NameError` on the What-If path, Material Icons rendering as literal text) found and fixed the same way. A PR was opened for all of it.

**2. Research + audit + user-idea integration request.** You asked for a properly researched master plan: benchmark against real systems, audit the current code, fold in your own idea, and produce one authoritative plan document. I researched five benchmarks (`research/BENCHMARKS.md`): MulVAL (logical attack graphs), BloodHound (collector/graph/UI separation), honeytoken/deception literature, Conpot (ICS honeypot design), and the ICS anomaly-detection evaluation literature (point-adjust metric pitfalls) — plus the MITRE ATT&CK-for-ICS / Purdue model as the reasoning spine.

**3. The key finding.** You asked, essentially, "aren't we just counting unauthorized IPs — doesn't that mean we're detecting compromise *after* it already happened?" Checking the code: the mechanism you guessed wasn't literally right (the model scores `duration_sec`/`packets`/`bytes`, not IPs directly), but the conclusion was correct and the real situation was worse. `src/data_generator.py` injected anomaly labels **only** on traffic from a hardcoded 3-IP threat list — the label was created by the IP list and then "discovered" by the model as a volume spike. Circular; proves nothing. Separately, all three ML features are *terminal aggregates of a completed flow* — they cannot exist before a payload has finished moving, so no model improvement of any kind can make detection earlier. This became the central problem the rest of the plan is built to solve.

**4. First draft plan.** Delivered a 5-phase plan with a "Deception Layer" as Phase 2 — scattered decoy assets an attacker might wander into during lateral movement, à la a classic honeypot.

**5. Your critique — the decoys are coincidental.** You pointed out that bait-style decoys only work if the attacker happens to touch one; a determined attacker going straight for a real asset via a path that never crosses a decoy is invisible to that design. You also asked directly: *why did you just patch the existing plan instead of doing a full redesign — was it already good enough, or did you not try?*

**6. Honest answer, then a redesign of the one piece that was actually wrong.** I owned that I'd defaulted to extending the existing module boundaries without validating that against a from-scratch alternative — a real gap, not a stylistic choice. But the fix wasn't "redesign everything": the foundational contracts (below, C1–C4) turn out to be close to forced moves for *any* architecture with this problem shape, benchmark-derived or not. The one piece that was conceptually wrong — opportunistic decoys — got reset at the concept level: from "bait an attacker might find" to a **mandatory chokepoint** every connection to a protected asset must pass through, whether the attacker is careful or not.

**7. Design decisions, made explicitly rather than assumed.** Five choices, each posed as an option with a stated trade-off, each one you picked:

| # | Question | Options | Decision |
|---|---|---|---|
| 1 | Should the gateway be a real topological chokepoint, or a passive monitoring tap alongside existing direct traffic? | Hard chokepoint (edges rewritten so nothing reaches a protected asset except through it) vs. Soft tap (traffic still flows directly, mirrored past an inspector) | **Hard chokepoint** — a soft tap doesn't actually make anything mandatory, undermining the whole point |
| 2 | How does the gateway keep detection precision at 100% without depending on luck? | Honeytoken credentials (zero-legitimate-use fake creds; any use = unambiguous compromise) vs. Behavioral scoring of real traffic vs. Both fused | **Honeytoken credentials**, with behavioral scoring as a secondary, lower-confidence signal |
| 3 | Is this a big enough architectural change to fold into the foundation phase, or a later increment? | Fold into Phase 1 (graph builder is gateway-aware from day one) vs. Ship a plain graph first, add gateway rewrite later | **Fold into Phase 1** — building the graph logic twice (plain, then gateway-aware) wastes hackathon time |
| 4 | Does compromising the gateway itself count as a step toward the real asset, or is it pure instrumentation? | Near-zero criticality (detection only, doesn't distort blast-radius math) vs. Real criticality (a gateway breach is itself a partial compromise, which is more realistic but muddies the lead-time story) | **Near-zero criticality** — keeps the CII math clean and the "how early did we detect" claim unambiguous |
| 5 | One gateway per protected asset, or one per trust zone? | Per-asset (maximum isolation, simplest per-node model) vs. Per-Purdue-level zone (fewer nodes, matches how real network segmentation/bastion hosts are actually built) | **One gateway per Purdue-level zone** |

**8. Verdict on "full redesign vs. this plan."** No full redesign. The test applied: would a from-scratch architect, given the same constraints (three real dataset adapters already working, correct Monte Carlo CII math, Streamlit as the demo surface, hackathon time budget), land somewhere fundamentally different on the *foundation*? No — C1–C4 below are close to forced moves regardless of detection philosophy. The redesign that *was* warranted (opportunistic decoys → mandatory gateway) already happened, at the concept level, not as a patch. That's the actual signal to watch for going forward: when something is genuinely wrong, reset the concept; don't paper over it.

**9. Plan rewritten for hand-off.** Reformatted into scannable per-phase checklists so you can order development phase by phase — but that pass dropped the explicit C1–C4 framing and the decision record above. This document restores both.

**10. Phases 1–3 built and verified.** C1–C4 landed, then the deception layer, then the evaluation harness. 229 tests. Each phase independently verified — full suite rerun, source-level spot checks, and live in-browser checks — rather than accepted on the implementer's report. Two real bugs were caught *during* that verification rather than after: the gateway edge-probability clobbering bug (multiple edges into one protected asset silently overwriting each other instead of combining — fixed via probabilistic union), and `_load_synthetic()` hardcoding every row as benign (which made every synthetic-fallback evaluation a 0%-positive degenerate split — almost certainly the root cause of the original all-zero-metrics bug from Section 1.1).

**11. The actual problem statement, compared against what we built.** You shared the hackathon brief — *"AI-Driven Cyber Risk Detection for Smart City Digital Infrastructure"* — and asked how AEGIS maps to it. Mapping it honestly:

| Brief requires | Status at end of Phase 3 |
|---|---|
| Ingest structured/semi-structured data | ✅ Strong — canonical schema, 4 real adapters, asset registry |
| AI/ML anomaly detection | ✅ **Strongest asset** — real ground truth, segment-wise ICS metrics, degenerate-split guard, documented rejection of point-adjust |
| Actionable insights: risk scores, **alerts**, dashboards | ⚠️ Scores ✅ dashboard ✅ — but **no alert mechanism at all**; everything surfaces only if a human is watching the screen |
| **Scalability for real-world environments** | ❌ Batch-only, static topology, 15–80 synthetic nodes, no persistence |
| Explainability | ⚠️ Graph-level (CII hop reasoning, MITRE mapping) ✅ — model-level ("why was *this row* flagged") ❌ |
| SDG 9 / SDG 11 alignment | ❌ Not mentioned anywhere in code or docs |

Two of the gaps — **scalability** and **alerts** — are named directly in the brief. That reframed the remaining work: the detection science was ahead of where a hackathon submission needs to be, and the operational packaging was behind.

**12. "Why not just restart properly?"** You asked whether, given how rigorous the system had become, it would be cleaner to rebuild from scratch. Answer was no, and specifically *not* out of sunk-cost attachment: the scalability gap needs a different **delivery mechanism**, not different **logic**. `core/pipeline.py` was already headless by construction (C1 was built in Phase 1 precisely so the analytics could be "swapped for an API response without touching analysis logic" — its own docstring says so). Restarting would mean rebuilding ingestion, detection, and the evaluation-honesty work from zero, with a high chance of shipping something *less* rigorous, while throwing away the one thing already ahead of the field. The correct move was to fix the packaging and leave the engine alone.

**13. Phase 5 promoted from post-hackathon to the critical path.** Decision: keep the entire Phase 1–3 engine untouched; replace the front end with a real operations console; add persistence and continuous ingestion of *real* telemetry. No simulation on the landing view — the demo opens on real recorded traffic streaming live.

**14. Phase 5 design decisions, made explicitly:**

| # | Question | Decision | Reasoning |
|---|---|---|---|
| 6 | Frontend stack | **Next.js + FastAPI** | Team already has Next.js design experience; FastAPI stays thin, exposing the existing Python engine. Streamlit fundamentally reads as an analyst notebook, not a product. |
| 7 | Database | **PostgreSQL** | The name judges recognize as real infrastructure. |
| 8 | Postgres delivery | **Homebrew primary, Docker Compose optional** | Docker is not installed on the dev machine (verified); installing Desktop across 3 machines could cost a day of an 8-day budget. Compose still ships for the reproducibility story. |
| 9 | Does the Streamlit app get deleted? | **Kept, reframed as the Research Console** | It holds the Phase 3 evaluation panel — real ground truth, segment-wise metrics, lead time. That is our credibility evidence. Two surfaces on one engine (Operations + Research) is a stronger pitch than one app, at zero extra engineering cost. |
| 10 | What happens to the scripted attack buttons? | **Demoted, not deleted** | Landing view is real traffic only. Injection becomes a small tucked-away control and the demo's second act: real stream → inject breach → tripwire fires on recon *before* exfil. Authenticity plus a controllable wow-moment. |
| 11 | How does the graph survive thousands of real IPs? | **Render assets, not packets** | Curated city assets + Purdue gateways, real IPs mapped on via the existing `AssetRegistry`, unresolved addresses aggregated into `/24` cluster nodes with count badges. Readable ~30–60 nodes, entirely real-traffic-driven. |

---

## 2. The Four Foundational Contracts (C1–C4)

These are what Phase 1 actually is. Everything else in the plan is downstream of these four.

| # | Contract | Lives in | The problem today | Why it can't be deferred |
|---|---|---|---|---|
| **C1** | Analytics extracted from the UI | `src/core/pipeline.py` | All data/train/score/CII logic sits inside the 993-line Streamlit script — nothing is importable or testable without launching a browser | Phase 5 (API, streaming) can't call code trapped inside a UI script. Every feature added to the UI in the meantime raises the eventual extraction cost. |
| **C2** | One authoritative graph | `src/graph_manager.py` | The graph is built twice, in two different files (`cii_calculator.py`'s `DiGraph`, and an inline `nx.Graph()` in `aegis_demo.py`), with no shared object | If the gateway (below) is added to one graph but not the other, the picture on screen and the blast-radius math silently disagree — wrong numbers that look correct in isolation. |
| **C3** | Detector protocol | `src/detectors/base.py` | New detectors have to be hand-edited into the UI script; no common interface | Phase 2's tripwire detector and Phase 4's future models need to plug in the same way, or each one is a one-off integration. |
| **C4** | Signal schema | `src/datasets/schema.py` — adds `signal_type`, `observed_at`, `purdue_level` | The canonical data format only knows how to represent a network flow (`duration_sec`, `packets`, `bytes`). It has no way to represent "someone touched a decoy" or "someone used a honeytoken." | **Highest-risk item.** Without this, Phase 2 is forced to fabricate flow-shaped fields for an event that has none — which recreates the exact circular-labeling bug found in Section 1.3. A schema migration *after* deception events exist means rewriting the adapter, its tests, and every fixture. This must land before any Phase 2 code touches data. |

**Sequencing rule that falls out of this:** C4 must be the first thing built in Phase 1. C1, C2, and C3 can proceed in any order relative to each other and to C4, but nothing in Phase 2 can start until C4 exists.

---

## 3. Executive Summary

AEGIS today is a post-hoc blast-radius calculator: it scores completed flows with an Isolation Forest over three terminal volumetric features, then computes Monte Carlo cascade impact for a compromise that already happened. The to-be architecture is a two-signal early-warning system: a **mandatory gateway** in front of every high-criticality asset — not bait an attacker might find, but a structural chokepoint every connection must pass through — seeded with a honeytoken credential that turns any use into an unambiguous, zero-luck-required detection, fused with the existing volumetric detector as a broader secondary signal. Both converge on one signal schema (C4), feed one graph (C2), and are consumed by an analytics core (C1) the UI merely renders, with every detector — old and new — implementing one interface (C3).

---

## 4. Phase Map

```
Phase 0  Stabilization                     ✅ DONE
   │
Phase 1  Foundation (C1, C2, C3, C4)
         + Gateway Topology                 ✅ DONE
   │
Phase 2  Deception Activation              ✅ CORE DONE (tab + docs open)
   │
Phase 3  Honest Evaluation                 ✅ DONE
   │
Phase 5  Live Operations Console           ← BUILD THIS NEXT (8-day sprint, team of 3)
         (streaming · Postgres · Next.js)     promoted to critical path — see §14
   │
Phase 4  Graph & Temporal ML               deferred — genuinely post-hackathon
```

**Phases 1–3 built the engine. Phase 5 makes it look and behave like a product.** Phase 4 is deliberately deferred: it deepens detection science that is already ahead of what the brief requires, while Phase 5 closes two gaps the brief names outright (scalability, alerts). Order is by judged value, not by phase number.

---

## Phase 0 — Stabilization ✅ DONE

Fixed 8 defects (Section 1.1). 88/88 tests pass, ruff clean, all fixes verified live in-browser. PR opened.

**Document audit:** `CLAUDE.md` valid (§7 needs a refresh to reflect Phase 0 completion). `docs/AEGIS_PROJECT_MASTER_PLAN.md` and `docs/ARCHITECTURE.md` outdated — describe a superseded design. `docs/DATASETS.md`, `docs/DESIGN.md` valid. `PLAN.md` and `src/graph_manager.py` did not exist prior to this plan — Phase 1 creates the latter; this document replaces the need for the former.

---

## Phase 1 — Foundation (C1, C2, C3, C4) + Gateway Topology ✅ DONE

**Depends on:** nothing. **Blocks:** every other phase.

**Built, tested, and verified live in-browser:**
- **C4** — `signal_type`/`observed_at`/`purdue_level` added to the schema (v2.0); `to_ml_features()` dispatches on `signal_type` so a future deception event won't get fabricated volumetrics; all three real-dataset adapters populate the new fields.
- **C3** — `detectors/base.py` (`BaseDetector` ABC), `ZScoreDetector`/`MADDetector` formalised as subclasses, `detectors/registry.py`. `ml_engine.py` re-exports for backward compatibility.
- **C2** — `graph_manager.py` is the sole graph builder. Implements the mandatory Purdue-zone gateway: every edge into a protected asset (criticality ≥ `SETTINGS.gateway.criticality_threshold`) is rewritten through a `Gateway_L<zone>` node, near-zero criticality (`SETTINGS.gateway.gateway_node_criticality`). Found and fixed a real bug during construction: multiple original edges into the same protected asset were silently clobbering each other in `nx.DiGraph.add_edge()` — fixed via probabilistic union (`P = 1 - Π(1-p_i)`), verified against the production graph (e.g. `City_Payment_Gateway`'s 3 inbound edges → union = 0.97).
- **C1** — `core/pipeline.py`'s `run_analysis()` is the single analytics entry point; verified to run with zero Streamlit import. `aegis_demo.py` reduced to widgets + one `run_analysis()` call + rendering (down from computation-and-rendering intermixed).
- Also fixed: the label-leakage bug in `data_generator.py` (anomaly injection was gated on `src in threat_ips`, making the label perfectly recoverable from a 3-IP list) — decoupled, verified the anomaly rate among threat-IP sources dropped from 100% to ~25% (matching their natural share of all valid IPs, i.e. no signal).
- 135/135 tests passing (47 new), ruff clean, live-verified: all 4 attack presets, What-If on multiple assets, all 5 tabs, SWaT dataset switch — gateway nodes visibly appear in the UI's Impacted Assets panel at criticality 0.0, exactly as designed.

### What gets built, and which contract it satisfies

| Task | Contract | Detail |
|---|---|---|
| `src/datasets/schema.py` — add `signal_type`, `observed_at`, `purdue_level`; bump `SCHEMA_VERSION` to `2.0` | **C4** | Build this first — see sequencing rule in Section 2 |
| `src/datasets/{cic_ids,paysim,swat}_adapter.py` — populate the three new fields | C4 | `purdue_level`: SWaT→1, CIC-IDS→3, PaySim→4 |
| `src/detectors/base.py` — `fit` / `predict` (-1/1) / `score_samples` (lower = more anomalous) | **C3** | Codifies the sklearn convention `ZScoreDetector`/`MADDetector` already follow informally |
| `src/detectors/statistical.py` — move `ZScoreDetector`/`MADDetector` here, conform to the interface | C3 | Re-export from `ml_engine` to preserve existing imports |
| `src/detectors/registry.py` — name → detector lookup table | C3 | Phase 3 iterates this instead of a hardcoded list |
| `src/graph_manager.py` — the one graph builder | **C2** | Absorbs `cii_calculator.build_dependency_graph` + the inline `nx.Graph()` build in `aegis_demo.py`. **Includes the gateway rewrite** (below) from day one, per Decision #3 |
| `src/cii_calculator.py` — read the graph from `graph_manager`, delete the local constructor | C2 | Preserve both existing public entry points |
| `src/core/pipeline.py` — extract data → preprocess → train → score → CII into `run_analysis()` | **C1** | No Streamlit import permitted in this module |
| `src/core/result.py` — frozen `AnalysisResult` dataclass | C1 | The UI's only input type; also becomes the Phase 5 API response shape |
| `src/aegis_demo.py` — cut down to page config, CSS, session state, sidebar, `run_analysis()` call, tab rendering | C1 | Target: ≤250 lines, zero computation |
| `src/config.py` — add `purdue_level` per asset; delete confirmed-dead constants (`PAGE_TITLE`, `CUSTOM_CSS`, `HEADER_HTML`, etc.) | supports C2/C4 | |
| `src/data_generator.py` — decouple attack labels from `EXTERNAL_THREAT_IPS` membership | fixes Section 1.3 finding | Without this, no metric produced in Phase 3 is meaningful — the label is currently trivially recoverable from a 3-item list |

### The gateway, built here (Decisions #1, #3, #4, #5 from Section 1.7)

`graph_manager.py`'s builder enforces a rule: **no inbound edge may terminate directly on an asset above the criticality threshold.** Any such edge is rewritten as `source → Gateway_<zone> → target`, where `Gateway_<zone>` is **one node per Purdue-level trust zone** (Decision #5), not one per asset. This is a **hard, structural chokepoint** (Decision #1) — the graph itself has no path around it, not a passive tap that traffic could bypass. The gateway node carries **near-zero criticality** (Decision #4), so a hit on it is pure detection signal and doesn't distort blast-radius arithmetic. What actually gets seeded inside the gateway (the honeytoken) is Phase 2's job (Decision #3: fold the *topology* into Phase 1, activate *detection* in Phase 2) — this phase just guarantees the door exists and nothing can go around it.

### Checklist

- [ ] `src/datasets/schema.py` — C4 schema fields (**build first**)
- [ ] `src/detectors/base.py` — C3 interface
- [ ] `src/detectors/statistical.py`, `src/detectors/registry.py`
- [ ] `src/graph_manager.py` — C2 builder + gateway rewrite
- [ ] `src/cii_calculator.py` — consume `graph_manager`
- [ ] `src/core/pipeline.py`, `src/core/result.py` — C1 extraction
- [ ] `src/aegis_demo.py` — reduce to UI-only
- [ ] `src/config.py` — `purdue_level` per asset, delete dead constants
- [ ] `src/datasets/{cic_ids,paysim,swat}_adapter.py` — populate new fields
- [ ] `src/data_generator.py` — decouple labels from threat-IP list
- [ ] Tests for all of the above

### Done when

- [ ] Dashboard runs headless (no Streamlit) via `run_analysis()`
- [ ] Exactly one place in the codebase builds a graph (`grep -rn "nx.Graph()\|nx.DiGraph()" src/` → one hit)
- [ ] Every path to a protected asset visibly routes through its zone's gateway node in the graph object itself, not just in the UI picture
- [ ] A detector trained only on traffic volume can no longer perfectly recover the attack label
- [ ] All existing tests still pass, new tests added, ruff clean

**Estimated effort:** 12–16 h.

---

## Phase 2 — Deception Activation ✅ CORE DONE (2 checklist items still open)

**Depends on:** Phase 1 — specifically **C4** (a tripwire event needs `signal_type` to exist without fabricating flow fields) and **C2** (the gateway nodes it activates must already exist in the one shared graph). **Blocks:** Phase 3.

### What this phase adds

Phase 1 built the mandatory door. Phase 2 puts something inside it (Decision #2): a **honeytoken credential** per gateway — a fake account or key with zero legitimate use, so any use is unambiguous compromise with no training data and no possibility of a false positive by construction. This is the piece that answers your original critique directly: detection no longer depends on an attacker coincidentally wandering onto bait, because the honeytoken sits inside the one door everything must pass through.

- **Tripwire detector** (`src/deception/tripwire.py`) — fires instantly on honeytoken use, conforms to the C3 interface. Note: it slots into the C3 *registry* (`detectors/registry.py`) but is evaluated separately from the volumetric precision/recall harness in Phase 3 — it uses a different feature (`is_honeytoken_use`) that real-traffic datasets don't have.
- **Secondary signal** — behavioral scoring (failed logins, odd request rates) on the gateway's *real* traffic, for attackers who avoid the honeytoken specifically (per Decision #2: honeytoken primary, behavioral secondary, not instead of).
- **Recon stages** added to each scripted attack scenario — a gateway touch at `t−N` before the existing exfiltration step at `t` — which is what makes "how much earlier did we detect this" measurable in Phase 3.

### Checklist

- [x] `src/config.py` — `HONEYTOKEN_CREDENTIALS`, one entry per Purdue-zone gateway (L0–L5), each with a unique `emulated_protocol`, declared as data
- [x] `src/deception/tripwire.py` — `TripwireDetector(BaseDetector)`. Deterministic, not learned: `fit()` is a no-op, `predict()`/`score_samples()` read `is_honeytoken_use` directly. Sklearn convention preserved.
- [x] `src/deception/adapter.py` — `generate_tripwire_events()` emits `signal_type="deception_tripwire"`; `duration_sec`/`packets`/`bytes` always `0`, never fabricated (protects C4's anti-circular-labeling guarantee)
- [x] `src/datasets/loader.py` — `load_dataset("deception")` registered. **Deviation from the original plan:** built as a *standalone* source (all 6 gateway zones, not merged with other traffic) rather than "mergeable" — simpler and sufficient for what Phase 2/3 actually need; revisit if a future phase needs blended real+deception traffic in one batch.
- [x] `src/data_generator.py` — `generate_scripted_attack()` adds a recon (tripwire) event `SETTINGS.deception.recon_delay_sec` (default 60s) before each attack's exfil edge; wired into all 4 scripted-attack sidebar buttons
- [x] `src/core/pipeline.py` — fuses tripwire + volume signals via OR on `is_anomaly`; new `confidence` column escalates when both fire (`SETTINGS.deception.confidence_*`); auto-triggers CII on a tripwire hit even with no attack explicitly selected
- [x] `src/cii_calculator.py` + `src/graph_manager.py` — `is_gateway_name()`/`gateway_target_assets()` added; a gateway-only compromise (tripwire fired, no exfil yet) now produces a real non-zero CII instead of zero
- [ ] `src/aegis_demo.py` — dedicated gateway status / tripwire feed / timeline tab — **not built**. Attack buttons now call `generate_scripted_attack()` so recon+exfil both flow through the existing tabs, but there's no standalone deception-focused view yet.
- [ ] `docs/DECEPTION.md` — **not written**

### Done when

- [x] Honeytoken use → detected with zero false positives — asserted by tests in `tests/test_deception_tripwire.py` (48 tests: protocol conformance, non-fabrication, fusion/confidence escalation, gateway-only CII)
- [ ] For ≥3 of 4 scripted attacks, the tripwire fires measurably earlier than the old volume-based detector — **not yet measured end-to-end.** The recon-then-exfil timing exists in the data (`observed_at` on the recon event vs. the exfil edge), but the lead-time *computation* is Phase 3's job (`src/evaluation/lead_time.py`) — this criterion is verified there, not here.
- [x] A gateway hit alone produces a blast-radius number before any exfiltration data exists — verified by test and live in-browser (`run_analysis(dataset="deception")`: 6/6 tripwires fire, CII = 0.147)

### Honest limitation (state this, don't hide it)

This catches reconnaissance and lateral movement — an attacker probing the network or trying credentials against the gateway. It does **not** catch an attacker who already holds a valid, real credential and goes straight for a real asset without probing anything — that traffic looks legitimate at the gateway too. Phase 4's behavioral scoring is the eventual (partial) answer to that; this phase is honest about not solving it yet.

**Estimated effort:** 8–10 h.

---

## Phase 3 — Honest Evaluation ✅ DONE

**Depends on:** Phase 1 (C3 registry) + Phase 2 (the system under test is now two signals — evaluating only the old detector grades the wrong system).

### What this phase fixes

1. **Degenerate metrics.** `run_evaluation()` once defaulted to a dataset slice that was 100% benign, silently reporting `P=0.000 R=0.000 F1=0.000 AUC=nan`. This phase makes that configuration **raise an error** instead of reporting a fake result.
2. **A metrics trap specific to time-series ICS data.** "Point-adjust" scoring can make random noise look like a state-of-the-art detector (research finding from `research/BENCHMARKS.md`, B5). Uses segment-wise precision/recall instead, with the rejection of point-adjust documented in-code.
3. **Lead time as a first-class metric** — computed from the C4 `observed_at` field, reporting how many seconds earlier the tripwire fired versus the old volume-based detection. This is the number that proves the Phase 2 thesis.

### Checklist

- [x] `src/evaluation/` — `src/evaluation.py` converted to a package (`__init__.py` + `__main__.py` for the `python -m evaluation` CLI); **why:** Python silently prefers a package over a same-named module when both exist, which would have made the flat `.py` file permanently dead code — verified empirically before converting. `run_evaluation()` now iterates `detectors.registry.DETECTORS`. Also added `src/detectors/sklearn_wrappers.py` (`IsolationForestDetector`/`OneClassSVMDetector`) so Isolation Forest and OCSVM go through the same registry as `zscore`/`mad` instead of being hardcoded — `ml_engine.py`'s existing callers are untouched, these are thin adapters around the same functions.
- [x] `DegenerateEvaluationError` — raised when the eval split's positive rate is outside `[SETTINGS.evaluation.min_positive_rate, max_positive_rate]` (1%–99% by default). Pinned by a test using `dataset="deception"` (100% positive by construction).
- [x] `src/evaluation/metrics.py` — segment-wise **recall**, row-wise **precision** (the deliberate, documented split from point-adjust — point-adjust's actual flaw is laundering false negatives into true positives for precision; this module never gives that amnesty). Verified against real SWaT data: Isolation Forest segment-recall 1.0, Z-Score/MAD segment-recall 0.0 — an honest, unflattering result, not a harness artifact.
- [x] `src/evaluation/lead_time.py` — replays the actual production `generate_scripted_attack()` timeline for each of the 4 presets and measures tripwire-vs-volumetric detection instant. Deliberately excluded from the P/R/F1/AUC loop (see file docstring: tripwire's only feature, `is_honeytoken_use`, is absent from every real-traffic row, so forcing it through that harness would be a meaningless, not just degenerate, result). **Verified live in-browser:** 4/4 scripted attacks, mean lead time 58.4s, using the exact same exfil-edge dicts as `aegis_demo.py`'s sidebar buttons (confirmed identical, not just similar).
- [x] `docs/EVALUATION.md` — protocol + reproduction commands, grounded in real pasted output from this repo's actual datasets (CIC-IDS2017, SWaT, and the degenerate-split repro).

**A real bug found and fixed along the way (not in the original checklist):** `datasets/loader.py`'s `_load_synthetic()` hardcoded `action = ACTION_PASS` for every row, completely ignoring `data_generator`'s `is_ground_truth_anomaly` column. Every evaluation run that fell back to synthetic data (e.g. any CI environment without the real datasets on disk) was silently scoring against a 0%-positive split — almost certainly the actual root cause of the original "100%-benign silent zeros" bug this phase exists to prevent. Fixed with `np.where(edges_df["is_ground_truth_anomaly"], ACTION_ALERT, ACTION_PASS)`; regression test added. Independently verified: `numpy` was already imported at module level, the fix is a one-line correction, not a new dependency.

### Done when

- [x] Every published number is reproducible from a committed config — no hand-typed numbers in the UI. Verified: the ML Inspector tab's tables/charts and the lead-time panel both come straight from `run_evaluation()`/`compute_all_scripted_attack_lead_times()`.
- [x] Lead time is reported next to precision/recall/F1 — a dedicated "Tripwire Lead-Time Benchmark" panel sits directly below the P/R/F1/AUC table in the same tab (verified live).
- [x] The degenerate-metric bug can never silently recur — `DegenerateEvaluationError` fires deterministically on a known-degenerate split (`--dataset deception`), verified by both a test and a live CLI repro.

**Estimated effort:** 6–8 h. **Actual:** ~1 subagent pass; 229/229 tests total (47 new), ruff clean, no duplicate definitions, verified independently (not just on the implementer's report) — full suite rerun, source-level spot checks of the segment-wise-scoring math and the loader fix, and a live browser check of the new UI panel.

---

## Phase 4 — Graph & Temporal ML *(deferred — genuinely post-hackathon)*

**Depends on:** Phases 1–3. **Deliberately sequenced after Phase 5** — see §14.

Replaces hand-typed edge probabilities in `config.py` with numbers derived from Purdue-level segmentation and observed data. Adds detectors using graph structure (distance to nearest gateway, centrality) and time patterns (rolling frequency, off-hours activity) instead of raw volume alone. Fixes a known gap: `shares_provider` is documented as modeling correlated common-mode failure but currently samples independently, identical to the default branch — this phase implements it for real using the already-plumbed-but-unused `provider_id` field. Also where the "attacker with a valid stolen credential" gap from Phase 2 gets a partial answer, via behavioral anomaly scoring at the gateway.

**Why it's deferred, stated plainly:** this phase makes good detection *better*. The brief does not ask for better detection — it asks for scalability, alerts, and explainability, none of which Phase 4 provides. Spending 8 contested days here would deepen the one dimension already ahead of the field while leaving two explicitly-graded gaps open. If the sprint finishes early, the highest-value fragment to pull forward is the `shares_provider` fix (a documented behavior that currently doesn't match its own docstring — a correctness gap, not an enhancement).

**Estimated effort:** 14–20 h. Not required for the demo.

---

## Phase 5 — Live Operations Console ← **ACTIVE**

**Depends on:** Phase 1 — specifically **C1** (logic must already be importable outside Streamlit) and **C4** (streaming needs event time `observed_at` distinct from processing time, which is why C4 landed in Phase 1 rather than here). Both exist. **Blocks:** nothing — this is the last phase before the deadline.

**Budget:** 8 days · team of 3 · GitHub issues + PRs.
**Implementation detail** (exact DDL, routes, WS envelope, per-ticket acceptance criteria) lives in **`docs/PHASE5_BUILD_PLAN.md`** — that is the developer's working reference. This section is authoritative for *what and why*; that document is authoritative for *how*. Keep them in sync or delete one.

### The thesis

Turn AEGIS from a batch research demo into something that looks and behaves like production: real telemetry streaming continuously, persisted to Postgres, served through an API, rendered live in a Next.js operations console. **The detection and risk math do not change.** Phases 1–3 are the asset being packaged, not replaced.

### Architecture

```
REPLAY ENGINE ──► FASTAPI ──► POSTGRES
(real CIC-IDS2017,   │  score · resolve · CII
 timestamp order,    │  (pre-fitted model)
 ~20× speed)         └──► WEBSOCKET ──► NEXT.JS CONSOLE
                                         feed │ graph │ alerts
```

```
aegis-project/
├── src/                    ← UNCHANGED except ONE new file
│   └── core/streaming.py   ← NEW: fit-once / score-per-event
├── backend/                ← NEW: FastAPI (main, db, replay, ingest, schemas)
├── frontend/               ← NEW: Next.js console
└── src/aegis_demo.py       ← KEPT as the Research Console (Decision #9)
```

### The one real engine change

`ml_engine.preprocess_features()` calls `scaler.fit_transform()` — it fits a **new** scaler on every call. Correct for batch, **wrong for streaming**: refitting per event lets the baseline drift toward the attack, and the anomaly scores stop meaning anything.

`src/core/streaming.py` introduces `StreamingScorer`:
- `fit_from_warmup()` — fit scaler + IsolationForest **once**, on benign-only historical rows (same discipline as `evaluation/`)
- `save()` / `load()` — warmup happens at build time, so the demo machine never trains live (see R11)
- `score_batch()` / `score_event()` — `transform()` only, never `fit_transform()`
- `explain()` — per-feature deviation vs. the warmup baseline (*"bytes 47σ above normal"*). Cheap, honest, and closes the model-explainability gap without SHAP.

Tripwire fusion is **not** reimplemented — it reuses the OR + confidence-escalation logic already in `core/pipeline.py`.

### Persistence (Postgres)

`assets` · `events` · `event_scores` · `cii_snapshots` · `alerts`

The `alerts` table with an acknowledge workflow is what closes the brief's *"actionable insights: alerts"* gap — currently the system has risk scores and a dashboard but no alerting mechanism whatsoever. Each alert carries its `explanation` JSONB (from `explain()`) and a foreign key to the `cii_snapshots` row holding its blast radius, so an operator sees *what*, *why*, and *what falls over next* in one place.

Full DDL and indexes: `docs/PHASE5_BUILD_PLAN.md` §6.

### Ticket breakdown

`[A]` backend/DB · `[B]` frontend · `[C]` infra/integration. One ticket = one PR.

| Day | Tickets |
|---|---|
| **0–1** foundations | `#1 [C]` Postgres (brew) + env/config · `#2 [A]` schema + models, seed assets from `config.py` · `#3 [B]` Next.js scaffold + `docs/DESIGN.md` theme tokens · **`#4 [B]` mock WebSocket server** · `#5 [A]` `StreamingScorer` + tests |
| **2–3** pipes | `#6 [C]` replay engine (real CIC-IDS2017, timestamp order, speed control) · `#7 [A]` ingest: score→persist→broadcast, CII debounce · `#8 [A]` REST routes · `#9 [A]` WebSocket endpoint · `#10 [B]` live feed · `#11 [B]` graph + `/24` clustering |
| **4** integration | `#12` all three — mock→real WS, end-to-end live · `#13 [C]` `POST /api/inject` |
| **5** payoff | `#14 [B]` CII cascade animation · `#15 [B]` alerts panel + ack + per-alert "why" · `#16 [A]` `/api/stats` |
| **6** credibility | `#17` SDG 9/11 in README + UI · `#18` README rewrite · `#19` styling, empty/error states, WS reconnect |
| **7–8** prove it | `#20` full dry run, fix breakages · `#21` pitch deck + rehearsal · **buffer** |

`#4` (mock WebSocket server) is the load-bearing scheduling decision: it ships Day 1 so frontend work never blocks on backend readiness.

### Demo arc

**real → live → predictive → explainable → proven.**

1. Open on live real CIC-IDS2017 traffic at 20×. Graph calm, counters ticking. *"Recorded traffic from real infrastructure — not a simulation."*
2. Anomalies surface naturally from the real data.
3. Inject Payment Gateway Breach → **honeytoken tripwire fires on the recon stage**, alert with full blast radius appears *before any exfiltration happens*.
4. CII cascade animates: payment gateway → bank API → welfare system.
5. Open the alert → *"bytes 47σ above baseline"* → explainability, not a black box.
6. Switch to the Research Console → *"and here's why you should believe it"* — real ground truth, segment-wise ICS metrics, 58.4s mean lead time, degenerate-split guard.
7. Close on SDG 9 (resilient infrastructure) / SDG 11 (safe, sustainable cities).

### Done when

- [ ] Real dataset streams continuously into Postgres; events survive a restart
- [ ] Console shows live feed + live graph with zero page refresh
- [ ] An anomaly produces a **persisted alert** with blast radius and a human-readable "why"
- [ ] Injection works on demand; tripwire fires before exfil
- [ ] `src/` engine tests still pass (229/229) — proof the engine was packaged, not modified
- [ ] README covers architecture, both consoles, setup, SDG alignment
- [ ] Full demo runs start-to-finish on the demo machine **with no internet dependency**

### Explicitly out of scope

Multi-tenancy (the original Phase 5 scope) is **cut**. Serving two cities' topologies without leakage is real production work with zero judged value in an 8-day sprint. Kafka is likewise cut — the replay engine plus an in-process queue carries demo volume fine, and "we used Kafka" impresses no one if the demo stalls.

---

## 5. Risk Register

| # | Risk | Prevented by | Consequence if skipped |
|---|---|---|---|
| **R1** | **Schema migration after deception ships.** Building Phase 2 on the current flow-shaped schema forces the deception adapter to fabricate `duration_sec`/`packets`/`bytes` for events that have none. | **C4** landing before any Phase 2 code | Fabricated fields make tripwires look like flows, so the volumetric detector scores them as volume anomalies — **reintroducing the exact circular-labeling bug from Section 1.3**. All Phase 2 fixtures invalidated. Lead-time metrics become unmeasurable, killing the Phase 2/3 headline claim. |
| **R2** | **Analytics welded to the UI.** Every feature added to `aegis_demo.py` before extraction raises the eventual extraction cost. | **C1** in Phase 1 | Phase 5 becomes a rewrite, not an addition — no API, no stream consumer, no headless test possible. Phase 3 can't benchmark without a browser session. |
| **R3** | **Divergent graphs.** Two constructors exist today; Phase 2 adds gateway/decoy semantics, Phase 4 extracts features. | **C2** as the sole constructor | The UI could show a cascade path the CII engine never simulated — silent, plausible, wrong numbers that survive review because both halves look correct in isolation. |
| **R4** | **Coincidental-detection gap reopens.** If a future contributor adds a new high-criticality asset directly to `config.py` without going through `graph_manager`'s rewrite path. | The rewrite rule lives in the graph *builder*, not in `config.py` — enforced centrally, not by convention | A new protected asset could accidentally bypass the gateway entirely, recreating the exact "coincidental luck" problem the redesign in Section 1.6–1.7 was meant to close. |

### Phase 5 sprint risks (8 days, team of 3)

| # | Risk | Mitigation | Consequence if ignored |
|---|---|---|---|
| **R5** | **Frontend blocked waiting on backend.** Classic 3-person parallelization failure — B idles for days while A builds. | Ticket `#4`: mock WebSocket server ships **Day 1**. B builds every component against it and swaps to the real stream on Day 4. | Roughly a third of total team capacity lost to waiting. In an 8-day budget that is the difference between shipping and not. |
| **R6** | **Environment setup eats a day.** Docker is not installed (verified); Docker Desktop across 3 machines is a meaningful install. | Homebrew Postgres is the primary path; `docker-compose.yml` ships for the production story but nothing depends on it. | Day 1 lost to install/debug on three machines instead of building. |
| **R7** | **Integration slips past Day 4.** Backend and frontend build to different assumptions and meet late. | The contract — schema, REST routes, WS envelope — is decided **now, in writing** (`docs/PHASE5_BUILD_PLAN.md` §6–§8). Both sides build to a fixed spec, not to each other's availability. | Integration debugging consumes the Day 7–8 buffer, leaving no rehearsal time. |
| **R8** | **Scope creep consumes the buffer.** Days 7–8 quietly absorb "one more feature." | Days 7–8 are buffer **by design** and are protected. Multi-tenancy and Kafka are already cut in writing. | Demo is first run end-to-end on stage. |
| **R9** | **Graph unreadable at real data volume.** Thousands of distinct IPs in real CIC-IDS2017. | `/24` cluster aggregation, decided up front (Decision #11) — render assets, not packets. | The centerpiece visual becomes an unreadable hairball, defeating the entire reason for the rebuild. |
| **R10** | **Streaming scorer silently refits and reports meaningless scores.** `preprocess_features()` calls `fit_transform()`; naively reusing it per event makes the baseline chase the attack. | `StreamingScorer` separates fit from transform, fit runs once on benign-only warmup, `transform()` only in the stream path. Pin it with a test. | Anomaly scores look plausible and are wrong — the worst failure mode, because nothing visibly breaks. |
| **R11** | **Demo machine trains a model live and stalls on stage.** | Warmup is fitted at **build time** and `joblib`-persisted; the service loads a fitted model at boot. | Dead air during the one moment that matters. |
| **R12** | **Demo depends on the internet.** Hackathon venue wifi is a known hazard. | Local Postgres, local dataset files, no cloud calls anywhere in the demo path. Verified in the Day 7 dry run. | Total demo failure for a reason entirely outside our control. |

---

## 6. Definition of Done (project-wide)

- Dashboard runs headless; the graph is built in exactly one place (C2)
- Gateway topology is structural and verifiable in the graph object itself, not just claimed in the UI (Decisions #1, #5)
- Tripwire precision is proven by a test, not eyeballed (Decision #2)
- Lead time is a reported, reproducible number, not a claim
- No fabricated data anywhere in the UI — no invented features, no hardcoded metrics, no phantom assets, no roadmap copy describing something unbuilt
- Every edge probability in `config.py` either has a stated rationale (Phase 4) or is explicitly marked as a placeholder
- Tests skip loudly when data is missing; a green run with skips is never reported as full coverage
- Ruff clean, no duplicate definitions across `src/`, all tunables in `settings.py`

**Added for Phase 5:**

- The landing view shows **real telemetry**, not a simulation — scripted injection exists but is not the premise
- An anomaly produces a **persisted, acknowledgeable alert**, not just a number on a screen someone happens to be watching
- Every alert answers *why* in human terms (feature deviation vs. baseline), not just *what*
- The engine test suite still passes unchanged (229/229) — proof Phase 5 packaged the system rather than modifying it
- The full demo runs offline, start to finish, on the machine it will be presented from
- SDG 9 / SDG 11 alignment is stated explicitly in the README and visible in the UI

---

## 7. Suggested Ticket Order

1. ~~**Phase 1**~~ ✅ Done — `datasets/schema.py` (C4), `detectors/base.py` (C3), `graph_manager.py` (C2, gateway rewrite), `core/pipeline.py` + slimmed `aegis_demo.py` (C1), adapter/config/generator edits. 135/135 tests, live-verified.
2. ~~**Phase 2**~~ ✅ Core done — honeytoken credentials (`config.py`), `TripwireDetector`, deception adapter, recon-stage injection into all 4 scripted attacks, signal fusion in `core/pipeline.py`, gateway-only CII. 182/182 tests, live-verified. **Open:** dedicated dashboard tab, `docs/DECEPTION.md`.
3. ~~**Phase 3**~~ ✅ Done — `evaluation/` package (registry-driven detector loop, `DegenerateEvaluationError`), `evaluation/metrics.py` (segment-wise recall / row-wise precision), `evaluation/lead_time.py` (4/4 scripted attacks, mean lead time 58.4s). 229/229 tests, live-verified. The hackathon-critical path (Phases 1–3) is now complete, modulo Phase 2's two open items (dashboard tab, `docs/DECEPTION.md`).
4. **Phase 5 — ACTIVE, build now.** 8-day sprint, team of 3, 21 tickets (see the Phase 5 section above; per-ticket acceptance criteria in `docs/PHASE5_BUILD_PLAN.md`). Start with `#1`–`#5`: Postgres + schema + `StreamingScorer` + Next.js scaffold + mock WS server — these five unblock all three people simultaneously.
5. **Phase 4 — deferred.** Deepens detection science that is already ahead of what the brief requires. Revisit only if the Phase 5 sprint finishes early, and then start with the `shares_provider` correctness fix rather than new modeling.

### Open items carried forward

Two Phase 2 checklist items remain unbuilt and are **not** silently dropped:

- `docs/DECEPTION.md` — placement policy + the honest limitation (tripwires catch recon and lateral movement; they do **not** catch an attacker holding a valid stolen credential who goes straight for a real asset). Cheap to write; fold into ticket `#18`'s documentation pass.
- A dedicated deception view — **superseded by Phase 5**. The tripwire feed, gateway status, and recon-vs-exfil timeline are now first-class parts of the new operations console (tickets `#10`, `#15`), which is a better home for them than another Streamlit tab.
