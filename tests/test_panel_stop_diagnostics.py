"""面板止损观测只读接线：原生事件与公开盘口分开，K 线不得伪造状态。"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jiaoyi.panel.hb import HbReader
from jiaoyi.panel.live_price import LivePrices
from jiaoyi.panel.market import KlineSource
from jiaoyi.panel.server import create_app
from tests.conftest import ROOT, load_example
from tests.test_panel import NOW, RISK, _cfg, _seed


def _runtime(executor_id: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "executor_id": executor_id,
        "runtime_epoch": "a" * 32,
        "executor_instance_epoch": "b" * 32,
        "source": "hb_runtime",
        "capability": "position_executor_stop_v1",
        "check_seq": 7,
        "observed_at": NOW.isoformat(),
        "check_at": NOW.isoformat(),
        "coverage_since": (NOW - timedelta(days=1)).isoformat(),
        "coverage_complete": True,
        "gap_detected": False,
        "price_basis": "mark_price",
        "decision_price": 0.4321,
        "net_pnl_observed": True,
        "stop_check_performed": True,
        "mark_price": 0.4321,
        "trailing_active": True,
        "actual_trailing_trigger_net_pnl_pct": 0.6,
        "checks": [
            {
                "ts": NOW.isoformat(),
                "stage": "check",
                "kind": "trailing_stop",
                "threshold_unit": "pct",
                "threshold": 0.6,
                "comparison": "<",
                "triggered": True,
                "check_seq": 7,
            }
        ],
        "events": [{"ts": NOW.isoformat(), "stage": "close_return", "kind": "trailing_stop"}],
        "private_extra": "must_not_escape",
    }


def test_positions_read_actual_runtime_and_keep_fixed_stop_without_klines(tmp_path: Path) -> None:
    _seed(tmp_path)
    ex = load_example("position_record")
    runtime = _runtime(ex["executor_id"])
    calls: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path == f"/executors/{ex['executor_id']}":
            return httpx.Response(
                200,
                json={
                    "id": ex["executor_id"],
                    "custom_info": {
                        "stop_diagnostics": runtime,
                        "private_extra": "must_not_escape",
                    },
                },
            )
        if req.url.path == "/fapi/v1/ticker/bookTicker":
            return httpx.Response(
                200, json={"symbol": ex["symbol"], "bidPrice": "0.4320", "askPrice": "0.4322"}
            )
        if req.url.path == "/fapi/v1/ticker/price":
            return httpx.Response(200, json=[])
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    hb = HbReader(
        "http://hb",
        "admin",
        "pw",
        account_name="a",
        connector_name="c",
        cache_s=0,
        transport=transport,
    )
    app = create_app(
        RISK,
        _cfg(tmp_path),
        hb,
        clock=lambda: NOW,
        lan_only=False,
        klines=KlineSource(transport=transport),
        live_prices=LivePrices("ws://127.0.0.1:9"),
    )
    with TestClient(app) as client:
        response = client.get("/api/positions")
        [p] = response.json()["positions"]
        assert p["trail"] is None
        assert p["stop_loss_price"] == ex["stop_loss_price"]
        assert p["stop_diagnostics"]["trailing_active"] is True
        assert [e["stage"] for e in p["stop_diagnostics"]["events"]] == ["check", "close_return"]
        assert p["stop_diagnostics"]["events"][0]["triggered"] is True
        assert p["book_quote"]["bid"] == 0.432 and p["book_quote"]["ask"] == 0.4322
        assert p["book_quote"]["source"] == "binance_rest_book_ticker"
        assert "must_not_escape" not in response.text
        runtime["check_at"] = (NOW - timedelta(seconds=20)).isoformat()
        [p] = client.get("/api/positions").json()["positions"]
        assert p["stop_diagnostics"]["status"] == "stale"
        assert p["stop_diagnostics"]["trailing_active"] is None
        assert p["stop_loss_price"] == ex["stop_loss_price"]
        runtime.clear()  # 旧镜像 / 无新字段仍可用，但不能据此说未启动。
        [p] = client.get("/api/positions").json()["positions"]
        assert p["stop_diagnostics"]["status"] == "unknown"
    assert ("GET", "/fapi/v1/klines") not in calls
    assert all(path not in ("/executors/", "/executors/search") for _, path in calls)


async def test_hb_diagnostics_identity_size_and_path_are_bounded() -> None:
    replies: list[httpx.Response] = [
        httpx.Response(200, json={"id": "other", "custom_info": {"stop_diagnostics": {"x": 1}}}),
        httpx.Response(200, content=b" " * (256 * 1024 + 1)),
        httpx.Response(404),
    ]
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        assert req.method == "GET"
        return replies.pop(0)

    hb = HbReader(
        "http://hb",
        "admin",
        "pw",
        account_name="a",
        connector_name="c",
        cache_s=0,
        transport=httpx.MockTransport(handler),
    )
    assert await hb.stop_diagnostics("../wrong") is None
    for _ in range(3):
        assert await hb.stop_diagnostics("id-1") is None
    assert calls == ["/executors/id-1"] * 3
    await hb.close()


def test_wld_old_candle_estimate_cannot_render_4916_or_638_as_locked_profit() -> None:
    """即使客户端还带旧估算，实际渲染也只取账本计划价与新鲜执行器观测。"""
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    helpers = source[source.index("const TRAIL_TIP =") : source.index("const cardPos =")]
    position = {
        "symbol": "WLDUSDT",
        "side": "long",
        "entry_price": 0.4278,
        "stop_loss_price": 0.4199,
        "quantity": 10000,
        "fee_bps": 0,
        "trailing_activation_pct": 0.7,
        "trailing_stop_pct": 3.3,
        "trail": {"active": True, "best_price": 0.5057, "stop_price": 0.4916},
        "stop_diagnostics": {"status": "unknown", "reason": "缺失执行器观测"},
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
"""
        + helpers
        + "\nconst p = "
        + json.dumps(position)
        + ";\n"
        + """
const shown = slCell(p) + trailTag(p) + trailNote(p);
assert(shown.includes('0.4199'));
assert(shown.includes('固定计划'));
assert(shown.includes('状态未知'));
for (const contaminated of ['0.4916', '638', '跟踪中', '估算']) {
  assert(!shown.includes(contaminated), shown);
}
p.stop_diagnostics.events = [{ts: '2026-10-22T06:30:00+00:00', stage: 'check',
  kind: 'trailing_stop', check_action: 'activation', comparison: '>',
  condition_met: true, close_requested: false}];
const activation = stopDiagnostics(p);
assert(activation.includes('移动止损启动条件检查'));
assert(activation.includes('条件满足'));
assert(activation.includes('本次未请求平仓流程'));
assert(!activation.includes('本次未触发'));
p.stop_diagnostics.events = [{ts: '2026-10-22T06:30:00+00:00', stage: 'close_request'}];
const closeFlow = stopDiagnostics(p);
assert(closeFlow.includes('平仓流程请求'));
assert(!closeFlow.includes('平仓下单申请'));
"""
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
