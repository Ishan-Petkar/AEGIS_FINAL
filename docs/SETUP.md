# Project Setup & Installation

Follow these steps to run AEGIS locally. The **Research Console**
(Streamlit) only needs steps 1–4. The Phase 5 **Operations Console** backend
(in progress) additionally needs the PostgreSQL section below.

## Prerequisites
- Python 3.11–3.13
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
The core engine (`src/`) is heavily configuration-driven via
`src/settings.py` and reads **no** environment variables — there is nothing
to set for the Research Console. `.env.example` documents the Phase 5
backend's environment variables only (see the PostgreSQL section below); it
is a placeholder for the engine itself.

## 3. Run the Evaluation Harness
To benchmark the anomaly detection models (see `docs/EVALUATION.md` for the
full protocol):
```bash
PYTHONPATH=src python -m evaluation --dataset swat --limit 20000

PYTHONPATH=src python -m evaluation --dataset cic_ids2017
```

## 4. Run the Streamlit Dashboard (Research Console)
```bash
streamlit run src/aegis_demo.py
```

## 5. PostgreSQL (Phase 5 backend, in progress)

The Phase 5 Operations Console persists replayed telemetry, scores, CII
snapshots, and alerts to PostgreSQL. This section documents what is actually
provisioned for local development.

**Security note:** the credentials below (`aegis` / `aegis`) are
**local-development only**. `.env` is gitignored — real credentials must
never be committed, and only `.env.example` (with placeholder values) is
tracked. The backend's API/WebSocket server also has **no authentication**,
which is why it binds to `127.0.0.1` (loopback) by default — an
unauthenticated server that binds `0.0.0.0` would hand every device on the
network (e.g. hackathon venue wifi) control of replay and injection
endpoints. Only rebind it if you specifically need container networking or
are demoing from another trusted device, and understand the trade-off.

### 5.1 Install Postgres

```bash
brew install postgresql@16
brew services start postgresql@16
```

Add the Postgres 16 binaries to your `PATH` (needed for `psql`, `pg_ctl`,
etc. — add this to your shell profile, e.g. `~/.zshrc`):

```bash
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
```

### 5.2 Create the role and database

Create a role named `aegis` (password `aegis`, for local development only)
and a database named `aegis` owned by that role:

```bash
createuser -s aegis
psql -d postgres -c "ALTER USER aegis WITH PASSWORD 'aegis';"
createdb -O aegis aegis
```

### 5.3 Install backend Python dependencies

Into the same venv created in step 1 — a separate `requirements-backend.txt`
keeps the core engine's pinned dependency set untouched:

```bash
pip install -r requirements-backend.txt
```

### 5.4 Configure

Copy `.env.example` to `.env` (gitignored) and adjust if your local Postgres
role/database/host differ from the defaults:

```bash
cp .env.example .env
```

All backend settings are read from the environment (prefix `AEGIS_`) or
`.env`, via `backend/config.py`'s `BACKEND_SETTINGS`. See `.env.example` for
the full list (DB connection, connection pool, API bind address, replay
speed/day, model artifact path). The single-URL escape hatch
`AEGIS_DATABASE_URL`, if set, takes precedence over the individual
`AEGIS_DB_*` component fields.

### 5.5 Initialize the schema and seed assets

Creates the five tables (`assets`, `events`, `event_scores`, `cii_snapshots`,
`alerts`) and seeds `assets` from `src/graph_manager.build_graph()` (the
curated topology plus its synthesized gateway nodes). Idempotent — safe to
re-run at any time, including after a graph/config change, to reconcile the
seeded rows:

```bash
python -m backend.init_db
```

### 5.6 Verify connectivity

```bash
python -m backend.db_check
```

This connects using `BACKEND_SETTINGS.database_url`, runs `SELECT
version()`, and prints the resolved (password-redacted) connection target
plus the server version. A clear, actionable error is printed (not a raw
stack trace) if it can't connect — check that Postgres is running
(`brew services list`), that the role/database exist, and that your
`AEGIS_DB_*` env vars or `.env` match your environment.
