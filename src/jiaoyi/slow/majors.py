"""「主流币」= 市值前 N 名（默认 20）：每天拉一次市值排名，结果缓存到 data/majors.json。

- 数据源：CoinGecko 免费接口（无需 key）按市值排序的前 250 个币。
- 去掉稳定币和包装 / 质押币（WBTC、stETH 之类跟着别的币走，不算独立标的）。
- 选币时再去掉币安没有 USDT 永续的（FIGR_HELOC、WBT、LEO 这类），往下补满 N 个，
  所以「前 20」是 20 个币安能交易的真币，名次是它们在 CoinGecko 上的原始市值名次。
- 映射到币安 USDT 永续：基础币名一致即可；币安给小币种加的 1000 / 1000000 / 1M 前缀（1000PEPE）
  在 :func:`base_of` 里去掉再比。
- 拉取失败沿用缓存（不管多旧），1 小时内不再重试；没有缓存就回退
  ``screener.FALLBACK_MAJORS`` 那 7 个。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

URL = "https://api.coingecko.com/api/v3/coins/markets"
REFRESH_EVERY = timedelta(hours=24)
CACHE_VERSION = 2  # 缓存格式变了（存全部候选、不再只存前 N）：旧缓存当过期
RETRY_AFTER_FAIL = timedelta(hours=1)
"""拉取失败后多久内不再试（直接用缓存），免得接口长期挂着时每轮选币都多等一次超时。"""

STABLES = frozenset(
    {"USDT", "USDC", "DAI", "USDE", "FDUSD", "TUSD", "USDD", "PYUSD", "USDS", "USDY", "BUIDL",
     "USD0", "USDG", "RLUSD", "FRAX", "LUSD", "GUSD", "USDP", "EURC", "SUSDE", "USDTB", "USDF"}
)  # fmt: skip
WRAPPED = frozenset(
    {"WBTC", "WETH", "STETH", "WSTETH", "WEETH", "WBETH", "CBBTC", "CBETH", "RETH", "EETH",
     "WBNB", "BSC-USD", "SOLVBTC", "LBTC", "TBTC", "METH", "RSETH", "EZETH", "JITOSOL", "BNSOL"}
)  # fmt: skip
_WRAP_ID = re.compile(r"(wrapped|staked|bridged|restaked|liquid-staking)")
_PREFIX = re.compile(r"^(1000000|10000|1000|1M)(?=[A-Z])")


def base_of(symbol: str) -> str:
    """币安永续代码 → 基础币名：``1000PEPEUSDT`` → ``PEPE``。"""
    return _PREFIX.sub("", symbol.removesuffix("USDT"))


def parse_markets(rows: list[dict[str, Any]]) -> dict[str, int]:
    """CoinGecko 行 → {基础币名: 市值名次}（按市值从大到小）：去掉稳定币和包装币，取前几名留给
    :meth:`MajorsSource.ranks` 按币安有没有永续再筛。"""
    out: dict[str, int] = {}
    for r in sorted(rows, key=lambda r: r.get("market_cap_rank") or 10**9):
        sym = str(r.get("symbol", "")).upper()
        rank = r.get("market_cap_rank")
        if not sym or not isinstance(rank, int) or sym in out:
            continue
        if sym in STABLES or sym in WRAPPED or _WRAP_ID.search(str(r.get("id", ""))):
            continue
        out[sym] = rank
    return out


@dataclass
class MajorRanks:
    """市值排名缓存：``ranks`` 是 {基础币名: 名次}。"""

    ranks: dict[str, int] = field(default_factory=dict)
    fetched_at: datetime | None = None
    version: int = 0


class MajorsSource:
    def __init__(
        self,
        path: Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._path = path
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns = client is None
        self.state = self._load()
        self.error: str | None = None
        self._failed_at: datetime | None = None

    def _load(self) -> MajorRanks:
        if self._path is None or not self._path.is_file():
            return MajorRanks()
        try:
            d = json.loads(self._path.read_text(encoding="utf-8"))
            return MajorRanks(
                {str(k): int(v) for k, v in d["ranks"].items()},
                datetime.fromisoformat(d["fetched_at"]),
                int(d.get("v", 0)),
            )
        except (ValueError, KeyError, TypeError, OSError):
            log.warning("市值排名缓存 %s 读不懂，忽略", self._path)
            return MajorRanks()

    def _save(self) -> None:
        if self._path is None or self.state.fetched_at is None:
            return
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(
                    {
                        "fetched_at": self.state.fetched_at.isoformat(),
                        "v": self.state.version,
                        "ranks": self.state.ranks,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self._path)
        except OSError:
            log.warning("市值排名缓存写不进 %s", self._path, exc_info=True)

    def ranks(self, top_n: int, available: Collection[str] | None = None) -> Mapping[str, int]:
        """市值前 ``top_n`` 个「币安有 USDT 永续」的真币：{基础币名: 市值名次}。

        稳定币、包装币拉名单时已去；这里再去掉币安没有永续的（``available`` 是币安永续的基础币名，
        不给就不筛），然后从大到小取满 ``top_n`` 个。
        空表示还没有任何数据（调用方回退写死的名单）。"""
        out: dict[str, int] = {}
        for base, rank in self.state.ranks.items():
            if len(out) >= top_n:
                break
            if available is None or base in available:
                out[base] = rank
        return out

    async def refresh(self, now: datetime) -> None:
        """超过一天（或缓存格式旧了）才重新拉；失败沿用旧缓存，只记 ``error``。"""
        st = self.state
        if self._failed_at is not None and now - self._failed_at < RETRY_AFTER_FAIL:
            return
        if (
            st.fetched_at is not None
            and st.version == CACHE_VERSION
            and now - st.fetched_at < REFRESH_EVERY
        ):
            return
        try:
            r = await self._client.get(
                URL,
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": 1,
                },
            )
            r.raise_for_status()
            ranks = parse_markets(r.json())
            if not ranks:
                raise ValueError("市值排名是空的")
        except Exception as exc:
            self._failed_at = now
            self.error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("拉市值排名失败，沿用上次（%s）", self.error)
            return
        self.error, self._failed_at = None, None
        self.state = MajorRanks(ranks, now.astimezone(UTC), CACHE_VERSION)
        self._save()

    async def aclose(self) -> None:
        if self._owns:
            await self._client.aclose()
