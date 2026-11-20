# AI TradFi Trading Team — internal paper trading

A Chief and seven AI agents analyse gold (XAUUSD), silver, FX pairs and indices, debate trade ideas, check each other, and trade a **virtual $100 account inside the project's own PaperBroker**. Every trade must pass a deterministic Risk Gate.

> **No orders are sent to Bybit or any other broker.** The code contains no order-sending functions; a test enforces this. LIVE and BROKER_DEMO modes do not exist.
> Nothing here promises profit.

## Status (2026-11-20)

| Area | State |
|---|---|
| PaperBroker | Virtual $100 account with an append-only money ledger. Simulates bid/ask fills, slippage, commission at open, tiered margin, free margin, margin level, swap at server midnight (triple-swap day), stop-out, SL/TP on quotes and bars, realized/unrealized PnL. Survives restarts; no duplicate orders; reset archives the old account. |
| Specs | **No invented values.** Confirmed from Bybit Help Center: XAUUSD contract 100, margin 0.2%, commission $6/lot (metals, FX). Min lot, lot step, tick size and swap rates are **still missing**. Until you enter them from the Bybit app, every trade is NO TRADE. |
| Live quotes | Read-only providers: **OANDA v20** (official, real-time; plug in a practice token later: `OANDA_API_TOKEN`) and **Dukascopy** (public; publishes ~25 min after each hour, so its quotes are always STALE). **Without a fresh quote no new paper trade is opened.** Source, quote time and delay are shown per symbol. |
| History (research) | Real **Dukascopy** M1 bid/ask candles, stored in SQLite (`python -m tradedesk fetch-history`), topped up daily. Used only for backtests and walk-forward OOS tests (`python -m tradedesk research`). It is always labelled HISTORICAL, never as a live quote. |
| Self-check | `GET /api/self-check` and on the dashboard: $100 initial balance, balance == ledger, all 8 agents OK, 12 dangerous proposals refused by the live Risk Gate, no order ever on mock data. |
| Agents | All 8 run autonomously every N minutes for every symbol while the server runs. Reports, debates (≤ 2 rounds), decisions, refusals and errors are stored in SQLite. |
| Learning | Post-trade error classification, lessons, an experiment pipeline (proposal → walk-forward OOS → baseline comparison → owner approval), versioning and rollback. Daily and weekly reports. |
| Dashboard | Phone-first, with tabs. Buttons: Start, Pause new trades, Emergency Stop (kill switch), Close all positions (confirm), Reset paper account (confirm), Analyze now, Review. Includes a spec editor. |
| Hosting | **Not deployed.** It runs only while a server runs. See [`docs/DEPLOY.md`](docs/DEPLOY.md). |
| Tests | `python3 -m pytest`, all passing. |

### What $100 means in practice

At 0.5% risk the budget per trade is **$0.50**. The 0.01-lot minimum for gold is about 1 oz:

- every $1.00 of price movement costs $1.00;
- Bybit's Tight-Spread commission adds $0.06.

So the stop including spread must be under about $0.44. With a realistic spread, the spread-to-stop limit and the leverage cap also block the trade. The Risk Gate therefore refuses almost every gold trade at $100, and the dashboard's feasibility table shows the exact numbers. This is the expected, honest result of the limits, not a bug.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env        # set OANDA_API_TOKEN for real-time quotes
python3 -m pytest
scripts/run_api.sh          # http://127.0.0.1:8000/
```

Then:
1. Open **Settings** and enter the missing XAUUSD spec fields from Bybit (TradFi → CFD → XAUUSD+ → Info).
2. Import history via `POST /api/research/import-csv` (an MT5 export), then run OOS tests.
3. Activate a strategy only if you choose to.

## Documents
- [`docs/AUDIT.md`](docs/AUDIT.md): what is confirmed by official sources, what is reported, what is unknown
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): components and enforced invariants
- [`docs/DEPLOY.md`](docs/DEPLOY.md): always-on hosting and iPhone access
- [`docs/ROADMAP.md`](docs/ROADMAP.md): what is done and what is missing
