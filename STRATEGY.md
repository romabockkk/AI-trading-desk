
# AI Trading Desk — Initial Trading Strategy

## Strategy Version
Version 1.0 — Demo Testing

## 1. Main Approach

The strategy combines:
- Higher-timeframe market structure
- Break of Structure (BOS)
- Change of Character (CHoCH)
- Order Blocks (OB)
- Liquidity Sweeps
- Fair Value Gaps (FVG)
- Risk/Reward confirmation

The team must use objective rules and avoid subjective entries.

## 2. Timeframes

- H4: Main market trend and structure.
- H1: Setup identification and important levels.
- M15: Confirmation of the setup.
- M5: Optional entry refinement after confirmation.

Do not enter solely because price touches an Order Block.

## 3. Market Direction

For LONG:
- Prefer bullish H4 structure.
- Identify higher highs and higher lows.
- Look for bullish CHoCH or BOS on the lower timeframe.

For SHORT:
- Prefer bearish H4 structure.
- Identify lower highs and lower lows.
- Look for bearish CHoCH or BOS on the lower timeframe.

If higher and lower timeframes strongly conflict, wait.

## 4. Liquidity Sweep

Look for price taking liquidity above or below:
- Previous swing highs
- Previous swing lows
- Equal highs
- Equal lows

A liquidity sweep alone is not an entry signal.

## 5. Order Block

A valid Order Block must:
- Be associated with a meaningful displacement.
- Be relevant to the current market structure.
- Have a clear invalidation level.
- Be evaluated together with liquidity and CHoCH/BOS.

Avoid treating every opposite-colored candle as an Order Block.

## 6. Entry Confirmation

LONG setup:
1. Identify bullish higher-timeframe context.
2. Wait for a downside liquidity sweep.
3. Look for bullish CHoCH on M15 or M5.
4. Identify a valid bullish Order Block or FVG.
5. Enter only after confirmation and a defined stop loss.

SHORT setup:
1. Identify bearish higher-timeframe context.
2. Wait for an upside liquidity sweep.
3. Look for bearish CHoCH on M15 or M5.
4. Identify a valid bearish Order Block or FVG.
5. Enter only after confirmation and a defined stop loss.

## 7. Stop Loss

- Place SL beyond the structural invalidation point.
- Do not place SL at an arbitrary fixed percentage.
- Consider volatility and spread.
- Reject setups where the stop is unclear.

## 8. Take Profit

- Minimum planned Risk/Reward: 1:2.
- Consider opposing liquidity and important swing levels.
- Do not move SL farther away to avoid a loss.
- Record partial exits separately.

## 9. Demo Risk Management

- Maximum planned risk: 1% of demo equity per trade.
- Maximum simultaneous open positions: 1.
- No martingale.
- No averaging down.
- No entry without SL.
- Risk Manager can reject any setup.

## 10. No-Trade Conditions

Do not trade when:
- Market data is missing or stale.
- Market structure is unclear.
- There is no valid confirmation.
- Risk/Reward is below 1:2.
- Major news creates unacceptable uncertainty.
- Stop loss cannot be defined.
- Analysts identify unresolved critical contradictions.

## 11. Testing Requirements

For every demo trade record:
- Pair
- Date and time
- Timeframes
- Market direction
- Liquidity sweep
- CHoCH/BOS
- Order Block or FVG
- Entry
- Stop Loss
- Take Profit
- Risk/Reward
- Result in R
- Fees and slippage assumptions
- Reason for entry
- Reason for exit
- Screenshots or market data references

Do not claim the strategy is profitable until sufficient out-of-sample testing has been completed.

Initial milestone: 100 documented demo trades.
