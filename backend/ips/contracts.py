"""
backend/ips/contracts.py — IPS layer data contracts.

Defines the shapes the IPS Policy Engine (backend/ips/policy.py) and the
Enforcement Adapter (backend/ips/enforcement.py) exchange, mirroring how
backend/detection/contracts.py defines the Hybrid IDS layer's own
contracts. Kept in a dedicated module for the same reason: policy.py and
enforcement.py both need these types, and neither should import the
other directly.

Deliberately NOT built on `backend.detection.contracts.ResponseAction`.
That enum already reserves THROTTLE/BLOCK for "a future IPS policy layer"
(see its own docstring and backend/detection/fusion.py's module
docstring) — but the IPS layer's real action set is a superset
(observe/alert/rate-limit/block/quarantine) that does not map cleanly
onto `FusedDecision.action`'s four values, and extending that pinned enum
would mean touching backend/detection/contracts.py and backend/detection/
fusion.py, which the IPS layer must not modify — its only input contract
from that package is `FusedDecision` itself, read-only (see
`backend/ips/policy.py`'s module docstring). `PreventionAction` below is
therefore this package's own enum; `ResponseAction.THROTTLE`/`.BLOCK`
stay declared-but-unused in fusion.py exactly as before this layer
existed, and `tests/test_fusion.py::test_never_emits_throttle_or_block`
stays correct and unmodified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional, Protocol

_EMPTY_EVIDENCE: Mapping[str, Any] = {}


class PreventionAction(str, Enum):
    """What the IPS Policy Engine decided to do about one asset.

    Ordered by severity — see `PREVENTION_SEVERITY` below. OBSERVE is
    "do nothing"; ALERT is "the existing IDS alert stands on its own, no
    active prevention warranted yet"; RATE_LIMIT/BLOCK/QUARANTINE are the
    three active-prevention tiers the requirement asks for ("rate-limit",
    "block", "quarantine/isolate" — QUARANTINE covers both words, since
    in this environment they mean the same thing: stop treating the
    asset as trustworthy until an operator clears it).
    """

    OBSERVE = "observe"
    ALERT = "alert"
    RATE_LIMIT = "rate_limit"
    BLOCK = "block"
    QUARANTINE = "quarantine"


#: Total order over `PreventionAction`, used to decide whether a new
#: decision for an asset that already has an active action is a
#: duplicate (same or lower severity — skip, the requirement's
#: "duplicate/conflicting-action protection"), an escalation (strictly
#: higher — supersede the old one), or moot (OBSERVE/ALERT — nothing to
#: enforce or track in the active-mitigation registry at all).
PREVENTION_SEVERITY: Mapping[PreventionAction, int] = {
    PreventionAction.OBSERVE: 0,
    PreventionAction.ALERT: 1,
    PreventionAction.RATE_LIMIT: 2,
    PreventionAction.BLOCK: 3,
    PreventionAction.QUARANTINE: 4,
}

#: The subset of `PreventionAction` that represents ACTIVE prevention —
#: i.e. something an `EnforcementAdapter` actually applies and that is
#: worth its own audit row, active-mitigation registry entry, TTL, and
#: rollback path. OBSERVE and ALERT never reach the adapter or the
#: registry — see `IPSPolicyEngine.decide`'s docstring.
ACTIVE_PREVENTION_ACTIONS = frozenset(
    {PreventionAction.RATE_LIMIT, PreventionAction.BLOCK, PreventionAction.QUARANTINE}
)


class ActionStatus(str, Enum):
    """Lifecycle state of one persisted `IpsAction` row (backend/models.py).

    SIMULATED and ENFORCED both mean "the decision was approved and
    recorded"; they differ only in whether the enforcement adapter was
    told this was a real application (ENFORCED) or a dry run (SIMULATED
    — `BACKEND_SETTINGS.ips_dry_run=True`, or an escalation superseding
    an existing dry-run action). See `SimulatedEnforcementAdapter`'s
    module docstring for why, in THIS environment, even "enforced" is
    itself a simulation of a real network control (no real network
    fabric exists here to enforce against) — but the SIMULATED/ENFORCED
    distinction is still real and worth keeping visible, since it is
    exactly the signal an operator needs before trusting this layer with
    a real adapter later.
    """

    SIMULATED = "simulated"
    ENFORCED = "enforced"
    FAILED = "failed"
    EXPIRED = "expired"
    ROLLED_BACK = "rolled_back"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class PreventionDecision:
    """Output of `IPSPolicyEngine.decide()` — the sole input the rest of
    the IPS layer (enforcement, persistence, broadcast) needs.

    `action == PreventionAction.OBSERVE` means "nothing to do"; no
    downstream code should persist, enforce, or broadcast it — see
    `is_active_prevention` and `ACTIVE_PREVENTION_ACTIONS`. `ALERT`
    decisions ARE persisted/broadcast (an audit-worthy "prevention was
    considered and withheld" record, per the requirement's "record every
    prevention decision") but never reach the enforcement adapter or the
    active-mitigation registry.
    """

    action: PreventionAction
    target_asset: str
    confidence: float
    reason: str
    evidence: Mapping[str, Any] = field(default_factory=lambda: dict(_EMPTY_EVIDENCE))
    #: Seconds until this action should auto-expire. `None` for
    #: OBSERVE/ALERT (nothing to expire); always set by
    #: `IPSPolicyEngine.decide()` for an active-prevention action.
    ttl_sec: Optional[float] = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence must be within [0, 1], got {self.confidence}")

    @property
    def is_active_prevention(self) -> bool:
        return self.action in ACTIVE_PREVENTION_ACTIONS


@dataclass(frozen=True)
class EnforcementResult:
    """Outcome of one `EnforcementAdapter.apply()` / `.rollback()` call."""

    status: ActionStatus
    detail: str


class EnforcementAdapter(Protocol):
    """Anything that can carry out (or roll back) a `PreventionDecision`.

    Deliberately a `Protocol`, mirroring `backend.ingest.Broadcaster` and
    `backend.detection.contracts.FlowDetector`: the IPS orchestration
    code (`backend/ingest.py`) depends only on this shape, never on a
    concrete adapter class, so a future adapter that talks to a real
    firewall / SDN controller / cloud security-group API is a drop-in
    replacement for `SimulatedEnforcementAdapter` — no change needed to
    `IPSPolicyEngine`, `IngestPipeline`, persistence, or the API routes.
    This is the requirement's "actual enforcement behind an adapter/
    interface so the enforcement mechanism can evolve later without
    changing IDS or policy logic," made concrete.

    `apply()`/`rollback()` must not raise for an ordinary "could not
    enforce" case — return an `EnforcementResult` with `status=FAILED`
    instead, so the reason is visible in the audit trail rather than only
    in a log line. Raising is reserved for a genuine adapter bug; the
    caller (`IngestPipeline._apply_ips_action`) wraps both calls in a
    `try/except` regardless (fail-open, per the requirement's "graceful/
    fail-safe handling when enforcement fails") — a raise is caught,
    logged, and turned into a FAILED status, but a well-behaved adapter
    should not rely on that as its normal error path.
    """

    def apply(self, decision: PreventionDecision, dry_run: bool = True) -> EnforcementResult: ...

    def rollback(self, target_asset: str, action: PreventionAction) -> EnforcementResult: ...
