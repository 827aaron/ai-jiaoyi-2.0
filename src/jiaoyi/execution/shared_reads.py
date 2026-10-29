"""保护轮里多笔仓共用一次持仓读取。

全账户持仓读取是主限速桶里最贵的一类（权重 5），每笔仓每轮要读两次（同步前取证、改单后核对）。
一轮里各仓并发跑，到了要读持仓时先在这里排队：本轮还没结束的仓都在等（或已结束）就发一次，
大家共用结果。每个调用方拿到的读取都是在它提出请求之后才发出的，所以「前后两次证据一致」的
核对含义不变，只是少发了请求。有人迟迟不来（在等别的锁、在读别的东西）时最多等 ``max_wait_s``
就先发，不会互相卡死。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class SharedRead(Generic[T]):
    def __init__(self, read: Callable[[], Awaitable[T]], *, max_wait_s: float) -> None:
        self._read = read
        self._max_wait_s = max_wait_s
        self._active = 0
        self._waiting = 0
        self._next: asyncio.Future[T] | None = None
        self._timer: asyncio.TimerHandle | None = None
        self._running: set[asyncio.Task[None]] = set()
        self.reads = 0
        """实际发出的读取次数（测试和日志用）。"""

    def join(self) -> None:
        """登记一个参与者。要在它的任务开始跑之前登记，否则先到的人会以为只有自己、马上就发。"""
        self._active += 1

    def leave(self) -> None:
        self._active -= 1
        self._maybe_issue()

    async def __call__(self) -> T:
        if self._next is None:
            self._next = asyncio.get_running_loop().create_future()
            self._timer = asyncio.get_running_loop().call_later(self._max_wait_s, self._issue)
        future = self._next
        self._waiting += 1
        self._maybe_issue()
        return await asyncio.shield(future)

    def _maybe_issue(self) -> None:
        if self._next is not None and self._waiting >= self._active:
            self._issue()

    def _issue(self) -> None:
        future, self._next, self._waiting = self._next, None, 0
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if future is None:
            return
        self.reads += 1
        task = asyncio.ensure_future(self._run(future))
        self._running.add(task)
        task.add_done_callback(self._running.discard)

    async def _run(self, future: asyncio.Future[T]) -> None:
        try:
            result = await self._read()
        except asyncio.CancelledError:
            future.cancel()
            raise
        except BaseException as error:
            future.set_exception(error)
            future.add_done_callback(lambda f: f.exception())  # 等的人都被取消时不报「没人取」
        else:
            future.set_result(result)


_current: ContextVar[SharedRead[Any] | None] = ContextVar("shared_positions_read", default=None)


def current() -> SharedRead[Any] | None:
    """本任务所在保护轮的共用持仓读取；不在保护轮里返回 None（照常直接读）。"""
    return _current.get()


@contextlib.contextmanager
def attached(shared: SharedRead[Any]) -> Iterator[None]:
    """本任务（已 ``join`` 过）用这一轮的共用读取；结束（含异常）时退出，别人不再等它。"""
    token = _current.set(shared)
    try:
        yield
    finally:
        _current.reset(token)
        shared.leave()
