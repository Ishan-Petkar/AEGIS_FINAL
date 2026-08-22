# AEGIS V2: Anomalous Event Graph Intelligence System

AEGIS is an advanced anomaly detection and risk propagation platform. It marries unsupervised machine learning (Isolation Forests) with graph-based cascading impact analysis (Monte Carlo simulation) to detect, trace, and score cyber-physical threats across complex networks.

## Core Capabilities

1. **Unsupervised Anomaly Detection:** Ingests vast amounts of network and sensor data, identifying anomalies without needing pre-labeled threat signatures.
2. **Cascading Impact Index (CII):** Models a digital twin of your network (assets and dependencies). When an anomaly occurs, AEGIS runs probabilistic Monte Carlo simulations to calculate the "blast radius" of the event.
3. **Data-Agnostic Adapters:** Out-of-the-box support for cybersecurity datasets like CIC-IDS2017 (network traffic) and SWaT (industrial OT sensors).
4. **Visual Dashboard:** An interactive Streamlit frontend that renders the blast radius, anomaly scores, and impacted assets in real time.

## Quick Start
Check out the `/docs/SETUP.md` for full installation instructions. 
```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
streamlit run src/aegis_demo.py
```

## Documentation
Please refer to the `/docs/SYSTEM_REFERENCE.md` as the definitive guide to understanding this project.
