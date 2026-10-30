#!/usr/bin/env python3
"""回退锁利止损的代码之前清理执行层账本，让旧代码（``extra="forbid"``）能读。

只改 ``positions.json`` 里旧代码不认识的东西，其余原样：
- ``server_protection`` 里锁利加的字段删掉。
- 腿的用途 ``profit_lock`` 改成 ``fixed``（交易所上那张单不动，旧代码当固定止损核对）。
- 平仓原因 / 退出原因 ``profit_lock`` 改成 ``trailing_stop``（盈利侧的止损，旧代码这样归类）。

必须先停主程序。默认只列出要改的地方；``--write`` 才改，改前把原文件存成
``positions.json.bak-profit-lock``。不联网、不读密钥。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

FIELDS = (
    "profit_lock_reached_at",
    "profit_lock_price",
    "profit_lock_r",
    "profit_lock_source",
    "profit_lock_at",
    "profit_lock_done",
    "profit_lock_unknown_since",
)


def clean(data: dict[str, Any]) -> list[str]:
    """就地清理账本 dict，返回改了什么（一条一句）。"""
    changes: list[str] = []
    for pid, tracked in (data.get("positions") or {}).items():
        record = tracked.get("record") or {}
        if record.get("close_reason") == "profit_lock":
            record["close_reason"] = "trailing_stop"
            changes.append(f"{pid}: close_reason profit_lock → trailing_stop")
        protection = tracked.get("server_protection")
        if not isinstance(protection, dict):
            continue
        for name in FIELDS:
            if name in protection:
                value = protection.pop(name)
                changes.append(f"{pid}: 删 {name}={value!r}")
        if protection.get("exit_reason") == "profit_lock":
            protection["exit_reason"] = "trailing_stop"
            changes.append(f"{pid}: exit_reason profit_lock → trailing_stop")
        for leg in protection.get("legs") or []:
            if leg.get("purpose") == "profit_lock":
                leg["purpose"] = "fixed"
                changes.append(f"{pid}: 腿 {leg.get('client_order_id')} 用途 profit_lock → fixed")
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
        print("没有要清理的：账本没用过锁利止损，旧代码可以直接读")
        return 0
    if args.write:
        shutil.copy2(args.ledger, args.ledger.with_name(args.ledger.name + ".bak-profit-lock"))
        args.ledger.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        print(f"已写 {args.ledger}")
    else:
        print("只列出，没改；确认后加 --write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
