"""总盘旁路诊断：每次调用局部关联、脱敏、有界，任何写入故障不得改变判断。"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import queue
import re
import threading
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
MAX_TEXT = 262_144
MAX_FILE_BYTES = 32 * 1024 * 1024
_LOCK = threading.Lock()
_START_LOCK = threading.Lock()
_QUEUE: queue.Queue[tuple[Path, dict[str, Any]] | threading.Event] = queue.Queue(maxsize=64)
_WORKER: threading.Thread | None = None
_URL_QUERY = re.compile(r"(https?://[^\s?<>]+)\?[^\s<>]+")
_SECRET = re.compile(r"(?:sk-[\w-]{16,}|gh[pousr]_[\w]{16,}|Bearer\s+[^\s]+)", re.I)
_ASSIGN = re.compile(
    r"(?i)((?:api[_-]?key|authorization|secret|access_token|signature)\s*[:=]\s*)[^\s,;]+"
)


def clean(value: Any) -> Any:
    """仅处理业务字段；不序列化 SDK、连接对象或异常内部配置。"""
    if isinstance(value, str):
        return _ASSIGN.sub(
            r"\1[redacted]", _SECRET.sub("[redacted]", _URL_QUERY.sub(r"\1?[redacted]", value))
        )[:MAX_TEXT]
    if isinstance(value, dict):
        return {
            str(k): "[redacted]"
            if str(k).lower() in {"api_key", "authorization", "secret", "token", "headers"}
            else clean(v)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [clean(v) for v in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    return None


def write_diagnostic(root: Path, row: dict[str, Any]) -> None:
    """单日最多两段32MiB；超限停止旁路记录，不抛出或修改生产状态。"""
    try:
        now = datetime.now(UTC)
        payload = (
            json.dumps(
                clean({"schema_version": 1, "write_started_at": now.isoformat(), **row}),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
            + b"\n"
        )
        if len(payload) > 2 * MAX_TEXT:
            payload = (
                json.dumps(
                    {
                        "schema_version": 1,
                        "write_started_at": now.isoformat(),
                        "trace_id": row.get("trace_id"),
                        "kind": "diagnostic_oversize",
                        "complete": False,
                    }
                ).encode()
                + b"\n"
            )
        with _LOCK:
            root.mkdir(parents=True, exist_ok=True)
            paths = [root / f"{now:%Y-%m-%d}{suffix}.jsonl" for suffix in ("", ".1")]
            for path in paths:
                if not path.exists() or path.stat().st_size + len(payload) <= MAX_FILE_BYTES:
                    with path.open("ab") as f:
                        f.write(payload)
                    break
            else:
                logger.warning("总盘诊断当日文件已满，本条未写入")
    except Exception:
        logger.warning("总盘旁路诊断写入失败，不影响原判断", exc_info=True)


def _drain() -> None:
    while True:
        item = _QUEUE.get()
        try:
            if isinstance(item, threading.Event):
                item.set()
            else:
                write_diagnostic(*item)
        except Exception:
            logger.warning("总盘诊断后台写入失败", exc_info=True)
        finally:
            _QUEUE.task_done()


def enqueue_diagnostic(root: Path, row: dict[str, Any]) -> None:
    """有界后台队列；满载可丢诊断，绝不等待磁盘或阻塞快环。"""
    global _WORKER
    try:
        with _START_LOCK:
            if _WORKER is None:
                _WORKER = threading.Thread(target=_drain, name="market-diagnostics", daemon=True)
                _WORKER.start()
        captured = clean(copy.deepcopy(row))
        if len(json.dumps(captured, ensure_ascii=False, allow_nan=False).encode()) > 2 * MAX_TEXT:
            captured = {
                "trace_id": row.get("trace_id"),
                "kind": "diagnostic_oversize",
                "complete": False,
            }
        _QUEUE.put_nowait((root, captured))
    except Exception:
        logger.warning("总盘诊断队列不可用，原判断继续")


def flush_diagnostics(timeout: float = 2.0) -> bool:
    """只给离线验收使用；生产路径不等待旁路队列。"""
    marker = threading.Event()
    try:
        _QUEUE.put_nowait(marker)
        return marker.wait(timeout)
    except queue.Full:
        return False


class MarketAudit:
    def __init__(self, writer: Any, context: dict[str, Any]) -> None:
        try:
            self.trace_id: str | None = uuid.uuid4().hex
        except Exception:
            self.trace_id = None  # 关联未知，不让诊断初始化故障改变原调用。
        self.writer = writer
        self.context = context
        self.attempt = 0

    def emit(self, kind: str, **fields: Any) -> None:
        try:
            if self.writer is not None:
                self.writer.write_diagnostic(
                    {**self.context, "trace_id": self.trace_id, "kind": kind, **fields}
                )
        except Exception:
            logger.warning("总盘诊断不可用，不影响原调用", exc_info=True)


CURRENT_AUDIT: ContextVar[MarketAudit | None] = ContextVar("market_audit", default=None)


def market_timing_note() -> str:
    try:
        return _market_timing_note()
    except Exception:
        return "\nInput source/window clocks unavailable; do not infer freshness.\n"


def _market_timing_note() -> str:
    """仅总盘prompt的口径纠错；不复用到管家或单币分析prompt。"""
    audit = CURRENT_AUDIT.get()
    context = audit.context if audit is not None else {}
    sources = context.get("pool_source_metadata") or {}
    windows = {}
    for field in (
        "ticker_window_end_at",
        "completed_hour_start",
        "completed_hour_end",
        "completed_4h_close_span_start",
        "completed_4h_close_span_end",
        "current_hour_open_at",
    ):
        values = sorted(
            str(m[field]) for m in sources.values() if isinstance(m, dict) and m.get(field)
        )
        windows[field] = {
            "covered": len(values),
            "min": values[0] if values else None,
            "max": values[-1] if values else None,
        }
    clocks: dict[str, Any] = {
        "pool_requested_at": context.get("pool_requested_at"),
        "pool_n": context.get("pool_n"),
        "windows": windows,
        "brief": context.get("brief_provenance") or {"status": "unknown"},
    }
    # 简报每源明细落诊断，模型只收同份文本的缓存/生成概况，避免重复整份来源表。
    brief = clocks["brief"]
    if isinstance(brief, dict):
        content = brief.get("content") or {}
        clocks["brief"] = {
            "status": brief.get("status"),
            "cache_hit": brief.get("cache_hit"),
            "returned_at": brief.get("returned_at"),
            "content_generated_at": content.get("generated_at"),
            "content_status": content.get("status"),
            "windows": [
                {
                    "symbol": part.get("symbol"),
                    "field": field,
                    "interval": m.get("interval"),
                    "anchor_scheduled_close_at": m.get("anchor_bar_scheduled_close_at"),
                    "last_scheduled_close_at": m.get("last_bar_scheduled_close_at"),
                    "last_bar_closed": m.get("last_bar_closed_at_observation"),
                    "required_bar_count_present": m.get("required_bar_count_present"),
                }
                for part in content.get("parts", [])
                if isinstance(part, dict)
                for field, m in (part.get("fields") or {}).items()
                if isinstance(m, dict)
            ],
        }
    return (
        "\nInput window semantics (do not treat as interchangeable): completed_hour is the latest "
        "complete UTC 1h candle open-to-close; completed_4h_close_span is last completed close "
        "versus four completed closes earlier; current_hour_open_to_ticker uses current hour open"
        " and ticker price; ticker_24h uses exchange rolling ticker. Sentinel breadth uses "
        "separate exact rolling 60-minute closed 1m bars and is NOT this completed-hour breadth. "
        "Brief horizons retain their own bar endpoints, possibly including an unfinished bar; "
        "unknown clocks remain unknown. Pool request time is NOT a source event timestamp.\n"
        + "Input clock/coverage metadata: "
        + json.dumps(clocks, ensure_ascii=False, default=str)
        + "\n"
    )


def _clock() -> str | None:
    try:
        return datetime.now(UTC).isoformat()
    except Exception:
        return None


def invoke_market(llm: Any, prompt: str, handler: Any, **context: Any) -> Any:
    """原样调用；时钟/脱敏/序列化失败均不改变返回、异常、重问次数。"""
    audit = CURRENT_AUDIT.get()
    attempt = 0
    try:
        if audit is not None:
            audit.attempt += 1
            attempt = audit.attempt
            stored = clean(prompt)
            audit.emit(
                "model_request",
                attempt=attempt,
                request_started_at=_clock(),
                prompt=stored,
                prompt_chars=len(prompt),
                prompt_original_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                prompt_stored_sha256=hashlib.sha256(stored.encode()).hexdigest(),
                prompt_redacted=stored != prompt[:MAX_TEXT],
                prompt_truncated=len(prompt) > MAX_TEXT,
                prompt_replay_exact=stored == prompt,
                **context,
            )
    except Exception:
        pass
    try:
        result = llm.invoke(prompt, config={"callbacks": [handler]})
    except BaseException as exc:
        if audit is not None:
            audit.emit(
                "model_response",
                attempt=attempt,
                response_completed_at=_clock(),
                status="failed",
                error_type=type(exc).__name__,
            )
        raise
    completed_at = _clock()  # 先取实际返回时刻，再做旁路序列化。
    if audit is not None:
        try:
            from pydantic import BaseModel

            response = result.model_dump(mode="json") if isinstance(result, BaseModel) else None
        except Exception:
            response = None
        audit.emit(
            "model_response",
            attempt=attempt,
            response_completed_at=completed_at,
            status="returned",
            structured_response=response,
        )
    return result
