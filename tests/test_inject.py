"""
tests/test_inject.py — Phase 5 Ticket #13: `backend/inject.py` (the
scenario registry backing `POST /api/inject`).

Pure unit tests against `backend.inject` directly — no FastAPI, no DB, no
real dataset CSV (a fake `ReplayFlowReader` substitute stands in, mirroring
tests/test_api.py's `_FakeReader` / tests/test_replay_engine.py's own).
Route-level wiring (503/409/422, the real `ReplayEngine.inject()` call) is
covered separately in tests/test_api.py.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.inject import (
    SCENARIOS,
    InjectionError,
    build_injection_flows,
    clear_pool_cache,
    resolvable_target_assets,
)
from backend.replay_reader import ReplayFlow

BASE_TS = datetime(2017, 7, 7, 9, 0, 0, tzinfo=timezone.utc)


def _flow(label: str, row_id: str, *, ts=BASE_TS) -> ReplayFlow:
    return ReplayFlow(
        ts=ts,
        source_ip="192.168.10.5",
        source_port=4444,
        destination_ip="192.168.10.50",
        destination_port=80,
        protocol="TCP",
        duration_sec=12.5,
        packets=7,
        bytes=4321,
        label=label,
        is_attack=(label != "BENIGN"),
        timing_provenance="capture_seconds",
        source_row_id=row_id,
        source_dataset="CIC-IDS2017-TrafficLabelling",
    )


class _CountingFakeReader:
    """Records every `iter_flows(day=...)` call so tests can assert the
    per-(day,label) cache actually prevents a second read."""

    def __init__(self, flows_by_day: dict[str, list[ReplayFlow]]) -> None:
        self._flows_by_day = flows_by_day
        self.calls: list[str | None] = []

    def iter_flows(self, day=None, limit=None):
        self.calls.append(day)
        flows = self._flows_by_day.get(day, [])
        yield from flows


@pytest.fixture(autouse=True)
def _fresh_pool_cache():
    """The module-level pool cache is process-global; reset it around
    every test so tests don't leak state into each other."""
    clear_pool_cache()
    yield
    clear_pool_cache()


# ---------------------------------------------------------------------------
# Scenario registry
# ---------------------------------------------------------------------------


def test_scenario_registry_has_the_required_minimum():
    assert {"bot_c2", "ddos", "port_scan", "honeytoken"} <= set(SCENARIOS)


def test_scenario_registry_uses_real_days_and_labels_never_fabricated():
    assert SCENARIOS["bot_c2"].day == "friday-morning"
    assert SCENARIOS["bot_c2"].label == "Bot"
    assert SCENARIOS["ddos"].day == "friday-afternoon-ddos"
    assert SCENARIOS["ddos"].label == "DDoS"
    assert SCENARIOS["port_scan"].day == "friday-afternoon-portscan"
    assert SCENARIOS["port_scan"].label == "PortScan"


def test_only_honeytoken_scenario_sets_the_honeytoken_flag():
    assert SCENARIOS["honeytoken"].is_honeytoken is True
    for name, spec in SCENARIOS.items():
        if name != "honeytoken":
            assert spec.is_honeytoken is False


def test_honeytoken_scenario_reuses_bot_c2s_real_day_and_label():
    """D13-2: the honeytoken scenario is the SAME real Bot/C2 telemetry —
    only the deception-layer flag differs."""
    assert SCENARIOS["honeytoken"].day == SCENARIOS["bot_c2"].day
    assert SCENARIOS["honeytoken"].label == SCENARIOS["bot_c2"].label


# ---------------------------------------------------------------------------
# resolvable_target_assets — curated-only, real static IPs
# ---------------------------------------------------------------------------


def test_resolvable_target_assets_are_exactly_the_curated_smart_city_assets():
    targets = resolvable_target_assets()
    assert targets["City_Payment_Gateway"] == "10.0.1.20"
    assert targets["Traffic_Controller"] == "10.0.1.12"
    # Gateway/synthesized nodes have no real static IP and must be absent.
    assert "Gateway_L4" not in targets
    assert "City_Grid" not in targets


# ---------------------------------------------------------------------------
# build_injection_flows — validation
# ---------------------------------------------------------------------------


def test_unknown_scenario_raises_injection_error():
    with pytest.raises(InjectionError, match="unknown scenario"):
        build_injection_flows("not_a_real_scenario", "City_Payment_Gateway", 10)


def test_unresolvable_target_asset_raises_injection_error():
    reader = _CountingFakeReader({"friday-morning": [_flow("Bot", "f:1")]})
    with pytest.raises(InjectionError, match="not a curated asset"):
        build_injection_flows("bot_c2", "Gateway_L4", 10, reader=reader)


def test_unknown_target_asset_string_raises_injection_error():
    reader = _CountingFakeReader({"friday-morning": [_flow("Bot", "f:1")]})
    with pytest.raises(InjectionError):
        build_injection_flows("bot_c2", "Not_A_Real_Asset", 10, reader=reader)


# ---------------------------------------------------------------------------
# build_injection_flows — real-behaviour, re-targeted-identity contract
# ---------------------------------------------------------------------------


def test_retargets_source_ip_preserves_every_other_real_characteristic():
    real_flow = _flow("Bot", "Friday-WorkingHours-Morning.pcap_ISCX.csv:42")
    reader = _CountingFakeReader({"friday-morning": [real_flow]})

    [injected] = build_injection_flows(
        "bot_c2", "City_Payment_Gateway", 1, reader=reader
    )

    # Re-targeted: source_ip now the curated asset's real static IP.
    assert injected.source_ip == "10.0.1.20"
    # Everything else about the REAL captured flow is untouched.
    assert injected.destination_ip == real_flow.destination_ip
    assert injected.source_port == real_flow.source_port
    assert injected.destination_port == real_flow.destination_port
    assert injected.protocol == real_flow.protocol
    assert injected.duration_sec == real_flow.duration_sec
    assert injected.packets == real_flow.packets
    assert injected.bytes == real_flow.bytes
    assert injected.label == "Bot"
    assert injected.ts == real_flow.ts
    assert injected.is_attack == real_flow.is_attack


def test_source_row_id_is_fresh_and_unique_never_the_original():
    real_flow = _flow("Bot", "Friday-WorkingHours-Morning.pcap_ISCX.csv:42")
    reader = _CountingFakeReader({"friday-morning": [real_flow, real_flow]})

    injected = build_injection_flows(
        "bot_c2", "City_Payment_Gateway", 2, reader=reader
    )
    ids = [f.source_row_id for f in injected]
    assert len(set(ids)) == 2  # unique even from the identical source row
    assert all(rid.startswith("injected:bot_c2:") for rid in ids)
    assert real_flow.source_row_id not in ids


def test_only_honeytoken_scenario_flags_the_flows():
    real_flow = _flow("Bot", "f:1")
    reader = _CountingFakeReader({"friday-morning": [real_flow]})

    [normal] = build_injection_flows(
        "bot_c2", "City_Payment_Gateway", 1, reader=reader
    )
    clear_pool_cache()
    [honeytoken] = build_injection_flows(
        "honeytoken", "City_Payment_Gateway", 1, reader=reader
    )

    assert normal.is_honeytoken_use is False
    assert honeytoken.is_honeytoken_use is True
    # The underlying telemetry is identical real Bot traffic either way.
    assert normal.label == honeytoken.label == "Bot"
    assert normal.bytes == honeytoken.bytes


def test_label_filter_excludes_non_matching_real_rows():
    reader = _CountingFakeReader(
        {
            "friday-morning": [
                _flow("BENIGN", "f:1"),
                _flow("Bot", "f:2"),
                _flow("BENIGN", "f:3"),
            ]
        }
    )
    injected = build_injection_flows(
        "bot_c2", "City_Payment_Gateway", 10, reader=reader
    )
    assert len(injected) == 1
    assert injected[0].label == "Bot"


def test_count_clamps_to_available_pool_never_fabricates_or_cycles():
    reader = _CountingFakeReader(
        {"friday-morning": [_flow("Bot", f"f:{i}") for i in range(3)]}
    )
    injected = build_injection_flows(
        "bot_c2", "City_Payment_Gateway", 100, reader=reader
    )
    assert len(injected) == 3


# ---------------------------------------------------------------------------
# Caching — no re-read of the (75-280MB) CSV per call
# ---------------------------------------------------------------------------


def test_pool_is_cached_across_calls_for_the_same_day_and_label():
    reader = _CountingFakeReader(
        {"friday-morning": [_flow("Bot", f"f:{i}") for i in range(5)]}
    )
    build_injection_flows("bot_c2", "City_Payment_Gateway", 2, reader=reader)
    build_injection_flows("bot_c2", "Traffic_Controller", 2, reader=reader)
    build_injection_flows("bot_c2", "City_Payment_Gateway", 1, reader=reader)
    assert reader.calls == ["friday-morning"]  # read exactly once


def test_pool_cache_is_shared_between_bot_c2_and_honeytoken():
    """Both scenarios share (day='friday-morning', label='Bot') — the
    cache key is (day, label), not scenario name, so they must not
    trigger two separate reads."""
    reader = _CountingFakeReader(
        {"friday-morning": [_flow("Bot", f"f:{i}") for i in range(5)]}
    )
    build_injection_flows("bot_c2", "City_Payment_Gateway", 2, reader=reader)
    build_injection_flows("honeytoken", "City_Payment_Gateway", 2, reader=reader)
    assert reader.calls == ["friday-morning"]


def test_different_days_are_cached_independently():
    reader = _CountingFakeReader(
        {
            "friday-morning": [_flow("Bot", "f:1")],
            "friday-afternoon-ddos": [_flow("DDoS", "f:2")],
        }
    )
    build_injection_flows("bot_c2", "City_Payment_Gateway", 1, reader=reader)
    build_injection_flows("ddos", "City_Payment_Gateway", 1, reader=reader)
    assert sorted(reader.calls) == ["friday-afternoon-ddos", "friday-morning"]
