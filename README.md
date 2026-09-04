TECO Quant Options Pro

TECO Quant Options Pro is a real-time decision-support workspace for analysing Indian index, stock, and commodity options. It connects to live broker market-data services, validates incoming snapshots, calculates technical and option metrics, ranks CE and PE contracts, estimates position risk, and presents the results through a responsive React dashboard.

The application is intentionally fail-closed: missing, stale, incomplete, invalid, or demo data cannot produce an actionable trade. The current release is designed for market analysis, signal evaluation, and paper-trading workflows; it does not place live broker orders.

What the project does

Discovers supported NSE, BSE, and MCX contracts from broker market-data sources.

Acquires live quotes, option chains, intraday candles, and streaming market updates.

Normalizes and validates market snapshots before analytics are calculated.

Scores CE and PE evidence independently and ranks eligible option contracts.

Applies liquidity, freshness, expiry, and safety checks before any decision becomes actionable.

Calculates whole-lot position sizing, estimated costs, stops, and risk-multiple targets.

Provides clear LIVE, STALE, UNAVAILABLE, and DEMO states.

Keeps live and demo data strictly separated.

Supports local development and cloud deployment through a backend + frontend architecture.

Technology stack

Layer

Technology

Backend

Python 3.11+, httpx, websockets

Frontend

React 19, TypeScript, Vite, CSS

Persistence

SQLite

Broker data

DhanHQ market-data REST APIs and WebSocket feed

Backend quality

unittest, Ruff, mypy

Frontend quality

Vitest, Testing Library, jsdom

Deployment

Render Web Service + Render Static Site

Dhan credentials remain server-side and are never included in the browser bundle.

Live market-data model

TECO Quant Options Pro uses the backend as the single source of truth for LIVE mode.

When LIVE mode is active:

the frontend requests market workspaces only from the backend;

the backend connects to DhanHQ market-data services;

live market snapshots are validated before being published;

stale or incomplete data is shown explicitly rather than silently replaced;

demo mode is available only through an explicit user choice.

A live display snapshot and an actionable trade decision are intentionally treated as separate concepts. The application can display current market data while still returning WAIT, NO TRADE, or INSUFFICIENT DATA when required safety conditions are not satisfied.

Dhan API usage

The backend uses DhanHQ read-only market-data services for:

instrument discovery;

market quotes and depth;

option-chain data;

intraday OHLCV history;

streaming market updates.

Live market values require valid Dhan credentials and the required market-data entitlement.

The current application does not submit live orders through Dhan Trading APIs.

Calculations

At a high level, TECO Quant Options Pro calculates:

trend and momentum evidence from market-price data;

option values and Greeks for supported contracts;

expected move and moneyness;

spread and liquidity quality;

volume and open-interest evidence;

CE and PE contract rankings;

whole-lot risk and position-sizing estimates;

stop and target levels;

replay and backtest summaries.

Detailed strategy weights, internal decision rules, and private scoring logic are intentionally not documented publicly.

Architecture

Local development

DhanHQ REST / WebSocket
          |
          v
TECO Quant Options Pro Backend
          |
          v
Validation + Analytics + Persistence
          |
          v
Local JSON API
          |
          v
React / Vite Frontend

Deployed architecture

User Browser
     |
     | HTTPS
     v
Render Static Site
React / Vite Frontend
     |
     | HTTPS
     v
Render Web Service
TECO Quant Options Pro Backend
     |
     | Server-side credentials
     v
DhanHQ Market Data

The frontend never connects directly to DhanHQ. Broker credentials stay inside the backend environment.

.
|-- backend/
|   |-- src/teco_quant/
|   |-- tests/
|   |-- .env.example
|   `-- pyproject.toml
|-- frontend/
|   |-- src/
|   |-- .env.example
|   |-- package.json
|   `-- vite.config.ts
|-- render.yaml
`-- README.md

Local setup and execution

Requirements:

Python 3.11 or newer

Node.js 20.10 or newer

npm

Backend

Open a PowerShell terminal from the repository root:

cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m teco_quant serve

Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN only to backend/.env.

Set:

TECO_DHAN_LIVE_ENABLED=true
TECO_EXECUTION_MODE=DATA_ONLY

when live Dhan market data is configured.

The backend starts at http://127.0.0.1:8000 by default.

Frontend

Open a second PowerShell terminal from the repository root:

cd frontend
Copy-Item .env.example .env
npm ci
npm run dev

Open the local URL printed by Vite, normally http://localhost:5173.

Cloud deployment

The project is prepared for deployment using Render:

the backend runs as a Render Web Service;

the frontend runs as a Render Static Site;

broker credentials are configured only as backend environment variables;

the frontend receives only the deployed backend API URL;

live order execution remains disabled.

For deployment, configure the backend with:

DHAN_CLIENT_ID
DHAN_ACCESS_TOKEN
TECO_DHAN_LIVE_ENABLED=true
TECO_EXECUTION_MODE=DATA_ONLY
TECO_ALLOWED_ORIGINS

Configure the frontend with:

VITE_API_BASE_URL

Never place Dhan credentials inside any VITE_* variable.

Dhan access-token rotation

A Dhan access-token refresh does not require a code change.

When a token expires:

generate a fresh Dhan access token;

update DHAN_ACCESS_TOKEN in the backend deployment environment;

restart or redeploy the backend service;

verify that LIVE market data reconnects successfully.

No Git commit is required for routine token rotation.

Verification

Backend:

cd backend
python -m unittest discover -s tests -v
python -m ruff check src tests
python -m mypy

Frontend:

cd frontend
npm test
npm run typecheck
npm run build

Security notes

Never commit .env, broker credentials, access tokens, private keys, local databases, or terminal logs.

Never place secrets in a VITE_* variable; Vite exposes those values to the browser.

Keep live order execution disabled unless a separately reviewed broker gateway and production controls are implemented.

Demo data is illustrative and must not be used as a live trading signal.

Broker authentication failures, stale data, and incomplete snapshots must remain visible and fail closed.

This software is a technical analysis and research tool, not financial advice.