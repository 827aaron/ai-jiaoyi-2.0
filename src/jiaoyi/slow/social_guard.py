"""给 TradingAgents 的 Reddit 抓取加一道闸，避免 429 限流拖慢、刷屏。

匿名 Reddit RSS 大约“每个出口 IP 每分钟一次”，而军师团并发开会（默认 4 个标的）都会各抓一次，
结果几乎必然 429；TradingAgents 遇到 429 会睡 60 秒再试一次，白白占住会议线程。这里在它前面包一层：

- 同一个币 ``cache_s`` 内重复要，直接用上次成功的结果；
- 两次真实请求至少隔 ``min_gap_s``，间隔内来的请求**不等待**，直接返回“不可用”占位；
- 一次失败（含 429）后进入 ``cooldown_s`` 冷却，期间都返回占位，不再请求；
- ``mode = "off"`` 完全不抓。

占位文字沿用 TradingAgents 的口径——“不可用 ≠ 没有讨论”，分析师不会把没抓到当成没人聊。
Reddit 只是社交情绪的一路背景数据（另有币安公告、媒体 RSS 和 StockTwits），缺了不影响出卡。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

UNAVAILABLE = "<Reddit unavailable: {why}; this is not an absence of discussion>"


class RedditGuard:
    def __init__(
        self,
        fetch: Callable[..., str],
        *,
        mode: str = "guarded",
        min_gap_s: float = 65.0,
        cooldown_s: float = 600.0,
        cache_s: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._mode = mode
        self._min_gap = min_gap_s
        self._cooldown = cooldown_s
        self._cache_s = cache_s
        self._clock = clock
        self._lock = threading.Lock()
        self._next_allowed = 0.0
        self._blocked_until = 0.0
        self._cache: dict[tuple[str, tuple[str, ...]], tuple[float, str]] = {}

    def __call__(self, ticker: str, subreddits: Any = None, **kw: Any) -> str:
        if self._mode == "off":
            return UNAVAILABLE.format(why="disabled by config")
        key = (ticker, tuple(subreddits or ()))
        with self._lock:
            now = self._clock()
            hit = self._cache.get(key)
            if hit is not None and now - hit[0] < self._cache_s:
                return hit[1]
            if now < self._blocked_until:
                return UNAVAILABLE.format(why="skipped, recently rate-limited")
            if now < self._next_allowed:
                return UNAVAILABLE.format(why="skipped to respect the rate limit")
            self._next_allowed = now + self._min_gap
        text = self._fetch(ticker, subreddits, **kw)  # 可能睡 60 秒，所以放在锁外
        with self._lock:
            done = self._clock()
            if text.startswith("<Reddit unavailable"):
                self._blocked_until = done + self._cooldown
            else:
                self._cache[key] = (done, text)
        return text


def register_reddit_guard(guard: RedditGuard) -> None:
    """把 TradingAgents 情绪分析师里的 ``fetch_reddit_posts`` 换成带闸的版本。"""
    from tradingagents.agents.analysts import sentiment_analyst

    sentiment_analyst.fetch_reddit_posts = guard
