"""China-market wall-clock helpers."""

from datetime import datetime
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def now_shanghai() -> datetime:
    """Return a naive datetime whose wall clock is fixed to Asia/Shanghai."""
    return datetime.now(SHANGHAI_TZ).replace(tzinfo=None)
