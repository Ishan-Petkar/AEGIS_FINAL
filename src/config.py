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
    {"ip": "10.0.1.10", "asset_name": "Traffic_Cam_1", "type": "IoT Sensor", "criticality": 0.2, "purdue_level": 0, "sector": "transport"},
    {"ip": "10.0.1.11", "asset_name": "Traffic_Cam_2", "type": "IoT Sensor", "criticality": 0.2, "purdue_level": 0, "sector": "transport"},
    {"ip": "10.0.1.12", "asset_name": "Traffic_Controller", "type": "ICS Controller", "criticality": 0.9, "purdue_level": 1, "sector": "transport"},
    {"ip": "10.0.1.13", "asset_name": "Power_Substation", "type": "Critical Infra", "criticality": 1.0, "purdue_level": 1, "sector": "energy"},
    {"ip": "10.0.1.14", "asset_name": "Emergency_Route_System", "type": "Public Safety API", "criticality": 0.95, "purdue_level": 3, "sector": "public_safety"},
    {"ip": "10.0.1.15", "asset_name": "Citizen_Portal", "type": "Public Web Service", "criticality": 0.4, "purdue_level": 4, "sector": "civic"},
    {"ip": "10.0.1.16", "asset_name": "SCADA_Historian", "type": "Data Logging Server", "criticality": 0.6, "purdue_level": 2, "sector": "monitoring"},

    # --- FINANCIAL ASSETS ---
    {"ip": "10.0.1.20", "asset_name": "City_Payment_Gateway", "type": "Financial Transaction System", "criticality": 0.95, "purdue_level": 4, "sector": "finance"},
    {"ip": "10.0.1.21", "asset_name": "Bank_Partner_API", "type": "External Financial Interface", "criticality": 0.85, "purdue_level": 5, "sector": "finance"},
    {"ip": "10.0.1.22", "asset_name": "Municipal_Bond_Platform", "type": "Financial Market System", "criticality": 0.75, "purdue_level": 4, "sector": "finance"},
    {"ip": "10.0.1.23", "asset_name": "Social_Welfare_System", "type": "Social Safety Net", "criticality": 0.90, "purdue_level": 4, "sector": "finance"},

    # --- PHASE 5 (TICKET #16): CITY SCALE-UP — ADDITIVE ONLY, D-C1 ---
    # Every asset above this line is untouched: exact name/ip/criticality/
    # purdue_level, per docs/PHASE5_CITY_SCALE_PLAN.md §1. Everything below
    # is new. New assets use 10.0.1.30-63 — comfortably clear of the
    # existing .10-.23 block and of tests/test_asset_registry.py's
    # 10.0.1.18 -> SCADA_Historian proximity fixture (nearest new IP is
    # 12 hosts away, outside the 5-host proximity window).
    #
    # Guardrail (do not violate without re-reading the plan): no asset at
    # purdue_level 0 or 2 may reach SETTINGS.gateway.criticality_threshold
    # (0.85). tests/test_deception_tripwire.py::test_gateway_with_no_guarded_asset_still_nonzero
    # and ::test_gateway_target_assets_matches_dependency_graph_reality pin
    # "Gateway_L0 guards nothing" as an engine-behavior contract, and
    # tests/test_backend_models.py::test_exactly_four_gateways_seeded pins
    # the gateway set to exactly {L1, L3, L4, L5}. Every new protected
    # (>=0.85) asset below sits at purdue_level 1, 3, or 4 — the same zones
    # the existing 6 protected assets already occupy — so no new gateway
    # zone is created.

    # Operations (hub) — see DEPENDENCY_GRAPH below for D-C2 edge direction.
    {"ip": "10.0.1.30", "asset_name": "City_Operations_Center", "type": "Security Operations Center", "criticality": 0.98, "purdue_level": 3, "sector": "operations"},

    # Energy
    {"ip": "10.0.1.31", "asset_name": "Power_Substation_Beta", "type": "Critical Infra", "criticality": 0.75, "purdue_level": 1, "sector": "energy"},
    {"ip": "10.0.1.32", "asset_name": "Solar_Array_West", "type": "IoT Sensor", "criticality": 0.25, "purdue_level": 0, "sector": "energy"},
    {"ip": "10.0.1.33", "asset_name": "Battery_Storage_Facility", "type": "Critical Infra", "criticality": 0.6, "purdue_level": 1, "sector": "energy"},
    {"ip": "10.0.1.34", "asset_name": "Grid_SCADA", "type": "ICS Controller", "criticality": 0.65, "purdue_level": 2, "sector": "energy"},

    # Water
    {"ip": "10.0.1.35", "asset_name": "Water_Treatment_Plant", "type": "Critical Infra", "criticality": 0.85, "purdue_level": 1, "sector": "water"},
    {"ip": "10.0.1.36", "asset_name": "Water_Pump_Station", "type": "ICS Controller", "criticality": 0.55, "purdue_level": 1, "sector": "water"},
    {"ip": "10.0.1.37", "asset_name": "Water_Quality_Sensors", "type": "IoT Sensor", "criticality": 0.2, "purdue_level": 0, "sector": "water"},
    {"ip": "10.0.1.38", "asset_name": "Wastewater_Facility", "type": "Critical Infra", "criticality": 0.5, "purdue_level": 1, "sector": "water"},

    # Transport
    {"ip": "10.0.1.39", "asset_name": "Metro_Signalling_System", "type": "ICS Controller", "criticality": 0.8, "purdue_level": 1, "sector": "transport"},
    {"ip": "10.0.1.40", "asset_name": "Bridge_Sensors", "type": "IoT Sensor", "criticality": 0.3, "purdue_level": 0, "sector": "transport"},
    {"ip": "10.0.1.41", "asset_name": "EV_Charging_Network", "type": "IoT Network", "criticality": 0.35, "purdue_level": 2, "sector": "transport"},

    # Public safety
    {"ip": "10.0.1.42", "asset_name": "Fire_Dispatch_System", "type": "Public Safety API", "criticality": 0.9, "purdue_level": 3, "sector": "public_safety"},
    {"ip": "10.0.1.43", "asset_name": "Police_CAD_System", "type": "Public Safety API", "criticality": 0.88, "purdue_level": 3, "sector": "public_safety"},
    {"ip": "10.0.1.44", "asset_name": "EMS_Dispatch", "type": "Public Safety API", "criticality": 0.85, "purdue_level": 3, "sector": "public_safety"},
    {"ip": "10.0.1.45", "asset_name": "Siren_Network", "type": "IoT Network", "criticality": 0.4, "purdue_level": 1, "sector": "public_safety"},

    # Health
    {"ip": "10.0.1.46", "asset_name": "Hospital_Network", "type": "Health System", "criticality": 0.8, "purdue_level": 4, "sector": "health"},
    {"ip": "10.0.1.47", "asset_name": "Ambulance_Telemetry", "type": "IoT Sensor", "criticality": 0.3, "purdue_level": 2, "sector": "health"},
    {"ip": "10.0.1.48", "asset_name": "Health_Registry", "type": "Data Logging Server", "criticality": 0.55, "purdue_level": 4, "sector": "health"},

    # Telecom / IT
    {"ip": "10.0.1.49", "asset_name": "Fiber_Backbone", "type": "Network Infrastructure", "criticality": 0.8, "purdue_level": 2, "sector": "telecom"},
    {"ip": "10.0.1.50", "asset_name": "Municipal_DNS", "type": "Network Infrastructure", "criticality": 0.5, "purdue_level": 2, "sector": "telecom"},
    {"ip": "10.0.1.51", "asset_name": "City_Data_Center", "type": "Data Logging Server", "criticality": 0.9, "purdue_level": 4, "sector": "telecom"},
    {"ip": "10.0.1.52", "asset_name": "Identity_Provider", "type": "Enterprise IT", "criticality": 0.88, "purdue_level": 4, "sector": "telecom"},
    {"ip": "10.0.1.53", "asset_name": "Backup_Vault", "type": "Data Logging Server", "criticality": 0.45, "purdue_level": 4, "sector": "telecom"},

    # Finance
    {"ip": "10.0.1.54", "asset_name": "Tax_Collection_System", "type": "Financial Transaction System", "criticality": 0.65, "purdue_level": 4, "sector": "finance"},
    {"ip": "10.0.1.55", "asset_name": "Payroll_System", "type": "Financial Transaction System", "criticality": 0.5, "purdue_level": 4, "sector": "finance"},

    # Civic
    {"ip": "10.0.1.56", "asset_name": "Permits_System", "type": "Public Web Service", "criticality": 0.35, "purdue_level": 4, "sector": "civic"},
    {"ip": "10.0.1.57", "asset_name": "Records_Archive", "type": "Data Logging Server", "criticality": 0.3, "purdue_level": 4, "sector": "civic"},
    {"ip": "10.0.1.58", "asset_name": "Voting_Infrastructure", "type": "Election System", "criticality": 0.92, "purdue_level": 4, "sector": "civic"},

    # Environment
    {"ip": "10.0.1.59", "asset_name": "Air_Quality_Sensors", "type": "IoT Sensor", "criticality": 0.15, "purdue_level": 0, "sector": "environment"},
    {"ip": "10.0.1.60", "asset_name": "Flood_Sensors", "type": "IoT Sensor", "criticality": 0.2, "purdue_level": 0, "sector": "environment"},
    {"ip": "10.0.1.61", "asset_name": "Waste_Management_System", "type": "ICS Controller", "criticality": 0.3, "purdue_level": 2, "sector": "environment"},

    # Monitoring
    {"ip": "10.0.1.62", "asset_name": "Log_Aggregator", "type": "Data Logging Server", "criticality": 0.4, "purdue_level": 2, "sector": "monitoring"},
    {"ip": "10.0.1.63", "asset_name": "Threat_Intel_Feed", "type": "Data Logging Server", "criticality": 0.3, "purdue_level": 4, "sector": "monitoring"},
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

    # --- PHASE 5 (TICKET #16): CITY SCALE-UP EDGES — ADDITIVE ONLY, D-C1 ---
    # No edge above this line is modified. No edge below targets one of the
    # 6 pre-existing protected assets (Traffic_Controller, Power_Substation,
    # Emergency_Route_System, City_Payment_Gateway, Bank_Partner_API,
    # Social_Welfare_System) — several of those appear here only as a
    # *source*, which does not touch graph_manager's gateway union math for
    # them. This preserves tests/test_graph_manager.py's exact regression
    # pin on Gateway_L4's union probability for City_Payment_Gateway
    # (three original edges -> 0.97) unmodified.

    # --- D-C2: the hub. Outbound controls/communicates_with = "if the SOC
    # is compromised, its credentials/management access can reach these
    # sector controllers" (the direction that makes it the most
    # consequential node). A handful of separate, LOW-probability inbound
    # telemetry edges model sector systems reporting status to the SOC
    # dashboard, deliberately kept weak so a compromised sensor cannot
    # trivially pivot to owning the hub. ---
    {
        "src": "City_Operations_Center", "tgt": "Grid_SCADA",
        "edge_type": "controls", "prob": 0.6,
        "source": "engineering_review", "owner": "soc_team",
        "rationale": "SOC holds administrative override access to the grid SCADA console",
        "confidence": 0.85, "last_reviewed": "2026-08-26",
    },
    {
        "src": "City_Operations_Center", "tgt": "Water_Pump_Station",
        "edge_type": "controls", "prob": 0.55,
        "source": "engineering_review", "owner": "soc_team",
        "rationale": "SOC remote-ops console can command pump station setpoints during incidents",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },
    {
        "src": "City_Operations_Center", "tgt": "Metro_Signalling_System",
        "edge_type": "controls", "prob": 0.6,
        "source": "engineering_review", "owner": "soc_team",
        "rationale": "SOC holds supervisory override on metro signalling during service incidents",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },
    {
        "src": "City_Operations_Center", "tgt": "Fire_Dispatch_System",
        "edge_type": "controls", "prob": 0.5,
        "source": "engineering_review", "owner": "soc_team",
        "rationale": "SOC cross-agency console can push overrides into fire dispatch during major incidents",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },
    {
        "src": "City_Operations_Center", "tgt": "Hospital_Network",
        "edge_type": "communicates_with", "prob": 0.4,
        "source": "engineering_review", "owner": "soc_team",
        "rationale": "SOC shares mass-casualty and infrastructure-status alerts with hospital ops",
        "confidence": 0.7, "last_reviewed": "2026-08-26",
    },
    {
        "src": "City_Operations_Center", "tgt": "City_Data_Center",
        "edge_type": "controls", "prob": 0.65,
        "source": "engineering_review", "owner": "soc_team",
        "rationale": "SOC administrative access reaches the data center's management plane",
        "confidence": 0.85, "last_reviewed": "2026-08-26",
    },
    {
        "src": "City_Operations_Center", "tgt": "Identity_Provider",
        "edge_type": "controls", "prob": 0.7,
        "source": "engineering_review", "owner": "soc_team",
        "rationale": "SOC operators hold elevated IAM admin rights over the identity provider",
        "confidence": 0.85, "last_reviewed": "2026-08-26",
    },
    {
        "src": "City_Operations_Center", "tgt": "Log_Aggregator",
        "edge_type": "communicates_with", "prob": 0.5,
        "source": "engineering_review", "owner": "soc_team",
        "rationale": "SOC dashboard queries the log aggregator directly for triage",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },
    {
        "src": "City_Operations_Center", "tgt": "Waste_Management_System",
        "edge_type": "controls", "prob": 0.3,
        "source": "engineering_review", "owner": "soc_team",
        "rationale": "Lower-priority municipal system; SOC console access exists but is rarely used",
        "confidence": 0.7, "last_reviewed": "2026-08-26",
    },
    {
        "src": "City_Operations_Center", "tgt": "Voting_Infrastructure",
        "edge_type": "controls", "prob": 0.3,
        "source": "engineering_review", "owner": "soc_team",
        "rationale": "Municipal IT provisions election-system infra outside election windows; access is narrow and audited",
        "confidence": 0.7, "last_reviewed": "2026-08-26",
    },
    {
        "src": "SCADA_Historian", "tgt": "City_Operations_Center",
        "edge_type": "communicates_with", "prob": 0.15,
        "source": "network_topology_scan", "owner": "infra_ops",
        "rationale": "Historian shares log exports with the SOC dashboard; low-value telemetry only",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Grid_SCADA", "tgt": "City_Operations_Center",
        "edge_type": "communicates_with", "prob": 0.2,
        "source": "network_topology_scan", "owner": "power_team",
        "rationale": "Grid status telemetry feeds the SOC dashboard; a sensor breach should not trivially own the SOC",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Metro_Signalling_System", "tgt": "City_Operations_Center",
        "edge_type": "communicates_with", "prob": 0.15,
        "source": "network_topology_scan", "owner": "transport_team",
        "rationale": "Signalling health telemetry feeds the SOC dashboard; advisory only",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Log_Aggregator", "tgt": "City_Operations_Center",
        "edge_type": "communicates_with", "prob": 0.2,
        "source": "network_topology_scan", "owner": "infra_ops",
        "rationale": "Aggregated log stream feeds the SOC dashboard; log-collection only",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },

    # --- Energy sector ---
    {
        "src": "Solar_Array_West", "tgt": "Power_Substation_Beta",
        "edge_type": "depends_on", "prob": 0.4,
        "source": "engineering_review", "owner": "power_team",
        "rationale": "Compromised inverter control could destabilise substation beta's local balancing",
        "confidence": 0.7, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Battery_Storage_Facility", "tgt": "Power_Substation_Beta",
        "edge_type": "depends_on", "prob": 0.6,
        "source": "engineering_review", "owner": "power_team",
        "rationale": "Battery storage buffers substation beta; a compromised BMS could trigger unsafe discharge",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Grid_SCADA", "tgt": "Power_Substation_Beta",
        "edge_type": "controls", "prob": 0.7,
        "source": "engineering_review", "owner": "power_team",
        "rationale": "SCADA directly commands substation beta breakers and setpoints",
        "confidence": 0.85, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Power_Substation_Beta", "tgt": "City_Grid",
        "edge_type": "depends_on", "prob": 0.9,
        "source": "engineering_review", "owner": "power_team",
        "rationale": "Beta substation feeds the shared city grid, mirroring the primary substation's dependency",
        "confidence": 0.9, "last_reviewed": "2026-08-26",
    },

    # --- Water sector ---
    {
        "src": "Water_Quality_Sensors", "tgt": "Water_Treatment_Plant",
        "edge_type": "communicates_with", "prob": 0.5,
        "source": "network_topology_scan", "owner": "water_team",
        "rationale": "Quality sensors feed the plant's chemical dosing control loop directly",
        "confidence": 0.85, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Water_Pump_Station", "tgt": "Water_Treatment_Plant",
        "edge_type": "depends_on", "prob": 0.7,
        "source": "engineering_review", "owner": "water_team",
        "rationale": "Pump station supplies the raw-water intake the treatment plant depends on",
        "confidence": 0.85, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Water_Treatment_Plant", "tgt": "Wastewater_Facility",
        "edge_type": "communicates_with", "prob": 0.3,
        "source": "network_topology_scan", "owner": "water_team",
        "rationale": "Shared SCADA network segment; advisory correlation, not an operational dependency",
        "confidence": 0.7, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Power_Substation", "tgt": "Water_Treatment_Plant",
        "edge_type": "depends_on", "prob": 0.4,
        "source": "engineering_review", "owner": "power_team",
        "rationale": "Treatment plant has generator backup; utility power is still a partial dependency",
        "confidence": 0.7, "last_reviewed": "2026-08-26",
    },

    # --- Transport sector ---
    {
        "src": "Bridge_Sensors", "tgt": "Metro_Signalling_System",
        "edge_type": "communicates_with", "prob": 0.3,
        "source": "network_topology_scan", "owner": "transport_team",
        "rationale": "Bridge structural sensors share the transport ops network with metro signalling",
        "confidence": 0.7, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Traffic_Controller", "tgt": "Metro_Signalling_System",
        "edge_type": "controls", "prob": 0.3,
        "source": "engineering_review", "owner": "transport_team",
        "rationale": "Shared transport control room issues cross-system incident advisories to metro signalling",
        "confidence": 0.7, "last_reviewed": "2026-08-26",
    },
    {
        "src": "EV_Charging_Network", "tgt": "Power_Substation_Beta",
        "edge_type": "depends_on", "prob": 0.45,
        "source": "engineering_review", "owner": "power_team",
        "rationale": "Charging stations draw from substation beta's local distribution segment",
        "confidence": 0.7, "last_reviewed": "2026-08-26",
    },

    # --- Public safety sector ---
    {
        "src": "Siren_Network", "tgt": "Fire_Dispatch_System",
        "edge_type": "communicates_with", "prob": 0.4,
        "source": "engineering_review", "owner": "public_safety",
        "rationale": "Siren activation is triggered directly by the fire dispatch console",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Fire_Dispatch_System", "tgt": "EMS_Dispatch",
        "edge_type": "communicates_with", "prob": 0.5,
        "source": "engineering_review", "owner": "public_safety",
        "rationale": "Shared CAD backend correlates fire and EMS incidents in real time",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Police_CAD_System", "tgt": "EMS_Dispatch",
        "edge_type": "communicates_with", "prob": 0.45,
        "source": "engineering_review", "owner": "public_safety",
        "rationale": "Joint dispatch coordination during multi-agency incidents",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Emergency_Route_System", "tgt": "Police_CAD_System",
        "edge_type": "communicates_with", "prob": 0.3,
        "source": "engineering_review", "owner": "public_safety",
        "rationale": "Route advisories are shared with police CAD for escort routing",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },

    # --- Health sector ---
    {
        "src": "Ambulance_Telemetry", "tgt": "Hospital_Network",
        "edge_type": "communicates_with", "prob": 0.4,
        "source": "engineering_review", "owner": "health_team",
        "rationale": "Ambulance vitals telemetry feeds the hospital intake system directly",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Hospital_Network", "tgt": "Health_Registry",
        "edge_type": "communicates_with", "prob": 0.5,
        "source": "engineering_review", "owner": "health_team",
        "rationale": "Patient records sync from the hospital network to the central registry",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },
    {
        "src": "EMS_Dispatch", "tgt": "Hospital_Network",
        "edge_type": "communicates_with", "prob": 0.35,
        "source": "engineering_review", "owner": "health_team",
        "rationale": "Dispatch shares incident data with the receiving hospital ahead of arrival",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },

    # --- Telecom / IT sector ---
    {
        "src": "Fiber_Backbone", "tgt": "Municipal_DNS",
        "edge_type": "depends_on", "prob": 0.7,
        "source": "engineering_review", "owner": "it_team",
        "rationale": "DNS resolvers run over the fiber backbone with no independent transport",
        "confidence": 0.85, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Municipal_DNS", "tgt": "City_Data_Center",
        "edge_type": "depends_on", "prob": 0.6,
        "source": "engineering_review", "owner": "it_team",
        "rationale": "Data center services rely on municipal DNS for internal service discovery",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },
    {
        "src": "City_Data_Center", "tgt": "Backup_Vault",
        "edge_type": "depends_on", "prob": 0.5,
        "source": "engineering_review", "owner": "it_team",
        "rationale": "Backups replicate continuously from the primary data center",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Identity_Provider", "tgt": "City_Data_Center",
        "edge_type": "controls", "prob": 0.75,
        "source": "engineering_review", "owner": "it_team",
        "rationale": "IdP issues the admin tokens used for the data center's management plane",
        "confidence": 0.85, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Fiber_Backbone", "tgt": "SCADA_Historian",
        "edge_type": "communicates_with", "prob": 0.2,
        "source": "network_topology_scan", "owner": "infra_ops",
        "rationale": "Historian's export pipeline rides the shared fiber backbone; log-collection only",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },

    # --- Finance sector (existing protected assets appear only as SOURCE
    # here — see the note at the top of this block) ---
    {
        "src": "City_Payment_Gateway", "tgt": "Tax_Collection_System",
        "edge_type": "pays_through", "prob": 0.5,
        "source": "application_architecture", "owner": "fintech_team",
        "rationale": "Gateway settles collected tax payments into the tax ledger",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Bank_Partner_API", "tgt": "Payroll_System",
        "edge_type": "pays_through", "prob": 0.4,
        "source": "application_architecture", "owner": "fintech_team",
        "rationale": "Payroll disbursements settle via the bank partner API",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Tax_Collection_System", "tgt": "Payroll_System",
        "edge_type": "communicates_with", "prob": 0.25,
        "source": "application_architecture", "owner": "fintech_team",
        "rationale": "Shared finance ERP backend; advisory correlation only, not a settlement path",
        "confidence": 0.7, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Identity_Provider", "tgt": "Tax_Collection_System",
        "edge_type": "controls", "prob": 0.5,
        "source": "engineering_review", "owner": "it_team",
        "rationale": "Finance ERP authenticates through the central identity provider",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },

    # --- Civic sector ---
    {
        "src": "Citizen_Portal", "tgt": "Permits_System",
        "edge_type": "pays_through", "prob": 0.5,
        "source": "application_architecture", "owner": "civic_team",
        "rationale": "Permit fee payments flow through the citizen portal's payment path",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Permits_System", "tgt": "Records_Archive",
        "edge_type": "communicates_with", "prob": 0.35,
        "source": "application_architecture", "owner": "civic_team",
        "rationale": "Approved permits are archived to the central records system",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Voting_Infrastructure", "tgt": "Identity_Provider",
        "edge_type": "depends_on", "prob": 0.5,
        "source": "engineering_review", "owner": "civic_team",
        "rationale": "Voter identity verification depends on the central identity provider",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },
    {
        "src": "City_Data_Center", "tgt": "Voting_Infrastructure",
        "edge_type": "depends_on", "prob": 0.6,
        "source": "engineering_review", "owner": "it_team",
        "rationale": "Election systems are hosted in the municipal data center",
        "confidence": 0.8, "last_reviewed": "2026-08-26",
    },

    # --- Environment sector ---
    {
        "src": "Air_Quality_Sensors", "tgt": "Waste_Management_System",
        "edge_type": "communicates_with", "prob": 0.2,
        "source": "network_topology_scan", "owner": "environment_team",
        "rationale": "Shared environmental monitoring dashboard; low-coupling correlation only",
        "confidence": 0.65, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Flood_Sensors", "tgt": "Waste_Management_System",
        "edge_type": "communicates_with", "prob": 0.25,
        "source": "network_topology_scan", "owner": "environment_team",
        "rationale": "Flood sensors feed the same environmental ops platform as waste management telemetry",
        "confidence": 0.65, "last_reviewed": "2026-08-26",
    },

    # --- Monitoring sector ---
    {
        "src": "Log_Aggregator", "tgt": "SCADA_Historian",
        "edge_type": "communicates_with", "prob": 0.2,
        "source": "network_topology_scan", "owner": "infra_ops",
        "rationale": "Log aggregator ingests historian exports; log-collection only, matching the historian's existing low-prob precedent",
        "confidence": 0.75, "last_reviewed": "2026-08-26",
    },
    {
        "src": "Threat_Intel_Feed", "tgt": "Log_Aggregator",
        "edge_type": "communicates_with", "prob": 0.15,
        "source": "network_topology_scan", "owner": "infra_ops",
        "rationale": "External threat-intel feed is correlated into the log aggregator; advisory data only",
        "confidence": 0.6, "last_reviewed": "2026-08-26",
    },
]

