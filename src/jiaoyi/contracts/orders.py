"""保镖的结论和开仓指令。

**AI 永远不直接下单。** 唯一能进执行器的东西是 ``OrderIntent``，而 ``OrderIntent``
只能由保镖（RiskGuard）在全部硬上限检查通过后生成，``approved_by`` 固定为
``risk_guard``。拍板人、军师团、持仓管家、预测员都只输出判断或概率。

JSON 示例见 ``contracts/examples/order_intent.json``。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, NoReturn

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
    open_symbols: list[str] = Field(
        default_factory=list,
        description="已有在管持仓（含开仓中、平仓中）的币。同一个币不许再开第二笔："
        "单向持仓模式下反向单会把原仓对冲掉。",
    )
    open_risk_usdt: NonNegFloat = Field(
        default=0.0,
        description="所有在管持仓按保命止损距离算的风险之和：已成交 数量×入场价×止损%，"
        "未成交按指令名义价值。保镖用它限制总风险。",
    )
    day_realized_pnl_usdt: float = Field(
        default=0.0, description="当日（曼谷 0 点起）已平仓位的净盈亏，亏损为负。"
    )
    paused: bool = Field(default=False, description="面板“暂停开新仓”按钮。")

    @property
    def day_pnl_pct(self) -> float:
        return (self.equity_usdt / self.day_start_equity_usdt - 1.0) * 100.0


class _Sealed(Contract):
    """签发后不能改、也不能跳过校验造一个：``model_copy`` 和 ``model_construct`` 直接报错。

    要换数量或止损，只能让保镖重新签发一张。
    """

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> NoReturn:
        raise TypeError(f"{type(self).__name__} 签发后不能修改，请让保镖重新签发")

    # pydantic 的 mypy 插件会为每个模型生成自己的签名，这里故意整体覆盖
    @classmethod
    def model_construct(  # type: ignore[override]
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> NoReturn:
        raise TypeError(f"{cls.__name__} 不能跳过校验构造")


class OrderIntent(_Sealed):
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
    limit_wait_s: int | None = Field(
        default=None,
        gt=0,
        description="限价进场（挂单等回踩）最多等几秒；到时没成交执行层撤单。只对 limit 有意义。",
    )
    limit_chase: bool = Field(
        default=False,
        description="限价等到时没成交：True = 撤单后市价追进（8h 以上档），False = 撤单不开。",
    )
    quantity: PosFloat = Field(description="已按交易所精度取整的合约数量。")
    notional_usdt: PosFloat
    leverage: int = Field(ge=1, le=125)
    risk_usdt: PosFloat = Field(description="打到止损时预计亏损，USDT。")
    stop_loss_pct: PosFloat
    take_profit_pct: PosFloat
    trailing_stop_pct: PosFloat | None = Field(
        default=None, description="移动止损回撤距离（hummingbot TrailingStop.trailing_delta）。"
    )
    trailing_activation_pct: PosFloat | None = Field(
        default=None,
        description="浮盈达到这个百分比才启动移动止损（hummingbot TrailingStop.activation_price）。"
        "必须和 trailing_stop_pct 一起给，且大于它。",
    )
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
        if self.order_type == "market" and (self.limit_wait_s is not None or self.limit_chase):
            raise ValueError("市价单不能带挂单等待 / 追单设置")
        if (self.trailing_stop_pct is None) != (self.trailing_activation_pct is None):
            raise ValueError("移动止损的回撤距离和激活价必须同时给或同时不给")
        if (
            self.trailing_stop_pct is not None
            and self.trailing_activation_pct is not None
            and self.trailing_activation_pct <= self.trailing_stop_pct
        ):
            raise ValueError("移动止损激活价必须大于回撤距离")
        return self


class RiskVerdict(_Sealed):
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
