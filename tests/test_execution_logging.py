"""下单精度提供者，以及执行层按验收约定写的决策日志。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.acceptance import audit, read_records, read_statuses
from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.contracts import OrderIntent, Outcome, Role
from jiaoyi.contracts.position import CloseReason
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution.exchange import CcxtBinanceFutures, ExchangeError, LotFilter
from jiaoyi.execution.hb_executor import HbExecutorAdapter
from jiaoyi.execution.lifeboat import BinanceLifeboat
from jiaoyi.execution.lot_rules import ExchangeInfoLotRules
from jiaoyi.execution.reconciler import ThreeWayReconciler
from jiaoyi.execution.registry import PositionRegistry
from jiaoyi.risk.guard import LotRules
from tests.conftest import load_example
from tests.fakes import FakeExchange, FakeHbApi

T0 = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)


class Clk:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += timedelta(seconds=s)


# ---------------------------------------------------------------- LotRulesProvider


async def test_lot_rules_fail_closed_until_loaded_and_when_stale() -> None:
    ex, clk = FakeExchange(), Clk()
    lots = ExchangeInfoLotRules(ex, max_age_s=3600, clock=clk)
    assert lots.lot_rules("BTCUSDT") is None  # 从没拉过
    assert await lots.refresh()
    assert lots.lot_rules("BTCUSDT") == LotRules(0.001, 0.001, 100.0)
    assert lots.lot_rules("DOGEUSDT") is None  # 不在列表里
    clk.t += timedelta(seconds=3601)
    assert lots.lot_rules("BTCUSDT") is None  # 过期


async def test_failed_refresh_keeps_cache_until_it_expires() -> None:
    ex, clk = FakeExchange(), Clk()
    lots = ExchangeInfoLotRules(ex, max_age_s=3600, clock=clk)
    await lots.refresh()
    ex.filters_down = True
    clk.t += timedelta(seconds=1800)
    assert not await lots.refresh()
    assert lots.lot_rules("BTCUSDT") is not None  # 还没过期，继续用
    clk.t += timedelta(seconds=1900)
    assert lots.lot_rules("BTCUSDT") is None


async def test_empty_exchange_info_is_ignored() -> None:
    ex, clk = FakeExchange(), Clk()
    lots = ExchangeInfoLotRules(ex, clock=clk)
    await lots.refresh()
    ex.filters = {}
    assert not await lots.refresh()
    assert lots.lot_rules("BTCUSDT") is not None


async def test_refresh_picks_up_new_rules() -> None:
    ex, clk = FakeExchange(), Clk()
    lots = ExchangeInfoLotRules(ex, clock=clk)
    await lots.refresh()
    ex.filters["BTCUSDT"] = LotFilter(0.01, 0.01, 50.0)
    await lots.refresh()
    assert lots.lot_rules("BTCUSDT") == LotRules(0.01, 0.01, 50.0)


class FakeCcxtMarkets:
    async def load_markets(self, reload: bool) -> dict[str, Any]:
        def market(symbol: str, **over: Any) -> dict[str, Any]:
            base = {
                "symbol": symbol,
                "swap": True,
                "linear": True,
                "settle": "USDT",
                "info": {
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.002"},
                        {"filterType": "MIN_NOTIONAL", "notional": "100"},
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "maxQty": "120"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    ],
                },
            }
            base.update(over)
            return base

        return {
            "a": market("BTC/USDT:USDT"),
            "b": market("BTC/USDT:USDT-261225"),  # 交割合约，跳过
            "c": market("ETH/USDT:USDT", info={"contractType": "PERPETUAL", "status": "BREAK"}),
            "d": market("SOL/USDT", swap=False),  # 现货，跳过
            "e": market("XRP/USDT:USDT", info={"contractType": "PERPETUAL", "status": "TRADING"}),
            "f": market(  # 美股永续：ccxt 不一定标成 swap，按 exchangeInfo 的字段认
                "TSLA/USDC",
                swap=False,
                info={
                    "symbol": "TSLAUSDT",
                    "contractType": "TRADIFI_PERPETUAL",
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "filters": [{"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"}],
                },
            ),
        }


async def test_ccxt_lot_filters_mapping() -> None:
    ex = CcxtBinanceFutures("k", "s", testnet=True)
    await ex._ex.close()
    ex._ex = FakeCcxtMarkets()
    assert await ex.lot_filters() == {
        "BTCUSDT": LotFilter(0.001, 0.002, 100.0, max_qty=120.0, tick_size=0.1),
        "TSLAUSDT": LotFilter(0.01, 0.01, 0.0),
    }


async def test_max_qty_flows_through_provider() -> None:
    ex, clk = FakeExchange(), Clk()
    ex.filters = {"BTCUSDT": LotFilter(0.001, 0.001, 100.0, max_qty=120.0, tick_size=0.1)}
    lots = ExchangeInfoLotRules(ex, clock=clk)
    await lots.refresh()
    assert lots.lot_rules("BTCUSDT") == LotRules(0.001, 0.001, 100.0, 120.0, 0.1)


async def test_ccxt_lot_filters_failure_is_exchange_error() -> None:
    class Boom:
        async def load_markets(self, reload: bool) -> None:
            raise RuntimeError("网络")

    ex = CcxtBinanceFutures("k", "s", testnet=True)
    await ex._ex.close()
    ex._ex = Boom()
    with pytest.raises(ExchangeError):
        await ex.lot_filters()


# ---------------------------------------------------------------- 决策日志


async def test_acceptance_m0_passes_on_a_full_trade(tmp_path: Path) -> None:
    clk, api, ex = Clk(), FakeHbApi(), FakeExchange()
    api.exchange = ex  # 明确注册本fixture的入场订单，未知CID不能伪造成交。
    log = JsonlDecisionLog(tmp_path / "data")

    def rec(role: Role) -> RoleRecorder:
        return RoleRecorder(role, log)

    lifeboat = BinanceLifeboat(ex, clock=clk, recorder=rec(Role.LIFEBOAT))
    registry = PositionRegistry(tmp_path / "positions.json")
    alerts = StubAlerter()
    adapter = HbExecutorAdapter(
        api,
        lifeboat,
        registry,
        alerts,
        account_name="acct",
        connector_name="binance_perpetual_testnet",
        fill_timeout_s=5,
        poll_s=1,
        clock=clk,
        sleep=clk.sleep,
        recorder=rec(Role.EXECUTOR),
        exchange=ex,
    )
    reconciler = ThreeWayReconciler(
        adapter,
        api,
        ex,
        lifeboat,
        registry,
        alerts,
        account_name="acct",
        recorder=rec(Role.RECONCILER),
        heartbeats=(adapter.heartbeat, lifeboat.heartbeat),
    )

    original = api.create_executor

    async def create_and_fill(cfg: dict[str, Any], acct: str, marker: str) -> dict[str, Any]:
        out = await original(cfg, acct, marker)
        api.fill(out["executor_id"], 68044.0)
        return out

    api.create_executor = create_and_fill  # type: ignore[method-assign]
    position = await adapter.open(OrderIntent.model_validate(load_example("order_intent")))
    api.on_terminate = lambda _: ex.set_position(position.symbol, position.side, 0)
    ex.set_position(position.symbol, position.side, position.quantity)
    clk.t += timedelta(seconds=10)
    assert (await reconciler.reconcile(clk.t)).clean
    clk.t += timedelta(seconds=10)
    api.executors["ex1"]["net_pnl_quote"] = 1.25
    await adapter.close(position.position_id, CloseReason.MANUAL)
    await adapter.positions()
    ex.set_position(position.symbol, position.side, 0)
    await reconciler.reconcile(clk.t)

    records = read_records(tmp_path / "data")
    by = {(r.role, r.outcome) for r in records}
    assert (Role.EXECUTOR, Outcome.ACTION) in by  # 开仓成交
    assert (Role.LIFEBOAT, Outcome.ACTION) in by  # 保命止损挂上
    assert (Role.EXECUTOR, Outcome.CLOSE) in by  # 平仓
    close = next(r for r in records if r.outcome is Outcome.CLOSE)
    assert close.position_id == position.position_id and close.output["net"] == 1.25
    opened = next(r for r in records if r.role is Role.EXECUTOR and r.outcome is Outcome.ACTION)
    assert opened.signal_id == position.signal_id and opened.position_id == position.position_id

    checks = audit(records, read_statuses(tmp_path / "data"), stage="m0", hours=0, now=clk.t)
    failed = [c.line() for c in checks if c.ok is False]
    assert not failed, failed


async def test_unfixed_reconcile_issue_is_logged_as_error(tmp_path: Path) -> None:
    clk, api, ex = Clk(), FakeHbApi(), FakeExchange()
    log = JsonlDecisionLog(tmp_path / "data")
    registry = PositionRegistry(tmp_path / "positions.json")
    lifeboat = BinanceLifeboat(ex, clock=clk)
    adapter = HbExecutorAdapter(
        api, lifeboat, registry, StubAlerter(), account_name="acct", connector_name="c", clock=clk
    )
    reconciler = ThreeWayReconciler(
        adapter,
        api,
        ex,
        lifeboat,
        registry,
        StubAlerter(),
        account_name="acct",
        recorder=RoleRecorder(Role.RECONCILER, log),
    )
    from jiaoyi.contracts import Side

    ex.set_position("ETHUSDT", Side.LONG, 2.0)  # 陌生仓位：只告警，没修
    await reconciler.reconcile(clk.t)
    (r,) = [x for x in read_records(tmp_path / "data") if x.role is Role.RECONCILER]
    assert r.outcome is Outcome.ERROR and r.symbol == "ETHUSDT"
    status = read_statuses(tmp_path / "data")["reconciler"]
    assert status.health.value == "degraded"
