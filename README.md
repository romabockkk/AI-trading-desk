# AI TradFi Trading Team

A research, backtesting and **paper** trading desk for **Bybit CFD** (formerly Bybit TradFi), starting with **XAUUSD**. A Chief and seven agents analyse the market and debate through structured messages. A deterministic Risk Gate makes the final allow/deny call.

> **No real money. There is no LIVE mode in the code.** Bybit Crypto Futures, TradFi Perpetuals and the Bybit V5 crypto API are not used.
> Nothing here claims or promises profit. Every number on mock data is labelled MOCK.

## Status (2026-11-19)

| Area | State |
|---|---|
| Agents | Chief + 7 agents run in every cycle. Each has its own prompt, config, timeout, retries, token limit and strict JSON schema. Debates are persisted (≤ 2 rounds). |
| Data | **MOCK synthetic data only.** No real market feed is reachable from this environment. An MT5 CSV importer exists for real history you export yourself. |
| Strategies | 6 strategies with formal rules (SMC/CHoCH/OB, trend continuation, breakout+retest, London session, volatility breakout, mean reversion), all at status `RESEARCH`. **None has shown an out-of-sample edge**, even on mock data. |
| Research | Backtests include spread, commission, swap, slippage and latency. Walk-forward OOS, bootstrap CI and Monte Carlo drawdown are implemented. |
| Risk | Deterministic Risk Gate: 36 named checks, plus a separate Risk Manager veto. Owner-only limits: 0.5% per trade, 1% open risk, 2% daily stop, 1 position, RR ≥ 2, pause after 2 losses. |
| Execution | **PAPER only**, in a local simulator. It is not a broker and is never shown as one. |
| Learning | Error taxonomy, lessons, experiment pipeline (proposal → OOS → baseline → owner approval), versioning and rollback, weekly review with a significance warning. |
| Dashboard | `http://127.0.0.1:8000/`. Desktop and phone, PAPER / BROKER DEMO banner, buttons wired to the backend. |
| BROKER_DEMO | **BLOCKED.** See [`docs/AUDIT.md`](docs/AUDIT.md). The MT5 package is Windows-only (confirmed), and Bybit reportedly offers no MT5/API/EA access on the CFD demo (unverified). |
| Tests | 189 passing (`python3 -m pytest`). |

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env             # optional; defaults are safe (PAPER, MOCK data, LLM off)
python3 -m pytest                # 189 tests
scripts/run_api.sh               # then open http://127.0.0.1:8000/
```

Typical flow in the dashboard:
1. **Start Analysis** runs one full cycle. With no approved strategy, the result is NO_TRADE.
2. **OOS test** on a strategy runs a walk-forward out-of-sample evidence experiment.
3. **Activate (owner)** puts a strategy version into PAPER. Without OOS evidence the Skeptic still blocks trades, unless you set `paper_allow_unproven_strategies = true` in `config/risk_limits.toml`.
4. **Review** runs error analysis and the weekly report. **Emergency Stop** turns on the kill switch, pauses, and closes all paper positions.

Optional settings:
- `TRADEDESK_SCHEDULER_MINUTES=15` runs automatic cycles.
- `TRADEDESK_LLM_PROVIDER=anthropic` with `LLM_API_KEY` enables LLM reviews. They can only make decisions more conservative.
- Economic calendar: put a sourced CSV at `data/calendar.csv` (format in `data/calendar.example.csv`). Without it, news status is UNKNOWN.

## Main API

- **Read:** `GET /api/status`, `/api/dashboard`, `/api/candles`, `/api/decisions`, `/api/cycles/{id}`, `/api/strategies`, `/api/audit`, `/api/errors`.
- **Actions:** `POST /api/actions/{start-analysis,pause,resume,emergency-stop,review}`, `/api/paper/update`, `/api/research/{backtest,evidence,experiments}`.
- **Owner only:** `POST /api/strategies/{id}/{activate,deactivate,rollback}`, `/api/research/experiments/{id}/approve`, `/api/actions/{reset-kill-switch,acknowledge-loss-streak}`. These require the `X-Owner-Token` header when `TRADEDESK_OWNER_TOKEN` is set.

## Documents

- [`docs/AUDIT.md`](docs/AUDIT.md): environment, and Bybit CFD facts marked confirmed / reported / unknown
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md): components and enforced invariants
- [`docs/ROADMAP.md`](docs/ROADMAP.md): stages and what is missing

The earlier crypto-era documents (`STRATEGY.md.`, `RISK_MANAGEMENT.md.`, `TEAM_RULES.md`, …) are kept for history. Where they conflict with this README (exchange choice, Bybit exclusion, 1% risk), the owner directive of 2026-11-19 and this README take precedence.
