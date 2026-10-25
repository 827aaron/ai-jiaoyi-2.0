from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
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
        _rec(c.Role.RISK_GUARD, c.Outcome.PASS, "BTC 风控员通过", t, symbol="BTCUSDT"),
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
    # 日初权益取熔断器记的 10000（和风控员同一口径），不取账本的 9000
    assert k.day_pnl_usdt == pytest.approx(80.0) and k.day_pnl_pct == pytest.approx(0.8)
    # 今日盈亏拆成 已平（今天平的净盈亏）+ 浮动（还没平的浮盈）+ 其它
    assert k.day_closed_usdt == pytest.approx(12.5) and k.day_floating_usdt == pytest.approx(-20.0)
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
    assert ov.card.symbols[0].key_levels[0].model_dump() == {"price": 68000.0, "kind": "resistance"}
    assert ov.card.leverage_max is not None
    # 摘要行写风控员真会用的：作战卡不约束（默认）时杠杆 = 硬上限；L 档每笔风险 = 总资产 × 风险%
    assert ov.card.leverage_used == int(RISK.max_leverage)
    assert ov.card.risk_per_trade_usdt == pytest.approx(10_080 * RISK.risk_per_trade_pct / 100)
    assert ov.card.size_fractions["L"] == 1.0 and 0 < ov.card.size_fractions["S"] <= 1  # 仓位档折扣

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


def _client(tmp_path: Path, host: str, live: Any = None) -> TestClient:
    from jiaoyi.panel.live_price import LivePrices
    from jiaoyi.panel.market import KlineSource

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/fapi/v1/klines":  # 面板 K 线：一根旧的 1 分钟线
            return httpx.Response(200, json=[[1_699_999_940_000, "1", "1", "1", "1", "9"]])
        return httpx.Response(503)

    hb = HbReader(
        "http://hb",
        "admin",
        "pw",
        account_name="a",
        connector_name="c",
        transport=httpx.MockTransport(handler),
    )
    app = create_app(
        RISK,
        _cfg(tmp_path),
        hb,
        clock=lambda: NOW,
        klines=KlineSource(transport=httpx.MockTransport(handler)),
        live_prices=live or LivePrices("ws://127.0.0.1:9"),  # 测试里不连币安
    )
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
        # app.js / app.css 带版本号，改了文件浏览器不用缓存里的旧版
        assert re.search(r'src="/static/app\.js\?v=[0-9a-f]+"', page.text)
        assert re.search(r'href="/static/app\.css\?v=[0-9a-f]+"', page.text)
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
    assert major.notional_usdt == pytest.approx(68044.0 * 0.015)
    assert major.cap_usdt == pytest.approx(major.cap_x * 10_080)
    assert [t.text for t in page.trails[p.position_id]] == ["BTC 拿住"]
    assert p.price_src == "mark"  # 没给最新价：按 hummingbot 浮盈倒推

    # 给了币安最新价（推送，和 K 线图同源）：现价、浮盈都按它算，不再用 hummingbot 的标记价浮盈
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    [q] = build_positions(files, HB, RISK, NOW, {"BTCUSDT": (68_100.0, "push")}).positions
    sign = 1 if q.side == "long" else -1
    assert (q.mark_price, q.price_src) == (68_100.0, "push")
    assert q.unrealized_usdt == pytest.approx(sign * 0.015 * (68_100.0 - 68_044.0))


def test_team_page_and_signal_chain(tmp_path: Path) -> None:
    from jiaoyi.panel.pages import build_signal, build_team

    _seed(tmp_path)
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    team = build_team(files, NOW, c.Role.GATE)
    by_role = {r.role: r for r in team.roles}
    assert by_role[c.Role.GATE].state == "ok"
    assert by_role[c.Role.RECONCILER].state == "bad"  # 心跳过期
    assert by_role[c.Role.PREDICTOR].state == "slow"  # 主程序在跑、它还没报到
    assert by_role[c.Role.PREDICTOR].status == "等待首次运行"
    assert by_role[c.Role.RECONCILER].status.startswith("无心跳超过")
    assert by_role[c.Role.GATE].status.startswith("正常（上次 ")
    (tmp_path / "runtime.json").write_text(json.dumps({"stub_roles": ["predictor"]}))
    stubbed = {r.role: r for r in build_team(files, NOW, c.Role.GATE).roles}
    assert (stubbed[c.Role.PREDICTOR].state, stubbed[c.Role.PREDICTOR].status) == (
        "off", "未启用（这次启动没接入）",
    )  # fmt: skip
    assert [r.text for r in team.records] == ["BTC 放行", "ETH 拍板放弃"]  # 最新在前

    log = JsonlDecisionLog(tmp_path)
    for role, outcome in ((c.Role.SENTINEL, c.Outcome.ACTION), (c.Role.GATE, c.Outcome.PASS)):
        log.write(_rec(role, outcome, f"{role.value} 链", NOW, signal_id="sig-1"))
    chain = build_signal(files, NOW, "sig-1")
    assert [s.role for s in chain] == [c.Role.SENTINEL, c.Role.GATE]
    assert build_signal(files, NOW, "nope") == []


def test_rejection_items_carry_signal_id_and_symbol(tmp_path: Path) -> None:
    """风控员拒绝记录上的两个按钮靠 signal_id + symbol 发命令：团队页和信号链都要带上。"""
    from jiaoyi.panel.pages import build_signal, build_team

    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    JsonlDecisionLog(tmp_path).write(
        _rec(
            c.Role.RISK_GUARD, c.Outcome.REJECT, "BTC 风控员拒绝", NOW,
            signal_id="sig-9", symbol="BTCUSDT",
            output={"approved": False, "reasons": ["价差太大"]},
        )
    )  # fmt: skip
    [from_team] = build_team(files, NOW, c.Role.RISK_GUARD).records
    [from_chain] = build_signal(files, NOW, "sig-9")
    for item in (from_team, from_chain):
        assert (item.signal_id, item.symbol) == ("sig-9", "BTCUSDT")


def test_lists_cross_the_bangkok_day_rollover(tmp_path: Path) -> None:
    """曼谷 0 点翻日后，各列表（团队页日志、信号流、信号链）仍给最新的记录，不是一片空白。"""
    from jiaoyi.panel.pages import build_signal, build_team

    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    now = datetime(2026, 10, 25, 17, 5, tzinfo=UTC)  # 曼谷 10-26 00:05
    log = JsonlDecisionLog(tmp_path)
    for ago_min, text in ((600, "10 小时前"), (30, "半小时前"), (1, "刚刚")):
        ts = now - timedelta(minutes=ago_min)
        log.write(_rec(c.Role.GATE, c.Outcome.PASS, text, ts, signal_id=f"s{ago_min}"))
    texts = [r.text for r in build_team(files, now, c.Role.GATE).records]
    assert texts == ["刚刚", "半小时前", "10 小时前"]  # 最新在前，跨了曼谷 0 点
    assert [r.text for r in build_signal(files, now, "s30")] == ["半小时前"]
    feed = build_overview(files, HB, RISK, _cfg(tmp_path), now).feed
    assert [e.text for e in feed][:2] == ["刚刚", "半小时前"]


def test_team_log_looks_back_into_older_files_when_recent_days_are_thin(tmp_path: Path) -> None:
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    log = JsonlDecisionLog(tmp_path)
    for days_ago in (5, 3):
        ts = now - timedelta(days=days_ago)
        log.write(_rec(c.Role.KEEPER, c.Outcome.HOLD, f"{days_ago} 天前", ts))
    got, more = files.role_records(c.Role.KEEPER, 5, now)
    assert [r.summary for r in got] == ["3 天前", "5 天前"] and more is False
    got, more = files.role_records(c.Role.KEEPER, 1, now)
    assert [r.summary for r in got] == ["3 天前"] and more is True


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


async def test_kline_source_prices_shared_and_backs_off_on_429() -> None:
    import asyncio

    from jiaoyi.panel.market import KlineError, KlineSource

    calls: list[str] = []
    status = {"code": 200}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if status["code"] != 200:
            return httpx.Response(status["code"], headers={"Retry-After": "30"})
        return httpx.Response(200, json=[{"symbol": "XLMUSDT", "price": "0.17317"}])

    src = KlineSource(transport=httpx.MockTransport(handler))
    a, b = await asyncio.gather(src.last_prices(), src.last_prices())  # 同时进来：只拉一次
    assert a == b == {"XLMUSDT": 0.17317} and calls == ["/fapi/v1/ticker/price"]

    status["code"] = 429
    with pytest.raises(KlineError):
        await src.klines("XLMUSDT", "1m")
    n = len(calls)
    for _ in range(3):  # Retry-After 到期前一个请求都不发
        with pytest.raises(KlineError, match="限流"):
            await src.klines("BTCUSDT", "1m")
    assert len(calls) == n
    await src.close()


def test_live_prices_push_and_merge_into_chart() -> None:
    from jiaoyi.panel.live_price import LivePrices, merge_live

    live = LivePrices()
    k = {
        "t": 1_700_000_060_000,
        "s": "XLMUSDT",
        "o": "0.1733",
        "h": "0.1734",
        "l": "0.1731",
        "c": "0.17317",
    }
    live.on_message(json.dumps({"stream": "xlmusdt@kline_1m", "data": {"e": "kline", "k": k}}))
    live.on_message("not json")  # 坏消息忽略
    assert live.price("XLMUSDT") == 0.17317 and live.price("BTCUSDT") is None
    bar = live.bar("XLMUSDT")
    old = [{"time": 1_700_000_000, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0}]
    assert merge_live(old, bar, "1m")[-1] == bar and len(merge_live(old, bar, "1m")) == 2
    assert merge_live(old, bar, "5m") == [{**old[0], "close": 0.17317, "low": 0.17317}]
    assert old[0]["close"] == 1.0  # 不动缓存里的
    stale = LivePrices(fresh_s=0.0)
    stale.on_message(json.dumps({"data": {"k": k}}))
    assert stale.price("XLMUSDT") is None  # 断流太久的不算


def test_positions_and_chart_use_pushed_price(tmp_path: Path) -> None:
    from jiaoyi.panel.live_price import LivePrices

    _seed(tmp_path)
    live = LivePrices("ws://127.0.0.1:9")
    k = {
        "t": 1_700_000_000_000,
        "s": "BTCUSDT",
        "o": "68000",
        "h": "68120",
        "l": "67990",
        "c": "68100",
    }
    live.on_message(json.dumps({"data": {"k": k}}))
    with _client(tmp_path, "10.0.0.2", live) as client:
        [p] = client.get("/api/positions").json()["positions"]
        assert (p["mark_price"], p["price_src"]) == (68_100.0, "push")
        bars = client.get("/api/klines?symbol=BTCUSDT&interval=1m").json()
        assert bars[-1]["close"] == 68_100.0 and len(bars) == 2  # 图最后一根 = 表里现价


def test_page_endpoints(tmp_path: Path) -> None:
    _seed(tmp_path)
    with _client(tmp_path, "10.0.0.2") as client:
        assert client.get("/api/positions").json()["lifeboats_ok"] == 1
        team = client.get("/api/team?role=gate").json()
        assert team["selected"] == "gate" and team["predictor_mode"] == "on_demand"
        assert team["gate_thresholds"]["max_p_toxic"] > 0 and team["snapshot"] is None
        snap = {"ts": "2026-10-22T06:00:00+00:00", "symbols": {"BTCUSDT": {"price": 1.0}}}
        (tmp_path / "market_snapshot.json").write_text(json.dumps(snap), encoding="utf-8")
        book = client.get("/api/team?role=book_reader").json()
        assert book["snapshot"] == snap
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
    [row] = page.rows
    ex = load_example("position_record")
    assert row.stop_loss_price == ex["stop_loss_price"]
    assert row.category == ex["category"]
    assert row.take_profit_price == ex["take_profit_price"]
    assert row.exit_price is None and not row.pnl_missing  # 有盈亏数，只是平仓价没回填
    [st] = page.by_symbol  # 按币种统计
    assert (st.symbol, st.trades, st.top_kind) == (ex["symbol"], 1, "volume_breakout")
    assert st.longs + st.shorts == 1 and st.win_rate == 1.0
    assert st.pnl_usdt == pytest.approx(12.5) and st.avg_hold_s > 0
    assert (st.wins, st.losses) == (1, 0) and st.avg_r is not None and st.avg_r > 0
    assert {(g.by, g.key) for g in st.splits} == {("side", ex["side"]), ("kind", "volume_breakout")}
    assert build_history(files, RISK, NOW, "all").by_symbol == page.by_symbol


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

    # 没有 income_daily.json：交易盈亏 / 手续费按账本，资金费不知道
    assert page.pnl_source == "ledger"
    today = page.pnl[0]
    assert (today.key, today.funding_usdt) == ("today", None)
    assert today.trading_usdt == pytest.approx(12.9) and today.fees_usdt == pytest.approx(-0.4)
    assert today.net_usdt == pytest.approx(12.5 - 0.21)


def test_funds_pnl_from_exchange_income(tmp_path: Path) -> None:
    """有币安收入流水：今天 / 近 7 天 / 累计都按它算，净盈亏扣掉 AI 费；坏的天跳过。"""
    from jiaoyi.panel.pages import build_funds

    _seed(tmp_path)
    days = {
        "2026-10-22": {"realized_pnl": 30.0, "commission": -2.0, "funding_fee": -0.5},
        "2026-10-18": {"realized_pnl": -10.0, "commission": -1.0, "funding_fee": 0.2},
        "2026-10-01": {"realized_pnl": 100.0, "commission": -5.0, "funding_fee": 1.0},
        "bad": {"realized_pnl": 1.0},
        "2026-10-20": {"realized_pnl": "x"},
    }
    (tmp_path / "income_daily.json").write_text(json.dumps({"tz": "Asia/Bangkok", "days": days}))
    page = build_funds(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, NOW)
    assert page.pnl_source == "exchange" and str(page.pnl_since) == "2026-10-01"
    today, week, total = page.pnl
    assert (today.trading_usdt, today.fees_usdt, today.funding_usdt) == (30.0, -2.0, -0.5)
    assert today.net_usdt == pytest.approx(27.5 - 0.21)
    assert week.trading_usdt == pytest.approx(20.0) and week.funding_usdt == pytest.approx(-0.3)
    assert total.trading_usdt == pytest.approx(120.0) and total.fees_usdt == pytest.approx(-8.0)


def test_funds_total_counts_ai_cost_before_first_trade(tmp_path: Path) -> None:
    """“累计”起点 = 第一笔交易和第一笔 AI 费里更早的那天，交易前开会的钱也算进去。"""
    from jiaoyi.panel.pages import build_funds

    _seed(tmp_path)
    log = JsonlDecisionLog(tmp_path)
    early = c.DecisionRecord(
        record_id="early", ts=NOW - timedelta(days=20), role=c.Role.COMMANDER,
        outcome=c.Outcome.ACTION, summary="早期开会", cost_usd=1.5,
    )  # fmt: skip
    log.write(early)
    page = build_funds(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, NOW)
    total = page.pnl[2]
    assert str(page.pnl_since) == str((NOW - timedelta(days=20)).date())
    assert total.ai_usd == pytest.approx(0.21 + 1.5)


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


def test_pool_page_shows_your_overrides(tmp_path: Path) -> None:
    """选币页的标签和按钮按 pool_overrides.json（立刻生效），不等候选池文件更新。"""
    from jiaoyi.core.pool_overrides import PoolOverrides, write_pool_overrides
    from jiaoyi.panel.pages import build_pool

    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    page = build_pool(files)
    assert (page.pinned, page.blacklist, page.manual, page.overrides_error) == ([], [], [], False)
    ov = PoolOverrides().apply("pin_symbol", "SOLUSDT").apply("add_symbol", "SUIUSDT")
    write_pool_overrides(tmp_path, ov.apply("blacklist_symbol", "DOGEUSDT"))
    page = build_pool(files)
    assert (page.pinned, page.blacklist, page.manual) == (["SOLUSDT"], ["DOGEUSDT"], ["SUIUSDT"])
    (tmp_path / "pool_overrides.json").write_text("{broken")
    assert build_pool(files).overrides_error  # 读不出：页面不让改


def test_pool_page_categories_layered(tmp_path: Path) -> None:
    """类别开关 / 上限：选币页设的 > slow.toml [screener.categories] > 默认。"""
    from jiaoyi.core.pool_overrides import PoolOverrides, write_pool_overrides
    from jiaoyi.panel.pages import build_pool
    from jiaoyi.slow.settings import CategoryRule, ScreenerSection

    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    (tmp_path / "symbol_pool.json").write_text(json.dumps(load_example("symbol_pool")))
    page = build_pool(files)
    cats = {x.category: x for x in page.categories}
    assert list(cats) == [x.value for x in c.Category]
    assert all(x.enabled and x.cap == 30 and x.enabled_from == x.cap_from == "default"
               for x in cats.values())  # fmt: skip
    assert sum(x.total for x in cats.values()) == len(page.entries)
    assert sum(x.in_pool for x in cats.values()) == page.in_pool
    scr = ScreenerSection(
        default_category_cap=20,
        categories={
            c.Category.US_TECH: CategoryRule(enabled=False),
            c.Category.ALT: CategoryRule(cap=10),
        },
    )
    ov = PoolOverrides().set_category(c.Category.US_TECH, True)
    write_pool_overrides(tmp_path, ov.set_category_cap(c.Category.MAJOR, 3))
    cats = {x.category: x for x in build_pool(files, scr).categories}
    assert (cats["us_tech"].enabled, cats["us_tech"].enabled_from) == (True, "pool")
    assert (cats["us_tech"].cap, cats["us_tech"].cap_from) == (20, "default")
    assert (cats["alt"].cap, cats["alt"].cap_from, cats["alt"].enabled_from) == (10, "slow", "slow")
    assert (cats["major"].cap, cats["major"].cap_from) == (3, "pool")
    # 「恢复默认」后的值：开关按 slow.toml（关），上限按每类默认（20）
    assert (cats["us_tech"].enabled_base, cats["major"].cap_base, cats["alt"].cap_base) == (
        False,
        20,
        10,
    )
    write_pool_overrides(tmp_path, PoolOverrides())
    cats = {x.category: x for x in build_pool(files, scr).categories}
    assert (cats["us_tech"].enabled, cats["us_tech"].enabled_from) == (False, "slow")


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
    assert lev.value == RISK.max_leverage and lev.bounds == "≥1" and lev.recommended == 5
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
        pool = client.get("/api/pool").json()
        assert pool["entries"] == [] and len(pool["categories"]) == len(c.Category)
        body = client.get("/api/settings").json()
        assert body["mode"] == "paper" and body["keys"] == {}


def test_history_hides_test_trades_by_default(tmp_path: Path) -> None:
    """没有哨兵 / 拍板 / 风控员记录的平仓（冒烟测试、手动单）默认不算进记录和统计。"""
    from jiaoyi.panel.pages import build_history

    _seed(tmp_path)  # 种子里的那笔平仓没有任何决策记录 → 测试单
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    page = build_history(files, RISK, NOW, "today")
    assert page.trades == 0 and page.rows == [] and page.curve == []
    assert page.net_pnl_usdt == 0.0 and page.test_trades == 1
    shown = build_history(files, RISK, NOW, "today", include_tests=True)
    assert shown.trades == 1 and shown.rows[0].test
    assert [k.kind for k in shown.by_kind] == ["test"]
    with _client(tmp_path, "10.0.0.2") as client:
        assert client.get("/api/history").json()["rows"] == []
        assert client.get("/api/history?tests=true").json()["rows"][0]["test"] is True


def test_meeting_process_read_tolerantly_and_served(tmp_path: Path) -> None:
    """开会过程：正在开的排前面、太久没更新算中断、坏步骤跳过、长文截断、币名过白名单。"""
    from jiaoyi.panel.meetings import MAX_TEXT, list_meetings, read_meeting
    from jiaoyi.panel.pages import build_team

    _seed(tmp_path)
    folder = tmp_path / "meeting_live"
    folder.mkdir()
    t = lambda m: (NOW - timedelta(minutes=m)).isoformat()  # noqa: E731
    live = {
        "symbol": "BTCUSDT", "status": "running", "stage": "debate",
        "started_at": t(4), "updated_at": t(1),
        "steps": [
            {"stage": "analysts", "who": "market", "ts": t(3), "text": "x" * (MAX_TEXT + 50)},
            {"stage": "debate", "who": "bull", "ts": t(1), "text": "放量突破，看多"},
            {"stage": "debate", "who": "bear"},  # 没有 text：跳过
        ],
    }  # fmt: skip
    done = {"status": "done", "stage": "decision", "started_at": t(90), "updated_at": t(80),
            "finished_at": t(80), "steps": [],
            "result": {"bias": "short", "reason": "资金费率过热"}}  # fmt: skip
    (folder / "BTCUSDT.json").write_text(json.dumps(live))
    (folder / "ETHUSDT.json").write_text(json.dumps(done))
    (folder / "SOLUSDT.json").write_text(json.dumps(live | {"updated_at": t(30)}))
    (folder / "BAD.json").write_text("{broken")

    m = read_meeting(tmp_path, "BTCUSDT", NOW)
    assert m and m.status == "running" and m.step_count == 2
    assert m.steps[0].text.endswith("后面省略）") and len(m.steps[0].text) < MAX_TEXT + 20
    assert read_meeting(tmp_path, "../BTCUSDT", NOW) is None
    summary = list_meetings(tmp_path, NOW)
    assert [(s.symbol, s.status) for s in summary] == [
        ("BTCUSDT", "running"), ("SOLUSDT", "stale"), ("ETHUSDT", "done"),
    ]  # fmt: skip
    assert summary[2].bias == "short" and summary[2].reason == "资金费率过热"
    # 总盘会：同格式，文件名 _MARKET，固定排第一（哪怕已经开完）
    (folder / "_MARKET.json").write_text(json.dumps(done | {"symbol": "_MARKET"}))
    assert [s.symbol for s in list_meetings(tmp_path, NOW)][:2] == ["_MARKET", "BTCUSDT"]
    assert read_meeting(tmp_path, "_MARKET", NOW) is not None
    assert read_meeting(tmp_path, "_OTHER", NOW) is None

    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    assert build_team(files, NOW, c.Role.GATE).meetings == []
    assert len(build_team(files, NOW, c.Role.COMMANDER).meetings) == 4
    # 流水线按数据流排；正在开会的军师团高亮“开会中”，总盘会排第一但已开完不算
    team = build_team(files, NOW, c.Role.COMMANDER)
    assert [r.role for r in team.roles][:3] == [
        c.Role.SCREENER,
        c.Role.COMMANDER,
        c.Role.BOOK_READER,
    ]
    doing = {r.role: r.doing for r in team.roles}
    assert doing[c.Role.COMMANDER] == "开会中：BTC"
    # 总盘会单独一个节点（流程图：选币 → 总盘会 → 军师团 / 盘口员）
    assert team.market is not None and team.market.symbol == "_MARKET"
    assert team.market_progress == "done"
    prog = {r.role: r.progress for r in team.roles}
    assert prog[c.Role.COMMANDER] == "doing"

    with _client(tmp_path, "192.168.1.20") as client:
        assert client.get("/api/meeting/BTCUSDT").json()["steps"][1]["who"] == "bull"
        assert client.get("/api/meeting/XRPUSDT").status_code == 404
        assert client.get("/api/meeting/_MARKET").json()["status"] == "done"
        assert client.get("/api/meeting/btc..").status_code == 404


def test_team_progress_follows_this_round_not_heartbeat() -> None:
    """流程图的点 = 本轮走到哪：心跳正常不再等于绿点（Aaron：哨兵什么都没干就绿了）。"""
    from jiaoyi.panel.pages import _latest_chain, _progress, _role_card, _round_start, _RunInfo

    run = _RunInfo(True, None, frozenset(), "Asia/Bangkok")

    def rec(role: c.Role, sig: str | None, mins: int, **kw: object) -> c.DecisionRecord:
        ts = NOW - timedelta(minutes=mins)
        return c.DecisionRecord(
            record_id=f"{role}{sig}{mins}", ts=ts, role=role, outcome=c.Outcome.PASS, summary="x",
            signal_id=sig, **kw,
        )  # fmt: skip

    card = rec(c.Role.COMMANDER, None, 20, card_id="v20")
    card = card.model_copy(update={"outcome": c.Outcome.ACTION})
    old = [
        rec(c.Role.SENTINEL, "s0", 40),
        rec(c.Role.GATE, "s0", 39),
        rec(c.Role.RISK_GUARD, "s0", 38),
    ]
    # 作战卡之后还没有新信号：上一轮的链不算，② 全部等待
    assert _latest_chain([*old, card], _round_start([*old, card])) is None
    records = [*old, card, rec(c.Role.SENTINEL, "s2", 3), rec(c.Role.GATE, "s2", 2)]
    chain = _latest_chain(records, _round_start(records))
    assert chain == {c.Role.SENTINEL, c.Role.GATE}
    assert _latest_chain([]) is None

    def prog(role: c.Role, holding: int = 0, ch: set[c.Role] | None = chain, acted: bool = False,
             **kw: object) -> str:  # fmt: skip
        st = c.ModuleStatus(role=role, health=c.Health.OK, ts=NOW, headline="")
        card = _role_card(role, st, NOW, run).model_copy(update=kw)
        return _progress(card, st, ch, holding, acted, NOW, run.tz)

    assert prog(c.Role.GATE) == "done" and prog(c.Role.RISK_GUARD) == "wait"
    assert prog(c.Role.GATE, ch=None) == "wait" and prog(c.Role.EXECUTOR) == "wait"
    assert prog(c.Role.RISK_GUARD, doing="刚刚：算仓位") == "doing"
    assert prog(c.Role.GATE, state="bad", doing="刚刚：x") == "err"
    # 心跳正常但没活 = 等待，不是绿点
    for role in (c.Role.KEEPER, c.Role.PATROL, c.Role.RECONCILER, c.Role.LIFEBOAT):
        assert prog(role) == "wait"
    assert prog(c.Role.SENTINEL) == "wait" and prog(c.Role.ALERTING) == "wait"
    assert prog(c.Role.ALERTING, acted=True) == "done"
    assert prog(c.Role.SCREENER) == "done" and prog(c.Role.COMMANDER) == "done"
    assert prog(c.Role.SCREENER, state="off") == "wait"


def test_team_dots_after_card_with_no_signal(tmp_path: Path) -> None:
    """出卡后没信号：哨兵 / 盘口员在盯（蓝），拍板人 / 风控员 / 执行器等待，没持仓第三层等待。"""
    from jiaoyi.panel.pages import build_team

    status = {
        r.value: c.ModuleStatus(role=r, health=c.Health.OK, ts=NOW, headline="") for r in c.Role
    }
    dump = {k: v.model_dump(mode="json") for k, v in status.items()}
    (tmp_path / "module_status.json").write_text(json.dumps(dump))
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    team = build_team(files, NOW, None)
    prog = {r.role: r.progress for r in team.roles}
    doing = {r.role: r.doing for r in team.roles}
    assert prog[c.Role.SENTINEL] == "doing" and doing[c.Role.SENTINEL] == "盯着信号"
    assert prog[c.Role.BOOK_READER] == "doing"
    for role in (c.Role.GATE, c.Role.RISK_GUARD, c.Role.EXECUTOR, c.Role.KEEPER,
                 c.Role.PATROL, c.Role.RECONCILER, c.Role.LIFEBOAT):  # fmt: skip
        assert prog[role] == "wait", role
    assert prog[c.Role.SCREENER] == "done"


def test_role_state_reads_schedule_fields_when_present(tmp_path: Path) -> None:
    """选币等定时任务报 last_run_at / next_run_at / symbol_count / error 时，状态里写出来。"""
    from jiaoyi.panel.pages import _role_card, _RunInfo, role_state

    class Sched(c.ModuleStatus):  # 慢环把这几个字段加进契约前，先用子类模拟
        last_run_at: datetime | None = None
        next_run_at: datetime | None = None
        symbol_count: int | None = None
        error: str | None = None

    run = _RunInfo(True, None, frozenset(), "Asia/Bangkok")  # NOW = 06:20 UTC = 13:20 曼谷
    base = {"role": c.Role.SCREENER, "health": c.Health.OK, "ts": NOW}
    ok = Sched(**base, last_run_at=NOW - timedelta(minutes=5), next_run_at=NOW + timedelta(hours=1),
               symbol_count=40)  # fmt: skip
    assert role_state(c.Role.SCREENER, ok, NOW, run)[1].startswith("正常（上次 ")
    assert "40 个币" in role_state(c.Role.SCREENER, ok, NOW, run)[1]
    first = Sched(**base, next_run_at=NOW + timedelta(minutes=10))
    assert role_state(c.Role.SCREENER, first, NOW, run)[1].startswith("等待首次运行 ")
    # 模块自己说了卡在哪一步：写明那一步，不笼统说“等待首次运行”
    warming = c.ModuleStatus(role=c.Role.PREDICTOR, health=c.Health.STARTING, ts=NOW,
                             headline="等待历史 K 线（启动时从币安回填）")  # fmt: skip
    assert role_state(c.Role.PREDICTOR, warming, NOW, run) == (
        "slow",
        "准备中：等待历史 K 线（启动时从币安回填）",
    )
    bad = Sched(**base, error="币安 451")
    assert role_state(c.Role.SCREENER, bad, NOW, run) == ("bad", "失败：币安 451")
    # 状态里已经有上次 / 币数 / 下次：摘要不再重复显示；没报这些字段的模块照常显示摘要
    assert _role_card(c.Role.SCREENER, ok.model_copy(update={"headline": "选出 40 个币"}), NOW,
                      run).headline == ""  # fmt: skip
    plain = c.ModuleStatus(**base, headline="池子 40 个")
    assert _role_card(c.Role.SCREENER, plain, NOW, run).headline == "池子 40 个"


def test_card_rows_without_conclusion_say_why(tmp_path: Path) -> None:
    """作战卡里没有结论的币不显示问号：按会议实况写开会中 / 失败 / 中断 / 没开会。"""
    from jiaoyi.panel.overview import CardRow, CardView, _with_pending

    folder = tmp_path / "meeting_live"
    folder.mkdir()
    t = lambda m: (NOW - timedelta(minutes=m)).isoformat()  # noqa: E731
    base = {"started_at": t(8), "updated_at": t(1), "steps": []}
    (folder / "XRPUSDT.json").write_text(
        json.dumps(base | {"symbol": "XRPUSDT", "status": "running", "stage": "debate"})
    )
    (folder / "ADAUSDT.json").write_text(
        json.dumps(base | {"symbol": "ADAUSDT", "status": "failed", "error": "超时"})
    )
    (folder / "DOTUSDT.json").write_text(
        json.dumps(base | {"symbol": "DOTUSDT", "status": "running", "updated_at": t(30)})
    )
    syms = ["XRPUSDT", "ADAUSDT", "DOTUSDT", "LTCUSDT"]
    rows = [CardRow(symbol=s, bias="unknown", category="other") for s in syms]
    card = CardView.model_construct(created_at=NOW - timedelta(minutes=5), symbols=rows)
    out = _with_pending(card, tmp_path, NOW, "Asia/Bangkok")
    assert out is not None
    got = {r.symbol: (r.pending_label, r.pending or "") for r in out.symbols}
    assert got["XRPUSDT"][0] == "开会中" and "第 2 / 5 步 · 多空辩论" in got["XRPUSDT"][1]
    assert got["ADAUSDT"] == ("开会失败", "开会失败：超时；这张卡里它没有结论，不会开仓")
    assert got["DOTUSDT"][0] == "会中断" and "15 分钟" in got["DOTUSDT"][1]
    assert got["LTCUSDT"][0] == "没开会"


def test_card_row_survives_usage_in_record(tmp_path: Path) -> None:
    """会议记录里夹着 token 用量时，观望的币也要显示成观望，不能变成「没出结论」。"""
    from jiaoyi.panel.overview import _card

    cs = {
        "symbol": "WLDUSDT", "category": "alt", "bias": "no_trade", "key_levels": [],
        "allowed_signals": [], "size_tier_max": "S", "notional_usdt": {"min": 10, "max": 20},
        "leverage": {"min": 1, "max": 2}, "stop_atr": {"min": 1, "max": 2},
        "take_profit_r": {"min": 1, "max": 2}, "reason": "触发区间不可达",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }  # fmt: skip
    t = NOW - timedelta(minutes=10)
    recs = [
        _rec(c.Role.COMMANDER, c.Outcome.ACTION, "WLD 开会结论", t, symbol="WLDUSDT", output=cs),
        _rec(c.Role.COMMANDER, c.Outcome.ACTION, "出卡", t + timedelta(minutes=1), card_id="card-1",
             output={"symbols": ["WLDUSDT"], "expires_at": (NOW + timedelta(hours=1)).isoformat()}),
    ]  # fmt: skip
    view = _card(recs, NOW)
    assert view is not None
    assert view.symbols[0].bias == "no_trade" and view.symbols[0].reason == "触发区间不可达"


def test_funds_ai_cost_detail(tmp_path: Path) -> None:
    """资金页 AI 费用明细：每次调用谁 / 币 / 花费 / tokens / 结果，按军师团 / 总盘会 / Jev 小计。"""
    from jiaoyi.panel.pages import build_funds

    log = JsonlDecisionLog(tmp_path)
    t = lambda m: NOW - timedelta(minutes=m)  # noqa: E731
    usage = {"usage": {"input_tokens": 900, "output_tokens": 100}}
    log.write(_rec(c.Role.COMMANDER, c.Outcome.ACTION, "BTCUSDT 开会结论：long", t(30),
                   symbol="BTCUSDT", cost_usd=0.08))  # fmt: skip
    log.write(_rec(c.Role.COMMANDER, c.Outcome.ERROR, "ETHUSDT 开会失败：超时", t(29),
                   symbol="ETHUSDT", cost_usd=0.02))  # fmt: skip
    log.write(_rec(c.Role.COMMANDER, c.Outcome.ACTION, "出作战卡 v3", t(28), card_id="v3",
                   cost_usd=0.01))  # fmt: skip
    log.write(_rec(c.Role.GATE, c.Outcome.PASS, "BTC 拍板", t(10), cost_usd=0.002, output=usage))
    log.write(_rec(c.Role.GATE, c.Outcome.REJECT, "规则拒绝", t(9)))  # 没调 AI，不算
    log.write(_rec(c.Role.KEEPER, c.Outcome.HOLD, "BTC 拿住", t(5), cost_usd=0.001))
    page = build_funds(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, NOW)
    ai = page.ai
    assert ai is not None and ai.range == "today" and ai.row_count == 5
    assert ai.total_usd == pytest.approx(0.113)
    groups = {g.kind: (g.cost_usd, g.calls) for g in ai.groups}
    assert groups["commander"] == (pytest.approx(0.1), 2)
    assert groups["market"] == (pytest.approx(0.01), 1) and groups["jev"][1] == 2
    first, *_, last = ai.rows  # 最新在前
    assert first.who == "Jev 持仓审查" and first.tokens is None
    assert last.who == "军师团 · BTC 会议"
    by_who = {r.who: r for r in ai.rows}
    assert by_who["军师团 · ETH 会议"].result == "timeout"
    assert by_who["Jev 拍板"].tokens == 1000
    assert by_who["总盘会（挑币上会）"].cost_usd == pytest.approx(0.01)
    with _client(tmp_path, "10.0.0.2") as client:
        assert client.get("/api/funds?ai=all").json()["ai"]["range"] == "all"
        assert client.get("/api/funds?ai=year").status_code == 422


def test_card_pool_rows_show_source_and_hide_expired(tmp_path: Path) -> None:
    """卡池：每行写「来自 hh:mm 的会 · hh:mm 到期」，快到期变灰，过期的币不显示，持仓币留着。"""
    from jiaoyi.panel.overview import _card, _with_pool_notes

    def cs(sym: str) -> dict[str, Any]:
        return {
            "symbol": sym, "category": "alt", "bias": "long", "key_levels": [],
            "allowed_signals": [], "size_tier_max": "S", "notional_usdt": {"min": 10, "max": 20},
            "leverage": {"min": 1, "max": 2}, "stop_atr": {"min": 1, "max": 2},
            "take_profit_r": {"min": 1, "max": 2}, "reason": "r",
        }  # fmt: skip

    t0 = NOW - timedelta(minutes=30)
    iso = lambda m: (NOW + timedelta(minutes=m)).isoformat()  # noqa: E731
    pool = {
        "AAUSDT": {"issued_at": t0.isoformat(), "expires_at": iso(60), "held": False},
        "BBUSDT": {"issued_at": t0.isoformat(), "expires_at": iso(5), "held": False},
        "CCUSDT": {"issued_at": t0.isoformat(), "expires_at": iso(-1), "held": False},
        "DDUSDT": {"issued_at": t0.isoformat(), "expires_at": iso(-1), "held": True},
    }
    recs = [
        _rec(c.Role.COMMANDER, c.Outcome.ACTION, f"{s} 结论", t0, symbol=s, output=cs(s))
        for s in pool
    ] + [
        _rec(c.Role.COMMANDER, c.Outcome.ACTION, "出卡", NOW - timedelta(minutes=1), card_id="k",
             output={"symbols": list(pool), "pool": pool, "expires_at": iso(60)})
    ]  # fmt: skip
    view = _with_pool_notes(_card(recs, NOW), NOW, "UTC")
    assert view is not None
    by = {r.symbol: r for r in view.symbols}
    assert set(by) == {"AAUSDT", "BBUSDT", "DDUSDT"}  # CC 过期消失
    assert (
        by["AAUSDT"].source_note
        == f"来自 {t0:%H:%M} 的会 · {NOW + timedelta(minutes=60):%H:%M} 到期"
    )
    assert not by["AAUSDT"].fading and by["BBUSDT"].fading
    assert (
        by["DDUSDT"].source_note is not None
        and "已过期·持仓中，不再开新仓" in by["DDUSDT"].source_note
    )


def test_card_history_lists_past_orders(tmp_path: Path) -> None:
    """历史卡：按日期列出过去的指令，写明到期 / 被替换 / 还在用，带当时参数和开过的仓。"""
    from jiaoyi.panel.card_history import build_card_history

    card = c.BattleCard.model_validate(load_example("battle_card"))
    sym = card.symbols[0]
    j = tmp_path / "card_review" / "cards.jsonl"
    j.parent.mkdir()

    def row(at: datetime, mins: int, bias: str) -> str:
        return json.dumps({
            "card_id": card.card_id, "version": 1, "created_at": at.isoformat(),
            "expires_at": (at + timedelta(minutes=mins)).isoformat(), "regime": "震荡",
            "refresh_reason": "scheduled", "symbol": sym.symbol, "category": "major", "bias": bias,
        })  # fmt: skip

    t0 = NOW - timedelta(hours=3)
    j.write_text("\n".join([
        row(t0, 80, "long"),  # 2 小时后出了新结论（到期后），算到期
        row(t0 + timedelta(hours=2), 80, "short"),  # 1 小时后被替换
        row(t0 + timedelta(hours=3), 80, "both"),  # 还在用
        row(t0 - timedelta(days=1), 80, "long"),  # 前一天
    ]) + "\n")  # fmt: skip
    log = JsonlDecisionLog(tmp_path)
    log.write(c.DecisionRecord(record_id="m1", ts=t0 + timedelta(minutes=2), role=c.Role.COMMANDER,
                               outcome=c.Outcome.ACTION, summary="结论", symbol=sym.symbol,
                               output={**sym.model_dump(mode="json"),
                                       "usage": {"prompt_tokens": 1}}))  # fmt: skip
    pos = load_example("position_record")
    pos.update(symbol=sym.symbol, opened_at=(t0 + timedelta(minutes=30)).isoformat())
    (tmp_path / "positions.json").write_text(
        json.dumps({"positions": {"p1": {"record": pos, "notional_usdt": 100}}})
    )
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    page = build_card_history(files, NOW)
    assert len(page.days) == 2 and page.day == page.days[0] and page.symbols == [sym.symbol]
    assert [r.end for r in page.rows] == ["active", "replaced", "expired"]  # 新的在前
    old = page.rows[-1]
    assert old.reason == sym.reason and old.leverage == (sym.leverage.min, sym.leverage.max)
    assert old.trades == 1 and page.rows[0].trades == 0
    assert page.rows[1].ended_at == t0 + timedelta(hours=3)
    prev = build_card_history(files, NOW, day=date.fromisoformat(page.days[1]))
    assert len(prev.rows) == 1 and prev.rows[0].reason == ""  # 那天没有会议记录
    assert build_card_history(files, NOW, symbol="X").rows == []


def test_position_view_mark_notional_margin(tmp_path: Path) -> None:
    """持仓表的现价、名义、本金占用、浮盈% 都由 hummingbot 浮盈和账本算出（同一口径）。"""
    from jiaoyi.panel.pages import build_positions

    _seed(tmp_path)
    page = build_positions(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, NOW)
    p = next(x for x in page.positions if x.symbol == "BTCUSDT")
    # 多单 0.015 个、开仓 68044、浮亏 20U → 现价 68044 − 20 / 0.015
    assert p.mark_price == pytest.approx(68044 - 20 / 0.015)
    assert p.notional_usdt == pytest.approx(0.015 * p.mark_price)
    assert p.margin_usdt == pytest.approx(p.notional_usdt / 3)
    assert p.pnl_pct == pytest.approx(-20 / p.notional_usdt * 100)
    # 扣费后：开仓那次用账本实收（样例 0.51U），平仓那次按现在名义 × 费率估
    fees = 0.51 + p.notional_usdt * RISK.taker_fee_bps / 10_000
    assert p.fees_est_usdt == pytest.approx(fees)
    assert p.net_unrealized_usdt == pytest.approx(-20 - fees)
    assert not p.fee_from_fills and p.fee_bps == RISK.taker_fee_bps


def test_fee_rate_from_recent_fills() -> None:
    """面板不拿密钥：账户实际费率从最近平掉的单实收倒推（Demo 是 0.04%，不是配置的 0.05%）。"""
    from jiaoyi.panel.overview import fee_rate_bps

    rec = c.PositionRecord.model_validate(load_example("position_record"))
    closed = rec.model_copy(
        update={"status": c.PositionStatus.CLOSED, "closed_at": NOW, "quantity": 1.0,
                "entry_price": 100.0, "exit_price": 100.0, "fees_usdt": 0.08}
    )  # fmt: skip
    assert fee_rate_bps([closed]) == pytest.approx(4.0)
    assert fee_rate_bps([rec]) is None  # 没平过的单：退回配置费率，页面标「估」


def test_market_role_and_run_line_fields(tmp_path: Path) -> None:
    """总盘会只在开会时报状态：没到下次计划时间不算断心跳；上次 / 下次带给面板。"""
    from jiaoyi.panel.pages import build_team

    _seed(tmp_path)
    log = JsonlDecisionLog(tmp_path)
    log.heartbeat(
        c.ModuleStatus(
            role=c.Role.MARKET,
            health=c.Health.OK,
            ts=NOW - timedelta(minutes=50),
            headline="挑了 5 个",
            last_run_at=NOW - timedelta(minutes=50),
            next_run_at=NOW + timedelta(minutes=70),
        )
    )
    team = build_team(PanelFiles(tmp_path, RISK.day_reset_tz), NOW, c.Role.GATE)
    mr = team.market_role
    assert mr is not None and mr.state == "ok" and mr.name == "总盘会"
    assert mr.next_run_at == NOW + timedelta(minutes=70) and mr.summary is None
    assert (
        build_team(
            PanelFiles(tmp_path, RISK.day_reset_tz), NOW + timedelta(hours=3), c.Role.GATE
        ).market_role.state
        == "bad"
    )  # type: ignore[union-attr]


async def test_price_decimals_from_tick_size() -> None:
    from jiaoyi.panel.market import KlineSource, tick_decimals

    assert tick_decimals("0.00010000") == 4 and tick_decimals("1") == 0

    def pf(tick: str) -> list[dict[str, str]]:
        return [{"filterType": "PRICE_FILTER", "tickSize": tick}]

    info = {
        "symbols": [
            {"symbol": "APTUSDT", "filters": pf("0.0001")},
            {"symbol": "BTCUSDT", "filters": pf("0.10")},
            {"symbol": "BTCUSDT_251226", "filters": []},
        ]
    }
    src = KlineSource(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=info)))
    assert await src.price_decimals() == {"APTUSDT": 4, "BTCUSDT": 1}
    await src.close()


async def test_hb_snapshot_asks_for_fresh_balance() -> None:
    """面板拉总资产时让 hummingbot-api 先从币安重拉余额，并记下拉到的时间。"""
    seen: list[dict[str, Any]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(json.loads(req.content))
        if req.url.path == "/portfolio/state":
            return httpx.Response(200, json={"a": {"c": [{"token": "USDT", "units": 100}]}})
        return httpx.Response(200, json={"data": []})

    hb = HbReader(
        "http://hb", "u", "p", account_name="a", connector_name="c",
        transport=httpx.MockTransport(handler),
    )  # fmt: skip
    snap = await hb.snapshot()
    assert seen[0]["refresh"] is True and snap.wallet_usdt == 100 and snap.fetched_at is not None
    await hb.close()


async def test_card_atr_same_formula_as_guard() -> None:
    """作战卡的 ATR：币安原生 N 分钟线丢掉没走完的最后一根，Wilder 14；短长周期按风控公式合起来。"""
    from jiaoyi.market.features import wilder_atr
    from jiaoyi.panel.market import KlineSource
    from jiaoyi.risk.guard import blend_atr

    assert wilder_atr([(11.0, 9.0, 10.0)] * 14) is None  # 14 根只有 13 个 TR
    assert wilder_atr([(11.0, 9.0, 10.0)] * 15) == pytest.approx(2.0)
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params["interval"])
        rows = [[i * 60_000, "10", "11", "9", "10"] for i in range(30)]
        rows.append([30 * 60_000, "10", "30", "1", "10"])  # 没走完的一根，不算
        return httpx.Response(200, json=rows)

    src = KlineSource(transport=httpx.MockTransport(handler))
    assert await src.atr("APTUSDT", 15) == pytest.approx(2.0) and seen == ["15m"]
    await src.close()
    fl = c.FastLoopConfig(atr_long_period_min=60)
    assert blend_atr(1.0, 4.0, fl) == pytest.approx(2.0)
    assert blend_atr(1.0, None, fl) == 1.0


def test_today_ai_cost_reads_slow_budget(tmp_path: Path) -> None:
    """军师团今天的花费以 slow_budget.json 为准（预记、失败都算）；别的日子的文件不认。"""
    from jiaoyi.core.recorder import local_day
    from jiaoyi.panel.pages import build_funds

    _seed(tmp_path)
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    base = build_funds(files, HB, RISK, NOW).pnl[0].ai_usd
    cmd = sum(r.cost_usd for r in files.decisions_today(NOW) if r.role is c.Role.COMMANDER)
    day = local_day(NOW, RISK.day_reset_tz)
    budget = tmp_path / "slow_budget.json"
    budget.write_text(json.dumps({"day": day.isoformat(), "cost_usd": 6.04}))
    assert build_funds(files, HB, RISK, NOW).pnl[0].ai_usd == pytest.approx(base - cmd + 6.04)
    budget.write_text(json.dumps({"day": "2026-01-01", "cost_usd": 6.04}))
    assert build_funds(files, HB, RISK, NOW).pnl[0].ai_usd == pytest.approx(base)


def test_history_flags_trade_without_numbers(tmp_path: Path) -> None:
    """执行器没报盈亏和手续费、也没回填平仓价：标成没取到，不当成 0。"""
    from jiaoyi.panel.pages import build_history

    _seed(tmp_path)
    raw = json.loads((tmp_path / "positions.json").read_text())
    for t in raw["positions"].values():
        t["record"].update(realized_pnl_usdt=0.0, fees_usdt=0.0, exit_price=None)
    (tmp_path / "positions.json").write_text(json.dumps(raw))
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    [row] = build_history(files, RISK, NOW, "today", include_tests=True).rows
    assert row.pnl_missing


def test_team_shows_sentinel_detail(tmp_path: Path) -> None:
    """哨兵报的每币一行带到团队页；掉出作战卡、也没持仓的币不列，只记个数。"""
    from jiaoyi.panel.pages import build_team

    _seed(tmp_path)  # BTC 有持仓
    line = "BTCUSDT 量 1.2 倍（突增要 4、突破要 3）主动买 55%，量能突增还差 2.8 倍"
    gone = "DOGEUSDT 量 0.9 倍（突增要 4、突破要 3）主动卖 52%，量能突增还差 3.1 倍；不在作战卡上"
    st = c.ModuleStatus(
        role=c.Role.SENTINEL, health=c.Health.OK, ts=NOW, headline="卡上能出信号的 1 个币",
        detail=[line, gone],
    )  # fmt: skip
    (tmp_path / "module_status.json").write_text(
        json.dumps({st.role.value: st.model_dump(mode="json")})
    )
    team = build_team(PanelFiles(tmp_path, RISK.day_reset_tz), NOW, c.Role.SENTINEL)
    sent = next(r for r in team.roles if r.role is c.Role.SENTINEL)
    assert sent.detail == [line] and sent.detail_hidden == 1
    assert sent.headline == "卡上能出信号的 1 个币"


def test_settings_majors_list_and_names_edit(tmp_path: Path) -> None:
    from jiaoyi.panel.actions import ActionError, _coerce_names, editable_fields
    from jiaoyi.panel.pages import _majors
    from jiaoyi.slow.settings import ScreenerSection

    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    scr = ScreenerSection(major_include=["link"], major_exclude=["DOGE"])
    m = _majors(files, scr)  # 还没缓存：固定 7 个 + 手动加入 − 排除
    assert m.fallback and "DOGE" not in [r.name for r in m.rows] and m.excluded == ["DOGE"]
    assert m.rows[-1].name == "LINK" and m.rows[-1].why == "你手动加入"
    (tmp_path / "majors.json").write_text(json.dumps(
        {"fetched_at": "2026-10-24T00:00:00+00:00", "v": 2,
         "ranks": {"BTC": 1, "ETH": 2, "XRP": 4, "SOL": 6},
         "effective": {"ETH": 2, "BTC": 1}, "effective_top_n": 20}
    ))  # fmt: skip
    m = _majors(files, scr)  # 面板只认选币器写的 effective，不把整张候选表当主流币
    assert not m.fallback and m.fetched_top_n == 20
    assert [(r.name, r.rank) for r in m.rows] == [("BTC", 1), ("ETH", 2), ("LINK", None)]
    assert _majors(files, ScreenerSection(major_top_n=0)).fallback  # 0 = 只用固定 7 个
    spec = editable_fields("screener")["major_include"]
    assert spec.names
    assert _coerce_names("k", "link, avaxusdt LINK") == ["LINK", "AVAX"]
    assert _coerce_names("k", []) == []
    with pytest.raises(ActionError):
        _coerce_names("k", ["BTC-1"])
    # 币名列表是 default_factory：没改过不该出「恢复默认」，改过才出，默认值显示空列表
    from jiaoyi.panel.pages import _items

    rows = {i.key: i for i in _items(ScreenerSection(), "screener")}
    assert not rows["major_include"].resettable
    rows = {i.key: i for i in _items(scr, "screener")}
    assert rows["major_include"].resettable and rows["major_include"].code_default == []


def test_pnl_curve_record_and_read(tmp_path: Path) -> None:
    from jiaoyi.panel import pnl_curve

    t = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)
    (tmp_path / "pnl_curve").mkdir()
    (tmp_path / "pnl_curve" / "2026-10-01.jsonl").write_text("")  # 超过 14 天的删掉
    for i, eq in enumerate([1000.0, 1030.0, 990.0, 1010.0]):
        pnl_curve.record(tmp_path, "2026-10-24", t + timedelta(minutes=i), eq, 1000.0)
    assert not (tmp_path / "pnl_curve" / "2026-10-01.jsonl").exists()
    with (tmp_path / "pnl_curve" / "2026-10-24.jsonl").open("a") as f:
        f.write('{"ts": "2026-10-24T01:0')  # 写到一半的行跳过
    now = pnl_curve.PnlPoint(ts=t + timedelta(minutes=4), pnl_usdt=5.0)
    c = pnl_curve.read(tmp_path, "2026-10-24", 1000.0, now)
    assert [p.pnl_usdt for p in c.points] == [0.0, 30.0, -10.0, 10.0, 5.0]
    assert c.high and c.high.pnl_usdt == 30.0 and c.high.ts == t + timedelta(minutes=1)
    assert c.low and c.low.pnl_usdt == -10.0 and c.now == now
    # 基准按现在的算（0 点基准事后改了，整条曲线跟着平移）
    assert pnl_curve.read(tmp_path, "2026-10-24", 990.0).high.pnl_usdt == 40.0  # type: ignore[union-attr]
    assert pnl_curve.read(tmp_path, "2026-10-23", None).points == []


def test_pnl_curve_endpoint(tmp_path: Path) -> None:
    _seed(tmp_path)
    with _client(tmp_path, "127.0.0.1") as client:
        body = client.get("/api/pnl-curve").json()
        assert body["day"] == "2026-10-22" and isinstance(body["points"], list)


def test_team_against_signals_only_recent_routed(tmp_path: Path) -> None:
    """流程图反向信号线：只要最近 30 分钟、真交出去了的（叫管家 / 申请临时会），新的在前。"""
    from jiaoyi.panel.pages import build_team

    def ev(mins: int, actions: list[str]) -> c.AgainstSignalEvent:
        return c.AgainstSignalEvent.model_validate({
            "symbol": "BTCUSDT", "ts": (NOW - timedelta(minutes=mins)).isoformat(),
            "kind": "volume_surge", "side": "long", "actions": actions,
        })  # fmt: skip

    st = c.ModuleStatus(
        role=c.Role.SENTINEL, health=c.Health.OK, ts=NOW,
        against_signals=[ev(40, ["keeper_recheck"]), ev(10, []), ev(5, ["meeting"]),
                         ev(2, ["keeper_recheck"])],
    )  # fmt: skip
    (tmp_path / "module_status.json").write_text(
        json.dumps({st.role.value: st.model_dump(mode="json")})
    )
    team = build_team(PanelFiles(tmp_path, RISK.day_reset_tz), NOW, None)
    assert [(NOW - e.ts).seconds // 60 for e in team.against] == [2, 5]
    assert build_team(PanelFiles(tmp_path / "x", RISK.day_reset_tz), NOW, None).against == []


def test_team_against_checks_pinned_and_tagged(tmp_path: Path) -> None:
    """管家卡的反向急查：配上信号之后同一笔仓的那次急查判断，例行复查冲不掉；日志里标「反向急查」。"""
    from jiaoyi.panel.pages import build_team

    def ev(mins: int, pid: str, **kw: Any) -> c.AgainstSignalEvent:
        return c.AgainstSignalEvent.model_validate({
            "symbol": "ZECUSDT", "ts": (NOW - timedelta(minutes=mins)).isoformat(),
            "kind": "volume_surge", "side": "long", "actions": ["keeper_recheck"],
            "position_id": pid, "desc": "放量拉升", **kw,
        })  # fmt: skip

    st = c.ModuleStatus(
        role=c.Role.SENTINEL, health=c.Health.OK, ts=NOW,
        against_signals=[ev(6, "p1", keeper_verdict="hold"), ev(1, "p2")],
    )  # fmt: skip
    (tmp_path / "module_status.json").write_text(
        json.dumps({st.role.value: st.model_dump(mode="json")})
    )
    log = JsonlDecisionLog(tmp_path)
    k = c.Role.KEEPER
    log.write(
        _rec(
            k,
            c.Outcome.HOLD,
            "ZECUSDT 拿住：平仓概率 0.20",
            NOW - timedelta(minutes=8),
            position_id="p1",
            output={"action": "hold", "p_close": 0.2},
        )
    )  # 信号前的例行
    log.write(
        _rec(
            k,
            c.Outcome.HOLD,
            "ZECUSDT 拿住：平仓概率 0.41，反向信号急查（放量拉升）",
            NOW - timedelta(minutes=5),
            position_id="p1",
            output={"action": "hold", "p_close": 0.41},
        )
    )
    log.write(
        _rec(
            k,
            c.Outcome.HOLD,
            "NEARUSDT 拿住：平仓概率 0.50",
            NOW - timedelta(minutes=1),
            position_id="p9",
            output={"action": "hold", "p_close": 0.5},
        )
    )  # 例行复查
    team = build_team(PanelFiles(tmp_path, RISK.day_reset_tz), NOW, k)
    new, old = team.against_checks
    assert (new.symbol, new.verdict, new.reason) == ("ZECUSDT", None, None)  # 还没查完
    assert (old.verdict, old.p_close) == ("hold", 0.41)
    assert old.reason == "平仓概率 0.41，反向信号急查（放量拉升）"
    assert [r.tag for r in team.records] == [None, "反向急查", None]

    # 新格式 output.against：第一次没问成留到下一轮，取查成的那次；对的是哪次信号按时间认
    ag = {"ts": (NOW - timedelta(minutes=1)).isoformat(), "desc": "放量拉升", "strong": False}
    log.write(
        _rec(
            k,
            c.Outcome.ERROR,
            "ZECUSDT 拿住：Jev 服务方繁忙",
            NOW - timedelta(seconds=40),
            position_id="p2",
            output={"action": "hold", "against": ag},
        )
    )
    log.write(
        _rec(
            k,
            c.Outcome.CLOSE,
            "ZECUSDT 全平：平仓概率 0.80",
            NOW - timedelta(seconds=20),
            position_id="p2",
            output={"action": "close", "p_close": 0.8, "against": ag},
        )
    )
    new = build_team(PanelFiles(tmp_path, RISK.day_reset_tz), NOW, k).against_checks[0]
    assert (new.verdict, new.p_close, new.failed) == ("close", 0.8, False)


def test_team_predictor_meetings_recent_only(tmp_path: Path) -> None:
    """预测员 → 军师团的线：作战卡流水里 30 分钟内的 event:predictor_against_card 才亮。"""
    from jiaoyi.panel.pages import build_team

    def row(mins: int, reason: str, sym: str) -> str:
        return json.dumps({
            "card_id": "c", "version": 2, "created_at": (NOW - timedelta(minutes=mins)).isoformat(),
            "expires_at": (NOW + timedelta(hours=1)).isoformat(), "regime": "x",
            "refresh_reason": reason, "symbol": sym, "category": "alt", "bias": "short",
        })  # fmt: skip

    (tmp_path / "card_review").mkdir()
    (tmp_path / "card_review" / "cards.jsonl").write_text(
        "\n".join([
            row(45, "event:predictor_against_card", "OLDUSDT"),  # 太久了
            row(20, "event:key_level_break", "KEYUSDT"),  # 别的原因
            row(12, "event:predictor_against_card", "AUSDT"),
            row(4, "event:predictor_against_card", "BUSDT"),
        ]) + "\n"
    )  # fmt: skip
    team = build_team(PanelFiles(tmp_path, RISK.day_reset_tz), NOW, None)
    assert [(m.symbol, m.bias) for m in team.predictor_meetings] == [
        ("BUSDT", "short"),
        ("AUSDT", "short"),
    ]

    # 流水很长时只读末尾：前面塞满旧行，结果不变
    from jiaoyi.panel import pages

    path = tmp_path / "card_review" / "cards.jsonl"
    tail = path.read_text()
    filler = (row(600, "scheduled", "XUSDT") + "\n") * (pages.TAIL_BYTES // 200 + 10)
    path.write_text(filler + tail)
    assert path.stat().st_size > pages.TAIL_BYTES
    again = build_team(PanelFiles(tmp_path, RISK.day_reset_tz), NOW, None).predictor_meetings
    assert again == team.predictor_meetings


def test_pnl_curve_backfill_from_ledger_and_klines(tmp_path: Path) -> None:
    """记录开始前那段：已平的按净盈亏（0 点前那段扣掉），开着的按 5 分钟收盘价算浮盈。"""
    import asyncio

    from jiaoyi.panel import pnl_curve

    since = datetime(2026, 10, 23, 17, 0, tzinfo=UTC)  # 曼谷 0 点
    until = since + timedelta(minutes=30)

    def rec(**kw: Any) -> c.PositionRecord:
        return c.PositionRecord.model_validate({**load_example("position_record"), **kw})

    a = rec(
        position_id="a",
        symbol="AAAUSDT",
        side="long",
        quantity=2.0,
        entry_price=100.0,
        opened_at=(since + timedelta(minutes=5)).isoformat(),
        status="closed",
        closed_at=(since + timedelta(minutes=17)).isoformat(),
        realized_pnl_usdt=15.0,
    )
    b = rec(position_id="b", symbol="BBBUSDT", side="short", quantity=1.0, entry_price=50.0,
            opened_at=(since - timedelta(hours=1)).isoformat(), status="open",
            closed_at=None, realized_pnl_usdt=None)  # fmt: skip
    t0 = int(since.timestamp())
    px = {"AAAUSDT": [100, 101, 104, 108, 108, 108, 108], "BBBUSDT": [48, 47, 46, 49, 50, 51, 52]}

    async def kl(sym: str) -> list[dict[str, float]]:
        return [{"time": t0 - 300 + i * 300, "close": float(v), "open": 0, "high": 0, "low": 0}
                for i, v in enumerate(px[sym])]  # fmt: skip

    pts = asyncio.run(pnl_curve.backfill([a, b], since, until, kl))
    assert [p.ts for p in pts][:2] == [since, since + timedelta(minutes=5)] and all(
        p.est for p in pts
    )
    # 0 点 B 的参考价 = 0 点前一根收盘 48；空单价格跌 → 赚
    assert pts[0].pnl_usdt == 0.0
    assert pts[1].pnl_usdt == 2 * (101 - 100) + (48 - 47)  # A 刚开（00:05 收 101）
    assert pts[4].pnl_usdt == 15.0 + (48 - 50)  # 00:20：A 已平按净盈亏，B 浮亏
    pnl_curve.save_est(tmp_path, "2026-10-24", pts)
    assert pnl_curve.load_est(tmp_path, "2026-10-24") == pts
    pnl_curve.record(tmp_path, "2026-10-24", until, 1010.0, 1000.0)
    c2 = pnl_curve.read(tmp_path, "2026-10-24", 1000.0, None, pts)
    assert len(c2.points) == len(pts) + 1 and c2.points[-1].est is False
