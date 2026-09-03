"""
backend/detection/signature.py — declarative rule/signature engine.

What this is, and what it is NOT
---------------------------------------------------------------------------
CIC-IDS2017's `TrafficLabelling` CSVs (the corpus this project replays —
see `backend/replay_reader.py`) carry **flow records**: one row per
bidirectional connection, summarised as ports, protocol, duration, packet
counts and byte counts. They do not carry packet payloads. This engine's
rules can therefore only ever match on **flow METADATA** — addresses,
ports, protocol, and byte/packet shape — never on packet contents.

That makes this module fundamentally unlike Snort, Suricata, or any other
payload-inspecting IDS, and no docstring, rule title, or evidence string in
this file may imply otherwise. `docs/DETECTION_STUDY.md` section 7
("Honest limitations") sets the tone this file follows: state what was
measured and what was not, rather than let a rule's name oversell it.
`BACKEND_SETTINGS.signature_enabled`'s own field docstring makes the same
point — this file is the implementation of that promise, not a place to
walk it back.

Given that ceiling, what a metadata-only rule CAN honestly claim is
narrower than "malicious": it can claim "this flow's shape matches a
pattern operators associate with a known technique" — a scan, a C2
heartbeat, contact with a known-bad address, a legacy admin protocol that
should not be reachable. Each rule below says exactly that, with a
confidence pinned to how specific its shape actually is, not to how
alarming its name sounds.

Why a declarative rule set rather than a hand-rolled `if` chain
---------------------------------------------------------------------------
A `SignatureRule` is a `(rule_id, title, description, confidence,
predicate)` tuple. Predicates are plain `FlowFeatures -> bool` callables,
so the rule set is data an operator (or `/api/stats`, eventually) can
enumerate and audit, and a test can inject a synthetic rule set without
touching the engine. This mirrors `config.DEPENDENCY_GRAPH`'s "list of
dicts with provenance, not a graph built by imperative code" choice in the
research engine (see `src/config.py`) — reference data belongs in data,
code belongs in code that reads it.

Scoring: max, not sum
---------------------------------------------------------------------------
When several rules match one flow, `calibrated_score` is the MAX of the
matching rules' confidences, never a sum or an average. Two independent
*weak* metadata signals (say, a high-numbered port AND a small payload)
do not compound into certainty the way two independent *strong* signals
might — both weak rules can fire on the same ordinary benign short-lived
connection, and summing would manufacture confidence neither rule alone
earns. The engine reports the strongest single piece of evidence and lists
every rule that fired in `evidence`, so an operator sees the full picture
without the score itself overstating it.

Certainty is always `Certainty.HEURISTIC`
---------------------------------------------------------------------------
Unlike the honeytoken tripwire (`verdict_from_tripwire` in `contracts.py`),
no rule here can be `Certainty.CONFIRMED`: a metadata match, however
specific, can always describe a legitimate flow that happens to share its
shape (an internal vulnerability scanner, a database migration script, an
operator's own RDP session). `BACKEND_SETTINGS.hybrid_weight_signature`
(0.85) already prices that residual uncertainty in as the verdict's
reliability; `Certainty.HEURISTIC` prices it in structurally, so fusion may
combine this signal probabilistically but may never let it alone force an
escalation the way the tripwire can.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from backend.config import BACKEND_SETTINGS
from backend.detection.contracts import (
    DETECTOR_SIGNATURE,
    Certainty,
    DetectorVerdict,
    FlowFeatures,
)
from config import EXTERNAL_THREAT_IPS

# ---------------------------------------------------------------------------
# Domain reference data — NOT settings. These are fixed catalogues (a
# threat-intel list, IANA-style port groupings), not tunable numerics, so
# per this codebase's convention (see CLAUDE.md section 5, and
# `src/config.py`'s SMART_CITY_ASSETS / DEPENDENCY_GRAPH) they live here as
# module constants rather than as BACKEND_SETTINGS fields.
# ---------------------------------------------------------------------------

#: Addresses from `src/config.py::EXTERNAL_THREAT_IPS` — the research
#: engine's curated (if small) threat-intel list. Reused rather than
#: duplicated, per this ticket's brief: it "exists and is currently barely
#: used." A `frozenset` of bare address strings; the richer per-IP metadata
#: (asset_name, type) in the source list is not needed for matching.
KNOWN_BAD_ADDRESSES: frozenset[str] = frozenset(
    entry["ip"] for entry in EXTERNAL_THREAT_IPS
)

#: High-risk administrative / legacy service ports: unencrypted or
#: historically exploit-heavy remote-management protocols (telnet, FTP,
#: legacy Windows file/RPC sharing, RDP). A connection to one of these is
#: not proof of compromise — plenty of legacy internal estates still run
#: them deliberately — but it is a defensible thing to flag on port number
#: alone, which is exactly the metadata this engine has.
HIGH_RISK_ADMIN_PORTS: frozenset[int] = frozenset(
    {
        21,  # FTP — cleartext credentials
        23,  # Telnet — cleartext, no integrity
        135,  # MSRPC endpoint mapper
        139,  # NetBIOS Session Service
        445,  # SMB — EternalBlue-class exploit history
        3389,  # RDP — internet-facing RDP is a top ransomware entry vector
    }
)

#: Common database/backend-service ports. A flow reaching one of these
#: FROM a non-private address is the shape of a database exposed directly
#: to the internet, rather than sitting behind an application tier — a
#: segmentation failure a network operator would want to know about
#: regardless of whether the specific flow is malicious.
DATABASE_SERVICE_PORTS: frozenset[int] = frozenset(
    {
        1433,  # MSSQL
        1521,  # Oracle
        3306,  # MySQL / MariaDB
        5432,  # PostgreSQL
        5984,  # CouchDB
        6379,  # Redis — no auth by default
        9200,  # Elasticsearch — no auth by default
        11211,  # Memcached
        27017,  # MongoDB
    }
)

#: IANA's boundary between "well-known" (system/registered-ish, < 1024)
#: and "high" ports. Standard networking convention, not a project-tunable
#: threshold, hence a module constant rather than a `BACKEND_SETTINGS`
#: field.
WELL_KNOWN_PORT_BOUNDARY = 1024


def _is_known_bad_address(flow: FlowFeatures) -> bool:
    return (
        flow.source_ip in KNOWN_BAD_ADDRESSES
        or flow.destination_ip in KNOWN_BAD_ADDRESSES
    )


def _is_small_payload_high_port(flow: FlowFeatures) -> bool:
    """C2-shaped: little data, moving to a non-standard port.

    Anchored to `docs/DETECTION_STUDY.md`'s measured finding that Bot C2
    beacons in this corpus carry a median of 6 bytes versus ~70 for benign
    traffic (also the rationale behind `beaconing_enabled`'s docstring in
    `backend/config.py`). `signature_small_payload_bytes` (default 64) sits
    with headroom above that 6-byte median and below the benign median, so
    it catches the C2 shape without also catching every ordinary short
    connection.
    """
    return (
        flow.bytes <= BACKEND_SETTINGS.signature_small_payload_bytes
        and flow.destination_port >= WELL_KNOWN_PORT_BOUNDARY
    )


def _is_scan_shaped(flow: FlowFeatures) -> bool:
    """Packets moved, but effectively no data — a completed handshake (or
    a probe that got a RST) with nothing behind it. `packets > 0` excludes
    the degenerate all-zero row; `signature_scan_max_bytes` (default 8) is
    intentionally tighter than the small-payload rule's 64-byte ceiling so
    this rule targets true zero-content probes rather than overlapping it
    entirely.
    """
    return (
        flow.packets > 0 and flow.bytes <= BACKEND_SETTINGS.signature_scan_max_bytes
    )


def _is_high_risk_admin_port(flow: FlowFeatures) -> bool:
    return flow.destination_port in HIGH_RISK_ADMIN_PORTS


def _is_external_to_database_port(flow: FlowFeatures) -> bool:
    """A non-private source address reaching a database/backend port.

    `ipaddress.ip_address(...).is_private` is used rather than the
    project's `EXTERNAL_THREAT_IPS` list on purpose: that list is three
    known-bad addresses (rule 1 already covers it), while "is this address
    even routable as internal" is a general property of the address
    itself, so this rule catches exposure to addresses that were never on
    anyone's watch-list, not just the three that are. Malformed or
    non-IPv4/IPv6 address strings (synthetic test fixtures, an
    unresolved-asset placeholder) fail the parse and are treated as "not
    applicable" rather than raising — this is a best-effort metadata rule,
    not a validator.
    """
    try:
        is_private = ipaddress.ip_address(flow.source_ip).is_private
    except ValueError:
        return False
    return (not is_private) and flow.destination_port in DATABASE_SERVICE_PORTS


# ---------------------------------------------------------------------------
# SignatureRule — one declarative rule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignatureRule:
    """One declarative metadata rule.

    Parameters
    ----------
    rule_id:
        Stable identifier (`"AEGIS-SIG-00N"`). Persisted in
        `DetectorVerdict.evidence`, so renaming an existing rule_id breaks
        the audit trail of any already-persisted verdict — add a new rule
        instead of renumbering.
    title:
        Short human-readable label for an operator's eye.
    description:
        What the rule matches and WHY that shape is suspicious on metadata
        alone. Must not claim or imply payload inspection.
    confidence:
        This rule's own P(malicious) when it matches, in [0, 1]. Not a
        detector-level calibration — a per-rule judgement call, defended in
        `description`. Deliberately conservative: see each built-in rule's
        docstring in this module for the specific justification.
    predicate:
        `(FlowFeatures) -> bool`. Stateless and single-flow — no rule here
        may read anything but the one flow passed to it.
    """

    rule_id: str
    title: str
    description: str
    confidence: float
    predicate: Callable[[FlowFeatures], bool]

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"{self.rule_id}: confidence must be in [0, 1], got "
                f"{self.confidence!r}."
            )
        if not self.rule_id:
            raise ValueError("SignatureRule.rule_id must be non-empty.")
        if not self.description:
            raise ValueError(f"{self.rule_id}: description must be non-empty.")

    def matches(self, flow: FlowFeatures) -> bool:
        return bool(self.predicate(flow))


# ---------------------------------------------------------------------------
# Built-in rule set
# ---------------------------------------------------------------------------
# Five rules. Each is defensible on flow metadata ALONE — no rule here
# assumes anything about packet contents. Confidences are per-rule
# judgement calls, justified in each entry below; they are deliberately
# NOT uniform, because a known-bad-address hit and a high-numbered-port hit
# do not deserve equal trust.

DEFAULT_RULES: tuple[SignatureRule, ...] = (
    SignatureRule(
        rule_id="AEGIS-SIG-001",
        title="Known-bad external address",
        description=(
            "Flow touches an address on the curated external threat list "
            "(src/config.py EXTERNAL_THREAT_IPS), as source or "
            "destination. Confidence 0.90, not 1.0: the list is a small, "
            "hand-curated set of addresses, not a live, continuously "
            "updated threat-intel feed, so a match is strong but not "
            "infallible evidence — an address can be reused or "
            "decommissioned after the list was written."
        ),
        confidence=0.90,
        predicate=_is_known_bad_address,
    ),
    SignatureRule(
        rule_id="AEGIS-SIG-002",
        title="Small-payload flow to a high port (C2-shaped)",
        description=(
            "Flow carries at most signature_small_payload_bytes of data "
            "to a destination port >= 1024. Shape-matches the measured "
            "Bot C2 median of 6 bytes (docs/DETECTION_STUDY.md) versus "
            "~70 for benign traffic. Confidence 0.50: this is a common "
            "shape for entirely benign short-lived connections too "
            "(keepalives, health checks, DNS-adjacent lookups on high "
            "ports), so on metadata alone this is a real but modest "
            "signal, not proof."
        ),
        confidence=0.50,
        predicate=_is_small_payload_high_port,
    ),
    SignatureRule(
        rule_id="AEGIS-SIG-003",
        title="Scan-shaped flow (packets, no data)",
        description=(
            "Flow has packets > 0 but at most signature_scan_max_bytes of "
            "data — a connection attempt that completed a handshake (or "
            "drew a reset) and moved nothing. Confidence 0.55: tighter "
            "byte ceiling than rule 002 makes this a sharper shape, but "
            "it is still just as consistent with a benign client aborting "
            "a connection early as with a port probe."
        ),
        confidence=0.55,
        predicate=_is_scan_shaped,
    ),
    SignatureRule(
        rule_id="AEGIS-SIG-004",
        title="High-risk administrative/legacy service port",
        description=(
            "Destination port is telnet/FTP/NetBIOS/SMB/RDP "
            "(HIGH_RISK_ADMIN_PORTS) — protocols with a long history of "
            "cleartext credentials or remote-code-execution exposure. "
            "Confidence 0.40, the lowest of the built-in rules: these "
            "ports carry entirely legitimate traffic on plenty of "
            "internal networks (an active-directory estate, a NAS), so a "
            "port number alone is weak evidence without knowing whether "
            "the network segment is meant to expose it."
        ),
        confidence=0.40,
        predicate=_is_high_risk_admin_port,
    ),
    SignatureRule(
        rule_id="AEGIS-SIG-005",
        title="External address reaching a database/backend port",
        description=(
            "Source address is not a private/internal address (stdlib "
            "ipaddress.is_private — RFC 1918/4193 private ranges plus "
            "other non-globally-routable reserved ranges) and destination "
            "port is "
            "a common database/backend service port (DATABASE_SERVICE_"
            "PORTS). Confidence 0.65: a database reachable directly from "
            "outside the internal network is a segmentation failure "
            "regardless of who is connecting, which makes this shape more "
            "specific than a bare port check, but it still cannot "
            "distinguish a legitimate remote administrator from an "
            "attacker on metadata alone."
        ),
        confidence=0.65,
        predicate=_is_external_to_database_port,
    ),
)


# ---------------------------------------------------------------------------
# SignatureEngine — the FlowDetector implementation
# ---------------------------------------------------------------------------


@dataclass
class _Match:
    rule_id: str
    title: str
    confidence: float


class SignatureEngine:
    """Stateless, declarative rule engine satisfying the `FlowDetector`
    protocol (`backend/detection/contracts.py`).

    Evaluates every rule against every flow independently — no cross-flow
    state, unlike `BeaconingDetector`. Constructed with an optional `rules`
    override (this codebase's optional-override convention, CLAUDE.md
    section 5) so tests can inject a synthetic rule set without touching
    `DEFAULT_RULES`, and so a future caller can run a reduced or extended
    rule set without subclassing.
    """

    name: str = DETECTOR_SIGNATURE

    def __init__(self, rules: Optional[Sequence[SignatureRule]] = None) -> None:
        self._rules: tuple[SignatureRule, ...] = (
            tuple(rules) if rules is not None else DEFAULT_RULES
        )

    @property
    def rules(self) -> tuple[SignatureRule, ...]:
        """The active rule set, for introspection (`/api/stats`-style
        surfaces, tests). Read-only — replace the whole engine to change
        rules, don't mutate this tuple's caller-visible copy."""
        return self._rules

    def examine(self, flows: Sequence[FlowFeatures]) -> list[DetectorVerdict]:
        """Return exactly one verdict per input flow, in input order (see
        `FlowDetector.examine`'s docstring on why order is contractual)."""
        return [self._examine_one(flow) for flow in flows]

    def _examine_one(self, flow: FlowFeatures) -> DetectorVerdict:
        matched: list[_Match] = [
            _Match(rule.rule_id, rule.title, rule.confidence)
            for rule in self._rules
            if rule.matches(flow)
        ]

        fired = bool(matched)
        # Max, not sum — see the module docstring's "Scoring: max, not
        # sum" section for why several weak matches must not compound
        # into a false certainty.
        calibrated_score = max((m.confidence for m in matched), default=0.0)

        return DetectorVerdict(
            detector=self.name,
            fired=fired,
            calibrated_score=calibrated_score,
            reliability=BACKEND_SETTINGS.hybrid_weight_signature,
            certainty=Certainty.HEURISTIC,
            raw_score=float(len(matched)),
            evidence={
                "channel": "signature",
                "matched_rules": [
                    {"rule_id": m.rule_id, "title": m.title} for m in matched
                ],
                "caveat": (
                    "rules match flow METADATA only (ports/protocol/"
                    "byte-shape/addresses) — CIC-IDS2017 carries no packet "
                    "payloads, so this is not payload inspection"
                ),
            },
        )
