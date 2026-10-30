"""持仓币加开临时会（2026-10-30，Aaron 批准）。

作战卡只在定时整轮或价格穿过卡上关键位的临时会时才换；行情急转但没碰关键位时，持仓币的卡
最多旧 2 小时，反向卡硬规则（``opposite_card_action``）就用不上。这里给**我们持有、且在亏的币**
多两种申请单币临时会的理由。开会、限流、去重都走军师团原来的临时会队列（同币间隔、失败退避、
并发、每日上限；同一个币已排队就不重复排，一场会顶掉这段时间里所有原因），结论走正常出卡路，
卡翻了由原来的反向卡规则平仓，这里不平仓：

- ``held_reverse_loss``：哨兵报了和持仓方向相反的信号（不管 ``against_signal_on`` 开没开），
  且这笔仓此刻已亏到 ``patrol_held_event_loss_r`` 倍 R（R = 开仓时定的止损距离
  ``stop_loss_pct``；亏损按标记价、扣来回吃单费）。每个反向信号都可以再申请，靠军师团的
  限流挡住重复。
- ``held_market_trail``：大盘移动止损刚锁住（``core/market_trail.py`` 判「真的刚锁住」），锁住方向
  上亏到 ``patrol_held_event_loss_r`` 倍 R 的仓每笔申请一场；这类会每天最多
  ``signal_market_trail_meetings_per_day`` 场，单独计数（存盘），军师团的限流照常。
- ``held_desk_flip``：大盘台此刻的方向（看涨 / 看跌）和持仓相反、这笔仓扣费后在亏、而且这个币
  从大盘台翻到这个方向以来还没开过会（也没在开）。会开过了就满足；没开上（排队过期、不在池里、
  重启丢了队列）就在下一个标记价时再申请，申请的决策记录每笔仓每次翻向只写一条。

大盘台的方向和翻向时间存在 ``data/held_events.json``，重启后照样认。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jiaoyi.contracts import (
    AgainstSignalEvent,
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
REVERSE_TRIGGER = "held_reverse_loss"
DESK_TRIGGER = "held_desk_flip"
TRAIL_TRIGGER = "held_market_trail"
TRIGGERS: frozenset[str] = frozenset({REVERSE_TRIGGER, DESK_TRIGGER, TRAIL_TRIGGER})
DESK_SOURCE = "market_desk"

_AGAINST = {Side.LONG: "看跌", Side.SHORT: "看涨"}
_SIDE_CN = {Side.LONG: "多单", Side.SHORT: "空单"}


def net_pnl_pct(pos: PositionRecord, price: float, fee_bps: float) -> float | None:
    """按 ``price`` 算的浮动盈亏（%），扣来回吃单费。"""
    entry = pos.entry_price
    if not entry:
        return None
    move = (price - entry) if pos.side is Side.LONG else (entry - price)
    return move / entry * 100 - 2 * fee_bps / 100


def loss_r(pos: PositionRecord, price: float, fee_bps: float) -> float | None:
    """扣费后亏了几倍 R（赚钱为负）。没有开仓时的止损距离返回 None：止损价会被移动止损挪动，
    拿它推 R 会把 R 算得很小。"""
    pnl = net_pnl_pct(pos, price, fee_bps)
    if pnl is None or not pos.stop_loss_pct:
        return None
    return -pnl / pos.stop_loss_pct


class HeldEventDesk:
    def __init__(
        self,
        cfg: FastLoopConfig,
        path: Path,
        *,
        tz: str = "Asia/Bangkok",
        fee_bps: Callable[[], float],
        request_event: Callable[[str, str], None] | None = None,
        meeting_ok: Callable[[str], bool] | None = None,
        met_since: Callable[[str, datetime], bool] | None = None,
        recorder: RoleRecorder | None = None,
    ) -> None:
        """``cfg`` 是热生效时就地改的那个对象；``meeting_ok(trigger)``：军师团此刻收不收这类
        临时会；``met_since(币, 时间)``：这个币在那之后开过会或正在开。"""
        self._cfg = cfg
        self._path = path
        self._tz = ZoneInfo(tz)
        self._fee_bps = fee_bps
        self._request_event = request_event
        self._meeting_ok = meeting_ok
        self._met_since = met_since
        self._rec = recorder
        self._desk_dir: str | None = None
        self._desk_since: datetime | None = None
        self._marks: dict[str, float] = {}
        self._logged: set[tuple[str, datetime]] = set()  # （持仓，翻向时间）已写过决策记录
        self._trail_day: str | None = None
        self._trail_count = 0
        self._load()

    # ------------------------------------------------------------ 反向信号且已亏
    def on_reverse(
        self, sig: AgainstSignalEvent, positions: Sequence[PositionRecord], now: datetime
    ) -> None:
        threshold = self._cfg.patrol_held_event_loss_r
        if threshold <= 0:
            return
        px = self._marks.get(sig.symbol) or sig.price
        if not px:
            return
        for pos in positions:
            if (
                pos.symbol != sig.symbol
                or pos.status is not PositionStatus.OPEN
                or pos.side is sig.side
            ):
                continue
            r = loss_r(pos, px, self._fee_bps())
            if r is None or r < threshold - 1e-9:
                continue
            self._ask(
                pos,
                REVERSE_TRIGGER,
                now,
                f"反向信号且持仓已亏 {r:.2f}R，加开临时会",
                {"loss_r": round(r, 4), "price": px, "signal_kind": sig.kind.value},
            )
            return  # 一个币一场会

    # ------------------------------------------------------------ 大盘台翻向
    def on_mark(
        self, symbol: str, price: float, positions: Sequence[PositionRecord], now: datetime
    ) -> None:
        self._marks[symbol] = price
        self._check_desk([p for p in positions if p.symbol == symbol], now, None)

    def on_market(
        self,
        view: MarketView,
        positions: Sequence[PositionRecord],
        now: datetime,
        price: Callable[[str], float | None] | None = None,
    ) -> None:
        """大盘台每次发布新方向时调一次（同方向的重发不调）。"""
        if view.source != DESK_SOURCE:
            return
        if view.regime_direction != self._desk_dir or self._desk_since is None:
            self._desk_dir, self._desk_since = view.regime_direction, view.observed_at
            self._logged.clear()
            self._save()
        self._check_desk(positions, now, price)

    def _check_desk(
        self,
        positions: Sequence[PositionRecord],
        now: datetime,
        price: Callable[[str], float | None] | None,
    ) -> None:
        since, direction = self._desk_since, self._desk_dir
        if not self._cfg.patrol_held_event_on_desk_flip or since is None:
            return
        for pos in positions:
            if pos.status is not PositionStatus.OPEN or _AGAINST[pos.side] != direction:
                continue
            if self._met_since is not None and self._met_since(pos.symbol, since):
                continue
            px = self._marks.get(pos.symbol)
            if px is None and price is not None:
                px = price(pos.symbol)
            pnl = net_pnl_pct(pos, px, self._fee_bps()) if px else None
            if pnl is None or pnl >= 0:
                continue
            key = (pos.position_id, since)
            if key in self._logged:
                if self._ok(DESK_TRIGGER) and self._request_event is not None:
                    self._request_event(pos.symbol, DESK_TRIGGER)
                continue
            self._logged.add(key)
            trend = "涨" if direction == "看涨" else "跌"
            self._ask(
                pos,
                DESK_TRIGGER,
                now,
                f"大盘台转{trend}，持仓{_SIDE_CN[pos.side]}加开临时会",
                {"direction": direction, "since": since.isoformat(), "net_pnl_pct": round(pnl, 4)},
            )

    # ------------------------------------------------------------ 大盘移动止损刚锁住
    def on_trail_lock(
        self,
        side: Side,
        positions: Sequence[PositionRecord],
        now: datetime,
        price: Callable[[str], float | None] | None = None,
    ) -> None:
        """一次锁住调一次（去重在 ``MarketTrail``），所以每笔仓每次锁最多一场。"""
        threshold = self._cfg.patrol_held_event_loss_r
        if threshold <= 0:
            return
        day = now.astimezone(self._tz).date().isoformat()
        if day != self._trail_day:
            self._trail_day, self._trail_count = day, 0
        cap = self._cfg.signal_market_trail_meetings_per_day
        for pos in positions:
            if pos.status is not PositionStatus.OPEN or pos.side is not side:
                continue
            px = self._marks.get(pos.symbol) or (price(pos.symbol) if price else None)
            r = loss_r(pos, px, self._fee_bps()) if px else None
            if r is None or r < threshold - 1e-9:
                continue
            ok = self._ok(TRAIL_TRIGGER)
            capped = self._trail_count >= cap
            detail: dict[str, object] = {"loss_r": round(r, 4), "price": px, "meeting_ok": ok}
            if capped:
                self._log_capped(pos, now, r, detail)
                continue
            self._ask(
                pos, TRAIL_TRIGGER, now, f"大盘闸门锁住且持仓在亏 {r:.2f}R，加开临时会", detail
            )
            if ok:
                self._trail_count += 1
        self._save()

    def _log_capped(
        self, pos: PositionRecord, now: datetime, r: float, detail: dict[str, object]
    ) -> None:
        if self._rec is not None:
            self._rec.record(
                now,
                Outcome.INFO,
                f"{pos.symbol} 大盘闸门锁住且持仓在亏 {r:.2f}R；今天这类临时会已到上限，只记录",
                symbol=pos.symbol,
                position_id=pos.position_id,
                output={
                    "held_event": {
                        "trigger": TRAIL_TRIGGER,
                        "queued": False,
                        "capped": True,
                        **detail,
                    }
                },
            )

    # ------------------------------------------------------------ 共用
    def _ok(self, trigger: str) -> bool:
        return self._request_event is not None and (
            self._meeting_ok is None or self._meeting_ok(trigger)
        )

    def _ask(
        self,
        pos: PositionRecord,
        trigger: str,
        now: datetime,
        why: str,
        detail: dict[str, object],
    ) -> None:
        ok = self._ok(trigger)
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
                output={"held_event": {"trigger": trigger, "queued": ok, **detail}},
            )

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            since = raw.get("desk_since")
            self._desk_dir = raw.get("desk_direction")
            self._desk_since = datetime.fromisoformat(since) if since else None
            self._trail_day = raw.get("trail_day")
            self._trail_count = int(raw.get("trail_count") or 0)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, AttributeError):
            logger.exception("读 %s 失败，等大盘台下一次发布再认方向", self._path)

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        data = {
            "desk_direction": self._desk_dir,
            "desk_since": self._desk_since.isoformat() if self._desk_since else None,
            "trail_day": self._trail_day,
            "trail_count": self._trail_count,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            logger.exception("写 %s 失败，重启后要等大盘台下一次发布再认方向", self._path)
