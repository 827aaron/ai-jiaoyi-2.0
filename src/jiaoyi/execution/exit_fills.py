"""Exchange exit fill time per position: ``data/exit_fills.json``.

The ledger's ``closed_at`` is when this program settles the position (a hosted position waits for
HB to terminate and cleanup to verify, often 20-30 s after the fill); daily P&L, reconciliation and
cooldowns keep using it. The panel reads this file to show the real fill time instead.

A side file rather than a ledger field: the ledger contract is ``extra="forbid"``, so a new key
would make older code refuse to load it after a rollback; older code simply ignores this file.

Format::

    {"pos-...": {"at": "2026-10-28T06:10:27.512000+00:00", "source": "order"}}

``source``: ``order`` = Binance ``updateTime`` of the filled direct reduce-only order;
``trades`` = last exit fill from the userTrades backfill (more exact, overrides ``order``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

FILE_NAME = "exit_fills.json"
KEEP = timedelta(days=90)
ExitFillSource = Literal["order", "trades"]


def read_exit_fills(path: Path) -> dict[str, datetime]:
    """Position id -> exit fill time; empty when missing or unreadable, bad rows skipped."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, datetime] = {}
    if not isinstance(raw, dict):
        return out
    for pid, row in raw.items():
        at = row.get("at") if isinstance(row, dict) else None
        if not isinstance(at, str):
            continue
        try:
            when = datetime.fromisoformat(at)
        except ValueError:
            continue
        if when.tzinfo is not None:
            out[str(pid)] = when
    return out


class ExitFills:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def record(self, position_id: str, at: datetime, source: ExitFillSource) -> None:
        """``trades`` overrides ``order``, never the reverse. A failed write is only logged."""
        if at.tzinfo is None:
            return
        with self._lock:
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raw = {}
            if not isinstance(raw, dict):
                raw = {}
            old = raw.get(position_id)
            if source == "order" and isinstance(old, dict) and old.get("source") == "trades":
                return
            if (
                isinstance(old, dict)
                and old.get("at") == at.isoformat()
                and old.get("source") == source
            ):
                return
            raw[position_id] = {"at": at.isoformat(), "source": source}
            cutoff = at - KEEP
            for pid, row in list(raw.items()):
                try:
                    if datetime.fromisoformat(row["at"]) < cutoff:
                        del raw[pid]
                except (KeyError, TypeError, ValueError):
                    del raw[pid]
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(tmp, self._path)
            except OSError:
                log.warning("平仓成交时刻写盘失败（%s），不影响结账", self._path, exc_info=True)
