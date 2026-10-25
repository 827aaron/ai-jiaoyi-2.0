"""候选池（选币）。

每小时代码筛一次全部在交易的 U 本位永续：先过硬条件，再打分，按类别上限取前 30–50 个。
军师团只能从池里挑。你的固定 / 拉黑 / 手动加入优先级最高，AI 改不了（设计文档 §六）。

JSON 示例见 ``contracts/examples/symbol_pool.json``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from jiaoyi.contracts.common import Category, Contract, NonNegFloat, Symbol


class HardFilters(Contract):
    """硬条件的原始数值和是否通过。手动加入的币也要过。"""

    quote_volume_24h_usdt: NonNegFloat
    spread_bps: NonNegFloat
    depth_usdt: NonNegFloat
    listed_days: int = Field(ge=0)
    passed: bool
    failed_reasons: list[str] = Field(default_factory=list)


class SymbolPoolEntry(Contract):
    symbol: Symbol
    category: Category
    score: float = Field(description="“值不值得盯”的总分，越高越好。")
    score_breakdown: dict[str, float] = Field(
        default_factory=dict,
        description="vol_vs_fees、volume_surge、funding_extreme、oi_change、liquidations 等。",
    )
    reasons: list[str] = Field(default_factory=list, description="入池理由，中文短句。")
    filters: HardFilters
    pinned: bool = False
    blacklisted: bool = False
    manual: bool = False
    in_pool: bool = Field(description="最终是否进池。拉黑的永远为 False。")
    tier: Literal["head", "mid", "tail"] | None = Field(
        default=None,
        description="分档（设计文档 §六）：head 头部档，开放交易；mid 中间档，军师每天挑几个，"
        "仓位档位封顶；tail 尾部档，只录数据看，不做。None = 没开分档或没进池。",
    )
    always_candidate: bool = Field(
        default=False,
        description="设置里「每轮进总盘会」的币：过了硬门槛就进池，每轮都进总盘会候选，"
        "不算头部、不占中间档每天的名额。",
    )
    metrics: dict[str, float] = Field(
        default_factory=dict,
        description="选币时用本地行情算好的数字（涨跌、波动倍数、量能、强平…），给总盘会当表格用。"
        "缺的数不写这个键（表里显示 n/a）。",
    )
    picked_by_card: bool = False
    pick_reason: str = ""

    @model_validator(mode="after")
    def _check(self) -> SymbolPoolEntry:
        if self.blacklisted and (self.in_pool or self.pinned):
            raise ValueError("拉黑的币不能进池，也不能同时固定")
        if self.in_pool and not self.filters.passed:
            raise ValueError("没过硬条件的币不能进池")
        return self


class SymbolPool(Contract):
    """一次选币的结果。"""

    pool_id: str = Field(min_length=1)
    ts: AwareDatetime
    entries: list[SymbolPoolEntry]

    @property
    def in_pool(self) -> list[SymbolPoolEntry]:
        return [e for e in self.entries if e.in_pool]
