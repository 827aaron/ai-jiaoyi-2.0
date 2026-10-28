"""开会过程实时落盘：面板据此显示每个标的的会议在讲什么。

只旁听，不改决策：把 TradingAgents 图的执行从 ``invoke`` 换成同一张图的 ``stream``
（返回的最终状态与 ``invoke`` 相同，不多一次模型调用），每走一步把新增的发言追加到
``<目录>/<SYMBOL>.json``（临时文件 + rename，整文件重写）。会后保留为上一场记录，
下一场开会时覆盖。写盘失败只记日志，绝不影响会议。字段说明见 docs/slow.md。

并发：一场会由多个线程写——TradingAgents 的分析师并行跑在 LangGraph 的线程池里，模型调用的
回调（``note_call`` 等）在各自线程里触发，``observe`` 在跑图的线程里，超时叫停（``fail``）在
事件循环线程里。所以每份实况一把锁（改内容 + 序列化在锁里），每个文件一把锁（换文件在锁里），
临时文件名各不相同。同一个币同时可能有两场会（超时取消的那场后台线程还在跑，下一场已经开了）：
文件只归最近开始的那场写，较早的那场之后的写入一律跳过，不会把新会的实况改回旧会。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PROCESS_ID = uuid.uuid4().hex
"""本进程的标识，写进每份实况；启动时用它认出「上一个进程留下的、还标着正在开」的会议。"""
INTERRUPTED_REASON = "主程序重启，会议中断"

_OPEN: set[MeetingLive] = set()
_OPEN_LOCK = threading.Lock()
_OWNERS: dict[Path, MeetingLive] = {}  # 每个文件现在归哪场会写；受 _OPEN_LOCK 保护
_PATH_LOCKS: dict[Path, threading.Lock] = {}


def _path_lock(path: Path) -> threading.Lock:
    with _OPEN_LOCK:
        return _PATH_LOCKS.setdefault(path, threading.Lock())


def _write_atomic(path: Path, text: str) -> None:
    """同目录唯一临时文件 + ``os.replace``：别的写者的临时文件不会被这次换走。调用方持文件锁。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# 图状态里每种发言的位置，按会议进行的先后排：分析师报告 → 多空辩论 → 研究经理 → 交易员 →
# 风控辩论 → 组合经理。同一份状态里一次出现好几步时也按这个顺序记。
# kind: "report"/"verdict" = 单值，"history" = 逐轮拼接的历史（只取新增的那一段）
_STEPS: tuple[tuple[str, str, str, str, str], ...] = (
    ("report", "", "market_report", "analysts", "market"),
    ("report", "", "sentiment_report", "analysts", "social"),
    ("report", "", "news_report", "analysts", "news"),
    ("report", "", "fundamentals_report", "analysts", "fundamentals"),
    ("history", "investment_debate_state", "bull_history", "debate", "bull"),
    ("history", "investment_debate_state", "bear_history", "debate", "bear"),
    ("verdict", "", "investment_plan", "debate", "research_manager"),
    ("verdict", "", "trader_investment_plan", "trader", "trader"),
    ("history", "risk_debate_state", "aggressive_history", "risk", "aggressive"),
    ("history", "risk_debate_state", "conservative_history", "risk", "conservative"),
    ("history", "risk_debate_state", "neutral_history", "risk", "neutral"),
    ("verdict", "", "final_trade_decision", "risk", "risk_manager"),
)


class MeetingLive:
    """一个标的一场会议的实时记录。"""

    def __init__(
        self,
        directory: Path,
        symbol: str,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._path = (directory / f"{symbol}.json").absolute()
        self._now = now
        self._lock = threading.RLock()
        self._doc: dict[str, Any] = {}
        self._seen: dict[tuple[str, str], int] = {}
        self._warned = False
        self._closed = False
        started = now()
        self._doc = {
            "symbol": symbol,
            "meeting_id": f"{symbol}-{started:%Y%m%dT%H%M%S}",
            "status": "running",
            "process": PROCESS_ID,
            "started_at": started.isoformat(),
            "updated_at": started.isoformat(),
            "finished_at": None,
            "stage": "analysts",
            "steps": [],
            "result": None,
            "error": None,
            "cost_usd": 0.0,
            "cost_estimated": False,
            "llm_calls": [],
            "truncated": 0,
            "retries": 0,
            "retry_reasons": [],
        }
        with _OPEN_LOCK:
            _OPEN.add(self)
            _OWNERS[self._path] = self
        self._flush()

    @property
    def doc(self) -> dict[str, Any]:
        return self._doc

    def _add(self, stage: str, who: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        ts = self._now().isoformat()
        self._doc["steps"].append({"stage": stage, "who": who, "ts": ts, "text": text})
        self._doc["stage"] = stage

    def step(self, stage: str, who: str, text: str) -> None:
        """直接记一条发言（不从图状态里取）：总盘会这类不走 TradingAgents 图的会议用。"""
        with self._lock:
            if self._closed:
                return
            self._add(stage, who, text)
            self._doc["updated_at"] = self._now().isoformat()
            self._flush()

    def observe(self, state: Mapping[str, Any]) -> None:
        """看一份（部分）图状态，把新出现的发言记下来。可重复喂同一份。"""
        with self._lock:
            if self._closed:
                return
            try:
                before = len(self._doc["steps"])
                for kind, outer, key, stage, who in _STEPS:
                    if kind == "history":
                        inner = state.get(outer)
                        if isinstance(inner, Mapping):
                            self._grow((stage, who), inner.get(key), stage, who)
                    else:
                        self._once((stage, who), state.get(key), stage, who)
                if len(self._doc["steps"]) != before:
                    self._doc["updated_at"] = self._now().isoformat()
                    self._flush()
            except Exception:
                self._warn()

    def _once(self, key: tuple[str, str], value: Any, stage: str, who: str) -> None:
        if isinstance(value, str) and value.strip() and key not in self._seen:
            self._seen[key] = len(value)
            self._add(stage, who, value)

    def _grow(self, key: tuple[str, str], value: Any, stage: str, who: str) -> None:
        if not isinstance(value, str):
            return
        done = self._seen.get(key, 0)
        if len(value) > done:
            self._seen[key] = len(value)
            self._add(stage, who, value[done:])

    def note_retry(self, why: str) -> None:
        """某一步回答有问题（length 被截断 / empty 空 / short 过短），已自动重试一次。"""
        with self._lock:
            if self._closed:
                return
            self._doc["retries"] += 1
            reasons = self._doc["retry_reasons"]
            reasons.append({"ts": self._now().isoformat(), "why": why})
            del reasons[:-40]
            self._doc["updated_at"] = self._now().isoformat()
            self._flush()

    def note_call(
        self,
        finish_reason: str | None,
        output_tokens: int,
        reasoning_tokens: int = 0,
        retries: int = 0,
    ) -> None:
        """记一次模型调用怎么结束的：``length`` = 被 max_tokens 截断，实况里一眼能看出。"""
        with self._lock:
            if self._closed:
                return
            calls = self._doc["llm_calls"]
            calls.append(
                {
                    "finish_reason": finish_reason,
                    "output_tokens": output_tokens,
                    "reasoning_tokens": reasoning_tokens,
                    "retries": retries,  # 1 = 这次调用本身是上一次出问题后的自动重试
                }
            )
            del calls[:-80]  # 一场会不会超过几十次调用；防万一只留最近的
            if finish_reason == "length":
                self._doc["truncated"] += 1
            self._doc["updated_at"] = self._now().isoformat()
            self._flush()

    def note_model(self, model: str) -> None:
        """补记最近一次调用实际用的模型 id（紧跟在 ``note_call`` 之后），方便和设置核对。"""
        with self._lock:
            if self._closed or not self._doc["llm_calls"]:
                return
            self._doc["llm_calls"][-1]["model"] = model
            self._flush()

    def note_usage(self, input_tokens: int, cached_tokens: int) -> None:
        """补记最近一次调用的输入 tokens 和命中缓存的输入 tokens（紧跟在 ``note_call`` 之后）。"""
        with self._lock:
            if self._closed or not self._doc["llm_calls"]:
                return
            last = self._doc["llm_calls"][-1]
            last["input_tokens"] = input_tokens
            last["cached_tokens"] = cached_tokens
            self._flush()

    def set_rejections(self, rows: list[dict[str, str]]) -> None:
        """总盘会没挑中的币和各自的理由（结构化：``symbol`` / ``reason``），面板按币显示。"""
        with self._lock:
            self._doc["rejections"] = rows

    def set_summary(self, text: str) -> None:
        """一句话摘要（总盘会：「挑了 N 个 / 落选 M 个」），团队页节点显示。"""
        with self._lock:
            self._doc["summary"] = text

    def converting(self) -> None:
        """军师团讨论完毕，开始整理成作战指令。"""
        with self._lock:
            if self._closed:
                return
            self._doc["stage"] = "decision"
            self._doc["updated_at"] = self._now().isoformat()
            self._flush()

    def finish(self, bias: str, reason: str, text: str, cost_usd: float) -> None:
        with self._lock:
            if self._closed:
                return
            self._add("decision", "convert", text)
            self._doc.update(
                result={"bias": bias, "reason": reason}, status="done", cost_usd=cost_usd
            )
            self._close()

    def fail(self, error: str, cost_usd: float, estimated: bool = False) -> None:
        with self._lock:
            if self._closed:  # 超时取消时已经标成失败；后台线程之后才结束，不能改回去
                return
            self._doc.update(
                status="failed", error=error, cost_usd=cost_usd, cost_estimated=estimated
            )
            self._close()

    def interrupt(self, reason: str = INTERRUPTED_REASON) -> None:
        """进程要退出了：标成中断。工作线程之后再写的任何东西都被忽略，不会把它改回「正在开」。"""
        with self._lock:
            if self._closed:
                return
            self._doc.update(status="interrupted", error=reason)
            self._close()

    def _close(self) -> None:
        self._closed = True
        with _OPEN_LOCK:
            _OPEN.discard(self)
        now = self._now().isoformat()
        self._doc["finished_at"] = self._doc["updated_at"] = now
        self._flush()

    def _flush(self) -> None:
        try:
            with self._lock:
                text = json.dumps(self._doc, ensure_ascii=False)
                with _path_lock(self._path):
                    with _OPEN_LOCK:
                        owner = _OWNERS.get(self._path) is self
                    if owner:
                        _write_atomic(self._path, text)
        except Exception:
            self._warn()

    def _warn(self) -> None:
        if not self._warned:
            self._warned = True
            logger.exception("会议实况写盘失败（%s），不影响开会", self._path)


def interrupt_open(reason: str = INTERRUPTED_REASON) -> int:
    """把本进程里所有还在开的会议标成中断（退出收尾时调；会议线程取消不掉，等不到它们自己收尾）。"""
    with _OPEN_LOCK:
        open_ = list(_OPEN)
    for live in open_:
        live.interrupt(reason)
    return len(open_)


def sweep_interrupted(
    directory: Path,
    *,
    reason: str = INTERRUPTED_REASON,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[str]:
    """启动时：目录里还标着正在开、却不是本进程写的实况，说明上一个进程死在会议中途，标成中断。

    返回被改的文件名（不含扩展名）。读不出或格式不对的文件原样不动。
    """
    swept: list[str] = []
    if not directory.is_dir():
        return swept
    for path in sorted(directory.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(doc, dict)
                or doc.get("status") != "running"
                or doc.get("process") == PROCESS_ID
            ):
                continue
            stamp = now().isoformat()
            doc.update(status="interrupted", error=reason, finished_at=stamp, updated_at=stamp)
            with _path_lock(path.absolute()):
                _write_atomic(path, json.dumps(doc, ensure_ascii=False))
            swept.append(path.stem)
        except (OSError, ValueError):
            logger.warning("清理中断的会议实况失败（%s）", path, exc_info=True)
    return swept


class _Tap:
    """包住 LangGraph 编译好的图：``invoke`` 改走 ``stream``，边走边喂给 ``MeetingLive``。

    stream_mode 用 ["values", "tasks"] + subgraphs（与 TradingAgents 自己的 ``stream_run`` 相同）：
    各分析师在自己的子图里干活，全部结束前顶层状态里没有报告，要从子图的任务结果里取。
    """

    def __init__(self, graph: Any, live: MeetingLive) -> None:
        self._graph = graph
        self._live = live

    def __getattr__(self, name: str) -> Any:
        return getattr(self._graph, name)

    def invoke(self, graph_input: Any, **args: Any) -> Any:
        args = {**args, "stream_mode": ["values", "tasks"]}
        final: Any = None
        for namespace, mode, chunk in self._graph.stream(graph_input, subgraphs=True, **args):
            if namespace:
                result = chunk.get("result") if mode == "tasks" else None
                if isinstance(result, Mapping):
                    self._live.observe(result)
            elif mode == "values":
                final = chunk
                self._live.observe(chunk)
        return final


def tap_graph(trading_graph: Any, live: MeetingLive) -> None:
    """让 ``trading_graph.propagate`` 在跑图时顺手记录会议过程；决策逻辑与返回值不变。"""
    trading_graph.graph = _Tap(trading_graph.graph, live)
