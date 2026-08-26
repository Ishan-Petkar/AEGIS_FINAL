"""
generate_paysim_sample.py — Generates a dumbed-down / sample version of the PaySim dataset.
"""

import pandas as pd
import numpy as np
import random
from pathlib import Path

DATASETS_DIR = Path(__file__).resolve().parent.parent.parent / "datasets"

def generate_paysim_sample(num_rows=5000):
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    dest_path = DATASETS_DIR / "paysim_sample.csv"
    
    print(f"Generating dummy PaySim sample -> {dest_path}")
    
    np.random.seed(42)
    random.seed(42)
    
    types = ['PAYMENT', 'TRANSFER', 'CASH_OUT', 'DEBIT', 'CASH_IN']
    
    data = {
        'step': np.random.randint(1, 10, num_rows),
        'type': np.random.choice(types, num_rows),
        'amount': np.round(np.random.exponential(scale=1000, size=num_rows), 2),
        'nameOrig': [f"C{random.randint(10000, 99999)}" for _ in range(num_rows)],
        'oldbalanceOrg': np.round(np.random.uniform(0, 10000, num_rows), 2),
        'newbalanceOrig': np.zeros(num_rows),
        'nameDest': [f"M{random.randint(10000, 99999)}" for _ in range(num_rows)],
        'oldbalanceDest': np.round(np.random.uniform(0, 10000, num_rows), 2),
        'newbalanceDest': np.zeros(num_rows),
        'isFraud': np.random.choice([0, 1], num_rows, p=[0.95, 0.05]),
        'isFlaggedFraud': np.zeros(num_rows, dtype=int)
    }
    
    df = pd.DataFrame(data)
    
    # Calculate simple balances (not perfectly realistic but enough for structure)
    df['newbalanceOrig'] = df['oldbalanceOrg'] - df['amount']
    df['newbalanceOrig'] = df['newbalanceOrig'].apply(lambda x: max(0, x))
    df['newbalanceDest'] = df['oldbalanceDest'] + df['amount']
    
    df.to_csv(dest_path, index=False)
    print(f"Successfully generated {dest_path} with {num_rows} rows.")

if __name__ == "__main__":
    generate_paysim_sample()
