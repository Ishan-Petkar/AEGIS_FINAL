# AEGIS Scalability & Performance Audit

Read-only investigation. No application code, config, or database was modified to produce this report.

## 1. Executive verdict

The pipeline has never crashed or dropped data under sustained real-flow load, and the recent architecture choices (per-client bounded WS queues with oldest-drop, debounced+cached CII, capped retention, single ingest transaction per batch) are all deliberate and load-aware. But the one throughput number this project has ever measured — **1,858.8 flows/sec** — was measured against `IngestPipeline.ingest_batch()` driving the volumetric scorer, supervised scorer, signature engine, beaconing detector, and fusion engine (`AEGIS_Judge_Room_Dossier.md:351-364`). **T-GNN is not in that list.** T-GNN was added to the pipeline after that load test, is now a sixth detector inside the same `ingest_batch()` call (`backend/ingest.py`), and its own cost driver — periodic `IsolationForest` refit every `tgnn_refit_every_batches` (100) batches over up to `tgnn_max_training_rows` (5000) buffered rows — has never been exercised at the 1,858.8 flows/sec pace. Treat that figure as **stale for current 6-channel capacity**, not wrong for what it measured.

Everything else in this report follows the rule the task set: verified fact (cited), measured performance (cited to the dossier's own load test), suspected bottleneck (reasoned from code, not measured), proposed change, or open unknown. Nothing here invents a number.

## 2. Verified architecture (as it exists today)

```
ReplayEngine (1 bg thread, in-process, threading.RLock/Lock)
  -> micro-batches (<=500 flows, replay_max_batch_size)
  -> IngestPipeline.ingest_batch()
       score_batch (volumetric) -> tripwire_flags -> fuse_tripwire_confidence
       -> compute_hybrid_decisions (signature + beaconing + tgnn + fusion)
       -> resolve identities
       -> ONE session_factory() transaction: persist_events + persist_scores + _handle_anomalies
            _handle_anomalies: is_anomaly OR is_injected -> CII (debounced+cached) -> alert_decision
       -> broadcast AFTER commit
  -> ws_broadcaster: per-client bounded asyncio.Queue, drop-oldest on overflow, one writer task/client
  -> frontend useEventStream: 200-event ring buffer, GET /api/events?since=<id> backfill on reconnect
```

Single Postgres engine, created lazily, process-wide singleton (`backend/db.py`), pool_size=5 + max_overflow=10 (`backend/config.py:98-118`) — i.e. at most 15 concurrent DB connections per backend process, no more.

## 3. Evidence table

| # | Claim | Type | Citation |
|---|---|---|---|
| 1 | Sustained throughput 1,858.8 flows/sec, 2,830,743 flows, 5,665 batches, 0 exceptions | Measured | `AEGIS_Judge_Room_Dossier.md:351-364` |
| 2 | That load test's detector set does **not** include T-GNN | Verified (by omission in the same passage) | `AEGIS_Judge_Room_Dossier.md:353` |
| 3 | p95 per-batch (500 flows) latency 290-460ms, max spikes 1.3-2.5s | Measured | `AEGIS_Judge_Room_Dossier.md:359` |
| 4 | T-GNN refits IsolationForest every 100 batches over <=5000 buffered rows, not O(graph) per flow | Verified | `backend/detection/tgnn.py` (this session's own rewrite) |
| 5 | DB pool: 5 persistent + 10 overflow = 15 max connections/process, 30s timeout, pre-ping on | Verified | `backend/config.py:98-121` |
| 6 | Retention: 500,000-row cap, checked every 200 batches (=100,000 flows), each check issues a full `COUNT(*)` then a `DELETE` | Verified | `backend/config.py:621-634`, `backend/retention.py:78,92` |
| 7 | Indexes present: `ix_events_source_asset`, `ix_events_ts_desc`, `ix_event_scores_event_id`, `ix_alerts_acknowledged_ts_desc`, `ix_ips_actions_target_asset`, `ix_ips_actions_ts_desc` | Verified | `backend/models.py:187-401` |
| 8 | ReplayEngine is a single `threading.Thread` per process; no cross-process/distributed lock guards it | Verified | `backend/replay_engine.py:22-407` |
| 9 | WS backpressure: bounded per-client queue (`ws_client_queue_max`), overflow drops the OLDEST envelope, one writer task per client, one client's failure never affects another | Verified | `backend/ws_broadcaster.py:27-281` |
| 10 | Frontend keeps last 200 events client-side; reconnect backfills via `GET /api/events?since=<id>`, not by replaying the full history | Verified | `frontend/src/lib/useEventStream.ts:48,244-252` |
| 11 | CII is debounced per origin asset (`cii_debounce_sec`=30s default) and cached (`cii_cache_max_entries`) — most batches during a sustained attack reuse a cached result rather than re-running Monte Carlo | Verified | `backend/ingest.py:1799-1833` |
| 12 | CII Monte Carlo default: 1000 iterations per (uncached) call, each a probabilistic BFS bounded by `bfs_max_hops` | Verified | `src/settings.py:45-48`, `src/cii_calculator.py:94-419` |
| 13 | 2,554,386 events pruned across the load test's two runs — retention held under peak ingest | Measured | `AEGIS_Judge_Room_Dossier.md:364` |

## 4. Ranked bottlenecks and unknowns

**Suspected bottlenecks (reasoned, not measured):**
1. **T-GNN's added per-batch cost is unmeasured at throughput.** Feature extraction is bounded per-node history, not O(graph²), but the periodic full-refit on up to 5000 rows every 100 batches is a synchronous call inside the same `ingest_batch()` that the 1,858.8 flows/sec figure was measured without. This is the single largest gap between "measured" and "current."
2. **Retention's `COUNT(*)` every 200 batches** is a full-table count on a table sized up to 500,000 rows, issued synchronously inside the ingest path's cadence (not inside the per-batch DB transaction itself, but on the same thread). Cost scales with table size, not batch size.
3. **Single ReplayEngine thread, single DB engine, no multi-instance coordination.** Running two backend processes against the same Postgres would double-ingest the same replay data with no lock preventing it — this is a horizontal-scaling blocker, not a throughput one.
4. **DB pool ceiling (15 connections/process)** is untested against concurrent operator API traffic (dashboard polling, `/api/inject`, IPS actions) layered on top of the ingest thread's own connection use.

**Open unknowns (no evidence found either way):**
- Whether `tgnn_refit_every_batches`/`tgnn_max_training_rows` were tuned with the 1,858.8 flows/sec target in mind, or independently of it.
- Actual wall-clock cost of one IsolationForest refit on 5000 rows in this environment — not benchmarked anywhere in the repo.
- Whether `graph_manager.build_graph()` is called once at startup and reused, or rebuilt per request — not confirmed in this pass (would need `backend/graph_manager.py` read in full).
- No dedicated benchmark harness or load-test script exists in the repo (`research/BENCHMARKS.md` is 119 lines, not a runnable tool) — the dossier's figure came from a one-off manual run, not a repeatable script.

## 5. What this does NOT justify

Per the original task's explicit constraint: no recommendation below defaults to Redis/Kafka/Kubernetes. Nothing in the evidence above shows a queueing, message-broker, or orchestration bottleneck — the actual friction points found (T-GNN refit cost, retention COUNT cost, single-instance replay) are algorithmic/architectural, not infrastructure-scale ones, and would not be fixed by any of those tools.

## 6. Proposed 2-hour sprint (read-only proposal — not implemented)

1. **Benchmark T-GNN's real marginal cost** (~45 min): re-run the dossier's exact load-test methodology (`IngestPipeline.ingest_batch()` back-to-back, no pacing) with T-GNN enabled vs. a config flag disabling it, same CIC-IDS2017 corpus, and report the delta in flows/sec and p95 batch latency. This directly answers the open question in §4 without guessing.
2. **Instrument the retention `COUNT(*)`** (~30 min): log its wall-clock cost at 100k/300k/500k rows to confirm or refute it as a real bottleneck before touching it.
3. **Write up findings** (~45 min): fold the two measurements above into this document's evidence table, converting "suspected" to "measured" or striking the concern.

**Fallback** if the load-test harness proves nontrivial to reconstruct: skip step 1, only do step 2 (retention is far cheaper to instrument), and flag T-GNN cost as still-open rather than force a number.

**Stretch**: also measure DB pool exhaustion by simulating concurrent `/api/inject` calls during a sustained replay run.

## 7. Benchmark / acceptance / rollback plan

- **Benchmark**: same corpus and methodology as the existing dossier test (`datasets/'TrafficLabelling '/`, all 8 CIC-IDS2017 days, `replay_max_batch_size`=500, no wall-clock pacing) so the new number is directly comparable to 1,858.8 flows/sec rather than a differently-shaped measurement.
- **Acceptance**: no crashes/dropped batches (matching the existing "0 of 5,665" bar); report the new sustained flows/sec and p95 latency alongside the old ones, labeled by detector-set version.
- **Rollback**: this sprint is measurement-only — nothing to roll back. Any config change proposed afterward (e.g. adjusting `tgnn_refit_every_batches`) should follow the existing `SETTINGS`/`BACKEND_SETTINGS` optional-override pattern so it's a one-field revert.

## 8. Staged longer-term architecture (proposal, not scoped to evidence at this depth)

Only sketched at the level the current evidence supports — deeper claims would need the open unknowns in §4 resolved first:
- **Stage 1**: make the T-GNN refit cost visible in `/metrics` (already exists per this session's earlier ROI work) so it's monitored in situ rather than only in a one-off benchmark.
- **Stage 2**: if retention COUNT(*) proves costly, maintain a running row-count counter updated on insert/delete instead of counting on demand — a targeted fix, not a new dependency.
- **Stage 3**: if multi-instance replay is ever required, that needs an explicit single-writer lock (e.g. a Postgres advisory lock via `backend/db.py`'s existing engine) — deferred until horizontal scaling is an actual requirement, not a hypothetical one.

## 9. Claims made now vs. claims needing more evidence

**Made now (cited above)**: pool/retention/index config, WS backpressure design, CII debounce/cache, T-GNN's algorithmic cost shape, the exclusion of T-GNN from the existing load test.

**Needs more evidence before claiming**: T-GNN's actual wall-clock refit cost; retention COUNT(*) actual cost at scale; `graph_manager.build_graph()` call frequency; concurrent-API behavior under the 15-connection pool ceiling.

## 10. Open questions / missing prerequisites

- Is there appetite to re-run the load test (it takes ~25 minutes wall-clock per the dossier's own 1,522.9s figure) as part of this sprint, or should the benchmark step be scheduled separately from this audit?
- `backend/graph_manager.py` was not read in full this pass — needed before any claim about topology-rebuild cost.
- No existing repeatable benchmark script was found; building one (per §6) is itself a prerequisite for making future throughput claims non-manual.
