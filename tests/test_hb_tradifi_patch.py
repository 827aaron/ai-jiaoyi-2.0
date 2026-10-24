"""hummingbot 补丁：币安永续连接器认美股 / 金属永续（TRADIFI_PERPETUAL）。"""

from __future__ import annotations

import importlib.util
import pathlib
import types
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).parents[1]
OLD = '''from typing import Any, Dict


def is_exchange_information_valid(rule: Dict[str, Any]) -> bool:
    """Verifies if a trading pair is enabled to operate with based on its exchange information"""
    if rule["contractType"] == "PERPETUAL" and rule["status"] == "TRADING":
        valid = True
    else:
        valid = False
    return valid
'''


def _script() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(
        "patch_tradifi", ROOT / "docker/hb-demo/patch_tradifi.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _valid(source: str, rule: dict[str, Any]) -> bool:
    ns: dict[str, Any] = {}
    exec(compile(source, "web_utils", "exec"), ns)
    return bool(ns["is_exchange_information_valid"](rule))


def test_old_connector_accepts_tradifi_after_patch() -> None:
    tsla = {"contractType": "TRADIFI_PERPETUAL", "status": "TRADING"}
    btc = {"contractType": "PERPETUAL", "status": "TRADING"}
    quarter = {"contractType": "CURRENT_QUARTER", "status": "TRADING"}
    assert not _valid(OLD, tsla)
    new = _script().patch(OLD)
    assert _valid(new, tsla) and _valid(new, btc) and not _valid(new, quarter)
    assert not _valid(new, {"contractType": "TRADIFI_PERPETUAL", "status": "SETTLING"})
    assert _script().patch(new) == new  # 再打一次不变


def test_new_connector_left_alone() -> None:
    new = OLD.replace('== "PERPETUAL"', 'in ("PERPETUAL", "TRADIFI_PERPETUAL")')
    assert _script().patch(new) == new


def test_unknown_shape_fails_build() -> None:
    odd = OLD.replace('rule["contractType"] == "PERPETUAL"', 'rule["contractType"].startswith("P")')
    with pytest.raises(ValueError):
        _script().patch(odd)
