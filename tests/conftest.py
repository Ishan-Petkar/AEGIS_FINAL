"""
conftest.py — Shared pytest fixtures for the AEGIS test suite.
"""

import pytest
import sys
import os

# Ensure the src directory is on the path for all tests.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Ensure the repo root is on the path too, so `backend` (a top-level
# package, not under src/) is importable. `python -m pytest` adds the
# current working directory to sys.path automatically, which is why this
# was never caught by any local run all session -- CI invokes the bare
# `pytest` console script instead, which does not, and every backend test
# module failed collection with `ModuleNotFoundError: No module named
# 'backend'` as a result. Reproduced locally with the same bare-pytest
# invocation before this fix; confirmed fixed after.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Settings isolation — the suite must not read the developer's local .env
# ---------------------------------------------------------------------------
# `BackendSettings` (backend/config.py) declares `env_file=".env"`, so the
# gitignored repo-root .env becomes silent test input. That file is
# legitimate local config for manual console testing -- it currently holds
# `AEGIS_IPS_ENABLED=true` -- but it made
# test_ingest_ips.py::test_ips_disabled_by_default_no_rows_no_envelope_no_counters
# fail locally while passing in CI, because that test asserts the DEFAULT
# (disabled) IPS posture and the .env had quietly enabled it. A suite whose
# result depends on a gitignored file is not reproducible.
#
# Rebuilding the singleton with `_env_file=None` reads committed field
# defaults only. This must happen at conftest module scope, not in a
# fixture: `BACKEND_SETTINGS` is a frozen module-level singleton built at
# import time, so by the time any fixture runs the test modules have
# already bound the .env-derived object. Same reason the sys.path inserts
# above are module-level.
#
# Deliberately NOT clearing `AEGIS_*` from os.environ: real exported
# variables are explicit, per-run overrides rather than ambient config, and
# the suite depends on one -- tests/test_backend_models.py gates its
# live-Postgres tests on `AEGIS_TEST_LIVE_DB=1`, which a blanket clear
# would silently turn into a skip.
import backend.config as _backend_config  # noqa: E402

_backend_config.BACKEND_SETTINGS = _backend_config.BackendSettings(_env_file=None)


# ---------------------------------------------------------------------------
# Shared graph fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def simple_chain_graph():
    """A → B → C linear dependency chain.
    Returns a list of (src, tgt, edge_type, prob) tuples compatible with
    DEPENDENCY_GRAPH format (legacy tuple format still supported).
    """
    return [
        ("A", "B", "depends_on", 0.8),
        ("B", "C", "depends_on", 0.9),
    ]


@pytest.fixture
def diamond_graph():
    """A → B and A → C, both → D (two independent paths to D)."""
    return [
        ("A", "B", "depends_on", 0.9),
        ("A", "C", "depends_on", 0.9),
        ("B", "D", "depends_on", 0.8),
        ("C", "D", "depends_on", 0.8),
    ]


@pytest.fixture
def redundant_graph():
    """A → B and A → C, both → D via backed_up_by (D only fails if ALL fail)."""
    return [
        {"src": "A", "tgt": "B", "edge_type": "depends_on", "prob": 1.0},
        {"src": "A", "tgt": "C", "edge_type": "depends_on", "prob": 1.0},
        {"src": "B", "tgt": "D", "edge_type": "backed_up_by", "prob": 0.5},
        {"src": "C", "tgt": "D", "edge_type": "backed_up_by", "prob": 0.5},
    ]


@pytest.fixture
def disconnected_graph():
    """A → B in one component; C → D in another (no path from A to C/D)."""
    return [
        ("A", "B", "depends_on", 0.9),
        ("C", "D", "depends_on", 0.5),
    ]


@pytest.fixture
def self_loop_graph():
    """A → A (self-loop). Should not cause infinite simulation."""
    return [("A", "A", "depends_on", 0.9)]


@pytest.fixture
def simple_criticality_map():
    """A generic criticality map for common node names."""
    return {
        "A": 1.0,
        "B": 0.8,
        "C": 0.6,
        "D": 0.9,
        "Power_Substation": 1.0,
        "Traffic_Controller": 0.9,
        "Emergency_Route_System": 0.95,
        "City_Payment_Gateway": 0.95,
        "Bank_Partner_API": 0.85,
        "Social_Welfare_System": 0.90,
        "City_Grid": 1.0,
        "SCADA_Historian": 0.6,
        "Traffic_Cam_1": 0.2,
        "Traffic_Cam_2": 0.2,
        "Citizen_Portal": 0.4,
        "Municipal_Bond_Platform": 0.75,
    }
