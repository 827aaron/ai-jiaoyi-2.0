"""作战卡单币方向的滞回（``[schedule] card_flip_confirm``）。

一个币的新会议结论和卡上生效的方向（long / short）不同时，先不换：卡上留着原指令、只清空
信号（不开新仓），记成「待确认翻转」；同一个币连续 ``card_flip_confirm`` 场会（整轮、临时会
都算，第一场算 1）都和生效方向不同（反向、中性交替也算），并且从第一次出现起至少过了
``card_flip_confirm_min`` 分钟，才换成最新那场的结论。会议回到原方向就取消候选，照常换上新结论。

转中性（no_trade / both / 到手即作废）同样要确认：不开新仓本来就立刻生效（候选期卡上没有
信号），只是持仓管家的卡规则（反向卡 / 中性卡）看的是确认过的方向，确认前不因它平仓。
从中性转出一个方向不算翻转，立即生效。

``[fast_loop] card_flip_confirm_by = "assistant"``（热生效，只在 ``card_flip_confirm`` ≥ 2 时起
作用）：会只提候选，场数够了也不确认；同样的结论再来一场只是刷新候选、重新交给管家助手问，
确认只经 ``BattleCardCommander.confirm_flip``。会回到原方向照样取消。

``[fast_loop] card_neutral_flip = "ignore"``（热生效）：会判不做（中性）不算翻转，卡上留着
确认过的做多 / 做空那条、照常能开仓（有效期跟新结论走），不记候选；做多 ↔ 做空照旧。

状态存在 ``data/card_flip.json``（不进作战卡存档：契约禁止多余字段，回滚的旧代码要能读卡）。
每条候选记着它留在卡上的那条指令的身份；卡上那条没了（没有持仓的币过了有效期被拿掉）或
被换掉（旧代码回滚后开过会、手工改卡）就作废，记一条 ``dropped``。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Callable
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
    # bias and research_token are left out: clamp_to_risk on restart may turn the held
    # instruction into NO_TRADE (category cap now 0) or change its ranges
    issued = cs.issued_at.isoformat() if cs.issued_at else ""
    return f"{cs.meeting_id or ''}|{issued}"


@dataclass(frozen=True)
class FlipCandidate:
    confirmed: FlipDir
    candidate: FlipDir
    count: int
    first_seen: datetime
    last_seen: datetime
    held: str
    """``held_key`` of the instruction left on the card while the flip is pending."""
    signals: tuple[str, ...] | None = None
    """``allowed_signals`` of that instruction before the candidate emptied them; None for
    candidates written by older code."""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "confirmed": self.confirmed,
            "candidate": self.candidate,
            "count": self.count,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "held": self.held,
        }
        if self.signals is not None:
            out["signals"] = list(self.signals)
        return out

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
        signals = raw.get("signals")
        return cls(
            confirmed,
            candidate,
            int(raw["count"]),
            first,
            last,
            str(raw["held"]),
            tuple(str(s) for s in signals) if isinstance(signals, list) else None,
        )


@dataclass(frozen=True)
class FlipOutcome:
    """``FlipBook.gate`` 的结果：上卡的指令，和要写进决策记录的事（None = 没有滞回的事）。"""

    card_symbol: CardSymbol
    event: str | None = None
    """pending / confirmed / cancelled"""
    detail: dict[str, Any] | None = None
    summary: str = ""
    dropped: FlipCandidate | None = None
    """A stored candidate whose held instruction was gone before this meeting."""


class FlipBook:
    """每个币的翻转候选。``need`` <= 1 时完全不介入（现状）。"""

    def __init__(
        self, need: int = 1, min_age: timedelta = timedelta(0), path: Path | None = None
    ) -> None:
        self.need = max(int(need), 1)
        self.min_age = min_age
        self._path = path
        self._flips: dict[str, FlipCandidate] = {}
        self.by_assistant: Callable[[], bool] = lambda: False
        self.neutral_ignored: Callable[[], bool] = lambda: False

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
            if (
                in_force is not None
                and card_direction(in_force) != "neutral"
                and card_direction(fresh) == "neutral"
                and self.neutral_ignored()
            ):
                return self._ignore(symbol, in_force, fresh, card_direction(in_force), None)
            return FlipOutcome(fresh)
        prior = self._flips.get(symbol)
        dropped: FlipCandidate | None = None
        if prior is not None and (
            in_force is None or in_force.allowed_signals or held_key(in_force) != prior.held
        ):
            dropped, prior = prior, None
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
                return FlipOutcome(fresh, dropped=dropped)
            self._flips.pop(symbol, None)
            self.save()
            return FlipOutcome(
                fresh,
                "cancelled",
                {"confirmed": confirmed, "candidate": prior.candidate, "count": prior.count},
                f"{symbol} 会议回到原方向{DIR_TEXT[confirmed]}，取消待确认的翻转"
                f"（{self.progress(prior.count, prior.first_seen, now)}）",
            )
        assert in_force is not None
        if new == "neutral" and self.neutral_ignored():
            return self._ignore(symbol, in_force, fresh, confirmed, dropped)
        count = prior.count + 1 if prior is not None else 1
        first = prior.first_seen if prior is not None else now
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
        if count >= self.need and age >= self.min_age and not self.by_assistant():
            self._flips.pop(symbol, None)
            self.save()
            return FlipOutcome(
                fresh,
                "confirmed",
                detail,
                f"{symbol} 翻转确认：连续 {count} 场会都不是{DIR_TEXT[confirmed]}，"
                f"卡上换成最新这场的结论（{DIR_TEXT[new]}）",
                dropped,
            )
        interim = self._interim(in_force, fresh, confirmed, new, count, first, now)
        signals = (
            prior.signals if prior is not None else tuple(s.value for s in in_force.allowed_signals)
        )
        self._flips[symbol] = FlipCandidate(
            confirmed, new, count, first, now, held_key(interim), signals
        )
        self.save()
        return FlipOutcome(
            interim,
            "pending",
            detail,
            f"{symbol} 待确认翻转 {self.progress(count, first, now)}：会议改判{DIR_TEXT[new]}，"
            f"卡上仍按{DIR_TEXT[confirmed]}只看不做，持仓不按卡规则平",
            dropped,
        )

    @staticmethod
    def _ignore(
        symbol: str,
        in_force: CardSymbol,
        fresh: CardSymbol,
        confirmed: FlipDir,
        dropped: FlipCandidate | None,
    ) -> FlipOutcome:
        """会判不做、设置不翻转：卡上那条原样留着（候选期的那条也是，候选不动），有效期跟新结论走，
        免得过期从卡上掉下去。"""
        expires = fresh.expires_at
        if in_force.expires_at is not None and (expires is None or in_force.expires_at > expires):
            expires = in_force.expires_at
        return FlipOutcome(
            in_force.model_copy(update={"expires_at": expires}),
            "ignored",
            {"confirmed": confirmed, "candidate": "neutral"},
            f"{symbol} 会议判不做，按设置不翻转，保留原方向（{DIR_TEXT[confirmed]}）",
            dropped,
        )

    def progress(self, count: int, first: datetime, now: datetime) -> str:
        """「k/N」，候选时长还没到 ``min_age`` 时加「，等满 X 分钟」；只由管家助手确认时是
        「第 k 场改判，等管家助手确认」。"""
        if self.by_assistant():
            return f"第 {count} 场改判，等管家助手确认"
        text = f"{min(count, self.need)}/{self.need}"
        if self.min_age > timedelta(0) and now - first < self.min_age:
            text += f"，等满 {self.min_age.total_seconds() / 60:g} 分钟"
        return text

    def _interim(
        self,
        in_force: CardSymbol,
        fresh: CardSymbol,
        confirmed: FlipDir,
        new: FlipDir,
        count: int,
        first: datetime,
        now: datetime,
    ) -> CardSymbol:
        """候选期留在卡上的指令：原指令（方向、出会时间、关键位都不动，持仓管家认的就是它）
        只清空信号，有效期跟新结论走。"""
        base = in_force.reason.split(REASON_MARK, 1)[-1]
        head = (
            f"待确认翻转（{self.progress(count, first, now)}）：{now:%H:%M} UTC 的会改判"
            f"{DIR_TEXT[new]}，"
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

    def discard(self, symbol: str) -> None:
        if self._flips.pop(symbol, None) is not None:
            self.save()

    def clear(self) -> None:
        if self._flips:
            self._flips.clear()
            self.save()

    def restore(self, card_symbols: dict[str, CardSymbol]) -> dict[str, FlipCandidate]:
        """重启后读回候选；卡上留着的那条指令对不上的丢掉并返回（币 → 候选）。
        ``need`` <= 1 时清空文件里的候选。"""
        if self._path is None or not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            flips = {str(k): FlipCandidate.from_json(v) for k, v in raw["flips"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("翻转候选文件读不了（%s），从空开始", exc.__class__.__name__)
            return {}
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
        return {sym: f for sym, f in flips.items() if sym not in kept}

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
