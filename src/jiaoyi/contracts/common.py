"""所有契约共用的基础类型。

约定：
- 时间一律是带时区的 UTC ``datetime``（``AwareDatetime``），显示时再换曼谷时间。
- 金额单位是 USDT，价格用交易所原始报价，百分比字段名带 ``_pct`` 且 1.0 表示 1%。
- 标的写币安 U 本位永续的统一格式，例如 ``BTCUSDT``。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

Symbol = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Z0-9]{2,20}USDT$"),
    Field(description="币安 U 本位永续合约代码，例如 BTCUSDT、TSLAUSDT"),
]
"""只接受 USDT 结尾的永续代码：币安以外、币本位一律不交易。"""

Probability = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegFloat = Annotated[float, Field(ge=0.0)]
PosFloat = Annotated[float, Field(gt=0.0)]


class Contract(BaseModel):
    """所有共享契约的基类：字段写错直接报错，对象不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class Side(StrEnum):
    """开仓方向。"""

    LONG = "long"
    SHORT = "short"


class Bias(StrEnum):
    """作战卡给某个标的的方向偏好。"""

    LONG = "long"
    SHORT = "short"
    BOTH = "both"
    """两边都可以，由快环按信号决定。"""
    NO_TRADE = "no_trade"
    """只看不做。"""


PERPETUAL_CONTRACT_TYPES = frozenset({"PERPETUAL", "TRADIFI_PERPETUAL"})
"""币安永续的 contractType：加密币是 PERPETUAL，美股 / 金属等传统资产永续是 TRADIFI_PERPETUAL。"""


class Category(StrEnum):
    """相关性分组，用于分组敞口上限（设计文档 §六）。"""

    MAJOR = "major"
    """主流币：BTC、ETH 等。"""
    ALT = "alt"
    """山寨币。"""
    CRYPTO_STOCK = "crypto_stock"
    """加密概念股：COIN、MSTR 等。"""
    US_TECH = "us_tech"
    """美股科技：TSLA、NVDA 等。"""
    PRECIOUS_METAL = "precious_metal"
    """贵金属：黄金、白银。"""
    OTHER = "other"


class SizeTier(StrEnum):
    """仓位档位：拍板人选档，风控员算具体数量。"""

    S = "S"
    M = "M"
    L = "L"


class StopTier(StrEnum):
    """止损宽窄档位：对应作战卡里的 ATR 倍数区间的低、中、高。"""

    NARROW = "narrow"
    MID = "mid"
    WIDE = "wide"


class SignalKind(StrEnum):
    """第一批信号类型（设计文档 §七）。"""

    VOLUME_BREAKOUT = "volume_breakout"
    """放量突破。"""
    VOLUME_SURGE = "volume_surge"
    """量能突增。"""
    LIQUIDATION_REBOUND = "liquidation_rebound"
    """强平瀑布反弹。"""
    CROWDED_REVERSAL = "crowded_reversal"
    """拥挤交易反向。"""
    SESSION_EVENT = "session_event"
    """时段与事件。"""
    OI_FOLLOW = "oi_follow"
    """持仓量变化：持仓量涨 + 价格同向走 = 新钱进场，跟着做（新信号研究 2026-10-24）。"""
    TOP_VS_CROWD = "top_vs_crowd"
    """大户散户分歧：大户加多、散户减多跟大户，反过来做空。"""
    BTC_LEAD = "btc_lead"
    """BTC 先动、小币跟：BTC 5 分钟大动、这个币还没动，按 BTC 方向做它。"""


class Role(StrEnum):
    """团队里的角色。决策日志、模块状态、团队页都用这个枚举。"""

    COMMANDER = "commander"
    """军师团（慢环，TradingAgents）。"""
    SCREENER = "screener"
    """选币：每小时代码筛候选池。"""
    MARKET = "market"
    """总盘会：每轮挑上会的币、要盯的币（只有模块状态，没有决策记录）。"""
    SENTINEL = "sentinel"
    """哨兵。"""
    BOOK_READER = "book_reader"
    """盘口员。"""
    PREDICTOR = "predictor"
    """预测员（Kronos，可关）。"""
    GATE = "gate"
    """拍板人（Jev）。"""
    RISK_GUARD = "risk_guard"
    """风控员。"""
    EXECUTOR = "executor"
    """执行（hummingbot-api 开仓执行器）。"""
    LIFEBOAT = "lifeboat"
    """保命止损（ccxt 挂在币安上）。"""
    KEEPER = "keeper"
    """持仓管家。"""
    PATROL = "patrol"
    """巡逻兵。"""
    RECONCILER = "reconciler"
    """对账员。"""
    ALERTING = "alerting"
    """告警。"""


class RunMode(StrEnum):
    """运行模式。切到 live 需要确认词 + 面板密码（设计文档 §十）。"""

    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"
