"""风控员要的下单精度：币安 exchangeInfo 的 stepSize / minQty / minNotional，带缓存。

风控员的 ``LotRulesProvider.lot_rules`` 是同步的（快环里不许阻塞），所以这里分两半：
``refresh`` 异步去币安拉，``lot_rules`` 只读缓存。故障即关闭：
  - 从没拉成功过 → 返回 None，风控员拒单
  - 缓存超过 ``max_age_s`` 没刷新成功 → 返回 None（交易所改了精度我们不知道，宁可不下单）
  - 某个合约不在列表里（下架、暂停交易）→ 返回 None
刷新失败保留旧缓存，只要没过期就继续用。

Leverage brackets ride on the same refresh but only while ``brackets_wanted()`` is true: fetched
on the first refresh and again once the cache is a day old, at most one attempt per
``BRACKET_RETRY`` after a failure. A failed fetch keeps the old tiers, which never expire: they
change rarely and an unknown tier only means the guard keeps its unclamped leverage.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta

from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.execution.clock import Clock, utc_now
from jiaoyi.execution.exchange import ExchangeClient
from jiaoyi.risk.guard import LeverageBracket, LotRules

log = logging.getLogger(__name__)

BRACKET_MAX_AGE = timedelta(days=1)
BRACKET_RETRY = timedelta(minutes=10)


class ExchangeInfoLotRules:
    def __init__(
        self,
        exchange: ExchangeClient,
        *,
        max_age_s: float = 6 * 3600.0,
        clock: Clock = utc_now,
        recorder: RoleRecorder | None = None,
        brackets_wanted: Callable[[], bool] | None = None,
    ) -> None:
        self._exchange = exchange
        self._brackets_wanted = brackets_wanted
        self._brackets: dict[str, tuple[LeverageBracket, ...]] = {}
        self._brackets_at: datetime | None = None
        self._brackets_tried: datetime | None = None
        self._max_age = timedelta(seconds=max_age_s)
        self._clock = clock
        self._rec = recorder
        self._rules: dict[str, LotRules] = {}
        self._loaded_at: datetime | None = None
        self._status: dict[str, str] = {}

    async def refresh(self) -> bool:
        """拉一次。成功返回 True；失败保留旧缓存并返回 False（杠杆分层成败不影响返回值）。"""
        ok = await self._refresh_filters()
        await self._refresh_brackets()
        return ok

    async def _refresh_filters(self) -> bool:
        now = self._clock()
        try:
            filters = await self._exchange.lot_filters()
        except Exception as e:
            log.warning("刷新下单精度失败，沿用旧缓存：%r", e)
            if self._rec is not None:
                from jiaoyi.contracts import Outcome

                self._rec.record(now, Outcome.ERROR, f"刷新下单精度失败：{e!r}")
            return False
        if not filters:
            log.warning("exchangeInfo 返回空列表，沿用旧缓存")
            return False
        self._rules = {
            sym: LotRules(
                f.step_size,
                f.min_qty,
                f.min_notional,
                f.max_qty,
                f.tick_size,
                onboard_at=f.onboard_at,
                delist_at=f.delist_at,
            )
            for sym, f in filters.items()
        }
        status = getattr(self._exchange, "untradeable_status", None)
        self._status = dict(status) if isinstance(status, dict) else {}
        self._loaded_at = now
        return True

    async def _refresh_brackets(self) -> None:
        fetch = getattr(self._exchange, "leverage_brackets", None)
        if self._brackets_wanted is None or not self._brackets_wanted() or not callable(fetch):
            return
        now = self._clock()
        if self._brackets_at is not None and now - self._brackets_at < BRACKET_MAX_AGE:
            return
        if self._brackets_tried is not None and now - self._brackets_tried < BRACKET_RETRY:
            return
        self._brackets_tried = now
        try:
            raw = await fetch()
            if not raw:
                raise ValueError("返回空列表")
        except Exception as e:
            log.warning("刷新杠杆分层失败，沿用旧缓存：%r", e)
            if self._rec is not None:
                from jiaoyi.contracts import Outcome

                self._rec.record(now, Outcome.ERROR, f"刷新杠杆分层失败，沿用旧缓存：{e!r}")
            return
        self._brackets = {
            sym: tuple(LeverageBracket(cap, lev) for cap, lev in tiers)
            for sym, tiers in raw.items()
        }
        self._brackets_at = now

    def leverage_brackets(self, symbol: str) -> tuple[LeverageBracket, ...] | None:
        return self._brackets.get(symbol)

    def unavailable(self, symbols: Iterable[str]) -> dict[str, str] | None:
        """这些币里交易所现在不能交易的（币 → 交易所状态，没有精度的写「无下单精度」）。

        缓存没拉到或已过期返回 None：此时 ``lot_rules`` 对所有币都是 None，不能据此剔币。"""
        if self._loaded_at is None or self._clock() - self._loaded_at > self._max_age:
            return None
        return {s: self._status.get(s, "无下单精度") for s in symbols if self._rules.get(s) is None}

    def lot_rules(self, symbol: str) -> LotRules | None:
        if self._loaded_at is None or self._clock() - self._loaded_at > self._max_age:
            return None
        return self._rules.get(symbol)

    @property
    def loaded_at(self) -> datetime | None:
        return self._loaded_at


async def run_lot_rules_refresh(
    provider: ExchangeInfoLotRules, interval_s: float, stop: asyncio.Event
) -> None:
    """启动时先拉一次，然后每 ``interval_s`` 秒刷新，直到 ``stop``。"""
    while not stop.is_set():
        await provider.refresh()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=interval_s)
