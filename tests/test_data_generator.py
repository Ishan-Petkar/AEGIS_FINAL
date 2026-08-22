"""
test_data_generator.py — Unit tests for the synthetic traffic generator.

Tests cover the M1.5 fix (PLAN_MASTER.md Phase 1): anomaly labels must not be
perfectly recoverable from source-IP membership in EXTERNAL_THREAT_IPS alone.
Before this fix, anomaly injection was gated on `src in threat_ips`, so every
ground-truth anomaly came from one of three hardcoded IPs — a detector (or a
human) could recover the label with zero knowledge of traffic volume.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from data_generator import generate_mock_network_data
from config import EXTERNAL_THREAT_IPS


class TestGroundTruthColumn(unittest.TestCase):

    def test_ground_truth_column_present(self):
        _, edges_df = generate_mock_network_data(num_edges=200, seed=42)
        self.assertIn("is_ground_truth_anomaly", edges_df.columns)
        self.assertEqual(edges_df["is_ground_truth_anomaly"].dtype, bool)

    def test_anomaly_rate_roughly_matches_setting(self):
        _, edges_df = generate_mock_network_data(num_edges=5000, anomaly_rate=0.15, seed=42)
        # Only the random (non-baseline) edges are subject to anomaly injection.
        observed_rate = edges_df["is_ground_truth_anomaly"].mean()
        self.assertGreater(observed_rate, 0.05)
        self.assertLess(observed_rate, 0.20)


class TestLabelNotRecoverableFromThreatIPs(unittest.TestCase):
    """The core M1.5 regression guard."""

    def test_anomalies_are_not_confined_to_threat_ips(self):
        threat_ips = {t["ip"] for t in EXTERNAL_THREAT_IPS}
        _, edges_df = generate_mock_network_data(num_edges=3000, anomaly_rate=0.15, seed=7)
        anomalies = edges_df[edges_df["is_ground_truth_anomaly"]]
        self.assertGreater(len(anomalies), 0, "test needs at least some anomalies to be meaningful")

        frac_from_threat_ips = anomalies["source"].isin(threat_ips).mean()
        # Before the fix this was 1.0 (100% — anomaly injection required a
        # threat-IP source). It should now roughly match the threat IPs'
        # natural share of all valid source addresses, and certainly not be
        # anywhere near total coverage.
        self.assertLess(
            frac_from_threat_ips, 0.6,
            f"anomalies should not be dominated by threat-IP sources "
            f"(got {frac_from_threat_ips:.2f} — label may still be leaking "
            f"through source-IP membership)",
        )

    def test_non_threat_ip_sources_can_be_anomalous(self):
        threat_ips = {t["ip"] for t in EXTERNAL_THREAT_IPS}
        _, edges_df = generate_mock_network_data(num_edges=3000, anomaly_rate=0.15, seed=7)
        anomalies = edges_df[edges_df["is_ground_truth_anomaly"]]
        non_threat_anomalies = anomalies[~anomalies["source"].isin(threat_ips)]
        self.assertGreater(
            len(non_threat_anomalies), 0,
            "at least some anomalies must originate from non-threat-IP sources",
        )

    def test_reproducible_with_seed(self):
        _, edges1 = generate_mock_network_data(num_edges=500, anomaly_rate=0.15, seed=99)
        _, edges2 = generate_mock_network_data(num_edges=500, anomaly_rate=0.15, seed=99)
        self.assertEqual(
            edges1["is_ground_truth_anomaly"].tolist(),
            edges2["is_ground_truth_anomaly"].tolist(),
        )


if __name__ == "__main__":
    unittest.main()
