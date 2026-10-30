"""交易所移动止损（``[fast_loop] trailing_native``）：币安 TRAILING_STOP_MARKET 代替程序每秒换代。

开着时带移动止损的仓在交易所只挂固定止损 + 交易所移动止损，不挂保命单；挂不上就照旧保命单 +
程序自己跟。只有合成订单和假时钟，不连接任何服务。

入场成本（``SlowHarness``）：2 张、成交额 200、开仓手续费 0.2；启动线 0.3%、回撤距离 0.5%。
激活价：多 (200 + 200×0.003 + 0.2) / 2 = 100.4，空 (200 − 0.8) / 2 = 99.6。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.brackets import BracketManager
from jiaoyi.execution.exchange import (
    BracketSubmissionRejected,
    BracketSubmissionUnknown,
    ExchangeBracket,
    ExchangeError,
    callback_rate,
)
from jiaoyi.panel.protection import lifeboat_summary, parse_protection
from tests.test_trailing_book_fresh import SlowHarness

NativeFail = Literal["reject", "immediate", "unknown"] | None


class NativeHarness(SlowHarness):
    """``native`` 是开关（热改）；``native_fail`` 让下一次交易所移动止损提交失败。"""

    def __init__(self, root: Path, side: Side, *, native: bool = True) -> None:
        self.native = native
        super().__init__(root, side, latency=0.0)
        self.native_fail: NativeFail = None
        self.trailing_calls: list[dict[str, Any]] = []
        self.exchange.place_trailing = self.place_trailing  # type: ignore[attr-defined]
        self.long = side is Side.LONG
        # 默认标记价离激活价还远：多 100.1 < 100.4，空 99.9 > 99.6
        self.mark_value = 100.1 if self.long else 99.9

    def build(self) -> BracketManager:
        self.exchange.registry = self.registry
        return BracketManager(
            self.exchange,
            self.registry,
            account_name="synthetic-account",
            connector_name="synthetic-demo",
            clock=lambda: self.now,
            sleep=self.sleep,
            stop_working_type="CONTRACT_PRICE",  # 交易所移动止损不跟它，固定按标记价
            native_trailing=lambda: self.native,
            unknown_submit_grace_s=0.0,
        )

    async def place_trailing(
        self,
        symbol: str,
        close_side: Literal["buy", "sell"],
        quantity: float,
        callback_rate: float,
        client_order_id: str,
        *,
        activate_price: float | None,
        working_type: str,
    ) -> ExchangeBracket:
        ex = self.exchange
        stored = self.registry.get("pos-1").server_protection
        leg = next(x for x in stored.legs if x.client_order_id == client_order_id)
        assert leg.state == "submit_pending"  # 提交之前已持久化意图
        ex.events.append(("create", client_order_id))
        self.trailing_calls.append(
            {
                "symbol": symbol,
                "close_side": close_side,
                "quantity": quantity,
                "callback_rate": callback_rate,
                "cid": client_order_id,
                "activate_price": activate_price,
                "working_type": working_type,
            }
        )
        fail, self.native_fail = self.native_fail, None
        if fail in ("reject", "immediate"):
            raise BracketSubmissionRejected("synthetic", immediate_trigger=fail == "immediate")
        if fail == "unknown":
            raise BracketSubmissionUnknown(client_order_id)
        order = ExchangeBracket(
            symbol=symbol,
            algo_id=str(len(ex.orders) + 1),
            client_order_id=client_order_id,
            kind="trailing_stop",
            close_side=close_side,
            quantity=quantity,
            trigger_price=activate_price or 0.0,
            position_side="BOTH",
            working_type=working_type,
            reduce_only=True,
            price_protect=False,
            close_position=False,
            algo_status="NEW",
            child_state="absent",
            activate_price=activate_price,
            callback_rate=callback_rate,
        )
        ex.orders[client_order_id] = order
        return order

    def live_cids(self) -> list[str]:
        return sorted(c for c, o in self.exchange.orders.items() if o.algo_status == "NEW")

    def set_trailing(self, activation: float, distance: float) -> None:
        tracked = self.registry.get("pos-1")
        rec = tracked.record.model_copy(
            update={"trailing_activation_pct": activation, "trailing_stop_pct": distance}
        )
        self.registry.put(tracked.model_copy(update={"record": rec}))

    def closed(self) -> bool:
        return self.registry.get("pos-1").record.status is PositionStatus.CLOSING


async def native_armed(h: NativeHarness) -> None:
    await h.armed()
    await h.adapter.protection_tick()


@pytest.mark.parametrize(
    ("side", "close_side", "activate"), [(Side.LONG, "sell", 100.4), (Side.SHORT, "buy", 99.6)]
)
async def test_native_leg_is_placed_with_fee_activation_and_no_lifeboat(
    tmp_path: Path, side: Side, close_side: str, activate: float
) -> None:
    h = NativeHarness(tmp_path, side)
    await native_armed(h)
    assert h.trailing_calls == [
        {
            "symbol": "BTCUSDT",
            "close_side": close_side,
            "quantity": 2.0,
            "callback_rate": 0.5,
            "cid": "br-ts-pos-1-1",
            "activate_price": activate,
            "working_type": "MARK_PRICE",
        }
    ]
    assert h.creates() == ["br-sl-pos-1-1", "br-ts-pos-1-1"]  # 保命单从没挂过
    assert h.live_cids() == ["br-sl-pos-1-1", "br-ts-pos-1-1"]
    assert h.manager.native_mode("pos-1") == "confirmed"
    state = h.state()
    assert state.native_activated is False and state.native_stop_price is None
    assert state.native_pending_since is None and state.native_fallback is None
    assert not h.closed() and not h.titles()


@pytest.mark.parametrize(
    ("distance", "rate"),
    [("0.45", "0.4"), ("1.57", "1.5"), ("0.24", "0.2"), ("0.1", "0.1"), ("0.09", "0.0")],
)
def test_callback_rate_floors_to_the_exchange_step(distance: str, rate: str) -> None:
    assert str(callback_rate(float(distance))) == rate


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_floored_callback_rate_is_what_gets_placed(tmp_path: Path, side: Side) -> None:
    h = NativeHarness(tmp_path, side)
    h.set_trailing(0.6, 0.45)
    await native_armed(h)
    assert [c["callback_rate"] for c in h.trailing_calls] == [0.4]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    ("distance", "reason"),
    [
        (10.15, "回撤距离 10.15% 不在交易所移动止损范围 0.1–10% 内"),  # 取整后 10.1
        (0.08, "回撤距离 0.08% 不在交易所移动止损范围 0.1–10% 内"),  # 取整后 0，挂上会更松
    ],
)
async def test_callback_rate_outside_the_exchange_range_keeps_our_own_trailing(
    tmp_path: Path, side: Side, distance: float, reason: str
) -> None:
    h = NativeHarness(tmp_path, side)
    h.set_trailing(12.0, distance)
    await native_armed(h)
    assert h.trailing_calls == []
    assert h.state().native_fallback == reason
    assert h.manager.native_mode("pos-1") == "off"
    assert h.live_cids() == ["br-sl-pos-1-1", "lb-pos-1-2"]  # 当轮补挂保命单
    assert [a.title for a in h.alerter.sent] == ["交易所移动止损没用上，改回程序自己跟"]


@pytest.mark.parametrize(("side", "mark"), [(Side.LONG, 100.5), (Side.SHORT, 99.5)])
async def test_mark_already_past_activation_places_without_activation_price(
    tmp_path: Path, side: Side, mark: float
) -> None:
    h = NativeHarness(tmp_path, side)
    h.mark_value = mark
    await native_armed(h)
    assert [c["activate_price"] for c in h.trailing_calls] == [None]
    state = h.state()
    assert state.native_activated is True and state.native_extreme == mark
    # 多 100.5 × 0.995、空 99.5 × 1.005
    assert state.native_stop_price == pytest.approx(99.9975)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_local_native_price_follows_the_best_mark_since_activation(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    await native_armed(h)
    sign = 1 if side is Side.LONG else -1
    marks = [100.3, 100.4, 101.0, 100.6] if sign > 0 else [99.7, 99.6, 99.0, 99.4]
    seen = []
    for m in marks:
        h.mark_value = m
        await h.adapter.protection_tick()
        seen.append(h.state().native_stop_price)
    # 未激活 → 刚好碰到激活价 → 新高 → 回落不放松
    expect_long = [None, 99.898, 100.495, 100.495]
    expect_short = [None, 100.098, 99.495, 99.495]
    want = expect_long if sign > 0 else expect_short
    assert seen == [pytest.approx(x) if x is not None else None for x in want]
    assert h.state().native_extreme == (101.0 if sign > 0 else 99.0)
    assert h.creates() == ["br-sl-pos-1-1", "br-ts-pos-1-1"]  # 程序没换代止损腿
    assert h.book_reads == []  # 程序自己的移动止损不跑（它要读盘口）


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize("fail", ["reject", "unknown"])
async def test_rejected_or_unknown_native_keeps_fixed_stop_adds_lifeboat_and_own_trailing(
    tmp_path: Path, side: Side, fail: NativeFail
) -> None:
    h = NativeHarness(tmp_path, side)
    h.native_fail = fail
    await native_armed(h)
    assert h.manager.native_mode("pos-1") == "off"
    assert h.state().native_fallback is not None
    assert "br-sl-pos-1-1" in h.live_cids() and any(c.startswith("lb-") for c in h.live_cids())
    assert not h.closed()
    # 下一轮程序自己跟：读盘口、净收益过启动线后换代止损腿
    h.bid, h.ask = (101.4, 101.5) if side is Side.LONG else (98.5, 98.6)
    h.mark_value = 101.45 if side is Side.LONG else 98.55
    await h.adapter.protection_tick()
    assert h.book_reads and h.state().trailing_activated
    assert [c for c in h.creates() if c.startswith("br-ts-")] == ["br-ts-pos-1-1"]  # 不再尝试
    assert any(c.startswith("br-sl-") and c != "br-sl-pos-1-1" for c in h.creates())


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_immediate_trigger_rejection_retries_once_without_activation(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    h.native_fail = "immediate"
    await native_armed(h)
    assert h.manager.native_mode("pos-1") == "pending" and h.trailing_calls[0]["activate_price"]
    h.mark_value = 100.5 if side is Side.LONG else 99.5
    await h.adapter.protection_tick()
    assert [c["cid"] for c in h.trailing_calls] == ["br-ts-pos-1-1", "br-ts-pos-1-2"]
    assert h.trailing_calls[1]["activate_price"] is None
    assert h.manager.native_mode("pos-1") == "confirmed"
    assert not any(c.startswith("lb-") for c in h.creates())


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_native_stuck_pending_gives_up_and_places_the_lifeboat(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    h.mark_error = ExchangeError("synthetic mark down", transient=True)
    await native_armed(h)
    assert h.manager.native_mode("pos-1") == "pending" and h.live_cids() == ["br-sl-pos-1-1"]
    assert lifeboat_summary(panel(h)) == ("native", None)  # 等着挂，按设计不挂保命单
    h.now += timedelta(seconds=31)
    await h.manager.sync("pos-1")
    assert h.state().native_fallback == "武装后 30 秒还没挂上交易所移动止损"
    assert h.live_cids() == ["br-sl-pos-1-1", "lb-pos-1-2"]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_restart_mid_trail_keeps_the_leg_and_the_local_extreme(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    await native_armed(h)
    h.mark_value = 101.0 if side is Side.LONG else 99.0
    await h.adapter.protection_tick()
    h.restart_adapter()
    raw = json.loads(h.path.read_text())["positions"]["pos-1"]["server_protection"]
    assert raw["native_extreme"] == h.mark_value and raw["native_activated"] is True
    h.mark_value = 100.8 if side is Side.LONG else 99.2  # 重启后回落：沿用重启前的极值
    await h.adapter.protection_tick()
    assert h.manager.native_mode("pos-1") == "confirmed"
    assert h.state().native_stop_price == pytest.approx(100.495 if side is Side.LONG else 99.495)
    assert h.creates() == ["br-sl-pos-1-1", "br-ts-pos-1-1"]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_paper_mainnet_rule_gets_the_local_native_price_as_its_own_source(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    await native_armed(h)
    assert "native_trailing" not in [s for s, _ in h.adapter.paper_stop_levels("pos-1")]
    h.mark_value = 101.0 if side is Side.LONG else 99.0
    await h.adapter.protection_tick()
    levels = h.adapter.paper_stop_levels("pos-1")
    want = 100.495 if side is Side.LONG else 99.495
    assert [p for s, p in levels if s == "native_trailing"] == [pytest.approx(want)]
    assert "trailing" not in [s for s, _ in levels]  # 程序自己没激活过
    assert "lifeboat" not in [s for s, _ in levels]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_existing_position_cancels_its_lifeboat_only_after_native_is_confirmed(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side, native=False)
    await h.armed()
    assert h.live_cids() == ["br-sl-pos-1-1", "lb-pos-1-1"]
    h.native = True  # 部署后开关打开
    h.native_fail = "unknown"
    await h.adapter.protection_tick()
    assert "lb-pos-1-1" in h.live_cids()  # 没确认挂上，保命单不撤
    h2 = NativeHarness(tmp_path / "b", side, native=False)
    await h2.armed()
    h2.native = True
    await h2.adapter.protection_tick()
    events = h2.exchange.events
    assert events.index(("create", "br-ts-pos-1-1")) < events.index(("cancel", "lb-pos-1-1"))
    assert h2.live_cids() == ["br-sl-pos-1-1", "br-ts-pos-1-1"]
    assert lifeboat_summary(panel(h2)) == ("native", None)


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_switching_off_moves_the_stop_to_the_native_price_then_cancels_it(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    await native_armed(h)
    h.mark_value = 101.0 if side is Side.LONG else 99.0
    await h.adapter.protection_tick()
    h.native = False
    await h.manager.sync("pos-1")
    events = h.exchange.events
    assert events.index(("create", "lb-pos-1-2")) < events.index(("cancel", "br-ts-pos-1-1"))
    stop = h.exchange.orders["br-sl-pos-1-2"]
    # 止损腿先收到交易所移动止损推算价（标记价外 5 基点缓冲内照挂）
    assert stop.trigger_price == (100.5 if side is Side.LONG else 99.5)
    assert sorted(h.live_cids()) == ["br-sl-pos-1-2", "lb-pos-1-2"]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_unreadable_native_leg_falls_back_without_failing_the_sync(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    await native_armed(h)
    original = h.exchange.get_bracket

    async def broken(symbol, *, algo_id=None, client_order_id=None):
        if (client_order_id or "").startswith("br-ts-"):
            raise ExchangeError("synthetic parse failure")
        return await original(symbol, algo_id=algo_id, client_order_id=client_order_id)

    h.exchange.get_bracket = broken
    assert await h.manager.sync("pos-1") == "armed"
    assert h.state().native_fallback is None  # 一次读不好先不放弃（Demo 读延迟）
    assert h.live_cids() == ["br-sl-pos-1-1", "br-ts-pos-1-1"]
    h.now += timedelta(seconds=30)
    assert await h.manager.sync("pos-1") == "armed"
    assert "回读失败" in (h.state().native_fallback or "")
    assert any(c.startswith("lb-") for c in h.live_cids()) and not h.closed()


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_abandoned_unknown_native_leg_is_settled_by_flat_cleanup(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    h.native_fail = "unknown"
    await native_armed(h)
    assert h.adapter.protection_exit_in_flight("pos-1") is None  # 不算「正在换代」
    calls: list[str | None] = []
    original = h.exchange._get

    def counting(algo_id, client_order_id):
        calls.append(client_order_id)
        return original(algo_id, client_order_id)

    h.exchange._get = counting
    events = len(h.exchange.events)
    await h.manager.sync("pos-1")
    # 没改单的一轮：每条腿回读一次，不走换代、不去撤它
    assert calls == ["br-sl-pos-1-1", "br-ts-pos-1-1", "lb-pos-1-2"]
    assert h.exchange.events[events:] == []
    h.exchange.held = 0.0
    h.manager.mark_exiting("pos-1")
    assert await h.manager.flat_cleanup("pos-1") is True
    legs = {leg.client_order_id: leg.state for leg in h.state().legs}
    assert legs["br-ts-pos-1-1"] == "rejected" and h.state().stage == "done"


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_unknown_native_leg_found_live_at_cleanup_is_cancelled(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    h.native_fail = "unknown"
    await native_armed(h)
    # 提交其实成功了，只是回执丢了，之后才查得到
    order = ExchangeBracket(
        symbol="BTCUSDT",
        algo_id="99",
        client_order_id="br-ts-pos-1-1",
        kind="trailing_stop",
        close_side="sell" if side is Side.LONG else "buy",
        quantity=2.0,
        trigger_price=1.0,
        position_side="BOTH",
        working_type="MARK_PRICE",
        reduce_only=True,
        price_protect=False,
        close_position=False,
        algo_status="NEW",
        child_state="absent",
        activate_price=1.0,
        callback_rate=0.9,
    )
    h.exchange.orders["br-ts-pos-1-1"] = order
    h.exchange.held = 0.0
    h.manager.mark_exiting("pos-1")
    assert await h.manager.flat_cleanup("pos-1") is True
    assert "br-ts-pos-1-1" in h.cancels()
    assert {leg.client_order_id: leg.state for leg in h.state().legs}["br-ts-pos-1-1"] == (
        "cancelled"
    )


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_limit_entry_still_filling_keeps_a_lifeboat_until_native_is_placed(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    h.exchange.entry = replace(h.exchange.entry, filled_quantity=1, status="PARTIALLY_FILLED")
    h.exchange.held = 1
    assert await h.manager.sync("pos-1") == "provisional"
    assert sorted(c[:3] for c in h.live_cids()) == ["br-", "lb-"]
    h.exchange.entry = replace(h.exchange.entry, filled_quantity=2, status="FILLED")
    h.exchange.held = 2
    await h.adapter.protection_tick()
    assert h.manager.native_mode("pos-1") == "confirmed"
    assert not [c for c in h.live_cids() if c.startswith("lb-")]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_opposite_card_anchor_hands_trailing_back_to_us(tmp_path: Path, side: Side) -> None:
    h = NativeHarness(tmp_path, side)
    await native_armed(h)
    basis = await h.entry_basis("BTCUSDT", "entry-1")
    h.mark_value = 100.25 if side is Side.LONG else 99.75
    h.bid, h.ask = (100.2, 100.3) if side is Side.LONG else (99.7, 99.8)
    async with h.registry.symbol_lock("BTCUSDT"):
        await h.manager.anchor_trailing(
            "pos-1", basis, h.execution_book, distance_mult=2.0, reason="反向卡"
        )
    assert h.state().native_fallback == "反向卡锚定移动止损，改回程序自己跟"
    await h.manager.sync("pos-1")
    assert "br-ts-pos-1-1" in h.cancels() and any(c.startswith("lb-") for c in h.live_cids())
    h.bid, h.ask = (100.6, 100.7) if side is Side.LONG else (99.3, 99.4)
    h.mark_value = 100.65 if side is Side.LONG else 99.35
    reads = len(h.book_reads)
    await h.adapter.protection_tick()
    assert len(h.book_reads) > reads  # 程序接着自己跟


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_open_check_accepts_no_lifeboat_for_a_native_position(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    await h.armed()
    tracked = h.registry.get("pos-1")
    assert h.adapter._stop_first_covered(tracked) is True
    h.native = False
    assert h.adapter._stop_first_covered(tracked) is False


def stream_events(cid: str, close_side: str, at: int) -> list[dict[str, Any]]:
    return [
        {
            "e": "ALGO_UPDATE",
            "T": at,
            "E": at + 1,
            "o": {"caid": cid, "s": "BTCUSDT", "X": "TRIGGERED", "ai": "777"},
        },
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": at + 3,
            "T": at,
            "o": {
                "s": "BTCUSDT",
                "c": "exchange-child",
                "S": close_side.upper(),
                "q": "2",
                "ap": "100.5",
                "x": "TRADE",
                "X": "FILLED",
                "i": 777,
                "l": "2",
                "z": "2",
                "L": "100.5",
                "N": "USDT",
                "n": "0.1",
                "T": at,
                "t": 1,
                "R": True,
                "ps": "BOTH",
                "rp": "1.0",
            },
        },
        {
            "e": "ACCOUNT_UPDATE",
            "E": at + 4,
            "T": at + 4,
            "a": {"P": [{"s": "BTCUSDT", "pa": "0", "ps": "BOTH"}]},
        },
    ]


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_native_fill_from_the_stream_closes_as_trailing_stop_and_cancels_the_rest(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    await native_armed(h)
    h.exchange.held = 0.0
    at = int((h.now + timedelta(seconds=5)).timestamp() * 1000)
    for msg in stream_events("br-ts-pos-1-1", "sell" if side is Side.LONG else "buy", at):
        h.adapter.on_stream_event(msg)
    await asyncio.gather(*list(h.adapter._stream_tasks))
    rec = h.registry.get("pos-1").record
    assert rec.status is PositionStatus.CLOSED and rec.close_reason is CloseReason.TRAILING_STOP
    assert h.state().exit_reason is CloseReason.TRAILING_STOP
    assert h.cancels() == ["br-sl-pos-1-1"]


def panel(h: NativeHarness):
    raw = json.loads(h.path.read_text())["positions"]["pos-1"]["server_protection"]
    view = parse_protection(raw, h.now, 0, long=h.long, activation_pct=0.3)
    assert view is not None
    return view


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
async def test_panel_shows_the_native_leg_and_does_not_count_the_lifeboat_missing(
    tmp_path: Path, side: Side
) -> None:
    h = NativeHarness(tmp_path, side)
    await native_armed(h)
    h.mark_value = 101.0 if side is Side.LONG else 99.0
    await h.adapter.protection_tick()
    view = panel(h)
    leg = next(x for x in view.legs if x.role == "trailing_native")
    assert (leg.state, leg.callback_rate) == ("live", 0.5)
    assert leg.activate_price == (100.4 if side is Side.LONG else 99.6)
    assert view.native_trailing and view.native_activated
    assert view.native_stop_price == pytest.approx(100.495 if side is Side.LONG else 99.495)
    assert lifeboat_summary(view) == ("native", None)


async def test_fallback_position_shows_its_lifeboat_not_native(tmp_path: Path) -> None:
    h = NativeHarness(tmp_path, Side.LONG)
    h.native_fail = "reject"
    await native_armed(h)
    view = panel(h)
    assert not view.native_trailing and view.native_fallback is not None
    assert lifeboat_summary(view) == ("active", 98.5)
    assert not [x for x in view.legs if x.role == "trailing_native"]  # 被拒的不显示


async def test_switch_off_ledger_carries_no_new_fields(tmp_path: Path) -> None:
    h = NativeHarness(tmp_path, Side.LONG, native=False)
    await native_armed(h)
    raw = json.loads(h.path.read_text())["positions"]["pos-1"]["server_protection"]
    assert not [k for k in raw if k.startswith("native_")]
    assert all("callback_rate" not in x and "activate_price" not in x for x in raw["legs"])


def load_rollback():
    path = Path(__file__).parents[1] / "scripts" / "native_trailing_rollback.py"
    spec = importlib.util.spec_from_file_location("native_trailing_rollback", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_rollback_script_refuses_while_a_native_leg_may_be_on_the_exchange(
    tmp_path: Path,
) -> None:
    h = NativeHarness(tmp_path, Side.LONG)
    await native_armed(h)
    rollback = load_rollback()
    before = h.path.read_text()
    assert rollback.main([str(h.path), "--write"]) == 1
    assert h.path.read_text() == before
    h.native = False
    await h.manager.sync("pos-1")  # 关掉：补保命单、撤交易所移动止损
    assert rollback.main([str(h.path), "--write"]) == 0
    raw = json.loads(h.path.read_text())["positions"]["pos-1"]["server_protection"]
    assert not [x for x in raw["legs"] if x["kind"] == "trailing_stop"]
    assert not [k for k in raw if k.startswith("native_")]
