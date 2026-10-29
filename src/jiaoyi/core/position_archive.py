"""账本归档：``data/positions_archive/YYYY-MM-DD.jsonl``，每行一笔已结束的持仓（和
``positions.json`` 里 ``positions`` 的一项同样格式），按平仓时刻的 UTC 日期分文件。

执行层（``execution.registry``）写，面板、主网影子、报告读。放在执行层外面，面板读它不用导入
执行层。归档里的记录不再变：只读，账本里同一笔仓（归档写好、账本还没改写时崩溃会两边都有）
以账本为准。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

ARCHIVE_DIR = "positions_archive"

_cache: dict[Path, tuple[int, int, dict[str, dict[str, Any]]]] = {}


def archive_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / ARCHIVE_DIR


def _parse(text: str, path: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            pid = str(row["record"]["position_id"])
        except (ValueError, KeyError, TypeError):
            log.warning("%s 第 %d 行读不懂，跳过", path, n)
            continue
        out[pid] = row
    return out


def _read_file(path: Path) -> dict[str, dict[str, Any]]:
    """position_id → 原始记录；按 mtime 和大小缓存，返回值共享，调用方不能改。"""
    try:
        st = path.stat()
        hit = _cache.get(path)
        if hit is not None and hit[:2] == (st.st_mtime_ns, st.st_size):
            return hit[2]
        rows = _parse(path.read_text(encoding="utf-8"), path)
    except FileNotFoundError:
        _cache.pop(path, None)
        return {}
    except OSError:
        log.warning("%s 读不出", path, exc_info=True)
        return {}
    _cache[path] = (st.st_mtime_ns, st.st_size, rows)
    return rows


def read_archive(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    """全部归档的 position_id → 原始记录（``positions.json`` 里 ``positions`` 一项的格式）。"""
    out: dict[str, dict[str, Any]] = {}
    root = archive_dir(data_dir)
    if not root.is_dir():
        return out
    for path in sorted(root.glob("*.jsonl")):
        out.update(_read_file(path))
    return out


def with_archive(data_dir: str | Path, positions: dict[str, Any]) -> dict[str, Any]:
    """账本里的 ``positions`` 加上归档；同一笔两边都有时用账本的。"""
    archived = read_archive(data_dir)
    if not archived:
        return positions
    return {**archived, **positions}


def _fsync_dir(path: Path) -> None:
    with contextlib.suppress(OSError):
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write_archive(data_dir: str | Path, rows: Iterable[tuple[str, str, str]]) -> None:
    """(UTC 日期, position_id, 一行 JSON) 写进对应日期的文件，返回时已落到磁盘。

    整个文件重写（原内容 + 新行，同一笔已在文件里就不再加）后 fsync、改名：崩溃时文件要么是旧的
    要么是新的，不会半行；调用方确认返回后才能把记录从账本删掉，所以归档写好、账本没改时崩溃，
    下次再归档同一笔也不会重复。写不了就抛错，账本不动。"""
    by_day: dict[str, list[tuple[str, str]]] = {}
    for day, pid, line in rows:
        by_day.setdefault(day, []).append((pid, line))
    root = archive_dir(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    for day, items in sorted(by_day.items()):
        path = root / f"{day}.jsonl"
        try:
            old = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            old = ""
        have = set(_parse(old, path))
        new = [line for pid, line in items if pid not in have]
        if not new:
            continue
        text = old if not old or old.endswith("\n") else old + "\n"
        text += "".join(f"{line}\n" for line in new)
        tmp = path.with_name(f".{path.name}.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(root)
