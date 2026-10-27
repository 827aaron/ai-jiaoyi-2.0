"""执行层：开仓 → 保命止损 → 平仓 / 自动结束，以及对账员的各种发现。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.contracts import Alert, AlertLevel, Health, OrderIntent, Side
from jiaoyi.contracts.position import CloseReason, PositionStatus, ReconcileIssueKind
from jiaoyi.execution.exchange import CloseFill, ExchangeError
from jiaoyi.execution.fills import TradeFill
from jiaoyi.execution.hb_client import HbApiError
from jiaoyi.execution.hb_executor import HbExecutorAdapter, SymbolAlreadyHeld
from jiaoyi.execution.lifeboat import BinanceLifeboat
from jiaoyi.execution.reconciler import ReconcileError, ThreeWayReconciler
from jiaoyi.execution.registry import PositionRegistry
from tests.conftest import load_example
from tests.fakes import FakeExchange, FakeHbApi

START = datetime(2026, 10, 22, 6, 17, tzinfo=UTC)


class _Alerts(StubAlerter):
    """notices=False 时丢掉开仓成交 / 平仓完成两类成功消息，老测试只看出问题的告警。"""

    def __init__(self, notices: bool) -> None:
        super().__init__()
        self._notices = notices

    async def send(self, alert: Alert) -> None:
        if self._notices or alert.kind not in ("open_filled", "close_done"):
            await super().send(alert)


class Rig:
    def __init__(
        self,
        tmp_path: Path,
        *,
        notices: bool = False,
        limit_tp: bool = False,
        tick: float | None = None,
    ) -> None:
        self.t = START
        self.limit_tp = limit_tp
        self.tick = tick
        self.notices = notices  # 开平仓成功的 INFO 推送；老测试只看告警，默认关
        self.api = FakeHbApi()
        self.ex = FakeExchange()
        self.api.exchange = self.ex  # 只为已创建的合成 CID 注册入场订单与成交证据。
        self.alerts = _Alerts(notices)
        self.api.on_terminate = self._flatten_exchange
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

    def _flatten_exchange(self, executor_id: str) -> None:
        pair = str(self.api.executors[executor_id]["trading_pair"])
        self.ex.set_position(pair.replace("-", ""), Side.LONG, 0)

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
            income_since=self.ex.income_since,
            exchange=self.ex,
            limit_take_profit=self.limit_tp,
            tick_size=lambda _s: self.tick,
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

    async def create_and_fill(cfg: dict, acct: str, marker: str) -> dict:  # type: ignore[type-arg]
        out = await original(cfg, acct, marker)
        rig.api.fill(out["executor_id"], price)
        return out

    rig.api.create_executor = create_and_fill  # type: ignore[method-assign]
    rec = await rig.adapter.open(intent(**over))
    return rec.position_id


# ---------------------------------------------------------------- 开仓


async def test_open_builds_executor_and_places_lifeboat(rig: Rig) -> None:
    pid = await open_filled(rig)
    cfg = rig.api.created[0]
    assert cfg["type"] == "position_executor"
    assert cfg["trading_pair"] == "BTC-USDT"
    assert cfg["side"] == 1  # hummingbot TradeType.BUY
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


async def test_stop_and_take_profit_compensate_open_fee(rig: Rig) -> None:
    """hummingbot 按扣了开仓手续费的净盈亏触发：止损加 5bp、止盈减 5bp，价格距离才等于风控算的。"""
    rig.adapter = HbExecutorAdapter(
        rig.api, rig.lifeboat, rig.registry, rig.alerts,
        account_name="acct", connector_name="binance_perpetual_testnet",
        fill_timeout_s=5, poll_s=1, clock=rig.clock, sleep=rig.sleep,
        income_since=rig.ex.income_since, exchange=rig.ex, open_fee_bps=5.0,
    )  # fmt: skip
    pid = await open_filled(rig)
    barrier = rig.api.created[0]["triple_barrier_config"]
    assert barrier["stop_loss"] == "0.003" and barrier["take_profit"] == "0.0045"
    # 账本、保命止损仍按价格距离算，不受补偿影响
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.stop_loss_price == pytest.approx(67873.89)
    assert rig.ex.placed[0][3] == pytest.approx(68044.0 * (1 - 0.00375))


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


async def test_trailing_stop_maps_to_hummingbot_trailing_stop(rig: Rig) -> None:
    pid = await open_filled(rig, trailing_stop_pct=0.1, trailing_activation_pct=0.3)
    barrier = rig.api.created[0]["triple_barrier_config"]
    assert barrier["trailing_stop"] == {"activation_price": "0.003", "trailing_delta": "0.001"}
    # 账本也记一份，面板画启动线 / 移动止损线用
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert (rec.trailing_activation_pct, rec.trailing_stop_pct) == (0.3, 0.1)
    pid2 = await open_filled(rig, intent_id="int-0002", symbol="ETHUSDT")
    assert "trailing_stop" not in rig.api.created[1]["triple_barrier_config"]
    assert rig.registry.get(pid2).record.trailing_stop_pct is None  # type: ignore[union-attr]


async def test_take_profit_order_type_follows_switch(tmp_path: Path) -> None:
    market = Rig(tmp_path / "m")
    await open_filled(market)
    barrier = market.api.created[0]["triple_barrier_config"]
    assert barrier["take_profit_order_type"] == 1  # 默认市价止盈
    limit = Rig(tmp_path / "l", limit_tp=True)
    await open_filled(limit)
    lb = limit.api.created[0]["triple_barrier_config"]
    assert lb["take_profit_order_type"] == 2  # 只减仓限价止盈
    # 限价止盈按入场价 × 止盈% 挂，不减开仓手续费；止损照样补手续费
    assert lb["stop_loss"] == barrier["stop_loss"]
    assert float(lb["take_profit"]) >= float(barrier["take_profit"])


def _hb_limit(entry: float, side: Side, take: float, tick: float) -> float:
    """hummingbot 挂限价止盈的价：入场价 × (1 ± take)，按 tick 往下取整。"""
    from decimal import ROUND_FLOOR, Decimal

    raw = Decimal(str(entry)) * (1 + (1 if side is Side.LONG else -1) * Decimal(str(take)))
    q = Decimal(str(tick))
    return float((raw / q).to_integral_value(ROUND_FLOOR) * q)


async def test_limit_tp_short_rounds_toward_fill(tmp_path: Path) -> None:
    # DASH 2026-10-24：空单止盈 50.6677 被 hummingbot 往下取整挂成 50.66，最低到 50.67 没成交
    r = Rig(tmp_path, limit_tp=True, tick=0.1)
    pid = await open_filled(r, price=68044.0, side="short")
    take = float(r.api.created[0]["triple_barrier_config"]["take_profit"])
    rec = r.registry.get(pid).record  # type: ignore[union-attr]
    target = 68044.0 * (1 - 0.005)  # 67703.78
    assert rec.take_profit_price == pytest.approx(67703.8)  # 止盈价上方最近一档
    assert _hb_limit(68044.0, Side.SHORT, take, 0.1) == pytest.approx(67703.8)
    assert rec.take_profit_price >= target


async def test_limit_tp_long_already_rounds_toward_fill(tmp_path: Path) -> None:
    r = Rig(tmp_path, limit_tp=True, tick=0.1)
    pid = await open_filled(r, price=68044.0)
    take = float(r.api.created[0]["triple_barrier_config"]["take_profit"])
    assert take == pytest.approx(0.005)  # 多单卖出往下取整本来就离成交近，不调
    rec = r.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.take_profit_price == pytest.approx(68384.2)
    assert _hb_limit(68044.0, Side.LONG, take, 0.1) == pytest.approx(68384.2)


async def test_limit_tp_without_tick_keeps_raw_price(tmp_path: Path) -> None:
    r = Rig(tmp_path, limit_tp=True)
    pid = await open_filled(r, price=68044.0, side="short")
    assert float(r.api.created[0]["triple_barrier_config"]["take_profit"]) == pytest.approx(0.005)
    rec = r.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.take_profit_price == pytest.approx(68044.0 * 0.995)


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


async def test_hb_unreachable_on_open_is_unknown_not_failed(rig: Rig) -> None:
    rig.api.down = True  # 建不了也查不了：结果不明，留给对账员按标记认领
    rec = await rig.adapter.open(intent())
    assert rec.status is PositionStatus.OPENING and rec.executor_id is None
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


async def test_close_reason_follows_what_actually_filled(rig: Rig) -> None:
    """巡逻兵下令平仓，但止盈单先成交：记止盈，不记巡逻兵（Aaron 10-25）；下过令的人另外留着。"""
    pid = await open_filled(rig)

    async def tp_fills_first(executor_id: str, *, keep_position: bool) -> None:
        rig.api.stopped.append((executor_id, keep_position))
        rig.api.terminate(executor_id, "TAKE_PROFIT")

    rig.api.stop_executor = tp_fills_first  # type: ignore[method-assign]
    await rig.adapter.close(pid, CloseReason.PATROL)
    tracked = rig.registry.get(pid)
    assert tracked is not None and tracked.record.close_reason is CloseReason.TAKE_PROFIT
    assert tracked.requested_close is CloseReason.PATROL


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
    rig.ex.income_down = True  # 这条只测“同一天基准不变、曼谷 0 点重置”，不走收入流水倒推
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


async def test_reconcile_round_updates_team_page_activity(tmp_path: Path, rig: Rig) -> None:
    from jiaoyi.contracts import Role
    from jiaoyi.core.recorder import RoleRecorder
    from jiaoyi.core.store import JsonlDecisionLog

    log = JsonlDecisionLog(tmp_path)
    rec = ThreeWayReconciler(
        rig.adapter, rig.api, rig.ex, rig.lifeboat, rig.registry, rig.alerts,
        account_name="acct", recorder=RoleRecorder(Role.RECONCILER, log),
    )  # fmt: skip
    rec.set_next_run(rig.t + timedelta(seconds=30))
    await rec.reconcile(rig.t)
    st = log.read_status()[Role.RECONCILER.value]
    assert st.last_run_at == rig.t and st.summary == "对账：三方一致（在管 0 笔）"
    assert st.next_run_at == rig.t + timedelta(seconds=30)


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


def with_close_unknown(rig: Rig) -> ThreeWayReconciler:
    return ThreeWayReconciler(
        rig.adapter,
        rig.api,
        rig.ex,
        rig.lifeboat,
        rig.registry,
        rig.alerts,
        account_name="acct",
        close_unknown_positions=True,
    )


async def test_close_unknown_switch_flattens_unknown_position(rig: Rig) -> None:
    rig.ex.set_position("ETHUSDT", Side.SHORT, 2.0)
    report = await with_close_unknown(rig).reconcile(rig.t)
    (issue,) = report.issues
    assert issue.kind is ReconcileIssueKind.ORPHAN_POSITION and issue.fixed
    assert rig.ex.market_closed == [("ETHUSDT", "buy", 2.0)]  # 空单用买单平


async def test_close_unknown_switch_stops_unknown_executor(rig: Rig) -> None:
    await rig.api.create_executor({"trading_pair": "SOL-USDT"}, "acct", "someone-else")
    report = await with_close_unknown(rig).reconcile(rig.t)
    assert report.issues[0].fixed
    assert rig.api.stopped == [("ex1", False)]


async def test_close_unknown_switch_failure_stays_unfixed(rig: Rig) -> None:
    rig.ex.set_position("ETHUSDT", Side.LONG, 2.0)
    rig.ex.fail_market_close = True
    (issue,) = (await with_close_unknown(rig).reconcile(rig.t)).issues
    assert not issue.fixed and "平仓失败" in issue.detail


async def test_close_unknown_switch_never_touches_our_own_position(rig: Rig) -> None:
    await open_filled(rig)
    report = await with_close_unknown(rig).reconcile(rig.t)
    assert report.clean and rig.ex.market_closed == [] and rig.api.stopped == []


async def test_close_unknown_defaults_to_alert_only(rig: Rig) -> None:
    from jiaoyi.contracts import AppConfig

    assert AppConfig.model_fields["reconciler_close_unknown_positions"].default is False


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


async def test_executor_terminated_but_position_remains_keeps_ledger_and_lifeboat(
    rig: Rig,
) -> None:
    """hummingbot 重启：执行器被 SYSTEM_CLEANUP 终止、仓位还在。不能记已平，更不能撤保命止损。"""
    pid = await open_filled(rig)
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    await rig.adapter.positions()
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.OPEN and rec.close_reason is None
    assert len(rig.ex.stops) == 1  # 保命止损没被撤
    assert any("执行器已终止但币安仍有仓" in a.title for a in rig.alerts.sent)


async def test_executor_terminated_position_remains_is_flattened_after_two_rounds(
    rig: Rig,
) -> None:
    pid = await open_filled(rig)
    rig.api.auto_terminate_on_stop = False  # 已终止的执行器，停止请求什么也不会做
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    first = await rig.rec.reconcile(rig.t)
    assert any(i.kind is ReconcileIssueKind.EXECUTOR_FAILED and not i.fixed for i in first.issues)
    assert rig.ex.market_closed == [] and len(rig.ex.stops) == 1  # 第 1 轮：止损兜底，不平
    second = await rig.rec.reconcile(rig.t)
    assert any(i.kind is ReconcileIssueKind.EXECUTOR_FAILED and i.fixed for i in second.issues)
    assert rig.ex.market_closed == [("BTCUSDT", "sell", 0.015)]
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED  # type: ignore[union-attr]
    assert rig.ex.stops == {}


async def test_lifeboat_replaced_when_hummingbot_cleanup_cancelled_it(rig: Rig) -> None:
    """重启时 hummingbot 把币安上的挂单一并撤了（保命止损不见了）：对账员补挂。"""
    pid = await open_filled(rig)
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    rig.ex.stops.clear()
    report = await rig.rec.reconcile(rig.t)
    assert len(rig.ex.stops) == 1
    assert pid and any(
        i.kind is ReconcileIssueKind.LIFEBOAT_MISSING and i.fixed for i in report.issues
    )


async def test_close_verifies_exchange_flat_and_falls_back_to_direct(rig: Rig) -> None:
    """停执行器返回成功、执行器也结束了，但币安仓位还在：直连只减仓补平，盈亏记进当日统计。"""
    pid = await open_filled(rig)
    rig.api.auto_terminate_on_stop = False
    rig.ex.exit_price = 68144.0
    rig.ex.exit_fee = 0.02
    rig.ex.trades = [TradeFill(rig.t, "entry", "buy", 68044.0, 0.015, 0.0, 0.0)]
    await rig.adapter.backfill_fills(rig.ex.user_trades)

    async def stop_and_leave(executor_id: str, *, keep_position: bool) -> None:
        rig.api.terminate(executor_id, "EARLY_STOP", leaves_position=True)

    rig.api.stop_executor = stop_and_leave  # type: ignore[method-assign]
    await rig.adapter.close(pid, CloseReason.MANUAL)
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.CLOSED
    assert rig.ex.market_closed == [("BTCUSDT", "sell", 0.015)]
    assert rec.realized_pnl_usdt is None  # identified 回报不含fee，不能把毛额当净额。
    rig.ex.trades.append(TradeFill(rig.t, "exit", "sell", 68144.0, 0.015, 0.02, 1.5))
    assert await rig.adapter.backfill_fills(rig.ex.user_trades) == 1
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.realized_pnl_usdt == pytest.approx(0.015 * (68144.0 - 68044.0) - 0.02)
    assert rec.fees_usdt == pytest.approx(0.02)
    assert rec.lifeboat is not None and rec.lifeboat.status.value == "cancelled"
    assert rig.ex.stops == {}
    assert (await rig.adapter.account()).day_realized_pnl_usdt == pytest.approx(1.48)


async def test_unverifiable_exchange_keeps_position_open(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.api.terminate("ex1", "TAKE_PROFIT")
    rig.ex.down = True  # 币安读不到：不结账
    await rig.adapter.positions()
    assert rig.registry.get(pid).record.status is PositionStatus.OPEN  # type: ignore[union-attr]
    rig.ex.down = False
    await rig.adapter.positions()
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED  # type: ignore[union-attr]


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
    await rig.api.create_executor({"trading_pair": "SOL-USDT"}, "acct", "other")
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


# 审查意见 1：补挂失败要自动平仓


async def test_lifeboat_replace_failing_twice_flattens_via_hummingbot(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.ex.stops.clear()
    rig.ex.fail_place = True
    first = await rig.rec.reconcile(rig.t)
    assert not first.issues[0].fixed and rig.api.stopped == []  # 第 1 轮只告警
    second = await rig.rec.reconcile(rig.t)
    assert second.issues[0].fixed and "已平仓" in second.issues[0].detail
    assert rig.api.stopped == [("ex1", False)]
    assert rig.registry.get(pid).record.close_reason is CloseReason.RECONCILER  # type: ignore[union-attr]
    assert rig.ex.market_closed == []


async def test_immediate_trigger_flattens_in_the_same_round(rig: Rig) -> None:
    await open_filled(rig)
    rig.ex.stops.clear()
    rig.ex.fail_place = ExchangeError(
        "-2021 Order would immediately trigger", immediate_trigger=True
    )
    report = await rig.rec.reconcile(rig.t)
    assert report.issues[0].fixed and "-2021" in report.issues[0].detail
    assert rig.api.stopped == [("ex1", False)]


async def test_flatten_falls_back_to_direct_reduce_only_order(rig: Rig) -> None:
    await open_filled(rig)
    rig.ex.stops.clear()
    rig.ex.fail_place = ExchangeError("-2021", immediate_trigger=True)
    rig.api.executors["ex1"]["is_active"] = True
    rig.api.stop_executor = _definite_stop_rejection  # type: ignore[method-assign]
    report = await rig.rec.reconcile(rig.t)
    # 执行层自己的 close() 在 hummingbot 停不掉时就会直连兜底，对账员看到的是已平仓
    assert report.issues[0].fixed
    assert rig.ex.market_closed == [("BTCUSDT", "sell", 0.015)]
    assert rig.ex.stops == {}
    # 净仓已平，但旧执行器清理未确认：仍归本仓认领，不误当无主或重复平仓。
    kinds = [i.kind for i in (await rig.rec.reconcile(rig.t)).issues]
    assert kinds == []
    tracked = next(iter(rig.registry.data.positions.values()))
    assert tracked.exit_cleanup_pending
    assert rig.ex.market_closed == [("BTCUSDT", "sell", 0.015)]
    with pytest.raises(SymbolAlreadyHeld):
        await rig.adapter.open(intent(intent_id="int-cleanup-still-owned"))


async def _always_down(executor_id: str, *, keep_position: bool) -> None:
    raise HbApiError("hummingbot-api 不可达")


async def _definite_stop_rejection(executor_id: str, *, keep_position: bool) -> None:
    raise HbApiError("合成明确拒绝，停止请求未接收", definite_rejection=True)


async def test_unflattenable_position_alerts_critical_every_round(rig: Rig) -> None:
    await open_filled(rig)
    rig.ex.stops.clear()
    rig.ex.fail_place = ExchangeError("-2021", immediate_trigger=True)
    rig.ex.fail_market_close = True
    rig.api.stop_executor = _always_down  # type: ignore[method-assign]
    for _ in range(3):
        report = await rig.rec.reconcile(rig.t)
        assert not report.issues[0].fixed
        rig.t += timedelta(seconds=30)
    critical = [a for a in rig.alerts.sent if a.level is AlertLevel.CRITICAL]
    assert len(critical) == 3
    assert len({a.dedup_key for a in critical}) == 3  # 不会被冷却去重吞掉


async def test_successful_replace_resets_failure_count(rig: Rig) -> None:
    await open_filled(rig)
    rig.ex.stops.clear()
    rig.ex.fail_place = True
    await rig.rec.reconcile(rig.t)  # 失败 1 次
    rig.ex.fail_place = False
    await rig.rec.reconcile(rig.t)  # 补上了
    rig.ex.stops.clear()
    rig.ex.fail_place = True
    report = await rig.rec.reconcile(rig.t)  # 又丢：重新从 1 次数起
    assert not report.issues[0].fixed and rig.api.stopped == []


async def test_unknown_positions_are_still_only_alerted(rig: Rig) -> None:
    rig.ex.set_position("ETHUSDT", Side.LONG, 2.0)
    rig.ex.fail_place = ExchangeError("-2021", immediate_trigger=True)
    await rig.rec.reconcile(rig.t)
    assert rig.api.stopped == [] and rig.ex.market_closed == []


# 审查意见 2：结果不明的开仓要能认领


async def test_open_writes_claim_marker(rig: Rig) -> None:
    pid = await open_filled(rig)
    assert rig.api.controller_ids == [f"jy-{pid}"]


async def test_create_timeout_but_executor_exists_is_claimed(rig: Rig) -> None:
    rig.api.create_error = HbApiError("超时")  # 执行器其实已建好
    rec = await rig.adapter.open(intent())
    assert rec.executor_id == "ex1"  # 按标记查到，当场认领
    rig.api.fill("ex1", 68044.0)
    (pos,) = await rig.adapter.positions()
    assert pos.status is PositionStatus.OPEN and pos.lifeboat is not None
    assert len(rig.ex.placed) == 1
    assert any("已认领" in a.title for a in rig.alerts.sent)


async def test_definite_rejection_is_failed_without_lookup(rig: Rig) -> None:
    rig.api.create_error_before = HbApiError("400", definite_rejection=True)
    rig.api.find_down = True  # 如果去查就会暴露
    with pytest.raises(HbApiError):
        await rig.adapter.open(intent())
    (tracked,) = rig.registry.data.positions.values()
    assert tracked.record.status is PositionStatus.FAILED


async def test_timeout_and_nothing_found_is_failed(rig: Rig) -> None:
    rig.api.create_error_before = HbApiError("超时")
    with pytest.raises(HbApiError):
        await rig.adapter.open(intent())
    (tracked,) = rig.registry.data.positions.values()
    assert tracked.record.status is PositionStatus.FAILED


async def test_unknown_outcome_is_claimed_later_by_reconciler(rig: Rig) -> None:
    rig.api.create_error = HbApiError("超时")
    rig.api.find_down = True  # 当场也查不了
    rec = await rig.adapter.open(intent())
    assert rec.status is PositionStatus.OPENING and rec.executor_id is None
    assert rig.alerts.sent[-1].level is AlertLevel.CRITICAL
    rig.api.find_down = False
    rig.api.fill("ex1", 68044.0)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.015)
    report = await rig.rec.reconcile(rig.t)
    assert any(i.fixed and "认领" in i.detail for i in report.issues)
    pos = rig.registry.get(rec.position_id).record  # type: ignore[union-attr]
    assert pos.executor_id == "ex1" and pos.status is PositionStatus.OPEN
    assert pos.lifeboat is not None and len(rig.ex.stops) == 1
    assert (await rig.rec.reconcile(rig.t)).clean


async def test_reconciler_reclaims_marked_executor_of_failed_position(rig: Rig) -> None:
    rig.api.create_error_before = HbApiError("超时")
    with pytest.raises(HbApiError):
        await rig.adapter.open(intent())
    (tracked,) = rig.registry.data.positions.values()
    # 之后才发现：其实建好了（请求在 hummingbot 那边慢慢处理完了）
    rig.api.create_error_before = None
    await rig.api.create_executor(
        {"trading_pair": "BTC-USDT"}, "acct", f"jy-{tracked.record.position_id}"
    )
    rig.api.fill("ex1", 68044.0)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.015)
    await rig.rec.reconcile(rig.t)
    pos = rig.registry.get(tracked.record.position_id).record  # type: ignore[union-attr]
    assert pos.status is PositionStatus.OPEN and pos.lifeboat is not None


async def test_unclaimable_open_is_marked_failed_after_grace(rig: Rig) -> None:
    rig.api.create_error_before = HbApiError("超时")
    rig.api.find_down = True  # 当场查不了，留 OPENING
    rec = await rig.adapter.open(intent())
    rig.api.find_down = False
    rig.t += timedelta(seconds=30)
    await rig.rec.reconcile(rig.t)
    assert rig.registry.get(rec.position_id).record.status is PositionStatus.OPENING  # type: ignore[union-attr]
    rig.t += timedelta(seconds=60)
    report = await rig.rec.reconcile(rig.t)
    assert rig.registry.get(rec.position_id).record.status is PositionStatus.FAILED  # type: ignore[union-attr]
    assert any("查不到执行器" in i.detail for i in report.issues)


async def test_failed_stop_keeps_closing_and_protection_for_evidence_recovery(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.api.stop_executor = _always_down  # type: ignore[method-assign]
    rig.ex.fail_market_close = True  # 直连兜底也不通
    with pytest.raises(HbApiError):
        await rig.adapter.close(pid, CloseReason.KEEPER)
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSING  # type: ignore[union-attr]
    assert rig.registry.get(pid).requested_close is CloseReason.KEEPER  # type: ignore[union-attr]
    assert len(rig.ex.stops) == 1 and rig.ex.market_closed == []


# 逻辑审查 B1 / I4 / 总风险字段


async def test_second_position_on_same_symbol_is_refused_before_touching_leverage(
    rig: Rig,
) -> None:
    await open_filled(rig)
    rig.ex.leverage_set.clear()
    for side in ("long", "short"):  # 同向、反向都拒
        with pytest.raises(SymbolAlreadyHeld):
            await rig.adapter.open(intent(intent_id=f"int-x-{side}", side=side))
    assert rig.ex.leverage_set == []  # 没改过杠杆
    assert len(rig.api.created) == 1  # 没建第二个执行器
    assert (await rig.adapter.account()).open_symbols == ["BTCUSDT"]


async def test_symbol_can_be_reopened_after_close(rig: Rig) -> None:
    pid = await open_filled(rig)
    await rig.adapter.close(pid, CloseReason.KEEPER)
    rig.api.executors["ex1"]["is_active"] = False
    await rig.adapter.positions()
    assert (await rig.adapter.account()).open_symbols == []


async def test_close_falls_back_to_direct_reduce_only_when_stop_executor_fails(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    rig.api.stop_executor = _definite_stop_rejection  # type: ignore[method-assign]
    await rig.adapter.close(pid, CloseReason.KEEPER)
    assert rig.ex.market_closed == [("BTCUSDT", "sell", 0.015)]
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED  # type: ignore[union-attr]
    assert any("直连" in a.title for a in rig.alerts.sent)
    assert rig.ex.stops == {}  # 保命止损同时撤掉，不会误平之后同币的新仓


async def test_direct_close_retries_lifeboat_cancel_then_alerts(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    rig.api.stop_executor = _definite_stop_rejection  # type: ignore[method-assign]
    rig.ex.fail_cancel = True
    await rig.adapter.close(pid, CloseReason.KEEPER)
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED  # type: ignore[union-attr]
    assert len(rig.ex.stops) == 1  # 撤不掉：留给对账员清扫
    assert any("撤保命止损失败" in a.title for a in rig.alerts.sent)


async def test_vanished_executor_with_exchange_position_is_flattened_directly(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.api.auto_terminate_on_stop = False
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)  # 启动清理已确认终止
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.015)
    first = await rig.rec.reconcile(rig.t)
    assert rig.ex.market_closed == []  # 第 1 轮只告警
    assert any(not i.fixed for i in first.issues if i.kind is ReconcileIssueKind.EXECUTOR_FAILED)
    report = await rig.rec.reconcile(rig.t)
    issue = next(i for i in report.issues if i.kind is ReconcileIssueKind.EXECUTOR_FAILED)
    assert issue.fixed
    assert rig.ex.market_closed == [("BTCUSDT", "sell", 0.015)]
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED  # type: ignore[union-attr]
    assert rig.ex.stops == {}  # 对账员直连平仓后也撤了保命止损


async def test_account_reports_open_risk_and_day_realized_pnl(rig: Rig) -> None:
    pid = await open_filled(rig)
    acc = await rig.adapter.account()
    # 已成交：数量 × 入场价 × lifeboat_stop_pct / 100
    expected = 0.015 * 68044.0 * intent().lifeboat_stop_pct / 100.0
    assert acc.open_risk_usdt == pytest.approx(expected)
    assert acc.day_realized_pnl_usdt == 0.0
    rig.registry.put(
        rig.registry.get(pid).model_copy(  # type: ignore[union-attr]
            update={
                "record": rig.registry.get(pid).record.model_copy(  # type: ignore[union-attr]
                    update={
                        "status": PositionStatus.CLOSED,
                        "closed_at": rig.t,
                        "realized_pnl_usdt": -12.5,
                    }
                )
            }
        )
    )
    acc = await rig.adapter.account()
    assert acc.open_risk_usdt == 0.0
    assert acc.day_realized_pnl_usdt == pytest.approx(-12.5)


# 审查意见 3：日亏基准对齐 0 点


async def test_late_start_uses_income_to_rebuild_midnight_base(rig: Rig) -> None:
    # 曼谷 0 点 = UTC 17:00；大脑 UTC 06:17 才第一次读账户，凌晨已亏 20
    rig.api.balances = [{"token": "USDT", "units": 980.0, "available_units": 900.0}]
    rig.ex.income = -20.0
    acc = await rig.adapter.account()
    assert acc.day_start_equity_usdt == pytest.approx(1000.0)  # 不是 980
    assert acc.day_pnl_pct == pytest.approx(-2.0)
    assert acc.equity_usdt == pytest.approx(980.0)


async def test_late_start_without_income_takes_stricter_and_warns(rig: Rig) -> None:
    rig.ex.income_down = True
    rig.registry.data.last_equity_usdt = 1030.0  # 上次落盘的权益更高
    rig.api.balances = [{"token": "USDT", "units": 980.0, "available_units": 900.0}]
    acc = await rig.adapter.account()
    assert acc.day_start_equity_usdt == pytest.approx(1030.0)
    assert any("日亏基准" in a.title and a.level is AlertLevel.WARN for a in rig.alerts.sent)


async def test_start_right_after_midnight_uses_current_equity(rig: Rig) -> None:
    rig.t = datetime(2026, 10, 22, 17, 3, tzinfo=UTC)  # 曼谷 00:03
    rig.ex.income = -50.0  # 即使有流水也不该用
    acc = await rig.adapter.account()
    assert acc.day_start_equity_usdt == pytest.approx(1000.0)
    assert rig.alerts.sent == []


async def test_day_base_stays_through_the_day_and_across_restart(rig: Rig, tmp_path: Path) -> None:
    rig.api.balances = [{"token": "USDT", "units": 980.0, "available_units": 900.0}]
    rig.ex.income = -20.0
    await rig.adapter.account()
    rig.api.balances = [{"token": "USDT", "units": 950.0, "available_units": 900.0}]
    rig.ex.income = -50.0
    again = PositionRegistry(tmp_path / "positions.json")  # 重启
    rig.registry = again
    rig.adapter = rig._adapter()
    assert (await rig.adapter.account()).day_start_equity_usdt == pytest.approx(1000.0)


async def test_unfilled_position_counts_toward_exposure(rig: Rig) -> None:
    await rig.adapter.open(intent(order_type="limit", limit_price=68000.0))  # 还没成交
    acc = await rig.adapter.account()
    assert acc.open_positions == 1
    assert acc.open_notional_usdt == pytest.approx(1020.6)
    assert acc.open_notional_by_category == {"major": pytest.approx(1020.6)}
    rig.api.fill("ex1", 68000.0)  # 成交后改按真实入场价
    await rig.adapter.positions()
    acc = await rig.adapter.account()
    assert acc.open_notional_usdt == pytest.approx(68000.0 * 0.015)


async def test_open_sets_leverage_before_creating_executor(rig: Rig) -> None:
    await open_filled(rig)
    assert rig.ex.leverage_set == [("BTCUSDT", intent().leverage)]


async def test_open_refused_when_leverage_cannot_be_set(rig: Rig) -> None:
    rig.ex.leverage_error = "杠杆对不上"
    with pytest.raises(ExchangeError):
        await rig.adapter.open(intent())
    assert rig.api.created == []  # 没建执行器
    statuses = [p.record.status for p in rig.registry.data.positions.values()]
    assert statuses == [PositionStatus.FAILED]


async def test_partial_fill_then_restart_is_not_treated_as_closed(rig: Rig) -> None:
    """限价只成交 40% 后 hummingbot 重启：以币安实际持仓为准，账本数量同步，不当作已平。"""
    pid = await open_filled(rig)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.006)  # 下单 0.015，只成交 0.006
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    await rig.adapter.positions()
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.OPEN
    assert rec.quantity == pytest.approx(0.006)
    assert len(rig.ex.stops) == 1
    rig.api.auto_terminate_on_stop = False
    await rig.rec.reconcile(rig.t)
    await rig.rec.reconcile(rig.t)
    assert rig.ex.market_closed == [("BTCUSDT", "sell", 0.006)]  # 按实际持仓平


async def test_reconciler_wording_matches_the_cause(rig: Rig) -> None:
    await open_filled(rig)
    rig.api.auto_terminate_on_stop = False
    rig.api.terminate("ex1", "SYSTEM_CLEANUP", leaves_position=True)
    await rig.rec.reconcile(rig.t)
    second = await rig.rec.reconcile(rig.t)
    detail = next(i.detail for i in second.issues if i.kind is ReconcileIssueKind.EXECUTOR_FAILED)
    assert "执行器已消失" in detail and "保命止损补不上" not in detail


async def _closing_with_slow_executor(rig: Rig) -> str:
    pid = await open_filled(rig)
    rig.api.auto_terminate_on_stop = False  # 停止请求发出了，执行器却一直不结束
    await rig.adapter.close(pid, CloseReason.MANUAL)
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSING  # type: ignore[union-attr]
    return pid


async def test_closing_with_unknown_executor_and_flat_exchange_keeps_protection(rig: Rig) -> None:
    pid = await _closing_with_slow_executor(rig)
    rig.api.executors.clear()  # hummingbot 查不到（返回空，不是 TERMINATED）
    rig.ex.set_position("BTCUSDT", Side.LONG, 0)  # 币安上已经平了
    await rig.adapter.positions()
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSING  # type: ignore[union-attr]
    await rig.adapter.positions()  # 连续第 2 次
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.CLOSING and rec.closed_at is None
    assert rig.ex.market_closed == [] and len(rig.ex.stops) == 1
    assert rig.ex.cancelled == []  # HB平仓请求可能尚有迟到副作用，不能只认瞬时零仓。


async def test_closing_with_unknown_executor_and_position_remaining_keeps_protection(
    rig: Rig,
) -> None:
    pid = await _closing_with_slow_executor(rig)
    rig.api.executors.clear()
    await rig.adapter.positions()
    await rig.adapter.positions()
    assert rig.ex.market_closed == []
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSING  # type: ignore[union-attr]
    assert len(rig.ex.stops) == 1 and rig.ex.cancelled == []


async def test_unknown_executor_counter_resets_when_it_comes_back(rig: Rig) -> None:
    pid = await _closing_with_slow_executor(rig)
    saved = dict(rig.api.executors)
    rig.api.executors.clear()
    await rig.adapter.positions()  # 第 1 次查不到
    rig.api.executors.update(saved)
    await rig.adapter.positions()  # 回来了：计数清零
    rig.api.executors.clear()
    await rig.adapter.positions()  # 又是第 1 次，不结账
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSING  # type: ignore[union-attr]


async def test_lifeboat_recorded_as_cancelled_after_normal_close(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.api.terminate("ex1", "TAKE_PROFIT")
    await rig.adapter.positions()
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.CLOSED
    assert rec.lifeboat is not None and rec.lifeboat.status.value == "cancelled"


# ---------------------------------------------------------------- 外部平仓（币安 App 手动平）


def _manual_close(rig: Rig) -> None:
    rig.ex.set_position("BTCUSDT", Side.LONG, 0)


async def test_manual_close_two_flat_rounds_settles_the_position(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.ex.manual_fill = CloseFill(68544.0, 0.2)
    _manual_close(rig)

    first = await rig.rec.reconcile(rig.t)
    assert first.issues == [] and rig.api.stopped == []  # 第 1 轮不动作
    second = await rig.rec.reconcile(rig.t)

    assert [i.kind for i in second.issues] == [ReconcileIssueKind.EXTERNAL_CLOSE]
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.CLOSED
    assert rec.close_reason is CloseReason.MANUAL
    assert rec.realized_pnl_usdt == pytest.approx(0.015 * (68544.0 - 68044.0) - 0.2)
    assert rig.api.stopped == [("ex1", True)]  # 停执行器但不让它再发平仓单
    assert rig.ex.market_closed == []  # 不再直连平仓
    assert len(rig.ex.placed) == 1  # 没有补挂保命止损
    assert rig.ex.stops == {}  # 残留的保命止损撤掉了
    info = [a for a in rig.alerts.sent if "手动平仓" in a.title]
    assert len(info) == 1 and info[0].level is AlertLevel.INFO
    # 之后不再有任何问题
    assert (await rig.rec.reconcile(rig.t)).issues == []


async def test_manual_close_never_reports_mismatch_or_replaces_lifeboat(rig: Rig) -> None:
    await open_filled(rig)
    rig.ex.manual_fill = CloseFill(68544.0, 0.2)
    rig.ex.stops.clear()  # 连保命止损也不在了，更不该去补
    _manual_close(rig)
    kinds: list[ReconcileIssueKind] = []
    for _ in range(3):
        kinds += [i.kind for i in (await rig.rec.reconcile(rig.t)).issues]
    assert kinds == [ReconcileIssueKind.EXTERNAL_CLOSE]
    assert len(rig.ex.placed) == 1


async def test_single_flat_round_does_nothing(rig: Rig) -> None:
    pid = await open_filled(rig)
    _manual_close(rig)
    report = await rig.rec.reconcile(rig.t)
    assert report.issues == []
    assert rig.registry.get(pid).record.status is PositionStatus.OPEN  # type: ignore[union-attr]
    assert rig.api.stopped == [] and len(rig.ex.stops) == 1


async def test_flat_counter_resets_when_the_position_reappears(rig: Rig) -> None:
    pid = await open_filled(rig)
    _manual_close(rig)
    await rig.rec.reconcile(rig.t)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.015)  # 一次读取抖动
    await rig.rec.reconcile(rig.t)
    _manual_close(rig)
    await rig.rec.reconcile(rig.t)  # 重新数第 1 轮
    assert rig.registry.get(pid).record.status is PositionStatus.OPEN  # type: ignore[union-attr]


async def test_manual_close_without_fill_waits_then_uses_mark_price(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.ex.manual_fill = None
    rig.ex.mark = 68100.0
    _manual_close(rig)
    await rig.rec.reconcile(rig.t)  # 第 1 轮：只记数
    for _ in range(4):  # 第 2–5 轮：空仓但没有成交证据，只告警，保命止损保留
        report = await rig.rec.reconcile(rig.t)
        assert not report.issues[0].fixed and "找不到平仓方向的成交" in report.issues[0].detail
        assert len(rig.ex.stops) == 1 and rig.api.stopped == []
    report = await rig.rec.reconcile(rig.t)  # 第 6 轮才结算
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.CLOSED
    # 标记价只作估算说明，缺真实成交/手续费不能声称净盈亏。
    assert rec.realized_pnl_usdt is None
    assert "标记价（估算）" in report.issues[0].detail
    assert rig.ex.stops == {}


async def test_manual_close_with_no_price_at_all_still_closes_eventually(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.ex.manual_fill = None
    rig.ex.mark = None
    _manual_close(rig)
    for _ in range(6):
        await rig.rec.reconcile(rig.t)
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.CLOSED and rec.realized_pnl_usdt is None


async def test_phantom_empty_reads_do_not_cancel_the_lifeboat(rig: Rig) -> None:
    """两轮读到空仓、结算前单独重读又读到有仓：接口抖动，不能撤保命止损。"""
    pid = await open_filled(rig)
    rig.ex.manual_fill = CloseFill(68544.0, 0.2)  # 就算成交查询碰巧有结果
    pos = list(rig.ex.pos)
    rig.ex.positions_queue = [[], [], pos]  # 第 1 轮空、第 2 轮空、第 2 轮的重读有仓
    await rig.rec.reconcile(rig.t)
    report = await rig.rec.reconcile(rig.t)
    assert report.issues == []
    assert rig.registry.get(pid).record.status is PositionStatus.OPEN  # type: ignore[union-attr]
    assert len(rig.ex.stops) == 1 and rig.ex.cancelled == [] and rig.api.stopped == []
    # 计数已清零：之后再有一次空读，也要重新数 2 轮
    rig.ex.positions_queue = [[]]
    await rig.rec.reconcile(rig.t)
    assert rig.registry.get(pid).record.status is PositionStatus.OPEN  # type: ignore[union-attr]


async def test_failed_recheck_abandons_settlement(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.ex.manual_fill = CloseFill(68544.0, 0.2)
    rig.ex.positions_queue = [[], [], ExchangeError("币安 502")]
    await rig.rec.reconcile(rig.t)
    report = await rig.rec.reconcile(rig.t)
    assert not report.issues[0].fixed and "重读失败" in report.issues[0].detail
    assert rig.registry.get(pid).record.status is PositionStatus.OPEN  # type: ignore[union-attr]
    assert len(rig.ex.stops) == 1 and rig.api.stopped == []


async def test_manual_close_recovers_evidence_without_repeating_unknown_stop(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.ex.manual_fill = CloseFill(68544.0, 0.2)
    _manual_close(rig)
    rig.api.stop_error = HbApiError("503")
    await rig.rec.reconcile(rig.t)
    failed = await rig.rec.reconcile(rig.t)
    assert not failed.issues[0].fixed
    assert rig.registry.get(pid).record.status is PositionStatus.OPEN  # type: ignore[union-attr]
    rig.api.stop_error = None
    pending = await rig.rec.reconcile(rig.t)
    assert not pending.issues[0].fixed and rig.ex.stops
    assert rig.api.stopped == []  # 503可能已接受，恢复连通不等于能重发停止。
    rig.api.terminate("ex1", "EARLY_STOP")  # 原请求后来完成的可核实终态。
    await rig.rec.reconcile(rig.t)
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED  # type: ignore[union-attr]
    assert rig.ex.stops == {}


async def test_manual_partial_reduce_only_warns(rig: Rig) -> None:
    pid = await open_filled(rig)
    rig.ex.set_position("BTCUSDT", Side.LONG, 0.005)  # 手动减了一部分
    for _ in range(3):
        report = await rig.rec.reconcile(rig.t)
        assert {i.kind for i in report.issues} == {ReconcileIssueKind.QTY_MISMATCH}
    assert rig.registry.get(pid).record.status is PositionStatus.OPEN  # type: ignore[union-attr]
    assert rig.api.stopped == [] and rig.ex.market_closed == []


async def test_fixed_issue_does_not_keep_reconcile_lamp_red(rig: Rig) -> None:
    """手动平仓已同步（fixed）后，对账灯不再报“发现 1 个问题”。"""
    beats: list[tuple[object, str]] = []

    class Rec:
        def record(self, *a: object, **k: object) -> None:
            pass

        def note(self, now: object, summary: str, count: bool = False) -> None:
            pass

        def heartbeat(self, now: object, health: object, headline: str) -> None:
            beats.append((health, headline))

    rig.rec._rec = Rec()  # type: ignore[assignment]
    await open_filled(rig)
    rig.ex.manual_fill = CloseFill(68544.0, 0.2)
    _manual_close(rig)
    await rig.rec.reconcile(rig.t)
    second = await rig.rec.reconcile(rig.t)
    assert [i.fixed for i in second.issues] == [True]
    assert beats[-1] == (Health.OK, "三方一致（本轮已处理 1 项）")


# ---------------------------------------------------------------- 反手仓（平仓单抢跑开出的反向仓）


async def _stopped_then_reversed(rig: Rig) -> tuple[str, float]:
    """保命止损先平了仓，执行器的止损单又成交一次：执行器 STOP_LOSS 结束，币安出现反向等量仓。"""
    pid = await open_filled(rig)
    rig.api.terminate("ex1", "STOP_LOSS", pnl=-5.0)
    await rig.adapter.positions()
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.CLOSED
    rig.ex.set_position(rec.symbol, Side.SHORT, rec.quantity)
    return pid, rec.quantity


async def test_reverse_position_after_executor_end_is_closed_in_the_same_round(rig: Rig) -> None:
    pid, qty = await _stopped_then_reversed(rig)
    rig.t += timedelta(minutes=3)
    report = await rig.rec.reconcile(rig.t)  # 开关默认关也要平：这是我们自己的仓
    (issue,) = report.issues
    assert issue.kind is ReconcileIssueKind.SIDE_MISMATCH and issue.fixed
    assert issue.position_id == pid and "反手仓" in issue.detail
    assert rig.ex.market_closed == [("BTCUSDT", "buy", qty)]  # 空仓用买单只减仓平
    assert any(a.level is AlertLevel.CRITICAL for a in rig.alerts.sent)


async def test_reverse_position_long_after_window_is_only_an_orphan(rig: Rig) -> None:
    await _stopped_then_reversed(rig)
    rig.t += timedelta(minutes=11)
    (issue,) = (await rig.rec.reconcile(rig.t)).issues
    assert issue.kind is ReconcileIssueKind.ORPHAN_POSITION and not issue.fixed
    assert rig.ex.market_closed == []


async def test_reverse_position_with_other_quantity_is_only_an_orphan(rig: Rig) -> None:
    _, qty = await _stopped_then_reversed(rig)
    rig.ex.set_position("BTCUSDT", Side.SHORT, qty * 3)
    (issue,) = (await rig.rec.reconcile(rig.t)).issues
    assert issue.kind is ReconcileIssueKind.ORPHAN_POSITION and rig.ex.market_closed == []


async def test_reverse_close_failure_stays_critical_and_unfixed(rig: Rig) -> None:
    await _stopped_then_reversed(rig)
    rig.ex.fail_market_close = True
    (issue,) = (await rig.rec.reconcile(rig.t)).issues
    assert issue.kind is ReconcileIssueKind.SIDE_MISMATCH and not issue.fixed
    assert rig.alerts.sent[-1].level is AlertLevel.CRITICAL


async def test_open_ledger_with_opposite_exchange_side_is_flattened_not_relifeboated(
    rig: Rig,
) -> None:
    """账本还开着多单，币安上却是等量空单：不按账本方向补保命止损（会失败），直接平反手仓。"""
    pid = await open_filled(rig)
    qty = rig.registry.get(pid).record.quantity  # type: ignore[union-attr]
    rig.ex.stops.clear()  # 保命止损已经触发
    rig.ex.set_position("BTCUSDT", Side.SHORT, qty)
    placed_before = len(rig.ex.placed)
    report = await rig.rec.reconcile(rig.t)
    kinds = [i.kind for i in report.issues]
    assert kinds == [ReconcileIssueKind.SIDE_MISMATCH] and report.issues[0].fixed
    assert len(rig.ex.placed) == placed_before  # 没有补保命止损
    assert rig.ex.market_closed == [("BTCUSDT", "buy", qty)]


async def test_open_ledger_opposite_side_other_quantity_only_alerts(rig: Rig) -> None:
    pid = await open_filled(rig)
    qty = rig.registry.get(pid).record.quantity  # type: ignore[union-attr]
    rig.ex.set_position("BTCUSDT", Side.SHORT, qty / 2)
    (issue,) = (await rig.rec.reconcile(rig.t)).issues
    assert issue.kind is ReconcileIssueKind.SIDE_MISMATCH and not issue.fixed
    assert rig.ex.market_closed == []


# 对账查持仓一时超时：本轮重试，连续 2 轮才告警


def _timeouts(n: int) -> list[ExchangeError]:
    return [ExchangeError("查持仓失败：RequestTimeout", transient=True) for _ in range(n)]


async def test_reconcile_retries_transient_timeout_in_round(rig: Rig) -> None:
    rig.rec._retry_delays = (0.0, 0.0)
    rig.ex.positions_queue = [*_timeouts(2)]  # 第 3 次成功
    await rig.rec.reconcile(rig.t)
    assert rig.alerts.sent == []


async def test_reconcile_alerts_only_after_two_failed_rounds(rig: Rig) -> None:
    rig.rec._retry_delays = (0.0, 0.0)
    rig.ex.positions_queue = [*_timeouts(3)]
    with pytest.raises(ReconcileError):
        await rig.rec.reconcile(rig.t)
    assert rig.alerts.sent == []  # 单轮只记日志
    await rig.rec.reconcile(rig.t)  # 下一轮好了：计数清零
    rig.ex.positions_queue = [*_timeouts(3)]
    with pytest.raises(ReconcileError):
        await rig.rec.reconcile(rig.t)
    assert rig.alerts.sent == []
    rig.ex.positions_queue = [*_timeouts(3)]
    with pytest.raises(ReconcileError):
        await rig.rec.reconcile(rig.t)
    assert [a.level for a in rig.alerts.sent] == [AlertLevel.CRITICAL]
    assert "连续 2 轮" in rig.alerts.sent[0].title


async def test_reconcile_definite_error_alerts_at_once(rig: Rig) -> None:
    rig.rec._retry_delays = (0.0, 0.0)
    rig.ex.positions_queue = [ExchangeError("查持仓失败：-2015 Invalid API-key")]
    with pytest.raises(ReconcileError):
        await rig.rec.reconcile(rig.t)
    assert [a.level for a in rig.alerts.sent] == [AlertLevel.CRITICAL]


def test_ccxt_transient_classification() -> None:
    import ccxt

    from jiaoyi.execution.exchange import is_transient

    assert is_transient(ccxt.RequestTimeout("t")) and is_transient(ccxt.NetworkError("n"))
    assert is_transient(ccxt.ExchangeNotAvailable("502"))
    for e in (ccxt.DDoSProtection("418"), ccxt.RateLimitExceeded("429")):
        assert not is_transient(e)
    assert not is_transient(ccxt.InvalidNonce("-1021"))
    assert not is_transient(ccxt.AuthenticationError("-2015"))
