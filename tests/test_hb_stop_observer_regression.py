"""容器只读观测独立验收：原返回、异常、判断边界和调用次数保持。"""

from __future__ import annotations

import asyncio
import importlib.util
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).parents[1]


def observer() -> Any:
    path = ROOT / "docker/hb-demo/stop_diagnostics.py"
    spec = importlib.util.spec_from_file_location("independent_stop_observer", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.WRITER = SimpleNamespace(submit=lambda state, row: None)
    return module


class PriceType(Enum):
    BestBid = 1
    BestAsk = 2


class CloseType(Enum):
    STOP_LOSS = 1
    TRAILING_STOP = 2


def executor_class() -> type[Any]:
    class Executor:
        def __init__(self, side: str = "long") -> None:
            self.side = side
            self.config = SimpleNamespace(
                id="synthetic-id",
                triple_barrier_config=SimpleNamespace(
                    stop_loss=Decimal("0.01"),
                    trailing_stop=SimpleNamespace(
                        activation_price=Decimal("0.02"), trailing_delta=Decimal("0.01")
                    ),
                ),
            )
            self.net = Decimal("0")
            self._trailing_stop_trigger_pct: Decimal | None = None
            self._open_order = SimpleNamespace(order_id="owned-open", order=None)
            self._close_order = None
            self._take_profit_limit_order = None
            self.requests: list[str] = []
            self.price_reads = self.net_reads = self.fills = 0
            self.consume_net = True
            self.close_error: Exception | None = None
            self.result = object()

        def get_price(self, connector: str, pair: str, *, price_type: PriceType) -> Decimal:
            self.price_reads += 1
            return Decimal("101") if price_type is PriceType.BestBid else Decimal("102")

        def get_net_pnl_pct(self) -> Decimal:
            self.net_reads += 1
            self.get_price(
                "mock",
                "BTC-USDT",
                price_type=PriceType.BestBid if self.side == "long" else PriceType.BestAsk,
            )
            return self.net

        @property
        def net_pnl_pct(self) -> Decimal:
            return self.get_net_pnl_pct()

        def control_stop_loss(self) -> None:
            stop = self.config.triple_barrier_config.stop_loss
            if stop and self.net_pnl_pct <= -stop:
                self.place_close_order_and_cancel_open_orders(close_type=CloseType.STOP_LOSS)

        def control_trailing_stop(self) -> None:
            trailing = self.config.triple_barrier_config.trailing_stop
            if trailing:
                net = self.get_net_pnl_pct()
                if not self._trailing_stop_trigger_pct:
                    if net > trailing.activation_price:
                        self._trailing_stop_trigger_pct = net - trailing.trailing_delta
                else:
                    if net < self._trailing_stop_trigger_pct:
                        self.place_close_order_and_cancel_open_orders(
                            close_type=CloseType.TRAILING_STOP
                        )
                    if net - trailing.trailing_delta > self._trailing_stop_trigger_pct:
                        self._trailing_stop_trigger_pct = net - trailing.trailing_delta

        async def control_task(self) -> object:
            if self.consume_net:
                self.control_stop_loss()
                await asyncio.sleep(0)
                self.control_trailing_stop()
            return self.result

        def place_close_order_and_cancel_open_orders(self, *, close_type: CloseType) -> object:
            self.requests.append(close_type.name)
            if self.close_error is not None:
                raise self.close_error
            return self.result

        def process_order_filled_event(self, tag: str, market: str, event: Any) -> str:
            if event.order_id == self._open_order.order_id:
                self.fills += 1
                return "owned"
            return "foreign"

        def get_custom_info(self) -> dict[str, Any]:
            return {"original": "unchanged"}

    return Executor


@pytest.mark.parametrize("side", ["long", "short"])
@pytest.mark.parametrize(
    "net,trigger,expected",
    [
        ("-0.01", None, ["STOP_LOSS"]),
        ("-0.009999", None, []),
        ("0.02", None, []),
        ("0.03", None, []),
        ("0.01", "0.01", []),
        ("0.009999", "0.01", ["TRAILING_STOP"]),
        ("0.001", "0", []),
    ],
)
async def test_observation_does_not_change_stop_boundaries_or_quote_reads(
    side: str,
    net: str,
    trigger: str | None,
    expected: list[str],
) -> None:
    module = observer()
    original, patched = executor_class(), executor_class()
    assert module.install(patched) and module.install(patched)  # 重复安装不能加倍包装。
    a, b = original(side), patched(side)
    for item in (a, b):
        item.net = Decimal(net)
        item._trailing_stop_trigger_pct = Decimal(trigger) if trigger is not None else None
        assert await item.control_task() is item.result
    assert a.requests == b.requests == expected
    assert (a.price_reads, a.net_reads) == (b.price_reads, b.net_reads) == (2, 2)
    assert a._trailing_stop_trigger_pct == b._trailing_stop_trigger_pct
    raw = b.get_custom_info()["stop_diagnostics"]
    assert raw["trailing_active"] is bool(b._trailing_stop_trigger_pct)
    assert raw["stop_loss_net_pnl_pct"] == -1.0
    assert raw["trailing_stop_price"] is None
    checks = raw["checks"]  # 当前逐项检查和异步订单事件分别存放，面板组合展示。
    assert len(checks) == 2
    assert checks[0]["comparison"] == "<="
    assert checks[-1]["net_reads"][-1]["value_pct"] == pytest.approx(float(Decimal(net) * 100))
    if net == "0.03" and trigger is None:
        assert checks[-1]["check_action"] == "activation"
        assert checks[-1]["condition_met"] is True
        assert checks[-1]["close_requested"] is False
    if net == "0.01" and trigger == "0.01":
        assert checks[-1]["comparison"] == "<"
        assert checks[-1]["condition_met"] is False


async def test_writer_error_cannot_replace_an_original_return_or_exception() -> None:
    module = observer()

    def fail(*args: Any) -> None:
        raise OSError("synthetic logging failure")

    module.WRITER = SimpleNamespace(submit=fail)
    cls = executor_class()
    assert module.install(cls)
    item = cls()
    assert await item.control_task() is item.result
    error = RuntimeError("synthetic original order failure")
    item.net, item.close_error = Decimal("-0.02"), error
    with pytest.raises(RuntimeError) as raised:
        await item.control_task()
    assert raised.value is error
    assert item.requests == ["STOP_LOSS"]


async def test_a_cycle_that_did_not_read_net_or_price_cannot_reuse_previous_observations() -> None:
    module = observer()
    cls = executor_class()
    assert module.install(cls)
    item = cls()
    item.net = Decimal("0.03")
    await item.control_task()
    item.consume_net = False  # 模拟原SHUTTING_DOWN分支，不再执行barrier检查。
    await item.control_task()
    raw = item.get_custom_info()["stop_diagnostics"]
    assert raw["net_pnl_observed"] is False and raw["net_pnl_pct"] is None
    assert raw["decision_price"] is None and raw["bid"] is None and raw["ask"] is None


async def test_foreign_order_callback_is_not_a_fill_for_this_executor() -> None:
    module = observer()
    cls = executor_class()
    assert module.install(cls)
    item = cls()
    await item.control_task()
    foreign = SimpleNamespace(order_id="foreign-order", price=Decimal("103"), amount=Decimal("1"))
    assert item.process_order_filled_event("tag", "market", foreign) == "foreign"
    raw = item.get_custom_info()["stop_diagnostics"]
    assert not any(e["stage"] == "fill" for e in raw["events"])
    owned = SimpleNamespace(order_id="owned-open", price=Decimal("101"), amount=Decimal("0.1"))
    assert item.process_order_filled_event("tag", "market", owned) == "owned"
    raw = item.get_custom_info()["stop_diagnostics"]
    assert item.fills == 1 and len([e for e in raw["events"] if e["stage"] == "fill"]) == 1


async def test_callback_diagnostic_failure_does_not_change_original_callback_result() -> None:
    module = observer()
    cls = executor_class()
    assert module.install(cls)
    item = cls()
    await item.control_task()

    def fail_hash(value: Any) -> None:
        raise OSError("synthetic diagnostic hash failure")

    module._hash = fail_hash
    event = SimpleNamespace(order_id="owned-open", price=Decimal("101"), amount=Decimal("0.1"))
    assert item.process_order_filled_event("tag", "market", event) == "owned"
    assert item.fills == 1
    assert item.get_custom_info()["stop_diagnostics"]["observer_errors"] >= 1


async def test_failed_close_metadata_cannot_suppress_or_deny_the_original_close_request() -> None:
    module = observer()
    cls = executor_class()
    assert module.install(cls)
    item = cls()
    item.net = Decimal("-0.02")

    def fail_enum(value: Any) -> None:
        raise ValueError("synthetic observation enum failure")

    module._enum = fail_enum
    assert await item.control_task() is item.result
    assert item.requests == ["STOP_LOSS"]
    raw = item.get_custom_info()["stop_diagnostics"]
    assert raw["observer_errors"] >= 1
    assert raw["checks"][0]["close_requested"] is not False
