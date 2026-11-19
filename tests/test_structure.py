from helpers import candle, series
from tradedesk.analysis import structure as ms
from tradedesk.analysis.indicators import atr, efficiency_ratio, ema


def zigzag(points, steps=4):
    closes = []
    for a, b in zip(points, points[1:]):
        closes += [a + (b - a) * k / steps for k in range(steps)]
    return closes + [points[-1]]


def test_swings_confirmed_only_after_k_bars():
    bars = series([10, 11, 12, 13, 12, 11, 10, 11, 12])
    sw = ms.find_swings(bars, k=2)
    hi = [s for s in sw if s.kind == "HIGH"][0]
    assert hi.index == 3 and hi.confirmed_at == 5


def test_hh_hl_labels_in_uptrend():
    bars = series(zigzag([100, 110, 105, 115, 108, 120, 112, 125]))
    labels = [s.label for s in ms.find_swings(bars, 2) if s.label]
    assert "HH" in labels and "HL" in labels and "LL" not in labels


def test_bos_then_choch():
    up = zigzag([100, 110, 105, 115, 108, 120, 112, 125])
    down = zigzag([125, 104, 110, 95])          # breaks below last HL (112) then 108...
    res = ms.analyze(series(up + down[1:]), k=2, displacement_atr=0.1)
    kinds = [(b.kind, b.direction) for b in res.breaks]
    assert ("BOS", "BULLISH") in kinds
    assert ("CHOCH", "BEARISH") in kinds
    ch = [b for b in res.breaks if b.kind == "CHOCH"][0]
    assert res.trend_at(ch.index) == "BEARISH" and res.trend_at(ch.index - 1) == "BULLISH"


def test_break_uses_only_confirmed_swings():
    up = zigzag([100, 110, 105, 115, 108, 120, 112, 125])
    bars = series(up)
    res = ms.analyze(bars, k=2)
    conf = {s.index: s.confirmed_at for s in res.swings}
    for b in res.breaks:
        assert conf[b.swing_index] <= b.index


def test_fvg_definition():
    bars = [candle(0, 10, 11, 9.5, 10.8), candle(1, 10.8, 14, 10.7, 13.8), candle(2, 13.8, 15, 12, 14.5)]
    res = ms.analyze(bars + [candle(3 + i, 14.5, 15, 14, 14.5) for i in range(5)], k=1)
    f = [z for z in res.fvgs if z.direction == "BULLISH"][0]
    assert f.index == 2 and f.low == 11 and f.high == 12


def test_liquidity_sweep_definition():
    closes = zigzag([100, 95, 105, 98, 108])
    bars = series(closes, wick=0.2)
    res0 = ms.analyze(bars, k=2)
    low = [s for s in res0.swings if s.kind == "LOW"][-1]
    n = len(bars)
    sweep = candle(n, 107, 107.5, low.price - 1.0, 107.2)
    res = ms.analyze(bars + [sweep], k=2)
    assert any(s.direction == "BULLISH" and s.index == n for s in res.sweeps)


def test_order_block_is_last_opposite_candle_and_requires_displacement():
    up = zigzag([100, 110, 105, 115, 108, 125], steps=5)
    res = ms.analyze(series(up), k=2, displacement_atr=0.5)
    obs = [o for o in res.order_blocks if o.direction == "BULLISH"]
    assert obs, "expected a bullish OB"
    bars = series(up)
    o = obs[-1]
    assert bars[o.origin_index].close < bars[o.origin_index].open
    none = ms.analyze(series(up), k=2, displacement_atr=50)
    assert none.order_blocks == []


def test_indicators_are_causal():
    closes = [float(x) for x in range(1, 60)]
    e_full = ema(closes, 10)
    e_part = ema(closes[:30], 10)
    assert e_full[:30] == e_part
    bars = series(closes)
    assert atr(bars)[:30] == atr(bars[:30])
    assert efficiency_ratio(closes, 10, 20) == 1.0
