"""面板展示交易所托管保护单（positions.json 的 server_protection）：每条腿真实状态、数据年龄。"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.hb import HbSnapshot
from jiaoyi.panel.overview import build_overview
from jiaoyi.panel.pages import build_positions
from jiaoyi.panel.protection import leg_state, parse_protection
from tests.conftest import ROOT
from tests.test_panel import NOW, RISK, _cfg, _position

HB = HbSnapshot(ok=False)


def _leg(kind: str, purpose: str, price: float, **kw: Any) -> dict[str, Any]:
    leg: dict[str, Any] = {
        "kind": kind,
        "purpose": purpose,
        "generation": 1,
        "client_order_id": f"{purpose}-{kind}-1",
        "quantity": 0.5,
        "trigger_price": price,
        "state": "confirmed",
        "algo_id": "9001",
        "algo_status": "NEW",
        "child_state": "absent",
    }
    leg.update(kw)
    return leg


def _armed(**kw: Any) -> dict[str, Any]:
    sp: dict[str, Any] = {
        "version": 1,
        "account_name": "a",
        "connector_name": "c",
        "intended_quantity": 0.5,
        "stage": "armed",
        "ever_armed": True,
        "generation": 1,
        "legs": [
            _leg("stop_loss", "lifeboat", 60_000.0),
            _leg("stop_loss", "fixed", 66_500.0),
            _leg("take_profit", "fixed", 70_000.0),
        ],
        "observed_at": (NOW - timedelta(seconds=20)).isoformat(),
        "last_error": None,
    }
    sp.update(kw)
    return sp


def _seed(tmp_path: Path, sp: dict[str, Any] | None) -> None:
    pos = _position()
    if sp is not None:
        pos["server_protection"] = sp
    (tmp_path / "positions.json").write_text(json.dumps({"positions": {"pos-0001": pos}}))


def _view(tmp_path: Path, sp: dict[str, Any] | None, stale_s: int = 120) -> Any:
    _seed(tmp_path, sp)
    page = build_positions(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, NOW, None, stale_s)
    return page, page.positions[0]


def _legs(v: Any) -> dict[str, Any]:
    return {x.role: x for x in v.protection.legs}


def test_armed_shows_confirmed_legs_with_real_prices(tmp_path: Path) -> None:
    page, v = _view(tmp_path, _armed())
    legs = _legs(v)
    assert [legs[r].state for r in ("lifeboat", "stop_loss", "take_profit")] == ["live"] * 3
    assert legs["lifeboat"].trigger_price == 60_000.0 and legs["lifeboat"].planned_price is None
    assert legs["stop_loss"].trigger_price == 66_500.0
    assert (v.lifeboat_status, v.lifeboat_price) == ("active", 60_000.0)
    assert v.protection.stage == "armed" and not v.protection.stale
    assert v.protection.age_s == 20.0
    assert page.lifeboats_ok == 1


def test_old_path_position_is_unchanged(tmp_path: Path) -> None:
    page, v = _view(tmp_path, None)
    assert v.protection is None
    assert v.lifeboat_status == "active" and v.lifeboat_price is not None  # 旧记录原样
    assert page.lifeboats_ok == 1


def test_provisional_pending_legs_show_plan_not_real_price(tmp_path: Path) -> None:
    sp = _armed(
        stage="provisional",
        ever_armed=False,
        legs=[
            _leg("stop_loss", "lifeboat", 60_000.0),
            _leg(
                "stop_loss",
                "fixed",
                66_500.0,
                state="submit_pending",
                algo_id=None,
                algo_status=None,
                child_state="unknown",
            ),
            _leg("take_profit", "fixed", 70_000.0, state="planned", algo_id=None, algo_status=None),
        ],
    )
    _, v = _view(tmp_path, sp)
    legs = _legs(v)
    assert legs["lifeboat"].state == "live"
    for role in ("stop_loss", "take_profit"):
        assert legs[role].state == "pending"
        assert legs[role].trigger_price is None  # 计划价不当成真实挂单
    assert legs["stop_loss"].planned_price == 66_500.0
    assert "未确认" in legs["stop_loss"].detail


def test_exiting_triggered_leg_and_late_trigger_on_old_generation(tmp_path: Path) -> None:
    sp = _armed(
        stage="exiting",
        legs=[
            _leg("stop_loss", "lifeboat", 60_000.0),
            _leg(
                "stop_loss",
                "fixed",
                66_500.0,
                generation=1,
                algo_status="TRIGGERED",
                actual_order_id="77",
                actual_order_status="FILLED",
                filled_quantity=0.5,
                child_state="identified",
            ),
            _leg("stop_loss", "trailing", 67_000.0, generation=2, state="superseded"),
            _leg("take_profit", "fixed", 70_000.0, state="cancelled", algo_status="CANCELED"),
        ],
    )
    _, v = _view(tmp_path, sp)
    legs = _legs(v)
    assert legs["stop_loss"].state == "triggered"  # 旧一代的触发不被新一代盖掉
    assert legs["stop_loss"].trigger_price == 66_500.0
    assert legs["take_profit"].state == "cancelled"
    assert v.protection.stage == "exiting"


def test_unknown_child_is_not_reported_as_live(tmp_path: Path) -> None:
    sp = _armed(
        last_error="查询子单失败：timeout",
        legs=[
            _leg("stop_loss", "lifeboat", 60_000.0, child_state="unknown"),
            _leg("stop_loss", "fixed", 66_500.0),
            _leg("take_profit", "fixed", 70_000.0),
        ],
    )
    page, v = _view(tmp_path, sp)
    assert _legs(v)["lifeboat"].state == "unknown"
    assert v.lifeboat_status == "unknown" and page.lifeboats_ok == 0
    assert v.protection.last_error == "查询子单失败：timeout"


def test_stale_observation_marks_and_threshold_zero_disables(tmp_path: Path) -> None:
    old = _armed(observed_at=(NOW - timedelta(minutes=10)).isoformat())
    page, v = _view(tmp_path, old)
    assert v.protection.stale and v.lifeboat_status == "stale" and page.lifeboats_ok == 0
    _, v = _view(tmp_path, old, stale_s=0)
    assert not v.protection.stale and v.lifeboat_status == "active"
    _, v = _view(tmp_path, _armed(observed_at=None))
    assert v.protection.stale and v.protection.age_s is None


def test_trailing_leg_state_and_missing_legs(tmp_path: Path) -> None:
    sp = _armed(
        trailing_activated=True,
        trailing_stop_price=67_200.0,
        legs=[
            _leg("stop_loss", "lifeboat", 60_000.0),
            _leg("stop_loss", "fixed", 66_500.0, generation=1, state="superseded"),
            _leg("stop_loss", "trailing", 67_200.0, generation=2),
        ],
    )
    _, v = _view(tmp_path, sp)
    legs = _legs(v)
    assert legs["stop_loss"].purpose == "trailing" and legs["stop_loss"].trigger_price == 67_200.0
    assert legs["take_profit"].state == "none"
    assert v.protection.trailing_activated and v.protection.trailing_stop_price == 67_200.0


def test_leg_state_requires_all_three_facts_for_live() -> None:
    base = _leg("stop_loss", "fixed", 1.0)
    assert leg_state(base) == "live"
    assert leg_state({**base, "algo_status": None}) == "unknown"
    assert leg_state({**base, "state": "rejected"}) == "rejected"
    assert leg_state({**base, "algo_status": "CANCELED"}) == "cancelled"
    assert parse_protection("garbage", NOW, 120) is None


def test_overview_uses_same_projection_and_config_threshold(tmp_path: Path) -> None:
    _seed(tmp_path, _armed(observed_at=(NOW - timedelta(seconds=100)).isoformat()))
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    cfg = _cfg(tmp_path)
    [v] = build_overview(files, HB, RISK, cfg, NOW).positions
    assert v.protection is not None and not v.protection.stale
    strict = cfg.model_copy(update={"protection_stale_s": 60})
    [v] = build_overview(files, HB, RISK, strict, NOW).positions
    assert v.protection is not None and v.protection.stale


def test_positions_api_skips_hb_diagnostics_for_server_protection(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient  # noqa: F401

    from tests.test_panel import _client

    _seed(tmp_path, _armed())
    with _client(tmp_path, "127.0.0.1") as client:
        body = client.get("/api/positions").json()
    p = body["positions"][0]
    assert p["stop_diagnostics"] is None
    assert p["protection"]["legs"][1]["trigger_price"] == 66_500.0


def test_js_renders_leg_states_without_showing_plan_as_real() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    helpers = source[source.index("const TRAIL_TIP =") : source.index("const cardPos =")]
    sp = _armed(
        stage="provisional",
        last_error="algo 提交超时",
        observed_at=(NOW - timedelta(minutes=10)).isoformat(),
        legs=[
            _leg("stop_loss", "lifeboat", 60_000.0),
            _leg("stop_loss", "fixed", 66_123.0, state="submit_pending", algo_status=None),
            _leg("take_profit", "fixed", 70_000.0, algo_status="TRIGGERED", actual_order_id="1"),
        ],
    )
    view = parse_protection(sp, NOW, 120)
    assert view is not None
    position = {
        "symbol": "BTCUSDT",
        "side": "long",
        "entry_price": 65_000.0,
        "stop_loss_price": 66_999.0,
        "take_profit_price": 71_111.0,
        "quantity": 0.5,
        "fee_bps": 0,
        "trailing_stop_pct": None,
        "protection": json.loads(view.model_dump_json()),
    }
    script = (
        """
const assert = require('node:assert/strict');
const esc = value => String(value);
const price = value => value == null ? '—' : String(value);
const signed = (value, digits) => (value >= 0 ? '+' : '') + value.toFixed(digits);
const cls = () => '';
const TZ = 'Asia/Bangkok';
const qtyFmt = value => String(value);
const dur = ms => Math.round(ms / 60000) + ' 分';
"""
        + helpers
        + "\nconst p = "
        + json.dumps(position)
        + """;
const sl = slCell(p), tp = tpCell(p), block = protBlock(p), brief = protBrief(p);
assert(sl.includes('66123') && sl.includes('计划') && sl.includes('提交中·未确认'), sl);
assert(!sl.includes('66999'), sl);  // 账本里的计划价不冒充真实挂单
assert(tp.includes('70000') && tp.includes('已触发') && !tp.includes('71111'), tp);
assert(block.includes('algo 提交超时') && block.includes('已过期'), block);
assert(block.includes('临时保护') && block.includes('60000'), block);
assert(brief.includes('数据过期'), brief);
"""
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
