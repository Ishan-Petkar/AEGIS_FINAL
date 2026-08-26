# Ticket #13 Plan — `POST /api/inject` (real attack scenarios)

Planning authority: this document.

---

## 1. The constraint that reshapes this ticket

The original design (`generate_scripted_attack` in `src/data_generator.py`)
**fabricates** attack events. That is now disallowed: no synthetic data
anywhere in the product.

It is also unnecessary. The capture already contains large volumes of
**real, labelled attack traffic**, verified present on this machine:

| Day | Real attack label | Flow count |
|---|---|---|
| friday-morning | `Bot` | **1,966** |
| friday-afternoon-DDos | `DDoS` | **128,027** |
| friday-afternoon-PortScan | `PortScan` | **158,930** |

So injection replays **real captured attack flows**, not invented ones.

---

## 2. Decision: real behaviour, operator-chosen target (D13-1)

There is one genuine tension. For the demo's payoff — *"inject a breach →
watch the blast radius cascade"* — the injected traffic must implicate a
**curated asset**, because K8 established that real capture IPs
(`192.168.10.x`) resolve to nothing in the dependency graph, so CII would
be all zeros and no cascade would render.

Two options were considered:

- **(A)** Replay real attack flows with their real IPs untouched. Fully
  faithful, but produces no CII and no cascade — the demo's second act
  does nothing.
- **(B)** Replay real attack flows with their **source/destination set to
  the curated asset the operator selected**, preserving every real traffic
  characteristic (bytes, packets, duration, protocol, ports, timing,
  label).

**Choose (B), and label it precisely.** This is exactly the question the
CII engine exists to answer — *"if this asset were compromised, what else
falls over?"* — applied to a real observed attack pattern rather than an
imagined one. The flow *behaviour* is real captured Bot/DDoS/PortScan
traffic; only the *target* is the operator's choice.

**It must be unmistakable in the product that this is a what-if
scenario, not observed telemetry:**
- Every injected event carries `batch_origin = injected`
  (`BATCH_ORIGIN_INJECTED` already exists in `BatchMeta`) and is persisted
  with it in `events.raw`.
- The response and the resulting alert state the scenario name and that
  the flows are real capture traffic re-targeted for a what-if.
- The UI must never present injected events as observed capture traffic.

Do **not** use `generate_scripted_attack()`. It fabricates flows.

---

## 3. Decision: the honeytoken tripwire (D13-2)

A honeytoken touch **cannot** exist in a 2017 public capture — the
honeytoken is AEGIS's own planted credential, part of its deception
instrumentation, not something an external dataset can contain.

So one scenario sets `is_honeytoken_use` on the real injected flows,
representing the planted credential being used by the attacker. The
telemetry stays real; only the deception-layer flag is set. This is the
system's own control signal, not fabricated data, and must be documented
as such.

This matters because `docs/DETECTION_STUDY.md` §5 measured the tripwire as
the **only** channel that catches novel threats (unsupervised P≈0.02;
supervised precision 0.000 on unseen families; tripwire perfect with zero
training data). Dropping it would remove the project's strongest
empirical claim.

---

## 4. Scope

**In scope:**
1. `POST /api/inject` with `{scenario, target_asset?, count?}`.
2. Scenario registry mapping names → (real dataset day, real label).
   At minimum: `bot_c2`, `ddos`, `port_scan`, plus one honeytoken variant.
3. Reading real labelled flows via `ReplayFlowReader`, filtered on the
   real `label`, re-targeted per D13-1.
4. Handing them to the existing `ReplayEngine.inject(flows)` — do not
   write a second injection path.
5. `GET /api/inject/scenarios` so the UI can list them rather than
   hardcoding.
6. Tests.

**OUT of scope:** the UI control (part of #14/#15 polish), cascade
animation (#14), ack (#15), `/api/stats` (#16).

---

## 5. Correctness requirements

- **Bounded.** `count` must be capped by a new
  `BACKEND_SETTINGS.inject_max_flows` (default 500). An unbounded inject
  hands the engine an arbitrarily large burst.
- **503** when the scorer never loaded (same as replay control).
- **422** for an unknown scenario or an unknown `target_asset` — validate
  the asset against `build_criticality_map()` (the graph authority),
  mirroring `/api/cii/{asset}`'s 404-on-unknown rule. Never silently
  accept an asset that will produce an all-zero CII.
- Injection must work **whether or not** a replay is currently running —
  `ReplayEngine.inject()` queues onto the next tick, and the engine must
  not need to be started first. If it does, say so explicitly rather than
  silently no-oping.
- Reading real attack flows must not re-read a 75-280MB CSV on every
  call. Cache the filtered attack flows per scenario after first load.

---

## 6. Verification

```bash
PYTHONPATH=src ./venv/bin/python -m pytest tests/ -q   # 494 + new
./venv/bin/ruff check src/ backend/ --select E,F,W --ignore E501
git status --short src/                                 # empty
```

End-to-end, with real data:
1. `GET /api/inject/scenarios` lists the real scenarios.
2. `POST /api/inject {"scenario":"bot_c2","target_asset":"City_Payment_Gateway"}`.
3. Confirm over the live WebSocket that injected `event` envelopes arrive
   and that their persisted rows carry the **real** attack label
   (`raw.label = "Bot"`) and `batch_origin = injected`.
4. Confirm the honeytoken scenario produces a **tripwire alert** and a
   **non-zero CII snapshot** naming the target asset — this is the demo's
   payoff and must be demonstrated, not assumed.
5. Report the real numbers: flows injected, label seen, alert id,
   CII median/p5/p95, impacted assets.

---

## 7. Constraints

- `src/` untouched (Invariant A). Do not call `generate_scripted_attack`.
- Invariant B: no model fit.
- Real captured flows only; nothing fabricated.
- Commit nothing.
