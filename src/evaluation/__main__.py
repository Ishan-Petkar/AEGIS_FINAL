"""
__main__.py — CLI entry point for ``python -m evaluation`` (AEGIS Phase 3).

Usage
-----
    PYTHONPATH=src python -m evaluation
    PYTHONPATH=src python -m evaluation --dataset swat --limit 20000 --no-ocsvm
"""
from __future__ import annotations

import argparse

from evaluation import DegenerateEvaluationError, results_to_dataframe, run_evaluation


def main() -> None:
    parser = argparse.ArgumentParser(description="AEGIS Phase 3 Evaluation Harness")
    parser.add_argument("--dataset", type=str, default="cic_ids2017",
                         help="Dataset to evaluate (default: cic_ids2017)")
    parser.add_argument("--limit", type=int, default=20_000,
                         help="Max rows to load from dataset (default: 20000)")
    parser.add_argument("--no-ocsvm", action="store_true",
                         help="Skip One-Class SVM (faster run)")
    args = parser.parse_args()

    try:
        results = run_evaluation(
            dataset=args.dataset, limit=args.limit, include_ocsvm=not args.no_ocsvm,
        )
    except DegenerateEvaluationError as exc:
        print(f"[evaluation] ERROR: {exc}")
        raise SystemExit(1) from exc

    df = results_to_dataframe(results)
    print("\nComparison table:")
    print(df[["precision", "recall", "f1", "roc_auc", "scoring"]].to_string())


if __name__ == "__main__":
    main()
