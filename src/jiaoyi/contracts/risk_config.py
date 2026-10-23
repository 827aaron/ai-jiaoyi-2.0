"""硬上限（你定，AI 改不了）和运行配置。

规则（设计文档 §四、§十一）：
- 每个字段都是**必填**，模型里不给默认值。配置缺项、多出未知字段、数值不合理
  （例如负数、百分比超过 100、杠杆超过币安最高 125 倍），程序拒绝启动。
  硬上限的数值由人定，不另设保守范围。
  保守默认值写在 ``config/risk.example.toml``，第一次用时复制成 ``config/risk.toml``
  再按需改；比推荐值冒险只提示（``recommended_caps`` / ``RiskConfig.cautions``）。
- 下层只能更紧：作战卡的范围、拍板人的档位都不能超过这里的数。

读文件和报错见 ``jiaoyi.core.config``。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, get_args

from pydantic import Field, model_validator

from jiaoyi.contracts.common import Category, Contract, RunMode

# 两套各选各的档位，各档的跨模块默认值见 ``jiaoyi.core.profiles``
MeetingInterval = Literal["15m", "30m", "1h", "2h", "6h", "12h", "24h"]  # 军师团开会间隔
HoldProfile = Literal["30m", "1h", "2h", "4h"]  # 单笔最长持仓
MEETING_PROFILES: tuple[str, ...] = get_args(MeetingInterval)
HOLD_PROFILES: tuple[str, ...] = get_args(HoldProfile)
PAPER_ONLY_HOLDS = frozenset({"30m"})  # 最长持仓 30m：止损贴近手续费，只能 paper


BINANCE_MAX_LEVERAGE = 125
"""币安 U 本位合约的物理最高杠杆；超过它的配置不可能生效，拒绝并写明原因。"""


class RiskConfig(Contract):
    """硬上限。Aaron 2026-10-23 定：只给推荐，不设限制，你填的就是上限。程序只拦数学上
    不成立的值（≤0、百分比超过 100、杠杆超过币安最高的 125 倍）；具体币种能开多少由币安
    按合约决定（下单前回读校验）。
    比推荐值冒险时提示但不拒绝（见 ``cautions``）。
    """

    risk_per_trade_pct: float = Field(
        gt=0.0,
        le=100.0,
        description="每笔最多亏总资金的百分比。推荐 1（按资金算，见 recommended_caps）。",
    )
    max_leverage: int = Field(
        ge=1, description="最大杠杆（具体币种能开多少以币安回读为准）。推荐 5。"
    )
    max_total_exposure_x: float = Field(
        gt=0.0, description="所有持仓名义价值合计 / 总资产的上限。推荐 3。"
    )
    category_exposure_x: dict[Category, float] = Field(
        description="每个相关组的敞口上限（总资产的倍数），不能超过总敞口上限。"
    )
    daily_loss_halt_pct: float = Field(
        gt=0.0, le=100.0, description="当日亏损达到这个百分比后只允许平仓。推荐为每笔风险 × 3。"
    )
    max_notional_per_trade_x: float = Field(
        default=2.0,
        gt=0.0,
        description="单笔名义价值上限 = 账户权益 × 这个倍数，随资金变。推荐 2。",
    )
    max_notional_per_trade_usdt: float | None = Field(
        default=None,
        gt=0.0,
        description="可选：单笔名义价值的绝对硬顶，USDT。不写就只按权益倍数。",
    )
    max_open_positions: int = Field(ge=1)
    lifeboat_stop_multiplier: float = Field(
        gt=1.0,
        description="保命止损距离 = 正常止损距离 × 这个倍数（必须比正常止损宽）。推荐 1.5。",
    )
    max_spread_bps: float = Field(gt=0.0, description="价差超过就不开仓。")
    max_slippage_bps: float = Field(gt=0.0)
    taker_fee_bps: float = Field(ge=0.0, description="单边吃单费率，用于手续费门槛。")
    min_tp_fee_multiple: float = Field(
        gt=0.0, description="止盈距离至少是来回手续费的几倍，不够就拒单。推荐 2。"
    )
    day_reset_tz: Literal["Asia/Bangkok", "UTC"] = Field(
        description="“今日盈亏”和日亏熔断按哪个时区 0 点清零。"
    )

    @model_validator(mode="after")
    def _check(self) -> RiskConfig:
        if self.max_leverage > BINANCE_MAX_LEVERAGE:
            raise ValueError(
                f"max_leverage {self.max_leverage} 不合理：币安 U 本位合约最高 "
                f"{BINANCE_MAX_LEVERAGE} 倍（具体币种更低，下单前按交易所回读）"
            )
        missing = set(Category) - set(self.category_exposure_x)
        if missing:
            raise ValueError(f"category_exposure_x 缺少分组：{sorted(missing)}")
        for cat, cap in self.category_exposure_x.items():
            if not 0.0 <= cap <= self.max_total_exposure_x:
                total = self.max_total_exposure_x
                raise ValueError(f"分组 {cat} 的上限 {cap} 必须在 0 到总敞口上限 {total} 之间")
        return self

    def notional_cap(self, equity_usdt: float | None) -> float:
        """单笔名义价值上限：权益 × ``max_notional_per_trade_x``，写了绝对硬顶再取小。

        不知道权益（慢环、日志验收）时只剩绝对硬顶；也没写就是 ``inf``，由保镖按实时权益封顶。
        """
        caps = (
            []
            if not equity_usdt or equity_usdt <= 0
            else [equity_usdt * self.max_notional_per_trade_x]
        )
        if self.max_notional_per_trade_usdt is not None:
            caps.append(self.max_notional_per_trade_usdt)
        return min(caps) if caps else math.inf

    def cautions(self, equity_usdt: float | None = None) -> list[str]:
        """比按资金算出的推荐值更冒险的项（只提示，不拒绝）。资金不知道时按 1000U 算。"""
        out = []
        for name, rec in recommended_caps(equity_usdt).items():
            value = getattr(self, name)
            if value is None:
                continue
            if (value > rec.value) if rec.higher_is_riskier else (value < rec.value):
                out.append(f"{name} = {value:g}（推荐 {rec.value:g}）")
        worst = self.risk_per_trade_pct * self.lifeboat_stop_multiplier
        if worst > self.daily_loss_halt_pct:
            out.append(
                f"daily_loss_halt_pct {self.daily_loss_halt_pct:g}% 小于一笔打到保命止损的亏损"
                f" {worst:g}%：满仓位的单子会被保镖全部拒掉"
            )
        return out


DEFAULT_EQUITY_USDT = 1000.0
"""不知道账户资金时（终端 check-config、面板连不上交易所）推荐值按这个算。"""


@dataclass(frozen=True)
class Recommendation:
    value: float
    why: str
    higher_is_riskier: bool = True


def recommended_caps(equity_usdt: float | None = None) -> dict[str, Recommendation]:
    """按账户资金算硬上限的推荐值（Aaron 2026-10-23：只给推荐，不设限制）。

    公式（docs/M0-setup.md §2 有同样的说明）：

    - 每笔风险：2 万 U 以下 1%，2–10 万 0.75%，10 万以上 0.5%（大资金单笔金额本身就大）。
    - 日亏熔断：每笔风险 × 3，约等于连亏 2 笔满仓（按保命止损 1.5 倍算）后停手。
    - 杠杆：1000U 以下 10 倍（不然单笔凑不够币安最小下单额），否则 5 倍。
    - 总敞口：3 倍资金。
    - 同时持仓：资金 ÷ 200U，取 2–5 笔。
    - 单笔名义上限：权益 × 2 倍（配置本身就按倍数写，随资金变）。
    - 保命止损倍数 1.5、止盈至少覆盖手续费 2 倍：和资金无关。
    """
    eq = equity_usdt if equity_usdt and equity_usdt > 0 else DEFAULT_EQUITY_USDT
    basis = f"按 {eq:,.0f}U 算"
    risk = 1.0 if eq < 20_000 else 0.75 if eq < 100_000 else 0.5
    exposure = 3.0
    return {
        "risk_per_trade_pct": Recommendation(risk, basis),
        "daily_loss_halt_pct": Recommendation(risk * 3, f"每笔风险 × 3，{basis}"),
        "max_leverage": Recommendation(10 if eq < 1000 else 5, basis),
        "max_total_exposure_x": Recommendation(exposure, "和资金无关"),
        "max_open_positions": Recommendation(min(5, max(2, int(eq // 200))), basis),
        "max_notional_per_trade_x": Recommendation(2.0, "和资金无关，金额随权益变"),
        "lifeboat_stop_multiplier": Recommendation(1.5, "和资金无关"),
        "min_tp_fee_multiple": Recommendation(2.0, "和资金无关", higher_is_riskier=False),
    }


class FastLoopConfig(Contract):
    """快环的门槛和节奏（哨兵、拍板人、保镖的档位换算、持仓管家、巡逻兵）。

    这些是策略参数，不是硬上限：只能在硬上限以内起作用，保镖仍按 ``RiskConfig`` 封顶。
    每项都有默认值（契约规则：新加字段必须给默认值，旧的 app.toml 照样能读），
    但同样有合理范围，超范围拒绝启动。键名在 ``config/app.example.toml`` 的 ``[fast_loop]``。
    """

    # ---- 拍板人 / 决策模型
    jev_provider: Literal["typesafe", "openrouter"] = Field(
        default="typesafe",
        description="Jev 走哪条通道：typesafe = 直连 TypeSafe（key 用 JEV_API_KEY）；"
        "openrouter = 经 OpenRouter 转发（key 用 OPENROUTER_API_KEY，jev_model 写 OpenRouter 的"
        "模型名，例如 typesafe/jev-1.14）。问题、门槛、日志格式两边一样。",
    )
    jev_model: str | None = Field(
        default=None,
        min_length=1,
        description="锁定的 Jev 模型版本（具体版本号，不许用 latest 这类浮动别名），门槛按它校准。"
        "不填时不能接真 Jev（有 key 也拒绝），只能用规则版或不开仓。",
    )
    rule_gate_without_jev: bool = Field(
        default=False,
        description="没有 Jev（没 key 或没锁版本）时是否用规则版拍板人。默认关：没有 Jev 就不开仓。"
        "只允许 paper 模式打开；Jev 运行中出错永远不会切到规则版。",
    )
    jev_usd_per_mtok: float = Field(
        default=0.042, ge=0.0, le=10.0, description="每百万输入 token 美元。"
    )
    jev_max_consecutive_errors: int = Field(
        default=5, ge=1, le=100, description="连续出错这么多次后暂停调用一段时间；成功一次就清零。"
    )
    jev_error_cooldown_s: int = Field(default=60, ge=5, le=3600)
    min_p_genuine: float = Field(default=0.6, ge=0.5, le=0.99, description="“真信号”概率门槛。")
    min_p_take: float = Field(default=0.6, ge=0.5, le=0.99, description="“做”概率门槛。")
    min_p_direction: float = Field(
        default=0.45, ge=0.34, le=0.99, description="信号方向（up/down）的概率门槛。"
    )
    max_p_toxic: float = Field(default=0.5, ge=0.05, le=0.95, description="订单流有毒概率上限。")
    # ---- 保镖的档位换算
    signal_max_age_s: float = Field(
        default=5.0, ge=0.5, le=60.0, description="信号从触发到保镖审核超过这么久就作废。"
    )
    size_fraction_s: float = Field(
        default=0.5, gt=0.0, le=1.0, description="S 档用每笔风险的几成。"
    )
    size_fraction_m: float = Field(default=0.75, gt=0.0, le=1.0, description="M 档；L 档固定 1.0。")
    atr_period_min: Literal[5, 15, 30, 60] = Field(
        default=15,
        description="止损用的 ATR 周期（分钟，ATR 取 14 根）。作战卡的止损倍数按这个尺度给，"
        "改了要同时让军师那边按新周期出卡。由 1 分钟 K 线聚合，启动回填 16 根这么长的数据。",
    )
    min_stop_atr: float = Field(
        default=0.5,
        ge=0.1,
        le=5.0,
        description="作战卡止损 ATR 倍数的下限，低于它整张卡这个标的不做（止损太近会被噪音扫掉）。",
    )
    stop_level_buffer_atr: float = Field(
        default=0.1, ge=0.0, le=1.0, description="止损放在关键位外侧多少个 ATR。"
    )
    max_stop_atr: float = Field(
        default=4.0,
        ge=0.5,
        le=10.0,
        description="作战卡止损 ATR 倍数的上限，超过整张卡这个标的不做。",
    )
    min_tp_r: float = Field(default=1.0, ge=0.5, le=5.0, description="作战卡止盈倍数下限。")
    max_tp_r: float = Field(default=6.0, ge=1.0, le=20.0, description="作战卡止盈倍数上限。")
    time_limit_s: int = Field(default=7200, ge=60, le=86400, description="单笔最长持仓时间。")
    min_stop_fee_multiple: float = Field(
        default=0.0,
        ge=0.0,
        le=20.0,
        description="止损距离至少是来回手续费的几倍（0 = 不查）；持仓越短要求越高，"
        "档位给推荐值，低于它这张卡的这个标的不开仓。",
    )
    # ---- 持仓管家
    keeper_close_p: float = Field(default=0.6, ge=0.5, le=0.99, description="平仓概率过线。")
    keeper_close_p_strong: float = Field(
        default=0.85, ge=0.5, le=1.0, description="概率明显过线时不用等第二次确认。"
    )
    keeper_confirmations: int = Field(default=2, ge=1, le=5, description="连续几次一致才平仓。")
    # ---- 巡逻兵
    patrol_shock_pct_1m: float = Field(
        default=2.0, ge=0.2, le=20.0, description="1 分钟内对持仓不利的波动达到这个百分比就全平。"
    )
    patrol_alert_pct_1m: float = Field(
        default=1.0, ge=0.1, le=20.0, description="达到这个百分比就让管家立刻复查。"
    )
    # ---- 哨兵
    breakout_vol_ratio: float = Field(default=3.0, ge=1.0, le=50.0)
    surge_vol_ratio: float = Field(default=4.0, ge=1.0, le=50.0)
    surge_taker_ratio: float = Field(default=0.7, ge=0.5, le=1.0)
    liq_min_usdt_1m: float = Field(
        default=100_000.0, ge=0.0, le=1e9, description="1 分钟爆仓额下限。"
    )
    liq_ratio: float = Field(
        default=5.0, ge=1.0, le=100.0, description="1 分钟爆仓额 / 30 分钟均值。"
    )
    liq_drop_pct_5m: float = Field(
        default=1.0, ge=0.1, le=20.0, description="爆仓砸出的 5 分钟跌幅。"
    )
    crowded_funding_rate: float = Field(
        default=0.0005,
        ge=0.0001,
        le=0.01,
        description="资金费率绝对值达到这个算拥挤（0.0005=0.05%）。",
    )
    session_window_min: int = Field(
        default=5, ge=1, le=60, description="开盘 / 资金费结算前后几分钟。"
    )
    session_vol_ratio: float = Field(default=2.0, ge=1.0, le=50.0)

    @model_validator(mode="after")
    def _check(self) -> FastLoopConfig:
        if self.jev_model is not None and "latest" in self.jev_model.lower():
            raise ValueError(
                "jev_model 必须是具体版本号，不能用 latest 这类浮动别名（会让门槛悄悄失效）"
            )
        if self.size_fraction_s > self.size_fraction_m:
            raise ValueError("size_fraction_s 不能大于 size_fraction_m")
        if self.keeper_close_p_strong < self.keeper_close_p:
            raise ValueError("keeper_close_p_strong 不能小于 keeper_close_p")
        if self.min_stop_atr >= self.max_stop_atr:
            raise ValueError("min_stop_atr 必须小于 max_stop_atr")
        if self.min_tp_r > self.max_tp_r:
            raise ValueError("min_tp_r 不能大于 max_tp_r")
        if self.patrol_alert_pct_1m > self.patrol_shock_pct_1m:
            raise ValueError("patrol_alert_pct_1m 不能大于 patrol_shock_pct_1m")
        return self


class PredictorConfig(Contract):
    """预测员（Kronos）怎么跑。开关仍是 ``AppConfig.predictor_enabled``。键在 ``[predictor]``。"""

    mode: Literal["on_demand", "all_symbols"] = Field(
        default="on_demand",
        description="on_demand = 只对作战卡里的币和有持仓的币在 5 分钟线收盘时预测，哨兵出信号而"
        "没有新鲜预测时临时补算一次；all_symbols = 候选池里所有订阅的币都算（CPU 吃得多）。",
    )
    size_policy: Literal["fixed", "adaptive"] = Field(
        default="fixed",
        description="fixed = 一直用 model_size；adaptive = 按排队深度在 base/small/mini 间切换。",
    )
    model_size: Literal["mini", "small", "base"] = Field(
        default="small", description="fixed 时用的模型。Mac mini 实测 small 约 3 秒/币。"
    )
    adaptive_base_max_queue: int = Field(
        default=0,
        ge=0,
        le=20,
        description="adaptive：排队的推理不超过这么多时用 base（更准更慢）。",
    )
    adaptive_mini_min_queue: int = Field(
        default=3,
        ge=1,
        le=50,
        description="adaptive：排队的推理达到这么多时用 mini（更快）；中间用 small。",
    )
    backfill_bars: int = Field(
        default=400, ge=64, le=1500, description="启动和新币入池时回填多少根 5 分钟 K 线。"
    )

    @model_validator(mode="after")
    def _check(self) -> PredictorConfig:
        if self.adaptive_base_max_queue >= self.adaptive_mini_min_queue:
            raise ValueError("adaptive_base_max_queue 必须小于 adaptive_mini_min_queue")
        return self


class AppConfig(Contract):
    """运行配置：模式、开关、节奏。除 ``fast_loop`` 外全部必填，超范围拒绝启动。"""

    mode: RunMode
    meeting_interval: MeetingInterval = Field(
        default="1h",
        description="开会间隔档：军师团每隔多久开一轮会（卡有效期 = 间隔 + 20 分钟）。"
        "只填配置文件里没写的策略参数（见 core/profiles.py）。",
    )
    hold: HoldProfile = Field(
        default="2h",
        description="最长持仓档：单笔最长拿多久；止损 / 止盈 / ATR 周期 / 拍板门槛 / 预测长度"
        "跟它走。30m 只能 paper。",
    )
    predictor_enabled: bool = Field(description="预测员（Kronos）开关。")
    signal_dedup_s: int = Field(ge=5, le=600, description="同一去重键的冷却时间。")
    jev_timeout_ms: int = Field(ge=100, le=5000, description="超时即放弃本次开仓。")
    keeper_interval_s: int = Field(ge=5, le=120)
    reconcile_interval_s: int = Field(ge=10, le=300)
    slow_loop_interval_min: int = Field(ge=15, le=1440)
    pool_size: int = Field(ge=5, le=100, description="候选池大小，设计值 30–50。")
    card_pick_max: int = Field(ge=1, le=50, description="军师每次最多挑几个，设计值 10–20。")
    data_dir: str = Field(min_length=1, description="决策日志、模块状态等写在这里。")
    sim_environment: Literal["demo", "legacy_testnet"] = Field(
        default="demo",
        description="paper / testnet 连哪个币安模拟环境：demo = Demo Trading，"
        "legacy_testnet = 旧测试网（已停用，仅回退）。hummingbot 侧补丁要与此一致。",
    )
    reconciler_close_unknown_positions: bool = Field(
        default=False,
        description="对账员遇到不在账本里的币安仓位 / hummingbot 执行器时直接平掉。"
        "默认只告警（可能是你手动开的）；要打开请在 M4 小额实盘前自己决定。",
    )
    predictor: PredictorConfig = Field(
        default_factory=PredictorConfig, description="预测员运行方式，见 ``[predictor]``。"
    )
    fast_loop: FastLoopConfig = Field(
        default_factory=FastLoopConfig, description="快环门槛；不写就用默认值（见示例）。"
    )

    @property
    def profile(self) -> str:
        """旧名：开会间隔档（原「持仓档位」）。"""
        return self.meeting_interval

    @model_validator(mode="after")
    def _check(self) -> AppConfig:
        if self.card_pick_max > self.pool_size:
            raise ValueError("card_pick_max 不能大于 pool_size")
        if self.hold in PAPER_ONLY_HOLDS and self.mode != RunMode.PAPER:
            raise ValueError(
                f"最长持仓档 {self.hold} 只能在 paper 模式用（费用占止损太高），"
                "testnet / live 请用 1h 或 2h 或 4h"
            )
        if self.fast_loop.rule_gate_without_jev and self.mode != RunMode.PAPER:
            raise ValueError(
                "rule_gate_without_jev 只能在 paper 模式打开，testnet / live 必须用 Jev"
            )
        return self
