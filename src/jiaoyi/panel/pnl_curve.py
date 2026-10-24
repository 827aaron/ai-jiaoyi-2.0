"""今日盈亏曲线：面板常驻服务每分钟记一个点（data/pnl_curve/<曼谷日期>.jsonl），总览点开看。

每个点记总资产和当时的 0 点基准；读的时候按**现在的**基准算盈亏，和顶部「今日盈亏」同一口径
（总资产 − 0 点总资产，含浮动）。总资产只在 hummingbot-api 拿得到时记，拿不到就空着不补，
免得用账本里的旧数画出一段假平线。只留最近 ``KEEP_DAYS`` 天。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

CURVE_DIR = "pnl_curve"
SAMPLE_EVERY_S = 60.0
KEEP_DAYS = 14


class PnlPoint(BaseModel):
    ts: datetime
    pnl_usdt: float


class PnlCurve(BaseModel):
    day: str
    start_equity_usdt: float | None
    points: list[PnlPoint]
    """时间升序；最后一个是此刻（和顶部同一个数）。"""
    high: PnlPoint | None = None
    low: PnlPoint | None = None
    now: PnlPoint | None = None


def _file(root: Path, day: str) -> Path:
    return root / CURVE_DIR / f"{day}.jsonl"


def record(root: str | Path, day: str, ts: datetime, equity: float, start: float) -> None:
    """追加一个点；顺手删掉 ``KEEP_DAYS`` 天以前的文件。"""
    path = _file(Path(root), day)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": ts.isoformat(), "equity": round(equity, 4), "start": round(start, 4)}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    cutoff = (date.fromisoformat(day) - timedelta(days=KEEP_DAYS)).isoformat()
    for old in path.parent.glob("*.jsonl"):
        if old.stem < cutoff:
            old.unlink(missing_ok=True)


def read(root: str | Path, day: str, start: float | None, now: PnlPoint | None = None) -> PnlCurve:
    """``start`` 是现在的 0 点基准（没有就按每个点记下的基准）；``now`` 是此刻顶部的数。"""
    points: list[PnlPoint] = []
    try:
        lines = _file(Path(root), day).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for ln in lines:
        try:
            r = json.loads(ln)
            base = start if start else float(r["start"])
            points.append(
                PnlPoint(ts=datetime.fromisoformat(r["ts"]), pnl_usdt=float(r["equity"]) - base)
            )
        except (ValueError, KeyError, TypeError):
            continue  # 写到一半的行（断电）跳过
    if now is not None and (not points or now.ts > points[-1].ts):
        points.append(now)
    return PnlCurve(
        day=day,
        start_equity_usdt=start,
        points=points,
        high=max(points, key=lambda p: p.pnl_usdt, default=None),
        low=min(points, key=lambda p: p.pnl_usdt, default=None),
        now=points[-1] if points else None,
    )
