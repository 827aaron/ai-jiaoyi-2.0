"""设置页「你的设置实际意味着」：按现在的硬上限、最长持仓档和本金算几行大白话，只提示不拦。

数字依据见研究稿 ``orders-spec.md`` 的 R1 / R2 和 ``orders-draft.toml``：
  - 每笔实际风险 = 风险% × 仓位档（S / M / L）
  - 同时最多几笔满仓 = 日亏熔断 ÷ (风险 × 保命倍数)
  - 实盘推荐（按每笔 1%，风险 r% 就乘 r）：杠杆 / 总敞口、单笔名义倍数，日亏 5%
  - AI 开会费：实测每个币一场 $0.089、每轮约 5 个币；推荐开会档 = 费用 ≤ 本金 0.05% / 天的最快一档
    （不快过 1 小时），推荐日上限 = 本金 × 0.05%（最少 $1）
"""

from __future__ import annotations

import math

from pydantic import BaseModel

from jiaoyi.contracts import AppConfig, RiskConfig
from jiaoyi.core import profiles

LIVE_LEVERAGE = {"30m": 8, "1h": 8, "2h": 7, "4h": 5, "8h": 4, "12h": 3, "24h": 2}
"""实盘推荐杠杆 = 总敞口倍数（按每笔风险 1%）。"""
LIVE_NOTIONAL_X = {"30m": 3, "1h": 3.5, "2h": 4, "4h": 5, "8h": 5, "12h": 5, "24h": 5}
LIVE_DAILY_LOSS_PCT = 5.0
LEVERAGE_SQUEEZE = {"30m": 54, "1h": 41, "2h": 22, "4h": 8}
"""研究实测：风险 5%、杠杆 10、L 档时单笔要的名义超过 10 倍的比例（%），其余档几乎不超。"""

COST_PER_SYMBOL_USD = 0.089
SYMBOLS_PER_ROUND = 5
AI_SHARE_PCT = 0.05
"""AI 开会费建议不超过本金每天的这个百分比。"""
MIN_DAILY_CAP_USD = 1.0
FASTEST_RECOMMENDED = "1h"
INTERVAL_MIN = {"15m": 15, "30m": 30, "1h": 60, "2h": 120, "6h": 360, "12h": 720, "24h": 1440}


class MeaningLine(BaseModel):
    label: str
    text: str
    warn: bool = False


class SettingsMeaning(BaseModel):
    lines: list[MeaningLine]


def _pct(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") + "%"


def meeting_cost(profile: str) -> tuple[float, float]:
    """这一档每天 AI 开会费（美元）：(不算临时会, 临时会开满)。"""
    rounds = 1440 / INTERVAL_MIN[profile]
    base = rounds * SYMBOLS_PER_ROUND * COST_PER_SYMBOL_USD
    sched = profiles.defaults(profile, "slow.schedule")
    events = sched.get("event_max_per_day", 0) if sched.get("event_meeting_enabled") else 0
    return base, base + events * COST_PER_SYMBOL_USD


def recommended_meeting(equity_usdt: float) -> str | None:
    """费用（临时会算一半）≤ 本金 0.05% / 天的最快一档，不快过 1 小时；一档都不够就是 None。"""
    budget = equity_usdt * AI_SHARE_PCT / 100
    order = list(profiles.PROFILES)
    for p in order[order.index(FASTEST_RECOMMENDED) :]:
        lo, hi = meeting_cost(p)
        if (lo + hi) / 2 <= budget:
            return p
    return None


def build_meaning(
    risk: RiskConfig,
    app: AppConfig,
    profile: str,
    daily_cost_limit_usd: float | None,
    equity_usdt: float | None,
) -> SettingsMeaning:
    r = risk.risk_per_trade_pct
    fl = app.fast_loop
    fs, fm = fl.size_fraction_s, fl.size_fraction_m
    mult = risk.lifeboat_stop_multiplier
    halt = risk.daily_loss_halt_pct
    lev = risk.max_leverage
    hold = app.hold
    lines = [
        MeaningLine(
            label="每笔实际风险",
            text=f"拍板人选 S / M / L 档时分别是本金的 {_pct(r * fs)} / {_pct(r * fm)} / {_pct(r)}"
            f"（风险 {_pct(r)} × 仓位档 {fs:g} / {fm:g} / 1）",
        ),
    ]
    full, small = math.floor(halt / (r * mult)), math.floor(halt / (r * fs * mult))
    lines.append(
        MeaningLine(
            label="同时最多几笔满仓",
            text=f"日亏熔断 {_pct(halt)} ÷（风险 {_pct(r)} × 保命 {mult:g} 倍）= {full} 笔；"
            f"都是 S 档时 {small} 笔。再多的话全打到保命止损会超过当天额度",
            warn=full < 1,
        )
    )
    squeeze = f"止损比 {r / lev * 100:.0f}bp 窄的单会被杠杆 {lev} 倍压小（单笔名义 = 风险 ÷ 止损）"
    if r == 5 and lev == 10 and hold in LEVERAGE_SQUEEZE:
        squeeze += f"；研究实测 {hold} 档 L 仓约 {LEVERAGE_SQUEEZE[hold]}% 的单会被压小"
    lines.append(MeaningLine(label="杠杆压小", text=squeeze))
    if hold in LIVE_LEVERAGE:
        lines.append(
            MeaningLine(
                label=f"实盘推荐（{hold} 档）",
                text=f"按每笔风险 1%：杠杆 ≤ {LIVE_LEVERAGE[hold]}、"
                f"总敞口 ≤ {LIVE_LEVERAGE[hold]} 倍、"
                f"单笔名义 ≤ {LIVE_NOTIONAL_X[hold]:g} 倍、日亏熔断 {LIVE_DAILY_LOSS_PCT:g}%；"
                "每笔风险改成 r% 时杠杆、敞口、名义都乘 r",
            )
        )
    lines.append(
        MeaningLine(
            label="滑点 / 价差",
            text="推荐按这笔止损的 1/4、1/10 算：止损越宽可以越大（2 小时档 BTC 约 11bp / 4.5bp）",
        )
    )
    lo, hi = meeting_cost(profile)
    cost = f"${lo:.1f}" if hi == lo else f"${lo:.1f}–{hi:.1f}"
    if equity_usdt is None or equity_usdt <= 0:
        lines.append(
            MeaningLine(
                label="AI 开会费",
                text=f"{profile} 档每天约 {cost}（不含失败重试）；拿不到本金，算不出占比和推荐",
            )
        )
        return SettingsMeaning(lines=lines)
    share_lo, share_hi = lo / equity_usdt * 100, hi / equity_usdt * 100
    share = f"{share_lo:.3f}%" if hi == lo else f"{share_lo:.3f}–{share_hi:.3f}%"
    lines.append(
        MeaningLine(
            label="AI 开会费",
            text=f"{profile} 档每天约 {cost}，占本金 {equity_usdt:,.0f} U 的 {share} / 天"
            f"（建议不超过 {AI_SHARE_PCT:g}%）",
            warn=(lo + hi) / 2 > equity_usdt * AI_SHARE_PCT / 100,
        )
    )
    rec = recommended_meeting(equity_usdt)
    cap = max(MIN_DAILY_CAP_USD, equity_usdt * AI_SHARE_PCT / 100)
    if daily_cost_limit_usd is None:
        now_cap = ""
    elif daily_cost_limit_usd == 0:
        now_cap = "（现在不限，没有日上限）"
    else:
        now_cap = f"（现在 ${daily_cost_limit_usd:g}）"
    meet = f"开会 {rec} 档" if rec else "最慢的 24h 档也超过本金 0.05% / 天，建议只跑模拟盘或加本金"
    lines.append(
        MeaningLine(
            label="按本金推荐",
            text=f"{meet}；每日 AI 费用上限 ${cap:,.2f}{now_cap}。只显示，不改",
            warn=rec is None,
        )
    )
    return SettingsMeaning(lines=lines)
