from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jiaoyi import contracts as c
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.panel.files import DecisionTail, PanelFiles
from jiaoyi.panel.hb import HbReader, HbSnapshot
from jiaoyi.panel.overview import build_overview
from jiaoyi.panel.server import create_app, is_lan
from tests.conftest import ROOT, load_example

RISK = load_risk_config(ROOT / "config" / "risk.example.toml")
# 曼谷 13:30；曼谷的“今天”从 UTC 前一天 17:00 开始
NOW = datetime(2026, 10, 22, 6, 30, tzinfo=UTC)


def _cfg(tmp_path: Path) -> c.AppConfig:
    cfg = load_app_config(ROOT / "config" / "app.example.toml")
    return cfg.model_copy(update={"data_dir": str(tmp_path)})


def _rec(
    role: c.Role, outcome: c.Outcome, summary: str, ts: datetime, **kw: Any
) -> c.DecisionRecord:
    return c.DecisionRecord(
        record_id=f"{role.value}-{ts.timestamp()}-{summary[:4]}",
        ts=ts,
        role=role,
        outcome=outcome,
        summary=summary,
        **kw,
    )


def _position(**update: Any) -> dict[str, Any]:
    rec = load_example("position_record")
    rec.update(update)
    return {
        "record": rec,
        "lifeboat_stop_pct": 0.5,
        "notional_usdt": 1000.0,
        "stop_loss_pct": 0.25,
        "take_profit_pct": 0.5,
    }


def _seed(tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path)
    t = NOW - timedelta(minutes=10)
    card_symbol = load_example("battle_card")["symbols"][0]
    for r in [
        # 曼谷昨天的记录（UTC 当天 16:00 之前那天）不算今天
        _rec(c.Role.SENTINEL, c.Outcome.ACTION, "昨天的信号", NOW - timedelta(hours=14)),
        _rec(c.Role.SENTINEL, c.Outcome.ACTION, "BTC 放量突破", t, symbol="BTCUSDT"),
        _rec(c.Role.SENTINEL, c.Outcome.ACTION, "ETH 量能突增", t, symbol="ETHUSDT"),
        _rec(c.Role.GATE, c.Outcome.REJECT, "ETH 拍板放弃", t, symbol="ETHUSDT", latency_ms=300),
        _rec(
            c.Role.GATE,
            c.Outcome.PASS,
            "BTC 放行",
            t,
            symbol="BTCUSDT",
            latency_ms=100,
            cost_usd=0.01,
        ),
        _rec(c.Role.RISK_GUARD, c.Outcome.PASS, "BTC 保镖通过", t, symbol="BTCUSDT"),
        _rec(
            c.Role.KEEPER, c.Outcome.HOLD, "BTC 拿住", t, symbol="BTCUSDT", position_id="pos-0001"
        ),
        _rec(c.Role.RECONCILER, c.Outcome.ACTION, "ETH 保命止损缺失，已补挂", t, symbol="ETHUSDT"),
        _rec(
            c.Role.COMMANDER,
            c.Outcome.ACTION,
            "BTC 会议结论",
            t - timedelta(minutes=5),
            symbol=card_symbol["symbol"],
            output=card_symbol,
            cost_usd=0.2,
        ),
        _rec(
            c.Role.COMMANDER,
            c.Outcome.ACTION,
            "出作战卡 v3：1 个标的，1 个可做",
            t - timedelta(minutes=4),
            card_id="card-1",
            output={
                "regime": "震荡偏多",
                "version": 3,
                "expires_at": (NOW + timedelta(hours=1)).isoformat(),
                "symbols": [card_symbol["symbol"]],
            },
        ),
    ]:
        log.write(r)
    log.heartbeat(c.ModuleStatus(role=c.Role.GATE, health=c.Health.OK, ts=NOW))
    log.heartbeat(
        c.ModuleStatus(
            role=c.Role.SENTINEL, health=c.Health.DEGRADED, ts=NOW, last_error="行情重连中"
        )
    )
    log.heartbeat(
        c.ModuleStatus(role=c.Role.RECONCILER, health=c.Health.OK, ts=NOW - timedelta(minutes=10))
    )
    closed = _position(
        position_id="pos-0000",
        status="closed",
        closed_at=(NOW - timedelta(hours=1)).isoformat(),
        realized_pnl_usdt=12.5,
        fees_usdt=0.4,
        opened_at=(NOW - timedelta(hours=2)).isoformat(),
    )
    (tmp_path / "positions.json").write_text(
        json.dumps(
            {
                "positions": {"pos-0001": _position(), "pos-0000": closed},
                "day_start": {"day": "2026-10-22", "equity_usdt": 9000.0},
                "last_equity_usdt": 9950.0,
            }
        )
    )
    (tmp_path / "risk_state.json").write_text(
        json.dumps({"day": "2026-10-22", "start_equity": 10000.0, "tripped": False})
    )
    (tmp_path / "blocked_symbols.json").write_text(json.dumps({"XYZUSDT": "币安下架公告"}))


HB = HbSnapshot(
    ok=True,
    wallet_usdt=10_100.0,
    positions=[{"trading_pair": "BTC-USDT", "unrealized_pnl": "-20", "amount": 0.015}],
)


def test_overview_numbers(tmp_path: Path) -> None:
    _seed(tmp_path)
    ov = build_overview(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, _cfg(tmp_path), NOW)

    assert ov.running and ov.mode == "paper"
    k = ov.kpis
    assert k.equity_source == "hummingbot" and k.equity_usdt == pytest.approx(10_080.0)
    # 日初权益取熔断器记的 10000（和保镖同一口径），不取账本的 9000
    assert k.day_pnl_usdt == pytest.approx(80.0) and k.day_pnl_pct == pytest.approx(0.8)
    assert k.to_halt_usdt == pytest.approx(10_080 - 10_000 * (1 - RISK.daily_loss_halt_pct / 100))
    assert not k.halted
    assert k.exposure_x == pytest.approx(68044.0 * 0.015 / 10_080)

    t = ov.tiles
    assert t.signals == 2  # 昨天那条不算
    assert (t.opened, t.closed, t.win_rate, t.fees_usdt) == (2, 1, 1.0, 0.4)  # 两笔都是曼谷今天开的
    assert t.jev_cost_usd == pytest.approx(0.01) and t.llm_cost_usd == pytest.approx(0.2)
    assert t.avg_gate_latency_ms == pytest.approx(200.0)

    [p] = ov.positions
    assert p.symbol == "BTCUSDT" and p.unrealized_usdt == pytest.approx(-20.0)
    assert p.keeper == "拿住" and p.lifeboat_status == "active"

    kinds = [(f.role, f.kind) for f in ov.feed]
    assert (c.Role.RISK_GUARD, "go") in kinds and (c.Role.GATE, "skip") in kinds
    assert all(f.role is not c.Role.SENTINEL for f in ov.feed)  # 哨兵只计数
    assert ov.feed[0].ts >= ov.feed[-1].ts  # 最新在上

    assert ov.card is not None
    assert (ov.card.version, ov.card.regime, ov.card.valid) == (3, "震荡偏多", True)
    assert [s.symbol for s in ov.card.symbols] == ["BTCUSDT"]
    assert ov.card.leverage_max is not None

    assert [a.level for a in ov.alerts] == ["warn"]
    assert ov.blocked == {"XYZUSDT": "币安下架公告"}

    lamps = {lamp.key: lamp for lamp in ov.lamps}
    assert lamps["fast"].state == "slow" and lamps["fast"].detail == "行情重连中"
    assert lamps["recon"].state == "bad"  # 心跳过期
    assert lamps["slow"].state == "off"  # 军师团没心跳 = 未接入
    assert lamps["hb"].state == "ok"
    assert "main" not in lamps


def test_overview_without_main_loop_or_hb(tmp_path: Path) -> None:
    """空目录 + hummingbot 连不上：不报错，灯显示主程序没在运行，资金为空。"""
    ov = build_overview(
        PanelFiles(tmp_path, RISK.day_reset_tz),
        HbSnapshot(ok=False, error="连不上"),
        RISK,
        _cfg(tmp_path),
        NOW,
    )
    assert not ov.running and ov.lamps[0].key == "main"
    assert ov.kpis.equity_usdt is None and ov.kpis.day_pnl_usdt is None
    assert ov.card is None and ov.feed == [] and ov.positions == []


def test_halted_breaker_and_ledger_fallback(tmp_path: Path) -> None:
    _seed(tmp_path)
    (tmp_path / "risk_state.json").write_text(
        json.dumps({"day": "2026-10-22", "start_equity": 10000.0, "tripped": True})
    )
    ov = build_overview(
        PanelFiles(tmp_path, RISK.day_reset_tz), HbSnapshot(ok=False), RISK, _cfg(tmp_path), NOW
    )
    assert ov.kpis.halted and ov.kpis.to_halt_usdt == 0.0
    assert ov.kpis.equity_source == "ledger" and ov.kpis.equity_usdt == 9950.0
    assert ov.positions[0].unrealized_usdt is None


def test_corrupt_files_are_ignored(tmp_path: Path) -> None:
    for name in ("module_status.json", "positions.json", "risk_state.json", "blocked_symbols.json"):
        (tmp_path / name).write_text("{坏")
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    assert files.module_status() == {} and files.ledger().positions == []
    assert files.breaker() is None and files.blocked() == {}


def test_decision_tail_reads_only_whole_new_lines(tmp_path: Path) -> None:
    tail = DecisionTail(tmp_path)
    path = tmp_path / f"{NOW.date().isoformat()}.jsonl"
    first = _rec(c.Role.GATE, c.Outcome.PASS, "一", NOW).model_dump_json()
    second = _rec(c.Role.GATE, c.Outcome.REJECT, "二", NOW).model_dump_json()
    path.write_text(first + "\n" + "不是 JSON\n" + second[:20])  # 第二条还没写完
    assert [r.summary for r in tail.read_day(NOW.date())] == ["一"]
    with path.open("a") as f:
        f.write(second[20:] + "\n")
    assert [r.summary for r in tail.read_day(NOW.date())] == ["一", "二"]
    path.write_text(second + "\n")  # 文件被换掉（变短）就从头读
    assert [r.summary for r in tail.read_day(NOW.date())] == ["二"]


async def test_hb_reader_parses_and_caches() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/portfolio/state":
            return httpx.Response(
                200,
                json={
                    "master_account": {
                        "conn": [
                            {"token": "USDT", "units": 1000.0, "available_units": 800.0},
                            {"token": "BNB", "units": 1.0},
                        ]
                    }
                },
            )
        return httpx.Response(
            200, json={"data": [{"trading_pair": "ETH-USDT", "unrealized_pnl": 5}]}
        )

    hb = HbReader(
        "http://hb",
        "admin",
        "pw",
        account_name="master_account",
        connector_name="conn",
        transport=httpx.MockTransport(handler),
    )
    snap = await hb.snapshot()
    assert snap.ok and snap.equity_usdt == 1005.0 and snap.unrealized_of("ETHUSDT") == 5.0
    assert snap.available_usdt == 800.0
    assert snap.unrealized_of("BTCUSDT") is None
    await hb.snapshot()
    assert len(calls) == 2  # 第二次走缓存
    await hb.close()


async def test_hb_reader_errors_are_reported_not_raised() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    hb = HbReader(
        "http://hb",
        "admin",
        "pw",
        account_name="a",
        connector_name="c",
        transport=httpx.MockTransport(handler),
    )
    snap = await hb.snapshot()
    assert not snap.ok and "401" in (snap.error or "")
    no_pw = HbReader("http://hb", "admin", None, account_name="a", connector_name="c")
    assert "HUMMINGBOT_API_PASSWORD" in ((await no_pw.snapshot()).error or "")
    await hb.close()
    await no_pw.close()


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("127.0.0.1", True),
        ("192.168.1.20", True),
        ("10.0.0.5", True),
        ("::1", True),
        ("::ffff:192.168.1.2", True),
        ("8.8.8.8", False),
        ("testclient", False),
        (None, False),
    ],
)
def test_is_lan(host: str | None, ok: bool) -> None:
    assert is_lan(host) is ok


def _client(tmp_path: Path, host: str) -> TestClient:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    hb = HbReader(
        "http://hb",
        "admin",
        "pw",
        account_name="a",
        connector_name="c",
        transport=httpx.MockTransport(handler),
    )
    app = create_app(RISK, _cfg(tmp_path), hb, clock=lambda: NOW)
    return TestClient(app, client=(host, 5000), base_url="http://localhost:8090")


def test_server_serves_lan_and_blocks_public(tmp_path: Path) -> None:
    _seed(tmp_path)
    with _client(tmp_path, "192.168.1.20") as client:
        r = client.get("/api/overview")
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "paper" and body["hb_ok"] is False
        assert r.headers["cache-control"] == "no-store"
        page = client.get("/")
        assert page.status_code == 200 and "AI 交易台" in page.text
        assert client.get("/static/theme.css").status_code == 200
    with _client(tmp_path, "8.8.8.8") as client:
        assert client.get("/api/overview").status_code == 403
        assert client.get("/").status_code == 403


def test_overview_json_never_contains_secrets(tmp_path: Path) -> None:
    _seed(tmp_path)
    with _client(tmp_path, "127.0.0.1") as client:
        text = client.get("/api/overview").text
    assert '"pw"' not in text and "password" not in text.lower()


def test_panel_cannot_trade_by_construction() -> None:
    """面板不导入执行层 / 组装层 / 交易所客户端：从结构上保证它下不了单。

    唯一例外是 ``jiaoyi.execution.live_gate``（校验面板密码），它自己只依赖时钟。
    """
    import ast

    bad = []
    for f in sorted((ROOT / "src" / "jiaoyi" / "panel").rglob("*.py")):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            names = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            for m in names:
                if m == "jiaoyi.execution.live_gate":  # 只用它校验面板密码（本身不碰交易所）
                    continue
                if m.startswith(
                    ("jiaoyi.execution", "jiaoyi.app", "ccxt", "binance", "hummingbot")
                ):
                    bad.append(f"{f.name} 导入了 {m}")
    gate = ROOT / "src" / "jiaoyi" / "execution" / "live_gate.py"
    for node in ast.walk(ast.parse(gate.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("jiaoyi"):
            assert node.module == "jiaoyi.execution.clock", node.module
    assert not bad, bad


def test_positions_page_groups_and_trail(tmp_path: Path) -> None:
    from jiaoyi.panel.pages import build_positions

    _seed(tmp_path)
    page = build_positions(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, NOW)
    [p] = page.positions
    assert page.unrealized_usdt == pytest.approx(-20.0) and page.lifeboats_ok == 1
    major = next(g for g in page.groups if g.category == "major")
    assert major.exposure_x == pytest.approx(68044.0 * 0.015 / 10_080)
    assert major.cap_x == RISK.category_exposure_x[c.Category.MAJOR]
    assert [t.text for t in page.trails[p.position_id]] == ["BTC 拿住"]


def test_team_page_and_signal_chain(tmp_path: Path) -> None:
    from jiaoyi.panel.pages import build_signal, build_team

    _seed(tmp_path)
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    team = build_team(files, NOW, c.Role.GATE)
    by_role = {r.role: r for r in team.roles}
    assert by_role[c.Role.GATE].state == "ok"
    assert by_role[c.Role.RECONCILER].state == "bad"  # 心跳过期
    assert by_role[c.Role.PREDICTOR].state == "off"
    assert [r.text for r in team.records] == ["BTC 放行", "ETH 拍板放弃"]  # 最新在前

    log = JsonlDecisionLog(tmp_path)
    for role, outcome in ((c.Role.SENTINEL, c.Outcome.ACTION), (c.Role.GATE, c.Outcome.PASS)):
        log.write(_rec(role, outcome, f"{role.value} 链", NOW, signal_id="sig-1"))
    chain = build_signal(files, NOW, "sig-1")
    assert [s.role for s in chain] == [c.Role.SENTINEL, c.Role.GATE]
    assert build_signal(files, NOW, "nope") == []


async def test_kline_source_validates_and_parses() -> None:
    from jiaoyi.panel.market import KlineError, KlineSource

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["symbol"] == "BTCUSDT"
        return httpx.Response(200, json=[[1_700_000_000_000, "1", "2", "0.5", "1.5", "9"]])

    src = KlineSource(transport=httpx.MockTransport(handler))
    bars = await src.klines("BTCUSDT", "1m")
    assert bars == [{"time": 1_700_000_000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}]
    with pytest.raises(KlineError):
        await src.klines("BTC/../x", "1m")
    await src.close()


def test_page_endpoints(tmp_path: Path) -> None:
    _seed(tmp_path)
    with _client(tmp_path, "10.0.0.2") as client:
        assert client.get("/api/positions").json()["lifeboats_ok"] == 1
        assert client.get("/api/team?role=gate").json()["selected"] == "gate"
        assert client.get("/api/team?role=boss").status_code == 422
        assert client.get("/api/signal/none").json() == []
        assert client.get("/api/klines?symbol=BTCUSDT&interval=7m").status_code == 422
        assert client.get("/static/lib/lightweight-charts-4.2.0.js").status_code == 200


def test_history_page(tmp_path: Path) -> None:
    from jiaoyi.panel.pages import build_history

    _seed(tmp_path)
    log = JsonlDecisionLog(tmp_path)
    sig = load_example("position_record")["signal_id"]
    log.write(
        _rec(c.Role.SENTINEL, c.Outcome.ACTION, "信号", NOW - timedelta(hours=2), signal_id=sig,
             output={"kind": "volume_breakout"})
    )  # fmt: skip
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    page = build_history(files, RISK, NOW, "today")
    assert page.trades == 1 and page.net_pnl_usdt == pytest.approx(12.5)
    assert page.win_rate == 1.0 and page.max_drawdown_usdt == 0.0
    assert [k.kind for k in page.by_kind] == ["volume_breakout"]
    assert [r.text for r in page.rejected] == ["ETH 拍板放弃"]
    assert len(page.curve) == 1


def test_history_reads_older_days_with_role_filter(tmp_path: Path) -> None:
    from jiaoyi.panel.pages import build_history

    log = JsonlDecisionLog(tmp_path)
    old = NOW - timedelta(days=5)
    log.write(_rec(c.Role.GATE, c.Outcome.REJECT, "五天前放弃", old))
    log.write(_rec(c.Role.KEEPER, c.Outcome.HOLD, "五天前拿住", old))
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    assert [r.text for r in build_history(files, RISK, NOW, "7d").rejected] == ["五天前放弃"]
    assert build_history(files, RISK, NOW, "today").rejected == []


def test_funds_page(tmp_path: Path) -> None:
    from jiaoyi.panel.pages import build_funds

    _seed(tmp_path)
    page = build_funds(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, NOW)
    assert page.equity_usdt == pytest.approx(10_080.0)
    assert page.realized_today_usdt == pytest.approx(12.5) and page.fees_today_usdt == 0.4
    # 10080 = 10000 + 12.5 + (-20) + 其他
    assert page.other_today_usdt == pytest.approx(87.5)
    assert page.halt_line_usdt == pytest.approx(10_000 * (1 - RISK.daily_loss_halt_pct / 100))
    gate = next(r for r in page.costs if r.role is c.Role.GATE)
    assert gate.today_usd == pytest.approx(0.01)
    assert page.cost_today_usd == pytest.approx(0.21)


def test_history_and_funds_endpoints(tmp_path: Path) -> None:
    _seed(tmp_path)
    with _client(tmp_path, "127.0.0.1") as client:
        assert client.get("/api/history?range=7d").json()["range"] == "7d"
        assert client.get("/api/history?range=1y").status_code == 422
        assert client.get("/api/funds").status_code == 200


def test_pool_page(tmp_path: Path) -> None:
    from jiaoyi.panel.pages import build_pool

    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    assert build_pool(files).entries == []
    (tmp_path / "symbol_pool.json").write_text(json.dumps(load_example("symbol_pool")))
    page = build_pool(files)
    assert page.entries[0].in_pool and page.in_pool + page.rejected == len(page.entries)
    assert all(a.score >= b.score for a, b in zip(page.entries, page.entries[1:], strict=False)
               if a.in_pool and b.in_pool)  # fmt: skip


def test_settings_page_never_has_key_values(tmp_path: Path) -> None:
    from jiaoyi.panel.pages import build_settings

    (tmp_path / "live_gate.json").write_text(
        json.dumps({"salt_hex": "00", "hash_hex": "deadbeefcafe", "armed_at": None})
    )
    page = build_settings(
        PanelFiles(tmp_path, RISK.day_reset_tz), RISK, _cfg(tmp_path), {"JEV_API_KEY": True}
    )
    assert page.password_set and not page.live_armed
    lev = next(i for i in page.risk if i.key == "max_leverage")
    assert lev.value == RISK.max_leverage and lev.bounds == "1–125"
    assert page.risk_cautions == []  # 保守默认没有提示
    hot = RISK.model_copy(update={"max_leverage": 10})
    hot_page = build_settings(PanelFiles(tmp_path, RISK.day_reset_tz), hot, _cfg(tmp_path), {})
    assert hot_page.risk_cautions == ["max_leverage = 10（推荐 5）"]
    assert page.category_caps["major"] == RISK.category_exposure_x[c.Category.MAJOR]
    assert any(i.key == "min_p_take" for i in page.fast_loop)
    assert page.keys == {"JEV_API_KEY": True}
    assert "deadbeefcafe" not in page.model_dump_json()  # 哈希不出面板


def test_pool_and_settings_endpoints(tmp_path: Path) -> None:
    with _client(tmp_path, "127.0.0.1") as client:
        assert client.get("/api/pool").json()["entries"] == []
        body = client.get("/api/settings").json()
        assert body["mode"] == "paper" and body["keys"] == {}
