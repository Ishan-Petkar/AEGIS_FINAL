"""
backend/ips — the IPS (prevention) layer.

Sits one step downstream of the Hybrid IDS layer (backend/detection/):

    Traffic -> Hybrid IDS -> Detection Fusion -> Risk + CII
            -> IPS Policy Engine -> Prevention Decision
            -> Enforcement Adapter -> Audit / Persistence / Alert / WS / UI

Three modules, mirroring backend/detection/'s own split:

  contracts.py    -- PreventionAction, PreventionDecision, EnforcementResult,
                      the EnforcementAdapter Protocol. No logic.
  policy.py        -- IPSPolicyEngine: pure, stateless, FusedDecision (+
                      asset criticality + CII median) in, PreventionDecision
                      out. Never touches backend.detection.contracts or
                      backend.detection.fusion.
  enforcement.py   -- SimulatedEnforcementAdapter: the default
                      EnforcementAdapter for this environment (see its
                      module docstring for why "simulated" is the honest
                      choice here, not a shortcut).

`backend.ingest.IngestPipeline` is the only orchestrator: it owns the
active-mitigation registry (which asset currently has what action active,
for how long), calls the policy engine, calls the enforcement adapter,
persists `backend.models.IpsAction` rows, and broadcasts `ips_action`
envelopes -- exactly the role it already plays for the Hybrid IDS layer's
own decisions.
"""

from __future__ import annotations

from backend.ips.contracts import (
    ACTIVE_PREVENTION_ACTIONS,
    PREVENTION_SEVERITY,
    ActionStatus,
    EnforcementAdapter,
    EnforcementResult,
    PreventionAction,
    PreventionDecision,
)
from backend.ips.enforcement import SimulatedEnforcementAdapter
from backend.ips.policy import IPSPolicyEngine

__all__ = [
    "ACTIVE_PREVENTION_ACTIONS",
    "PREVENTION_SEVERITY",
    "ActionStatus",
    "EnforcementAdapter",
    "EnforcementResult",
    "IPSPolicyEngine",
    "PreventionAction",
    "PreventionDecision",
    "SimulatedEnforcementAdapter",
]
