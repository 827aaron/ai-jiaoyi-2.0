"""币安 U 本位收入流水按日汇总，写 ``data/income_daily.json`` 给面板的盈利口径。

- 主程序每 ``INCOME_REFRESH_S`` 秒拉一次，每次重算最近 ``RECOMPUTE_DAYS`` 天（含今天），
  更早的日子保留文件里原来的数，不动。
- 日期按 ``risk.day_reset_tz`` 的当地日切（同日亏熔断）。三个数直接用币安的正负号：
  手续费为负，资金费可正可负，``realized_pnl`` 是扣手续费前的已实现盈亏。
- 拉不到（网络、交易所报错）就不写，面板看 ``updated_at`` 知道多久没更新。
  文件读不出时按空的重建（只剩最近 7 天），并记日志。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jiaoyi.execution.exchange import IncomeRow

log = logging.getLogger(__name__)

INCOME_DAILY_FILE = "income_daily.json"
INCOME_REFRESH_S = 300.0
RECOMPUTE_DAYS = 7
_FIELDS = {"REALIZED_PNL": "realized_pnl", "COMMISSION": "commission", "FUNDING_FEE": "funding_fee"}

IncomeRows = Callable[[datetime], Awaitable[list[IncomeRow]]]
DayTotals = dict[str, float]


def _empty() -> DayTotals:
    return {name: 0.0 for name in _FIELDS.values()}


def summarize(rows: Iterable[IncomeRow], tz: ZoneInfo) -> dict[str, DayTotals]:
    """流水 → {当地日期: {realized_pnl, commission, funding_fee}}。别的类型不算。"""
    days: dict[str, DayTotals] = {}
    for row in rows:
        field = _FIELDS.get(row.kind)
        if field is None:
            continue
        day = row.time.astimezone(tz).date().isoformat()
        days.setdefault(day, _empty())[field] += row.income
    return days


def read_income_daily(data_dir: str | Path) -> dict[str, Any] | None:
    """读文件；不存在返回 None，读不出也返回 None（记日志）。"""
    path = Path(data_dir) / INCOME_DAILY_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("%s 读不出，按空的重建", path, exc_info=True)
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("days"), dict):
        log.warning("%s 格式不对，按空的重建", path)
        return None
    return raw


class IncomeDaily:
    def __init__(self, fetch: IncomeRows, data_dir: str | Path, tz: str) -> None:
        self._fetch = fetch
        self._path = Path(data_dir) / INCOME_DAILY_FILE
        self._data_dir = data_dir
        self._tz_name = tz
        self._tz = ZoneInfo(tz)

    async def refresh(self, now: datetime) -> bool:
        """拉最近 7 天流水、并进文件。拉不到返回 False，文件不动。"""
        today = now.astimezone(self._tz).date()
        first = today - timedelta(days=RECOMPUTE_DAYS - 1)
        start = datetime.combine(first, datetime.min.time(), self._tz)
        try:
            rows = await self._fetch(start)
        except Exception as e:
            log.warning("拉收入流水失败，income_daily.json 这次不更新：%r", e)
            return False
        fresh = summarize(rows, self._tz)
        old = read_income_daily(self._data_dir)
        days: dict[str, DayTotals] = {}
        if old is not None:
            days = {d: v for d, v in old["days"].items() if d < first.isoformat()}
        for i in range(RECOMPUTE_DAYS):
            day = (first + timedelta(days=i)).isoformat()
            totals = fresh.get(day, _empty())
            days[day] = {k: round(v, 8) for k, v in totals.items()}
        doc = {
            "tz": self._tz_name,
            "updated_at": now.astimezone(self._tz).isoformat(),
            "since": min(days),
            "days": dict(sorted(days.items())),
        }
        tmp = self._path.with_name(f".{self._path.name}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            log.exception("写 %s 失败", self._path)
            return False
        return True
