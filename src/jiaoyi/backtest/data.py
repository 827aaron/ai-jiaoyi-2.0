"""币安历史数据：K 线和 aggTrades 的读取、下载（data.binance.vision）、合并成事件流。

数据来源是币安官方的公开数据站，U 本位永续按天打包成 zip：

- ``futures/um/daily/klines/<SYMBOL>/<interval>/<SYMBOL>-<interval>-<YYYY-MM-DD>.zip``
- ``futures/um/daily/aggTrades/<SYMBOL>/<SYMBOL>-aggTrades-<YYYY-MM-DD>.zip``

下载的文件缓存在本机目录（默认 ``data/history``，已被 .gitignore 忽略），不进仓库。
"""

from __future__ import annotations

import csv
import heapq
import io
import urllib.request
import zipfile
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import IO

from jiaoyi.contracts import Interval, Kline, MarketEvent, Side, Trade

BASE_URL = "https://data.binance.vision/data/futures/um/daily"
DEFAULT_CACHE = Path("data/history")
_INTERVAL_MS = {Interval.M1: 60_000, Interval.M5: 300_000, Interval.M15: 900_000}


def _ts(ms: str | int | float) -> datetime:
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC)


def _rows(fh: IO[str]) -> Iterator[list[str]]:
    """跳过表头（第一列不是数字就是表头）。"""
    for row in csv.reader(fh):
        if row and row[0].strip().lstrip("-").replace(".", "", 1).isdigit():
            yield row


def parse_klines(fh: IO[str], symbol: str, interval: Interval = Interval.M1) -> Iterator[Kline]:
    """币安 K 线 CSV：open_time,open,high,low,close,volume,close_time,quote_volume,..."""
    for r in _rows(fh):
        yield Kline(
            symbol=symbol,
            interval=interval,
            open_time=_ts(r[0]),
            close_time=_ts(r[6]),
            open=float(r[1]),
            high=float(r[2]),
            low=float(r[3]),
            close=float(r[4]),
            volume=float(r[5]),
            quote_volume=float(r[7]),
        )


def parse_agg_trades(fh: IO[str], symbol: str) -> Iterator[Trade]:
    """币安 aggTrades CSV：id,price,qty,first_id,last_id,transact_time,is_buyer_maker。

    ``is_buyer_maker=true`` 表示买方是挂单方，也就是主动成交的一方在卖。
    """
    for r in _rows(fh):
        maker_is_buyer = r[6].strip().lower() == "true"
        yield Trade(
            symbol=symbol,
            ts=_ts(r[5]),
            price=float(r[1]),
            qty=float(r[2]),
            taker_side=Side.SHORT if maker_is_buyer else Side.LONG,
        )


def _open_zip_csv(path: Path) -> IO[str]:
    if path.suffix == ".zip":
        zf = zipfile.ZipFile(path)
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        return io.TextIOWrapper(zf.open(name), encoding="utf-8")
    return path.open(encoding="utf-8")


def load_klines(path: Path, symbol: str, interval: Interval = Interval.M1) -> list[Kline]:
    with _open_zip_csv(path) as fh:
        return list(parse_klines(fh, symbol, interval))


def iter_agg_trades(path: Path, symbol: str) -> Iterator[Trade]:
    """逐笔读（一天 BTC 有几百万笔，不一次性读进内存）。"""
    with _open_zip_csv(path) as fh:
        yield from parse_agg_trades(fh, symbol)


def download(url: str, dest: Path, timeout_s: float = 120.0) -> Path:
    """下载到缓存；已有就跳过。先写临时文件再改名，半截文件不会被当成缓存。"""
    if dest.is_file():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=timeout_s) as resp, tmp.open("wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    tmp.replace(dest)
    return dest


def fetch_day(
    symbol: str, day: date, cache: Path = DEFAULT_CACHE, interval: Interval = Interval.M1
) -> tuple[Path, Path]:
    """下载某天的 K 线和 aggTrades，返回 (K 线路径, aggTrades 路径)。"""
    d = day.isoformat()
    k_name = f"{symbol}-{interval.value}-{d}.zip"
    t_name = f"{symbol}-aggTrades-{d}.zip"
    k = download(f"{BASE_URL}/klines/{symbol}/{interval.value}/{k_name}", cache / symbol / k_name)
    t = download(f"{BASE_URL}/aggTrades/{symbol}/{t_name}", cache / symbol / t_name)
    return k, t


def merge_events(*streams: Iterable[MarketEvent]) -> Iterator[MarketEvent]:
    """把多路已按时间排序的事件合并成一路。同一时刻 K 线（收盘）排在成交之后。"""

    def keyed(
        stream: Iterable[MarketEvent], idx: int
    ) -> Iterator[tuple[datetime, int, int, MarketEvent]]:
        for n, ev in enumerate(stream):
            # K 线用收盘时间；它在收盘那一刻才“已知”
            ts = ev.close_time if isinstance(ev, Kline) else ev.ts
            yield (ts, 1 if isinstance(ev, Kline) else 0, idx * 10**9 + n, ev)

    for _, _, _, ev in heapq.merge(
        *(keyed(s, i) for i, s in enumerate(streams)), key=lambda t: (t[0], t[1], t[2])
    ):
        yield ev
