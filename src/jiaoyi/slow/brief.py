"""共享市场简报：每轮开会前算一次，所有标的的会议复用（设计优化 (3)）。

内容：BTC / ETH 各一行（字段按最长持仓档的写法取，见 ta_prompts.WORDS）、
恐慌贪婪指数（alternative.me，免费无 key）。
美元指数（DXY）没有稳定的免费无 key 来源（试过的站点会拦截云端 / 脚本请求），先不接，
需要时加一个 ``BriefPart`` 即可，不用改别处。

每一部分独立失败：拿不到只在简报里写“暂不可用”，其余照常；整份简报也有缓存，
一轮里多次调用不会重复请求。
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import httpx

from jiaoyi.core.ticks import price_formatter
from jiaoyi.slow.ta_prompts import BRIEF_FIELDS, WORDS
from jiaoyi.slow.ticks import TICKS

_FAPI = "https://fapi.binance.com"
_FNG = "https://api.alternative.me/fng/"

BriefPart = Callable[[httpx.AsyncClient], Awaitable[str]]
_PART_TRACE: ContextVar[dict[str, Any] | None] = ContextVar("brief_part_trace", default=None)


def _trace_time() -> str | None:
    trace = _PART_TRACE.get()
    if trace is None:
        return None
    try:
        value = trace["clock"]()
        if (
            isinstance(value, datetime)
            and value.tzinfo is not None
            and value.utcoffset() is not None
        ):
            return value.astimezone(UTC).isoformat()
    except Exception:
        pass
    return None


def _millis_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, UTC).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _bar_window(rows: Sequence[Sequence[Any]], observed: str | None) -> dict[str, Any]:
    if not rows:
        return {"available_bars": 0, "coverage": "unavailable"}
    first, last = rows[0], rows[-1]
    close = _millis_iso(last[6]) if len(last) > 6 else None
    complete = (
        datetime.fromisoformat(close) <= datetime.fromisoformat(observed)
        if close is not None and observed is not None
        else None
    )
    return {
        "available_bars": len(rows),
        "first_bar_open_at": _millis_iso(first[0]),
        "last_bar_open_at": _millis_iso(last[0]),
        "last_bar_scheduled_close_at": close,
        "last_bar_closed_at_observation": complete,
        "source_price_event_at": None,
        "observed_at": observed,
        "coverage": "bar_boundaries_only_not_price_event_time",
    }


def _trace_fields(
    symbol: str,
    fields: Sequence[str],
    series: dict[str, Sequence[Sequence[Any]]],
) -> None:
    trace = _PART_TRACE.get()
    if trace is None:
        return
    with contextlib.suppress(Exception):
        sources = {row["interval"]: row for row in trace["sources"] if "interval" in row}
        specs = {
            "chg_1h": ("5m", 13, "close_endpoints"),
            "chg_4h": ("5m", 49, "close_endpoints"),
            "chg_24h": ("5m", 289, "close_endpoints"),
            "chg_3d": ("1h", 73, "close_endpoints"),
            "chg_7d": ("1d", 8, "close_endpoints"),
            "range_24h": ("5m", 288, "high_low_range"),
            "range_3d": ("1h", 72, "high_low_range"),
            "vol_1h": ("5m", 288, "last_12_bar_volume_over_288_bar_volume_divided_by_24"),
            "sma20": ("1d", 20, "mean_close"),
            "rsi_1h": ("1h", 15, "last_14_close_changes"),
            "rsi_4h": ("4h", 15, "last_14_close_changes"),
            "rsi_1d": ("1d", 15, "last_14_close_changes"),
        }
        details = {}
        for name in fields:
            interval, count, kind = specs[name]
            rows = series[interval][-count:]
            observed = sources.get(interval, {}).get("observed_at")
            detail = {
                "interval": interval,
                "calculation": kind,
                "required_bars": count,
                "required_bar_count_present": len(rows) == count,
                **_bar_window(rows, observed),
            }
            if kind == "close_endpoints" and rows:
                detail["anchor_bar_scheduled_close_at"] = (
                    _millis_iso(rows[0][6]) if len(rows[0]) > 6 else None
                )
            if name == "vol_1h":
                detail["numerator_window"] = _bar_window(rows[-12:], observed)
            details[name] = detail
        trace["symbol"], trace["fields"] = symbol, details


def _sma(xs: Sequence[float], n: int) -> float:
    tail = xs[-n:]
    return sum(tail) / len(tail)


def _rsi(closes: Sequence[float], n: int = 14) -> float:
    gains = [max(b - a, 0.0) for a, b in pairwise(closes)]
    losses = [max(a - b, 0.0) for a, b in pairwise(closes)]
    ag, al = sum(gains[-n:]) / n, sum(losses[-n:]) / n
    return 100.0 if al == 0 else 100 - 100 / (1 + ag / al)


SHORT_FIELDS = {"chg_1h", "chg_4h", "chg_24h", "range_24h", "vol_1h"}
HOURLY_FIELDS = {"chg_3d", "range_3d", "rsi_1h"}
DAILY_FIELDS = {"chg_7d", "sma20", "rsi_1d"}
H4_FIELDS = {"rsi_4h"}
_BARS_5M = 289  # 24 小时 + 1：1 小时 / 4 小时 / 24 小时涨跌、24 小时区间、成交倍数都用 5 分钟线算
_BARS_1H = 73  # 3 天 + 1
_BARS_1D = 21
_BARS_4H = 30


def _pct(last: float, then: float) -> float:
    return (last / then - 1) * 100


def _range_pos(
    price: float, highs: Sequence[float], lows: Sequence[float]
) -> tuple[float, float, float]:
    hi, lo = max(highs), min(lows)
    return ((price - lo) / (hi - lo) * 100 if hi > lo else 50.0), lo, hi


def describe_trend(
    name: str,
    fields: Sequence[str],
    background: Sequence[str],
    m5: Sequence[Sequence[Any]],
    h1: Sequence[Sequence[Any]],
    h4: Sequence[Sequence[Any]],
    d1: Sequence[Sequence[Any]],
    fmt: Callable[[float], str] = lambda x: f"{x:,.1f}",
) -> str:
    """一个币一行：现价 + 这一档的主字段 + 背景字段。每个字段只用自己要的数据。纯函数，便于测试。

    K 线行是币安原样（开、高、低、收、量在第 1–5 列）；最后一根可能还没收盘，当作现价。
    """

    def cl(rows: Sequence[Sequence[Any]]) -> list[float]:
        return [float(k[4]) for k in rows]

    def text(field: str) -> str:
        zh = BRIEF_FIELDS[field]
        if field in ("chg_1h", "chg_4h", "chg_24h"):
            n = {"chg_1h": 12, "chg_4h": 48, "chg_24h": 288}[field]
            c = cl(m5)
            return f"{zh} {_pct(c[-1], c[-1 - n]):+.1f}%"
        if field == "chg_3d":
            c = cl(h1)
            return f"{zh} {_pct(c[-1], c[-73]):+.1f}%"
        if field == "chg_7d":
            c = cl(d1)
            return f"{zh} {_pct(c[-1], c[-8]):+.1f}%"
        if field in ("range_24h", "range_3d"):
            rows, n = (m5, 288) if field == "range_24h" else (h1, 72)
            pos, lo, hi = _range_pos(
                float(rows[-1][4]),
                [float(k[2]) for k in rows[-n:]],
                [float(k[3]) for k in rows[-n:]],
            )
            return f"{zh} {pos:.0f}% (low {fmt(lo)} – high {fmt(hi)})"
        if field == "vol_1h":
            v = [float(k[5]) for k in m5[-288:]]
            avg = sum(v) / 24
            return f"{zh} {sum(v[-12:]) / avg:.1f}x" if avg > 0 else f"{zh} unavailable"
        if field == "sma20":
            c = cl(d1)
            sma = _sma(c, 20)
            return f"{zh} {'above' if c[-1] >= sma else 'below'} ({fmt(sma)})"
        rows = {"rsi_1h": h1, "rsi_4h": h4, "rsi_1d": d1}[field]
        c = cl(rows)
        return f"{zh} {_rsi(c):.0f}" if len(c) > 14 else f"{zh} unavailable"

    price = float((m5 or h1 or h4 or d1)[-1][4]) if (m5 or h1 or h4 or d1) else float("nan")
    main = ", ".join(text(f) for f in fields)
    bg = f"; background: {', '.join(text(f) for f in background)}" if background else ""
    return f"- {name} price {fmt(price)}: {main}{bg}"


async def _rows(c: httpx.AsyncClient, symbol: str, interval: str, limit: int) -> list[list[Any]]:
    started = _trace_time()
    r = await c.get(
        f"{_FAPI}/fapi/v1/klines", params={"symbol": symbol, "interval": interval, "limit": limit}
    )
    r.raise_for_status()
    rows: list[list[Any]] = r.json()
    trace = _PART_TRACE.get()
    if trace is not None:
        with contextlib.suppress(Exception):
            observed = _trace_time()
            trace["sources"].append(
                {
                    "source": "binance_futures_klines",
                    "symbol": symbol,
                    "interval": interval,
                    "requested_bars": limit,
                    "request_started_at": started,
                    **_bar_window(rows, observed),
                }
            )
    return rows


def trend_part(symbol: str, name: str, style: str = "short") -> BriefPart:
    """BTC / ETH 一行：这一档（short / mid / long）的字段（``ta_prompts.WORDS`` 里的简报字段）。
    按字段取数：5m x 289 / 1h x 73 / 4h x 30 / 1d x 21，只取用得到的。"""
    w = WORDS.get(style, WORDS["short"])
    fields, background = w.brief_main, w.brief_bg
    used = set(fields) | set(background)

    async def part(c: httpx.AsyncClient) -> str:
        trace = _PART_TRACE.get()
        if trace is not None:
            trace.update(symbol=symbol, requested_fields=[*fields, *background])

        async def get(want: set[str], interval: str, n: int) -> list[list[Any]]:
            return await _rows(c, symbol, interval, n) if used & want else []

        m5, h1, h4, d1 = await asyncio.gather(
            get(SHORT_FIELDS, "5m", _BARS_5M),
            get(HOURLY_FIELDS, "1h", _BARS_1H),
            get(H4_FIELDS, "4h", _BARS_4H),
            get(DAILY_FIELDS, "1d", _BARS_1D),
        )
        _trace_fields(symbol, (*fields, *background), {"5m": m5, "1h": h1, "4h": h4, "1d": d1})
        tick = await asyncio.to_thread(TICKS.get, symbol)
        return describe_trend(
            name, fields, background, m5, h1, h4, d1, price_formatter(tick, thousands=True)
        )

    return part


async def fear_greed_part(c: httpx.AsyncClient) -> str:
    started = _trace_time()
    r = await c.get(_FNG, params={"limit": 2})
    r.raise_for_status()
    rows: list[dict[str, Any]] = r.json()["data"]
    trace = _PART_TRACE.get()
    if trace is not None:
        with contextlib.suppress(Exception):
            trace["sources"].append(
                {
                    "source": "alternative_me_fear_greed",
                    "request_started_at": started,
                    "observed_at": _trace_time(),
                    "source_published_at": [
                        _millis_iso(float(row["timestamp"]) * 1000)
                        if row.get("timestamp") is not None
                        else None
                        for row in rows[:2]
                    ],
                }
            )
    now, prev = rows[0], rows[1] if len(rows) > 1 else None
    tail = f" (previous day {prev['value']})" if prev else ""
    return f"- Fear & Greed index: {now['value']} ({now['value_classification']}){tail}"


def default_parts(style: str = "short") -> list[BriefPart]:
    return [
        trend_part("BTCUSDT", "BTC", style),
        trend_part("ETHUSDT", "ETH", style),
        fear_greed_part,
    ]


class MarketBriefService:
    """``refresh`` 每轮开会前调一次（太近则用缓存）；``text`` 同步读，不可变字符串所以线程安全。"""

    def __init__(
        self,
        parts: Sequence[BriefPart] | None = None,
        client: httpx.AsyncClient | None = None,
        min_age: timedelta = timedelta(minutes=10),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        style: str = "short",
        observation_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._parts = list(parts) if parts is not None else default_parts(style)
        self._c = client or httpx.AsyncClient(timeout=10.0)
        self._min_age = min_age
        self._clock = clock
        self._text = ""
        self._at: datetime | None = None
        self.failed_parts = 0
        self._refresh_lock = asyncio.Lock()
        self._observation_clock = observation_clock
        self._content_provenance: dict[str, Any] | None = None
        self._returned_provenance: dict[str, Any] = {}

    @property
    def text(self) -> str:
        """当前简报；还没生成过或全部失败时为空串。"""
        return self._text

    async def refresh(self) -> str:
        async with self._refresh_lock:
            return await self._refresh_once()

    async def refresh_with_provenance(self) -> tuple[str, dict[str, Any]]:
        """元数据和本次返回文本在同一锁内配对，缓存内容不借用新的行情时间。"""
        async with self._refresh_lock:
            text = await self._refresh_once()
            return text, copy.deepcopy(self._returned_provenance)

    def _observed_time(self) -> str | None:
        try:
            value = self._observation_clock()
            if value.tzinfo is not None and value.utcoffset() is not None:
                return value.astimezone(UTC).isoformat()
        except Exception:
            pass
        return None

    def _note_return(self, status: str, attempt: dict[str, Any] | None = None) -> None:
        self._returned_provenance = {
            "schema_version": 1,
            "status": status,
            "cache_hit": status == "cache_hit",
            "returned_at": self._observed_time(),
            "cache_clock_at": self._at.isoformat() if self._at is not None else None,
            "cache_ttl_s": self._min_age.total_seconds(),
            "content": self._content_provenance,
            "attempt": attempt,
        }

    async def _part_observed(self, part: BriefPart, trace: dict[str, Any]) -> str:
        token = _PART_TRACE.set({**trace, "clock": self._observation_clock})
        captured = _PART_TRACE.get()
        try:
            value = await part(self._c)
            trace["status"] = "ok" if isinstance(value, str) else "unavailable"
            return value
        except Exception as error:
            trace.update(status="unavailable", error_kind=type(error).__name__)
            raise
        finally:
            if captured is not None:
                trace.update({k: v for k, v in captured.items() if k != "clock"})
            trace["completed_at"] = self._observed_time()
            _PART_TRACE.reset(token)

    async def _refresh_once(self) -> str:
        now = self._clock()
        if self._at is not None and now - self._at < self._min_age:
            self._note_return("cache_hit")
            return self._text
        attempt: dict[str, Any] = {"started_at": self._observed_time()}
        parts: list[dict[str, Any]] = [{"index": i, "sources": []} for i in range(len(self._parts))]
        results = await asyncio.gather(
            *(self._part_observed(p, part) for p, part in zip(self._parts, parts, strict=True)),
            return_exceptions=True,
        )
        parts = copy.deepcopy(parts)
        attempt.update(completed_at=self._observed_time(), parts=parts)
        lines = [r if isinstance(r, str) else "- (one data item is unavailable)" for r in results]
        self.failed_parts = sum(1 for r in results if not isinstance(r, str))
        if self.failed_parts == len(self._parts):
            # 全部失败：保留上一份（可能为空），不更新时间，下一轮再试
            self._note_return("refresh_failed_retained_cache", attempt)
            return self._text
        self._text = (
            f"## Market brief ({now:%Y-%m-%d %H:%M} UTC, shared by every meeting this round)\n"
            + "\n".join(lines)
        )
        self._at = now
        self._content_provenance = {
            "generated_at": self._observed_time(),
            "header_clock_at": now.isoformat(),
            "status": "partial" if self.failed_parts else "complete",
            "parts": parts,
        }
        self._note_return("refreshed", attempt)
        return self._text

    async def aclose(self) -> None:
        await self._c.aclose()
