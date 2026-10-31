"""持仓管家规则 v2：Jev 只记录（record）、执行所价（venue）、Jev 盈利门控、规则顺序和不变量。

仓：多 BTCUSDT，开仓 68000、止损 67880，1R ≈ 17.65bp；0.5R ≈ 68060。
"""

from __future__ import annotations

import json
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
    "keeper_close_min_loss_r",
    "keeper_model_mode",
    "keeper_price_source",
    "keeper_thesis_take_on",
    "keeper_thesis_take_min_r",
    "keeper_thesis_take_p",
    "keeper_thesis_take_max_age_s",
    "keeper_close_guard",
    "keeper_close_guard_replacing_max_s",
    "keeper_close_guard_exiting_max_s",
    "keeper_close_guard_total_max_s",
    "keeper_venue_alert_reviews",
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
    cd: c.BattleCard | None = None,
    **kw: Any,
) -> JevKeeper:
    it = prices

    def feats(_s: str) -> c.FeatureSnapshot:
        return features(now, price=next(it) if it is not None else 68000.0)

    return JevKeeper(
        JevDecisionModel(t, cfg, timeout_ms=200),
        cfg,
        feats,
        lambda n: cd if cd is not None else card(now),
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


async def test_venue_unavailable_holds_without_falling_back_to_mainnet(
    now: datetime, tmp_path: Path
) -> None:
    """峰记在 Demo 价 30bp 上；之后 Demo 价读不到、主网 16bp：拿主网价比会凭空回吐 47%。"""
    log = JsonlDecisionLog(tmp_path)
    cfg = FAST.model_copy(update={"keeper_price_source": "venue"})
    marks = iter([68204.0, None])

    async def read(symbol: str) -> tuple[float, datetime] | None:
        px = next(marks)
        if px is None:
            raise RuntimeError("premiumIndex 500")
        return px, now

    t = MockJevTransport(answer=answer(0.99))
    k = make(t, now, cfg, iter([68204.0, 68109.0]), log=log, venue_price=read)
    pos = position(now)
    await k.review(pos, now)
    v = await k.review(pos, now + timedelta(seconds=30))
    assert v.action == "hold" and "执行所标记价读不到" in v.reason and len(t.calls) == 1
    out = keeper_records(log, now)[-1].output
    votes = {r["rule"]: r for r in out["rules"]}
    assert votes["price"]["vote"] == "venue_unavailable"
    assert votes["giveback"]["vote"] == "hold" and votes["thesis_take"]["vote"] == "hold"
    assert out["price"]["used"] == "venue" and out["price"]["venue"] is None


class _Alerts:
    def __init__(self) -> None:
        self.sent: list[c.Alert] = []

    async def send(self, alert: c.Alert) -> None:
        self.sent.append(alert)


@pytest.mark.parametrize(("every", "alerts"), [(3, 1), (0, 0)])
async def test_venue_misses_raise_one_alert_and_show_in_the_record(
    now: datetime, tmp_path: Path, every: int, alerts: int
) -> None:
    log = JsonlDecisionLog(tmp_path)
    cfg = FAST.model_copy(
        update={"keeper_price_source": "venue", "keeper_venue_alert_reviews": every}
    )
    ok: dict[str, Any] = {"v": False, "at": now}

    async def read(symbol: str) -> tuple[float, datetime] | None:
        return (68000.0, ok["at"]) if ok["v"] else None

    al = _Alerts()
    t = MockJevTransport(answer=answer(0.0))
    k = make(t, now, cfg, log=log, venue_price=read, alerter=al)
    pos = position(now)
    for i in range(5):
        v = await k.review(pos, now + timedelta(seconds=20 * i))
        assert v.action == "hold" and t.calls == []  # 不退回主网价
    assert len(al.sent) == alerts
    out = keeper_records(log, now)[-1].output
    if every:
        assert al.sent[0].kind == "system" and "读不到执行所标记价" in al.sent[0].title
        assert out["price"]["venue_misses"] == 3 and out["price"]["venue_alert"] is True
        assert "已连续 3 次" in keeper_records(log, now)[-1].summary
    ok.update(v=True, at=now + timedelta(seconds=200))
    assert len(t.calls) == 0
    await k.review(pos, now + timedelta(seconds=200))
    assert len(t.calls) == 1  # 读到了：照常判
    ok["v"] = False
    for i in range(3):
        await k.review(pos, now + timedelta(seconds=220 + 20 * i))
    assert len(al.sent) == 2 * alerts  # 恢复后再断一段，重新计数、再告一次


async def test_hot_switch_of_price_source_keeps_peaks_apart(now: datetime) -> None:
    """主网 10bp；切到 Demo 价：Demo 16bp（Demo 峰从这里起）不平，同时记下主网涨到的 30bp；
    切回主网 16bp：对主网峰 30bp 回吐 47%，平。"""
    mainnet = iter([68068.0, 68204.0, 68109.0])
    t = MockJevTransport(answer=answer(0.0))
    k = make(t, now, FAST, mainnet, venue_price=venue_from([68109.0], lambda: now))
    pos = position(now)
    assert (await k.review(pos, now)).action == "hold"
    k._cfg = FAST.model_copy(update={"keeper_price_source": "venue"})
    assert (await k.review(pos, now)).action == "hold"
    k._cfg = FAST
    v = await k.review(pos, now)
    assert v.action == "close" and "回吐保护" in v.reason


async def test_recalled_peak_keeps_its_price_source(now: datetime, tmp_path: Path) -> None:
    """重启前用 Demo 价，峰 30bp；重启后改回主网价 16bp：Demo 的峰不拿来和主网价比。"""
    log = JsonlDecisionLog(tmp_path)
    venue_cfg = FAST.model_copy(update={"keeper_price_source": "venue"})
    t = MockJevTransport(answer=answer(0.0))
    k1 = make(
        t, now, venue_cfg, iter([68000.0]), log=log, scan_day=log.scan_day,
        venue_price=venue_from([68204.0], lambda: now),
    )  # fmt: skip
    await k1.review(position(now), now)
    k2 = make(t, now, FAST, iter([68109.0]), log=log, scan_day=log.scan_day)
    assert (await k2.review(position(now), now)).action == "hold"
    k3 = make(
        t, now, venue_cfg, iter([68000.0]), scan_day=log.scan_day,
        venue_price=venue_from([68109.0], lambda: now),
    )  # fmt: skip
    assert (await k3.review(position(now), now)).action == "close"  # Demo 峰 30 → 16bp


def _premium_client(mark: str, source_at: datetime) -> Any:
    from unittest.mock import AsyncMock

    from jiaoyi.execution.exchange import CcxtBinanceFutures

    obj = CcxtBinanceFutures.__new__(CcxtBinanceFutures)
    obj._markets_loaded = True
    ex = AsyncMock()
    ex.fapiPublicGetPremiumIndex.return_value = {
        "symbol": "BTCUSDT",
        "markPrice": mark,
        "time": int(source_at.timestamp() * 1000),
    }
    obj._ex = ex
    return obj


@pytest.mark.parametrize(
    ("max_age", "age_s", "fresh"),
    [(30.0, 45, False), (30.0, 20, True), (0.0, 6, False), (0.0, 3, True)],
)
async def test_venue_age_guard_through_execution_mark(
    now: datetime, monkeypatch: pytest.MonkeyPatch, max_age: float, age_s: int, fresh: bool
) -> None:
    """真的 ``execution_mark``（只把交易所回包换成合成的）：过旧的 Demo 标记价由管家按
    ``keeper_price_max_age_s`` 判过旧（0 时 5 秒），不是读不到。"""
    from jiaoyi.execution import exchange as module

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return now

    monkeypatch.setattr(module, "datetime", Clock)
    adapter = _adapter(_premium_client("68100", now - timedelta(seconds=age_s)), lambda: now)
    cfg = FAST.model_copy(
        update={"keeper_price_source": "venue", "keeper_price_max_age_s": max_age}
    )
    t = MockJevTransport(answer=answer(0.0))
    v = await make(t, now, cfg, venue_price=adapter.venue_mark).review(position(now), now)
    if fresh:
        assert len(t.calls) == 1 and "68100" in t.calls[0]
    else:
        assert t.calls == [] and f"执行所标记价已旧 {age_s} 秒" in v.reason


async def test_venue_pnl_on_a_short(now: datetime, tmp_path: Path) -> None:
    """空单：主网 68000（0），Demo 标记价 67900 → 浮盈 +14.7bp。"""
    log = JsonlDecisionLog(tmp_path)
    cfg = FAST.model_copy(update={"keeper_price_source": "venue"})
    t = MockJevTransport(answer=answer(0.0))
    k = make(
        t, now, cfg, log=log, cd=card(now, bias="short"),
        venue_price=venue_from([67900.0], lambda: now),
    )  # fmt: skip
    await k.review(_short(now), now)
    snap = json.loads(t.calls[0])
    assert snap["pos"]["pnl_bps"] == pytest.approx(14.7, abs=0.1)


def _short(now: datetime) -> c.PositionRecord:
    return position(now, side="short", stop_loss_price=68120.0, take_profit_price=67820.0)


@pytest.mark.parametrize(("px", "closes"), [(67900.0, True), (68100.0, False), (67960.0, False)])
async def test_thesis_gate_on_a_short(now: datetime, px: float, closes: bool) -> None:
    """空单 1R ≈ 17.6bp：67900 赚 0.83R 平；68100 亏着、67960 只赚 0.33R 不平。"""
    t = MockJevTransport(answer=answer(0.0, thesis_no=0.9))
    k = make(t, now, GATE, iter([px]), cd=card(now, bias="short"))
    v = await k.review(_short(now), now)
    assert (v.action == "close") is closes


LOSS_ONLY = FAST.model_copy(update={"keeper_close_min_loss_r": 0.2, "giveback_on": False})


@pytest.mark.parametrize(
    ("px", "p_close", "closes"),
    [
        (68030.0, 0.6, True),  # 亏 0.25R，过线一次就平
        (68030.0, 0.59, False),
        (68020.0, 0.99, False),  # 只亏 0.17R，强概率也不平
        (67900.0, 0.99, False),  # 赚钱不平
    ],
)
async def test_keeper_review_p_close_only_acts_on_a_loss(
    now: datetime, px: float, p_close: float, closes: bool
) -> None:
    """空单 1R = 120：68030 亏 0.25R，68020 亏 0.17R，67900 赚 0.83R。"""
    t = MockJevTransport(answer=answer(p_close))
    k = make(t, now, LOSS_ONLY, iter([px]), cd=card(now, bias="short"))
    assert ((await k.review(_short(now), now)).action == "close") is closes


async def test_keeper_review_loss_only_clears_the_streak_above_the_line(now: datetime) -> None:
    t = MockJevTransport(answer=answer(0.7))
    cfg = LOSS_ONLY.model_copy(update={"keeper_close_min_loss_r": 0.0})
    k = make(t, now, cfg, iter([68030.0, 67900.0, 68030.0]), cd=card(now, bias="short"))
    assert (await k.review(_short(now), now)).action == "hold"
    k._cfg = LOSS_ONLY
    assert (await k.review(_short(now), now + timedelta(seconds=30))).action == "hold"
    k._cfg = cfg
    v = await k.review(_short(now), now + timedelta(seconds=60))
    assert v.action == "hold"  # 中间那轮赚钱清了计数，这是新的第 1 次


@pytest.mark.parametrize(("px", "calls"), [(67900.0, 0), (68020.0, 0), (68030.0, 1)])
async def test_keeper_review_asks_the_model_only_on_a_loss(
    now: datetime, tmp_path: Path, px: float, calls: int
) -> None:
    log = JsonlDecisionLog(tmp_path)
    t = MockJevTransport(answer=answer(0.1))
    k = make(t, now, LOSS_ONLY, iter([px]), log=log, cd=card(now, bias="short"))
    assert (await k.review(_short(now), now)).action == "hold"
    assert len(t.calls) == calls
    assert keeper_records(log, now)[-1].summary


async def test_keeper_review_blocked_symbol_still_asks_the_model(now: datetime) -> None:
    t = MockJevTransport(answer=answer(0.1))
    k = make(
        t, now, LOSS_ONLY, iter([67900.0, 67900.0]), cd=card(now, bias="short"),
        blocked=lambda: {"BTCUSDT": "测试"},
    )  # fmt: skip
    await k.review(_short(now), now)
    assert len(t.calls) == 1


async def test_keeper_review_loss_only_respects_record_mode(now: datetime) -> None:
    t = MockJevTransport(answer=answer(0.99))
    cfg = LOSS_ONLY.model_copy(update={"keeper_model_mode": "record"})
    k = make(t, now, cfg, iter([68030.0]), cd=card(now, bias="short"))
    assert (await k.review(_short(now), now)).action == "hold"


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


# ---- 4. 平仓前检查（keeper_close_guard）

GUARD = FAST.model_copy(update={"keeper_close_guard": True})


async def test_close_guard_off_by_default_closes_like_today(now: datetime) -> None:
    t = MockJevTransport(answer=answer(0.99))
    k = make(
        t, now, FAST, review_interval_s=20.0, exit_in_flight=lambda pid: ("exiting", "exiting", "x")
    )
    pos = position(now)
    assert (await k.review(pos, now)).action == "close"
    assert (await k.review(pos, now + timedelta(seconds=1))).action == "close"


async def test_one_close_request_per_position_per_round(now: datetime, tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path)
    cfg = GATE.model_copy(update={"keeper_close_guard": True})
    t = MockJevTransport(answer=answer(0.99, thesis_no=0.99))
    k = make(t, now, cfg, log=log, review_interval_s=20.0)
    pos = position(now)
    assert (await k.review(pos, now)).action == "close"
    again = await k.review(pos, now + timedelta(seconds=5))  # 同一轮（巡逻兵急查）
    assert again.action == "hold" and again.reason.startswith("不发平仓：这一轮已发过平仓请求")
    assert keeper_records(log, now)[-1].output["close_blocked"]
    assert (await k.review(pos, now + timedelta(seconds=20))).action == "close"  # 下一轮再发


async def test_exiting_leg_defers_close_then_closes_with_note(
    now: datetime, tmp_path: Path
) -> None:
    log = JsonlDecisionLog(tmp_path)
    t = MockJevTransport(answer=answer(0.99))
    k = make(t, now, GUARD, log=log, exit_in_flight=lambda pid: ("exiting", "exiting", "exiting"))
    pos = position(now)
    v = await k.review(pos, now)
    assert v.action == "hold" and "交易所保护腿已触发或正在退出" in v.reason
    assert (await k.review(pos, now + timedelta(seconds=120))).action == "hold"
    assert (await k.review(pos, now + timedelta(seconds=121))).action == "close"
    note = keeper_records(log, now)[-1].output["close_guard_note"]
    assert "已 121 秒" in note and "超过 120 / 180 秒" in note


async def test_exiting_cleared_lets_the_next_close_through(now: datetime) -> None:
    state: dict[str, tuple[str, str, str] | None] = {"v": ("exiting", "exiting", "exiting")}
    t = MockJevTransport(answer=answer(0.99))
    k = make(t, now, GUARD, exit_in_flight=lambda pid: state["v"])
    pos = position(now)
    assert (await k.review(pos, now)).action == "hold"
    state["v"] = None
    assert (await k.review(pos, now + timedelta(seconds=1))).action == "close"


async def test_replacing_leg_defers_close_at_most_30s(now: datetime) -> None:
    t = MockJevTransport(answer=answer(0.99))
    k = make(t, now, GUARD, exit_in_flight=lambda pid: ("replacing", "gen-2", "第 2 代 stop_loss"))
    pos = position(now)
    assert (await k.review(pos, now)).action == "hold"
    assert (await k.review(pos, now + timedelta(seconds=30))).action == "hold"
    assert (await k.review(pos, now + timedelta(seconds=31))).action == "close"


async def test_each_replacement_gets_its_own_wait(now: datetime) -> None:
    """换代时拦过一次；之后几轮不在途、模型说拿住；很久以后又在换代，照样要等。"""
    state: dict[str, Any] = {"v": ("replacing", "gen-2", "第 2 代 stop_loss"), "p": 0.99}
    t = MockJevTransport(answer=lambda s, q: answer(state["p"])(s, q))
    k = make(t, now, GUARD, exit_in_flight=lambda pid: state["v"])
    pos = position(now)
    assert (await k.review(pos, now)).action == "hold"
    state.update(v=None, p=0.1)
    for i in range(1, 4):
        assert (await k.review(pos, now + timedelta(seconds=30 * i))).action == "hold"
    state.update(v=("replacing", "gen-2", "第 2 代 stop_loss"), p=0.99)  # 同样的说明，也要重新等
    v = await k.review(pos, now + timedelta(seconds=200))
    assert v.action == "hold" and "正在换代" in v.reason


async def test_new_generation_restarts_the_replacing_wait(now: datetime) -> None:
    state: dict[str, Any] = {"v": ("replacing", "gen-2", "第 2 代 stop_loss")}
    t = MockJevTransport(answer=answer(0.99))
    k = make(t, now, GUARD, exit_in_flight=lambda pid: state["v"])
    pos = position(now)
    assert (await k.review(pos, now)).action == "hold"
    state["v"] = ("replacing", "gen-3", "第 3 代 stop_loss")
    assert (await k.review(pos, now + timedelta(seconds=25))).action == "hold"
    assert (await k.review(pos, now + timedelta(seconds=50))).action == "hold"
    assert (await k.review(pos, now + timedelta(seconds=56))).action == "close"


async def test_leg_steps_within_one_generation_do_not_restart_the_wait(now: datetime) -> None:
    state: dict[str, Any] = {"v": ("replacing", "gen-2", "第 2 代 stop_loss submit_pending")}
    t = MockJevTransport(answer=answer(0.99))
    k = make(t, now, GUARD, exit_in_flight=lambda pid: state["v"])
    pos = position(now)
    assert (await k.review(pos, now)).action == "hold"
    state["v"] = ("replacing", "gen-2", "第 2 代 stop_loss cancel_pending")
    assert (await k.review(pos, now + timedelta(seconds=20))).action == "hold"
    assert (await k.review(pos, now + timedelta(seconds=31))).action == "close"


async def test_exit_stages_count_as_one_exit(now: datetime) -> None:
    """触发 → 退出 → 收尾：说明变了，等待不重新起算。"""
    state: dict[str, Any] = {"v": ("exiting", "exiting", "leg_triggered")}
    t = MockJevTransport(answer=answer(0.99))
    k = make(t, now, GUARD, exit_in_flight=lambda pid: state["v"])
    pos = position(now)
    assert (await k.review(pos, now)).action == "hold"
    state["v"] = ("exiting", "exiting", "exiting")
    assert (await k.review(pos, now + timedelta(seconds=60))).action == "hold"
    state["v"] = ("exiting", "exiting", "cleanup")
    assert (await k.review(pos, now + timedelta(seconds=121))).action == "close"


async def test_back_to_back_replacements_are_capped_in_total(now: datetime, tmp_path: Path) -> None:
    """每 25 秒换一代（每代都没到 30 秒），连续在途合计超过 180 秒就照常平。"""
    log = JsonlDecisionLog(tmp_path)
    state: dict[str, Any] = {"v": None}
    t = MockJevTransport(answer=answer(0.99))
    k = make(t, now, GUARD, log=log, exit_in_flight=lambda pid: state["v"])
    pos = position(now)
    for i in range(8):  # 0、25 … 175 秒
        state["v"] = ("replacing", f"gen-{i}", f"第 {i} 代 stop_loss")
        assert (await k.review(pos, now + timedelta(seconds=25 * i))).action == "hold"
    state["v"] = ("replacing", "gen-8", "第 8 代 stop_loss")
    assert (await k.review(pos, now + timedelta(seconds=181))).action == "close"
    assert "连续在途 181 秒" in keeper_records(log, now)[-1].output["close_guard_note"]


async def test_card_hard_close_is_also_blocked_while_exiting(now: datetime) -> None:
    t = MockJevTransport(answer=answer(0.0))
    k = JevKeeper(
        JevDecisionModel(t, GUARD, timeout_ms=200),
        GUARD,
        lambda s: features(now),
        lambda n: card(now, bias="short"),
        exit_in_flight=lambda pid: ("exiting", "exiting", "cleanup"),
    )
    v = await k.review(position(now), now)
    assert v.action == "hold" and "反向硬规则" in v.reason and t.calls == []


async def test_loop_card_channel_does_not_tighten_a_blocked_close(
    tmp_path: Path, now: datetime
) -> None:
    """换代中 + 反向卡价格已确认：规则通道既不平也不收紧（收紧本身会换代或平仓）。"""
    from tests.test_opposite_card_tighten import TIGHT, TightenExecutor, _runner, drain

    cfg = TIGHT.model_copy(update={"keeper_close_guard": True})
    model = JevDecisionModel(MockJevTransport(answer=answer(0.0)), cfg, timeout_ms=200)
    k = JevKeeper(
        model,
        cfg,
        lambda s: features(now, price=67900.0),  # 亏 0.92R：确认
        lambda n: card(now, bias="short"),
        exit_in_flight=lambda pid: ("replacing", "gen-2", "第 2 代 stop_loss submit_pending"),
    )
    ex = TightenExecutor(position(now), {"action": "close", "why": "not_armed"})
    runner = _runner(tmp_path, now, k, ex)
    await runner._review_card_rules()
    await drain(runner)
    assert ex.tighten_calls == [] and ex.closed == []


# ---- 执行器：执行所标记价、保护腿在途


class _MarkExchange:
    def __init__(self) -> None:
        self.reads = 0

    async def execution_mark(self, symbol: str, *, max_age_s: float) -> MarkEvidence:
        assert max_age_s == 600.0
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


def test_protection_exit_in_flight_identity_ignores_leg_steps() -> None:
    a = _adapter(None, lambda: START)
    a._registry = _protection("provisional", True, _leg("submit_pending"))
    first = a.protection_exit_in_flight("pos-1")
    a._registry = _protection("provisional", True, _leg("cancel_pending"))
    second = a.protection_exit_in_flight("pos-1")
    assert first is not None and second is not None
    assert first[:2] == second[:2] == ("replacing", "gen-0") and first[2] != second[2]
    keys = set()
    for stage, legs in (
        ("armed", [_leg("confirmed", triggered=True)]),
        ("exiting", []),
        ("cleanup", []),
    ):
        a._registry = _protection(stage, True, *legs)
        got = a.protection_exit_in_flight("pos-1")
        assert got is not None
        keys.add(got[:2])
    assert keys == {("exiting", "exiting")}


async def test_pre_close_guard_blocks_while_exit_in_flight_and_marks_the_round(
    now: datetime,
) -> None:
    state: dict[str, tuple[str, str, str] | None] = {"v": ("exiting", "exiting", "x")}
    t = MockJevTransport(answer=answer(0.99))
    k = make(t, now, GUARD, review_interval_s=20.0, exit_in_flight=lambda pid: state["v"])
    pos = position(now)
    assert "交易所保护腿已触发或正在退出" in (k.pre_close_guard(pos.position_id, now) or "")
    state["v"] = None
    assert k.pre_close_guard(pos.position_id, now) is None
    again = await k.review(pos, now + timedelta(seconds=5))  # 管家同一轮不再发第二次
    assert again.action == "hold" and "这一轮已发过平仓请求" in again.reason
