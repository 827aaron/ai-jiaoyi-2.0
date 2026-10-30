"""持仓币加开临时会（2026-10-30，Aaron 批准）。

作战卡只在定时整轮或价格穿过卡上关键位的临时会时才换；行情急转但没碰关键位时，持仓币的卡
最多旧 2 小时，反向卡硬规则（``opposite_card_action``）就用不上。这里给**我们持有的币**
多两种申请单币临时会的理由，开会、限流、去重都走军师团原来的临时会队列（同币间隔、失败退避、
并发、每日上限；同一个币已排队就不重复排），结论走正常出卡路，卡翻了由原来的反向卡规则平仓，
这里不平仓：

- ``held_adverse_move``：持仓逆向走到 ``patrol_held_event_adverse_r`` 倍 R（R = 开仓时定的止损距离
  ``stop_loss_pct``；按标记价算价格逆向的幅度，不扣手续费），每笔仓每档只申请 1 次；
  ``patrol_held_event_adverse_r2`` 可再设一档。
- ``held_desk_flip``：大盘台发布的方向（看涨 / 看跌）和持仓方向相反，且这笔仓此刻按标记价扣
  来回吃单费是亏的；赚钱的仓留给移动止损，不加会。同一笔仓同一个相反方向只申请 1 次，大盘台
  回到中性 / 同向后再翻过来才再申请。

申请过的档位 / 方向存在 ``data/held_events.json``，重启不重复申请；仓结束就删。
每次触发在决策日志里记一条（巡逻兵），写明理由。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from jiaoyi.contracts import (
    FastLoopConfig,
    MarketView,
    Outcome,
    PositionRecord,
    PositionStatus,
    Side,
)
from jiaoyi.core.recorder import RoleRecorder

logger = logging.getLogger(__name__)

HELD_EVENTS_FILE = "held_events.json"
ADVERSE_TRIGGER = "held_adverse_move"
DESK_TRIGGER = "held_desk_flip"
TRIGGERS: frozenset[str] = frozenset({ADVERSE_TRIGGER, DESK_TRIGGER})
DESK_SOURCE = "market_desk"

_AGAINST = {Side.LONG: "看跌", Side.SHORT: "看涨"}
_ENDED = (PositionStatus.CLOSED, PositionStatus.FAILED)


def adverse_r(pos: PositionRecord, price: float) -> float | None:
    """价格朝不利方向走了几倍 R；顺向为负。没有入场价或止损距离返回 None。"""
    entry = pos.entry_price
    if not entry or price <= 0:
        return None
    stop_pct = pos.stop_loss_pct
    if stop_pct is None and pos.stop_loss_price:
        stop_pct = abs(entry - pos.stop_loss_price) / entry * 100
    if not stop_pct:
        return None
    move = (entry - price) if pos.side is Side.LONG else (price - entry)
    return move / entry * 100 / stop_pct


def net_pnl_pct(pos: PositionRecord, price: float, fee_bps: float) -> float | None:
    """按 ``price`` 算的浮动盈亏（%），扣来回吃单费。"""
    entry = pos.entry_price
    if not entry:
        return None
    move = (price - entry) if pos.side is Side.LONG else (entry - price)
    return move / entry * 100 - 2 * fee_bps / 100


class HeldEventDesk:
    def __init__(
        self,
        cfg: FastLoopConfig,
        path: Path,
        *,
        fee_bps: Callable[[], float],
        request_event: Callable[[str, str], None] | None = None,
        meeting_ok: Callable[[str], bool] | None = None,
        recorder: RoleRecorder | None = None,
    ) -> None:
        """``cfg`` 是热生效时就地改的那个对象；``meeting_ok(trigger)``：军师团此刻收不收
        这类临时会。"""
        self._cfg = cfg
        self._path = path
        self._fee_bps = fee_bps
        self._request_event = request_event
        self._meeting_ok = meeting_ok
        self._rec = recorder
        self._adverse: dict[str, list[float]] = {}  # 持仓 → 已申请过的档位
        self._desk: dict[str, str] = {}  # 持仓 → 已为哪个相反的大盘方向申请过
        self._marks: dict[str, float] = {}
        self._load()

    # ------------------------------------------------------------ 逆向 R
    def on_mark(
        self, symbol: str, price: float, positions: Sequence[PositionRecord], now: datetime
    ) -> None:
        self._marks[symbol] = price
        levels = sorted(
            lv
            for lv in (
                self._cfg.patrol_held_event_adverse_r,
                self._cfg.patrol_held_event_adverse_r2,
            )
            if lv > 0
        )
        if not levels:
            return
        for pos in positions:
            if pos.symbol != symbol or pos.status is not PositionStatus.OPEN:
                continue
            r = adverse_r(pos, price)
            if r is None:
                continue
            done = self._adverse.get(pos.position_id, [])
            hit = [lv for lv in levels if r >= lv - 1e-9 and lv not in done]
            if not hit:
                continue
            self._adverse[pos.position_id] = sorted({*done, *hit})
            self._save()
            side = "多单" if pos.side is Side.LONG else "空单"
            self._ask(
                pos,
                ADVERSE_TRIGGER,
                now,
                f"持仓逆向 {max(hit):g}R，加开临时会",
                {"adverse_r": round(r, 4), "levels": hit, "price": price, "side": side},
            )

    # ------------------------------------------------------------ 大盘台翻向
    def on_market(
        self,
        view: MarketView,
        positions: Sequence[PositionRecord],
        now: datetime,
        price: Callable[[str], float | None] | None = None,
    ) -> None:
        """大盘台每次发布新方向时调一次（同方向的重发不调）。"""
        if view.source != DESK_SOURCE or not self._cfg.patrol_held_event_on_desk_flip:
            return
        direction = view.regime_direction
        changed = False
        for pos in positions:
            if pos.status is not PositionStatus.OPEN:
                continue
            pid = pos.position_id
            if _AGAINST[pos.side] != direction:
                changed |= self._desk.pop(pid, None) is not None
                continue
            if self._desk.get(pid) == direction:
                continue
            px = self._marks.get(pos.symbol)
            if px is None and price is not None:
                px = price(pos.symbol)
            pnl = net_pnl_pct(pos, px, self._fee_bps()) if px else None
            if pnl is None or pnl >= 0:
                continue
            self._desk[pid] = direction
            changed = True
            trend = "涨" if direction == "看涨" else "跌"
            side = "多单" if pos.side is Side.LONG else "空单"
            self._ask(
                pos,
                DESK_TRIGGER,
                now,
                f"大盘台转{trend}，持仓{side}加开临时会",
                {
                    "market_view_id": view.market_view_id,
                    "direction": direction,
                    "net_pnl_pct": round(pnl, 4),
                    "price": px,
                },
            )
        live = {p.position_id for p in positions if p.status not in _ENDED}
        for book in (self._adverse, self._desk):
            for pid in [k for k in book if k not in live]:
                del book[pid]
                changed = True
        if changed:
            self._save()

    # ------------------------------------------------------------ 共用
    def _ask(
        self,
        pos: PositionRecord,
        trigger: str,
        now: datetime,
        why: str,
        detail: dict[str, object],
    ) -> None:
        ok = self._request_event is not None and (
            self._meeting_ok is None or self._meeting_ok(trigger)
        )
        if ok and self._request_event is not None:
            self._request_event(pos.symbol, trigger)
            summary = why
        else:
            summary = f"{why.removesuffix('，加开临时会')}；临时会没开，只记录"
        if self._rec is not None:
            self._rec.record(
                now,
                Outcome.ACTION if ok else Outcome.INFO,
                f"{pos.symbol} {summary}",
                symbol=pos.symbol,
                position_id=pos.position_id,
                output={"held_event": {"trigger": trigger, "requested": ok, **detail}},
            )

    def forget(self, position_id: str) -> None:
        adverse = self._adverse.pop(position_id, None)
        desk = self._desk.pop(position_id, None)
        if adverse is not None or desk is not None:
            self._save()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._adverse = {
                str(k): [float(x) for x in v] for k, v in raw.get("adverse", {}).items()
            }
            self._desk = {str(k): str(v) for k, v in raw.get("desk", {}).items()}
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, AttributeError):
            logger.exception("读 %s 失败，按没申请过算（可能重复申请一次临时会）", self._path)

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps({"adverse": self._adverse, "desk": self._desk}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            logger.exception("写 %s 失败，重启后可能重复申请一次临时会", self._path)
