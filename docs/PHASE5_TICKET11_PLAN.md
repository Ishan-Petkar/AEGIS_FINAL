# Ticket #11 Plan — city graph + `/24` cluster aggregation

Planning authority: this document. `docs/DESIGN_CONSOLE.md` governs visuals.

---

## 1. Scope

Replace `GraphPanel`'s placeholder with a real force-directed graph.

**In scope:** `react-force-graph-2d` canvas render of the curated topology
from `GET /api/topology`, `/24` cluster aggregation derived from the live
event stream, anomaly pulsing, node styling per asset type, and the
resolution of **K8**.

**OUT of scope:** CII cascade animation (**#14**), ack flow (#15),
`/api/stats` (#16), inject (#13).

---

## 2. Decision: K8 is resolved as "two honest layers" (D11-1)

**This is the ticket's real decision, and it has been open since Ticket #7.**

`docs/PHASE5_BUILD_PLAN.md` §5 assumed real IPs map onto curated assets via
`AssetRegistry.resolve()` — "already built, already tested". K8 measured
that assumption false: **0 of 20,000** real friday-morning source IPs
resolve to a dependency-graph node, because real CIC-IDS2017 hosts are
`192.168.10.x` while every curated asset is `10.0.1.x`.

The tempting fix — inventing synthetic edges from cluster nodes into the
curated topology so the cascade has somewhere to go — is **rejected**. It
would fabricate dependency relationships that do not exist, and the CII
number computed over them would be meaningless. That is precisely the kind
of fake-signal this project has repeatedly refused (P5-15, D8-3).

**Instead the graph renders two visually distinct layers:**

1. **City asset model** (curated) — the 16 nodes and 20 dependency edges
   from `GET /api/topology`. This is the *risk model*: what depends on
   what, and what the CII engine reasons over.
2. **Observed traffic** (live) — `/24` cluster nodes derived from the
   event stream, sized/badged by flow count, with edges representing
   *observed flows* between clusters.

The two layers are **not** joined by invented edges. They are genuinely
disconnected for ambient replay traffic, and the UI should **say so**
rather than hide it — a small honest caption (e.g. "observed capture
traffic does not intersect the modelled city assets") turns a limitation
into a credible statement about what the system does and does not know.

When Ticket #13's injection runs, injected attacks name **curated assets**
(`generate_scripted_attack` passes asset names, not IPs), so the cascade
lands on layer 1 and the demo's headline moment works exactly as intended.

---

## 3. Decision: clustering is bounded and throttled (D11-2)

Ticket #10 measured the real stream at **2000 events/s** at `speed=500`.
Cluster derivation must therefore never run per event, and the node count
must never grow unbounded (risk T5: one node per unique IP → thousands).

- Aggregate observed IPs into `/24` buckets, maintained incrementally in a
  `Map`, updated on the **same throttled cadence** Ticket #10 uses for the
  feed (~1 frame), never per message.
- **Cap rendered clusters at the top N by flow count** (N from a constant,
  default 24) and roll the remainder into a single `other` node carrying
  its own aggregate count. This is what keeps the graph in the "readable
  ~30–60 node" range §5 asks for.
- Cluster labels use the real CIDR form, e.g. `192.168.10.0/24 ×1,284`.
- Never re-seed the force simulation from scratch on every update — mutate
  the node/link arrays and let the simulation settle, or the graph will
  visibly thrash.

---

## 4. Node and edge styling

Per `DESIGN_CONSOLE.md` §6:

- **Curated infrastructure** — circle, sized by `criticality`.
- **Financial assets** — diamond, `--financial` (gold), never used elsewhere.
- **Gateway/chokepoint** — larger, outlined ring (they are the Purdue
  chokepoints and should read as structural).
- **`/24` clusters** — visually distinct from curated nodes (e.g. hollow /
  dashed / muted), so a viewer can never mistake observed traffic for a
  modelled asset. Layer 2 must not masquerade as layer 1.
- **Anomaly pulse** — a node touched by an anomalous event pulses
  `--sev-warning`; `tripwire_fired` pulses `--sev-critical`. Respect
  `prefers-reduced-motion` (§4): no pulsing when reduced motion is set.
- Edges: curated dependency edges styled per `edge_type`; observed-flow
  edges thinner and dimmer, so the risk model reads as primary.

---

## 5. States and interaction

- Loading / topology-unreachable / empty-stream states are all required —
  never a blank panel, and the copy must be literally true (this has been
  a real defect twice now).
- Hovering a node shows its identity and, for curated assets, criticality;
  for clusters, the flow count.
- The graph shares the existing `useStream()` context (Ticket #10's fix) —
  **do not call `useEventStream()` directly**, or you reintroduce the
  duplicate-socket defect that fix removed.
- Topology comes from `useConnection()`-driven fetch, consistent with
  `GraphPanel`'s existing recovery behaviour.

---

## 6. Dependency

`react-force-graph-2d` was deliberately deferred from Ticket #3 to keep
the scaffold small. Add it now as a normal dependency. It renders to
canvas, which is what makes this node count viable.

---

## 7. Verification

```bash
cd frontend && npx tsc --noEmit && npm run lint && npm run build
git status --short src/ backend/     # empty
```

In a browser, against the **real** stream (mock stays down):
1. Curated layer renders 16 nodes / 20 edges, correct shapes per type.
2. Start a real replay; `/24` clusters appear with real CIDR labels and
   real counts (`192.168.10.0/24 ×N`).
3. **High-rate test:** `speed=500`. Confirm the graph stays responsive,
   the cluster cap holds, and the simulation does not thrash. Report
   measured node count and observed frame behaviour.
4. Anomalous events pulse the right nodes; reduced-motion disables it.
5. Exactly **one** WebSocket per tab still (do not regress Ticket #10's
   fix) — measure it.
6. Zero console errors in a fresh tab. Screenshot.

---

## 8. Constraints

- **No synthetic data.** Real stream only; the mock stays down.
- Do not modify `src/` or `backend/`.
- No raw hex/`rgba()` in components — tokens only.
- Do not invent edges between observed clusters and curated assets (D11-1).
- Commit nothing.
