"""读主循环写在 ``data_dir`` 里的文件。全部只读；文件缺失或读不懂一律当“没有”，面板不能因此崩。

用到的文件（谁写的见各自模块）::

    decision_log/YYYY-MM-DD.jsonl   决策日志（jiaoyi.core.store），按 UTC 日期分文件
    module_status.json              每个角色最新一条 ModuleStatus
    positions.json                  执行层账本（持仓 + 当天起始权益）
    positions_archive/*.jsonl       移出账本的已结束持仓（jiaoyi.core.position_archive）
    risk_state.json                 日亏熔断状态（日期、日初权益、是否已触发）
    blocked_symbols.json            被下架公告封掉新开仓的币
    exit_fills.json                 exit fill time per position (jiaoyi.execution.exit_fills)
    paper_stops.json                模拟盘按主网止损记的模拟成交（jiaoyi.core.paper_stop）
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jiaoyi.contracts import DecisionRecord, ModuleStatus, PositionRecord, Role, SymbolPool
from jiaoyi.contracts.position import PositionStatus
from jiaoyi.core.paper_stop import PAPER_STOP_FILE, PaperStop, PaperStopCache, booked_record
from jiaoyi.core.position_archive import read_archive
from jiaoyi.core.recorder import local_day
from jiaoyi.core.shadow_ledger import read_shadow

log = logging.getLogger(__name__)

DONE = {PositionStatus.CLOSED, PositionStatus.FAILED}
ROLE_LOOKBACK_DAYS = 14
"""团队页某角色最近几条不够时，往前翻多少天的旧日志。"""


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
        with self._lock:
            return list(self._records(utc_day))

    def read_from(self, utc_day: date, start: int) -> tuple[list[DecisionRecord], int]:
        """Records of that day's file from position ``start`` on (file order), and how many the
        file holds now. Only the slice is copied, so polling it every second stays cheap."""
        with self._lock:
            recs = self._records(utc_day)
            return recs[max(0, start) :], len(recs)

    def _records(self, utc_day: date) -> list[DecisionRecord]:
        """Caller holds ``_lock``; the list returned is the cache itself."""
        path = self._dir / f"{utc_day.isoformat()}.jsonl"
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
        return tail.records


class PanelFiles:
    def __init__(self, data_dir: str | Path, tz: str) -> None:
        self.root = Path(data_dir)
        self.tz = tz
        self._decisions = DecisionTail(self.root / "decision_log")
        self._old: dict[tuple[date, frozenset[Role]], tuple[int, list[DecisionRecord]]] = {}
        self._paper = PaperStopCache(self.root / PAPER_STOP_FILE)
        self._archived: dict[str, tuple[object, PositionRecord | None]] = {}
        self.paper_book_pnl: Callable[[], bool] = lambda: True
        """``[fast_loop] paper_stop_book_pnl``, read on every ``ledger()``."""

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

    def decisions_from(self, utc_day: date, start: int) -> tuple[list[DecisionRecord], int]:
        """Parsed records of one UTC day's log from index ``start`` on, and how many it holds."""
        return self._decisions.read_from(utc_day, start)

    def decisions_days(self, now: datetime) -> list[DecisionRecord]:
        """昨天和今天两个 UTC 日的全部记录（不按曼谷日截断），时间升序。

        列表类页面（信号流、团队页、持仓走势、信号链）用它：曼谷 0 点翻日后不会一片空白。
        各项“今天”统计仍用 ``decisions_today``。"""
        utc_today = now.date()
        out: list[DecisionRecord] = []
        for d in (utc_today - timedelta(days=1), utc_today):
            out += self._decisions.read_day(d)
        out.sort(key=lambda r: r.ts)
        return out

    def role_records(
        self, role: Role, limit: int, now: datetime
    ) -> tuple[list[DecisionRecord], bool]:
        """这个角色最新的 ``limit`` 条记录（最新在前，不分哪天），再多一条的话第二项为 True。

        先用昨天 + 今天的；不够就一天天往前翻旧文件，最多翻 ``ROLE_LOOKBACK_DAYS`` 天。"""
        out = [r for r in self.decisions_days(now) if r.role is role]
        day = now.date() - timedelta(days=2)
        back = 0
        while len(out) <= limit and back < ROLE_LOOKBACK_DAYS:
            out = self._old_day(day, frozenset({role})) + out
            day -= timedelta(days=1)
            back += 1
        newest = out[::-1]
        return newest[:limit], len(newest) > limit

    def decisions_since(
        self, start: datetime, now: datetime, roles: frozenset[Role]
    ) -> list[DecisionRecord]:
        """``start`` 以来这几个角色的记录（历史页、资金页用），时间升序。

        今天和昨天走增量缓存；更早的文件先按 ``"role":"…"`` 子串筛行再解析（哨兵一天上万行，
        没必要全解析），结果按文件大小缓存。
        """
        recent = {now.date(), now.date() - timedelta(days=1)}
        out: list[DecisionRecord] = []
        d = start.date()
        while d <= now.date():
            if d in recent:
                out += [r for r in self._decisions.read_day(d) if r.role in roles]
            else:
                out += self._old_day(d, roles)
            d += timedelta(days=1)
        out = [r for r in out if start <= r.ts <= now]
        out.sort(key=lambda r: r.ts)
        return out

    def _old_day(self, day: date, roles: frozenset[Role]) -> list[DecisionRecord]:
        path = self.root / "decision_log" / f"{day.isoformat()}.jsonl"
        try:
            size = path.stat().st_size
        except OSError:
            return []
        key = (day, roles)
        hit = self._old.get(key)
        if hit and hit[0] == size:
            return hit[1]
        needles = [f'"role":"{r.value}"'.encode() for r in roles]
        recs: list[DecisionRecord] = []
        with path.open("rb") as f:
            for line in f:
                if not any(n in line for n in needles):
                    continue
                try:
                    rec = DecisionRecord.model_validate_json(line)
                except ValidationError:
                    continue
                if rec.role in roles:
                    recs.append(rec)
        if len(self._old) > 128:
            self._old.clear()
        self._old[key] = (size, recs)
        return recs

    def first_log_day(self) -> date | None:
        """最早一份决策日志的日期（文件按 UTC 日期命名）；没有就是 None。"""
        days = []
        for f in (self.root / "decision_log").glob("*.jsonl"):
            try:
                days.append(date.fromisoformat(f.stem))
            except ValueError:
                continue
        return min(days, default=None)

    def income_daily(self) -> Any:
        """主程序每 5 分钟从币安收入流水汇总的按日盈亏（data/income_daily.json）；没有就是 None。"""
        return self._json("income_daily.json")

    def mainnet_shadow(self) -> dict[str, dict[str, Any]]:
        """position_id -> mainnet-equivalent row (data/mainnet_shadow.jsonl); empty if none."""
        return read_shadow(self.root)

    def slow_budget_cost(self, day: date) -> float | None:
        """军师团今天已花的钱（data/slow_budget.json，开会前就预记、失败也算，和日上限同一个数）。

        文件的日子不是 ``day`` 或读不懂就是 None。每次都读文件，不缓存。
        """
        raw = self._json("slow_budget.json")
        if not isinstance(raw, dict) or raw.get("day") != day.isoformat():
            return None
        cost = raw.get("cost_usd")
        return float(cost) if isinstance(cost, int | float) and cost >= 0 else None

    def llm_credits(self) -> Any:
        """额度看护写的 data/llm_credits.json（OpenRouter 余额 / 402 时间）；没有就是 None。"""
        return self._json("llm_credits.json")

    def majors(self) -> dict[str, Any] | None:
        """选币器每天拉的市值排名缓存（data/majors.json）；没有就是 None。"""
        raw = self._json("majors.json")
        return raw if isinstance(raw, dict) else None

    def market_snapshot(self) -> dict[str, Any] | None:
        """盘口员每 5 秒写的逐币行情和各路推送计数（data/market_snapshot.json）；没有就是 None。"""
        raw = self._json("market_snapshot.json")
        return raw if isinstance(raw, dict) else None

    def gate_calibration(self, hold: str) -> dict[str, Any] | None:
        """快环 ``jiaoyi gate-calibrate`` 写的 data/gate_calibration/<hold>.json；没有就是 None。"""
        if not re.fullmatch(r"\d+[mh]", hold):
            return None
        raw = self._json(f"gate_calibration/{hold}.json")
        return raw if isinstance(raw, dict) else None

    def prompts_active(self) -> tuple[dict[str, Any], datetime] | None:
        """军师团引擎启动时写的 data/prompts_active.json（生效的提示词原文）和写的时间。"""
        path = self.root / "prompts_active.json"
        raw = self._json("prompts_active.json")
        if not isinstance(raw, dict):
            return None
        try:
            at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        except OSError:
            return None
        return raw, at

    def runtime(self) -> dict[str, Any]:
        """主程序这次启动写的 runtime.json（启动时间、桩角色等）；没有或读不出就是空的。"""
        raw = self._json("runtime.json")
        return raw if isinstance(raw, dict) else {}

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

    def _archived_records(
        self, live: dict[str, Any]
    ) -> list[tuple[dict[str, Any], PositionRecord]]:
        """归档里账本没有的那些仓；按原始行缓存校验结果，归档文件没变就不重新校验。"""
        out: list[tuple[dict[str, Any], PositionRecord]] = []
        seen: dict[str, tuple[object, PositionRecord | None]] = {}
        for pid, t in read_archive(self.root).items():
            hit = self._archived.get(pid)
            if hit is not None and hit[0] is t:
                rec = hit[1]
            else:
                try:
                    rec = PositionRecord.model_validate(t["record"])
                except (ValidationError, KeyError, TypeError):
                    rec = None
            seen[pid] = (t, rec)
            if rec is not None and pid not in live:
                out.append((t, rec))
        self._archived = seen
        return out

    def ledger(self) -> Ledger:
        raw = self._json("positions.json")
        if not isinstance(raw, dict):
            raw = {}
        positions: list[PositionRecord] = []
        notional: dict[str, float] = {}
        protection: dict[str, object] = {}
        filled: set[str] = set()
        live = raw.get("positions") or {}
        for t, rec in self._archived_records(live):
            positions.append(rec)
            notional[rec.position_id] = float(t.get("notional_usdt") or 0.0)
        for t in live.values():
            try:
                rec = PositionRecord.model_validate(t["record"])
            except (ValidationError, KeyError, TypeError):
                continue
            positions.append(rec)
            notional[rec.position_id] = float(t.get("notional_usdt") or 0.0)
            if t.get("server_protection"):
                protection[rec.position_id] = t["server_protection"]
            if _exit_filled(t):
                filled.add(rec.position_id)
        paper = self._paper.get()
        actual: dict[str, PositionRecord] = {}
        if paper and self.paper_book_pnl():
            booked = []
            for rec in positions:
                stop = paper.get(rec.position_id)
                moved = booked_record(rec, stop) if stop is not None else rec
                if moved is not rec:
                    actual[rec.position_id] = rec
                booked.append(moved)
            positions = booked
        ds = raw.get("day_start") or {}
        return Ledger(
            positions=positions,
            exit_filled=filled,
            paper=paper,
            actual=actual,
            planned_notional=notional,
            day_start_day=str(ds.get("day")) if ds.get("day") else None,
            day_start_equity=_float(ds.get("equity_usdt")),
            day_refs={
                str(k): px
                for k, v in (ds.get("refs") or {}).items()
                if (px := _float(v)) is not None and px > 0
            }
            if isinstance(ds.get("refs"), dict)
            else {},
            last_equity=_float(raw.get("last_equity_usdt")),
            protection=protection,
        )

    def exit_fills(self) -> dict[str, datetime]:
        """Position id -> time of its last exchange exit fill; empty when missing or unreadable,
        bad rows skipped. Same format as ``jiaoyi.execution.exit_fills``, parsed here because the
        panel must not import the execution layer."""
        raw = self._json("exit_fills.json")
        out: dict[str, datetime] = {}
        for pid, row in raw.items() if isinstance(raw, dict) else ():
            at = row.get("at") if isinstance(row, dict) else None
            try:
                when = datetime.fromisoformat(at) if isinstance(at, str) else None
            except ValueError:
                continue
            if when is not None and when.tzinfo is not None:
                out[str(pid)] = when
        return out

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


def _exit_filled(tracked: dict[str, object]) -> bool:
    """A closing position whose direct close order, or a triggered protection leg's child
    order, reads back FILLED."""
    record = tracked.get("record")
    if not isinstance(record, dict) or record.get("status") != "closing":
        return False
    if tracked.get("direct_close_order_status") == "FILLED":
        return True
    state = tracked.get("server_protection")
    legs = state.get("legs") if isinstance(state, dict) else None
    return isinstance(legs, list) and any(
        isinstance(leg, dict) and leg.get("actual_order_status") == "FILLED" for leg in legs
    )


@dataclass
class Ledger:
    positions: list[PositionRecord] = field(default_factory=list)
    planned_notional: dict[str, float] = field(default_factory=dict)
    """开仓指令里的名义价值；还没成交（没有入场价）时敞口按它算。"""
    day_start_day: str | None = None
    day_start_equity: float | None = None
    day_refs: dict[str, float] = field(default_factory=dict)
    """币 → ``day_start_day`` 当地 0 点的价格（当日参考价）；0 点时大脑没在跑就没有。"""
    last_equity: float | None = None
    protection: dict[str, object] = field(default_factory=dict)
    """持仓号 → positions.json 里 server_protection 的原始 JSON（旧仓位没有）。"""
    exit_filled: set[str] = field(default_factory=set)
    """Position ids whose exit order has filled on the exchange; the ledger still says closing
    until the protection legs are cancelled and zero is verified."""
    paper: dict[str, PaperStop] = field(default_factory=dict)
    """Position id -> its paper stop booking, whether or not ``positions`` uses it."""
    actual: dict[str, PositionRecord] = field(default_factory=dict)
    """Demo's own record of every position whose entry in ``positions`` was moved to its
    paper exit; positions not moved are absent."""

    def demo(self, p: PositionRecord) -> PositionRecord:
        """The record as Demo filled it (money that really moved on the account)."""
        return self.actual.get(p.position_id, p)

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


def read_pool(root: Path) -> SymbolPool | None:
    """最新一次选币（主循环每次选完写 ``symbol_pool.json``）。"""
    try:
        return SymbolPool.model_validate_json(
            (root / "symbol_pool.json").read_text(encoding="utf-8")
        )
    except (OSError, ValidationError):
        return None


def read_live_gate(root: Path) -> tuple[bool, bool]:
    """(面板密码设了没有, 实盘解锁了没有)。只看字段有没有值，不碰哈希本身。"""
    try:
        raw = json.loads((root / "live_gate.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, False
    if not isinstance(raw, dict):
        return False, False
    return bool(raw.get("hash_hex")), raw.get("armed_at") is not None
