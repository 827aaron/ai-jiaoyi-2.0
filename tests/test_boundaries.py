"""结构性规则：AI 永远不直接下单。

1. AI 角色的包（拍板人、持仓管家、巡逻兵、慢环、预测员）不得导入 jiaoyi.execution。
2. 只有保镖（jiaoyi.risk）可以构造 OrderIntent。
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.conftest import ROOT

PKG = ROOT / "src" / "jiaoyi"
AI_PACKAGES = ["gate", "keeper", "patrol", "slow", "predictor", "sentinel", "market"]
INTENT_ALLOWED = {PKG / "risk", PKG / "contracts"}


def _py_files(path: Path) -> list[Path]:
    return sorted(path.rglob("*.py"))


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_ai_packages_do_not_import_execution() -> None:
    bad = [
        f"{f.relative_to(ROOT)} 导入了 {m}"
        for pkg in AI_PACKAGES
        for f in _py_files(PKG / pkg)
        for m in _imports(f)
        if m == "jiaoyi.execution" or m.startswith("jiaoyi.execution.")
    ]
    assert not bad, "\n".join(bad)


def test_only_risk_guard_builds_order_intent() -> None:
    bad: list[str] = []
    for f in _py_files(PKG):
        if any(f.is_relative_to(p) for p in INTENT_ALLOWED):
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if name == "OrderIntent":
                    bad.append(f"{f.relative_to(ROOT)}:{node.lineno}")
    assert not bad, "只有 jiaoyi.risk 能构造 OrderIntent：" + ", ".join(bad)
