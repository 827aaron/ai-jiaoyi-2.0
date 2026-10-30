"""反向作战卡没经价格确认时的收紧止损价（#314）。

目标 = 扣掉实际开仓手续费、按吃单费率估的平仓手续费之后，至少还赚 ``buffer_bps``（相对开仓名义）
的平仓价，即保本价再往有利方向挪一点。只算数，不下单；挂单走托管保护状态机。
"""

from __future__ import annotations

from decimal import Decimal

from jiaoyi.contracts import Side


def net_profit_pct(
    side: Side, quantity: float, entry_quote: float, entry_fee: float, rate: float, price: float
) -> float:
    """按 ``price`` 平仓扣掉实际开仓费和吃单平仓费后赚多少（开仓名义的百分比）。"""
    value = price * quantity
    gross = value - entry_quote if side is Side.LONG else entry_quote - value
    return (gross - entry_fee - value * rate) / entry_quote * 100


def breakeven_stop_price(
    side: Side,
    quantity: float,
    entry_quote: float,
    entry_fee: float,
    exit_fee_rate: float,
    buffer_bps: float,
) -> float:
    """多：(P·q − 名义) − P·q·费率 = 开仓费 + 缓冲 → P = (名义·(1+b) + 开仓费) / (q·(1−费率))；
    空：(名义 − P·q) − P·q·费率 = 开仓费 + 缓冲 → P = (名义·(1−b) − 开仓费) / (q·(1+费率))。"""
    q, quote, fee, rate, b = (
        Decimal(str(v)) for v in (quantity, entry_quote, entry_fee, exit_fee_rate, buffer_bps)
    )
    if not all(v.is_finite() for v in (q, quote, fee, rate, b)):
        raise ValueError("收紧止损的入场或费用数字无效")
    if q <= 0 or quote <= 0 or fee < 0 or not 0 <= rate < 1 or b < 0:
        raise ValueError("收紧止损的入场或费用数字超出范围")
    b = b / Decimal(10_000)
    if side is Side.LONG:
        price = (quote * (1 + b) + fee) / (q * (1 - rate))
    else:
        price = (quote * (1 - b) - fee) / (q * (1 + rate))
    if price <= 0:
        raise ValueError("收紧止损价不为正")
    return float(price)
