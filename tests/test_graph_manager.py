"""
test_graph_manager.py — Unit tests for the single authoritative graph builder
and the mandatory access-gateway topology (Phase 1, contract C2).

Tests cover:
- Non-protected edges pass through unmodified
- Protected-asset edges are rewritten through a gateway node
- Multiple sources into the same protected asset combine via probabilistic
  union, not last-write-wins (the bug found and fixed while building this)
- Gateway node/edge attribution (is_gateway_edge)
- gateway_nodes() matches what build_graph() actually inserts
- apply_gateway=False bypasses gating entirely
- CII engine treats gateway nodes as near-zero criticality regardless of the
  caller's criticality_map (Decision #4)
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import graph_manager
from settings import SETTINGS


class TestNonProtectedEdgesUnaffected(unittest.TestCase):
    """Synthetic node names (not in SMART_CITY_ASSETS) are never gated."""

    def test_synthetic_graph_untouched(self):
        raw = [
            {"src": "A", "tgt": "B", "edge_type": "depends_on", "prob": 0.8},
            {"src": "B", "tgt": "C", "edge_type": "depends_on", "prob": 0.9},
        ]
        G = graph_manager.build_graph(raw_graph=raw)
        self.assertEqual(set(G.nodes()), {"A", "B", "C"})
        self.assertTrue(G.has_edge("A", "B"))
        self.assertTrue(G.has_edge("B", "C"))
        self.assertEqual(G["A"]["B"]["prob"], 0.8)
        self.assertFalse(G["A"]["B"]["is_gateway_edge"])

    def test_legacy_tuple_format_accepted(self):
        raw = [("A", "B", "depends_on", 0.7)]
        G = graph_manager.build_graph(raw_graph=raw)
        self.assertTrue(G.has_edge("A", "B"))
        self.assertEqual(G["A"]["B"]["prob"], 0.7)


class TestGatewayRewrite(unittest.TestCase):
    """Edges targeting a protected (high-criticality) asset are rewritten
    through that asset's Purdue-zone gateway node."""

    def test_protected_asset_not_directly_reachable(self):
        G = graph_manager.build_graph()
        # Traffic_Controller (criticality 0.9) is protected — no direct edge
        # from any of its known predecessors should terminate on it.
        self.assertFalse(G.has_edge("Traffic_Cam_1", "Traffic_Controller"))
        self.assertFalse(G.has_edge("SCADA_Historian", "Traffic_Controller"))

    def test_protected_asset_reachable_only_via_its_gateway(self):
        G = graph_manager.build_graph()
        gateway = "Gateway_L1"  # Traffic_Controller is purdue_level 1
        self.assertTrue(G.has_edge("Traffic_Cam_1", gateway))
        self.assertTrue(G.has_edge(gateway, "Traffic_Controller"))

    def test_unprotected_low_criticality_asset_still_direct(self):
        """Traffic_Cam_1/2 (criticality 0.2) are below the gateway threshold —
        edges targeting them (if any existed) would remain direct. Assert via
        the threshold itself since no edges target the cameras in the real graph."""
        self.assertLess(0.2, SETTINGS.gateway.criticality_threshold)

    def test_gateway_edges_flagged(self):
        G = graph_manager.build_graph()
        gw = "Gateway_L1"
        self.assertTrue(G["Traffic_Cam_1"][gw]["is_gateway_edge"])
        self.assertTrue(G[gw]["Traffic_Controller"]["is_gateway_edge"])

    def test_apply_gateway_false_bypasses_gating(self):
        G = graph_manager.build_graph(apply_gateway=False)
        self.assertTrue(G.has_edge("Traffic_Cam_1", "Traffic_Controller"))
        self.assertFalse(any(n.startswith("Gateway_") for n in G.nodes()))


class TestUnionProbability(unittest.TestCase):
    """Multiple original edges into the same protected asset must combine via
    probabilistic union — NOT silently overwrite each other. This is the bug
    found while building graph_manager.py: nx.DiGraph.add_edge() on a repeated
    (u, v) pair replaces prior attributes rather than merging them."""

    def test_two_sources_combine_via_union_not_last_write_wins(self):
        raw = [
            {"src": "External_1", "tgt": "City_Payment_Gateway", "edge_type": "depends_on", "prob": 0.9},
            {"src": "External_2", "tgt": "City_Payment_Gateway", "edge_type": "depends_on", "prob": 0.6},
        ]
        G = graph_manager.build_graph(raw_graph=raw)
        gw = "Gateway_L4"  # City_Payment_Gateway is purdue_level 4
        expected = 1 - (1 - 0.9) * (1 - 0.6)  # 0.96
        self.assertAlmostEqual(G[gw]["City_Payment_Gateway"]["prob"], expected, places=6)
        # Neither original prob (0.9 or 0.6) should appear verbatim — union
        # is strictly greater than both single-source probabilities.
        self.assertGreater(G[gw]["City_Payment_Gateway"]["prob"], 0.9)

    def test_single_source_union_equals_original_prob(self):
        """With only one source, union degenerates to that source's own prob."""
        raw = [
            {"src": "External_1", "tgt": "City_Payment_Gateway", "edge_type": "depends_on", "prob": 0.42},
        ]
        G = graph_manager.build_graph(raw_graph=raw)
        gw = "Gateway_L4"
        self.assertAlmostEqual(G[gw]["City_Payment_Gateway"]["prob"], 0.42, places=6)

    def test_hop1_edges_remain_distinct_per_source(self):
        """Unlike the shared hop2 edge, each source's hop1 (src -> gateway)
        edge is independent and must keep its own original probability."""
        raw = [
            {"src": "External_1", "tgt": "City_Payment_Gateway", "edge_type": "depends_on", "prob": 0.9},
            {"src": "External_2", "tgt": "City_Payment_Gateway", "edge_type": "depends_on", "prob": 0.6},
        ]
        G = graph_manager.build_graph(raw_graph=raw)
        gw = "Gateway_L4"
        self.assertAlmostEqual(G["External_1"][gw]["prob"], 0.9, places=6)
        self.assertAlmostEqual(G["External_2"][gw]["prob"], 0.6, places=6)

    def test_real_dependency_graph_city_payment_gateway_union(self):
        """Regression pin for the actual production graph: three edges target
        City_Payment_Gateway (Citizen_Portal 0.9, Traffic_Controller 0.5,
        Power_Substation 0.4) -> union = 1 - 0.1*0.5*0.6 = 0.97."""
        G = graph_manager.build_graph()
        gw = "Gateway_L4"
        expected = 1 - (0.1 * 0.5 * 0.6)
        self.assertAlmostEqual(G[gw]["City_Payment_Gateway"]["prob"], expected, places=6)


class TestGatewayNodes(unittest.TestCase):

    def test_gateway_nodes_matches_graph_contents(self):
        G = graph_manager.build_graph()
        declared = graph_manager.gateway_nodes()
        present_in_graph = {n for n in G.nodes() if n.startswith("Gateway_")}
        self.assertEqual(declared, present_in_graph)

    def test_gateway_nodes_nonempty_for_real_topology(self):
        self.assertTrue(len(graph_manager.gateway_nodes()) > 0)


class TestSingleGraphConstructor(unittest.TestCase):
    """M1.2: exactly one place in src/ should construct the dependency graph
    (nx.Graph()/nx.DiGraph() from DEPENDENCY_GRAPH) — graph_manager.py."""

    def test_cii_calculator_delegates_to_graph_manager(self):
        import ast
        import pathlib
        cii_source = pathlib.Path(__file__).resolve().parent.parent / "src" / "cii_calculator.py"
        tree = ast.parse(cii_source.read_text())
        calls_nx_constructor = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("Graph", "DiGraph")
            for node in ast.walk(tree)
        )
        self.assertFalse(
            calls_nx_constructor,
            "cii_calculator.py should delegate graph construction to graph_manager.py, "
            "not call nx.Graph()/nx.DiGraph() itself.",
        )


class TestGatewayCriticalityInCII(unittest.TestCase):
    """A gateway hop must not distort CII totals (Decision #4)."""

    def test_gateway_node_criticality_is_near_zero_setting(self):
        self.assertLessEqual(SETTINGS.gateway.gateway_node_criticality, 0.05)

    def test_gateway_hit_does_not_use_default_criticality(self):
        """Regression guard: before this was fixed, an unlisted gateway node
        would fall back to SETTINGS.cii.default_criticality (0.5) instead of
        the near-zero gateway value, inflating every CII score that happened
        to route through a gateway hop."""
        from cii_calculator import _criticality_of
        gw_nodes = frozenset({"Gateway_L1"})
        crit = _criticality_of("Gateway_L1", criticality_map={}, default_criticality=0.5, gateway_nodes=gw_nodes)
        self.assertEqual(crit, SETTINGS.gateway.gateway_node_criticality)
        self.assertNotEqual(crit, 0.5)


if __name__ == "__main__":
    unittest.main()
