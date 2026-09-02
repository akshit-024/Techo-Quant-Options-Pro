# TECO Quant Pro

TECO Quant Pro is a local decision-support workspace for analysing Indian index, stock, and commodity options. It collects market data, validates each snapshot, calculates technical and option metrics, ranks CE and PE contracts, estimates position risk, and presents the results in a React dashboard.

The application is intentionally fail-closed: missing, stale, incomplete, or demo data cannot produce an actionable trade. The current release supports data analysis and deterministic paper-trading foundations; it does not place live broker orders.

## What the project does

- Discovers supported NSE, BSE, and MCX contracts from the broker instrument master.
- Acquires quotes, option chains, intraday candles, and live ticks when the required market-data entitlement is available.
- Normalizes and validates coherent point-in-time market snapshots before analysis.
- Scores CE and PE evidence independently, applies liquidity and safety gates, and ranks eligible strikes.
- Calculates whole-lot position sizing, estimated costs, stops, and risk-multiple targets.
- Stores local market, signal, paper-execution, and audit state in SQLite.
- Provides a responsive dashboard with live/demo separation and explicit operational status.

## Technology stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, threaded WSGI server, `httpx`, `websockets` |
| Frontend | React 19, TypeScript, Vite, CSS |
| Persistence | SQLite |
| Broker data | DhanHQ market-data REST APIs and WebSocket feed |
| Backend quality | `unittest`, Ruff, mypy |
| Frontend quality | Vitest, Testing Library, jsdom |

The frontend communicates only with the local backend. Dhan credentials remain server-side and are never included in the browser bundle.

## Dhan API usage

The backend uses DhanHQ read-only market-data services for instrument discovery, market quotes and depth, option-chain data, intraday OHLCV history, and streaming ticks. Live market values require an active Dhan market-data subscription in addition to valid credentials.

The current application does not submit live orders through Dhan Trading APIs. Without market-data access, the interface remains usable as an explicitly labelled, non-actionable demo.

## Calculations

At a high level, TECO Quant Pro calculates:

- Trend and momentum evidence using completed-candle VWAP, EMA/WMA, Wilder RSI and ATR, plus higher-timeframe confirmation.
- Option values and Greeks using Black-Scholes for supported spot options and Black-76 for commodity futures options.
- Expected move, moneyness, spread quality, volume, open interest, change in open interest, and liquidity evidence.
- Independent call/put scores and strike rankings after freshness, expiry, liquidity, affordability, and event-risk checks.
- Position size from account risk and premium-allocation limits, including estimated charges, stops, and 1R/2R/3R targets.
- Replay and backtest summaries such as P&L, win rate, expectancy, drawdown, costs, and slippage.

Detailed formulas, internal score weights, and private strategy rules are intentionally not documented here.

## Architecture

```text
Dhan REST / WebSocket
          |
          v
Broker adapters and ingestion
          |
          v
Normalization and validation
          |
          v
SQLite persistence
          |
          v
Analytics, signals, backtesting, and paper execution
          |
          v
Local Python JSON service
          |
          v
React dashboard
```

```text
.
|-- backend/
|   |-- src/teco_quant/
|   |   |-- analytics/
|   |   |-- api/
|   |   |-- automation/
|   |   |-- backtesting/
|   |   |-- brokers/
|   |   |-- domain/
|   |   |-- execution/
|   |   |-- ingestion/
|   |   |-- persistence/
|   |   |-- signals/
|   |   `-- strategy/
|   |-- tests/
|   |-- .env.example
|   `-- pyproject.toml
|-- frontend/
|   |-- src/
|   |   |-- api/
|   |   |-- components/
|   |   |-- data/
|   |   |-- domain/
|   |   |-- features/
|   |   |-- hooks/
|   |   |-- lib/
|   |   `-- styles/
|   |-- .env.example
|   |-- package.json
|   `-- vite.config.ts
`-- README.md
```

## Local setup and execution

Requirements:

- Python 3.11 or newer
- Node.js 20.10 or newer
- npm

### Backend

Open a PowerShell terminal from the repository root:

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
python -m teco_quant serve
```

Add `DHAN_CLIENT_ID` and `DHAN_ACCESS_TOKEN` only to `backend/.env`. The file is ignored by Git. Set `TECO_DHAN_LIVE_ENABLED=true` only when the account has an active Dhan market-data entitlement. Other runtime and decision-profile settings are described safely by name in `backend/.env.example`.

The backend starts at `http://127.0.0.1:8000` by default.

### Frontend

Open a second PowerShell terminal from the repository root:

```powershell
cd frontend
Copy-Item .env.example .env
npm ci
npm run dev
```

Open the local URL printed by Vite, normally `http://localhost:5173`.

## Verification

Backend:

```powershell
cd backend
python -m unittest discover -s tests -v
python -m ruff check src tests
python -m mypy
```

Frontend:

```powershell
cd frontend
npm run typecheck
npm test
npm run build
```

## Security notes

- Never commit `.env`, broker credentials, access tokens, private keys, local databases, or terminal logs.
- Never place secrets in a `VITE_*` variable; Vite exposes those values to the browser.
- Keep live order execution disabled until a separately reviewed broker gateway and production controls are implemented.
- Demo data is illustrative and must not be used as a live trading signal.

This software is a technical analysis and research tool, not financial advice.
