"""币安合约价格精度（tickSize）：把价格写成交易所认的小数位。"""

from __future__ import annotations


def tick_decimals(tick: str) -> int:
    """``"0.00010000"`` → 4；``"1"`` → 0。"""
    frac = tick.split(".", 1)[1].rstrip("0") if "." in tick else ""
    return len(frac)
