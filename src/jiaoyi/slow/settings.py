"""慢环的成本 / 节奏配置（``config/slow.toml``）。

只管开会花多少钱、多频繁：分析师数量、辩论轮数、每个环节用 flash 还是 pro、并发、卡片有效期、
每个币的最小开会间隔。**不放任何风控数值**（硬上限只在 ``risk.toml``）。

和 ``risk.toml`` / ``app.toml`` 不同，这个文件可以不存在：缺文件就用默认值。``[schedule]`` /
``[budget]`` 里没写的键取当前持仓档位的默认（app.toml 的 ``profile``，见 ``core/profiles.py``）。
写了但不合格（拼错字段、超范围）则报错拒绝启动。
示例见 ``config/slow.example.toml``。
"""

from __future__ import annotations

import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jiaoyi.core import profiles
from jiaoyi.core.config import ConfigError
from jiaoyi.slow.tradingagents_engine import PROVIDER_MODELS, TradingAgentsSettings

DEFAULT_SLOW_PATH = Path("config/slow.toml")
ANALYSTS = ("market", "social", "news", "fundamentals")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LlmConfig(_Strict):
    provider: Literal["deepseek", "openrouter"] = Field(
        default="deepseek",
        description="deepseek=直连；openrouter=经 OpenRouter（各自的 API key）",
    )


class MeetingConfig(_Strict):
    analysts_crypto: tuple[str, ...] = Field(
        default=("market", "social", "news"), description="加密币每次会议请哪几位分析师。"
    )
    analysts_stock: tuple[str, ...] = Field(
        default=("market", "social", "news", "fundamentals"), description="美股永续的分析师。"
    )
    debate_rounds: int = Field(default=1, ge=0, le=3, description="多空辩论轮数；越多越贵。")
    risk_rounds: int = Field(default=1, ge=0, le=3, description="风控讨论轮数。")
    quick_model: str | None = Field(default=None, description="不写 = 用所选 provider 的默认模型。")
    deep_model: str | None = Field(default=None, description="不写 = 用所选 provider 的默认模型。")
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
    max_cards_per_round: int | None = Field(
        default=None,
        ge=1,
        le=50,
        description="每轮最多出几张作战卡（几个标的开会）。不写 = 用 app.toml 的 card_pick_max；"
        "写了取两者较小值，不会超过 card_pick_max。",
    )
    concurrency: int = Field(default=4, ge=1, le=8, description="同时开几个标的的会议。")
    card_ttl_hours: float = Field(default=2.0, ge=1.0, le=4.0, description="作战卡有效期。")
    symbol_min_interval_hours: float = Field(
        default=2.0, ge=0.5, le=12.0, description="同一个币两次会议的最小间隔。"
    )
    refresh_lead_min: float = Field(
        default=15.0,
        ge=5.0,
        le=60.0,
        description="作战卡到期前多久开会换卡。要大于一轮会议的耗时，否则卡过期到新卡出来之间不开新仓。",
    )
    symbol_timeout_s: float = Field(
        default=600.0, ge=60.0, le=1800.0, description="单个标的的会议超时。"
    )
    min_refresh_gap_min: float = Field(
        default=5.0, ge=1.0, le=60.0, description="两轮开会的最小间隔（防抖）。"
    )


class BudgetConfig(_Strict):
    daily_cost_limit_usd: float = Field(
        default=10.0, gt=0, le=1000, description="军师团每个交易日（曼谷 0 点清零）最多花多少美元。"
    )
    failed_meeting_cost_usd: float = Field(
        default=0.05,
        ge=0,
        le=5,
        description="失败的会议拿不到真实 token 用量时（超时、断网）按这个估算价计入日花费。",
    )
    fail_backoff_minutes: tuple[float, ...] = Field(
        default=(5.0, 10.0, 30.0), description="开会连续失败后的等待时间，最后一档之后保持不变。"
    )

    @field_validator("fail_backoff_minutes")
    @classmethod
    def _backoff(cls, v: tuple[float, ...]) -> tuple[float, ...]:
        if not v or any(not 1 <= m <= 240 for m in v):
            raise ValueError("至少一档，每档 1–240 分钟")
        return v


class PricesConfig(_Strict):
    """每百万 token 的美元价，只用于花费统计。⚠ 占位值，M2 真机验证时按官网核对。"""

    flash_in: float = Field(default=0.15, ge=0)
    flash_out: float = Field(default=0.60, ge=0)
    pro_in: float = Field(default=0.50, ge=0)
    pro_out: float = Field(default=2.00, ge=0)


class SlowConfig(_Strict):
    llm: LlmConfig = LlmConfig()
    meeting: MeetingConfig = MeetingConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    budget: BudgetConfig = BudgetConfig()
    prices: PricesConfig = PricesConfig()

    def engine_settings(self) -> TradingAgentsSettings:
        m, p = self.meeting, self.prices
        quick, deep = PROVIDER_MODELS[self.llm.provider]
        return TradingAgentsSettings(
            provider=self.llm.provider,
            quick_model=m.quick_model or quick,
            deep_model=m.deep_model or deep,
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

    def effective_pick_max(self, app_pick_max: int) -> int:
        """每轮最多出几张卡：``max_cards_per_round`` 与 ``card_pick_max`` 取小。"""
        n = self.schedule.max_cards_per_round
        return app_pick_max if n is None else min(n, app_pick_max)

    def commander_kwargs(self) -> dict[str, Any]:
        """传给 ``BattleCardCommander(...)`` 的节奏参数。"""
        s = self.schedule
        return {
            "concurrency": s.concurrency,
            "ttl": timedelta(hours=s.card_ttl_hours),
            "symbol_min_interval": timedelta(hours=s.symbol_min_interval_hours),
            "expiry_lead": timedelta(minutes=s.refresh_lead_min),
            "symbol_timeout_s": s.symbol_timeout_s,
            "min_refresh_gap": timedelta(minutes=s.min_refresh_gap_min),
            "daily_cost_limit_usd": self.budget.daily_cost_limit_usd,
            "failed_meeting_cost_usd": self.budget.failed_meeting_cost_usd,
            "fail_backoff": tuple(timedelta(minutes=m) for m in self.budget.fail_backoff_minutes),
        }


def with_profile(raw: dict[str, Any], profile: str) -> dict[str, Any]:
    """档位默认垫在 slow.toml 下面：写了的键优先（见 core/profiles.py）。"""
    data = dict(raw)
    sections: tuple[tuple[str, profiles.Section], ...] = (
        ("schedule", "slow.schedule"),
        ("budget", "slow.budget"),
    )
    for name, key in sections:
        section = data.get(name, {})
        if isinstance(section, dict):
            data[name] = profiles.fill(section, profile, key)
    return data


def slow_defaults(profile: str = profiles.DEFAULT_PROFILE) -> SlowConfig:
    """没有 slow.toml 时用的配置：代码默认 + 档位默认。"""
    return SlowConfig.model_validate(with_profile({}, profile))


def load_slow_config(
    path: Path = DEFAULT_SLOW_PATH, profile: str = profiles.DEFAULT_PROFILE
) -> SlowConfig:
    """缺文件 → 默认值（含档位）；有文件但不合格 → ``ConfigError``。"""
    if not path.is_file():
        return slow_defaults(profile)
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} 不是合法的 TOML：{e}") from e
    try:
        return SlowConfig.model_validate(with_profile(data, profile))
    except ValidationError as e:
        lines = [f"{path} 不合格，拒绝启动："]
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(整体)"
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from e
