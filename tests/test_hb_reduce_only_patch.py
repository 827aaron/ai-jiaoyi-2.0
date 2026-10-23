"""hummingbot 补丁：执行器平仓单永远只减仓（保命止损先成交后不会反向开仓）。"""

from __future__ import annotations

import importlib.util
import pathlib
import types

import pytest

ROOT = pathlib.Path(__file__).parents[1]
SNIPPET = (ROOT / "tests/fixtures/hb_position_executor_snippet.py.txt").read_text(encoding="utf-8")


def _script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "patch_reduce_only", ROOT / "docker/hb-demo/patch_reduce_only.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _close_action(source: str, mode: str) -> str:
    ns: dict[str, object] = {}
    exec(compile(source, "position_executor", "exec"), ns)
    pm = ns["PositionMode"]
    conn = types.SimpleNamespace(position_mode=getattr(pm, mode))
    ex = ns["PositionExecutor"](conn)  # type: ignore[operator]
    return str(ex.close_position_action.value)


def test_upstream_oneway_close_is_not_reduce_only() -> None:
    # 打补丁前：单向持仓的平仓单是 OPEN（不带 reduce-only），这就是反手仓的来源
    assert _close_action(SNIPPET, "ONEWAY") == "OPEN"


def test_patch_makes_every_close_reduce_only_and_is_idempotent() -> None:
    mod = _script()
    patched = mod.patch(SNIPPET)
    assert mod.MARK in patched and patched != SNIPPET
    assert _close_action(patched, "ONEWAY") == "CLOSE"
    assert _close_action(patched, "HEDGE") == "CLOSE"
    assert mod.patch(patched) == patched  # 第二次不再改
    # 其它属性原样保留
    assert "def trade_pnl_pct(self) -> Decimal:" in patched


def test_patch_refuses_unknown_upstream() -> None:
    mod = _script()
    with pytest.raises(ValueError):
        mod.patch(SNIPPET.replace("def close_position_action(self)", "def close_action(self)"))
    # 老版本：没有这个属性、平仓直接写 CLOSE，本来就是只减仓，原样通过
    old = "x = dict(position_action=PositionAction.CLOSE)\n"
    assert mod.patch(old) == old
