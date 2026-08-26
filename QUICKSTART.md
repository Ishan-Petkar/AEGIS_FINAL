# AEGIS — Quick Setup

A simple, verified setup guide. If you just cloned this repo, start here.

For the full reference (troubleshooting, PostgreSQL details, dataset
licensing, architecture), see `README.md` and `docs/SETUP.md`.

---

## 1. Prerequisites

- Python 3.11+
- Node 20+
- PostgreSQL 16
- The `datasets/` folder — sent to you separately, **not** in git (it's
  3.2 GB of real network capture data). See `docs/DATASETS.md`.

## 2. Clone

```bash
git clone -b final-clean https://github.com/Ishan-Petkar/AEGIS_FINAL.git aegis-project
cd aegis-project
```

## 3. Add the datasets

Drop the `datasets/` folder you were given at the **repo root** — a
sibling of `backend/` and `src/`, not inside either of them:

```
aegis-project/
├── datasets/     ← put it here
├── backend/
├── src/
└── ...
```

## 4. Python environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-backend.txt
```

## 5. Database

```bash
createuser aegis --pwprompt      # enter "aegis" as the password when prompted
createdb aegis -O aegis
cp .env.example .env
```

## 6. Build the app

```bash
PYTHONPATH=src python -m backend.init_db
PYTHONPATH=src python -m backend.warmup
```

`init_db` creates the tables and seeds the 50 city assets. `warmup` fits
the anomaly-detection model once on real benign traffic and saves it —
this only needs to happen once, and takes about 5 seconds.

## 7. Start the backend

Leave this running in its own terminal:

```bash
PYTHONPATH=src uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

## 8. Start the frontend

In a **new terminal**:

```bash
cd aegis-project
npm --prefix frontend install
npm --prefix frontend run dev
```

## 9. Open it and start a replay

Open **http://localhost:3000** in your browser.

In a **third terminal**, kick off a live replay of real captured traffic:

```bash
curl -X POST http://127.0.0.1:8000/api/replay/start \
  -H 'Content-Type: application/json' \
  -d '{"dataset":"friday-morning","speed":20.0}'
```

You should see live traffic flowing in the console within a few seconds.

---

## The one thing that trips people up

**Every backend Python command needs `PYTHONPATH=src` in front of it.**
This project doesn't install itself as a normal Python package — it runs
as loose modules with `src/` manually put on the path. Forget
`PYTHONPATH=src` and you'll get `ModuleNotFoundError: No module named
'backend'` or similar. That's not a bug — it's just how the project is
structured (see `CLAUDE.md` if you want the full reasoning).

## Try the demo payoff

Once traffic is flowing, trigger a scripted "what-if" attack using real
captured attack data re-aimed at a chosen city asset:

```bash
curl -X POST http://127.0.0.1:8000/api/inject \
  -H 'Content-Type: application/json' \
  -d '{"scenario":"honeytoken","target_asset":"City_Payment_Gateway","count":5}'
```

Watch the console: a critical alert appears, the risk score jumps, and
the city graph animates the blast radius — a real, measured cascade
computation, not a scripted animation.
