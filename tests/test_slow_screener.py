from __future__ import annotations

from datetime import datetime

import pytest

from jiaoyi.contracts import Category
from jiaoyi.slow.screener import (
    CodeSymbolScreener,
    ScreenerConfig,
    SymbolStats,
    UserOverrides,
    classify,
)


def stat(sym: str, **kw: float) -> SymbolStats:
    base = {
        "quote_volume_24h_usdt": 100e6,
        "spread_bps": 1.0,
        "depth_usdt": 500e3,
        "listed_days": 100,
        "range_24h_pct": 5.0,
    }
    return SymbolStats(symbol=sym, **{**base, **kw})  # type: ignore[arg-type]


class FakeSource:
    def __init__(self, stats: list[SymbolStats]) -> None:
        self.stats = stats

    async def fetch(self, now: datetime) -> list[SymbolStats]:
        return self.stats


async def test_hard_filters_reject(now: datetime) -> None:
    src = FakeSource(
        [
            stat("OKUSDT"),
            stat("THINUSDT", quote_volume_24h_usdt=1e6),
            stat("WIDEUSDT", spread_bps=20),
            stat("SHALLOWUSDT", depth_usdt=1e3),
            stat("NEWUSDT", listed_days=3),
        ]
    )
    pool = await CodeSymbolScreener(src).screen(now)
    by = {e.symbol: e for e in pool.entries}
    assert [e.symbol for e in pool.in_pool] == ["OKUSDT"]
    assert by["NEWUSDT"].filters.failed_reasons == ["上线不满 7 天"]
    assert not by["THINUSDT"].in_pool and not by["WIDEUSDT"].in_pool


async def test_score_orders_pool_and_size_limit(now: datetime) -> None:
    src = FakeSource([stat(f"C{i}USDT", range_24h_pct=0.1 * (i + 1)) for i in range(10)])
    pool = await CodeSymbolScreener(src, ScreenerConfig(pool_size=3)).screen(now)
    assert [e.symbol for e in pool.in_pool] == ["C9USDT", "C8USDT", "C7USDT"]


async def test_blacklist_beats_pin_and_pool(now: datetime) -> None:
    src = FakeSource([stat("AAUSDT", range_24h_pct=50), stat("BBUSDT")])
    ov = UserOverrides(pinned=frozenset({"AAUSDT"}), blacklist=frozenset({"AAUSDT"}))
    pool = await CodeSymbolScreener(src, overrides=ov).screen(now)
    a = next(e for e in pool.entries if e.symbol == "AAUSDT")
    assert a.blacklisted and not a.in_pool and not a.pinned


async def test_pin_bypasses_category_cap_but_not_hard_filters(now: datetime) -> None:
    src = FakeSource(
        [stat("AAAUSDT", range_24h_pct=9), stat("BBBUSDT", range_24h_pct=8),
         stat("PINUSDT", range_24h_pct=0.1), stat("BADUSDT", listed_days=1)]
    )  # fmt: skip
    ov = UserOverrides(pinned=frozenset({"PINUSDT", "BADUSDT"}), category_caps={Category.ALT: 2})
    pool = await CodeSymbolScreener(src, overrides=ov).screen(now)
    names = {e.symbol for e in pool.in_pool}
    assert names == {"PINUSDT", "AAAUSDT"}  # 置顶占名额但不受上限限制，BBB 被类别上限挡掉
    bad = next(e for e in pool.entries if e.symbol == "BADUSDT")
    assert bad.pinned and not bad.in_pool


async def test_empty_source_fails_closed(now: datetime) -> None:
    with pytest.raises(RuntimeError):
        await CodeSymbolScreener(FakeSource([])).screen(now)


def test_classify() -> None:
    assert classify("BTCUSDT") is Category.MAJOR
    assert classify("TSLAUSDT") is Category.US_TECH
    assert classify("COINUSDT") is Category.CRYPTO_STOCK
    assert classify("XAUUSDT") is Category.PRECIOUS_METAL
    assert classify("PEPEUSDT") is Category.ALT
