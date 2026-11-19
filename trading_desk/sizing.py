"""Single position-sizing function shared by the live Risk Manager and the backtester.

    contracts = risk money / (loss per contract at the stop, incl. fees & slippage)

If the margin needed at the configured leverage exceeds 90% of available balance, the
size is reduced to fit (risk then stays BELOW the 1% limit, never above). If that leaves
less than ``min_risk_fraction`` of the intended risk, the trade is rejected: the stop is
too tight for this account size and the setup is effectively un-tradeable.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SizeResult:
    contracts: float
    risk_usd: float
    notional: float
    margin: float
    capped_by_margin: bool
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.contracts > 0 and not self.reason


def position_size(entry: float, stop: float, equity: float, available: float, risk_pct: float,
                  leverage: int, ct_val: float, lot: float, min_sz: float,
                  taker_fee: float, slippage: float, min_risk_fraction: float = 0.25) -> SizeResult:
    risk_money = equity * risk_pct / 100
    per_contract = ct_val * (abs(entry - stop) + entry * taker_fee + stop * (taker_fee + slippage))
    if per_contract <= 0 or equity <= 0:
        return SizeResult(0, 0, 0, 0, False, "invalid inputs")
    contracts = math.floor(risk_money / per_contract / lot + 1e-9) * lot
    capped = False
    max_by_margin = math.floor(0.9 * available * leverage / (ct_val * entry) / lot + 1e-9) * lot
    if contracts > max_by_margin:
        contracts, capped = max_by_margin, True
    contracts = round(contracts, 8)
    risk_usd = contracts * per_contract
    notional = contracts * ct_val * entry
    res = SizeResult(contracts, round(risk_usd, 2), notional, notional / leverage, capped)
    if contracts < min_sz:
        res.reason = f"size below exchange minimum {min_sz}"
    elif capped and risk_usd < min_risk_fraction * risk_money:
        res.reason = (f"stop too tight for account: margin cap leaves {risk_usd:.2f} USDT risk "
                      f"(< {min_risk_fraction:.0%} of {risk_money:.2f})")
    return res
