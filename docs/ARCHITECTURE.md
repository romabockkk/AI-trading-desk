# Architecture

```
 owner ── .env · config/risk_limits.toml · config/agents.toml · approvals (X-Owner-Token)
   │
 Data service (MOCK | future MT5) ─► Validator ─► market_snapshots
   │
   ├── Market Structure ─┐
   ├── Quant ────────────┼─ independent strict-JSON reports (agent_reports)
   └── Macro ────────────┘
             │
          Chief: hypothesis + alternative ─────────────┐
             │                                         │ debates table:
          Skeptic objections ⇄ agents answer w/ facts  │ HYPOTHESIS / ALTERNATIVE / OBJECTION /
             │ (≤ 2 rounds)                            │ RESPONSE / DEMAND_NO_TRADE / CONCESSION
          Risk Manager (independent sizing, VETO) ─────┘
             │
          Chief final (LLM review may only downgrade)
             │
          Deterministic Risk Gate (36 named checks, all evaluated) ─► ALLOW / DENY  (risk_events)
             │ ALLOW only
          Execution ─► PaperBroker (PAPER)  |  MT5Adapter (BROKER_DEMO, guarded, BLOCKED)
             │
          Performance & Learning ─► trades · lessons · experiments · weekly_reviews
```

## Modules (`src/tradedesk/`)

| Module | Purpose | Status |
|---|---|---|
| `config.py` | Modes (no LIVE), settings, frozen owner risk limits with sha256 | done, tested |
| `db/` | SQLite, checksummed forward-only migrations, append-only hash-chained `audit_log` | done, tested |
| `schemas/` | Pydantic models: market, trading, 8 agent payloads (`extra=forbid`) | done, tested |
| `safety.py` | DEMO-proof guard for any broker order | done, tested |
| `data/` | `MockFeed` (labelled MOCK), MT5 CSV importer (server TZ → UTC), validator, resampler, snapshot service | done, tested |
| `analysis/` | Causal indicators; formal SMC rules MS.SWING/LABEL/TREND/BOS/CHOCH/OB/FVG/SWEEP v1 | done, tested |
| `strategies/` | 6 strategies with formal rules; no-look-ahead contract | done, tested |
| `backtest/` | Bar backtester with spread/commission/swap/slippage/latency, walk-forward OOS, bootstrap, Monte Carlo | done, tested |
| `risk/` | Sizing from contract spec; deterministic Risk Gate | done, tested |
| `execution/paper.py` | Local simulator: idempotent orders, SL/TP on M5 bars, reconcile, emergency close | done, tested |
| `agents/` | Agent runtime (timeout, retry, schema validation), 8 agents, orchestrator, LLM adapter | done, tested (LLM via fakes only) |
| `learning/` | Strategy registry (owner-only activate/rollback), experiment pipeline, error taxonomy, weekly review | done, tested |
| `api/app.py`, `web/index.html` | FastAPI + responsive dashboard, optional scheduler | done, tested + browser-checked |
| `broker/mt5_adapter.py` | MT5 demo adapter with guards | code + fake-module tests; **never connected** |

## Key invariants (each has tests)

- No LIVE mode anywhere; `LIVE` in config or DB is rejected.
- LLMs never produce prices, sizes or orders. An LLM review can only add objections or downgrade to NO_TRADE.
- Any failed / timed-out / invalid critical agent (Chief, Market Structure, Macro, Risk Manager, Skeptic) ⇒ NO_TRADE.
- Strategies only see closed candles; a signal at bar *i* fills at bar *i+1* open. Tested by recomputing signals on
  truncated data, and by a null test on a driftless random walk.
- Volume is computed only by the Risk Gate from the contract spec; RR is re-checked at the live price.
- Only owner-approved (`ACTIVE_PAPER`) strategy versions can produce trades; without OOS evidence
  (≥ 30 OOS trades, bootstrap CI95 > 0, ≥ half folds positive) the Skeptic raises unresolved HIGH objections ⇒ NO_TRADE,
  unless the owner sets `paper_allow_unproven_strategies = true` (PAPER only).
- Orders are idempotent on `client_order_id`; duplicates are blocked and logged.
- After `max_consecutive_losses` losses, trading pauses until the owner acknowledges a review.
- Improvements: proposal → walk-forward backtest → OOS → baseline comparison → owner approval → new version → owner activation.
  Rollback re-activates the previous approved version. Single trades never change rules (recurrence ≥ 3 of last 20).
