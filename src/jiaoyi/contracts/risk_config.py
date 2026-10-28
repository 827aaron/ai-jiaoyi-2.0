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
from typing import Annotated, Literal, get_args

from pydantic import Field, model_validator

from jiaoyi.alerting.kinds import KINDS, field_of
from jiaoyi.contracts.common import Category, Contract, Probability, RunMode, SignalKind

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

        不知道权益（慢环、日志验收）时只剩绝对硬顶；也没写就是 ``inf``，由风控员按实时权益封顶。
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
                f" {worst:g}%：满仓位的单子会被风控员全部拒掉"
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
    """快环的门槛和节奏（哨兵、拍板人、风控员的档位换算、持仓管家、巡逻兵）。

    这些是策略参数，不是硬上限：只能在硬上限以内起作用，风控员仍按 ``RiskConfig`` 封顶。
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
    min_p_take_by_kind: dict[SignalKind, Probability] = Field(
        default_factory=dict,
        description="按信号类型单独定“做”的门槛，例如 btc_lead = 0.0：这类信号拍板人照常问、"
        "照常记，“做”概率不再拦，其余风控（持仓笔数、同币持仓、敞口和分组上限、每笔风险、止损、信号过期、"
        "作战卡、日亏熔断）照旧。没写的类型用 min_p_take。不写 = 全部用 min_p_take。"
        "同一次 BTC 大动会在多个小币上同时触发 btc_lead，这些仓由持仓笔数、总敞口和 "
        "max_open_by_kind 封顶。",
    )
    max_open_by_kind: dict[SignalKind, Annotated[int, Field(ge=0, le=100)]] = Field(
        default_factory=dict,
        description="按信号类型限制同时持仓笔数，例如 btc_lead = 3：这类信号开的在管持仓"
        "（含开仓中、平仓中）到了这个数，同类新信号就拒绝；0 = 这类信号不开仓。"
        "没写的类型不单独限，仍受 max_open_positions 和各项敞口上限约束。"
        "升级前开的仓记不得类型，不计入。",
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
    # ---- 风控员的档位换算
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
    leverage_clamp_to_bracket: bool = Field(
        default=False,
        description="杠杆按币安这个币的上限封顶（执行所的 /fapi/v1/leverageBracket，启动时和之后"
        "每天拉一次）：超过上限的按上限开，保证金按封顶后的杠杆重算，名义价值再不超过该杠杆那档"
        "的名义上限；每笔风险金额和各项敞口上限不变。默认关 = 原行为（杠杆超过上限时币安拒绝"
        "设杠杆，这笔不开）。分层没拉到时按原杠杆。",
    )
    card_range_clamp: bool = Field(
        default=True,
        description="作战卡的止损 ATR / 止盈 R 范围超出档位范围时取交集，交集为空才不做；"
        "关掉 = 一端越界整个币不做。",
    )
    book_depth_levels: int = Field(
        default=5,
        ge=1,
        le=20,
        description="看吃单那一侧前几档挂单（1–20）：用来限制单笔大小（max_notional_book_frac）、"
        "给拍板人看深度、算盘口失衡。档数越多，算出的深度越大。Aaron 10-26，默认 5（和以前一样）。",
    )
    max_notional_book_frac: float = Field(
        default=0.5,
        ge=0.0,
        le=5.0,
        description="单笔名义 ≤ 吃单一侧前 N 档（档数见 book_depth_levels）挂单额 × 这个比例，"
        "超了缩单不拒（0 = 不看盘口）。",
    )
    margin_safety_pct: float = Field(
        default=5.0,
        ge=0.0,
        le=50.0,
        description="开仓要的保证金（名义 ÷ 杠杆 + 开仓手续费）最多用到可用保证金的 (100 − 这个)%，"
        "超了缩单不拒。留的余量给下单前的价格变动和币安市价单的开仓损失，"
        "否则 hummingbot 会报「Not enough budget」不下单。",
    )
    min_size_ratio: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="缩单下限：各项上限（可用保证金、盘口深度、敞口余量等）把这笔压到计划风险"
        "（本金 × 每笔风险 × 仓位档）的这个比例以下就不开，免得付手续费、占持仓名额去开一笔"
        "赚不到钱的小仓。0 = 关（默认，原行为：压到多小都开）。Demo 推荐 0.25。",
    )
    respect_card_notional_min: bool = Field(
        default=False,
        description="作战卡不当硬约束（card_caps_binding 关）时，仍然守卡上的名义价值下限："
        "取整后的名义价值低于它就不开。默认关 = 原行为（卡上的名义范围只当建议记下）。"
        "card_caps_binding 开着时这条本来就生效。",
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
        default=5.0, ge=0.5, le=60.0, description="信号从触发到风控员审核超过这么久就作废。"
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
    keeper_price_max_age_s: float = Field(
        default=0,
        ge=0,
        le=600,
        allow_inf_nan=False,
        description="管家复查时这个币的行情最多旧几秒（时钟同开仓检查）；超过就这一轮跳过、"
        "维持现状，不平仓、不问模型，连续计数不变。0 = 不查（现状）。",
    )
    keeper_model_mode: Literal["act", "record"] = Field(
        default="act",
        description="act = 按 Jev 的平仓概率平（现状）；record = 每次照常问 Jev、全部记下，"
        "但不按平仓概率平（反向信号急查「过线即平」也不平），代码规则和 Jev 盈利门控照常。",
    )
    keeper_price_source: Literal["mainnet", "venue"] = Field(
        default="mainnet",
        description="管家算浮盈 / 最高浮盈 / 回吐 / 盈利门控和快照用哪个价：mainnet = 行情快照的"
        "最新成交价（现状）；venue = 执行所的标记价（Demo 上是 Demo 标记价；模拟盘和实盘就是"
        "主网行情）。读不到执行所标记价时这次退回主网价并记下。",
    )
    keeper_thesis_take_on: bool = Field(
        default=False,
        description="Jev 盈利门控：浮盈至少 keeper_thesis_take_min_r R，且 Jev 最近一次（不超过 "
        "keeper_thesis_take_max_age_s 秒）答「进场理由不成立」的概率至少 keeper_thesis_take_p，"
        "就全平。只平不放宽。",
    )
    keeper_thesis_take_min_r: float = Field(
        default=0.5,
        ge=0.0,
        le=10.0,
        allow_inf_nan=False,
        description="盈利门控：浮盈至少几倍止损距离（R = 开仓价到止损价，未扣手续费，"
        "和回吐保护同口径）。",
    )
    keeper_thesis_take_p: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        allow_inf_nan=False,
        description="盈利门控：Jev「开仓理由还成立吗」答 no 的概率至少这么多。",
    )
    keeper_close_guard: bool = Field(
        default=False,
        description="管家发平仓前先看：交易所保护腿已触发 / 正在退出（最多等 "
        "keeper_close_guard_exiting_max_s 秒）、正在换代（最多等 "
        "keeper_close_guard_replacing_max_s 秒）、或同一笔仓半个复查间隔内已发过一次平仓，"
        "就这次不发。关 = 现状（不看）。",
    )
    keeper_close_guard_replacing_max_s: float = Field(
        default=30.0,
        ge=0.0,
        le=600.0,
        allow_inf_nan=False,
        description="平仓前检查：同一次保护腿换代最多让管家等几秒，过了照常平（0 = 不等）。",
    )
    keeper_close_guard_exiting_max_s: float = Field(
        default=120.0,
        ge=0.0,
        le=3600.0,
        allow_inf_nan=False,
        description="平仓前检查：交易所保护腿已触发 / 正在退出时最多让管家等几秒，过了照常平"
        "并记下（0 = 不等）。",
    )
    keeper_close_guard_total_max_s: float = Field(
        default=180.0,
        ge=0.0,
        le=3600.0,
        allow_inf_nan=False,
        description="平仓前检查：保护腿连续在途（接连换代、换代接退出）合计最多让管家等几秒，"
        "过了照常平（0 = 不等）。",
    )
    keeper_venue_alert_reviews: int = Field(
        default=3,
        ge=0,
        le=100,
        description="keeper_price_source = venue 时执行所标记价连续这么多次复查读不到或过旧就告警"
        "（同一段只告一次；不退回主网价）。0 = 不告警。",
    )
    keeper_thesis_take_max_age_s: float = Field(
        default=180.0,
        ge=1.0,
        le=3600.0,
        allow_inf_nan=False,
        description="盈利门控：Jev 那次回答最多是多少秒之前的（按那次复查的时刻算）。",
    )
    frozen_peak_exit_on: bool = Field(
        default=True,
        description="Enable observed profit target freezing for new Demo positions only.",
    )
    frozen_peak_exit_after_s: int = Field(
        default=3600,
        ge=60,
        le=604800,
        description="Seconds after the first verified exchange entry fill to freeze the target.",
    )
    frozen_peak_evidence_mode: Literal["sync", "background"] = Field(
        default="sync",
        description="sync = every keeper review waits for the exchange evidence read (up to "
        "12 s); background = a per-position refresher reads it every "
        "frozen_peak_evidence_interval_s, reviews use the cached result only while it is "
        "fresh, and the routine review round no longer waits for its slowest position.",
    )
    frozen_peak_evidence_interval_s: int = Field(
        default=30,
        ge=5,
        le=600,
        description="Background mode: seconds between evidence reads for one open position.",
    )
    frozen_peak_evidence_max_age_s: int = Field(
        default=90,
        ge=0,
        le=3600,
        description="Background mode: older cached evidence skips the frozen peak rule for "
        "that review (0 = no age limit; otherwise at least the interval plus 30 s).",
    )
    giveback_on: bool = Field(
        default=True,
        description="回吐保护开关（代码规则，不问管家模型）：最高浮盈到过 giveback_arm_r 倍止损距离"
        "之后，浮盈从最高点吐掉 giveback_close_pct % 就全平。Aaron 10-25，默认开。",
    )
    giveback_close_pct: float = Field(
        default=45.0,
        ge=10.0,
        le=95.0,
        description="回吐保护：浮盈从最高点吐掉这个百分比就全平。Aaron 10-25，默认 45。",
    )
    giveback_arm_r: float = Field(
        default=0.5,
        ge=0.1,
        le=10.0,
        description="回吐保护从最高浮盈到几倍止损距离（R）才开始看。Aaron 10-25，默认 0.5。",
    )
    # ---- 反向作战卡（#314）
    opposite_card_action: Literal["close", "confirm_or_tighten"] = Field(
        default="close",
        description="有效新卡方向和持仓相反时怎么办。close = 立刻全平（原规则）；"
        "confirm_or_tighten = 价格确认（下面三条任一）才全平，否则把交易所托管止损收紧到保本附近、"
        "继续盯，之后确认了再平。旧通道（没有交易所托管保护）的仓照旧全平。",
    )
    opposite_card_ab: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="反向卡事件按 持仓号 + 币 + 这条指令的发布时间（issued_at）+ 方向 固定抽签："
        "这个比例走新规则，其余照旧全平，两组都记反事实；整卡换版本（别的币变了）不重抽，"
        "这个币重新开会出新指令才重抽。0 = 不分组（全按 opposite_card_action）；"
        "只在 confirm_or_tighten 时可设。",
    )
    opposite_confirm_loss_r: float = Field(
        default=0.5,
        ge=0.0,
        le=10.0,
        description="价格确认①：按行情价已亏到这么多倍原始止损距离"
        "（R = 账本开仓价到止损价）。0 = 不用这条。",
    )
    opposite_confirm_atr: float = Field(
        default=1.0,
        ge=0.0,
        le=20.0,
        description="价格确认②：从第一次看到这张反向卡起，价格朝不利方向走了这么多倍 ATR"
        "（止损用的那个 ATR）。0 = 不用这条。",
    )
    opposite_confirm_key_level: bool = Field(
        default=True,
        description="价格确认③：价格越过反向卡自己在开仓价之外最近的关键位（卡做空看支撑 / 枢轴，"
        "卡做多看阻力 / 枢轴）。false = 不用这条。",
    )
    opposite_tighten_buffer_bps: float = Field(
        default=5.0,
        ge=0.0,
        le=200.0,
        description="收紧止损的目标 = 开仓价 ±（实际开仓手续费 + 估算平仓手续费"
        " + 这么多基点的开仓名义），即扣费后至少保本这么多。",
    )
    opposite_tighten_mark_gap_bps: float = Field(
        default=10.0,
        ge=0.0,
        le=500.0,
        description="收紧的止损离触发价源（止损按标记价触发时就是标记价）至少隔这么多基点才挂："
        "多要 目标 < 价 ×（1 − gap），空要 目标 > 价 ×（1 + gap）；不够就按原规则全平，"
        "不靠交易所拒单。0 = 只要求在安全一侧（仍至少按保护状态机的标记价缓冲）。",
    )
    opposite_profit_mode: Literal["trail", "breakeven"] = Field(
        default="trail",
        description="价格没确认、仓位赚得够（保本价可挂）时怎么收紧：trail = 在第一次看到反向卡时的"
        "可成交价强制启动移动止损（之后照常只收紧）；breakeven = 止损挪到扣费保本 + 缓冲。"
        "亏着或赚得不够的照旧全平。只在 confirm_or_tighten 时起作用。",
    )
    opposite_trail_distance_mult: float = Field(
        default=1.0,
        gt=0.0,
        le=5.0,
        description="trail 模式：锚定的移动止损回撤距离 = 开仓时带的移动止损距离 × 这个倍数"
        "（之后这笔仓的移动止损都按它）。1 = 和平常一样。",
    )
    opposite_anchor_retry_s: float = Field(
        default=30.0,
        ge=0.0,
        le=600.0,
        description="trail 模式锚定时盘口疑似插针（盘口价偏离标记价太多）：这么多秒内每轮再试、"
        "不告警，过了按原规则全平并告警。0 = 立刻平。",
    )
    # ---- 新作战卡把持仓的币改成中性
    card_neutral_action: Literal["hold", "tighten", "close"] = Field(
        default="hold",
        description="持仓开仓之后发布的有效新卡把这个币改成中性（no_trade，或 stale_on_arrival "
        "作废成只看不做）时怎么办。hold = 不管（现状）；tighten = 和反向卡没确认时同一套："
        "亏着或赚得不够就全平，赚得够就在第一次看到时的可成交价锚定移动止损（只锚一次）；"
        "close = 直接全平。方向反了的卡归 opposite_card_action 管。",
    )
    card_neutral_on_absent: bool = Field(
        default=False,
        description="新卡里干脆没有这个币也算中性（按 card_neutral_action 处理）。false = 不算。",
    )
    card_neutral_trail_distance_mult: float = Field(
        default=1.0,
        gt=0.0,
        le=5.0,
        description="card_neutral_action = tighten 锚定的移动止损回撤距离 = 开仓时带的移动止损距离"
        " × 这个倍数。1 = 和平常一样。",
    )
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
        default=False,
        description="只在卡的进场区间内开仓（默认关：10-24 数据显示会挡掉好信号）："
        "卡上这个币"
        "写了进场区间、触发价在区间外就不开，连拍板人都不问；往交易方向（做多往上、做空往下）"
        "最多放宽 0.1R（R ≈ 卡上止损倍数中值 × 止损 ATR）。卡上没写区间照旧不限。",
    )
    entry_limit_pullback: bool = Field(
        default=False,
        description="进场用挂单等回踩（默认关，关 = 市价进）：风控员按触发价往有利方向让一段"
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
    chase_price_max_age_s: float = Field(
        default=0,
        ge=0,
        le=600,
        allow_inf_nan=False,
        description="改市价追进前按现价复查进场区间时，这个币的行情最多旧几秒（和开仓检查同一个"
        "时钟：成交 / 盘口 / 标记价任一最后更新）；超过就这次不追。0 = 不查（现状）。",
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
    market_review_on: bool = Field(
        default=True,
        description="市场模型可提名现卡与原池交集中的币复核；仍需打开总临时会开关，"
        "不依赖旧 event_triggers 名单，不直接改卡或延长有效期。",
    )
    # ---- 大盘方向变化：只更新市场背景，不重跑各币会议
    regime_flip_on: bool = Field(
        default=True, description="每 5 分钟看一次大盘是否翻向；满足条件就独立更新大盘。"
    )
    regime_flip_live: bool = Field(
        default=False, description="测试网 / 实盘也启用独立大盘更新；默认关，仅模拟盘开启。"
    )
    regime_flip_target: Literal["flash_market_only", "desk", "off"] = Field(
        default="flash_market_only",
        description="确认翻转后叫谁更新大盘：flash_market_only = 总盘会的 Flash 独立刷新"
        "（以前的做法）；"
        "desk = 让大盘台立即重跑一轮（slow.toml [market_desk] 要开）；off = 照常检查和记录，"
        "不更新大盘。大盘台接管（mode = replace）时不能是 flash_market_only。",
    )
    regime_flip_breadth: int = Field(
        default=30,
        ge=0,
        le=40,
        description="按 40 个有效币算，至少几个同向才算方向变化（中性对称检查上下）；"
        "默认 30，即四分之三。有效币数不同就按比例算；0 = 不要求广度条件。",
    )
    regime_flip_btc_pct: float = Field(
        default=1.0,
        ge=0,
        allow_inf_nan=False,
        description="BTC 相比市场参照价至少同向走百分之几（中性对称检查上下）；"
        "默认 1 表示 1%，0 = 不要求 BTC 幅度条件。",
    )
    regime_flip_confirm: int = Field(
        default=2,
        ge=0,
        description="连续几次同方向检查满足条件才更新大盘；默认 2 次。0 = 不等连续确认。",
    )
    regime_flip_min_gap_min: float = Field(
        default=60,
        ge=0,
        allow_inf_nan=False,
        description="两次独立大盘更新至少隔几分钟；默认 60，填 0 不限制间隔。",
    )
    regime_flip_min_symbols: int = Field(
        default=10,
        ge=0,
        description="至少有几个币能算出一小时涨跌才判断；默认 10，0 = 不限制个数。"
        "持平和缺数据的不计入。",
    )
    regime_flip_check_min: float = Field(
        default=5,
        ge=0,
        allow_inf_nan=False,
        description="隔几分钟检查一次大盘翻转；默认 5，可填小数，0 = 停止定时检查。",
    )
    regime_flip_suspect_log_min: float = Field(
        default=30,
        ge=0,
        allow_inf_nan=False,
        description="连续同类疑似在独立观测文件里隔几分钟再记一次；默认 30，0 = 每次都记。"
        "决策日志和面板始终记录每次疑似，不受此项影响。",
    )
    regime_flip_price_max_age_min: float = Field(
        default=5,
        ge=0,
        allow_inf_nan=False,
        description="总盘会取 BTC 参照价时，行情最多可旧几分钟；默认 5，0 = 不限制新鲜程度。"
        "完全没有价格时仍无法计算涨跌。",
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
        if 0 < self.frozen_peak_evidence_max_age_s < self.frozen_peak_evidence_interval_s + 30:
            raise ValueError(
                "frozen_peak_evidence_max_age_s 要么填 0（不限），要么至少 "
                "frozen_peak_evidence_interval_s + 30 秒（一次证据读最多十几秒），"
                "否则正常节奏下缓存也会算过期"
            )
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
        if self.opposite_card_ab > 0 and self.opposite_card_action != "confirm_or_tighten":
            raise ValueError(
                "opposite_card_ab 只在 opposite_card_action = confirm_or_tighten 时能大于 0"
            )
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
    keeper_snapshot: Literal["direction", "rank", "off"] = Field(
        default="direction",
        description="管家（持仓复查）快照里怎么带预测员。direction = 照旧：kronos.agree 按平均路径"
        "终点在现价哪一侧给 1 / 0，再带全池名次（p_up 只有 0 / 1，实测多数是 0，做多多半是 0、"
        "做空多半是 1，多空不对称）；rank = 只带全池名次 top / mid / bottom（和拍板加分同一个"
        "排名，多空镜像对称），排不出名次就不写；off = 快照不带预测员。改后重启。",
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
    jev_shared_cooldown: bool = Field(
        default=True,
        description="开仓拍板和持仓管家共用一份 Jev 连续出错计数和冷却（原行为）：管家连错几次"
        "也会让开仓拍板进冷却。关掉 = 两边各数各的、各冷却各的（通道、模型、超时照旧共用）。"
        "改后重启生效。",
    )
    account_read_retries: int = Field(
        default=0,
        ge=0,
        le=3,
        description="拍板通过后开仓前读账户（hummingbot 的账户状态）失败时，隔 1 秒再读几次；"
        "都失败就记一条风控员拒绝「读账户失败，本信号放弃」。只对很快就失败的情况有用（连不上、"
        "5xx）：hummingbot 请求超时是 5 秒，一次超时就把信号有效期（signal_max_age_s）"
        "用得差不多了；"
        "信号剩下的有效期不够再等一次就不重读，直接放弃。重读成功后再查一遍暂停 / 重启和信号是否"
        "过期。0 = 不重读（原行为）。热生效。",
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
    protection_stale_s: int = Field(
        default=120,
        ge=0,
        le=3600,
        description="面板：交易所托管保护单的观测时间超过这么多秒就标“数据过期”（对账员约每 "
        "reconcile_interval_s 刷新一次）；0 = 不标。",
    )
    pool_size: int = Field(ge=5, le=100, description="候选池大小，设计值 30–50。")
    card_pick_max: int = Field(ge=1, le=50, description="军师每次最多挑几个，设计值 10–20。")
    data_dir: str = Field(min_length=1, description="决策日志、模块状态等写在这里。")
    sim_environment: Literal["demo", "legacy_testnet"] = Field(
        default="demo",
        description="paper / testnet 连哪个币安模拟环境：demo = Demo Trading，"
        "legacy_testnet = 旧测试网（已停用，仅回退）。hummingbot 侧补丁要与此一致。",
    )
    trade_notices: bool = Field(
        default=True,
        description="旧开关，已拆成下面的 ``notify_*``。写成 false 时，开仓成交 / 平仓完成两项"
        "默认跟着关（没单独写那两项的话）；新配置请直接用 ``notify_*``。",
    )
    notify_open_filled: bool = Field(default=True, description="推 Telegram：开仓成交时推一条。")
    notify_close_done: bool = Field(
        default=True, description="推 Telegram：平仓完成时推一条（含盈亏）。"
    )
    notify_lifeboat_hit: bool = Field(
        default=True, description="推 Telegram：币安上的保命止损被打到时推。"
    )
    notify_lifeboat_missing: bool = Field(
        default=True,
        description="推 Telegram：保命止损缺失、挂不上、重挂 / 撤销失败时推。",
    )
    notify_order_failed: bool = Field(
        default=True,
        description="推 Telegram：开仓或平仓失败、结果不明、没成交时推。",
    )
    notify_reconcile: bool = Field(
        default=True,
        description="推 Telegram：对账发现不一致或连不上时推。",
    )
    notify_news_block: bool = Field(
        default=True, description="推 Telegram：因新闻 / 事件禁止某个币新开仓时推。"
    )
    notify_model_credit: bool = Field(
        default=True, description="推 Telegram：模型额度快用完或被拒（402）时推。"
    )
    credit_low_floor_usd: float = Field(
        default=5.0,
        gt=0,
        le=10000,
        description="OpenRouter 余额低于这么多美元就提醒（日均花费 × 2 天更高时按那个算）。",
    )
    credit_resend_hours: float = Field(
        default=6.0,
        ge=1,
        le=168,
        description="同一条额度提醒（快用完 / 被拒）多久重复推一次，单位小时。",
    )
    card_score_every_min: int = Field(
        default=60,
        ge=0,
        le=1440,
        description="面板每隔多少分钟给窗口走完的作战卡补算方向记分（发布时刻起算、扣掉同期币池，"
        "写 data/card_score/）。只读主网公开 K 线，不影响任何交易判断；0 = 不算。"
        "只有面板用，改了不用重启主循环，面板这一轮等完（最多一个间隔）后生效。",
    )
    card_score_window_days: int = Field(
        default=30,
        ge=0,
        le=365,
        description="方向记分汇总最近多少天出的卡（逐卡结果照样全留）；0 = 全部。"
        "只有面板用，改了不用重启主循环。",
    )
    notify_meeting_paused: bool = Field(
        default=True,
        description="推 Telegram：军师团因失败或当日花费超限暂停开会时推。",
    )
    notify_spend_alert: bool = Field(
        default=True,
        description="推 Telegram：军师团日上限设为不限时，当天花费超过本档默认日上限"
        "提醒一次（只提醒，不停会）。",
    )
    notify_control: bool = Field(
        default=True,
        description="推 Telegram：面板暂停 / 恢复开新仓、全部平仓、重新加载配置时推。",
    )
    notify_live_started: bool = Field(default=True, description="推 Telegram：实盘启动时推。")
    notify_system: bool = Field(
        default=True,
        description="推 Telegram：拒绝启动、状态文件损坏、巡逻兵全平等系统级异常时推。",
    )
    reconciler_close_unknown_positions: bool = Field(
        default=False,
        description="对账员遇到不在账本里的币安仓位 / hummingbot 执行器时直接平掉。"
        "默认只告警（可能是你手动开的）；要打开请在 M4 小额实盘前自己决定。",
    )
    reconciler_executor_stop_retry_rounds: int = Field(
        default=0,
        ge=0,
        le=10000,
        description="对账员停外来执行器（只停不平，keep_position=True）结果不明、之后又连续这么多轮"
        "没确认终止，就再发一次停止。0 = 永不重发（只查）。本程序托管仓平仓时停自己的执行器"
        "另由 hb_stop_unknown_retries 管。",
    )
    reconcile_opening_grace_s: float = Field(
        default=0.0,
        ge=0.0,
        le=600.0,
        description="对账员：新仓建账后这么多秒内，「交易所托管还没到 armed 阶段」「运行列表暂时"
        "没返回执行器（逐项查询仍 active / 查不清）」这两类开仓过渡状态记「信息」而不是「出错」，"
        "不推 Telegram、不点红对账灯。只改标注，平仓、补挂、终止计数都不变；"
        "过了这个时间照旧记「出错」。"
        "0 = 一律记出错（原行为）。热生效。",
    )
    reconcile_closing_grace_s: float = Field(
        default=0.0,
        ge=0.0,
        le=600.0,
        description="对账员：从第一次看到这笔在平仓"
        "（状态平仓中、平仓结果待确认、托管在退出 / 清理）算起这么多秒内，"
        "「平仓结果尚未确认」「交易所托管仍在退出阶段」"
        "「运行列表未返回执行器」这几类平仓过渡状态记「信息」而不是「出错」，"
        "不推 Telegram、不点红对账灯。"
        "只改标注，平仓和保护处理都不变；过了这个时间照旧记「出错」。"
        "平仓失败又重试不重新计时；重启后从重启后第一次看到重新算。"
        "0 = 一律记出错（原行为）。热生效。",
    )
    protection_readback_grace_s: float = Field(
        default=0.0,
        ge=0.0,
        le=30.0,
        description="Demo 托管保护：本程序刚提交、交易所回执已接受的条件单，回执后这么多秒内"
        "查不到就按 algoId 只读重试（不重发、不当已撤、不建新代）；过时仍查不到照旧报保护未确认。"
        "0 = 不等（原行为）。改后重启生效。",
    )
    protection_prearm_grace_s: float = Field(
        default=0.0,
        ge=0.0,
        le=600.0,
        description="Demo 托管保护：新仓还没完整托管（保命 + 止损 + 止盈从没同时确认过）时，"
        "同步失败最多连续容忍这么多秒（从第一次失败算）再按对账原因平仓。只宽限交易所读延迟和"
        "网络一时失败，且必须仍有已确认的保命单或止损、没有提交/撤销未知或已触发的腿、不在退出中；"
        "其余错误照旧立即平仓。宽限内照样告警并下一轮重试。0 = 不容忍（原行为）。改后重启生效。",
    )
    protection_unknown_submit_grace_s: float = Field(
        default=0.0,
        ge=0.0,
        le=10.0,
        description="Demo 托管保护：条件单提交结果未知（超时、5xx、回执解析不了）时，当场按原 CID "
        "只读重查最多这么多秒，查到就当已挂上、接着挂下一条腿；绝不重发、不换新 CID。"
        "等待期间这个币的平仓和整个托管保护轮询都暂停。仍查不到时，若有已确认的保命单或止损，"
        "这笔提交未知也按未托管宽限保留仓位、每轮只查原 CID，宽限用完照旧平仓；"
        "需要 protection_prearm_grace_s > 0 才会在之后每轮重查原单。"
        "0 = 提交未知立即平仓（原行为）。改后重启生效。",
    )
    position_mode_cache_s: float = Field(
        default=0.0,
        ge=0.0,
        le=3600.0,
        description="币安持仓模式（单向 / 双向）查一次后这么多秒内不再查（0 = 每次都查，原行为）。"
        "这个查询占 30 权重，本地限流每秒只有 20，托管保护和冻结盈利价证据每轮都要查好几次。"
        "有持仓或挂单时币安不允许切换模式；开新仓前的检查、任何 -4061 或非单向持仓都会重新查。"
        "改后重启生效。",
    )
    protection_leg_order: Literal["lifeboat_first", "stop_first"] = Field(
        default="lifeboat_first",
        description="Demo 托管保护：新仓的三条腿按什么顺序挂。"
        "lifeboat_first = 保命单 → 止损 → 止盈（原行为）；stop_first = 止损 → 止盈 → 保命单："
        "交易所托管的止损已经能保护仓位，保命单只是更宽的后备。"
        "stop_first 下止损确认后保命单挂失败（被拒、提交结果未知）不立即平仓："
        "在未托管宽限（protection_prearm_grace_s）内告警、下一轮重试"
        "（被拒的换新单号重挂，未知的只查原单号），宽限用完仍没挂上照旧平仓；"
        "止损、止盈挂失败照旧立即平仓。只影响之后新挂的一代，旧接口的仓不变。改后重启生效。",
    )
    hosted_close_mode: Literal["hb_first", "direct"] = Field(
        default="hb_first",
        description="Demo 托管仓平仓顺序。hb_first = 先停 HB 执行器、等它结束并核完条件单"
        "再直连平仓（原行为，决定到成交常要 15–110 秒）；direct = 管家 / 巡逻兵等决定平仓时，"
        "HB 执行器仍在跑、没有自己的平仓单、入场已结束、保护腿状态都明确，就先按持久化单号"
        "下只减仓市价单，再只停不平 HB；交易所止损一直挂到确认零仓后才撤。"
        "不满足条件的照旧走 hb_first；提交结果未知照旧只查不重发。改后重启生效。",
    )
    protection_tick_alert_after: int = Field(
        default=1,
        ge=1,
        le=100,
        description="Demo 托管保护：后台每秒一轮的保护核验，同一笔仓连续失败这么多轮才发紧急告警"
        "「托管保护本轮未确认」，之前只记日志；成功一轮就重新计数。1 = 每轮失败都发（原行为）。"
        "改后重启生效。",
    )
    hb_stop_unknown_retries: int = Field(
        default=0,
        ge=0,
        le=10,
        description="Demo 托管仓：平仓时程序停自己的 HB 执行器（只停不平，keep_position=True），"
        "停止结果未知、"
        "HB 仍报 RUNNING 时，最多再补发几次，间隔 5、10、20… 秒翻倍；这种停止从不卖出，"
        "补发无害。不补发时零仓也得等 HB 自己结束才撤交易所保护腿。次数按进程计，重启重新计。"
        "外来执行器的重发是 reconciler_executor_stop_retry_rounds。"
        "0 = 不补发（原行为）。改后重启生效。",
    )
    protection_stop_working_type: Literal["CONTRACT_PRICE", "MARK_PRICE"] = Field(
        default="CONTRACT_PRICE",
        description="Demo 托管保护：止损（含移动止损）按什么价触发。CONTRACT_PRICE = 最新成交价"
        "（原行为）；MARK_PRICE = 标记价，不会被 Demo 盘口的瞬间插针扫掉，但触发后仍按当时盘口"
        "市价成交。只影响之后新挂的止损（新仓、移动止损换价），已挂的单照旧按挂单时的核对。"
        "保命单固定按标记价。改后重启生效。",
    )
    protection_take_profit_working_type: Literal["CONTRACT_PRICE", "MARK_PRICE"] = Field(
        default="CONTRACT_PRICE",
        description="Demo 托管保护：止盈按什么价触发。CONTRACT_PRICE = 最新成交价（原行为）；"
        "MARK_PRICE = 标记价。只影响之后新挂的止盈，已挂的单照旧按挂单时的核对。改后重启生效。",
    )
    protection_trailing_book_mark_max_dev_bps: float = Field(
        default=30.0,
        ge=0.0,
        le=1000.0,
        description="Demo 托管保护·移动止损：用来算峰值的盘口价往“抬高峰值”的方向偏离同时读到的"
        "标记价超过这么多基点（多仓买一高于标记价、空仓卖一低于标记价），就当这一轮盘口不可信"
        "（Demo 插针），不更新峰值、不激活，记原因并告警；反方向（价差宽）只会让峰值更保守，不拦。"
        "默认 30 基点（正常时买一不会高过标记价这么多；插针一般远大于）。"
        "0 = 不比对（旧行为，不建议：插针会把峰值永久抬到针尖）。改后重启生效。",
    )
    protection_trailing_stop_mark_buffer_bps: float = Field(
        default=5.0,
        ge=0.0,
        le=500.0,
        description="Demo 托管保护·移动止损：挂新一代止损前先读标记价，多仓要求新止损价 < 标记价 ×"
        "（1 − 这么多基点），空仓要求 > 标记价 ×（1 + 这么多基点）；不满足（或标记价读不到）这一轮"
        "就不换代，旧止损留着，不靠交易所拒单（-2021）去触发平仓。0 = 只要求在标记价正确一侧。"
        "改后重启生效。",
    )
    protection_trailing_min_step_bps: float = Field(
        default=5.0,
        ge=0.0,
        le=500.0,
        description="Demo 托管保护·移动止损：新止损价比交易所上正在挂的止损至少收紧这么多基点"
        "才换一代（撤旧挂新），免得每跳一个最小价位就换一次单。"
        "默认 5 基点（多数币约 2 个最小价位）。"
        "0 = 一有变化就换（旧行为）。改后重启生效。",
    )
    protection_trailing_book_critical_after_s: float = Field(
        default=300.0,
        ge=0.0,
        le=86400.0,
        description="Demo 托管保护·移动止损：已激活的移动止损连续这么多秒都没拿到有效盘口，告警从"
        "“提醒”升级成“紧急”（正文带最后一次成功观测时间）。0 = 一直只发提醒。改后重启生效。",
    )
    predictor: PredictorConfig = Field(
        default_factory=PredictorConfig, description="预测员运行方式，见 ``[predictor]``。"
    )
    fast_loop: FastLoopConfig = Field(
        default_factory=FastLoopConfig, description="快环门槛；不写就用默认值（见示例）。"
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate_trade_notices(cls, data: object) -> object:
        """老的 ``trade_notices = false`` 等于关掉开仓成交 / 平仓完成两类（没单独写的话）。"""
        if isinstance(data, dict) and data.get("trade_notices") is False:
            data = {"notify_open_filled": False, "notify_close_done": False, **data}
        return data

    @property
    def muted_alert_kinds(self) -> frozenset[str]:
        """设置里关掉的 Telegram 告警类别（``alerting/kinds.py``）。"""
        return frozenset(k for k in KINDS if not getattr(self, field_of(k)))

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
