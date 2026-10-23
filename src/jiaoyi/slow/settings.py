"""慢环的成本 / 节奏配置（``config/slow.toml``）。

只管开会花多少钱、多频繁：分析师数量、辩论轮数、每个环节用 flash 还是 pro、并发、卡片有效期、
每个币的最小开会间隔。**不放任何风控数值**（硬上限只在 ``risk.toml``）。

和 ``risk.toml`` / ``app.toml`` 不同，这个文件可以不存在：缺文件就用默认值，默认值与不配置时
完全一致（设计优化 (4) 要求“默认不变”）。写了但不合格（拼错字段、超范围）则报错拒绝启动。
示例见 ``config/slow.example.toml``。
"""

from __future__ import annotations

import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jiaoyi.core.config import ConfigError
from jiaoyi.slow.tradingagents_engine import TradingAgentsSettings

DEFAULT_SLOW_PATH = Path("config/slow.toml")
ANALYSTS = ("market", "social", "news", "fundamentals")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MeetingConfig(_Strict):
    analysts_crypto: tuple[str, ...] = Field(
        default=("market", "social", "news"), description="加密币每次会议请哪几位分析师。"
    )
    analysts_stock: tuple[str, ...] = Field(
        default=("market", "social", "news", "fundamentals"), description="美股永续的分析师。"
    )
    debate_rounds: int = Field(default=1, ge=0, le=3, description="多空辩论轮数；越多越贵。")
    risk_rounds: int = Field(default=1, ge=0, le=3, description="风控讨论轮数。")
    quick_model: str = "deepseek-flash"
    deep_model: str = "deepseek-v4-pro"
    pick_tier: Literal["quick", "deep"] = Field(
        default="quick", description="总盘会（挑币）用哪档模型：quick=flash（便宜），deep=pro。"
    )
    convert_tier: Literal["quick", "deep"] = Field(
        default="quick", description="把会议结论整理成作战指令那一步用哪档模型。"
    )
    max_tokens: int = Field(default=8192, ge=1024, le=32768)
    max_retries: int = Field(default=6, ge=0, le=10)

    @field_validator("analysts_crypto", "analysts_stock")
    @classmethod
    def _analysts(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("至少要有一位分析师")
        bad = [a for a in v if a not in ANALYSTS]
        if bad or len(set(v)) != len(v):
            raise ValueError(f"分析师只能从 {ANALYSTS} 里选且不能重复，收到 {list(v)}")
        return v


class ScheduleConfig(_Strict):
    concurrency: int = Field(default=4, ge=1, le=8, description="同时开几个标的的会议。")
    card_ttl_hours: float = Field(default=2.0, ge=1.0, le=4.0, description="作战卡有效期。")
    symbol_min_interval_hours: float = Field(
        default=2.0, ge=0.5, le=12.0, description="同一个币两次会议的最小间隔。"
    )


class PricesConfig(_Strict):
    """每百万 token 的美元价，只用于花费统计。⚠ 占位值，M2 真机验证时按官网核对。"""

    flash_in: float = Field(default=0.15, ge=0)
    flash_out: float = Field(default=0.60, ge=0)
    pro_in: float = Field(default=0.50, ge=0)
    pro_out: float = Field(default=2.00, ge=0)


class SlowConfig(_Strict):
    meeting: MeetingConfig = MeetingConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    prices: PricesConfig = PricesConfig()

    def engine_settings(self) -> TradingAgentsSettings:
        m, p = self.meeting, self.prices
        return TradingAgentsSettings(
            quick_model=m.quick_model,
            deep_model=m.deep_model,
            debate_rounds=m.debate_rounds,
            risk_rounds=m.risk_rounds,
            max_tokens=m.max_tokens,
            max_retries=m.max_retries,
            price_per_mtok={"flash": (p.flash_in, p.flash_out), "pro": (p.pro_in, p.pro_out)},
            analysts_crypto=m.analysts_crypto,
            analysts_stock=m.analysts_stock,
            pick_tier=m.pick_tier,
            convert_tier=m.convert_tier,
        )

    def commander_kwargs(self) -> dict[str, Any]:
        """传给 ``BattleCardCommander(...)`` 的节奏参数。"""
        s = self.schedule
        return {
            "concurrency": s.concurrency,
            "ttl": timedelta(hours=s.card_ttl_hours),
            "symbol_min_interval": timedelta(hours=s.symbol_min_interval_hours),
        }


def load_slow_config(path: Path = DEFAULT_SLOW_PATH) -> SlowConfig:
    """缺文件 → 默认值；有文件但不合格 → ``ConfigError``。"""
    if not path.is_file():
        return SlowConfig()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} 不是合法的 TOML：{e}") from e
    try:
        return SlowConfig.model_validate(data)
    except ValidationError as e:
        lines = [f"{path} 不合格，拒绝启动："]
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(整体)"
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from e
