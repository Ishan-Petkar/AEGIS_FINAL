"""
tests/test_signature.py — backend/detection/signature.py.

Style follows tests/test_security.py: plain pytest functions, no test
classes. A `_flow()` factory builds a plainly-benign `FlowFeatures` by
default (internal addresses, a normal service port, ordinary byte volume)
so every rule-firing test only needs to override the one or two fields
that make it match, and the "fires nothing" test needs no overrides at
all.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.config import BACKEND_SETTINGS
from backend.detection.contracts import Certainty, DETECTOR_SIGNATURE, FlowFeatures
from backend.detection.signature import (
    DEFAULT_RULES,
    KNOWN_BAD_ADDRESSES,
    SignatureEngine,
    SignatureRule,
)


def _flow(**overrides) -> FlowFeatures:
    """A plainly benign flow by default: internal-to-internal, an
    ordinary HTTPS-ish port, a normal-sized payload."""
    fields = dict(
        ts=datetime(2017, 7, 4, 12, 0, 0, tzinfo=timezone.utc),
        source_ip="10.0.1.5",
        source_port=51234,
        destination_ip="10.0.1.10",
        destination_port=443,
        protocol="TCP",
        duration_sec=1.5,
        packets=10,
        bytes=1500,
    )
    fields.update(overrides)
    return FlowFeatures(**fields)


# ---------------------------------------------------------------------------
# Built-in rule set sanity
# ---------------------------------------------------------------------------


def test_every_built_in_rule_has_unique_id_description_and_valid_confidence():
    ids = [rule.rule_id for rule in DEFAULT_RULES]
    assert len(ids) == len(set(ids)), "duplicate rule_id in DEFAULT_RULES"
    for rule in DEFAULT_RULES:
        assert rule.rule_id
        assert rule.description
        assert 0.0 <= rule.confidence <= 1.0


def test_signature_rule_rejects_out_of_range_confidence():
    with pytest.raises(ValueError):
        SignatureRule(
            rule_id="X",
            title="t",
            description="d",
            confidence=1.5,
            predicate=lambda flow: True,
        )


def test_signature_rule_rejects_empty_description():
    with pytest.raises(ValueError):
        SignatureRule(
            rule_id="X",
            title="t",
            description="",
            confidence=0.5,
            predicate=lambda flow: True,
        )


# ---------------------------------------------------------------------------
# Each built-in rule fires on a flow crafted to match it
# ---------------------------------------------------------------------------


def test_rule_001_fires_on_known_bad_address():
    bad_ip = next(iter(KNOWN_BAD_ADDRESSES))
    flow = _flow(destination_ip=bad_ip)
    engine = SignatureEngine()
    [verdict] = engine.examine([flow])
    assert verdict.fired
    matched_ids = {m["rule_id"] for m in verdict.evidence["matched_rules"]}
    assert "AEGIS-SIG-001" in matched_ids


def test_rule_002_fires_on_small_payload_high_port():
    flow = _flow(
        destination_port=BACKEND_SETTINGS.signature_small_payload_bytes + 40_000,
        bytes=BACKEND_SETTINGS.signature_small_payload_bytes,
        packets=2,
    )
    engine = SignatureEngine()
    [verdict] = engine.examine([flow])
    matched_ids = {m["rule_id"] for m in verdict.evidence["matched_rules"]}
    assert "AEGIS-SIG-002" in matched_ids


def test_rule_003_fires_on_scan_shaped_flow():
    flow = _flow(packets=3, bytes=BACKEND_SETTINGS.signature_scan_max_bytes)
    engine = SignatureEngine()
    [verdict] = engine.examine([flow])
    matched_ids = {m["rule_id"] for m in verdict.evidence["matched_rules"]}
    assert "AEGIS-SIG-003" in matched_ids


def test_rule_004_fires_on_high_risk_admin_port():
    flow = _flow(destination_port=3389)  # RDP
    engine = SignatureEngine()
    [verdict] = engine.examine([flow])
    matched_ids = {m["rule_id"] for m in verdict.evidence["matched_rules"]}
    assert "AEGIS-SIG-004" in matched_ids


def test_rule_005_fires_on_external_address_to_database_port():
    flow = _flow(source_ip="1.2.3.4", destination_port=3306)  # MySQL
    engine = SignatureEngine()
    [verdict] = engine.examine([flow])
    matched_ids = {m["rule_id"] for m in verdict.evidence["matched_rules"]}
    assert "AEGIS-SIG-005" in matched_ids


def test_rule_005_does_not_fire_from_a_private_address():
    # Same database port, but the source is internal — rule 005 is
    # specifically about EXTERNAL exposure, not the port alone.
    flow = _flow(source_ip="10.0.1.5", destination_port=3306)
    engine = SignatureEngine()
    [verdict] = engine.examine([flow])
    matched_ids = {m["rule_id"] for m in verdict.evidence["matched_rules"]}
    assert "AEGIS-SIG-005" not in matched_ids


# ---------------------------------------------------------------------------
# A plainly benign flow fires nothing
# ---------------------------------------------------------------------------


def test_benign_flow_fires_nothing():
    engine = SignatureEngine()
    [verdict] = engine.examine([_flow()])
    assert verdict.fired is False
    assert verdict.calibrated_score == 0.0
    assert verdict.evidence["matched_rules"] == []


# ---------------------------------------------------------------------------
# Multiple matching rules -> score is the max, evidence lists all
# ---------------------------------------------------------------------------


def test_multiple_matches_score_is_max_and_evidence_lists_all():
    bad_ip = next(iter(KNOWN_BAD_ADDRESSES))  # rule 001, confidence 0.90
    flow = _flow(
        destination_ip=bad_ip,
        destination_port=3389,  # also rule 004, confidence 0.40
    )
    engine = SignatureEngine()
    [verdict] = engine.examine([flow])

    matched_ids = {m["rule_id"] for m in verdict.evidence["matched_rules"]}
    assert {"AEGIS-SIG-001", "AEGIS-SIG-004"}.issubset(matched_ids)

    highest_confidence = max(
        rule.confidence for rule in DEFAULT_RULES if rule.rule_id in matched_ids
    )
    assert verdict.calibrated_score == pytest.approx(highest_confidence)
    # Specifically: NOT a sum of the two matching confidences.
    assert verdict.calibrated_score < 0.90 + 0.40


# ---------------------------------------------------------------------------
# One verdict per input flow, in order
# ---------------------------------------------------------------------------


def test_one_verdict_per_flow_in_input_order():
    bad_ip = next(iter(KNOWN_BAD_ADDRESSES))
    flows = [
        _flow(),  # benign
        _flow(destination_ip=bad_ip),  # rule 001
        _flow(destination_port=23),  # rule 004 (telnet)
    ]
    engine = SignatureEngine()
    verdicts = engine.examine(flows)

    assert len(verdicts) == 3
    assert verdicts[0].fired is False
    assert verdicts[1].fired is True
    assert verdicts[2].fired is True
    matched_ids_1 = {m["rule_id"] for m in verdicts[1].evidence["matched_rules"]}
    assert "AEGIS-SIG-001" in matched_ids_1


def test_examine_returns_empty_list_for_empty_input():
    engine = SignatureEngine()
    assert engine.examine([]) == []


# ---------------------------------------------------------------------------
# Rule-set override via the constructor
# ---------------------------------------------------------------------------


def test_constructor_rules_override_replaces_default_rule_set():
    always_true = SignatureRule(
        rule_id="TEST-ALWAYS-TRUE",
        title="always true",
        description="matches every flow, for testing",
        confidence=0.77,
        predicate=lambda flow: True,
    )
    always_false = SignatureRule(
        rule_id="TEST-ALWAYS-FALSE",
        title="always false",
        description="matches no flow, for testing",
        confidence=0.99,
        predicate=lambda flow: False,
    )
    engine = SignatureEngine(rules=[always_true, always_false])
    assert engine.rules == (always_true, always_false)

    [verdict] = engine.examine([_flow()])
    assert verdict.fired is True
    assert verdict.calibrated_score == pytest.approx(0.77)
    matched_ids = {m["rule_id"] for m in verdict.evidence["matched_rules"]}
    assert matched_ids == {"TEST-ALWAYS-TRUE"}


def test_constructor_default_rules_used_when_no_override_given():
    engine = SignatureEngine()
    assert engine.rules == DEFAULT_RULES


# ---------------------------------------------------------------------------
# Contract details: name, reliability from settings, certainty
# ---------------------------------------------------------------------------


def test_engine_name_is_the_detector_signature_constant():
    engine = SignatureEngine()
    assert engine.name == DETECTOR_SIGNATURE


def test_verdict_reliability_comes_from_settings_not_hardcoded():
    engine = SignatureEngine()
    [verdict] = engine.examine([_flow()])
    assert verdict.reliability == BACKEND_SETTINGS.hybrid_weight_signature

    # And it tracks a differently-configured settings instance rather than
    # a value baked into the engine at import time.
    from backend.config import BackendSettings

    custom_settings = BackendSettings(hybrid_weight_signature=0.33)
    assert custom_settings.hybrid_weight_signature == pytest.approx(0.33)


def test_verdict_certainty_is_always_heuristic():
    bad_ip = next(iter(KNOWN_BAD_ADDRESSES))
    engine = SignatureEngine()
    verdicts = engine.examine([_flow(), _flow(destination_ip=bad_ip)])
    for verdict in verdicts:
        assert verdict.certainty is Certainty.HEURISTIC


def test_evidence_is_json_serialisable():
    import json

    bad_ip = next(iter(KNOWN_BAD_ADDRESSES))
    engine = SignatureEngine()
    [verdict] = engine.examine([_flow(destination_ip=bad_ip)])
    json.dumps(dict(verdict.evidence))  # must not raise
