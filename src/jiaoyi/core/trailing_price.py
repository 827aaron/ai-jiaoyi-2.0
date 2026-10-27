"""移动止损的净收益口径和它的反函数，执行层判断与面板画线共用这一份。

净收益率 = (方向 × (观察价 × 数量 − 成交额) − 实收手续费) ÷ 成交额；观察价多仓买一、空仓卖一。
只依赖标准库：面板按结构不能导入执行层。
"""

from __future__ import annotations

import math
from decimal import Decimal


def net_of(long: bool, price: Decimal, quantity: Decimal, quote: Decimal, fee: Decimal) -> Decimal:
    sign = Decimal(1) if long else Decimal(-1)
    return (sign * (price * quantity - quote) - fee) / quote


def net_to_price(
    long: bool, quote: Decimal, quantity: Decimal, fee: Decimal, net: Decimal
) -> Decimal:
    """``net_of`` 的反函数。"""
    sign = Decimal(1) if long else Decimal(-1)
    return (quote + sign * (quote * net + fee)) / quantity


def activation_price(
    long: bool, quote: float, quantity: float, fee: float, activation_pct: float
) -> float | None:
    """观察价越过它（多仓严格高于、空仓严格低于）才激活：执行层判断的是 ``net_of > 启动线``。
    输入无效为 None。"""
    values = (quote, quantity, fee, activation_pct)
    if not all(math.isfinite(v) for v in values) or min(quote, quantity) <= 0:
        return None
    q, n, f, a = (Decimal(str(v)) for v in values)
    price = net_to_price(long, q, n, f, a / 100)
    return float(price) if price.is_finite() and price > 0 else None
