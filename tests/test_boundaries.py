"""结构性规则：AI 永远不直接下单。

1. AI 角色的包不得导入 jiaoyi.execution，也不得导入交易所客户端（ccxt、binance、hummingbot）。
2. ``OrderIntent`` / ``RiskVerdict`` 只能在 jiaoyi.risk（和契约本身）里创建；别处只能当类型注解用，
   不能调用、不能取 ``model_validate`` 等属性、不能起别名导入。改数量和跳过校验在契约里直接报错
   （见 tests/test_contracts.py）。
3. 币安密钥只有 jiaoyi.execution 能读（core/secrets.py 登记名字除外）。
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.conftest import ROOT

PKG = ROOT / "src" / "jiaoyi"
AI_PACKAGES = ["gate", "keeper", "patrol", "slow", "predictor", "sentinel"]
NON_EXECUTION_PACKAGES = [*AI_PACKAGES, "market", "risk", "alerting"]
EXCHANGE_CLIENTS = ("ccxt", "binance", "hummingbot")
SEALED = {"OrderIntent", "RiskVerdict"}
SEALED_ALLOWED = {PKG / "risk", PKG / "contracts"}
BINANCE_KEY_ALLOWED = {PKG / "execution", PKG / "core" / "secrets.py"}


def _py_files(path: Path) -> list[Path]:
    return sorted(path.rglob("*.py"))


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _imports(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _annotation_nodes(tree: ast.Module) -> set[int]:
    """所有出现在类型注解里的节点 id。"""
    roots: list[ast.expr] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.arg) and node.annotation is not None:
            roots.append(node.annotation)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.returns is not None:
            roots.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
    return {id(n) for r in roots for n in ast.walk(r)}


def _rel(f: Path) -> Path:
    return f.relative_to(ROOT)


def test_ai_packages_do_not_reach_exchange() -> None:
    bad = []
    for pkg in AI_PACKAGES:
        for f in _py_files(PKG / pkg):
            for m in _imports(_tree(f)):
                top = m.split(".")[0]
                if m == "jiaoyi.execution" or m.startswith("jiaoyi.execution."):
                    bad.append(f"{_rel(f)} 导入了 {m}")
                elif top.startswith(EXCHANGE_CLIENTS):
                    bad.append(f"{_rel(f)} 导入了交易所客户端 {m}")
    assert not bad, "\n".join(bad)


def _sealed_violations(tree: ast.Module, label: str) -> list[str]:
    bad: list[str] = []
    in_annotation = _annotation_nodes(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            bad += [
                f"{label}:{node.lineno} 起了别名 {a.asname}"
                for a in node.names
                if a.name.split(".")[-1] in SEALED and a.asname
            ]
        elif isinstance(node, ast.Name) and node.id in SEALED and id(node) not in in_annotation:
            bad.append(f"{label}:{node.lineno} 在注解以外使用了 {node.id}")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in SEALED
            and id(node) not in in_annotation
        ):
            bad.append(f"{label}:{node.lineno} 在注解以外使用了 {node.attr}")
    return bad


def test_order_intent_only_issued_by_risk_guard() -> None:
    bad = [
        v
        for f in _py_files(PKG)
        if not any(f.is_relative_to(p) for p in SEALED_ALLOWED)
        for v in _sealed_violations(_tree(f), str(_rel(f)))
    ]
    assert not bad, "只有 jiaoyi.risk 能签发 OrderIntent / RiskVerdict：\n" + "\n".join(bad)


def test_only_execution_reads_binance_keys() -> None:
    bad = [
        str(_rel(f))
        for f in _py_files(PKG)
        if not any(f == p or f.is_relative_to(p) for p in BINANCE_KEY_ALLOWED)
        and "BINANCE_API" in f.read_text(encoding="utf-8")
    ]
    assert not bad, "只有 jiaoyi.execution 能读币安密钥：" + ", ".join(bad)


def test_boundary_checker_catches_bypasses() -> None:
    """确认上面的检查真能抓到绕过写法，同时放过纯类型注解。"""
    src = (
        "from jiaoyi.contracts import OrderIntent as OI\n"
        "from jiaoyi.contracts import OrderIntent, RiskVerdict\n"
        "import jiaoyi.contracts as c\n"
        "def f(x: OrderIntent) -> RiskVerdict:\n"
        "    a = OrderIntent.model_validate({})\n"
        "    b = c.RiskVerdict(signal_id='s', approved=False, reasons=['r'])\n"
        "    return b\n"
    )
    bad = sorted(_sealed_violations(ast.parse(src), "x"))
    assert bad == [
        "x:1 起了别名 OI",
        "x:5 在注解以外使用了 OrderIntent",
        "x:6 在注解以外使用了 RiskVerdict",
    ]
