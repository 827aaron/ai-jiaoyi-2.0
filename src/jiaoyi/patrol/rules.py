"""巡逻兵：盯暴涨暴跌，只会要求平仓（close-only）。

每个成交 / 标记价格事件都看一眼，纯规则、不调模型，几微秒：
- 1 分钟内对持仓不利的波动 ≥ ``patrol_shock_pct_1m`` → 立即要求全平这个标的的持仓。
  突发冲击时等模型回答太慢，所以这一档不问 Jev。
- ≥ ``patrol_alert_pct_1m`` 但没到冲击线 → 不直接动作，把持仓放进 ``urgent``，
  让持仓管家马上（不等 15–30 秒）用决策模型复查一次。这就是巡逻兵“规则 + Jev”里 Jev 的部分：
  判断交给管家，巡逻兵本身永远只能平仓、不能开仓或放宽止损。
同一个标的 30 秒内只要求一次，避免重复下平仓。
冲击线不在盈利仓上触发（Aaron 10-25「盈利也要平吗？这个不对吧」）：现价比开仓价好、且超过来回
手续费的仓，冲高回吐一个止损距离不全平，改成叫管家马上复查（和报警线一样）；亏损或持平的仓规则不变。
跳过的事每个币每分钟最多写一条记录，方便数有多少次。

两条线按这笔的止损距离定：冲击线 = ``patrol_shock_stop_mult`` × 止损%（默认 1.0），
报警线 = ``patrol_alert_stop_mult`` × 止损%（默认 0.5）。档位百分比只在没有止损价（或倍数设成 0）时
兜底：取 max 的话，ETHFI 止损 1.02% 冲击线却是 2%，巡逻兵永远赶不在止损前面。
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
ROUND = timedelta(seconds=10)
"""团队页「刚刚：…」每 10 秒汇总一次（逐笔成交都在查，每笔都写会刷屏）；今日次数 = 巡检轮数。"""


def _stop_pct(pos: PositionRecord) -> float:
    """这笔的止损距离（%）；没有止损价是 0。"""
    if pos.entry_price and pos.stop_loss_price:
        return abs(pos.entry_price - pos.stop_loss_price) / pos.entry_price * 100.0
    return 0.0


class RulePatrol:
    """``Patrol`` 的实现。"""

    def __init__(
        self, cfg: FastLoopConfig, recorder: RoleRecorder | None = None, *, fee_bps: float = 5.0
    ) -> None:
        """``fee_bps``：单边吃单手续费；浮盈超过来回手续费才算「盈利中」。"""
        self._cfg = cfg
        self._rec = recorder
        self._fee_pct = 2.0 * fee_bps / 100.0
        self._skip_logged: dict[str, datetime] = {}
        self._px: dict[str, deque[tuple[datetime, float]]] = {}
        self._last_action: dict[str, datetime] = {}
        self._round_at: datetime | None = None
        self._seen: dict[str, tuple[int, float, float, float]] = {}
        """这一轮看过的币 → (持仓笔数, 最大逆向波动 ÷ 止损距离, 报警线%, 冲击线%)。"""
        self.urgent: set[str] = set()
        """需要管家马上复查的 position_id；管家复查后由接线代码 ``discard``。"""

    def lines(self, pos: PositionRecord) -> tuple[float, float]:
        """这笔持仓的（报警线, 冲击线），百分比。没有止损价就用档位百分比兜底。"""
        cfg = self._cfg
        stop = _stop_pct(pos)
        alert = cfg.patrol_alert_stop_mult * stop or cfg.patrol_alert_pct_1m
        shock = cfg.patrol_shock_stop_mult * stop or cfg.patrol_shock_pct_1m
        return min(alert, shock), shock

    def _round(self, now: datetime) -> None:
        if self._rec is None or (self._round_at is not None and now - self._round_at < ROUND):
            return
        self._round_at = now
        parts = [
            f"{sym.removesuffix('USDT')} {n} 笔（1 分钟最大逆向 {r:.1f} 个止损距离，"
            f"冲击线 {shock:.2f}% · 报警线 {alert:.2f}%）"
            for sym, (n, r, alert, shock) in sorted(self._seen.items())
        ]
        calm = all(v[1] < 0.5 for v in self._seen.values())
        self._rec.note(
            now, f"查了 {'、'.join(parts)}，{'没异常' if calm else '波动偏大，盯着'}", count=True
        )
        self._rec.set_next_run(now + ROUND)
        self._seen = {}

    def _in_profit(self, pos: PositionRecord, price: float) -> bool:
        """现价比开仓价好、且超过来回手续费。没有开仓价（还没成交）算不盈利。"""
        if not pos.entry_price:
            return False
        sign = 1.0 if pos.side == Side.LONG else -1.0
        return sign * (price / pos.entry_price - 1.0) * 100.0 > self._fee_pct

    def _note_skip(self, sym: str, now: datetime, pos: PositionRecord, price: float) -> None:
        last = self._skip_logged.get(sym)
        if self._rec is None or (last is not None and now - last < WINDOW):
            return
        self._skip_logged[sym] = now
        self._rec.record(
            now,
            Outcome.HOLD,
            f"{sym} 巡逻兵：盈利中，冲击规则不触发",
            symbol=sym,
            position_id=pos.position_id,
            input={"price": price, "entry": pos.entry_price},
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
            stop = _stop_pct(pos)
            ratio = adverse / stop if stop > 0 else 0.0
            worst_ratio = self._seen.get(sym, (0, 0.0, 0.0, 0.0))[1]
            self._seen[sym] = (len(mine), max(worst_ratio, ratio), alert, shock)
            if adverse >= shock and self._in_profit(pos, price):
                self.urgent.add(pos.position_id)  # 赚着钱：不全平，让管家马上看
                self._note_skip(sym, now, pos, price)
            elif adverse >= shock:
                hit.append((adverse, shock))
            elif adverse >= alert:
                self.urgent.add(pos.position_id)

        self._round(now)
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
