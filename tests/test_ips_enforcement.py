"""
tests/test_ips_enforcement.py — backend/ips/enforcement.py
(`SimulatedEnforcementAdapter`).

Small file: the adapter is deliberately simple (see its own module
docstring for why "simulated" is the honest default, not a shortcut).
These tests pin its one real contract — `apply()`/`rollback()` never
raise and always return a status matching the `dry_run` argument they
were given — since `IngestPipeline._apply_ips_action` relies on that to
implement the requirement's "graceful/fail-safe handling when
enforcement fails" around a DIFFERENT (potentially raising) adapter, not
this one.
"""

from __future__ import annotations

from backend.ips.contracts import ActionStatus, PreventionAction, PreventionDecision
from backend.ips.enforcement import SimulatedEnforcementAdapter


def make_decision(action: PreventionAction = PreventionAction.BLOCK) -> PreventionDecision:
    return PreventionDecision(
        action=action,
        target_asset="AssetX",
        confidence=0.9,
        reason="test fixture",
    )


def test_apply_dry_run_returns_simulated_and_mentions_no_real_fabric():
    adapter = SimulatedEnforcementAdapter()
    result = adapter.apply(make_decision(), dry_run=True)
    assert result.status == ActionStatus.SIMULATED
    assert "no real network fabric" in result.detail


def test_apply_not_dry_run_returns_enforced():
    adapter = SimulatedEnforcementAdapter()
    result = adapter.apply(make_decision(), dry_run=False)
    assert result.status == ActionStatus.ENFORCED


def test_apply_default_argument_is_dry_run():
    """`dry_run` defaults True on the Protocol and this implementation —
    a caller that forgets the argument gets the safe behavior."""
    adapter = SimulatedEnforcementAdapter()
    result = adapter.apply(make_decision())
    assert result.status == ActionStatus.SIMULATED


def test_rollback_always_succeeds():
    adapter = SimulatedEnforcementAdapter()
    result = adapter.rollback("AssetX", PreventionAction.BLOCK)
    assert result.status == ActionStatus.ROLLED_BACK
    assert "AssetX" in result.detail


def test_apply_and_rollback_never_raise_for_any_action_type():
    adapter = SimulatedEnforcementAdapter()
    for action in PreventionAction:
        adapter.apply(make_decision(action), dry_run=True)
        adapter.apply(make_decision(action), dry_run=False)
        adapter.rollback("AssetX", action)
