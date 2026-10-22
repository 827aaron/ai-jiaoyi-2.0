"""执行层：开仓 → 保命止损 → 平仓 / 自动结束，以及对账员的各种发现。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.contracts import AlertLevel, OrderIntent, Side
from jiaoyi.contracts.position import CloseReason, PositionStatus, ReconcileIssueKind
from jiaoyi.execution.hb_client import HbApiError
from jiaoyi.execution.hb_executor import HbExecutorAdapter, UnsupportedIntent
from jiaoyi.execution.lifeboat import BinanceLifeboat
from jiaoyi.execution.reconciler import ReconcileError, ThreeWayReconciler
from jiaoyi.execution.registry import PositionRegistry
from tests.conftest import load_example
from tests.fakes import FakeExchange, FakeHbApi

START = datetime(2026, 10, 22, 6, 17, tzinfo=UTC)


class Rig:
    def __init__(self, tmp_path: Path) -> None:
        self.t = START
        self.api = FakeHbApi()
        self.ex = FakeExchange()
        self.alerts = StubAlerter()
        self.registry = PositionRegistry(tmp_path / "positions.json")
        self.lifeboat = BinanceLifeboat(self.ex, clock=self.clock)
        self.adapter = self._adapter()
        self.rec = ThreeWayReconciler(
            self.adapter,
            self.api,
            self.ex,
            self.lifeboat,
            self.registry,
            self.alerts,
            account_name="acct",
        )

    def clock(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)

    def _adapter(self) -> HbExecutorAdapter:
        return HbExecutorAdapter(
            self.api,
            self.lifeboat,
            self.registry,
            self.alerts,
            account_name="acct",
            connector_name="binance_perpetual_testnet",
            fill_timeout_s=5,
            poll_s=1,
            clock=self.clock,
            sleep=self.sleep,
        )


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def intent(**over: object) -> OrderIntent:
    data = load_example("order_intent") | over
    return OrderIntent.model_validate(data)


async def open_filled(rig: Rig, price: float = 68044.0, **over: object) -> str:
    """开一笔并让 fake 立刻成交，返回 position_id。"""
    rig.api.auto_fill = price  # type: ignore[attr-defined]
    original = rig.api.create_executor

    async def create_and_fill(cfg: dict, acct: str) -> dict:  # type: ignore[type-arg]
        out = await original(cfg, acct)
        rig.api.fill(out["executor_id"], price)
        return out

    rig.api.create_executor = create_and_fill  # type: ignore[method-assign]
    rec = await rig.adapter.open(intent(**over))
    rig.ex.set_position(rec.symbol, rec.side, rec.quantity)
    return rec.position_id


# ---------------------------------------------------------------- 开仓


async def test_open_builds_executor_and_places_lifeboat(rig: Rig) -> None:
    pid = await open_filled(rig)
    cfg = rig.api.created[0]
    assert cfg["type"] == "position_executor"
    assert cfg["trading_pair"] == "BTC-USDT"
    assert cfg["side"] == "BUY"
    assert cfg["amount"] == "0.015"
    assert cfg["triple_barrier_config"]["stop_loss"] == "0.0025"
    assert cfg["triple_barrier_config"]["take_profit"] == "0.005"
    assert cfg["triple_barrier_config"]["time_limit"] == 3600

    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.OPEN
    assert rec.entry_price == 68044.0
    assert rec.stop_loss_price == pytest.approx(67873.89)
    assert rec.take_profit_price == pytest.approx(68384.22)
    # 保命止损：多单挂在入场价下方 0.375%，方向卖出，只减仓单的 clientOrderId 带前缀
    symbol, side, qty, price, cid = rig.ex.placed[0]
    assert (symbol, side, qty) == ("BTCUSDT", "sell", 0.015)
    assert price == pytest.approx(68044.0 * (1 - 0.00375))
    assert cid == f"lb-{pid}"
    assert rec.lifeboat is not None and rec.lifeboat.status == "active"


async def test_short_lifeboat_is_above_entry(rig: Rig) -> None:
    await open_filled(rig, side="short")
    _, side, _, price, _ = rig.ex.placed[0]
    assert side == "buy"
    assert price > 68044.0


async def test_open_is_idempotent_per_intent(rig: Rig) -> None:
    first = await open_filled(rig)
    again = await rig.adapter.open(intent())
    assert again.position_id == first
    assert len(rig.api.created) == 1


async def test_trailing_stop_is_refused(rig: Rig) -> None:
    with pytest.raises(UnsupportedIntent):
        await rig.adapter.open(intent(trailing_stop_pct=0.2))
    assert rig.api.created == []


async def test_limit_order_not_filled_yet_has_no_lifeboat_until_fill(rig: Rig) -> None:
    rec = await rig.adapter.open(intent(order_type="limit", limit_price=68000.0))
    assert rec.status is PositionStatus.OPENING
    assert rig.api.created[0]["triple_barrier_config"]["open_order_type"] == 2
    assert rig.api.created[0]["entry_price"] == "68000.0"
    assert rig.ex.placed == []
    # 之后成交：任何一次 positions() 同步都会补挂保命止损
    rig.api.fill("ex1", 68000.0)
    (pos,) = await rig.adapter.positions()
    assert pos.status is PositionStatus.OPEN and pos.lifeboat is not None
    assert len(rig.ex.placed) == 1


async def test_lifeboat_failure_on_open_flattens_position(rig: Rig) -> None:
    rig.ex.fail_place = True
    pid = await open_filled(rig)
    assert rig.api.stopped == [("ex1", False)]
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.CLOSED
    assert rec.close_reason is CloseReason.RECONCILER
    titles = [a.title for a in rig.alerts.sent]
    assert any("保命止损挂不上" in t for t in titles)
    assert all(a.level is AlertLevel.CRITICAL for a in rig.alerts.sent[:1])


async def test_create_executor_failure_marks_failed_and_alerts(rig: Rig) -> None:
    rig.api.down = True
    with pytest.raises(HbApiError):
        await rig.adapter.open(intent())
    (tracked,) = rig.registry.data.positions.values()
    assert tracked.record.status is PositionStatus.FAILED
    assert rig.alerts.sent[0].level is AlertLevel.CRITICAL


# ---------------------------------------------------------------- 平仓与自动结束


async def test_close_stops_executor_then_cancels_lifeboat(rig: Rig) -> None:
    pid = await open_filled(rig)
    await rig.adapter.close(pid, CloseReason.KEEPER)
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rig.api.stopped == [("ex1", False)]
    assert rec.status is PositionStatus.CLOSED
    assert rec.close_reason is CloseReason.KEEPER
    assert rig.ex.stops == {}
    assert await rig.adapter.positions() == []


async def test_lifeboat_stays_until_executor_terminates(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.api.auto_terminate_on_stop = False
    await rig.adapter.close(pid, CloseReason.MANUAL)
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.CLOSING
    assert len(rig.ex.stops) == 1  # 平仓没确认之前不撤
    assert any("迟迟没结束" in a.title for a in rig.alerts.sent)
    rig.api.terminate("ex1", "EARLY_STOP")
    await rig.adapter.positions()
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED  # type: ignore[union-attr]
    assert rig.ex.stops == {}


@pytest.mark.parametrize(
    ("close_type", "reason"),
    [
        ("TAKE_PROFIT", CloseReason.TAKE_PROFIT),
        ("STOP_LOSS", CloseReason.STOP_LOSS),
        ("TIME_LIMIT", CloseReason.TIME_LIMIT),
        ("TRAILING_STOP", CloseReason.TRAILING_STOP),
    ],
)
async def test_executor_ends_by_itself(rig: Rig, close_type: str, reason: CloseReason) -> None:
    pid = await open_filled(rig)
    rig.api.executors["ex1"]["cum_fees_quote"] = 0.51
    rig.api.terminate("ex1", close_type, pnl=1.23)
    assert await rig.adapter.positions() == []
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.CLOSED
    assert rec.close_reason is reason
    assert rec.realized_pnl_usdt == 1.23 and rec.fees_usdt == 0.51
    assert rig.ex.stops == {}  # 平仓后保命止损被撤掉


async def test_cancel_failure_after_close_only_warns(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.ex.fail_cancel = True
    rig.api.terminate("ex1", "STOP_LOSS")
    await rig.adapter.positions()
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED  # type: ignore[union-attr]
    assert rig.alerts.sent[-1].level is AlertLevel.WARN


async def test_registry_survives_restart(rig: Rig, tmp_path: Path) -> None:
    pid = await open_filled(rig)
    reopened = PositionRegistry(tmp_path / "positions.json")
    assert reopened.get(pid) is not None
    assert reopened.by_intent("int-0001") is not None


# ---------------------------------------------------------------- 账户


async def test_account_includes_unrealized_and_day_start(rig: Rig) -> None:
    await open_filled(rig)
    rig.api.pos = [{"unrealized_pnl": 20.0}, {"unrealized_pnl": -5.0}]
    acc = await rig.adapter.account()
    assert acc.equity_usdt == pytest.approx(1015.0)
    assert acc.available_usdt == 900.0
    assert acc.day_start_equity_usdt == pytest.approx(1015.0)
    assert acc.open_positions == 1
    assert acc.open_notional_usdt == pytest.approx(68044.0 * 0.015)
    # 同一天内起始权益不变；曼谷 00:00（UTC 17:00）后重置
    rig.api.balances = [{"token": "USDT", "units": 900.0, "available_units": 900.0}]
    rig.api.pos = []
    assert (await rig.adapter.account()).day_start_equity_usdt == pytest.approx(1015.0)
    rig.t = datetime(2026, 10, 22, 17, 5, tzinfo=UTC)
    assert (await rig.adapter.account()).day_start_equity_usdt == pytest.approx(900.0)


async def test_account_fails_closed_without_balance(rig: Rig) -> None:
    rig.api.balances = []
    with pytest.raises(HbApiError):
        await rig.adapter.account()
    rig.api.balances = [{"token": "USDT", "units": 0.0, "available_units": 0.0}]
    with pytest.raises(HbApiError):
        await rig.adapter.account()


# ---------------------------------------------------------------- 对账员


async def test_clean_reconcile(rig: Rig) -> None:
    await open_filled(rig)
    report = await rig.rec.reconcile(rig.t)
    assert report.clean and report.checked_positions == 1
    assert rig.alerts.sent == []


async def test_missing_lifeboat_is_replaced_and_alerted(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.ex.stops.clear()  # 有人手动撤了 / 币安丢了
    report = await rig.rec.reconcile(rig.t)
    (issue,) = report.issues
    assert issue.kind is ReconcileIssueKind.LIFEBOAT_MISSING and issue.fixed
    assert len(rig.ex.stops) == 1
    assert rig.registry.get(pid).record.lifeboat is not None  # type: ignore[union-attr]
    assert rig.alerts.sent[0].level is AlertLevel.CRITICAL
    assert rig.alerts.sent[0].dedup_key == f"lifeboat_missing:{pid}"
    assert (await rig.rec.reconcile(rig.t)).clean  # 补好后下一轮干净


async def test_missing_lifeboat_unfixable(rig: Rig) -> None:
    await open_filled(rig)
    rig.ex.stops.clear()
    rig.ex.fail_place = True
    report = await rig.rec.reconcile(rig.t)
    assert not report.issues[0].fixed
    assert rig.alerts.sent[0].level is AlertLevel.CRITICAL


async def test_orphan_exchange_position_is_reported_not_touched(rig: Rig) -> None:
    rig.ex.set_position("ETHUSDT", Side.LONG, 2.0)
    report = await rig.rec.reconcile(rig.t)
    (issue,) = report.issues
    assert issue.kind is ReconcileIssueKind.ORPHAN_POSITION and not issue.fixed
    assert rig.api.stopped == [] and rig.ex.cancelled == []


async def test_stale_lifeboat_cancelled_when_flat(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.api.terminate("ex1", "STOP_LOSS")
    rig.ex.fail_cancel = True  # 同步时撤失败，留下残留
    await rig.adapter.positions()
    rig.ex.fail_cancel = False
    rig.ex.set_position("BTCUSDT", Side.LONG, 0)
    report = await rig.rec.reconcile(rig.t)
    (issue,) = report.issues
    assert issue.kind is ReconcileIssueKind.STALE_LIFEBOAT and issue.fixed
    assert rig.ex.stops == {}
    assert pid  # 账本里这笔已 CLOSED


async def test_stale_lifeboat_kept_when_position_still_open(rig: Rig) -> None:
    await open_filled(rig)
    rig.api.terminate("ex1", "STOP_LOSS")
    rig.ex.fail_cancel = True
    await rig.adapter.positions()
    rig.ex.fail_cancel = False
    # 账本说平了，币安还有仓位：绝不撤保命止损
    report = await rig.rec.reconcile(rig.t)
    kinds = {i.kind for i in report.issues}
    assert ReconcileIssueKind.STALE_LIFEBOAT in kinds
    assert ReconcileIssueKind.ORPHAN_POSITION in kinds
    assert len(rig.ex.stops) == 1


async def test_quantity_mismatch(rig: Rig) -> None:
    await open_filled(rig)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.010)
    report = await rig.rec.reconcile(rig.t)
    assert [i.kind for i in report.issues] == [ReconcileIssueKind.QTY_MISMATCH]


async def test_executor_silent_retry_surfaces_as_alert(rig: Rig) -> None:
    await open_filled(rig)
    rig.api.executors["ex1"].update(error_count=7, last_error="Binance -2019 margin insufficient")
    report = await rig.rec.reconcile(rig.t)
    (issue,) = report.issues
    assert issue.kind is ReconcileIssueKind.EXECUTOR_FAILED
    assert "margin" in issue.detail
    assert rig.alerts.sent[0].level is AlertLevel.WARN


async def test_unknown_running_executor_is_orphan(rig: Rig) -> None:
    await rig.api.create_executor({"trading_pair": "SOL-USDT"}, "acct")
    report = await rig.rec.reconcile(rig.t)
    assert report.issues[0].kind is ReconcileIssueKind.ORPHAN_POSITION


async def test_executor_vanished_is_reported(rig: Rig) -> None:
    await open_filled(rig)
    del rig.api.executors["ex1"]
    report = await rig.rec.reconcile(rig.t)
    assert ReconcileIssueKind.EXECUTOR_FAILED in {i.kind for i in report.issues}


@pytest.mark.parametrize("which", ["api", "exchange"])
async def test_unreachable_raises_and_alerts_critical(rig: Rig, which: str) -> None:
    if which == "api":
        rig.api.down = True
    else:
        rig.ex.down = True
    with pytest.raises(ReconcileError):
        await rig.rec.reconcile(rig.t)
    assert rig.alerts.sent[0].level is AlertLevel.CRITICAL
