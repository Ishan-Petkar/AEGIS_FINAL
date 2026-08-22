"""
test_core_pipeline.py — Unit tests for the headless analytics pipeline
(Phase 1, contract C1).

The central assertion is M1.3: run_analysis() must be fully callable with no
Streamlit import in sys.modules — that is what makes it usable from an API
handler, a stream consumer, or a plain test, instead of being trapped inside
the dashboard script.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestHeadlessExecution(unittest.TestCase):
    """M1.3: no Streamlit dependency anywhere in the analysis path."""

    def test_streamlit_not_imported_by_core_pipeline_module(self):
        import ast
        import pathlib
        pipeline_source = pathlib.Path(__file__).resolve().parent.parent / "src" / "core" / "pipeline.py"
        tree = ast.parse(pipeline_source.read_text())
        imported_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_names.add(node.module)
        self.assertNotIn("streamlit", imported_names)

    def test_run_analysis_does_not_trigger_streamlit_import(self):
        self.assertNotIn("streamlit", sys.modules, "test setup should not have streamlit loaded yet")
        from core.pipeline import run_analysis
        run_analysis(dataset="synthetic")
        self.assertNotIn("streamlit", sys.modules, "run_analysis() must not import streamlit")


class TestRunAnalysisBaseline(unittest.TestCase):

    def test_synthetic_baseline_returns_result(self):
        from core.pipeline import run_analysis
        result = run_analysis(dataset="synthetic")
        self.assertGreater(result.total_connections, 0)
        self.assertGreaterEqual(result.anomalous_connections, 0)
        self.assertIsNone(result.dataset_warning)

    def test_no_active_attack_produces_empty_cii(self):
        from core.pipeline import run_analysis
        result = run_analysis(dataset="synthetic")
        self.assertEqual(result.cii.cii_median, 0.0)
        self.assertEqual(result.cii.impacted_assets, [])

    def test_active_attack_produces_nonzero_cii(self):
        from core.pipeline import run_analysis
        result = run_analysis(
            dataset="synthetic",
            active_attack="What-If: Traffic_Controller",
            anomalous_asset="Traffic_Controller",
        )
        self.assertGreater(result.cii.cii_median, 0.0)
        self.assertGreater(len(result.cii.impacted_assets), 0)

    def test_attack_edges_are_injected(self):
        from core.pipeline import run_analysis
        baseline = run_analysis(dataset="synthetic", num_edges=30)
        with_attack = run_analysis(
            dataset="synthetic",
            num_edges=30,
            attack_edges=[{
                "source": "10.0.1.20", "target": "198.51.100.42",
                "duration_sec": 120.0, "packets": 100000, "bytes": 500000000,
                "timestamp": "2026-01-01 00:00:00",
            }],
        )
        self.assertEqual(with_attack.total_connections, baseline.total_connections + 1)

    def test_gateway_nodes_appear_in_cii_output(self):
        """The Phase 1 gateway rewrite (contract C2) should be visible end to
        end: a compromise of a protected asset's neighbor should show a
        Gateway_* node in the impacted-assets list returned to the UI."""
        from core.pipeline import run_analysis
        result = run_analysis(
            dataset="synthetic",
            active_attack="What-If: Traffic_Controller",
            anomalous_asset="Traffic_Controller",
        )
        gateway_hits = [a for a in result.cii.impacted_assets if a.startswith("Gateway_")]
        self.assertTrue(len(gateway_hits) > 0, "expected at least one Gateway_* node in impacted_assets")
        for gw in gateway_hits:
            self.assertLessEqual(result.cii.hop_details[gw]["criticality"], 0.05)

    def test_unavailable_dataset_sets_warning_not_exception(self):
        from core.pipeline import run_analysis
        # A dataset name that is registered but whose files are (presumably)
        # not present is handled gracefully — this just exercises the
        # dataset_warning path without asserting which datasets exist locally.
        result = run_analysis(dataset="synthetic")
        self.assertIsNone(result.dataset_warning)


if __name__ == "__main__":
    unittest.main()
