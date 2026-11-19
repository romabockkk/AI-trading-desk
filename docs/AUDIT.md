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

## 3. Bybit CFD / MT5 findings

Bybit pages could not be fetched directly (DNS blocked). Findings come from web-search summaries of Bybit's own pages, so treat them as **UNVERIFIED until re-read on a machine with access**:

1. Bybit renamed the product "TradFi" to **"CFD"** (MT5 CFD Account), reportedly in July 2026.
2. **CFD Demo Trading**: Bybit Global only, subject to regional availability, requires KYC level 1+, credited with 10,000 virtual USDx.
3. **Critical:** the Bybit help page *How to get started with Bybit CFD Demo Trading* reportedly says Copy Trading, **MT5 Client, API and Expert Advisors (EA) access are not supported** for CFD Demo at launch. If true, no automated BROKER_DEMO order path exists today.
4. Older Bybit MT5 demo guides mention server `Bybit-Demo` and gold as `XAUUSD+`; a third-party library uses `XAUUSD.s`. Exact symbol and server must be read from the MT5 terminal; nothing is hard-coded except the owner-editable allowlist.
5. The MetaTrader5 Python package requires Windows and a running MT5 terminal.
6. Region availability for the owner: **UNKNOWN** (secondary sources say EEA/UK/US/Canada are restricted). Owner must check on the Bybit sign-up screen.
7. Bybit V5 crypto API is not a substitute and is not used.

**Verdict: BROKER_DEMO = BLOCKED.** Reasons are reported live by `GET /api/status` → `broker_demo.reasons`.

Sources: [Bybit CFD Demo Trading](https://www.bybit.com/en/learn/bybit-tradfi/bybit-cfd-demo-trading), [What is Bybit CFD](https://www.bybit.com/en/learn/bybit-tradfi/what-is-bybit-cfd), [Bybit MT5 demo trading](https://www.bybit.com/en/learn/tradfi/mt5-demo-trading), [NexusTrader Bybit TradFi docs](https://nexustrader.readthedocs.io/en/latest/exchange/bybit_tradfi.html).

## 4. Paths to a real BROKER_DEMO later (owner decision)

- A. Windows host (or VM) with MT5 terminal logged into a Bybit CFD **demo** — only works if Bybit enables MT5 client/API for demo.
- B. If Bybit demo stays app/web-only: BROKER_DEMO remains blocked; PAPER mode with broker-matched specs is the only automated option.
- C. Another regulated broker's MT5 demo would need a separate owner decision. Not planned.

## 5. Inventory after Stage 1–2 (what really exists)

See README "Status". Agents, data feeds, strategies, risk gate, paper execution and dashboard **do not exist yet**.
