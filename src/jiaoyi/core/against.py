"""和作战卡相反的信号怎么处理（研究 2026-10-24，Aaron 批准）。

哨兵不看持仓，只报「看到一个和卡方向相反的信号」。这里由主循环拿**账本里此刻的持仓**
（包括正在平仓的）决定怎么分流：

- 这个币有持仓、且信号和持仓方向相反 → 叫管家马上复查一次，并把信号写进管家这次的提示。
  默认仍要连续 ``keeper_confirmations`` 次判翻向才平；这笔仓已亏到 ``against_loss_stop_frac``
  倍止损距离（或强反向信号：放量突破 / 强平瀑布反弹且量 ≥ ``against_strong_breakout_vol_ratio``
  倍，默认 50 倍基本不会）→ 管家这次判 1 次翻向就平。
  开仓 ``against_grace_s`` 内的不算；同一笔仓 ``against_recheck_cooldown_s`` 内最多急查 1 次；
  正在开 / 正在平的仓不叫管家。
- 同一个币 ``against_meeting_window_s`` 内反向信号 ≥ ``against_meeting_count`` 次（有仓没仓都算，
  同一种信号去重在哨兵那边做）→ 申请这个币的单币临时会（``signal_against_card``），
  同一个币 ``against_meeting_gap_s`` 内最多申请 1 次；开不开、花多少还归军师团的临时会规则管。
- 其余 → 只记录。

不自动反手，巡逻兵不变。每次处理在决策日志里记一条，最近 30 分钟的放进哨兵心跳
（``ModuleStatus.against_signals``），团队页按它亮线。
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Collection, Sequence
from datetime import datetime, timedelta
from typing import Literal

from jiaoyi.contracts import (
    AgainstSignalEvent,
    FastLoopConfig,
    Outcome,
    PositionRecord,
    PositionStatus,
    Side,
)
from jiaoyi.core.recorder import RoleRecorder

MEETING_TRIGGER = "signal_against_card"
KEEP = timedelta(minutes=30)
"""心跳里保留最近多久的反向信号。"""

_KIND_CN = {
    "volume_breakout": "放量突破",
    "volume_surge": "量能突增",
    "liquidation_rebound": "强平瀑布反弹",
    "crowded_reversal": "拥挤交易反向",
    "oi_follow": "持仓量变化",
    "top_vs_crowd": "大户散户分歧",
    "btc_lead": "BTC 先动",
    "session_event": "时段与事件",
}


def against_line(sig: AgainstSignalEvent) -> str:
    side = "多" if sig.side is Side.LONG else "空"
    return f"{sig.symbol} {_KIND_CN.get(sig.kind.value, sig.kind.value)}（{side}）：{sig.desc}"


class AgainstSignalDesk:
    def __init__(
        self,
        cfg: FastLoopConfig,
        *,
        request_event: Callable[[str, str], None] | None = None,
        meeting_ok: Callable[[], bool] | None = None,
        recorder: RoleRecorder | None = None,
    ) -> None:
        """``request_event``：军师团的单币临时会入口；``meeting_ok``：设置里开了这类临时会没有。"""
        self._cfg = cfg
        self._request_event = request_event
        self._meeting_ok = meeting_ok
        self._rec = recorder
        self._hits: dict[str, deque[datetime]] = {}
        self._met: dict[str, datetime] = {}  # 币 → 上次申请临时会
        self._rechecked: dict[str, datetime] = {}  # 持仓 → 上次因反向信号急查
        self._events: list[AgainstSignalEvent] = []

    def route(
        self,
        sig: AgainstSignalEvent,
        positions: Sequence[PositionRecord],
        closing: Collection[str],
        now: datetime,
    ) -> tuple[AgainstSignalEvent, list[tuple[PositionRecord, bool]]]:
        """``positions``：账本里此刻还没结束的持仓；``closing``：主循环正在平的持仓。

        返回（带结果的事件，要急查的持仓及「判 1 次就平」）。"""
        c = self._cfg
        if not c.against_signal_on:
            return self._done(sig, now, [], "只记录（反向信号处理已关）", None), []
        actions: list[Literal["keeper_recheck", "meeting"]] = []
        parts: list[str] = []
        notes: list[str] = []
        rechecks: list[tuple[PositionRecord, bool]] = []
        for p in positions:
            if p.symbol != sig.symbol or p.status in (PositionStatus.CLOSED, PositionStatus.FAILED):
                continue
            why = self._skip_reason(p, sig, closing, now)
            if why is not None:
                notes.append(why)
                continue
            loss = self._loss_hit(p, sig)
            rechecks.append((p, sig.strong or loss))
            self._rechecked[p.position_id] = now
            if sig.strong:
                parts.append("已叫管家复查（强信号，判 1 次翻向就平）")
            elif loss:
                parts.append(
                    f"已叫管家复查（已亏到 {c.against_loss_stop_frac:g} 倍止损，判 1 次就平）"
                )
            else:
                parts.append("已叫管家复查")
        if rechecks:
            actions.append("keeper_recheck")

        meeting = self._meeting(sig.symbol, now)
        if meeting == "asked":
            actions.append("meeting")
            parts.append("已申请临时会")
        elif meeting:
            notes.append(meeting)

        if parts:
            result = "；".join(parts + notes)
        else:
            result = "只记录" + (f"（{'；'.join(notes)}）" if notes else "")
        pid = rechecks[0][0].position_id if rechecks else None
        return self._done(sig, now, actions, result, pid), rechecks

    def _skip_reason(
        self, p: PositionRecord, sig: AgainstSignalEvent, closing: Collection[str], now: datetime
    ) -> str | None:
        c = self._cfg
        if p.status is PositionStatus.CLOSING or p.position_id in closing:
            return "持仓正在平仓"
        if p.status is not PositionStatus.OPEN:
            return "持仓还在开"
        if p.side is sig.side:
            return "信号和持仓同向，不用复查"
        if (now - p.opened_at).total_seconds() < c.against_grace_s:
            return f"开仓不到 {c.against_grace_s / 60:g} 分钟，不算"
        last = self._rechecked.get(p.position_id)
        if last is not None and (now - last).total_seconds() < c.against_recheck_cooldown_s:
            return f"这笔仓 {c.against_recheck_cooldown_s / 60:g} 分钟内已急查过"
        return None

    def _loss_hit(self, p: PositionRecord, sig: AgainstSignalEvent) -> bool:
        frac = self._cfg.against_loss_stop_frac
        entry, stop, px = p.entry_price, p.stop_loss_price, sig.price
        if frac <= 0 or not entry or not stop or not px:
            return False
        dist = abs(entry - stop)
        adverse = entry - px if p.side is Side.LONG else px - entry
        return dist > 0 and adverse >= frac * dist

    def _meeting(self, symbol: str, now: datetime) -> str | None:
        """够次数就申请临时会：返回 ``"asked"``；够次数但没申请返回原因；不够次数返回 None。"""
        c = self._cfg
        hits = self._hits.setdefault(symbol, deque())
        hits.append(now)
        window = timedelta(seconds=c.against_meeting_window_s)
        while hits and now - hits[0] > window:
            hits.popleft()
        if len(hits) < c.against_meeting_count:
            return None
        last = self._met.get(symbol)
        if last is not None and (now - last).total_seconds() < c.against_meeting_gap_s:
            return f"{c.against_meeting_gap_s / 60:g} 分钟内已申请过临时会"
        if self._request_event is None or (self._meeting_ok is not None and not self._meeting_ok()):
            return "反复出现，但这档没开这类临时会（等下一轮开会重看）"
        self._request_event(symbol, MEETING_TRIGGER)
        self._met[symbol] = now
        hits.clear()
        return "asked"

    def _done(
        self,
        sig: AgainstSignalEvent,
        now: datetime,
        actions: list[Literal["keeper_recheck", "meeting"]],
        result: str,
        position_id: str | None,
    ) -> AgainstSignalEvent:
        ev = sig.model_copy(
            update={"actions": actions, "result": result, "position_id": position_id}
        )
        self._events.append(ev)
        self._prune(now)
        if self._rec is not None:
            self._rec.record(
                now,
                Outcome.ACTION if actions else Outcome.INFO,
                f"反向信号 {against_line(sig)}；{result}",
                symbol=sig.symbol,
                position_id=position_id,
                output={"against_signal": ev.model_dump(mode="json")},
            )
        return ev

    def settle(self, position_id: str, verdict: Literal["close", "hold"]) -> None:
        """管家急查完：把结论写回最近一条叫了这笔仓复查、还没结论的反向信号。"""
        for i in range(len(self._events) - 1, -1, -1):
            ev = self._events[i]
            if ev.position_id == position_id and ev.keeper_verdict is None:
                self._events[i] = ev.model_copy(update={"keeper_verdict": verdict})
                return

    def forget(self, position_id: str) -> None:
        self._rechecked.pop(position_id, None)

    def recent(self, now: datetime) -> list[AgainstSignalEvent]:
        self._prune(now)
        return list(self._events)

    def _prune(self, now: datetime) -> None:
        self._events = [e for e in self._events if now - e.ts <= KEEP]
