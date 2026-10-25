"""比 tests/test_boundaries.py 更严的“防绕过”检查（审查员发现的漏洞）。

1. OrderIntent 只能由 jiaoyi.risk 产出。除了直接 ``OrderIntent(...)``，还要拦住：
   起别名导入、``OrderIntent.model_validate / model_construct / model_copy`` 等工厂方法、
   对 ``*intent*`` 变量调用 ``model_copy / model_construct``、继承 OrderIntent、
   用字符串 ``getattr(..., "OrderIntent")`` 取类。
2. JevResponse 的 ``ok=False`` 不能带答案（否则下游可能误用一份“失败但有答案”的回复）。

检查器本身也有自测（``test_checker_catches_*``）：先证明它能抓住每种绕过写法，
再用它扫描全仓库，避免“检查器写错、永远通过”。
"""

from __future__ import annotations

import ast

import pytest
from pydantic import ValidationError

from jiaoyi.contracts import JevResponse
from tests.conftest import ROOT, load_example

PKG = ROOT / "src" / "jiaoyi"
ALLOWED = (PKG / "risk", PKG / "contracts")
FACTORY_METHODS = {
    "model_validate",
    "model_validate_json",
    "model_validate_strings",
    "model_construct",
    "model_copy",
    "construct",
    "copy",
    "parse_obj",
    "parse_raw",
}
INSTANCE_METHODS = {"model_copy", "model_construct", "copy", "construct"}


def violations(source: str) -> list[str]:
    """返回源码里所有可能绕过“只有风控员产出 OrderIntent”的写法（行号 + 说明）。"""
    tree = ast.parse(source)
    aliases = {"OrderIntent"}
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name == "OrderIntent" and a.asname:
                    aliases.add(a.asname)
        elif (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
        ):
            aliases |= {t.id for t in node.targets if isinstance(t, ast.Name)}

    def is_intent_class(n: ast.expr) -> bool:
        if isinstance(n, ast.Name):
            return n.id in aliases
        return isinstance(n, ast.Attribute) and n.attr == "OrderIntent"

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if is_intent_class(fn):
                out.append(f"{node.lineno}: 直接构造 OrderIntent")
            elif isinstance(fn, ast.Attribute):
                if is_intent_class(fn.value) and fn.attr in FACTORY_METHODS:
                    out.append(f"{node.lineno}: OrderIntent.{fn.attr}(...)")
                elif (
                    fn.attr in INSTANCE_METHODS
                    and isinstance(fn.value, ast.Name)
                    and "intent" in fn.value.id.lower()
                ):
                    out.append(f"{node.lineno}: 对 {fn.value.id} 调 {fn.attr}")
            elif isinstance(fn, ast.Name) and fn.id == "getattr" and len(node.args) >= 2:
                arg = node.args[1]
                if isinstance(arg, ast.Constant) and arg.value == "OrderIntent":
                    out.append(f"{node.lineno}: getattr 取 OrderIntent")
        elif isinstance(node, ast.ClassDef):
            if any(is_intent_class(base) for base in node.bases):
                out.append(f"{node.lineno}: 继承 OrderIntent")
    return out


@pytest.mark.parametrize(
    "snippet",
    [
        "from jiaoyi.contracts import OrderIntent\nOrderIntent(a=1)",
        "from jiaoyi.contracts import OrderIntent as OI\nOI(a=1)",
        "from jiaoyi.contracts import OrderIntent\nX = OrderIntent\nX(a=1)",
        "import jiaoyi.contracts as c\nc.OrderIntent(a=1)",
        "from jiaoyi.contracts import OrderIntent\nOrderIntent.model_validate({})",
        "from jiaoyi.contracts import OrderIntent\nOrderIntent.model_validate_json('{}')",
        "from jiaoyi.contracts import OrderIntent\nOrderIntent.model_construct(a=1)",
        "from jiaoyi.contracts import OrderIntent as OI\nOI.model_validate({})",
        "def f(order_intent):\n    return order_intent.model_copy(update={'quantity': 1})",
        "def f(intent):\n    return intent.model_construct()",
        "import jiaoyi.contracts as c\nK = getattr(c, 'OrderIntent')",
        "from jiaoyi.contracts import OrderIntent\nclass Mine(OrderIntent):\n    pass",
    ],
)
def test_checker_catches_bypass(snippet: str) -> None:
    assert violations(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        "from jiaoyi.contracts import OrderIntent\ndef f(i: OrderIntent) -> None:\n    return None",
        "from jiaoyi.contracts import PositionRecord\nPositionRecord.model_validate({})",
        "def f(card):\n    return card.model_copy(update={})",
    ],
)
def test_checker_allows_normal_use(snippet: str) -> None:
    assert not violations(snippet), snippet


def test_only_risk_package_can_produce_order_intent() -> None:
    bad = []
    for f in sorted(PKG.rglob("*.py")):
        if any(f.is_relative_to(p) for p in ALLOWED):
            continue
        bad += [f"{f.relative_to(ROOT)}:{v}" for v in violations(f.read_text(encoding="utf-8"))]
    assert not bad, "只有 jiaoyi.risk 能产出 OrderIntent：\n" + "\n".join(bad)


def test_contracts_package_does_not_hand_out_a_factory() -> None:
    """契约包里也不能有“根据任意输入造 OrderIntent”的便利函数。"""
    bad = []
    for f in sorted((PKG / "contracts").glob("*.py")):
        for v in violations(f.read_text(encoding="utf-8")):
            if f.name != "orders.py":
                bad.append(f"{f.name}:{v}")
    assert not bad, "\n".join(bad)


# ---------------------------------------------------------------- JevResponse
def test_failed_jev_response_cannot_carry_answers() -> None:
    ok = load_example("jev_entry_response")
    with pytest.raises(ValidationError):
        JevResponse.model_validate({**ok, "ok": False, "error": "超时"})
