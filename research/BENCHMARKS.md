# AEGIS — Autonomous Landscape Research

Date: 2026-08-21. Scope: architectural benchmarks for graph-based cyber-physical risk analytics.

---

## 1. Search Strategy (self-defined)

AEGIS sits at the intersection of four fields. I searched each for the artifact that best solves *our* specific architectural problem, not for general "best security tool" lists.

| Domain | Why it is in scope | What I searched for |
|---|---|---|
| Logical attack-graph generation | AEGIS computes blast radius over a dependency graph | How mature engines separate topology from reasoning |
| Attack-path products (identity/AD) | AEGIS has a collector → graph → UI shape | Production separation of collection, ingest, analytics, render |
| Deception / honeytokens | User's Phase 3 hypothesis | Whether deception genuinely yields *pre-compromise* signal |
| ICS/OT deception + ICS anomaly-detection evaluation | AEGIS targets a simulated water/grid city and uses SWaT | Concrete OT honeypot design; how to evaluate honestly |

Framework spine: **MITRE ATT&CK for ICS** + the **Purdue model** — used to decide *where* in the architecture a detection belongs.

---

## 2. Selected Benchmarks

### B1 — MulVAL (logical attack graphs, Datalog)
Multi-host, multi-stage vulnerability analysis; an open-source logic-based attack-graph generator built on Datalog. Its inputs are *separated by kind*: known exploits and their effects, host configuration, network/firewall configuration, principals and permissions, **interaction rules**, and policy. It emits a logical attack graph whose size is polynomial in the network, generated in quadratic time, demonstrated on fully connected networks of 1000 machines.

**Pattern extracted — declarative reasoning separated from topology.** MulVAL does not hardcode "A compromises B at p=0.8". It stores *facts* (topology, config) and *rules* (how compromise propagates) independently, then derives the graph. A derivation trace explains every edge.

**Gap it exposes in AEGIS.** `src/config.py` fuses fact and rule: each `DEPENDENCY_GRAPH` dict carries both the topology (`src`, `tgt`) and the reasoning (`prob`, `edge_type`) as a literal. There is no rule engine, so propagation semantics are hardcoded inside `cii_calculator._simulate_one_iteration`. Adding a new edge semantic today means editing simulation code.

### B2 — BloodHound / SharpHound (SpecterOps)
BloodHound CE runs the application server, a PostgreSQL config store, and a **Neo4j graph database** as separate containers. Collection is a wholly separate binary: SharpHound queries the domain over LDAP and writes a JSON/zip artifact that is *uploaded* to the app, which parses it and builds the graph. In the enterprise architecture, "the data ingestor, graph database and analytics engine may be remotely located from the data collectors."

**Pattern extracted — four hard boundaries: collector → ingest → graph store → analytics/UI.** The collector never renders. The UI never collects. Each side scales and fails independently.

**Gap it exposes in AEGIS.** `src/aegis_demo.py` (993 lines) performs *all four roles in one procedural script*: it loads data, trains the model, builds a `networkx.Graph`, computes layout, and renders Plotly — top to bottom, at module scope. Nothing in it is importable without starting Streamlit.

### B3 — Deception technology: honeytokens & canary credentials
Deception assets **have no legitimate use**, so any interaction is malicious by construction — this "eliminates false positives" and yields high-fidelity alerts without a trained model. Crucially for us: *"After an initial compromise, an attacker's primary activity is internal reconnaissance and lateral movement, which is precisely the activity that a well-placed honeytoken is designed to detect."*

**Pattern extracted — precision by construction, and signal at the recon stage.** Detection quality comes from *asset placement*, not from model sophistication. The alert fires during reconnaissance — before the payload moves.

**Why this is decisive for AEGIS.** It is the only benchmark that produces a signal *earlier in the kill chain than our current features can observe*. See §4.

### B4 — Conpot (ICS honeypot)
Open-source ICS honeypot emulating **Modbus, S7comm, SNMP, IEC 60870-5-104, BACnet, HTTP, TFTP** via **XML-defined device templates**, simulating PLCs and SCADA components. Documented limitation: responses are "largely static, offering limited adaptability to unexpected queries, and skilled attackers can identify such systems within minutes through timing analysis and response inconsistencies."

**Pattern extracted — template-defined decoys, plus an honest ceiling.** Decoy *personality* is declarative config, not code. Low-interaction deception catches scanning and opportunistic lateral movement; it does not fool a determined, patient adversary.

**Application to AEGIS.** Our decoys should be declared as data (extending `SMART_CITY_ASSETS`), not coded. And we must scope the claim: the deception layer catches **reconnaissance and lateral movement**, not a targeted adversary who fingerprints it.

### B5 — ICS anomaly-detection evaluation (ESORICS 2022 suite + point-adjust critique)
The `pwwl/ics-anomaly-detection` suite accompanies a comprehensive evaluation of reconstruction-based anomaly detection in ICS. The surrounding metrics literature is blunt about a specific trap: **point-adjust** scoring — counting any hit inside an anomaly window as a true positive — "can inflate scores by masking timing errors," to the degree that "a detection algorithm outputting random noise is expected to produce very good scores, and capable of outperforming state of the art methods." Recommended instead: range/segment-wise precision and recall, or no post-hoc adjustment at all.

**Pattern extracted — the evaluation protocol is part of the architecture.** On a time-series ICS dataset, the metric definition can manufacture state-of-the-art numbers from noise.

**Direct relevance.** We already hit the adjacent failure: `run_evaluation()` defaulted to SWaT, whose first rows are all `Normal`, producing 0.000 P/R/F1 and `nan` AUC. Fixed, but it proves the evaluation path is load-bearing and currently unguarded.

### Framework spine — MITRE ATT&CK for ICS + Purdue
Use ATT&CK **Enterprise** for upper Purdue levels (historians, workstations) and ATT&CK **for ICS** for levels 0–2 (PLCs, sensors, actuators). Lateral movement is modeled across the real architecture, where "the Purdue model layers and identified pivot points determine which paths exist between initial access and target assets," and "strong segmentation lowers the probability that lateral movement succeeds."

**Application.** This is the principled source for `prob` on our dependency edges — segmentation strength between Purdue levels — replacing today's hand-assigned literals.

---

## 3. Consolidated Pattern Table

| # | Pattern | Benchmark | AEGIS today | Phase that fixes it |
|---|---|---|---|---|
| P1 | Collector / ingest / analytics / UI are separate | B2 | All four inline in `aegis_demo.py` | Phase 1 |
| P2 | Facts separated from propagation rules | B1 | Fused in `config.DEPENDENCY_GRAPH` | Phase 1 → 4 |
| P3 | Signal from zero-legitimate-use assets | B3 | None | Phase 2 |
| P4 | Decoys declared as templates, not code | B4 | None | Phase 2 |
| P5 | Evaluation protocol is a first-class artifact | B5 | Single unguarded `evaluation.py` path | Phase 3 |
| P6 | Detection placed by Purdue level | Spine | No level model on assets | Phase 1 (schema) → 2 |

---

## 4. The Finding That Reframes The Project

The user's hypothesis was that AEGIS "counts threats by the number of unauthorised IP addresses." **The mechanism is different; the conclusion is correct, and the true situation is worse.**

**What actually happens.** `THREAT ANOMALIES` is `edges_df['is_anomaly'].sum()` (`src/aegis_demo.py:485`) — the output of an unsupervised `IsolationForest` over exactly three volumetric features (`duration_sec`, `packets`, `bytes`). No IP is consulted at scoring time.

**Finding 1 — the synthetic demo is circular.** In `src/data_generator.py:104`:

```python
if src in threat_ips and random.random() < anomaly_rate:
    duration = ...; bytes_transferred = 50–1500 MB; packet_count = ...
```

Anomalies are injected **only** on edges whose source is one of the three hardcoded `EXTERNAL_THREAT_IPS`. The label is created by the IP list, expressed as a volume spike, and then "discovered" by IsolationForest as a volume spike. The demo cannot fail and demonstrates nothing about detection.

**Finding 2 — every signal is post-compromise.** All four scripted attacks trigger on completed transfers: `bytes: 500000000`, `500000000`, `1500000000`, `20000000` (`src/aegis_demo.py:362–389`). The detection event *is* the exfiltration record. The CII then computes the blast radius of a breach that has already finished.

**Finding 3 — early detection is impossible with the current feature set, at any model quality.** `duration_sec`, `packets`, and `bytes` are all **terminal aggregates of a completed flow**. They cannot exist before the payload has moved. No amount of model sophistication — GNN, transformer, conformal prediction — can move detection earlier, because the observation itself is lagging. This cannot be fixed by improving the model. It can only be fixed by changing what is observed.

**Conclusion.** AEGIS is today a **post-hoc blast-radius calculator**, not an early-warning system. The user's instinct is right, and the deception layer (B3/B4) is not an optional enhancement — it is the only architecturally sound way to introduce a pre-compromise signal, and it happens to emit exactly the input the CII engine already consumes: *"asset X was touched by an adversary."*

---

## Sources

- [A scalable approach to attack graph generation (MulVAL lineage, ACM CCS)](https://dl.acm.org/doi/10.1145/1180405.1180446)
- [A Survey of MulVAL Extensions and Their Attack Scenarios Coverage (arXiv)](https://arxiv.org/pdf/2208.05750)
- [Finding Software Supply Chain Attack Paths with Logical Attack Graphs](https://upsilon.cc/~zack/research/publications/fps-2025-ssc-mulval.pdf)
- [BloodHound Glossary — SpecterOps](https://bloodhound.specterops.io/resources/glossary/overview)
- [System and method for continuous collection, analysis and reporting of attack path choke points (USPTO 11539725)](https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/11539725)
- [Understanding Honeytokens: Functions and Different Types — Acalvio](https://www.acalvio.com/resources/glossary/honeytoken/)
- [Canary Tokens & Honeytokens: Beyond the Alert with Deception — CounterCraft](https://www.countercraftsec.com/blog/canary-tokens-honeytokens-explained/)
- [Honeypot-Factory: The Use of Deception in ICS/OT Environments — The Hacker News](https://thehackernews.com/2023/02/honeypot-factory-use-of-deception-in.html)
- [The ICS Defender's Guide To Conpot](https://undercodetesting.com/the-ics-defenders-guide-to-conpot-deploying-a-honeypot-to-decipher-attacker-behavior/)
- [LLMPot: Dynamically Configured LLM-based Honeypot for Industrial Protocol and Physical Process Emulation (arXiv)](https://arxiv.org/pdf/2405.05999)
- [pwwl/ics-anomaly-detection — ESORICS 2022 evaluation suite](https://github.com/pwwl/ics-anomaly-detection)
- [Did We Actually Fix It? Adversarial Stress-Test of Post-Point-Adjustment Metrics (arXiv)](https://arxiv.org/html/2607.11969v1)
- [Navigating the Metric Maze: A Taxonomy of Time-Series Anomaly Detection Metrics (arXiv)](https://arxiv.org/pdf/2303.01272)
- [Detection Engineering in ICS — Ukraine 2016 Case Study (MITRE)](https://www.mitre.org/sites/default/files/2022-04/pr-22-0094-detection-engineering-in-industrial-control-systems-ukraine-2016-attack-case-study.pdf)
- [MITRE ATT&CK for ICS Explained: Tactics, Techniques, and Cross-Domain Attack Paths — DeNexus](https://www.denexus.io/learn/articles/mitre-attck-for-ics-explained-tactics-techniques-and-cross-domain-attack-paths)
- [The Purdue Model for OT and ICS Security Explained — Asimily](https://asimily.com/blog/leveraging-the-purdue-model-to-understand-your-organizations-ics-security-needs/)
