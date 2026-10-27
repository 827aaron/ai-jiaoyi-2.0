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
from jiaoyi.execution.exchange import EntryBasis, ExecutionBook
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
    state: Path | None = None,
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
        opposite_state_path=state,
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
        self.extra: list[tuple[str, float, str | None]] = []

    def live_positions(self) -> list[c.PositionRecord]:
        return self.held

    async def tighten_stop(
        self,
        position_id: str,
        *,
        reason: str,
        buffer_bps: float,
        mark_gap_bps: float,
        profit_mode: str,
        distance_mult: float,
        card_key: str | None,
        anchor_retry_s: float,
    ) -> dict[str, Any]:
        self.tighten_calls.append((position_id, reason, buffer_bps, mark_gap_bps))
        self.extra.append((profit_mode, distance_mult, card_key))
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
    assert "在现价启动移动止损" in ex.tighten_calls[0][1]  # 默认 trail 模式
    assert ex.extra[0] == (
        runner.app.app_cfg.fast_loop.opposite_profit_mode,
        runner.app.app_cfg.fast_loop.opposite_trail_distance_mult,
        "BTCUSDT|2026-10-22T06:00:00+00:00|short",
    )
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
    before, reads = list(h.exchange.events), h.exchange.mark_reads
    again = await h.tighten()
    assert again["action"] == "kept" and again["why"] == "already_tightened"
    assert h.exchange.events == before and h.exchange.mark_reads == reads
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
    # 同样的临时保护、没有确认在挂的止损，但亏着 / 赚得不够：照用户规则直接平
    h.mark = 100.1
    result = await h.tighten()
    assert result["action"] == "close" and result["why"] == "target_crossed"
    assert h.critical() == []


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
        {"opposite_profit_mode": "stop"},
        {"opposite_trail_distance_mult": 0.0},
        {"opposite_trail_distance_mult": 5.1},
        {"opposite_anchor_retry_s": -1.0},
        {"opposite_anchor_retry_s": 601.0},
    ):
        with pytest.raises(ValidationError):
            c.FastLoopConfig(**bad)
    shipped = load_app_config(ROOT / "config" / "app.example.toml").fast_loop
    assert shipped.opposite_card_action == "close" and shipped.opposite_card_ab == 0
    assert shipped.opposite_profit_mode == "trail" and shipped.opposite_trail_distance_mult == 1.0
    names = {
        "opposite_card_action",
        "opposite_card_ab",
        "opposite_confirm_loss_r",
        "opposite_confirm_atr",
        "opposite_confirm_key_level",
        "opposite_tighten_buffer_bps",
        "opposite_tighten_mark_gap_bps",
        "opposite_profit_mode",
        "opposite_trail_distance_mult",
        "opposite_anchor_retry_s",
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


# ---------------------------------------------------------------- trail 模式（默认）


class TrailHarness(TightenHarness):
    """开仓带移动止损（启动 0.3%、回撤 1.2%），盘口 / 标记价都在 START。"""

    def __init__(self, root: Path, side: Side) -> None:
        super().__init__(root, side)
        tracked = self.registry.get("pos-1")
        record = tracked.record.model_copy(
            update={"trailing_activation_pct": 0.3, "trailing_stop_pct": 1.2}
        )
        self.registry.put(tracked.model_copy(update={"record": record}))
        self.book_at = START
        self.spread = 0.1

        async def book(symbol: str) -> ExecutionBook:
            px = self.exchange.mark
            bid, ask = (px, px + self.spread) if side is Side.LONG else (px - self.spread, px)
            return ExecutionBook(symbol, bid, ask, self.book_at, self.book_at)

        self.exchange.execution_book = book  # type: ignore[attr-defined]

    async def trail(
        self, mult: float = 1.0, key: str = "BTCUSDT|t1|short", retry_s: float = 0.0
    ) -> dict[str, Any]:
        return await self.adapter.tighten_stop(
            "pos-1",
            reason="反向卡 v1 未确认",
            buffer_bps=5.0,
            mark_gap_bps=10.0,
            profit_mode="trail",
            distance_mult=mult,
            card_key=key,
            anchor_retry_s=retry_s,
        )

    def at(self, when: datetime) -> None:
        """把执行器、保护状态机、标记价和盘口都挪到同一时刻。"""
        self.adapter._clock = lambda: when
        self.manager.clock = lambda: when
        self.exchange.mark_at = self.book_at = when

    def live(self) -> list[tuple[str, float]]:
        return [(x.purpose, x.trigger_price) for x in self.stop_legs() if x.state == "confirmed"]


def _trail_stop(side: Side, peak_px: float, distance_pct: float) -> float:
    """2 张、名义 200、开仓费 0.2：峰值价 → 净收益 → 减距离 → 逆算止损价（2 位小数）。"""
    sign = 1 if side is Side.LONG else -1
    peak = (sign * (peak_px * 2 - 200) - 0.2) / 200
    threshold = peak - distance_pct / 100
    return round((200 + sign * (200 * threshold + 0.2)) / 2, 2)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_trail_anchors_at_current_executable_price(tmp_path: Path, side: Side):
    h = TrailHarness(tmp_path, side)
    await h.protect()
    result = await h.trail()
    assert result["action"] == "tightened" and result["mode"] == "trail"
    price = 100.5 if side is Side.LONG else 99.5  # 多看买一、空看卖一
    state = h.state()
    assert state.opposite_anchor_price == price and state.trailing_activated
    assert state.trailing_distance_pct == pytest.approx(1.2)
    assert state.opposite_card_key == "BTCUSDT|t1|short"
    stop = _trail_stop(side, price, 1.2)
    assert stop == (99.3 if side is Side.LONG else 100.7)  # 赚得少：止损落在开仓价另一侧
    assert h.live() == [("trailing", stop)]
    assert state.tightened_stop_price is None
    assert h.registry.get("pos-1").record.stop_loss_price == (99.0 if side is Side.LONG else 101.0)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_trail_anchor_then_ratchets_with_multiplied_distance(tmp_path: Path, side: Side):
    h = TrailHarness(tmp_path, side)
    await h.protect()
    assert (await h.trail(mult=0.5))["action"] == "tightened"
    assert h.live() == [("trailing", _trail_stop(side, h.mark, 0.6))]
    # 价格往有利方向走：照常的移动止损按锚定时定下的 0.6% 距离继续收紧
    later = START + timedelta(seconds=1)
    h.manager.clock = lambda: later
    h.book_at = h.exchange.mark_at = later
    h.mark = 101.5 if side is Side.LONG else 98.5
    basis = await h.exchange.entry_basis("BTCUSDT", "entry-1")  # type: ignore[attr-defined]
    assert await h.manager.observe_trailing("pos-1", basis, h.exchange.execution_book)  # type: ignore[attr-defined]
    assert await h.manager.sync("pos-1") == "armed"
    assert h.live() == [("trailing", _trail_stop(side, h.mark, 0.6))]
    # 回落：只收紧不放松
    h.mark = 100.8 if side is Side.LONG else 99.2
    later2 = START + timedelta(seconds=2)
    h.manager.clock = lambda: later2
    h.book_at = h.exchange.mark_at = later2
    await h.manager.observe_trailing("pos-1", basis, h.exchange.execution_book)  # type: ignore[attr-defined]
    assert await h.manager.sync("pos-1") == "armed"
    assert h.live() == [("trailing", _trail_stop(side, 101.5 if side is Side.LONG else 98.5, 0.6))]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_trail_no_reanchor_on_new_card_or_restart(tmp_path: Path, side: Side):
    h = TrailHarness(tmp_path, side)
    await h.protect()
    await h.trail()
    anchored = h.state().opposite_anchor_price
    h.mark = 101.0 if side is Side.LONG else 99.0  # 价格变了也不重锚
    reads = h.exchange.mark_reads
    again = await h.trail(key="BTCUSDT|t2|short")
    assert again["action"] == "kept" and again["why"] == "already_tightened"
    assert again["anchor_price"] == anchored and h.exchange.mark_reads == reads
    h.restart_adapter()

    async def book(symbol: str) -> ExecutionBook:
        raise AssertionError("已锚定不再读盘口")

    h.exchange.execution_book = book  # type: ignore[attr-defined]
    assert (await h.trail(key="BTCUSDT|t3|short"))["action"] == "kept"
    state = h.state()
    assert state.opposite_anchor_price == anchored and state.opposite_card_key == "BTCUSDT|t1|short"


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_trail_anchor_never_looser_than_live_stop(tmp_path: Path, side: Side):
    h = TrailHarness(tmp_path, side)
    tracked = h.registry.get("pos-1")
    tight = 99.6 if side is Side.LONG else 100.4  # 账本固定止损比锚定的 99.3 / 100.7 还紧
    record = tracked.record.model_copy(update={"stop_loss_price": tight})
    h.registry.put(tracked.model_copy(update={"record": record}))
    await h.protect()
    creates = list(h.creates())
    result = await h.trail()
    assert result["action"] == "kept" and result["why"] == "live_stop_unchanged"
    assert result["anchored_stop"] == pytest.approx(99.3 if side is Side.LONG else 100.7)
    assert h.live() == [("fixed", tight)] and h.creates() == creates
    assert h.state().opposite_anchor_price is not None  # 锚还是落了，之后移动止损从这里收紧


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_trail_losing_position_closes_and_does_not_anchor(tmp_path: Path, side: Side):
    h = TrailHarness(tmp_path, side)
    await h.protect()
    h.mark = 100.1 if side is Side.LONG else 99.9  # 扣费保本价 100.2 / 99.8 挂不上
    creates = list(h.creates())
    result = await h.trail()
    assert result["action"] == "close" and result["why"] == "target_crossed"
    assert h.state().opposite_anchor_price is None and h.creates() == creates
    assert h.critical() == [] and h.state().last_error is None


async def test_trail_without_trailing_config_falls_back_to_breakeven(tmp_path: Path) -> None:
    h = TightenHarness(tmp_path, Side.LONG)  # 开仓没带移动止损
    await h.protect()
    result = await h.adapter.tighten_stop(
        "pos-1", reason="r", buffer_bps=5.0, mark_gap_bps=10.0, profit_mode="trail"
    )
    assert result["action"] == "tightened" and result["mode"] == "breakeven"
    assert result["fallback"] == "no_trailing_config_breakeven"
    live = [(x.purpose, x.trigger_price) for x in h.stop_legs() if x.state == "confirmed"]
    assert live == [("tightened", _target(Side.LONG))]


async def test_trail_anchor_book_spike_is_evidence_failure(tmp_path: Path) -> None:
    h = TrailHarness(tmp_path, Side.LONG)
    await h.protect()
    h.spread = 0.0

    async def spiky(symbol: str) -> ExecutionBook:
        return ExecutionBook(symbol, h.mark * 1.01, h.mark * 1.011, START, START)

    h.exchange.execution_book = spiky  # type: ignore[attr-defined]
    result = await h.trail()
    assert result["action"] == "close" and result["why"] == "evidence_unavailable"
    assert h.state().opposite_anchor_price is None
    assert h.state().trailing_last_error is None  # 不记成移动止损自己的错误
    assert [a.level for a in h.critical()] == [c.AlertLevel.CRITICAL]


def test_panel_shows_trail_anchor() -> None:
    raw = {
        **_protection_raw(),
        "tightened_stop_price": None,
        "opposite_anchor_price": 68_200.0,
        "trailing_distance_pct": 1.2,
    }
    view = parse_protection(raw, START, 120)
    assert view is not None and view.opposite_anchor_price == 68_200.0
    assert view.trailing_distance_pct == 1.2
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    helpers = source[source.index("const TRAIL_TIP =") : source.index("const cardPos =")]
    p = {"symbol": "BTCUSDT", "side": "long", "protection": json.loads(view.model_dump_json())}
    script = (
        "const assert = require('node:assert/strict');\n"
        "const esc = v => String(v); const price = v => v == null ? '—' : String(v);\n"
        "const signed = (v, d) => (v >= 0 ? '+' : '') + v.toFixed(d); const cls = () => '';\n"
        "const TZ = 'Asia/Bangkok'; const qtyFmt = v => String(v);\n"
        "const dur = ms => Math.round(ms / 60000) + ' 分';\n"
        + helpers
        + "\nconst p = "
        + json.dumps(p)
        + ";\nconst block = protBlock(p), brief = protBrief(p);\n"
        "assert(block.includes('反向作战卡锚定移动止损：锚定价 68200'), block);\n"
        "assert(block.includes('1.20%'), block);\n"
        "assert(brief.includes('反向卡锚定移动止损 68200'), brief);\n"
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_anchor_keeps_tighter_active_trailing_stop(tmp_path: Path, side: Side):
    h = TrailHarness(tmp_path, side)
    await h.protect()
    prior = 99.9 if side is Side.LONG else 100.1  # 移动止损已激活且比锚定算出的 99.3 / 100.7 紧
    h.manager._save(
        "pos-1",
        trailing_activated=True,
        trailing_peak_net_exact="0.005",
        trailing_peak_net=0.005,
        trailing_stop_price=prior,
    )
    assert await h.manager.sync("pos-1") == "armed"
    assert h.live() == [("trailing", prior)]
    creates = list(h.creates())
    result = await h.trail()
    assert result["anchored_stop"] == prior and h.state().trailing_stop_price == prior
    assert h.live() == [("trailing", prior)] and h.creates() == creates


async def test_manager_anchor_is_one_shot(tmp_path: Path) -> None:
    h = TrailHarness(tmp_path, Side.LONG)
    await h.protect()
    basis = await h.exchange.entry_basis("BTCUSDT", "entry-1")  # type: ignore[attr-defined]
    async with h.registry.symbol_lock("BTCUSDT"):
        first = await h.manager.anchor_trailing(
            "pos-1",
            basis,
            h.exchange.execution_book,
            distance_mult=1.0,
            reason="a",  # type: ignore[attr-defined]
        )

        async def no_book(symbol: str) -> ExecutionBook:
            raise AssertionError("已锚定不再读盘口")

        h.mark = 101.0
        again = await h.manager.anchor_trailing(
            "pos-1", basis, no_book, distance_mult=0.5, reason="b", card_key="k2"
        )
    assert first == again == 99.3
    assert h.state().opposite_anchor_price == 100.5 and h.state().tightened_reason == "a"


# ---------------------------------------------------------------- 逻辑评审第二轮


async def test_trail_anchor_spike_retries_then_closes(tmp_path: Path) -> None:
    h = TrailHarness(tmp_path, Side.LONG)
    await h.protect()

    async def spiky(symbol: str) -> ExecutionBook:
        return ExecutionBook(symbol, h.mark * 1.01, h.mark * 1.011, h.book_at, h.book_at)

    h.exchange.execution_book = spiky  # type: ignore[attr-defined]
    first = await h.trail(retry_s=30.0)
    assert first["action"] == "pending" and first["why"] == "anchor_book_spike_retry"
    assert h.state().opposite_anchor_retry_since == START and h.critical() == []
    assert h.state().opposite_anchor_price is None
    h.restart_adapter()  # 起始时刻落盘：重启不重新计时
    h.exchange.execution_book = spiky  # type: ignore[attr-defined]
    h.at(START + timedelta(seconds=20))
    again = await h.trail(retry_s=30.0)
    assert again["action"] == "pending" and again["waited_s"] == 20.0 and h.critical() == []
    h.at(START + timedelta(seconds=31))
    last = await h.trail(retry_s=30.0)
    assert last["action"] == "close" and last["why"] == "evidence_unavailable"
    assert [a.level for a in h.critical()] == [c.AlertLevel.CRITICAL]


async def test_trail_anchor_spike_recovers_and_clears_retry(tmp_path: Path) -> None:
    h = TrailHarness(tmp_path, Side.LONG)
    await h.protect()
    good = h.exchange.execution_book  # type: ignore[attr-defined]

    async def spiky(symbol: str) -> ExecutionBook:
        return ExecutionBook(symbol, h.mark * 1.01, h.mark * 1.011, h.book_at, h.book_at)

    h.exchange.execution_book = spiky  # type: ignore[attr-defined]
    assert (await h.trail(retry_s=30.0))["action"] == "pending"
    h.exchange.execution_book = good  # type: ignore[attr-defined]
    h.at(START + timedelta(seconds=10))
    result = await h.trail(retry_s=30.0)
    assert result["action"] == "tightened" and h.state().opposite_anchor_price == 100.5
    assert h.state().opposite_anchor_retry_since is None


def test_keeper_confirmation_baseline_survives_restart(tmp_path: Path, now: datetime) -> None:
    cfg = TIGHT.model_copy(
        update={"opposite_confirm_loss_r": 0.0, "opposite_confirm_key_level": False}
    )
    path = tmp_path / "keeper_opposite.json"
    k, _, _, price = make_keeper(now, cfg, [68000.0], state=path)
    assert k.review_card_rule(position(now), now).action == "hold"  # type: ignore[union-attr]
    later = now + timedelta(minutes=5)
    price[0] = 67919.0  # 离 68000 走了 1.01 ATR；重启后若从这里重量就不够
    k2, rec2, _, _ = make_keeper(now, cfg, price, state=path)
    v = k2.review_card_rule(position(now), later)
    assert v is not None and v.action == "close"
    rule = rec2.rows[-1]["output"]["card_rule"]
    assert rule["ref_price"] == 68000.0 and rule["ref_at"] == now.isoformat()
    # 新规则组的反事实也用重启前第一次碰到反向卡的时间和价
    k3, rec3, _, _ = make_keeper(now, TIGHT, [68010.0], state=path)
    k3.review_card_rule(position(now), later)
    cf = rec3.rows[-1]["output"]["card_rule"]["counterfactual"]
    assert cf == {
        "legacy_action": "close",
        "would_close_at": now.isoformat(),
        "would_close_price": 68000.0,
    }
    k3.forget("pos-1")
    k4, rec4, _, _ = make_keeper(now, cfg, price, state=path)
    k4.review_card_rule(position(now), later)
    assert rec4.rows[-1]["output"]["card_rule"]["ref_price"] == 67919.0  # 平掉后清掉


def test_unused_rule_ledger_loads_in_pre_314_model(tmp_path: Path) -> None:
    from pydantic import ConfigDict, create_model

    from jiaoyi.execution.registry import OPPOSITE_CARD_FIELDS, ServerProtection

    old_fields = {
        name: (field.annotation, field)
        for name, field in ServerProtection.model_fields.items()
        if name not in OPPOSITE_CARD_FIELDS
    }
    Old = create_model(
        "PreOppositeServerProtection", __config__=ConfigDict(extra="forbid"), **old_fields
    )
    h = TightenHarness(tmp_path, Side.LONG)
    h.registry.put(
        h.registry.get("pos-1").model_copy(
            update={
                "server_protection": h.state().model_copy(
                    update={"trailing_stop_price": 99.5, "trailing_activated": True}
                )
            }
        )
    )
    raw = json.loads(h.path.read_text())["positions"]["pos-1"]["server_protection"]
    assert not set(OPPOSITE_CARD_FIELDS) & set(raw)
    old = Old.model_validate(raw)
    assert old.model_dump_json() == h.state().model_dump_json()  # 其余输出一字不差


async def test_rollback_script_makes_used_ledger_loadable_by_pre_314_model(tmp_path: Path) -> None:
    import importlib.util

    from pydantic import ConfigDict, create_model

    from jiaoyi.execution.registry import OPPOSITE_CARD_FIELDS, ServerProtection

    spec = importlib.util.spec_from_file_location(
        "opposite_card_rollback", ROOT / "scripts" / "opposite_card_rollback.py"
    )
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert tuple(script.FIELDS) == OPPOSITE_CARD_FIELDS
    h = TightenHarness(tmp_path, Side.LONG)
    await h.protect()
    await h.tighten()  # 收紧腿 purpose = tightened，6 个新字段有值
    tracked = h.registry.get("pos-1")
    closed = tracked.record.model_copy(update={"close_reason": CloseReason.TIGHTENED_STOP})
    h.registry.put(tracked.model_copy(update={"record": closed}))
    raw = json.loads(h.path.read_text())
    assert script.main([str(h.path)]) == 0  # 默认只列出，不改
    assert json.loads(h.path.read_text()) == raw
    assert script.main([str(h.path), "--write"]) == 0
    assert (tmp_path / "positions.json.bak-314").is_file()
    cleaned = json.loads(h.path.read_text())["positions"]["pos-1"]
    old_fields = {
        name: (field.annotation, field)
        for name, field in ServerProtection.model_fields.items()
        if name not in OPPOSITE_CARD_FIELDS
    }
    Old = create_model(
        "PreOppositeServerProtection", __config__=ConfigDict(extra="forbid"), **old_fields
    )
    Old.model_validate(cleaned["server_protection"])
    assert {leg["purpose"] for leg in cleaned["server_protection"]["legs"]} <= {
        "fixed",
        "trailing",
        "lifeboat",
    }
    assert cleaned["record"]["close_reason"] == "stop_loss"
    assert script.clean(json.loads(h.path.read_text())) == []
