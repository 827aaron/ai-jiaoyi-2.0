"""#314 反向作战卡：价格确认才平，否则收紧托管止损（合成数据，不连模型 / 交易所）。"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import shutil
import subprocess
from contextlib import suppress
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from jiaoyi import contracts as c
from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.core import hotconfig
from jiaoyi.core.config import load_app_config
from jiaoyi.execution.brackets import BracketManager, ProtectionIncomplete
from jiaoyi.execution.exchange import EntryBasis
from jiaoyi.execution.hb_executor import HbExecutorAdapter
from jiaoyi.execution.tighten import breakeven_stop_price
from jiaoyi.keeper.keeper import JevKeeper, opposite_ab_draw, opposite_instruction_key
from jiaoyi.loop import Runner
from jiaoyi.panel.protection import parse_protection
from tests.conftest import ROOT
from tests.fastloop_helpers import FAST, card, features
from tests.test_execution import START
from tests.test_keeper_patrol import close_p, position
from tests.test_loop import FakeExecutor, _app
from tests.test_server_protection_regression import AdapterHarness

TIGHT = c.FastLoopConfig(opposite_card_action="confirm_or_tighten")


class Rec:
    """只收集管家决策记录。"""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def record(self, now: datetime, outcome: c.Outcome, summary: str, **kw: Any) -> None:
        self.rows.append({"outcome": outcome, "summary": summary, **kw})


def make_keeper(
    now: datetime,
    cfg: c.FastLoopConfig = TIGHT,
    price: list[float] | None = None,
    cd: c.BattleCard | None = None,
) -> tuple[JevKeeper, Rec, AsyncMock, list[float]]:
    holder = price if price is not None else [68000.0]
    model = AsyncMock()
    model.ask.side_effect = AssertionError("规则通道不得调用模型")
    rec = Rec()
    k = JevKeeper(
        model,
        cfg,
        lambda _: features(now, price=holder[0], atr=80.0),
        lambda _: cd if cd is not None else card(now, bias="short"),
        recorder=rec,  # type: ignore[arg-type]
    )
    return k, rec, model, holder


# ---------------------------------------------------------------- 默认 = 今天


def test_default_setting_is_immediate_close_and_never_reads_market(now: datetime) -> None:
    assert FAST.opposite_card_action == "close" and FAST.opposite_card_ab == 0
    k, rec, _, _ = make_keeper(now, FAST)
    k._features = lambda _: (_ for _ in ()).throw(AssertionError("默认不读行情"))
    pos = position(now)
    assert k.card_rule_watch(pos, now) is False
    v = k.review_card_rule(pos, now)
    assert v is not None and v.action == "close"
    assert v.reason == "作战卡反向硬规则：新作战卡方向改成 short，不等模型或确认"
    rule = rec.rows[-1]["output"]["card_rule"]
    assert "arm" not in rule and "confirm" not in rule and rule["requires_confirmation"] is False


# ---------------------------------------------------------------- 价格确认


def test_loss_in_r_confirms_and_closes_on_the_same_path(now: datetime) -> None:
    cfg = TIGHT.model_copy(
        update={"opposite_confirm_atr": 0.0, "opposite_confirm_key_level": False}
    )
    # 开仓 68000、止损 67880（R=120）；67930 亏 0.58R ≥ 0.5R
    k, rec, _, _ = make_keeper(now, cfg, [67930.0])
    v = k.review_card_rule(position(now), now)
    assert v is not None and v.action == "close" and "价格确认" in v.reason
    rule = rec.rows[-1]["output"]["card_rule"]
    assert rule["branch"] == "confirmed_close" and rule["arm"] == "confirm_or_tighten"
    assert rule["confirm"]["loss_r"]["hit"] and rule["confirm"]["loss_r"]["r_distance"] == 120
    assert rule["confirm"]["loss_r"]["pnl_r"] == pytest.approx(-70 / 120, abs=1e-4)


def test_key_level_of_opposite_card_confirms(now: datetime) -> None:
    cfg = TIGHT.model_copy(update={"opposite_confirm_loss_r": 0.0, "opposite_confirm_atr": 0.0})
    levels = [
        {"price": 67990.0, "kind": "support"},
        {"price": 67000.0, "kind": "support"},
        {"price": 68100.0, "kind": "resistance"},
    ]
    cd = card(now, bias="short", key_levels=levels)
    k, rec, _, price = make_keeper(now, cfg, [67995.0], cd)
    v = k.review_card_rule(position(now), now)
    assert v is not None and v.action == "hold"  # 开仓价下方最近的支撑 67990 还没破
    assert rec.rows[-1]["output"]["card_rule"]["confirm"]["key_level"]["level"] == 67990.0
    price[0] = 67989.0
    v = k.review_card_rule(position(now), now)
    assert v is not None and v.action == "close"
    assert rec.rows[-1]["output"]["card_rule"]["confirm"]["key_level"]["hit"] is True


def test_atr_move_since_first_sighting_confirms_later_and_closes(now: datetime) -> None:
    cfg = TIGHT.model_copy(
        update={"opposite_confirm_loss_r": 0.0, "opposite_confirm_key_level": False}
    )
    k, rec, _, price = make_keeper(now, cfg, [68000.0])
    first = k.review_card_rule(position(now), now)
    assert first is not None and first.action == "hold"
    price[0] = 67930.0  # 只走了 0.875 ATR
    later = now + timedelta(minutes=5)
    assert k.review_card_rule(position(now), later).action == "hold"  # type: ignore[union-attr]
    assert len(rec.rows) == 1  # 同一张卡的「收紧」只记第一次
    price[0] = 67919.0  # 1.01 ATR
    v = k.review_card_rule(position(now), later)
    assert v is not None and v.action == "close"
    rule = rec.rows[-1]["output"]["card_rule"]
    assert rule["ref_price"] == 68000.0 and rule["ref_at"] == now.isoformat()
    assert rule["confirm"]["atr"]["hit"] and rule["confirm"]["atr"]["against_atr"] > 1.0


async def test_unconfirmed_holds_logs_counterfactual_and_model_review_continues(
    now: datetime,
) -> None:
    from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport

    transport = MockJevTransport(answer=close_p(0.1))
    rec = Rec()
    k = JevKeeper(
        JevDecisionModel(transport, FAST, timeout_ms=200),
        TIGHT,
        lambda _: features(now, price=68010.0),
        lambda _: card(now, bias="short"),
        recorder=rec,  # type: ignore[arg-type]
    )
    pos = position(now)
    assert k.card_rule_watch(pos, now) is True
    v = await k.review(pos, now)
    assert v.action == "hold" and len(transport.calls) == 1  # 模型照常复查
    tighten = next(r for r in rec.rows if "card_rule" in r["output"])
    rule = tighten["output"]["card_rule"]
    assert rule["branch"] == "tighten" and rule["requires_confirmation"] is True
    assert rule["counterfactual"] == {
        "legacy_action": "close",
        "would_close_at": now.isoformat(),
        "would_close_price": 68010.0,
    }
    assert tighten["outcome"] == c.Outcome.HOLD and "收紧止损" in tighten["summary"]


def test_no_market_data_only_tightens_never_closes(now: datetime) -> None:
    k, rec, _, price = make_keeper(now, price=[67000.0])  # 有行情时会确认（亏 8R）
    k._features = lambda _: None  # 刚重启，还没有行情快照
    for _ in range(3):
        v = k.review_card_rule(position(now), now)
        assert v is not None and v.action == "hold" and "没有行情快照" in v.reason
    assert len(rec.rows) == 1
    rule = rec.rows[-1]["output"]["card_rule"]
    assert rule["branch"] == "no_market_data_tighten" and rule["confirm"] is None
    assert rule["counterfactual"]["would_close_at"] == now.isoformat()
    assert rule["counterfactual"]["would_close_price"] is None
    # 行情回来了：照常判确认
    k._features = lambda _: features(now, price=price[0], atr=80.0)
    later = now + timedelta(seconds=30)
    v = k.review_card_rule(position(now), later)
    assert v is not None and v.action == "close"
    assert rec.rows[-1]["output"]["card_rule"]["branch"] == "confirmed_close"


def test_counterfactual_keeps_first_opposite_event_across_new_instructions(
    now: datetime,
) -> None:
    k, rec, _, price = make_keeper(now, price=[68010.0])
    assert k.review_card_rule(position(now), now).action == "hold"  # type: ignore[union-attr]
    price[0] = 68020.0
    later = now + timedelta(minutes=10)
    reissued = card(now, bias="short", issued_at=(now + timedelta(minutes=5)).isoformat())
    k._card = lambda _: reissued
    assert k.review_card_rule(position(now), later).action == "hold"  # type: ignore[union-attr]
    rule = rec.rows[-1]["output"]["card_rule"]
    assert rule["counterfactual"]["would_close_at"] == now.isoformat()  # 第一次碰到反向卡
    assert rule["counterfactual"]["would_close_price"] == 68010.0
    assert rule["ref_price"] == 68020.0 and rule["ref_at"] == later.isoformat()  # 新指令新起点


async def test_loop_no_market_data_asks_executor_to_tighten_without_closing(
    tmp_path: Path, now: datetime
) -> None:
    k, _, _, _ = make_keeper(now)
    k._features = lambda _: None
    ex = TightenExecutor(position(now), {"action": "kept", "why": "already_tightened"})
    runner = _runner(tmp_path, now, k, ex)
    await runner._review_card_rules()
    await drain(runner)
    assert ex.closed == [] and len(ex.tighten_calls) == 1


# ---------------------------------------------------------------- A/B


def test_ab_draw_is_deterministic_and_arm_is_recorded(now: datetime) -> None:
    cd = card(now, bias="short")
    key = opposite_instruction_key(cd.symbols[0], cd)
    assert key == "BTCUSDT|2026-10-22T06:00:00+00:00|short"  # 币|这条指令的 issued_at|方向
    raw = f"pos-1|{key}".encode()
    expected = int.from_bytes(hashlib.sha256(raw).digest()[:8], "big") / 2**64
    assert opposite_ab_draw("pos-1", key) == expected
    # 整卡换版本（别的币变了）不改这条指令，抽签不变；重新开会出新指令才重抽
    bumped = cd.model_copy(update={"version": cd.version + 1})
    assert opposite_instruction_key(bumped.symbols[0], bumped) == key
    again = card(now, bias="short", issued_at="2026-10-22T06:20:00Z")
    assert opposite_instruction_key(again.symbols[0], again) != key
    draws = {f"pos-{i}": opposite_ab_draw(f"pos-{i}", key) for i in range(40)}
    low = next(p for p, d in draws.items() if d < 0.5)
    high = next(p for p, d in draws.items() if d >= 0.5)
    cfg = TIGHT.model_copy(update={"opposite_card_ab": 0.5})
    k, rec, _, _ = make_keeper(now, cfg, [68010.0], cd)
    new = k.review_card_rule(position(now, position_id=low), now)
    assert new is not None and new.action == "hold"
    assert rec.rows[-1]["output"]["card_rule"]["arm"] == "confirm_or_tighten"
    legacy = k.review_card_rule(position(now, position_id=high), now)
    assert legacy is not None and legacy.action == "close" and "A/B 原规则组" in legacy.reason
    rule = rec.rows[-1]["output"]["card_rule"]
    assert rule["arm"] == "close" and rule["branch"] == "ab_legacy_close"
    assert rule["ab"] == 0.5 and rule["ab_draw"] == pytest.approx(draws[high], abs=1e-6)
    assert rule["counterfactual"] == {"new_rule": "tighten"}
    # 重算 / 新建管家（重启）抽到同一组
    k2, _, _, _ = make_keeper(now, cfg, [68010.0], cd)
    assert k2.card_rule_watch(position(now, position_id=low), now) is True
    assert k2.card_rule_watch(position(now, position_id=high), now) is False
    assert k2.card_rule_key(position(now, position_id=low), now) == key
    k2._card = lambda _: bumped
    assert k2.card_rule_watch(position(now, position_id=low), now) is True


# ---------------------------------------------------------------- 主循环接线


class TightenExecutor(FakeExecutor):
    def __init__(self, pos: c.PositionRecord, result: dict[str, Any] | None) -> None:
        super().__init__()
        self.held = [pos]
        self.result = result
        self.tighten_calls: list[tuple[str, str, float, float]] = []

    def live_positions(self) -> list[c.PositionRecord]:
        return self.held

    async def tighten_stop(
        self, position_id: str, *, reason: str, buffer_bps: float, mark_gap_bps: float
    ) -> dict[str, Any]:
        self.tighten_calls.append((position_id, reason, buffer_bps, mark_gap_bps))
        assert self.result is not None
        return self.result

    async def close(self, position_id: str, reason: CloseReason) -> None:
        await super().close(position_id, reason)
        self.held = [self.held[0].model_copy(update={"status": PositionStatus.CLOSING})]


async def drain(runner: Runner) -> None:
    while runner._tasks:
        await asyncio.gather(*runner._tasks)
        await asyncio.sleep(0)


def _runner(tmp_path: Path, now: datetime, k: JevKeeper, ex: FakeExecutor) -> Runner:
    app = _app(tmp_path, now)
    app.m = dataclasses.replace(app.m, executor=ex, keeper=k)
    runner = Runner(app)
    runner.positions = [position(now)]
    return runner


async def test_loop_tightens_instead_of_closing_then_closes_once_on_confirmation(
    tmp_path: Path, now: datetime
) -> None:
    k, _, _, price = make_keeper(now, price=[68010.0])
    ex = TightenExecutor(position(now), {"action": "tightened", "why": "sync_armed"})
    runner = _runner(tmp_path, now, k, ex)
    attempted = await runner._review_card_rules()
    await drain(runner)
    assert attempted == set()  # 收紧的仓模型复查照常
    assert ex.closed == [] and [c[0] for c in ex.tighten_calls] == ["pos-1"]
    assert "收紧止损" in ex.tighten_calls[0][1]
    fl = runner.app.app_cfg.fast_loop
    assert ex.tighten_calls[0][2:] == (
        fl.opposite_tighten_buffer_bps,
        fl.opposite_tighten_mark_gap_bps,
    )
    ex.result = None  # 同一条指令已收紧：之后每轮不再进执行器
    for _ in range(2):
        await runner._review_card_rules()
        await drain(runner)
    assert ex.closed == [] and len(ex.tighten_calls) == 1
    price[0] = 67900.0  # 亏 0.92R：确认
    await runner._review_card_rules()
    await drain(runner)
    await runner._review(position(now))  # 模型复查通道也会看到确认，但不重复发平仓
    await runner._review_card_rules()
    await drain(runner)
    assert ex.closed == [("pos-1", CloseReason.KEEPER)] and len(ex.tighten_calls) == 1


async def test_loop_retries_pending_but_not_kept(tmp_path: Path, now: datetime) -> None:
    k, _, _, _ = make_keeper(now, price=[68010.0])
    ex = TightenExecutor(position(now), {"action": "pending", "why": "sync_unconfirmed"})
    runner = _runner(tmp_path, now, k, ex)
    rec = runner.app.recorders[c.Role.KEEPER]
    rows: list[dict[str, Any]] = []
    rec.record = lambda *a, **kw: rows.append({"args": a, **kw})  # type: ignore[method-assign]
    for _ in range(2):
        await runner._review_card_rules()
        await drain(runner)
    assert len(ex.tighten_calls) == 2  # 没确认的会再进执行器
    ex.result = {"action": "kept", "why": "already_tightened", "target": 68_050.0}
    for _ in range(2):
        await runner._review_card_rules()
        await drain(runner)
    assert len(ex.tighten_calls) == 3 and ex.closed == []
    logged = [r["output"] for r in rows if "opposite_tighten" in r.get("output", {})]
    assert [o["opposite_tighten"]["action"] for o in logged] == ["pending", "kept"]
    assert logged[-1]["instruction_key"] == "BTCUSDT|2026-10-22T06:00:00+00:00|short"
    assert logged[-1]["counterfactual"] == {"estimated_target": 68_050.0, "placeable": None}


@pytest.mark.parametrize(
    "why", ["not_server_managed", "not_armed", "target_crossed", "evidence_unavailable"]
)
async def test_loop_closes_when_executor_cannot_tighten(
    tmp_path: Path, now: datetime, why: str
) -> None:
    k, _, _, _ = make_keeper(now, price=[68010.0])
    ex = TightenExecutor(position(now), {"action": "close", "why": why})
    runner = _runner(tmp_path, now, k, ex)
    rec = runner.app.recorders[c.Role.KEEPER]
    rows: list[dict[str, Any]] = []
    rec.record = lambda *a, **kw: rows.append({"args": a, **kw})  # type: ignore[method-assign]
    await runner._review_card_rules()
    await drain(runner)
    assert ex.closed == [("pos-1", CloseReason.KEEPER)]
    logged = [r for r in rows if "opposite_tighten" in r.get("output", {})]
    assert logged and logged[0]["output"]["opposite_tighten"]["why"] == why


async def test_loop_executor_without_tightening_closes_like_today(
    tmp_path: Path, now: datetime
) -> None:
    k, _, _, _ = make_keeper(now, price=[68010.0])
    ex = FakeExecutor()
    runner = _runner(tmp_path, now, k, ex)
    await runner._review_card_rules()
    await drain(runner)
    assert ex.closed == [("pos-1", CloseReason.KEEPER)]


async def test_loop_default_setting_still_closes_without_tightening(
    tmp_path: Path, now: datetime
) -> None:
    k, _, _, _ = make_keeper(now, FAST, [68010.0])
    ex = TightenExecutor(position(now), None)
    runner = _runner(tmp_path, now, k, ex)
    assert await runner._review_card_rules() == {"pos-1"}
    await drain(runner)
    assert ex.closed == [("pos-1", CloseReason.KEEPER)] and ex.tighten_calls == []


# ---------------------------------------------------------------- 执行层：托管止损换代


class TightenHarness(AdapterHarness):
    def __init__(self, root: Path, side: Side) -> None:
        super().__init__(root, side)
        self.side = side
        self.exchange.mark = 100.5 if side is Side.LONG else 99.5

        async def basis(symbol: str, client_order_id: str) -> EntryBasis:
            return EntryBasis(
                "BTCUSDT",
                "entry-1",
                "100",
                "buy" if side is Side.LONG else "sell",
                2.0,
                200.0,
                0.2,
                100.0,
                "FILLED",
            )

        self.exchange.entry_basis = basis  # type: ignore[attr-defined]

    @property
    def mark(self) -> float:
        return self.exchange.mark  # 带交易所时间的同环境标记价（execution_mark）

    @mark.setter
    def mark(self, value: float) -> None:
        self.exchange.mark = value

    def build(self) -> BracketManager:
        manager = super().build()
        manager.stop_working_type = "MARK_PRICE"
        return manager

    def make_adapter(self) -> HbExecutorAdapter:
        adapter = super().make_adapter()
        adapter._open_fee = 5 / 1e4
        return adapter

    async def tighten(self, gap: float = 10.0) -> dict[str, Any]:
        return await self.adapter.tighten_stop(
            "pos-1", reason="反向卡 v1 未确认", buffer_bps=5.0, mark_gap_bps=gap
        )

    def critical(self) -> list[Any]:
        return [a for a in self.alerter.sent if a.dedup_key == "opposite_tighten:pos-1"]

    def stop_legs(self) -> list[Any]:
        return [x for x in self.state().legs if x.kind == "stop_loss" and x.purpose != "lifeboat"]


def _target(side: Side) -> float:
    return 100.2 if side is Side.LONG else 99.8  # 2 位小数取整后的保本 + 5bp


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_breakeven_target_nets_exactly_the_buffer_after_fees(side: Side) -> None:
    price = breakeven_stop_price(side, 2.0, 200.0, 0.2, 5 / 1e4, 5.0)
    sign = 1 if side is Side.LONG else -1
    net = sign * (price * 2.0 - 200.0) - 0.2 - price * 2.0 * 5 / 1e4
    assert net == pytest.approx(200.0 * 5 / 1e4)
    assert price == pytest.approx(_target(side), abs=0.001)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_tighten_replaces_stop_leg_new_first_with_mark_price(tmp_path: Path, side: Side):
    h = TightenHarness(tmp_path, side)
    await h.protect()
    old = h.stop_legs()[0]
    assert old.working_type == "MARK_PRICE"
    result = await h.tighten()
    assert result["action"] == "tightened" and result["working_type"] == "MARK_PRICE"
    new = [x for x in h.stop_legs() if x.client_order_id != old.client_order_id]
    assert len(new) == 1 and new[0].trigger_price == _target(side)
    assert new[0].working_type == "MARK_PRICE" and new[0].state == "confirmed"
    assert new[0].purpose == "tightened"  # 自己的腿用途，触发后平仓原因单独记
    assert h.exchange.orders[new[0].client_order_id].working_type == "MARK_PRICE"
    assert h.cancels() == [old.client_order_id]
    events = h.exchange.events
    assert events.index(("create", new[0].client_order_id)) < events.index(
        ("cancel", old.client_order_id)
    )
    tracked = h.registry.get("pos-1")
    assert tracked.record.stop_loss_price == (99.0 if side is Side.LONG else 101.0)  # R 不变
    assert h.state().tightened_stop_price == pytest.approx(result["target"])
    assert h.state().tightened_reason == "反向卡 v1 未确认"
    # 再来一次：已收紧，不读交易所、不换单
    before = list(h.exchange.events)
    assert (await h.tighten())["action"] == "kept" and h.exchange.events == before
    assert h.critical() == [] and h.state().last_error is None


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_tightened_leg_trigger_closes_with_its_own_reason(tmp_path: Path, side: Side):
    h = TightenHarness(tmp_path, side)
    await h.protect()
    await h.tighten()
    leg = next(x for x in h.stop_legs() if x.purpose == "tightened")
    h.exchange.orders[leg.client_order_id] = replace(
        h.exchange.orders[leg.client_order_id],
        algo_status="FINISHED",
        child_state="identified",
        actual_order_id="201",
        actual_order_status="FILLED",
        filled_quantity=2.0,
        triggered_at=START,
    )
    h.exchange.held = 0
    assert await h.manager.sync("pos-1") == "exit"
    assert h.state().exit_reason is CloseReason.TIGHTENED_STOP


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_tightened_tighter_than_trailing_takes_the_leg(tmp_path: Path, side: Side):
    h = TightenHarness(tmp_path, side)
    await h.protect()
    trailing = 100.1 if side is Side.LONG else 99.9  # 比收紧价松
    h.manager._save("pos-1", trailing_stop_price=trailing, trailing_activated=True)
    assert await h.manager.sync("pos-1") == "armed"
    assert [x.purpose for x in h.stop_legs() if x.state == "confirmed"] == ["trailing"]
    assert (await h.tighten())["action"] == "tightened"
    live = [x for x in h.stop_legs() if x.state == "confirmed"]
    assert [(x.purpose, x.trigger_price) for x in live] == [("tightened", _target(side))]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_tightened_price_survives_restart_without_rebuilding(tmp_path: Path, side: Side):
    h = TightenHarness(tmp_path, side)
    await h.protect()
    await h.tighten()
    creates = list(h.creates())
    h.restart_adapter()
    assert h.state().tightened_stop_price is not None
    assert await h.manager.sync("pos-1") == "armed"
    assert h.creates() == creates  # 重启后同步仍按收紧价核对，不退回原止损
    live = [x for x in h.stop_legs() if x.state == "confirmed" and x.algo_status == "NEW"]
    assert [x.trigger_price for x in live] == [_target(side)]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_never_loosens_existing_tighter_trailing_or_tightened_stop(
    tmp_path: Path, side: Side
):
    h = TightenHarness(tmp_path, side)
    await h.protect()
    trailing = 101.5 if side is Side.LONG else 98.5
    h.manager._save("pos-1", trailing_stop_price=trailing)
    assert await h.manager.sync("pos-1") == "armed"
    creates = list(h.creates())
    result = await h.tighten()
    assert result["action"] == "kept" and result["why"] == "existing_stop_tighter"
    assert h.state().tightened_stop_price is None and h.creates() == creates
    # 收紧价本身只收紧：更松的价格不覆盖
    async with h.registry.symbol_lock("BTCUSDT"):
        assert h.manager.set_tightened_stop("pos-1", 100.3 if side is Side.LONG else 99.7, "a")
        assert not h.manager.set_tightened_stop("pos-1", 100.1 if side is Side.LONG else 99.9, "b")
    assert h.state().tightened_stop_price == (100.3 if side is Side.LONG else 99.7)
    # 同步时和移动止损取最紧：移动止损更紧，止损腿不动
    assert await h.manager.sync("pos-1") == "armed" and h.creates() == creates


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_target_already_crossed_by_mark_closes_instead(tmp_path: Path, side: Side):
    h = TightenHarness(tmp_path, side)
    await h.protect()
    h.mark = 100.1 if side is Side.LONG else 99.9
    creates = list(h.creates())
    result = await h.tighten()
    assert result["action"] == "close" and result["why"] == "target_crossed"
    assert result["reference_price"] == h.mark
    assert h.state().tightened_stop_price is None and h.creates() == creates
    assert result["placeable"] is False
    # 正常分支：不写 last_error、不发 CRITICAL（决策记录由主循环写）
    assert h.state().last_error is None and h.critical() == []


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_target_too_close_to_mark_closes_before_exchange_rejects(tmp_path: Path, side: Side):
    h = TightenHarness(tmp_path, side)
    await h.protect()
    # 目标 100.2 / 99.8 还在安全一侧，但离标记价不到 10 bp
    h.mark = 100.28 if side is Side.LONG else 99.72
    creates = list(h.creates())
    result = await h.tighten()
    assert result["action"] == "close" and result["why"] == "target_crossed"
    assert result["placeable_limit"] == pytest.approx(
        h.mark * (0.999 if side is Side.LONG else 1.001)
    )
    assert h.creates() == creates and h.state().tightened_stop_price is None
    # 设置 0 时仍按保护状态机换代用的标记价缓冲（默认 5 bp）核对：这个标记价够 5 bp，可以挂
    assert h.manager.trailing_stop_mark_buffer_bps == 5.0
    result = await h.tighten(gap=0.0)
    assert result["action"] == "tightened" and result["mark_gap_bps"] == 5.0
    h2 = TightenHarness(tmp_path / "b", side)
    await h2.protect()
    h2.mark = 100.24 if side is Side.LONG else 99.76  # 不够 5 bp
    assert (await h2.tighten(gap=0.0))["why"] == "target_crossed"


async def test_old_path_and_unarmed_positions_close(tmp_path: Path) -> None:
    h = TightenHarness(tmp_path, Side.LONG)
    result = await h.tighten()  # 还没同步过：stage = entry
    assert result == {"action": "close", "why": "not_armed", "stage": "entry"}
    await h.protect()
    armed = h.state()
    # 曾完整托管、开仓已终态，但这一轮退回临时保护（如仓量变化）：不收紧
    # （曾完整托管、退回临时保护的情形见下面单独的测试）
    # #325 开仓宽限期：从未完整托管、同步失败计时中（阶段字段被别处写成 armed 也不算）
    h.manager._save("pos-1", stage="armed", ever_armed=False, prearm_failing_since=START)
    assert (await h.tighten())["why"] == "not_armed"
    h.manager._save("pos-1", ever_armed=True, prearm_failing_since=START)
    assert (await h.tighten())["why"] == "not_armed"
    h.manager._save("pos-1", ever_armed=False, prearm_failing_since=None)
    assert (await h.tighten())["why"] == "not_armed"  # 从没完整托管过
    h.manager._save("pos-1", ever_armed=True, stage="exiting")
    exiting = {"action": "skip", "why": "protection_exiting", "stage": "exiting"}
    assert await h.tighten() == exiting
    h.manager._save("pos-1", **{k: getattr(armed, k) for k in ("stage", "prearm_failing_since")})
    assert h.state().tightened_stop_price is None
    tracked = h.registry.get("pos-1")
    h.registry.put(tracked.model_copy(update={"server_protection": None}))
    assert await h.tighten() == {"action": "close", "why": "not_server_managed"}


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_provisional_after_failed_replacement_persists_target_and_waits(
    tmp_path: Path, side: Side
) -> None:
    h = TightenHarness(tmp_path, side)
    await h.protect()
    old = h.stop_legs()[0].client_order_id
    # 真实的换代失败：固定止损换价（开仓价回填后），新腿提交结果未知 → 退回临时保护
    tracked = h.registry.get("pos-1")
    moved = 99.5 if side is Side.LONG else 100.5
    record = tracked.record.model_copy(update={"stop_loss_price": moved})
    h.registry.put(tracked.model_copy(update={"record": record}))
    h.exchange.unknown_submit = True
    with pytest.raises(ProtectionIncomplete):
        await h.manager.sync("pos-1")
    state = h.state()
    assert state.stage == "provisional" and state.ever_armed
    assert h.manager.retains_confirmed_stop("pos-1")
    creates = list(h.creates())
    result = await h.tighten()
    assert result["action"] == "pending" and result["why"] == "waiting_protection_sync"
    assert h.state().tightened_stop_price == pytest.approx(_target(side), abs=0.001)
    assert h.creates() == creates and h.exchange.orders[old].algo_status == "NEW"
    # 交易所恢复后，保护轮询的下一次同步直接按收紧价换代
    h.exchange.unknown_submit = False
    h.exchange.hidden.clear()
    assert await h.manager.sync("pos-1") == "armed"
    live = [x for x in h.stop_legs() if x.state == "confirmed"]
    assert [(x.purpose, x.trigger_price) for x in live] == [("tightened", _target(side))]


async def test_provisional_without_confirmed_stop_is_left_to_protection(tmp_path: Path) -> None:
    h = TightenHarness(tmp_path, Side.LONG)
    await h.protect()
    sl = h.stop_legs()[0]
    h.exchange.orders[sl.client_order_id] = replace(
        h.exchange.orders[sl.client_order_id], algo_status="CANCELED"
    )
    with suppress(ProtectionIncomplete):
        await h.manager._refresh("pos-1")
    h.manager._save("pos-1", stage="provisional")
    assert not h.manager.retains_confirmed_stop("pos-1")
    result = await h.tighten()
    assert result["action"] == "skip" and result["why"] == "protection_unconfirmed"
    assert h.state().tightened_stop_price is None


async def test_closing_position_is_not_tightened(tmp_path: Path) -> None:
    h = TightenHarness(tmp_path, Side.LONG)
    await h.protect()
    tracked = h.registry.get("pos-1")
    h.registry.put(tracked.model_copy(update={"requested_close": CloseReason.KEEPER}))
    assert await h.tighten() == {"action": "skip", "why": "not_open"}
    assert h.state().tightened_stop_price is None


async def test_unreadable_mark_closes_instead(tmp_path: Path) -> None:
    h = TightenHarness(tmp_path, Side.LONG)
    await h.protect()

    h.exchange.mark_at = START - timedelta(seconds=6)  # 标记价过期：不当作可用
    result = await h.tighten()
    assert result["action"] == "close" and result["why"] == "evidence_unavailable"
    assert "收紧止损的证据读不到" in (h.state().last_error or "")
    assert [a.level for a in h.critical()] == [c.AlertLevel.CRITICAL]
    assert h.state().trailing_stop_price is None  # 不碰移动止损自己的字段


async def test_unknown_replacement_keeps_target_and_old_stop(tmp_path: Path) -> None:
    h = TightenHarness(tmp_path, Side.LONG)
    await h.protect()
    old = h.stop_legs()[0].client_order_id
    h.exchange.unknown_submit = True
    result = await h.tighten()
    assert result["action"] == "pending" and result["why"] == "sync_unconfirmed"
    assert h.state().tightened_stop_price is not None
    assert h.exchange.orders[old].algo_status == "NEW" and not h.cancels()
    assert h.state().last_error and [a.level for a in h.critical()] == [c.AlertLevel.CRITICAL]


async def test_previous_trailing_generation_in_flight_waits_instead_of_failing(
    tmp_path: Path,
) -> None:
    h = TightenHarness(tmp_path, Side.LONG)
    await h.protect()
    legs = h.state().legs
    sl = next(x for x in legs if x.kind == "stop_loss" and x.purpose != "lifeboat")
    pending = sl.model_copy(update={"state": "submit_pending"})
    h.manager._save(
        "pos-1", legs=[pending if x.client_order_id == sl.client_order_id else x for x in legs]
    )
    creates = list(h.creates())
    result = await h.tighten()
    assert result["action"] == "pending" and result["why"] == "waiting_previous_generation"
    assert h.state().tightened_stop_price == pytest.approx(_target(Side.LONG), abs=0.001)
    assert h.creates() == creates and h.critical() == []
    # 上一代确认后，保护轮询的下一次同步按收紧价换代
    h.manager._save("pos-1", legs=legs)
    assert await h.manager.sync("pos-1") == "armed"
    live = [x for x in h.stop_legs() if x.state == "confirmed"]
    assert [(x.purpose, x.trigger_price) for x in live] == [("tightened", _target(Side.LONG))]


# ---------------------------------------------------------------- 设置


def test_config_validation() -> None:
    c.FastLoopConfig(opposite_card_action="confirm_or_tighten", opposite_card_ab=0.3)
    with pytest.raises(ValidationError, match="opposite_card_ab"):
        c.FastLoopConfig(opposite_card_ab=0.3)  # 默认 close 时不能分组
    for bad in (
        {"opposite_card_action": "tighten"},
        {"opposite_card_action": "confirm_or_tighten", "opposite_card_ab": 1.01},
        {"opposite_confirm_loss_r": -0.1},
        {"opposite_confirm_atr": -1.0},
        {"opposite_tighten_buffer_bps": -1.0},
        {"opposite_tighten_buffer_bps": 201.0},
    ):
        with pytest.raises(ValidationError):
            c.FastLoopConfig(**bad)
    shipped = load_app_config(ROOT / "config" / "app.example.toml").fast_loop
    assert shipped.opposite_card_action == "close" and shipped.opposite_card_ab == 0
    names = {
        "opposite_card_action",
        "opposite_card_ab",
        "opposite_confirm_loss_r",
        "opposite_confirm_atr",
        "opposite_confirm_key_level",
        "opposite_tighten_buffer_bps",
    }
    assert names <= hotconfig.HOT_FAST


# ---------------------------------------------------------------- 面板


def _protection_raw() -> dict[str, Any]:
    return {
        "version": 1,
        "account_name": "a",
        "connector_name": "c",
        "intended_quantity": 0.5,
        "stage": "armed",
        "ever_armed": True,
        "legs": [
            {
                "kind": "stop_loss",
                "purpose": "tightened",
                "generation": 2,
                "client_order_id": "br-sl-pos-1-2",
                "quantity": 0.5,
                "trigger_price": 68_050.0,
                "state": "confirmed",
                "algo_id": "9",
                "algo_status": "NEW",
                "child_state": "absent",
            }
        ],
        "observed_at": START.isoformat(),
        "tightened_stop_price": 68_050.0,
        "tightened_reason": "作战卡反向（card-1:v2）价格未确认",
        "tightened_at": START.isoformat(),
    }


def test_panel_shows_tightened_stop_read_only() -> None:
    view = parse_protection(_protection_raw(), START, 120)
    assert view is not None and view.tightened_stop_price == 68_050.0
    assert view.tightened_reason == "作战卡反向（card-1:v2）价格未确认"
    assert view.tightened_at == START
    plain = parse_protection({**_protection_raw(), "tightened_stop_price": None}, START, 120)
    assert plain is not None and plain.tightened_stop_price is None
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    helpers = source[source.index("const TRAIL_TIP =") : source.index("const cardPos =")]
    p = {"symbol": "BTCUSDT", "side": "long", "protection": json.loads(view.model_dump_json())}
    script = (
        """
const assert = require('node:assert/strict');
const esc = value => String(value);
const price = value => value == null ? '—' : String(value);
const signed = (value, digits) => (value >= 0 ? '+' : '') + value.toFixed(digits);
const cls = () => '';
const TZ = 'Asia/Bangkok';
const qtyFmt = value => String(value);
const dur = ms => Math.round(ms / 60000) + ' 分';
"""
        + helpers
        + "\nconst p = "
        + json.dumps(p)
        + """;
const block = protBlock(p), brief = protBrief(p);
assert(block.includes('反向作战卡收紧止损') && block.includes('68050'), block);
assert(block.includes('card-1:v2'), block);
assert(brief.includes('反向卡收紧止损 68050'), brief);
assert(block.includes('<span class="n">反向卡收紧止损</span>'), block);
p.protection.tightened_stop_price = null;
assert(!protBlock(p).includes('反向作战卡'));
"""
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


# ---------------------------------------------------------------- 接 #333 的止损目标


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_live_tightened_leg_restart_never_replaced_by_looser(tmp_path: Path, side: Side):
    h = TightenHarness(tmp_path, side)
    tracked = h.registry.get("pos-1")
    no_trail = tracked.record.model_copy(
        update={"trailing_activation_pct": None, "trailing_stop_pct": None}
    )
    h.registry.put(tracked.model_copy(update={"record": no_trail}))
    await h.protect()
    assert (await h.tighten())["action"] == "tightened"
    assert h.state().trailing_stop_price is None
    creates = list(h.creates())
    h.restart_adapter()  # 新的 BracketManager 从账本读
    assert h.manager.live_stop_price("pos-1") == _target(side)
    assert await h.manager.sync("pos-1") == "armed"
    # 账本固定止损被开仓价回填改得更松：收紧价仍是下限，不换松
    tracked = h.registry.get("pos-1")
    looser = 98.5 if side is Side.LONG else 101.5
    record = tracked.record.model_copy(update={"stop_loss_price": looser})
    h.registry.put(tracked.model_copy(update={"record": record}))
    assert await h.manager.sync("pos-1") == "armed"
    assert h.creates() == creates and h.manager.live_stop_price("pos-1") == _target(side)
    assert [x.purpose for x in h.stop_legs() if x.state == "confirmed"] == ["tightened"]
    assert h.state().tightened_stop_price is not None  # 持仓期间不清、不放松


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_tightened_candidate_ignores_trailing_min_step(tmp_path: Path, side: Side):
    h = TightenHarness(tmp_path, side)
    await h.protect()
    h.manager.trailing_min_step_bps = 500.0  # 移动止损要收紧 5% 才换代
    assert (await h.tighten())["action"] == "tightened"
    assert h.manager.live_stop_price("pos-1") == _target(side)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_persisted_tightened_beyond_mark_is_clamped_by_protection(tmp_path: Path, side: Side):
    # 落盘后才越过标记价（还没换上就跌了）：由 #333 夹到标记价旁可挂的最紧价，不走原规则平
    h = TightenHarness(tmp_path, side)
    await h.protect()
    async with h.registry.symbol_lock("BTCUSDT"):
        h.manager.set_tightened_stop("pos-1", _target(side), "反向卡")
    h.mark = 100.1 if side is Side.LONG else 99.9
    assert await h.manager.sync("pos-1") == "armed"
    live = [x for x in h.stop_legs() if x.state == "confirmed"]
    expected = round(h.mark * (0.9995 if side is Side.LONG else 1.0005), 2)
    assert [(x.purpose, x.trigger_price) for x in live] == [("tightened", expected)]
    assert h.state().tightened_stop_price == _target(side)
