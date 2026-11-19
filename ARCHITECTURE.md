# AI Trading Desk — Architecture

## 1. What we borrowed and from where

The desk is built from the strongest ideas of well-known open-source trading systems and research papers. Only ideas were taken, not code.

| Source | Idea taken | Where in this repo |
|---|---|---|
| **TradingAgents** (TauricResearch, LangGraph multi-agent "trading firm") | Analyst team → bull/bear challenge → trader → risk team → portfolio manager pipeline; structured (typed) agent outputs; a persistent decision log that is "settled" later and turned into reflections | `orchestrator.py`, `models.py`, `memory.py` |
| **ai-hedge-fund** (virattt) | An explicit approval step before any order is executed, a kill switch, and a hash-chained ledger of every session | `cli.py` (`_ask`), `state/KILL`, `journal.py` |
| **Freqtrade** | Dry-run first; account protections *StoplossGuard*, *MaxDrawdown*, *CooldownPeriod*; fees and slippage in backtests | `protections.py`, `backtest/engine.py` |
| **NautilusTrader** | Backtest/live parity: the same strategy, sizing and protection code runs in both | `analysis/smc.py`, `sizing.py` and `protections.py`, used by both |
| **FinMem / FinAgent** (LLM trading-agent papers) | Layered memory (recent memories for the same instrument plus long-term statistics per setup) and reflection after each trade | `memory.py`, `TradeAuditor.review_closed` |

What we deliberately did **not** copy:
- **LLMs making the trade decision.** All 8 agents are deterministic, testable rules. An optional Claude reviewer (`DESK_LLM=1`) can only *add* objections. It can never approve a trade or override the Risk Manager.
- **Majority voting.** The Risk Manager's veto is absolute (TEAM_RULES.md §6).

## 2. Research cycle (one per instrument, every closed M15 candle)

```
              ┌──────────────── shared Workspace (blackboard, persisted) ────────────────┐
 OKX data ──▶ │ Technical ─▶ Quant ─▶ News ─▶ Strategy ─▶ Devil's Advocate ─▶ (Claude) ─▶ │
 validate     │                                     Risk Manager (VETO) ─▶ Head Trader   │
              └───────────────────────────────────────────────┬─────────────────────────┘
                                                              ▼
                                   Trade Auditor ─▶ hash-chained journal (decision recorded first)
                                                              ▼
                                   approval gate (y/N or --auto-approve) ─▶ OKX DEMO executor
                                                              ▼
                                next cycle: reconcile orders/positions ─▶ post-trade review ─▶ memory
```

Every agent writes an `AgentReport` (agent, timestamps, findings, evidence, sources, confidence, objections, next action), as required by AGENT_WORKFLOW.md §1. Later agents read the earlier reports:
- The Devil's Advocate collects every other agent's objections and adds its own.
- The Risk Manager reads the proposal plus all reports.
- The Head Trader decides using only what is in the workspace.

The full transcript of each cycle is saved to `workspace/cycles/<id>_<instrument>/transcript.md`.

### Decision rules (Head Trader)
| Condition | Decision |
|---|---|
| Market data failed validation (gaps, stale candles, missing bars) | NOT ENOUGH DATA |
| No setup / setup stale or invalid | REJECTED (NO TRADE) |
| Sweep seen, CHoCH not yet | WAIT FOR CONFIRMATION |
| Risk Manager veto | REJECTED (cannot be overridden) |
| Any CRITICAL objection, ≥2 MAJOR objections, or 1 MAJOR objection plus Quant OPPOSE | REJECTED |
| Otherwise | APPROVED FOR DEMO |

## 3. The eight agents

| Agent | What it actually computes |
|---|---|
| Technical Analyst | STRATEGY.md v1.0 as objective rules (`analysis/smc.py`): H4 structure → H1 liquidity sweep → M15 CHoCH → FVG/Order-Block entry, structural SL, TP at opposing liquidity (≥2R, capped at 4R) |
| Quant Analyst | ATR volatility percentile, relative volume, H1 EMA50 slope, funding rate, open interest |
| News Analyst | OKX announcements (delistings, suspensions, maintenance) plus a user macro calendar `config/events.json` with blackout windows. It reports its limitation honestly: no general news feed is connected |
| Strategy Researcher | Checks the setup matches the tested rule version; reads the latest backtest (out-of-sample expectancy); compares it with live demo results and memory |
| Devil's Advocate | Liquidity obstacles before 2R, shallow sweeps, marginal R:R, far entries, losing streaks; collects every other objection |
| Risk Manager | Protections, kill switch, max 1 position/order, SL side, R:R ≥ 2, fee-inclusive sizing ≤1% of equity, margin and liquidation distance. **Veto** |
| Head Trader | Final decision with a written reason (table above) |
| Trade Auditor | Journals every decision before any order; verifies the hash chain; post-trade review (result in R, rule violations) → memory |

## 4. OKX demo integration (`data/okx_client.py`, `execution/okx_executor.py`)

- REST v5, HMAC-SHA256 signing. **Every private request carries `x-simulated-trading: 1`** and there is no code path that removes it, so this client cannot touch a live account.
- Orders are USDT-margined perpetual swaps in isolated margin at configurable leverage (default 3x, hard-capped at 5x). Each order is a limit entry at the zone midpoint with an attached TP/SL (`attachAlgoOrds`, market exits; the SL triggers on mark price).
- A deterministic `clOrdId` per proposal, plus a check of open orders and positions on the exchange, prevents duplicate orders.
- After sending, the order is re-read. After a fill, the attached SL is confirmed and the position size is compared with the approved size. Any mismatch activates the kill switch.
- Unfilled entries are cancelled after 8 M15 bars (2 hours).

## 5. Known limitations (honest list)
- The demo results and backtests so far are **not** evidence of profitability. See `workspace/backtests/` for the current numbers.
- The backtest does not model historical funding/OI filters, news, partial fills or outages.
- The News Analyst has no general news or sentiment feed yet.
- PnL attribution on close uses OKX `positions-history`. This is correct while only one position is open at a time, which the rules enforce.
- `DATA_SOURCES.md` appears truncated (it starts at §7). Sections 1–6 should be restored by the author.
