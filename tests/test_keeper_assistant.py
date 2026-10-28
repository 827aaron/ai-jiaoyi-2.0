from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.contracts import FastLoopConfig, KeyLevel, LevelKind, Outcome, Role, Side
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.jev import MockJevTransport
from jiaoyi.keeper import assistant as ka
from jiaoyi.keeper.assistant import (
    QUESTION_ID,
    KeeperAssistant,
    TransportScorer,
    Verdict,
)
from jiaoyi.slow.card_flip import FlipCandidate
from tests.fastloop_helpers import features
from tests.test_keeper_patrol import position

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


def cand(now: datetime, direction: str = "long") -> FlipCandidate:
    return FlipCandidate("short", direction, 1, now, now, "m|t")  # type: ignore[arg-type]


class Cards:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok
        self.confirmed: list[tuple[str, FlipCandidate]] = []

    def confirm_flip(
        self, symbol: str, expected: FlipCandidate, why: str, detail: dict[str, Any]
    ) -> bool:
        self.confirmed.append((symbol, expected))
        return self.ok


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
        **cfg: Any,
    ) -> None:
        self.cfg = FastLoopConfig(**{**ON, **cfg})
        self.clock = [now]
        self.closed: list[tuple[str, str]] = []
        self.closing = closing
        self.price = [price]
        self.alerts: list[tuple[str, str]] = []
        self.used_at_close: list[int] = []
        self.log = JsonlDecisionLog(tmp_path)
        self.now = now
        self.a = KeeperAssistant(
            lambda: self.cfg,
            RoleRecorder(Role.KEEPER_ASSISTANT, self.log),
            features=lambda sym: features(self.clock[0], price=self.price[0]),
            close=self._close,
            clock=lambda: self.clock[0],
            data_dir=tmp_path,
            jev=a,
            b_scorer=(lambda model: b) if b is not None else None,
            cards=cards,
            alert=lambda title, text: self.alerts.append((title, text)),
            day_reset_tz="UTC",
        )

    def _close(self, pos: c.PositionRecord, reason: str) -> str | None:
        self.used_at_close.append(self.a._slots.used)
        if self.closing:
            return "这笔正在平"
        self.closed.append((pos.position_id, reason))
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
    assert rec["closed"] is True and rec["asked"] == "上涨"
    assert [j["p_yes"] for j in rec["judges"]] == [0.8, 0.7]
    rows = (tmp_path / "keeper_assistant" / f"{now:%Y-%m-%d}.jsonl").read_text().splitlines()
    assert json.loads(rows[0])["trigger"] == "reverse_signal"


@pytest.mark.parametrize(
    ("a", "b", "kw"),
    [
        (0.8, 0.4, {}),
        (0.4, 0.8, {}),
        (0.8, 0.59, {}),
        (0.8, None, {}),
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
    assert h.records()[0]["closed"] is False


async def test_KeeperAssistant_on_reverse_signal_holds_without_model_b(
    tmp_path: Path, now: datetime
) -> None:
    h = Harness(tmp_path, now, Scorer(0.9), None)
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "x")
    await h.settle()
    assert h.closed == []
    [rec] = h.records()
    assert rec["judges"][1]["error"] and rec["judges"][1]["p_yes"] is None


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


async def test_KeeperAssistant_on_reverse_signal_asks_both_at_once_blind_and_mirrored(
    tmp_path: Path, now: datetime
) -> None:
    gate = asyncio.Event()
    a, b = Scorer(0.9, gate), Scorer(0.9, gate)
    h = Harness(tmp_path, now, a, b)
    h.a.on_reverse_signal(short_pos(now), Side.LONG, "强平反弹")
    await asyncio.wait_for(asyncio.gather(a.started.wait(), b.started.wait()), 1.0)
    gate.set()
    await h.settle()
    (state_a, q_a, opts_a), (state_b, q_b, opts_b) = a.calls[0], b.calls[0]
    assert opts_a == ("是", "否") and opts_b == ("否", "是")
    assert q_a == "BTCUSDT 接下来 1–4 小时会上涨吗？（是 / 否）"
    assert q_b == "BTCUSDT 接下来 1–4 小时会上涨吗？（否 / 是）"
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
    assert rec["closed"] is False and "正在平" in rec["why"]


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
    assert check["would_close"] is True and check["closed"] is False
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
    h.a.on_closed(pos.model_copy(update={"exit_price": 104.0, "status": "closed"}), None)
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
    for i in range(10):
        h.a.on_reverse_signal(
            short_pos(now, position_id=f"p{i}", symbol=f"A{i}USDT"), Side.LONG, "x"
        )
        await h.settle()
        assert len(h.alerts) == (1 if i == 9 else 0)
    assert "已平 10 笔" in h.alerts[0][0]


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
         "price_at_signal": 103.0, "r_at_signal": -0.3, "would_close": True, "closed": False},
        {"ts": now.isoformat(), "kind": "closed", "position_id": "p", "exit_price": 110.0},
    ]  # fmt: skip
    (folder / f"{now:%Y-%m-%d}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    assert main(["keeper-assistant-report", "--app", str(app)]) == 0
    out = capsys.readouterr().out
    assert "n = 1，平均 ΔR = +0.700，合计 ΔU = +14.00 U" in out
    assert "真平了的仓" in out
