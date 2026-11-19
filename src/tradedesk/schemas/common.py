from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator


def _require_utc(v: datetime) -> datetime:
    if v.tzinfo is None or v.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware (UTC)")
    if v.utcoffset().total_seconds() != 0:
        v = v.astimezone(timezone.utc)
    return v


UtcDatetime = Annotated[datetime, AfterValidator(_require_utc)]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
