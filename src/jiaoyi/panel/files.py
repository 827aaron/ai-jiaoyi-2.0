"""读主循环写在 ``data_dir`` 里的文件。全部只读；文件缺失或读不懂一律当“没有”，面板不能因此崩。

用到的文件（谁写的见各自模块）::

    decision_log/YYYY-MM-DD.jsonl   决策日志（jiaoyi.core.store），按 UTC 日期分文件
    module_status.json              每个角色最新一条 ModuleStatus
    positions.json                  执行层账本（持仓 + 当天起始权益）
    risk_state.json                 日亏熔断状态（日期、日初权益、是否已触发）
    blocked_symbols.json            被下架公告封掉新开仓的币
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jiaoyi.contracts import DecisionRecord, ModuleStatus, PositionRecord
from jiaoyi.contracts.position import PositionStatus
from jiaoyi.core.recorder import local_day

log = logging.getLogger(__name__)

DONE = {PositionStatus.CLOSED, PositionStatus.FAILED}


@dataclass
class _Tail:
    offset: int = 0
    records: list[DecisionRecord] = field(default_factory=list)


class DecisionTail:
    """增量读决策日志：每个文件记住读到哪，下次只解析新追加的整行。

    快环一天能写上万行，面板每几秒刷新一次，不能每次全量重读。文件变短（被换掉）就从头读。
    """

    def __init__(self, log_dir: Path) -> None:
        self._dir = log_dir
        self._tails: dict[str, _Tail] = {}
        self._lock = threading.Lock()

    def read_day(self, utc_day: date) -> list[DecisionRecord]:
        path = self._dir / f"{utc_day.isoformat()}.jsonl"
        with self._lock:
            tail = self._tails.setdefault(path.name, _Tail())
            try:
                size = path.stat().st_size
            except OSError:
                self._tails.pop(path.name, None)
                return []
            if size < tail.offset:
                tail.offset, tail.records = 0, []
            if size > tail.offset:
                with path.open("rb") as f:
                    f.seek(tail.offset)
                    chunk = f.read(size - tail.offset)
                end = chunk.rfind(b"\n") + 1  # 只吃整行；正在写的半行留到下次
                for line in chunk[:end].splitlines():
                    if not line.strip():
                        continue
                    try:
                        tail.records.append(DecisionRecord.model_validate_json(line))
                    except ValidationError:
                        log.warning("跳过读不懂的决策日志行：%.80r", line)
                tail.offset += end
            return list(tail.records)


class PanelFiles:
    def __init__(self, data_dir: str | Path, tz: str) -> None:
        self.root = Path(data_dir)
        self.tz = tz
        self._decisions = DecisionTail(self.root / "decision_log")

    def _json(self, name: str) -> Any:
        try:
            return json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def decisions_today(self, now: datetime) -> list[DecisionRecord]:
        """按 ``tz`` 的“今天”（曼谷 0 点起）取记录，时间升序。日志按 UTC 日期分文件，所以读两天。"""
        today = local_day(now, self.tz)
        utc_today = now.date()
        out: list[DecisionRecord] = []
        for d in (utc_today - timedelta(days=1), utc_today):
            out += [r for r in self._decisions.read_day(d) if local_day(r.ts, self.tz) == today]
        out.sort(key=lambda r: r.ts)
        return out

    def module_status(self) -> dict[str, ModuleStatus]:
        raw = self._json("module_status.json")
        out: dict[str, ModuleStatus] = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                try:
                    out[str(k)] = ModuleStatus.model_validate(v)
                except ValidationError:
                    log.warning("module_status.json 里 %s 读不懂", k)
        return out

    def ledger(self) -> Ledger:
        raw = self._json("positions.json")
        if not isinstance(raw, dict):
            return Ledger()
        positions: list[PositionRecord] = []
        notional: dict[str, float] = {}
        for t in (raw.get("positions") or {}).values():
            try:
                rec = PositionRecord.model_validate(t["record"])
            except (ValidationError, KeyError, TypeError):
                continue
            positions.append(rec)
            notional[rec.position_id] = float(t.get("notional_usdt") or 0.0)
        ds = raw.get("day_start") or {}
        return Ledger(
            positions=positions,
            planned_notional=notional,
            day_start_day=str(ds.get("day")) if ds.get("day") else None,
            day_start_equity=_float(ds.get("equity_usdt")),
            last_equity=_float(raw.get("last_equity_usdt")),
        )

    def breaker(self) -> BreakerState | None:
        raw = self._json("risk_state.json")
        if not isinstance(raw, dict) or "day" not in raw:
            return None
        return BreakerState(
            day=str(raw["day"]),
            start_equity=_float(raw.get("start_equity")),
            tripped=bool(raw.get("tripped")),
        )

    def blocked(self) -> dict[str, str]:
        """格式同 ``jiaoyi.app.read_blocked``；自己读，免得把组装层（含执行层）拉进来。"""
        raw = self._json("blocked_symbols.json")
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


@dataclass
class Ledger:
    positions: list[PositionRecord] = field(default_factory=list)
    planned_notional: dict[str, float] = field(default_factory=dict)
    """开仓指令里的名义价值；还没成交（没有入场价）时敞口按它算。"""
    day_start_day: str | None = None
    day_start_equity: float | None = None
    last_equity: float | None = None

    def live(self) -> list[PositionRecord]:
        return [p for p in self.positions if p.status not in DONE]

    def notional(self, p: PositionRecord) -> float:
        return p.notional_usdt or self.planned_notional.get(p.position_id, 0.0)


@dataclass
class BreakerState:
    day: str
    start_equity: float | None
    tripped: bool


def _float(v: object) -> float | None:
    if isinstance(v, int | float) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None
