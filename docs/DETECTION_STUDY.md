# Detection Capability Study — why the volumetric detector misses, and what actually works

**Date:** 2026-08-24 · **Status:** authoritative, measurement-backed
**Trigger:** Ticket #5 planning found the volumetric detector had near-zero precision on real replayed traffic. This document is the investigation that followed.

All numbers below are reproducible from this repo against
`datasets/TrafficLabelling /`. Nothing here is estimated or asserted
without a measurement.

---

## 1. The finding that started it

Fitting the existing detector (`IsolationForest` over `duration_sec`,
`packets`, `bytes`) on Monday's all-benign traffic and scoring other days:

| day | attack % | ROC AUC | TP | FP | precision |
|---|---|---|---|---|---|
| friday-morning (landing day) | 1.06% | 0.585 | 5 | 811 | **0.006** |
| wednesday (DoS Hulk) | 21.35% | 0.778 | 0 | 632 | **0.000** |
| friday-afternoon-portscan | 40.55% | 0.480 | 0 | 469 | **0.000** |
| friday-afternoon-ddos | 64.20% | 0.518 | 0 | 174 | **0.000** |
| tuesday (brute force) | 5.29% | 0.519 | 0 | 736 | **0.000** |

Near-zero precision everywhere. On friday-morning the five most-anomalous
flows are all **benign** bulk transfers.

**Not a threshold problem.** Recalibrating the threshold to the target
stream's top 1% does not rescue it — Wednesday still yields 0 TP despite
AUC 0.778. The attacks rank moderately anomalous; the extreme tail is
dominated by large benign transfers.

**Root cause, first layer:** Bot C2 beaconing is *low-volume by design* —
median 6 bytes vs 70 for benign traffic. A detector built on volume is
structurally blind to it.

---

## 2. Feature engineering was tried, and made it worse

CIC-IDS2017 ships **78 engineered flow features**; the engine used 3.
The obvious hypothesis was that better features would fix it. Univariate
discriminative power on friday-morning:

| AUC | feature | |
|---|---|---|
| 0.890 | Destination Port | |
| 0.777 | Average Packet Size | |
| 0.767 | Init_Win_bytes_backward | |
| 0.742 | Fwd Packet Length Min | |
| 0.648 | Total Length of Fwd Packets | ← in use |
| 0.533 | Flow Duration | ← in use |
| 0.515 | Total Fwd Packets | ← in use |

The features in use are among the *weakest available*. But adding the
strong ones **made the model worse**:

| feature set | n | AUC | precision |
|---|---|---|---|
| current (3 volumetric) | 3 | 0.670 | 0.025 |
| + packet-size shape | 6 | **0.206** | 0.021 |
| + TCP window + rates | 10 | **0.223** | 0.021 |
| + destination port | 11 | 0.510 | 0.023 |

**AUC below 0.5 means the model ranks attacks as *more normal* than benign
traffic.** Adding features that describe "normal-looking" behaviour makes
stealthy C2 look *more* normal, because looking normal is precisely its
design goal.

---

## 3. The real diagnosis: the paradigm, not the data

Same features, supervised instead of unsupervised:

| model | AUC | precision | recall | F1 |
|---|---|---|---|---|
| IsolationForest (unsupervised) | 0.21–0.67 | **0.02** | 0.02 | — |
| RandomForest (supervised) | **0.9994** | **0.9948** | 0.9796 | **0.9872** |

The signal is **fully present in the data**. Unsupervised outlier
detection is simply the wrong instrument for this threat class:
Isolation Forest finds points that are *rare or extreme*; Bot C2 flows are
neither. They are ordinary-looking and *specifically* different — a
distinction only a supervised boundary can draw.

Top supervised features: `Bwd Packet Length Mean` (0.269),
`Init_Win_bytes_backward` (0.170), `Init_Win_bytes_forward` (0.123),
`Average Packet Size` (0.115).

---

## 4. But supervised has its own hard limit

Two honest evaluations, deliberately avoiding same-distribution self-testing:

**Test 1 — temporal split (known threat, deployed forward in time).**
Train on the first half of friday-morning (321 Bot flows), test on the
second half (1,645 Bot flows):

> **AUC 0.847 · precision 0.9979 · recall 0.585 · F1 0.737**

Realistic and strong: 99.8% precision means an operator is not drowning in
false alarms.

**Test 2 — cross-day, novel attack family.** Train on Tuesday + Wednesday
(90,708 attacks: brute force, DoS), test on friday-morning (Bot):

> **precision 0.000 · recall 0.000**

**Supervised detection catches nothing it has not been trained on.**

---

## 5. What this means for AEGIS — the three-detector picture

| approach | known threat | novel/unseen threat | training data needed |
|---|---|---|---|
| Unsupervised (IsolationForest) | weak (P≈0.02) | weak | benign baseline |
| Supervised (RandomForest) | **strong (P≈0.998)** | **zero** | labelled attacks |
| **Honeytoken tripwire** | perfect | **perfect** | **none** |

This is the empirical justification for the deception layer, and it is
stronger than the original argument-from-first-principles:

- Supervised detection is excellent on threats it has seen and **blind to
  everything else**.
- Unsupervised detection is weak on any attack designed to look ordinary —
  which is what competent attackers build.
- The honeytoken tripwire needs **no training data at all**. A credential
  with zero legitimate use cannot produce a false positive, and it works on
  the first-ever sighting of a novel attacker.

The gap in rows 1 and 2 is exactly the gap row 3 fills. PLAN_MASTER
Decision #2 asserted this; it is now measured.

---

## 6. Consequences for the plan

**PLAN_MASTER demo-arc step 2 must be reworded.** *"Anomalies surface
naturally from the real data"* is not supportable with the unsupervised
detector alone — what surfaces is ~800 false positives and 5 true
detections.

**Actions:**

1. **Add a supervised detector** through the existing C3 registry
   (`src/detectors/registry.py` was built for exactly this drop-in case).
   Report it with the honest temporal-split numbers, never
   same-distribution self-test numbers.
2. **Keep the unsupervised detector**, reported honestly as the
   novel-threat channel, weak on stealthy traffic. Tune contamination low
   so the console stays quiet rather than crying wolf.
3. **Tripwire remains primary** for the demo's headline moment — and now
   has measured evidence behind that status.
4. **Report all three side by side** in the Research Console. A benchmark
   showing a detector's real limits is more credible than one showing only
   its wins.

---

## 7. Honest limitations of this study

- One attack family (Bot) on one capture day drives the headline numbers;
  the cross-day test used brute-force and DoS as the training families.
- CIC-IDS2017 is a 2017 testbed capture, not live municipal traffic.
- Test 1's 58% recall means roughly 4 in 10 known-threat flows are still
  missed; the reported precision does not imply full coverage.
- No hyperparameter search was performed on either model. These are
  default-configuration results and should be read as a capability
  comparison, not as tuned upper bounds.
