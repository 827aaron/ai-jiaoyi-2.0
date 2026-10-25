"""总盘会的候选表：每个候选一行，全是选币时用本地行情算好的数字（见 ``binance_stats``）。

总盘会的模型只能引用表、池子概况、大盘简报和新闻里有的东西。缺的数写 ``n/a``，不编。
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable, Sequence

from jiaoyi.contracts import SymbolPoolEntry

NA = "n/a"
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")


def count_numbers(text: str) -> int:
    """一句话里有几个数字（「4h」「1.2%」「37 倍」都各算一个）。"""
    return len(_NUMBER.findall(text or ""))


def _pct(m: dict[str, float], key: str, signed: bool = True, digits: int = 1) -> str:
    v = m.get(key)
    if v is None:
        return NA
    return f"{v:+.{digits}f}%" if signed else f"{v:.{digits}f}%"


def _num(m: dict[str, float], key: str, fmt: str, unit: str = "") -> str:
    v = m.get(key)
    return NA if v is None else f"{format(v, fmt)}{unit}"


def format_row(e: SymbolPoolEntry, hold: str, headlines: str = "") -> str:
    """一个候选一行：英文字段名、数字带单位、缺的写 n/a。``headlines`` 是已经格式化好的新闻标题。"""
    m = e.metrics
    move = m.get("move_hold")
    move_txt = (
        f"{move:.1f}% ({m.get('fee_x', 0):.0f}x fee, rank {m['rank']:.0f}/{m['pool_n']:.0f})"
        if move is not None and "rank" in m and "pool_n" in m
        else (f"{move:.1f}% ({m.get('fee_x', 0):.0f}x fee)" if move is not None else NA)
    )
    if "liq_1h" in m:
        share = f" ({m['liq_share']:.1f}% of 1h vol)" if "liq_share" in m else ""
        liq = f"{m['liq_1h'] / 1000:,.0f}k{share}"
    elif "liq_feed_min" in m:
        liq = f"{NA} (feed {m['liq_feed_min']:.0f} min)"
    else:
        liq = NA
    row = (
        f"{e.symbol} tier={e.tier or '-'} cat={e.category.value} score={e.score:.2f}"
        f" | chg thisHour {_pct(m, 'chg_this_hour')} 1h {_pct(m, 'chg_1h')}"
        f" 4h {_pct(m, 'chg_4h')} 24h {_pct(m, 'chg_24h')}"
        f" | pos24h {_num(m, 'pos_24h', '.0f', '%')}"
        f" | move{hold} {move_txt}"
        f" | vol1h {_num(m, 'vol_ratio_1h', '.1f', 'x')}"
        f" | takerBuy1h {_num(m, 'taker_buy_1h', '.0f', '%')}"
        f" | funding {_pct(m, 'funding', digits=3)}"
        f" | OI1h {_pct(m, 'oi_chg_1h')}"
        f" | liq1h {liq}"
        f" | spread {_num(m, 'spread_bps', '.1f', 'bp')} depth {_num(m, 'depth_k', ',.0f', 'k')}"
        f" | corrBTC {_num(m, 'corr_btc_24h', '.2f')}"
        f" | vol24h {_num(m, 'qv_24h_m', ',.0f', 'M')}"
    )
    return row + headlines


def _breadth(values: Iterable[float | None]) -> str:
    vs = [v for v in values if v is not None]
    if not vs:
        return f"{NA}"
    up = sum(1 for v in vs if v > 0)
    return f"up {up} / down {len(vs) - up}, median {statistics.median(vs):+.1f}%"


def breadth_line(pool: Sequence[SymbolPoolEntry]) -> str:
    """池子概况：覆盖池里全部币（含尾部），给大盘判断用，全由代码算。"""
    n = len(pool)
    return (
        f"Pool breadth ({n}): last hour {_breadth(e.metrics.get('chg_1h') for e in pool)}; "
        f"last 4h {_breadth(e.metrics.get('chg_4h') for e in pool)}; "
        f"24h {_breadth(e.metrics.get('chg_24h') for e in pool)}"
    )
