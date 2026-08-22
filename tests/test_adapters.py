"""
test_adapters.py — Unit tests for CICIDSAdapter, PaySimAdapter, and unified loader.

Tests cover:
- CICIDSAdapter header whitespace stripping, attack label resolution, schema compliance
- PaySimAdapter transaction type mapping, fraud flag handling, chunked load
- Unified loader interface, unsupported dataset handling, sentinel exception
"""

import unittest
from datasets.loader import load_dataset, available_datasets, DatasetNotAvailable
from datasets.schema import (
    PROVENANCE_CIC_IDS2017,
    PROVENANCE_PAYSIM,
    PROVENANCE_SYNTHETIC,
    SIGNAL_NETWORK_FLOW,
    SIGNAL_FINANCIAL_TXN,
    SIGNAL_ICS_READING,
)


class TestAdapters(unittest.TestCase):

    def test_available_datasets_dict(self):
        datasets = available_datasets()
        self.assertIn("cic_ids2017", datasets)
        self.assertIn("paysim", datasets)
        self.assertIn("synthetic", datasets)

    def test_cic_ids_adapter_loads_schema_batch(self):
        try:
            batch = load_dataset("cic_ids2017", limit=1000)
            self.assertGreater(len(batch), 0)
            self.assertEqual(batch.df["provenance"].iloc[0], PROVENANCE_CIC_IDS2017)
            batch.validate_schema()
        except DatasetNotAvailable:
            self.skipTest("CIC-IDS2017 dataset not available locally")

    def test_cic_ids_adapter_populates_signal_type_and_purdue_level(self):
        try:
            batch = load_dataset("cic_ids2017", limit=200)
            self.assertTrue((batch.df["signal_type"] == SIGNAL_NETWORK_FLOW).all())
            self.assertTrue((batch.df["purdue_level"] == 3).all())
        except DatasetNotAvailable:
            self.skipTest("CIC-IDS2017 dataset not available locally")

    def test_paysim_adapter_loads_schema_batch(self):
        try:
            batch = load_dataset("paysim", limit=1000)
            self.assertGreater(len(batch), 0)
            self.assertEqual(batch.df["provenance"].iloc[0], PROVENANCE_PAYSIM)
            batch.validate_schema()
        except DatasetNotAvailable:
            self.skipTest("PaySim dataset not available locally")

    def test_paysim_adapter_populates_signal_type_and_purdue_level(self):
        try:
            batch = load_dataset("paysim", limit=200)
            self.assertTrue((batch.df["signal_type"] == SIGNAL_FINANCIAL_TXN).all())
            self.assertTrue((batch.df["purdue_level"] == 4).all())
        except DatasetNotAvailable:
            self.skipTest("PaySim dataset not available locally")

    def test_paysim_fraud_only_mode(self):
        try:
            batch = load_dataset("paysim", limit=100, fraud_only=True)
            self.assertGreater(len(batch), 0)
            # All loaded rows should be fraud alerts
            self.assertTrue((batch.df["attck_evidence"] == "TRANSFER_FRAUD").all())
        except DatasetNotAvailable:
            self.skipTest("PaySim dataset not available locally")

    def test_synthetic_loader_fallback(self):
        batch = load_dataset("synthetic", limit=500)
        self.assertGreaterEqual(len(batch), 500)
        self.assertEqual(batch.df["provenance"].iloc[0], PROVENANCE_SYNTHETIC)
        batch.validate_schema()

    def test_synthetic_loader_action_reflects_ground_truth(self):
        """Regression test (Phase 3): _load_synthetic used to hardcode
        action="PASS" for every row regardless of data_generator's
        is_ground_truth_anomaly flag, making load_dataset("synthetic", ...)
        always 0% positive — a real, previously-silent cause of the
        degenerate-eval-split bug whenever evaluation.run_evaluation() fell
        back to synthetic data. Some ALERT rows must now be present."""
        from datasets.schema import ACTION_ALERT, ACTION_PASS
        batch = load_dataset("synthetic", limit=800)
        actions = set(batch.df["action"].unique())
        self.assertIn(ACTION_ALERT, actions)
        self.assertIn(ACTION_PASS, actions)
        # Should roughly track SETTINGS.data_gen.anomaly_rate (0.15 default) —
        # loose bounds only, this is a randomized generator.
        alert_rate = (batch.df["action"] == ACTION_ALERT).mean()
        self.assertGreater(alert_rate, 0.01)
        self.assertLess(alert_rate, 0.5)

    def test_swat_loads_schema_batch(self):
        from datasets.schema import PROVENANCE_SWAT
        try:
            batch = load_dataset("swat", limit=1000)
            self.assertGreater(len(batch), 0)
            self.assertEqual(batch.df["provenance"].iloc[0], PROVENANCE_SWAT)
            batch.validate_schema()
        except DatasetNotAvailable:
            self.skipTest("SWaT dataset not available locally")

    def test_swat_adapter_populates_signal_type_and_purdue_level(self):
        try:
            batch = load_dataset("swat", limit=200)
            self.assertTrue((batch.df["signal_type"] == SIGNAL_ICS_READING).all())
            self.assertTrue((batch.df["purdue_level"] == 1).all())
        except DatasetNotAvailable:
            self.skipTest("SWaT dataset not available locally")

    def test_invalid_dataset_name_raises_value_error(self):
        with self.assertRaises(ValueError):
            load_dataset("nonexistent_dataset")


if __name__ == "__main__":
    unittest.main()
