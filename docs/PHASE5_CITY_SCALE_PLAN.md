# City Scale-Up Plan — 16 → ~50 nodes, hub-centred layout

Planning authority: this document.

---

## 0. This deliberately breaks Invariant A

Every Phase 5 ticket has held **Invariant A: `src/` is never modified**,
verified per ticket with `git status --short src/`. This change edits
`src/config.py` on explicit instruction to scale the city.

That makes the **229 engine regression tests the safety net**. They must
still pass. If they do not, the change is wrong — not the tests.

---

## 1. Hard rule: additive only (D-C1)

**Every one of the 11 existing assets keeps its exact name, IP,
criticality, and Purdue level. Every existing dependency edge stays.**

This is not conservatism, it is what makes the change survivable: tests
across `tests/conftest.py`, `test_api.py`, `test_backend_models.py`,
`test_cii_calculator.py`, `test_graph_manager.py`, `test_ingest.py`,
`test_inject.py`, `test_asset_registry.py`, `test_core_pipeline.py` and
`test_deception_tripwire.py` reference these assets **by name**
(`Traffic_Cam_1`, `City_Grid`, `City_Payment_Gateway`,
`Traffic_Controller`, …). Renaming or re-scoring any of them breaks the
suite for no benefit.

Verified in advance: **no test asserts an exact CII value or impacted-set
size**, so adding nodes will not break the CII tests on numbers alone.

---

## 2. Target shape

**~44 curated assets + auto-synthesised gateways ≈ 50 nodes.**

Gateways are **not** hand-written — `graph_manager` materialises
`Gateway_L{n}` automatically for any asset with
`criticality >= SETTINGS.gateway.criticality_threshold` (0.85). So
criticality choices directly control how many gateways appear. Do not add
gateway entries to `SMART_CITY_ASSETS`.

### Sectors (the "organised" half of "crazy but organised")

| Sector | Assets (existing kept, new added) |
|---|---|
| **Operations (hub)** | `City_Operations_Center` ← **the central hub** |
| Energy | `Power_Substation`*, `City_Grid`*, + substation beta, solar array, battery storage, grid SCADA |
| Water | treatment plant, pump station, quality sensors, wastewater |
| Transport | `Traffic_Cam_1`*, `Traffic_Cam_2`*, `Traffic_Controller`*, + metro signalling, bridge sensors, EV charging |
| Public safety | `Emergency_Route_System`*, + fire dispatch, police CAD, EMS dispatch, siren network |
| Health | hospital network, ambulance telemetry, health registry |
| Telecom / IT | fibre backbone, municipal DNS, data centre, identity provider, backup vault |
| Finance | `City_Payment_Gateway`*, `Bank_Partner_API`*, `Municipal_Bond_Platform`*, `Social_Welfare_System`*, + tax collection, payroll |
| Civic | `Citizen_Portal`*, + permits, records archive, voting infrastructure |
| Environment | air quality, flood sensors, waste management |
| Monitoring | `SCADA_Historian`*, + log aggregator, threat-intel feed |

`*` = existing, unchanged.

---

## 3. The hub (D-C2)

`City_Operations_Center` — Purdue 3, criticality **0.98**, IP in the
`10.0.1.x` block.

It is the visual and logical centre: it **monitors** every sector.

**Edge direction matters and must not be got backwards.** In this graph
`A → B` means *"if A is compromised, B may fall"*. A compromised SOC
holds credentials and management access into the sectors it operates, so
the hub gets **outbound** `controls` / `communicates_with` edges to each
sector's controller. That is both realistic and what makes the hub the
most consequential node in the city — the demo's best "what if this
falls" target.

Sector telemetry flowing *into* the SOC is a separate, weaker inbound
relationship; model a few of those, but keep them low-probability so a
compromised sensor does not trivially own the whole city.

---

## 4. Edge design rules (D-C3)

- Every new edge needs the full metadata shape already used
  (`src, tgt, edge_type, prob, source, owner, rationale, confidence,
  last_reviewed`). The `rationale` must state a real engineering reason —
  it is shown in the UI and is part of the project's honesty claim.
- `prob` must reflect genuine coupling strength. A sensor feeding a
  controller is high; a log collector touching a controller is low (the
  existing `SCADA_Historian` edges use 0.2 for exactly this reason —
  follow that precedent).
- Physical dependencies (power, network) should be higher probability
  than advisory/data ones.
- Avoid making everything critical. If most assets sit above the 0.85
  gateway threshold the graph becomes all-gateways and the Purdue story
  stops meaning anything. Aim for a realistic spread: a handful at
  0.9–1.0, most in 0.3–0.8.

---

## 5. Watch for CII saturation (D-C4)

**This is the main correctness risk of scaling up.** Per-iteration impact
is `anomaly_score × Σ criticality(compromised nodes)`, clamped to
`SETTINGS.cii.cii_max_value`. Tripling the node count triples the
reachable criticality mass, so CII may now **saturate at the clamp for
every asset**, making the score meaningless — every alert would read
"maximum impact".

**Required check:** after the graph change, compute CII for several
assets of genuinely different importance (e.g. `Traffic_Cam_1` vs
`City_Payment_Gateway` vs `City_Operations_Center`) and confirm the
results still **spread**, rather than all pinning to the max. If they
saturate, report it — the fix is a settings change, not a graph hack, and
it is the user's call.

Also confirm Monte Carlo runtime stays acceptable: CII now walks a much
larger graph, and `/api/cii/{asset}` is called on demand from the UI.

---

## 6. Frontend: hub-centred radial layout (D-C5)

The current `computeCuratedLayout` in `CityGraph.tsx` lays nodes out in
**columns by Purdue level**. At 50 nodes that becomes a wall of text, and
it cannot express "hub in the middle".

Replace with a **concentric / radial sector layout**:
- `City_Operations_Center` pinned at the **centre**.
- Each sector occupies an angular wedge around it.
- Within a wedge, assets are placed by Purdue level as **distance from
  centre** (operational/field tech further out, enterprise closer in) —
  so the Purdue story survives, expressed as radius instead of column.
- Keep nodes pinned (`fx`/`fy`) — Ticket #14 established that a stationary
  curated layer is what makes both labels and the cascade readable.
- Preserve the existing greedy label-collision placement; with 44 labels
  it matters far more, not less. If labels still collide at 50 nodes,
  prefer sector-coloured dots with labels on hover for the low-criticality
  periphery, but the hub, all financial assets, and any cascade-involved
  node must stay permanently labelled.

Keep every Ticket #11/#14 property: the two-layer separation from the
`/24` cluster layer, the honest caption, the 24-cluster cap, cascade
animation driven by the real `impacted` payload, and reduced-motion.

---

## 7. Verification

```bash
PYTHONPATH=src ./venv/bin/python -m pytest tests/ -q   # 518 baseline
./venv/bin/ruff check src/ backend/ --select E,F,W --ignore E501
```

The engine subset is the real gate — if `tests/test_cii_calculator.py`,
`test_graph_manager.py`, `test_core_pipeline.py` or
`test_deception_tripwire.py` fail, the graph is malformed.

Then, live:
1. `GET /api/topology` reports ~50 nodes; count gateways separately.
2. CII spread check from §5, with real numbers reported.
3. Browser: hub visibly centred, sectors readable, labels legible.
4. `POST /api/inject {"scenario":"honeytoken","target_asset":"City_Payment_Gateway"}`
   → cascade still animates correctly on the larger graph.
5. Feed, alerts, one-socket-per-tab, responsive widths: unregressed.

---

## 8. Constraints

- `backend/` unchanged — this is a data + frontend-layout change.
- Existing 11 assets and all existing edges untouched (D-C1).
- New assets need `10.0.1.x` IPs so `AssetRegistry` resolves them and
  they are valid `POST /api/inject` targets.
- No raw hex in components; tokens only.
- Commit nothing until the suite is green.
