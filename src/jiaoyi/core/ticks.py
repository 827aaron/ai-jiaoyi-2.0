"""币安合约价格精度（tickSize）：把价格写成交易所认的小数位。"""

from __future__ import annotations

from collections.abc import Callable


def tick_decimals(tick: str) -> int:
    """``"0.00010000"`` → 4；``"1"`` → 0。"""
    frac = tick.split(".", 1)[1].rstrip("0") if "." in tick else ""
    return len(frac)


def price_formatter(tick: str | None, *, thousands: bool = False) -> Callable[[float], str]:
    """价格按 tickSize 的小数位写（STRK 0.0490 不能变成 0.05）；不知道精度就 6 位有效数字。"""
    comma = "," if thousands else ""
    if tick is None:
        return lambda x: f"{x:{comma}.6g}"
    dec = tick_decimals(tick)
    return lambda x: f"{x:{comma}.{dec}f}"
