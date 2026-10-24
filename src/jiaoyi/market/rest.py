"""启动时回填 1 分钟 K 线，让 ATR 和均量不用等 15 分钟；复盘 / 回放补结果 K 线。

只读公开行情接口，不需要 key。用标准库 urllib，放在线程池里跑，不阻塞快环。

限流：币安按 IP 算请求权重（每分钟 2400），超了先回 429，接着还打就回 418 封 IP（2 分钟到 3 天），
同一台机器上跑着的交易程序也会一起被封。所以：

- 遇到 429 / 418 读 ``Retry-After``，抛 :class:`RateLimited`，并且记住到期时间：这之前本进程
  所有请求直接抛同一个错，不再去敲币安；
- 离线工具（复盘、回放、校准）传一个 :class:`WeightBudget`：按每分钟权重预算排队（默认只用
  300，大头留给交易程序），响应头里本 IP 已用权重过半也先等到下一分钟。
"""

from __future__ import annotations

import asyncio
import email.message
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
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


IP_WEIGHT_PER_MIN = 2400
"""币安 U 本位每个 IP 每分钟的请求权重上限。"""
DEFAULT_RETRY_AFTER_S = {429: 60, 418: 120}
"""币安没给 ``Retry-After`` 时的保守等待。"""


class RateLimited(Exception):
    """被币安限流（429）或封 IP（418）。``until`` 之前别再请求。"""

    def __init__(self, status: int, until: float) -> None:
        self.status = status
        self.until = until
        super().__init__(f"HTTP {status}：被币安限流，{self.wait_min()} 分钟后再试")

    def wait_min(self, now: float | None = None) -> int:
        left = self.until - (time.time() if now is None else now)
        return max(1, -(-int(left) // 60))


_blocked: RateLimited | None = None
"""本进程最近一次被限流；到期前所有请求直接抛它（到期后留着当记录，不再拦）。"""
_used_weight: int | None = None
"""最近一次响应头里本 IP 这一分钟已用的权重（含同机交易程序）。"""


def _retry_after(headers: email.message.Message | None, status: int) -> float:
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        return max(float(raw), 1.0) if raw else float(DEFAULT_RETRY_AFTER_S[status])
    except ValueError:
        return float(DEFAULT_RETRY_AFTER_S[status])


def check_blocked(now: float | None = None) -> None:
    """还在限流期内就抛 :class:`RateLimited`。"""
    if _blocked is not None and (time.time() if now is None else now) < _blocked.until:
        raise _blocked


def blocked() -> RateLimited | None:
    """本进程最近一次被限流（到期了也留着，给命令行收尾时说一句）。"""
    return _blocked


def mark_blocked(status: int, headers: Any) -> RateLimited:
    """别的请求通道（慢环分析师的 httpx）收到 429 / 418 时登记，全进程共用同一个封禁状态。"""
    global _blocked
    until = time.time() + _retry_after(headers, status)
    if _blocked is None or until > _blocked.until:
        _blocked = RateLimited(status, until)
    return _blocked


def note_used_weight(header: str | None) -> None:
    """记下响应头 ``X-MBX-USED-WEIGHT-1M``（本 IP 这一分钟已用权重，含同机其他程序）。"""
    global _used_weight
    _used_weight = int(header) if header and header.isdigit() else None


def take_used_weight() -> int | None:
    """取走并清掉最近的已用权重读数（清掉是为了等过一个窗口后不再重复等）。"""
    global _used_weight
    used, _used_weight = _used_weight, None
    return used


def _get(url: str, timeout_s: float) -> list[list[Any]]:
    global _blocked, _used_weight
    check_blocked()
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
            _used_weight = int(used) if used and used.isdigit() else None
            data: list[list[Any]] = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in DEFAULT_RETRY_AFTER_S:
            _blocked = RateLimited(e.code, time.time() + _retry_after(e.headers, e.code))
            raise _blocked from None
        raise
    return data


def klines_weight(limit: int) -> int:
    """``/fapi/v1/klines`` 一次请求的权重：按 limit 分档。"""
    return 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10


class WeightBudget:
    """离线工具用的权重预算：滚动 60 秒内最多花 ``per_min``；本 IP 已用过半也先等。"""

    def __init__(
        self,
        per_min: int = 300,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.per_min = per_min
        self._clock = clock
        self._sleep = sleep
        self._spent: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    async def take(self, weight: int) -> None:
        global _used_weight
        async with self._lock:
            check_blocked()
            if _used_weight is not None and _used_weight > IP_WEIGHT_PER_MIN // 2:
                _used_weight = None
                await self._sleep(60.0 - time.time() % 60.0 + 1.0)  # 等到币安下一分钟窗口
            while True:
                now = self._clock()
                while self._spent and now - self._spent[0][0] >= 60.0:
                    self._spent.popleft()
                if sum(w for _, w in self._spent) + weight <= self.per_min:
                    self._spent.append((now, weight))
                    return
                await self._sleep(60.0 - (now - self._spent[0][0]) + 0.01)


async def _page(url: str, timeout_s: float, weight: int, budget: WeightBudget | None) -> Any:
    if budget is not None:
        await budget.take(weight)
    else:
        check_blocked()
    return await asyncio.to_thread(_get, url, timeout_s)


MAX_KLINES_PER_REQUEST = 1500
_INTERVAL_MS = {
    Interval.M1: 60_000,
    Interval.M5: 300_000,
    Interval.M15: 900_000,
    Interval.M30: 1_800_000,
    Interval.H1: 3_600_000,
    Interval.H4: 14_400_000,
}


async def fetch_recent_klines(
    symbol: str,
    *,
    limit: int = 60,
    interval: Interval = Interval.M1,
    base_url: str = DEFAULT_REST_URL,
    timeout_s: float = 5.0,
    budget: WeightBudget | None = None,
) -> list[Kline]:
    rows: list[Any] = []
    end_ms: int | None = None
    while len(rows) < limit:  # 币安一次最多 1500 根；4 小时 ATR 要回填 3840 根 1 分钟线，往前分页拉
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval.value,
            "limit": min(limit - len(rows), MAX_KLINES_PER_REQUEST),
        }
        if end_ms is not None:
            params["endTime"] = end_ms
        q = urllib.parse.urlencode(params)
        url = f"{base_url}/fapi/v1/klines?{q}"
        page = await _page(url, timeout_s, klines_weight(params["limit"]), budget)
        if not page:
            break
        rows = [*page, *rows]
        end_ms = int(page[0][0]) - 1
        if len(page) < params["limit"]:
            break
    return parse_klines(symbol, rows, datetime.now(UTC), interval)


async def fetch_klines_between(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    interval: Interval = Interval.M1,
    base_url: str = DEFAULT_REST_URL,
    timeout_s: float = 5.0,
    budget: WeightBudget | None = None,
) -> list[Kline]:
    """``start``–``end`` 之间已收盘的 K 线（往后分页拉，复盘回填用）。

    limit 按要的根数给（权重按 limit 分档：4 小时 241 根权重 2，固定要 1500 根是 10）。
    """
    rows: list[Any] = []
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    step = _INTERVAL_MS[interval]
    while start_ms <= end_ms:
        want = (end_ms - start_ms) // step + 2
        limit = max(1, min(want, MAX_KLINES_PER_REQUEST))
        params = {
            "symbol": symbol,
            "interval": interval.value,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": limit,
        }
        q = urllib.parse.urlencode(params)
        url = f"{base_url}/fapi/v1/klines?{q}"
        page = await _page(url, timeout_s, klines_weight(limit), budget)
        if not page:
            break
        rows.extend(page)
        start_ms = int(page[-1][0]) + 1
        if len(page) < limit:
            break
    return parse_klines(symbol, rows, datetime.now(UTC), interval)


async def fetch_json(
    path: str,
    params: dict[str, Any],
    *,
    weight: int = 1,
    base_url: str = DEFAULT_REST_URL,
    timeout_s: float = 5.0,
    budget: WeightBudget | None = None,
) -> Any:
    """任意公开 GET 接口（持仓量、多空比……），返回解析后的 JSON。限流规则同上。"""
    url = f"{base_url}{path}?{urllib.parse.urlencode(params)}"
    return await _page(url, timeout_s, weight, budget)
