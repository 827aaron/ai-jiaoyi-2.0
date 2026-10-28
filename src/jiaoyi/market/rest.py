"""启动时回填 1 分钟 K 线，让 ATR 和均量不用等 15 分钟；复盘 / 回放补结果 K 线。

只读公开行情接口，不需要 key。用标准库 urllib，放在线程池里跑，不阻塞快环。

限流：币安按 IP 算请求权重（每分钟 2400），超了先回 429，接着还打就回 418 封 IP（2 分钟到 3 天），
同一台机器上跑着的交易程序也会一起被封。所以：

- 遇到 429 / 418 读 ``Retry-After``，抛 :class:`RateLimited`，并且记住到期时间：这之前本进程
  所有请求直接抛同一个错，不再去敲币安；
- 离线工具（复盘、回放、校准）传一个 :class:`WeightBudget`：按每分钟权重预算排队（默认只用
  300，大头留给交易程序），响应头里本 IP 已用权重过半也先等到下一分钟。

Binance bans the IP, not the process, and every request sent during a ban extends it. After
``use_shared_ban(data_dir)`` a ban this process receives also goes to ``data_dir/rest_ban.json``,
which the main loop, the panel and the CLI tools all check before sending. A -1003 body's
``banned until <ms>`` wins over a shorter ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jiaoyi.contracts import Interval, Kline

DEFAULT_REST_URL = "https://fapi.binance.com"
BAN_FILE = "rest_ban.json"

log = logging.getLogger(__name__)


def parse_klines(
    symbol: str, rows: list[list[Any]], now: datetime, interval: Interval = Interval.M1
) -> list[Kline]:
    """``/fapi/v1/klines`` 的原始行 → 已收盘的 ``Kline``（收盘时间晚于 ``now`` 的丢掉）。"""
    out: list[Kline] = []
    for r in rows:
        close_time = datetime.fromtimestamp(int(r[6]) / 1000.0, UTC)
        if close_time > now:
            continue
        out.append(
            Kline(
                symbol=symbol,
                interval=interval,
                open_time=datetime.fromtimestamp(int(r[0]) / 1000.0, UTC),
                close_time=close_time,
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5]),
                quote_volume=float(r[7]),
            )
        )
    return out


IP_WEIGHT_PER_MIN = 2400
"""币安 U 本位每个 IP 每分钟的请求权重上限。"""
DEFAULT_RETRY_AFTER_S = {429: 60, 418: 120}
"""币安没给 ``Retry-After`` 时的保守等待。"""


MAX_BAN_S = 3 * 86400.0
"""Binance's longest IP ban; an end time further out than this is garbage, not a ban."""


class RateLimited(Exception):
    """被币安限流（429）或封 IP（418）。``until`` 之前别再请求。

    ``refused=True``: no request was sent because a known ban is still running."""

    def __init__(self, status: int, until: float, *, refused: bool = False) -> None:
        self.status = status
        self.until = until
        self.refused = refused
        if refused:
            msg = f"币安限流中，至 {_clock_label(until)} 解除，本次不请求"
        else:
            msg = f"HTTP {status}：被币安限流，{self.wait_min()} 分钟后再试"
        super().__init__(msg)

    def wait_min(self, now: float | None = None) -> int:
        left = self.until - (time.time() if now is None else now)
        if math.isnan(left) or left <= 0:
            return 1
        return max(1, -(-int(min(left, MAX_BAN_S)) // 60))


def _clock_label(until: float) -> str:
    """Local wall-clock time; the date too when the ban outlives today (418 bans run to days)."""
    try:
        at = datetime.fromtimestamp(until)
    except (OverflowError, ValueError, OSError):
        return "未知时间"
    return at.strftime("%H:%M" if at.date() == datetime.now().date() else "%m-%d %H:%M")


_blocked: RateLimited | None = None
"""本进程最近一次被限流；到期前所有请求直接抛它（到期后留着当记录，不再拦）。"""
_own: RateLimited | None = None
"""The latest ban this process itself received. Only this one is persisted: a ban adopted from
the file is never written back, so deleting a bad file clears it everywhere."""
_used_weight: int | None = None
"""最近一次响应头里本 IP 这一分钟已用的权重（含同机交易程序）。"""
_ban_path: Path | None = None
"""Ban file shared across processes; None keeps the ban in this process only."""
_FileKey = tuple[int, int, int]
_seen: dict[Path, tuple[_FileKey, RateLimited | None, bool]] = {}
"""Per file: (inode, mtime_ns, size) → (ban, unusable), so a request or heartbeat costs one stat."""
_warned: set[Any] = set()
_ban_lock = threading.Lock()
_BANNED_UNTIL = re.compile(r"banned until (\d{10,14})")


def _warn_once(key: Any, msg: str, *args: Any) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def _sane(until: float, now: float) -> bool:
    return math.isfinite(until) and until <= now + MAX_BAN_S


def _retry_after(headers: Any, status: int) -> float:
    raw = headers.get("Retry-After") if headers is not None else None
    try:
        wait = max(float(raw), 1.0) if raw else float(DEFAULT_RETRY_AFTER_S[status])
    except ValueError:
        return float(DEFAULT_RETRY_AFTER_S[status])
    if not math.isfinite(wait) or wait > MAX_BAN_S:
        _warn_once(("retry-after", raw), "币安 Retry-After=%r 不合理，按默认等待", raw)
        return float(DEFAULT_RETRY_AFTER_S[status])
    return wait


def parse_banned_until(body: str | bytes | None) -> float | None:
    """Unix seconds from a -1003 body (``... IP(x) banned until 1730123456789 ...``), else None."""
    if not body:
        return None
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    m = _BANNED_UNTIL.search(text)
    if m is None:
        return None
    raw = int(m.group(1))
    return raw / 1000.0 if raw > 10**11 else float(raw)


def use_shared_ban(data_dir: str | Path | None) -> None:
    """Share the ban with every process using ``data_dir``; ``None`` keeps it in-process."""
    global _ban_path
    with _ban_lock:
        _ban_path = Path(data_dir) / BAN_FILE if data_dir else None


def _file_key(path: Path) -> _FileKey | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _read(path: Path, now: float) -> tuple[_FileKey | None, RateLimited | None, bool]:
    """(file key, ban, unusable) for ``{"status": 418, "until": <unix s>}`` on file.

    Unusable — unparseable, or an end time that is not finite or lies beyond ``MAX_BAN_S`` —
    counts as no ban, so a bad file never stops market data; it is warned about once."""
    key = _file_key(path)
    if key is None:
        return None, None, False
    seen = _seen.get(path)
    if seen is not None and seen[0] == key:
        return key, seen[1], seen[2]
    ban: RateLimited | None = None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        status, until = int(raw["status"]), float(raw["until"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        _warn_once((path, key), "币安封禁文件 %s 读不了（%s），当作没有封禁", path, e)
    else:
        if _sane(until, now):
            ban = RateLimited(status, until)
        else:
            _warn_once(
                (path, key), "币安封禁文件 %s 的解封时刻 %r 不合理，当作没有封禁", path, until
            )
    _seen[path] = (key, ban, ban is None)
    return key, ban, ban is None


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[bool]:
    """Exclusive flock on a sibling ``.lock`` across processes; False when the dir is missing."""
    if not path.parent.is_dir():
        _warn_once(("no-dir", path.parent), "数据目录 %s 不存在，币安封禁不写盘", path.parent)
        yield False
        return
    try:
        fd = os.open(path.with_name(f".{path.name}.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        _warn_once(("lock", path), "币安封禁锁文件打不开（%s），不写盘", e)
        yield False
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield True
    finally:
        os.close(fd)


def _atomic_write(path: Path, lim: RateLimited) -> None:
    record = {
        "status": lim.status,
        "until": lim.until,
        "until_iso": datetime.fromtimestamp(lim.until, UTC).isoformat(timespec="seconds"),
        "seen_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "pid": os.getpid(),
    }
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.warning("币安封禁写不进 %s，别的进程看不到这次限流", path, exc_info=True)
        with contextlib.suppress(OSError):
            tmp.unlink()


def _persist(path: Path, own: RateLimited | None, now: float) -> None:
    """Under both locks: write ``own`` if it ends later than the file; with no live ``own``,
    remove a file that is expired or unusable, unless another process replaced it meanwhile."""
    with _ban_lock, _file_lock(path) as locked:
        if not locked:
            return
        key, cur, bad = _read(path, now)
        if own is not None:
            if cur is None or own.until > cur.until:
                _atomic_write(path, own)
            return
        if key is None or not (bad or (cur is not None and cur.until <= now)):
            return
        if _file_key(path) == key:
            with contextlib.suppress(OSError):
                path.unlink()
            if bad:
                log.warning("已删除不可用的币安封禁文件 %s", path)


def _read_shared(now: float) -> RateLimited | None:
    return _read(_ban_path, now)[1] if _ban_path is not None else None


def load_ban(data_dir: str | Path, now: float | None = None) -> RateLimited | None:
    """A ban persisted under ``data_dir`` that has not expired, adopted into this process."""
    global _blocked
    t = time.time() if now is None else now
    ban = _read(Path(data_dir) / BAN_FILE, t)[1]
    if ban is None or ban.until <= t:
        return None
    if _blocked is None or ban.until > _blocked.until:
        _blocked = ban
    return ban


def save_ban(data_dir: str | Path, now: float | None = None) -> None:
    """Persist the live ban this process received itself under ``data_dir``; drop an expired or
    unusable file. A live ban another process wrote is never shortened or removed."""
    t = time.time() if now is None else now
    own = _own if _own is not None and _own.until > t else None
    _persist(Path(data_dir) / BAN_FILE, own, t)


def active_ban(now: float | None = None) -> RateLimited | None:
    """The running ban from this process or the shared file, whichever ends later."""
    t = time.time() if now is None else now
    live = [b for b in (_blocked, _read_shared(t)) if b is not None and t < b.until]
    return max(live, key=lambda b: b.until) if live else None


def check_blocked(now: float | None = None) -> None:
    """Raise a ``refused`` :class:`RateLimited` while a ban runs, adopting it in this process."""
    global _blocked
    ban = active_ban(now)
    if ban is None:
        return
    if _blocked is None or ban.until > _blocked.until:
        _blocked = ban
    raise RateLimited(ban.status, ban.until, refused=True)


def blocked() -> RateLimited | None:
    """本进程最近一次被限流（到期了也留着，给命令行收尾时说一句）。"""
    return _blocked


def mark_blocked(status: int, headers: Any, body: str | bytes | None = None) -> RateLimited:
    """Record a 429 / 418 from any request path and return the :class:`RateLimited` to raise.

    Only a later end than the one already known replaces it, so a short 429 never shortens a 418."""
    global _blocked, _own
    now = time.time()
    until = now + _retry_after(headers, status)
    banned = parse_banned_until(body)
    if banned is not None:
        if _sane(banned, now):
            until = max(until, banned)
        else:
            _warn_once(
                ("banned-until", banned), "币安 banned until %r 不合理，按 Retry-After", banned
            )
    if _own is None or until > _own.until:
        _own = RateLimited(status, until)
        if _ban_path is not None:
            _persist(_ban_path, _own, now)
    if _blocked is None or _own.until > _blocked.until:
        _blocked = _own
    return _blocked


def note_used_weight(header: str | None) -> None:
    """记下响应头 ``X-MBX-USED-WEIGHT-1M``（本 IP 这一分钟已用权重，含同机其他程序）。"""
    global _used_weight
    _used_weight = int(header) if header and header.isdigit() else None


def take_used_weight() -> int | None:
    """取走并清掉最近的已用权重读数（清掉是为了等过一个窗口后不再重复等）。"""
    global _used_weight
    used, _used_weight = _used_weight, None
    return used


def _get(url: str, timeout_s: float) -> list[list[Any]]:
    global _used_weight
    check_blocked()
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            used = resp.headers.get("X-MBX-USED-WEIGHT-1M")
            _used_weight = int(used) if used and used.isdigit() else None
            data: list[list[Any]] = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in DEFAULT_RETRY_AFTER_S:
            body: bytes | None = None
            with contextlib.suppress(Exception):
                body = e.read()
            raise mark_blocked(e.code, e.headers, body) from None
        raise
    return data


def klines_weight(limit: int) -> int:
    """``/fapi/v1/klines`` 一次请求的权重：按 limit 分档。"""
    return 1 if limit < 100 else 2 if limit < 500 else 5 if limit <= 1000 else 10


class WeightBudget:
    """离线工具用的权重预算：滚动 60 秒内最多花 ``per_min``；本 IP 已用过半也先等。"""

    def __init__(
        self,
        per_min: int = 300,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        check_ban: bool = True,
    ) -> None:
        """``check_ban=False`` for a budget spent on another host (Demo), which the mainnet ban
        does not cover."""
        self.per_min = per_min
        self._check_ban = check_ban
        self._clock = clock
        self._sleep = sleep
        self._spent: deque[tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    async def take(self, weight: int) -> None:
        global _used_weight
        async with self._lock:
            if self._check_ban:
                check_blocked()
            if _used_weight is not None and _used_weight > IP_WEIGHT_PER_MIN // 2:
                _used_weight = None
                await self._sleep(60.0 - time.time() % 60.0 + 1.0)  # 等到币安下一分钟窗口
            while True:
                now = self._clock()
                while self._spent and now - self._spent[0][0] >= 60.0:
                    self._spent.popleft()
                if sum(w for _, w in self._spent) + weight <= self.per_min:
                    self._spent.append((now, weight))
                    return
                await self._sleep(60.0 - (now - self._spent[0][0]) + 0.01)


async def _page(url: str, timeout_s: float, weight: int, budget: WeightBudget | None) -> Any:
    if budget is not None:
        await budget.take(weight)
    else:
        check_blocked()
    return await asyncio.to_thread(_get, url, timeout_s)


MAX_KLINES_PER_REQUEST = 1500
_INTERVAL_MS = {
    Interval.M1: 60_000,
    Interval.M5: 300_000,
    Interval.M15: 900_000,
    Interval.M30: 1_800_000,
    Interval.H1: 3_600_000,
    Interval.H4: 14_400_000,
}


async def fetch_recent_klines(
    symbol: str,
    *,
    limit: int = 60,
    interval: Interval = Interval.M1,
    base_url: str = DEFAULT_REST_URL,
    timeout_s: float = 5.0,
    budget: WeightBudget | None = None,
) -> list[Kline]:
    rows: list[Any] = []
    end_ms: int | None = None
    while len(rows) < limit:  # 币安一次最多 1500 根；4 小时 ATR 要回填 3840 根 1 分钟线，往前分页拉
        params: dict[str, Any] = {
            "symbol": symbol,
            "interval": interval.value,
            "limit": min(limit - len(rows), MAX_KLINES_PER_REQUEST),
        }
        if end_ms is not None:
            params["endTime"] = end_ms
        q = urllib.parse.urlencode(params)
        url = f"{base_url}/fapi/v1/klines?{q}"
        page = await _page(url, timeout_s, klines_weight(params["limit"]), budget)
        if not page:
            break
        rows = [*page, *rows]
        end_ms = int(page[0][0]) - 1
        if len(page) < params["limit"]:
            break
    return parse_klines(symbol, rows, datetime.now(UTC), interval)


async def fetch_klines_between(
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    interval: Interval = Interval.M1,
    base_url: str = DEFAULT_REST_URL,
    timeout_s: float = 5.0,
    budget: WeightBudget | None = None,
) -> list[Kline]:
    """``start``–``end`` 之间已收盘的 K 线（往后分页拉，复盘回填用）。

    limit 按要的根数给（权重按 limit 分档：4 小时 241 根权重 2，固定要 1500 根是 10）。
    """
    rows: list[Any] = []
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    step = _INTERVAL_MS[interval]
    while start_ms <= end_ms:
        want = (end_ms - start_ms) // step + 2
        limit = max(1, min(want, MAX_KLINES_PER_REQUEST))
        params = {
            "symbol": symbol,
            "interval": interval.value,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": limit,
        }
        q = urllib.parse.urlencode(params)
        url = f"{base_url}/fapi/v1/klines?{q}"
        page = await _page(url, timeout_s, klines_weight(limit), budget)
        if not page:
            break
        rows.extend(page)
        start_ms = int(page[-1][0]) + 1
        if len(page) < limit:
            break
    return parse_klines(symbol, rows, datetime.now(UTC), interval)


async def fetch_json(
    path: str,
    params: dict[str, Any],
    *,
    weight: int = 1,
    base_url: str = DEFAULT_REST_URL,
    timeout_s: float = 5.0,
    budget: WeightBudget | None = None,
) -> Any:
    """任意公开 GET 接口（持仓量、多空比……），返回解析后的 JSON。限流规则同上。"""
    url = f"{base_url}{path}?{urllib.parse.urlencode(params)}"
    return await _page(url, timeout_s, weight, budget)
