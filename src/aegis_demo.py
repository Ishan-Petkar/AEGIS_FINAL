import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import networkx as nx

# Canonical module imports — do NOT redefine these here.
from config import FINANCIAL_TYPES, DEPENDENCY_GRAPH
from core.pipeline import run_analysis
from data_generator import generate_scripted_attack
from ml_engine import model_hyperparameters
from evaluation import DegenerateEvaluationError, run_evaluation, results_to_dataframe
from evaluation.lead_time import compute_all_scripted_attack_lead_times, summarize_lead_times
from ui_theme import DASHBOARD_CSS, HEADER_HTML


# ==============================================================================
# PAGE CONFIGURATION & THEME SETUP
# ==============================================================================
st.set_page_config(
    page_title="AEGIS // Cyber Risk Console",
    page_icon="◆",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown(DASHBOARD_CSS, unsafe_allow_html=True)

# ==============================================================================
# STATE MANAGEMENT
# ==============================================================================
if 'active_attack' not in st.session_state:
    st.session_state.active_attack = None
if 'anomalous_asset' not in st.session_state:
    st.session_state.anomalous_asset = None
if 'attack_edges' not in st.session_state:
    st.session_state.attack_edges = []

# ==============================================================================
# HEADER
# ==============================================================================
st.markdown(HEADER_HTML, unsafe_allow_html=True)
st.markdown("<div style='border-bottom: 1px solid #1e2d45; margin: 0 0 24px 0;'></div>", unsafe_allow_html=True)

# ==============================================================================
# SIDEBAR
# ==============================================================================
# SECTION 1: Attack Simulation
st.sidebar.markdown('<div class="sidebar-section-header"><h3>Attack Simulation</h3></div>', unsafe_allow_html=True)

if st.sidebar.button("Simulate Payment Gateway Data Breach (Financial)", use_container_width=True):
    st.session_state.active_attack = "Payment Gateway Breach"
    st.session_state.anomalous_asset = "City_Payment_Gateway"
    st.session_state.attack_edges = generate_scripted_attack(
        "Payment Gateway Breach",
        {'source': "10.0.1.20", 'target': "198.51.100.42", 'duration_sec': 120.0,
         'packets': 100000, 'bytes': 500000000},
    )

if st.sidebar.button("Simulate Camera Spoofing (ICS)", use_container_width=True):
    st.session_state.active_attack = "Camera Spoofing"
    st.session_state.anomalous_asset = "Traffic_Cam_1"
    st.session_state.attack_edges = generate_scripted_attack(
        "Camera Spoofing",
        {'source': "10.0.1.10", 'target': "10.0.1.12", 'duration_sec': 100.0,
         'packets': 50000, 'bytes': 500000000},
    )

if st.sidebar.button("Simulate Data Exfiltration (IT/OT)", use_container_width=True):
    st.session_state.active_attack = "Data Exfiltration"
    st.session_state.anomalous_asset = "SCADA_Historian"
    st.session_state.attack_edges = generate_scripted_attack(
        "Data Exfiltration",
        {'source': "10.0.1.16", 'target': "45.227.254.12", 'duration_sec': 300.0,
         'packets': 150000, 'bytes': 1500000000},
    )

if st.sidebar.button("Simulate Lateral Movement (Web)", use_container_width=True):
    st.session_state.active_attack = "Lateral Movement"
    st.session_state.anomalous_asset = "Citizen_Portal"
    st.session_state.attack_edges = generate_scripted_attack(
        "Lateral Movement",
        {'source': "10.0.1.15", 'target': "10.0.1.13", 'duration_sec': 45.0,
         'packets': 20000, 'bytes': 20000000},
    )

st.sidebar.markdown('<div class="sidebar-section-header" style="margin-top: 16px;"><h3>What-If Interactive Simulation</h3></div>', unsafe_allow_html=True)
known_whatif_assets = [
    "City_Payment_Gateway", "Traffic_Controller", "SCADA_Historian", "Citizen_Portal",
    "Emergency_Route_System", "Traffic_Cam_1",
]
whatif_asset = st.sidebar.selectbox(
    "Select Asset to Compromise",
    options=["-- None (Custom) --"] + known_whatif_assets,
    index=0,
    help="Interactively select any network node to run real-time Monte Carlo risk propagation analysis."
)
if whatif_asset != "-- None (Custom) --":
    st.session_state.active_attack = f"What-If: {whatif_asset}"
    st.session_state.anomalous_asset = whatif_asset

if st.sidebar.button("Reset Simulation Status", use_container_width=True):
    st.session_state.active_attack = None
    st.session_state.anomalous_asset = None
    st.session_state.attack_edges = []
    st.rerun()

# SECTION 2: Telemetry Data Source
st.sidebar.markdown('<div class="sidebar-section-header" style="margin-top: 24px;"><h3>Telemetry Source</h3></div>', unsafe_allow_html=True)
dataset_choice = st.sidebar.selectbox(
    "Data Source",
    options=["synthetic", "cic_ids2017", "paysim", "swat"],
    format_func=lambda x: {
        "synthetic": "Synthetic Generator (Baseline)",
        "cic_ids2017": "CIC-IDS2017 (Network Flows)",
        "paysim": "PaySim (Financial Fraud)",
        "swat": "SWaT (ICS/OT Telemetry)",
    }[x],
    index=0
)

# SECTION 3: Network Topology Configuration
st.sidebar.markdown('<div class="sidebar-section-header" style="margin-top: 24px;"><h3>Network Topology</h3></div>', unsafe_allow_html=True)
num_servers = st.sidebar.slider("Number of Nodes", min_value=15, max_value=80, value=35)
num_edges = st.sidebar.slider("Connection Count", min_value=30, max_value=150, value=70)

# SECTION 4: ML Engine Configuration
st.sidebar.markdown('<div class="sidebar-section-header" style="margin-top: 24px;"><h3>ML Anomaly Settings</h3></div>', unsafe_allow_html=True)
anomaly_rate = st.sidebar.slider("Contamination Rate", min_value=0.01, max_value=0.20, value=0.08)
model_estimators = st.sidebar.selectbox("Tree Count", options=[50, 100, 200], index=1)

# ==============================================================================
# ANALYSIS — single call into the headless pipeline (contract C1). No ML
# training, graph construction, or CII computation happens in this file.
# ==============================================================================
result = run_analysis(
    dataset=dataset_choice,
    num_nodes=num_servers,
    num_edges=num_edges,
    anomaly_rate=anomaly_rate,
    model_estimators=model_estimators,
    attack_edges=st.session_state.attack_edges,
    active_attack=st.session_state.active_attack,
    anomalous_asset=st.session_state.anomalous_asset,
)

if result.dataset_warning:
    st.warning(result.dataset_warning)

nodes_df = result.nodes_df
edges_df = result.edges_df
criticality_map = result.criticality_map
total_connections = result.total_connections
anomalous_connections = result.anomalous_connections
cii_score = result.cii.cii_median
cii_p5 = result.cii.cii_p5
cii_p95 = result.cii.cii_p95
impacted_assets = result.cii.impacted_assets
hop_details = result.cii.hop_details
network_risk = result.network_risk

# ==============================================================================
# METRICS GRID
# ==============================================================================
col_m1, col_m2, col_m3, col_m4 = st.columns(4)

with col_m1:
    st.markdown(f"""<div class="metric-card"><div class="metric-label">Total Connections</div>
        <div class="metric-val blue">{total_connections}</div></div>""", unsafe_allow_html=True)
with col_m2:
    st.markdown(f"""<div class="metric-card"><div class="metric-label">Active Assets</div>
        <div class="metric-val purple">{len(nodes_df)}</div></div>""", unsafe_allow_html=True)
with col_m3:
    st.markdown(f"""<div class="metric-card"><div class="metric-label">Threat Anomalies</div>
        <div class="metric-val red">{anomalous_connections}</div></div>""", unsafe_allow_html=True)
with col_m4:
    risk_color = "red" if network_risk > 50 else ("purple" if network_risk > 25 else "green")
    st.markdown(f"""<div class="metric-card"><div class="metric-label">Cascading Risk Index</div>
        <div class="metric-val {risk_color}">{network_risk}/100</div></div>""", unsafe_allow_html=True)

# Full-Width Span Financial Exposure Bar
if st.session_state.active_attack == "Payment Gateway Breach":
    st.markdown("""
    <div class="financial-exposure-bar" style="border-left: 2px solid #ff3355;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <div>
                <span class="metric-label" style="color: #ff3355; font-weight: 700;">Financial Exposure Status: CRITICAL</span>
                <p style="margin: 4px 0 0 0; font-size: 0.85rem; color: #8899b4;">Active Payment Gateway Data Breach In Progress. High economic system vulnerability identified.</p>
            </div>
            <div style="font-family: 'JetBrains Mono', monospace; font-weight: 700; color: #ff3355; font-size: 1.1rem; text-shadow: 0 0 10px rgba(255, 51, 85, 0.25);">POTENTIAL IMPACT: HIGH</div>
        </div>
    </div>
    """, unsafe_allow_html=True)
else:
    st.markdown("""
    <div class="financial-exposure-bar" style="border-left: 2px solid #0aff8e;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <div>
                <span class="metric-label" style="color: #0aff8e; font-weight: 700;">Financial Exposure Status: SECURE</span>
                <p style="margin: 4px 0 0 0; font-size: 0.85rem; color: #8899b4;">All municipal payment nodes and external APIs behaving within normal limits.</p>
            </div>
            <div style="font-family: 'JetBrains Mono', monospace; font-weight: 700; color: #0aff8e; font-size: 1.1rem; text-shadow: 0 0 10px rgba(10, 255, 142, 0.25);">POTENTIAL IMPACT: NONE</div>
        </div>
    </div>
    """, unsafe_allow_html=True)

st.markdown("<br>", unsafe_allow_html=True)

# ==============================================================================
# TABS & CONTENT
# ==============================================================================
tab_network, tab_charts, tab_ml, tab_data, tab_roadmap = st.tabs([
    "Interactive Network Topology",
    "Traffic Threat Analytics",
    "Machine Learning Model Inspector",
    "Raw Traffic Database",
    "System Evolution & Roadmap"
])

with tab_network:
    st.markdown('<div class="section-header">Smart City Connection Graph & Dependencies</div>', unsafe_allow_html=True)

    # Monte Carlo Risk & Uncertainty Profile Banner
    if st.session_state.active_attack and st.session_state.anomalous_asset:
        impacted_summary = ", ".join(impacted_assets[:3]) + ("..." if len(impacted_assets) > 3 else "")
        st.markdown(f"""
        <div style="border: 1px solid #ff3355; background: #0f1724; padding: 16px; border-radius: 4px; margin-bottom: 20px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
                <div style="font-weight: 700; font-size: 1.05rem; color: #ff3355;">
                    ⚡ MONTE CARLO RISK & UNCERTAINTY PROFILE: {st.session_state.anomalous_asset}
                </div>
                <div style="font-size: 0.8rem; font-family: 'JetBrains Mono', monospace; color: #8899b4;">
                    1,000 MONTE CARLO ITERATIONS
                </div>
            </div>
            <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 14px;">
                <div style="background: #080c14; padding: 10px; border-radius: 4px; border: 1px solid #1e2d45;">
                    <div style="font-size: 0.75rem; color: #8899b4;">P50 Median CII Score</div>
                    <div style="font-size: 1.3rem; font-weight: 700; color: #ff3355;">{cii_score:.3f}</div>
                </div>
                <div style="background: #080c14; padding: 10px; border-radius: 4px; border: 1px solid #1e2d45;">
                    <div style="font-size: 0.75rem; color: #8899b4;">90% Confidence Interval</div>
                    <div style="font-size: 1.1rem; font-weight: 700; color: #00d4ff;">[{cii_p5:.3f}, {cii_p95:.3f}]</div>
                </div>
                <div style="background: #080c14; padding: 10px; border-radius: 4px; border: 1px solid #1e2d45;">
                    <div style="font-size: 0.75rem; color: #8899b4;">Impacted Downstream Nodes</div>
                    <div style="font-size: 1.3rem; font-weight: 700; color: #f59e0b;">{len(impacted_assets)}</div>
                </div>
                <div style="background: #080c14; padding: 10px; border-radius: 4px; border: 1px solid #1e2d45;">
                    <div style="font-size: 0.75rem; color: #8899b4;">Relative Network Risk</div>
                    <div style="font-size: 1.3rem; font-weight: 700; color: #ff3355;">{network_risk}/100</div>
                </div>
            </div>
            <div style="font-size: 0.88rem; color: #e8edf5; background: rgba(255, 51, 85, 0.08); padding: 12px; border-radius: 4px; border-left: 3px solid #ff3355;">
                <strong>PLAIN-ENGLISH CONSEQUENCE TRANSLATION:</strong> In 90% of Monte Carlo stochastic simulations, initial breach of <code>{st.session_state.anomalous_asset}</code> propagates across dependency channels to compromise {len(impacted_assets)} downstream asset(s) ({impacted_summary if impacted_summary else 'none'}). Operational impact severity is <strong>{'CRITICAL' if cii_score > 0.4 else 'HIGH' if cii_score > 0.2 else 'MODERATE'}</strong> with probabilistic confidence bounds spanning [{cii_p5*100:.1f}%, {cii_p95*100:.1f}%].
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.markdown("""
        <div style="border: 1px solid #1e2d45; background: #0f1724; padding: 14px 18px; border-radius: 4px; margin-bottom: 20px;">
            <div style="font-weight: 700; font-size: 0.95rem; color: #00d4ff; margin-bottom: 4px;">
                ℹ MONTE CARLO STOCHASTIC ENGINE BASELINE
            </div>
            <div style="font-size: 0.85rem; color: #8899b4;">
                No compromise simulation active. Select an <strong>Attack Simulation Preset</strong> or use the <strong>What-If Simulator</strong> in the sidebar to choose any node and calculate real-time probabilistic risk propagation across 1,000 Monte Carlo iterations.
            </div>
        </div>
        """, unsafe_allow_html=True)

    # GRAPH FIRST (Full Width) — this is the observed-traffic topology graph,
    # a different object from the asset-dependency graph graph_manager.py
    # builds for the CII engine (see PLAN_MASTER.md Phase 1, contract C2).
    G = nx.Graph()
    for _, node in nodes_df.iterrows():
        G.add_node(node['asset_name'], type=node['type'], status=node['status'])

    for _, edge in edges_df.iterrows():
        G.add_edge(edge['source_asset'], edge['target_asset'], is_anomaly=edge['is_anomaly'])

    pos = nx.kamada_kawai_layout(G)

    # Network Traffic Edges styling matching DESIGN.md
    normal_x, normal_y, anomaly_x, anomaly_y = [], [], [], []
    for edge in G.edges(data=True):
        if edge[0] in pos and edge[1] in pos:
            x0, y0 = pos[edge[0]]
            x1, y1 = pos[edge[1]]
            if edge[2].get('is_anomaly', False):
                anomaly_x.extend([x0, x1, None])
                anomaly_y.extend([y0, y1, None])
            else:
                normal_x.extend([x0, x1, None])
                normal_y.extend([y0, y1, None])

    edge_traces = [
        go.Scatter(x=normal_x, y=normal_y, line=dict(width=1, color='rgba(30, 45, 69, 0.4)'), hoverinfo='none', mode='lines', name='Network Traffic'),
        go.Scatter(x=anomaly_x, y=anomaly_y, line=dict(width=2.5, color='rgba(255, 51, 85, 0.8)'), hoverinfo='none', mode='lines', name='Anomalous Flow')
    ]

    # Dependency Graph Overlay
    if st.session_state.active_attack and impacted_assets:
        dep_x, dep_y = [], []
        for entry in DEPENDENCY_GRAPH:
            src, tgt = entry["src"], entry["tgt"]
            if src == st.session_state.anomalous_asset or src in impacted_assets:
                if tgt in impacted_assets:
                    if src in pos and tgt in pos:
                        x0, y0 = pos[src]
                        x1, y1 = pos[tgt]
                        dep_x.extend([x0, x1, None])
                        dep_y.extend([y0, y1, None])

        edge_traces.append(go.Scatter(
            x=dep_x, y=dep_y, line=dict(width=3, color='rgba(245, 158, 11, 0.8)', dash='dash'),
            hoverinfo='none', mode='lines', name='Cascading Impact Path'
        ))

    # Nodes Processing with DESIGN.md colors & symbols
    node_x, node_y, node_color, node_size, node_text, node_symbols = [], [], [], [], [], []
    for node in G.nodes():
        if node in pos:
            x, y = pos[node]
            node_x.append(x)
            node_y.append(y)
            is_threat = G.nodes[node].get('status') == 'Threat Highlighted'
            is_impacted = node in impacted_assets
            node_type = G.nodes[node].get('type', '')

            # Marker Symbol
            if node_type in FINANCIAL_TYPES:
                node_symbols.append('diamond')
            else:
                node_symbols.append('circle')

            # Color Logic
            if is_threat:
                color = '#ff3355' # Danger red
            elif is_impacted:
                color = '#f59e0b' # Warning amber
            elif node_type in FINANCIAL_TYPES:
                color = '#d4a843' # Gold for financial
            else:
                color = '#00d4ff' # Electric cyan for standard
            node_color.append(color)

            node_size.append(14 + (G.degree(node) * 2))

            status_label = "OUTLIER ACTIVITY" if is_threat else ("DOWNSTREAM IMPACT" if is_impacted else "SECURE")
            node_text.append(f"<b>Asset:</b> {node}<br><b>Type:</b> {node_type}<br><b>Status:</b> {status_label}")

    node_trace = go.Scatter(
        x=node_x, y=node_y, mode='markers+text' if num_servers < 40 else 'markers',
        text=[n if n in criticality_map else "" for n in G.nodes()],
        textposition="top center", textfont=dict(color="#e8edf5", family="Inter"),
        hovertext=node_text, hoverinfo='text',
        marker=dict(color=node_color, size=node_size, symbol=node_symbols, line=dict(width=1.5, color='#080c14'))
    )

    fig = go.Figure(data=[*edge_traces, node_trace])
    fig.update_layout(
        showlegend=True,
        legend=dict(
            yanchor="top", y=0.99, xanchor="left", x=0.01,
            bgcolor="rgba(8, 12, 20, 0.8)", bordercolor="#1e2d45", borderwidth=1,
            font=dict(color="#e8edf5", family="Inter", size=10)
        ),
        margin=dict(b=0, l=0, r=0, t=0),
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        yaxis=dict(showgrid=False, zeroline=False, showticklabels=False),
        plot_bgcolor='#080c14',
        paper_bgcolor='#080c14',
        height=600
    )
    st.plotly_chart(fig, use_container_width=True)

    # TWO-COLUMN LAYOUT BELOW THE GRAPH
    if st.session_state.active_attack:
        st.markdown("<div style='border-bottom: 1px solid #1e2d45; margin: 16px 0;'></div>", unsafe_allow_html=True)
        col_left, col_right = st.columns([2, 1])

        with col_left:
            st.markdown('<div class="section-header">Cascading Impact & Alert Details</div>', unsafe_allow_html=True)

            # Setup specific details and borders matching DESIGN.md
            if st.session_state.active_attack == "Payment Gateway Breach":
                alert_title = f"DATA BREACH: Anomaly detected on {st.session_state.anomalous_asset}"
                attack_desc = "Massive data exfiltration from City_Payment_Gateway to external IP 198.51.100.42. Transfer size: 500 MB (1000x normal transaction volume)."
                impact_desc = "If financial data is breached, citizen payment information is exposed, welfare payments may be frozen, and the city's bond rating could be downgraded, halting infrastructure projects."
                rec_action = "Immediately isolate City_Payment_Gateway from all external networks and initiate incident response."
                mitre_id = "T0822"
                border_color = "#f59e0b"
            elif st.session_state.active_attack == "Camera Spoofing":
                alert_title = f"ANOMALY: Anomaly detected on {st.session_state.anomalous_asset}"
                attack_desc = f"{st.session_state.anomalous_asset} sent an unexpected massive volume of data, indicative of anomalous malformed SET commands."
                impact_desc = "This could disrupt traffic light timing and subsequently delay critical Emergency Route operations."
                rec_action = "Isolate Traffic_Cam_1 and verify Traffic_Controller firmware integrity."
                mitre_id = "T0856 / T0842"
                border_color = "#ff3355"
            elif st.session_state.active_attack == "Data Exfiltration":
                alert_title = f"ANOMALY: Anomaly detected on {st.session_state.anomalous_asset}"
                attack_desc = f"{st.session_state.anomalous_asset} initiated massive outbound data transfer to external threat IP 45.227.254.12."
                impact_desc = "This compromises the security and audit trail of all connected field devices."
                rec_action = "Block outbound connection to external IP and isolate Historian server."
                mitre_id = "T0822"
                border_color = "#ff3355"
            elif st.session_state.active_attack == "Lateral Movement":
                alert_title = f"ANOMALY: Anomaly detected on {st.session_state.anomalous_asset}"
                attack_desc = f"{st.session_state.anomalous_asset} was compromised and is attempting unauthorized large-scale connections to the Power_Substation."
                impact_desc = "This could allow the attacker to compromise grid stability leading to widespread physical power disruption."
                rec_action = "Revoke Citizen_Portal credentials and harden internal firewall rules."
                mitre_id = "T0867 / T0883"
                border_color = "#ff3355"
            else:
                # What-If Simulator path — generic copy, no scripted attack narrative.
                severity = "CRITICAL" if cii_score > 0.4 else "HIGH" if cii_score > 0.2 else "MODERATE"
                alert_title = f"WHAT-IF SIMULATION: Hypothetical compromise of {st.session_state.anomalous_asset}"
                attack_desc = f"User-selected what-if scenario: {st.session_state.anomalous_asset} is assumed compromised to model downstream Monte Carlo risk propagation."
                impact_desc = f"Estimated operational impact severity is {severity}. See the Impacted Assets panel for the full downstream compromise chain."
                rec_action = f"Review the dependency chain from {st.session_state.anomalous_asset} and confirm existing controls limit lateral movement to the assets listed."
                mitre_id = "N/A (hypothetical scenario)"
                border_color = "#ff3355" if severity == "CRITICAL" else "#f59e0b" if severity == "HIGH" else "#00d4ff"

            # Left panel layout (CII Box + Details callout)
            col_cii, col_det = st.columns([1, 3])
            with col_cii:
                st.markdown(f"""
                <div style='text-align: center; padding: 20px; background-color: #162032; border: 1px solid #1e2d45; border-left: 2px solid {border_color}; border-radius: 2px;'>
                    <h1 style='color: {border_color}; margin: 0; font-size: 2.5rem; font-family: "JetBrains Mono", monospace;'>{cii_score:.2f}</h1>
                    <p style='color: #8899b4; margin: 4px 0 0 0; font-size: 0.8rem; font-weight: 600; text-transform: uppercase;'>CII Score (Median)</p>
                    <p style='color: #8899b4; margin: 2px 0 0 0; font-size: 0.7rem; font-family: "JetBrains Mono", monospace;'>90% CI: {cii_p5:.2f} – {cii_p95:.2f}</p>
                </div>
                """, unsafe_allow_html=True)
            with col_det:
                st.markdown(f"""
                <div class="aegis-alert" style="border-left: 2px solid {border_color}; background-color: #162032;">
                    <div style="font-weight: 700; font-size: 1.1rem; color: {border_color}; margin-bottom: 8px;">{alert_title}</div>
                    <div style="margin-bottom: 6px; font-size: 0.9rem;"><strong style="color: #e8edf5;">Anomaly Profile:</strong> <span style="color: #8899b4;">{attack_desc}</span></div>
                    <div style="margin-bottom: 6px; font-size: 0.9rem;"><strong style="color: #e8edf5;">Consequences:</strong> <span style="color: #8899b4;">{impact_desc}</span></div>
                    <div style="font-size: 0.9rem;"><strong style="color: #e8edf5;">Mitigation:</strong> <span style="color: #8899b4;">{rec_action} (MITRE ATT&CK: {mitre_id})</span></div>
                </div>
                """, unsafe_allow_html=True)

        with col_right:
            st.markdown('<div class="section-header">Impacted Assets & Severity</div>', unsafe_allow_html=True)

            # Asset list with visual indicators (Complies with DESIGN.md)
            for asset in impacted_assets:
                details = hop_details[asset]
                crit = details['criticality']
                hop = details['hop']

                # Indicator color based on criticality
                if crit >= 0.8:
                    indicator_color = "#ff3355" # Danger red
                elif crit >= 0.5:
                    indicator_color = "#f59e0b" # Warning amber
                else:
                    indicator_color = "#00d4ff" # Accent cyan

                st.markdown(f"""
                <div style="display: flex; align-items: center; padding: 12px 16px; background-color: #0f1724; border: 1px solid #1e2d45; border-radius: 2px; margin-bottom: 8px; transition: all 150ms ease-out;">
                    <span style="color: {indicator_color}; font-size: 1rem; margin-right: 12px;">◆</span>
                    <div>
                        <div style="font-weight: 600; font-size: 0.95rem; color: #e8edf5;">{asset} (Hop {hop})</div>
                        <div style="font-size: 0.75rem; color: #8899b4; font-family: 'JetBrains Mono', monospace;">CRITICALITY: {crit} | COMPROMISE FREQ: {details['prob']:.0%}</div>
                    </div>
                </div>
                """, unsafe_allow_html=True)

with tab_charts:
    st.markdown('<div class="section-header">Network Performance & Security Intelligence</div>', unsafe_allow_html=True)
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        threat_hosts = edges_df[edges_df['is_anomaly']]
        src_anomalies = threat_hosts.groupby('source_asset').size().reset_index(name='Anomaly Count')
        src_anomalies = src_anomalies.sort_values(by='Anomaly Count', ascending=False).head(10)

        financial_asset_names = nodes_df[nodes_df['type'].isin(FINANCIAL_TYPES)]['asset_name'].tolist()
        color_seq = ['#d4a843' if n in financial_asset_names else '#ff3355' for n in src_anomalies['source_asset']]

        fig_bar = px.bar(src_anomalies, x='Anomaly Count', y='source_asset', orientation='h',
                         title='Top Threat Originating Sources (Asset Names)', color='Anomaly Count', color_continuous_scale='Reds')
        fig_bar.update_layout(
            plot_bgcolor='#080c14',
            paper_bgcolor='#080c14',
            font_color='#e8edf5',
            font_family="Inter",
            xaxis=dict(gridcolor='#1e2d45', zerolinecolor='#1e2d45'),
            yaxis=dict(gridcolor='#1e2d45', zerolinecolor='#1e2d45')
        )
        st.plotly_chart(fig_bar, use_container_width=True)
    with col_c2:
        fig_scatter = px.scatter(edges_df, x='packets', y='bytes', color='is_anomaly',
                                 color_discrete_map={True: '#ff3355', False: '#0aff8e'},
                                 title='Payload Analysis: Packets vs Transferred Bytes',
                                 hover_data=['source_asset', 'target_asset'])
        fig_scatter.update_layout(
            plot_bgcolor='#080c14',
            paper_bgcolor='#080c14',
            font_color='#e8edf5',
            font_family="Inter",
            xaxis=dict(type='log', gridcolor='#1e2d45', zerolinecolor='#1e2d45'),
            yaxis=dict(type='log', gridcolor='#1e2d45', zerolinecolor='#1e2d45')
        )
        st.plotly_chart(fig_scatter, use_container_width=True)

with tab_ml:
    st.markdown('<div class="section-header">Phase 3 — Ground-Truth Evaluation Benchmark Panel</div>', unsafe_allow_html=True)

    @st.cache_data
    def _cached_run_evaluation():
        try:
            results = run_evaluation(limit=5000, include_ocsvm=False, verbose=False)
            return results_to_dataframe(results), None
        except DegenerateEvaluationError as exc:
            # Phase 3: the eval split's positive rate was outside the sane
            # band (SETTINGS.evaluation.min/max_positive_rate) — surface it
            # as a warning instead of either crashing the tab or silently
            # falling back to the old P=0.000 R=0.000 F1=0.000 AUC=nan bug.
            return pd.DataFrame(), str(exc)
        except Exception as exc:
            return pd.DataFrame(), f"Evaluation harness failed: {exc}"

    eval_df, eval_warning = _cached_run_evaluation()
    if eval_warning:
        st.warning(f"Ground-truth benchmark unavailable: {eval_warning}")
    if not eval_df.empty:
        col_ev1, col_ev2 = st.columns([1, 1])
        with col_ev1:
            st.markdown("#### Detector Performance Comparison (CIC-IDS2017 Ground Truth)")
            st.dataframe(
                eval_df[["precision", "recall", "f1", "roc_auc", "n_predicted_anomalies"]]
                .style.highlight_max(subset=["precision", "recall", "f1", "roc_auc"], color="rgba(10, 255, 142, 0.2)")
                .format({"precision": "{:.3f}", "recall": "{:.3f}", "f1": "{:.3f}", "roc_auc": "{:.3f}"}),
                use_container_width=True
            )
            st.caption("Models trained exclusively on benign traffic split and evaluated against full ground-truth labeled test split.")

        with col_ev2:
            fig_eval = px.bar(
                eval_df.reset_index(),
                x="detector",
                y=["precision", "recall", "f1", "roc_auc"],
                barmode="group",
                title="Ground Truth Benchmark Metrics",
                color_discrete_sequence=["#00d4ff", "#0aff8e", "#ff3355", "#f59e0b"]
            )
            fig_eval.update_layout(
                plot_bgcolor='#080c14',
                paper_bgcolor='#080c14',
                font_color='#e8edf5',
                font_family="Inter",
                xaxis=dict(gridcolor='#1e2d45', zerolinecolor='#1e2d45'),
                yaxis=dict(gridcolor='#1e2d45', zerolinecolor='#1e2d45', range=[0, 1.05])
            )
            st.plotly_chart(fig_eval, use_container_width=True)

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">Phase 3 — Tripwire Lead-Time Benchmark</div>', unsafe_allow_html=True)

    @st.cache_data
    def _cached_lead_times():
        try:
            results = compute_all_scripted_attack_lead_times()
            return results, summarize_lead_times(results), None
        except Exception as exc:
            return [], {}, f"Lead-time benchmark failed: {exc}"

    lt_results, lt_summary, lt_warning = _cached_lead_times()
    if lt_warning:
        st.warning(lt_warning)
    elif lt_results:
        lt_df = pd.DataFrame([r.to_dict() for r in lt_results])
        st.dataframe(
            lt_df[["attack_name", "gateway_zone", "recon_detected_at",
                   "exfil_detected_at", "lead_time_seconds"]]
            .style.format({"lead_time_seconds": "{:.1f}s"}),
            use_container_width=True,
        )
        st.caption(
            f"Tripwire fired earlier than the volumetric detector in "
            f"{lt_summary['n_tripwire_earlier']}/{lt_summary['n_attacks']} scripted attacks "
            f"(mean lead time {lt_summary['mean_lead_time_seconds']:.1f}s). "
            "Volumetric detection instant is modeled as the exfil connection's own "
            "timestamp (the most generous possible case for the baseline) — see "
            "docs/EVALUATION.md for the full methodology."
        )

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown('<div class="section-header">Isolation Forest Model Inspector</div>', unsafe_allow_html=True)
    col_ml1, col_ml2 = st.columns([2, 1])
    with col_ml1:
        fig_hist = px.histogram(edges_df, x='raw_score', color='is_anomaly',
                                color_discrete_map={True: '#ff3355', False: '#00d4ff'},
                                title='Isolation Forest Decision Score Distribution')
        fig_hist.update_layout(
            plot_bgcolor='#080c14',
            paper_bgcolor='#080c14',
            font_color='#e8edf5',
            font_family="Inter",
            xaxis=dict(gridcolor='#1e2d45', zerolinecolor='#1e2d45'),
            yaxis=dict(gridcolor='#1e2d45', zerolinecolor='#1e2d45')
        )
        st.plotly_chart(fig_hist, use_container_width=True)
    with col_ml2:
        st.markdown("#### Hyperparameter Settings")
        st.json(model_hyperparameters(n_estimators=model_estimators, contamination=anomaly_rate))

with tab_data:
    st.markdown('<div class="section-header">Live Connection Log Table</div>', unsafe_allow_html=True)
    search = st.text_input("Filter by Asset Name or IP")
    filtered_df = edges_df.copy()
    if search:
        filtered_df = filtered_df[filtered_df['source_asset'].str.contains(search, case=False) | filtered_df['target_asset'].str.contains(search, case=False)]

    def color_anomaly(val): return 'background-color: rgba(255, 51, 85, 0.1); color: #ff3355' if val else 'color: #0aff8e'
    st.dataframe(filtered_df[['timestamp', 'source_asset', 'target_asset', 'duration_sec', 'packets', 'bytes', 'is_anomaly']]
                 .sort_values(by='is_anomaly', ascending=False)
                 .style.map(color_anomaly, subset=['is_anomaly']))

with tab_roadmap:
    st.markdown('<div class="section-header">AEGIS System Evolution & Roadmap Progress</div>', unsafe_allow_html=True)

    col_p1, col_p2 = st.columns(2)

    with col_p1:
        st.markdown("""
        <div class="aegis-alert" style="border-left: 2px solid #0aff8e; background-color: #0f1724;">
            <div style="font-weight: 700; font-size: 1.1rem; color: #0aff8e; margin-bottom: 8px;">Phase 0 — Foundation Correctness (Completed)</div>
            <ul style="color: #8899b4; font-size: 0.85rem; padding-left: 18px; margin: 0;">
                <li><strong>ML → CII Wiring:</strong> Real anomaly scores directly feed risk propagation with zero hardcoded literals.</li>
                <li><strong>Single Canonical Implementation:</strong> Consolidated graph, CII calculator, and data generator into single canonical modules.</li>
                <li><strong>Config-Driven Parameters:</strong> Typed Pydantic <code>settings.py</code> replacing all magic numbers.</li>
                <li><strong>Test Coverage & CI:</strong> Comprehensive Pytest suite enforcing graph bounds, decay, and edge handling.</li>
            </ul>
        </div>

        <div class="aegis-alert" style="border-left: 2px solid #0aff8e; background-color: #0f1724;">
            <div style="font-weight: 700; font-size: 1.1rem; color: #0aff8e; margin-bottom: 8px;">Phase 1 — Foundation & Gateway Topology (Completed)</div>
            <ul style="color: #8899b4; font-size: 0.85rem; padding-left: 18px; margin: 0;">
                <li><strong>Multi-Dataset Integration:</strong> Adapters for SWaT (OT), CIC-IDS2017 (Network), and PaySim (Financial fraud).</li>
                <li><strong>Canonical Event Schema v2:</strong> Unified <code>CanonicalEvent</code>/<code>CanonicalBatch</code>, extended with <code>signal_type</code>, <code>observed_at</code>, <code>purdue_level</code> for non-flow signals.</li>
                <li><strong>Single Graph Authority:</strong> <code>graph_manager.py</code> is the sole graph builder, implementing a mandatory Purdue-zone access gateway in front of every high-criticality asset.</li>
                <li><strong>Analytics/UI Separation:</strong> <code>core.pipeline.run_analysis()</code> runs headless — no Streamlit dependency in the analysis path.</li>
            </ul>
        </div>

        <div class="aegis-alert" style="border-left: 2px solid #0aff8e; background-color: #0f1724;">
            <div style="font-weight: 700; font-size: 1.1rem; color: #0aff8e; margin-bottom: 8px;">Phase 2 — Causal & Probabilistic Risk Modeling (Completed)</div>
            <ul style="color: #8899b4; font-size: 0.85rem; padding-left: 18px; margin: 0;">
                <li><strong>Explicit Edge Semantics:</strong> Added <code>depends_on</code>, <code>controls</code>, <code>communicates_with</code>, <code>pays_through</code>, <code>backed_up_by</code>, and <code>shares_provider</code>.</li>
                <li><strong>Monte Carlo Engine:</strong> Replaced BFS with sampling engine solving diamond dependencies & redundancy AND-gates.</li>
                <li><strong>Edge Metadata Provenance:</strong> Tracked <code>source</code>, <code>owner</code>, <code>rationale</code>, <code>confidence</code>, and <code>last_reviewed</code> per edge.</li>
                <li><strong>Uncertainty Distributions:</strong> Computed 90% confidence intervals (P5, P50 median, P95) for all risk outputs.</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)

    with col_p2:
        st.markdown("""
        <div class="aegis-alert" style="border-left: 2px solid #00d4ff; background-color: #0f1724;">
            <div style="font-weight: 700; font-size: 1.1rem; color: #00d4ff; margin-bottom: 8px;">Phase 2b — Deception Activation (Next Step)</div>
            <p style="color: #8899b4; font-size: 0.85rem; margin-bottom: 8px;">Seeding honeytoken credentials into the Phase 1 gateway topology for pre-compromise detection.</p>
            <ul style="color: #8899b4; font-size: 0.85rem; padding-left: 18px; margin: 0;">
                <li><strong>Honeytoken Credentials:</strong> Zero-legitimate-use fake credentials per gateway — any use is unambiguous compromise.</li>
                <li><strong>Tripwire Detector:</strong> Fires on honeytoken use, conforming to the shared detector interface.</li>
                <li><strong>Lead-Time Measurement:</strong> Recon-stage scripted scenarios to quantify detection lead time vs. volume-based detection.</li>
            </ul>
        </div>

        <div class="aegis-alert" style="border-left: 2px solid #8899b4; background-color: #0f1724;">
            <div style="font-weight: 700; font-size: 1.1rem; color: #8899b4; margin-bottom: 8px;">Upcoming Vision Roadmap</div>
            <ul style="color: #8899b4; font-size: 0.85rem; padding-left: 18px; margin: 0;">
                <li><strong>Phase 3:</strong> Honest Evaluation — segment-wise metrics, degenerate-split guards, lead-time as a first-class benchmark.</li>
                <li><strong>Phase 4:</strong> Graph-Aware & Temporal ML (GraphSAGE/GCN, conformal prediction, SHAP explainability).</li>
                <li><strong>Phase 5:</strong> Streaming, API & Multi-Tenancy (Kafka, Neo4j, FastAPI, tenant isolation).</li>
            </ul>
        </div>
        """, unsafe_allow_html=True)
