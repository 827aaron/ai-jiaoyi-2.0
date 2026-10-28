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
import logging
import math
import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from jiaoyi.contracts import Category, SizeTier
from jiaoyi.core import profiles
from jiaoyi.core.config import ConfigError
from jiaoyi.interfaces import SecretStore
from jiaoyi.slow.bias_fix import BiasFix
from jiaoyi.slow.commander import EVENT_GAP_FLOOR, EventRules, TierRules
from jiaoyi.slow.screener import ScreenerConfig
from jiaoyi.slow.tradingagents_engine import (
    PROVIDER_KEY_ENV,
    PROVIDER_MODELS,
    TradingAgentsSettings,
    parse_kline_spec,
)

DEFAULT_SLOW_PATH = Path("config/slow.toml")
ANALYSTS = ("market", "social", "news", "fundamentals")
SCREENER_SPREAD_BPS = ScreenerConfig.max_spread_bps
"""选币价差门槛：选币和硬上限都没写时的兜底（万分之几）。"""

logger = logging.getLogger(__name__)


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
    always_candidates: list[str] = Field(
        default_factory=lambda: ["BTC", "ETH"],
        description="每轮都进总盘会候选的币：过了硬门槛就进池，不算头部、不占中间档每天的名额。"
        "总盘会照样按数字决定挑不挑；写币名，不带 USDT。",
    )
    always_candidates_meet: bool = Field(
        default=False,
        description="打开 = 上面这些币每轮不经总盘会挑选，直接开单币会议"
        "（会议自己定做多 / 做空 / 不做），"
        "不占每轮的挑币名额；执行交易所上不在交易的币照样不开。关掉 = 照旧由总盘会决定挑不挑。",
    )

    @field_validator("always_candidates", mode="before")
    @classmethod
    def _base_names(cls, v: Any) -> Any:
        """写 BTC 或 BTCUSDT 都行，统一成不带 USDT 的大写币名。"""
        if not isinstance(v, list):
            return v
        out: list[str] = []
        for x in v:
            name = str(x).strip().upper().removesuffix("USDT")
            if name and name not in out:
                out.append(name)
        return out

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
        "免得进池的币风控员一律拒。写得比硬上限大会被提示。",
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
        ge=0,
        le=100,
        description="每一类币（主流 / 山寨 / 币股 / 美股科技 / 贵金属）默认最多进池几个。"
        "选币页单独改过的类别以选币页为准。0 = 不限（只受候选池大小管）。",
    )
    tiers: TiersSection = TiersSection()
    major_top_n: int = Field(
        default=20,
        ge=0,
        le=100,
        description="「主流币」= 市值前几名（每天自动拉排名，去掉稳定币和包装币，对应到币安永续）。"
        "0 = 不用市值排名，只认写死的 7 个（BTC ETH BNB SOL XRP DOGE ADA）。",
    )
    major_include: list[str] = Field(
        default_factory=list,
        description="手动加入主流币的币名（如 LINK），不管市值排名。",
    )
    major_exclude: list[str] = Field(
        default_factory=list,
        description="手动排除出主流币的币名；被排除的按山寨币算。",
    )
    allow_tradfi: bool = Field(
        default=False,
        description="让币安 TRADIFI_PERPETUAL（美股 / 金属等传统资产永续）进池。"
        "默认关：等在 Demo 上把开平仓、保命止损、行情流整条链验证完再开。",
    )
    exclude_untradeable: bool = Field(
        default=True,
        description="剔掉执行交易所（Demo / 实盘）上不在交易的币（状态不是 TRADING，如结算中，"
        "或没有下单精度）：选币用主网数据，这类币进池会白占会议和卡位。关掉 = 不管交易所状态。",
    )
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
        default=30.0,
        gt=0,
        le=1000,
        description="按每小时折算的正常波动是来回手续费的几倍算「波动分」满分"
        "（默认 30，也就是每小时 3%）。",
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

    def cautions(self, risk_max_spread_bps: float | None) -> list[str]:
        """比推荐冒险的设置（只提示，不拒绝）。"""
        out: list[str] = []
        if self.min_quote_volume_usdt < 5_000_000:
            out.append("最低成交额低于 500 万美元，冷门币进出容易滑点（推荐 2000 万）")
        if self.min_depth_usdt < 20_000:
            out.append("盘口最小深度低于 2 万美元，大单可能吃不下（推荐 10 万）")
        if self.min_listed_days < 3:
            out.append("上线不满 3 天的新币也会进池，波动和价差不稳（推荐 7 天）")
        if self.max_spread_bps is not None and risk_max_spread_bps is not None:
            if self.max_spread_bps > risk_max_spread_bps:
                out.append(
                    f"选币的最大价差 {self.max_spread_bps:g} 比硬上限里的 "
                    f"{risk_max_spread_bps:g} 宽，多出来的币进池后风控员会一律拒绝"
                )
        elif self.max_spread_bps is not None and self.max_spread_bps > SCREENER_SPREAD_BPS:
            out.append(
                f"选币的最大价差 {self.max_spread_bps:g} 比推荐的 {SCREENER_SPREAD_BPS:g} 宽，"
                "风控员按止损的 1/10 卡价差，价差大的币进池后多半开不了仓"
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
    analyst_klines: tuple[str, ...] | None = Field(
        default=None,
        description='分析师看的 K 线，每段 "周期 x 根数"，如 ["15m x 96", "1h x 72", "4h x 60"]。'
        "不写 = 按最长持仓档：30m 5m×48 + 15m×48 + 1h×48；1h / 2h 5m×48 + 15m×96 + 1h×72；"
        "4h 15m×96 + 1h×72 + 4h×60；8h 起 1h×72 + 4h×90 + 1d×60。"
        "开会间隔比最细一段还短，最前面自动补 15m×48。",
    )
    snapshot_interval: Literal["5m", "15m", "30m", "1h", "4h"] | None = Field(
        default=None,
        description="行情分析师的「已收盘快照」和指标用几分钟 K 线；不写 = 按最长持仓档"
        "（4h 档是 1h，最旧约 1 小时）。写 15m 快照最旧约 15 分钟。提示词里的指标周期、"
        "多空比的统计周期跟着一起换。",
    )
    snapshot_label: Literal["legacy", "neutral"] = Field(
        default="legacy",
        description="快照怎么写：legacy = 叫「source of truth」（现状）；neutral = 写明「最后一根"
        "已收盘 K 线 HH:MM 收盘，距现在 N 分钟」，行情分析师的说明同样不再叫它权威。",
    )
    live_price_line: bool = Field(
        default=False,
        description="开 = 会议说明加一行实时价（开会时的最新成交价、离最后一根已收盘 K 线涨跌多少，"
        "没走完的那根标成未收盘），转作战指令前再取一次现价写进去。每场会多 4 个币安公开请求。",
    )
    stale_on_arrival: bool = Field(
        default=False,
        description="开 = 转作战指令后用现价核对：做多现价已跌到失效价或以下，做空现价已涨到"
        "失效价或以上；或开会后价格穿过了整个进场区间（开会时还没越过）→ 按 "
        "stale_on_arrival_action 处理，理由写 stale_on_arrival，记日志。",
    )
    stale_on_arrival_action: Literal["watch", "no_trade"] = Field(
        default="watch",
        description="stale_on_arrival 命中后：watch = 只盯不开（方向不动，清掉信号和进场区间，"
        "持仓的反向信号和方向记分照旧）；no_trade = 改成观望。",
    )
    input_time_labels: Literal["legacy", "explicit"] = Field(
        default="legacy",
        description="legacy = 现状；explicit = 总盘会表格的 takerBuy1h / OI1h 写成「已收盘小时 "
        "HH:00–HH:00」，单币会议的资金费率写「上次结算 HH:MM」并加币安 premiumIndex 的实时估算"
        "（每场会多 1 个公开请求）。",
    )
    market_table_rolling: bool = Field(
        default=False,
        description="开 = 总盘会的池子概况另给一行滚动 60 分钟涨跌家数（1 分钟 K 线，截到开会"
        "这一刻，例如 10:54 开会就是 09:54–10:54）；池里每个币多 1 个公开请求。",
    )
    lessons_scoring: Literal["raw", "excess"] = Field(
        default="raw",
        description="喂给会议的教训怎么给卡打分：raw = 会后一个持仓期的原始涨跌（山寨币另比 BTC，"
        "现状）；excess = 和方向记分同口径，发布时刻起 4 小时、扣掉同期币池等权收益（读 "
        "data/card_score/，不另拉 K 线）。",
    )
    latest_market_regime: bool = Field(
        default=False,
        description="开 = 整轮里每个单币会开始时取当时最新发布的大盘判断（本轮中途 Flash 更新过"
        "就用新的），并在 meeting_setup 里记用了哪一版；关 = 用本轮开始时的大盘判断（现状）。",
    )
    event_brief_refresh: bool = Field(
        default=False,
        description="开 = 临时会开会前刷新大盘简报（简报自己 10 分钟内不重复拉）；关 = 用上一轮"
        "留下的简报（现状，最旧可到一个开会间隔）。",
    )
    regime_time_label: bool = Field(
        default=False,
        description="开 = 各币会议的大盘判断前写明这份判断几点发布、距开会多少分钟（不管 "
        "publish_intra 开没开）；持仓管家和拍板人快照的 current_market 加 age_min。关 = 现状"
        "（只有 publish_intra 开着、观点是大盘台发的时候才写发布时刻）。",
    )
    regime_max_age_min: float = Field(
        default=0.0,
        ge=0.0,
        le=1440.0,
        description="大盘判断发布超过这么多分钟：各币会议里标「已过期，仅作参考」，管家和拍板人"
        "快照的 current_market 加 age_min 和 stale。0 = 不标（现状）。",
    )
    pool_scope_label: bool = Field(
        default=False,
        description="开 = 各币会议里大盘判断和「4 小时之内」前标明其中的涨跌、主动买卖占比等数字是"
        "全池口径，不是这个币自己的。关 = 现状。",
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
    max_tokens: int | None = Field(
        default=None,
        ge=1024,
        le=2_000_000,
        description="每次回答的长度上限，思考模型的思考也算在里面。不写 = 16384（和模型最大输出"
        "取小）：OpenRouter 按它预留额度，写成模型最大 384000 会让并发开会很快撞 402；写了就按"
        "你的。低于 8192 实测会让多空辩论被挤成半句或空壳。出作战指令、挑币两步另按 8192 起。",
    )
    retry_bad_steps: bool = Field(
        default=True,
        description="某一步回答被截断 / 空 / 过短时自动重试一次（关掉推理、提示直接给结论），"
        "重试仍不行才让整场失败。",
    )
    speech_min_chars: int = Field(
        default=50,
        ge=0,
        le=2000,
        description="每个辩手每轮发言至少多少字，低于就算没发言，这场会不能用。0 = 只查空。",
    )
    report_min_chars: int = Field(
        default=50,
        ge=0,
        le=2000,
        description="分析师报告至少多少字，低于（或短且像工具报错）算缺失：行情技术缺了这场会不能用；"
        "新闻 / 社交 / 基本面缺了照常出结论，但仓位档位下调一级。",
    )
    retry_min_chars: int = Field(
        default=20, ge=0, le=500, description="回答（没有工具调用时）少于这么多字算过短，触发重试。"
    )
    reasoning_effort: Literal["none", "low", "medium", "high"] = Field(
        default="low",
        description="推理模型想多久：none 关掉推理，low / medium / high 越高想得越久。"
        "思考也算在 max_tokens 里，不限的话个别币会把额度想完、回答被截断。只对走 OpenRouter 的"
        "通道生效。",
    )
    max_retries: int = Field(default=6, ge=0, le=10)
    # 偏空整改（见 slow/bias_fix.py 和 short-bias-investigation-2026-10-28），默认 = 现状
    debate_first: Literal["bull", "bear", "alternate"] = Field(
        default="bull",
        description="多空辩论谁先说：bull = 多头先、空头最后说（现状）；bear = 反过来；"
        "alternate = 按会议交替（按会议 id 哈希，可复现），两边各一半时间有最后发言。",
    )
    funding_label: Literal["legacy", "neutral"] = Field(
        default="legacy",
        description="资金费率怎么写：legacy = 正费率写成「多头拥挤」（现状）；"
        "neutral = 只写谁付谁 + 币安基准 +0.01%/8h（按结算间隔折算），写明明显高于基准才算"
        "多头拥挤、明显低于基准才算空头拥挤。",
    )
    bear_bull_symmetry: Literal["legacy", "mirror"] = Field(
        default="legacy",
        description="legacy = 现状（空头独有「拥挤 / 已被挤出的不再是燃料」，多头独有「强平耗尽后"
        "常反弹」等）；mirror = 多空两份提示词逐句对称，经理 / 交易员 / 共用说明里的单边写法补齐"
        "另一边。",
    )
    crowd_ratio_mode: Literal["raw", "relative"] = Field(
        default="raw",
        description="账户多空比：raw = 只给原值（现状）；relative = 再给相对这个币自己 "
        "30 天中位数的偏离并注明多数币长期在 1 以上（每个币多一次币安公开请求，缓存 6 小时）。",
    )
    bias_fix_ab: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="0 = 上面四项每场会都按配置；> 0 = 析因对照：每个改了的开关各自独立按这个"
        "概率用配置值、否则用现状（按会议 id 加盐哈希，可复现），记进决策日志和复盘流水。",
    )

    @model_validator(mode="after")
    def _bias_fix_ab_needs_a_change(self) -> MeetingConfig:
        if self.bias_fix_ab > 0 and self.bias_fix().is_legacy:
            raise ValueError(
                "bias_fix_ab > 0 但 debate_first / funding_label / bear_bull_symmetry / "
                "crowd_ratio_mode 都是现状，两组一模一样，对照没有意义：先改至少一项"
            )
        return self

    def bias_fix(self) -> BiasFix:
        return BiasFix(
            debate_first=self.debate_first,
            funding_label=self.funding_label,
            bear_bull_symmetry=self.bear_bull_symmetry,
            crowd_ratio_mode=self.crowd_ratio_mode,
            ab=self.bias_fix_ab,
        )

    @field_validator("analysts_crypto", "analysts_stock")
    @classmethod
    def _analysts(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if not v:
            raise ValueError("至少要有一位分析师")
        bad = [a for a in v if a not in ANALYSTS]
        if bad or len(set(v)) != len(v):
            raise ValueError(f"分析师只能从 {ANALYSTS} 里选且不能重复，收到 {list(v)}")
        return v

    @field_validator("analyst_klines")
    @classmethod
    def _klines(cls, v: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if v is not None:
            if not v:
                raise ValueError("至少一段 K 线")
            for spec in v:
                parse_kline_spec(spec)
        return v


EventTrigger = Literal[
    "key_level_break",
    "move_since_card",
    "new_head_symbol",
    "predictor_against_card",
    "signal_against_card",
    "news_high_impact",
    "stale_card_on_entry",
]


class ScheduleConfig(_Strict):
    max_cards_per_round: int | None = Field(
        default=None,
        ge=0,
        le=50,
        description="每轮最多出几张作战卡（几个标的开会）。不写 = 用 app.toml 的 card_pick_max；"
        "写了取两者较小值，不会超过 card_pick_max。0 = 不限（只受 card_pick_max 管）。",
    )
    concurrency: int = Field(
        default=8,
        ge=1,
        le=32,
        description="同时开几个标的的会议（整轮各币会 + 临时会合计不超过它）。"
        "16 起留意 OpenRouter 的同时请求数和币安 REST 权重（见 docs/slow.md）。",
    )
    card_ttl_min: float = Field(
        default=140.0,
        ge=10.0,
        le=1560.0,
        description="作战卡有效期（分钟）。档位默认 = 开会间隔 + 换卡提前量。",
    )
    symbol_min_interval_min: float = Field(
        default=115.0, ge=5.0, le=1440.0, description="同一个币两次会议的最小间隔（分钟）。"
    )
    reuse_extend_expiry: bool = Field(
        default=False,
        description="开 = 整轮里因为同币最小间隔没重开会、沿用上一张卡的币，到期时间顺延到"
        "这一轮新卡的到期时间，但从它自己开会那一刻算最多活一个卡有效期（card_ttl_min）；"
        "整轮选币期间开完的临时会同样顺延。关 = 沿用的指令保留原到期时间（现状，可能在下一轮"
        "之前就消失）。",
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
        default=120.0,
        ge=1.0,
        le=1440.0,
        description="两轮开会的最小间隔（分钟）。关键位突破、大消息、新入池都要等过了这个间隔"
        "才开会；档位默认 = 档位分钟数（2h 档 120 / 60m 60 / 30m 30 / 15m 15），"
        "即「档位 = 开会间隔」。",
    )

    event_meeting_enabled: bool = Field(
        default=False,
        description="开临时会：卡有效期里出了事件（关键位突破、走了 3 倍 ATR 等），"
        "只给那一个币补开一场，不重开整轮。开会间隔 2h 起默认开；15m–1h 一轮本身够勤，默认关。"
        "关掉时关键位突破 / 大消息 / 新入池照旧重开整轮。",
    )
    event_triggers: tuple[EventTrigger, ...] = Field(
        default=(),
        description="哪些事件能触发临时会。不在名单里的事件不会开会，等下一轮定时会。"
        "signal_against_card = 同一个币 30 分钟内哨兵 2 次看到和卡相反的信号"
        "（次数和时间窗在 app.toml [fast_loop] against_meeting_*）。",
    )
    event_symbol_gap_min: float = Field(
        default=60.0,
        ge=0.0,
        le=1440.0,
        description="同一个币两场临时会（含正式会）至少隔多久。0 = 不限（内部仍隔 5 分钟，"
        "免得关键位一直被穿时同一个币连着开）。",
    )
    event_concurrency: int = Field(
        default=1,
        ge=0,
        le=32,
        description="同时最多开几场临时会（不同币）。1 = 一场一场排队；0 = 跟 concurrency 一样。"
        "大于 1 时整轮开会期间也能给本轮不开的币开临时会，和整轮共用 concurrency 的名额；"
        "同一个币不会同时开两场。不能大于 concurrency。",
    )
    event_max_per_day: int = Field(
        default=12,
        ge=0,
        le=200,
        description="每个交易日（曼谷）最多开几场临时会。0 = 不限（场数照常计数和记录）。",
    )
    key_level_trigger: Literal["outside", "cross"] = Field(
        default="outside",
        description="key_level_break 怎么算：outside = 价格在关键位外侧就算（只要还在外面，"
        "隔够间隔就再开）；cross = 出卡后价格从里侧穿到外侧那一下才算，出卡时已在外侧的位"
        "要等价格回到里侧再穿过去。",
    )
    move_since_card_atr: float = Field(
        default=3.0,
        gt=0,
        le=20.0,
        description="move_since_card：价格离开上次开会时的价格 ≥ 这么多倍 ATR（最长持仓档的 ATR）"
        "就开临时会。",
    )
    predictor_against_runs: int = Field(
        default=2,
        ge=1,
        le=10,
        description="predictor_against_card：预测员连续几次（每次 5 分钟）看反作战卡的方向才算。",
    )
    entry_max_card_age_min: float = Field(
        default=0.0,
        ge=0.0,
        le=4320.0,
        description="开仓前查卡龄：这个币上次开会距今超过这么久，这次信号不开仓（卡上信号暂时清空），"
        "先补开一场该币的会，新结论出来后的信号再开。0 = 不查。要在 event_triggers 里带 "
        "stale_card_on_entry 才会补会，否则只拦不补。",
    )
    schedule_at: str | None = Field(
        default=None,
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
        description="每天固定时间开整轮会（曼谷时间，如 08:00），不按启动时间漂。"
        "不写 = 按开会间隔。",
    )
    over_budget: Literal["slow", "stop"] = Field(
        default="slow",
        description="花到日上限的 80% 怎么办：slow = 把开会间隔放慢到 slowdown_interval_min（没写则"
        "直接 stop）；stop = 不放慢。不管选哪个，花到 100% 当天都停开会（持仓照常管）。",
    )
    slowdown_interval_min: float | None = Field(
        default=None,
        ge=5.0,
        le=1440.0,
        description="放慢后的开会间隔（分钟）。档位默认 = 下一档（15m→30m→1h→2h→6h）。",
    )

    @model_validator(mode="after")
    def _lead(self) -> ScheduleConfig:
        if self.refresh_lead_min >= self.card_ttl_min:
            raise ValueError("refresh_lead_min 必须小于作战卡有效期（card_ttl_min）")
        if self.min_refresh_gap_min >= self.card_ttl_min:
            raise ValueError("min_refresh_gap_min 必须小于作战卡有效期，否则卡会过期断档")
        if self.event_concurrency > self.concurrency:
            raise ValueError(
                f"event_concurrency（{self.event_concurrency}）不能大于 concurrency"
                f"（{self.concurrency}）：临时会和整轮共用这些名额"
            )
        return self

    def caution(self, pick_max: int | None = None, direct: int = 0) -> str | None:
        """只提示不拒绝：①单币超时 + 5 分钟余量比提前量长，个别币拖到超时时旧卡过期后到新卡出来
        之间不开新仓；②最坏一轮（每波都拖到超时）跑不完开会间隔，下一轮会晚开 / 与上一轮重叠；
        ③每轮直接开会的币（``direct`` 个，不占挑币名额）把一轮的场数推过 ``concurrency``。"""
        notes: list[str] = []
        need = self.symbol_timeout_s / 60 + 5
        if need > self.refresh_lead_min:
            notes.append(
                f"单币会议超时 {self.symbol_timeout_s:.0f} 秒（加 5 分钟余量约 {need:.0f} 分钟）"
                f"比换卡提前量 {self.refresh_lead_min:g} 分钟长，个别币拖到超时时旧卡过期后"
                "到新卡出来之间可能不开新仓"
            )
        # 每轮实际开会的个数：max_cards_per_round（0 / 不写 = 不另设）与 card_pick_max 取小
        picked = min(self.max_cards_per_round or pick_max or self.concurrency, pick_max or 10**6)
        cards = picked + direct
        if direct and cards > self.concurrency >= picked:
            notes.append(
                f"每轮挑 {picked} 个币，再加每轮直接开会的 {direct} 个，共 {cards} 场，"
                f"超过同时开会上限 concurrency（{self.concurrency}），要多排一波，整轮会变长"
            )
        waves = math.ceil(cards / self.concurrency)
        worst = waves * self.symbol_timeout_s / 60
        if worst >= self.min_refresh_gap_min:
            notes.append(
                f"开会间隔 {self.min_refresh_gap_min:g} 分钟，最坏一轮（{waves} 波 × 单币超时 "
                f"{self.symbol_timeout_s:.0f} 秒）要 {worst:g} 分钟，个别币拖到超时就赶不上下一轮；"
                "可调小 symbol_timeout_s、减少 max_cards_per_round，或换大一点的档位"
                "（不会自动拉长间隔）"
            )
        return "；".join(notes) or None


class BudgetConfig(_Strict):
    daily_cost_limit_usd: float = Field(
        default=10.0,
        ge=0,
        le=1000,
        description="军师团每个交易日（曼谷 0 点清零）最多花多少美元。"
        "0 = 不限（不停开会，也不放慢）。",
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


class MarketDeskSection(_Strict):
    """大盘台（``[market_desk]``，设计 2026-10-28）：独立判断「候选池等权组合未来 4 小时」
    涨 / 平 / 跌三项概率，方向由代码按门槛派生。模型 = DeepSeek V4.1 Flash（经 OpenRouter，
    和开会同一个模型），每轮独立问 ``samples`` 次，按 ``aggregation`` 合并。

    回滚注意：旧版本代码不认识这一段，会拒绝启动；回滚到没有大盘台的版本前先删掉整段。
    回滚到没有 ``prior_mode`` / ``momentum_guidance`` / ``aggregation`` 的版本前，先删掉这三行；
    面板改过的 ``band_sigma`` / ``call_p_min`` / ``call_edge_min`` / ``publish_min_gap_min`` 旧代码
    认识，但留着会按改过的值跑，要回到原值就一起删掉。
    改这一段要重启才生效。"""

    mode: Literal["off", "replace"] = Field(
        default="off",
        description="off = 不运行，一次模型调用都没有（和以前一样）；replace = 大盘台接管大盘方向："
        "只有它发布市场观点、设翻转参照，总盘会的大盘判断只记录不发布。"
        "replace 要求 app.toml [fast_loop] regime_flip_target 不是 flash_market_only。",
    )
    model: str = Field(
        default="deepseek/deepseek-v4.1-flash-1017",
        min_length=1,
        description="OpenRouter 模型 id。默认 DeepSeek V4.1 Flash，和开会同一个。",
    )
    reasoning_effort: Literal["none", "low", "medium", "high"] = Field(
        default="low", description="推理长度（OpenRouter reasoning.effort），思考也算在回答长度里。"
    )
    max_tokens: int = Field(
        default=4096, ge=1024, le=65536, description="每次回答的长度上限（含思考）。"
    )
    timeout_s: float = Field(
        default=120.0, ge=10.0, le=900.0, description="单次模型调用超时（秒）。"
    )
    cadence_min: int = Field(
        default=60,
        ge=5,
        le=240,
        description="多久定时跑一轮（分钟），按 UTC 对齐。Demo 推荐 15；每轮花费 × samples。",
    )
    offset_min: int = Field(
        default=2,
        ge=0,
        le=59,
        description="每轮在对齐时刻之后几分钟跑（默认 HH:02，等整点 K 线收好）。"
        "要小于 cadence_min。",
    )
    move_trigger_pct: float = Field(
        default=0.0,
        ge=0.0,
        le=20.0,
        description="急动加跑：move_trigger_window_min 分钟内 |BTC| 或 |候选池涨跌中位数| 走了"
        "≥ 这么多 %（只算上一轮开跑之后的价）就立即加跑一轮；0 = 不按急动加跑。"
        '翻转确认（app.toml regime_flip_target = "desk"）也会立即加跑。',
    )
    move_trigger_window_min: float = Field(
        default=15.0, ge=1.0, le=120.0, description="急动加跑看多长时间窗（分钟）。"
    )
    min_run_gap_min: float = Field(
        default=5.0,
        ge=0.0,
        le=240.0,
        description="加跑（急动 / 翻转）离上一轮开跑至少隔几分钟；没到就等到点再跑。定时轮不受限。",
    )
    samples: int = Field(
        default=5, ge=1, le=10, description="每轮独立问几次（自洽投票），概率取平均；花费 × 次数。"
    )
    min_valid_samples: int = Field(
        default=3,
        ge=1,
        le=10,
        description="一轮至少几次回答合格才采用；不够这一轮作废（不发布、照常记账）。",
    )
    mirror_test_rate: float = Field(
        default=1 / 6,
        ge=0.0,
        le=1.0,
        description="镜像测试的轮次比例（按轮次哈希挑，可复现）：把数据包涨跌全部取反再问一遍，"
        "看 p_up(原) 与 p_down(镜像) 差多少。0 = 不做。镜像那一遍不影响方向，只算偏置。",
    )
    mirror_alert_gap: float = Field(
        default=0.05,
        gt=0.0,
        le=0.5,
        description="最近几次镜像测试的 |p_up(原) − p_down(镜像)| 平均值超过它就告警（方向偏置）。",
    )
    band_sigma: float = Field(
        default=0.5,
        gt=0.0,
        le=3.0,
        description="「平」的宽度：|等权收益| ≤ 这个系数 × σ 算平（4h、1h 各用自己的 σ，"
        "σ 由过去 sigma_lookback_h 个滚动窗口算）。数据包里的基础率、日志里记分用的 "
        "flat_band_pct、提示词里的分类都按它，改了下一轮全部按新宽度重算。"
        "0.5σ 时 30 天基础率「平」约占一半；Demo 推荐 0.3（三类约各占三分之一）。",
    )
    prior_mode: Literal["anchor", "context", "off"] = Field(
        default="anchor",
        description="提示词怎么用先验（基础率）：anchor = 从基础率出发、证据弱就贴着它"
        "（「平」常是众数，回答容易一直中性）；context = 基础率只是输入之一，"
        "明说不要默认它；off = 提示词里不给基础率（数据包和记分照样有）。Demo 推荐 context。",
    )
    momentum_guidance: bool = Field(
        default=False,
        description="提示词里加几句（涨跌成对、中立）：怎样按近 15 分钟 / 1 / 4 小时等权收益、"
        "涨跌家数和 BTC 的同向程度更新概率。Demo 推荐开。",
    )
    aggregation: Literal["mean", "median", "trimmed", "logodds"] = Field(
        default="mean",
        description="几次回答怎么合成一组概率（每项分别合再归一）：mean = 平均；median = 中位数；"
        "trimmed = 去掉最高、最低各一个再平均；logodds = 对数平均（几次都同意的一项被放大）。"
        "平均本身不往「平」收：每项的平均就是各次的平均，涨跌差也是各次的平均。",
    )
    sigma_lookback_h: int = Field(
        default=168, ge=24, le=336, description="算 σ 用过去多少个滚动窗口（小时）。"
    )
    base_rate_days: int = Field(
        default=30, ge=7, le=30, description="先验（涨 / 平 / 跌基础率）统计过去几天。"
    )
    call_p_min: float = Field(
        default=0.45,
        gt=0.0,
        lt=1.0,
        description="方向门槛 θ：p_up ≥ θ 且 p_up − p_down ≥ δ 才算看涨，看跌对称，其余中性。",
    )
    call_edge_min: float = Field(
        default=0.15, ge=0.0, lt=1.0, description="方向门槛 δ（见 call_p_min）。"
    )
    max_age_min: float = Field(
        default=75.0,
        ge=10.0,
        le=1440.0,
        description="最新一轮结果多久内算「可用」；超过（或重启后没有）按 fallback 处理。",
    )
    fallback: Literal["flash", "hold"] = Field(
        default="flash",
        description="大盘台不可用（超过 max_age_min 没有新结果、日上限 / 余额低线自停）时："
        "flash = 整轮总盘会的 Flash 大盘判断恢复发布（和以前一样），大盘台恢复后第一轮立即接管；"
        "hold = 一直沿用大盘台最后一次发布的观点。",
    )
    publish_confirm: int = Field(
        default=2,
        ge=1,
        le=6,
        description="滞回：新方向要连续几轮一致才发布（每发布一版，所有持仓排队复核）。"
        "大盘台第一次接管（当前观点不是大盘台发的）不等，立即发布。",
    )
    publish_confirm_min: float = Field(
        default=30.0,
        ge=0.0,
        le=1440.0,
        description="滞回（按时间）：新方向从第一次出现到这一轮至少持续这么多分钟才发布，"
        "和 publish_confirm 同时满足。例外：连续一致的几轮里有一轮是急动 / 翻转加跑的，"
        "只看轮数（加跑那轮和下一轮定时轮一致就发）。15 分钟一轮时默认约要 3 轮（30 分钟）。",
    )
    publish_min_gap_min: float = Field(
        default=0.0,
        ge=0.0,
        le=1440.0,
        description="两次方向发布至少隔几分钟；0 = 只靠连续确认。",
    )
    republish_after_min: float = Field(
        default=0.0,
        ge=0.0,
        le=1440.0,
        description="方向没变时，当前生效的大盘台观点发布超过这么多分钟，就把这一轮的话和数字"
        "按同一方向重发一版（版本号 +1；每发一版持仓都会排队复核，翻转参照不变）。"
        "0 = 方向没变不发（现状）。",
    )
    daily_cost_limit_usd: float = Field(
        default=0.0,
        ge=0.0,
        le=1000.0,
        description="大盘台每个交易日（曼谷 0 点清零）最多花多少美元，单独记账，不占军师团日上限；"
        "0 = 不限。到了当天停跑。",
    )
    balance_floor_usd: float = Field(
        default=10.0,
        ge=0.0,
        le=10000.0,
        description="OpenRouter 余额低于这条线大盘台自停，把余额留给开会和 Jev；0 = 不看余额。",
    )
    max_input_age_min: float = Field(
        default=5.0,
        ge=0.0,
        le=120.0,
        description="不能有延后信息：实时类字段（滚动窗口、现价、资金费、多空比、持仓量）的 "
        "as_of 比"
        "运行时刻旧过这么多分钟就不给模型（写进缺失原因）；5 分钟统计的数再多给 5 分钟。0 = 不查。"
        "σ、基础率（已收盘 1h 线上的统计量）和恐慌贪婪（日值）不按这个拦。",
    )
    publish_intra: bool = Field(
        default=False,
        description="大盘也告诉各币会议 4 小时之内的行情：开着时，各币会议的大盘说明和"
        "面板「最新大盘判断」在 4 小时判断后面再带两段——大盘台最近一轮的"
        "「未来 1 小时概率」和代码按行情写的「短线现状」（池 15 分钟 / 1 小时等权涨跌和"
        "涨跌家数、BTC 15 分钟 / 1 小时、1 小时主动买卖占比）。短线现状按 intra_refresh_s "
        "在两轮之间刷新，数据旧过 max_input_age_min 就写「缺失」。不多调模型、不改 4 小时"
        "方向和发布节奏。false = 和以前一样。",
    )
    keeper_intra: bool = Field(
        default=False,
        description="管家复查快照也带同一段「4 小时之内」（current_market.within_4h）。和 "
        "publish_intra 分开：管家的判断门槛是在没有这项输入时定的，打开前先单独验证。",
    )
    intra_refresh_s: float = Field(
        default=60.0,
        ge=0.0,
        le=900.0,
        description="两轮大盘台之间多久刷新一次「短线现状」（秒）：用大盘翻转检查每分钟已经取到"
        "的 1 分钟收盘价重算池和 BTC 的涨跌，不另发请求、不调模型（大盘翻转检查没开时不刷新）。"
        "主动买卖占比只在大盘台每轮取。0 = 只在大盘台每轮更新。",
    )
    exclude_symbols: tuple[str, ...] = Field(
        default=("PAXGUSDT", "XAUTUSDT"),
        description="不进等权池的币（贵金属等，和大盘无关）。",
    )

    @field_validator("exclude_symbols", mode="before")
    @classmethod
    def _symbols(cls, v: Any) -> Any:
        if not isinstance(v, list | tuple):
            return v
        out: list[str] = []
        for x in v:
            name = str(x).strip().upper()
            name = name if name.endswith("USDT") else f"{name}USDT"
            if name not in out:
                out.append(name)
        return tuple(out)

    @model_validator(mode="after")
    def _sane(self) -> MarketDeskSection:
        if self.min_valid_samples > self.samples:
            raise ValueError("min_valid_samples 不能大于 samples")
        if self.offset_min >= self.cadence_min:
            raise ValueError("offset_min 必须小于 cadence_min")
        if self.max_age_min <= self.cadence_min:
            raise ValueError("max_age_min 必须大于 cadence_min，否则两轮之间就算不可用")
        return self

    def daily_calls(self) -> float:
        """按定时轮估的每天模型调用次数（含镜像测试，不含加跑）。"""
        runs = 24 * 60 / self.cadence_min
        return runs * self.samples * (1 + self.mirror_test_rate)


class SlowConfig(_Strict):
    llm: LlmConfig = LlmConfig()
    meeting: MeetingConfig = MeetingConfig()
    social: SocialConfig = SocialConfig()
    levels: LevelsConfig = LevelsConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    screener: ScreenerSection = ScreenerSection()
    budget: BudgetConfig = BudgetConfig()
    prices: PricesConfig = PricesConfig()
    market_desk: MarketDeskSection = MarketDeskSection()

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
            json_max_tokens=None if m.max_tokens is None else max(8192, m.max_tokens),
            retry_bad_steps=m.retry_bad_steps,
            retry_min_chars=m.retry_min_chars,
            speech_min_chars=m.speech_min_chars,
            report_min_chars=m.report_min_chars,
            max_retries=m.max_retries,
            reasoning_effort=m.reasoning_effort,
            price_per_mtok={"flash": (p.flash_in, p.flash_out), "pro": (p.pro_in, p.pro_out)},
            analyst_klines=m.analyst_klines,
            analysts_crypto=m.analysts_crypto,
            analysts_stock=m.analysts_stock,
            reddit=self.social.reddit,
            level_span_sigma=self.levels.span_sigma,
            pick_tier=m.pick_tier,
            convert_tier=m.convert_tier,
            bias_fix=m.bias_fix(),
            snapshot_interval=m.snapshot_interval,
            snapshot_label=m.snapshot_label,
            live_price_line=m.live_price_line,
            stale_on_arrival=m.stale_on_arrival,
            stale_on_arrival_action=m.stale_on_arrival_action,
            input_time_labels=m.input_time_labels,
            market_table_rolling=m.market_table_rolling,
            lessons_scoring=m.lessons_scoring,
        )

    def screener_config(
        self, pool_size: int, risk_max_spread_bps: float | None, round_trip_fee_bps: float
    ) -> ScreenerConfig:
        """``[screener]`` → 选币器参数。池大小来自 app.toml，价差默认和手续费跟硬上限走。"""
        s = self.screener
        return ScreenerConfig(
            pool_size=pool_size,
            min_quote_volume_usdt=s.min_quote_volume_usdt,
            # 硬上限也没写（风控员只按止损比例）时用选币器自己的默认
            max_spread_bps=s.max_spread_bps or risk_max_spread_bps or SCREENER_SPREAD_BPS,
            min_depth_usdt=s.min_depth_usdt,
            min_listed_days=s.min_listed_days,
            round_trip_fee_bps=round_trip_fee_bps,
            default_category_cap=s.default_category_cap,
            tiers=s.tiers.enabled,
            tier_head=s.tiers.head,
            tier_mid=s.tiers.mid,
            major_top_n=s.major_top_n,
            major_include=frozenset(x.strip().upper() for x in s.major_include),
            major_exclude=frozenset(x.strip().upper() for x in s.major_exclude),
            always_candidates=frozenset(s.tiers.always_candidates),
            allow_tradfi=s.allow_tradfi,
            exclude_untradeable=s.exclude_untradeable,
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

    def direct_meet_count(self) -> int:
        """Upper bound on coins met each round outside the pick slots (``always_candidates_meet``);
        the real count is lower when some of them miss the pool."""
        tiers = self.screener.tiers
        return len(tiers.always_candidates) if tiers.always_candidates_meet else 0

    def effective_pick_max(self, app_pick_max: int) -> int:
        """每轮最多出几张卡：``max_cards_per_round`` 与 ``card_pick_max`` 取小。"""
        n = self.schedule.max_cards_per_round
        return min(n, app_pick_max) if n else app_pick_max  # 不写 / 0 = 不另设上限

    def _slowdown(self) -> tuple[timedelta, timedelta] | None:
        """日花费到 80% 后的（卡有效期，开会间隔）；不放慢返回 None。"""
        s = self.schedule
        if s.over_budget != "slow" or s.slowdown_interval_min is None:
            return None
        every = timedelta(minutes=s.slowdown_interval_min)
        return every + timedelta(minutes=s.refresh_lead_min), every

    def commander_kwargs(self, profile: str = profiles.DEFAULT_PROFILE) -> dict[str, Any]:
        """传给 ``BattleCardCommander(...)`` 的节奏参数。``profile``：日上限关了（0）时，
        用这一档的默认日上限当「只提醒不停」的线。"""
        s = self.schedule
        spend_alert = None
        if not self.budget.daily_cost_limit_usd:
            default = profiles.defaults(profile, "slow.budget").get("daily_cost_limit_usd")
            spend_alert = float(default) if default else None
        return {
            "concurrency": s.concurrency,
            "ttl": timedelta(minutes=s.card_ttl_min),
            "symbol_min_interval": timedelta(minutes=s.symbol_min_interval_min),
            "expiry_lead": timedelta(minutes=s.refresh_lead_min),
            "symbol_timeout_s": s.symbol_timeout_s,
            "min_refresh_gap": timedelta(minutes=s.min_refresh_gap_min),
            "daily_cost_limit_usd": self.budget.daily_cost_limit_usd or None,  # 0 = 不限
            "spend_alert_usd": spend_alert,
            "events": EventRules(
                enabled=s.event_meeting_enabled,
                triggers=frozenset(s.event_triggers),
                symbol_gap=timedelta(minutes=s.event_symbol_gap_min) or EVENT_GAP_FLOOR,  # 0 = 不限
                max_per_day=s.event_max_per_day,
                move_atr=s.move_since_card_atr,
                entry_max_age=timedelta(minutes=s.entry_max_card_age_min),
                key_level_trigger=s.key_level_trigger,
                concurrency=s.event_concurrency or s.concurrency,  # 0 = 跟整轮一样
            ),
            "schedule_at": s.schedule_at,
            "slowdown": self._slowdown(),
            "failed_meeting_cost_usd": self.budget.failed_meeting_cost_usd,
            "tiers": TierRules(
                enabled=self.screener.tiers.enabled,
                mid_daily_pick=self.screener.tiers.mid_daily_pick,
                mid_size_max=SizeTier(self.screener.tiers.mid_size_max),
                always_meet=self.screener.tiers.always_candidates_meet,
            ),
            "fail_backoff": tuple(timedelta(minutes=m) for m in self.budget.fail_backoff_minutes),
            "latest_market_regime": self.meeting.latest_market_regime,
            "event_brief_refresh": self.meeting.event_brief_refresh,
            "reuse_extend_expiry": s.reuse_extend_expiry,
            "regime_time_label": self.meeting.regime_time_label,
            "regime_max_age": timedelta(minutes=self.meeting.regime_max_age_min),
            "pool_scope_label": self.meeting.pool_scope_label,
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
    data = upgrade_legacy_schedule(data)
    try:
        return SlowConfig.model_validate(with_profile(data, profile))
    except ValidationError as e:
        lines = [f"{path} 不合格，拒绝启动："]
        for err in e.errors():
            loc = ".".join(str(p) for p in err["loc"]) or "(整体)"
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from e


def upgrade_legacy_schedule(data: dict[str, Any]) -> dict[str, Any]:
    """slow.toml [schedule] 里按小时的旧键（card_ttl_hours / symbol_min_interval_hours）
    自动换算成分钟键并告警；两个键都写了以新键为准。返回新 dict，不改 ``data``。"""
    section = data.get("schedule")
    if not isinstance(section, dict):
        return data
    renamed = {
        "card_ttl_hours": "card_ttl_min",
        "symbol_min_interval_hours": "symbol_min_interval_min",
    }
    if not any(old in section for old in renamed):
        return data
    new = {k: v for k, v in section.items() if k not in renamed}
    for old, key in renamed.items():
        if old in section:
            hours = section[old]
            if key not in section and isinstance(hours, int | float):
                new[key] = hours * 60
            logger.warning(
                "slow.toml [schedule] 的 %s 已改名 %s（分钟），已自动 ×60 换算", old, key
            )
    return {**data, "schedule": new}


def market_desk_conflict(slow: SlowConfig, regime_flip_target: str) -> str | None:
    """跨文件校验：大盘台接管（replace）时只能有一个发布者，翻转后不能再让 Flash 独立刷新大盘
    （CONFLICTS #2）；翻转目标是大盘台时大盘台必须开着。有冲突返回原因，否则 None。
    启动、面板保存 / 重启前、热生效都按这一条拦。"""
    if slow.market_desk.mode == "replace" and regime_flip_target == "flash_market_only":
        return (
            'slow.toml [market_desk] mode = "replace" 时，app.toml [fast_loop] '
            "regime_flip_target 不能是 flash_market_only（会有两个大盘发布者来回改方向）；"
            '改成 "desk"（翻转时让大盘台重跑）或 "off"（只记录）'
        )
    if slow.market_desk.mode == "off" and regime_flip_target == "desk":
        return (
            'app.toml [fast_loop] regime_flip_target = "desk" 要求 slow.toml [market_desk] '
            'mode 不是 off（大盘台没开，翻转时没人更新大盘）；改回 "flash_market_only" 或 "off"'
        )
    return None


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
