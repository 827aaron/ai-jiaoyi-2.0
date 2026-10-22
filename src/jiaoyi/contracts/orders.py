"""保镖的结论和开仓指令。

**AI 永远不直接下单。** 唯一能进执行器的东西是 ``OrderIntent``，而 ``OrderIntent``
只能由保镖（RiskGuard）在全部硬上限检查通过后生成，``approved_by`` 固定为
``risk_guard``。拍板人、军师团、持仓管家、预测员都只输出判断或概率。

JSON 示例见 ``contracts/examples/order_intent.json``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from jiaoyi.contracts.common import Category, Contract, NonNegFloat, PosFloat, Side, Symbol


class AccountState(Contract):
    """保镖做判断时看到的账户状态（来自币安账户推送或 hummingbot-api 模拟账户）。"""

    ts: AwareDatetime
    equity_usdt: NonNegFloat = Field(description="总资产（含浮盈浮亏），即币安的权益。")
    available_usdt: NonNegFloat
    day_start_equity_usdt: PosFloat = Field(description="当天清零时刻的权益，用于日亏熔断。")
    open_notional_usdt: NonNegFloat = Field(description="所有持仓名义价值合计。")
    open_notional_by_category: dict[Category, NonNegFloat] = Field(default_factory=dict)
    open_positions: int = Field(ge=0)
    paused: bool = Field(default=False, description="面板“暂停开新仓”按钮。")

    @property
    def day_pnl_pct(self) -> float:
        return (self.equity_usdt / self.day_start_equity_usdt - 1.0) * 100.0


class OrderIntent(Contract):
    """经保镖批准的开仓指令，交给执行器（hummingbot-api 开仓执行器）。

    百分比相对开仓价。止损止盈由执行器每秒检查；保命止损另由 lifeboat 挂到币安。
    """

    intent_id: str = Field(min_length=1)
    signal_id: str = Field(min_length=1)
    card_id: str = Field(min_length=1)
    symbol: Symbol
    category: Category
    side: Side
    order_type: Literal["market", "limit"] = "market"
    limit_price: PosFloat | None = None
    quantity: PosFloat = Field(description="已按交易所精度取整的合约数量。")
    notional_usdt: PosFloat
    leverage: int = Field(ge=1, le=125)
    risk_usdt: PosFloat = Field(description="打到止损时预计亏损，USDT。")
    stop_loss_pct: PosFloat
    take_profit_pct: PosFloat
    trailing_stop_pct: PosFloat | None = None
    time_limit_s: int = Field(gt=0)
    lifeboat_stop_pct: PosFloat = Field(description="保命止损距离，必须比正常止损更宽。")
    approved_by: Literal["risk_guard"] = "risk_guard"
    approved_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> OrderIntent:
        if self.lifeboat_stop_pct <= self.stop_loss_pct:
            raise ValueError("保命止损必须比正常止损更宽")
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("限价单必须给 limit_price")
        return self


class RiskVerdict(Contract):
    """保镖的结论：放行（带 ``intent``）或拒绝（带原因）。"""

    signal_id: str = Field(min_length=1)
    approved: bool
    reasons: list[str] = Field(default_factory=list, description="拒绝原因，中文短句。")
    intent: OrderIntent | None = None

    @model_validator(mode="after")
    def _check(self) -> RiskVerdict:
        if self.approved and self.intent is None:
            raise ValueError("放行必须带 intent")
        if not self.approved and (self.intent is not None or not self.reasons):
            raise ValueError("拒绝时不能带 intent，且必须写原因")
        return self
