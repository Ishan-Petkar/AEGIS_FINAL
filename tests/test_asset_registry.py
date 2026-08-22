"""
test_asset_registry.py — Unit tests for AssetRegistry.

Tests cover:
- Pre-seeded static IP lookup (known assets)
- PaySim customer/merchant prefix heuristic resolution
- Subnet proximity heuristic resolution (10.0.1.x)
- Graceful auto-discovery fallback for unknown IDs (never drops an event)
- Criticality map export for CII calculator
"""

import unittest
from datasets.asset_registry import AssetRegistry


class TestAssetRegistry(unittest.TestCase):

    def setUp(self):
        self.registry = AssetRegistry.from_config()

    def test_known_ip_resolves_with_full_confidence(self):
        res = self.registry.resolve("10.0.1.12")
        self.assertEqual(res.asset_name, "Traffic_Controller")
        self.assertEqual(res.criticality, 0.9)
        self.assertEqual(res.confidence, 1.0)
        self.assertTrue(res.is_known)

    def test_paysim_customer_prefix_resolution(self):
        res = self.registry.resolve("C12345678")
        self.assertEqual(res.asset_name, "City_Payment_Gateway")
        self.assertGreater(res.confidence, 0.8)

    def test_paysim_merchant_prefix_resolution(self):
        res = self.registry.resolve("M98765432")
        self.assertEqual(res.asset_name, "Bank_Partner_API")
        self.assertGreater(res.confidence, 0.8)

    def test_unknown_ip_in_subnet_matches_by_proximity(self):
        # 10.0.1.18 is unmapped, but close to 10.0.1.16 (SCADA_Historian)
        res = self.registry.resolve("10.0.1.18")
        self.assertEqual(res.asset_name, "SCADA_Historian")
        self.assertFalse(res.is_known)
        self.assertLess(res.confidence, 1.0)

    def test_unknown_id_auto_discovered_without_dropping(self):
        res = self.registry.resolve("99.99.99.99")
        self.assertTrue(res.asset_name.startswith("Unresolved_"))
        self.assertFalse(res.is_known)
        self.assertEqual(res.confidence, 0.3)
        self.assertIn("99.99.99.99", self.registry.all_discovered())

    def test_criticality_map_covers_registered_and_discovered(self):
        self.registry.resolve("10.0.1.12")
        self.registry.resolve("88.88.88.88")
        cmap = self.registry.criticality_map()
        self.assertIn("Traffic_Controller", cmap)
        self.assertIn("Unresolved_88_88_88_88", cmap)


if __name__ == "__main__":
    unittest.main()
