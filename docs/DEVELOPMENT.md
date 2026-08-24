# Development Guide

## Adding a New Dataset

There is no `BaseAdapter` class to inherit from — adapters are plain classes
that expose a `load(limit: Optional[int] = None) -> CanonicalBatch` method.
`src/datasets/swat_adapter.py` is the simplest real example to copy:

1. Create `src/datasets/<name>_adapter.py` with a class that takes a data
   directory and an `AssetRegistry` in `__init__`, and implements `load()`.
   Inside `load()`, build a `pd.DataFrame` with every column in
   `CANONICAL_COLUMNS` (`src/datasets/schema.py`) populated — including the
   v2.0 fields `signal_type`, `observed_at`, and `purdue_level` — then attach
   any extra dataset-specific feature columns (e.g. SWaT's `FIT101`,
   `LIT101`), and return `CanonicalBatch.from_dataframe(canonical)`.
2. Ensure every value conforms to `CanonicalEvent`'s validators in
   `src/datasets/schema.py` (e.g. `signal_type` must be one of
   `VALID_SIGNAL_TYPES`, `purdue_level` must be 0–5 or `None`).
3. Register the new adapter in `src/datasets/loader.py`:
   - Add an entry to the `SUPPORTED_DATASETS` dict (name → description) —
     this is the actual registry; `available_datasets()` is a derived
     reporting function that just checks whether each registered dataset's
     files exist on disk, it is not where you register anything.
   - Add a `name == "<name>"` branch to `load_dataset()`'s dispatch.
   - Add a private `_load_<name>()` helper following the existing
     `_load_swat()` / `_load_cic_ids2017()` pattern: raise
     `DatasetNotAvailable` if the source files aren't present, otherwise
     construct the adapter and call `.load(limit=limit)`.
4. Add tests, and skip (don't fail) when the dataset isn't present on disk —
   see the existing dataset tests for the pattern.

## Testing

`pytest` alone will fail — `src/` must be on `PYTHONPATH` for the flat-import
style (`from settings import SETTINGS`, etc.) to resolve:

```bash
# Run all tests
PYTHONPATH=src python -m pytest tests/ -q

# Run with coverage
PYTHONPATH=src python -m pytest tests/ --cov=src -q
```

Using the project venv directly (matches CI and the exact command used to
verify this repo's current state):

```bash
PYTHONPATH=src venv/bin/python -m pytest tests/ -q
```

Tests **skip** rather than fail when a real dataset isn't present on disk —
check the test output for `skipped` counts; a fully green run doesn't by
itself prove the adapter paths were exercised.

## Linting

The project uses **ruff**, not flake8, and it is enforced by CI
(`.github/workflows/ci.yml`):

```bash
ruff check src/ --select E,F,W --ignore E501
```

Run it against `backend/` too when working there, since Phase 5 code is held
to the same standard even though CI's duplicate-definition check (below)
doesn't scan it:

```bash
ruff check src/ backend/ --select E,F,W --ignore E501
```

## Conventions enforced by CI or by explicit project design

These aren't just style preferences — breaking them fails CI or breaks a
guarantee other code relies on.

**No duplicate definitions.** CI walks the AST of every `src/*.py` file and
fails the build if any function or class name is defined more than once
across the directory (see the "Check for duplicate function definitions"
step in `.github/workflows/ci.yml`). If you need the same name in two files,
import the canonical implementation instead of redefining it — check the "do
NOT redefine these here" comment near the top of `src/aegis_demo.py` for an
example of the pattern this prevents. The check only scans `src/*.py`;
`backend/` is a separate package and isn't walked by it, but avoid
introducing accidental duplicates there either.

**No magic numbers.** Every tunable value — thresholds, decay rates,
hyperparameters, pool sizes, timeouts — lives in a typed, bounded Pydantic
settings field, never hardcoded inline. Engine tuning lives in
`src/settings.py`, reachable via the frozen singleton `SETTINGS`
(`SETTINGS.cii.*`, `SETTINGS.ml.*`, `SETTINGS.data_gen.*`, …) and reads no
environment variables. Backend/deployment tuning (DB connection, API bind
address, replay pacing, retention limits) lives in `backend/config.py`,
reachable via `BACKEND_SETTINGS`, and *does* read the environment (prefix
`AEGIS_`, plus an optional `.env` file — see `docs/SETUP.md`). Pick the one
that matches what you're tuning; don't hardcode either kind of value inline.

**Optional-override signature convention.** Public functions take
`param: T | None = None` and fall back to the settings singleton inside the
function body, e.g. `src/ml_engine.py`'s
`train_isolation_forest(n_estimators: int | None = None, contamination: float | None = None, ...)`.
This lets tests and hyperparameter sweeps override a single value without
mutating global config. Preserve this shape when adding new tunable
parameters to a public function.
