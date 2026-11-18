
# AI Trading Desk

A multi-agent cryptocurrency trading research and demo-trading system.

## Mission

Build a professional AI trading team that researches crypto markets, debates trade opportunities, manages risk, tests strategies, and maintains a transparent trade journal.

## Initial Mode

DEMO ONLY.

- No real-money trading.
- No live order execution.
- No Bybit integration.
- Initial exchange candidate: BingX Demo.
- Alternative exchange candidate: OKX Demo.
- Never request or expose API secrets in chat.

## Team

1. Head Trader — coordinates the team and makes final trade recommendations.
2. Technical Analyst — analyzes market structure, BOS, CHoCH, order blocks, liquidity, and key levels.
3. Quant Analyst — evaluates volatility, volume, correlations, funding, and open interest.
4. Risk Manager — independently checks risk and can veto any trade.
5. News Analyst — monitors crypto and macroeconomic events.
6. Strategy Researcher — develops and backtests strategies.
7. Trade Auditor — reviews every trade and maintains the journal.
8. Devil's Advocate — challenges trade ideas and searches for reasons to stay out.

## Decision Process

1. Analysts independently investigate the market.
2. They share findings and challenge each other's conclusions.
3. Risk Manager checks every proposed trade.
4. Head Trader issues a final recommendation.
5. Execution remains disabled during the initial research phase.

## Safety Rules

- Never invent market data or test results.
- If data is missing, report NOT ENOUGH DATA.
- NO TRADE is a valid decision.
- Every proposal must include entry, stop loss, take profit, risk/reward, and reasoning.
- Backtest before changing strategy rules.
- Record every demo trade, including losses.
- Never enable live trading without explicit user approval.

## Development Roadmap

1. Create team instructions and agent roles.
2. Establish communication and reporting protocols.
3. Connect reliable market data.
4. Build historical backtesting.
5. Connect a demo exchange environment.
6. Evaluate at least 100 demo trades.
7. Consider live trading only after a separate safety review.
