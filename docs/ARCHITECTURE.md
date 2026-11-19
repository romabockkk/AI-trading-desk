# Architecture

```
            ┌──────────── owner (.env, config/risk_limits.toml, approvals) ────────────┐
            │                                                                          │
 Data service ──► Validator ──► market_snapshots (SQLite)                               │
                                   │                                                    │
       ┌───────────────┬───────────┴───────┐                                            │
 Market Structure     Quant             Macro        (independent, strict JSON reports) │
       └───────────────┴─────────┬─────────┘                                            │
                               Chief ──► hypothesis + alternative                       │
                                 │                                                      │
                              Skeptic ⇄ agents   (≤ 2 rounds, rows in `debates`)        │
                                 │                                                      │
                           Risk Manager (VETO)                                          │
                                 │                                                      │
                     Deterministic Risk Gate  ◄── frozen RiskLimits ◄───────────────────┘
                                 │ ALLOW / DENY   (no LLM input can change it)
                         Chief records decision
                                 │ ALLOW only
                         Execution ──► PaperBroker | MT5Adapter (demo guard)
                                 │
                       Performance & Learning ──► trades, lessons, experiments, weekly_reviews
```

## Modules (`src/tradedesk/`)

| Module | Stage | Status |
|---|---|---|
| `config.py` — modes (no LIVE), settings, owner risk limits (frozen, hashed) | 2 | done |
| `db/` — SQLite, forward-only checksummed migrations, append-only hash-chained `audit_log` | 2 | done |
| `schemas/` — market, trading, agent report payloads (Pydantic, `extra=forbid`) | 2 | done |
| `safety.py` — deterministic DEMO-proof guard for any broker order | 2 | done |
| `broker/base.py` — `BrokerAdapter` interface; `broker/status.py` — honest BLOCKED reasons | 2 | interface only |
| `api/app.py` — health, status, pause/resume/emergency stop, audit, errors | 2 | partial |
| `data/` — mock feed, CSV import, validator | 3 | not started |
| `agents/` — 8 agents, LLM adapter, orchestrator | 4 | not started |
| `strategies/`, `backtest/` | 5 | not started |
| `risk/gate.py` | 6 | not started |
| `execution/paper.py` | 7 | not started |
| `learning/` | 8 | not started |
| dashboard | 9 | not started |
| `broker/mt5_adapter.py` | 10 | blocked |

## Invariants enforced in code (with tests)

- `LIVE`/`REAL`/`PRODUCTION` mode → startup error; DB `CHECK` rejects `LIVE` in `decisions.mode`.
- Risk limits: frozen model, hard ceilings, `require_stop_loss` must be true, file sha256 audited.
- DB: LONG/SHORT decision requires entry + stop-loss; a trade decision without ALLOW needs a reason; orders need a decision; `client_order_id` unique (duplicate protection); `broker_account_is_demo` can only be 1 or NULL.
- Audit log: UPDATE/DELETE blocked by triggers; chain verification detects tampering.
- Broker order guard: mode BROKER_DEMO + enabled flag + owner phrase + integration tests + kill switch OFF + not paused + live account `trade_mode == DEMO` + exact server allowlist + server name contains "demo" + non-mock source. Any failure → blocked.
- Secrets are `SecretStr`, excluded from `public_dict()` and `repr`.
