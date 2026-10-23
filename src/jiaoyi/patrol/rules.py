"""巡逻兵：盯暴涨暴跌，只会要求平仓（close-only）。

每个成交 / 标记价格事件都看一眼，纯规则、不调模型，几微秒：
- 1 分钟内对持仓不利的波动 ≥ ``patrol_shock_pct_1m`` → 立即要求全平这个标的的持仓。
  突发冲击时等模型回答太慢，所以这一档不问 Jev。
- ≥ ``patrol_alert_pct_1m`` 但没到冲击线 → 不直接动作，把持仓放进 ``urgent``，
  让持仓管家马上（不等 15–30 秒）用决策模型复查一次。这就是巡逻兵“规则 + Jev”里 Jev 的部分：
  判断交给管家，巡逻兵本身永远只能平仓、不能开仓或放宽止损。
同一个标的 30 秒内只要求一次，避免重复下平仓。

两条线都不低于这笔的止损距离：冲击线 = max(档位百分比, ``patrol_shock_stop_mult`` × 止损%)，
报警线 = max(档位百分比, ``patrol_alert_stop_mult`` × 止损%)。急动后多半会回一部分，冲击线比止损
还近时巡逻兵会割在低点（orders-spec P1）。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from datetime import datetime, timedelta

from jiaoyi.contracts import (
    FastLoopConfig,
    MarketEvent,
    MarkPrice,
    Outcome,
    PatrolAction,
    PositionRecord,
    PositionStatus,
    Side,
    Trade,
)
from jiaoyi.core.recorder import RoleRecorder

WINDOW = timedelta(seconds=60)
REPEAT = timedelta(seconds=30)


class RulePatrol:
    """``Patrol`` 的实现。"""

    def __init__(self, cfg: FastLoopConfig, recorder: RoleRecorder | None = None) -> None:
        self._cfg = cfg
        self._rec = recorder
        self._px: dict[str, deque[tuple[datetime, float]]] = {}
        self._last_action: dict[str, datetime] = {}
        self.urgent: set[str] = set()
        """需要管家马上复查的 position_id；管家复查后由接线代码 ``discard``。"""

    def lines(self, pos: PositionRecord) -> tuple[float, float]:
        """这笔持仓的（报警线, 冲击线），百分比。没有止损价就只用档位百分比。"""
        cfg = self._cfg
        stop = 0.0
        if pos.entry_price and pos.stop_loss_price:
            stop = abs(pos.entry_price - pos.stop_loss_price) / pos.entry_price * 100.0
        return (
            max(cfg.patrol_alert_pct_1m, cfg.patrol_alert_stop_mult * stop),
            max(cfg.patrol_shock_pct_1m, cfg.patrol_shock_stop_mult * stop),
        )

    def check(self, event: MarketEvent, positions: Sequence[PositionRecord]) -> PatrolAction | None:
        if isinstance(event, Trade):
            price = event.price
        elif isinstance(event, MarkPrice):
            price = event.mark_price
        else:
            return None
        sym, now = event.symbol, event.ts
        dq = self._px.setdefault(sym, deque())
        dq.append((now, price))
        while dq and dq[0][0] < now - WINDOW:
            dq.popleft()

        mine = [
            p
            for p in positions
            if p.symbol == sym and p.status in (PositionStatus.OPEN, PositionStatus.OPENING)
        ]
        if not mine:
            return None
        hi = max(p for _, p in dq)
        lo = min(p for _, p in dq)
        hit: list[tuple[float, float]] = []  # (逆向波动, 这笔的冲击线)
        for pos in mine:
            # 多单怕从 1 分钟高点跌下来，空单怕从低点涨上去
            adverse = (
                (1.0 - price / hi) * 100.0 if pos.side == Side.LONG else (price / lo - 1.0) * 100.0
            )
            alert, shock = self.lines(pos)
            if adverse >= shock:
                hit.append((adverse, shock))
            elif adverse >= alert:
                self.urgent.add(pos.position_id)

        if not hit:
            return None
        worst, shock = max(hit)
        last = self._last_action.get(sym)
        if last is not None and now - last < REPEAT:
            return None
        self._last_action[sym] = now
        action = PatrolAction(
            ts=now,
            symbols=[sym],
            reason=f"{sym} 1 分钟内逆向波动 {worst:.2f}%（≥{shock:.2f}%），全平",
        )
        if self._rec is not None:
            self._rec.record(
                now,
                Outcome.CLOSE,
                action.reason,
                symbol=sym,
                position_id=mine[0].position_id if len(mine) == 1 else None,
                input={"price": price, "hi_1m": hi, "lo_1m": lo},
                output={"symbols": action.symbols, "positions": [p.position_id for p in mine]},
            )
        return action
