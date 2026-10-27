"""本地只读止损观测；包装 Hummingbot 原方法，不替换任何原判断。

兼容目标：Makefile 钉住镜像中的 hummingbot/strategy_v2/executors/position_executor。
上游 Hummingbot 为 Apache-2.0；本文件不复制上游方法体，只调用原方法一次。
"""

from __future__ import annotations

import contextlib
import copy
import functools
import inspect
import json
import math
import queue
import re
import threading
import time
import uuid
from collections import deque
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

CAPABILITY = "position_executor_stop_v1"
ROOT = Path("/hummingbot-api/bots/logs/stop_diagnostics")
MAX_ROW_BYTES = 32768
MAX_SEGMENT_BYTES = 16 * 1024 * 1024
SEGMENTS = 4
RETENTION_DAYS = 3
MAX_EVENTS = 32
_CURRENT: ContextVar[Any] = ContextVar("jiaoyi_stop_check", default=None)


def _id() -> str | None:
    try:
        return uuid.uuid4().hex
    except Exception:
        return None


RUNTIME_EPOCH = _id()


def _clock() -> str | None:
    try:
        return datetime.now(UTC).isoformat()
    except Exception:
        return None


def _num(value: Any, percent: bool = False) -> float | None:
    try:
        number = float(value * 100) if percent else float(value)
        return number if math.isfinite(number) else None
    except Exception:
        return None


def _enum(value: Any) -> str | None:
    name = getattr(value, "name", None)
    return str(name).lower() if name is not None else None


def _hash(value: Any) -> str | None:
    if value is None:
        return None
    import hashlib

    return hashlib.sha256(str(value).encode()).hexdigest()


class State:
    def __init__(self, executor_id: str) -> None:
        self.executor_id = executor_id
        self.instance = _id()
        self.seq = 0
        self.written_seq = 0
        self.since: str | None = None
        self.previous_ns: int | None = None
        self.last: dict[str, Any] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self.dropped = 0
        self.write_errors = 0
        self.oversize = 0
        self.gaps = 0
        self.event_overflow = 0
        self.observer_errors = 0
        self.record_seq = 0
        self.written_record_seq = 0
        self.event_seq = 0
        self.order_requests: dict[str, str | None] = {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "capability": CAPABILITY,
            "source": "hb_runtime",
            "executor_id": self.executor_id,
            "runtime_epoch": RUNTIME_EPOCH,
            "executor_instance_epoch": self.instance,
            **copy.deepcopy(self.last),
            "check_seq": self.last.get("check_seq", 0),
            "produced_check_seq": self.seq,
            "written_seq": self.written_seq,
            "record_seq": self.record_seq,
            "written_record_seq": self.written_record_seq,
            "coverage_since": self.since,
            "coverage_scope": "since_first_observed_check",
            "coverage_complete": False,
            "coverage_status": "bounded_observer_not_full_lifecycle_proof",
            "retention_evictions": getattr(WRITER, "retention_evictions", None),
            "retained_since": getattr(WRITER, "retained_since", None),
            "persistence": "background_append_no_fsync",
            "gap_detected": bool(self.gaps or self.dropped or self.write_errors),
            "dropped": self.dropped,
            "write_errors": self.write_errors,
            "oversize": self.oversize,
            "observer_errors": self.observer_errors,
            "gap_count": self.gaps,
            "event_overflow": self.event_overflow,
            "events": copy.deepcopy(list(self.events)),
            "trailing_stop_price": None,
        }


class Writer:
    """无磁盘操作进入控制线程；有界队列，状态可经custom_info核查。"""

    def __init__(self, root: Path = ROOT) -> None:
        self.root = root
        self.pending: queue.Queue[Any] = queue.Queue(maxsize=128)
        self.started = False
        self.pruned_day: str | None = None
        self.retention_evictions = 0
        self.retained_since: str | None = None

    def submit(self, state: State, row: dict[str, Any]) -> None:
        try:
            state.record_seq += 1
            row["record_seq"] = state.record_seq
            row["enqueued_at"] = _clock()
            text = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(text.encode()) > MAX_ROW_BYTES:
                state.oversize += 1
                state.dropped += 1
                return
            if not self.started:
                self.started = True
                threading.Thread(target=self._run, name="stop-diagnostics", daemon=True).start()
            self.pending.put_nowait(
                (state, row.get("check_seq", 0), row["record_seq"], row.get("kind"), text)
            )
        except queue.Full:
            state.dropped += 1
        except Exception:
            state.observer_errors += 1
            state.dropped += 1

    def _write(self, text: str) -> None:
        now = datetime.now(UTC)
        day = f"{now:%Y-%m-%d}"
        self.root.mkdir(parents=True, exist_ok=True)
        if self.pruned_day != day:
            cutoff = (now - timedelta(days=RETENTION_DAYS)).date().isoformat()
            for path in self.root.glob("stop-*.jsonl"):
                match = re.fullmatch(r"stop-(\d{4}-\d{2}-\d{2})-[0-3]\.jsonl", path.name)
                if match and match[1] <= cutoff and not path.is_symlink():
                    path.unlink()
                    self.retention_evictions += 1
            self.pruned_day = day
        encoded = (text + "\n").encode()
        for segment in range(SEGMENTS):
            path = self.root / f"stop-{day}-{segment}.jsonl"
            if path.is_symlink():
                raise OSError("拒绝写入非自有诊断链接")
            if not path.exists() or path.stat().st_size + len(encoded) <= MAX_SEGMENT_BYTES:
                with path.open("ab") as file:
                    file.write(encoded)
                return
        # 当前日满后复用最旧自有段，不能在第1小时满后整天停录。
        candidates = [self.root / f"stop-{day}-{segment}.jsonl" for segment in range(SEGMENTS)]
        oldest = min(candidates, key=lambda path: path.stat().st_mtime_ns)
        if oldest.is_symlink():
            raise OSError("拒绝覆盖非自有日志")
        with oldest.open("wb") as file:
            file.write(encoded)
        self.retention_evictions += 1
        clocks = []
        for path in self.root.glob("stop-*.jsonl"):
            if path.is_symlink():
                continue
            with path.open() as file:
                row = json.loads(file.readline())
            clock = row.get("check_at") or row.get("enqueued_at")
            if isinstance(clock, str):
                clocks.append(clock)
        self.retained_since = min(clocks) if clocks else None

    def _run(self) -> None:
        while True:
            state, seq, record_seq, kind, text = self.pending.get()
            try:
                self._write(text)
                if kind == "executor_stop_check":
                    state.written_seq = max(state.written_seq, seq)
                state.written_record_seq = max(state.written_record_seq, record_seq)
                self.retained_since = self.retained_since or state.since
            except Exception:
                state.write_errors += 1
                state.dropped += 1
            finally:
                self.pending.task_done()


WRITER = Writer()


def _state(executor: Any) -> State:
    state = executor.__dict__.get("_jiaoyi_stop_observation")
    if state is None:
        identifier = str(executor.config.id)
        state = State(identifier)
        executor.__dict__["_jiaoyi_stop_observation"] = state
    return state


def _raw(executor: Any) -> Any:
    return executor.__dict__.get("_trailing_stop_trigger_pct")


def _order(order: Any) -> dict[str, Any] | None:
    if order is None:
        return None
    try:
        info = getattr(order, "order", None)
        return {
            "order_id_hash": _hash(getattr(order, "order_id", None)),
            "filled_base": _num(getattr(info, "executed_amount_base", None)),
            "status": _enum(getattr(info, "current_state", None)),
            "last_update_at": _num(getattr(order, "last_update_timestamp", None)),
            "source": "cached_tracked_order",
        }
    except Exception:
        return {"source": "cached_tracked_order", "status": "unknown"}


def _event(executor: Any, stage: str, kind: str, **fields: Any) -> dict[str, Any]:
    state = _state(executor)
    event = {
        "ts": _clock(),
        "stage": stage,
        "kind": kind,
        "check_seq": state.seq,
        "threshold_unit": "pct",
        **fields,
    }
    if len(state.events) == MAX_EVENTS:
        state.event_overflow += 1
    state.event_seq += 1
    event["event_seq"] = state.event_seq
    state.events.append(event)
    row = state.snapshot()
    row.update(kind="executor_stop_event", event=copy.deepcopy(event))
    WRITER.submit(state, row)  # 终态后没有下一轮检查，也独立保存真实native事件。
    return event


def _safe(function: Any, *args: Any, _owner: Any = None, **kwargs: Any) -> Any:
    try:
        return function(*args, **kwargs)
    except Exception:
        with contextlib.suppress(Exception):
            frame = _CURRENT.get()
            owner = _owner if _owner is not None else args[0] if args else frame["executor"]
            _state(owner).observer_errors += 1
        return None


def _begin(executor: Any) -> dict[str, Any]:
    state = _state(executor)
    now = _clock()
    monotonic = time.monotonic_ns()
    # 只标注采样间隙，不参与任何调度或原确认规则。
    if state.previous_ns is not None and monotonic - state.previous_ns > 5_000_000_000:
        state.gaps += 1
    state.previous_ns = monotonic
    state.seq += 1
    state.since = state.since or now
    return {
        "executor": executor,
        "check_at": now,
        "prices": [],
        "nets": [],
        "checks": [],
        "check_seq": state.seq,
        "current_check": None,
    }


def _finish(executor: Any, frame: dict[str, Any], failed: bool) -> None:
    state = _state(executor)
    config = executor.config.triple_barrier_config
    performed = [item for item in frame["checks"] if item.get("net_pnl_observed")]
    selected = performed[-1] if performed else {}
    state.last = {
        "check_seq": frame["check_seq"],
        "control_cycle_at": frame["check_at"],
        "control_cycle_completed_at": _clock(),
        "check_at": selected.get("ts"),
        "observed_at": selected.get("completed_at"),
        "selected_check_kind": selected.get("kind"),
        "stop_check_performed": any(item.get("net_pnl_observed") for item in frame["checks"]),
        "check_failed": failed,
        "checks": frame["checks"],
        "decision_price": selected.get("price"),
        "price_basis": selected.get("price_basis"),
        "price_reads": frame["prices"],
        "bid": next(
            (
                p["value"]
                for p in reversed(selected.get("price_reads", []))
                if p["basis"] == "best_bid"
            ),
            None,
        ),
        "ask": next(
            (
                p["value"]
                for p in reversed(selected.get("price_reads", []))
                if p["basis"] == "best_ask"
            ),
            None,
        ),
        "mark_price": None,
        "mark_price_status": "not_read_by_original_check",
        "net_pnl_pct": selected.get("net_pnl_pct"),
        "net_pnl_observed": bool(selected.get("net_pnl_observed")),
        "net_pnl_reads": frame["nets"],
        "stop_loss_net_pnl_pct": _num(-config.stop_loss, True)
        if config.stop_loss is not None
        else None,
        "trailing_active": bool(_raw(executor)),
        "actual_trailing_trigger_net_pnl_pct": _num(_raw(executor), True),
        "take_profit_order": _order(executor.__dict__.get("_take_profit_limit_order")),
        "close_order": _order(executor.__dict__.get("_close_order")),
    }
    row = state.snapshot()
    row["kind"] = "executor_stop_check"
    WRITER.submit(state, row)


def _capture(executor: Any, kind: str, value: Any, price_type: Any = None) -> None:
    frame = _CURRENT.get()
    if frame is None or frame["executor"] is not executor:
        return
    check = frame.get("current_check")
    if kind == "net":
        observed = _num(value, True)
        frame["nets"].append(observed)
        if check is not None:
            check["net_pnl_pct"] = observed
            check["net_pnl_observed"] = True
            check["_net_value"] = value
            check.setdefault("net_reads", []).append(
                {
                    "read_seq": len(frame["nets"]),
                    "value_pct": observed,
                    "value_ratio_decimal": str(value) if observed is not None else None,
                    "observed_at": _clock(),
                }
            )
    else:
        name = getattr(price_type, "name", None)
        basis = {"BestBid": "best_bid", "BestAsk": "best_ask"}.get(name)
        observed = {"basis": basis, "value": _num(value), "observed_at": _clock()}
        frame["prices"].append(observed)
        if check is not None:
            check["price"] = observed["value"]
            check["price_basis"] = basis
            check.setdefault("price_reads", []).append(copy.deepcopy(observed))


def _check_wrapper(original: Any, kind: str) -> Any:
    @functools.wraps(original)
    def wrapped(executor: Any, *args: Any, **kwargs: Any) -> Any:
        frame = _CURRENT.get()
        check = None
        if frame is not None and frame["executor"] is executor:

            def prepare() -> dict[str, Any]:
                config = executor.config.triple_barrier_config
                raw = _raw(executor)
                trailing = config.trailing_stop
                if kind == "stop_loss":
                    threshold = -config.stop_loss if config.stop_loss is not None else None
                else:
                    threshold = raw if raw else trailing.activation_price if trailing else None
                item = {
                    "ts": _clock(),
                    "stage": "check",
                    "kind": kind,
                    "check_seq": frame["check_seq"],
                    "threshold_unit": "pct",
                    "threshold": _num(threshold, True),
                    "_threshold_value": threshold,
                    "trailing_active_before": bool(raw),
                    "check_action": "activation"
                    if kind == "trailing_stop" and not raw
                    else "close",
                    "comparison": "<=" if kind == "stop_loss" else "<" if raw else ">",
                    "net_pnl_observed": False,
                    "net_pnl_pct": None,
                    "net_reads": [],
                    "price_reads": [],
                    "price": None,
                    "price_basis": None,
                    "condition_met": None,
                    "triggered": None,
                    "close_requested": False,
                }
                frame["checks"].append(item)
                frame["current_check"] = item
                return item

            check = _safe(prepare, _owner=executor)
        failed = True
        try:
            result = original(executor, *args, **kwargs)
            failed = False
            return result
        finally:

            def complete() -> None:
                if check is not None:
                    value = check.pop("_net_value", None)
                    threshold = check.pop("_threshold_value", None)
                    check["failed"] = failed
                    values = {entry.get("value_ratio_decimal") for entry in check["net_reads"]}
                    multiple = len(values) > 1
                    check["multiple_read_values"] = multiple
                    activated = not check["trailing_active_before"] and bool(_raw(executor))
                    check["activation_observed"] = activated if kind == "trailing_stop" else None
                    if check["close_requested"]:
                        check["condition_met"] = True
                        check["condition_met_source"] = "original_close_method_invoked"
                    elif activated and check["check_action"] == "activation":
                        check["condition_met"] = True
                        check["condition_met_source"] = "native_trigger_became_active"
                    elif (
                        not failed and not multiple and value is not None and threshold is not None
                    ):
                        if check["comparison"] == "<=":
                            met = value <= threshold
                        elif check["comparison"] == "<":
                            met = value < threshold
                        else:
                            met = value > threshold
                        check["condition_met"] = bool(met)
                        check["condition_met_source"] = "observer_recomputed_from_native_read"
                    else:
                        check["condition_met_source"] = "unknown"
                    check["triggered"] = check["condition_met"]
                    check["completed_at"] = _clock()
                    check["trailing_active_after"] = bool(_raw(executor))
                if frame is not None and frame["executor"] is executor:
                    frame["current_check"] = None

            _safe(complete, _owner=executor)

    return wrapped


def install(cls: Any) -> bool:
    """只包调用边界；不读任何连接器配置、headers、环境凭据或SDK响应对象。"""
    if getattr(cls, "_jiaoyi_stop_diagnostics_installed", False):
        return True
    task = cls.control_task
    if not inspect.iscoroutinefunction(task):
        return False

    @functools.wraps(task)
    async def control(executor: Any, *args: Any, **kwargs: Any) -> Any:
        frame = _safe(_begin, executor)
        token = _CURRENT.set(frame)
        failed = True
        try:
            result = await task(executor, *args, **kwargs)
            failed = False
            return result
        finally:
            if frame is not None:
                _safe(_finish, executor, frame, failed)
            _CURRENT.reset(token)

    price = cls.get_price

    @functools.wraps(price)
    def get_price(executor: Any, *args: Any, **kwargs: Any) -> Any:
        result = price(executor, *args, **kwargs)
        price_type = kwargs.get("price_type", args[2] if len(args) > 2 else None)
        _safe(_capture, executor, "price", result, price_type)
        return result

    net = cls.get_net_pnl_pct

    @functools.wraps(net)
    def get_net(executor: Any, *args: Any, **kwargs: Any) -> Any:
        result = net(executor, *args, **kwargs)
        _safe(_capture, executor, "net", result)
        return result

    close = cls.place_close_order_and_cancel_open_orders

    @functools.wraps(close)
    def close_order(executor: Any, *args: Any, **kwargs: Any) -> Any:
        def before() -> dict[str, Any]:
            frame = _CURRENT.get()
            if frame is not None and frame["executor"] is executor:
                check = frame.get("current_check")
                if check is not None:
                    check["close_requested"] = True
            close_type = kwargs.get("close_type", args[0] if args else None)
            observation = {"kind": _enum(close_type) or "unknown", "request_id": _id()}
            _event(
                executor, "close_request", observation["kind"], request_id=observation["request_id"]
            )
            return observation

        observation = _safe(before, _owner=executor) or {"kind": "unknown", "request_id": None}
        kind, request = observation["kind"], observation["request_id"]
        try:
            result = close(executor, *args, **kwargs)
        except BaseException as error:
            _safe(
                _event,
                executor,
                "close_error",
                kind,
                request_id=request,
                error_type=type(error).__name__,
            )
            raise
        _safe(_event, executor, "close_return", kind, request_id=request, is_fill=False)
        return result

    place = getattr(cls, "place_order", None)
    if place is not None and not inspect.iscoroutinefunction(place):

        @functools.wraps(place)
        def place_order(executor: Any, *args: Any, **kwargs: Any) -> Any:
            request = _id()

            def requested() -> None:
                frame = _CURRENT.get()
                check = (
                    frame.get("current_check")
                    if frame is not None and frame["executor"] is executor
                    else None
                )
                kind = check["kind"] if check is not None else "unknown"
                _event(
                    executor,
                    "order_request",
                    kind,
                    request_id=request,
                    order_type=_enum(kwargs.get("order_type")),
                    position_action=_enum(kwargs.get("position_action")),
                    price=_num(kwargs.get("price")),
                    amount=_num(kwargs.get("amount")),
                )

            _safe(requested, _owner=executor)
            try:
                result = place(executor, *args, **kwargs)
            except BaseException as error:
                _safe(
                    _event,
                    executor,
                    "order_error",
                    "native_order",
                    request_id=request,
                    error_type=type(error).__name__,
                )
                raise

            def returned() -> None:
                _event(
                    executor,
                    "order_return",
                    "native_order",
                    request_id=request,
                    order_id_hash=_hash(result),
                    is_fill=False,
                )

            _safe(returned, _owner=executor)
            return result

        cls.place_order = place_order

    custom = cls.get_custom_info

    @functools.wraps(custom)
    def get_custom(executor: Any, *args: Any, **kwargs: Any) -> Any:
        result = custom(executor, *args, **kwargs)
        try:
            state = executor.__dict__.get("_jiaoyi_stop_observation")
            if state is not None and isinstance(result, dict):
                return {**result, "stop_diagnostics": state.snapshot()}
        except Exception:
            pass
        return result

    cls.control_task = control
    cls.get_price = get_price
    cls.get_net_pnl_pct = get_net
    cls.control_stop_loss = _check_wrapper(cls.control_stop_loss, "stop_loss")
    cls.control_trailing_stop = _check_wrapper(cls.control_trailing_stop, "trailing_stop")
    cls.place_close_order_and_cancel_open_orders = close_order
    cls.get_custom_info = get_custom
    for name, stage, kind in [
        ("process_order_filled_event", "fill", "native_order_filled"),
        ("process_order_completed_event", "order_completed", "native_order_completed"),
    ]:
        original = getattr(cls, name, None)
        if original is not None and not inspect.iscoroutinefunction(original):

            def make_callback(function: Any, callback_stage: str, callback_kind: str) -> Any:
                @functools.wraps(function)
                def callback(executor: Any, *args: Any, **kwargs: Any) -> Any:
                    def owned() -> dict[str, Any] | None:
                        event = args[-1] if args else kwargs.get("event")
                        order_id = getattr(event, "order_id", None)
                        known = {
                            getattr(order, "order_id", None)
                            for order in (
                                executor.__dict__.get("_open_order"),
                                executor.__dict__.get("_close_order"),
                                executor.__dict__.get("_take_profit_limit_order"),
                            )
                            if order is not None
                        }
                        if order_id is None or order_id not in known:
                            return None
                        return {
                            "order_id_hash": _hash(order_id),
                            "price": _num(getattr(event, "price", None)),
                            "fill_amount": _num(getattr(event, "amount", None)),
                            "native_event_timestamp": _num(getattr(event, "timestamp", None)),
                            "is_fill": callback_stage == "fill",
                        }

                    observation = _safe(owned, _owner=executor)
                    result = function(executor, *args, **kwargs)
                    if observation is not None:
                        _safe(_event, executor, callback_stage, callback_kind, **observation)
                    return result

                return callback

            setattr(cls, name, make_callback(original, stage, kind))
    cls._jiaoyi_stop_diagnostics_installed = True
    return True
