#!/usr/bin/env python3
"""回退交易所移动止损（``[fast_loop] trailing_native``）代码之前清理执行层账本，让旧代码
（``extra="forbid"``）能读。

旧代码不认识 ``trailing_stop`` 腿（``br-ts-``）和 ``native_*`` 字段。先把 ``trailing_native``
关掉（热生效），等每笔仓补挂好保命单、交易所移动止损被撤掉，再停主程序跑这个脚本：
- ``server_protection`` 里 ``native_*`` 字段删掉；
- 已经不在交易所上的交易所移动止损腿（已撤、被拒、从没提交，或这笔仓的保护已清理完）删掉；
- 还可能在交易所上的（在挂、提交 / 撤销未知、已触发但仓位没收尾）一条也不删，整个脚本不写：
  旧代码看不到它，它会一直挡住这个币开新仓。

默认只列出要改的地方；``--write`` 才改，改前把原文件存成 ``positions.json.bak-native``。
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
    "native_fallback",
    "native_pending_since",
    "native_extreme",
    "native_activated",
    "native_stop_price",
    "native_observed_at",
    "native_mainnet_extreme",
    "native_mainnet_activated",
)
_GONE = {"cancelled", "rejected", "superseded", "planned"}


def clean(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    """就地清理账本 dict，返回（改了什么，挡住回退的腿）。"""
    changes: list[str] = []
    blocked: list[str] = []
    for pid, tracked in (data.get("positions") or {}).items():
        protection = tracked.get("server_protection")
        if not isinstance(protection, dict):
            continue
        for name in FIELDS:
            if name in protection:
                value = protection.pop(name)
                changes.append(f"{pid}: 删 {name}={value!r}")
        done = protection.get("stage") == "done"
        kept = []
        for leg in protection.get("legs") or []:
            if leg.get("kind") != "trailing_stop":
                kept.append(leg)
                continue
            cid = leg.get("client_order_id")
            if done or leg.get("state") in _GONE:
                changes.append(f"{pid}: 删交易所移动止损腿 {cid}（{leg.get('state')}）")
            else:
                blocked.append(
                    f"{pid}: 交易所移动止损腿 {cid} 状态 {leg.get('state')}，可能还在交易所上"
                )
                kept.append(leg)
        protection["legs"] = kept
    return changes, blocked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ledger", type=Path, help="data/positions.json")
    parser.add_argument("--write", action="store_true", help="真的改（先备份）")
    args = parser.parse_args(argv)
    data = json.loads(args.ledger.read_text(encoding="utf-8"))
    changes, blocked = clean(data)
    for line in changes:
        print(line)
    if blocked:
        for line in blocked:
            print(line)
        print("有交易所移动止损腿还没撤清：先关 trailing_native 等它撤掉（或平完），再跑；没改账本")
        return 1
    if not changes:
        print("没有要清理的：账本没用过交易所移动止损，旧代码可以直接读")
        return 0
    if not args.write:
        print(f"共 {len(changes)} 处；加 --write 才改（先停主程序）")
        return 0
    shutil.copy2(args.ledger, args.ledger.with_name(args.ledger.name + ".bak-native"))
    tmp = args.ledger.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(args.ledger)
    print(f"已改 {len(changes)} 处，原文件存为 {args.ledger.name}.bak-native")
    return 0


if __name__ == "__main__":
    sys.exit(main())
