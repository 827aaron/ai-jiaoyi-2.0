"""风控员：唯一能签发 ``OrderIntent`` 的地方。

四层决定权（设计文档 §四），下一层只能在上一层的范围里选：
1. 硬上限（``RiskConfig``，人定）：每笔风险、杠杆、总敞口、相关组敞口、日亏熔断、单笔名义、
   持仓笔数、价差、滑点、手续费门槛。
2. 作战卡（军师定）：方向、允许的信号、仓位档上限、名义价值范围、杠杆范围、止损 ATR 范围、止盈倍数。
3. 拍板人档位（Jev）：止损窄 / 中 / 宽 → 作战卡 ATR 范围的低 / 中 / 高；仓位 S / M / L。
4. 代码算数：止损距离 = 档位 ATR 倍数 × ATR（放到关键位外侧）；数量 = 风险 USDT ÷ 止损距离，
   再按所有上限封顶、按交易所精度向下取整。

故障即关闭：Jev 不可用、没有有效作战卡、没有盘口、没有交易所精度、任何数字算不出来 → 拒绝。
框架思路借鉴 buberlo/jev-trader 的 ``risk/limits.py``（MIT，提交 15d089e）：全部检查都跑完，
把每条不通过的原因都写下来，再给结论。日亏按天清零（它的版本不清零，这里修掉）。
"""

from __future__ import annotations

import json
import logging
import math
import os
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from jiaoyi.contracts import (
    ENTRY_CHOICES,
    ENTRY_ONE,
    AccountState,
    BattleCard,
    Bias,
    CandidateSignal,
    CardSymbol,
    EntryQuestion,
    FastLoopConfig,
    FeatureSnapshot,
    JevResponse,
    LevelKind,
    OrderIntent,
    Outcome,
    Range,
    RiskConfig,
    RiskVerdict,
    Side,
    SizeTier,
)
from jiaoyi.contracts.common import Category
from jiaoyi.core.recorder import RoleRecorder, local_day
from jiaoyi.core.us_hours import us_entry_window

logger = logging.getLogger(__name__)

_TIER_ORDER = {SizeTier.S: 0, SizeTier.M: 1, SizeTier.L: 2}
TOXIC_ALWAYS_BLOCK = 0.8
"""拍板只记不拦时，订单流有毒概率到这个数仍然拦。"""
ENTRY_ZONE_SLACK_R = 0.1
"""进场区间往交易方向最多放宽几个 R（研究：0.1R 内追进和区间内差不多）。"""


BANGKOK = ZoneInfo("Asia/Bangkok")


def session_factor(fast: FastLoopConfig, now: datetime) -> float:
    """入场时刻的曼谷时段系数：周一到周五查 ``weekday``，周六周日查 ``weekend``，取当前小时。"""
    if not fast.session_factor:
        return 1.0
    local = now.astimezone(BANGKOK)
    table = fast.session_factor_weekend if local.weekday() >= 5 else fast.session_factor_weekday
    return table[local.hour]


def blend_atr(short: float, long_: float | None, fast: FastLoopConfig) -> float:
    """短周期 ATR 和长周期 ATR 的几何平均；没配长周期或长周期没就绪时只用短周期（不含时段系数）。"""
    if fast.atr_long_period_min and long_ and long_ > 0:
        return math.sqrt(short * long_)
    return short


def entry_zone_miss(
    signal: CandidateSignal, card: BattleCard, fast: FastLoopConfig, now: datetime
) -> str | None:
    """触发价不在卡的进场区间里 → 返回「没到区间」的原因；在区间里、卡上没写区间、开关关了 → None。

    往交易方向放宽 ``ENTRY_ZONE_SLACK_R`` 个 R（做多可以比区间上沿高一点、做空可以比下沿低一点），
    R ≈ 卡上止损倍数的中值 × 止损 ATR（拍板前还不知道止损档，用中档估）。另一侧不放宽。"""
    return entry_zone_miss_at(
        signal.symbol, signal.side_hint, signal.trigger_price, signal.features, card, fast, now
    )


def entry_zone_miss_at(
    symbol: str,
    side_hint: Side,
    px: float,
    features: FeatureSnapshot,
    card: BattleCard,
    fast: FastLoopConfig,
    now: datetime,
) -> str | None:
    """同 ``entry_zone_miss``，按给定价格查：挂单没成交改市价追进时用此刻的价格再查一次。"""
    if not fast.entry_zone_only:
        return None
    cs = card.for_symbol(symbol)
    if cs is None or cs.entry_zone is None:
        return None
    z = cs.entry_zone
    r = (cs.stop_atr.min + cs.stop_atr.max) / 2.0 * stop_atr(features, fast, now, {})
    slack = ENTRY_ZONE_SLACK_R * max(r, 0.0)
    if side_hint == Side.LONG:
        lo, hi, side = z.min, z.max + slack, "多"
    else:
        lo, hi, side = z.min - slack, z.max, "空"
    if lo <= px <= hi:
        return None
    return (
        f"没到区间：触发价 {px:g}，卡的进场区间 {z.min:g}–{z.max:g}"
        f"（做{side}往交易方向最多放宽 {ENTRY_ZONE_SLACK_R:g}R，可接受 {lo:g}–{hi:g}）"
    )


def stop_atr(
    f: FeatureSnapshot, fast: FastLoopConfig, now: datetime, detail: dict[str, float | str]
) -> float:
    """止损用的 ATR = √(短周期 ATR × 长周期 ATR) × 曼谷时段系数，只在入场时算一次。

    短周期先按 7 天中位数 × ``atr_floor_7d`` 兜底（默认 0 = 不兜）；没配长周期、或长周期还没
    攒够 / 周期对不上时只用短周期。算的过程记进 ``detail``。
    """
    short = f.atr
    floor = fast.atr_floor_7d * (f.atr_median_7d or 0.0)
    if floor > short:
        detail.update(atr_now=round(short, 8), atr_floor_7d=round(floor, 8))
        short = floor
    detail["atr_short"] = round(short, 8)
    long_ = f.atr_long if f.atr_long_period_min == fast.atr_long_period_min else None
    atr = blend_atr(short, long_, fast)
    if fast.atr_long_period_min:
        if long_ and long_ > 0:
            detail["atr_long"] = round(long_, 8)
        else:
            detail["atr_long"] = "未就绪，只用短周期"
    k = session_factor(fast, now)
    detail["session_factor"] = k
    return atr * k


@dataclass(frozen=True)
class LotRules:
    """交易所对某个合约的下单精度（来自币安 exchangeInfo，由执行层提供）。"""

    step_size: float
    min_qty: float
    min_notional: float
    max_qty: float | None = None
    """市价单单笔最大数量；超过交易所会拒单，所以按它封顶。None = 不限。"""
    tick_size: float | None = None
    """价格最小变动（PRICE_FILTER.tickSize）；限价止盈取整用。None = 不知道。"""
    onboard_at: datetime | None = None
    """上线时间（exchangeInfo onboardDate）；None = 不知道（不挡）。"""
    delist_at: datetime | None = None
    """排了下架的时间（deliveryDate）；None = 没排。"""


DELIST_WINDOW = timedelta(days=7)
"""排了下架、离下架不到这么久就不开新仓（新信号研究 10-24「不做」过滤）。"""


class LotRulesProvider(Protocol):
    def lot_rules(self, symbol: str) -> LotRules | None: ...


class DayLossBreaker:
    """日亏熔断。按 ``day_reset_tz`` 的 0 点清零；当天一旦触发就锁到第二天，权益回升也不解锁。

    给了 ``state_path`` 就把（当天日期、日初权益、是否已触发）落盘，重启后读回，
    所以“熔断后重启”也照样锁到第二天。状态文件读不懂 → 当天按已触发处理（故障即关闭）。
    """

    def set_halt_pct(self, halt_pct: float) -> None:
        """配置热生效：新的日亏线，下一次权益检查起按新的算。"""
        self._halt = halt_pct

    def __init__(self, halt_pct: float, tz: str, state_path: Path | None = None) -> None:
        self._halt = halt_pct
        self._tz = tz
        self._path = state_path
        self._day: date | None = None
        self._start_equity: float | None = None
        self.tripped = False
        self._corrupt = False
        self._load()

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            d = json.loads(self._path.read_text(encoding="utf-8"))
            self._day = date.fromisoformat(d["day"])
            start = d.get("start_equity")
            self._start_equity = float(start) if start is not None else None
            self.tripped = bool(d["tripped"])
        except (ValueError, KeyError, TypeError, OSError):
            logger.exception("日亏熔断状态文件 %s 读不懂，今天按已熔断处理", self._path)
            self._corrupt = True

    def _save(self) -> None:
        if self._path is None or self._day is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "day": self._day.isoformat(),
                        "start_equity": self._start_equity,
                        "tripped": self.tripped,
                    }
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            logger.exception("写日亏熔断状态失败")

    def check(self, account: AccountState, now: datetime) -> tuple[bool, float]:
        """返回 (是否熔断, 今日盈亏百分比)。"""
        day = local_day(now, self._tz)
        if self._corrupt:
            self._corrupt = False
            self._day, self._start_equity, self.tripped = day, None, True
            self._save()
        if day != self._day:
            self._day = day
            self._start_equity = account.day_start_equity_usdt
            self.tripped = False
            self._save()
        start = self._start_equity or account.day_start_equity_usdt
        # 两个口径取更差的：账户给的日初权益和我们自己记下的日初权益
        pnl_pct = min(
            account.day_pnl_pct, (account.equity_usdt / start - 1.0) * 100.0 if start > 0 else 0.0
        )
        if pnl_pct <= -self._halt and not self.tripped:
            self.tripped = True
            self._save()
        return self.tripped, pnl_pct


@dataclass(frozen=True)
class _Stop:
    dist: float
    """止损距离，价格单位。"""
    pct: float
    """止损距离占入场价的百分比。"""


def _limit(
    fixed_bps: float | None, frac: float, stop_bps: float | None
) -> tuple[float, str] | None:
    """价差 / 滑点上限：止损 × 比例，设置里写了固定 bp 就取小。两样都没有返回 None（不查）。"""
    opts: list[tuple[float, str]] = []
    if stop_bps is not None:
        opts.append((frac * stop_bps, f"止损 {stop_bps:.0f}bp 的 {frac:g}"))
    if fixed_bps is not None:
        opts.append((fixed_bps, "你设的上限"))
    return min(opts) if opts else None


def _floor_step(qty: float, step: float) -> float:
    if step <= 0:
        return 0.0
    n = math.floor(qty / step + 1e-9)
    decimals = max(0, -math.floor(math.log10(step))) if step < 1 else 0
    return round(n * step, decimals)


class RiskGuardImpl:
    """``RiskGuard`` 的实现。"""

    def __init__(
        self,
        risk: RiskConfig,
        fast: FastLoopConfig,
        lots: LotRulesProvider,
        recorder: RoleRecorder | None = None,
        *,
        state_dir: str | Path | None = None,
        gate_record_only: bool = False,
        blocked: Callable[[], Mapping[str, str]] | None = None,
        adl_risk: Callable[[str], str | None] | None = None,
        live: bool = False,
        halted: Callable[[str], str | None] | None = None,
    ) -> None:
        """``gate_record_only``：拍板人概率没过门槛也放行，只记「拍板本会拦」（paper 校准用）。

        ``blocked``：被封的币 → 原因（下架公告等），拒单时把原因写全；
        ``adl_risk``：币 → 币安自动减仓风险 ``low`` / ``medium`` / ``high``
        （拿不到 None = 不挡）；``halted``：币 → 合约临时不在交易的原因（只挡新开仓）。"""
        self._halted = halted
        self._blocked = blocked
        self._adl = adl_risk
        self._live = live  # testnet / live：挂单等回踩还要 entry_limit_pullback_live
        self._risk = risk
        self._fast = fast
        self._record_only = gate_record_only
        self._lots = lots
        self._rec = recorder
        # 接线时传 AppConfig.data_dir，熔断状态写在 data/risk_state.json
        path = Path(state_dir) / "risk_state.json" if state_dir is not None else None
        self.breaker = DayLossBreaker(risk.daily_loss_halt_pct, risk.day_reset_tz, path)

    def set_record_only(self, record_only: bool) -> None:
        """配置热生效：拍板只记不拦的开关。"""
        self._record_only = record_only

    def review(
        self,
        signal: CandidateSignal,
        jev: JevResponse,
        card: BattleCard | None,
        account: AccountState,
        now: datetime,
        *,
        market: FeatureSnapshot | None,
    ) -> RiskVerdict:
        reasons: list[str] = []
        detail: dict[str, float | str] = {}
        intent: OrderIntent | None = None
        gate: list[str] = []  # 拍板人概率没过门槛的那几条（reasons 的子集）
        try:
            intent = self._review(signal, jev, card, account, now, market, reasons, detail, gate)
        except Exception as e:  # 算不出来就拒绝
            reasons.append(f"风控员计算出错：{type(e).__name__}: {e}")
            intent = None
        # 从信号触发到风控员审完用了多久（滑点主要看这段：拍板 + 排队 + 读账户）
        detail["since_trigger_ms"] = round((now - signal.ts).total_seconds() * 1000)
        if reasons and all(r in gate for r in reasons):
            detail["by"] = "gate"  # 只因为拍板人说不做：面板显示黄色「拍板放弃」，不算风控员拒绝
        if reasons or intent is None:
            verdict = RiskVerdict(
                signal_id=signal.signal_id, approved=False, reasons=reasons or ["未通过"]
            )
        else:
            verdict = RiskVerdict(signal_id=signal.signal_id, approved=True, intent=intent)
        self._log(now, signal, card, verdict, detail)
        return verdict

    # ------------------------------------------------------------------ 主流程

    def _review(
        self,
        signal: CandidateSignal,
        jev: JevResponse,
        card: BattleCard | None,
        account: AccountState,
        now: datetime,
        market: FeatureSnapshot | None,
        reasons: list[str],
        detail: dict[str, float | str],
        gate: list[str],
    ) -> OrderIntent | None:
        risk, fast = self._risk, self._fast

        # ---- 账户级
        halted, day_pnl = self.breaker.check(account, now)
        detail["day_pnl_pct"] = round(day_pnl, 3)
        if halted:
            reasons.append(
                f"今日亏损 {day_pnl:.2f}% 已触发熔断（{risk.daily_loss_halt_pct}%），只允许平仓"
            )
        if account.paused:
            reasons.append("面板已暂停开新仓")
        if account.open_positions >= risk.max_open_positions:
            reasons.append(f"持仓已有 {account.open_positions} 笔，达到上限")
        if signal.symbol in account.open_symbols:
            reasons.append(f"{signal.symbol} 已有在管持仓，不开第二笔（含反向）")
        if account.equity_usdt <= 0:
            reasons.append("账户权益为 0")
        if now - signal.ts > timedelta(seconds=fast.signal_max_age_s):
            age = (now - signal.ts).total_seconds()
            reasons.append(f"信号已过 {age:.1f} 秒，超过 {fast.signal_max_age_s} 秒作废")
        self._symbol_filters(signal.symbol, now, reasons)

        # ---- 拍板人：先看 ok，再读答案
        if not jev.ok:
            reasons.append(f"拍板人不可用：{jev.error}")
        else:
            hard, soft = self._check_jev(signal, jev, detail)
            reasons += hard
            if self._record_only:
                # 只记不拦：门槛没过照样放行，写进 detail；订单流明显有毒（≥ 0.8）还是拦
                if soft:
                    detail["gate_would_block"] = "；".join(soft)
                tox = jev.answer(EntryQuestion.TOXICITY.value)
                p_tox = tox.probs.get("high") if tox is not None else None
                if p_tox is not None and p_tox >= TOXIC_ALWAYS_BLOCK:
                    reasons.append(
                        f"订单流有毒概率 {p_tox:.2f} ≥ {TOXIC_ALWAYS_BLOCK}（只记不拦模式也拦）"
                    )
            else:
                gate += soft
                reasons += soft

        # ---- 作战卡
        cs = self._card_symbol(signal, card, now, reasons)

        # ---- 盘口：用拍板回来后重新取的行情，不用触发时那份（那份的最新价就是触发价）
        f = market
        if f is None:
            reasons.append("没有拍板后的最新行情，无法核对滑点和价差")
        elif f.symbol != signal.symbol:
            reasons.append(f"最新行情是 {f.symbol}，和信号 {signal.symbol} 不符")
            f = None
        elif f.atr_period_min != fast.atr_period_min:
            # 作战卡的止损倍数按配置的周期给，ATR 周期对不上止损会差好几倍
            reasons.append(
                f"行情 ATR 是 {f.atr_period_min} 分钟口径，配置要 {fast.atr_period_min} 分钟"
            )
        elif now - f.ts > timedelta(seconds=fast.signal_max_age_s):
            reasons.append(f"最新行情已过 {(now - f.ts).total_seconds():.1f} 秒")
        # ---- 止损距离先算出来，价差和滑点按它的比例卡（理由照样全收集，不早退）
        stop = (
            self._stop(signal, f, jev, cs, now, reasons, detail)
            if f is not None and cs is not None and jev.ok
            else None
        )
        if f is not None:
            stop_bps = stop.pct * 100.0 if stop is not None else None
            book = f.book
            if book is None:
                reasons.append("没有盘口数据")
            else:
                lim = _limit(risk.max_spread_bps, fast.max_spread_stop_frac, stop_bps)
                if lim is not None and book.spread_bps > lim[0]:
                    reasons.append(f"价差 {book.spread_bps:.1f}bp 超过 {lim[0]:.1f}bp（{lim[1]}）")
            # 只拦不利方向：做多价格比触发时高、做空价格比触发时低；往有利方向走了不拦
            sign = 1.0 if signal.side_hint == Side.LONG else -1.0
            slip = sign * (f.last_price / signal.trigger_price - 1.0) * 1e4
            detail["slippage_bps"] = round(slip, 2)  # 正数 = 不利
            lim = _limit(risk.max_slippage_bps, fast.max_slippage_stop_frac, stop_bps)
            if lim is not None and slip > lim[0]:
                reasons.append(
                    f"触发后价格往不利方向跑了 {slip:.1f}bp，"
                    f"超过滑点上限 {lim[0]:.1f}bp（{lim[1]}）"
                )

        if reasons or cs is None or not jev.ok or f is None or stop is None:
            return None
        return self._size(signal, f, jev, card, cs, stop, account, now, reasons, detail)

    def _check_jev(
        self,
        signal: CandidateSignal,
        jev: JevResponse,
        detail: dict[str, float | str] | None = None,
    ) -> tuple[list[str], list[str]]:
        """（回答不全 / 不对，概率没过门槛）。前者永远拦，后者「只记不拦」时只记。

        “做”概率和门槛比之前先按预测员挪一点（``predictor_weight``），原始值和挪后的值写进
        ``detail``。"""
        fast = self._fast
        out: list[str] = []
        # 契约不保证 ok=True 的回复答全了：按配置的问法，该答的题必须都在，选项必须一模一样。
        # 问法按配置定，不按回答猜（six 下漏答 genuine 要拦，不能当成一道题放过去）。
        got = {a.id: set(a.probs) for a in jev.answers}
        one = fast.gate_questions == "one"
        asked = ENTRY_ONE if one else tuple(ENTRY_CHOICES)
        for q in asked:
            choices = ENTRY_CHOICES[q]
            if q.value not in got:
                out.append(f"拍板人没回答“{q.value}”")
            elif got[q.value] != set(choices):
                out.append(f"拍板人“{q.value}”的选项 {sorted(got[q.value])} 不对")
        if out:
            return out, []

        def p(q: EntryQuestion, choice: str) -> float | None:
            a = jev.answer(q.value)
            return None if a is None else a.probs.get(choice)

        checks: tuple[tuple[EntryQuestion, str, float, str], ...] = (
            (EntryQuestion.TAKE, "take", fast.min_p_take, "做"),
        )
        if not one:
            checks = ((EntryQuestion.GENUINE, "real", fast.min_p_genuine, "真信号"), *checks)
        for q, choice, floor, cn in checks:
            v = p(q, choice)
            note = ""
            if v is not None and q == EntryQuestion.TAKE:
                raw, nudge = v, self._predictor_nudge(signal, detail)
                v = min(1.0, max(0.0, raw + nudge))
                if detail is not None and "predictor" in detail:
                    detail["take_raw"] = round(raw, 4)
                    detail["take_adj"] = round(v, 4)
                if nudge:
                    note = f"（预测员 {nudge:+.2f}）"
            if v is None:
                out.append(f"拍板人没回答“{q.value}”")
            elif v < floor:
                out.append(f"“{cn}”概率 {v:.2f}{note} 低于门槛 {floor}")
        if not one:
            tox = p(EntryQuestion.TOXICITY, "high")
            if tox is None:
                out.append("拍板人没回答“toxicity”")
            elif tox > fast.max_p_toxic:
                out.append(f"订单流有毒概率 {tox:.2f} 高于上限 {fast.max_p_toxic}")
            want, other = ("up", "down") if signal.side_hint == Side.LONG else ("down", "up")
            pw, po = p(EntryQuestion.DIRECTION, want), p(EntryQuestion.DIRECTION, other)
            if pw is None or po is None:
                out.append("拍板人没回答“direction”")
            elif pw < fast.min_p_direction or pw <= po:
                out.append(f"方向 {want} 概率 {pw:.2f} 不够（反方向 {po:.2f}）")
        hard = []
        for q in (EntryQuestion.STOP_WIDTH, EntryQuestion.SIZE):
            if jev.answer(q.value) is None:
                hard.append(f"拍板人没回答“{q.value}”")
        missing = [r for r in out if r.startswith("拍板人没回答")]
        return hard + missing, [r for r in out if r not in missing]

    def _predictor_nudge(
        self, signal: CandidateSignal, detail: dict[str, float | str] | None
    ) -> float:
        """预测员给“做”概率加多少（官方 Kronos 用法，Aaron 10-24「直接改成官方的」）：

        同一时刻所有币按预测涨幅（10 条路径平均后的终点）排名，做多信号排在前段、做空信号排在
        后段（``[predictor] rank_band``，默认各 1/6）就 + 权重 × 0.5，其余不加也不扣（「预测员和
        卡片一致就加分。相反就不扣」；信号方向就是卡的方向）。看坏那一头给做空用是我们的镜像，
        官方只做多。预测员关掉、过期、币太少排不出名次、或权重 0 时不加。"""
        weight = self._fast.predictor_weight
        fc = signal.forecast
        if weight <= 0:
            return 0.0
        if fc is None or fc.stale_after <= signal.ts:
            if detail is not None:
                detail["predictor"] = "没有预测"
            return 0.0
        if fc.rank_view is None or fc.rank_pct is None:
            if detail is not None:
                detail["predictor"] = "排不出名次（一起预测的币太少）"
            return 0.0
        long = signal.side_hint == Side.LONG
        agrees = fc.rank_view == ("top" if long else "bottom")
        nudge = weight * 0.5 if agrees else 0.0
        if detail is not None:
            view = {"top": "看好", "bottom": "看坏", "mid": "不表态"}[fc.rank_view]
            agree = "同向" if agrees else "不同向，不扣分"
            n = fc.rank_n or 0
            place = round((1.0 - fc.rank_pct) * (n - 1)) + 1 if n > 1 else 1
            detail["predictor"] = f"{agree}（{view}，{n} 个币里排第 {place}）"
            detail["predictor_rank_pct"] = round(fc.rank_pct, 4)
            if fc.score is not None:
                detail["predictor_score_bps"] = round(fc.score * 1e4, 1)
            detail["predictor_nudge"] = round(nudge, 4)
        return nudge

    def recheck_chase(
        self, intent: OrderIntent, card: BattleCard | None, account: AccountState, now: datetime
    ) -> list[str]:
        """挂单等回踩没成交、要改市价追进前再审一遍（批准是最多一小时前给的）。

        查此刻的熔断、暂停、持仓笔数、同币持仓、封币 / 临时停 / 要下架、作战卡（有效、还有这个币、
        方向没反、没改成只看不做）和总 / 相关组敞口。返回拦下的理由，空 = 可以追。"""
        risk, reasons = self._risk, []
        sym = intent.symbol
        halted, day_pnl = self.breaker.check(account, now)
        if halted:
            reasons.append(f"今日亏损 {day_pnl:.2f}% 已触发熔断，只允许平仓")
        if account.paused:
            reasons.append("面板已暂停开新仓")
        if account.open_positions >= risk.max_open_positions:
            reasons.append(f"持仓已有 {account.open_positions} 笔，达到上限")
        if sym in account.open_symbols:
            reasons.append(f"{sym} 已有在管持仓")
        self._symbol_filters(sym, now, reasons)
        cs = card.for_symbol(sym) if card is not None and card.is_valid(now) else None
        why_blocked = self._blocked().get(sym) if self._blocked is not None else None
        if cs is None:
            reasons.append("作战卡已过期或没有这个币了")
        elif why_blocked:
            reasons.append(f"{why_blocked}，不开新仓")
        elif cs.bias == Bias.NO_TRADE:
            reasons.append("作战卡已改成只看不做")
        elif cs.bias.value not in ("both", intent.side.value):
            reasons.append(f"作战卡方向已改成 {cs.bias.value}")
        equity = account.equity_usdt
        if account.open_notional_usdt + intent.notional_usdt > risk.max_total_exposure_x * equity:
            reasons.append("加上这笔会超过总敞口上限")
        if cs is not None:
            used = account.open_notional_by_category.get(cs.category, 0.0)
            if used + intent.notional_usdt > risk.category_exposure_x[cs.category] * equity:
                reasons.append("加上这笔会超过这一类币的合计仓位上限")
        return reasons

    def _symbol_filters(self, sym: str, now: datetime, reasons: list[str]) -> None:
        """「不做」过滤：要下架、刚上线、自动减仓风险高。数据拿不到就不挡。"""
        fast = self._fast
        why_halted = self._halted(sym) if self._halted is not None else None
        if why_halted:
            reasons.append(f"{why_halted}，先不开新仓（恢复交易后自动放开）")
        lot = self._lots.lot_rules(sym)
        if lot is not None and lot.delist_at is not None and lot.delist_at - now <= DELIST_WINDOW:
            reasons.append(
                f"币安合约信息：{sym} 将于 {lot.delist_at:%m-%d %H:%M} UTC 下架，不开新仓"
            )
        hours = fast.new_listing_block_h
        if hours > 0 and lot is not None and lot.onboard_at is not None:
            age_h = (now - lot.onboard_at).total_seconds() / 3600.0
            if age_h < hours:
                reasons.append(
                    f"{sym} 上线不到 {hours:g} 小时（{age_h:.0f} 小时），价格还不稳，不开新仓"
                )
        if fast.adl_high_block and self._adl is not None and self._adl(sym) == "high":
            reasons.append(
                "这个币被自动减仓的风险是高（币安每 30 分钟更新），赚钱的仓可能被强制减掉，不开新仓"
            )

    def _card_symbol(
        self, signal: CandidateSignal, card: BattleCard | None, now: datetime, reasons: list[str]
    ) -> CardSymbol | None:
        if card is None or not card.is_valid(now):
            reasons.append("没有有效作战卡，不开新仓")
            return None
        # 同一天 card_id 不变、刷新只加版本号，所以两个都要比
        if (signal.card_id, signal.card_version) != (card.card_id, card.version):
            reasons.append(
                f"信号触发时的作战卡（{signal.card_id} v{signal.card_version}）已换成 "
                f"{card.card_id} v{card.version}"
            )
        cs = card.for_symbol(signal.symbol)
        if cs is None:
            reasons.append("作战卡里没有这个标的")
            return None
        f_ = self._fast
        if (
            f_.us_hours_gate
            and cs.category in (Category.US_TECH, Category.CRYPTO_STOCK)
            and not us_entry_window(now, f_.us_open_delay_min, f_.us_close_lead_min)
        ):
            reasons.append(
                f"美股休市或不在开仓时段（开盘后 {f_.us_open_delay_min} 分钟到收盘前 "
                f"{f_.us_close_lead_min} 分钟），{signal.symbol} 不开新仓"
            )
        why_blocked = self._blocked().get(signal.symbol) if self._blocked is not None else None
        if why_blocked:
            reasons.append(f"{why_blocked}，不开新仓")  # 封币的理由写全，别只写「只看不做」
        elif cs.bias == Bias.NO_TRADE:
            reasons.append("作战卡：这个标的只看不做")
        elif cs.bias.value != "both" and cs.bias.value != signal.side_hint.value:
            reasons.append(f"方向 {signal.side_hint.value} 和作战卡 {cs.bias.value} 相反")
        if signal.kind not in cs.allowed_signals:
            reasons.append(f"作战卡不允许 {signal.kind.value} 信号")
        fast = self._fast
        upd: dict[str, Range] = {}
        for name, rng, lo, hi, what, unit in (
            ("stop_atr", cs.stop_atr, fast.min_stop_atr, fast.max_stop_atr, "止损", " ATR "),
            ("take_profit_r", cs.take_profit_r, fast.min_tp_r, fast.max_tp_r, "止盈", " 倍"),
        ):
            if lo <= rng.min and rng.max <= hi:
                continue
            a, b = max(rng.min, lo), min(rng.max, hi)
            if fast.card_range_clamp and a <= b:
                upd[name] = Range(min=a, max=b)  # 越界的那截不要，剩下的照用
                continue
            empty = "（没有交集）" if fast.card_range_clamp else ""
            reasons.append(f"作战卡{what} {rng.min}–{rng.max}{unit}超出 {lo}–{hi}{empty}")
        return cs.model_copy(update=upd) if upd else cs

    def _stop(
        self,
        signal: CandidateSignal,
        f: FeatureSnapshot,
        jev: JevResponse,
        cs: CardSymbol,
        now: datetime,
        reasons: list[str],
        detail: dict[str, float | str],
    ) -> _Stop | None:
        """止损：档位 → 作战卡 ATR 范围 → 放到关键位外侧。回答不全时返回 None（理由别处已记）。"""
        stop_ans = jev.answer(EntryQuestion.STOP_WIDTH.value)
        if stop_ans is None:
            return None
        atr = stop_atr(f, self._fast, now, detail)
        if atr <= 0:
            reasons.append("ATR 为 0，算不出止损")
            return None
        stop_tier = stop_ans.top()[0]
        lo, hi = cs.stop_atr.min, cs.stop_atr.max
        mult = {"narrow": lo, "mid": (lo + hi) / 2.0, "wide": hi}.get(stop_tier, 0.0)
        if mult <= 0:
            reasons.append("作战卡止损 ATR 倍数为 0")
            return None
        entry = f.last_price
        dist = mult * atr
        if self._fast.stop_level_snap:
            dist = self._beyond_level(cs, signal.side_hint, entry, dist, hi * atr, atr)
        pct = dist / entry * 100.0
        detail.update(stop_tier=stop_tier, stop_atr=round(dist / atr, 3), stop_pct=round(pct, 4))
        return _Stop(dist=dist, pct=pct)

    def _book_cap(self, f: FeatureSnapshot, side: Side) -> float | None:
        """吃单那一侧前 5 档挂单额 × ``max_notional_book_frac``；关了或没有盘口返回 None。"""
        frac = self._fast.max_notional_book_frac
        if frac <= 0 or f.book is None:
            return None
        b = f.book
        depth = b.ask_depth_usdt if side is Side.LONG else b.bid_depth_usdt
        if depth is None:
            depth = b.depth_usdt / 2.0  # 旧快照没分两侧：按一半估
        return depth * frac

    def _size(
        self,
        signal: CandidateSignal,
        f: FeatureSnapshot,
        jev: JevResponse,
        card: BattleCard | None,
        cs: CardSymbol,
        stop: _Stop,
        account: AccountState,
        now: datetime,
        reasons: list[str],
        detail: dict[str, float | str],
    ) -> OrderIntent | None:
        risk, fast = self._risk, self._fast
        entry = f.last_price
        side = signal.side_hint
        size_ans = jev.answer(EntryQuestion.SIZE.value)
        assert size_ans is not None
        dist, stop_pct = stop.dist, stop.pct

        stop_floor_bps = self._fast.min_stop_fee_multiple * 2.0 * risk.taker_fee_bps
        if stop_pct * 100.0 < stop_floor_bps:
            if not fast.stop_floor_widen:
                reasons.append(
                    f"止损 {stop_pct * 100:.1f}bp 离手续费太近（至少 {stop_floor_bps:.1f}bp）"
                )
                return None
            # 拉宽到下限；下面的仓位、止盈、救生艇、移动止损都按新止损算（每笔风险金额不变）
            detail["stop_widened"] = (
                f"止损从 {stop_pct * 100:.1f}bp 拉宽到 {stop_floor_bps:.1f}bp，仓位按新止损重算"
            )
            stop_pct = stop_floor_bps / 100.0
            dist = entry * stop_pct / 100.0
            detail["stop_pct"] = round(stop_pct, 4)
            detail["stop_atr"] = round(dist / stop.dist * float(detail.get("stop_atr", 0.0)), 3)
        tp_pct = stop_pct * cs.take_profit_r.min

        fee_floor_bps = risk.min_tp_fee_multiple * 2.0 * risk.taker_fee_bps
        if tp_pct * 100.0 < fee_floor_bps:
            reasons.append(f"止盈 {tp_pct * 100:.1f}bp 不够付手续费（至少 {fee_floor_bps:.1f}bp）")
            return None

        if self._fast.funding_against_max_bp > 0:
            rate = f.values.get("funding_rate")  # 拿不到费率就不拦：资金费只是成本，不是风险
            paying = rate is not None and (rate > 0) == (side == Side.LONG) and rate != 0
            if paying and rate is not None and abs(rate) * 1e4 > self._fast.funding_against_max_bp:
                reasons.append(
                    f"开仓方向每次要付资金费 {abs(rate) * 1e4:.1f}bp，"
                    f"超过 {self._fast.funding_against_max_bp:g}bp"
                )
                return None

        # ---- 仓位：档位封顶到作战卡，风险 USDT ÷ 止损距离
        tier = SizeTier(size_ans.top()[0])
        if _TIER_ORDER[tier] > _TIER_ORDER[cs.size_tier_max]:
            tier = cs.size_tier_max
        frac = {
            SizeTier.S: fast.size_fraction_s,
            SizeTier.M: fast.size_fraction_m,
            SizeTier.L: 1.0,
        }[tier]
        equity = account.equity_usdt
        risk_budget = equity * risk.risk_per_trade_pct / 100.0 * frac
        notional = risk_budget / dist * entry

        # ---- 杠杆：作战卡约束时和硬上限取小；不约束（默认）就用硬上限，卡上的只当建议记下
        binding_card = fast.card_caps_binding
        if binding_card:
            lev = math.floor(min(cs.leverage.max, float(risk.max_leverage)))
            if lev < 1 or lev < cs.leverage.min:
                reasons.append(
                    f"作战卡杠杆 {cs.leverage.min}–{cs.leverage.max} 超出硬上限 {risk.max_leverage}"
                )
                return None
        else:
            lev = max(1, math.floor(float(risk.max_leverage)))
            detail.update(
                card_notional_min=cs.notional_usdt.min,
                card_notional_max=cs.notional_usdt.max,
                card_leverage_max=cs.leverage.max,
            )

        caps = {
            **({"作战卡名义上限": cs.notional_usdt.max} if binding_card else {}),
            "单笔名义上限": risk.notional_cap(equity),
            "总敞口余量": risk.max_total_exposure_x * equity - account.open_notional_usdt,
            "相关组敞口余量": risk.category_exposure_x[cs.category] * equity
            - account.open_notional_by_category.get(cs.category, 0.0),
            # hummingbot 下单前按「名义 ÷ 杠杆 + 开仓手续费」核保证金，不够就整笔不下
            # （BTC 那几笔：要 2,871U，可用 2,852U）。按同样的口径算，再留一点余量
            "可用保证金": account.available_usdt
            * (1.0 - fast.margin_safety_pct / 100.0)
            / (1.0 / lev + risk.taker_fee_bps / 1e4),
        }
        book_cap = self._book_cap(f, side)
        if book_cap is not None:
            caps["盘口深度"] = book_cap
            detail["book_cap_usdt"] = round(book_cap, 2)
        binding = "风险金额"
        for name, cap in caps.items():
            if cap < notional:
                notional, binding = cap, name
        detail.update(size_tier=tier.value, leverage=lev, binding=binding)
        if notional <= 0:
            reasons.append(f"{binding}已用完")
            return None
        if binding_card and notional < cs.notional_usdt.min:
            reasons.append(
                f"按{binding}只能开 {notional:.0f}U，低于作战卡下限 {cs.notional_usdt.min:.0f}U"
            )
            return None

        lot = self._lots.lot_rules(signal.symbol)
        if lot is None:
            reasons.append("没有这个合约的交易所精度，拒绝下单")
            return None
        qty = _floor_step(notional / entry, lot.step_size)
        if lot.max_qty is not None and qty > lot.max_qty:
            qty = _floor_step(lot.max_qty, lot.step_size)
            detail["capped_by_max_qty"] = qty
        if qty < lot.min_qty or qty <= 0:
            reasons.append(f"数量 {qty} 低于交易所最小下单量 {lot.min_qty}")
            return None
        notional = qty * entry
        if notional < lot.min_notional:
            reasons.append(f"名义价值 {notional:.2f}U 低于交易所最小 {lot.min_notional}U")
            return None
        if binding_card and notional < cs.notional_usdt.min:
            reasons.append(f"取整后 {notional:.0f}U 低于作战卡下限 {cs.notional_usdt.min:.0f}U")
            return None
        risk_usdt = qty * dist
        detail.update(qty=qty, notional=round(notional, 2), risk_usdt=round(risk_usdt, 4))

        # ---- 总持仓风险：全部按保命止损算，加上这笔不能超过当天还能亏的额度，否则几笔一起打到
        # 保命止损会穿过日亏熔断线。只扣已实现亏损：浮亏已经算在各笔的保命止损风险里，不重复扣。
        lifeboat_pct = stop_pct * risk.lifeboat_stop_multiplier
        new_risk = notional * lifeboat_pct / 100.0
        budget = risk.daily_loss_halt_pct / 100.0 * account.day_start_equity_usdt - max(
            0.0, -account.day_realized_pnl_usdt
        )
        detail.update(
            open_risk_usdt=round(account.open_risk_usdt, 2),
            new_lifeboat_risk_usdt=round(new_risk, 2),
            day_risk_budget_usdt=round(budget, 2),
        )
        if account.open_risk_usdt + new_risk > budget + 1e-9:
            reasons.append(
                f"总风险 {account.open_risk_usdt:.2f}U + 本笔 {new_risk:.2f}U "
                f"超过当日剩余额度 {max(budget, 0.0):.2f}U"
            )
            return None

        assert card is not None
        limit = self._pullback_price(side, entry, dist, lot)
        if limit is not None:
            detail["limit_price"] = limit
        trail_act, trail_delta, trail_note = self._trailing(stop_pct)
        if trail_note:
            detail["trailing"] = trail_note
        return OrderIntent(
            order_type="limit" if limit is not None else "market",
            limit_price=limit,
            limit_wait_s=fast.entry_limit_wait_min * 60 if limit is not None else None,
            limit_chase=limit is not None and fast.entry_limit_chase,
            intent_id=f"int-{uuid.uuid4().hex[:16]}",
            signal_id=signal.signal_id,
            card_id=card.card_id,
            symbol=signal.symbol,
            category=cs.category,
            side=side,
            quantity=qty,
            notional_usdt=notional,
            leverage=lev,
            risk_usdt=risk_usdt,
            stop_loss_pct=stop_pct,
            take_profit_pct=tp_pct,
            time_limit_s=fast.time_limit_s,
            lifeboat_stop_pct=lifeboat_pct,
            trailing_activation_pct=trail_act,
            trailing_stop_pct=trail_delta,
            approved_at=now,
        )

    def _trailing(self, stop_pct: float) -> tuple[float | None, float | None, str]:
        """保本 / 移动止损（持仓管家的开关，默认关）→ hummingbot trailing_stop（启动线, 回撤距离）。

        单位都是价格百分比、按扣过开仓手续费的盈亏算（和执行器止损同口径）。hummingbot 盈利过启动线
        后把止损线定在「当时盈利 − 回撤距离」，之后只往上挪不往下放。``fee`` = 平仓手续费：止损线
        在它上面，平掉时开平两次手续费扣完不亏。
        - 只开移动：启动线 = max(开始 R, 移动距离 + fee)，回撤距离 = 移动距离
          （启动那一刻止损线不低于保本价，之后跟着最高盈利收紧）；
        - 只开保本：启动线 = 保本 R，回撤距离 = 启动线 − fee（启动那一刻止损在保本价）；
        - 都开：启动线 = 保本 R，回撤距离 = min(移动距离, 启动线 − fee)。
        算不出（距离 ≤ 0）就不加，理由写进记录。"""
        fast = self._fast
        if not (fast.breakeven_on or fast.trailing_on):
            return None, None, ""
        fee = self._risk.taker_fee_bps / 100.0
        trail = fast.trail_distance_r * stop_pct
        if fast.breakeven_on:
            act = fast.breakeven_trigger_r * stop_pct
            delta = act - fee
            if fast.trailing_on:
                delta = min(delta, trail)
        else:
            act, delta = max(fast.trail_start_r * stop_pct, trail + fee), trail
        if delta <= 0:
            return None, None, f"保本启动线 {act:.3f}% 不到手续费 {fee:.3f}%，这笔不加保本"
        if fast.breakeven_on and fast.trailing_on:
            what = "保本 + 移动止损"
        else:
            what = "保本" if fast.breakeven_on else "移动止损"
        note = f"{what}：赚到 {act:.2f}% 起止损不低于保本价，之后离最高盈利 {delta:.2f}% 跟着收紧"
        return round(act, 6), round(delta, 6), note

    def _pullback_price(self, side: Side, entry: float, dist: float, lot: LotRules) -> float | None:
        """挂单等回踩的挂单价：触发价往有利方向让 ``entry_limit_offset_stop`` × 止损距离，
        按最小跳动价取整（买单往下、卖单往上）。没开 / 让 0 / 算不出来 → None（市价进）。"""
        fast = self._fast
        if not fast.entry_limit_pullback or (self._live and not fast.entry_limit_pullback_live):
            return None
        off = fast.entry_limit_offset_stop * dist
        if off <= 0:
            return None
        raw = entry - off if side == Side.LONG else entry + off
        tick = lot.tick_size
        if tick:
            steps = raw / tick
            raw = (
                math.floor(steps + 1e-9) if side == Side.LONG else math.ceil(steps - 1e-9)
            ) * tick
            raw = round(raw, 12)
        return raw if raw > 0 else None

    def _beyond_level(
        self, cs: CardSymbol, side: Side, entry: float, dist: float, max_dist: float, atr: float
    ) -> float:
        """止损线外侧半个 ATR 内有关键位（多单看支撑，空单看压力），就把止损挪到关键位外侧。

        关键位在止损线以内的不用管（止损已经在它外面）；挪动后不超过作战卡 ATR 上限。
        数量按挪动后的距离算，所以每笔风险金额不变。
        """
        buf = self._fast.stop_level_buffer_atr * atr
        reach = min(max_dist, dist + 0.5 * atr)
        needs: list[float] = []
        for lv in cs.key_levels:
            if side == Side.LONG and lv.price < entry and lv.kind != LevelKind.RESISTANCE:
                needs.append(entry - (lv.price - buf))
            elif side == Side.SHORT and lv.price > entry and lv.kind != LevelKind.SUPPORT:
                needs.append((lv.price + buf) - entry)
        beyond = [n for n in needs if dist < n <= reach]
        return min(beyond) if beyond else dist

    def _log(
        self,
        now: datetime,
        signal: CandidateSignal,
        card: BattleCard | None,
        verdict: RiskVerdict,
        detail: dict[str, float | str],
    ) -> None:
        if self._rec is None:
            return
        side = "多" if signal.side_hint == Side.LONG else "空"
        if verdict.approved and verdict.intent is not None:
            it = verdict.intent
            summary = (
                f"{signal.symbol} {side} 放行：{it.quantity} 张 ≈{it.notional_usdt:.0f}U，"
                f"{it.leverage}x，止损 {it.stop_loss_pct:.2f}% 止盈 {it.take_profit_pct:.2f}%，"
                f"风险 {it.risk_usdt:.2f}U"
            )
            if trail := detail.get("trailing"):
                summary += f"；{str(trail).split('：')[0]}开"
            if would := detail.get("gate_would_block"):
                summary += f"（拍板本会拦，只记不拦放行：{str(would).split('；')[0]}）"
            outcome = Outcome.PASS
        else:
            what = "拍板放弃" if detail.get("by") == "gate" else "拒绝"
            more = f"（共 {len(verdict.reasons)} 条）" if len(verdict.reasons) > 1 else ""
            summary = f"{signal.symbol} {side} {what}：{verdict.reasons[0]}{more}"
            outcome = Outcome.REJECT
        self._rec.record(
            now,
            outcome,
            summary,
            symbol=signal.symbol,
            signal_id=signal.signal_id,
            card_id=card.card_id if card is not None else None,
            input={
                "trigger_price": signal.trigger_price,
                "kind": signal.kind.value,
                "side": signal.side_hint.value,
            },
            # 放行时 output 顶层就是 OrderIntent 的字段（验收脚本 jiaoyi.acceptance 按这个读），
            # 计算过程放在 detail 里；拒绝时写原因
            output=(
                {**verdict.intent.model_dump(mode="json"), "detail": detail}
                if verdict.intent is not None
                else {"approved": False, "reasons": verdict.reasons, "detail": detail}
            ),
        )
