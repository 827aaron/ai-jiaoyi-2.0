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
HoldProfile = Literal["30m", "1h", "2h", "4h", "8h", "12h", "24h"]  # 单笔最长持仓
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
    max_spread_bps: float | None = Field(
        default=None,
        gt=0.0,
        description="价差超过就不开仓（万分之几）。不写 = 只按止损的比例"
        "（fast_loop.max_spread_stop_frac）；写了就和比例那条取小。",
    )
    max_slippage_bps: float | None = Field(
        default=None,
        gt=0.0,
        description="拍板期间价格往不利方向跑超过这么多就不开（万分之几）。不写 = 只按止损的比例"
        "（fast_loop.max_slippage_stop_frac）；写了就和比例那条取小。",
    )
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
        "只在 paper 生效（testnet / live 写了也当关）；Jev 运行中出错永远不会切到规则版。",
    )
    jev_usd_per_mtok: float = Field(
        default=0.042, ge=0.0, le=10.0, description="每百万输入 token 美元。"
    )
    jev_max_consecutive_errors: int = Field(
        default=5, ge=1, le=100, description="连续出错这么多次后暂停调用一段时间；成功一次就清零。"
    )
    jev_error_cooldown_s: int = Field(default=60, ge=5, le=3600)
    # 概率门槛只给推荐（档位默认），0–1 之间都收（Aaron：用户设置就是上限，只有不合理才失败）
    min_p_genuine: float = Field(default=0.6, ge=0.0, le=1.0, description="“真信号”概率门槛。")
    min_p_take: float = Field(
        default=0.55,
        ge=0.0,
        le=1.0,
        description="拍板人：做的把握至少多少。一道题问法下这是唯一的放行门槛"
        "（Aaron 10-24 定 0.55），预测员的加减分加在这个概率上。",
    )
    predictor_nudge_mode: Literal["card_agree", "symmetric"] = Field(
        default="card_agree",
        description="已停用：预测员改成官方的排名用法（[predictor] rank_band），写了也不生效。",
    )
    gate_questions: Literal["one", "six"] = Field(
        default="one",
        description="拍板人怎么问：one = 只问一道「做不做」（按之后 30 分钟、扣手续费后判断，"
        "方向、真假、盘口有毒都并进这一道），只看 min_p_take；six = 旧的 6 道题，"
        "真信号 / 方向 / 有毒三个门槛也生效（留着回放对照用）。止损档、仓位档两题两种都问。",
    )
    min_p_direction: float = Field(
        default=0.45, ge=0.0, le=1.0, description="信号方向（up/down）的概率门槛。"
    )
    max_p_toxic: float = Field(default=0.5, ge=0.0, le=1.0, description="订单流有毒概率上限。")
    predictor_weight: float = Field(
        default=0.2,
        ge=0.0,
        le=1.0,
        description="预测员（Kronos）意见占几成：拿“做”概率和门槛比之前，按预测员的看法加一点："
        "同一时刻所有币按预测涨幅排名，做多信号排在前段、做空信号排在后段（[predictor] rank_band）"
        "就加 这个数 × 0.5（0.2 时加 0.10），其余不加也不扣。"
        "只动“做”这一道门，预测员不进拍板人的快照。0 = 不听；没有有效预测时不挪。",
    )
    gate_record_only: bool | None = Field(
        default=None,
        description="拍板只记不拦：拍板人的概率照常问、照常记，但没过上面这几个门槛也放行"
        "（订单流有毒概率 ≥ 0.8 仍然拦），用来在 paper 攒样本校准门槛。"
        "不写 = 关（拍板一票否决）；只在 paper 生效，testnet / live 写 true 当关（启动时警告）。",
    )
    # ---- 保镖的档位换算
    max_slippage_stop_frac: float = Field(
        default=0.25,
        gt=0.0,
        le=1.0,
        description="不利滑点上限 = 这笔止损距离 × 这个比例（推荐 1/4）。",
    )
    max_spread_stop_frac: float = Field(
        default=0.10,
        gt=0.0,
        le=1.0,
        description="价差上限 = 这笔止损距离 × 这个比例（推荐 1/10）。",
    )
    card_caps_binding: bool = Field(
        default=False,
        description="作战卡上的名义价值 / 杠杆范围当硬约束。默认关：只当军师建议记下来，"
        "仓位按风险金额和你的硬上限算，杠杆用 max_leverage。",
    )
    card_range_clamp: bool = Field(
        default=True,
        description="作战卡的止损 ATR / 止盈 R 范围超出档位范围时取交集，交集为空才不做；"
        "关掉 = 一端越界整个币不做。",
    )
    max_notional_book_frac: float = Field(
        default=0.5,
        ge=0.0,
        le=5.0,
        description="单笔名义 ≤ 吃单一侧前 5 档挂单额 × 这个比例，超了缩单不拒（0 = 不看盘口）。",
    )
    margin_safety_pct: float = Field(
        default=5.0,
        ge=0.0,
        le=50.0,
        description="开仓要的保证金（名义 ÷ 杠杆 + 开仓手续费）最多用到可用保证金的 (100 − 这个)%，"
        "超了缩单不拒。留的余量给下单前的价格变动和币安市价单的开仓损失，"
        "否则 hummingbot 会报「Not enough budget」不下单。",
    )
    limit_take_profit: bool = Field(
        default=True,
        description="paper：开仓成交后挂只减仓限价止盈（挂单费 2bp，碰到价位就成交）。改了要重启。",
    )
    limit_take_profit_live: bool = Field(
        default=False,
        description="testnet / live 也用限价止盈。默认关，Demo 验收后再开。改了要重启。",
    )
    tp_limit_fallback_s: float = Field(
        default=10.0,
        ge=1.0,
        le=300.0,
        description="限价止盈：价格穿过止盈价（见 tp_limit_fallback_through_bps）这么多秒仍没平掉，"
        "改市价平。",
    )
    tp_limit_fallback_through_bps: float = Field(
        default=5.0,
        ge=0.0,
        le=100.0,
        description="限价止盈：价格要越过止盈价这么多 bp（0.01%）才开始算「没成交」。只碰到止盈价时"
        "限价单还在排队，不算；穿过去还没成交才改市价。0 = 碰到就算（旧行为）。",
    )
    signal_max_age_s: float = Field(
        default=5.0, ge=0.5, le=60.0, description="信号从触发到保镖审核超过这么久就作废。"
    )
    size_fraction_s: float = Field(
        default=0.5, gt=0.0, le=1.0, description="S 档用每笔风险的几成。"
    )
    size_fraction_m: float = Field(default=0.75, gt=0.0, le=1.0, description="M 档；L 档固定 1.0。")
    atr_period_min: Literal[5, 15, 30, 60, 240] = Field(
        default=15,
        description="止损用的 ATR 周期（分钟，ATR 取 14 根）。作战卡的止损倍数按这个尺度给，"
        "改了要同时让军师那边按新周期出卡。由 1 分钟 K 线聚合，启动回填 16 根这么长的数据。",
    )
    atr_long_period_min: Literal[0, 15, 30, 60, 240] = Field(
        default=0,
        description="长周期 ATR（分钟，也取 14 根）。写了止损 ATR 就用 √(短周期 ATR × 长周期 ATR)："
        "短的跟得上眼前波动，长的不被一两根安静 K 线带窄。"
        "0 = 只用 atr_period_min 那一个（不开平方）。"
        "必须比 atr_period_min 长；长周期还没攒够时先只用短周期。",
    )
    session_factor: bool = Field(
        default=False,
        description="止损 ATR 再乘曼谷时段系数（按开仓时的曼谷小时、工作日 / 周末查表）：美股开盘"
        "前后波动大放宽，周末和亚洲早盘收窄。",
    )
    session_factor_weekday: list[float] = Field(
        default_factory=lambda: [1.0] * 24,
        description="工作日曼谷 0–23 点各一个系数（24 个）。",
    )
    session_factor_weekend: list[float] = Field(
        default_factory=lambda: [1.0] * 24,
        description="周六、周日（曼谷日期）0–23 点各一个系数（24 个）。",
    )
    atr_floor_7d: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="止损 ATR 下限系数：短周期 ATR 先取 max(当前, 7 天中位数 × 这个数)。"
        "0 = 关掉（默认；用了长短周期开平方后下限只修一半，周末还偏宽）。",
    )
    min_stop_atr: float = Field(
        default=0.5,
        ge=0.1,
        le=5.0,
        description="作战卡止损 ATR 倍数的下限，低于它整张卡这个标的不做（止损太近会被噪音扫掉）。",
    )
    stop_level_snap: bool = Field(
        default=False,
        description="止损挪到作战卡关键位外侧（再加 stop_level_buffer_atr）。默认关：数据里价格碰到"
        "关键位后 93% 会穿过 0.1 ATR，和随手画的线一样，挪过去只是让止损更宽。",
    )
    stop_level_buffer_atr: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description="止损放在关键位外侧多少个 ATR（只在 stop_level_snap 开时用）。",
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
    us_hours_gate: bool = Field(
        default=True,
        description="美股 / 加密股合约只在美股常规盘的开仓时段开新仓（见下面两项）；金属不受限。"
        "已有持仓不强平，止损和持仓管家照常。",
    )
    us_open_delay_min: int = Field(
        default=15, ge=0, le=120, description="美股开盘后多少分钟才开始开新仓。"
    )
    us_close_lead_min: int = Field(
        default=15, ge=0, le=120, description="美股收盘前多少分钟停止开新仓。"
    )
    funding_against_max_bp: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="开仓方向每次要付的资金费超过这么多 bp 就不开（0 = 不查）；"
        "只对持仓可能跨资金费结算的长持仓档给推荐值。",
    )
    min_stop_fee_multiple: float = Field(
        default=0.0,
        ge=0.0,
        le=20.0,
        description="止损距离至少是来回手续费的几倍（0 = 不查）；持仓越短要求越高，"
        "档位给推荐值，低于它时按 stop_floor_widen 拉宽止损或不开仓。",
    )
    stop_floor_widen: bool = Field(
        default=True,
        description="止损离手续费太近（不到 min_stop_fee_multiple 倍来回手续费）时："
        "开 = 把止损拉宽到这条下限，仓位按新止损重算（每笔亏损金额不变，所以张数变少），"
        "止盈也按新止损的 R 重算；"
        "关 = 不开这一单。Aaron 10-24：默认开，所有模式。",
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
    patrol_shock_stop_mult: float = Field(
        default=1.0,
        ge=0.0,
        le=5.0,
        description="冲击线 = 这个 × 这笔止损距离（%）。没有止损价或写 0 时用上面的百分比兜底。",
    )
    patrol_alert_stop_mult: float = Field(
        default=0.5,
        ge=0.0,
        le=5.0,
        description="报警线 = 这个 × 这笔止损距离（%）。没有止损价或写 0 时用上面的百分比兜底。",
    )
    # ---- 哨兵
    volume_surge_live: bool = Field(
        default=True,
        description="testnet / live 也出量能突增信号（默认开，Aaron 2026-10-24）。"
        "关掉后 testnet / live 不出；paper 不管这个开关照出（攒校准样本）。",
    )
    session_cme_open: bool = Field(
        default=False,
        description="时段与事件多看一个「CME 周日开盘」（芝加哥周日 17:00，曼谷周一 5:00 / 6:00）。"
        "档位推荐 30m / 1h / 2h 开（时段研究 10-24：只有这三档比随便进好），其它档关。",
    )
    session_cme_open_live: bool = Field(
        default=False,
        description="CME 周日开盘在 testnet / live 也出（默认关：证据还不够，只在模拟盘攒样本）。",
    )
    breakout_vol_ratio: float = Field(default=3.0, ge=1.0, le=50.0)
    surge_vol_ratio: float = Field(
        default=5.0,
        ge=1.0,
        le=50.0,
        description="量能突增：1 分钟成交额至少是平时几倍（Aaron 10-24 定 5 倍）。",
    )
    surge_taker_ratio: float = Field(default=0.7, ge=0.5, le=1.0)
    entry_zone_only: bool = Field(
        default=True,
        description="只在卡的进场区间内开仓（Aaron 10-24「让快环尊重卡的进场区间」）：卡上这个币"
        "写了进场区间、触发价在区间外就不开，连拍板人都不问；往交易方向（做多往上、做空往下）"
        "最多放宽 0.1R（R ≈ 卡上止损倍数中值 × 止损 ATR）。卡上没写区间照旧不限。",
    )
    entry_limit_pullback: bool = Field(
        default=False,
        description="进场用挂单等回踩（默认关，关 = 市价进）：保镖按触发价往有利方向让一段"
        "（止损距离的几成，跟档位走）挂只挂单的限价单，等够时间没成交就撤（8h 以上档改市价追）。"
        "成交后才挂保命止损。",
    )
    entry_limit_pullback_live: bool = Field(
        default=False, description="testnet / live 也用挂单等回踩（默认关，先在模拟盘看）。"
    )
    entry_limit_offset_stop: float = Field(
        default=0.25,
        ge=0.0,
        le=1.0,
        description="挂单比触发价让多少，按止损距离的几成算。档位推荐 30m 0.6、1h 0.4、"
        "2h / 4h 0.25、8h 以上 0.1（量能突增研究 v3）。",
    )
    entry_limit_wait_min: int = Field(
        default=60, ge=1, le=240, description="挂单最多等几分钟，没成交就撤（或市价追）。"
    )
    entry_limit_chase: bool = Field(
        default=False,
        description="挂单等到时没成交改市价追进。档位推荐 8h 以上开，4h 以下关（撤单不开）。",
    )
    breakeven_on: bool = Field(
        default=False,
        description="保本：赚到 breakeven_trigger_r 倍止损距离后，最坏按保本价（扣完手续费）平。"
        "默认关；开了只对之后新开的仓生效。",
    )
    breakeven_trigger_r: float = Field(
        default=0.8, ge=0.1, le=10.0, description="赚到几倍止损距离（R）启动保本。"
    )
    trailing_on: bool = Field(
        default=True,
        description="移动止损：赚到 trail_start_r 以后，止损跟着最高盈利往上挪，只收紧不放松，"
        "保住最高利润的一大半。默认开（Aaron 10-24）；改了只对之后新开的仓生效。",
    )
    trail_start_r: float = Field(
        default=0.7,
        ge=0.1,
        le=10.0,
        description="赚到几倍止损距离（R）才开始移动止损，刚开的仓不碰。档位推荐都是 0.7"
        "（止盈多在 1.1–1.5R，要在止盈前就开始跟）。",
    )
    trail_distance_r: float = Field(
        default=1.0,
        ge=0.1,
        le=10.0,
        description="移动止损离最高盈利多远（止损距离的几倍），放宽一点平时不碍事。"
        "档位推荐 30m–2h 0.7、4h 以上 1.0（1.2R 止盈前最高时锁住约 0.5R）。",
    )
    new_listing_block_h: float = Field(
        default=24.0,
        ge=0.0,
        le=168.0,
        description="新上线的币多少小时内不开新仓（按币安上线时间，价格还不稳）；0 = 不挡。",
    )
    adl_high_block: bool = Field(
        default=True,
        description="币安自动减仓风险是「高」的币不开新仓（每 30 分钟查一次，查不到不挡；"
        "已有持仓不管）。",
    )
    # 下面三项已停用：Aaron 10-24「这个 10 秒取消。还换成之前一分钟。」哨兵只看 1 分钟量。
    # 留着字段是让旧配置（Mac 的 app.toml 里写了 burst_10s_on = true）照样能读，写了也不生效。
    burst_10s_on: bool = Field(
        default=False, description="已停用：10 秒提前触发取消了，写了也不生效。"
    )
    burst_10s_ratio: float = Field(
        default=5.0, ge=1.0, le=100.0, description="已停用（10 秒提前触发取消了）。"
    )
    burst_min_1m_ratio: float = Field(
        default=2.0, ge=0.0, le=50.0, description="已停用（10 秒提前触发取消了）。"
    )
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
    # ---- 新信号（研究 2026-10-24）：都先在模拟盘出，testnet / live 各一个开关默认关
    oi_follow_on: bool = Field(
        default=False,
        description="出「持仓量变化」信号。档位推荐 2h–24h 开，30m / 1h 关。",
    )
    oi_window_min: int = Field(
        default=240,
        ge=15,
        le=720,
        description="持仓量变化看多长时间（分钟），同一个币同一个方向这么久只出一次。"
        "档位推荐 2h 60、4h 以上 240。",
    )
    oi_min_pct: float = Field(
        default=1.0, ge=0.2, le=10.0, description="持仓量变化：持仓量至少涨百分之几。"
    )
    oi_price_z: float = Field(
        default=1.0,
        ge=0.0,
        le=5.0,
        description="持仓量变化：同一段时间价格至少走几倍平时波动"
        "（过去 24 小时 1 分钟收益标准差 × √分钟数）。档位推荐 2h 2、4h 以上 1。",
    )
    oi_follow_live: bool = Field(
        default=False, description="testnet / live 也出持仓量变化信号（默认关，只在模拟盘出）。"
    )
    top_crowd_on: bool = Field(
        default=False,
        description="出「大户散户分歧」信号（反着大户做）。档位推荐 4h–24h 开，2h 以下关。",
    )
    top_crowd_min_pct: float = Field(
        default=5.0,
        ge=0.5,
        le=20.0,
        description="大户散户分歧：大户持仓多空比和全市场人数多空比各至少反向变百分之几"
        "（研究 16:38Z：5%；想多出信号可调到 2%）。",
    )
    top_crowd_window_min: int = Field(
        default=240,
        ge=15,
        le=720,
        description="大户散户分歧看多长时间（分钟），同一个币同一个方向这么久只出一次。",
    )
    top_crowd_live: bool = Field(
        default=False, description="testnet / live 也出大户散户分歧信号（默认关，只在模拟盘出）。"
    )
    btc_lead_on: bool = Field(
        default=False, description="出「BTC 先动、小币跟」信号。档位推荐 30m–2h 开，4h 以上关。"
    )
    btc_lead_z: float = Field(
        default=3.0,
        ge=1.5,
        le=10.0,
        description="BTC 先动：BTC 5 分钟至少走几倍平时波动"
        "（过去 24 小时 1 分钟收益标准差 × √5）。",
    )
    btc_lead_live: bool = Field(
        default=False, description="testnet / live 也出 BTC 先动信号（默认关，只在模拟盘出）。"
    )
    # ---- 和作战卡相反的信号（研究 2026-10-24：有仓交管家急查，反复出才开单币临时会）
    against_signal_on: bool = Field(
        default=True,
        description="哨兵看到和卡方向相反的信号时：有仓叫管家急查、反复出开临时会。"
        "关掉 = 只记录（和以前一样）。不会因此开反方向的仓。",
    )
    against_dedup_s: int = Field(
        default=300, ge=10, le=3600, description="同一个币同一种反向信号多久内只算 1 次（秒）。"
    )
    against_grace_s: int = Field(
        default=180, ge=0, le=3600, description="开仓后多久内的反向信号不算（刚进场的噪音，秒）。"
    )
    against_recheck_cooldown_s: int = Field(
        default=300, ge=10, le=3600, description="同一笔仓多久内最多因反向信号急查 1 次（秒）。"
    )
    against_strong_breakout_vol_ratio: float = Field(
        default=50.0,
        ge=1.0,
        le=50.0,
        description="放量突破 / 强平瀑布反弹的量 ≥ 这么多倍才算强反向信号，强的管家判 1 次翻向"
        "就平。默认 50 = 基本不算强：一次就平只看这笔仓是否已亏到 against_loss_stop_frac 倍止损"
        "（哨兵研究 10-24），其余要连续确认。",
    )
    against_loss_stop_frac: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="持仓已亏到这么多倍止损距离时，反向信号也按强的算（管家判 1 次就平）。"
        "0 = 不用。",
    )
    against_meeting_count: int = Field(
        default=2, ge=1, le=20, description="同一个币多久内出几次反向信号就申请单币临时会。"
    )
    against_meeting_window_s: int = Field(
        default=1800, ge=60, le=14400, description="上面「几次」的时间窗（秒）。"
    )
    against_meeting_gap_s: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="同一个币因反向信号最多多久申请 1 次临时会（秒）。",
    )

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
        if 0 < self.atr_long_period_min <= self.atr_period_min:
            raise ValueError("atr_long_period_min 必须比 atr_period_min 长")
        for name in ("session_factor_weekday", "session_factor_weekend"):
            table = getattr(self, name)
            if len(table) != 24 or not all(0.3 <= x <= 3.0 for x in table):
                raise ValueError(f"{name} 要 24 个数（曼谷 0–23 点），每个在 0.3–3.0 之间")
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
        description="on_demand = 只对作战卡里的币和有持仓的币在每根 K 线收盘时预测，哨兵出信号而"
        "没有新鲜预测时临时补算一次；all_symbols = 候选池里所有订阅的币都算（CPU 吃得多）。",
    )
    size_policy: Literal["fixed", "adaptive"] = Field(
        default="fixed",
        description="fixed = 一直用 model_size；adaptive = 按排队深度在 base/small/mini 间切换。",
    )
    model_size: Literal["mini", "small", "base"] = Field(
        default="base",
        description="预测模型（fixed 时用）。base = 最大，更准但每个币算得比标准慢 3–4 倍"
        "（Mac mini 每币约 3.5 秒）"
        "；small = 标准；mini = 最快最粗。加载失败自动改用标准。",
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
        default=400, ge=64, le=1500, description="启动和新币入池时回填多少根预测员周期的 K 线。"
    )
    interval: Literal["5m", "15m", "1h"] = Field(
        default="5m",
        description="喂给模型的 K 线周期（由 1 分钟线拼）。档位按最长持仓给：4h 及以下 5m，"
        "8h / 12h 15m，24h 1h。每根收完预测一次，过两根没更新算过期。",
    )
    pred_len: int = Field(
        default=12,
        ge=3,
        le=112,
        description="往后预测几根（× interval = 预测多远，档位按最长持仓给）。"
        "回看根数 + 这个 ≤ 512。",
    )
    lookback: int = Field(
        default=400,
        ge=64,
        le=500,
        description="喂给模型多少根历史 K 线（模型上下文上限 512，回看 + 往后预测 ≤ 512）。"
        "越多看得越久远，越少越贴近最近的走势。",
    )
    paths: int = Field(
        default=10,
        ge=1,
        le=64,
        description="每次模拟几条走势，模型自己平均成一条（官方用法，论文 10 条）。"
        "越多越稳、算得越慢（一次批量算完）。",
    )
    temperature: float = Field(
        default=0.6,
        ge=0.1,
        le=2.0,
        description="采样温度（官方回测和论文猜方向用 0.6；越高走势越乱）。",
    )
    rank_band: float = Field(
        default=1.0 / 6.0,
        ge=0.05,
        le=0.5,
        description="同一时刻所有币按预测涨幅排名，前这么多算预测员看好、后这么多算看坏"
        "（官方回测买前 1/6；看坏这一头是我们做空用的镜像，官方没有）。",
    )
    rank_min_peers: int = Field(
        default=6,
        ge=2,
        le=200,
        description="一起排名的新鲜预测至少几个币才算名次；不够就当预测员不表态。",
    )
    against_card_meeting: bool = Field(
        default=False,
        description="预测员连续几次看反作战卡时申请单币临时会。默认关（Aaron 2026-10-24：预测员还要"
        "观察）：关着时只在决策日志里记一条，不申请会；预测照常给 Jev、照常落盘。",
    )

    @model_validator(mode="after")
    def _check(self) -> PredictorConfig:
        if self.adaptive_base_max_queue >= self.adaptive_mini_min_queue:
            raise ValueError("adaptive_base_max_queue 必须小于 adaptive_mini_min_queue")
        if self.lookback + self.pred_len > 512:
            raise ValueError(
                f"回看 {self.lookback} + 往后预测 {self.pred_len} 超过模型上下文 512，"
                "请调小其中一个"
            )
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
    jev_max_concurrent: int = Field(
        default=4,
        ge=1,
        le=8,
        description="拍板人同时最多判几个信号（不同的币并行；同一个币一次只判一个，"
        "后来的排队、只留最新一条）。OpenRouter 和开会共用并发，别开太大。",
    )
    keeper_interval_s: int = Field(ge=5, le=120)
    reconcile_interval_s: int = Field(ge=10, le=300)
    slow_loop_interval_min: int = Field(ge=15, le=1440)
    screener_interval_min: int = Field(
        default=60,
        ge=15,
        le=1440,
        description="选币多久跑一次，默认跟开会间隔档位走（15 分钟下限保护币安接口权重）。",
    )
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

    @property
    def limit_take_profit_on(self) -> bool:
        """用不用限价止盈：paper 看 ``limit_take_profit``，testnet / live 看 ``…_live``。"""
        fast = self.fast_loop
        return fast.limit_take_profit if self.mode == RunMode.PAPER else fast.limit_take_profit_live

    @property
    def ignored_paper_switches(self) -> list[str]:
        """写着开、但只在 paper 生效的开关（testnet / live 下当关，不拒绝启动）。"""
        if self.mode == RunMode.PAPER:
            return []
        fast = self.fast_loop
        on = {
            "拍板只记不拦": fast.gate_record_only,
            "没有拍板人时用规则代替": fast.rule_gate_without_jev,
        }
        return [name for name, v in on.items() if v]

    @property
    def gate_record_only(self) -> bool:
        """拍板只记不拦实际开没开：没写时关；只有 paper 能开，testnet / live 永远关。"""
        return self.mode == RunMode.PAPER and bool(self.fast_loop.gate_record_only)

    @model_validator(mode="after")
    def _check(self) -> AppConfig:
        if self.card_pick_max > self.pool_size:
            raise ValueError("card_pick_max 不能大于 pool_size")
        if self.hold in PAPER_ONLY_HOLDS and self.mode != RunMode.PAPER:
            raise ValueError(
                f"最长持仓档 {self.hold} 只能在 paper 模式用（费用占止损太高），"
                "testnet / live 请用 1h 或 2h 或 4h"
            )
        return self
