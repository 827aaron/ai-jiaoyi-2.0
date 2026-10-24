"""分析师（会议线程里同步的 httpx）拉币安数据的全局闸，和快环 / 离线工具共用一套封禁与权重信号。

封禁状态在 ``market.rest``：任何一边收到 418 / 429，另一边立刻也不再请求（多发只会延长封禁）。
这里额外做两件事：全局最小请求间隔；本 IP 这一分钟已用权重过半（响应头）就等下一分钟。
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx

from jiaoyi.market import rest
from jiaoyi.market.rest import IP_WEIGHT_PER_MIN, RateLimited

__all__ = ["MIN_GAP_S", "RateLimited", "banned_until", "get", "reset"]

MIN_GAP_S = 0.05  # 全局最小请求间隔

_lock = threading.Lock()
_next_at = 0.0


def banned_until() -> float:
    """还在封禁期返回解封 unix 时间，否则 0。"""
    b = rest.blocked()
    return b.until if b is not None and b.until > time.time() else 0.0


def reset() -> None:
    global _next_at
    with _lock:
        _next_at = 0.0
    rest._blocked = None
    rest._used_weight = None


def get(url: str, params: dict[str, Any] | None = None, timeout: float = 10.0) -> httpx.Response:
    """共用限速的 GET；418 / 429 登记封禁并抛 ``RateLimited``，其他状态码交给调用方。"""
    global _next_at
    rest.check_blocked()
    used = rest.take_used_weight()
    if used is not None and used > IP_WEIGHT_PER_MIN // 2:
        time.sleep(60.0 - time.time() % 60.0 + 1.0)  # 等到币安下一分钟窗口
    with _lock:
        now = time.time()
        wait = _next_at - now
        _next_at = max(now, _next_at) + MIN_GAP_S
    if wait > 0:
        time.sleep(wait)
    rest.check_blocked()  # 排队期间别的线程可能刚被封
    r = httpx.get(url, params=params, timeout=timeout)
    if r.status_code in (418, 429):
        raise rest.mark_blocked(r.status_code, r.headers)
    rest.note_used_weight(r.headers.get("X-MBX-USED-WEIGHT-1M"))
    return r
