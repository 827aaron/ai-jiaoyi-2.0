"""盘口员此刻看到的行情，每 5 秒原子写一次 ``data/market_snapshot.json``，给面板「盘口员」用。

面板是另一个进程，读不到主循环内存，所以落一个小文件：

```json
{"ts": "...",
 "symbols": {"BTCUSDT": {"price": 68000.0, "chg_1m_pct": 0.12, "chg_5m_pct": -0.3,
                          "taker_buy_ratio_1m": 0.55, "spread_bps": 0.2,
                          "liq_long_usdt_1m": 0.0, "liq_short_usdt_1m": 1.2e4, "book_age_s": 0.4}},
 "streams": {"trade": {"count_today": 123456, "last_at": "..."}, "book": {...}, ...},
 "loop_lag": {"last_ms": 0.4, "last_minute": {"max_ms": 3.1, "avg_ms": 0.6, "samples": 60}}}
```

``count_today`` 按日切时区（``risk.day_reset_tz``）的当天计数，过了 0 点清零。拿不到的值写 null
（例如盘口超过 10 秒没更新时 ``spread_bps`` 为 null，``book_age_s`` 照写真实秒数）。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jiaoyi.contracts import MarketEvent
from jiaoyi.interfaces import FeatureEngine

log = logging.getLogger(__name__)

SNAPSHOT_FILE = "market_snapshot.json"
WRITE_EVERY_S = 5.0


def _r(x: float | None, nd: int) -> float | None:
    return None if x is None else round(x, nd)


class MarketSnapshotFile:
    def __init__(self, data_dir: str | Path, tz: str) -> None:
        self._path = Path(data_dir) / SNAPSHOT_FILE
        self._tz = ZoneInfo(tz)
        self._day: date | None = None
        self._counts: dict[str, int] = {}
        self._last: dict[str, datetime] = {}

    def count(self, event: MarketEvent, now: datetime) -> None:
        """每条行情推送调一次：按类型（trade / book / kline / mark / liquidation）计数。"""
        day = now.astimezone(self._tz).date()
        if day != self._day:
            self._day, self._counts = day, {}
        kind = event.type
        self._counts[kind] = self._counts.get(kind, 0) + 1
        self._last[kind] = now

    def build(self, features: FeatureEngine, symbols: list[str], now: datetime) -> dict[str, Any]:
        age = getattr(features, "book_age_s", None)  # 只有真特征引擎有
        out: dict[str, Any] = {}
        for sym in symbols:
            snap = features.snapshot(sym)
            if snap is None:
                continue
            v = snap.values
            ret_1m, ret_5m = v.get("ret_1m"), v.get("ret_5m")
            out[sym] = {
                "price": snap.last_price,
                "chg_1m_pct": _r(ret_1m * 100 if ret_1m is not None else None, 4),
                "chg_5m_pct": _r(ret_5m * 100 if ret_5m is not None else None, 4),
                "taker_buy_ratio_1m": _r(v.get("taker_buy_ratio_1m"), 4),
                "spread_bps": _r(snap.book.spread_bps if snap.book is not None else None, 3),
                "liq_long_usdt_1m": _r(v.get("liq_long_usdt_1m", 0.0), 2),
                "liq_short_usdt_1m": _r(v.get("liq_short_usdt_1m", 0.0), 2),
                "book_age_s": _r(age(sym, now), 1) if callable(age) else None,
            }
        if self._day is not None and now.astimezone(self._tz).date() != self._day:
            self._day, self._counts = now.astimezone(self._tz).date(), {}
        streams = {
            k: {"count_today": self._counts.get(k, 0), "last_at": t.isoformat()}
            for k, t in sorted(self._last.items())
        }
        return {"ts": now.isoformat(), "symbols": out, "streams": streams}

    def write(
        self,
        features: FeatureEngine,
        symbols: list[str],
        now: datetime,
        *,
        loop_lag: dict[str, Any] | None = None,
    ) -> None:
        """``loop_lag``：主循环事件循环延迟（``LoopLagMonitor.status``），有就原样写进去。"""
        data = self.build(features, symbols, now)
        if loop_lag is not None:
            data["loop_lag"] = loop_lag
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning("行情快照写不进去：%s", e)
