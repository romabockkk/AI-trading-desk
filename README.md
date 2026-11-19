# AI TradFi Trading Team

Research, backtesting and **paper/demo** trading desk for **Bybit CFD** (formerly Bybit TradFi), starting with **XAUUSD**. A Chief and seven specialised agents analyse the market; a deterministic Risk Gate makes the final allow/deny call.

> **No real money. No LIVE mode exists in the code.** Bybit Crypto Futures, TradFi Perpetuals and the Bybit V5 crypto API are out of scope.
> The goal is reliability, controlled risk and reproducible research. Nothing here promises profit.

## Current status (2026-11-19)

| Area | State |
|---|---|
| Stage 1 Audit | done → [`docs/AUDIT.md`](docs/AUDIT.md) |
| Stage 2 Foundation | done: config, owner risk limits, SQLite + migrations (15 tables + system state), schemas, demo safety guard, broker interface, minimal API, 69 tests |
| Agents, data feed, strategies, risk gate, paper execution, dashboard | **not implemented yet** (API reports them as `NOT_IMPLEMENTED`) |
| BROKER_DEMO | **BLOCKED**: Linux host, no MetaTrader5 package, disabled by default, no owner authorization, and Bybit reportedly does not offer MT5/API access on CFD Demo |
| Trading | **The system does not trade.** |

## Modes

`RESEARCH` · `BACKTEST` · `PAPER` (default) · `BROKER_DEMO` (off by default; needs separate owner permission). Setting `LIVE` causes a startup error.

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env            # optional; defaults are safe (PAPER)
python3 -m pytest               # or scripts/run_tests.sh
PYTHONPATH=src python3 -m tradedesk init-db
PYTHONPATH=src python3 -m tradedesk status
scripts/run_api.sh              # http://127.0.0.1:8000/api/status
```

API (current): `GET /health`, `GET /api/status`, `GET /api/audit`, `GET /api/errors`, `POST /api/actions/{pause,resume,emergency-stop,reset-kill-switch}`. `POST /api/actions/start-analysis` returns **501** until the orchestrator exists.

## Risk limits

Owner-only, in [`config/risk_limits.toml`](config/risk_limits.toml): 0.5% per trade, 1% total open risk, 2% daily drawdown stop, 1 position, RR ≥ 2, pause after 2 losses, stop-loss mandatory. These are conservative starting values for testing. They are not proven optimal or safe. Martingale, averaging down and grids are not configurable: they do not exist.

## Documents

- [`docs/AUDIT.md`](docs/AUDIT.md): environment and Bybit CFD findings
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): components and enforced invariants
- [`docs/ROADMAP.md`](docs/ROADMAP.md): stages
- Earlier crypto-era documents (`STRATEGY.md.`, `RISK_MANAGEMENT.md.`, `TEAM_RULES.md`, …) are kept for history. Where they conflict with this README (exchange choice, Bybit exclusion, 1% risk), **this README and the owner directive of 2026-11-19 take precedence**.
