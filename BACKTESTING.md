
# AI Trading Desk — Backtesting

## 1. Objective

Evaluate trading strategies using historical market data before demo deployment.

Never claim profitability without measurable evidence.

## 2. Testing Requirements

Every strategy test must include:
- Trading pair
- Exchange and market type
- Timeframe
- Historical testing period
- Number of trades
- Entry and exit rules
- Stop loss and take profit rules
- Trading fees
- Estimated slippage
- Funding costs where applicable

## 3. Performance Metrics

Calculate:
- Total return
- Win rate
- Profit factor
- Average reward/risk
- Expectancy per trade
- Maximum drawdown
- Average winning trade
- Average losing trade
- Consecutive losses

## 4. Testing Method

1. Define strategy rules before testing.
2. Use historical data.
3. Separate training and testing periods.
4. Perform out-of-sample testing.
5. Include realistic fees and slippage.
6. Check performance across different market conditions.
7. Document all results.

## 5. Overfitting Prevention

- Do not optimize only for historical profit.
- Avoid excessive indicator combinations.
- Do not change rules after every losing trade.
- Keep a separate untouched testing period.
- Record every strategy modification.

## 6. Demo Validation

Minimum initial milestone: 100 documented demo trades.

Evaluate:
- Strategy consistency
- Risk management compliance
- Execution quality
- Actual versus expected results
- Performance across market conditions

100 trades are a testing milestone, not proof of profitability.

## 7. Reporting

Strategy Researcher must produce:
- Test methodology
- Dataset source
- Performance metrics
- Weaknesses
- Market conditions where the strategy fails
- Recommendations

Trade Auditor independently reviews the results.

## 8. Safety

No strategy may move to live trading based only on backtesting results.

Live trading requires separate testing, security review, and explicit user approval.
