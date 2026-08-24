"""
tests/test_replay_reader.py — Phase 5 Ticket #5b: `backend.replay_reader`.

Two classes of test here:

1. Pure-function / synthetic-CSV tests (AM/PM correction, malformed-row
   resilience, unit conversion, stable-sort tie-breaking) — never touch
   the real dataset, always run.
2. Real-dataset tests (chronological ordering across real files,
   timing_provenance detection, real IPs, source_row_id uniqueness,
   limit semantics) — skip cleanly via `_require_real_dataset()` if
   `datasets/TrafficLabelling ` is not present on disk, matching the
   repo's existing graceful-degradation convention (CLAUDE.md section 5;
   see tests/test_adapters.py for the same pattern against
   MachineLearningCVE/PaySim/SWaT).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from backend.models import (  # noqa: E402
    TIMING_PROVENANCE_CAPTURE_SECONDS,
    TIMING_PROVENANCE_INTERPOLATED_MINUTE_BUCKET,
)
from backend.replay_reader import (  # noqa: E402
    ReplayFlowReader,
    _parse_timestamp,
)
from datasets.loader import DatasetNotAvailable  # noqa: E402

_REQUIRED_HEADER = [
    "Source IP",
    "Source Port",
    "Destination IP",
    "Destination Port",
    "Protocol",
    "Timestamp",
    "Flow Duration",
    "Total Fwd Packets",
    "Total Length of Fwd Packets",
    "Label",
]


def _require_real_dataset() -> ReplayFlowReader:
    reader = ReplayFlowReader()
    if not reader.data_dir.exists():
        pytest.skip(
            f"CIC-IDS2017 TrafficLabelling dataset not found at {reader.data_dir}; "
            "see docs/DATASETS.md."
        )
    return reader


def _write_csv(path: Path, rows: list[list[str]]) -> None:
    with open(path, "w", encoding="latin-1", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_REQUIRED_HEADER)
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# AM/PM correction (pure function, Fact 1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_hour,expected_hour",
    [
        (1, 13),
        (2, 14),
        (3, 15),
        (4, 16),
        (5, 17),
        (6, 18),
        (7, 19),
        (8, 8),
        (9, 9),
        (10, 10),
        (11, 11),
        (12, 12),
    ],
)
def test_am_pm_correction(raw_hour, expected_hour):
    raw = f"7/7/2017 {raw_hour}:15"
    result = _parse_timestamp(raw)
    assert result is not None
    ts, provenance = result
    assert ts.hour == expected_hour
    assert ts.minute == 15
    assert provenance == TIMING_PROVENANCE_INTERPOLATED_MINUTE_BUCKET


def test_am_pm_correction_with_seconds_is_capture_seconds():
    raw = "03/07/2017 08:55:58"
    result = _parse_timestamp(raw)
    assert result is not None
    ts, provenance = result
    assert ts.hour == 8
    assert ts.minute == 55
    assert ts.second == 58
    assert provenance == TIMING_PROVENANCE_CAPTURE_SECONDS


def test_am_pm_correction_pm_with_seconds():
    raw = "7/7/2017 3:30:45"
    result = _parse_timestamp(raw)
    assert result is not None
    ts, provenance = result
    assert ts.hour == 15
    assert provenance == TIMING_PROVENANCE_CAPTURE_SECONDS


def test_unparseable_timestamp_returns_none():
    assert _parse_timestamp("") is None
    assert _parse_timestamp("not a timestamp") is None
    assert _parse_timestamp("7/7/2017 13:00") is None  # hour > 12, outside documented range
    assert _parse_timestamp("7/7/2017") is None  # missing time part


# ---------------------------------------------------------------------------
# Unit conversion — Flow Duration microseconds -> duration_sec
# ---------------------------------------------------------------------------


def test_flow_duration_microseconds_to_seconds(tmp_path):
    _write_csv(
        tmp_path / "Monday-WorkingHours.pcap_ISCX.csv",
        [
            ["192.168.1.1", "80", "10.0.0.5", "443", "6", "7/7/2017 9:05:10", "2500000", "10", "1000", "BENIGN"],
        ],
    )
    reader = ReplayFlowReader(data_dir=tmp_path)
    flows = list(reader.iter_flows(day="monday"))
    assert len(flows) == 1
    assert flows[0].duration_sec == pytest.approx(2.5)


def test_negative_duration_clamped_at_zero(tmp_path):
    _write_csv(
        tmp_path / "Monday-WorkingHours.pcap_ISCX.csv",
        [
            ["192.168.1.1", "80", "10.0.0.5", "443", "6", "7/7/2017 9:05:10", "-500000", "10", "1000", "BENIGN"],
        ],
    )
    reader = ReplayFlowReader(data_dir=tmp_path)
    flows = list(reader.iter_flows(day="monday"))
    assert len(flows) == 1
    assert flows[0].duration_sec == 0.0


# ---------------------------------------------------------------------------
# Malformed-row resilience
# ---------------------------------------------------------------------------


def test_malformed_rows_skipped_and_counted(tmp_path):
    rows = [
        # good row
        ["192.168.1.1", "80", "10.0.0.5", "443", "6", "7/7/2017 9:05:10", "500000", "10", "1000", "BENIGN"],
        # NaN row — empty Flow Duration
        ["192.168.1.2", "80", "10.0.0.6", "443", "6", "7/7/2017 9:06:10", "", "10", "1000", "BENIGN"],
        # repeated header row mid-file
        _REQUIRED_HEADER,
        # ragged row — too few columns
        ["192.168.1.3", "80", "10.0.0.7"],
        # good row
        ["192.168.1.4", "81", "10.0.0.8", "444", "17", "7/7/2017 9:07:00", "600000", "5", "500", "PortScan"],
    ]
    _write_csv(tmp_path / "Monday-WorkingHours.pcap_ISCX.csv", rows)
    reader = ReplayFlowReader(data_dir=tmp_path)
    flows = list(reader.iter_flows(day="monday"))

    assert len(flows) == 2
    assert {f.source_ip for f in flows} == {"192.168.1.1", "192.168.1.4"}

    stats = reader.last_read_stats
    assert stats is not None
    assert stats.rows_seen == 5
    assert stats.rows_emitted == 2
    assert stats.skipped_short_or_ragged == 1
    assert stats.skipped_repeated_header == 1
    assert stats.skipped_unparseable_fields == 1
    assert stats.skipped_bad_timestamp == 0


def test_bad_timestamp_row_skipped_and_counted(tmp_path):
    rows = [
        ["192.168.1.1", "80", "10.0.0.5", "443", "6", "not-a-timestamp", "500000", "10", "1000", "BENIGN"],
        ["192.168.1.4", "81", "10.0.0.8", "444", "17", "7/7/2017 9:07:00", "600000", "5", "500", "PortScan"],
    ]
    _write_csv(tmp_path / "Monday-WorkingHours.pcap_ISCX.csv", rows)
    reader = ReplayFlowReader(data_dir=tmp_path)
    flows = list(reader.iter_flows(day="monday"))

    assert len(flows) == 1
    stats = reader.last_read_stats
    assert stats.skipped_bad_timestamp == 1
    assert stats.rows_emitted == 1


# ---------------------------------------------------------------------------
# Stable sort — ties keep original file order (P5-7)
# ---------------------------------------------------------------------------


def test_stable_sort_preserves_file_order_within_equal_timestamps(tmp_path):
    same_ts = "7/7/2017 9:00"
    rows = [
        ["10.0.0.1", "1001", "10.0.0.9", "80", "6", same_ts, "1000", "1", "1", "BENIGN"],
        ["10.0.0.2", "1002", "10.0.0.9", "80", "6", same_ts, "1000", "1", "1", "BENIGN"],
        ["10.0.0.3", "1003", "10.0.0.9", "80", "6", same_ts, "1000", "1", "1", "BENIGN"],
    ]
    _write_csv(tmp_path / "Monday-WorkingHours.pcap_ISCX.csv", rows)
    reader = ReplayFlowReader(data_dir=tmp_path)
    flows = list(reader.iter_flows(day="monday"))

    assert [f.source_port for f in flows] == [1001, 1002, 1003]
    assert len({f.ts for f in flows}) == 1  # all shared the same corrected ts


# ---------------------------------------------------------------------------
# source_row_id — stable identifier, unique within a synthetic file
# ---------------------------------------------------------------------------


def test_source_row_id_format_and_uniqueness(tmp_path):
    rows = [
        ["10.0.0.1", "1001", "10.0.0.9", "80", "6", "7/7/2017 9:00", "1000", "1", "1", "BENIGN"],
        ["10.0.0.2", "1002", "10.0.0.9", "80", "6", "7/7/2017 9:01", "1000", "1", "1", "BENIGN"],
    ]
    _write_csv(tmp_path / "Monday-WorkingHours.pcap_ISCX.csv", rows)
    reader = ReplayFlowReader(data_dir=tmp_path)
    flows = list(reader.iter_flows(day="monday"))

    ids = [f.source_row_id for f in flows]
    assert len(ids) == len(set(ids))
    assert all(i.startswith("Monday-WorkingHours.pcap_ISCX.csv:") for i in ids)


# ---------------------------------------------------------------------------
# is_attack / label
# ---------------------------------------------------------------------------


def test_is_attack_flag(tmp_path):
    rows = [
        ["10.0.0.1", "1001", "10.0.0.9", "80", "6", "7/7/2017 9:00", "1000", "1", "1", "BENIGN"],
        ["10.0.0.2", "1002", "10.0.0.9", "80", "6", "7/7/2017 9:01", "1000", "1", "1", "PortScan"],
    ]
    _write_csv(tmp_path / "Monday-WorkingHours.pcap_ISCX.csv", rows)
    reader = ReplayFlowReader(data_dir=tmp_path)
    flows = {f.source_ip: f for f in reader.iter_flows(day="monday")}

    assert flows["10.0.0.1"].is_attack is False
    assert flows["10.0.0.2"].is_attack is True


# ---------------------------------------------------------------------------
# Errors — missing directory / unknown day / missing file
# ---------------------------------------------------------------------------


def test_missing_data_dir_raises_dataset_not_available(tmp_path):
    reader = ReplayFlowReader(data_dir=tmp_path / "does-not-exist")
    with pytest.raises(DatasetNotAvailable):
        list(reader.iter_flows(day="monday"))


def test_unknown_day_raises_dataset_not_available(tmp_path):
    _write_csv(
        tmp_path / "Monday-WorkingHours.pcap_ISCX.csv",
        [["10.0.0.1", "1001", "10.0.0.9", "80", "6", "7/7/2017 9:00", "1000", "1", "1", "BENIGN"]],
    )
    reader = ReplayFlowReader(data_dir=tmp_path)
    with pytest.raises(DatasetNotAvailable):
        list(reader.iter_flows(day="not-a-real-day"))


def test_missing_day_file_raises_dataset_not_available(tmp_path):
    tmp_path.mkdir(exist_ok=True)  # dir exists but empty
    reader = ReplayFlowReader(data_dir=tmp_path)
    with pytest.raises(DatasetNotAvailable):
        list(reader.iter_flows(day="tuesday"))


# ---------------------------------------------------------------------------
# Ticket #5b review fix — default day defers to BACKEND_SETTINGS, not a
# locally-held constant (the replay-day config divergence)
# ---------------------------------------------------------------------------


def test_iter_flows_no_day_resolves_to_backend_settings_default(tmp_path):
    from backend.config import BACKEND_SETTINGS

    assert BACKEND_SETTINGS.replay_default_dataset_day == "friday-morning"

    _write_csv(
        tmp_path / "Friday-WorkingHours-Morning.pcap_ISCX.csv",
        [["10.0.0.1", "1001", "10.0.0.9", "80", "6", "7/7/2017 9:00", "1000", "1", "1", "BENIGN"]],
    )
    reader = ReplayFlowReader(data_dir=tmp_path)
    flows = list(reader.iter_flows())  # no day argument
    assert len(flows) == 1
    assert reader.last_read_stats.day == "friday-morning"


def test_iter_flows_no_day_defers_to_monkeypatched_setting(tmp_path, monkeypatch):
    """Proves the reader genuinely reads BACKEND_SETTINGS at call time,
    rather than caching the default in a module- or class-level constant
    at import time."""
    from backend import replay_reader
    from backend.config import BackendSettings

    _write_csv(
        tmp_path / "Monday-WorkingHours.pcap_ISCX.csv",
        [["10.0.0.1", "1001", "10.0.0.9", "80", "6", "7/7/2017 9:00", "1000", "1", "1", "BENIGN"]],
    )
    monkeypatch.setattr(
        replay_reader,
        "BACKEND_SETTINGS",
        BackendSettings(replay_default_dataset_day="monday"),
    )
    reader = ReplayFlowReader(data_dir=tmp_path)
    flows = list(reader.iter_flows())  # no day argument
    assert len(flows) == 1
    assert reader.last_read_stats.day == "monday"


def test_iter_flows_explicit_day_overrides_setting(tmp_path):
    """An explicit `day` argument always wins over the setting default."""
    _write_csv(
        tmp_path / "Monday-WorkingHours.pcap_ISCX.csv",
        [["10.0.0.1", "1001", "10.0.0.9", "80", "6", "7/7/2017 9:00", "1000", "1", "1", "BENIGN"]],
    )
    reader = ReplayFlowReader(data_dir=tmp_path)
    flows = list(reader.iter_flows(day="monday"))
    assert len(flows) == 1
    assert reader.last_read_stats.day == "monday"


def test_default_day_is_not_a_standalone_class_constant():
    """Ticket #5b review fix: DEFAULT_DAY must not exist as a locally-held
    constant that could drift from BACKEND_SETTINGS.replay_default_dataset_day
    — there must be exactly one place the default day is declared."""
    assert not hasattr(ReplayFlowReader, "DEFAULT_DAY")


# ---------------------------------------------------------------------------
# Real-dataset tests — skip cleanly if datasets/TrafficLabelling  absent
# ---------------------------------------------------------------------------


def test_available_days_lists_real_files():
    reader = _require_real_dataset()
    days = reader.available_days()
    assert "friday-morning" in days
    assert "monday" in days
    assert "warmup" in days  # alias for Monday


@pytest.mark.parametrize("day", ["monday", "friday-morning"])
def test_chronological_ordering_real_data(day):
    reader = _require_real_dataset()
    flows = list(reader.iter_flows(day=day, limit=20_000))
    assert len(flows) > 0
    timestamps = [f.ts for f in flows]
    assert timestamps == sorted(timestamps), (
        f"emitted ts values for day={day!r} are not non-decreasing — "
        "Invariant E (timestamp order) violated"
    )


def test_monday_is_capture_seconds():
    reader = _require_real_dataset()
    flows = list(reader.iter_flows(day="monday", limit=5_000))
    assert len(flows) > 0
    provenances = {f.timing_provenance for f in flows}
    assert provenances == {TIMING_PROVENANCE_CAPTURE_SECONDS}


def test_friday_morning_is_interpolated_minute_bucket():
    reader = _require_real_dataset()
    flows = list(reader.iter_flows(day="friday-morning", limit=5_000))
    assert len(flows) > 0
    provenances = {f.timing_provenance for f in flows}
    assert provenances == {TIMING_PROVENANCE_INTERPOLATED_MINUTE_BUCKET}


def test_real_ips_present():
    reader = _require_real_dataset()
    flows = list(reader.iter_flows(day="friday-morning", limit=5_000))
    source_ips = {f.source_ip for f in flows}
    # Must be real dotted-quad IPs, and genuinely diverse — this is the
    # entire point of this reader versus CICIDSAdapter's port heuristic.
    assert len(source_ips) > 10
    for ip in list(source_ips)[:5]:
        octets = ip.split(".")
        assert len(octets) == 4
        assert all(o.isdigit() and 0 <= int(o) <= 255 for o in octets)


def test_source_row_id_unique_within_real_day():
    reader = _require_real_dataset()
    flows = list(reader.iter_flows(day="friday-morning", limit=20_000))
    ids = [f.source_row_id for f in flows]
    assert len(ids) == len(set(ids))


def test_limit_returns_chronologically_first_n():
    reader = _require_real_dataset()
    full = list(reader.iter_flows(day="friday-morning", limit=2_000))
    limited = list(reader.iter_flows(day="friday-morning", limit=10))
    assert [f.source_row_id for f in limited] == [f.source_row_id for f in full[:10]]
