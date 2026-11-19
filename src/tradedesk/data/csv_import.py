"""Import historical candles exported from MetaTrader 5 (History Center / "Export Bars").

MT5 export format (tab separated, header row):
  <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>
  2026.01.05  01:00:00  2650.10 ...
Timestamps are in the *broker server time zone*, not UTC. Bybit's MT5 demo guide
states server time is UTC+2 (UTC+3 during DST); which DST rule applies is not
documented, so the owner must pass the zone explicitly (IANA name) or a fixed offset.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..schemas.market import Candle, DataSource, Timeframe


class CsvImportError(ValueError):
    pass


def _tz(server_tz: str):
    if server_tz.upper().startswith("UTC") and len(server_tz) > 3:
        sign = 1 if server_tz[3] == "+" else -1
        hours = float(server_tz[4:])
        return timezone(sign * timedelta(hours=hours))
    if server_tz.upper() == "UTC":
        return timezone.utc
    return ZoneInfo(server_tz)


def import_mt5_csv(path: str | Path, symbol: str, timeframe: Timeframe, server_tz: str) -> tuple[list[Candle], DataSource]:
    p = Path(path)
    tz = _tz(server_tz)
    text = p.read_text(encoding="utf-8-sig")
    delim = "\t" if "\t" in text.splitlines()[0] else ","
    rows = list(csv.reader(text.splitlines(), delimiter=delim))
    if not rows:
        raise CsvImportError("empty file")
    header = [h.strip("<> ").upper() for h in rows[0]]
    need = {"DATE", "OPEN", "HIGH", "LOW", "CLOSE"}
    if not need <= set(header):
        raise CsvImportError(f"missing columns {need - set(header)}; got {header}")
    ix = {h: i for i, h in enumerate(header)}
    out: list[Candle] = []
    for n, r in enumerate(rows[1:], start=2):
        if not r or all(not x.strip() for x in r):
            continue
        try:
            ds = r[ix["DATE"]].strip()
            ts = r[ix["TIME"]].strip() if "TIME" in ix else "00:00:00"
            local = datetime.strptime(f"{ds} {ts}", "%Y.%m.%d %H:%M:%S" if "." in ds else "%Y-%m-%d %H:%M:%S")
            t = local.replace(tzinfo=tz).astimezone(timezone.utc)
            out.append(Candle(
                symbol=symbol, timeframe=timeframe, open_time_utc=t,
                open=float(r[ix["OPEN"]]), high=float(r[ix["HIGH"]]), low=float(r[ix["LOW"]]),
                close=float(r[ix["CLOSE"]]),
                tick_volume=float(r[ix["TICKVOL"]]) if "TICKVOL" in ix else 0.0,
                spread_points=float(r[ix["SPREAD"]]) if "SPREAD" in ix else None,
                is_closed=True))
        except (ValueError, IndexError) as e:
            raise CsvImportError(f"line {n}: {e}") from e
    return out, DataSource(name=f"CSV:{p.name}:tz={server_tz}", is_mock=False)
