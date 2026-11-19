# Roadmap

| # | Stage | Exit criterion (must be tested) | Status |
|---|---|---|---|
| 1 | Audit | Inventory, environment, Bybit CFD findings documented | done |
| 2 | Foundation | Config, DB + migrations, schemas, safety guard, tests green | done |
| 3 | Data | MockFeed (labelled MOCK), CSV importer, validator (gaps, timezone, closed candles, staleness, spread), snapshots persisted | next |
| 4 | Agents | 8 agents with prompts/config/timeouts/retries/token limits; deterministic fallback with LLM provider `none`; orchestrator; ≤ 2 debate rounds; critical failure → NO_TRADE | |
| 5 | Strategies | Formal rules for SMC, trend continuation, breakout+retest, session, volatility breakout, mean reversion; backtester with spread/commission/swap/slippage/latency; walk-forward + OOS; bootstrap | |
| 6 | Risk Gate | Deterministic checks for every limit, unit tests per rule | |
| 7 | Paper execution | Simulator, idempotent orders, reconciliation, kill switch flattening | |
| 8 | Learning | Error taxonomy, lessons, experiment pipeline, weekly review with significance warning | |
| 9 | Dashboard | Responsive UI on real API data; PAPER/DEMO banner; working buttons | |
| 10 | Broker Demo | Only if technically available + verified DEMO + **separate owner permission** | blocked |
