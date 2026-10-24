from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.slow.news import (
    BinanceAnnouncements,
    NewsHub,
    NewsItem,
    NewsKind,
    OkxAnnouncements,
    RssNews,
    _mentions,
    classify_title,
    format_items,
)
from jiaoyi.slow.tradingagents_engine import build_config, make_news_vendors

NOW = datetime(2026, 10, 23, 8, 0, tzinfo=UTC)
MS = int(NOW.timestamp() * 1000)


def client(handler: httpx.MockTransport | object) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)  # type: ignore[arg-type]


def binance_transport() -> httpx.MockTransport:
    def h(req: httpx.Request) -> httpx.Response:
        cat = int(req.url.params["catalogId"])
        title = {161: "币安合约将下架 PTBUSDT 和 MITOUSDT U本位永续合约", 48: "币安将上线 FOO"}.get(
            cat, "币安更新 API"
        )
        art = {"id": cat * 10, "code": f"c{cat}", "title": title, "releaseDate": MS}
        return httpx.Response(
            200,
            json={"code": "000000", "data": {"catalogs": [{"catalogId": cat, "articles": [art]}]}},
        )

    return httpx.MockTransport(h)


async def test_binance_parses_and_types() -> None:
    items = await BinanceAnnouncements(client(binance_transport())).fetch()
    by = {i.item_id: i for i in items}
    assert by["1610"].kind is NewsKind.DELISTING and by["480"].kind is NewsKind.LISTING
    assert by["1610"].url.endswith("/c161") and by["1610"].published_at == NOW


async def test_binance_schema_change_raises() -> None:
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"code": "000000", "data": {}}))
    with pytest.raises(KeyError):
        await BinanceAnnouncements(client(t)).fetch()
    t2 = httpx.MockTransport(lambda r: httpx.Response(200, json={"code": "50001"}))
    with pytest.raises(ValueError):
        await BinanceAnnouncements(client(t2)).fetch()


async def test_okx_parses() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        t = req.url.params["annType"]
        d = {"annType": t, "title": f"欧易 {t}", "url": f"https://okx/{t}", "pTime": str(MS)}
        return httpx.Response(200, json={"code": "0", "data": [{"details": [d]}]})

    items = await OkxAnnouncements(client(httpx.MockTransport(h))).fetch()
    kinds = {i.kind for i in items}
    assert NewsKind.DELISTING in kinds and NewsKind.LISTING in kinds and len(items) == 3
    assert "announcements-trading-rules" not in OkxAnnouncements.TYPES  # 接口已下线，请求会 400


async def test_okx_one_bad_type_does_not_sink_the_source() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        t = req.url.params["annType"]
        if t == "announcements-api":
            return httpx.Response(400, json={"code": "51000", "data": []})  # 类型改名 / 下线
        d = {"annType": t, "title": f"欧易 {t}", "url": f"https://okx/{t}", "pTime": str(MS)}
        return httpx.Response(200, json={"code": "0", "data": [{"details": [d]}]})

    items = await OkxAnnouncements(client(httpx.MockTransport(h))).fetch()
    assert len(items) == 2  # 其它类照常
    dead = httpx.MockTransport(lambda r: httpx.Response(500))
    with pytest.raises(ValueError, match="全部失败"):
        await OkxAnnouncements(client(dead)).fetch()


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Bitcoin ETF inflows surge</title><link>https://m/1</link>
<pubDate>Fri, 23 Oct 2026 07:00:00 +0000</pubDate>
<description>&lt;p&gt;Big week&lt;/p&gt;</description></item>
<item><title>no link</title><pubDate>Fri, 23 Oct 2026 07:00:00 +0000</pubDate></item>
</channel></rss>"""


async def test_rss_parses_and_strips_html() -> None:
    t = httpx.MockTransport(lambda r: httpx.Response(200, content=RSS))
    items = await RssNews("coindesk", "https://x", client(t)).fetch()
    assert len(items) == 1 and items[0].summary == "Big week" and items[0].kind is NewsKind.MEDIA


async def test_rss_rejects_xml_bomb_and_empty() -> None:
    bomb = '<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "x"><!ENTITY b "&a;&a;">]><rss>&b;</rss>'
    for body in (bomb, "<rss><channel/></rss>"):
        t = httpx.MockTransport(lambda r, b=body: httpx.Response(200, content=b))
        with pytest.raises(Exception):  # noqa: B017  defusedxml 的异常或我们的 ValueError
            await RssNews("x", "https://x", client(t)).fetch()


class Fake:
    def __init__(self, name: str, items: list[NewsItem] | Exception) -> None:
        self.name, self.items = name, items

    async def fetch(self) -> list[NewsItem]:
        if isinstance(self.items, Exception):
            raise self.items
        return self.items


def item(src: str, i: str, title: str, kind: NewsKind, age_h: float = 1) -> NewsItem:
    return NewsItem(src, i, title, NOW - timedelta(hours=age_h), kind=kind)


async def test_hub_fail_soft_dedup_and_status_log(tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path)
    good = Fake("binance", [item("binance", "1", "下架 PTBUSDT", NewsKind.DELISTING)])
    bad = Fake("okx", RuntimeError("403"))
    hub = NewsHub([good, bad], log, clock=lambda: NOW)
    assert len(await hub.poll()) == 1
    assert await hub.poll() == []  # 去重
    assert hub.down_sources == {"okx"}
    recs = log.read_day(NOW.date().isoformat())
    assert len(recs) == 1 and "okx" in recs[0].summary  # 连续失败只记一次
    bad.items = [item("okx", "9", "上线 FOO", NewsKind.LISTING)]
    await hub.poll()
    assert hub.down_sources == set() and "恢复" in log.read_day(NOW.date().isoformat())[-1].summary


async def test_all_sources_down_means_empty_not_error() -> None:
    hub = NewsHub([Fake("a", RuntimeError("x"))], clock=lambda: NOW)
    assert await hub.poll() == []
    assert hub.for_symbol("BTCUSDT") == [] and hub.market() == []
    assert "no relevant news" in format_items([])


async def test_poll_and_notify_only_for_announcements() -> None:
    src = Fake(
        "s",
        [
            item("binance", "1", "币安将下架 PEPEUSDT", NewsKind.DELISTING),
            item("coindesk", "2", "Solana network hits record", NewsKind.MEDIA),
        ],
    )
    hub = NewsHub([src], clock=lambda: NOW)
    hit: list[str] = []
    await hub.poll_and_notify(["PEPEUSDT", "SOLUSDT"], hit.append)
    assert hit == ["PEPEUSDT"]  # 媒体新闻不触发开会


async def test_delisting_symbols_and_retention() -> None:
    src = Fake(
        "s",
        [
            item("binance", "1", "币安合约将下架 PTBUSDT 和 MITOUSDT", NewsKind.DELISTING),
            item("binance", "2", "币安将上线 FOO", NewsKind.LISTING),
            item("binance", "3", "旧的下架 OLDUSDT", NewsKind.DELISTING, age_h=100),
        ],
    )
    hub = NewsHub([src], clock=lambda: NOW)
    await hub.poll()
    assert hub.delisting_symbols(["PTBUSDT", "MITOUSDT", "BTCUSDT", "OLDUSDT"]) == {
        "PTBUSDT",
        "MITOUSDT",
    }


def test_symbol_matching() -> None:
    assert _mentions("币安合约将下架 PTBUSDT 和 MITOUSDT", "PTB")  # 短代码 + USDT
    assert _mentions("币安将下架 EPIC、QUICK", "EPIC")
    assert not _mentions("Why AI is changing everything", "AI")  # 短代码裸词不算
    assert _mentions("Buy $AI now", "AI")
    assert not _mentions("EPICENTER news", "EPIC")  # 不是整词
    assert _mentions("Bitcoin ETF inflows surge", "BTC")  # 别名
    assert classify_title("币安将下架 X") is NewsKind.DELISTING


def test_vendors_use_hub_for_crypto_and_fall_back_for_stocks() -> None:
    hub = NewsHub([], clock=lambda: NOW)
    hub._items["a:1"] = item("a", "1", "Bitcoin ETF inflows surge", NewsKind.MEDIA)
    calls: list[str] = []

    def stock_news(t: str, s: str, e: str) -> str:
        calls.append(t)
        return "yf news"

    def boom(*a: object) -> str:
        raise RuntimeError("yahoo down")

    get_news, get_global = make_news_vendors(hub, stock_news, boom)
    assert "Bitcoin ETF" in get_news("BTC-USD", "2026-10-22", "2026-10-23") and not calls
    assert get_news("TSLA", "2026-10-22", "2026-10-23") == "yf news" and calls == ["TSLA"]
    assert "Bitcoin ETF" in get_global("2026-10-23")
    empty = make_news_vendors(NewsHub([], clock=lambda: NOW), None, boom)[1]("2026-10-23")
    assert (
        "no relevant news" in empty and "yahoo down" in empty
    )  # 没新闻 + 原源也挂：会议照常，文本说明


def test_build_config_switches_news_vendor() -> None:
    from jiaoyi.slow.tradingagents_engine import TradingAgentsSettings

    base = {"data_vendors": {"news_data": "yfinance"}}
    s = TradingAgentsSettings()
    assert build_config(base, s)["data_vendors"]["news_data"] == "yfinance"
    assert build_config(base, s, crypto_news=True)["data_vendors"]["news_data"] == "crypto_news"


async def test_binance_delisting_blocks_symbol_but_okx_and_media_do_not() -> None:
    src = Fake(
        "s",
        [
            item(
                "binance",
                "1",
                "币安合约将下架 PTBUSDT 和 MITOUSDT U本位永续合约",
                NewsKind.DELISTING,
            ),
            item("okx", "2", "欧易将下架 FOOUSDT", NewsKind.DELISTING),
            item("binance", "3", "币安将下架 BARUSDT", NewsKind.DELISTING, age_h=24 * 8),
            item("coindesk", "4", "BAZ delisted", NewsKind.MEDIA),
        ],
    )
    hub = NewsHub([src], clock=lambda: NOW)
    blocked: dict[str, str] = {}
    await hub.poll_and_notify(
        ["PTBUSDT", "MITOUSDT", "FOOUSDT", "BARUSDT", "BTCUSDT"],
        lambda s: None,
        lambda s, r: blocked.setdefault(s, r),
    )
    assert set(blocked) == {"PTBUSDT", "MITOUSDT"}  # 非币安、超过 7 天、媒体的都不挡


async def test_rss_size_cap_and_aclose() -> None:
    big = b"<rss>" + b"x" * 2_100_000 + b"</rss>"
    t = httpx.MockTransport(lambda r: httpx.Response(200, content=big))
    with pytest.raises(ValueError, match="超过"):
        await RssNews("x", "https://x", client(t)).fetch()
    src = RssNews("x", "https://x", client(httpx.MockTransport(lambda r: httpx.Response(200))))
    await NewsHub([src], clock=lambda: NOW).aclose()
    assert src._c.is_closed


async def test_delisting_symbols_only_counts_binance() -> None:
    src = Fake(
        "s",
        [
            item("binance", "1", "币安将下架 AAAAUSDT", NewsKind.DELISTING),
            item("okx", "2", "欧易将下架 BBBBUSDT", NewsKind.DELISTING),
        ],
    )
    hub = NewsHub([src], clock=lambda: NOW)
    await hub.poll()
    assert hub.delisting_symbols(["AAAAUSDT", "BBBBUSDT"]) == {"AAAAUSDT"}


async def test_flapping_source_logs_at_most_one_error_per_gap(tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path)
    clock = [NOW]
    flaky = Fake("okx", RuntimeError("429"))
    hub = NewsHub([flaky], log, clock=lambda: clock[0])
    for _ in range(4):  # 反复 失败 → 恢复 → 失败
        flaky.items = RuntimeError("429")
        await hub.poll()
        flaky.items = []
        await hub.poll()
        clock[0] += timedelta(minutes=5)
    errs = [r for r in log.read_day(NOW.date().isoformat()) if r.outcome.value == "error"]
    assert len(errs) == 1  # 30 分钟内只记一条
    clock[0] += timedelta(minutes=40)
    flaky.items = RuntimeError("429")
    await hub.poll()
    errs = [r for r in log.read_day(NOW.date().isoformat()) if r.outcome.value == "error"]
    assert len(errs) == 2
