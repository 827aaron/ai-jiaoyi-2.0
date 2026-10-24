"""今日盈亏曲线：面板常驻服务每分钟记一个点（data/pnl_curve/<曼谷日期>.jsonl），总览点开看。

每个点记总资产和当时的 0 点基准；读的时候按**现在的**基准算盈亏，和顶部「今日盈亏」同一口径
（总资产 − 0 点总资产，含浮动）。总资产只在 hummingbot-api 拿得到时记，拿不到就空着不补，
免得用账本里的旧数画出一段假平线。只留最近 ``KEEP_DAYS`` 天。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ValidationError

from jiaoyi.contracts import PositionRecord

CURVE_DIR = "pnl_curve"
SAMPLE_EVERY_S = 60.0
KEEP_DAYS = 14


class PnlPoint(BaseModel):
    ts: datetime
    pnl_usdt: float
    est: bool = False
    """回填的估算点（面板开始记之前那段，按账本成交 + 5 分钟 K 线算）。"""


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
    for old in path.parent.glob("*.json*"):
        if old.stem < cutoff:
            old.unlink(missing_ok=True)


def read(
    root: str | Path,
    day: str,
    start: float | None,
    now: PnlPoint | None = None,
    est: list[PnlPoint] | None = None,
) -> PnlCurve:
    """``start`` 是现在的 0 点基准（没有就按每个点记下的基准）；``now`` 是此刻顶部的数；
    ``est`` 是记录开始之前的回填估算点（只取早于第一个记录点的）。"""
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
    if est and points:
        points = [p for p in est if p.ts < points[0].ts] + points
    return PnlCurve(
        day=day,
        start_equity_usdt=start,
        points=points,
        high=max(points, key=lambda p: p.pnl_usdt, default=None),
        low=min(points, key=lambda p: p.pnl_usdt, default=None),
        now=points[-1] if points else None,
    )


def first_recorded(root: str | Path, day: str) -> datetime | None:
    """当天第一个记录点的时间；还没有就是 None。"""
    try:
        with _file(Path(root), day).open(encoding="utf-8") as f:
            return datetime.fromisoformat(json.loads(f.readline())["ts"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


EST_STEP = timedelta(minutes=5)
KlineFn = Callable[[str], Awaitable[list[dict[str, float]]]]


async def backfill(
    positions: list[PositionRecord], since: datetime, until: datetime, klines: KlineFn
) -> list[PnlPoint]:
    """面板开始记之前（0 点 → ``until``）的估算曲线，每 5 分钟一个点。

    盈亏 = 0 点后平掉的单的净盈亏（0 点前就开着的，扣掉 0 点前那段）
         + 那一刻还开着的单按 5 分钟收盘价算的浮盈（0 点前开的从 0 点价算起）。
    不含资金费、转账，所以和第一个真实记录点会差一点。K 线拿不到就抛 ``KlineError``。
    """
    mine = [
        p
        for p in positions
        if p.entry_price is not None
        and p.opened_at < until
        and (p.closed_at is None or p.closed_at >= since)
    ]
    bars = {sym: await klines(sym) for sym in sorted({p.symbol for p in mine})}

    def price(sym: str, t: datetime) -> float | None:
        """``t`` 那一刻最近一根已收的 5 分钟收盘价。"""
        ts = t.timestamp()
        last = None
        for b in bars[sym]:
            if b["time"] + 300 <= ts:
                last = b["close"]
            else:
                break
        return last

    out: list[PnlPoint] = []
    t = since
    while t < until:
        total = 0.0
        for p in mine:
            sign = 1.0 if p.side.value == "long" else -1.0
            assert p.entry_price is not None
            ref = p.entry_price
            if p.opened_at < since:  # 0 点前就开着：只算 0 点之后的变化
                ref = price(p.symbol, since) or p.entry_price
            if p.closed_at is not None and p.closed_at <= t:
                pre = sign * p.quantity * (ref - p.entry_price)  # 0 点前那段（0 点后开的为 0）
                total += (p.realized_pnl_usdt or 0.0) - pre
            elif p.opened_at <= t:
                now_px = price(p.symbol, t)
                if now_px is not None:
                    total += sign * p.quantity * (now_px - ref)
        out.append(PnlPoint(ts=t, pnl_usdt=total, est=True))
        t += EST_STEP
    return out


def save_est(root: str | Path, day: str, points: list[PnlPoint]) -> None:
    path = Path(root) / CURVE_DIR / f"{day}.est.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([p.model_dump(mode="json") for p in points]), encoding="utf-8")


def load_est(root: str | Path, day: str) -> list[PnlPoint] | None:
    try:
        raw = json.loads((Path(root) / CURVE_DIR / f"{day}.est.json").read_text(encoding="utf-8"))
        return [PnlPoint.model_validate(x) for x in raw]
    except (OSError, ValueError, TypeError, ValidationError):
        return None
