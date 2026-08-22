# Development Guide

## Adding a New Dataset
1. Create a new adapter in `src/datasets/<name>_adapter.py`.
2. Inherit from `BaseAdapter` or implement the `_normalise(self, raw: pd.DataFrame)` method.
3. Ensure all columns output by the adapter conform to the `CanonicalEvent` schema found in `src/datasets/schema.py`.
4. Register the new adapter in `src/datasets/loader.py` within `available_datasets()`.

## Testing
We use `pytest` for all unit testing.
```bash
# Run all tests
pytest tests/

# Run tests with coverage
pytest --cov=src tests/
```

## Linting
We recommend using `flake8` to maintain code cleanliness. Unused imports and unused variables will cause errors.
```bash
flake8 src tests --select=F401,F841
```
