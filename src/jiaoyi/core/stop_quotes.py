"""持仓同环境盘口旁路：独立连接、独立有界磁盘线程，不向交易模块传递事件。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from websockets.asyncio.client import connect

BASES = {"demo": "wss://demo-fstream.binance.com", "live": "wss://fstream.binance.com"}
SYMBOL = re.compile(r"^[A-Z0-9]{2,20}USDT$")
SEGMENT_BYTES = 64 * 1024 * 1024
MAX_SEGMENTS = 16
SILENT_S = 15.0


def number(value: object) -> float | None:
    try:
        result = float(str(value))
        return result if math.isfinite(result) and result > 0 else None
    except (ValueError, TypeError):
        return None


def event_time(value: object) -> str | None:
    try:
        if isinstance(value, bool):
            return None
        return datetime.fromtimestamp(float(str(value)) / 1000, UTC).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


class StopQuoteRecorder:
    """每个进程有 epoch；seq 包含丢弃项，收据保留累计丢样/写盘错误，不能据此保证全覆盖。"""

    def __init__(
        self,
        data_dir: str | Path,
        environment: str,
        *,
        max_queue: int = 2048,
        writer: Callable[[dict[str, Any]], None] | None = None,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if environment not in BASES:
            raise ValueError("盘口诊断只支持明确的 demo/live 环境")
        if max_queue < 1:
            raise ValueError("盘口诊断队列必须有界")
        self.root = Path(data_dir) / "stop_quote_observations"
        self.environment = environment
        self.epoch = uuid.uuid4().hex
        self._wall = wall
        self._symbols: frozenset[str] = frozenset()
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue)
        self._stats = {"seq": 0, "dropped": 0, "write_errors": 0, "invalid": 0, "valid": 0}
        self._writer = writer or self._write
        self._persist_status = writer is None
        self._status_at = 0.0
        self._closed = False
        self._stop = threading.Event()
        self._segment = 0
        self._pruned_segment = -1
        self._thread = threading.Thread(target=self._drain, name="stop-quotes", daemon=True)
        self._thread.start()

    @property
    def stats(self) -> dict[str, int]:
        return {**self._stats, "queued": self._queue.qsize()}

    def _offer(self, fields: dict[str, Any]) -> None:
        if self._closed:
            return
        self._stats["seq"] += 1
        record = {
            "schema_version": 1,
            "source": "independent_exchange_stream",
            "source_env": self.environment,
            "process_epoch": self.epoch,
            "received_at": self._wall().isoformat(),
            "received_monotonic_ns": time.monotonic_ns(),
            "participates_in_decision": False,
            "coverage_complete": False,
            **fields,
            **self.stats,
        }
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            self._stats["dropped"] += 1

    def update_symbols(self, symbols: set[str]) -> bool:
        selected = frozenset(s for s in symbols if SYMBOL.fullmatch(s))
        if selected == self._symbols:
            return False
        self._symbols = selected
        self._offer({"kind": "subscription_changed", "symbols": sorted(selected)})
        return True

    def capture(self, raw: str | bytes, *, epoch: str = "test") -> None:
        """只投影公开行情字段；未知时间为空，绝不以本地时间冒充交易所事件时间。"""
        try:
            message = json.loads(raw)
            data = message.get("data", message)
            symbol = data.get("s")
            if symbol not in self._symbols:
                return
            kind = data.get("e")
            if kind == "bookTicker" or str(message.get("stream", "")).endswith("@bookTicker"):
                bid, ask = number(data.get("b")), number(data.get("a"))
                if bid is None or ask is None or ask < bid:
                    raise ValueError("无效盘口")
                fields = {
                    "kind": "book",
                    "bid": bid,
                    "ask": ask,
                    "event_at": event_time(data.get("E")),
                    "transaction_at": event_time(data.get("T")),
                    "exchange_update_id": data.get("u") if type(data.get("u")) is int else None,
                }
            elif kind == "markPriceUpdate":
                mark = number(data.get("p"))
                if mark is None:
                    raise ValueError("无效标记价")
                fields = {
                    "kind": "mark",
                    "mark_price": mark,
                    "event_at": event_time(data.get("E")),
                    "transaction_at": None,
                }
            else:
                return
            self._stats["valid"] += 1
            self._offer({**fields, "symbol": symbol, "connection_epoch": epoch})
        except (ValueError, TypeError, AttributeError):
            self._stats["invalid"] += 1

    async def _connection(self, route: str, symbols: frozenset[str]) -> None:
        suffix = "bookTicker" if route == "public" else "markPrice@1s"
        streams = "/".join(f"{symbol.lower()}@{suffix}" for symbol in sorted(symbols))
        paths = (f"/{route}/stream", "/stream")
        attempt = 0
        while not self._closed:
            path = paths[attempt % len(paths)]
            epoch = uuid.uuid4().hex
            url = f"{BASES[self.environment]}{path}?streams={streams}"
            try:
                async with connect(url, open_timeout=5, close_timeout=1, max_size=65536) as ws:
                    last_valid = time.monotonic()
                    self._offer(
                        {
                            "kind": "connected",
                            "route": route,
                            "connection_epoch": epoch,
                            "path": path,
                        }
                    )
                    while not self._closed:
                        raw = await asyncio.wait_for(ws.recv(), SILENT_S)
                        before = self._stats["valid"]
                        self.capture(raw, epoch=epoch)
                        if self._stats["valid"] > before:
                            last_valid = time.monotonic()
                        elif time.monotonic() - last_valid > SILENT_S:
                            raise TimeoutError("未收到有效诊断行情")
            except asyncio.CancelledError:
                self._offer(
                    {
                        "kind": "connection_end",
                        "route": route,
                        "connection_epoch": epoch,
                        "reason": "subscription_or_shutdown",
                    }
                )
                raise
            except Exception as exc:
                self._offer(
                    {
                        "kind": "connection_gap",
                        "route": route,
                        "connection_epoch": epoch,
                        "error_type": type(exc).__name__,
                    }
                )
            attempt += 1
            await asyncio.sleep(min(2 ** min(attempt, 4), 15))

    def read_symbols(self) -> set[str]:
        """只读落盘账本；不能调用 adapter.positions()，它含同步与限价单管理等交易副作用。"""
        path = self.root.parent / "positions.json"
        if not path.exists():
            return set()
        if path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("诊断账本读取超限")
        ledger = json.loads(path.read_text())
        return {
            item["record"]["symbol"]
            for item in ledger["positions"].values()
            if item["record"].get("status") in ("open", "opening", "closing")
        }

    async def run(self) -> None:
        tasks: list[asyncio.Task[None]] = []
        try:
            while not self._closed:
                try:
                    symbols = await asyncio.to_thread(self.read_symbols)
                    changed = self.update_symbols(symbols)
                except Exception as exc:
                    self._offer({"kind": "subscription_gap", "error_type": type(exc).__name__})
                    changed = self.update_symbols(set())
                if changed:
                    for task in tasks:
                        task.cancel()
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    tasks = (
                        [
                            asyncio.create_task(self._connection(route, self._symbols))
                            for route in ("public", "market")
                        ]
                        if self._symbols
                        else []
                    )
                await asyncio.sleep(2)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def _write(self, record: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"quotes-{self.epoch}-{self._segment:05d}.jsonl"
        line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
        if path.exists() and path.stat().st_size + len(line.encode()) > SEGMENT_BYTES:
            self._segment += 1
            path = self.root / f"quotes-{self.epoch}-{self._segment:05d}.jsonl"
        with path.open("a") as stream:
            stream.write(line)
        if self._pruned_segment != self._segment:
            files = sorted(self.root.glob("quotes-*.jsonl"), key=lambda p: p.stat().st_mtime)
            for old in files[:-MAX_SEGMENTS]:
                if old != path:
                    old.unlink()
            self._pruned_segment = self._segment

    def _drain(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            self._write_status()
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._writer(item)
            except Exception:
                self._stats["write_errors"] += 1
            finally:
                self._queue.task_done()
        self._write_status(force=True)

    def _write_status(self, *, force: bool = False) -> None:
        if not self._persist_status or (not force and time.monotonic() - self._status_at < 1):
            return
        self._status_at = time.monotonic()
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            target = self.root / "status.json"
            temporary = self.root / "status.tmp"
            temporary.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source_env": self.environment,
                        "process_epoch": self.epoch,
                        "updated_at": self._wall().isoformat(),
                        "symbols": sorted(self._symbols),
                        "closed": self._closed,
                        "coverage_complete": False,
                        **self.stats,
                    }
                )
            )
            temporary.replace(target)
        except Exception:
            self._stats["write_errors"] += 1

    def close(self) -> None:
        if self._closed:
            return
        self._offer({"kind": "recorder_end", "reason": "shutdown"})
        self._closed = True
        self._stop.set()
        self._thread.join(timeout=0.2)

    async def aclose(self, task: asyncio.Task[None]) -> None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await asyncio.to_thread(self.close)
