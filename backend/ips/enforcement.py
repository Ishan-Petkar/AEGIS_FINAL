"""
backend/ips/enforcement.py — the Enforcement Adapter.

AEGIS has no real network fabric, host agent, or SDN/firewall control
plane to enforce against — this is a closed replay/synthetic demo
environment (CLAUDE.md section 1: "no live ingestion, no persistence
layer [beyond its own demo Postgres], ... the asset topology is
hardcoded"), not a production security product with a real network
sitting behind it. Claiming a "real" network-level block in that
environment would be dishonest in exactly the way this project's own
documentation already refuses to be elsewhere — the unsupervised
detector's real 0.006 precision published rather than hidden, the CII
engine's honest median-of-zero reporting, the deliberately-not-invented
edge between the two disconnected graph layers. `SimulatedEnforcementAdapter`
is therefore the correct DEFAULT adapter for this environment, not a
shortcut taken to skip real work: it is the requirement's own "validate
in dry-run mode; enable real enforcement only where the current
environment safely supports it" resolved honestly for an environment
where no unsafe real enforcement is even possible in the first place.

What this adapter genuinely, safely does:
  - `apply()` always succeeds — there is no external system to fail
    against — and returns ENFORCED or SIMULATED depending on the
    caller-supplied `dry_run` flag. The CALLER (`IngestPipeline`)
    decides which, since only it knows the current
    `BACKEND_SETTINGS.ips_dry_run` value and whether this call is an
    escalation over an already-active (possibly already-dry-run) action.
  - `rollback()` always succeeds for the same reason.

What this adapter deliberately does NOT do: track which assets currently
have an active mitigation, for how long, or de-duplicate/escalate repeat
decisions. That state lives in `IngestPipeline` (mirroring exactly where
CII debounce state and alert debounce state already live — not a new
pattern introduced for this layer), because it describes the PIPELINE's
own behavior (should flow-scoring treat asset X as already actioned),
not what a specific enforcement mechanism does. This adapter stays
stateless and would remain so even behind a future adapter that calls a
real API — see `EnforcementAdapter`'s docstring in contracts.py for why
that separation is the actual point of making it a `Protocol`.
"""

from __future__ import annotations

import logging

from backend.ips.contracts import (
    ActionStatus,
    EnforcementResult,
    PreventionAction,
    PreventionDecision,
)

logger = logging.getLogger(__name__)


class SimulatedEnforcementAdapter:
    """Default `EnforcementAdapter`. See the module docstring for why
    "simulated" is the honest default in this environment, not merely a
    development placeholder."""

    def apply(self, decision: PreventionDecision, dry_run: bool = True) -> EnforcementResult:
        if dry_run:
            logger.info(
                "ips: [dry-run] would %s %s (confidence %.3f) — %s",
                decision.action.value,
                decision.target_asset,
                decision.confidence,
                decision.reason,
            )
            return EnforcementResult(
                status=ActionStatus.SIMULATED,
                detail=(
                    f"dry-run: would {decision.action.value} {decision.target_asset} — "
                    "no real network fabric in this environment, no pipeline state changed"
                ),
            )
        logger.warning(
            "ips: %s %s (confidence %.3f) — %s",
            decision.action.value,
            decision.target_asset,
            decision.confidence,
            decision.reason,
        )
        return EnforcementResult(
            status=ActionStatus.ENFORCED,
            detail=(
                f"{decision.action.value} recorded for {decision.target_asset} "
                "(simulated enforcement — AEGIS has no real network fabric to act "
                "against; see backend/ips/enforcement.py module docstring)"
            ),
        )

    def rollback(self, target_asset: str, action: PreventionAction) -> EnforcementResult:
        logger.info("ips: rollback %s on %s", action.value, target_asset)
        return EnforcementResult(
            status=ActionStatus.ROLLED_BACK,
            detail=f"{action.value} on {target_asset} rolled back",
        )
