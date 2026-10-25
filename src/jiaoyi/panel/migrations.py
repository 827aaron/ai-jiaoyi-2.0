"""一次性配置迁移：新默认值要生效，先把 app.toml 里过去手写、会压过默认的那几行删掉。

Aaron 10-25：新的默认他只跑 ``make update``，不在网页上一项项点「恢复默认」。所以
``jiaoyi run`` 启动时（读配置之前）跑一遍：每条迁移只跑一次，跑过的 id 记在
``data/config_migrations.json``；删之前留备份，改完和面板保存同一套校验，每删一行记进
``data/config_changes.jsonl``（带 ``note``，面板「修改记录」能看到）。校验不过就不写、
也不记成跑过，原样启动（下次再试）。
"""

from __future__ import annotations

import json
import logging
import shutil
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from jiaoyi.panel.actions import (
    CHANGES_FILE,
    ActionError,
    ConfigPaths,
    _validate,
    set_toml_value,
)

log = logging.getLogger(__name__)

DONE_FILE = "config_migrations.json"


@dataclass(frozen=True)
class Migration:
    id: str
    note: str
    clears: tuple[tuple[str, str], ...]
    """app.toml 里要删的 (段, 键)；段 "" = 顶层。文件里没写的跳过。"""


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        id="2026-10-25-surge-vol-ratio",
        note="一次性迁移：删掉手写的量能突增倍数，回到默认 5（Aaron 10-25 同意）",
        # 只动这一个：其余手写项（surge_taker_ratio、breakout_vol_ratio 等）按约定留着
        clears=(("fast_loop", "surge_vol_ratio"),),
    ),
)


def _done(data_dir: Path) -> set[str]:
    try:
        raw = json.loads((data_dir / DONE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(x) for x in raw.get("done", [])} if isinstance(raw, dict) else set()


def _mark(data_dir: Path, done: set[str]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = data_dir / f".{DONE_FILE}.tmp"
    tmp.write_text(json.dumps({"done": sorted(done)}, ensure_ascii=False), encoding="utf-8")
    tmp.replace(data_dir / DONE_FILE)


def apply_pending(
    paths: ConfigPaths,
    data_dir: str | Path,
    now: datetime,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> list[dict[str, Any]]:
    """跑还没跑过的迁移，返回删掉的行（``{"migration", "section", "key", "old"}``）。"""
    data = Path(data_dir)
    done = _done(data)
    todo = [m for m in migrations if m.id not in done]
    if not todo or not paths.app.is_file():
        return []
    text = paths.app.read_text(encoding="utf-8")
    try:
        current = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return []  # 启动校验会报原因
    removed: list[dict[str, Any]] = []
    for m in todo:
        for section, key in m.clears:
            table = current.get(section, {}) if section else current
            if not isinstance(table, dict) or key not in table:
                continue
            text = set_toml_value(text, section or None, key, None)
            removed.append({"migration": m.id, "section": section, "key": key,
                            "old": table[key], "note": m.note})  # fmt: skip
    if removed:
        try:
            _validate("app", text, paths)
        except ActionError as e:
            log.warning("配置迁移没做：%s（原样启动，下次再试）", e)
            return []
        stamp = now.strftime("%Y%m%dT%H%M%S")
        shutil.copy2(paths.app, paths.app.with_name(f"{paths.app.name}.bak-{stamp}"))
        tmp = paths.app.with_name(f".{paths.app.name}.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(paths.app)
        data.mkdir(parents=True, exist_ok=True)
        with (data / CHANGES_FILE).open("a", encoding="utf-8") as f:
            for r in removed:
                row = {
                    "ts": now.isoformat(),
                    "file": paths.app.name,
                    **{k: r[k] for k in ("section", "key", "old")},
                    "new": None,
                    "note": r["note"],
                }
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
    _mark(data, done | {m.id for m in todo})
    return removed
