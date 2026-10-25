"""总盘会的记录：每轮写一行，事后回填池子里每个币的实际结果，挑得准不准才算得出来。

- ``data/market_meeting/YYYY-MM-DD.jsonl``（UTC 日期）：每轮一行——开会档位、挑中几个、大盘判断、
  池子概况、花费、用时、是否重问、校验问题，以及池子里每个币的
  {tier, score, score_old, score_breakdown, metrics}，候选再加 {pick, reason}。
- ``data/market_meeting/outcomes.jsonl``：持仓时长走完后，用选币时拉到的 1 小时 K 线算每个币从开会
  后第一个整点起、一个持仓时长内的高低差和涨跌（近似）。

只写文件，不改任何决策。写失败只打日志。
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi.contracts import SymbolPoolEntry

logger = logging.getLogger(__name__)

HOUR_MS = 3_600_000
LOOKBACK_DAYS = 3
"""回填只看最近几天的记录（K 线也只拉了几十小时）。"""
GIVE_UP_AFTER = timedelta(hours=72)
"""结果算不出来（K 线不够早）且过了这么久就不再试，写一行空结果收尾。"""


def round_id(ts: datetime) -> str:
    return f"{ts.astimezone(UTC):%Y%m%dT%H%M%S}"


def pool_rows(
    pool: Sequence[SymbolPoolEntry], decided: dict[str, tuple[bool, str]]
) -> list[dict[str, Any]]:
    """池子里每个币一行；候选再带 pick / reason。"""
    rows: list[dict[str, Any]] = []
    for e in pool:
        row: dict[str, Any] = {
            "symbol": e.symbol,
            "tier": e.tier,
            "category": e.category.value,
            "score": e.score,
            "score_old": e.metrics.get("score_old"),
            "score_breakdown": e.score_breakdown,
            "metrics": e.metrics,
            "always_candidate": e.always_candidate,
        }
        if e.symbol in decided:
            row["pick"], row["reason"] = decided[e.symbol]
        rows.append(row)
    return rows


class MarketLog:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def outcomes_path(self) -> Path:
        return self._root / "outcomes.jsonl"

    def write_round(self, row: dict[str, Any]) -> None:
        ts = datetime.fromisoformat(str(row["ts"]))
        path = self._root / f"{ts.astimezone(UTC):%Y-%m-%d}.jsonl"
        self._append(path, row)

    def _append(self, path: Path, row: dict[str, Any]) -> None:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        except OSError:
            logger.exception("写 %s 失败：这一轮总盘会的记录丢了", path)

    # ------------------------------------------------------------ 回填
    def _pending(self, now: datetime) -> list[dict[str, Any]]:
        done: set[str] = set()
        if self.outcomes_path.is_file():
            for line in self.outcomes_path.read_text(encoding="utf-8").splitlines():
                try:
                    done.add(str(json.loads(line)["round_id"]))
                except (ValueError, KeyError, TypeError):
                    continue
        out: list[dict[str, Any]] = []
        for back in range(LOOKBACK_DAYS + 1):
            day = (now - timedelta(days=back)).astimezone(UTC)
            path = self._root / f"{day:%Y-%m-%d}.jsonl"
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("round_id") and r["round_id"] not in done:
                    out.append(r)
        return out

    def backfill(
        self, now: datetime, bars: dict[str, Sequence[tuple[int, float, float, float, float]]]
    ) -> int:
        """持仓时长走完的轮次，用 ``bars``（{币: [(开盘毫秒, 开, 高, 低, 收)…]}）补上结果。
        返回这次写了几轮。"""
        try:
            pending = self._pending(now)
        except OSError:
            logger.exception("读总盘会记录失败，这次不回填")
            return 0
        wrote = 0
        now_ms = now.timestamp() * 1000.0
        for r in pending:
            try:
                ts = datetime.fromisoformat(str(r["ts"]))
                hold_h = max(float(r["hold_hours"]), 1.0)
            except (KeyError, ValueError, TypeError):
                continue
            n = max(math.ceil(hold_h), 1)
            first_ms = (int(ts.timestamp() * 1000) // HOUR_MS + 1) * HOUR_MS  # 开会后第一个整点
            end_ms = first_ms + n * HOUR_MS
            if now_ms < end_ms:
                continue  # 持仓时长还没走完
            symbols: dict[str, dict[str, float]] = {}
            for sym in (str(x["symbol"]) for x in r.get("pool", []) if "symbol" in x):
                window = [b for b in bars.get(sym, ()) if first_ms <= b[0] < end_ms]
                if len(window) != n:
                    continue
                first_open = window[0][1]
                if first_open <= 0:
                    continue
                symbols[sym] = {
                    "range_pct": (max(b[2] for b in window) - min(b[3] for b in window))
                    / first_open
                    * 100.0,
                    "chg_pct": (window[-1][4] / first_open - 1.0) * 100.0,
                }
            if not symbols and now - ts < GIVE_UP_AFTER:
                continue  # K 线可能还没到，下次再试
            self._append(
                self.outcomes_path,
                {
                    "round_id": r["round_id"],
                    "ts": r["ts"],
                    "hold_hours": hold_h,
                    "from": datetime.fromtimestamp(first_ms / 1000, UTC).isoformat(),
                    "approx": True,
                    "symbols": symbols,
                },
            )
            wrote += 1
        return wrote
