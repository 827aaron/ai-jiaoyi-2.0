from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from jiaoyi import contracts as c
from jiaoyi.contracts import FastLoopConfig, KeyLevel, LevelKind, Outcome, Role, Side
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
from jiaoyi.gate.questions import HOLD_TWO, MARKET_GLOSSARY, hold_questions
from jiaoyi.keeper import assistant as ka
from jiaoyi.keeper.assistant import (
    QUESTION_ID,
    KeeperAssistant,
    TransportScorer,
    Verdict,
)
from jiaoyi.keeper.keeper import JevKeeper
from jiaoyi.slow.card_flip import FlipCandidate
from tests.fastloop_helpers import FAST, card, features
from tests.test_keeper_patrol import close_p, position

ON = {
    "keeper_assistant_on": True,
    "keeper_assistant_close": True,
    "keeper_assistant_b_record_only": False,
    "keeper_assistant_b_model": "microsoft/decision-1",
    "keeper_assistant_flip_confirm": True,
}


class Scorer:
    """Answers p(是) from a table; ``gate`` holds every call until set (concurrency checks)."""

    def __init__(self, p_yes: float | None, gate: asyncio.Event | None = None) -> None:
        self.p_yes = p_yes
        self.gate = gate
        self.calls: list[tuple[str, str, tuple[str, ...]]] = []
        self.hold_calls: list[tuple[str, tuple[c.JevQuestion, ...]]] = []
        self.started = asyncio.Event()

    async def ask(
        self,
        judge: str,
        symbol: str,
        state: str,
        question: str,
        options: Sequence[str],
        timeout_s: float,
    ) -> Verdict:
        self.calls.append((state, question, tuple(options)))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.p_yes is None:
            return Verdict(judge, None, None, tuple(options), "超时", 1.0)
        return Verdict(judge, "m", self.p_yes, tuple(options), None, 1.0, 0.001)

    async def ask_hold(
        self,
        judge: str,
        symbol: str,
        state: str,
        questions: Sequence[c.JevQuestion],
        timeout_s: float,
    ) -> Verdict:
        self.hold_calls.append((state, tuple(questions)))
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        opts = next(q.choices for q in questions if q.id == "action")
        if self.p_yes is None:
            return Verdict(judge, None, None, opts, "超时", 1.0, question="hold")
        return Verdict(judge, "m", self.p_yes, opts, None, 1.0, 0.001, question="hold")


def cand(now: datetime, direction: str = "long") -> FlipCandidate:
    return FlipCandidate("short", direction, 1, now, now, "m|t")  # type: ignore[arg-type]


class Cards:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.confirmed: list[tuple[str, FlipCandidate]] = []
        self.calls: list[str] = []
        self.answered: list[str] = []
        self.stuck: list[tuple[str, FlipCandidate]] = []

    def confirm_flip(
        self, symbol: str, expected: FlipCandidate, why: str, detail: dict[str, Any]
    ) -> bool:
        self.confirmed.append((symbol, expected))
        return self.ok

    def sweep_flips(self) -> None:
        self.calls.append("sweep")

    def clear_neutral_flips(self) -> None:
        self.calls.append("clear_neutral")

    def flip_answered(self, symbol: str, cand: FlipCandidate) -> None:
        self.answered.append(symbol)

    def stuck_held_flips(self, now: datetime, older: timedelta) -> list[tuple[str, FlipCandidate]]:
        return [(s, f) for s, f in self.stuck if now - f.first_seen > older]


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        now: datetime,
        a: Scorer | None,
        b: Scorer | None,
        *,
        cards: Cards | None = None,
        closing: bool = False,
        price: float = 103.7,
        hold: Any = None,
        **cfg: Any,
    ) -> None:
        self.cfg = FastLoopConfig(**{**ON, **cfg})
        self.clock = [now]
        self.closed: list[tuple[str, str]] = []
        self.closing = closing
        self.price = [price]
        self.snap_age = 0.0
        self.live_s = 3600.0
        self.alerts: list[tuple[str, str]] = []
        self.warns: list[tuple[str, str]] = []
        self.used_at_close: list[int] = []
        self.accepted: set[str] = set()
        """持仓 id：执行器真收下了平仓（主循环这时才记 close_sources）。"""
        self.log = JsonlDecisionLog(tmp_path)
        self.now = now
        self.a = KeeperAssistant(
            lambda: self.cfg,
            RoleRecorder(Role.KEEPER_ASSISTANT, self.log),
            features=lambda sym: features(
                self.clock[0] - timedelta(seconds=self.snap_age),
                price=self.price[0],
                live_s=self.live_s,
            ),
            close=self._close,
            clock=lambda: self.clock[0],
            data_dir=tmp_path,
            jev=a,
            b_scorer=(lambda model: b) if b is not None else None,
            cards=cards,
            alert=lambda title, text: self.alerts.append((title, text)),
            warn=lambda title, text: self.warns.append((title, text)),
            day_reset_tz="UTC",
            close_source=lambda pid: "keeper_assistant" if pid in self.accepted else None,
            hold=hold,
        )

    def features(self, sym: str) -> c.FeatureSnapshot:
        return features(
            self.clock[0] - timedelta(seconds=self.snap_age),
            price=self.price[0],
            live_s=self.live_s,
        )

    def _close(self, pos: c.PositionRecord, reason: str) -> str | None:
        self.used_at_close.append(self.a._slots.used)
        if self.closing:
            return "这笔正在平"
        self.closed.append((pos.position_id, reason))
        self.accepted.add(pos.position_id)
        return None

    async def settle(self) -> None:
        for _ in range(3):
            await asyncio.gather(*list(self.a._tasks))

    def records(self) -> list[dict[str, Any]]:
        return [
            r.output["keeper_assistant"]
            for r in self.log.read_day(self.now.date().isoformat())
            if "keeper_assistant" in r.output
        ]


def short_pos(now: datetime, **over: Any) -> c.PositionRecord:
    return position(now, side="short", entry_price=100.0, stop_loss_price=110.0, **over)


@pytest.mark.parametrize("entry", [90.0, 110.0])
async def test_KeeperAssistant_on_reverse_signal_closes_profit_or_loss_once_when_both_say_yes(
    tmp_path: Path, now: datetime, entry: float
) -> None:
    h = Harness(tmp_path, now, Scorer(0.8), Scorer(0.7))
    pos = position(now, side="short", entry_price=entry, stop_loss_price=entry * 1.1)
    h.a.on_reverse_signal(pos, Side.LONG, "volume_breakout")
    h.a.on_reverse_signal(pos, Side.LONG, "volume_breakout")
    await h.settle()
    assert h.closed == [("pos-1", "管家助手：反向信号，两个模型一致")]
    [rec] = h.records()
    assert rec["handed_off"] is True and rec["asked"] == "上涨"
    assert [j["p_yes"] for j in rec["judges"]] == [0.8, 0.7]
    rows = (tmp_path / "keeper_assistant" / f"{now:%Y-%m-%d}.jsonl").read_text().splitlines()
    assert json.loads(rows[0])["trigger"] == "reverse_signal"


@pytest.mark.parametrize(
    ("a", "b", "kw"),
    [
        (0.8, 0.4, {}),
        (0.4, 0.8, {}),
        (0.8, 0.59, {}),
        (None, 0.8, {}),
        (0.8, 0.8, {"keeper_assistant_b_record_only": True}),
        (0.8, 0.8, {"keeper_assistant_close": False}),
    ],
)
async def test_KeeperAssistant_on_reverse_signal_holds_unless_both_say_yes(
    tmp_path: Path, now: datetime, a: float | None, b: float | None, kw: dict[str, Any]
) -> None:
    h = Harness(tmp_path, now, Scorer(a), Scorer(b), **kw)
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    assert h.closed == []
    assert h.records()[0]["handed_off"] is False


@pytest.mark.parametrize("why", ["unset", "unbuilt", "cooling"])
async def test_KeeperAssistant_skips_without_asking_jev_when_b_is_unavailable(
    tmp_path: Path, now: datetime, why: str
) -> None:
    a = Scorer(0.9)
    b: Any = None
    kw: dict[str, Any] = {"keeper_assistant_gap_s": 0}
    if why == "unset":
        kw["keeper_assistant_b_model"] = None
    if why == "cooling":
        clock = [0.0]
        b = TransportScorer(
            MockJevTransport(fail=RuntimeError("503")),
            usd_per_mtok=0,
            max_errors=1,
            cooldown_s=60,
            clock=lambda: clock[0],
        )
        await b.ask("B", "X", "{}", "q", ("否", "是"), 1.0)
    h = Harness(tmp_path, now, a, b, **kw)
    pos = short_pos(now)
    for minutes in (0, 30, 61):
        h.clock[0] = now + timedelta(minutes=minutes)
        h.a.on_reverse_signal(pos, Side.LONG, "x")
        await h.settle()
    assert a.calls == [] and h.closed == [] and h.a._slots.used == 0
    recs = h.records()
    assert len(recs) == 2  # once per hour per reason
    assert all(r["skipped"].startswith("跳过：B 不可用") for r in recs)


@pytest.mark.parametrize(
    ("age", "live", "asked"), [(31.0, 3600.0, False), (5.0, 299.0, False), (30.0, 300.0, True)]
)
async def test_KeeperAssistant_asks_only_on_a_fresh_warm_snapshot(
    tmp_path: Path, now: datetime, age: float, live: float, asked: bool
) -> None:
    a = Scorer(0.9)
    cards = Cards()
    h = Harness(tmp_path, now, a, Scorer(0.9), cards=cards, keeper_assistant_gap_s=0)
    h.snap_age, h.live_s = age, live
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    h.a.on_flip_pending("BTCUSDT", cand(now))
    await h.settle()
    assert (len(a.calls) == 2) is asked and (len(cards.confirmed) == 1) is asked
    assert bool(h.closed) is asked
    if asked:
        assert [r["snapshot_source"] for r in ka.read_rows(tmp_path / "keeper_assistant")] == [
            "ws",
            "ws",
        ]
        return
    assert a.calls == []
    [rec] = h.records()  # once per coin per minute
    assert rec["skipped"] == "行情快照过旧/未预热，不问"
    assert (rec["reason"], rec["snapshot_age_s"]) == ("stale" if age > 30 else "cold", age)
    h.clock[0] = now + timedelta(minutes=1)
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    assert len(h.records()) == 2


async def test_KeeperAssistant_asks_with_the_injected_snapshot_provider(
    tmp_path: Path, now: datetime
) -> None:
    asked: list[tuple[str, float]] = []

    async def provider(symbol: str, max_age_s: float) -> tuple[Any, str, float | None]:
        asked.append((symbol, max_age_s))
        return features(now - timedelta(minutes=10), live_s=0.0), "rest", 0.5

    a = Scorer(0.9)
    h = Harness(tmp_path, now, a, Scorer(0.9), keeper_assistant_snapshot_max_age_s=12)
    h.a._snapshot = provider
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    assert asked == [("BTCUSDT", 12.0)] and len(a.calls) == 1
    [row] = ka.read_rows(tmp_path / "keeper_assistant")
    assert row["snapshot_source"] == "rest"


async def test_KeeperAssistant_on_reverse_signal_ignores_signals_with_the_position_or_when_off(
    tmp_path: Path, now: datetime
) -> None:
    a = Scorer(0.9)
    h = Harness(tmp_path, now, a, Scorer(0.9))
    h.a.on_reverse_signal(short_pos(now), Side.SHORT, "x")
    off = Harness(tmp_path / "off", now, a, Scorer(0.9), keeper_assistant_on=False)
    off.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    await off.settle()
    assert a.calls == [] and h.closed == [] and off.closed == []


@pytest.mark.parametrize("glossary", [False, True])
async def test_KeeperAssistant_on_reverse_signal_asks_both_at_once_blind_and_mirrored(
    tmp_path: Path, now: datetime, glossary: bool
) -> None:
    gate = asyncio.Event()
    a, b = Scorer(0.9, gate), Scorer(0.9, gate)
    h = Harness(tmp_path, now, a, b, keeper_assistant_blind_glossary=glossary)
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "强平反弹")
    await asyncio.wait_for(asyncio.gather(a.started.wait(), b.started.wait()), 1.0)
    gate.set()
    await h.settle()
    (state_a, q_a, opts_a), (state_b, q_b, opts_b) = a.calls[0], b.calls[0]
    assert opts_a == ("是", "否") and opts_b == ("否", "是")
    lead = MARKET_GLOSSARY + "\n" if glossary else ""
    assert q_a == lead + "BTCUSDT 接下来 1–4 小时会上涨吗？（是 / 否）"
    assert q_b == lead + "BTCUSDT 接下来 1–4 小时会上涨吗？（否 / 是）"
    assert a.hold_calls == [] and b.hold_calls == []
    q_a = q_a.splitlines()[-1]
    assert state_a == state_b
    snapshot = json.loads(state_a)
    assert snapshot["sym"] == "BTCUSDT"
    for word in ("pos", "side", "entry", "pnl", "card", "current_market", "against", "kronos"):
        assert word not in snapshot
    for word in ("100.0", "110.0", "强平反弹", "pos-1"):
        assert word not in state_a
    for word in ("short", "空", "持仓", "仓位", "反向", "信号", "强平反弹", "100", "110"):
        assert word not in q_a
    assert h.closed


HOLD = {"keeper_assistant_question": "hold"}


class Jev:
    """The keeper's Jev: answers p(close) and keeps every (state, questions) it was sent."""

    def __init__(self, p: float) -> None:
        self.seen: list[tuple[str, tuple[c.JevQuestion, ...]]] = []
        self.p = p
        self.t = MockJevTransport(answer=self.answer)

    def answer(self, state: str, qs: Sequence[c.JevQuestion]) -> Mapping[str, Mapping[str, float]]:
        self.seen.append((state, tuple(qs)))
        return close_p(self.p)(state, qs)  # type: ignore[no-any-return]


def against(now: datetime) -> c.AgainstSignalEvent:
    return c.AgainstSignalEvent(
        symbol="BTCUSDT", ts=now, kind=c.SignalKind.VOLUME_BREAKOUT, side=Side.LONG, desc="反向"
    )


def hold_harness(
    tmp_path: Path, now: datetime, a: Scorer, b: Scorer, jev: Jev, **cfg: Any
) -> tuple[Harness, JevKeeper]:
    keeper: list[JevKeeper] = []

    class Late:
        """The keeper reads the harness's clock-bound features, so it is built after it."""

        async def hold_request(self, *args: Any) -> Any:
            return await keeper[0].hold_request(*args)

        async def wait_hold(self, *args: Any) -> Any:
            return await keeper[0].wait_hold(*args)

    h = Harness(tmp_path, now, a, b, hold=Late(), **{**HOLD, **cfg})
    keeper.append(
        JevKeeper(
            JevDecisionModel(jev.t, FAST, timeout_ms=200),
            h.cfg,
            h.features,
            lambda n: card(now, bias="short"),
            RoleRecorder(Role.KEEPER, h.log),
        )
    )
    return h, keeper[0]


async def test_KeeperAssistant_hold_asks_both_judges_the_keepers_question_and_snapshot(
    tmp_path: Path, now: datetime
) -> None:
    a, b, jev = Scorer(0.8), Scorer(0.7), Jev(0.1)
    h, keeper = hold_harness(tmp_path, now, a, b, jev)
    pos, sig = short_pos(now), against(now)
    h.a.on_reverse_signal(pos, Side.LONG, "x", sig)
    await h.settle()
    keeper.note_against(pos.position_id, sig, False)
    await keeper.review(pos, now)
    [(keeper_state, keeper_qs)] = jev.seen
    [(state_a, qs_a)], [(state_b, qs_b)] = a.hold_calls, b.hold_calls
    assert [q.id for q in keeper_qs] == ["thesis_intact", "action"]  # no adverse_risk
    assert a.calls == [] and b.calls == []
    assert state_a == state_b == keeper_state
    assert qs_a == keeper_qs
    assert [(q.id, q.prompt) for q in qs_b] == [(q.id, q.prompt) for q in keeper_qs]
    assert [q.choices for q in qs_b] == [tuple(reversed(q.choices)) for q in keeper_qs]
    snapshot = json.loads(state_a)
    assert {"pos", "card", "against", "trail"} <= set(snapshot)
    assert MARKET_GLOSSARY in "".join(q.prompt for q in qs_a)
    [rec] = h.records()
    assert rec["question"] == "hold" and rec["a_reused"] is False
    assert rec["snapshot_hash"] == ka.snapshot_hash(keeper_state)
    assert rec["p_close"] == [0.8, 0.7]
    assert [j["p_close"] for j in rec["judges"]] == [0.8, 0.7]


@pytest.mark.parametrize(
    ("pa", "pb", "closed"),
    [
        (0.8, 0.6, True),
        (0.8, 0.4, False),
        (0.4, 0.8, False),
        (0.8, 0.59, False),
        (None, 0.9, False),
    ],
)
async def test_KeeperAssistant_hold_closes_only_when_both_say_close(
    tmp_path: Path, now: datetime, pa: float | None, pb: float | None, closed: bool
) -> None:
    h, _ = hold_harness(tmp_path, now, Scorer(pa), Scorer(pb), Jev(0.1))
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x", against(now))
    await h.settle()
    assert bool(h.closed) is closed
    [rec] = h.records()
    assert (rec["would_close"], rec["p_close"]) == (closed, [pa, pb])


@pytest.mark.parametrize(
    ("after_s", "same_signal", "reuse_s", "reused"),
    [(5, True, 10, True), (11, True, 10, False), (5, False, 10, False), (5, True, 0, False)],
)
async def test_KeeperAssistant_hold_reuses_the_keepers_fresh_answer_to_the_same_signal(
    tmp_path: Path, now: datetime, after_s: int, same_signal: bool, reuse_s: int, reused: bool
) -> None:
    a, b, jev = Scorer(0.1), Scorer(0.9), Jev(0.8)
    h, keeper = hold_harness(tmp_path, now, a, b, jev, keeper_assistant_reuse_s=reuse_s)
    pos, sig = short_pos(now), against(now)
    keeper.note_against(pos.position_id, sig, False)
    await keeper.review(pos, now)
    [(keeper_state, _)] = jev.seen
    h.clock[0] = now + timedelta(seconds=after_s)
    h.a.on_reverse_signal(pos, Side.LONG, "x", sig if same_signal else against(h.clock[0]))
    await h.settle()
    [(state_b, _)] = b.hold_calls
    [rec] = h.records()
    assert len(jev.seen) == 1
    assert rec["a_reused"] is reused
    if reused:
        assert a.hold_calls == [] and state_b == keeper_state
        assert rec["judges"][0]["reused"] is True and rec["p_close"] == [0.8, 0.9]
        assert rec["a_reused_age_s"] == after_s and bool(h.closed)
    else:
        [(state_a, _)] = a.hold_calls
        assert state_a == state_b != keeper_state
        assert rec["p_close"] == [0.1, 0.9] and not h.closed


async def test_KeeperAssistant_hold_takes_assistant_1_from_the_keepers_urgent_check(
    tmp_path: Path, now: datetime
) -> None:
    a, b, jev = Scorer(0.1), Scorer(0.9), Jev(0.8)
    h, keeper = hold_harness(tmp_path, now, a, b, jev)
    pos, sig = short_pos(now), against(now)
    h.a.on_reverse_signal(pos, Side.LONG, "x", sig)
    keeper.note_against(pos.position_id, sig, False)  # the main loop's order
    for _ in range(5):
        await asyncio.sleep(0)
    assert keeper._ask_waiters.get(pos.position_id)  # waiting for the keeper, not asking Jev
    await keeper.review(pos, now)
    await h.settle()
    [(keeper_state, _)] = jev.seen
    [(state_b, _)] = b.hold_calls
    assert a.hold_calls == [] and state_b == keeper_state
    [rec] = h.records()
    assert rec["a_reused"] is True and rec["p_close"] == [0.8, 0.9] and h.closed
    assert rec["chain"] == (
        "管家问 → 助手 1（Jev）p(平) 0.80（管家急查那次） / 助手 2（m）p(平) 0.90 → 平"
    )


async def test_KeeperAssistant_hold_asks_assistant_1_itself_when_the_keepers_jev_fails(
    tmp_path: Path, now: datetime
) -> None:
    a, b, jev = Scorer(0.8), Scorer(0.9), Jev(0.8)
    jev.t.fail = RuntimeError("down")
    h, keeper = hold_harness(tmp_path, now, a, b, jev)
    pos, sig = short_pos(now), against(now)
    h.a.on_reverse_signal(pos, Side.LONG, "x", sig)
    keeper.note_against(pos.position_id, sig, False)
    for _ in range(5):
        await asyncio.sleep(0)
    await keeper.review(pos, now)
    await h.settle()
    [(state_a, _)] = a.hold_calls
    [(state_b, _)] = b.hold_calls
    assert state_a == state_b
    [rec] = h.records()
    assert rec["a_reused"] is False and rec["p_close"] == [0.8, 0.9] and h.closed
    assert "管家急查的 Jev 没答上" in rec["a_keeper_error"]


async def test_KeeperAssistant_hold_does_not_ask_once_the_position_is_gone(
    tmp_path: Path, now: datetime
) -> None:
    a, b = Scorer(0.9), Scorer(0.9)
    h, _ = hold_harness(tmp_path, now, a, b, Jev(0.1))
    h.a._still_open = lambda pid: False
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x", against(now))
    await h.settle()
    assert a.hold_calls == b.hold_calls == [] and not h.closed
    [rec] = h.records()
    assert rec["skipped"] == ka.CLOSED_MEANWHILE


async def test_KeeperAssistant_hold_errors_never_cool_down_the_flip_confirmation(
    tmp_path: Path, now: datetime
) -> None:
    made: list[MockJevTransport] = []

    def factory(model: str) -> TransportScorer:
        made.append(MockJevTransport(fail=RuntimeError("503")))
        return TransportScorer(made[-1], usd_per_mtok=0, max_errors=1, cooldown_s=600)

    cards = Cards()
    h, _ = hold_harness(tmp_path, now, Scorer(0.9), Scorer(0.9), Jev(0.1), cards=cards)
    h.a._b_factory = factory
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x", against(now))
    await h.settle()
    [hold_lane] = made
    assert len(hold_lane.calls) == 1 and h.a._b["hold"][1].cooling()  # type: ignore[union-attr]
    h.a.on_flip_pending("BTCUSDT", cand(now))
    await h.settle()
    assert len(made) == 2 and len(made[1].calls) == 1  # the flip lane still asks B


def test_b_questions_reverses_every_option_and_can_fold_into_one_action_question() -> None:
    qs = hold_questions("direction", None, HOLD_TWO)
    both = ka.b_questions(qs, False)
    assert [(q.id, q.prompt, q.choices) for q in both] == [
        (q.id, q.prompt, tuple(reversed(q.choices))) for q in qs
    ]
    [one] = ka.b_questions(qs, True)
    assert one.id == "action" and one.choices == ("close", "hold")
    assert one.prompt == qs[0].prompt + " " + qs[1].prompt and MARKET_GLOSSARY in one.prompt


async def test_probe_hold_retries_b_with_action_only_when_several_questions_fail(
    now: datetime,
) -> None:
    def b_answer(state: str, qs: Sequence[c.JevQuestion]) -> Mapping[str, Mapping[str, float]]:
        if len(qs) > 1:
            raise RuntimeError("only one question per request")
        return {"action": {"close": 0.7, "hold": 0.3}}

    a = MockJevTransport(answer=close_p(0.4), model="jev")
    b = MockJevTransport(answer=b_answer, model="ms")
    state = ka.probe_hold_snapshot("ETHUSDT", Side.SHORT, now, "direction")
    qs = hold_questions("direction", state, HOLD_TWO)
    rows = await ka.probe_hold([("B", b), ("A", a)], FastLoopConfig(), symbol="ETHUSDT",
                               state=state, questions=qs)  # fmt: skip
    assert [(r["judge"], r["action_only"], r["p_close"]) for r in rows] == [
        ("B", False, None),
        ("B", True, 0.7),
        ("A", False, 0.4),
    ]
    assert rows[2]["questions"] == ["thesis_intact", "action"]
    assert {"pos", "against", "trail"} <= set(json.loads(state))


def test_keeper_assistant_probe_cli_hold_sends_the_two_hold_questions(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jiaoyi import cli
    from jiaoyi.gate import jev
    from tests.conftest import ROOT

    reply = {
        "id": "gen-dec-2",
        "model": "microsoft/microsoft-decision-1-20261020",
        "answers": {
            "thesis_intact": {"probabilities": {"yes": 0.4, "no": 0.6}},
            "action": {"probabilities": {"hold": 0.25, "close": 0.75}},
        },
        "usage": {"input_tokens": 900, "output_tokens": 0, "cost": 0.00004},
    }
    fake = Decisions(reply)
    real = jev.OpenRouterDecisionsTransport

    def with_fake(key: str, model: str, **kw: Any) -> Any:
        return real(key, model, http_transport=httpx.MockTransport(fake))

    monkeypatch.setattr(jev, "OpenRouterDecisionsTransport", with_fake)
    monkeypatch.setattr(cli.ChainedSecretStore, "get", lambda self, k: "sk-or-test")
    app = ROOT / "config" / "app.example.toml"
    assert cli.main(["keeper-assistant-probe", "--app", str(app), "--model", DECISION_1,
                     "--hold"]) == 0  # fmt: skip
    out = capsys.readouterr().out
    assert "p(平)：0.7500" in out and "2 道题一起" in out
    [req] = fake.requests
    body = json.loads(req.content)
    assert list(body["questions"]) == ["thesis_intact", "action"]
    assert MARKET_GLOSSARY in body["questions"]["thesis_intact"]["instructions"]
    assert "pos" in json.loads(body["state"])


async def test_KeeperAssistant_hold_skips_without_the_keeper(tmp_path: Path, now: datetime) -> None:
    a, b = Scorer(0.9), Scorer(0.9)
    h = Harness(tmp_path, now, a, b, **HOLD)
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x", against(now))
    await h.settle()
    assert a.hold_calls == a.calls == b.hold_calls == [] and not h.closed
    [rec] = h.records()
    assert rec["question"] == "hold" and "管家没接上" in rec["skipped"]


async def test_KeeperAssistant_gap_and_in_flight_dedup(tmp_path: Path, now: datetime) -> None:
    gate = asyncio.Event()
    a = Scorer(0.1, gate)
    h = Harness(tmp_path, now, a, Scorer(0.1), keeper_assistant_gap_s=0)
    pos = short_pos(now)
    h.a.on_reverse_signal(pos, Side.LONG, "x")
    h.a.on_reverse_signal(pos, Side.LONG, "x")
    gate.set()
    await h.settle()
    assert len(a.calls) == 1
    h.a.on_reverse_signal(pos, Side.LONG, "x")
    await h.settle()
    assert len(a.calls) == 2  # gap 0: only the in-flight check is deduplicated

    g = Harness(tmp_path / "g", now, a, Scorer(0.1), keeper_assistant_gap_s=120)
    g.a.on_reverse_signal(pos, Side.LONG, "x")
    await g.settle()
    g.clock[0] = now + timedelta(seconds=119)
    g.a.on_reverse_signal(pos, Side.LONG, "x")
    await g.settle()
    assert len(a.calls) == 3
    g.clock[0] = now + timedelta(seconds=120)
    g.a.on_reverse_signal(pos, Side.LONG, "x")
    await g.settle()
    assert len(a.calls) == 4


async def test_KeeperAssistant_no_double_close_when_already_closing(
    tmp_path: Path, now: datetime
) -> None:
    h = Harness(tmp_path, now, Scorer(0.9), Scorer(0.9), closing=True)
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    [rec] = h.records()
    assert rec["handed_off"] is False and "正在平" in rec["why"]


async def test_KeeperAssistant_on_price_triggers_on_adverse_key_level_cross_only_when_on(
    tmp_path: Path, now: datetime
) -> None:
    levels = [KeyLevel(price=105.0, kind=LevelKind.RESISTANCE)]
    a = Scorer(0.1)
    off = Harness(tmp_path / "off", now, a, Scorer(0.1))
    pos = short_pos(now)
    off.a.on_price(pos, levels, 104.0)
    off.a.on_price(pos, levels, 106.0)
    await off.settle()
    assert a.calls == []
    h = Harness(tmp_path, now, a, Scorer(0.1), keeper_assistant_key_level=True)
    h.a.on_price(pos, levels, 106.0)  # first price only seeds
    h.a.on_price(pos, levels, 104.0)  # downwards: favours the short
    await h.settle()
    assert a.calls == []
    h.a.on_price(pos, levels, 105.5)
    await h.settle()
    assert len(a.calls) == 1 and h.records()[0]["trigger"] == "key_level"


async def test_KeeperAssistant_crash_in_a_judge_is_recorded_and_frees_the_coin(
    tmp_path: Path, now: datetime
) -> None:
    class Broken(Scorer):
        async def ask(self, *a: Any, **k: Any) -> Verdict:
            raise RuntimeError("boom")

    h = Harness(tmp_path, now, Broken(None), Scorer(0.9))
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    assert h.closed == [] and h.a.busy == frozenset()
    recs = h.log.read_day(now.date().isoformat())
    assert [r.outcome for r in recs] == [Outcome.ERROR]


@pytest.mark.parametrize(
    ("a", "b", "asked"), [(0.9, 0.8, True), (0.9, 0.3, False), (0.9, None, False)]
)
async def test_KeeperAssistant_on_flip_pending_confirms_only_when_both_say_yes(
    tmp_path: Path, now: datetime, a: float, b: float | None, asked: bool
) -> None:
    sa, sb = Scorer(a), Scorer(b)
    cards = Cards()
    h = Harness(tmp_path, now, sa, sb, cards=cards)
    c0 = cand(now)
    h.a.on_flip_pending("BTCUSDT", c0)
    await h.settle()
    assert cards.confirmed == ([("BTCUSDT", c0)] if asked else [])
    assert sa.calls[0][1] == "BTCUSDT 接下来 1–4 小时会上涨吗？（是 / 否）"


async def test_KeeperAssistant_on_flip_pending_off_or_neutral_does_not_ask(
    tmp_path: Path, now: datetime
) -> None:
    a = Scorer(0.9)
    off = Harness(tmp_path / "off", now, a, Scorer(0.9), keeper_assistant_flip_confirm=False)
    off.a.on_flip_pending("BTCUSDT", cand(now))
    h = Harness(tmp_path, now, a, Scorer(0.9), cards=Cards())
    h.a.on_flip_pending("BTCUSDT", cand(now, "neutral"))
    await off.settle()
    await h.settle()
    assert a.calls == []


def neutral_cand(now: datetime, confirmed: str = "long") -> FlipCandidate:
    return FlipCandidate(confirmed, "neutral", 1, now, now, "m|t")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("confirmed", "a", "b", "agreed"),
    [
        ("long", 0.1, 0.4, True),
        ("short", 0.3, 0.2, True),
        ("long", 0.1, 0.7, False),
        ("long", 0.45, 0.1, False),
        ("long", 0.1, 0.55, False),
        ("long", 0.1, None, False),
    ],
)
async def test_KeeperAssistant_on_flip_pending_neutral_confirms_only_when_both_say_no(
    tmp_path: Path, now: datetime, confirmed: str, a: float, b: float | None, agreed: bool
) -> None:
    sa, sb = Scorer(a), Scorer(b)
    cards = Cards()
    h = Harness(tmp_path, now, sa, sb, cards=cards, keeper_assistant_flip_neutral=True)
    c0 = neutral_cand(now, confirmed)
    h.a.on_flip_pending("BTCUSDT", c0)
    await h.settle()
    move = "上涨" if confirmed == "long" else "下跌"
    assert sa.calls[0][1] == f"BTCUSDT 接下来 1–4 小时会{move}吗？（是 / 否）"
    assert sb.calls[0][1] == f"BTCUSDT 接下来 1–4 小时会{move}吗？（否 / 是）"
    assert sa.calls[0][0] == sb.calls[0][0]
    assert cards.confirmed == ([("BTCUSDT", c0)] if agreed else [])
    [rec] = h.records()
    assert (rec["kind"], rec["candidate"], rec["asked"]) == ("flip_neutral", "neutral", move)
    assert rec["both_no"] is agreed
    assert rec["result"] == ("confirmed" if agreed else "not_agreed")
    [row] = ka.read_rows(tmp_path / "keeper_assistant")
    assert row["kind"] == "flip_neutral"


@pytest.mark.parametrize(
    ("min_p", "p", "no"),
    [(0.7, 0.3, True), (0.9, 0.1, True), (0.6, 0.4, True), (0.7, 0.31, False), (0.6, 0.5, False)],
)
def test_Verdict_no_accepts_exactly_one_minus_min_p(min_p: float, p: float, no: bool) -> None:
    assert Verdict("A", "m", p, ("是", "否"), None, 1.0).no(min_p) is no


@pytest.mark.parametrize(("b", "answered"), [(0.9, True), (None, False)])
async def test_KeeperAssistant_tells_the_commander_only_when_both_judges_answered(
    tmp_path: Path, now: datetime, b: float | None, answered: bool
) -> None:
    cards = Cards()
    h = Harness(tmp_path, now, Scorer(0.2), Scorer(b), cards=cards)
    h.a.on_flip_pending("BTCUSDT", cand(now))
    await h.settle()
    assert cards.answered == (["BTCUSDT"] if answered else [])


async def test_KeeperAssistant_warns_once_when_a_held_coin_flip_is_stuck(
    tmp_path: Path, now: datetime
) -> None:
    cards = Cards()
    h = Harness(tmp_path, now, Scorer(0.2), Scorer(0.2), cards=cards)
    cards.stuck = [("BTCUSDT", cand(now))]
    h.a.tick(now + timedelta(hours=2))
    assert h.warns == []
    h.a.tick(now + timedelta(hours=2, minutes=1))
    h.a.tick(now + timedelta(hours=3))
    assert len(h.warns) == 1 and "BTCUSDT 持仓中" in h.warns[0][1]
    [rec] = h.log.read_day(now.date().isoformat())
    assert rec.outcome is Outcome.ERROR and rec.output["keeper_assistant"]["kind"] == "flip_stuck"


async def test_KeeperAssistant_on_flip_pending_neutral_off_does_not_ask(
    tmp_path: Path, now: datetime
) -> None:
    a = Scorer(0.1)
    cards = Cards()
    off = Harness(tmp_path / "off", now, a, Scorer(0.1), cards=cards)
    off.a.on_flip_pending("BTCUSDT", neutral_cand(now))
    main_off = Harness(
        tmp_path,
        now,
        a,
        Scorer(0.1),
        cards=cards,
        keeper_assistant_flip_neutral=True,
        keeper_assistant_on=False,
    )
    main_off.a.on_flip_pending("BTCUSDT", neutral_cand(now))
    await off.settle()
    await main_off.settle()
    assert a.calls == [] and cards.confirmed == []


async def test_KeeperAssistant_on_flip_pending_neutral_stale_candidate_is_dropped(
    tmp_path: Path, now: datetime
) -> None:
    cards = Cards(ok=False)
    h = Harness(
        tmp_path, now, Scorer(0.1), Scorer(0.1), cards=cards, keeper_assistant_flip_neutral=True
    )
    h.a.on_flip_pending("BTCUSDT", neutral_cand(now))
    await h.settle()
    assert len(cards.confirmed) == 1
    [rec] = h.records()
    assert (rec["kind"], rec["result"]) == ("flip_neutral", "stale")


async def test_KeeperAssistant_on_flip_pending_neutral_skips_when_b_is_unavailable(
    tmp_path: Path, now: datetime
) -> None:
    a = Scorer(0.1)
    cards = Cards()
    h = Harness(tmp_path, now, a, None, cards=cards, keeper_assistant_flip_neutral=True)
    h.a.on_flip_pending("BTCUSDT", neutral_cand(now))
    await h.settle()
    assert a.calls == [] and cards.confirmed == []
    [rec] = h.records()
    assert rec["kind"] == "flip_neutral" and rec["skipped"].startswith("跳过：B 不可用")


@pytest.mark.parametrize(
    ("kw", "candidate", "why"),
    [
        ({"keeper_assistant_on": False}, "long", "keeper_assistant_on 关着"),
        ({"keeper_assistant_flip_confirm": False}, "long", "keeper_assistant_flip_confirm 关着"),
        ({}, "neutral", "keeper_assistant_flip_neutral 关着"),
    ],
)
async def test_KeeperAssistant_on_flip_pending_by_assistant_logs_why_it_did_not_ask(
    tmp_path: Path, now: datetime, kw: dict[str, Any], candidate: str, why: str
) -> None:
    a = Scorer(0.9)
    by = {"card_flip_confirm_by": "assistant"}
    h = Harness(tmp_path, now, a, Scorer(0.9), cards=Cards(), **by, **kw)
    h.a.on_flip_pending("BTCUSDT", cand(now, candidate))
    h.a.on_flip_pending("BTCUSDT", cand(now, candidate))
    await h.settle()
    assert a.calls == []
    assert [r["skipped"] for r in h.records()] == [why, why]


async def test_KeeperAssistant_on_flip_pending_asks_again_only_after_the_gap(
    tmp_path: Path, now: datetime
) -> None:
    a = Scorer(0.3)
    h = Harness(tmp_path, now, a, Scorer(0.3), cards=Cards(), card_flip_confirm_by="assistant")
    h.a.on_flip_pending("BTCUSDT", cand(now))
    await h.settle()
    h.clock[0] = now + timedelta(seconds=60)
    h.a.on_flip_pending("BTCUSDT", replace(cand(now), count=2))
    await h.settle()
    h.clock[0] = now + timedelta(seconds=121)
    h.a.on_flip_pending("BTCUSDT", replace(cand(now), count=3))
    await h.settle()
    assert len(a.calls) == 2
    recs = h.records()
    assert [r.get("result") or r["skipped"] for r in recs] == [
        "not_agreed",
        "离上次问不到 120 秒",
        "not_agreed",
    ]
    assert recs[0]["why"].endswith("候选照旧只看不做，下一场会同样结论时再问")


async def test_KeeperAssistant_tick_hands_over_pending_flips_once_per_switch(
    tmp_path: Path, now: datetime
) -> None:
    cards = Cards()
    h = Harness(tmp_path, now, Scorer(0.1), Scorer(0.1), cards=cards)
    h.a.tick(now)
    assert cards.calls == []
    h.cfg = h.cfg.model_copy(
        update={"card_flip_confirm_by": "assistant", "card_neutral_flip": "ignore"}
    )
    h.a.tick(now)
    h.a.tick(now)
    assert cards.calls == ["clear_neutral", "sweep"]
    h.cfg = h.cfg.model_copy(
        update={"card_flip_confirm_by": "meeting", "card_neutral_flip": "flip"}
    )
    h.a.tick(now)
    h.cfg = h.cfg.model_copy(
        update={"card_flip_confirm_by": "assistant", "card_neutral_flip": "ignore"}
    )
    h.a.tick(now)
    assert cards.calls == ["clear_neutral", "sweep"] * 2


async def test_KeeperAssistant_on_flip_pending_neutral_not_asked_when_neutral_flips_are_ignored(
    tmp_path: Path, now: datetime
) -> None:
    a = Scorer(0.1)
    h = Harness(
        tmp_path,
        now,
        a,
        Scorer(0.1),
        cards=Cards(),
        keeper_assistant_flip_neutral=True,
        card_neutral_flip="ignore",
    )
    h.a.on_flip_pending("BTCUSDT", neutral_cand(now))
    await h.settle()
    assert a.calls == [] and h.records() == []


async def test_KeeperAssistant_on_flip_pending_while_position_check_runs_is_deferred_not_dropped(
    tmp_path: Path, now: datetime
) -> None:
    gate = asyncio.Event()
    a = Scorer(0.1, gate)
    cards = Cards()
    h = Harness(tmp_path, now, a, Scorer(0.9), cards=cards)
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await asyncio.wait_for(a.started.wait(), 1.0)
    h.a.on_flip_pending("BTCUSDT", cand(now))
    assert len(a.calls) == 1  # only the position check is asking
    a.p_yes = 0.9
    gate.set()
    await h.settle()
    questions = [q for _, q, _ in a.calls]
    assert questions == [
        "BTCUSDT 接下来 1–4 小时会上涨吗？（是 / 否）",
        "BTCUSDT 接下来 1–4 小时会上涨吗？（是 / 否）",
    ]
    assert len(cards.confirmed) == 1


async def test_KeeperAssistant_position_checks_take_slots_before_flip_confirms(
    tmp_path: Path, now: datetime
) -> None:
    gate = asyncio.Event()
    order: list[str] = []

    class Tagged(Scorer):
        async def ask(self, judge: str, symbol: str, *a: Any, **k: Any) -> Verdict:
            order.append(symbol)
            return await super().ask(judge, symbol, *a, **k)

    a = Tagged(0.1, gate)
    h = Harness(tmp_path, now, a, Scorer(0.1, gate), cards=Cards(), keeper_assistant_concurrency=1)
    h.a.on_reverse_signal(short_pos(now, symbol="AAUSDT", position_id="p-a"), Side.LONG, "x")
    await asyncio.sleep(0)
    h.a.on_flip_pending("BBUSDT", cand(now))
    h.a.on_reverse_signal(short_pos(now, symbol="CCUSDT", position_id="p-c"), Side.LONG, "x")
    for _ in range(10):  # everyone is queued for the one slot
        await asyncio.sleep(0)
    gate.set()
    await h.settle()
    assert order == ["AAUSDT", "CCUSDT", "BBUSDT"]


async def test_KeeperAssistant_judges_share_one_hard_timeout_and_then_hold(
    tmp_path: Path, now: datetime
) -> None:
    never = asyncio.Event()
    h = Harness(tmp_path, now, Scorer(0.9), Scorer(0.9, never), keeper_assistant_timeout_s=0.2)
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await asyncio.wait_for(h.settle(), 2.0)
    assert h.closed == []
    [rec] = h.records()
    assert [j["p_yes"] for j in rec["judges"]] == [None, None]
    assert "0.2 秒" in rec["judges"][0]["error"]
    assert h.a._slots.used == 0


async def test_KeeperAssistant_closes_after_its_slots_are_free(
    tmp_path: Path, now: datetime
) -> None:
    h = Harness(tmp_path, now, Scorer(0.9), Scorer(0.9))
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    assert h.closed and h.used_at_close == [0]


async def test_KeeperAssistant_records_counterfactual_inputs_and_exit(
    tmp_path: Path, now: datetime
) -> None:
    h = Harness(tmp_path, now, Scorer(0.9), Scorer(0.9), keeper_assistant_close=False)
    pos = short_pos(now)
    h.a.on_reverse_signal(pos, Side.LONG, "x")
    await h.settle()
    h.a.on_closed(pos.model_copy(update={"exit_price": 108.0, "status": "closed"}), 107.0)
    h.a.on_closed(pos, 107.0)  # second end of the same position writes nothing
    rows = ka.read_rows(tmp_path / "keeper_assistant")
    check, closed = rows
    assert check["would_close"] is True and check["handed_off"] is False
    assert (check["price_at_signal"], check["r_at_signal"]) == (103.7, -0.37)
    assert (closed["kind"], closed["exit_price"], closed["r_exit"]) == ("closed", 108.0, -0.8)
    pairs, open_n = ka.counterfactual(rows, {})
    assert open_n == 0 and len(pairs) == 1
    p = pairs[0]
    assert (p.r_signal, p.r_exit, p.delta_r) == (-0.37, -0.8, 0.43)
    assert p.delta_usdt == pytest.approx(4.3 * pos.quantity)
    text = ka.report_text(pairs, open_n)
    assert "n = 1" in text and "+0.430" in text


async def test_KeeperAssistant_restart_keeps_waiting_for_the_exit(
    tmp_path: Path, now: datetime
) -> None:
    h = Harness(tmp_path, now, Scorer(0.9), Scorer(0.2))
    pos = short_pos(now)
    h.a.on_reverse_signal(pos, Side.LONG, "x")
    await h.settle()
    again = Harness(tmp_path, now, Scorer(0.9), Scorer(0.2))
    again.a.on_closed(pos, 101.0)
    assert [r["kind"] for r in ka.read_rows(tmp_path / "keeper_assistant")] == ["check", "closed"]


async def test_KeeperAssistant_live_close_follows_the_shadow_and_summarises(
    tmp_path: Path, now: datetime
) -> None:
    h = Harness(tmp_path, now, Scorer(0.9), Scorer(0.9))
    pos = short_pos(now, take_profit_price=95.0)
    h.a.on_reverse_signal(pos, Side.LONG, "x")
    await h.settle()
    rows = ka.read_rows(tmp_path / "keeper_assistant")
    assert rows[0]["handed_off"] is True
    h.a.on_closed(pos.model_copy(update={"exit_price": 104.0, "status": "closed"}), None)
    assert ka.read_rows(tmp_path / "keeper_assistant")[-1]["closed_by_assistant"] is True
    h.price[0] = 106.0
    h.a.tick(now + timedelta(minutes=1))
    assert ka.live_pairs(ka.read_rows(tmp_path / "keeper_assistant"))[0] == []
    h.price[0] = 111.0  # shadow short hits its stop at 110
    h.a.tick(now + timedelta(minutes=2))
    pairs, pending = ka.live_pairs(ka.read_rows(tmp_path / "keeper_assistant"))
    assert pending == 0 and len(pairs) == 1
    assert (pairs[0].r_signal, pairs[0].r_exit, pairs[0].delta_r) == (-0.4, -1.0, 0.6)
    h.a.tick(now + timedelta(days=1))
    title, text = h.alerts[-1]
    assert f"{now.date()}" in title and "平均 ΔR +0.600" in text
    assert any(r.get("kind") == "summary" for r in h.records())


async def test_KeeperAssistant_summarises_after_every_ten_closes(
    tmp_path: Path, now: datetime
) -> None:
    h = Harness(tmp_path, now, Scorer(0.9), Scorer(0.9), keeper_assistant_gap_s=0)
    for i in range(11):
        pos = short_pos(now, position_id=f"p{i}", symbol=f"A{i}USDT")
        h.a.on_reverse_signal(pos, Side.LONG, "x")
        await h.settle()
        if i == 10:
            h.accepted.discard(pos.position_id)  # the executor never took this one
        h.a.on_closed(pos, 104.0)
        assert len(h.alerts) == (1 if i >= 9 else 0)
    assert "已平 10 笔" in h.alerts[0][0]
    assert h.a._closes == 10


async def test_KeeperAssistant_handed_off_close_that_never_happens_is_not_counted(
    tmp_path: Path, now: datetime
) -> None:
    h = Harness(tmp_path, now, Scorer(0.9), Scorer(0.9))
    pos = short_pos(now)
    h.a.on_reverse_signal(pos, Side.LONG, "x")
    await h.settle()
    h.accepted.clear()  # closing was refused downstream (leaving, already closing, error)
    h.a.on_closed(pos, 104.0)
    closed = ka.read_rows(tmp_path / "keeper_assistant")[-1]
    assert closed["kind"] == "closed" and closed["closed_by_assistant"] is False
    assert h.a._closes == 0 and h.a._shadows == {}


async def test_KeeperAssistant_hot_off_stops_closing_and_confirming_at_once(
    tmp_path: Path, now: datetime
) -> None:
    gate = asyncio.Event()
    cards = Cards()
    h = Harness(tmp_path, now, Scorer(0.9, gate), Scorer(0.9), cards=cards)
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    h.a.on_flip_pending("ETHUSDT", cand(now))
    await asyncio.sleep(0)
    h.a.on_flip_pending("BTCUSDT", cand(now))  # deferred behind the position check
    h.cfg = h.cfg.model_copy(update={"keeper_assistant_on": False})
    gate.set()
    await h.settle()
    assert h.closed == [] and cards.confirmed == []
    assert h.a._deferred == {}
    assert sum("BTCUSDT" in q for _, q, _ in h.a._jev.calls) == 1  # type: ignore[union-attr]


async def test_KeeperAssistant_no_summary_alert_when_off_or_no_checks(
    tmp_path: Path, now: datetime
) -> None:
    h = Harness(tmp_path, now, Scorer(0.9), Scorer(0.9))
    h.a.tick(now + timedelta(days=1))  # no checks that day
    assert h.alerts == []
    h.a.on_reverse_signal(short_pos(now + timedelta(days=1)), Side.LONG, "x")
    h.clock[0] = now + timedelta(days=1)
    await h.settle()
    h.cfg = h.cfg.model_copy(update={"keeper_assistant_on": False})
    h.a.tick(now + timedelta(days=2))
    assert h.alerts == []
    assert any(r.get("kind") == "summary" for r in h.records())


async def test_KeeperAssistant_summary_day_follows_the_configured_zone(
    tmp_path: Path, now: datetime
) -> None:
    late = datetime(2026, 10, 22, 20, 0, tzinfo=now.tzinfo)  # 03:00 next day in Bangkok
    rows = [
        {"ts": late.isoformat(), "kind": "closed", "position_id": "p", "closed_by_assistant": True,
         "side": "short", "exit_price": 104.0, "r_exit": -0.4, "quantity": 1.0},
        {"ts": late.isoformat(), "kind": "shadow_exit", "position_id": "p",
         "shadow_exit_price": 110.0, "r_shadow": -1.0},
    ]  # fmt: skip
    assert len(ka.live_pairs(rows, "2026-10-23", "Asia/Bangkok")[0]) == 1
    assert ka.live_pairs(rows, "2026-10-22", "Asia/Bangkok")[0] == []
    assert len(ka.live_pairs(rows, "2026-10-22", "UTC")[0]) == 1


def test_shadow_exit_rules(now: datetime) -> None:
    sh = ka._Shadow("p", "BTCUSDT", "long", 100.0, 95.0, 110.0, now + timedelta(hours=1), 1.0, "")
    assert ka.shadow_exit(sh, 100.0, now) == (None, None)
    assert ka.shadow_exit(sh, 94.0, now) == (95.0, "stop_loss")
    assert ka.shadow_exit(sh, 111.0, now) == (110.0, "take_profit")
    assert ka.shadow_exit(sh, 101.0, now + timedelta(hours=1)) == (101.0, "time_limit")
    assert ka.shadow_exit(sh, None, now + timedelta(hours=1)) == (None, "deadline_no_price")


async def test_TransportScorer_ask_sends_one_yes_no_question_and_reads_p_yes(
    now: datetime,
) -> None:
    def answer(state: str, qs: Sequence[c.JevQuestion]) -> Mapping[str, Mapping[str, float]]:
        assert [q.choices for q in qs] == [("否", "是")]
        return {QUESTION_ID: {"是": 0.7, "否": 0.3}}

    t = MockJevTransport(answer=answer, model="ms-decision")
    s = TransportScorer(t, usd_per_mtok=0.042, max_errors=2, cooldown_s=60)
    v = await s.ask("B", "BTCUSDT", "{}", "q", ("否", "是"), 1.0)
    assert (v.model, v.p_yes, v.error) == ("ms-decision", 0.7, None)


async def test_TransportScorer_ask_hold_sends_every_question_and_reads_p_close() -> None:
    qs = tuple(hold_questions())
    sent: list[tuple[c.JevQuestion, ...]] = []

    def answer(state: str, got: Sequence[c.JevQuestion]) -> Mapping[str, Mapping[str, float]]:
        sent.append(tuple(got))
        return close_p(0.7)(state, got)  # type: ignore[no-any-return]

    s = TransportScorer(
        MockJevTransport(answer=answer), usd_per_mtok=0, max_errors=2, cooldown_s=60
    )
    v = await s.ask_hold("A", "BTCUSDT", "{}", qs, 1.0)
    assert sent == [qs]
    assert (v.p_yes, v.options, v.question, v.error) == (0.7, ("hold", "close"), "hold", None)
    assert v.row()["p_close"] == 0.7 and v.text == "p(平) 0.70"


async def test_TransportScorer_ask_counts_errors_and_cools_down() -> None:
    t = MockJevTransport(fail=RuntimeError("503"))
    clock = [0.0]
    s = TransportScorer(t, usd_per_mtok=0, max_errors=2, cooldown_s=60, clock=lambda: clock[0])
    for _ in range(2):
        v = await s.ask("A", "BTCUSDT", "{}", "q", ("是", "否"), 1.0)
        assert v.p_yes is None and "503" in (v.error or "")
    v = await s.ask("A", "BTCUSDT", "{}", "q", ("是", "否"), 1.0)
    assert "冷却" in (v.error or "") and len(t.calls) == 2
    clock[0] = 61.0
    t.fail = None
    t.answer = lambda state, qs: {QUESTION_ID: {"是": 0.6, "否": 0.4}}
    v = await s.ask("A", "BTCUSDT", "{}", "q", ("是", "否"), 1.0)
    assert v.p_yes == 0.6


def test_keeper_assistant_report_cli_prints_both_comparisons(
    tmp_path: Path, now: datetime, capsys: pytest.CaptureFixture[str]
) -> None:
    from jiaoyi.cli import main
    from tests.conftest import ROOT

    text = (ROOT / "config" / "app.example.toml").read_text(encoding="utf-8")
    app = tmp_path / "app.toml"
    app.write_text(
        text.replace('data_dir = "data"', f'data_dir = "{tmp_path}"', 1), encoding="utf-8"
    )
    folder = tmp_path / "keeper_assistant"
    folder.mkdir()
    rows = [
        {"ts": now.isoformat(), "kind": "check", "position_id": "p", "symbol": "BTCUSDT",
         "side": "short", "entry_price": 100.0, "stop_loss_price": 110.0, "quantity": 2.0,
         "price_at_signal": 103.0, "r_at_signal": -0.3, "would_close": True, "handed_off": False},
        {"ts": now.isoformat(), "kind": "closed", "position_id": "p", "exit_price": 110.0},
    ]  # fmt: skip
    (folder / f"{now:%Y-%m-%d}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    risk = str(ROOT / "config" / "risk.example.toml")
    assert main(["keeper-assistant-report", "--risk", risk, "--app", str(app)]) == 0
    out = capsys.readouterr().out
    assert "n = 1，平均 ΔR = +0.700，合计 ΔU = +14.00 U" in out
    assert "真平了的仓" in out


async def test_KeeperAssistant_heartbeat_marks_model_b_failures_degraded(
    tmp_path: Path, now: datetime
) -> None:
    clock = [0.0]
    b = TransportScorer(
        MockJevTransport(fail=RuntimeError("503")),
        usd_per_mtok=0,
        max_errors=3,
        cooldown_s=60,
        clock=lambda: clock[0],
    )
    h = Harness(tmp_path, now, Scorer(0.9), b)
    h.a.heartbeat(now)
    assert h.log.read_status()["keeper_assistant"].health is c.Health.OK
    await b.ask("B", "X", "{}", "q", ("否", "是"), 1.0)
    h.a.heartbeat(now)
    st = h.log.read_status()["keeper_assistant"]
    assert st.health is c.Health.DEGRADED and "模型 B 连续出错 1 次" in st.headline
    for _ in range(2):
        await b.ask("B", "X", "{}", "q", ("否", "是"), 1.0)
    h.a.heartbeat(now)
    assert "冷却中" in h.log.read_status()["keeper_assistant"].headline


async def test_KeeperAssistant_closes_model_b_when_it_changes_and_on_shutdown(
    tmp_path: Path, now: datetime
) -> None:
    closed: list[str] = []

    class Closable(Scorer):
        def __init__(self, name: str) -> None:
            super().__init__(0.1)
            self.name = name

        async def aclose(self) -> None:
            closed.append(self.name)

    h = Harness(tmp_path, now, Scorer(0.1), None)
    h.a._b_factory = lambda model: Closable(model)
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    h.cfg = h.cfg.model_copy(update={"keeper_assistant_b_model": "other/model"})
    h.clock[0] = now + timedelta(minutes=5)
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    assert closed == ["microsoft/decision-1"]
    await h.a.aclose()
    assert closed == ["microsoft/decision-1", "other/model"]


DECISION_1 = "microsoft/microsoft-decision-1"


class Decisions:
    """Fake OpenRouter Decisions route: records each request, answers from ``reply``."""

    def __init__(self, reply: Any) -> None:
        self.reply = reply
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.reply, int):
            return httpx.Response(self.reply, text="upstream down")
        return httpx.Response(200, json=self.reply)

    def body(self, i: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[i].content)


def decisions_reply(probs: Mapping[str, float]) -> dict[str, Any]:
    return {
        "id": "gen-dec-1",
        "model": "microsoft/microsoft-decision-1-20261020",
        "provider": "Microsoft",
        "answers": {QUESTION_ID: {"type": "choice", "choice": "否", "probabilities": probs}},
        "usage": {"input_tokens": 300, "output_tokens": 0, "cost": 0.0000126},
    }


def model_b(fake: Decisions, **cfg: Any) -> Any:
    fl = FastLoopConfig(**cfg)
    return ka.openrouter_scorer("sk-or-test", lambda: fl, httpx.MockTransport(fake))(DECISION_1)


async def test_openrouter_scorer_asks_decisions_route_blind_with_reversed_options(
    tmp_path: Path, now: datetime
) -> None:
    fake = Decisions(decisions_reply({"否": 0.2, "是": 0.8}))
    h = Harness(tmp_path, now, Scorer(0.9), model_b(fake))
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    req = fake.requests[0]
    assert (req.method, str(req.url)) == ("POST", "https://openrouter.ai/api/alpha/decisions")
    assert req.headers["Authorization"] == "Bearer sk-or-test"
    body = fake.body()
    assert body["model"] == DECISION_1
    q = body["questions"][QUESTION_ID]
    assert q["type"] == "choice" and list(q["criteria"]) == ["否", "是"]
    assert q["instructions"] == "BTCUSDT 接下来 1–4 小时会上涨吗？（否 / 是）"
    state = json.loads(body["state"])
    assert state["sym"] == "BTCUSDT" and state["px"] == 103.7
    assert set(state) == {
        "sym", "px", "atr_bps", "ret1m_bps", "ret5m_bps", "vol_x", "buy", "cvd30_k",
        "rv5m_bps", "liq_long_k", "liq_short_k", "book",
    }  # fmt: skip
    for word in ("entry", "stop", "pnl", "side", "持", "仓", "反向", "110", "100.0"):
        assert word not in body["state"] + q["instructions"], word
    b = h.records()[0]["judges"][1]
    assert (b["model"], b["p_yes"], b["options"]) == (
        "microsoft/microsoft-decision-1-20261020",
        0.8,
        ["否", "是"],
    )
    assert b["cost_usd"] == 0.0000126 and b["usage"] == {"input_tokens": 300, "output_tokens": 0}
    assert len(h.closed) == 1


@pytest.mark.parametrize(
    "reply",
    [
        {"model": DECISION_1, "usage": {}},
        decisions_reply({"Yes": 0.8, "No": 0.2}),
        {**decisions_reply({}), "answers": {QUESTION_ID: {"type": "choice", "choice": "是"}}},
    ],
    ids=["no-answers", "other-labels", "no-probabilities"],
)
async def test_openrouter_scorer_treats_malformed_answer_as_no_answer(
    tmp_path: Path, now: datetime, reply: dict[str, Any]
) -> None:
    h = Harness(tmp_path, now, Scorer(0.9), model_b(Decisions(reply)))
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    b = h.records()[0]["judges"][1]
    assert b["p_yes"] is None and b["error"]
    assert h.closed == []


async def test_openrouter_scorer_counts_http_errors_and_cools_down() -> None:
    fake = Decisions(503)
    b = model_b(fake, jev_max_consecutive_errors=2, jev_error_cooldown_s=300)
    for _ in range(2):
        v = await b.ask("B", "BTCUSDT", "{}", "q", ("否", "是"), 1.0)
        assert v.p_yes is None and "HTTP 503" in (v.error or "")
    v = await b.ask("B", "BTCUSDT", "{}", "q", ("否", "是"), 1.0)
    assert "冷却" in (v.error or "") and len(fake.requests) == 2 and b.cooling()
    await b.aclose()


async def test_probe_asks_each_judge_once_and_reports_raw_probabilities(now: datetime) -> None:
    fake = Decisions(decisions_reply({"否": 0.35, "是": 0.65}))
    jev = MockJevTransport(answer=lambda s, qs: {QUESTION_ID: {"是": 0.3, "否": 0.7}}, model="jev")
    b = ka.OpenRouterDecisionsTransport("k", DECISION_1, http_transport=httpx.MockTransport(fake))
    state = ka.probe_snapshot("ETHUSDT", now)
    rows = await ka.probe(
        [("B", b), ("A", jev)], FastLoopConfig(), symbol="ETHUSDT", want="short", state=state
    )
    assert [(r["judge"], r["p_yes"]) for r in rows] == [("B", 0.65), ("A", 0.3)]
    assert rows[0]["raw_probs"] == {"否": 0.35, "是": 0.65}
    assert rows[0]["question"] == "ETHUSDT 接下来 1–4 小时会下跌吗？（否 / 是）"
    assert rows[1]["question"] == "ETHUSDT 接下来 1–4 小时会下跌吗？（是 / 否）"
    assert rows[0]["request_id"] == "gen-dec-1" and rows[0]["latency_ms"] >= 0
    assert fake.body()["state"] == state and jev.calls == [state]


def test_keeper_assistant_probe_cli_prints_p_yes_for_model_b(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from jiaoyi import cli
    from jiaoyi.gate import jev
    from tests.conftest import ROOT

    fake = Decisions(decisions_reply({"否": 0.1, "是": 0.9}))
    real = jev.OpenRouterDecisionsTransport

    def with_fake(key: str, model: str, **kw: Any) -> Any:
        return real(key, model, http_transport=httpx.MockTransport(fake))

    monkeypatch.setattr(jev, "OpenRouterDecisionsTransport", with_fake)
    monkeypatch.setattr(cli.ChainedSecretStore, "get", lambda self, k: "sk-or-test")
    app = ROOT / "config" / "app.example.toml"
    args = ["keeper-assistant-probe", "--app", str(app), "--model", DECISION_1]
    assert cli.main(args) == 0
    out = capsys.readouterr().out
    assert "p(是)：0.9000" in out and '"是": 0.9' in out
    assert "BTCUSDT 接下来 1–4 小时会上涨吗？（否 / 是）" in out
    assert len(fake.requests) == 1
