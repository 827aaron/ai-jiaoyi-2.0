"""启动时回填 1 分钟 K 线，让 ATR 和均量不用等 15 分钟。

只读公开行情接口，不需要 key。用标准库 urllib，放在线程池里跑，不阻塞快环。
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from typing import Any

from jiaoyi.contracts import Interval, Kline

DEFAULT_REST_URL = "https://fapi.binance.com"


def parse_klines(
    symbol: str, rows: list[list[Any]], now: datetime, interval: Interval = Interval.M1
) -> list[Kline]:
    """``/fapi/v1/klines`` 的原始行 → 已收盘的 ``Kline``（收盘时间晚于 ``now`` 的丢掉）。"""
    out: list[Kline] = []
    for r in rows:
        close_time = datetime.fromtimestamp(int(r[6]) / 1000.0, UTC)
        if close_time > now:
            continue
        out.append(
            Kline(
                symbol=symbol,
                interval=interval,
                open_time=datetime.fromtimestamp(int(r[0]) / 1000.0, UTC),
                close_time=close_time,
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
                quote_volume=float(r[7]),
            )
        )
    return out


def _get(url: str, timeout_s: float) -> list[list[Any]]:
    with urllib.request.urlopen(url, timeout=timeout_s) as resp:
        data: list[list[Any]] = json.loads(resp.read())
    return data


async def fetch_recent_klines(
    symbol: str,
    *,
    limit: int = 60,
    interval: Interval = Interval.M1,
    base_url: str = DEFAULT_REST_URL,
    timeout_s: float = 5.0,
) -> list[Kline]:
    q = urllib.parse.urlencode({"symbol": symbol, "interval": interval.value, "limit": limit})
    rows = await asyncio.to_thread(_get, f"{base_url}/fapi/v1/klines?{q}", timeout_s)
    return parse_klines(symbol, rows, datetime.now(UTC), interval)
