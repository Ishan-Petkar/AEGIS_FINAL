# Config module for AEGIS Cyber Risk Console

# ==============================================================================
# PHASE 2: HONEYTOKEN CREDENTIAL DECLARATIONS
#
# Each Purdue-level zone gateway is seeded with exactly one fake credential:
# a username/password pair (or API key) with ZERO legitimate use anywhere in
# the entire system. Any use of this credential is unambiguous compromise —
# no background legitimate traffic, no coincidence, no false positives by
# construction.
#
# Gateway zones are named Gateway_L0 through Gateway_L5 per Purdue level.
# ==============================================================================
HONEYTOKEN_CREDENTIALS = {
    # Level 0: Physical sensors / actuators (PLCs, RTUs, field devices)
    "Gateway_L0": {
        "credential_id": "HT-L0-SENSOR",
        "username": "admin_field_maintenance_l0",
        "password": "8e4d9f2c-a1b5-4e7a-9c3d-5f8a2b6e1d7c",  # UUID format, never used
        "emulated_protocol": "Modbus TCP",  # ICS-layer protocol
        "description": "Field device supervisory access — never legitimately used over network",
        "criticality": 0.0,  # Fake credential doesn't itself have criticality
        "last_activated": None,  # Will be set when/if triggered
    },
    # Level 1: Supervisory control (SCADA/HMI, PLC supervisors)
    "Gateway_L1": {
        "credential_id": "HT-L1-SCADA",
        "username": "scada_admin_remote_v2",
        "password": "c7b3e9a2-4f8d-42c1-b6a9-e8d5c2f1a9b4",  # UUID format, never used
        "emulated_protocol": "OPC-DA",  # SCADA protocol
        "description": "SCADA/HMI supervisory access — no remote admin permitted",
        "criticality": 0.0,
        "last_activated": None,
    },
    # Level 2: Operations supervisory (secondary SCADA, sensor integration)
    "Gateway_L2": {
        "credential_id": "HT-L2-OPS",
        "username": "operations_console_fallback",
        "password": "a6d1f4c8-9e2b-4a7d-c5f2-e3a9b1d6f8c2",  # UUID format, never used
        "emulated_protocol": "OPC-UA",  # Modern industrial protocol
        "description": "Operations console fallback — reserved for emergencies only",
        "criticality": 0.0,
        "last_activated": None,
    },
    # Level 3: Site operations (local IT, building mgmt, shift supervisors)
    "Gateway_L3": {
        "credential_id": "HT-L3-SITOPS",
        "username": "site_shift_supervisor_backup",
        "password": "f2e8a4b9-d1c6-4f3e-a5b7-c9e2d1a8f6b3",  # UUID format, never used
        "emulated_protocol": "HTTPS",  # Enterprise protocol
        "description": "Shift supervisor override — never legitimately accessed",
        "criticality": 0.0,
        "last_activated": None,
    },
    # Level 4: Business logistics / Enterprise IT (finance, HR, business apps)
    "Gateway_L4": {
        "credential_id": "HT-L4-BIZOPS",
        "username": "business_operations_readonly_L4",
        "password": "e9c2f7d1-a4b8-4c6e-f3a9-b5d2c8e1a6f4",  # UUID format, never used
        "emulated_protocol": "LDAP",  # Directory protocol
        "description": "Business operations read-only access — all writes forbidden",
        "criticality": 0.0,
        "last_activated": None,
    },
    # Level 5: External / Enterprise-facing (partners, external APIs, public interfaces)
    "Gateway_L5": {
        "credential_id": "HT-L5-EXTERNAL",
        "username": "external_partner_api_key_reserved",
        "password": "b1a9e4c7-f2d6-4b3a-c8e5-d1f4a2b9c6e3",  # UUID format, never used
        "emulated_protocol": "OAuth2",  # External API protocol
        "description": "External partner API credential — no legitimate external use",
        "criticality": 0.0,
        "last_activated": None,
    },
}

# Asset Types classified as Financial
FINANCIAL_TYPES = [
    "Financial Transaction System",
    "External Financial Interface",
    "Financial Market System",
    "Social Safety Net"
]

# Baseline Smart City & Financial Assets
#
# purdue_level follows the ISA-95 / Purdue Enterprise Reference Architecture
# used by MITRE ATT&CK for ICS: 0=physical process/sensors, 1=basic control
# (PLCs), 2=supervisory control (SCADA/HMI), 3=site operations, 4=business
# logistics/enterprise IT, 5=external/enterprise-facing interfaces. It drives
# which Purdue-zone gateway (graph_manager.py) an asset sits behind.
SMART_CITY_ASSETS = [
    {"ip": "10.0.1.10", "asset_name": "Traffic_Cam_1", "type": "IoT Sensor", "criticality": 0.2, "purdue_level": 0},
    {"ip": "10.0.1.11", "asset_name": "Traffic_Cam_2", "type": "IoT Sensor", "criticality": 0.2, "purdue_level": 0},
    {"ip": "10.0.1.12", "asset_name": "Traffic_Controller", "type": "ICS Controller", "criticality": 0.9, "purdue_level": 1},
    {"ip": "10.0.1.13", "asset_name": "Power_Substation", "type": "Critical Infra", "criticality": 1.0, "purdue_level": 1},
    {"ip": "10.0.1.14", "asset_name": "Emergency_Route_System", "type": "Public Safety API", "criticality": 0.95, "purdue_level": 3},
    {"ip": "10.0.1.15", "asset_name": "Citizen_Portal", "type": "Public Web Service", "criticality": 0.4, "purdue_level": 4},
    {"ip": "10.0.1.16", "asset_name": "SCADA_Historian", "type": "Data Logging Server", "criticality": 0.6, "purdue_level": 2},

    # --- FINANCIAL ASSETS ---
    {"ip": "10.0.1.20", "asset_name": "City_Payment_Gateway", "type": "Financial Transaction System", "criticality": 0.95, "purdue_level": 4},
    {"ip": "10.0.1.21", "asset_name": "Bank_Partner_API", "type": "External Financial Interface", "criticality": 0.85, "purdue_level": 5},
    {"ip": "10.0.1.22", "asset_name": "Municipal_Bond_Platform", "type": "Financial Market System", "criticality": 0.75, "purdue_level": 4},
    {"ip": "10.0.1.23", "asset_name": "Social_Welfare_System", "type": "Social Safety Net", "criticality": 0.90, "purdue_level": 4}
]

# Known Threat Actors (External IPs)
EXTERNAL_THREAT_IPS = [
    {"ip": "185.220.101.5", "asset_name": "External_185.220.101.5", "type": "External IP", "criticality": 0.0, "is_internal": False},
    {"ip": "45.227.254.12", "asset_name": "External_45.227.254.12", "type": "External IP", "criticality": 0.0, "is_internal": False},
    {"ip": "198.51.100.42", "asset_name": "External_198.51.100.42", "type": "External IP", "criticality": 0.0, "is_internal": False}
]

# Cascading Impact Dependency Graph Definition
# Each edge is a dict with:
#   src, tgt         — asset names
#   edge_type        — semantic type (depends_on | backed_up_by | shares_provider |
#                      controls | communicates_with | pays_through)
#   prob             — base propagation probability [0, 1]
#   source           — who/what defined this edge
#   owner            — responsible team/system
#   rationale        — why this probability/criticality
#   confidence       — how certain is this edge itself [0, 1]
#   last_reviewed    — ISO date of last review
#   provider_id      — (optional) shared failure-mode identifier for shares_provider edges
DEPENDENCY_GRAPH = [
    {
        "src": "Traffic_Cam_1", "tgt": "Traffic_Controller",
        "edge_type": "communicates_with", "prob": 0.9,
        "source": "network_topology_scan", "owner": "infra_ops",
        "rationale": "Camera feeds are primary input to the controller",
        "confidence": 0.95, "last_reviewed": "2026-06-01",
    },
    {
        "src": "Traffic_Cam_2", "tgt": "Traffic_Controller",
        "edge_type": "communicates_with", "prob": 0.9,
        "source": "network_topology_scan", "owner": "infra_ops",
        "rationale": "Camera feeds are primary input to the controller",
        "confidence": 0.95, "last_reviewed": "2026-06-01",
    },
    {
        "src": "Traffic_Controller", "tgt": "Power_Substation",
        "edge_type": "depends_on", "prob": 0.95,
        "source": "engineering_review", "owner": "power_team",
        "rationale": "Controller relies on substation for power; no local UPS",
        "confidence": 0.9, "last_reviewed": "2026-05-15",
    },
    {
        "src": "Traffic_Controller", "tgt": "Emergency_Route_System",
        "edge_type": "controls", "prob": 0.5,
        "source": "engineering_review", "owner": "public_safety",
        "rationale": "Controller pushes route advisories; emergency system can operate independently",
        "confidence": 0.85, "last_reviewed": "2026-05-15",
    },
    {
        "src": "Power_Substation", "tgt": "City_Grid",
        "edge_type": "depends_on", "prob": 0.95,
        "source": "engineering_review", "owner": "power_team",
        "rationale": "Substation feeds the city grid directly",
        "confidence": 0.95, "last_reviewed": "2026-05-15",
    },
    {
        "src": "SCADA_Historian", "tgt": "Traffic_Controller",
        "edge_type": "communicates_with", "prob": 0.2,
        "source": "network_topology_scan", "owner": "infra_ops",
        "rationale": "Historian collects logs; compromise unlikely to affect controller operation",
        "confidence": 0.8, "last_reviewed": "2026-06-01",
    },
    {
        "src": "SCADA_Historian", "tgt": "Traffic_Cam_1",
        "edge_type": "communicates_with", "prob": 0.2,
        "source": "network_topology_scan", "owner": "infra_ops",
        "rationale": "Log collection only",
        "confidence": 0.8, "last_reviewed": "2026-06-01",
    },
    {
        "src": "SCADA_Historian", "tgt": "Traffic_Cam_2",
        "edge_type": "communicates_with", "prob": 0.2,
        "source": "network_topology_scan", "owner": "infra_ops",
        "rationale": "Log collection only",
        "confidence": 0.8, "last_reviewed": "2026-06-01",
    },

    # --- FINANCIAL SECTOR DEPENDENCIES ---
    {
        "src": "Citizen_Portal", "tgt": "City_Payment_Gateway",
        "edge_type": "pays_through", "prob": 0.9,
        "source": "application_architecture", "owner": "fintech_team",
        "rationale": "Portal is primary citizen-facing payment entry point",
        "confidence": 0.95, "last_reviewed": "2026-06-10",
    },
    {
        "src": "City_Payment_Gateway", "tgt": "Bank_Partner_API",
        "edge_type": "pays_through", "prob": 0.95,
        "source": "application_architecture", "owner": "fintech_team",
        "rationale": "All settlements route through bank API",
        "confidence": 0.95, "last_reviewed": "2026-06-10",
    },
    {
        "src": "City_Payment_Gateway", "tgt": "Social_Welfare_System",
        "edge_type": "pays_through", "prob": 0.85,
        "source": "application_architecture", "owner": "fintech_team",
        "rationale": "Welfare disbursements flow through payment gateway",
        "confidence": 0.9, "last_reviewed": "2026-06-10",
    },
    {
        "src": "Municipal_Bond_Platform", "tgt": "Bank_Partner_API",
        "edge_type": "communicates_with", "prob": 0.7,
        "source": "application_architecture", "owner": "fintech_team",
        "rationale": "Market data feed; disruption degrades but doesn't halt operations",
        "confidence": 0.8, "last_reviewed": "2026-06-10",
    },
    {
        "src": "Traffic_Controller", "tgt": "City_Payment_Gateway",
        "edge_type": "communicates_with", "prob": 0.5,
        "source": "application_architecture", "owner": "infra_ops",
        "rationale": "Automated fine collection; can queue offline",
        "confidence": 0.75, "last_reviewed": "2026-06-10",
    },
    {
        "src": "Power_Substation", "tgt": "City_Payment_Gateway",
        "edge_type": "depends_on", "prob": 0.4,
        "source": "engineering_review", "owner": "power_team",
        "rationale": "Payment gateway has battery backup; partial dependency on utility power",
        "confidence": 0.7, "last_reviewed": "2026-06-10",
    },
]

