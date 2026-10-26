"""作战卡：慢环（军师团）写，快环只读。

快环只在卡的范围内开仓：没有有效作战卡（缺失或过期）就不开新仓，已有持仓照常盯。
卡里的范围只能比硬上限更紧，风控员会再核一遍（设计文档 §四）。

JSON 示例见 ``contracts/examples/battle_card.json``。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from jiaoyi.contracts.common import (
    Bias,
    Category,
    Contract,
    PosFloat,
    SignalKind,
    SizeTier,
    Symbol,
)


class MarketView(Contract):
    """独立市场观点；时间和版本与各币研究、作战卡有效期分开。"""

    market_view_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    observed_at: AwareDatetime
    regime: str = Field(min_length=1)
    regime_direction: Literal["看涨", "看跌", "中性"]
    source: str = "market_only"
    refresh_reason: str = "regime_flip"
    pool_id: str = ""
    source_ts: AwareDatetime | None = Field(
        default=None, description="本次使用的池内行情统计时间。"
    )


class LevelKind(StrEnum):
    SUPPORT = "support"
    RESISTANCE = "resistance"
    PIVOT = "pivot"


class KeyLevel(Contract):
    """关键价位。哨兵用它判断“冲过关键位”，风控员把止损放在它外侧。"""

    price: PosFloat
    kind: LevelKind
    note: str = ""


class Range(Contract):
    """闭区间 ``[min, max]``，要求 ``min <= max``。"""

    min: float = Field(ge=0.0)
    max: float = Field(ge=0.0)

    @model_validator(mode="after")
    def _ordered(self) -> Range:
        if self.min > self.max:
            raise ValueError(f"区间下限 {self.min} 大于上限 {self.max}")
        return self

    def contains(self, value: float) -> bool:
        return self.min <= value <= self.max


class CardSymbol(Contract):
    """作战卡里单个标的的作战指令。只能来自候选池（symbol pool）。"""

    symbol: Symbol
    category: Category
    bias: Bias
    key_levels: list[KeyLevel] = Field(default_factory=list)
    allowed_signals: list[SignalKind] = Field(
        default_factory=lambda: list(SignalKind),
        description="允许哪些信号类型触发；空列表等于不交易。",
    )
    size_tier_max: SizeTier = Field(description="今天这个标的最多用到的仓位档。")
    notional_usdt: Range = Field(description="单笔名义价值范围，USDT。")
    leverage: Range = Field(description="杠杆范围，倍。")
    stop_atr: Range = Field(
        description="止损距离范围，ATR(14) 倍数，如 1–2；周期见 risk.toml atr_period_min。"
    )
    take_profit_r: Range = Field(description="止盈距离范围，止损距离的倍数，例如 1.5–3。")
    entry_zone: Range | None = Field(
        default=None,
        description="会议结论里的进场价格区间（反抽做空区 / 回调做多区），币安价格；None = 不限。"
        "快环只在区间内开仓（``fast_loop.entry_zone_only`` 打开时）；旧卡没有这个字段也能读。",
    )
    reason: str = Field(min_length=1, description="军师给的理由，中文。")
    issued_at: AwareDatetime | None = Field(
        default=None,
        description="这条指令是哪一刻开会出的；没有（旧卡 / 手写卡）按卡的 created_at。",
    )
    expires_at: AwareDatetime | None = Field(
        default=None,
        description="这条指令自己的到期时间（该轮开会间隔 + 提前量）；没有按卡的 expires_at。"
        "持仓币的指令不因到期消失，只是不再开新仓。",
    )
    meeting_id: str | None = Field(default=None, description="出这条指令的会议编号。")
    source_market_view_id: str | None = Field(
        default=None, description="研究启动时的市场观点编号。"
    )
    source_regime: str | None = Field(
        default=None, description="研究启动时的市场判断；旧卡回退整卡判断。"
    )

    def research_token(self) -> str:
        """仅按本币研究内容签名，无关币换卡或市场版本刷新不使它失效。"""
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()[:24]


class MarketReviewEvidence(Contract):
    """市场提名引用的同池快照数字；字段名与本次输入一致。"""

    field: str = Field(min_length=1)
    value: float = Field(allow_inf_nan=False)


class MarketReviewCandidate(Contract):
    """市场只提名单币复核，不改已有指令或选择新币。"""

    symbol: Symbol
    research_token: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence: list[MarketReviewEvidence] = Field(min_length=1)


class MarketReviewCard(Contract):
    """提供给独立市场模型的旧研究摘要，含本币内容令牌。"""

    symbol: Symbol
    research_token: str
    bias: Bias
    reason: str
    issued_at: AwareDatetime | None = None
    source_regime: str | None = None
    source_market_view_id: str | None = None
    key_levels: list[KeyLevel] = Field(default_factory=list)


class QueuedMarketReview(Contract):
    """市场提名的持久排队状态；到期从可执行时间起算。"""

    candidate: MarketReviewCandidate
    market_view_id: str
    first_seen: AwareDatetime
    eligible_at: AwareDatetime
    expires_at: AwareDatetime
    reasons: list[str] = Field(default_factory=lambda: ["market_review"])
    reason_queued_at: dict[str, AwareDatetime] = Field(default_factory=dict)


class BattleCard(Contract):
    """一张作战卡。快环随时读最新一张有效的卡。"""

    card_id: str = Field(min_length=1)
    version: int = Field(ge=1, description="同一 card_id 每次刷新加 1。")
    created_at: AwareDatetime
    expires_at: AwareDatetime = Field(description="到期即失效，快环停止新开仓直到新卡到来。")
    source: str = Field(default="tradingagents", description="tradingagents / manual / replay")
    regime: str = Field(min_length=1, description="军师对大盘的一句话判断。")
    regime_direction: Literal["看涨", "看跌", "中性"] | None = Field(
        default=None, description="出卡时同一份市场判断的结构化方向；旧卡没有则不猜。"
    )
    symbols: list[CardSymbol] = Field(min_length=1, max_length=50)
    current_market: MarketView | None = Field(
        default=None, description="只读视图附带的当前市场背景；不会改写各币研究来源。"
    )
    refresh_reason: str = Field(
        default="scheduled",
        description="scheduled / key_level_break / news / new_entrant / manual",
    )
    llm_cost_usd: float = Field(default=0.0, ge=0.0)

    @model_validator(mode="after")
    def _check(self) -> BattleCard:
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at 必须晚于 created_at")
        names = [s.symbol for s in self.symbols]
        if len(names) != len(set(names)):
            raise ValueError("作战卡里同一标的出现了多次")
        return self

    def is_valid(self, now: datetime) -> bool:
        """``now`` 在有效期内才算有效。"""
        return self.created_at <= now < self.expires_at

    def symbol_expires_at(self, symbol: str) -> datetime | None:
        """这个币的指令何时到期：币自己的，没有就按整张卡的。"""
        cs = self.for_symbol(symbol)
        return None if cs is None else (cs.expires_at or self.expires_at)

    def for_symbol(self, symbol: str) -> CardSymbol | None:
        return next((s for s in self.symbols if s.symbol == symbol), None)
