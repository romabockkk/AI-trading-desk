# Roadmap

| # | Stage | State (2026-11-19) | What is still missing |
|---|---|---|---|
| 1 | Audit | done | Re-read Bybit pages from a network that can reach bybit.com |
| 2 | Foundation | done | — |
| 3 | Data | done for MOCK + MT5 CSV import | Real-time real data feed (needs MT5 on Windows or another licensed source) |
| 4 | Agents | done (deterministic core; Anthropic LLM review optional) | LLM path not exercised against the real API (no key here); macro bias feeds (DXY, yields) |
| 5 | Strategies | 6 strategies + walk-forward/OOS/bootstrap | Evidence on **real** XAUUSD history (owner CSV export); session DST handling |
| 6 | Risk Gate | done | Real contract specs from broker |
| 7 | Paper execution | done | Partial fills / latency modelling beyond fixed slippage |
| 8 | Learning | done | Needs real paper history to be meaningful |
| 9 | Dashboard | done | Auth beyond localhost + optional owner token |
| 10 | Broker Demo | **BLOCKED** | Windows host, Bybit CFD demo with MT5 access, integration tests, **separate owner permission** |
