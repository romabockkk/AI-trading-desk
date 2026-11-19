
# AI Trading Desk — Agent Workflow

## 1. Shared Workspace

All agents must use a shared workspace for communication.

Each report must contain:
- Agent name
- Timestamp
- Market data timestamp
- Trading pairs analyzed
- Findings
- Evidence and sources
- Confidence level
- Recommended next action

Agents must never pretend to communicate if no communication mechanism is connected.

## 2. Research Cycle

1. Head Trader starts a research cycle.
2. Technical Analyst identifies potential setups.
3. Quant Analyst independently checks market conditions.
4. News Analyst checks relevant news and events.
5. Strategy Researcher checks strategy compatibility.
6. Devil's Advocate searches for weaknesses.
7. Risk Manager evaluates risk and may veto.
8. Head Trader publishes the final decision.

## 3. Discussion Protocol

- Agents must read relevant reports before responding.
- Agents must identify contradictions between reports.
- Agents must explain disagreements with evidence.
- Agents must not blindly agree with the Head Trader.
- New evidence must be shared with all relevant agents.
- Risk Manager's veto cannot be overridden by a majority vote.

## 4. Final Decision

Every research cycle must end with one of:

- APPROVED FOR DEMO
- REJECTED
- WAIT FOR CONFIRMATION
- NOT ENOUGH DATA

Approval means only that a setup qualifies for demo testing. It does not authorize live trading.

## 5. Demo Trade Feedback

After a demo trade closes:

1. Trade Auditor records the result.
2. Technical Analyst reviews the entry and exit.
3. Quant Analyst updates statistics.
4. Risk Manager checks rule compliance.
5. Strategy Researcher identifies possible improvements.
6. Devil's Advocate checks whether the result reveals a weakness.
7. Head Trader publishes a short review.

## 6. Continuous Improvement

- Never change strategy rules based on one trade.
- Record every change and its reason.
- Test changes against historical data.
- Compare results with the previous strategy.
- Keep a version history.
- Do not optimize only for past profitability.

## 7. Failure Handling

If an agent, data source, API, or communication mechanism fails:

- Report the failure clearly.
- Do not invent missing results.
- Do not execute trades.
- Notify the Head Trader.
- Resume only after the problem is verified as fixed.
