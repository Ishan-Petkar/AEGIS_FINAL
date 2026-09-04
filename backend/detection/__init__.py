"""
backend/detection — the Hybrid IDS layer.

A package rather than more flat `backend/*.py` modules because these four
files are one cohesive capability with a single entry contract, mirroring
how `src/detectors/`, `src/deception/` and `src/evaluation/` are grouped in
the research engine. Nothing here imports `backend.ingest`; the dependency
runs one way (ingest -> detection), so this layer stays unit-testable with
no pipeline, no database and no replay engine.

  `contracts.py`  the detector-independent flow view and verdict types
                  every detector in this package speaks. Import these, not
                  `ReplayFlow`/`ScoredFlow`, in any new detector.
  `beaconing.py`  temporal/periodicity detector — the documented answer to
                  the volumetric channel's measured blind spot.
  `signature.py`  declarative rule/signature matching over flow metadata.
  `tgnn.py`       graph-structural anomaly detector — a lightweight,
                  NetworkX + IsolationForest stand-in for a temporal GNN
                  (Anomal-E / E-GraphSAGE style), the documented answer to
                  the blind spot where an attacker is quiet, regular, and
                  rule-compliant but topologically unusual.
  `fusion.py`     `HybridFusionEngine` — combines verdicts into one
                  decision by weighted noisy-OR with a confirmed-signal
                  precedence override.

The existing detectors are NOT moved in here. `StreamingScorer`
(unsupervised) and `SupervisedFlowScorer` (supervised) keep their current
modules, their current behaviour and their current tests; they join the
hybrid layer through thin adapters in `contracts.py` instead. That was a
deliberate choice — see `contracts.py`'s module docstring.
"""
