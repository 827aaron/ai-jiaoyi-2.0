"""候选信号：哨兵发现、交给拍板人判断的东西。

候选信号不是开仓指令。它只说“这里可能有机会”，后面还要过拍板人（Jev）和风控员。
同一个 ``dedup_key`` 在冷却期（默认 30 秒）内只发一次。

JSON 示例见 ``contracts/examples/candidate_signal.json``。
"""

from __future__ import annotations

from pydantic import AwareDatetime, Field

from jiaoyi.contracts.common import Contract, PosFloat, Side, SignalKind, Symbol
from jiaoyi.contracts.market import FeatureSnapshot, Forecast


class CandidateSignal(Contract):
    signal_id: str = Field(min_length=1, description="全局唯一，贯穿决策日志和持仓记录。")
    symbol: Symbol
    kind: SignalKind
    side_hint: Side = Field(description="规则给出的方向，拍板人可以否决但不能反向开仓。")
    ts: AwareDatetime
    trigger_price: PosFloat
    trigger_desc: str = Field(
        min_length=1, description="一句话写清触发条件，例如“冲过 68000，1 分钟量 4.2 倍”。"
    )
    dedup_key: str = Field(min_length=1, description="去重键，例如 BTCUSDT:volume_breakout:long")
    card_id: str | None = Field(default=None, description="触发时生效的作战卡；没有卡就不该触发。")
    card_version: int | None = Field(
        default=None,
        ge=1,
        description="触发时作战卡的版本号。同一天的 card_id 不变、每次刷新版本加 1，"
        "风控员比 (card_id, card_version) 判断拍板期间卡有没有换；没带版本的信号风控员一律拒绝。",
    )
    features: FeatureSnapshot
    forecast: Forecast | None = Field(default=None, description="预测员关掉或过期时为空。")
