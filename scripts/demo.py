#!/usr/bin/env python3
"""
scripts/demo.py — standalone terminal showcase of the six detection channels.

Runs in well under 30 seconds with NO Postgres, NO Redis, NO Next.js, and
no prebuilt model artifact. Nothing here touches the database or the
`IngestPipeline`: the detectors are exercised directly through the same
`FlowDetector` contract `backend/ingest.py` uses in production, and the
verdicts are fused by the same `HybridFusionEngine`. What you see is the
real detection code on synthetic traffic, not a mock of it.

    PYTHONPATH=src venv/bin/python scripts/demo.py
    PYTHONPATH=src venv/bin/python scripts/demo.py --no-color

Why synthetic flows rather than a replay slice: the real CIC-IDS2017
captures are gitignored (see docs/DATASETS.md), so a script that depended
on them would fail for exactly the evaluator this exists to serve. The
flows below are hand-built to isolate ONE channel each, which a real
capture slice cannot guarantee — that is the point of a showcase, and it
is why the numbers here are illustrative of the mechanism rather than a
measurement. The measured figures live in docs/DETECTION_STUDY.md and in
each setting's docstring in backend/config.py.

The supervised (RandomForest) channel is the one detector NOT exercised:
it scores from a joblib artifact built by `python -m backend.warmup_
supervised` against real labelled capture data, which by design does not
exist in a clean checkout. It is reported as skipped rather than faked.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for path in (str(REPO_ROOT), str(REPO_ROOT / "src")):
    if path not in sys.path:
        sys.path.insert(0, path)

from backend.detection.beaconing import BeaconingDetector  # noqa: E402
from backend.detection.contracts import (  # noqa: E402
    DetectorVerdict,
    FlowFeatures,
    verdict_from_scored_flow,
    verdict_from_tripwire,
)
from backend.detection.fusion import HybridFusionEngine  # noqa: E402
from backend.detection.signature import SignatureEngine  # noqa: E402
from backend.detection.tgnn import TGNNDetector  # noqa: E402
from backend.config import BACKEND_SETTINGS  # noqa: E402
# The three pre-hybrid detector names live in backend/ingest.py (they
# predate the detection package and other code already imports them from
# there) — see that module's own comment. Imported, never redefined.
from backend.ingest import (  # noqa: E402
    DETECTOR_SUPERVISED,
    DETECTOR_TRIPWIRE,
    DETECTOR_VOLUMETRIC,
)
from backend.replay_reader import ReplayFlow  # noqa: E402
from backend.streaming import StreamingScorer  # noqa: E402

T0 = datetime(2024, 1, 1, 9, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Terminal formatting (no dependencies — plain ANSI, disabled by --no-color)
# ---------------------------------------------------------------------------


class Style:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.enabled else text

    def bold(self, t: str) -> str:
        return self._wrap("1", t)

    def dim(self, t: str) -> str:
        return self._wrap("2", t)

    def red(self, t: str) -> str:
        return self._wrap("31", t)

    def green(self, t: str) -> str:
        return self._wrap("32", t)

    def yellow(self, t: str) -> str:
        return self._wrap("33", t)

    def cyan(self, t: str) -> str:
        return self._wrap("36", t)


def flow(
    ts: datetime,
    src: str,
    dst: str,
    *,
    dport: int = 443,
    sport: int = 54321,
    nbytes: int = 800,
    packets: int = 6,
    duration: float = 0.4,
    honeytoken: bool = False,
) -> ReplayFlow:
    """One synthetic flow. `label`/`is_attack` are set to the honest
    BENIGN default and never read by any detector — `FlowFeatures`
    structurally excludes them (see backend/detection/contracts.py), which
    is exactly why this script cannot accidentally leak ground truth into
    a verdict."""
    return ReplayFlow(
        ts=ts,
        source_ip=src,
        source_port=sport,
        destination_ip=dst,
        destination_port=dport,
        protocol="TCP",
        duration_sec=duration,
        packets=packets,
        bytes=nbytes,
        label="BENIGN",
        is_attack=False,
        timing_provenance="capture_seconds",
        source_row_id=f"demo:{src}->{dst}@{ts.isoformat()}",
        source_dataset="synthetic_demo",
        is_honeytoken_use=honeytoken,
    )


#: Each workstation talks to a FIXED small set of servers. Stability here
#: is as load-bearing as the timing jitter below: the graph channel scores
#: a host against its own peer history, so a background where every source
#: eventually reaches every destination would make novelty the norm and
#: the channel would (correctly) flag the baseline itself. Real hosts have
#: consistent peer sets; the fixture has to as well.
#: Ten hosts, not six: the graph channel defers fitting its baseline until
#: at least `tgnn_min_baseline_nodes` (8) scorable nodes exist, so a
#: smaller fixture leaves it permanently abstaining with
#: `baseline_not_fitted` — again a fixture bug wearing a detector's
#: clothes.
STABLE_PEERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("10.0.0.10", ("10.0.1.1", "10.0.1.2")),
    ("10.0.0.11", ("10.0.1.2", "10.0.1.3")),
    ("10.0.0.12", ("10.0.1.1", "10.0.1.3")),
    ("10.0.0.13", ("10.0.1.3", "10.0.1.4")),
    ("10.0.0.14", ("10.0.1.1", "10.0.1.4")),
    ("10.0.0.15", ("10.0.1.2", "10.0.1.4")),
    ("10.0.0.16", ("10.0.1.1", "10.0.1.5")),
    ("10.0.0.17", ("10.0.1.2", "10.0.1.5")),
    ("10.0.0.18", ("10.0.1.3", "10.0.1.5")),
    ("10.0.0.19", ("10.0.1.4", "10.0.1.5")),
)


def benign_background(n: int = 1200, start: datetime = T0) -> list[ReplayFlow]:
    """Ordinary internal chatter: a handful of workstations talking to a
    handful of servers, IRREGULAR timing, unremarkable sizes.

    The irregularity is load-bearing, not cosmetic. Evenly-spaced
    background traffic is a beacon by this system's definition (low
    interval coefficient of variation), so a tidy `i * 7` schedule would
    make the beaconing channel fire on the benign baseline and the
    showcase would be demonstrating a fixture bug rather than a detector.

    Which host talks next is RANDOM rather than round-robin, for the same
    reason: round-robin over N pairs gives every pair a near-constant
    inter-arrival (the sum of N jitters, which concentrates by the
    central limit theorem), so even jittered per-flow gaps produced a
    per-pair CV of 0.19 — inside the beaconing threshold. Random
    selection makes each pair's gaps geometric, with the high variance
    ordinary traffic actually has. Seeded, so every run prints identical
    numbers.

    `n` defaults above `BACKEND_SETTINGS.warmup_min_rows` (1,000) — the
    volumetric scorer refuses to fit on less, on the grounds that a tiny
    warmup yields zero-variance feature columns and meaningless sigma
    figures. That floor is the real production guardrail; the demo
    respects it rather than lowering it.
    """
    rng = random.Random(42)
    flows: list[ReplayFlow] = []
    clock = start
    for _ in range(n):
        src, peers = rng.choice(STABLE_PEERS)
        dst = rng.choice(peers)
        clock += timedelta(seconds=rng.uniform(1.0, 45.0))
        flows.append(
            flow(
                clock,
                src,
                dst,
                nbytes=rng.randint(400, 1500),
                packets=rng.randint(4, 14),
                duration=rng.uniform(0.15, 1.4),
            )
        )
    return flows


# ---------------------------------------------------------------------------
# Detector harness
# ---------------------------------------------------------------------------


class DemoHarness:
    """Holds one long-lived instance of each stateful detector, exactly as
    `IngestPipeline` does — beaconing and T-GNN accumulate history across
    batches, so rebuilding them per scenario would erase the very state
    they detect against."""

    def __init__(self) -> None:
        self.signature = SignatureEngine()
        self.beaconing = BeaconingDetector()
        # Demo-timeline overrides, NOT tuning claims. `baseline_batches=4`
        # fits the baseline within a few synthetic batches instead of the
        # production default of 10. `window_sec=3600` matches this
        # script's compressed synthetic clock: the production default of
        # 60s is calibrated to real friday-morning traffic density (~11
        # flows/sec), where a 60s window holds thousands of edges, while
        # this demo's background averages one flow every ~23 seconds — at
        # the production window almost every edge would be pruned before
        # the next arrived and no node would ever reach a scorable
        # out-degree. The measured production values are in
        # backend/config.py; nothing here changes them.
        self.tgnn = TGNNDetector(
            baseline_batches=4, min_edges_to_score=2, window_sec=3600.0
        )
        self.fusion = HybridFusionEngine()
        self.scorer: StreamingScorer | None = None

    def warm_up(self, flows: list[ReplayFlow]) -> None:
        """Fit the volumetric IsolationForest and let the graph/timing
        channels build their baselines — the same 'first batches are
        assumed benign' posture the real pipeline has (and the same
        documented limitation: an attack during warmup poisons it)."""
        self.scorer = StreamingScorer().fit_from_warmup(flows=flows)
        for i in range(0, len(flows), 60):
            batch = [FlowFeatures.from_replay_flow(f) for f in flows[i : i + 60]]
            self.beaconing.examine(batch)
            self.tgnn.examine(batch)

    def examine(self, flows: list[ReplayFlow]) -> list[list[DetectorVerdict]]:
        """Return per-flow verdict lists, one entry per input flow."""
        features = [FlowFeatures.from_replay_flow(f) for f in flows]
        assert self.scorer is not None, "warm_up() must run first"

        scored = self.scorer.score_batch(flows)
        signature = self.signature.examine(features)
        beaconing = self.beaconing.examine(features)
        tgnn = self.tgnn.examine(features)

        per_flow: list[list[DetectorVerdict]] = []
        for i, feat in enumerate(features):
            per_flow.append(
                [
                    verdict_from_scored_flow(
                        scored[i], BACKEND_SETTINGS.hybrid_weight_volumetric, DETECTOR_VOLUMETRIC
                    ),
                    verdict_from_tripwire(
                        feat.is_honeytoken_use,
                        1.0,
                        BACKEND_SETTINGS.hybrid_weight_tripwire,
                        DETECTOR_TRIPWIRE,
                    ),
                    signature[i],
                    beaconing[i],
                    tgnn[i],
                ]
            )
        return per_flow


# ---------------------------------------------------------------------------
# Scenarios — each isolates ONE channel
# ---------------------------------------------------------------------------


def scenario_flows(base: datetime) -> list[tuple[str, str, list[ReplayFlow]]]:
    """`(title, what-to-watch-for, flows)` per scenario, in demo order.

    `base` must be AFTER the last warmup timestamp. The T-GNN sliding
    window prunes against the newest timestamp it has ever seen, so
    scenario flows dated before the warmup's end would be pruned on
    arrival as stale and every graph verdict would abstain with
    `insufficient_edges` — a fixture bug that looks exactly like a broken
    detector.
    """

    # Every scenario below reuses ESTABLISHED (source, destination) pairs
    # from STABLE_PEERS wherever the point is to isolate a non-graph
    # channel — otherwise T-GNN would legitimately co-fire on the novel
    # peer and the scenario would no longer isolate anything.
    quiet = [
        flow(base + timedelta(seconds=i * 11), src, peers[i % len(peers)])
        for i, (src, peers) in enumerate(STABLE_PEERS)
    ]

    flood = [
        flow(
            base + timedelta(minutes=5, seconds=i),
            "10.0.0.11",
            "10.0.1.2",  # established peer — volumetric only
            nbytes=9_000_000,
            packets=8_000,
            duration=120.0,
        )
        for i in range(4)
    ]

    honeytoken = [
        flow(
            base + timedelta(minutes=10),
            "10.0.0.13",
            "10.0.1.3",  # established peer — tripwire only
            dport=8080,
            honeytoken=True,
        )
    ]

    # Metronomic 60s callbacks — the timing signature no volumetric or
    # rule-based feature can express.
    c2 = [
        flow(base + timedelta(minutes=20) + timedelta(seconds=60 * i), "10.0.0.14", "203.0.113.7")
        for i in range(10)
    ]

    # One host suddenly fanning out to 30 destinations it has never
    # contacted, at ordinary sizes and irregular timing.
    fanout = [
        flow(
            base + timedelta(minutes=40, seconds=i * 2),
            "10.0.0.10",
            f"10.0.7.{i}",
            nbytes=700,
        )
        for i in range(30)
    ]

    # Destination port 3389 (RDP) — AEGIS-SIG-004, high-risk admin service.
    # Established peer, so the rule match is the only signal present.
    sig = [
        flow(base + timedelta(minutes=50), "10.0.0.15", "10.0.1.4", dport=3389, nbytes=1200)
    ]

    return [
        ("1. Benign background", "all channels quiet, fused score near zero", quiet),
        (
            "2. Volumetric flood",
            "volumetric fires — but at reliability 0.02 (its MEASURED precision) "
            "it barely moves the fused score, by design",
            flood,
        ),
        ("3. Honeytoken touch", "tripwire fires — CONFIRMED, never averaged", honeytoken),
        (
            "4. Periodic C2 callback",
            "beaconing fires on timing regularity; tgnn corroborates — a real "
            "callback is BOTH metronomic and to a peer never seen before",
            c2,
        ),
        ("5. Host topology drift", "tgnn fires on novel-peer fan-out", fanout),
        ("6. Known-bad pattern", "signature fires on an admin-port rule match", sig),
    ]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_scenario(
    st: Style,
    title: str,
    expectation: str,
    verdicts: list[DetectorVerdict],
    threat_score: float,
    band: str,
    rationale: str,
) -> None:
    print()
    print(st.bold(st.cyan(title)))
    print(st.dim(f"   watch for: {expectation}"))
    print()
    print(f"   {'DETECTOR':<16} {'FIRED':<5} {'SCORE':>5}   EVIDENCE")
    print(f"   {'-' * 16} {'-' * 5} {'-' * 5}   {'-' * 44}")

    for v in verdicts:
        fired_cell = st.red("YES ") if v.fired else st.green("no  ")
        summary = v.evidence.get("summary")
        if summary is None:
            abstained = v.evidence.get("abstained")
            summary = st.dim(f"abstained: {abstained}") if abstained else st.dim("—")
        print(
            f"   {v.detector:<16} {fired_cell}  "
            f"{v.calibrated_score:>5.2f}   {summary}"
        )

    print(
        f"   {DETECTOR_SUPERVISED:<16} {st.dim('skip')}  {st.dim('    —')}   "
        f"{st.dim('requires artifact: python -m backend.warmup_supervised')}"
    )

    band_colour = st.red if threat_score >= 0.5 else (st.yellow if threat_score >= 0.2 else st.green)
    print()
    print(
        f"   {st.bold('FUSED')}        {band_colour(band.upper()):<16} "
        f"{st.bold(f'{threat_score:.2f}')}   {rationale}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--no-color", action="store_true", help="disable ANSI colour output")
    args = parser.parse_args()
    st = Style(enabled=not args.no_color and sys.stdout.isatty())

    print()
    print(st.bold("AEGIS — detection channel showcase"))
    print(st.dim("Real detectors, synthetic traffic. No database, no artifacts, no UI."))
    print(st.dim("Each scenario is built to isolate one channel; the fused decision"))
    print(st.dim("at the bottom of each is what the operations console would show."))

    harness = DemoHarness()
    print()
    print(st.dim("Warming up baselines on benign background traffic..."), end=" ", flush=True)
    background = benign_background()
    harness.warm_up(background)
    print(st.green("done"))

    # Scenarios must start after the warmup's last timestamp — see
    # `scenario_flows`' docstring for why an earlier `base` silently
    # breaks every graph verdict.
    base = background[-1].ts + timedelta(minutes=1)

    for title, expectation, flows in scenario_flows(base):
        per_flow = harness.examine(flows)
        # Report the LAST flow of each scenario: the stateful channels
        # (beaconing, tgnn) need the whole sequence before they have
        # anything to say, so the final flow is the one that sees it all.
        verdicts = per_flow[-1]
        decision = harness.fusion.fuse(verdicts)
        render_scenario(
            st,
            title,
            expectation,
            verdicts,
            decision.threat_score,
            decision.band.value,
            decision.rationale,
        )

    print()
    print(st.dim("Fused scores use each channel's configured reliability weight"))
    print(st.dim("(backend/config.py hybrid_weight_*); a CONFIRMED tripwire hit"))
    print(st.dim("escalates outright rather than being averaged with the rest."))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
