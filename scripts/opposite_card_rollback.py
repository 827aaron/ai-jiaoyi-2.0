#!/usr/bin/env python3
"""回退 #314（反向作战卡收紧）代码之前清理执行层账本，让旧代码（``extra="forbid"``）能读。

只改 ``positions.json`` 里旧代码不认识的东西，其余原样：
- ``server_protection`` 里反向卡加的字段删掉（``tightened_*``、``opposite_*``、
  ``trailing_distance_pct``）；锚定过的仓之后移动止损按开仓指令带的距离继续走。
- 腿的用途 ``tightened`` 改成 ``fixed``（交易所上那张单不动，旧代码当固定止损核对）。
- 平仓原因 / 退出原因 ``tightened_stop`` 改成 ``stop_loss``。

必须先停主程序。默认只列出要改的地方；``--write`` 才改，改前把原文件存成
``positions.json.bak-314``。
不联网、不读密钥。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

FIELDS = (
    "tightened_stop_price",
    "tightened_reason",
    "tightened_at",
    "opposite_anchor_price",
    "opposite_card_key",
    "trailing_distance_pct",
    "opposite_anchor_retry_since",
)


def clean(data: dict[str, Any]) -> list[str]:
    """就地清理账本 dict，返回改了什么（一条一句）。"""
    changes: list[str] = []
    for pid, tracked in (data.get("positions") or {}).items():
        record = tracked.get("record") or {}
        if record.get("close_reason") == "tightened_stop":
            record["close_reason"] = "stop_loss"
            changes.append(f"{pid}: close_reason tightened_stop → stop_loss")
        protection = tracked.get("server_protection")
        if not isinstance(protection, dict):
            continue
        for name in FIELDS:
            if name in protection:
                value = protection.pop(name)
                changes.append(f"{pid}: 删 {name}={value!r}")
        if protection.get("exit_reason") == "tightened_stop":
            protection["exit_reason"] = "stop_loss"
            changes.append(f"{pid}: exit_reason tightened_stop → stop_loss")
        for leg in protection.get("legs") or []:
            if leg.get("purpose") == "tightened":
                leg["purpose"] = "fixed"
                changes.append(f"{pid}: 腿 {leg.get('client_order_id')} 用途 tightened → fixed")
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
        print("没有要清理的：账本没用过反向卡收紧，旧代码可以直接读")
        return 0
    if not args.write:
        print(f"共 {len(changes)} 处；加 --write 才改（先停主程序）")
        return 0
    shutil.copy2(args.ledger, args.ledger.with_name(args.ledger.name + ".bak-314"))
    tmp = args.ledger.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(args.ledger)
    print(f"已改 {len(changes)} 处，原文件存为 {args.ledger.name}.bak-314")
    return 0


if __name__ == "__main__":
    sys.exit(main())
