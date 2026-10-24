"""主流币 = 市值前 N：解析、缓存、回退、币安前缀映射、手动加入 / 排除。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from jiaoyi.contracts import Category
from jiaoyi.slow.majors import MajorsSource, base_of, parse_markets
from jiaoyi.slow.screener import CodeSymbolScreener, ScreenerConfig, classify
from tests.test_slow_screener import FakeSource, stat

NOW = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)


def rows() -> list[dict[str, object]]:
    def r(rank: int, sym: str, id_: str = "") -> dict[str, object]:
        return {"symbol": sym.lower(), "id": id_ or sym.lower(), "market_cap_rank": rank}

    return [
        r(1, "btc"), r(2, "eth"), r(3, "usdt"), r(4, "xrp"), r(5, "bnb"),
        r(6, "sol"), r(7, "usdc"), r(8, "steth", "staked-ether"), r(9, "wbtc", "wrapped-bitcoin"),
        r(10, "doge"), r(11, "pepe"), r(12, "link"),
    ]  # fmt: skip


def test_parse_drops_stables_and_wrapped_keeps_rank_order() -> None:
    got = parse_markets(rows())
    assert list(got) == ["BTC", "ETH", "XRP", "BNB", "SOL", "DOGE", "PEPE", "LINK"]
    assert got["DOGE"] == 10


def test_binance_prefix_maps_to_base() -> None:
    assert base_of("1000PEPEUSDT") == "PEPE"
    assert base_of("1MBABYDOGEUSDT") == "BABYDOGE"
    assert base_of("BTCUSDT") == "BTC"
    ranks = {"PEPE": 11}
    assert classify("1000PEPEUSDT", majors=ranks) is Category.MAJOR
    assert (
        classify("BTCUSDT", majors=ranks) is Category.ALT
    )  # 有排名表就以排名为准，不再用写死的 7 个
    assert classify("BTCUSDT") is Category.MAJOR  # 没有排名表：回退写死名单
    inc, exc = frozenset({"LINK"}), frozenset({"PEPE"})
    assert classify("LINKUSDT", majors=ranks, include=inc) is Category.MAJOR
    assert classify("1000PEPEUSDT", majors=ranks, exclude=exc) is Category.ALT


def _client(calls: list[int], fail: bool = False) -> httpx.AsyncClient:
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if fail:
            return httpx.Response(429)
        return httpx.Response(200, json=rows())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_refresh_once_a_day_cache_survives_restart_and_failure(tmp_path: Path) -> None:
    path, calls = tmp_path / "majors.json", []
    src = MajorsSource(path, _client(calls))
    await src.refresh(NOW)
    await src.refresh(NOW + timedelta(hours=5))
    assert len(calls) == 1 and src.ranks(3) == {"BTC": 1, "ETH": 2, "XRP": 4}
    again = MajorsSource(path, _client(calls))  # 重启读缓存
    assert again.ranks(3) == src.ranks(3)
    bad_calls: list[int] = []
    failing = MajorsSource(path, _client(bad_calls, fail=True))
    await failing.refresh(NOW + timedelta(days=2))  # 到期了但接口挂了：沿用旧的
    assert failing.ranks(3) == src.ranks(3) and failing.error is not None and bad_calls
    empty = MajorsSource(tmp_path / "none.json", _client([], fail=True))
    await empty.refresh(NOW)
    assert empty.ranks(3) == {}  # 什么都没有：调用方回退写死名单
    assert again.ranks(2) == {"BTC": 1, "ETH": 2}


async def test_screener_marks_majors_with_rank_and_include_exclude(
    tmp_path: Path, now: datetime
) -> None:
    src = MajorsSource(tmp_path / "m.json", _client([]))
    fake = FakeSource([stat("1000PEPEUSDT"), stat("SOLUSDT"), stat("LINKUSDT"), stat("DOGEUSDT")])
    cfg = ScreenerConfig(
        major_top_n=2, major_include=frozenset({"LINK"}), major_exclude=frozenset({"DOGE"})
    )
    pool = await CodeSymbolScreener(fake, cfg, majors=src).screen(now)
    by = {e.symbol: e for e in pool.entries}
    assert (
        by["SOLUSDT"].category is Category.MAJOR and "主流币 · 市值第 6 名" in by["SOLUSDT"].reasons
    )
    assert by["1000PEPEUSDT"].category is Category.ALT  # 币安上的 SOL、DOGE 占满 2 个名额
    assert (
        by["LINKUSDT"].category is Category.MAJOR
        and "主流币（你手动加入）" in by["LINKUSDT"].reasons
    )
    assert by["DOGEUSDT"].category is Category.ALT  # 手动排除


async def test_failed_fetch_cools_down_for_an_hour() -> None:
    calls: list[int] = []
    src = MajorsSource(None, _client(calls, fail=True))
    await src.refresh(NOW)
    await src.refresh(NOW + timedelta(minutes=59))  # 冷却中：不再请求
    assert len(calls) == 1
    await src.refresh(NOW + timedelta(minutes=61))
    assert len(calls) == 2


async def test_coins_without_binance_perps_do_not_take_a_slot(tmp_path: Path) -> None:
    src = MajorsSource(tmp_path / "m.json", _client([]))
    await src.refresh(NOW)
    # 币安上没有 XRP、BNB 的永续：往下补 SOL、DOGE，前 4 个仍是 4 个真能交易的币
    assert src.ranks(4, {"BTC", "ETH", "SOL", "DOGE", "PEPE", "LINK"}) == {
        "BTC": 1, "ETH": 2, "SOL": 6, "DOGE": 10,
    }  # fmt: skip
    assert list(src.ranks(20)) == ["BTC", "ETH", "XRP", "BNB", "SOL", "DOGE", "PEPE", "LINK"]


async def test_old_cache_without_version_is_refetched(tmp_path: Path) -> None:
    import json

    path, calls = tmp_path / "majors.json", []
    path.write_text(
        json.dumps({"fetched_at": NOW.isoformat(), "top_n": 20, "ranks": {"BTC": 1}}),
        encoding="utf-8",
    )
    src = MajorsSource(path, _client(calls))
    await src.refresh(NOW + timedelta(hours=1))  # 旧格式的缓存只有前 N 个：不到一天也要重拉
    assert len(calls) == 1 and "LINK" in src.ranks(20)
