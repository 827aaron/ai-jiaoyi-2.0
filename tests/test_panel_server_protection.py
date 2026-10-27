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


def _seed(tmp_path: Path, sp: dict[str, Any] | None, **record: Any) -> None:
    pos = _position(**record)
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
    rejected = {**base, "algo_status": "REJECTED", "triggered_at": "2026-10-27T22:43:44.459000Z"}
    assert leg_state(rejected) == "cancelled"
    assert leg_state({**rejected, "child_state": "unknown"}) == "triggered"
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


def test_leg_trigger_source_old_and_new_legs_can_be_mixed(tmp_path: Path) -> None:
    # 改成 MARK_PRICE 后新挂的止损按标记价；旧账本没记的腿按当时的固定行为补。
    lifeboat = _leg("stop_loss", "lifeboat", 60_000.0)
    take_profit = _leg("take_profit", "fixed", 70_000.0)
    sp = _armed(
        legs=[
            lifeboat,
            _leg("stop_loss", "fixed", 66_500.0, working_type="MARK_PRICE"),
            take_profit,
        ]
    )
    legs = _legs(_view(tmp_path, sp)[1])
    assert legs["lifeboat"].working_type == "MARK_PRICE"
    assert legs["stop_loss"].working_type == "MARK_PRICE"
    assert legs["take_profit"].working_type == "CONTRACT_PRICE"
    sp["legs"][2] = {**take_profit, "working_type": "LAST_TRADE"}  # 读不懂的不猜
    assert _legs(_view(tmp_path, sp)[1])["take_profit"].working_type is None


@pytest.mark.parametrize(
    "newest",
    [
        {"state": "submit_pending", "algo_id": None, "algo_status": None},
        {"state": "rejected", "algo_id": None, "algo_status": None},
        {"state": "planned", "algo_id": None, "algo_status": None},
    ],
)
def test_replacement_in_flight_also_shows_the_still_live_previous_generation(
    tmp_path: Path, newest: dict[str, Any]
) -> None:
    sp = _armed(
        generation=2,
        legs=[
            _leg("stop_loss", "lifeboat", 60_000.0),
            _leg("stop_loss", "fixed", 66_500.0, generation=1),
            _leg("take_profit", "fixed", 70_000.0),
            _leg(
                "stop_loss",
                "trailing",
                67_200.0,
                generation=2,
                client_order_id="trailing-stop_loss-2",
                working_type="MARK_PRICE",
                **newest,
            ),
        ],
    )
    sl = _legs(_view(tmp_path, sp)[1])["stop_loss"]
    assert sl.state != "live" and sl.planned_price == 67_200.0 and sl.working_type == "MARK_PRICE"
    assert sl.previous is not None and sl.previous.state == "live"
    assert sl.previous.trigger_price == 66_500.0 and sl.previous.purpose == "fixed"
    assert sl.previous.working_type == "CONTRACT_PRICE"


def test_previous_generation_only_when_newest_is_not_live_and_old_one_is(tmp_path: Path) -> None:
    sp = _armed(
        legs=[
            _leg("stop_loss", "lifeboat", 60_000.0),
            _leg("stop_loss", "fixed", 66_500.0, generation=1),
            _leg("stop_loss", "trailing", 67_200.0, generation=2, client_order_id="t-2"),
            _leg("take_profit", "fixed", 70_000.0),
        ]
    )
    assert _legs(_view(tmp_path, sp)[1])["stop_loss"].previous is None  # 新一代已挂
    sp["legs"][1] = {**sp["legs"][1], "state": "cancelled", "algo_status": "CANCELED"}
    sp["legs"][2] = {**sp["legs"][2], "state": "submit_pending", "algo_status": None}
    assert _legs(_view(tmp_path, sp)[1])["stop_loss"].previous is None  # 旧一代也不在挂


def test_lifeboat_summary_counts_live_previous_generation(tmp_path: Path) -> None:
    sp = _armed(
        legs=[
            _leg("stop_loss", "lifeboat", 60_000.0, generation=1),
            _leg(
                "stop_loss",
                "lifeboat",
                60_100.0,
                generation=2,
                client_order_id="lifeboat-2",
                state="submit_pending",
            ),
            _leg("stop_loss", "fixed", 66_500.0),
            _leg("take_profit", "fixed", 70_000.0),
        ]
    )
    _, v = _view(tmp_path, sp)
    assert v.lifeboat_status == "active" and v.lifeboat_price == 60_000.0


def test_trailing_error_is_shown_separately_from_protection_error(tmp_path: Path) -> None:
    text = "TrailingBookUnavailable: 移动止损盘口过期、来自未来或本地时钟回退"
    _, v = _view(tmp_path, _armed(trailing_last_error=text))
    assert v.protection.trailing_error == text and v.protection.last_error is None
    _, v = _view(tmp_path, _armed())
    assert v.protection.trailing_error is None


def test_js_shows_trigger_source_and_live_previous_during_replacement() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    helpers = source[source.index("const TRAIL_TIP =") : source.index("const cardPos =")]
    sp = _armed(
        generation=2,
        trailing_last_error="TrailingBookUnavailable: 移动止损盘口读取失败",
        legs=[
            _leg("stop_loss", "lifeboat", 60_000.0),
            _leg("stop_loss", "fixed", 66_500.0, generation=1),
            _leg("take_profit", "fixed", 70_000.0, working_type="MARK_PRICE"),
            _leg(
                "stop_loss",
                "trailing",
                67_200.0,
                generation=2,
                client_order_id="t-2",
                state="submit_pending",
                algo_status=None,
                working_type="MARK_PRICE",
            ),
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
// 止损格主显示仍在挂的旧一代（成交价触发），新一代只作附注
assert(sl.startsWith('66500') && sl.includes('成交价') && sl.includes('固定止损·已挂'), sl);
assert(sl.includes('新一代') && sl.includes('67200') && sl.includes('标记价'), sl);
assert(sl.includes('提交中·未确认') && sl.includes('旧一代仍挂着'), sl);
assert(!sl.split('<div')[0].includes('warn'), sl);  // 旧单在挂，主显示不报警
assert(tp.includes('70000') && tp.includes('标记价'), tp);
assert(block.includes('旧一代仍挂') && block.includes('66500') && block.includes('67200'), block);
assert(block.includes('移动止损本轮没更新') && block.includes('移动止损盘口读取失败'), block);
assert(brief.includes('固定止损已挂') && brief.includes('新一代提交中'), brief);
assert(brief.includes('移动止损本轮没更新'), brief);
"""
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


_BASIS = {"trailing_entry_quote": 200.0, "trailing_entry_quantity": 2.0, "trailing_fees_quote": 0.2}
_TRAIL = {"trailing_activation_pct": 0.3, "trailing_stop_pct": 0.5}


@pytest.mark.parametrize(("side", "expected"), [("long", 100.4), ("short", 99.6)])
def test_payload_carries_trailing_activation_price(
    tmp_path: Path, side: str, expected: float
) -> None:
    from tests.test_panel import _client

    _seed(tmp_path, _armed(**_BASIS), side=side, **_TRAIL)
    page = build_positions(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, NOW, None, 120)
    assert page.positions[0].protection.trailing_activation_price == pytest.approx(expected)
    [v] = build_overview(
        PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, _cfg(tmp_path), NOW
    ).positions
    assert v.protection is not None
    assert v.protection.trailing_activation_price == pytest.approx(expected)
    with _client(tmp_path, "127.0.0.1") as client:
        body = client.get("/api/positions").json()
    assert body["positions"][0]["protection"]["trailing_activation_price"] == pytest.approx(
        expected
    )


def test_trailing_activation_price_needs_basis_and_trailing_config(tmp_path: Path) -> None:
    _seed(tmp_path, _armed(), **_TRAIL)
    page = build_positions(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, NOW, None, 120)
    assert page.positions[0].protection.trailing_activation_price is None
    _seed(tmp_path, _armed(**_BASIS))
    page = build_positions(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, NOW, None, 120)
    assert page.positions[0].protection.trailing_activation_price is None


def test_js_draws_trailing_lines_only_from_hosted_protection() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    helpers = source[source.index("const TRAIL_TIP =") : source.index("const cardPos =")]
    fixed = _leg("stop_loss", "fixed", 66_500.0)
    pending = _leg(
        "stop_loss", "trailing", 67_200.0, generation=2, state="submit_pending", algo_status=None
    )

    def pos(sp: dict[str, Any] | None) -> dict[str, Any]:
        view = parse_protection(sp, NOW, 120, long=True, activation_pct=0.3) if sp else None
        return {
            "symbol": "BTCUSDT",
            "side": "long",
            "trailing_activation_pct": 0.3,
            "trailing_stop_pct": 0.5,
            "protection": json.loads(view.model_dump_json()) if view else None,
        }

    cases = {
        "idle": pos(_armed(**_BASIS)),
        "nobasis": pos(_armed()),
        "live": pos(
            _armed(
                trailing_activated=True,
                trailing_stop_price=67_300.0,
                legs=[
                    {**fixed, "state": "superseded"},
                    _leg("stop_loss", "trailing", 67_200.0, generation=2),
                ],
            )
        ),
        "unconfirmed": pos(
            _armed(trailing_activated=True, trailing_stop_price=67_200.0, legs=[fixed, pending])
        ),
        "legacy": pos(None),
        "looser": pos(_armed(trailing_activated=True, trailing_stop_price=66_400.0)),
        "small": pos(_armed(trailing_activated=True, trailing_stop_price=66_510.0)),
        "tightened": pos(
            _armed(
                trailing_activated=True,
                trailing_stop_price=66_600.0,
                legs=[_leg("stop_loss", "tightened", 66_800.0)],
            )
        ),
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
        + "\nconst C = "
        + json.dumps(cases)
        + """;
assert.deepEqual(trailLines(C.idle), { start: 100.4 });
const idle = trailTag(C.idle);
assert(idle.includes('移动止损未激活') && idle.includes('到 100.4 开始跟，回撤距离 0.50%'), idle);
assert(trailNote(C.idle).includes('100.4') && trailLegend(C.idle).includes('启动线'));
assert.equal(trailLines(C.nobasis), null);
assert(trailNote(C.nobasis).includes('第一次看盘口') && trailLegend(C.nobasis) === '');
assert.deepEqual(trailLines(C.live), { stop: 67200, confirmed: true });
assert(trailTag(C.live).includes('交易所已挂') && trailLegend(C.live).includes('交易所已挂'));
assert.deepEqual(trailLines(C.unconfirmed), { stop: 67200, confirmed: false });
assert(trailTag(C.unconfirmed).includes('程序记录（未确认）'));
assert(trailLegend(C.unconfirmed).includes('未确认'));
assert.equal(trailLines(C.legacy), null);
assert.equal(trailLegend(C.legacy), '');
const looser = trailNote(C.looser);
assert(looser.includes('比在挂的固定止损 66500 松，未换挂'), looser);
assert(!looser.includes('还没确认'), looser);
assert(trailTag(C.looser).includes('未换挂') && trailLegend(C.looser).includes('未换挂'));
assert.deepEqual(trailLines(C.looser).held, { name: '固定止损', price: 66500, looser: true });
assert(trailNote(C.small).includes('紧但收紧不够一步'), trailNote(C.small));
assert(trailNote(C.tightened).includes('比在挂的收紧止损 66800 松，未换挂'));
"""
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
