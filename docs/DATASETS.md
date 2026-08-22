# AEGIS Dataset Provenance & Ingestion Documentation

This document records the provenance, licensing terms, schema mappings, and download instructions for all real-world security telemetry datasets integrated into AEGIS Phase 1.

---

## 1. CIC-IDS2017 (Network Intrusion Traffic)

- **Source:** Canadian Institute for Cybersecurity (CIC), University of New Brunswick (UNB)
- **URL:** [https://www.unb.ca/cic/datasets/ids-2017.html](https://www.unb.ca/cic/datasets/ids-2017.html)
- **Direct Download:** `http://205.174.165.80/CICDataset/CIC-IDS-2017/Dataset/MachineLearningCSV.zip` (~900 MB)
- **License:** Creative Commons Attribution 4.0 International (CC BY 4.0)
- **Local Path:** `datasets/MachineLearningCVE/*.csv` (8 CSV files)

### Attack Families Covered
- **DDoS / DoS:** `DDoS`, `DoS Hulk`, `DoS GoldenEye`, `DoS slowloris`, `DoS Slowhttptest`
- **Reconnaissance:** `PortScan`
- **Infiltration & Malware:** `Infiltration`, `Bot`
- **Brute Force:** `FTP-Patator`, `SSH-Patator`
- **Web Attacks:** `Web Attack - Brute Force`, `Web Attack - XSS`, `Web Attack - Sql Injection`

### Schema Mapping
- `Destination Port` → Heuristically resolved to AEGIS infrastructure nodes (`Citizen_Portal`, `SCADA_Historian`, `Traffic_Controller`, etc.) via `AssetRegistry`.
- `Flow Duration`, `Total Fwd Packets`, `Total Length of Fwd Packets` → Mapped to `duration_sec`, `packets`, `bytes`, and `payload_size`.
- `Label` → Normalized to `attck_evidence` and `calibrated_alert_level`.

---

## 2. PaySim (Financial Fraud Simulation)

- **Source:** Edgar Alonso Lopez-Rojas et al. (Kaggle Dataset)
- **URL:** [https://www.kaggle.com/datasets/ealaxi/paysim1](https://www.kaggle.com/datasets/ealaxi/paysim1)
- **File Name:** `PS_20174392719_1491204439457_log.csv` (~471 MB, 6.36 million rows)
- **License:** Creative Commons Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)
- **Local Path:** `datasets/PS_20174392719_1491204439457_log.csv`

### Transaction Types
- `PAYMENT`, `TRANSFER`, `CASH_OUT`, `DEBIT`, `CASH_IN`

### Schema Mapping
- `nameOrig` (Customer account) → Resolved to `City_Payment_Gateway` via `AssetRegistry`.
- `nameDest` (Merchant / Destination) → Resolved to `Bank_Partner_API` via `AssetRegistry`.
- `amount` → Mapped to `payload_size` and `bytes`.
- `isFraud == 1` → Mapped to `attck_evidence="TRANSFER_FRAUD"` and `calibrated_alert_level="CRITICAL"`.

---

## 3. SWaT — Secure Water Treatment (ICS/OT Process Data)

- **Source:** iTrust, Singapore University of Technology and Design (SUTD)
- **URL:** [https://itrust.sutd.edu.sg/itrust-labs_datasets/dataset_info/](https://itrust.sutd.edu.sg/itrust-labs_datasets/dataset_info/)
- **Access:** Granted via iTrust research data-use agreement (not openly licensed like CIC-IDS2017/PaySim)
- **Local Path:** `datasets/SWaT/merged.csv` (falls back to `datasets/SWaT/attack.csv` if `merged.csv` is absent)
- **Collection Window:** 28 Dec 2015 – 2 Jan 2016, 1-second sampling, 1,441,719 rows (~3.8% attack rows)
- **Role:** Physical process telemetry from a 6-stage water treatment testbed — 51 PLC/SCADA sensor and actuator tags (`FIT101`, `LIT101`, `MV101`, `P101`, ... `P603`).

### Schema Mapping
- `Timestamp` → parsed to canonical `timestamp` (format `%d/%m/%Y %I:%M:%S %p`, with a `mixed`-format fallback).
- `Normal/Attack` → mapped to canonical `action` (`ACTION_ALERT` for `Attack`, `ACTION_PASS` for `Normal`).
- All 51 sensor/actuator columns are carried through unchanged as extra numeric columns for ML feature engineering; they do not populate `duration_sec`/`packets`/`bytes` directly — `CanonicalBatch.to_ml_features()` derives those from `payload_size`.
- `source_asset_id` / `destination_asset_id` are both fixed to `"SWaT_System"` (the adapter does not yet resolve individual PLC tags to distinct graph nodes).

---

## 4. Synthetic Data Generator

- **Implementation:** `src/data_generator.py`
- **Role:** Baseline mock network telemetry when real datasets are not present on disk.
- **Provenance:** Tagged as `"synthetic"`, `confidence = 0.5`.
