"""慢环的成本 / 节奏配置（``config/slow.toml``）。

只管开会花多少钱、多频繁：分析师数量、辩论轮数、每个环节用 flash 还是 pro、并发、卡片有效期、
每个币的最小开会间隔。**不放任何风控数值**（硬上限只在 ``risk.toml``）。

和 ``risk.toml`` / ``app.toml`` 不同，这个文件可以不存在：缺文件就用默认值。``[schedule]`` /
``[budget]`` 里没写的键取当前持仓档位的默认（app.toml 的 ``profile``，见 ``core/profiles.py``）。
写了但不合格（拼错字段、超范围）则报错拒绝启动。
示例见 ``config/slow.example.toml``。
"""

from __future__ import annotations

import importlib.util
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from jiaoyi.contracts import Category, SizeTier
from jiaoyi.core import profiles
from jiaoyi.core.config import ConfigError
from jiaoyi.interfaces import SecretStore
from jiaoyi.slow.commander import TierRules
from jiaoyi.slow.screener import ScreenerConfig
from jiaoyi.slow.tradingagents_engine import (
    PROVIDER_KEY_ENV,
    PROVIDER_MODELS,
    TradingAgentsSettings,
)

DEFAULT_SLOW_PATH = Path("config/slow.toml")
ANALYSTS = ("market", "social", "news", "fundamentals")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LlmConfig(_Strict):
    provider: Literal["deepseek", "openrouter"] = Field(
        default="deepseek",
        description="deepseek=直连；openrouter=经 OpenRouter（各自的 API key）",
    )


class SocialConfig(_Strict):
    reddit: Literal["guarded", "off"] = Field(
        default="guarded",
        description="guarded=抓 Reddit 但加闸（缓存、限间隔、失败冷却）；off=不抓。",
    )


class LevelsConfig(_Strict):
    span_sigma: float = Field(
        default=1.5,
        ge=0.5,
        le=5.0,
        description="关键位范围 = 这个系数 × 该币 ATR × √(持仓时长 / ATR 周期)。"
        "对所有币、所有档位用同一条公式：ATR 乘根号时间就是持仓期内价格的典型波动。",
    )


class CategoryRule(_Strict):
    """一类币的开关和进池上限（``[screener.categories.<类别>]``）。"""

    enabled: bool = Field(
        default=True,
        description="关掉后这一类不再靠打分进池；你固定 / 手动加入的币仍进池（要过硬条件）。",
    )
    cap: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="这一类最多进池几个；不写 = 用上面的「每类默认上限」。0 = 一个都不要。",
    )


class TiersSection(_Strict):
    """分档交易（``[screener.tiers]``，设计文档 §六）：头部档开放交易；中间档军师每天挑几个、
    仓位档位封顶；尾部档只看不做。池里的币按分数从高到低依次分进三档。"""

    enabled: bool = Field(
        default=True, description="关掉 = 不分档，整个候选池都参与开会（原来的做法）。"
    )
    head: int = Field(
        default=10, ge=1, le=100, description="头部档几个（你固定 / 手动加入的币一律算头部）。"
    )
    mid: int = Field(default=15, ge=0, le=100, description="中间档几个；头部之后的都是尾部。")
    mid_daily_pick: int = Field(
        default=5, ge=0, le=100, description="中间档每天（曼谷日）最多挑几个来交易，当天第一轮挑。"
    )
    mid_size_max: Literal["S", "M", "L"] = Field(
        default="S", description="中间档的币仓位档位最大封到哪一档（S 最小）。"
    )

    def cautions(self) -> list[str]:
        out: list[str] = []
        if self.mid_daily_pick > self.mid:
            out.append("中间档每天挑的个数比中间档本身还多，多出来的名额用不上")
        if self.mid_size_max == "L":
            out.append("中间档仓位档位不封顶，和头部档一样大（设计稿的意思是比头部谨慎）")
        return out


class ScreenerSection(_Strict):
    """选币器的门槛和权重（``[screener]``）。每小时用代码筛一遍全部币安 U 本位永续：先过硬门槛，
    再按下面的权重打分，取前 N 个进候选池。默认值就是原来写死的值。你定的值就是上限，只提示不拦，
    除非数学上不成立（权重全 0、下限大于上限）。"""

    min_quote_volume_usdt: float = Field(
        default=20_000_000.0,
        ge=0,
        description="最低 24 小时成交额（美元）。太冷清的币进出容易滑点。",
    )
    max_spread_bps: float | None = Field(
        default=None,
        gt=0,
        description="最大买卖价差（万分之几）。不写 = 跟硬上限里的「最大价差」走，"
        "免得进池的币保镖一律拒。写得比硬上限大会被提示。",
    )
    min_depth_usdt: float = Field(
        default=100_000.0, ge=0, description="盘口最小深度（美元）。太浅的币大单吃不下。"
    )
    min_listed_days: int = Field(
        default=7, ge=0, le=365, description="至少上线几天。新币波动和价差不稳。"
    )
    min_range_24h_pct: float = Field(
        default=0.0,
        ge=0,
        le=100,
        description="24 小时波动下限（%）。0 = 不设；推荐 1.5，波动太小赚不回手续费。",
    )
    max_range_24h_pct: float = Field(
        default=0.0,
        ge=0,
        le=500,
        description="24 小时波动上限（%）。0 = 不设；推荐 30，波动过大止损容易被针扫。",
    )
    default_category_cap: int = Field(
        default=30,
        ge=1,
        le=100,
        description="每一类币（主流 / 山寨 / 币股 / 美股科技 / 贵金属）默认最多进池几个。"
        "选币页单独改过的类别以选币页为准。",
    )
    tiers: TiersSection = TiersSection()
    us_events: bool = Field(
        default=True,
        description="美股类别（加密概念股 / 美股科技）用美股开盘时段和财报日历：开盘期间加分，"
        "财报日前后在选币理由里标注，军师团新闻里带上财报日。",
    )
    us_open_bonus: float = Field(
        default=0.08,
        ge=0,
        le=0.3,
        description="美股常规盘开盘期间，美股类别的币总分加多少（总分满分约 1；0 = 不加）。",
    )
    categories: dict[Category, CategoryRule] = Field(
        default_factory=dict,
        description="按类别开关和设上限：major / alt / crypto_stock / us_tech / precious_metal / "
        "other。不写的类别 = 开、用默认上限。选币页上的设置优先于这里。",
    )
    vol_full_multiple: float = Field(
        default=20.0,
        gt=0,
        le=1000,
        description="24 小时波动是来回手续费的几倍算「波动分」满分。",
    )
    surge_full_multiple: float = Field(
        default=8.0,
        gt=1,
        le=1000,
        description="最近 1 小时成交额是平时的几倍算「量能分」满分。",
    )
    funding_full: float = Field(
        default=0.001,
        gt=0,
        le=0.1,
        description="资金费率绝对值到多少算「资金费分」满分（0.001 = 0.1%）。",
    )
    oi_full_pct: float = Field(
        default=10.0, gt=0, le=1000, description="持仓量一小时变化多少 % 算「持仓量分」满分。"
    )
    liq_full_usdt: float = Field(
        default=10_000_000.0, gt=10, description="近 1 小时强平额多少美元算「强平分」满分。"
    )
    weight_volatility: float = Field(
        default=0.35, ge=0, le=1, description="波动够不够覆盖手续费，占总分的权重。"
    )
    weight_volume_surge: float = Field(
        default=0.25, ge=0, le=1, description="量能突增，占总分的权重。"
    )
    weight_funding: float = Field(
        default=0.15, ge=0, le=1, description="资金费率极端，占总分的权重。"
    )
    weight_oi: float = Field(default=0.15, ge=0, le=1, description="持仓量变化，占总分的权重。")
    weight_liquidations: float = Field(
        default=0.10, ge=0, le=1, description="近 1 小时强平，占总分的权重。"
    )

    @model_validator(mode="after")
    def _sane(self) -> ScreenerSection:
        if (
            self.min_range_24h_pct > 0
            and self.max_range_24h_pct > 0
            and self.min_range_24h_pct >= self.max_range_24h_pct
        ):
            raise ValueError("波动下限必须小于波动上限（两个都不为 0 时）")
        if self.weights_total <= 0:
            raise ValueError("五项权重不能全是 0，否则所有币同分、选不出先后")
        return self

    @property
    def weights_total(self) -> float:
        return (
            self.weight_volatility
            + self.weight_volume_surge
            + self.weight_funding
            + self.weight_oi
            + self.weight_liquidations
        )

    def cautions(self, risk_max_spread_bps: float) -> list[str]:
        """比推荐冒险的设置（只提示，不拒绝）。"""
        out: list[str] = []
        if self.min_quote_volume_usdt < 5_000_000:
            out.append("最低成交额低于 500 万美元，冷门币进出容易滑点（推荐 2000 万）")
        if self.min_depth_usdt < 20_000:
            out.append("盘口最小深度低于 2 万美元，大单可能吃不下（推荐 10 万）")
        if self.min_listed_days < 3:
            out.append("上线不满 3 天的新币也会进池，波动和价差不稳（推荐 7 天）")
        if self.max_spread_bps is not None and self.max_spread_bps > risk_max_spread_bps:
            out.append(
                f"选币的最大价差 {self.max_spread_bps:g} 比硬上限里的 {risk_max_spread_bps:g} 宽，"
                "多出来的币进池后保镖会一律拒绝"
            )
        out.extend(self.tiers.cautions())
        if self.categories and not any(
            self.categories.get(c, CategoryRule()).enabled for c in Category
        ):
            out.append("所有类别都关了：除了你固定 / 手动加入的币，候选池会是空的")
        return out


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
    max_tokens: int = Field(
        default=8192,
        ge=1024,
        le=32768,
        description="每次回答的长度上限，思考模型的思考也算在里面：低于 8192 实测会让多空辩论"
        "被挤成半句或空壳。出作战指令、挑币两步另按 8192 起。",
    )
    reasoning_effort: Literal["none", "low", "medium", "high"] = Field(
        default="low",
        description="推理模型想多久：none 关掉推理，low / medium / high 越高想得越久。"
        "思考也算在 max_tokens 里，不限的话个别币会把额度想完、回答被截断。只对走 OpenRouter 的"
        "通道生效。",
    )
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
    concurrency: int = Field(default=8, ge=1, le=8, description="同时开几个标的的会议。")
    card_ttl_hours: float = Field(default=2.0, ge=1.0, le=4.0, description="作战卡有效期。")
    symbol_min_interval_hours: float = Field(
        default=2.0, ge=0.5, le=12.0, description="同一个币两次会议的最小间隔。"
    )
    refresh_lead_min: float = Field(
        default=20.0,
        ge=5.0,
        le=60.0,
        description="作战卡到期前多久开会换卡。要大于一轮会议的耗时，否则卡过期到新卡出来之间不开新仓。",
    )
    symbol_timeout_s: float = Field(
        default=900.0, ge=60.0, le=1800.0, description="单个标的的会议超时。"
    )
    min_refresh_gap_min: float = Field(
        default=5.0,
        ge=1.0,
        le=1440.0,
        description="两轮开会的最小间隔（分钟）。关键位突破、大消息、新入池都要等过了这个间隔"
        "才开会；档位默认 = 卡有效期 − 提前量（2h 档 100 分钟），即「卡多久有效就多久开一次」。",
    )

    @model_validator(mode="after")
    def _lead(self) -> ScheduleConfig:
        if self.refresh_lead_min >= self.card_ttl_hours * 60:
            raise ValueError("refresh_lead_min 必须小于作战卡有效期（card_ttl_hours × 60）")
        if self.min_refresh_gap_min >= self.card_ttl_hours * 60:
            raise ValueError("min_refresh_gap_min 必须小于作战卡有效期，否则卡会过期断档")
        return self

    def caution(self) -> str | None:
        """单币超时 + 5 分钟余量比提前量还长时的提示（只提示不拒绝）：
        一个币拖到超时，旧卡过期后到新卡出来之间就不开新仓。"""
        need = self.symbol_timeout_s / 60 + 5
        if need <= self.refresh_lead_min:
            return None
        return (
            f"单币会议超时 {self.symbol_timeout_s:.0f} 秒（加 5 分钟余量约 {need:.0f} 分钟）"
            f"比换卡提前量 {self.refresh_lead_min:g} 分钟长，个别币拖到超时时旧卡过期后"
            "到新卡出来之间可能不开新仓"
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
    """每百万 token 的美元价。只在通道没报真实花费时估算用：走 OpenRouter 时用它返回的
    ``usage.cost``，不看这里。flash 按 OpenRouter deepseek/deepseek-v4.1-flash 标价（2026-10-23
    核对）；pro 是占位值。"""

    flash_in: float = Field(default=0.30, ge=0)
    flash_out: float = Field(default=1.20, ge=0)
    pro_in: float = Field(default=0.50, ge=0)
    pro_out: float = Field(default=2.00, ge=0)


class SlowConfig(_Strict):
    llm: LlmConfig = LlmConfig()
    meeting: MeetingConfig = MeetingConfig()
    social: SocialConfig = SocialConfig()
    levels: LevelsConfig = LevelsConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    screener: ScreenerSection = ScreenerSection()
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
            json_max_tokens=max(8192, m.max_tokens),
            max_retries=m.max_retries,
            reasoning_effort=m.reasoning_effort,
            price_per_mtok={"flash": (p.flash_in, p.flash_out), "pro": (p.pro_in, p.pro_out)},
            analysts_crypto=m.analysts_crypto,
            analysts_stock=m.analysts_stock,
            reddit=self.social.reddit,
            level_span_sigma=self.levels.span_sigma,
            pick_tier=m.pick_tier,
            convert_tier=m.convert_tier,
        )

    def screener_config(
        self, pool_size: int, risk_max_spread_bps: float, round_trip_fee_bps: float
    ) -> ScreenerConfig:
        """``[screener]`` → 选币器参数。池大小来自 app.toml，价差默认和手续费跟硬上限走。"""
        s = self.screener
        return ScreenerConfig(
            pool_size=pool_size,
            min_quote_volume_usdt=s.min_quote_volume_usdt,
            max_spread_bps=s.max_spread_bps or risk_max_spread_bps,
            min_depth_usdt=s.min_depth_usdt,
            min_listed_days=s.min_listed_days,
            round_trip_fee_bps=round_trip_fee_bps,
            default_category_cap=s.default_category_cap,
            tiers=s.tiers.enabled,
            tier_head=s.tiers.head,
            tier_mid=s.tiers.mid,
            us_events=s.us_events,
            us_open_bonus=s.us_open_bonus,
            category_caps={c: r.cap for c, r in s.categories.items() if r.cap is not None},
            category_enabled={c: r.enabled for c, r in s.categories.items()},
            min_range_24h_pct=s.min_range_24h_pct,
            max_range_24h_pct=s.max_range_24h_pct,
            vol_full_multiple=s.vol_full_multiple,
            surge_full_multiple=s.surge_full_multiple,
            funding_full=s.funding_full,
            oi_full_pct=s.oi_full_pct,
            liq_full_usdt=s.liq_full_usdt,
            weights={
                "vol_vs_fees": s.weight_volatility,
                "volume_surge": s.weight_volume_surge,
                "funding_extreme": s.weight_funding,
                "oi_change": s.weight_oi,
                "liquidations": s.weight_liquidations,
            },
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
            "tiers": TierRules(
                enabled=self.screener.tiers.enabled,
                mid_daily_pick=self.screener.tiers.mid_daily_pick,
                mid_size_max=SizeTier(self.screener.tiers.mid_size_max),
            ),
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


def missing_slow_stack(cfg: SlowConfig, secrets: SecretStore) -> str | None:
    """配了军师团的 key 却没装 ``tradingagents`` 时返回错误说明，否则 None。

    只有“没有 key”才允许用桩（M0/M1 阶段）；有 key 说明想跑真军师团，这时静默退回桩会让
    整个 M2 空跑（不出卡、不开新仓却看不出来），所以直接报错拒绝启动。
    """
    key_env = PROVIDER_KEY_ENV[cfg.llm.provider]
    if secrets.get(key_env) and importlib.util.find_spec("tradingagents") is None:
        return (
            f"已配置 {key_env}，但没装 tradingagents，军师团跑不起来。"
            "先执行：make install（或 uv pip install -e '.[slow]'），装好再启动。"
        )
    return None
