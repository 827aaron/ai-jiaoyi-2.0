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


def read_shadow(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    """position_id -> row, first row per id wins; unreadable lines are skipped."""
    path = Path(data_dir) / SHADOW_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
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
        if isinstance(pid, str) and pid not in out:
            out[pid] = row
    return out


@dataclass
class DayTotals:
    day: str
    trades: int = 0
    demo_net_usdt: float = 0.0
    mainnet_net_usdt: float = 0.0
    notional_usdt: float = 0.0
    gap_r_sum: float = 0.0
    r_trades: int = 0
    demo_r_sum: float = 0.0
    mainnet_r_sum: float = 0.0

    @property
    def gap_usdt(self) -> float:
        return self.demo_net_usdt - self.mainnet_net_usdt

    @property
    def gap_bp(self) -> float | None:
        """Notional-weighted: total gap over total entry notional."""
        return self.gap_usdt / self.notional_usdt * 10_000 if self.notional_usdt > 0 else None

    def avg(self, total: float) -> float | None:
        return total / self.r_trades if self.r_trades else None


def by_day(rows: Iterable[dict[str, Any]], tz: str) -> list[DayTotals]:
    """Per local day of the close (``tz`` = the daily-loss reset zone), oldest first."""
    zone = ZoneInfo(tz)
    days: dict[str, DayTotals] = {}
    for r in rows:
        try:
            day = datetime.fromisoformat(r["closed_at"]).astimezone(zone).date().isoformat()
            d = days.setdefault(day, DayTotals(day))
            d.trades += 1
            d.demo_net_usdt += float(r["demo_net_usdt"])
            d.mainnet_net_usdt += float(r["mainnet_net_usdt"])
            d.notional_usdt += float(r["notional_usdt"])
        except (KeyError, TypeError, ValueError):
            continue
        if r.get("gap_r") is not None:
            d.r_trades += 1
            d.gap_r_sum += float(r["gap_r"])
            d.demo_r_sum += float(r["demo_r"])
            d.mainnet_r_sum += float(r["mainnet_r"])
    return [days[k] for k in sorted(days)]
