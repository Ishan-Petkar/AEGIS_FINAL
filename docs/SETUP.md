# Project Setup & Installation

Follow these steps to run AEGIS locally.

## Prerequisites
- Python 3.10+
- `pip`

## 1. Clone & Environment
```bash
git clone <repository_url>
cd aegis-project
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. Environment Variables
AEGIS is heavily configuration-driven via `src/settings.py`. There are currently no required environment variables or API keys. See `.env.example`.

## 3. Run the Evaluation Harness
To benchmark the anomaly detection models (see `docs/EVALUATION.md` for the
full protocol):
```bash
# Run on the SWaT dataset (requires datasets/SWaT/merged.csv)
PYTHONPATH=src python -m evaluation --dataset swat --limit 20000

# Run on CIC-IDS2017
PYTHONPATH=src python -m evaluation --dataset cic_ids2017
```

## 4. Run the Streamlit Dashboard
```bash
streamlit run src/aegis_demo.py
```
