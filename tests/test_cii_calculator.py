"""
test_cii_calculator.py — Unit tests for the Monte Carlo Cascading Impact Index engine.

Tests cover:
- Monte Carlo termination (no infinite loops, respects max_hops)
- CII output bounds ([0.0, cii_max_value])
- Edge cases: unknown node, disconnected graph, self-loop, zero anomaly score
- Diamond dependency: two independent paths correctly yield P(A∪B) > max(P(A),P(B))
- Redundant (backed_up_by): node only compromised when ALL backup paths fail
- Common-mode (shares_provider): correlated failure via shared provider_id
- Decay / hop behavior: deeper hops have lower compromise frequency
- Criticality fallback: missing nodes use default_criticality
- Uncertainty output: p5 <= median <= p95
- Backward compatibility: 3-tuple return from compute_cascading_impact()
"""

import sys
import os
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from settings import SETTINGS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_cii(graph_entries, anomalous_asset, anomaly_score, criticality_map,
             max_hops=None, mc_iterations=2000, random_seed=42):
    """
    Run compute_cascading_impact with a patched DEPENDENCY_GRAPH.

    Accepts both legacy tuples and new dicts.
    """
    import cii_calculator

    with patch.object(cii_calculator, "DEPENDENCY_GRAPH", graph_entries):
        from cii_calculator import compute_cascading_impact
        kwargs = {"criticality_map": criticality_map, "mc_iterations": mc_iterations,
                  "random_seed": random_seed}
        if max_hops is not None:
            kwargs["max_hops"] = max_hops
        return compute_cascading_impact(anomalous_asset, anomaly_score, **kwargs)


def _run_cii_full(graph_entries, anomalous_asset, anomaly_score, criticality_map,
                  max_hops=None, mc_iterations=2000, random_seed=42):
    """
    Run compute_cascading_impact_full with a patched DEPENDENCY_GRAPH.
    """
    import cii_calculator

    with patch.object(cii_calculator, "DEPENDENCY_GRAPH", graph_entries):
        from cii_calculator import compute_cascading_impact_full
        kwargs = {"criticality_map": criticality_map, "mc_iterations": mc_iterations,
                  "random_seed": random_seed}
        if max_hops is not None:
            kwargs["max_hops"] = max_hops
        return compute_cascading_impact_full(anomalous_asset, anomaly_score, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCIIBounds(unittest.TestCase):
    """CII score must always be in [0, cii_max_value]."""

    def _crit(self, nodes):
        return {n: 1.0 for n in nodes}

    def test_cii_never_exceeds_max(self):
        graph = [
            ("A", "B", "depends_on", 1.0),
            ("B", "C", "depends_on", 1.0),
        ]
        score, _, _ = _run_cii(graph, "A", 1.0, self._crit(["A", "B", "C"]))
        self.assertLessEqual(score, SETTINGS.cii.cii_max_value)

    def test_cii_never_below_zero(self):
        graph = [("A", "B", "depends_on", 0.5)]
        score, _, _ = _run_cii(graph, "A", 0.5, self._crit(["A", "B"]))
        self.assertGreaterEqual(score, 0.0)

    def test_zero_anomaly_score_produces_zero_cii(self):
        graph = [("A", "B", "depends_on", 0.9)]
        score, assets, _ = _run_cii(graph, "A", 0.0, self._crit(["A", "B"]))
        self.assertEqual(score, 0.0)


class TestMonteCarloTermination(unittest.TestCase):
    """Simulation must terminate and respect max_hops."""

    def test_linear_chain_respects_max_hops(self):
        graph = [
            ("A", "B", "d", 0.9),
            ("B", "C", "d", 0.9),
            ("C", "D", "d", 0.9),
            ("D", "E", "d", 0.9),
        ]
        # With max_hops=2, should not reach D or E.
        _, assets, _ = _run_cii(
            graph, "A", 1.0,
            {"A": 1, "B": 1, "C": 1, "D": 1, "E": 1},
            max_hops=2,
        )
        self.assertIn("B", assets)
        self.assertIn("C", assets)
        self.assertNotIn("D", assets)
        self.assertNotIn("E", assets)

    def test_self_loop_terminates(self):
        graph = [("A", "A", "depends_on", 0.9)]
        # Should not raise or loop forever.
        score, assets, _ = _run_cii(graph, "A", 0.8, {"A": 1.0})
        self.assertGreaterEqual(score, 0.0)
        self.assertEqual(assets, [])  # A is the origin node, not counted.

    def test_cycle_terminates(self):
        graph = [
            ("A", "B", "d", 0.9),
            ("B", "C", "d", 0.9),
            ("C", "A", "d", 0.9),  # cycle back to A
        ]
        score, assets, _ = _run_cii(graph, "A", 1.0, {"A": 1, "B": 1, "C": 1})
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, SETTINGS.cii.cii_max_value)


class TestEdgeCases(unittest.TestCase):
    """Edge cases: unknown node, disconnected graph, isolated node."""

    def test_unknown_node_returns_zeros(self):
        graph = [("A", "B", "d", 0.9)]
        score, assets, hops = _run_cii(graph, "Z", 1.0, {"A": 1, "B": 1})
        self.assertEqual(score, 0.0)
        self.assertEqual(assets, [])
        self.assertEqual(hops, {})

    def test_disconnected_graph_no_cross_propagation(self):
        graph = [
            ("A", "B", "d", 0.9),
            ("C", "D", "d", 0.5),
        ]
        _, assets, _ = _run_cii(graph, "A", 1.0, {"A": 1, "B": 1, "C": 1, "D": 1})
        self.assertIn("B", assets)
        self.assertNotIn("C", assets)
        self.assertNotIn("D", assets)

    def test_leaf_node_has_no_impacted_assets(self):
        graph = [("A", "B", "d", 0.9)]
        _, assets, _ = _run_cii(graph, "B", 0.9, {"A": 1, "B": 1})
        self.assertEqual(assets, [])


class TestDiamondDependency(unittest.TestCase):
    """Diamond: A→B→D and A→C→D. D should be reached via union of paths."""

    def test_diamond_d_compromise_frequency_exceeds_single_path(self):
        """With two independent paths each having prob 0.5 to D,
        P(D compromised) ≈ 1 - (1-0.5)*(1-0.5) = 0.75.

        The MC engine should produce a D compromise_freq significantly
        higher than 0.5 (the single-path probability).
        """
        graph = [
            ("A", "B", "d", 1.0),  # A→B fires with certainty
            ("A", "C", "d", 1.0),  # A→C fires with certainty
            ("B", "D", "d", 0.5),  # B→D fires 50%
            ("C", "D", "d", 0.5),  # C→D fires 50%
        ]
        result = _run_cii_full(
            graph, "A", 1.0,
            {"B": 0.5, "C": 0.5, "D": 0.9},
            max_hops=3, mc_iterations=5000, random_seed=42,
        )
        d_freq = result.hop_details["D"]["compromise_freq"]
        # Expected ≈ 0.75 (union of two independent 0.5 paths)
        # Allow tolerance for Monte Carlo variance
        self.assertGreater(d_freq, 0.60, f"D freq {d_freq} should be > 0.60 (union of paths)")
        self.assertLess(d_freq, 0.90, f"D freq {d_freq} should be < 0.90")

    def test_diamond_d_appears_in_impacted_assets(self):
        graph = [
            ("A", "B", "d", 0.9),
            ("A", "C", "d", 0.9),
            ("B", "D", "d", 0.8),
            ("C", "D", "d", 0.8),
        ]
        _, assets, _ = _run_cii(graph, "A", 1.0, {"B": 0.5, "C": 0.5, "D": 0.9}, max_hops=3)
        self.assertIn("B", assets)
        self.assertIn("C", assets)
        self.assertIn("D", assets)


class TestBackedUpByEdge(unittest.TestCase):
    """backed_up_by: node only compromised when ALL backup paths fail."""

    def test_backed_up_node_rarely_compromised_with_redundancy(self):
        """A→B (depends_on, prob=1.0), A→C (depends_on, prob=1.0),
        B→D (backed_up_by, prob=0.5), C→D (backed_up_by, prob=0.5).

        D is compromised only if BOTH B→D AND C→D fire.
        P(D) ≈ 0.5 * 0.5 = 0.25.
        """
        graph = [
            {"src": "A", "tgt": "B", "edge_type": "depends_on", "prob": 1.0},
            {"src": "A", "tgt": "C", "edge_type": "depends_on", "prob": 1.0},
            {"src": "B", "tgt": "D", "edge_type": "backed_up_by", "prob": 0.5},
            {"src": "C", "tgt": "D", "edge_type": "backed_up_by", "prob": 0.5},
        ]
        result = _run_cii_full(
            graph, "A", 1.0,
            {"B": 0.5, "C": 0.5, "D": 0.9},
            max_hops=3, mc_iterations=5000, random_seed=42,
        )
        # D should be compromised in roughly 25% of iterations
        d_freq = result.hop_details.get("D", {}).get("compromise_freq", 0.0)
        self.assertGreater(d_freq, 0.10, f"D freq {d_freq} should be > 0.10")
        self.assertLess(d_freq, 0.45, f"D freq {d_freq} should be < 0.45 (backed_up_by = AND)")

    def test_backed_up_vs_depends_on_lower_compromise(self):
        """backed_up_by should yield LOWER compromise freq than depends_on
        for the same topology."""
        base_graph = [
            {"src": "A", "tgt": "B", "edge_type": "depends_on", "prob": 1.0},
            {"src": "A", "tgt": "C", "edge_type": "depends_on", "prob": 1.0},
        ]
        # depends_on variant: either path compromises D
        depends_graph = base_graph + [
            {"src": "B", "tgt": "D", "edge_type": "depends_on", "prob": 0.5},
            {"src": "C", "tgt": "D", "edge_type": "depends_on", "prob": 0.5},
        ]
        # backed_up_by variant: BOTH paths must fail to compromise D
        backup_graph = base_graph + [
            {"src": "B", "tgt": "D", "edge_type": "backed_up_by", "prob": 0.5},
            {"src": "C", "tgt": "D", "edge_type": "backed_up_by", "prob": 0.5},
        ]

        crit = {"B": 0.5, "C": 0.5, "D": 0.9}
        depends_result = _run_cii_full(depends_graph, "A", 1.0, crit,
                                       max_hops=3, mc_iterations=5000, random_seed=42)
        backup_result = _run_cii_full(backup_graph, "A", 1.0, crit,
                                      max_hops=3, mc_iterations=5000, random_seed=42)

        depends_d = depends_result.hop_details.get("D", {}).get("compromise_freq", 0)
        backup_d = backup_result.hop_details.get("D", {}).get("compromise_freq", 0)

        self.assertGreater(depends_d, backup_d,
                           f"depends_on freq ({depends_d}) should be > backed_up_by freq ({backup_d})")


class TestSharesProviderEdge(unittest.TestCase):
    """shares_provider: edges sharing a provider_id must fire together
    (correlated common-mode failure), not independently.

    Setup: A -> B and A -> C, both shares_provider, same provider_id,
    prob=0.6, equal criticality 0.5 each. graph_criticality_mass = 1.0
    (B + C, A excluded as origin), so per-iteration cii (anomaly_score=1.0)
    takes value 0.0 (neither compromised), 0.5 (exactly one), or 1.0
    (both). Correlated sampling can only ever produce 0.0 or 1.0 — a
    single-node outcome (0.5) would mean the shared draw was not actually
    shared.
    """

    def test_same_provider_id_edges_never_split(self):
        graph = [
            {"src": "A", "tgt": "B", "edge_type": "shares_provider", "prob": 0.6,
             "provider_id": "aws-us-east-1"},
            {"src": "A", "tgt": "C", "edge_type": "shares_provider", "prob": 0.6,
             "provider_id": "aws-us-east-1"},
        ]
        result = _run_cii_full(
            graph, "A", 1.0, {"B": 0.5, "C": 0.5},
            max_hops=3, mc_iterations=4000, random_seed=7,
        )
        split_outcomes = result.cii_scores[
            (result.cii_scores > 0.3) & (result.cii_scores < 0.7)
        ]
        self.assertEqual(
            len(split_outcomes), 0,
            "shares_provider edges with the same provider_id must fire "
            "together — found iterations where only one of B/C was "
            "compromised, meaning the draw was independent, not shared.",
        )
        # Both bounding outcomes (neither / both) must still actually occur
        # — otherwise the test would trivially pass on a broken "always
        # fires" or "never fires" implementation too.
        self.assertTrue((result.cii_scores < 0.1).any())
        self.assertTrue((result.cii_scores > 0.9).any())

    def test_same_provider_id_marginal_frequency_tracks_prob(self):
        graph = [
            {"src": "A", "tgt": "B", "edge_type": "shares_provider", "prob": 0.6,
             "provider_id": "aws-us-east-1"},
            {"src": "A", "tgt": "C", "edge_type": "shares_provider", "prob": 0.6,
             "provider_id": "aws-us-east-1"},
        ]
        result = _run_cii_full(
            graph, "A", 1.0, {"B": 0.5, "C": 0.5},
            max_hops=3, mc_iterations=4000, random_seed=7,
        )
        b_freq = result.hop_details["B"]["compromise_freq"]
        c_freq = result.hop_details["C"]["compromise_freq"]
        self.assertAlmostEqual(b_freq, 0.6, delta=0.05)
        self.assertAlmostEqual(c_freq, 0.6, delta=0.05)
        # Correlated: B and C are compromised in lockstep, so their
        # marginal frequencies must match each other almost exactly, not
        # just both hover near 0.6 independently.
        self.assertAlmostEqual(b_freq, c_freq, delta=0.01)

    def test_different_provider_id_edges_are_independent(self):
        """Control case: different provider_id -> no forced correlation,
        so single-node outcomes (0.5) DO occur — confirms the correlated
        test above isn't passing for an unrelated reason (e.g. a topology
        that can never produce a split outcome regardless of sampling)."""
        graph = [
            {"src": "A", "tgt": "B", "edge_type": "shares_provider", "prob": 0.6,
             "provider_id": "aws-us-east-1"},
            {"src": "A", "tgt": "C", "edge_type": "shares_provider", "prob": 0.6,
             "provider_id": "gcp-us-central1"},
        ]
        result = _run_cii_full(
            graph, "A", 1.0, {"B": 0.5, "C": 0.5},
            max_hops=3, mc_iterations=4000, random_seed=7,
        )
        split_outcomes = result.cii_scores[
            (result.cii_scores > 0.3) & (result.cii_scores < 0.7)
        ]
        self.assertGreater(
            len(split_outcomes), 0,
            "different provider_ids should sample independently, "
            "producing some single-node (split) outcomes",
        )

    def test_edges_without_provider_id_sample_independently(self):
        """No provider_id at all (field omitted) must not crash and must
        behave as plain independent sampling — same control shape as the
        different-provider-id test above."""
        graph = [
            {"src": "A", "tgt": "B", "edge_type": "shares_provider", "prob": 0.6},
            {"src": "A", "tgt": "C", "edge_type": "shares_provider", "prob": 0.6},
        ]
        result = _run_cii_full(
            graph, "A", 1.0, {"B": 0.5, "C": 0.5},
            max_hops=3, mc_iterations=4000, random_seed=7,
        )
        split_outcomes = result.cii_scores[
            (result.cii_scores > 0.3) & (result.cii_scores < 0.7)
        ]
        self.assertGreater(len(split_outcomes), 0)


class TestUncertaintyOutput(unittest.TestCase):
    """p5 <= median <= p95 for all outputs."""

    def test_percentile_ordering(self):
        graph = [
            ("A", "B", "d", 0.8),
            ("B", "C", "d", 0.7),
        ]
        result = _run_cii_full(graph, "A", 0.9, {"B": 0.8, "C": 0.6},
                               max_hops=3, mc_iterations=2000, random_seed=42)
        self.assertLessEqual(result.cii_p5, result.cii_median)
        self.assertLessEqual(result.cii_median, result.cii_p95)

    def test_raw_scores_array_has_correct_length(self):
        graph = [("A", "B", "d", 0.9)]
        n = 500
        result = _run_cii_full(graph, "A", 1.0, {"B": 1.0},
                               mc_iterations=n, random_seed=42)
        self.assertEqual(len(result.cii_scores), n)


class TestCriticalityFallback(unittest.TestCase):
    """Assets missing from criticality_map should use default_criticality."""

    def test_missing_asset_uses_default_criticality(self):
        graph = [("A", "B", "d", 1.0)]
        # Do NOT include B in criticality_map.
        _, _, hops = _run_cii(graph, "A", 1.0, {})
        self.assertAlmostEqual(hops["B"]["criticality"], SETTINGS.cii.default_criticality)


class TestBackwardCompatibility(unittest.TestCase):
    """compute_cascading_impact() returns (float, list, dict) 3-tuple."""

    def test_returns_three_tuple(self):
        graph = [("A", "B", "d", 0.9)]
        result = _run_cii(graph, "A", 0.8, {"B": 0.5})
        self.assertEqual(len(result), 3)
        score, assets, hops = result
        self.assertIsInstance(score, float)
        self.assertIsInstance(assets, list)
        self.assertIsInstance(hops, dict)

    def test_hop_details_has_backward_compat_keys(self):
        graph = [("A", "B", "d", 0.9)]
        _, _, hops = _run_cii(graph, "A", 0.8, {"B": 0.5})
        for node, details in hops.items():
            self.assertIn("hop", details)
            self.assertIn("prob", details)
            self.assertIn("impact", details)
            self.assertIn("criticality", details)


class TestLegacyTupleFormat(unittest.TestCase):
    """Legacy (src, tgt, edge_type, prob) tuples still work."""

    def test_legacy_tuples_accepted(self):
        graph = [
            ("A", "B", "depends_on", 0.8),
            ("B", "C", "depends_on", 0.9),
        ]
        score, assets, hops = _run_cii(graph, "A", 0.9, {"B": 0.5, "C": 0.6})
        self.assertGreater(score, 0.0)
        self.assertIn("B", assets)


class TestNewDictFormat(unittest.TestCase):
    """New dict format with metadata is accepted and works."""

    def test_dict_format_accepted(self):
        graph = [
            {"src": "A", "tgt": "B", "edge_type": "depends_on", "prob": 0.8,
             "source": "test", "owner": "test", "rationale": "test",
             "confidence": 0.9, "last_reviewed": "2026-01-01"},
        ]
        score, assets, _ = _run_cii(graph, "A", 0.9, {"B": 0.5})
        self.assertGreater(score, 0.0)
        self.assertIn("B", assets)


class TestCIIWithProductionGraph(unittest.TestCase):
    """Smoke tests against the real DEPENDENCY_GRAPH from config."""

    def setUp(self):
        from config import SMART_CITY_ASSETS
        self.criticality_map = {a["asset_name"]: a["criticality"] for a in SMART_CITY_ASSETS}
        self.criticality_map["City_Grid"] = 1.0

    def test_traffic_controller_produces_nonzero_cii(self):
        from cii_calculator import compute_cascading_impact
        score, assets, _ = compute_cascading_impact(
            "Traffic_Controller", 0.9, criticality_map=self.criticality_map,
            random_seed=42,
        )
        self.assertGreater(score, 0.0)
        self.assertTrue(len(assets) > 0)

    def test_city_payment_gateway_breach_propagates_to_bank(self):
        from cii_calculator import compute_cascading_impact
        _, assets, _ = compute_cascading_impact(
            "City_Payment_Gateway", 0.95, criticality_map=self.criticality_map,
            random_seed=42,
        )
        self.assertIn("Bank_Partner_API", assets)


if __name__ == "__main__":
    unittest.main()
