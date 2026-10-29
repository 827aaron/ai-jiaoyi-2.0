"""托管保护的挂腿顺序（protection_leg_order）；只有合成订单，不连接任何服务。

stop_first：止损 → 保命单。止损确认后保命单挂失败只差后备，交给未托管宽限重试；
止损失败和 lifeboat_first（默认）照旧。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Literal

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.brackets import BracketManager, ProtectionIncomplete
from jiaoyi.execution.exchange import BracketSubmissionRejected
from tests.test_execution import START
from tests.test_protection_readback_grace import GraceHarness

TITLE = "普通托管保护未完整确认"
Fail = Literal["reject", "unknown", "immediate"] | None


class OrderHarness(GraceHarness):
    """回读宽限 0；``lifeboat_fail`` / ``stop_fail`` 让下一次保命单 / 固定止损提交失败。"""

    def __init__(self, root: Path, side: Side, *, order: str | None, prearm: float = 60.0) -> None:
        self.order, self.prearm = order, prearm
        super().__init__(root, side, grace=0.0, prefix="none-")
        self.lifeboat_fail: Fail = None
        self.stop_fail: Fail = None
        place_lifeboat, place_bracket = self.exchange.place_lifeboat, self.exchange.place_bracket

        async def failing(mode: Fail, cid: str, place):
            if mode in {"reject", "immediate"}:
                self.exchange.events.append(("create", cid))
                raise BracketSubmissionRejected(
                    "synthetic definite rejection", immediate_trigger=mode == "immediate"
                )
            self.exchange.unknown_submit = mode == "unknown"
            try:
                return await place()
            finally:
                self.exchange.unknown_submit = False

        async def lifeboat(symbol, close_side, quantity, trigger_price, cid):
            return await failing(
                self.lifeboat_fail,
                cid,
                lambda: place_lifeboat(symbol, close_side, quantity, trigger_price, cid),
            )

        async def bracket(symbol, close_side, quantity, trigger_price, cid, *, kind, working_type):
            return await failing(
                self.stop_fail if kind == "stop_loss" else None,
                cid,
                lambda: place_bracket(
                    symbol,
                    close_side,
                    quantity,
                    trigger_price,
                    cid,
                    kind=kind,
                    working_type=working_type,
                ),
            )

        self.exchange.place_lifeboat = lifeboat
        self.exchange.place_bracket = bracket

    def build(self) -> BracketManager:
        manager = super().build()
        manager.prearm_grace_s = self.prearm
        if self.order is not None:
            manager.leg_order = self.order
        return manager

    def closing(self) -> bool:
        """按对账原因进入直连平仓（腿都已明确时直连当场成交，状态已到 CLOSED）。"""
        rec = self.registry.get("pos-1").record
        return rec.status in {PositionStatus.CLOSING, PositionStatus.CLOSED} and (
            rec.close_reason is CloseReason.RECONCILER
            or self.registry.get("pos-1").requested_close is CloseReason.RECONCILER
        )

    def roles(self) -> list[str]:
        return [c.split("-pos-")[0] for c in self.creates()]

    def alerts(self):
        return [a for a in self.alerter.sent if a.title == TITLE]

    def leg(self, cid: str):
        return next(leg for leg in self.state().legs if leg.client_order_id == cid)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    ("order", "roles"),
    [
        (None, ["lb", "br-sl"]),
        ("lifeboat_first", ["lb", "br-sl"]),
        ("stop_first", ["br-sl", "lb"]),
    ],
)
async def test_new_position_places_legs_in_the_configured_order(
    tmp_path: Path, side: Side, order: str | None, roles: list[str]
):
    h = OrderHarness(tmp_path, side, order=order)
    assert await h.manager.sync("pos-1") == "armed"
    assert h.roles() == roles
    assert {leg.generation for leg in h.state().legs} == {1}
    assert h.state().ever_armed


def test_unknown_leg_order_is_refused(tmp_path: Path):
    h = OrderHarness(tmp_path, Side.LONG, order=None)
    with pytest.raises(ValueError, match="保护腿顺序"):
        BracketManager(
            h.exchange,
            h.registry,
            account_name="synthetic-account",
            connector_name="synthetic-demo",
            leg_order="tp_first",  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_stop_first_rejected_lifeboat_keeps_position_and_retries_with_new_cid(
    tmp_path: Path, side: Side
):
    h = OrderHarness(tmp_path, side, order="stop_first")
    h.lifeboat_fail = "reject"
    await h.adapter.protection_tick()
    assert h.roles() == ["br-sl", "lb"]
    assert not h.closing() and not h.exchange.direct
    assert h.registry.get("pos-1").record.status is PositionStatus.OPEN
    assert h.leg("lb-pos-1-1").state == "rejected"
    assert h.state().stage == "provisional" and not h.state().ever_armed
    assert h.state().prearm_failing_since == START
    (alert,) = h.alerts()
    assert alert.kind == "lifeboat_missing"
    assert "保命单被交易所明确拒绝" in alert.body and "已 0 / 60 秒" in alert.body
    h.now += timedelta(seconds=10)
    await h.adapter.protection_tick()  # 仍被拒：换新 CID 再试，止损不重挂。
    assert h.creates()[2:] == ["lb-pos-1-2"]
    assert not h.closing() and "已 10 / 60 秒" in h.alerts()[-1].body
    h.lifeboat_fail = None
    h.now += timedelta(seconds=10)
    await h.adapter.protection_tick()
    assert h.creates()[3:] == ["lb-pos-1-3"]
    state = h.state()
    assert state.stage == "armed" and state.ever_armed and state.prearm_failing_since is None
    assert not h.closing() and not h.cancels()
    assert h.registry.get("pos-1").record.lifeboat.client_order_id == "lb-pos-1-3"


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_stop_first_unknown_lifeboat_only_rechecks_original_cid_within_grace(
    tmp_path: Path, side: Side
):
    h = OrderHarness(tmp_path, side, order="stop_first")
    h.lifeboat_fail = "unknown"
    await h.adapter.protection_tick()
    h.lifeboat_fail = None
    assert not h.closing() and h.leg("lb-pos-1-1").state == "submit_pending"
    assert h.alerts()[-1].kind == "lifeboat_missing"
    h.restart_adapter()  # 重启后同样只查原 CID。
    h.now += timedelta(seconds=20)
    await h.adapter.protection_tick()
    assert not h.closing() and "已 20 / 60 秒" in h.alerts()[-1].body
    assert "保命单提交结果仍未知" in h.alerts()[-1].body
    h.exchange.hidden.clear()  # 交易所其实收下了。
    await h.adapter.protection_tick()
    assert h.roles() == ["br-sl", "lb"]  # 每个 CID 只提交一次。
    assert h.state().stage == "armed" and not h.closing()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("fail", ["reject", "unknown"])
async def test_stop_first_lifeboat_still_missing_after_grace_closes(
    tmp_path: Path, side: Side, fail: Fail
):
    h = OrderHarness(tmp_path, side, order="stop_first")
    h.lifeboat_fail = fail
    await h.adapter.protection_tick()
    assert not h.closing()
    h.now += timedelta(seconds=60)
    await h.adapter.protection_tick()
    assert h.closing()
    assert "宽限内" not in h.alerts()[-1].body


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("fail", ["reject", "unknown"])
async def test_stop_first_lifeboat_failure_without_grace_closes_as_before(
    tmp_path: Path, side: Side, fail: Fail
):
    h = OrderHarness(tmp_path, side, order="stop_first", prearm=0.0)
    h.lifeboat_fail = fail
    await h.adapter.protection_tick()
    assert h.closing()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_stop_first_lifeboat_immediate_trigger_still_exits(tmp_path: Path, side: Side):
    h = OrderHarness(tmp_path, side, order="stop_first")
    h.lifeboat_fail = "immediate"
    assert await h.manager.sync("pos-1") == "exit"
    assert h.state().stage == "exiting"


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("order", ["lifeboat_first", "stop_first"])
@pytest.mark.parametrize("fail", ["reject", "unknown"])
async def test_first_protective_leg_failure_closes_immediately(
    tmp_path: Path, side: Side, order: str, fail: Fail
):
    h = OrderHarness(tmp_path, side, order=order)
    if order == "stop_first":
        h.stop_fail = fail
    else:
        h.lifeboat_fail = fail
    await h.adapter.protection_tick()
    assert h.closing()
    assert len(h.creates()) == 1
    assert "宽限内" not in h.alerts()[-1].body


def _partial_then_full(h: OrderHarness) -> None:
    h.exchange.entry = replace(h.exchange.entry, filled_quantity=2, status="FILLED")
    h.exchange.held = 2


async def _partial_first_generation(h: OrderHarness) -> None:
    h.exchange.entry = replace(h.exchange.entry, filled_quantity=1, status="PARTIALLY_FILLED")
    h.exchange.held = 1
    assert await h.manager.sync("pos-1") == "provisional"
    _partial_then_full(h)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_lifeboat_first_rejected_lifeboat_is_never_retried(tmp_path: Path, side: Side):
    h = OrderHarness(tmp_path, side, order="lifeboat_first")
    await _partial_first_generation(h)
    h.lifeboat_fail = "reject"
    with pytest.raises(ProtectionIncomplete) as raised:
        await h.manager.sync("pos-1")
    assert not raised.value.backup_only
    h.lifeboat_fail = None
    with pytest.raises(ProtectionIncomplete, match="已有提交或撤销未确认"):
        await h.manager.sync("pos-1")
    assert h.creates()[2:] == ["lb-pos-1-2"]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_stop_first_replacement_generation_retries_rejected_lifeboat(
    tmp_path: Path, side: Side
):
    h = OrderHarness(tmp_path, side, order="stop_first")
    await _partial_first_generation(h)
    h.lifeboat_fail = "reject"
    with pytest.raises(ProtectionIncomplete) as raised:
        await h.manager.sync("pos-1")
    assert raised.value.backup_only
    assert h.creates()[2:] == ["br-sl-pos-1-2", "lb-pos-1-2"]
    assert not h.cancels()  # 新一代没挂齐，旧一代的腿都留着。
    h.lifeboat_fail = None
    assert await h.manager.sync("pos-1") == "armed"
    assert h.creates()[4:] == ["lb-pos-1-3"]
    assert sorted(h.cancels()) == ["br-sl-pos-1-1", "lb-pos-1-1"]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_restart_with_stop_placed_resumes_only_the_planned_lifeboat(
    tmp_path: Path, side: Side
):
    h = OrderHarness(tmp_path, side, order="stop_first")

    class Crash(BaseException):
        pass

    async def crash(*args, **kwargs):
        raise Crash

    real = h.exchange.place_lifeboat
    h.exchange.place_lifeboat = crash
    with pytest.raises(Crash):
        await h.manager.sync("pos-1")
    h.exchange.place_lifeboat = real
    # 进程在保命单写成 submit_pending 之前退出：账本里它还是 planned。
    tracked = h.registry.get("pos-1")
    state = tracked.server_protection
    legs = [
        leg.model_copy(update={"state": "planned"}) if leg.purpose == "lifeboat" else leg
        for leg in state.legs
    ]
    h.registry.put(
        tracked.model_copy(update={"server_protection": state.model_copy(update={"legs": legs})})
    )
    h.restart()
    assert await h.manager.sync("pos-1") == "armed"
    assert h.roles() == ["br-sl", "lb"]
    assert h.creates()[1] == "lb-pos-1-2"
    assert h.leg("lb-pos-1-1").state == "superseded"


@pytest.mark.parametrize("order", ["lifeboat_first", "stop_first"])
async def test_backup_only_failure_needs_stop_first_and_a_confirmed_stop(
    tmp_path: Path, order: str
):
    h = OrderHarness(tmp_path, Side.LONG, order="stop_first")
    h.lifeboat_fail = "reject"
    with pytest.raises(ProtectionIncomplete) as raised:
        await h.manager.sync("pos-1")
    error = raised.value
    assert error.backup_only
    h.manager.leg_order = order
    held = h.manager.prearm_grace("pos-1", error)
    assert (held == 0) if order == "stop_first" else held is None
    h.manager.leg_order = "stop_first"
    tracked = h.registry.get("pos-1")
    state = tracked.server_protection
    legs = [
        leg.model_copy(update={"algo_status": "CANCELED", "state": "cancelled"})
        if leg.client_order_id.startswith("br-sl-")
        else leg
        for leg in state.legs
    ]
    h.registry.put(
        tracked.model_copy(update={"server_protection": state.model_copy(update={"legs": legs})})
    )
    assert h.manager.prearm_grace("pos-1", error) is None  # 止损也不在了：照旧平。


@pytest.mark.parametrize(
    ("order", "fail", "covered"),
    [
        ("stop_first", "reject", True),
        ("lifeboat_first", "reject", False),
        ("stop_first", "stop", False),
    ],
)
async def test_open_check_accepts_missing_lifeboat_only_behind_a_stop_first_confirmed_stop(
    tmp_path: Path, order: str, fail: str, covered: bool
):
    h = OrderHarness(tmp_path, Side.LONG, order="stop_first")
    if fail == "stop":
        h.stop_fail = "reject"
    else:
        h.lifeboat_fail = "reject"
    with pytest.raises(ProtectionIncomplete):
        await h.manager.sync("pos-1")
    h.manager.leg_order = order
    tracked = h.registry.get("pos-1")
    assert tracked.record.lifeboat is None
    assert h.adapter._stop_first_covered(tracked) is covered


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_stop_first_does_not_resend_rejected_lifeboat_once_ever_armed(
    tmp_path: Path, side: Side
):
    h = OrderHarness(tmp_path, side, order="stop_first")
    await _partial_first_generation(h)
    tracked = h.registry.get("pos-1")
    armed = tracked.server_protection.model_copy(update={"ever_armed": True})
    h.registry.put(tracked.model_copy(update={"server_protection": armed}))
    h.lifeboat_fail = "reject"
    with pytest.raises(ProtectionIncomplete):
        await h.manager.sync("pos-1")
    h.lifeboat_fail = None
    with pytest.raises(ProtectionIncomplete, match="已有提交或撤销未确认"):
        await h.manager.sync("pos-1")
    assert h.creates()[2:] == ["br-sl-pos-1-2", "lb-pos-1-2"]
    assert not h.cancels()
