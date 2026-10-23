"""收入流水按日汇总：曼谷日切、重算最近 7 天、更早的保留、拉不到不写。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from jiaoyi.execution.exchange import CcxtBinanceFutures, ExchangeError, IncomeRow
from jiaoyi.execution.income_daily import INCOME_DAILY_FILE, IncomeDaily, summarize

BKK = ZoneInfo("Asia/Bangkok")
NOW = datetime(2026, 10, 23, 8, 0, tzinfo=UTC)  # 曼谷 15:00


def _row(local: str, kind: str, income: float) -> IncomeRow:
    return IncomeRow(datetime.fromisoformat(local).replace(tzinfo=BKK), kind, income)


def test_summarize_splits_by_local_day_and_ignores_other_types() -> None:
    rows = [
        _row("2026-10-22T23:59:00", "REALIZED_PNL", 10.0),
        _row("2026-10-23T00:00:30", "REALIZED_PNL", -4.0),
        _row("2026-10-23T00:00:30", "COMMISSION", -0.5),
        _row("2026-10-23T08:00:00", "FUNDING_FEE", 0.25),
        _row("2026-10-23T08:00:00", "TRANSFER", 1000.0),
    ]
    days = summarize(rows, BKK)
    assert days["2026-10-22"] == {"realized_pnl": 10.0, "commission": 0.0, "funding_fee": 0.0}
    assert days["2026-10-23"] == {"realized_pnl": -4.0, "commission": -0.5, "funding_fee": 0.25}


async def test_refresh_recomputes_last_7_days_and_keeps_older(tmp_path: Path) -> None:
    old = {
        "tz": "Asia/Bangkok",
        "since": "2026-10-10",
        "days": {
            "2026-10-10": {"realized_pnl": 1.0, "commission": -0.1, "funding_fee": 0.0},
            "2026-10-20": {"realized_pnl": 99.0, "commission": 0.0, "funding_fee": 0.0},
        },
    }
    (tmp_path / INCOME_DAILY_FILE).write_text(json.dumps(old), encoding="utf-8")
    seen: list[datetime] = []

    async def fetch(since: datetime) -> list[IncomeRow]:
        seen.append(since)
        return [_row("2026-10-23T09:00:00", "COMMISSION", -0.3)]

    assert await IncomeDaily(fetch, tmp_path, "Asia/Bangkok").refresh(NOW)
    assert seen == [datetime(2026, 10, 17, tzinfo=BKK)]
    doc = json.loads((tmp_path / INCOME_DAILY_FILE).read_text(encoding="utf-8"))
    assert doc["tz"] == "Asia/Bangkok" and doc["since"] == "2026-10-10"
    assert datetime.fromisoformat(doc["updated_at"]).utcoffset() == timedelta(hours=7)
    assert list(doc["days"]) == ["2026-10-10", *(f"2026-10-{d}" for d in range(17, 24))]
    assert doc["days"]["2026-10-10"]["realized_pnl"] == 1.0  # 7 天前的不动
    assert doc["days"]["2026-10-20"]["realized_pnl"] == 0.0  # 7 天内按流水重算
    assert doc["days"]["2026-10-23"]["commission"] == -0.3


async def test_fetch_failure_leaves_the_file_alone(tmp_path: Path) -> None:
    (tmp_path / INCOME_DAILY_FILE).write_text("坏了", encoding="utf-8")

    async def broken(since: datetime) -> list[IncomeRow]:
        raise ExchangeError("网络")

    assert not await IncomeDaily(broken, tmp_path, "Asia/Bangkok").refresh(NOW)
    assert (tmp_path / INCOME_DAILY_FILE).read_text(encoding="utf-8") == "坏了"

    async def empty(since: datetime) -> list[IncomeRow]:
        return []

    assert await IncomeDaily(empty, tmp_path, "Asia/Bangkok").refresh(NOW)  # 坏文件按空重建
    doc = json.loads((tmp_path / INCOME_DAILY_FILE).read_text(encoding="utf-8"))
    assert doc["since"] == "2026-10-17" and len(doc["days"]) == 7


async def test_ccxt_income_rows_pages_and_filters() -> None:
    class FakeCcxt:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def fapiPrivateGetIncome(self, params: dict[str, Any]) -> list[dict[str, Any]]:
            self.calls.append(params)
            if params["startTime"] < 2000:
                return [
                    {"time": 1000 + i, "incomeType": "COMMISSION", "income": "-0.01"}
                    for i in range(1000)
                ]
            return [
                {"time": 5000, "incomeType": "TRANSFER", "income": "500"},
                {"time": 5001, "incomeType": "REALIZED_PNL", "income": "2.5"},
            ]

    ex = CcxtBinanceFutures("k", "s", testnet=True)
    fake = FakeCcxt()
    ex._ex = fake  # type: ignore[assignment]
    rows = await ex.income_rows(datetime.fromtimestamp(1, UTC))
    assert len(rows) == 1001 and rows[-1].kind == "REALIZED_PNL"
    assert fake.calls[1]["startTime"] == 2000
    assert await ex.income_since(datetime.fromtimestamp(1, UTC)) == pytest.approx(-10 + 2.5)
