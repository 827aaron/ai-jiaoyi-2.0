"""谁发起的平仓（``data/close_sources.json``）：持仓 id → {source, detail, ts}。

``PositionRecord.close_reason`` 是枚举、合同不收多余字段，往账本里加新值或新字段，回滚后的旧代码读
``positions.json`` 会直接报错。所以平仓原因照旧记已有的枚举值，「是谁、为什么」另记在这个文件里，
面板和复盘按持仓 id 查。旧代码不读它，回滚安全。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FILE = "close_sources.json"
KEEP = 2000
"""最多留这么多条（最旧的先丢），文件不会一直长。"""


def record(data_dir: str | Path, position_id: str, source: str, detail: str, ts: datetime) -> None:
    """记一条；写不进去只记日志（不影响平仓）。"""
    path = Path(data_dir) / FILE
    rows = read(data_dir)
    rows[position_id] = {"source": source, "detail": detail, "ts": ts.isoformat()}
    if len(rows) > KEEP:
        for pid in sorted(rows, key=lambda k: str(rows[k].get("ts", "")))[: len(rows) - KEEP]:
            rows.pop(pid, None)
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logger.exception("记平仓来源失败")
        with contextlib.suppress(OSError):
            tmp.unlink()


def read(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads((Path(data_dir) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
