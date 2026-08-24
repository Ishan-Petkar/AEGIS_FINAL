"""
backend/__init__.py — import bridge to the existing AEGIS engine.

The project has no `setup.py` / `pyproject.toml` (see CLAUDE.md section 5,
"Flat imports"): every module under `src/` is imported by bare name
(`from settings import SETTINGS`, `from config import DEPENDENCY_GRAPH`,
etc.) because `src/` is placed on `sys.path`, not because the project is
an installed package.

The `backend` package follows the identical convention so later Phase 5
tickets can do things like `from core.pipeline import run_analysis` or
`from graph_manager import build_graph` without needing to know `backend/`
exists or duplicate any engine logic (CLAUDE.md: "No duplicate
definitions" — import the canonical implementation instead of re-defining
it). This insertion happens exactly once, at `backend` package import
time, guarded so re-imports (and running the test suite, which imports
`backend` many times) never grow `sys.path` unbounded.
"""

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent.parent / "src"
_SRC_DIR_STR = str(_SRC_DIR)

if _SRC_DIR_STR not in sys.path:
    sys.path.insert(0, _SRC_DIR_STR)
