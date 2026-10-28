"""Reader side of ``data/mainnet_shadow.jsonl`` (written by ``execution.mainnet_shadow``),
kept outside the execution layer so the panel can read it without importing it."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

SHADOW_FILE = "mainnet_shadow.jsonl"
SHADOW_VERSION = 2
PRECISE_SOURCES = frozenset({"agg_trade", "mark", "ws_trade", "limit_price"})
"""Second-level mainnet prices (or the leg's own limit price); anything else is a 1-minute
bar interpolation."""

Quality = tuple[int, int, int]


def quality(row: dict[str, Any]) -> Quality:
    """(row format version, both legs priced to the second, exact Demo fill times). A row
    for the same position with a higher tuple replaces a lower one."""
    try:
        v = int(row.get("v", 1))
    except (TypeError, ValueError):
        v = 1
    precise = row.get("entry_source") in PRECISE_SOURCES and row.get("exit_source") in (
        PRECISE_SOURCES
    )
    return (v, 2 if precise else 1, 1 if row.get("time_source") == "fills" else 0)


_cache: dict[Path, tuple[int, int, dict[str, dict[str, Any]]]] = {}


def read_shadow(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    """position_id -> best row (by :func:`quality`; on a tie the later line wins, so a
    recompute replaces). Unreadable lines are skipped. Cached by file mtime and size; the
    returned dict is shared, callers must not mutate it."""
    path = Path(data_dir) / SHADOW_FILE
    try:
        st = path.stat()
        hit = _cache.get(path)
        if hit is not None and hit[:2] == (st.st_mtime_ns, st.st_size):
            return hit[2]
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        _cache.pop(path, None)
        return {}
    except OSError:
        log.warning("%s 读不出", path, exc_info=True)
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in text.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        pid = row.get("position_id") if isinstance(row, dict) else None
        if isinstance(pid, str) and (pid not in out or quality(row) >= quality(out[pid])):
            out[pid] = row
    _cache[path] = (st.st_mtime_ns, st.st_size, out)
    return out


@dataclass
class DayTotals:
    day: str
    trades: int = 0
    demo_net_usdt: float = 0.0
    mainnet_net_usdt: float = 0.0
    price_gap_usdt: float = 0.0
    fee_gap_usdt: float = 0.0
    """Taker-rate fees minus Demo's actual-rate fees, on mainnet notional."""
    notional_usdt: float = 0.0
    gap_r_sum: float = 0.0
    r_trades: int = 0
    demo_r_sum: float = 0.0
    mainnet_r_sum: float = 0.0
    limit_legs: int = 0

    @property
    def gap_usdt(self) -> float:
        return self.demo_net_usdt - self.mainnet_net_usdt

    def bp(self, usdt: float) -> float | None:
        """Notional-weighted: ``usdt`` over total entry notional."""
        return usdt / self.notional_usdt * 10_000 if self.notional_usdt > 0 else None

    @property
    def gap_bp(self) -> float | None:
        return self.bp(self.gap_usdt)

    def avg(self, total: float) -> float | None:
        return total / self.r_trades if self.r_trades else None

    def add(self, other: DayTotals) -> None:
        for k in (
            "trades",
            "demo_net_usdt",
            "mainnet_net_usdt",
            "price_gap_usdt",
            "fee_gap_usdt",
            "notional_usdt",
            "gap_r_sum",
            "r_trades",
            "demo_r_sum",
            "mainnet_r_sum",
            "limit_legs",
        ):
            setattr(self, k, getattr(self, k) + getattr(other, k))


def by_day(rows: Iterable[dict[str, Any]], tz: str) -> list[DayTotals]:
    """Per local day of the close (``tz`` = the daily-loss reset zone), oldest first."""
    zone = ZoneInfo(tz)
    days: dict[str, DayTotals] = {}
    for r in rows:
        try:
            day = datetime.fromisoformat(r["closed_at"]).astimezone(zone).date().isoformat()
            vals = [
                float(r[k])
                for k in (
                    "demo_net_usdt",
                    "mainnet_net_usdt",
                    "price_gap_usdt",
                    "fee_gap_usdt",
                    "notional_usdt",
                )
            ]
        except (KeyError, TypeError, ValueError):
            continue
        d = days.setdefault(day, DayTotals(day))
        d.trades += 1
        d.demo_net_usdt += vals[0]
        d.mainnet_net_usdt += vals[1]
        d.price_gap_usdt += vals[2]
        d.fee_gap_usdt += vals[3]
        d.notional_usdt += vals[4]
        d.limit_legs += (r.get("entry_type") == "limit") + (r.get("exit_type") == "limit")
        if r.get("gap_r") is not None:
            d.r_trades += 1
            d.gap_r_sum += float(r["gap_r"])
            d.demo_r_sum += float(r["demo_r"])
            d.mainnet_r_sum += float(r["mainnet_r"])
    return [days[k] for k in sorted(days)]
