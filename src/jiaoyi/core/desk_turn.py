"""大盘台转向时暂停旧方向的新仓（2026-10-30，Aaron 批准）。

大盘台每一轮都判一次方向（``DeskRun.direction``，发布没发布都算，就是
``market_desk/<日期>.jsonl`` 每行的 ``direction``）。上一轮是看跌 / 看涨、这一轮不是同一个方向，
就算从那个方向转走了（看跌 → 中性 / 看涨，看涨 → 中性 / 看跌）。转向时间 = 这一轮发布的时间
（没发布就是跑完的时刻）。不等第二轮确认：

- 转走时立刻暂停：卡上方向就是旧方向的币（看跌 → 卡上做空的币）不开旧方向的新仓，另一个方向
  不受影响；持仓不动（止损、移动止损、反向卡照旧）。大盘台回到那个方向就立刻全部恢复。来回
  翻只是开关暂停，不花钱、不申请会。
- 只按需开会：暂停期间哨兵给某个币出了旧方向的开仓信号，拒掉这个信号，并给这个币申请一场临时会
  （触发名 ``desk_turn``，走军师团原来的临时会队列和限流；转向后的会已在排 / 在开就不再申请）。
- 这个币在大盘台上一次换方向之后开始的第一场会（卡上 ``issued_at`` ≥ 那个时间）出了结论：还是
  旧方向，就把它从暂停里放出来（大盘台还没回来也可以开）；翻了或转中性就按新卡走。持仓币因
  大盘台翻向开的会也算这场会。放出来一直到大盘台下一次换方向，换了要再开一场才再放。

暂停、恢复、放出、拒掉的信号都在决策日志里记风控员的一条。状态存在 ``data/desk_turn_pause.json``，
重启照样拦。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jiaoyi.contracts import BattleCard, Bias, FastLoopConfig, Outcome, Side
from jiaoyi.core.recorder import RoleRecorder

logger = logging.getLogger(__name__)

DESK_TURN_FILE = "desk_turn_pause.json"
TRIGGER = "desk_turn"
_BIAS = {"看跌": Bias.SHORT, "看涨": Bias.LONG}
_SIDE = {"看跌": Side.SHORT, "看涨": Side.LONG}
_SIDE_CN = {Side.SHORT: "空单", Side.LONG: "多单"}
_CARD_CN = {"看跌": "做空", "看涨": "做多"}


class DeskTurnPause:
    def __init__(
        self,
        cfg: FastLoopConfig,
        path: Path,
        *,
        tz: str = "Asia/Bangkok",
        request_event: Callable[[str, str], None] | None = None,
        meeting_ok: Callable[[str], bool] | None = None,
        pending: Callable[[str], bool] | None = None,
        active: Callable[[], bool] | None = None,
        recorder: RoleRecorder | None = None,
        initial_direction: str | None = None,
    ) -> None:
        """``initial_direction``：没有存档时用的上一轮方向（大盘台自己存的最新一轮）；
        ``pending(币)``：这个币的会此刻在排 / 已派出 / 在开；``active()``：大盘台此刻是大盘的
        发布者（大盘台关了或由 Flash 接管时，存档里的暂停没人解，一律不拦）。"""
        self._cfg = cfg
        self._path = path
        self._tz = ZoneInfo(tz)
        self._request_event = request_event
        self._meeting_ok = meeting_ok
        self._pending = pending
        self._active = active
        self._rec = recorder
        self._last: str | None = initial_direction
        self._pauses: dict[str, tuple[datetime, str]] = {}  # 旧方向 → （转向时间，「看跌→中性」）
        self._released: dict[str, set[str]] = {}  # 旧方向 → 转向后开过会、还是旧方向的币
        self._changed: datetime | None = None  # 大盘台上一次换方向：放出来要这之后开始的会
        self._load()

    # ------------------------------------------------------------ 大盘台每一轮
    def on_desk_run(self, direction: str, now: datetime, at: datetime | None = None) -> None:
        """大盘台每跑完一轮（有方向时）调一次；``at``：这一轮的发布时间，没发布不传。"""
        prev, self._last = self._last, direction
        if prev == direction:
            return
        when = at or now
        self._released.clear()  # 放出来只到大盘台下一次换方向
        self._changed = when
        if direction in self._pauses:
            since, turn = self._pauses.pop(direction)
        else:
            since = None
        if since is not None and self._cfg.signal_desk_turn_pause:
            self._note(
                now,
                Outcome.INFO,
                f"大盘台 {self._hm(when)} 回到{direction}，恢复新{_SIDE_CN[_SIDE[direction]]}"
                f"（{self._hm(since)} {turn} 起暂停）",
                {"lifted": direction},
            )
        if prev in _BIAS:
            turn = f"{prev}→{direction}"
            self._pauses[prev] = (when, turn)
            if self._cfg.signal_desk_turn_pause:
                self._note(
                    now,
                    Outcome.ACTION,
                    f"大盘台 {self._hm(when)} {turn}，卡上{_CARD_CN[prev]}的币暂停开新"
                    f"{_SIDE_CN[_SIDE[prev]]}，有信号时再给那个币加开临时会",
                    {"paused": prev, "turn": turn},
                )
        self._save()

    # ------------------------------------------------------------ 开仓前
    def holds(self, symbol: str, side: Side, card: BattleCard | None, now: datetime) -> str | None:
        """只看不动：这个方向的新仓此刻是不是暂停着，是就返回「大盘台 HH:MM 看跌→中性，
        暂停新空单」。追单复核、面板重新检查用它（不申请会）。"""
        if not self._cfg.signal_desk_turn_pause or (
            self._active is not None and not self._active()
        ):
            return None
        for old, (since, turn) in self._pauses.items():
            if side is _SIDE[old] and self._in_pause(old, symbol, card, now):
                return f"大盘台 {self._hm(since)} {turn}，暂停新{_SIDE_CN[side]}"
        return None

    def blocked(
        self, symbol: str, side: Side, card: BattleCard | None, now: datetime
    ) -> str | None:
        """这个信号要不要拦：拦就返回理由，并按需申请一场临时会。会已在排 / 在开就不再申请；
        开过了却没放出来（判不做被忽略、开失败、结论作废）可以再申请，由同币间隔限流。"""
        head = self.holds(symbol, side, card, now)
        if head is None:
            return None
        if self._pending is not None and self._pending(symbol):
            return f"{head}，{symbol} 的临时会已在排 / 在开"
        if self._request_event is None or (
            self._meeting_ok is not None and not self._meeting_ok(TRIGGER)
        ):
            return f"{head}；临时会没开，等 {symbol} 下一轮开会"
        self._request_event(symbol, TRIGGER)
        return f"{head}，已给 {symbol} 加开临时会"

    def sweep(self, card: BattleCard | None, now: datetime) -> None:
        """新卡出来时调：转向后开过会、还是旧方向的币放出来（记一条）。"""
        if card is None:
            return
        for old in list(self._pauses):
            for cs in card.symbols:
                self._in_pause(old, cs.symbol, card, now)

    def paused_sides(self) -> dict[str, str]:
        return {old: turn for old, (_, turn) in self._pauses.items()}

    def released(self, old: str) -> set[str]:
        return set(self._released.get(old, set()))

    def _in_pause(self, old: str, symbol: str, card: BattleCard | None, now: datetime) -> bool:
        """卡上这个币是旧方向、还没放出来。转向后开过会、还是旧方向的，这里顺手放出来。"""
        cs = card.for_symbol(symbol) if card is not None else None
        if cs is None or cs.bias is not _BIAS[old] or symbol in self._released.get(old, set()):
            return False
        since, turn = self._pauses[old]
        if cs.issued_at is None or cs.issued_at < self._ref(since):
            return True
        self._released.setdefault(old, set()).add(symbol)
        self._save()
        if self._cfg.signal_desk_turn_pause:
            self._note(
                now,
                Outcome.INFO,
                f"大盘台 {self._hm(since)} {turn} 后 {symbol} 开过会，还是{_CARD_CN[old]}，"
                f"恢复开新{_SIDE_CN[_SIDE[old]]}",
                {"released": symbol, "turn": turn},
                symbol=symbol,
            )
        return False

    def _ref(self, since: datetime) -> datetime:
        return max(since, self._changed) if self._changed is not None else since

    def _hm(self, at: datetime) -> str:
        return at.astimezone(self._tz).strftime("%H:%M")

    def _note(
        self,
        now: datetime,
        outcome: Outcome,
        summary: str,
        out: dict[str, object],
        symbol: str | None = None,
    ) -> None:
        if self._rec is not None:
            self._rec.record(now, outcome, summary, symbol=symbol, output={"desk_turn": out})

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._last = raw.get("last_direction", self._last)
            self._pauses = {
                old: (datetime.fromisoformat(p["since"]), str(p["turn"]))
                for old, p in raw.get("pauses", {}).items()
            }
            self._released = {old: set(s) for old, s in raw.get("released", {}).items()}
            changed = raw.get("changed_at")
            self._changed = datetime.fromisoformat(changed) if changed else None
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            logger.exception("读 %s 失败，按没有暂停算", self._path)

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        data = {
            "last_direction": self._last,
            "pauses": {
                old: {"since": since.isoformat(), "turn": turn}
                for old, (since, turn) in self._pauses.items()
            },
            "released": {old: sorted(s) for old, s in self._released.items()},
            "changed_at": self._changed.isoformat() if self._changed else None,
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            logger.exception("写 %s 失败，重启后暂停可能丢失", self._path)
