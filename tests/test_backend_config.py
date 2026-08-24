"""
tests/test_backend_config.py — Phase 5 Ticket #1: backend/config.py.

Covers the configuration surface only. Deliberately does NOT touch a live
database — `backend/db_check.py` is the manual/live verification path, and
these tests must pass in CI, which has no Postgres available.
"""

import sys
from pathlib import Path

import pydantic
import pytest

# Env vars this module cares about, cleared before each test so a
# developer's shell environment (or a leftover .env) can never leak into
# an assertion about defaults or overrides.
_AEGIS_ENV_VARS = [
    "AEGIS_DB_HOST",
    "AEGIS_DB_PORT",
    "AEGIS_DB_USER",
    "AEGIS_DB_PASSWORD",
    "AEGIS_DB_NAME",
    "AEGIS_DB_POOL_SIZE",
    "AEGIS_DB_MAX_OVERFLOW",
    "AEGIS_DB_POOL_TIMEOUT_SEC",
    "AEGIS_DB_POOL_PRE_PING",
    "AEGIS_DATABASE_URL",
    "AEGIS_API_HOST",
    "AEGIS_API_PORT",
    "AEGIS_REPLAY_SPEED",
    "AEGIS_REPLAY_DEFAULT_DATASET_DAY",
    "AEGIS_WARMUP_DATASET_DAY",
    "AEGIS_MODEL_ARTIFACT_PATH",
]


@pytest.fixture(autouse=True)
def _clean_aegis_env(monkeypatch):
    """Strip AEGIS_* env vars so every test starts from a known baseline."""
    for var in _AEGIS_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# sys.path bridge (backend/__init__.py)
# ---------------------------------------------------------------------------


def test_backend_import_puts_src_on_syspath():
    import backend  # noqa: F401

    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    assert src_dir in sys.path


def test_bridge_actually_makes_an_src_module_importable():
    import backend  # noqa: F401

    # `settings` is a plain src/ module (src/settings.py) with no backend
    # awareness whatsoever - if this import works, the bridge is real, not
    # just a sys.path entry that happens to point somewhere.
    import settings as engine_settings

    assert hasattr(engine_settings, "SETTINGS")


# ---------------------------------------------------------------------------
# BACKEND_SETTINGS singleton
# ---------------------------------------------------------------------------


def test_backend_settings_importable_and_frozen():
    from backend.config import BACKEND_SETTINGS

    with pytest.raises(pydantic.ValidationError):
        BACKEND_SETTINGS.db_host = "mutated"


def test_backend_settings_is_the_expected_type():
    from backend.config import BACKEND_SETTINGS, BackendSettings

    assert isinstance(BACKEND_SETTINGS, BackendSettings)


# ---------------------------------------------------------------------------
# database_url composition
# ---------------------------------------------------------------------------


def test_database_url_composes_from_components():
    from backend.config import BackendSettings

    s = BackendSettings(
        db_host="dbhost",
        db_port=6543,
        db_user="someuser",
        db_password="somepass",
        db_name="somedb",
    )
    assert s.database_url == "postgresql+psycopg://someuser:somepass@dbhost:6543/somedb"
    assert s.database_url.startswith("postgresql+psycopg://")


def test_database_url_override_wins(monkeypatch):
    monkeypatch.setenv("AEGIS_DATABASE_URL", "postgresql+psycopg://x:y@otherhost:1/otherdb")
    from backend.config import BackendSettings

    # Component fields are also set (to their defaults) but must be ignored
    # once the override is present.
    s = BackendSettings(db_host="ignored-host")
    assert s.database_url == "postgresql+psycopg://x:y@otherhost:1/otherdb"


def test_database_url_override_absent_falls_back_to_components():
    from backend.config import BackendSettings

    s = BackendSettings(db_host="fallback-host")
    assert "fallback-host" in s.database_url
    assert s.db_url_override is None


# ---------------------------------------------------------------------------
# Defaults match the provisioned environment
# ---------------------------------------------------------------------------


def test_defaults_match_provisioned_environment():
    from backend.config import BackendSettings

    s = BackendSettings()
    assert s.db_host == "127.0.0.1"
    assert s.db_port == 5432
    assert s.db_user == "aegis"
    assert s.db_password == "aegis"
    assert s.db_name == "aegis"
    assert s.database_url == "postgresql+psycopg://aegis:aegis@127.0.0.1:5432/aegis"
    assert s.db_pool_pre_ping is True
    assert s.api_port == 8000
    assert s.replay_speed == 20.0
    assert s.model_artifact_path == Path("artifacts/streaming_scorer.joblib")


# ---------------------------------------------------------------------------
# Ticket #5b review fix: replay-day config divergence
# ---------------------------------------------------------------------------


def test_replay_default_dataset_day_is_friday_morning():
    from backend.config import BackendSettings

    s = BackendSettings()
    assert s.replay_default_dataset_day == "friday-morning"


def test_warmup_dataset_day_is_monday():
    from backend.config import BackendSettings

    s = BackendSettings()
    assert s.warmup_dataset_day == "monday"


def test_replay_default_dataset_day_overridable_via_env(monkeypatch):
    monkeypatch.setenv("AEGIS_REPLAY_DEFAULT_DATASET_DAY", "monday")
    from backend.config import BackendSettings

    s = BackendSettings()
    assert s.replay_default_dataset_day == "monday"


def test_warmup_dataset_day_overridable_via_env(monkeypatch):
    monkeypatch.setenv("AEGIS_WARMUP_DATASET_DAY", "friday-afternoon-ddos")
    from backend.config import BackendSettings

    s = BackendSettings()
    assert s.warmup_dataset_day == "friday-afternoon-ddos"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_speed", [0, -1, -20.0])
def test_replay_speed_must_be_positive(bad_speed):
    from backend.config import BackendSettings

    with pytest.raises(pydantic.ValidationError):
        BackendSettings(replay_speed=bad_speed)


def test_db_port_out_of_range_rejected():
    from backend.config import BackendSettings

    with pytest.raises(pydantic.ValidationError):
        BackendSettings(db_port=70_000)

    with pytest.raises(pydantic.ValidationError):
        BackendSettings(db_port=0)


def test_pool_timeout_must_be_positive():
    from backend.config import BackendSettings

    with pytest.raises(pydantic.ValidationError):
        BackendSettings(db_pool_timeout_sec=0)


# ---------------------------------------------------------------------------
# HIGH-1: database_url percent-encodes credentials
# ---------------------------------------------------------------------------


def test_database_url_encodes_special_characters_in_credentials():
    from sqlalchemy.engine import make_url

    from backend.config import BackendSettings

    s = BackendSettings(
        db_host="dbhost",
        db_port=6543,
        db_user="us:er@name",
        db_password="p@ss:w/rd#with?chars",
        db_name="somedb",
    )
    # The raw URL must not contain the literal unescaped special characters
    # from the password in a position that would confuse URL parsing - the
    # authoritative check is that SQLAlchemy's own parser round-trips it.
    parsed = make_url(s.database_url)
    assert parsed.host == "dbhost"
    assert parsed.port == 6543
    assert parsed.username == "us:er@name"
    assert parsed.password == "p@ss:w/rd#with?chars"
    assert parsed.database == "somedb"
    assert parsed.drivername == "postgresql+psycopg"


def test_database_url_override_not_encoded(monkeypatch):
    # db_url_override is supplied pre-formed by the operator and must pass
    # through byte-for-byte, even if it contains characters that would
    # otherwise be percent-encoded when composing from components.
    raw = "postgresql+psycopg://x:y%2Fz@otherhost:1/otherdb?sslmode=require"
    monkeypatch.setenv("AEGIS_DATABASE_URL", raw)
    from backend.config import BackendSettings

    s = BackendSettings()
    assert s.database_url == raw


# ---------------------------------------------------------------------------
# HIGH-2: api_host defaults to loopback only
# ---------------------------------------------------------------------------


def test_api_host_defaults_to_loopback():
    from backend.config import BackendSettings

    s = BackendSettings()
    assert s.api_host == "127.0.0.1"


# ---------------------------------------------------------------------------
# MEDIUM-1: model_artifact_path_resolved is CWD-independent
# ---------------------------------------------------------------------------


def test_model_artifact_path_resolved_relative_default_uses_repo_root(monkeypatch, tmp_path):
    from backend.config import BackendSettings

    repo_root = Path(__file__).resolve().parent.parent

    monkeypatch.chdir(tmp_path)
    s = BackendSettings()
    resolved = s.model_artifact_path_resolved

    assert resolved.is_absolute()
    assert resolved == repo_root / "artifacts" / "streaming_scorer.joblib"


def test_model_artifact_path_resolved_absolute_override_unchanged(monkeypatch, tmp_path):
    from backend.config import BackendSettings

    absolute = tmp_path / "custom" / "scorer.joblib"
    monkeypatch.chdir(tmp_path)
    s = BackendSettings(model_artifact_path=absolute)

    assert s.model_artifact_path_resolved == absolute
