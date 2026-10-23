"""保镖的参考实现（测试替身）。

**不是生产代码。** 真正的保镖在 ``jiaoyi.risk``，由快环工程师实现。这份参考实现按设计文档 §四
把规则写成最直白的样子，有两个用途：

1. 让回放框架在真保镖做好之前就能跑通；
2. 作为 ``tests/support/contracts.py`` 里风控契约套件的“对照答案”——套件先在这份实现上通过，
   说明套件本身是对的，再拿去测真保镖。

只放在 tests/ 下，所以不受“只有 jiaoyi.risk 能构造 OrderIntent”的结构检查影响；
生产代码不得导入 ``tests``。
"""

from __future__ import annotations

import math
from datetime import datetime

from jiaoyi.contracts import (
    AccountState,
    BattleCard,
    Bias,
    CandidateSignal,
    FeatureSnapshot,
    JevResponse,
    OrderIntent,
    RiskConfig,
    RiskVerdict,
    Side,
    SizeTier,
)

MIN_PROB = 0.6
"""拍板人每个关键回答至少要有的把握。真保镖的门槛来自配置，这里是测试用固定值。"""
TIER_SCALE = {SizeTier.S: 0.5, SizeTier.M: 0.75, SizeTier.L: 1.0}
TIER_ORDER = [SizeTier.S, SizeTier.M, SizeTier.L]
QTY_STEP = 0.001
TIME_LIMIT_S = 300


class ReferenceRiskGuard:
    def __init__(self, cfg: RiskConfig, time_limit_s: int = TIME_LIMIT_S) -> None:
        self._cfg = cfg
        self._time_limit_s = time_limit_s

    def review(
        self,
        signal: CandidateSignal,
        jev: JevResponse,
        card: BattleCard | None,
        account: AccountState,
        now: datetime,
        *,
        market: FeatureSnapshot | None = None,
    ) -> RiskVerdict:
        reasons = self._reasons(signal, jev, card, account, now)
        if reasons:
            return RiskVerdict(signal_id=signal.signal_id, approved=False, reasons=reasons)
        assert card is not None
        return self._size(signal, jev, card, account, now)

    # ---- 逐条检查，收集全部拒绝原因
    def _reasons(
        self,
        signal: CandidateSignal,
        jev: JevResponse,
        card: BattleCard | None,
        account: AccountState,
        now: datetime,
    ) -> list[str]:
        cfg = self._cfg
        out: list[str] = []
        if not jev.ok:
            out.append("决策模型不可用，故障即关闭")
        if card is None or not card.is_valid(now):
            out.append("没有有效作战卡")
        if account.paused:
            out.append("已暂停开新仓")
        if account.day_pnl_pct <= -cfg.daily_loss_halt_pct:
            out.append(f"当日亏损 {account.day_pnl_pct:.2f}% 已触发熔断")
        if account.open_positions >= cfg.max_open_positions:
            out.append("持仓笔数已满")
        total_cap = cfg.max_total_exposure_x * account.equity_usdt
        if account.open_notional_usdt >= total_cap:
            out.append("总敞口已满")
        if signal.features.book and signal.features.book.spread_bps > cfg.max_spread_bps:
            out.append("价差过大")
        if card is not None:
            cs = card.for_symbol(signal.symbol)
            if cs is None:
                out.append("作战卡里没有这个标的")
            else:
                if cs.bias is Bias.NO_TRADE:
                    out.append("作战卡标记只看不做")
                elif cs.bias.value in ("long", "short") and cs.bias.value != signal.side_hint.value:
                    out.append("方向和作战卡偏好相反")
                if signal.kind not in cs.allowed_signals:
                    out.append("作战卡不允许这类信号")
        if jev.ok:
            out += self._jev_reasons(signal, jev)
        return out

    def _jev_reasons(self, signal: CandidateSignal, jev: JevResponse) -> list[str]:
        out: list[str] = []
        want = {"genuine": "real", "toxicity": "low", "take": "take"}
        for q, choice in want.items():
            ans = jev.answer(q)
            if ans is None:
                out.append(f"决策模型缺少 {q} 的回答")
            elif ans.probs.get(choice, 0.0) < MIN_PROB:
                out.append(f"拍板人不认可：{q}")
        direction = jev.answer("direction")
        if direction is None:
            out.append("决策模型缺少 direction 的回答")
        else:
            top, p = direction.top()
            expected = "up" if signal.side_hint is Side.LONG else "down"
            if top != expected or p < MIN_PROB:
                out.append("决策模型判断的方向和信号不一致")
        return out

    # ---- 通过后：按风险金额定数量
    def _size(
        self,
        signal: CandidateSignal,
        jev: JevResponse,
        card: BattleCard,
        account: AccountState,
        now: datetime,
    ) -> RiskVerdict:
        cfg = self._cfg
        cs = card.for_symbol(signal.symbol)
        assert cs is not None
        price = signal.trigger_price
        width = jev.answer("stop_width")
        tier = width.top()[0] if width else "mid"
        lo, hi = cs.stop_atr.min, cs.stop_atr.max
        mult = {"narrow": lo, "mid": (lo + hi) / 2, "wide": hi}[tier]
        stop_pct = signal.features.atr * mult / price * 100.0
        tp_pct = stop_pct * (cs.take_profit_r.min + cs.take_profit_r.max) / 2
        round_trip_bps = 2 * cfg.taker_fee_bps
        if tp_pct * 100.0 < cfg.min_tp_fee_multiple * round_trip_bps:
            return self._reject(signal, "止盈太小，覆盖不了手续费")
        if stop_pct <= 0:
            return self._reject(signal, "止损距离为零")

        risk_usdt = account.equity_usdt * cfg.risk_per_trade_pct / 100.0
        notional = risk_usdt / (stop_pct / 100.0)
        size = jev.answer("size")
        tier_size = SizeTier(size.top()[0]) if size else SizeTier.S
        tier_size = min(tier_size, cs.size_tier_max, key=TIER_ORDER.index)
        notional *= TIER_SCALE[tier_size]
        leverage = max(1, min(int(cs.leverage.max), cfg.max_leverage))
        caps = [
            cs.notional_usdt.max,
            cfg.max_notional_per_trade_usdt,
            account.available_usdt * leverage,
            cfg.max_total_exposure_x * account.equity_usdt - account.open_notional_usdt,
            cfg.category_exposure_x[cs.category] * account.equity_usdt
            - account.open_notional_by_category.get(cs.category, 0.0),
        ]
        notional = min(notional, *caps)
        qty = math.floor(notional / price / QTY_STEP) * QTY_STEP
        notional = qty * price
        if qty <= 0 or notional < cs.notional_usdt.min:
            return self._reject(signal, "按风险金额算出的仓位太小")

        intent = OrderIntent(
            intent_id=f"int-{signal.signal_id}",
            signal_id=signal.signal_id,
            card_id=card.card_id,
            symbol=signal.symbol,
            category=cs.category,
            side=signal.side_hint,
            quantity=round(qty, 8),
            notional_usdt=notional,
            leverage=leverage,
            risk_usdt=qty * price * stop_pct / 100.0,
            stop_loss_pct=stop_pct,
            take_profit_pct=tp_pct,
            time_limit_s=self._time_limit_s,
            lifeboat_stop_pct=stop_pct * cfg.lifeboat_stop_multiplier,
            approved_at=now,
        )
        return RiskVerdict(signal_id=signal.signal_id, approved=True, intent=intent)

    @staticmethod
    def _reject(signal: CandidateSignal, reason: str) -> RiskVerdict:
        return RiskVerdict(signal_id=signal.signal_id, approved=False, reasons=[reason])
