"""
test_deception_tripwire.py — Phase 2 deception/tripwire integration tests.

Covers: detector protocol conformance, non-fabrication of flow fields
(contract C4), recon-before-exfil lead time, tripwire+volume fusion
(confidence escalation), and gateway-only CII scenarios.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


# ---------------------------------------------------------------------------
# TripwireDetector protocol conformance
# ---------------------------------------------------------------------------

class TestTripwireDetectorProtocol(unittest.TestCase):

    def test_is_base_detector_subclass(self):
        from deception.tripwire import TripwireDetector
        from detectors.base import BaseDetector
        self.assertTrue(issubclass(TripwireDetector, BaseDetector))

    def test_constructible_with_no_required_args(self):
        from deception.tripwire import TripwireDetector
        TripwireDetector()  # should not raise

    def test_fit_returns_self(self):
        from deception.tripwire import TripwireDetector
        det = TripwireDetector()
        X = np.array([[0.0], [1.0]])
        self.assertIs(det.fit(X), det)

    def test_predict_flags_honeytoken_rows_as_anomaly(self):
        from deception.tripwire import TripwireDetector
        det = TripwireDetector().fit(None)
        X = np.array([[0.0], [1.0], [0.0], [1.0]])
        preds = det.predict(X)
        np.testing.assert_array_equal(preds, [1, -1, 1, -1])

    def test_score_samples_lower_is_more_anomalous(self):
        from deception.tripwire import TripwireDetector
        det = TripwireDetector().fit(None)
        X = np.array([[0.0], [1.0]])
        scores = det.score_samples(X)
        self.assertLess(scores[1], scores[0])
        self.assertLess(scores[1], 0.0)

    def test_features_from_df_missing_column_defaults_false(self):
        from deception.tripwire import TripwireDetector
        df = pd.DataFrame({"foo": [1, 2, 3]})
        X = TripwireDetector.features_from_df(df)
        self.assertEqual(X.shape, (3, 1))
        self.assertTrue((X == 0.0).all())

    def test_features_from_df_reads_is_honeytoken_use(self):
        from deception.tripwire import TripwireDetector
        df = pd.DataFrame({"is_honeytoken_use": [True, False, np.nan]})
        X = TripwireDetector.features_from_df(df)
        np.testing.assert_array_equal(X.flatten(), [1.0, 0.0, 0.0])

    def test_registered_in_detector_registry(self):
        from detectors.registry import DETECTORS
        from deception.tripwire import TripwireDetector
        self.assertIn("tripwire", DETECTORS)
        self.assertIs(DETECTORS["tripwire"], TripwireDetector)

    def test_end_to_end_protocol_on_registry_entry(self):
        from detectors.registry import DETECTORS
        cls = DETECTORS["tripwire"]
        det = cls()
        X = np.array([[1.0], [0.0]])
        det.fit(X)
        preds = det.predict(X)
        scores = det.score_samples(X)
        self.assertEqual(list(preds), [-1, 1])
        self.assertEqual(len(scores), 2)


# ---------------------------------------------------------------------------
# Tripwire event generation — no fabricated flow fields (contract C4)
# ---------------------------------------------------------------------------

class TestGenerateTripwireEvents(unittest.TestCase):

    def test_unknown_gateway_zone_raises(self):
        from deception.adapter import generate_tripwire_events
        with self.assertRaises(KeyError):
            generate_tripwire_events("Gateway_L99")

    def test_returns_canonical_columns(self):
        from deception.adapter import generate_tripwire_events
        from datasets.schema import CANONICAL_COLUMNS
        df = generate_tripwire_events("Gateway_L4")
        for col in CANONICAL_COLUMNS:
            self.assertIn(col, df.columns)

    def test_does_not_fabricate_flow_fields(self):
        from deception.adapter import generate_tripwire_events
        df = generate_tripwire_events("Gateway_L4", count=5)
        self.assertTrue((df["duration_sec"] == 0.0).all())
        self.assertTrue((df["packets"] == 0).all())
        self.assertTrue((df["bytes"] == 0.0).all())
        self.assertTrue((df["payload_size"] == 0.0).all())

    def test_is_honeytoken_use_column_always_true(self):
        from deception.adapter import generate_tripwire_events
        df = generate_tripwire_events("Gateway_L2", count=3)
        self.assertTrue(df["is_honeytoken_use"].all())

    def test_signal_type_is_deception_tripwire(self):
        from deception.adapter import generate_tripwire_events
        from datasets.schema import SIGNAL_DECEPTION_TRIPWIRE
        df = generate_tripwire_events("Gateway_L1")
        self.assertTrue((df["signal_type"] == SIGNAL_DECEPTION_TRIPWIRE).all())

    def test_destination_is_the_gateway_zone(self):
        from deception.adapter import generate_tripwire_events
        df = generate_tripwire_events("Gateway_L5")
        self.assertTrue((df["destination_asset_id"] == "Gateway_L5").all())

    def test_purdue_level_parsed_from_zone_name(self):
        from deception.adapter import generate_tripwire_events
        df = generate_tripwire_events("Gateway_L3")
        self.assertTrue((df["purdue_level"] == 3).all())

    def test_observed_at_is_after_timestamp(self):
        from deception.adapter import generate_tripwire_events
        df = generate_tripwire_events("Gateway_L4")
        self.assertGreater(df.iloc[0]["observed_at"], df.iloc[0]["timestamp"])

    def test_count_zero_returns_empty_frame(self):
        from deception.adapter import generate_tripwire_events
        df = generate_tripwire_events("Gateway_L4", count=0)
        self.assertEqual(len(df), 0)

    def test_count_generates_multiple_rows(self):
        from deception.adapter import generate_tripwire_events
        df = generate_tripwire_events("Gateway_L0", count=4)
        self.assertEqual(len(df), 4)

    def test_negative_count_raises(self):
        from deception.adapter import generate_tripwire_events
        with self.assertRaises(ValueError):
            generate_tripwire_events("Gateway_L4", count=-1)

    def test_builds_valid_canonical_batch(self):
        from deception.adapter import generate_tripwire_events
        from datasets.schema import CanonicalBatch
        df = generate_tripwire_events("Gateway_L4", count=2)
        batch = CanonicalBatch.from_dataframe(df)  # should not raise
        self.assertEqual(len(batch), 2)

    def test_credential_id_embedded_in_attck_evidence(self):
        from deception.adapter import generate_tripwire_events
        from config import HONEYTOKEN_CREDENTIALS
        df = generate_tripwire_events("Gateway_L4")
        cred_id = HONEYTOKEN_CREDENTIALS["Gateway_L4"]["credential_id"]
        self.assertIn(cred_id, df.iloc[0]["attck_evidence"])

    def test_reproducible_with_same_seed(self):
        from deception.adapter import generate_tripwire_events
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        df1 = generate_tripwire_events("Gateway_L4", count=3, seed=7, base_timestamp=base)
        df2 = generate_tripwire_events("Gateway_L4", count=3, seed=7, base_timestamp=base)
        pd.testing.assert_series_equal(df1["timestamp"], df2["timestamp"])


# ---------------------------------------------------------------------------
# datasets.loader "deception" source
# ---------------------------------------------------------------------------

class TestDeceptionDatasetLoader(unittest.TestCase):

    def test_deception_in_supported_datasets(self):
        from datasets.loader import SUPPORTED_DATASETS
        self.assertIn("deception", SUPPORTED_DATASETS)

    def test_load_dataset_deception_returns_batch(self):
        from datasets.loader import load_dataset
        from config import HONEYTOKEN_CREDENTIALS
        batch = load_dataset("deception")
        self.assertEqual(len(batch), len(HONEYTOKEN_CREDENTIALS))

    def test_load_dataset_deception_all_tripwire_signal(self):
        from datasets.loader import load_dataset
        from datasets.schema import SIGNAL_DECEPTION_TRIPWIRE
        batch = load_dataset("deception")
        self.assertTrue((batch.df["signal_type"] == SIGNAL_DECEPTION_TRIPWIRE).all())

    def test_load_dataset_deception_covers_every_gateway_zone(self):
        from datasets.loader import load_dataset
        from config import HONEYTOKEN_CREDENTIALS
        batch = load_dataset("deception")
        zones = set(batch.df["destination_asset_id"])
        self.assertEqual(zones, set(HONEYTOKEN_CREDENTIALS))

    def test_load_dataset_deception_respects_limit(self):
        from datasets.loader import load_dataset
        batch = load_dataset("deception", limit=2)
        self.assertLessEqual(len(batch), 2)

    def test_load_dataset_deception_count_per_zone_kwarg(self):
        from datasets.loader import load_dataset
        from config import HONEYTOKEN_CREDENTIALS
        batch = load_dataset("deception", limit=None, count_per_zone=3)
        self.assertEqual(len(batch), 3 * len(HONEYTOKEN_CREDENTIALS))

    def test_available_datasets_lists_deception(self):
        from datasets.loader import available_datasets
        avail = available_datasets()
        self.assertIn("deception", avail)
        self.assertNotIn("[NOT FOUND]", avail["deception"])


# ---------------------------------------------------------------------------
# Recon stage timing (data_generator.generate_scripted_attack)
# ---------------------------------------------------------------------------

class TestScriptedAttackRecon(unittest.TestCase):

    def test_unknown_attack_name_raises(self):
        from data_generator import generate_scripted_attack
        with self.assertRaises(ValueError):
            generate_scripted_attack("Not A Real Attack", {})

    def test_returns_two_events(self):
        from data_generator import generate_scripted_attack
        events = generate_scripted_attack(
            "Payment Gateway Breach",
            {"source": "10.0.1.20", "target": "198.51.100.42",
             "duration_sec": 120.0, "packets": 100000, "bytes": 500_000_000},
        )
        self.assertEqual(len(events), 2)

    def test_recon_event_is_honeytoken_use(self):
        from data_generator import generate_scripted_attack
        recon, exfil = generate_scripted_attack(
            "Camera Spoofing",
            {"source": "10.0.1.10", "target": "10.0.1.12",
             "duration_sec": 100.0, "packets": 50000, "bytes": 500_000_000},
        )
        self.assertTrue(recon["is_honeytoken_use"])
        self.assertFalse(exfil["is_honeytoken_use"])

    def test_recon_does_not_fabricate_flow_fields(self):
        from data_generator import generate_scripted_attack
        recon, _exfil = generate_scripted_attack(
            "Data Exfiltration",
            {"source": "10.0.1.16", "target": "45.227.254.12",
             "duration_sec": 300.0, "packets": 150000, "bytes": 1_500_000_000},
        )
        self.assertEqual(recon["duration_sec"], 0.0)
        self.assertEqual(recon["packets"], 0)
        self.assertEqual(recon["bytes"], 0.0)

    def test_recon_fires_measurably_before_exfil(self):
        from data_generator import generate_scripted_attack
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        recon, exfil = generate_scripted_attack(
            "Lateral Movement",
            {"source": "10.0.1.15", "target": "10.0.1.13",
             "duration_sec": 45.0, "packets": 20000, "bytes": 20_000_000},
            recon_delay_sec=90,
            base_timestamp=base,
        )
        recon_ts = datetime.strptime(recon["timestamp"], "%Y-%m-%d %H:%M:%S")
        exfil_ts = datetime.strptime(exfil["timestamp"], "%Y-%m-%d %H:%M:%S")
        delta = (exfil_ts - recon_ts).total_seconds()
        self.assertGreaterEqual(delta, 89)  # allow the recon's own sub-second jitter
        self.assertLessEqual(delta, 91)

    def test_default_recon_delay_from_settings(self):
        from data_generator import generate_scripted_attack
        from settings import SETTINGS
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        recon, exfil = generate_scripted_attack(
            "Payment Gateway Breach",
            {"source": "10.0.1.20", "target": "198.51.100.42",
             "duration_sec": 120.0, "packets": 100000, "bytes": 500_000_000},
            base_timestamp=base,
        )
        recon_ts = datetime.strptime(recon["timestamp"], "%Y-%m-%d %H:%M:%S")
        exfil_ts = datetime.strptime(exfil["timestamp"], "%Y-%m-%d %H:%M:%S")
        delta = (exfil_ts - recon_ts).total_seconds()
        self.assertAlmostEqual(delta, SETTINGS.deception.recon_delay_sec, delta=2)

    def test_all_four_scripted_attacks_have_recon_gateways(self):
        from data_generator import ATTACK_RECON_GATEWAY, generate_scripted_attack
        for name in ["Payment Gateway Breach", "Camera Spoofing",
                     "Data Exfiltration", "Lateral Movement"]:
            self.assertIn(name, ATTACK_RECON_GATEWAY)
            events = generate_scripted_attack(name, {"source": "a", "target": "b",
                                                       "duration_sec": 1.0, "packets": 1, "bytes": 1})
            self.assertEqual(len(events), 2)


# ---------------------------------------------------------------------------
# Fusion: tripwire + volume in core/pipeline.run_analysis
# ---------------------------------------------------------------------------

class TestPipelineFusion(unittest.TestCase):

    def test_tripwire_only_row_flagged_anomalous(self):
        from core.pipeline import run_analysis
        recon_event = {
            "source": "Unknown_External_Probe", "target": "Gateway_L4",
            "duration_sec": 0.0, "packets": 0, "bytes": 0.0,
            "timestamp": "2026-01-01 00:00:00", "is_honeytoken_use": True,
        }
        result = run_analysis(dataset="synthetic", attack_edges=[recon_event])
        tw_rows = result.edges_df[result.edges_df["is_honeytoken_use"] == True]  # noqa: E712
        self.assertGreater(len(tw_rows), 0)
        self.assertTrue(tw_rows["is_anomaly"].all())
        self.assertTrue(tw_rows["tripwire_fired"].all())

    def test_tripwire_only_escalates_confidence_not_max(self):
        from core.pipeline import run_analysis
        from settings import SETTINGS
        recon_event = {
            "source": "Unknown_External_Probe", "target": "Gateway_L4",
            "duration_sec": 0.0, "packets": 0, "bytes": 0.0,
            "timestamp": "2026-01-01 00:00:00", "is_honeytoken_use": True,
        }
        result = run_analysis(dataset="synthetic", attack_edges=[recon_event])
        tw_rows = result.edges_df[result.edges_df["is_honeytoken_use"] == True]  # noqa: E712
        self.assertTrue((tw_rows["confidence"] == SETTINGS.deception.confidence_tripwire_only).all())

    def test_no_honeytoken_column_all_confidence_reflects_volume_only_or_none(self):
        from core.pipeline import run_analysis
        from settings import SETTINGS
        result = run_analysis(dataset="synthetic")
        allowed = {SETTINGS.deception.confidence_volume_only, SETTINGS.deception.confidence_none}
        self.assertTrue(set(result.edges_df["confidence"].unique()).issubset(allowed))

    def test_tripwire_alone_triggers_cii_without_explicit_attack(self):
        from core.pipeline import run_analysis
        recon_event = {
            "source": "Unknown_External_Probe", "target": "Gateway_L4",
            "duration_sec": 0.0, "packets": 0, "bytes": 0.0,
            "timestamp": "2026-01-01 00:00:00", "is_honeytoken_use": True,
        }
        result = run_analysis(dataset="synthetic", attack_edges=[recon_event])
        self.assertGreater(result.cii.cii_median, 0.0)

    def test_fusion_column_present_even_without_any_tripwire(self):
        from core.pipeline import run_analysis
        result = run_analysis(dataset="synthetic")
        self.assertIn("tripwire_fired", result.edges_df.columns)
        self.assertFalse(result.edges_df["tripwire_fired"].any())


# ---------------------------------------------------------------------------
# CII gateway-guarded and gateway-only (no guarded asset) scenarios
# ---------------------------------------------------------------------------

class TestCIIGatewayTripwireScenarios(unittest.TestCase):

    def test_gateway_with_guarded_asset_produces_nonzero_cii(self):
        from cii_calculator import compute_cascading_impact_full
        # Gateway_L4 guards City_Payment_Gateway / Social_Welfare_System.
        result = compute_cascading_impact_full(
            "Gateway_L4", 0.98, criticality_map={}, random_seed=1,
        )
        self.assertGreater(result.cii_median, 0.0)

    def test_gateway_with_no_guarded_asset_still_nonzero(self):
        from cii_calculator import compute_cascading_impact_full
        # No purdue_level-0 asset meets the gateway criticality threshold,
        # so Gateway_L0 guards nothing in DEPENDENCY_GRAPH today.
        result = compute_cascading_impact_full(
            "Gateway_L0", 0.98, criticality_map={}, random_seed=1,
        )
        self.assertGreater(result.cii_median, 0.0)
        self.assertIn("Gateway_L0", result.impacted_assets)

    def test_gateway_target_assets_matches_dependency_graph_reality(self):
        import graph_manager
        guarded_l4 = graph_manager.gateway_target_assets("Gateway_L4")
        self.assertIn("City_Payment_Gateway", guarded_l4)
        guarded_l0 = graph_manager.gateway_target_assets("Gateway_L0")
        self.assertEqual(guarded_l0, [])

    def test_unrelated_unknown_node_still_returns_empty_result(self):
        from cii_calculator import compute_cascading_impact_full
        result = compute_cascading_impact_full(
            "Totally_Unknown_Node_123", 0.98, criticality_map={}, random_seed=1,
        )
        self.assertEqual(result.cii_median, 0.0)
        self.assertEqual(result.impacted_assets, [])

    def test_gateway_only_result_is_deterministic(self):
        from cii_calculator import compute_cascading_impact_full
        r1 = compute_cascading_impact_full("Gateway_L2", 0.5, criticality_map={}, random_seed=1)
        r2 = compute_cascading_impact_full("Gateway_L2", 0.5, criticality_map={}, random_seed=1)
        self.assertEqual(r1.cii_median, r2.cii_median)


if __name__ == "__main__":
    unittest.main()
