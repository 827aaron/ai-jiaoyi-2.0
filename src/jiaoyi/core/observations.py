"""管家报价证据旁路；队列、磁盘和时钟故障均不参与交易判断。"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jiaoyi.contracts import BookTop, MarketEvent, MarkPrice, Trade


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware(value: object) -> datetime | None:
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        return result if result.tzinfo is not None and result.utcoffset() is not None else None
    except (ValueError, TypeError):
        return None


def freeze_values(value: Any, depth: int = 0) -> Any:
    if depth > 24:
        raise ValueError("Diagnostic nesting limit")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("Diagnostic keys must be strings")
        return {key: freeze_values(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [freeze_values(item, depth + 1) for item in value]
    raise ValueError("Unsupported diagnostic value")


class KeeperObservationCollector:
    """单事件循环持有pending；后台writer仅拥有队列中的冻结纯值，不共享决策日志锁。"""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        max_queue: int = 256,
        max_pending: int = 64,
        ttl_s: float = 60.0,
        wall: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        writer: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._root = Path(data_dir) / "keeper_observations"
        self._wall, self._mono = wall, monotonic
        self._ttl, self._max_pending = ttl_s, max_pending
        self._queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_queue)
        self._pending: dict[str, dict[str, Any]] = {}
        self._closed = False
        self._stop = threading.Event()
        self._writer = writer or self._write
        self._stats = {
            "dropped": 0,
            "write_errors": 0,
            "capture_errors": 0,
            "expired": 0,
            "clock_inconsistent": 0,
        }
        self._thread = threading.Thread(target=self._run, name="keeper-observations", daemon=True)
        self._thread.start()

    @property
    def stats(self) -> dict[str, int]:
        return {**self._stats, "pending": len(self._pending), "queued": self._queue.qsize()}

    def _enqueue(self, record: dict[str, Any]) -> bool:
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            self._stats["dropped"] += 1
            return False

    def _expire(self, now: float) -> None:
        for key, pending in list(self._pending.items()):
            if now >= pending["deadline"]:
                self._stats["expired"] += 1
                self._finish(key, "ttl_expired")

    def _finish(self, key: str, reason: str) -> None:
        pending = self._pending.pop(key)
        self._enqueue(
            {
                "schema_version": 1,
                "kind": "keeper_observation_end",
                "request_id": key,
                "symbol": pending["symbol"],
                "reason": reason,
                "missing_sources": sorted(pending["remaining"]),
                "dropped_sources": sorted(pending["dropped"]),
                "delivery_status": "unknown_due_to_queue_drop"
                if pending["dropped"]
                else "enqueued_receipts_required",
                "response_completed_at": pending["after"].isoformat(),
            }
        )

    def offer(self, record: dict[str, Any]) -> None:
        if self._closed:
            self._stats["dropped"] += 1
            return
        try:
            now = self._mono()
            self._expire(now)
            frozen: dict[str, Any] = freeze_values(record)
            self._enqueue({**frozen, "collector_stats": self.stats})
            after = aware(record.get("response_completed_at"))
            key, symbol = record.get("request_id"), record.get("symbol")
            if (
                after is None
                or not isinstance(key, str)
                or not isinstance(symbol, str)
                or record.get("clock_consistent") is False
            ):
                return
            if key in self._pending:
                self._finish(key, "replaced")
            if len(self._pending) >= self._max_pending:
                self._finish(next(iter(self._pending)), "pending_capacity")
            self._pending[key] = {
                "symbol": symbol,
                "after": after,
                "deadline": now + self._ttl,
                "remaining": {"trade", "book", "mark"},
                "dropped": set(),
            }
        except Exception:
            self._stats["capture_errors"] += 1

    def on_event(self, event: MarketEvent, received_at: datetime | None = None) -> None:
        if self._closed or not self._pending:
            return
        try:
            self._expire(self._mono())
            if not isinstance(event, (Trade, BookTop, MarkPrice)):
                return
            received = aware(received_at if received_at is not None else self._wall())
            kind = event.type
            for key, pending in list(self._pending.items()):
                if pending["symbol"] != event.symbol:
                    continue
                if self._mono() >= pending["deadline"]:
                    self._stats["expired"] += 1
                    self._finish(key, "ttl_expired")
                    continue
                if kind not in pending["remaining"] or received is None:
                    continue
                if event.ts > received:
                    self._stats["clock_inconsistent"] += 1
                    continue
                if event.ts <= pending["after"] or received <= pending["after"]:
                    continue
                quote: dict[str, Any]
                if isinstance(event, Trade):
                    quote = {"price": event.price, "qty": event.qty}
                elif isinstance(event, BookTop):
                    quote = {
                        "bid": event.bids[0][0],
                        "bid_qty": event.bids[0][1],
                        "ask": event.asks[0][0],
                        "ask_qty": event.asks[0][1],
                    }
                elif isinstance(event, MarkPrice):
                    quote = {"price": event.mark_price}
                else:
                    continue
                if not all(
                    isinstance(v, (int, float)) and math.isfinite(v) for v in quote.values()
                ):
                    self._stats["capture_errors"] += 1
                    continue
                delivered = self._enqueue(
                    {
                        "schema_version": 1,
                        "kind": "keeper_first_newer_event",
                        "request_id": key,
                        "symbol": event.symbol,
                        "source": kind,
                        "response_completed_at": pending["after"].isoformat(),
                        "source_event_ts": event.ts.isoformat(),
                        "observed_at": received.isoformat(),
                        "strict_after_response": True,
                        "quote": quote,
                        "is_execution_fill": False,
                    }
                )
                if not delivered:
                    pending["dropped"].add(kind)
                pending["remaining"].remove(kind)
                if not pending["remaining"]:
                    self._finish(key, "all_sources_processed")
        except Exception:
            self._stats["capture_errors"] += 1

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for key in list(self._pending):
            self._finish(key, "collector_closed")
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                record = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self._writer(record)
            except Exception:
                self._stats["write_errors"] += 1
            finally:
                self._queue.task_done()

    def _write(self, record: dict[str, Any]) -> None:
        started = aware(self._wall())
        if started is None:
            raise ValueError("Diagnostic write clock unavailable")
        stamped = {**record, "write_started_at": started.isoformat()}
        line = json.dumps(stamped, ensure_ascii=False, allow_nan=False) + "\n"
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f"{started.date().isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(line)
            stream.flush()
            confirmed = aware(self._wall())
            if confirmed is not None and confirmed < started:
                confirmed = None
            receipt = {
                "schema_version": 1,
                "kind": "visibility_receipt",
                "request_id": record.get("request_id"),
                "source_kind": record.get("kind"),
                "source": record.get("source"),
                "visibility_confirmed_at": confirmed.isoformat() if confirmed else None,
                "collector_stats": self.stats,
                "durability": "flushed_not_fsynced",
            }
            stream.write(json.dumps(receipt, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
