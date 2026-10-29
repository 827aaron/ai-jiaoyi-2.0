"""成交推送收尾：交易所托管腿的子单成交后，按用户数据流的推送当场记平，不等 hummingbot-api / REST。

账本取自 Demo 2026-10-29 1000PEPEUSDT 空仓 pos-44fb94e7e407 触发前的落盘：第 7 代移动止损
br-sl-pos-44fb94e7e407-7 在 22:11:55.080Z 触发、子单 904783683 一笔全成 323802 @ 0.0045603。
那一晚 hummingbot-api 和 demo-fapi 都在超时，系统 22:16:34 才记平、22:19:44 才有盈亏。
推送字段按币安 USDⓈ-M 用户数据流文档的 ORDER_TRADE_UPDATE / ALGO_UPDATE / ACCOUNT_UPDATE 构造。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.contracts import Side
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.brackets import BracketManager
from jiaoyi.execution.exchange import (
    ExchangeBracket,
    ExchangeError,
    ExchangePosition,
    IdentifiedCloseOrder,
)
from jiaoyi.execution.exit_fills import ExitFills
from jiaoyi.execution.fills import TradeFill
from jiaoyi.execution.hb_client import HbApiError
from jiaoyi.execution.hb_executor import HbExecutorAdapter
from jiaoyi.execution.lifeboat import BinanceLifeboat
from jiaoyi.execution.registry import PositionRegistry, TrackedPosition
from jiaoyi.execution.stream_close import StreamBook, evaluate
from jiaoyi.execution.user_stream import UserStream
from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.pages import build_history
from tests.fakes import FakeExchange, FakeHbApi
from tests.support.builders import risk_config

LEDGER = Path(__file__).parent / "fixtures" / "pepe_trailing_fill_ledger.json"
PID = "pos-44fb94e7e407"
SYMBOL = "1000PEPEUSDT"
QTY = 323802.0
ENTRY = Decimal("0.0048697")
EXIT = Decimal("0.0045603")
G7 = "br-sl-pos-44fb94e7e407-7"
G6 = "br-sl-pos-44fb94e7e407-6"
LB = "lb-pos-44fb94e7e407-1"
CHILD = "904783683"
FILL_MS = 1793311915080
NOW = datetime(2026, 10, 29, 22, 11, 55, 200000, tzinfo=UTC)
GROSS = float((ENTRY - EXIT) * Decimal(323802))
EXIT_FEE = 0.5906537
ENTRY_FEE = 0.63072743


def ledger() -> dict[str, Any]:
    return json.loads(LEDGER.read_text())


def trade(
    *,
    order_id: str = CHILD,
    status: str = "FILLED",
    last: float = QTY,
    cum: float = QTY,
    price: str = str(EXIT),
    fee: str = str(EXIT_FEE),
    rp: float = GROSS,
    tid: int = 5550001,
    at: int = FILL_MS,
) -> dict[str, Any]:
    return {
        "e": "ORDER_TRADE_UPDATE",
        "E": at + 3,
        "T": at,
        "o": {
            "s": SYMBOL,
            "c": "autoclose-algo-child",
            "S": "BUY",
            "o": "MARKET",
            "ot": "STOP_MARKET",
            "f": "GTC",
            "q": str(QTY),
            "p": "0",
            "ap": price,
            "sp": "0.0045928",
            "x": "TRADE",
            "X": status,
            "i": int(order_id),
            "l": str(last),
            "z": str(cum),
            "L": price,
            "N": "USDT",
            "n": fee,
            "T": at,
            "t": tid,
            "R": True,
            "ps": "BOTH",
            "wt": "MARK_PRICE",
            "rp": str(rp),
        },
    }


def algo(cid: str = G7, status: str = "TRIGGERED", actual: str = CHILD, at: int = FILL_MS) -> dict:
    return {
        "e": "ALGO_UPDATE",
        "T": at,
        "E": at + 1,
        "o": {
            "caid": cid,
            "aid": 1000000264458388,
            "at": "CONDITIONAL",
            "o": "STOP_MARKET",
            "s": SYMBOL,
            "S": "BUY",
            "ps": "BOTH",
            "q": str(QTY),
            "X": status,
            "ai": actual,
            "tp": "0.0045928",
            "wt": "MARK_PRICE",
            "R": True,
        },
    }


def account(amount: str = "0", at: int = FILL_MS + 1) -> dict[str, Any]:
    return {
        "e": "ACCOUNT_UPDATE",
        "E": at,
        "T": at,
        "a": {
            "m": "ORDER",
            "B": [{"a": "USDT", "wb": "5000", "cw": "5000", "bc": "0"}],
            "P": [{"s": SYMBOL, "pa": amount, "ep": "0", "up": "0", "mt": "cross", "ps": "BOTH"}],
        },
    }


class SlowVenue:
    """Demo 那一晚：REST 全部超时（``down``）；恢复后按真实形状回读、撤单。"""

    def __init__(self) -> None:
        self.down = True
        self.held = 0.0
        self.calls: list[tuple[str, str]] = []
        self.algos: dict[str, str] = {LB: "NEW", G7: "NEW"}

    def _gate(self, what: str, key: str) -> None:
        self.calls.append((what, key))
        if self.down:
            raise ExchangeError(f"{what} RequestTimeout", transient=True)

    def _leg(self, cid: str) -> ExchangeBracket:
        lifeboat = cid == LB
        status = self.algos.get(cid, "CANCELED")
        triggered = cid == G7 and status == "FINISHED"
        return ExchangeBracket(
            symbol=SYMBOL,
            algo_id="1000000264430325" if lifeboat else "1000000264458388",
            client_order_id=cid,
            kind="stop_loss",
            close_side="buy",
            quantity=QTY,
            trigger_price=0.0051758 if lifeboat else 0.0045928,
            position_side="BOTH",
            working_type="MARK_PRICE",
            reduce_only=True,
            price_protect=False,
            close_position=False,
            algo_status=status,
            actual_order_id=CHILD if triggered else None,
            actual_order_status="FILLED" if triggered else None,
            filled_quantity=QTY if triggered else None,
            triggered_at=NOW if triggered else None,
            child_state="identified" if triggered else "absent",
        )

    async def get_bracket(self, symbol, *, algo_id=None, client_order_id=None):
        self._gate("get_bracket", str(client_order_id or algo_id))
        return self._leg(client_order_id or G7)

    async def get_lifeboat(self, symbol, *, client_order_id, algo_id=None):
        self._gate("get_lifeboat", client_order_id)
        return self._leg(client_order_id)

    async def cancel_bracket(self, symbol, algo_id, *, expected_client_order_id) -> None:
        self._gate("cancel", expected_client_order_id)
        self.algos[expected_client_order_id] = "CANCELED"

    async def cancel_lifeboat(self, symbol, algo_id, *, expected_client_order_id) -> None:
        self._gate("cancel", expected_client_order_id)
        self.algos[expected_client_order_id] = "CANCELED"

    async def positions_evidence(self) -> list[ExchangePosition]:
        self._gate("positions", SYMBOL)
        return [ExchangePosition(SYMBOL, Side.SHORT, self.held)] if self.held else []

    async def close_positions_evidence(self) -> list[ExchangePosition]:
        return await self.positions_evidence()

    async def place_close(self, symbol, close_side, quantity, client_order_id):
        self._gate("direct", client_order_id)
        return None

    async def entry_order(self, symbol: str, client_order_id: str):
        self._gate("entry_order", client_order_id)
        raise AssertionError("not reached while down")

    def did(self, what: str) -> list[str]:
        return [key for action, key in self.calls if action == what]


def build_on(tmp_path: Path, *, registry: PositionRegistry) -> HbExecutorAdapter:
    return build(tmp_path, registry=registry)[2]


def build(
    tmp_path: Path,
    data: dict[str, Any] | None = None,
    *,
    registry: PositionRegistry | None = None,
):
    if registry is None:
        registry = PositionRegistry(tmp_path / "positions.json")
        registry.put(TrackedPosition.model_validate(data or ledger()))
    venue = SlowVenue()
    manager = BracketManager(
        venue,  # type: ignore[arg-type]
        registry,
        account_name="master_account",
        connector_name="binance_perpetual_testnet",
        clock=lambda: NOW,
    )
    api = FakeHbApi()
    api.down = True
    alerter = StubAlerter()
    adapter = HbExecutorAdapter(
        api,
        BinanceLifeboat(FakeExchange(), clock=lambda: NOW),
        registry,
        alerter,
        account_name="master_account",
        connector_name="binance_perpetual_testnet",
        exchange=venue,  # type: ignore[arg-type]
        protection=manager,
        clock=lambda: NOW,
        exit_fills=ExitFills(tmp_path / "exit_fills.json"),
    )
    return registry, venue, adapter, alerter, api


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def leg(registry: PositionRegistry, cid: str):
    tracked = registry.get(PID)
    assert tracked is not None and tracked.server_protection is not None
    return next(x for x in tracked.server_protection.legs if x.client_order_id == cid)


async def test_pepe_trailing_fill_closes_from_the_stream_while_rest_and_hb_time_out(
    tmp_path: Path,
) -> None:
    registry, venue, adapter, alerter, api = build(tmp_path)
    seen: list[str] = []
    adapter.on_position_closed = lambda rec: seen.append(rec.position_id)

    adapter.on_stream_event(algo())
    adapter.on_stream_event(trade())
    assert registry.get(PID).record.status is PositionStatus.OPEN  # 还没看到净仓为 0
    adapter.on_stream_event(account())

    rec = registry.get(PID).record
    assert rec.status is PositionStatus.CLOSED
    assert rec.close_reason is CloseReason.TRAILING_STOP
    assert rec.exit_price == pytest.approx(0.0045603, rel=1e-12)
    assert rec.fees_usdt == pytest.approx(EXIT_FEE + ENTRY_FEE, abs=1e-9)
    # 和 22:19:44 成交明细回填出来的一样
    assert rec.realized_pnl_usdt == pytest.approx(98.96295767, abs=1e-6)
    assert seen == [PID]
    tracked = registry.get(PID)
    assert tracked.exit_cleanup_pending is True
    g7 = leg(registry, G7)
    assert (g7.actual_order_id, g7.actual_order_status, g7.filled_quantity) == (
        CHILD,
        "FILLED",
        QTY,
    )
    assert tracked.server_protection.stage == "exiting"
    assert tracked.server_protection.exit_reason is CloseReason.TRAILING_STOP

    await settle()
    assert venue.did("cancel") == [LB]  # 马上去撤保命单；REST 超时，留给收尾
    assert [a.title for a in alerter.sent] == ["平仓完成"]
    assert api.stopped == []
    assert registry.get(PID).record.status is PositionStatus.CLOSED
    raw = json.loads((tmp_path / "exit_fills.json").read_text())
    assert raw[PID]["at"].startswith("2026-10-29T22:11:55.080")


async def test_close_shows_up_in_the_panel_files_with_pnl(tmp_path: Path) -> None:
    _, _, adapter, _, _ = build(tmp_path)
    for msg in (algo(), trade(), account()):
        adapter.on_stream_event(msg)
    files = PanelFiles(tmp_path, "UTC")
    led = files.ledger()
    assert all(p.position_id != PID for p in led.positions if p.status is not PositionStatus.CLOSED)
    page = build_history(files, risk_config(), NOW, "today", include_tests=True)
    row = next(r for r in page.rows if r.position_id == PID)
    assert row.exit_price == pytest.approx(0.0045603, rel=1e-12)
    assert row.pnl_usdt == pytest.approx(98.96295767, abs=1e-6)
    assert row.pnl_missing is False
    assert row.exit_filled_at is not None


async def test_child_fill_before_algo_update_links_once_the_algo_update_arrives(
    tmp_path: Path,
) -> None:
    registry, venue, adapter, _, _ = build(tmp_path)
    adapter.on_stream_event(trade())
    adapter.on_stream_event(account())
    assert registry.get(PID).record.status is PositionStatus.OPEN
    await settle()  # 对不上号：按 REST 补读一次，超时
    assert set(venue.did("get_lifeboat")) | set(venue.did("get_bracket")) == {LB, G7}
    adapter.on_stream_event(algo())
    assert registry.get(PID).record.status is PositionStatus.CLOSED


async def test_child_fill_is_linked_by_a_rest_read_when_no_algo_update_comes(
    tmp_path: Path,
) -> None:
    registry, venue, adapter, _, _ = build(tmp_path)
    venue.down = False
    venue.algos[G7] = "FINISHED"
    adapter.on_stream_event(trade())
    adapter.on_stream_event(account())
    await settle()
    assert registry.get(PID).record.status is PositionStatus.CLOSED


async def test_missing_account_update_falls_back_to_one_rest_position_read(
    tmp_path: Path,
) -> None:
    registry, venue, adapter, _, _ = build(tmp_path)
    adapter.on_stream_event(algo())
    adapter.on_stream_event(trade())
    await settle()
    assert venue.did("positions") == [SYMBOL]  # 超时：不记平
    assert registry.get(PID).record.status is PositionStatus.OPEN
    venue.down = False
    adapter.on_stream_event(trade())  # 重复推送不再触发第二次 REST
    await settle()
    assert venue.did("positions") == [SYMBOL]
    assert registry.get(PID).record.status is PositionStatus.OPEN
    adapter.on_stream_event(account())
    assert registry.get(PID).record.status is PositionStatus.CLOSED


async def test_rest_position_read_of_zero_closes_without_account_update(tmp_path: Path) -> None:
    registry, venue, adapter, _, _ = build(tmp_path)
    venue.down = False
    adapter.on_stream_event(algo())
    adapter.on_stream_event(trade())
    await settle()
    assert registry.get(PID).record.status is PositionStatus.CLOSED


async def test_account_update_still_holding_does_not_close(tmp_path: Path) -> None:
    registry, _, adapter, _, _ = build(tmp_path)
    for msg in (algo(), trade(), account("-323802")):
        adapter.on_stream_event(msg)
    assert registry.get(PID).record.status is PositionStatus.OPEN


async def test_partial_fill_only_marks_the_leg_and_closes_on_the_final_fill(
    tmp_path: Path,
) -> None:
    registry, _, adapter, _, _ = build(tmp_path)
    first = 100000.0
    adapter.on_stream_event(algo())
    adapter.on_stream_event(
        trade(status="PARTIALLY_FILLED", last=first, cum=first, fee="0.18", rp=30.94, tid=1)
    )
    adapter.on_stream_event(account(f"-{QTY - first:g}"))
    tracked = registry.get(PID)
    assert tracked.record.status is PositionStatus.OPEN
    g7 = leg(registry, G7)
    assert (g7.actual_order_status, g7.filled_quantity) == ("PARTIALLY_FILLED", first)
    assert tracked.server_protection.stage == "exiting"

    adapter.on_stream_event(
        trade(last=QTY - first, cum=QTY, fee="0.4106537", rp=GROSS - 30.94, tid=2, at=FILL_MS + 4)
    )
    adapter.on_stream_event(account(at=FILL_MS + 5))
    rec = registry.get(PID).record
    assert rec.status is PositionStatus.CLOSED
    assert rec.fees_usdt == pytest.approx(EXIT_FEE + ENTRY_FEE, abs=1e-9)
    assert rec.realized_pnl_usdt == pytest.approx(98.96295767, abs=1e-6)


async def test_duplicate_events_count_once_and_close_once(tmp_path: Path) -> None:
    registry, venue, adapter, alerter, _ = build(tmp_path)
    for _ in range(2):
        for msg in (algo(), trade(), trade(), account(), algo(status="FINISHED")):
            adapter.on_stream_event(msg)
        await settle()
    rec = registry.get(PID).record
    assert rec.fees_usdt == pytest.approx(EXIT_FEE + ENTRY_FEE, abs=1e-9)
    assert rec.realized_pnl_usdt == pytest.approx(98.96295767, abs=1e-6)
    assert [a.title for a in alerter.sent] == ["平仓完成"]
    assert venue.did("cancel") == [LB]


async def test_fill_of_an_older_generation_still_closes_with_its_own_leg(tmp_path: Path) -> None:
    data = ledger()
    g6 = data["server_protection"]["legs"][6]
    g6.update(state="cancel_pending", algo_status="NEW")  # 换代撤旧单和触发撞在一起
    registry, venue, adapter, _, _ = build(tmp_path, data)
    venue.down = False
    for msg in (algo(G6, actual="904783600"), trade(order_id="904783600"), account()):
        adapter.on_stream_event(msg)
    rec = registry.get(PID).record
    assert rec.status is PositionStatus.CLOSED
    assert rec.close_reason is CloseReason.TRAILING_STOP
    assert leg(registry, G6).actual_order_id == "904783600"
    await settle()
    assert sorted(venue.did("cancel")) == sorted([LB, G7])  # 新一代和保命单都撤
    assert leg(registry, LB).state == "cancelled"


async def test_fill_that_does_not_cover_the_entry_quantity_is_left_to_polling(
    tmp_path: Path,
) -> None:
    data = ledger()
    data["server_protection"]["entry_filled_quantity"] = QTY * 2
    registry, _, adapter, _, _ = build(tmp_path, data)
    for msg in (algo(), trade(), account()):
        adapter.on_stream_event(msg)
    assert registry.get(PID).record.status is PositionStatus.OPEN


async def test_fill_before_the_position_opened_is_not_ours(tmp_path: Path) -> None:
    registry, venue, adapter, _, _ = build(tmp_path)
    old = trade(order_id="904000001", at=FILL_MS - 3_600_000)
    adapter.on_stream_event(old)
    await settle()
    assert venue.calls == []
    assert registry.get(PID).record.status is PositionStatus.OPEN


async def test_inflight_close_cannot_reopen_a_stream_closed_position(tmp_path: Path) -> None:
    registry, _, adapter, _, _ = build(tmp_path)
    stale = registry.get(PID)
    for msg in (algo(), trade(), account()):
        adapter.on_stream_event(msg)
    adapter._set(stale, status=PositionStatus.CLOSING, requested_close=CloseReason.STOP_LOSS)
    done = registry.get(PID)
    registry.put(
        done.model_copy(
            update={
                "server_protection": done.server_protection.model_copy(update={"stage": "done"})
            }
        )
    )  # 收尾核完之后，旧流程的直连记账只剩缺数的回读
    adapter.mark_closed_directly(PID, CloseReason.STOP_LOSS)
    rec = registry.get(PID).record
    assert rec.status is PositionStatus.CLOSED
    assert rec.realized_pnl_usdt == pytest.approx(98.96295767, abs=1e-6)


async def test_entry_fee_unknown_still_closes_and_asks_backfill_to_refine(tmp_path: Path) -> None:
    data = ledger()
    data["server_protection"]["trailing_fees_quote"] = None
    registry, _, adapter, _, _ = build(tmp_path, data)
    for msg in (algo(), trade(), account()):
        adapter.on_stream_event(msg)
    rec = registry.get(PID).record
    assert rec.status is PositionStatus.CLOSED
    assert rec.realized_pnl_usdt == pytest.approx(GROSS - EXIT_FEE, abs=1e-6)
    # 落盘：重启之后回填照样会再核
    assert PositionRegistry(tmp_path / "positions.json").get(PID).stream_refine is True

    async def trades(symbol: str, since: datetime) -> list[TradeFill]:
        return [
            TradeFill(
                datetime(2026, 10, 29, 21, 47, 27, tzinfo=UTC),
                "904770017",
                "sell",
                float(ENTRY),
                QTY,
                ENTRY_FEE,
                0.0,
            ),
            TradeFill(
                datetime(2026, 10, 29, 22, 11, 55, 80000, tzinfo=UTC),
                CHILD,
                "buy",
                float(EXIT),
                QTY,
                EXIT_FEE,
                GROSS,
            ),
        ]

    restarted = build_on(tmp_path, registry=PositionRegistry(tmp_path / "positions.json"))
    assert await restarted.backfill_fills(trades) == 1
    tracked = PositionRegistry(tmp_path / "positions.json").get(PID)
    assert tracked.stream_refine is False
    assert tracked.record.realized_pnl_usdt == pytest.approx(98.96295767, abs=1e-6)
    assert await restarted.backfill_fills(trades) == 0  # 只再核一次


async def test_complete_fees_are_not_marked_for_refine(tmp_path: Path) -> None:
    registry, _, adapter, _, _ = build(tmp_path)
    for msg in (algo(), trade(), account()):
        adapter.on_stream_event(msg)
    assert registry.get(PID).stream_refine is False
    assert "stream_refine" not in (tmp_path / "positions.json").read_text()


async def test_cleanup_waits_for_an_unverified_direct_close_order(tmp_path: Path) -> None:
    data = ledger()
    data.update(
        direct_close_pending=True,
        direct_close_client_order_id="dc-pos-44fb94e7e407-1",
        direct_close_generation=1,
        direct_close_quantity=QTY,
        direct_close_order_status="SUBMIT_PENDING",
    )
    registry, venue, adapter, _, _ = build(tmp_path, data)
    for msg in (algo(), trade(), account()):
        adapter.on_stream_event(msg)
    assert registry.get(PID).record.status is PositionStatus.CLOSED
    ok = AsyncMock(return_value=True)
    adapter.entry_finished = ok  # type: ignore[method-assign]
    adapter._hb_exit_finished = ok  # type: ignore[method-assign]
    adapter._protection.flat_cleanup = ok  # type: ignore[method-assign]
    venue.down = False
    assert await adapter._finish_exit_cleanup(PID) is False  # 直连单结果未明
    assert registry.get(PID).exit_cleanup_pending is True
    assert adapter.exit_pending_symbols() == {SYMBOL}

    async def close_order(symbol: str, cid: str) -> IdentifiedCloseOrder:
        return IdentifiedCloseOrder(
            symbol=SYMBOL,
            client_order_id=cid,
            order_id="904783700",
            close_side="buy",
            quantity=QTY,
            filled_quantity=0.0,
            remaining_quantity=QTY,
            average_price=None,
            status="EXPIRED",
            position_side="BOTH",
            reduce_only=True,
            is_terminal=True,
        )

    venue.close_order = close_order  # type: ignore[attr-defined]
    assert await adapter._finish_exit_cleanup(PID) is True
    assert registry.get(PID).exit_cleanup_pending is False
    assert adapter.exit_pending_symbols() == set()


async def test_cancel_after_flat_leaves_legs_to_cleanup_while_the_symbol_lock_is_held(
    tmp_path: Path,
) -> None:
    registry, venue, adapter, _, _ = build(tmp_path)
    venue.down = False
    held, release = asyncio.Event(), asyncio.Event()

    async def rearm() -> None:
        async with registry.symbol_lock(SYMBOL):
            held.set()
            await release.wait()

    task = asyncio.create_task(rearm())
    await held.wait()
    for msg in (algo(), trade(), account()):
        adapter.on_stream_event(msg)
    await settle()
    assert registry.get(PID).record.status is PositionStatus.CLOSED
    assert venue.did("cancel") == []
    assert not adapter._stream_tasks  # 没在等锁：直接让给收尾
    release.set()
    await task
    await adapter._protection.cancel_after_flat(PID)
    assert venue.did("cancel") == [LB]


def test_stream_book_ignores_stale_order_updates() -> None:
    book = StreamBook()
    book.ingest(trade())
    book.ingest(trade(status="PARTIALLY_FILLED", last=1.0, cum=1.0, tid=9))
    fills = book.order(CHILD)
    assert fills is not None
    assert (fills.status, fills.filled) == ("FILLED", QTY)


def test_evaluate_needs_entry_terminal() -> None:
    data = ledger()
    data["server_protection"]["entry_terminal"] = False
    book = StreamBook()
    for msg in (algo(), trade(), account()):
        book.ingest(msg)
    assert evaluate(TrackedPosition.model_validate(data), book) is None


class _Socket:
    def __init__(self, msgs: list[dict[str, Any]]) -> None:
        self._msgs = [json.dumps(m) for m in msgs]

    async def __aenter__(self) -> _Socket:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __aiter__(self) -> _Socket:
        return self

    async def __anext__(self) -> str:
        if not self._msgs:
            raise StopAsyncIteration
        return self._msgs.pop(0)


async def test_user_stream_hands_evidence_events_to_the_executor(tmp_path: Path) -> None:
    registry, _, adapter, _, _ = build(tmp_path)
    woke: list[float] = []

    async def on_fill() -> None:
        woke.append(time.monotonic())

    async def nothing() -> None:
        return None

    async def key() -> str:
        return "k"

    stream = UserStream(
        key,
        nothing,
        on_fill,
        ws_base="wss://example.invalid",
        on_event=adapter.on_stream_event,
    )
    await stream._read(_Socket([algo(), trade(), account()]))
    assert registry.get(PID).record.status is PositionStatus.CLOSED
    assert len(woke) == 2  # ORDER_TRADE_UPDATE + ACCOUNT_UPDATE still wake the ledger backfill


async def test_hb_api_down_does_not_block_the_stream_close(tmp_path: Path) -> None:
    registry, _, adapter, _, api = build(tmp_path)
    with pytest.raises(HbApiError):
        await api.get_executor("FwPtErAdvgFZcowTV2jNAUdB23k4m1vtMBoFNovCQVRq")
    await adapter.protection_tick()  # 那一晚的样子：这一轮什么都确认不了
    assert registry.get(PID).record.status is PositionStatus.OPEN
    for msg in (algo(), trade(), account()):
        adapter.on_stream_event(msg)
    assert registry.get(PID).record.status is PositionStatus.CLOSED
    await adapter.protection_tick()  # 收尾照旧在后台重试，不改已记的平仓
    assert registry.get(PID).record.status is PositionStatus.CLOSED
    assert registry.get(PID).exit_cleanup_pending is True


async def test_close_in_flight_when_the_stream_closes_reports_success(tmp_path: Path) -> None:
    registry, _, adapter, _, _ = build(tmp_path)

    async def stream_closes_meanwhile(*args: Any, **kwargs: Any) -> bool:
        for msg in (algo(), trade(), account()):
            adapter.on_stream_event(msg)
        return False  # 那一轮的 REST 都超时了

    adapter._close_directly_locked = stream_closes_meanwhile  # type: ignore[method-assign]
    assert await adapter._close_directly(registry.get(PID), CloseReason.KEEPER, None) is True


async def test_direct_close_submit_with_a_stale_copy_neither_sends_nor_overwrites(
    tmp_path: Path,
) -> None:
    registry, venue, adapter, _, _ = build(tmp_path)
    stale = registry.get(PID)
    for msg in (algo(), trade(), account()):
        adapter.on_stream_event(msg)
    assert await adapter._submit_direct_close(stale, QTY) is None
    assert venue.did("direct") == []
    tracked = registry.get(PID)
    assert tracked.record.status is PositionStatus.CLOSED
    assert tracked.exit_cleanup_pending is True
    assert tracked.direct_close_generation == 0
