"""
backend/replay_reader.py — Phase 5 Ticket #5b: `ReplayFlowReader`.

Reads `datasets/TrafficLabelling ` (note the trailing space in the
directory name — a quirk of the upstream CIC-IDS2017 distribution), the
85-column variant of the CIC-IDS2017 capture that carries real `Source
IP` / `Destination IP` / `Timestamp` columns, unlike `datasets/
MachineLearningCVE/` (79 columns, no IPs, no per-row timestamp) that
`datasets.cic_ids_adapter.CICIDSAdapter` reads for the Phase 1-3 batch
benchmark.

This module is purely additive. It does NOT modify, import from, or
duplicate logic in `cic_ids_adapter.py` (frozen, Invariant A —
docs/PHASE5_RECON.md section D) and does NOT touch anything under `src/`.
It exists to unblock Phase 5 Ticket #6 (the chronological replay engine),
which needs real IPs and a real, sortable event-time ordering that the
Phase 1-3 adapter cannot provide (see docs/PHASE5_RECON.md FINDING 1).

Scope (P5-9): this reader yields raw `source_ip` / `destination_ip`
identifiers verbatim. It does NOT call `AssetRegistry.resolve()` or do
any `/24` clustering — that belongs to Tickets #7 / #11. Keeping this
reader single-purpose keeps it independently testable and avoids
duplicating resolution logic that already exists in
`datasets/asset_registry.py` (CLAUDE.md: "no duplicate definitions").

Key design decisions (see docs/PHASE5_RECON.md section 0.5 for the full
investigation and measured evidence backing each one)
----------------------------------------------------------------------
Fact 1 — 12-hour clock, no AM/PM marker. No capture file reports an hour
above 12; afternoon captures read e.g. "7/7/2017 1:00". Corrected
deterministically: hours 1-7 -> +12 (PM), hours 8-12 -> unchanged (AM).
Justified by these being known working-hours captures (~08:00-17:00)
with morning/afternoon declared in the filenames.

Fact 2 — file order is NOT chronological (CICFlowMeter emits flows in
completion order, not start order; measured ~24-39% pairwise inversions
in the two files audited). P5-7: this reader therefore ALWAYS sorts by
corrected timestamp before yielding — never relies on file order for
correctness, even for the two files (Monday, Friday-Morning) that happen
to already be monotonic.

Fact 3 — timestamp precision differs per file. Monday has genuine
second-resolution capture timestamps; every other file only carries
minute resolution. This is detected per-row from the parsed timestamp
string (does the time field carry a third, seconds, component?) rather
than hardcoded by filename, and tagged onto every flow as
`timing_provenance` (values imported from `backend.models`, not
restated — CLAUDE.md "no duplicate definitions").

P5-7 — stable sort. Python's `list.sort()` is stable (Timsort), so
sorting by `ts` alone preserves each row's original within-file order
for ties. This matters most for minute-granularity files: within one
minute bucket, the original file order is the *only* ordering signal the
source data offers, and preserving it (rather than inventing a
tie-break) is the more defensible choice. Sub-minute placement of a
minute bucket's events (spreading them across `60/speed` seconds during
actual replay pacing) is a Ticket #6 concern, not this reader's.

P5-8 — default day is `friday-morning`, not Monday. Monday is 0.0%
attack traffic (see docs/PHASE5_RECON.md Fact 4) — a legitimate,
genuinely-timed all-benign baseline, but a landing stream with zero real
anomalies undercuts a live demo (Invariant E). It remains available as
the `warmup` day (StreamingScorer fitting, Ticket #5) via `WARMUP_DAY`.
Friday-Afternoon files are 55-57% attack traffic, unrealistically
hostile for a landing stream. Friday-Morning (1.0% Bot/C2, monotonic,
191k rows) is mostly benign with occasional genuine attacks -- the
credible default for an operations console. This default is declared
exactly once, as `BackendSettings.replay_default_dataset_day` in
`backend/config.py` — `iter_flows()` reads that setting (not a local
constant) so the reader and the rest of the backend can never disagree
about which day is the landing stream.

Memory — this module never loads a capture file with `pandas.read_csv`.
Wednesday-workingHours is 692k rows / 268MB+ and the naive pandas path
costs roughly 2GB RAM for a file this wide (85 columns). Instead it
streams the file with the stdlib `csv` module, extracts only the ~10
fields this reader needs into compact `ReplayFlow` objects, and sorts
that list. Full read + sort is still O(n log n) in a list of small
objects, not a wide DataFrame.

`limit` semantics — chronological ordering requires seeing every row
before any row can be yielded (there is no way to know the first N
chronological rows without visiting the whole file), so `limit` is
applied AFTER the full read and stable sort: the result is the
chronologically-first N flows for the day, never a chronologically
arbitrary window of file-order rows.

Encoding — `TrafficLabelling ` CSVs are `latin-1`, not UTF-8 (Web Attack
labels contain mojibake under UTF-8 decoding; UTF-8 raises outright on
some byte sequences in this capture).
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

# Reused, not redefined (CLAUDE.md "no duplicate definitions").
# `backend/__init__.py` puts `src/` on sys.path so this resolves.
from datasets.loader import DatasetNotAvailable  # noqa: F401 (re-exported for callers)

from backend.config import BACKEND_SETTINGS
from backend.models import (
    TIMING_PROVENANCE_CAPTURE_SECONDS,
    TIMING_PROVENANCE_INTERPOLATED_MINUTE_BUCKET,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Provenance tag for events sourced through this reader. Deliberately
#: distinct from `datasets.schema.PROVENANCE_CIC_IDS2017` (used by the
#: frozen Phase 1-3 `CICIDSAdapter`) so downstream consumers can never
#: conflate a Phase 3 batch-benchmark row with a Phase 5 replay row that
#: happens to originate from the same underlying capture.
SOURCE_DATASET = "CIC-IDS2017-TrafficLabelling"

#: Numeric `Protocol` column -> readable name. IANA-assigned numbers;
#: anything else falls back to a numeric label rather than raising.
_PROTOCOL_NAMES: dict[int, str] = {1: "ICMP", 6: "TCP", 17: "UDP"}

#: Required source columns, after header-name stripping. Values are the
#: `datasets/TrafficLabelling ` header names (already whitespace-normalised
#: by `_normalise_header`).
_REQUIRED_COLUMNS = (
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
)

#: day key -> filename under `datasets/TrafficLabelling `. Verified against
#: the files actually present on disk (docs/PHASE5_RECON.md section 0.5).
_DAY_FILES: dict[str, str] = {
    "monday": "Monday-WorkingHours.pcap_ISCX.csv",
    "tuesday": "Tuesday-WorkingHours.pcap_ISCX.csv",
    "wednesday": "Wednesday-workingHours.pcap_ISCX.csv",
    "thursday-morning-webattacks": "Thursday-WorkingHours-Morning-WebAttacks.pcap_ISCX.csv",
    "thursday-afternoon-infiltration": "Thursday-WorkingHours-Afternoon-Infilteration.pcap_ISCX.csv",
    "friday-morning": "Friday-WorkingHours-Morning.pcap_ISCX.csv",
    "friday-afternoon-portscan": "Friday-WorkingHours-Afternoon-PortScan.pcap_ISCX.csv",
    "friday-afternoon-ddos": "Friday-WorkingHours-Afternoon-DDos.pcap_ISCX.csv",
    # P5-8: warmup is an alias for Monday (all-benign, genuine-second
    # timing), used by Ticket #5 to fit StreamingScorer, never as the
    # landing/default stream.
    "warmup": "Monday-WorkingHours.pcap_ISCX.csv",
}

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DATA_DIR = _PROJECT_ROOT / "datasets" / "TrafficLabelling "


# ---------------------------------------------------------------------------
# ReplayFlow
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayFlow:
    """One normalised CIC-IDS2017 TrafficLabelling flow, real IP/timestamp.

    Deliberately NOT the `CanonicalEvent` schema (`datasets/schema.py`):
    this reader is a raw source of identifiers (P5-9), not an ingestion
    adapter. Asset resolution into canonical form happens downstream in
    Ticket #7.
    """

    ts: datetime
    source_ip: str
    source_port: int
    destination_ip: str
    destination_port: int
    protocol: str
    duration_sec: float
    packets: int
    bytes: int
    label: str
    is_attack: bool
    timing_provenance: str
    #: Stable identifier for the D4 dedupe constraint (backend/models.py,
    #: `uq_events_replay_session_source_row`): "<filename>:<line_number>",
    #: where line_number is the row's position in the ORIGINAL file (1 =
    #: header), independent of the chronological sort applied afterwards.
    source_row_id: str
    source_dataset: str


@dataclass(frozen=True)
class ReadStats:
    """Row-level outcome counts for one `iter_flows()` call.

    Exposed via `ReplayFlowReader.last_read_stats` so Ticket #6 can log
    data quality (skipped rows) instead of silently dropping traffic.
    """

    day: str
    source_file: str
    rows_seen: int
    rows_emitted: int
    skipped_short_or_ragged: int
    skipped_repeated_header: int
    skipped_unparseable_fields: int
    skipped_bad_timestamp: int


# ---------------------------------------------------------------------------
# Internal parsing helpers
# ---------------------------------------------------------------------------


def _normalise_header(raw_header: list[str]) -> list[str]:
    return [h.strip() for h in raw_header]


def _protocol_name(proto_num: int) -> str:
    return _PROTOCOL_NAMES.get(proto_num, f"PROTO_{proto_num}")


def _parse_timestamp(raw: str) -> Optional[tuple[datetime, str]]:
    """Parse a CIC-IDS2017 `Timestamp` field with the Fact-1 AM/PM fix.

    Format is `D/M/YYYY H:MM` or `D/M/YYYY H:MM:SS` (neither the date nor
    the hour is zero-padded consistently across files). No file reports
    an hour above 12 (Fact 1/Fact 3): hours 1-7 are corrected to 13-19
    (PM), hours 8-12 are left unchanged (AM). Returns `None` for any
    string that does not fit this shape, so the caller can skip-and-count
    rather than crash.

    Returns
    -------
    (corrected UTC datetime, timing_provenance) or None if unparseable.
    timing_provenance is `TIMING_PROVENANCE_CAPTURE_SECONDS` when the raw
    string carries a seconds component, else
    `TIMING_PROVENANCE_INTERPOLATED_MINUTE_BUCKET` (Fact 3) — detected
    per-row from the string shape, not hardcoded by filename.
    """
    raw = raw.strip()
    if not raw:
        return None
    parts = raw.split(" ")
    if len(parts) != 2:
        return None
    date_str, time_str = parts
    date_parts = date_str.split("/")
    if len(date_parts) != 3:
        return None
    time_parts = time_str.split(":")
    if len(time_parts) not in (2, 3):
        return None
    try:
        day, month, year = (int(p) for p in date_parts)
        hour = int(time_parts[0])
        minute = int(time_parts[1])
        second = int(time_parts[2]) if len(time_parts) == 3 else 0
    except ValueError:
        return None

    if not (1 <= hour <= 12):
        # Outside the documented 12-hour-clock range (Fact 1) — not a
        # shape we can trust to correct; skip rather than guess.
        return None

    provenance = (
        TIMING_PROVENANCE_CAPTURE_SECONDS
        if len(time_parts) == 3
        else TIMING_PROVENANCE_INTERPOLATED_MINUTE_BUCKET
    )

    # Fact 1 correction: hours 1-7 -> PM (+12), hours 8-12 -> AM (as-is).
    if 1 <= hour <= 7:
        hour += 12

    try:
        ts = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    except ValueError:
        return None
    return ts, provenance


def _parse_row(
    row: list[str],
    idx: dict[str, int],
    filename: str,
    line_no: int,
) -> Optional[ReplayFlow]:
    """Parse one data row into a `ReplayFlow`, or `None` if it must be skipped.

    Raises nothing — every failure mode (missing field, NaN, ragged row,
    unparseable number, unparseable timestamp) resolves to `None` so the
    caller can count it and move on (robustness requirement).
    """
    try:
        src_ip = row[idx["Source IP"]].strip()
        dst_ip = row[idx["Destination IP"]].strip()
        label = row[idx["Label"]].strip()
        ts_raw = row[idx["Timestamp"]]
    except IndexError:
        return None

    if not src_ip or not dst_ip or not label:
        return None

    try:
        src_port = int(float(row[idx["Source Port"]]))
        dst_port = int(float(row[idx["Destination Port"]]))
        proto_num = int(float(row[idx["Protocol"]]))
        dur_us = float(row[idx["Flow Duration"]])
        pkts_f = float(row[idx["Total Fwd Packets"]])
        bytes_f = float(row[idx["Total Length of Fwd Packets"]])
    except (ValueError, IndexError):
        return None

    if any(math.isnan(v) or math.isinf(v) for v in (dur_us, pkts_f, bytes_f)):
        return None

    parsed_ts = _parse_timestamp(ts_raw)
    if parsed_ts is None:
        return None
    ts, provenance = parsed_ts

    return ReplayFlow(
        ts=ts,
        source_ip=src_ip,
        source_port=src_port,
        destination_ip=dst_ip,
        destination_port=dst_port,
        protocol=_protocol_name(proto_num),
        # CIC `Flow Duration` is microseconds (matches the semantic the
        # frozen CICIDSAdapter uses at cic_ids_adapter.py: `/ 1_000_000`).
        duration_sec=max(dur_us / 1_000_000.0, 0.0),
        packets=int(pkts_f),
        bytes=int(bytes_f),
        label=label,
        is_attack=label.strip().upper() != "BENIGN",
        timing_provenance=provenance,
        source_row_id=f"{filename}:{line_no}",
        source_dataset=SOURCE_DATASET,
    )


# ---------------------------------------------------------------------------
# ReplayFlowReader
# ---------------------------------------------------------------------------


class ReplayFlowReader:
    """Reads `datasets/TrafficLabelling ` into chronologically-ordered
    `ReplayFlow` objects.

    Parameters
    ----------
    data_dir :
        Path to the `TrafficLabelling ` directory (note trailing space).
        `None` (default) resolves to `<repo root>/datasets/TrafficLabelling `.

    See the module docstring for the timing-correction, sort-stability,
    default-day, and memory design decisions this class implements.
    """

    #: P5-8: Monday, accessed via this alias, is the warmup-only day.
    WARMUP_DAY = "warmup"

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else _DEFAULT_DATA_DIR
        self._last_stats: Optional[ReadStats] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def available_days(self) -> list[str]:
        """Day keys resolvable from files actually present on disk."""
        if not self.data_dir.exists():
            return []
        return sorted(
            day for day, fname in _DAY_FILES.items() if (self.data_dir / fname).exists()
        )

    def iter_flows(
        self,
        day: str | None = None,
        limit: int | None = None,
    ) -> Iterator[ReplayFlow]:
        """Yield `ReplayFlow`s for `day` in non-decreasing `ts` order.

        `day` defaults to `BACKEND_SETTINGS.replay_default_dataset_day`
        (P5-8: `"friday-morning"`) when `None`, per CLAUDE.md's
        optional-override signature convention: the setting — not a
        class constant — is the single source of truth for the default
        day, so this reader genuinely defers to `backend/config.py`
        rather than holding its own copy (resolved fresh on every call,
        so overriding the setting changes subsequent reads).

        `limit`, if given, returns the chronologically-first `limit`
        flows (see the module docstring's "limit semantics" note) — the
        full day is read and sorted first; this method cannot know the
        first-N-chronological rows without seeing every row.
        """
        resolved_day = (
            day if day is not None else BACKEND_SETTINGS.replay_default_dataset_day
        )
        path = self._resolve_path(resolved_day)
        flows, stats = self._read_day(path, resolved_day)
        # P5-7: stable sort by ts only. Python's sort is stable, so rows
        # sharing a timestamp keep their original file-order relative
        # position -- the only ordering signal minute-granularity files
        # can offer within a bucket.
        flows.sort(key=lambda f: f.ts)
        self._last_stats = stats
        if limit is not None:
            flows = flows[:limit]
        yield from flows

    @property
    def last_read_stats(self) -> Optional[ReadStats]:
        """`ReadStats` for the most recently completed `iter_flows()` call.

        `None` until `iter_flows()` has been consumed at least once. Set
        right after the full read + sort, before the first row is
        yielded, so it is populated deterministically once the generator
        starts producing rows.
        """
        return self._last_stats

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_path(self, day: str) -> Path:
        if not self.data_dir.exists():
            raise DatasetNotAvailable(
                f"CIC-IDS2017 TrafficLabelling directory not found at "
                f"{self.data_dir}. Populate datasets/'TrafficLabelling '/ "
                "(note the trailing space) per docs/DATASETS.md."
            )
        key = day.strip().lower()
        if key not in _DAY_FILES:
            available = ", ".join(sorted(_DAY_FILES))
            raise DatasetNotAvailable(
                f"Unknown replay day '{day}'. Available day keys: {available}."
            )
        path = self.data_dir / _DAY_FILES[key]
        if not path.exists():
            raise DatasetNotAvailable(
                f"Replay file for day '{day}' not found at {path}. "
                "Populate datasets/'TrafficLabelling '/ per docs/DATASETS.md."
            )
        return path

    def _read_day(self, path: Path, day: str) -> tuple[list[ReplayFlow], ReadStats]:
        """Stream-parse one CSV file. Never loads it with pandas (memory)."""
        flows: list[ReplayFlow] = []
        rows_seen = 0
        skipped_short = 0
        skipped_header = 0
        skipped_bad = 0
        skipped_ts = 0

        with open(path, encoding="latin-1", newline="") as f:
            reader = csv.reader(f)
            try:
                header = _normalise_header(next(reader))
            except StopIteration:
                header = []
            idx = {name: i for i, name in enumerate(header)}
            missing = [c for c in _REQUIRED_COLUMNS if c not in idx]
            if missing:
                raise DatasetNotAvailable(
                    f"{path} is missing required columns {missing}; expected the "
                    "85-column TrafficLabelling variant (Source IP / Destination IP / "
                    "Timestamp present), not the 79-column MachineLearningCVE variant."
                )

            line_no = 1  # header consumed as line 1
            for row in reader:
                line_no += 1
                rows_seen += 1

                if len(row) != len(header):
                    skipped_short += 1
                    continue

                # Repeated header row mid-file (a known CIC-IDS2017 CSV
                # concatenation artifact).
                if row[idx["Label"]].strip() == "Label" or row[idx["Source IP"]].strip() == "Source IP":
                    skipped_header += 1
                    continue

                flow = _parse_row(row, idx, path.name, line_no)
                if flow is None:
                    # Distinguish a bad timestamp from other malformed
                    # fields for the stats breakdown, without re-parsing
                    # everything twice: cheap enough to re-check just the
                    # timestamp field for the more specific count.
                    ts_field = row[idx["Timestamp"]] if idx["Timestamp"] < len(row) else ""
                    if _parse_timestamp(ts_field) is None:
                        skipped_ts += 1
                    else:
                        skipped_bad += 1
                    continue

                flows.append(flow)

        stats = ReadStats(
            day=day,
            source_file=path.name,
            rows_seen=rows_seen,
            rows_emitted=len(flows),
            skipped_short_or_ragged=skipped_short,
            skipped_repeated_header=skipped_header,
            skipped_unparseable_fields=skipped_bad,
            skipped_bad_timestamp=skipped_ts,
        )
        return flows, stats
