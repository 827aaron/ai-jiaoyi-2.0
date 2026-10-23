"""共享市场简报：每轮开会前算一次，所有标的的会议复用（设计优化 (3)）。

内容：BTC / ETH 趋势（币安日线与 4 小时线）、恐慌贪婪指数（alternative.me，免费无 key）。
美元指数（DXY）没有稳定的免费无 key 来源（试过的站点会拦截云端 / 脚本请求），先不接，
需要时加一个 ``BriefPart`` 即可，不用改别处。

每一部分独立失败：拿不到只在简报里写“暂不可用”，其余照常；整份简报也有缓存，
一轮里多次调用不会重复请求。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import httpx

_FAPI = "https://fapi.binance.com"
_FNG = "https://api.alternative.me/fng/"

BriefPart = Callable[[httpx.AsyncClient], Awaitable[str]]


def _sma(xs: Sequence[float], n: int) -> float:
    tail = xs[-n:]
    return sum(tail) / len(tail)


def _rsi(closes: Sequence[float], n: int = 14) -> float:
    gains = [max(b - a, 0.0) for a, b in pairwise(closes)]
    losses = [max(a - b, 0.0) for a, b in pairwise(closes)]
    ag, al = sum(gains[-n:]) / n, sum(losses[-n:]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


def describe_trend(name: str, daily: Sequence[float], h4: Sequence[float]) -> str:
    """日线 / 4 小时收盘价 → 一行趋势描述。纯函数，便于测试。"""
    last = daily[-1]
    chg_1d = (last / daily[-2] - 1) * 100 if len(daily) >= 2 else 0.0
    chg_7d = (last / daily[-8] - 1) * 100 if len(daily) >= 8 else 0.0
    sma20 = _sma(daily, 20)
    side = "上方" if last >= sma20 else "下方"
    rsi = _rsi(h4) if len(h4) > 14 else float("nan")
    return (
        f"- {name}：{last:,.1f}，24h {chg_1d:+.1f}%，7d {chg_7d:+.1f}%，"
        f"位于 20 日均线{side}（{sma20:,.1f}），4h RSI {rsi:.0f}"
    )


async def _closes(c: httpx.AsyncClient, symbol: str, interval: str, limit: int) -> list[float]:
    r = await c.get(
        f"{_FAPI}/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit}
    )
    r.raise_for_status()
    return [float(k[4]) for k in r.json()]


def trend_part(symbol: str, name: str) -> BriefPart:
    async def part(c: httpx.AsyncClient) -> str:
        daily, h4 = await asyncio.gather(_closes(c, symbol, "1d", 30), _closes(c, symbol, "4h", 30))
        return describe_trend(name, daily, h4)

    return part


async def fear_greed_part(c: httpx.AsyncClient) -> str:
    r = await c.get(_FNG, params={"limit": 2})
    r.raise_for_status()
    rows: list[dict[str, Any]] = r.json()["data"]
    now, prev = rows[0], rows[1] if len(rows) > 1 else None
    tail = f"（前一日 {prev['value']}）" if prev else ""
    return f"- 恐慌贪婪指数：{now['value']}（{now['value_classification']}）{tail}"


def default_parts() -> list[BriefPart]:
    return [trend_part("BTCUSDT", "BTC"), trend_part("ETHUSDT", "ETH"), fear_greed_part]


class MarketBriefService:
    """``refresh`` 每轮开会前调一次（太近则用缓存）；``text`` 同步读，不可变字符串所以线程安全。"""

    def __init__(
        self,
        parts: Sequence[BriefPart] | None = None,
        client: httpx.AsyncClient | None = None,
        min_age: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._parts = list(parts) if parts is not None else default_parts()
        self._c = client or httpx.AsyncClient(timeout=10.0)
        self._min_age = min_age
        self._clock = clock
        self._text = ""
        self._at: datetime | None = None
        self.failed_parts = 0

    @property
    def text(self) -> str:
        """当前简报；还没生成过或全部失败时为空串。"""
        return self._text

    async def refresh(self) -> str:
        now = self._clock()
        if self._at is not None and now - self._at < self._min_age:
            return self._text
        results = await asyncio.gather(*(p(self._c) for p in self._parts), return_exceptions=True)
        lines = [r if isinstance(r, str) else "- （一项数据暂不可用）" for r in results]
        self.failed_parts = sum(1 for r in results if not isinstance(r, str))
        if self.failed_parts == len(self._parts):
            # 全部失败：保留上一份（可能为空），不更新时间，下一轮再试
            return self._text
        self._text = f"## 市场简报（{now:%Y-%m-%d %H:%M} UTC，本轮所有会议共用）\n" + "\n".join(
            lines
        )
        self._at = now
        return self._text

    async def aclose(self) -> None:
        await self._c.aclose()
