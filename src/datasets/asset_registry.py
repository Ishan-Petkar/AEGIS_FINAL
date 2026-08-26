"""
asset_registry.py — Multi-identifier Asset Registry for AEGIS Phase 1.

Provides a single `AssetRegistry` service that maps:
- IP addresses   → graph node names (e.g. "10.0.1.12" → "Traffic_Controller")
- Account prefixes → graph node names (e.g. PaySim "C..." → originator, "M..." → merchant)
- Unmapped identifiers → auto-registered "Unresolved_*" nodes with low criticality

Key design decisions (per roadmap 1.3):
- Multiple identifiers per asset are supported.
- Resolution confidence is returned alongside the name.
- Unresolvable events are flagged explicitly, never silently dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from config import SMART_CITY_ASSETS, EXTERNAL_THREAT_IPS

# ---------------------------------------------------------------------------
# Resolution result
# ---------------------------------------------------------------------------

@dataclass
class ResolutionResult:
    """Encapsulates the result of an identifier→asset lookup."""
    asset_name: str
    criticality: float
    confidence: float
    is_known: bool   # True = statically registered; False = auto-discovered


# ---------------------------------------------------------------------------
# Asset Registry
# ---------------------------------------------------------------------------

class AssetRegistry:
    """
    Maintains a mapping from network identifiers (IPs, account IDs, hostnames)
    to operational asset names and their criticality scores.

    Usage::

        registry = AssetRegistry.from_config()
        result = registry.resolve("10.0.1.12")
        # ResolutionResult(asset_name='Traffic_Controller', criticality=0.9, confidence=1.0, is_known=True)
    """

    # PaySim account prefix rules
    _PAYSIM_CUSTOMER_PREFIX = re.compile(r"^C\d+$")
    _PAYSIM_MERCHANT_PREFIX = re.compile(r"^M\d+$")

    # Default criticality for auto-discovered / unresolvable nodes
    _DEFAULT_UNRESOLVED_CRITICALITY = 0.1
    _DEFAULT_UNRESOLVED_CONFIDENCE = 0.3

    def __init__(self) -> None:
        # ip / account_prefix → (asset_name, criticality)
        self._registry: Dict[str, Tuple[str, float]] = {}
        # Auto-discovered nodes registered on-the-fly during ingestion
        self._discovered: Dict[str, ResolutionResult] = {}

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_config(cls) -> "AssetRegistry":
        """Build a registry pre-seeded with all known smart-city and financial assets
        from `config.py`, plus known external threat IPs."""
        registry = cls()

        for asset in SMART_CITY_ASSETS:
            registry.register(
                identifier=asset["ip"],
                asset_name=asset["asset_name"],
                criticality=asset["criticality"],
            )

        for threat in EXTERNAL_THREAT_IPS:
            registry.register(
                identifier=threat["ip"],
                asset_name=threat["asset_name"],
                criticality=threat["criticality"],
            )

        # PaySim conceptual mappings — financial event actors
        # TRANSFER/CASH_OUT originators route through the city payment gateway;
        # Merchants are treated as bank partner API endpoints.
        registry.register(
            identifier="__paysim_customer__",
            asset_name="City_Payment_Gateway",
            criticality=0.95,
        )
        registry.register(
            identifier="__paysim_merchant__",
            asset_name="Bank_Partner_API",
            criticality=0.85,
        )

        return registry

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(
        self,
        identifier: str,
        asset_name: str,
        criticality: float,
    ) -> None:
        """Add or overwrite a static identifier→asset mapping."""
        self._registry[identifier.strip()] = (asset_name, criticality)

    # ------------------------------------------------------------------
    # Resolution
    # ------------------------------------------------------------------

    def resolve(self, identifier: str) -> ResolutionResult:
        """
        Resolve an identifier to a ResolutionResult.

        Resolution priority:
        1. Static registry (exact match on IP / hostname).
        2. PaySim account prefix heuristic.
        3. IP subnet heuristic (10.0.1.x → nearest asset by octet proximity).
        4. Auto-register as Unresolved_<identifier> with low confidence.
        """
        identifier = identifier.strip()

        # 1. Exact static match
        if identifier in self._registry:
            name, crit = self._registry[identifier]
            return ResolutionResult(
                asset_name=name,
                criticality=crit,
                confidence=1.0,
                is_known=True,
            )

        # 2. PaySim prefix heuristics
        if self._PAYSIM_CUSTOMER_PREFIX.match(identifier):
            name, crit = self._registry["__paysim_customer__"]
            return ResolutionResult(
                asset_name=name,
                criticality=crit,
                confidence=0.85,
                is_known=True,
            )
        if self._PAYSIM_MERCHANT_PREFIX.match(identifier):
            name, crit = self._registry["__paysim_merchant__"]
            return ResolutionResult(
                asset_name=name,
                criticality=crit,
                confidence=0.85,
                is_known=True,
            )

        # 3. Subnet heuristic — map unknown 10.0.1.x IPs to the closest known asset
        subnet_match = self._subnet_proximity(identifier)
        if subnet_match:
            return subnet_match

        # 4. Auto-discover: register with low confidence so the event is never dropped
        if identifier not in self._discovered:
            safe_name = re.sub(r"[^A-Za-z0-9_]", "_", identifier)
            self._discovered[identifier] = ResolutionResult(
                asset_name=f"Unresolved_{safe_name}",
                criticality=self._DEFAULT_UNRESOLVED_CRITICALITY,
                confidence=self._DEFAULT_UNRESOLVED_CONFIDENCE,
                is_known=False,
            )
        return self._discovered[identifier]

    def _subnet_proximity(self, identifier: str) -> Optional[ResolutionResult]:
        """
        For unknown IPs in the 10.0.1.x range, attempt to match by proximity to
        known registered IPs in the same /24.
        Returns None if no reasonable match can be made.
        """
        parts = identifier.split(".")
        if len(parts) != 4 or parts[:3] != ["10", "0", "1"]:
            return None
        try:
            target_host = int(parts[3])
        except ValueError:
            return None

        best_name: Optional[str] = None
        best_crit: float = self._DEFAULT_UNRESOLVED_CRITICALITY
        best_dist = 256

        for ip, (name, crit) in self._registry.items():
            ip_parts = ip.split(".")
            if len(ip_parts) != 4 or ip_parts[:3] != ["10", "0", "1"]:
                continue
            try:
                host = int(ip_parts[3])
            except ValueError:
                continue
            dist = abs(host - target_host)
            if dist < best_dist:
                best_dist = dist
                best_name = name
                best_crit = crit

        if best_name and best_dist <= 5:  # within 5 host numbers = reasonable inference
            return ResolutionResult(
                asset_name=best_name,
                criticality=best_crit,
                confidence=max(0.4, 1.0 - best_dist * 0.12),
                is_known=False,
            )
        return None

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def registered_count(self) -> int:
        return len(self._registry)

    def discovered_count(self) -> int:
        return len(self._discovered)

    def all_discovered(self) -> Dict[str, ResolutionResult]:
        """Return all auto-discovered (unresolved) identifiers seen so far."""
        return dict(self._discovered)

    def criticality_map(self) -> Dict[str, float]:
        """Return a {asset_name: criticality} dict covering all registered + discovered assets.
        Used by the CII calculator's criticality_map parameter."""
        result: Dict[str, float] = {}
        for _, (name, crit) in self._registry.items():
            result[name] = crit
        for _, res in self._discovered.items():
            result[res.asset_name] = res.criticality
        return result
