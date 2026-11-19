
# AI Trading Desk — Risk Management

## 1. Main Principle

Capital preservation is more important than profit.

No trade is mandatory. Protecting the demo account comes first.

## 2. Initial Risk Limits

- Maximum risk per trade: 1% of demo equity.
- Maximum simultaneous positions: 1.
- Minimum Risk/Reward: 1:2.
- No martingale.
- No averaging down.
- No unlimited position sizing.
- Every trade must have a stop loss.

## 3. Position Sizing

Calculate position size using:

Position Size = Maximum Monetary Risk / Stop Loss Distance

For derivatives, account for contract specifications, leverage, fees, and liquidation risk.

Never confuse leverage with acceptable risk.

## 4. Daily Protection

- Stop opening new trades after 2 consecutive losses.
- Stop trading for the day after a 2% daily equity drawdown.
- Investigate unusual losses or execution errors.
- Resume only after the Risk Manager approves.

## 5. Risk Manager Authority

Risk Manager has independent veto authority.

Risk Manager must reject a trade if:
- Stop loss is missing.
- Position size is incorrect.
- Risk exceeds limits.
- Risk/Reward is below 1:2.
- Market data is unreliable.
- Exchange or execution status is uncertain.
- A critical safety rule is violated.

## 6. Exchange Safety

For future exchange integration:
- Use demo or testnet credentials first.
- Use separate API credentials for testing.
- Never enable withdrawal permissions.
- Use the minimum permissions required.
- Never expose API secrets in logs or reports.
- Verify exchange symbol, contract size, and order status.
- Confirm stop loss and take profit protection.

## 7. Emergency Stop

Immediately disable new demo orders if:
- API behaves unexpectedly.
- Duplicate orders appear.
- Position size differs from the approved size.
- Stop loss fails.
- Market data stops updating.
- Account equity cannot be verified.

## 8. Reporting

After each trade report:
- Planned risk
- Actual risk
- Result in R
- Fees and slippage
- Rule violations
- Account equity
- Current drawdown

## 9. Live Trading Restriction

Live trading is prohibited during initial development.

Any future live trading requires:
1. At least 100 documented demo trades.
2. Independent performance review.
3. Backtesting and out-of-sample testing.
4. Security review.
5. Explicit user approval.
