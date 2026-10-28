"""账本按币安成交回填、结账只做一次、用户数据流。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi.contracts.position import PositionStatus
from jiaoyi.execution.exit_fills import ExitFills, read_exit_fills
from jiaoyi.execution.fills import TradeFill, entry_fill, exit_fill
from jiaoyi.execution.ledger_sync import LedgerSync
from jiaoyi.execution.user_stream import StreamStatus, UserStream, is_fill, ws_urls
from tests.test_execution import START, Rig, open_filled


def _t(
    sec: float, side: str, price: float, qty: float, fee: float | None, pnl: float = 0.0
) -> TradeFill:
    return TradeFill(START + timedelta(seconds=sec), "o1", side, price, qty, fee, pnl)


def test_entry_and_exit_vwap_from_trades(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    pid = asyncio.run(open_filled(rig, price=68044.0))
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    q = rec.quantity
    trades = [
        _t(-60, "sell", 1.0, q, 0.1),  # 开仓前的别的单，不算
        _t(1, "buy", 68000.0, q / 3, 0.1),
        _t(2, "buy", 68030.0, 2 * q / 3, 0.2),
        _t(600, "sell", 68100.0, q, 0.3, pnl=0.9),
    ]
    e = entry_fill(rec, trades)
    assert e is not None and abs(e.price - 68020.0) < 1e-6 and abs(e.commission_usdt - 0.3) < 1e-9  # type: ignore[operator]
    closed = rec.model_copy(update={"closed_at": START + timedelta(seconds=610)})
    x = exit_fill(closed, trades, e)
    assert x is not None and x.price == 68100.0 and x.realized_pnl == 0.9
    # 平仓时间之后 60 秒还没有的成交不算
    early = rec.model_copy(update={"closed_at": START + timedelta(seconds=100)})
    assert exit_fill(early, trades, e) is None
    # 数量凑不够就不回填
    assert entry_fill(rec, trades[:2]) is None


async def test_backfill_fixes_entry_then_exit_and_logs(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, price=0.7497, symbol="APTUSDT")
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    q = rec.quantity
    rig.ex.trades = [_t(1, "buy", 0.7487, q, 0.05)]
    assert await rig.adapter.backfill_fills(rig.ex.user_trades) == 1
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.entry_price == 0.7487 and rec.intended_price == 0.7497
    assert rec.stop_loss_price is not None and rec.stop_loss_price < 0.7487
    assert await rig.adapter.backfill_fills(rig.ex.user_trades) == 0  # 回填过不再动
    # 外部手动平仓：执行器结束，账本记成已平，但没有出场价
    rig.t = START + timedelta(minutes=10)
    rig.api.terminate("ex1", "EARLY_STOP", pnl=9.0)
    await rig.adapter.positions()
    rig.ex.trades.append(_t(590, "sell", 0.7600, q, 0.05, pnl=(0.7600 - 0.7487) * q))
    assert await rig.adapter.backfill_fills(rig.ex.user_trades) == 1
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.CLOSED and rec.exit_price == 0.76
    assert abs(rec.fees_usdt - 0.10) < 1e-9
    assert abs(rec.realized_pnl_usdt - ((0.76 - 0.7487) * q - 0.10)) < 1e-9  # type: ignore[operator]


async def test_backfill_records_the_exit_fill_time_beside_the_ledger(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    fills = ExitFills(tmp_path / "exit_fills.json")
    rig.adapter._exit_fills = fills
    pid = await open_filled(rig, price=0.7497, symbol="APTUSDT")
    q = rig.registry.get(pid).record.quantity  # type: ignore[union-attr]
    fills.record(pid, START + timedelta(seconds=599), "order")
    rig.ex.trades = [_t(1, "buy", 0.7487, q, 0.05)]
    rig.t = START + timedelta(minutes=10)
    rig.api.terminate("ex1", "EARLY_STOP", pnl=9.0)
    await rig.adapter.positions()
    rig.ex.trades += [
        _t(570, "sell", 0.7600, q / 2, 0.02, pnl=0.1),
        _t(575, "sell", 0.7601, q / 2, 0.02, pnl=0.1),
    ]
    assert await rig.adapter.backfill_fills(rig.ex.user_trades) == 1
    assert read_exit_fills(tmp_path / "exit_fills.json") == {pid: START + timedelta(seconds=575)}
    fills.record(pid, START + timedelta(seconds=599), "order")  # trades already won
    assert read_exit_fills(tmp_path / "exit_fills.json") == {pid: START + timedelta(seconds=575)}
    assert "exit_fill" not in (tmp_path / "positions.json").read_text()


def test_exit_fills_file_unreadable_or_partial_reads_as_empty(tmp_path: Path) -> None:
    path = tmp_path / "exit_fills.json"
    assert read_exit_fills(path) == {}
    path.write_text("{not json", encoding="utf-8")
    assert read_exit_fills(path) == {}
    path.write_text(
        json.dumps({"a": {"at": "2026-10-28T06:10:27+00:00"}, "b": {"at": "bad"}, "c": 1}),
        encoding="utf-8",
    )
    assert list(read_exit_fills(path)) == ["a"]


async def test_finalize_runs_once_when_two_paths_race(tmp_path: Path) -> None:
    """平仓等待循环和主循环同步同时发现执行器结束：只记一条平仓、撤一次保命止损。"""
    rig = Rig(tmp_path)
    pid = await open_filled(rig)
    rig.api.terminate("ex1", "STOP_LOSS", pnl=-1.0)
    tracked = rig.registry.get(pid)
    assert tracked is not None
    detail = rig.api.executors["ex1"]
    closes: list[str] = []
    rig.adapter._record = lambda outcome, summary, rec, **kw: closes.append(summary)  # type: ignore[method-assign]
    real_positions = rig.ex.positions

    async def slow_positions() -> Any:
        await asyncio.sleep(0)  # 让两条路径在「问币安还有没有仓」时交错
        return await real_positions()

    rig.ex.positions = slow_positions  # type: ignore[method-assign]
    a, b = await asyncio.gather(
        rig.adapter._finalize(tracked, detail, None),
        rig.adapter._finalize(tracked, detail, None),
    )
    assert a.record.position_id == b.record.position_id == pid
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED  # type: ignore[union-attr]
    # 第二条路径看到已经结过账，不再记一次平仓
    assert len([c for c in closes if "平仓" in c]) == 1


def test_user_stream_helpers() -> None:
    assert ws_urls("wss://x", "k") == ["wss://x/private/ws?listenKey=k", "wss://x/ws/k"]
    assert is_fill({"e": "ORDER_TRADE_UPDATE", "o": {"x": "TRADE", "l": "1"}})
    assert not is_fill({"e": "ORDER_TRADE_UPDATE", "o": {"x": "NEW", "l": "0"}})
    assert is_fill({"e": "ACCOUNT_UPDATE"})
    assert "60 秒轮询" in StreamStatus(last_error="boom").text()
    assert "实时" in StreamStatus(connected=True).text()


class _WS:
    def __init__(self, msgs: list[Any]) -> None:
        self._msgs = msgs

    async def __aenter__(self) -> _WS:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def __aiter__(self) -> _WS:
        return self

    async def __anext__(self) -> str:
        if not self._msgs:
            raise StopAsyncIteration
        return json.dumps(self._msgs.pop(0))


async def test_user_stream_falls_back_to_old_path_and_pokes() -> None:
    tried: list[str] = []
    fills: list[int] = []
    sleeps: list[float] = []

    async def connector(url: str) -> _WS:
        tried.append(url)
        if "/private/" in url:
            raise OSError("404")
        return _WS([{"e": "ORDER_TRADE_UPDATE", "o": {"x": "TRADE", "l": "1"}}, {"e": "x"}])

    async def new_key() -> str:
        return "k1"

    async def keepalive() -> None:
        return None

    async def on_fill() -> None:
        fills.append(1)

    async def sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    stream = UserStream(
        new_key, keepalive, on_fill, ws_base="wss://x", connector=connector, sleep=sleep
    )
    with contextlib.suppress(asyncio.CancelledError):
        await stream.run()
    assert tried[:2] == ["wss://x/private/ws?listenKey=k1", "wss://x/ws/k1"]
    assert fills and stream.status.last_event_at is not None
    assert sleeps[0] == 1.0  # 服务器断开后马上（1 秒）重连


async def test_ledger_sync_refresh_writes_note_and_backfills(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, price=68044.0)
    q = rig.registry.get(pid).record.quantity  # type: ignore[union-attr]
    rig.ex.trades = [_t(1, "buy", 68000.0, q, 0.1)]
    sync = LedgerSync(rig.adapter, rig.ex.user_trades, None, clock=rig.clock)
    await sync.refresh()
    assert rig.registry.get(pid).record.entry_price == 68000.0  # type: ignore[union-attr]
    assert rig.adapter.fills_note == "成交回报只走 60 秒轮询"
    assert isinstance(sync.last_run_at, datetime)
