"""持仓管家规则 v2：Jev 只记录（record）、执行所价（venue）、Jev 盈利门控、规则顺序和不变量。

仓：多 BTCUSDT，开仓 68000、止损 67880，1R ≈ 17.65bp；0.5R ≈ 68060。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.core.hotconfig import HOT_FAST
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution.exchange import MarkEvidence
from jiaoyi.execution.hb_executor import HbExecutorAdapter
from jiaoyi.execution.registry import ProtectiveLeg, ServerProtection
from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
from jiaoyi.keeper.keeper import RULE_ORDER, JevKeeper
from jiaoyi.panel.actions import editable_fields
from tests.fastloop_helpers import FAST, card, features
from tests.test_keeper_patrol import position

NEW_KEYS = {
    "keeper_model_mode",
    "keeper_price_source",
    "keeper_thesis_take_on",
    "keeper_thesis_take_min_r",
    "keeper_thesis_take_p",
    "keeper_thesis_take_max_age_s",
}
RECORD = FAST.model_copy(update={"keeper_model_mode": "record"})
GATE = FAST.model_copy(update={"keeper_thesis_take_on": True, "giveback_on": False})


def answer(p_close: float, thesis_no: float = 0.0) -> Any:
    def f(state: str, qs: Sequence[c.JevQuestion]) -> Mapping[str, Mapping[str, float]]:
        return {
            "thesis_intact": {"yes": 1 - thesis_no, "no": thesis_no},
            "adverse_risk": {"low": 0.5, "high": 0.5},
            "action": {"hold": 1 - p_close, "close": p_close},
        }

    return f


def make(
    t: MockJevTransport,
    now: datetime,
    cfg: c.FastLoopConfig,
    prices: Iterator[float] | None = None,
    log: JsonlDecisionLog | None = None,
    **kw: Any,
) -> JevKeeper:
    it = prices

    def feats(_s: str) -> c.FeatureSnapshot:
        return features(now, price=next(it) if it is not None else 68000.0)

    return JevKeeper(
        JevDecisionModel(t, cfg, timeout_ms=200),
        cfg,
        feats,
        lambda n: card(now),
        RoleRecorder(c.Role.KEEPER, log) if log is not None else None,
        **kw,
    )


def keeper_records(log: JsonlDecisionLog, now: datetime) -> list[Any]:
    return [r for r in log.read_day(now.date().isoformat()) if r.role is c.Role.KEEPER]


def test_new_settings_default_to_todays_behaviour_and_are_hot_and_editable() -> None:
    d = c.FastLoopConfig()
    assert (d.keeper_model_mode, d.keeper_price_source, d.keeper_thesis_take_on) == (
        "act",
        "mainnet",
        False,
    )
    assert (d.keeper_thesis_take_min_r, d.keeper_thesis_take_p) == (0.5, 0.5)
    assert d.keeper_thesis_take_max_age_s == 180
    assert NEW_KEYS <= HOT_FAST
    assert set(editable_fields("fast_loop")) >= NEW_KEYS


# ---- 1. record 模式


@pytest.mark.parametrize("one_shot", [False, True])
async def test_record_mode_never_closes_on_p_close(
    now: datetime, tmp_path: Path, one_shot: bool
) -> None:
    log = JsonlDecisionLog(tmp_path)
    t = MockJevTransport(answer=answer(0.99))
    k = make(t, now, RECORD, log=log)
    pos = position(now)
    for i in range(3):
        if one_shot:
            sig = c.AgainstSignalEvent(
                symbol="BTCUSDT", ts=now, kind=c.SignalKind.VOLUME_BREAKOUT, side=c.Side.SHORT,
                desc="反向", strong=True,
            )  # fmt: skip
            k.note_against(pos.position_id, sig, True)
        v = await k.review(pos, now + timedelta(seconds=30 * i))
        assert v.action == "hold" and v.p_close == pytest.approx(0.99)
        assert "只记录" in v.reason
    assert len(t.calls) == 3  # 每次照常问 Jev
    out = keeper_records(log, now)[-1].output
    vote = next(r for r in out["rules"] if r["rule"] == "p_close")
    assert vote["vote"] == "record" and vote["would_close"] is True
    assert out["rule_fired"] is None and out["keeper_model_mode"] == "record"


async def test_act_mode_closes_on_strong_p_close(now: datetime) -> None:
    t = MockJevTransport(answer=answer(0.99))
    assert (await make(t, now, FAST).review(position(now), now)).action == "close"


async def test_record_mode_blocked_symbol_is_a_hard_close(now: datetime) -> None:
    t = MockJevTransport(answer=answer(0.0))
    k = make(t, now, RECORD, blocked=lambda: {"BTCUSDT": "币安下架公告"})
    v = await k.review(position(now), now)
    assert v.action == "close" and "币安下架公告" in v.reason and t.calls == []


async def test_record_mode_still_feeds_thesis_gate(now: datetime) -> None:
    cfg = GATE.model_copy(update={"keeper_model_mode": "record"})
    t = MockJevTransport(answer=answer(0.99, thesis_no=0.9))
    v = await make(t, now, cfg, iter([68100.0])).review(position(now), now)  # ≈ 0.83R
    assert v.action == "close" and v.reason.startswith("盈利且进场理由不成立，止盈")


# ---- 2. 价格来源


def venue_from(seq: list[float], at: Callable[[], datetime]) -> Any:
    it = iter(seq)

    async def read(symbol: str) -> tuple[float, datetime] | None:
        return next(it), at()

    return read


@pytest.mark.parametrize(
    ("source", "closes"),
    [("mainnet", True), ("venue", False)],
)
async def test_price_source_changes_giveback_trigger(
    now: datetime, tmp_path: Path, source: str, closes: bool
) -> None:
    """主网：峰 30bp → 16bp（吐 47%）该平；Demo 标记价：峰 30bp → 20bp（吐 33%）不平。"""
    log = JsonlDecisionLog(tmp_path)
    cfg = FAST.model_copy(update={"keeper_price_source": source})
    t = MockJevTransport(answer=answer(0.0))
    k = make(
        t, now, cfg, iter([68204.0, 68109.0]), log=log,
        venue_price=venue_from([68204.0, 68136.0], lambda: now),
    )  # fmt: skip
    pos = position(now)
    assert (await k.review(pos, now)).action == "hold"
    v = await k.review(pos, now)
    assert (v.action == "close") is closes
    price = keeper_records(log, now)[-1].output["price"]
    assert price["source"] == source and price["mainnet"] == 68109.0
    if source == "venue":
        assert price["used"] == "venue" and price["venue"] == 68136.0
        snap = t.calls[-1]
        assert "68136" in snap  # 给 Jev 的快照也用执行所价
    else:
        assert price["used"] == "mainnet" and price["venue"] is None


async def test_venue_price_respects_age_guard(now: datetime) -> None:
    cfg = FAST.model_copy(update={"keeper_price_source": "venue", "keeper_price_max_age_s": 30.0})
    t = MockJevTransport(answer=answer(0.99))
    old = venue_from([68000.0], lambda: now - timedelta(seconds=45))
    v = await make(t, now, cfg, venue_price=old).review(position(now), now)
    assert v.action == "hold" and "执行所标记价已旧 45 秒" in v.reason and t.calls == []


async def test_venue_unavailable_falls_back_to_mainnet(now: datetime, tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path)
    cfg = FAST.model_copy(update={"keeper_price_source": "venue"})

    async def broken(symbol: str) -> tuple[float, datetime] | None:
        raise RuntimeError("premiumIndex 500")

    t = MockJevTransport(answer=answer(0.0))
    await make(t, now, cfg, log=log, venue_price=broken).review(position(now), now)
    price = keeper_records(log, now)[-1].output["price"]
    assert price["used"] == "mainnet" and price["note"] == "venue_unavailable_fallback_mainnet"


# ---- 3. Jev 盈利门控


@pytest.mark.parametrize(
    ("px", "thesis_no", "closes"),
    [
        (68100.0, 0.6, True),  # 0.83R、no 0.6
        (68050.0, 0.9, False),  # 0.42R < 0.5R
        (68100.0, 0.45, False),  # no 0.45 < 0.5
        (67950.0, 0.9, False),  # 亏着
    ],
)
async def test_thesis_gate_needs_profit_and_thesis_no(
    now: datetime, px: float, thesis_no: float, closes: bool
) -> None:
    t = MockJevTransport(answer=answer(0.0, thesis_no=thesis_no))
    v = await make(t, now, GATE, iter([px])).review(position(now), now)
    assert (v.action == "close") is closes
    assert v.reason.startswith("盈利且进场理由不成立，止盈") is closes


@pytest.mark.parametrize(("px", "closes"), [(67950.0, False), (68000.0, False), (68010.0, True)])
async def test_thesis_gate_with_zero_min_r_still_needs_a_profit(
    now: datetime, px: float, closes: bool
) -> None:
    cfg = GATE.model_copy(update={"keeper_thesis_take_min_r": 0.0})
    t = MockJevTransport(answer=answer(0.0, thesis_no=0.9))
    v = await make(t, now, cfg, iter([px])).review(position(now), now)
    assert (v.action == "close") is closes


async def test_thesis_gate_off_by_default(now: datetime) -> None:
    t = MockJevTransport(answer=answer(0.0, thesis_no=0.99))
    cfg = FAST.model_copy(update={"giveback_on": False})
    assert (await make(t, now, cfg, iter([68150.0])).review(position(now), now)).action == "hold"


@pytest.mark.parametrize(("later_s", "closes"), [(170, True), (190, False)])
async def test_thesis_gate_uses_last_answer_only_while_fresh(
    now: datetime, later_s: int, closes: bool
) -> None:
    """第一次在 0.2R 时答 no 0.9（浮盈不够不平）；之后 Jev 出错，涨到 0.83R：
    上次的回答不超过 180 秒才算。"""
    t = MockJevTransport(answer=answer(0.0, thesis_no=0.9))
    k = make(t, now, GATE, iter([68024.0, 68100.0]), sleep=_no_sleep)
    pos = position(now)
    assert (await k.review(pos, now)).action == "hold"
    t.fail = PermissionError("401")
    v = await k.review(pos, now + timedelta(seconds=later_s))
    assert (v.action == "close") is closes


async def _no_sleep(s: float) -> None:
    return None


async def test_giveback_fires_before_thesis_gate(now: datetime, tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path)
    cfg = GATE.model_copy(update={"giveback_on": True})
    t = MockJevTransport(answer=answer(0.0, thesis_no=0.9))
    k = make(t, now, cfg, iter([68204.0, 68109.0]), log=log)  # 第二次：回吐 47%，浮盈 0.9R
    pos = position(now)
    await k.review(pos, now)
    v = await k.review(pos, now + timedelta(seconds=30))
    assert v.action == "close" and "回吐保护" in v.reason
    out = keeper_records(log, now)[-1].output
    assert out["rule_fired"] == "giveback"
    assert [r["rule"] for r in out["rules"]] == list(RULE_ORDER)
    votes = {r["rule"]: r["vote"] for r in out["rules"]}
    assert votes["thesis_take"] == "not_reached" and votes["p_close"] == "not_reached"
    assert votes["price"] == "ok" and votes["card"] == "none"


# ---- 4. 不变量


async def test_one_close_request_per_position_per_round(now: datetime, tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path)
    t = MockJevTransport(answer=answer(0.99, thesis_no=0.99))
    k = make(t, now, GATE, log=log, review_interval_s=20.0)
    pos = position(now)
    assert (await k.review(pos, now)).action == "close"
    again = await k.review(pos, now + timedelta(seconds=5))  # 同一轮（巡逻兵急查）
    assert again.action == "hold" and again.reason.startswith("不发平仓：这一轮已发过平仓请求")
    assert keeper_records(log, now)[-1].output["close_blocked"]
    assert (await k.review(pos, now + timedelta(seconds=20))).action == "close"  # 下一轮再发


async def test_no_keeper_close_while_exchange_leg_is_exiting(now: datetime) -> None:
    state: dict[str, tuple[str, str] | None] = {"v": ("exiting", "exiting")}
    t = MockJevTransport(answer=answer(0.99))
    k = make(t, now, FAST, exit_in_flight=lambda pid: state["v"])
    pos = position(now)
    v = await k.review(pos, now)
    assert v.action == "hold" and "交易所保护腿已触发或正在退出" in v.reason
    state["v"] = None
    assert (await k.review(pos, now + timedelta(seconds=1))).action == "close"


async def test_replacing_leg_defers_close_at_most_30s(now: datetime) -> None:
    t = MockJevTransport(answer=answer(0.99))
    k = make(t, now, FAST, exit_in_flight=lambda pid: ("replacing", "stop_loss submit_pending"))
    pos = position(now)
    assert (await k.review(pos, now)).action == "hold"
    assert (await k.review(pos, now + timedelta(seconds=30))).action == "hold"
    assert (await k.review(pos, now + timedelta(seconds=31))).action == "close"


async def test_card_hard_close_is_also_blocked_while_exiting(now: datetime) -> None:
    t = MockJevTransport(answer=answer(0.0))
    k = JevKeeper(
        JevDecisionModel(t, FAST, timeout_ms=200),
        FAST,
        lambda s: features(now),
        lambda n: card(now, bias="short"),
        exit_in_flight=lambda pid: ("exiting", "cleanup"),
    )
    v = await k.review(position(now), now)
    assert v.action == "hold" and "反向硬规则" in v.reason and t.calls == []


# ---- 执行器：执行所标记价、保护腿在途


class _MarkExchange:
    def __init__(self) -> None:
        self.reads = 0

    async def execution_mark(self, symbol: str) -> MarkEvidence:
        self.reads += 1
        return MarkEvidence(symbol, 68010.0 + self.reads, START, START)


START = datetime(2026, 10, 22, 6, 30).astimezone()


def _adapter(exchange: Any, clock: Callable[[], datetime]) -> HbExecutorAdapter:
    a = HbExecutorAdapter.__new__(HbExecutorAdapter)
    a._exchange = exchange
    a._clock = clock
    a._venue_marks = {}
    a._venue_reads = {}
    return a


async def test_venue_mark_reuses_one_read_per_symbol() -> None:
    ex = _MarkExchange()
    at = {"t": START}
    a = _adapter(ex, lambda: at["t"])
    assert await a.venue_mark("BTCUSDT") == (68011.0, START)
    at["t"] = START + timedelta(seconds=1)
    assert await a.venue_mark("BTCUSDT") == (68011.0, START)
    assert ex.reads == 1
    at["t"] = START + timedelta(seconds=3)
    assert (await a.venue_mark("BTCUSDT"))[0] == 68012.0  # type: ignore[index]


def _protection(stage: str, ever_armed: bool, *legs: ProtectiveLeg) -> Any:
    sp = ServerProtection(
        account_name="a", connector_name="c", intended_quantity=1.0, stage=stage,  # type: ignore[arg-type]
        ever_armed=ever_armed, legs=list(legs),
    )  # fmt: skip
    return SimpleNamespace(get=lambda pid: SimpleNamespace(server_protection=sp))


def _leg(state: str, triggered: bool = False) -> ProtectiveLeg:
    return ProtectiveLeg(
        kind="stop_loss", generation=1, client_order_id="br-sl-pos-1-1", quantity=1.0,
        trigger_price=67880.0, state=state,  # type: ignore[arg-type]
        triggered_at=START if triggered else None,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("stage", "armed", "legs", "want"),
    [
        ("armed", True, [_leg("confirmed")], None),
        ("exiting", True, [_leg("confirmed")], "exiting"),
        ("cleanup", True, [], "exiting"),
        ("armed", True, [_leg("confirmed", triggered=True)], "exiting"),
        ("provisional", True, [_leg("submit_pending")], "replacing"),
        ("provisional", False, [_leg("submit_pending")], None),  # 第一次挂，还没托管过
    ],
)
def test_protection_exit_in_flight(
    stage: str, armed: bool, legs: list[ProtectiveLeg], want: str | None
) -> None:
    a = _adapter(None, lambda: START)
    a._registry = _protection(stage, armed, *legs)
    got = a.protection_exit_in_flight("pos-1")
    assert (got[0] if got else None) == want
