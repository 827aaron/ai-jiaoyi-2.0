"""保镖：唯一能签发 ``OrderIntent`` 的地方。

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
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from jiaoyi.contracts import (
    ENTRY_CHOICES,
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
    RiskConfig,
    RiskVerdict,
    Side,
    SizeTier,
)
from jiaoyi.core.recorder import RoleRecorder, local_day

logger = logging.getLogger(__name__)

_TIER_ORDER = {SizeTier.S: 0, SizeTier.M: 1, SizeTier.L: 2}


BANGKOK = ZoneInfo("Asia/Bangkok")


def session_factor(fast: FastLoopConfig, now: datetime) -> float:
    """入场时刻的曼谷时段系数：周一到周五查 ``weekday``，周六周日查 ``weekend``，取当前小时。"""
    if not fast.session_factor:
        return 1.0
    local = now.astimezone(BANGKOK)
    table = fast.session_factor_weekend if local.weekday() >= 5 else fast.session_factor_weekday
    return table[local.hour]


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
    atr = short
    detail["atr_short"] = round(short, 8)
    if fast.atr_long_period_min:
        long_ = f.atr_long
        if long_ and long_ > 0 and f.atr_long_period_min == fast.atr_long_period_min:
            atr = math.sqrt(short * long_)
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


class LotRulesProvider(Protocol):
    def lot_rules(self, symbol: str) -> LotRules | None: ...


class DayLossBreaker:
    """日亏熔断。按 ``day_reset_tz`` 的 0 点清零；当天一旦触发就锁到第二天，权益回升也不解锁。

    给了 ``state_path`` 就把（当天日期、日初权益、是否已触发）落盘，重启后读回，
    所以“熔断后重启”也照样锁到第二天。状态文件读不懂 → 当天按已触发处理（故障即关闭）。
    """

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
    ) -> None:
        self._risk = risk
        self._fast = fast
        self._lots = lots
        self._rec = recorder
        # 接线时传 AppConfig.data_dir，熔断状态写在 data/risk_state.json
        path = Path(state_dir) / "risk_state.json" if state_dir is not None else None
        self.breaker = DayLossBreaker(risk.daily_loss_halt_pct, risk.day_reset_tz, path)

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
        try:
            intent = self._review(signal, jev, card, account, now, market, reasons, detail)
        except Exception as e:  # 算不出来就拒绝
            reasons.append(f"保镖计算出错：{type(e).__name__}: {e}")
            intent = None
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

        # ---- 拍板人：先看 ok，再读答案
        if not jev.ok:
            reasons.append(f"拍板人不可用：{jev.error}")
        else:
            reasons += self._check_jev(signal, jev)

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
        if f is not None:
            book = f.book
            if book is None:
                reasons.append("没有盘口数据")
            elif book.spread_bps > risk.max_spread_bps:
                reasons.append(f"价差 {book.spread_bps:.1f}bp 超过 {risk.max_spread_bps}bp")
            slip = abs(f.last_price / signal.trigger_price - 1.0) * 1e4
            detail["slippage_bps"] = round(slip, 2)
            if slip > risk.max_slippage_bps:
                reasons.append(
                    f"触发后价格已跑 {slip:.1f}bp，超过滑点上限 {risk.max_slippage_bps}bp"
                )

        if reasons or cs is None or not jev.ok or f is None:
            return None
        return self._size(signal, f, jev, card, cs, account, now, reasons, detail)

    def _check_jev(self, signal: CandidateSignal, jev: JevResponse) -> list[str]:
        fast = self._fast
        out: list[str] = []
        # 契约不保证 ok=True 的回复答全了：6 问必须都在，选项必须和问的一模一样
        got = {a.id: set(a.probs) for a in jev.answers}
        for q, choices in ENTRY_CHOICES.items():
            if q.value not in got:
                out.append(f"拍板人没回答“{q.value}”")
            elif got[q.value] != set(choices):
                out.append(f"拍板人“{q.value}”的选项 {sorted(got[q.value])} 不对")
        if out:
            return out

        def p(q: EntryQuestion, choice: str) -> float | None:
            a = jev.answer(q.value)
            return None if a is None else a.probs.get(choice)

        checks = (
            (EntryQuestion.GENUINE, "real", fast.min_p_genuine, "真信号"),
            (EntryQuestion.TAKE, "take", fast.min_p_take, "做"),
        )
        for q, choice, floor, cn in checks:
            v = p(q, choice)
            if v is None:
                out.append(f"拍板人没回答“{q.value}”")
            elif v < floor:
                out.append(f"“{cn}”概率 {v:.2f} 低于门槛 {floor}")
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
        for q in (EntryQuestion.STOP_WIDTH, EntryQuestion.SIZE):
            if jev.answer(q.value) is None:
                out.append(f"拍板人没回答“{q.value}”")
        return out

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
        if cs.bias == Bias.NO_TRADE:
            reasons.append("作战卡：这个标的只看不做")
        elif cs.bias.value != "both" and cs.bias.value != signal.side_hint.value:
            reasons.append(f"方向 {signal.side_hint.value} 和作战卡 {cs.bias.value} 相反")
        if signal.kind not in cs.allowed_signals:
            reasons.append(f"作战卡不允许 {signal.kind.value} 信号")
        fast = self._fast
        if cs.stop_atr.min < fast.min_stop_atr or cs.stop_atr.max > fast.max_stop_atr:
            reasons.append(
                f"作战卡止损 {cs.stop_atr.min}–{cs.stop_atr.max} ATR 超出 "
                f"{fast.min_stop_atr}–{fast.max_stop_atr}"
            )
        if cs.take_profit_r.min < fast.min_tp_r or cs.take_profit_r.max > fast.max_tp_r:
            reasons.append(
                f"作战卡止盈 {cs.take_profit_r.min}–{cs.take_profit_r.max} 倍超出 "
                f"{fast.min_tp_r}–{fast.max_tp_r}"
            )
        return cs

    def _size(
        self,
        signal: CandidateSignal,
        f: FeatureSnapshot,
        jev: JevResponse,
        card: BattleCard | None,
        cs: CardSymbol,
        account: AccountState,
        now: datetime,
        reasons: list[str],
        detail: dict[str, float | str],
    ) -> OrderIntent | None:
        risk, fast = self._risk, self._fast
        entry = f.last_price
        side = signal.side_hint
        atr = stop_atr(f, fast, now, detail)
        if atr <= 0:
            reasons.append("ATR 为 0，算不出止损")
            return None

        # ---- 止损：档位 → 作战卡 ATR 范围 → 放到关键位外侧
        stop_ans = jev.answer(EntryQuestion.STOP_WIDTH.value)
        size_ans = jev.answer(EntryQuestion.SIZE.value)
        assert stop_ans is not None and size_ans is not None
        stop_tier = stop_ans.top()[0]
        lo, hi = cs.stop_atr.min, cs.stop_atr.max
        mult = {"narrow": lo, "mid": (lo + hi) / 2.0, "wide": hi}[stop_tier]
        if mult <= 0:
            reasons.append("作战卡止损 ATR 倍数为 0")
            return None
        dist = mult * atr
        if fast.stop_level_snap:
            dist = self._beyond_level(cs, side, entry, dist, hi * atr, atr)
        stop_pct = dist / entry * 100.0
        tp_pct = stop_pct * cs.take_profit_r.min
        detail.update(
            stop_tier=stop_tier, stop_atr=round(dist / atr, 3), stop_pct=round(stop_pct, 4)
        )

        fee_floor_bps = risk.min_tp_fee_multiple * 2.0 * risk.taker_fee_bps
        if tp_pct * 100.0 < fee_floor_bps:
            reasons.append(f"止盈 {tp_pct * 100:.1f}bp 不够付手续费（至少 {fee_floor_bps:.1f}bp）")
            return None

        stop_floor_bps = self._fast.min_stop_fee_multiple * 2.0 * risk.taker_fee_bps
        if stop_pct * 100.0 < stop_floor_bps:
            reasons.append(
                f"止损 {stop_pct * 100:.1f}bp 离手续费太近（至少 {stop_floor_bps:.1f}bp）"
            )
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

        # ---- 杠杆：作战卡上限和硬上限取小
        lev = math.floor(min(cs.leverage.max, float(risk.max_leverage)))
        if lev < 1 or lev < cs.leverage.min:
            reasons.append(
                f"作战卡杠杆 {cs.leverage.min}–{cs.leverage.max} 超出硬上限 {risk.max_leverage}"
            )
            return None

        caps = {
            "作战卡名义上限": cs.notional_usdt.max,
            "单笔名义上限": risk.notional_cap(equity),
            "总敞口余量": risk.max_total_exposure_x * equity - account.open_notional_usdt,
            "相关组敞口余量": risk.category_exposure_x[cs.category] * equity
            - account.open_notional_by_category.get(cs.category, 0.0),
            "可用保证金": account.available_usdt * lev,
        }
        binding = "风险金额"
        for name, cap in caps.items():
            if cap < notional:
                notional, binding = cap, name
        detail.update(size_tier=tier.value, leverage=lev, binding=binding)
        if notional <= 0:
            reasons.append(f"{binding}已用完")
            return None
        if notional < cs.notional_usdt.min:
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
        if notional < cs.notional_usdt.min:
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
        return OrderIntent(
            intent_id=f"int-{uuid.uuid4().hex[:16]}",
            signal_id=signal.signal_id,
            card_id=card.card_id,
            symbol=signal.symbol,
            category=cs.category,
            side=side,
            order_type="market",
            quantity=qty,
            notional_usdt=notional,
            leverage=lev,
            risk_usdt=risk_usdt,
            stop_loss_pct=stop_pct,
            take_profit_pct=tp_pct,
            time_limit_s=fast.time_limit_s,
            lifeboat_stop_pct=lifeboat_pct,
            approved_at=now,
        )

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
            outcome = Outcome.PASS
        else:
            summary = f"{signal.symbol} {side} 拒绝：{verdict.reasons[0]}"
            outcome = Outcome.REJECT
        self._rec.record(
            now,
            outcome,
            summary,
            symbol=signal.symbol,
            signal_id=signal.signal_id,
            card_id=card.card_id if card is not None else None,
            input={"trigger_price": signal.trigger_price, "kind": signal.kind.value},
            # 放行时 output 顶层就是 OrderIntent 的字段（验收脚本 jiaoyi.acceptance 按这个读），
            # 计算过程放在 detail 里；拒绝时写原因
            output=(
                {**verdict.intent.model_dump(mode="json"), "detail": detail}
                if verdict.intent is not None
                else {"approved": False, "reasons": verdict.reasons, "detail": detail}
            ),
        )
