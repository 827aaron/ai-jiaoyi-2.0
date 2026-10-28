"""作战卡单币方向的滞回（``[schedule] card_flip_confirm``）。

一个币的新会议结论和卡上生效的方向（long / short）不同时，先不换：卡上留着原指令、只清空
信号（不开新仓），记成「待确认翻转」；同一个币接下来的会（整轮、临时会都算）连续
``card_flip_confirm`` 场给出同一个新方向（并且从第一次出现起至少过了 ``card_flip_confirm_min``
分钟）才换成最新那场的结论。会议回到原方向就取消候选，照常换上新结论。

转中性（no_trade / both / 到手即作废）同样要确认：不开新仓本来就立刻生效（候选期卡上没有
信号），只是持仓管家的卡规则（反向卡 / 中性卡）看的是确认过的方向，确认前不因它平仓。
从中性转出一个方向不算翻转，立即生效。

状态存在 ``data/card_flip.json``（不进作战卡存档：契约禁止多余字段，回滚的旧代码要能读卡）。
每条候选记着它留在卡上的那条指令的身份；卡上那条被换掉（旧代码回滚后开过会、手工改卡）
就作废。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from jiaoyi.contracts import Bias, CardSymbol

logger = logging.getLogger(__name__)

FlipDir = Literal["long", "short", "neutral"]
DIR_TEXT: dict[str, str] = {"long": "做多", "short": "做空", "neutral": "中性（只看不做）"}
REASON_MARK = "｜原结论："
REASON_MAX = 1000


def card_direction(cs: CardSymbol) -> FlipDir:
    """这条指令的方向，口径和持仓管家的卡规则一致：闸门改成 no_trade 的按会议原方向
    （``no_trade_from``），到手即作废的按中性。"""
    if not cs.allowed_signals and "stale_on_arrival" in cs.reason:
        return "neutral"
    if cs.bias is Bias.LONG:
        return "long"
    if cs.bias is Bias.SHORT:
        return "short"
    if cs.bias is Bias.NO_TRADE and cs.no_trade_from is Bias.LONG:
        return "long"
    if cs.bias is Bias.NO_TRADE and cs.no_trade_from is Bias.SHORT:
        return "short"
    return "neutral"


def held_key(cs: CardSymbol) -> str:
    # issued_at / meeting_id / bias survive clamp_to_risk and pool-expiry extension, unlike
    # research_token, so a restart with changed risk limits keeps the candidate
    issued = cs.issued_at.isoformat() if cs.issued_at else ""
    return f"{cs.meeting_id or ''}|{issued}|{cs.bias.value}"


@dataclass(frozen=True)
class FlipCandidate:
    confirmed: FlipDir
    candidate: FlipDir
    count: int
    first_seen: datetime
    last_seen: datetime
    held: str
    """``held_key`` of the instruction left on the card while the flip is pending."""

    def to_json(self) -> dict[str, Any]:
        return {
            "confirmed": self.confirmed,
            "candidate": self.candidate,
            "count": self.count,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "held": self.held,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> FlipCandidate:
        first = datetime.fromisoformat(raw["first_seen"])
        last = datetime.fromisoformat(raw["last_seen"])
        if first.tzinfo is None or last.tzinfo is None:
            raise ValueError("时间必须带时区")
        confirmed, candidate = raw["confirmed"], raw["candidate"]
        if confirmed not in ("long", "short") or candidate not in DIR_TEXT:
            raise ValueError("方向不对")
        if candidate == confirmed or int(raw["count"]) < 1:
            raise ValueError("候选不成立")
        return cls(confirmed, candidate, int(raw["count"]), first, last, str(raw["held"]))


@dataclass(frozen=True)
class FlipOutcome:
    """``FlipBook.gate`` 的结果：上卡的指令，和要写进决策记录的事（None = 没有滞回的事）。"""

    card_symbol: CardSymbol
    event: str | None = None
    """pending / confirmed / cancelled / dropped"""
    detail: dict[str, Any] | None = None
    summary: str = ""


class FlipBook:
    """每个币的翻转候选。``need`` <= 1 时完全不介入（现状）。"""

    def __init__(
        self, need: int = 1, min_age: timedelta = timedelta(0), path: Path | None = None
    ) -> None:
        self.need = max(int(need), 1)
        self.min_age = min_age
        self._path = path
        self._flips: dict[str, FlipCandidate] = {}

    @property
    def on(self) -> bool:
        return self.need >= 2

    def get(self, symbol: str) -> FlipCandidate | None:
        return self._flips.get(symbol)

    def items(self) -> dict[str, FlipCandidate]:
        return dict(self._flips)

    def gate(
        self, symbol: str, fresh: CardSymbol, in_force: CardSymbol | None, now: datetime
    ) -> FlipOutcome:
        """新结论 ``fresh`` 要换掉卡上的 ``in_force``（没有 = None）时，决定上卡的是哪条。"""
        if not self.on:
            return FlipOutcome(fresh)
        prior = self._flips.get(symbol)
        if prior is not None and (
            in_force is None or in_force.allowed_signals or held_key(in_force) != prior.held
        ):
            prior = None  # the held instruction is gone: nothing left to confirm against
            self._flips.pop(symbol, None)
            self.save()
        confirmed = (
            prior.confirmed
            if prior is not None
            else (card_direction(in_force) if in_force is not None else "neutral")
        )
        new = card_direction(fresh)
        if confirmed == "neutral" or new == confirmed:
            if prior is None:
                return FlipOutcome(fresh)
            self._flips.pop(symbol, None)
            self.save()
            return FlipOutcome(
                fresh,
                "cancelled",
                {"confirmed": confirmed, "candidate": prior.candidate, "count": prior.count},
                f"{symbol} 会议回到原方向{DIR_TEXT[confirmed]}，取消待确认的翻转"
                f"（候选{DIR_TEXT[prior.candidate]} {prior.count}/{self.need}）",
            )
        assert in_force is not None
        same = prior is not None and prior.candidate == new
        count = prior.count + 1 if same and prior is not None else 1
        first = prior.first_seen if same and prior is not None else now
        age = now - first
        detail: dict[str, Any] = {
            "confirmed": confirmed,
            "candidate": new,
            "count": count,
            "need": self.need,
            "first_seen": first.isoformat(),
            "age_min": round(age.total_seconds() / 60, 1),
            "min_age_min": round(self.min_age.total_seconds() / 60, 1),
        }
        if count >= self.need and age >= self.min_age:
            self._flips.pop(symbol, None)
            self.save()
            return FlipOutcome(
                fresh,
                "confirmed",
                detail,
                f"{symbol} 翻转确认：连续 {count} 场会都是{DIR_TEXT[new]}，"
                f"卡上从{DIR_TEXT[confirmed]}换成新结论",
            )
        interim = self._interim(in_force, fresh, confirmed, new, count, now)
        self._flips[symbol] = FlipCandidate(confirmed, new, count, first, now, held_key(interim))
        self.save()
        return FlipOutcome(
            interim,
            "pending",
            detail,
            f"{symbol} 待确认翻转 {count}/{self.need}：会议改判{DIR_TEXT[new]}，"
            f"卡上仍按{DIR_TEXT[confirmed]}只看不做，持仓不按卡规则平",
        )

    def _interim(
        self,
        in_force: CardSymbol,
        fresh: CardSymbol,
        confirmed: FlipDir,
        new: FlipDir,
        count: int,
        now: datetime,
    ) -> CardSymbol:
        """候选期留在卡上的指令：原指令（方向、出会时间、关键位都不动，持仓管家认的就是它）
        只清空信号，有效期跟新结论走。"""
        base = in_force.reason.split(REASON_MARK, 1)[-1]
        head = (
            f"待确认翻转（{count}/{self.need}）：{now:%H:%M} UTC 的会改判{DIR_TEXT[new]}，"
            f"卡上仍按{DIR_TEXT[confirmed]}只看不做；确认前不开新仓，持仓不按卡规则平"
        )
        expires = fresh.expires_at
        if in_force.expires_at is not None and (expires is None or in_force.expires_at > expires):
            expires = in_force.expires_at
        return in_force.model_copy(
            update={
                "allowed_signals": [],
                "expires_at": expires,
                "reason": f"{head}{REASON_MARK}{base}"[:REASON_MAX],
            }
        )

    def clear(self) -> None:
        if self._flips:
            self._flips.clear()
            self.save()

    def restore(self, card_symbols: dict[str, CardSymbol]) -> None:
        """重启后读回候选；卡上留着的那条指令对不上的丢掉。``need`` <= 1 时清空文件里的候选。"""
        if self._path is None or not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            flips = {str(k): FlipCandidate.from_json(v) for k, v in raw["flips"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("翻转候选文件读不了（%s），从空开始", exc.__class__.__name__)
            return
        kept = {
            s: f
            for s, f in flips.items()
            if self.on
            and (cs := card_symbols.get(s)) is not None
            and not cs.allowed_signals
            and held_key(cs) == f.held
        }
        self._flips = kept
        if len(kept) != len(flips):
            self.save()
        if kept:
            logger.info("已恢复待确认翻转：%s", sorted(kept))

    def save(self) -> None:
        if self._path is None:
            return
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(
                    {"version": 1, "flips": {s: f.to_json() for s, f in self._flips.items()}},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except Exception:
            logger.exception("翻转候选写不进 %s：重启后候选从头数", self._path)
            with contextlib.suppress(OSError):
                tmp.unlink()
