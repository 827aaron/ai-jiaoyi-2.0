"""开会过程：慢环按阶段写 ``data/meeting_live/<SYMBOL>.json``，面板只读、转给前端显示。

一个币一个文件，开会时每一步整份重写；会开完文件留着，就是这个币上一场会的完整过程。
文件是慢环写的、格式可能比面板新：读不出的步骤跳过，不认识的字段忽略，文字过长截断。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from jiaoyi.core.pool_overrides import SYMBOL_RE
from jiaoyi.slow.commander import MARKET_LIVE

MEETING_DIR = "meeting_live"
MARKET = MARKET_LIVE
"""总盘会（看大盘、挑币）的文件名，和币的会同一格式；团队页固定排在最上面。"""
STAGES = ("analysts", "debate", "trader", "risk", "decision")
"""分析师报告 → 多空辩论 → 交易员计划 → 风控辩论 → 结论。"""
MAX_TEXT = 8000
"""单条发言最多给前端这么多字，后面截断。"""
STALE_S = 15 * 60
"""标着“正在开”却这么久没更新：按“中断”显示（兜底；正常重启由启动清理直接标 interrupted）。"""

MeetingStatus = Literal["running", "done", "failed", "interrupted", "stale"]


class MeetingStep(BaseModel):
    stage: str
    who: str
    ts: datetime | None = None
    text: str


class MeetingSummary(BaseModel):
    symbol: str
    status: MeetingStatus
    stage: str | None = None
    """现在开到哪个阶段（``STAGES`` 之一；格式更新后可能是别的）。"""
    started_at: datetime | None = None
    updated_at: datetime | None = None
    finished_at: datetime | None = None
    step_count: int = 0
    bias: str | None = None
    reason: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    summary: str | None = None
    """总盘会：「挑了 N 个 / 落选 M 个」（团队页节点用）。"""
    rejections: list[dict[str, str]] = []
    """总盘会：没挑中的币和各自的理由。"""


class Meeting(MeetingSummary):
    steps: list[MeetingStep] = []


def _time(v: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None
    return t if t and t.tzinfo else None


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _parse(symbol: str, raw: Any, now: datetime) -> Meeting | None:
    if not isinstance(raw, dict):
        return None
    steps = []
    for s in raw.get("steps") or []:
        if isinstance(s, dict) and isinstance(s.get("text"), str):
            text = s["text"]
            if len(text) > MAX_TEXT:
                text = text[:MAX_TEXT] + "…（太长，后面省略）"
            steps.append(
                MeetingStep(
                    stage=_str(s.get("stage")) or "",
                    who=_str(s.get("who")) or "",
                    ts=_time(s.get("ts")),
                    text=text,
                )
            )
    status = raw.get("status")
    if status not in ("running", "done", "failed", "interrupted"):
        status = "failed"
    updated = _time(raw.get("updated_at")) or _time(raw.get("started_at"))
    if status == "running" and (updated is None or (now - updated).total_seconds() > STALE_S):
        status = "stale"
    res = raw.get("result")
    result: dict[str, Any] = res if isinstance(res, dict) else {}
    cost = raw.get("cost_usd")
    return Meeting(
        symbol=symbol,
        status=status,
        stage=_str(raw.get("stage")),
        started_at=_time(raw.get("started_at")),
        updated_at=updated,
        finished_at=_time(raw.get("finished_at")),
        step_count=len(steps),
        bias=_str(result.get("bias")),
        reason=_str(result.get("reason")),
        error=_str(raw.get("error")),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        summary=_str(raw.get("summary")),
        rejections=[
            {"symbol": str(r.get("symbol", "")), "reason": str(r.get("reason", ""))}
            for r in raw.get("rejections") or []
            if isinstance(r, dict)
        ],
        steps=steps,
    )


def read_meeting(data_dir: Path, symbol: str, now: datetime) -> Meeting | None:
    """这个币最近一场会（正在开或上一场）；没有文件、读不出都是 None。``symbol`` 先过白名单。"""
    if symbol != MARKET and not SYMBOL_RE.match(symbol):
        return None
    try:
        raw = json.loads((data_dir / MEETING_DIR / f"{symbol}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return _parse(symbol, raw, now)


def list_meetings(data_dir: Path, now: datetime) -> list[MeetingSummary]:
    """所有币最近一场会的概要：总盘会固定第一，然后正在开的在前，其余按更新时间新到旧。"""
    folder = data_dir / MEETING_DIR
    out: list[MeetingSummary] = []
    for f in folder.glob("*.json") if folder.is_dir() else []:
        m = read_meeting(data_dir, f.stem, now)
        if m:
            out.append(MeetingSummary.model_validate(m.model_dump(exclude={"steps"})))
    out.sort(
        key=lambda m: (
            m.symbol != MARKET,
            m.status != "running",
            -(m.updated_at or now).timestamp(),
        )
    )
    return out
