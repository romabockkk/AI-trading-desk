# Roadmap (2026-11-20)

| Step | State | Missing |
|---|---|---|
| Database, PaperBroker, $100 account, PnL, tests | done | — |
| Quote sources | OANDA + Dukascopy providers implemented | **live verification**: OANDA practice token; Dukascopy is rate-limited from this container |
| Instrument specs | XAUUSD contract/margin/commission confirmed from Bybit Help Center | min lot, lot step, tick size, swaps for every symbol (owner enters from Bybit app) |
| Agents & debate | done, autonomous loop | macro feeds (calendar CSV, DXY/yields) |
| Strategies & research | 6 strategies, walk-forward OOS, bootstrap, experiment pipeline | real XAUUSD history (MT5 CSV export or OANDA candles) |
| Learning & reports | done | real paper trades to learn from |
| Phone dashboard | done (tabs, controls, spec editor, login) | hosting with HTTPS |
| 24/7 operation | Dockerfile, compose, systemd unit written | **not deployed, not verified** |
