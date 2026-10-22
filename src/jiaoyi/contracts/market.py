"""行情事件、特征快照、盘口指标、预测员输出。

行情接入（MarketDataFeed）产出 ``MarketEvent``；特征引擎（FeatureEngine）
把事件变成 ``FeatureSnapshot`` 和 ``BookMetrics``；预测员产出 ``Forecast``。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from jiaoyi.contracts.common import Contract, NonNegFloat, PosFloat, Probability, Side, Symbol


class Trade(Contract):
    """逐笔成交。``taker_side`` 是主动方。"""

    type: Literal["trade"] = "trade"
    symbol: Symbol
    ts: AwareDatetime
    price: PosFloat
    qty: PosFloat
    taker_side: Side


class BookTop(Contract):
    """盘口快照（前 N 档）。``bids`` 从高到低，``asks`` 从低到高，元素是 ``[价格, 数量]``。"""

    type: Literal["book"] = "book"
    symbol: Symbol
    ts: AwareDatetime
    bids: list[tuple[PosFloat, NonNegFloat]] = Field(min_length=1)
    asks: list[tuple[PosFloat, NonNegFloat]] = Field(min_length=1)


class Liquidation(Contract):
    """强平单。``side`` 是被强平仓位的方向。"""

    type: Literal["liquidation"] = "liquidation"
    symbol: Symbol
    ts: AwareDatetime
    side: Side
    price: PosFloat
    qty: PosFloat


class MarkPrice(Contract):
    """标记价格和资金费率。``funding_rate`` 是当期费率，0.0001 表示 0.01%。"""

    type: Literal["mark"] = "mark"
    symbol: Symbol
    ts: AwareDatetime
    mark_price: PosFloat
    index_price: PosFloat | None = None
    funding_rate: float
    next_funding_at: AwareDatetime | None = None


class Interval(StrEnum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"


class Kline(Contract):
    """一根已收盘的 K 线（未收盘的不发）。"""

    type: Literal["kline"] = "kline"
    symbol: Symbol
    interval: Interval
    open_time: AwareDatetime
    close_time: AwareDatetime
    open: PosFloat
    high: PosFloat
    low: PosFloat
    close: PosFloat
    volume: NonNegFloat
    quote_volume: NonNegFloat = 0.0


MarketEvent = Annotated[
    Trade | BookTop | Liquidation | MarkPrice | Kline,
    Field(discriminator="type"),
]
"""行情事件，按 ``type`` 字段区分。"""


class BookMetrics(Contract):
    """盘口员的公式输出（第一版不训练，只作辅助判断）。"""

    mid: PosFloat
    microprice: PosFloat
    spread_bps: NonNegFloat
    depth_usdt: NonNegFloat = Field(description="买卖各前 N 档合计名义价值。")
    imbalance: float = Field(ge=-1.0, le=1.0, description="(买量-卖量)/(买量+卖量)")
    ofi: float = Field(description="订单流失衡（窗口累计），正数偏买。")
    cvd: float = Field(description="累计主动买卖差（窗口内），正数偏买。")
    taker_buy_ratio: Probability


class FeatureSnapshot(Contract):
    """某个标的此刻的特征。``values`` 的键由快环工程师在 docs 里登记，避免各写各的。"""

    symbol: Symbol
    ts: AwareDatetime
    last_price: PosFloat
    atr: NonNegFloat = Field(description="用于止损的 ATR（默认 1 分钟 K 线 14 根）。")
    values: dict[str, float] = Field(
        default_factory=dict,
        description="例如 ret_1m、ret_5m、vol_ratio_1m、realized_vol_5m。",
    )
    book: BookMetrics | None = None


class Forecast(Contract):
    """预测员（Kronos）对未来一段时间的价格范围判断。只作参考，不能单独触发开仓。"""

    symbol: Symbol
    ts: AwareDatetime = Field(description="基于哪根 K 线收盘算的。")
    horizon_min: int = Field(gt=0, le=240)
    price_low: PosFloat
    price_high: PosFloat
    p_up: Probability
    model: str = Field(min_length=1, description="例如 Kronos-small。")
    stale_after: AwareDatetime = Field(description="过期后当作没有这条输入。")
