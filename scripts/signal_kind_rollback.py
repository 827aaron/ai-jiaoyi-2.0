#!/usr/bin/env python3
"""回退「按信号类型限制同时持仓」（max_open_by_kind）代码之前清理执行层账本，让旧代码（``extra="forbid"``）能读。

新代码只在还在管的持仓上写 ``record.signal_kind`` 和追单指令 ``chase.signal_kind``（为空或仓已结束时
不落盘）；这里把这两个键删掉，其余原样。删掉后回退的旧代码不按类型数仓，别的不受影响。

必须先停主程序。默认只列出要改的地方；``--write`` 才改，改前把原文件存成
``positions.json.bak-signal-kind``。
不联网、不读密钥。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

BACKUP_SUFFIX = ".bak-signal-kind"


def clean(data: dict[str, Any]) -> list[str]:
    """就地清理账本 dict，返回改了什么（一条一句）。"""
    changes: list[str] = []
    for pid, tracked in (data.get("positions") or {}).items():
        for part in ("record", "chase"):
            node = tracked.get(part)
            if isinstance(node, dict) and "signal_kind" in node:
                value = node.pop("signal_kind")
                changes.append(f"{pid}: 删 {part}.signal_kind={value!r}")
    return changes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ledger", type=Path, help="data/positions.json")
    parser.add_argument("--write", action="store_true", help="真的改（先备份）")
    args = parser.parse_args(argv)
    data = json.loads(args.ledger.read_text(encoding="utf-8"))
    changes = clean(data)
    for line in changes:
        print(line)
    if not changes:
        print("没有要清理的：账本里没有 signal_kind，旧代码可以直接读")
        return 0
    if not args.write:
        print(f"共 {len(changes)} 处；加 --write 才改（先停主程序）")
        return 0
    shutil.copy2(args.ledger, args.ledger.with_name(args.ledger.name + BACKUP_SUFFIX))
    tmp = args.ledger.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(args.ledger)
    print(f"已改 {len(changes)} 处，原文件存为 {args.ledger.name}{BACKUP_SUFFIX}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
