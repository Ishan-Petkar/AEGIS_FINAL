"""
test_schema.py — Unit tests for CanonicalEvent schema and CanonicalBatch.

Tests cover:
- Pydantic validation (required fields, valid types, bounds)
- Whitespace stripping on string fields
- CanonicalBatch creation from events and DataFrame
- Missing column detection in CanonicalBatch
- Filtering by provenance tag
- Feature matrix extraction for ML engine
"""

import unittest
from datetime import datetime, timezone
import pandas as pd
from pydantic import ValidationError

from datasets.schema import (
    CanonicalEvent,
    CanonicalBatch,
    CANONICAL_COLUMNS,
    PROVENANCE_CIC_IDS2017,
    PROVENANCE_PAYSIM,
    ACTION_PASS,
    ACTION_ALERT,
    SCHEMA_VERSION,
    SIGNAL_NETWORK_FLOW,
    SIGNAL_DECEPTION_TRIPWIRE,
)


class TestCanonicalEvent(unittest.TestCase):

    def _sample_event_dict(self):
        return {
            "timestamp": datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc),
            "source_asset_id": "Traffic_Cam_1 ",  # extra space to test stripping
            "destination_asset_id": "Traffic_Controller",
            "protocol": "TCP",
            "payload_size": 1500.0,
            "action": ACTION_PASS,
            "zone": "INTERNAL",
            "process_or_service": "HTTP",
            "attck_evidence": None,
            "raw_anomaly_score": None,
            "calibrated_alert_level": None,
            "provenance": PROVENANCE_CIC_IDS2017,
            "confidence": 1.0,
        }

    def test_valid_event_creation(self):
        data = self._sample_event_dict()
        event = CanonicalEvent(**data)
        self.assertEqual(event.source_asset_id, "Traffic_Cam_1")  # whitespace stripped
        self.assertEqual(event.payload_size, 1500.0)
        self.assertEqual(event.confidence, 1.0)
        self.assertEqual(event.schema_version, SCHEMA_VERSION)
        self.assertEqual(event.signal_type, SIGNAL_NETWORK_FLOW)
        self.assertIsNone(event.observed_at)
        self.assertIsNone(event.purdue_level)

    def test_iso_string_timestamp_coerced(self):
        data = self._sample_event_dict()
        data["timestamp"] = "2026-01-01T12:00:00+00:00"
        event = CanonicalEvent(**data)
        self.assertIsInstance(event.timestamp, datetime)

    def test_payload_size_cannot_be_negative(self):
        data = self._sample_event_dict()
        data["payload_size"] = -10.0
        with self.assertRaises(ValidationError):
            CanonicalEvent(**data)

    def test_confidence_bounds_enforced(self):
        data = self._sample_event_dict()
        data["confidence"] = 1.5
        with self.assertRaises(ValidationError):
            CanonicalEvent(**data)

    def test_invalid_signal_type_rejected(self):
        data = self._sample_event_dict()
        data["signal_type"] = "not_a_real_signal_type"
        with self.assertRaises(ValidationError):
            CanonicalEvent(**data)

    def test_deception_tripwire_signal_type_accepted(self):
        data = self._sample_event_dict()
        data["signal_type"] = SIGNAL_DECEPTION_TRIPWIRE
        event = CanonicalEvent(**data)
        self.assertEqual(event.signal_type, SIGNAL_DECEPTION_TRIPWIRE)

    def test_purdue_level_bounds_enforced(self):
        data = self._sample_event_dict()
        data["purdue_level"] = 6  # valid range is 0-5
        with self.assertRaises(ValidationError):
            CanonicalEvent(**data)

    def test_purdue_level_accepts_valid_range(self):
        data = self._sample_event_dict()
        data["purdue_level"] = 1
        event = CanonicalEvent(**data)
        self.assertEqual(event.purdue_level, 1)

    def test_to_dict_keys_match_canonical_columns(self):
        event = CanonicalEvent(**self._sample_event_dict())
        d = event.to_dict()
        self.assertEqual(set(d.keys()), set(CANONICAL_COLUMNS))


class TestCanonicalBatch(unittest.TestCase):

    def _sample_event(self, provenance=PROVENANCE_CIC_IDS2017, evidence=None):
        return CanonicalEvent(
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source_asset_id="A",
            destination_asset_id="B",
            protocol="TCP",
            payload_size=100.0,
            action=ACTION_PASS if evidence is None else ACTION_ALERT,
            zone="INTERNAL",
            process_or_service="HTTP",
            attck_evidence=evidence,
            provenance=provenance,
            confidence=1.0,
        )

    def test_from_events(self):
        events = [self._sample_event(), self._sample_event(evidence="DDoS")]
        batch = CanonicalBatch.from_events(events)
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch.is_alert().sum(), 1)

    def test_empty_batch_handling(self):
        batch = CanonicalBatch.from_events([])
        self.assertEqual(len(batch), 0)

    def test_missing_column_raises(self):
        df = pd.DataFrame({"source_asset_id": ["A"]})
        with self.assertRaises(ValueError):
            CanonicalBatch.from_dataframe(df)

    def test_filter_by_provenance(self):
        events = [
            self._sample_event(provenance=PROVENANCE_CIC_IDS2017),
            self._sample_event(provenance=PROVENANCE_PAYSIM),
        ]
        batch = CanonicalBatch.from_events(events)
        filtered = batch.filter_by_provenance(PROVENANCE_PAYSIM)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered.df["provenance"].iloc[0], PROVENANCE_PAYSIM)

    def test_to_ml_features_shape(self):
        events = [self._sample_event()]
        batch = CanonicalBatch.from_events(events)
        X_df = batch.to_ml_features()
        self.assertIn("bytes", X_df.columns)
        self.assertIn("packets", X_df.columns)
        self.assertIn("duration_sec", X_df.columns)

    def test_to_ml_features_zeroes_tripwire_volume(self):
        """A deception_tripwire event has no real flow volume — to_ml_features()
        must not fabricate duration_sec/packets/bytes from payload_size for it,
        or a tripwire touch would look like a volume anomaly to the detector
        (see PLAN_MASTER.md Phase 1, contract C4)."""
        normal = self._sample_event()
        tripwire = CanonicalEvent(
            timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
            source_asset_id="Attacker",
            destination_asset_id="Gateway_L4",
            protocol="HTTP",
            payload_size=999_999.0,  # large payload_size should NOT leak into bytes
            action=ACTION_ALERT,
            zone="DMZ",
            process_or_service="Honeytoken",
            provenance=PROVENANCE_CIC_IDS2017,
            confidence=1.0,
            signal_type=SIGNAL_DECEPTION_TRIPWIRE,
        )
        batch = CanonicalBatch.from_events([normal, tripwire])
        X_df = batch.to_ml_features()
        self.assertEqual(X_df.iloc[1]["bytes"], 0.0)
        self.assertEqual(X_df.iloc[1]["packets"], 0)
        self.assertEqual(X_df.iloc[1]["duration_sec"], 0.0)
        # The normal event is unaffected.
        self.assertEqual(X_df.iloc[0]["bytes"], 100.0)


if __name__ == "__main__":
    unittest.main()
