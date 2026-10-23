"""币安公共接口的全局闸：分析师拉 K 线共用一个限速，遇到 418 / 429 就整体暂停到解封。

分析师在会议线程里同步拉数据，七场会同时开很容易撞限流。418 是 IP 被封（``Retry-After`` 秒数），
429 是请求太快。一旦收到，记下“解封时间”；封禁期间所有请求直接抛 ``RateLimited``，不再打给币安，
否则每多发一个请求都会延长封禁。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

MIN_GAP_S = 0.05  # 全局最小请求间隔
DEFAULT_BAN_S = 120.0  # 币安没给 Retry-After 时的保守等待
MAX_BAN_S = 3600.0 * 3

_lock = threading.Lock()
_next_at = 0.0
_banned_until = 0.0


class RateLimited(RuntimeError):
    """币安限流 / 封禁；``until`` 是解封的 unix 时间。"""

    def __init__(self, until: float, status: int = 418) -> None:
        self.until = until
        self.status = status
        super().__init__(f"币安限流（HTTP {status}），{max(0, int(until - time.time()))} 秒后解封")


def banned_until() -> float:
    """还在封禁期返回解封 unix 时间，否则 0。"""
    return _banned_until if _banned_until > time.time() else 0.0


def reset() -> None:
    global _banned_until, _next_at
    with _lock:
        _banned_until, _next_at = 0.0, 0.0


def _retry_after(r: httpx.Response) -> float:
    try:
        return min(max(float(r.headers.get("Retry-After", "")), 1.0), MAX_BAN_S)
    except ValueError:
        return DEFAULT_BAN_S


def get(url: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> httpx.Response:
    """共用限速的 GET；418 / 429 记封禁并抛 ``RateLimited``，其他状态码交给调用方。"""
    global _next_at, _banned_until
    with _lock:
        now = time.time()
        if _banned_until > now:
            raise RateLimited(_banned_until)
        wait = _next_at - now
        _next_at = max(now, _next_at) + MIN_GAP_S
    if wait > 0:
        time.sleep(wait)
    r = httpx.get(url, params=params, timeout=timeout)
    if r.status_code in (418, 429):
        until = time.time() + _retry_after(r)
        with _lock:
            _banned_until = max(_banned_until, until)
        raise RateLimited(until, r.status_code)
    return r
