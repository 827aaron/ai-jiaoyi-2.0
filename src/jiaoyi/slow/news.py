"""军师团的新闻 / 公告情报（替换 TradingAgents 自带的 yfinance 新闻）。

来源可插拔（``NewsSource``），故障即降级而不是中断：
1. 币安公告（主源，公开接口，中文）：上新、下架、合约变化、API 更新
2. 欧易公告（辅源，官方公开接口）
3. 加密媒体 RSS（CoinDesk、Cointelegraph、The Block）：行业新闻，只当背景，不触发开会

某个源挂了：记一条日志（状态变化时才记，不刷屏），其余源照常；全挂了会议照开，只是没有新闻。
``NewsHub`` 只读缓存的方法是同步的，TradingAgents 在工作线程里直接读，不碰网络。
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import Any, Protocol

import httpx
from defusedxml import ElementTree

from jiaoyi.contracts import DecisionRecord, Outcome, Role
from jiaoyi.interfaces import DecisionLog

MAX_RSS_BYTES = 2_000_000
BLOCK_WINDOW = timedelta(days=7)
"""下架公告多久之内算数。合约真正下架通常在公告后几天。"""


class NewsKind(StrEnum):
    LISTING = "listing"
    DELISTING = "delisting"
    CONTRACT = "contract"
    """合约规则 / 杠杆 / 保证金等变化。"""
    ANNOUNCEMENT = "announcement"
    MEDIA = "media"
    """媒体新闻，只当背景。"""


@dataclass(frozen=True)
class NewsItem:
    source: str
    item_id: str
    title: str
    published_at: datetime
    url: str = ""
    kind: NewsKind = NewsKind.MEDIA
    summary: str = ""

    @property
    def key(self) -> str:
        return f"{self.source}:{self.item_id}"


class NewsSource(Protocol):
    name: str

    async def fetch(self) -> list[NewsItem]: ...


_DELIST = re.compile(r"下架|移除|delist|remov", re.I)
_LIST = re.compile(r"上线|上新|上市|开启|launch|list(?:ing)?\b|will list", re.I)
_CONTRACT = re.compile(
    r"调整|杠杆|保证金|合约|永续|adjust|leverage|margin|perpetual|contract", re.I
)


def classify_title(title: str) -> NewsKind:
    if _DELIST.search(title):
        return NewsKind.DELISTING
    if _LIST.search(title):
        return NewsKind.LISTING
    if _CONTRACT.search(title):
        return NewsKind.CONTRACT
    return NewsKind.ANNOUNCEMENT


def _ms(v: Any) -> datetime:
    return datetime.fromtimestamp(int(v) / 1000, tz=UTC)


class BinanceAnnouncements:
    """币安公告页背后的公开接口（非官方，改版会坏，所以解析失败直接报错而不是猜）。"""

    name = "binance"
    URL = "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query"
    DETAIL = "https://www.binance.com/zh-CN/support/announcement/detail/"
    # 48 上新、161 下架、49 最新动态、51 API 更新
    CATALOGS = (48, 161, 49, 51)

    def __init__(self, client: httpx.AsyncClient | None = None, page_size: int = 20) -> None:
        self._c = client or httpx.AsyncClient(timeout=10.0, headers={"lang": "zh-CN"})
        self._size = page_size

    async def _one(self, catalog: int) -> list[NewsItem]:
        r = await self._c.get(
            self.URL,
            params={"type": 1, "catalogId": catalog, "pageNo": 1, "pageSize": self._size},
        )
        r.raise_for_status()
        body = r.json()
        if body.get("code") != "000000":
            raise ValueError(f"币安公告接口返回 code={body.get('code')}")
        out: list[NewsItem] = []
        for cat in body["data"]["catalogs"]:
            for a in cat["articles"]:
                title = str(a["title"])
                kind = {161: NewsKind.DELISTING, 48: NewsKind.LISTING}.get(
                    catalog, classify_title(title)
                )
                out.append(
                    NewsItem(
                        self.name, str(a["id"]), title, _ms(a["releaseDate"]),
                        self.DETAIL + str(a["code"]), kind,
                    )
                )  # fmt: skip
        return out

    async def fetch(self) -> list[NewsItem]:
        parts = await asyncio.gather(*(self._one(c) for c in self.CATALOGS))
        return [i for p in parts for i in p]

    async def aclose(self) -> None:
        await self._c.aclose()


class OkxAnnouncements:
    """欧易 v5 公开公告接口（官方文档有）。注意出口 IP 不同看到的站点（全球 / 美国）不同。"""

    name = "okx"
    URL = "https://www.okx.com/api/v5/support/announcements"
    TYPES = (
        "announcements-new-listings",
        "announcements-delistings",
        "announcements-trading-rules",
        "announcements-api",
    )  # fmt: skip

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._c = client or httpx.AsyncClient(timeout=10.0, headers={"Accept-Language": "zh-CN"})

    async def fetch(self) -> list[NewsItem]:
        out: list[NewsItem] = []
        for t in self.TYPES:  # 限速 5 次 / 2 秒：顺序请求足够
            r = await self._c.get(self.URL, params={"annType": t, "page": 1})
            r.raise_for_status()
            body = r.json()
            if body.get("code") != "0":
                raise ValueError(f"欧易公告接口返回 code={body.get('code')}")
            for page in body["data"]:
                for d in page["details"]:
                    kind = {
                        "announcements-new-listings": NewsKind.LISTING,
                        "announcements-delistings": NewsKind.DELISTING,
                        "announcements-trading-rules": NewsKind.CONTRACT,
                    }.get(t, NewsKind.ANNOUNCEMENT)
                    out.append(
                        NewsItem("okx", d["url"], str(d["title"]), _ms(d["pTime"]), d["url"], kind)
                    )
        return out

    async def aclose(self) -> None:
        await self._c.aclose()


DEFAULT_FEEDS = {
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
    "theblock": "https://www.theblock.co/rss.xml",
}


class RssNews:
    """单个媒体 RSS。每个媒体一个实例，这样挂一个不影响别的。"""

    def __init__(self, name: str, url: str, client: httpx.AsyncClient | None = None) -> None:
        self.name = name
        self._url = url
        self._c = client or httpx.AsyncClient(
            timeout=10.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 jiaoyi"}
        )

    async def fetch(self) -> list[NewsItem]:
        async with self._c.stream("GET", self._url) as r:
            r.raise_for_status()
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_RSS_BYTES:
                    raise ValueError(f"RSS 超过 {MAX_RSS_BYTES} 字节，丢弃")
        root = ElementTree.fromstring(body)
        out: list[NewsItem] = []
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            when = it.findtext("pubDate")
            if not title or not link or not when:
                continue
            desc = re.sub(r"<[^>]+>", "", it.findtext("description") or "").strip()
            out.append(
                NewsItem(
                    self.name, link, title, parsedate_to_datetime(when).astimezone(UTC),
                    link, NewsKind.MEDIA, desc[:200],
                )
            )  # fmt: skip
        if not out:
            raise ValueError("RSS 里没有解析出任何条目")
        return out

    async def aclose(self) -> None:
        await self._c.aclose()


def default_sources() -> list[NewsSource]:
    return [
        BinanceAnnouncements(),
        OkxAnnouncements(),
        *(RssNews(n, u) for n, u in DEFAULT_FEEDS.items()),
    ]


# ---------------------------------------------------------------- 标的匹配
_ALIASES = {
    "BTC": ("bitcoin", "比特币"),
    "ETH": ("ethereum", "以太坊"),
    "SOL": ("solana",),
    "BNB": ("bnb chain",),
    "DOGE": ("dogecoin", "狗狗币"),
    "XRP": ("ripple",),
    "ADA": ("cardano",),
}


def _mentions(text: str, base: str) -> bool:
    """标题里提到这个币吗。≤3 个字母的代码（AI、OP、S…）太容易误中，要求带 $ / 括号 / USDT。"""
    if len(base) <= 3:
        pat = rf"(?:\${base}\b|\({base}\)|(?<![A-Za-z0-9]){base}(?:USDT|/USDT|-USD)(?![A-Za-z]))"
    else:
        pat = rf"(?<![A-Za-z0-9]){base}(?:USDT|/USDT|-USD)?(?![A-Za-z0-9])"
    if re.search(pat, text):
        return True
    low = text.lower()
    return any(a in low for a in _ALIASES.get(base, ()))


def base_of(symbol: str) -> str:
    return symbol.removesuffix("USDT")


# ---------------------------------------------------------------- 汇总
class NewsHub:
    def __init__(
        self,
        sources: Sequence[NewsSource],
        log: DecisionLog | None = None,
        *,
        retention: timedelta = timedelta(hours=48),
        max_items: int = 2000,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sources = list(sources)
        self._log = log
        self._retention = retention
        self._max = max_items
        self._clock = clock
        self._items: dict[str, NewsItem] = {}
        self._down: set[str] = set()

    # ------------------------------------------------------------ 拉取（异步，主循环定时调）
    async def poll(self) -> list[NewsItem]:
        """拉所有源，返回这次新出现的条目。单个源失败不影响其它源。"""
        results = await asyncio.gather(*(s.fetch() for s in self._sources), return_exceptions=True)
        fresh: list[NewsItem] = []
        for src, res in zip(self._sources, results, strict=True):
            if isinstance(res, BaseException):
                self._mark(src.name, ok=False, detail=repr(res))
                continue
            self._mark(src.name, ok=True)
            for it in res:
                if it.key not in self._items:
                    self._items[it.key] = it
                    fresh.append(it)
        self._prune()
        return fresh

    async def poll_and_notify(
        self,
        symbols: Iterable[str],
        notify: Callable[[str], None],
        block: Callable[[str, str], None] | None = None,
    ) -> list[NewsItem]:
        """拉一轮，处理新条目提到的币。

        - ``notify(symbol)``：公告类（不含媒体）提到候选币 → 军师团的大消息触发
        - ``block(symbol, reason)``：币安的下架 / 移除公告（7 天内）提到我们交易的币 → 禁止新开仓。
          宁可多挡：现货交易对移除的公告提到同名币也会挡，代价只是少做几单。
        """
        syms = list(symbols)
        fresh = await self.poll()
        now = self._clock()
        for it in fresh:
            if it.kind is NewsKind.MEDIA:
                continue  # 媒体新闻太多，只当背景，不触发开会
            for s in syms:
                if not _mentions(it.title, base_of(s)):
                    continue
                notify(s)
                recent = now - it.published_at <= BLOCK_WINDOW
                is_delist = it.kind is NewsKind.DELISTING and it.source == "binance"
                if block is not None and is_delist and recent:
                    block(s, it.title)
        return fresh

    def _mark(self, name: str, *, ok: bool, detail: str = "") -> None:
        was_down = name in self._down
        if ok and was_down:
            self._down.discard(name)
            self._note(Outcome.INFO, f"新闻源 {name} 恢复")
        elif not ok and not was_down:
            self._down.add(name)
            self._note(
                Outcome.ERROR, f"新闻源 {name} 失败，会议在没有它的情况下继续：{detail}"[:200]
            )

    def _note(self, outcome: Outcome, summary: str) -> None:
        if self._log is None:
            return
        self._log.write(
            DecisionRecord(
                record_id=uuid.uuid4().hex,
                ts=self._clock(),
                role=Role.COMMANDER,
                outcome=outcome,
                summary=summary,
            )
        )

    def _prune(self) -> None:
        cutoff = self._clock() - self._retention
        keep = sorted(
            (i for i in self._items.values() if i.published_at >= cutoff),
            key=lambda i: i.published_at,
            reverse=True,
        )[: self._max]
        self._items = {i.key: i for i in keep}

    async def aclose(self) -> None:
        """关掉各个源持有的 HTTP 客户端。"""
        for src in self._sources:
            close = getattr(src, "aclose", None)
            if close is not None:
                await close()

    @property
    def down_sources(self) -> set[str]:
        return set(self._down)

    # ------------------------------------------------------------ 读（同步，只读缓存）
    def _recent(self, hours: float) -> list[NewsItem]:
        cutoff = self._clock() - timedelta(hours=hours)
        return sorted(
            (i for i in self._items.values() if i.published_at >= cutoff),
            key=lambda i: i.published_at,
            reverse=True,
        )

    def for_symbol(self, symbol: str, hours: float = 48, limit: int = 8) -> list[NewsItem]:
        base = base_of(symbol)
        return [i for i in self._recent(hours) if _mentions(i.title, base)][:limit]

    def market(self, hours: float = 24, limit: int = 10) -> list[NewsItem]:
        """大盘背景：交易所公告优先，其次媒体。"""
        items = self._recent(hours)
        ann = [i for i in items if i.kind is not NewsKind.MEDIA]
        media = [i for i in items if i.kind is NewsKind.MEDIA]
        return (ann[: limit // 2] + media)[:limit]

    def delisting_symbols(self, symbols: Iterable[str], hours: float = 72) -> set[str]:
        """近期币安的下架 / 移除公告提到的币（口径与 ``poll_and_notify`` 的封禁一致）。"""
        items = [
            i for i in self._recent(hours) if i.kind is NewsKind.DELISTING and i.source == "binance"
        ]
        return {s for s in symbols if any(_mentions(i.title, base_of(s)) for i in items)}


def format_items(items: Sequence[NewsItem], now: datetime | None = None) -> str:
    """给 LLM 看的文本：每条一行，带来源、类型、时间。"""
    now = now or datetime.now(UTC)
    if not items:
        return "（暂无相关新闻或公告；新闻源可能不可用，不代表没有消息）"
    lines = []
    for i in items:
        age_h = max((now - i.published_at).total_seconds() / 3600, 0)
        tail = f" —— {i.summary}" if i.summary else ""
        lines.append(f"- [{i.source}/{i.kind.value}，{age_h:.0f} 小时前] {i.title}{tail}")
    return "\n".join(lines)
