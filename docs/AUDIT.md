# Stage 1 — Audit (2026-11-19)

Everything below was checked in this session. Items marked **UNVERIFIED** could not be confirmed from a primary source.

## 1. Repository before this work

| File | Content | Status |
|---|---|---|
| `README.md` | Crypto multi-agent desk, BingX/OKX demo, "No Bybit" | **Superseded** by owner directive of 2026-11-19 (Bybit CFD, TradFi) |
| `AGENT_WORKFLOW.md.`, `TEAM_RULES.md` | 8 crypto-era roles, decision labels | Principles kept (no invented data, veto, NO TRADE valid); roles replaced |
| `STRATEGY.md.` | SMC v1.0 (H4/H1/M15/M5, CHoCH/OB/FVG, RR ≥ 2) | Kept as the starting hypothesis for the SMC strategy |
| `RISK_MANAGEMENT.md.` | 1% per trade, 2 losses pause, 2% daily DD | **Tightened**: 0.5% per trade, 1% total open risk |
| `BACKTESTING.md.`, `TRADE_JOURNAL.md.` | Methodology / journal fields | Kept, implemented via DB schema |
| `DATA_SOURCES.md.` | Truncated (18 lines, starts mid-document at section 7) | Incomplete file |
| `EXCHANGE_INTEGRATION.md.`, `CLAUDE_INSTRUCTIONS.md.` | BingX primary, Bybit excluded | **Conflicts** with new mission; order-safety rules kept |

Notes
- Seven files have a trailing dot in their names (`STRATEGY.md.` etc.). Not renamed, to avoid breaking existing links; recommend the owner renames them.
- No code, tests, database or config existed. Nothing was deleted.

## 2. Environment (measured)

| Item | Value |
|---|---|
| OS | Linux x86_64 (cloud container), 4 CPU, 16 GB RAM |
| Python | 3.13.18 (project requires ≥ 3.11) |
| SQLite | 3.53.1 (JSON1 available) |
| Pre-installed | pydantic 2.13, pytest 9.1, uvicorn 0.54, numpy 2.5, pandas 3.0 |
| Installed now | fastapi 0.165.0, pydantic-settings 2.16.3, httpx 0.28.1 |
| Network | Package registries reachable. `www.bybit.com`, `api.bybit.com`, `mql5.com`, `stooq.com`, Yahoo Finance: **not reachable** (connection refused by environment policy) |
| `pip download metatrader5` | **No matching distribution** on Linux — package publishes Windows wheels only |

Consequence: no real market data and no broker connection are possible from this container right now. Stage 3 will use a clearly labelled MOCK data source and a CSV importer for owner-supplied historical data.

## 3. Bybit CFD / MT5 findings (re-checked 2026-11-19, second pass)

`www.bybit.com`, `learn.bybit.com`, `bybit-global.com`, `mql5.com` and Bybit's API docs are **blocked from this container**
(DNS refused by the network policy). Bybit pages could therefore only be read through web-search summaries of
those pages. PyPI was reachable and was read directly.

### Verification levels
- **CONFIRMED (primary source read directly)**: checked against the original, machine-readable source.
- **REPORTED BY BYBIT PAGES (via search summary)**: Bybit's own help/learn pages as quoted by a search engine;
  the page itself could not be opened here. Must be re-read before Stage 10.
- **THIRD-PARTY**: non-Bybit sources.
- **UNKNOWN**: no reliable information found.

| # | Fact | Level |
|---|---|---|
| 1 | Python package `MetaTrader5` latest version 5.0.6231 (uploaded 2026-09-27), wheels **win_amd64 only**; official docs at mql5.com | **CONFIRMED** (PyPI JSON API) |
| 2 | `pip download metatrader5` fails on this Linux host | **CONFIRMED** (executed) |
| 3 | Product formerly "Bybit TradFi" is now **"CFD"** (MT5 CFD Account), renamed July 2026 | REPORTED BY BYBIT PAGES |
| 4 | CFD Demo Trading: Bybit Global only, subject to regional availability, 10,000 virtual USDx, **no KYC needed for the demo** (earlier "KYC 1+" came from a search summary and was WRONG) | **CONFIRMED** (Bybit Learn page dated 2026-11-05, read directly on 2026-11-20) |
| 5 | **CFD Demo: "Copy Trading is unsupported at launch, as is MT5 Client, API and Expert Advisors (EA) access"** | **CONFIRMED** (same page) |
| 6 | Live CFD accounts: MT5 desktop client available "for Expert Advisors, advanced charting and algorithmic strategies" | REPORTED BY BYBIT PAGES (CFD trading guide) |
| 7 | Bybit's REST/V5 API automates TradFi **Perpetuals**, not CFD; no CFD-specific API documentation found | REPORTED BY BYBIT PAGES (CFD vs Perps comparison) + absence of evidence |
| 8 | Older MT5 demo setup: server **Bybit-Demo** (live: Bybit-Live); demo server time **UTC+2, UTC+3 in DST**, not changeable | REPORTED BY BYBIT PAGES (MT5 FAQ / MT5 demo guide; publication date unknown) |
| 9 | Gold symbol shown as `XAUUSD+` on Bybit pages; a third-party library uses `XAUUSD.s` | REPORTED BY BYBIT PAGES / THIRD-PARTY — must be read from Market Watch |
| 10 | A third-party article claims the demo includes EAs | THIRD-PARTY, contradicts #5, not CFD-specific |
| 11 | Whether the older "Bybit-Demo" MT5 login still works for the new CFD demo | **UNKNOWN** (#5 vs #8 conflict) |
| 12 | Availability of CFD demo in the owner's region | **UNKNOWN** (secondary sources list EEA/UK/US/Canada restrictions) |
| 13 | Real contract specs (tick value, contract size, swaps, commission, stops level, trading hours) for Bybit CFD XAUUSD | **UNKNOWN** — only obtainable from the MT5 terminal; the project uses clearly labelled MOCK specs |
| 14 | Server time GMT+2; GMT+3 from the 2nd Sunday of March 02:00 UTC to the 1st Sunday of November 02:00 UTC (US DST dates) | **CONFIRMED** (Help Center "How to Get Started With CFD Trading", updated 2026-08-03) |
| 15 | XAUUSD contract size 100; margin 0.2% up to 10M exposure (tiered) | **CONFIRMED** (Help Center "Margin Calculations on CFD Trading", updated 2026-07-30) |
| 16 | Commission (Tight-Spread mode): Forex $6/lot, Metal $6/lot, Oil $3, "Other" $3, charged at position open; Zero-Fee mode: included in spread | **CONFIRMED** (Help Center "Bybit CFD: Fees Explained", updated 2026-07-30) |
| 17 | Swap: FX/metals by points, indices by money, stocks by %, charged past 00:00 server time | **CONFIRMED** (same fees page) |
| 18 | Per-contract min lot, lot step, tick size, swap rates, triple-swap day | **UNKNOWN** — only on each contract's Info page in the Bybit app/website; must be entered by the owner |
| 19 | Any official API that serves Bybit **CFD** quotes | **NOT FOUND** — Bybit V5 covers crypto and TradFi Perpetuals (not allowed as substitute) |

**Verdict: BROKER_DEMO = BLOCKED.** Two independent reasons: (a) this host is Linux and the official MT5 Python
package is Windows-only (confirmed); (b) Bybit reportedly does not provide MT5/API/EA access on the CFD demo (reported, unverified).
Bybit Crypto Futures / TradFi Perpetuals / V5 API are **not** used as a substitute.

Sources: [Bybit CFD Demo Trading](https://www.bybit.com/en/learn/bybit-tradfi/bybit-cfd-demo-trading),
[Bybit CFD trading guide](https://www.bybit.com/en/learn/bybit-tradfi/bybit-cfd-trading-guide),
[CFD vs TradFi Perpetuals](https://www.bybit.com/en/learn/derivatives/tradfi-cfds-vs-tradfi-perpetual-contracts),
[Bybit MT5 demo trading](https://www.bybit.com/en/learn/tradfi/mt5-demo-trading),
[Bybit MT5 FAQ](https://bybit.nl/en/help-center/article/FAQ-MetaTrader-5-MT5),
[Bybit EA introduction](https://www.bybit.com/en/help-center/article/Introduction-to-Expert-Advisors-EAs),
[PyPI MetaTrader5](https://pypi.org/project/MetaTrader5/),
[NexusTrader Bybit TradFi](https://nexustrader.readthedocs.io/en/latest/exchange/bybit_tradfi.html),
[CoinGabbar](https://www.coingabbar.com/en/bybit-cfd-demo-trading-virtual-funds).

## 4. Paths to a real BROKER_DEMO later (owner decision)

- A. Windows host/VM + MT5 terminal logged into a Bybit **demo** — only if Bybit enables MT5 access for the CFD demo
  (check #5/#11 on a machine that can open bybit.com). `broker/mt5_adapter.py` is ready for this.
- B. If the Bybit CFD demo stays web/app-only: BROKER_DEMO stays blocked; PAPER mode remains the automated option.
- C. Another broker's MT5 demo: same adapter would work, but this needs a separate owner decision. Not planned.

## 5. Inventory after Stage 1–2 (what really exists)

See README "Status".

## 6. Quote sources (2026-11-20)

| Source | Status here | Notes |
|---|---|---|
| Bybit CFD | not available | no documented quote API for CFD; demo has no MT5/API |
| OANDA v20 practice API | implemented, **not verified live** | official documented REST API, real-time bid/ask; needs a free practice token; host not reachable from this container without that credential |
| Dukascopy datafeed | implemented, **reachable but HTTP 429** from this container's shared IP on the very first request | public tick history; live data typically delayed (≥ 1 h) → quotes treated as STALE → NO TRADE |
| Swissquote public BBO feed | rejected | undocumented endpoint; the request was blocked as possible unauthorised access |
| MetaTrader 5 (read-only provider) | implemented, not usable on Linux | Windows-only package; useful later to read real Bybit specs from a live MT5 CFD account |
