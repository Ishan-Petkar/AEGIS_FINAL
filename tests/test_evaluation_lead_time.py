"""
test_evaluation_lead_time.py — Tests for tripwire lead-time measurement
(AEGIS Phase 3, evaluation/lead_time.py).

Pins the Phase 2 thesis's measurement infrastructure: for at least 3 of the
4 scripted attacks, the honeytoken tripwire's detection instant must be
measurably earlier than the volumetric detector's.
"""
import pathlib
import sys
from datetime import datetime, timezone

import pytest

_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_SRC))

from data_generator import ATTACK_RECON_GATEWAY
from evaluation.lead_time import (
    LeadTimeResult,
    SCRIPTED_ATTACK_EXFIL_EDGES,
    compute_all_scripted_attack_lead_times,
    compute_lead_time,
    summarize_lead_times,
)


class TestComputeLeadTime:
    def test_unknown_attack_raises(self):
        with pytest.raises(ValueError):
            compute_lead_time("Not A Real Attack")

    def test_returns_lead_time_result(self):
        result = compute_lead_time(
            "Payment Gateway Breach",
            base_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        assert isinstance(result, LeadTimeResult)
        assert result.attack_name == "Payment Gateway Breach"
        assert result.gateway_zone == "Gateway_L4"

    def test_lead_time_is_positive_by_default(self):
        result = compute_lead_time(
            "Camera Spoofing",
            base_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        assert result.lead_time_seconds > 0

    def test_recon_detected_before_exfil_detected(self):
        result = compute_lead_time(
            "Data Exfiltration",
            base_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        assert result.recon_detected_at < result.exfil_detected_at

    def test_lead_time_roughly_matches_recon_delay(self):
        """With default recon_delay_sec (60s), lead time should be close to
        60s minus the tripwire's own small detection latency/jitter."""
        result = compute_lead_time(
            "Lateral Movement",
            base_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        assert 50.0 <= result.lead_time_seconds <= 60.0

    def test_custom_recon_delay_changes_lead_time(self):
        short = compute_lead_time(
            "Payment Gateway Breach", recon_delay_sec=10,
            base_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        long = compute_lead_time(
            "Payment Gateway Breach", recon_delay_sec=200,
            base_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        assert long.lead_time_seconds > short.lead_time_seconds

    def test_to_dict_serializable(self):
        result = compute_lead_time(
            "Camera Spoofing",
            base_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        d = result.to_dict()
        assert isinstance(d["recon_detected_at"], str)
        assert isinstance(d["exfil_detected_at"], str)
        assert d["attack_name"] == "Camera Spoofing"


class TestAllScriptedAttackLeadTimes:
    def test_covers_all_four_attacks(self):
        results = compute_all_scripted_attack_lead_times(
            base_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        assert len(results) == 4
        names = {r.attack_name for r in results}
        assert names == set(ATTACK_RECON_GATEWAY)

    def test_at_least_three_of_four_show_positive_lead_time(self):
        """The plan's Phase 2 'Done when' criterion, measured: for >=3 of 4
        scripted attacks, the tripwire must fire measurably earlier than the
        volumetric detector."""
        results = compute_all_scripted_attack_lead_times(
            base_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        n_positive = sum(1 for r in results if r.lead_time_seconds > 0)
        assert n_positive >= 3

    def test_every_attack_has_an_exfil_edge_defined(self):
        for name in ATTACK_RECON_GATEWAY:
            assert name in SCRIPTED_ATTACK_EXFIL_EDGES


class TestSummarizeLeadTimes:
    def test_summary_shape(self):
        results = compute_all_scripted_attack_lead_times(
            base_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        summary = summarize_lead_times(results)
        assert summary["n_attacks"] == 4
        assert summary["n_tripwire_earlier"] >= 3
        assert summary["mean_lead_time_seconds"] > 0

    def test_empty_results_does_not_crash(self):
        summary = summarize_lead_times([])
        assert summary["n_attacks"] == 0
        assert summary["n_tripwire_earlier"] == 0
