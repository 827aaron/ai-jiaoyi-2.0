"""总盘会的候选表：每个候选一行，全是选币时用本地行情算好的数字（见 ``binance_stats``）。

总盘会的模型只能引用表、池子概况、大盘简报和新闻里有的东西。缺的数写 ``n/a``，不编。
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from jiaoyi.contracts import SymbolPoolEntry

NA = "n/a"
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def count_numbers(text: str) -> int:
    """一句话里有几个数字（「4h」「1.2%」「37 倍」都各算一个）。"""
    return len(_NUMBER.findall(text or ""))


def _pct(m: dict[str, float], key: str, signed: bool = True, digits: int = 1) -> str:
    v = m.get(key)
    if v is None or not math.isfinite(v):
        return NA
    return f"{v:+.{digits}f}%" if signed else f"{v:.{digits}f}%"


def _num(m: dict[str, float], key: str, fmt: str, unit: str = "") -> str:
    v = m.get(key)
    return NA if v is None or not math.isfinite(v) else f"{format(v, fmt)}{unit}"


def _hhmm(iso: Any, end: bool = False) -> str | None:
    """ISO 时间 → ``HH:MM``；``end``：K 线收盘时刻是 xx:59:59.999，补 1 毫秒写成整点。"""
    try:
        t = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    return f"{t + timedelta(milliseconds=1) if end else t:%H:%M}"


def _window(meta: Mapping[str, Any] | None, start: str, end: str) -> str:
    """来源元数据里的窗口 → ``[09:00-10:00Z]``；不知道就 ``[time unknown]``，不编。"""
    a = _hhmm((meta or {}).get(start))
    b = _hhmm((meta or {}).get(end), end=True)
    return f"[{a}-{b}Z]" if a and b else "[time unknown]"


def format_row(
    e: SymbolPoolEntry,
    hold: str,
    headlines: str = "",
    labels: str = "legacy",
    meta: Mapping[str, Any] | None = None,
) -> str:
    """一个候选一行：英文字段名、数字带单位、缺的写 n/a。``headlines`` 是已经格式化好的新闻标题。
    ``labels`` = explicit：主动买占比和持仓量变化写明是哪个已收盘小时（``meta`` 是这个币的
    选币来源元数据，见 ``binance_stats._source_metadata_impl``）。"""
    m = {key: value for key, value in e.metrics.items() if math.isfinite(value)}
    move = m.get("move_hold")
    move_txt = (
        f"{move:.1f}% ({_num(m, 'fee_x', '.0f')}x fee, rank {m['rank']:.0f}/{m['pool_n']:.0f})"
        if move is not None and "rank" in m and "pool_n" in m
        else (f"{move:.1f}% ({_num(m, 'fee_x', '.0f')}x fee)" if move is not None else NA)
    )
    if "liq_1h" in m:
        share = f" ({m['liq_share']:.1f}% of 1h vol)" if "liq_share" in m else ""
        liq = f"{m['liq_1h'] / 1000:,.0f}k{share}"
    elif "liq_feed_min" in m:
        liq = f"{NA} (feed {m['liq_feed_min']:.0f} min)"
    else:
        liq = NA
    if labels == "explicit":
        taker = "takerBuy_closedHour" + _window(meta, "completed_hour_start", "completed_hour_end")
        oi = "OI_closedHour" + _window(meta, "oi_window_start", "oi_window_end")
    else:
        taker, oi = "takerBuy1h", "OI1h"
    row = (
        f"{e.symbol} tier={e.tier or '-'} cat={e.category.value} score={e.score:.2f}"
        f" | chg current_hour_open_to_ticker {_pct(m, 'chg_this_hour')}"
        f" completed_hour {_pct(m, 'chg_1h')}"
        f" completed_4h_close_span {_pct(m, 'chg_4h')} ticker_24h {_pct(m, 'chg_24h')}"
        f" | pos24h {_num(m, 'pos_24h', '.0f', '%')}"
        f" | move{hold} {move_txt}"
        f" | vol1h {_num(m, 'vol_ratio_1h', '.1f', 'x')}"
        f" | {taker} {_num(m, 'taker_buy_1h', '.0f', '%')}"
        f" | funding {_pct(m, 'funding', digits=3)}"
        f" | {oi} {_pct(m, 'oi_chg_1h')}"
        f" | liq1h {liq}"
        f" | spread {_num(m, 'spread_bps', '.1f', 'bp')} depth {_num(m, 'depth_k', ',.0f', 'k')}"
        f" | corrBTC {_num(m, 'corr_btc_24h', '.2f')}"
        f" | vol24h {_num(m, 'qv_24h_m', ',.0f', 'M')}"
    )
    return row + headlines


def _breadth(values: Iterable[float | None]) -> str:
    raw = list(values)
    vs = [v for v in raw if v is not None and math.isfinite(v)]
    up = sum(1 for v in vs if v > 0)
    down = sum(1 for v in vs if v < 0)
    flat = sum(1 for v in vs if v == 0)
    median = f"{statistics.median(vs):+.1f}%" if vs else NA
    return f"up {up} / down {down} / flat {flat} / missing {len(raw) - len(vs)}, median {median}"


def breadth_line(pool: Sequence[SymbolPoolEntry]) -> str:
    """池子概况：覆盖池里全部币（含尾部），给大盘判断用，全由代码算。"""
    n = len(pool)
    return (
        f"Pool breadth ({n}): completed_hour {_breadth(e.metrics.get('chg_1h') for e in pool)}; "
        f"completed_4h_close_span {_breadth(e.metrics.get('chg_4h') for e in pool)}; "
        f"ticker_24h {_breadth(e.metrics.get('chg_24h') for e in pool)}"
    )


def rolling_breadth_line(
    changes: Mapping[str, float | None], start: datetime, end: datetime
) -> str:
    """滚动 60 分钟涨跌家数：每个币「开会这一刻的最新价 ÷ 60 分钟前那一分钟的开盘价」。
    和 completed_hour（上一个已收盘整点小时）不是一回事，两行都给、各自写明窗口。"""
    return (
        f"Pool breadth rolling_60m ({start:%H:%M}-{end:%H:%M} UTC, 1m bars ending at the meeting "
        f"time, last minute still open) ({len(changes)}): {_breadth(changes.values())}"
    )
