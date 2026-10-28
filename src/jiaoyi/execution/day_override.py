"""启动时一次性改当天日亏基准：``data/day_start_override.json``。

运行中的进程把基准放在内存里、会覆盖落盘文件，所以只能在启动、执行器和风控员第一次用基准之前改。
格式 ``{"day": "YYYY-MM-DD", "equity_usdt": 5751.86, "refs": {"BTCUSDT": 68000.0},
"reason": "..."}``。
用掉后改名 ``.applied-<时间>``，日期不是今天或数不对改名 ``.rejected-<时间>``，都不会再用第二次。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

OVERRIDE_FILE = "day_start_override.json"
CHANGES_FILE = "config_changes.jsonl"


class _Executor(Protocol):
    def override_day_start(
        self, day: str, equity: float, refs: dict[str, float]
    ) -> float | None: ...


class _Breaker(Protocol):
    def override_start(self, day: date, equity: float) -> float | None: ...


def _parse(raw: Any, today: date) -> tuple[float, dict[str, float], str]:
    if not isinstance(raw, dict):
        raise ValueError("不是 JSON 对象")
    if raw.get("day") != today.isoformat():
        raise ValueError(f"日期 {raw.get('day')!r} 不是今天 {today.isoformat()}")
    equity = raw.get("equity_usdt")
    if isinstance(equity, bool) or not isinstance(equity, int | float):
        raise ValueError(f"equity_usdt {equity!r} 不是数")
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError(f"equity_usdt {equity!r} 不是正数")
    refs_raw = raw.get("refs") or {}
    if not isinstance(refs_raw, dict):
        raise ValueError("refs 不是对象")
    refs: dict[str, float] = {}
    for sym, px in refs_raw.items():
        if isinstance(px, bool) or not isinstance(px, int | float):
            raise ValueError(f"refs.{sym} {px!r} 不是数")
        if not math.isfinite(px) or px <= 0:
            raise ValueError(f"refs.{sym} {px!r} 不是正数")
        refs[str(sym)] = float(px)
    return float(equity), refs, str(raw.get("reason") or "")


def _direction(old: float | None, new: float) -> str:
    if old is None:
        return "原来没有今天的基准"
    if new > old:
        return "熔断变严（基准调高，同样的权益算出的今日亏损更大）"
    if new < old:
        return "熔断变松（基准调低，同样的权益算出的今日亏损更小）"
    return "熔断松紧不变"


def apply_day_override(
    data_dir: str | Path,
    now: datetime,
    tz: str,
    executor: _Executor,
    breaker: _Breaker | None,
) -> bool:
    """有 override 文件就用掉它（改执行器基准、风控员日初权益、当日参考价），返回是否改了。"""
    path = Path(data_dir) / OVERRIDE_FILE
    if not path.is_file():
        return False
    stamp = now.astimezone(ZoneInfo(tz)).strftime("%Y%m%dT%H%M%S")
    today = now.astimezone(ZoneInfo(tz)).date()
    try:
        equity, refs, reason = _parse(json.loads(path.read_text(encoding="utf-8")), today)
    except (OSError, ValueError) as e:
        path.rename(path.with_name(f"{path.name}.rejected-{stamp}"))
        log.warning("日亏基准修正文件没用上（%s），什么都没改，已改名 .rejected-%s", e, stamp)
        return False
    old_exec = executor.override_day_start(today.isoformat(), equity, refs)
    old_breaker = breaker.override_start(today, equity) if breaker is not None else None
    old = old_breaker if old_breaker is not None else old_exec
    direction = _direction(old, equity)
    row = {
        "ts": now.isoformat(),
        "file": OVERRIDE_FILE,
        "section": "day_start",
        "key": "equity_usdt",
        "old": old,
        "new": equity,
        "note": f"{today.isoformat()} 日亏基准人工修正：{direction}"
        + (f"；{reason}" if reason else ""),
    }
    with (Path(data_dir) / CHANGES_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    path.rename(path.with_name(f"{path.name}.applied-{stamp}"))
    log.warning(
        "日亏基准已人工修正 %s：执行器 %s → %.2f，风控员 %s → %.2f，当日参考价 %d 个币；%s",
        today.isoformat(),
        old_exec,
        equity,
        old_breaker,
        equity,
        len(refs),
        direction,
    )
    return True
