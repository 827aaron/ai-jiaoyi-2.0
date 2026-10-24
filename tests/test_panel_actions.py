"""面板写操作：指令、改配置、密码、Host 白名单和跨站校验。"""

from __future__ import annotations

import json
import shutil
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from jiaoyi.contracts import Category
from jiaoyi.core import profiles
from jiaoyi.core.commands import (
    CommandInbox,
    CommandResult,
    config_fingerprint,
    write_paused,
    write_runtime,
)
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.execution.live_gate import CONFIRM_WORD, LiveGate
from jiaoyi.panel import actions
from jiaoyi.panel.actions import ActionError, ConfigPaths, set_toml_value
from jiaoyi.panel.hb import HbReader
from jiaoyi.panel.server import create_app, host_ok
from jiaoyi.slow.settings import load_slow_config
from tests.conftest import ROOT

NOW = datetime(2026, 10, 22, 6, 30, tzinfo=UTC)
PW = "correct horse"
HEADERS = {"X-Jiaoyi-Panel": "1", "Origin": "http://localhost:8090"}


def _setup(tmp_path: Path, password: bool = True) -> tuple[ConfigPaths, Path]:
    data = tmp_path / "data"
    data.mkdir()
    cfg = tmp_path / "config"
    cfg.mkdir()
    shutil.copy(ROOT / "config" / "risk.example.toml", cfg / "risk.toml")
    text = (ROOT / "config" / "app.example.toml").read_text(encoding="utf-8")
    (cfg / "app.toml").write_text(
        text.replace('data_dir = "data"', f'data_dir = "{data}"'), encoding="utf-8"
    )
    if password:
        LiveGate(data / "live_gate.json").set_password(PW)
    return ConfigPaths(risk=cfg / "risk.toml", app=cfg / "app.toml", slow=cfg / "slow.toml"), data


def _client(paths: ConfigPaths, now: datetime = NOW, ip: str = "192.168.1.20") -> TestClient:
    hb = HbReader(
        "http://hb",
        "u",
        None,
        account_name="a",
        connector_name="c",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    app = create_app(
        load_risk_config(paths.risk),
        load_app_config(paths.app),
        hb,
        clock=lambda: now,
        config_paths=paths,
    )
    return TestClient(app, client=(ip, 5000), base_url="http://localhost:8090")


# ---------------------------------------------------------------- 安全


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("localhost:8090", True),
        ("192.168.1.20:8090", True),
        ("[::1]:8090", True),
        ("aarons-mac-mini.local:8090", True),
        ("10.0.0.5", True),
        ("evil.example.com:8090", False),
        ("8.8.8.8:8090", False),
        ("", False),
        (None, False),
    ],
)
def test_host_allowlist(host: str | None, ok: bool) -> None:
    assert host_ok(host) is ok


def test_host_allowlist_extra_names() -> None:
    assert host_ok("jiaoyi.lan:8090", frozenset({"jiaoyi.lan"}))
    assert not host_ok("jiaoyi.lan:8090")


def test_dns_rebinding_host_is_rejected(tmp_path: Path) -> None:
    paths, _ = _setup(tmp_path)
    with _client(paths) as client:
        r = client.get("/api/overview", headers={"Host": "attacker.example.com"})
        assert r.status_code == 403


def test_post_needs_same_origin_and_custom_header(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    body = {"kind": "pause"}
    with _client(paths) as client:
        assert client.post("/api/command", json=body).status_code == 403
        r = client.post("/api/command", json=body, headers={"X-Jiaoyi-Panel": "1"})
        assert r.status_code == 403  # 没有 Origin
        evil = {"X-Jiaoyi-Panel": "1", "Origin": "http://evil.example.com"}
        assert client.post("/api/command", json=body, headers=evil).status_code == 403
        no_header = {"Origin": "http://localhost:8090"}
        assert client.post("/api/command", json=body, headers=no_header).status_code == 403
        assert not (data / "panel_commands").exists() or not any(
            (data / "panel_commands").glob("*.json")
        )
        assert client.post("/api/command", json=body, headers=HEADERS).status_code == 200


# ---------------------------------------------------------------- 指令


def _inbox_commands(data: Path) -> list[dict[str, str]]:
    return [json.loads(p.read_text()) for p in sorted((data / "panel_commands").glob("*.json"))]


def test_pause_and_close_need_no_password(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    with _client(paths) as client:
        for kind, params in (
            ("pause", {}),
            ("close_all", {}),
            ("close_position", {"position_id": "pos-0001"}),
            ("refresh_cards", {}),
        ):
            r = client.post("/api/command", json={"kind": kind, "params": params}, headers=HEADERS)
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "pending"
    cmds = _inbox_commands(data)
    assert sorted(c["kind"] for c in cmds) == [
        "close_all",
        "close_position",
        "pause",
        "refresh_cards",
    ]
    assert all(c["by"] == "panel" for c in cmds)
    # 主循环那边能读出来（契约一致、没过期）
    inbox = CommandInbox(data)
    got = [cmd for _, cmd, _ in inbox.pending()]
    assert len(got) == 4
    assert all(
        cmd is not None and inbox.check(cmd, NOW + timedelta(seconds=1)) is None for cmd in got
    )


def test_close_position_needs_id(tmp_path: Path) -> None:
    paths, _ = _setup(tmp_path)
    with _client(paths) as client:
        r = client.post("/api/command", json={"kind": "close_position"}, headers=HEADERS)
    assert r.status_code == 400 and "position_id" in r.json()["detail"]


def test_resume_and_unblock_need_password(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    with _client(paths) as client:
        r = client.post("/api/command", json={"kind": "resume"}, headers=HEADERS)
        assert r.status_code == 400 and "密码不对" in r.json()["detail"]
        body = {"kind": "unblock_symbol", "params": {"symbol": "XUSDT"}, "password": "nope"}
        r = client.post("/api/command", json=body, headers=HEADERS)
        assert r.status_code == 400
        assert not list((data / "panel_commands").glob("*.json"))
        r = client.post("/api/command", json={"kind": "resume", "password": PW}, headers=HEADERS)
        assert r.status_code == 200
    assert [c["kind"] for c in _inbox_commands(data)] == ["resume"]


def test_restart_needs_password(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    with _client(paths) as client:
        r = client.post("/api/command", json={"kind": "restart"}, headers=HEADERS)
        assert r.status_code == 400
        r = client.post("/api/command", json={"kind": "restart", "password": PW}, headers=HEADERS)
        assert r.status_code == 200
    assert [c["kind"] for c in _inbox_commands(data)] == ["restart"]


def test_password_lockout_is_shared_with_cli(tmp_path: Path) -> None:
    _, data = _setup(tmp_path)
    for _ in range(5):
        with pytest.raises(ActionError):
            actions.check_password(data, "wrong")
    with pytest.raises(ActionError, match="锁定"):
        actions.check_password(data, PW)  # 锁住期间对的也不行
    state = json.loads((data / "live_gate.json").read_text())
    assert state["locked_until"] is not None  # 落盘：命令行 go-live 也被锁


def test_no_password_set_is_explained(tmp_path: Path) -> None:
    _, data = _setup(tmp_path, password=False)
    with pytest.raises(ActionError, match="set-panel-password"):
        actions.check_password(data, "anything")


def test_command_result_endpoint(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    with _client(paths) as client:
        cid = client.post("/api/command", json={"kind": "pause"}, headers=HEADERS).json()["id"]
        assert client.get(f"/api/command/{cid}").json()["status"] == "pending"
        done = data / "panel_commands" / "done"
        done.mkdir(parents=True, exist_ok=True)
        res = CommandResult(id=cid, kind="pause", status="done", detail="已暂停", ts=NOW)
        (done / f"{cid}.json").write_text(res.model_dump_json())
        assert client.get(f"/api/command/{cid}").json() == {
            "id": cid,
            "status": "done",
            "detail": "已暂停",
        }
        assert client.get("/api/command/..%2F..%2Fx").status_code in (400, 404)


def test_overview_shows_pause_and_corrupt_pause(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    with _client(paths) as client:
        ctl = client.get("/api/overview").json()["control"]
        assert ctl["paused"] is False and ctl["pw_set"] is True and ctl["writable"] is True
        write_paused(data, True, since=NOW.isoformat(), reason="全部平仓前自动暂停")
        ctl = client.get("/api/overview").json()["control"]
        assert ctl["paused"] and not ctl["paused_corrupt"]
        assert ctl["paused_reason"] == "全部平仓前自动暂停"
        (data / "paused.json").write_text("{broken")
        ctl = client.get("/api/overview").json()["control"]
        assert ctl["paused"] and ctl["paused_corrupt"]


# ---------------------------------------------------------------- 改配置


def test_set_toml_value_keeps_comments_and_layout() -> None:
    text = (
        "# 头注释\n"
        "a = 1            # 说明 a\n"
        "# b = 2  被注释掉的不动\n"
        "\n"
        "# 段前注释\n"
        "[fast_loop]\n"
        "x = 0.5   # 说明 x\n"
        'name = "keep # not a comment"\n'
    )
    out = set_toml_value(text, None, "a", 3)
    assert "a = 3            # 说明 a\n" in out and "# b = 2" in out
    out = set_toml_value(out, None, "b", True)
    lines = out.splitlines()
    assert lines.index("b = true") == lines.index("a = 3            # 说明 a") + 1
    out = set_toml_value(out, "fast_loop", "x", 0.7)
    assert "x = 0.7   # 说明 x" in out
    out = set_toml_value(out, "fast_loop", "y", 2)
    assert out.endswith('name = "keep # not a comment"\ny = 2\n')
    out = set_toml_value(out, "other", "z", 1.5)
    data = tomllib.loads(out)
    assert data == {
        "a": 3,
        "b": True,
        "fast_loop": {"x": 0.7, "name": "keep # not a comment", "y": 2},
        "other": {"z": 1.5},
    }


def test_clearing_notional_usdt_cap_deletes_the_line(tmp_path: Path) -> None:
    """面板上把「单笔绝对上限 USDT」清空 = 删掉 risk.toml 那一行，只按权益倍数。"""
    paths, data = _setup(tmp_path)
    text = paths.risk.read_text(encoding="utf-8")
    paths.risk.write_text("max_notional_per_trade_usdt = 2000\n" + text, encoding="utf-8")
    assert load_risk_config(paths.risk).max_notional_per_trade_usdt == 2000
    changed = actions.update_config(
        paths, data, "risk", {"max_notional_per_trade_usdt": None}, PW, NOW
    )
    assert changed == [{"key": "max_notional_per_trade_usdt", "old": 2000, "new": None}]
    lines = paths.risk.read_text(encoding="utf-8").splitlines()
    assert not any(ln.startswith("max_notional_per_trade_usdt") for ln in lines)
    assert load_risk_config(paths.risk).max_notional_per_trade_usdt is None


def test_update_risk_writes_backup_and_log(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    before = paths.risk.read_text(encoding="utf-8")
    changed = actions.update_config(
        paths, data, "risk", {"max_leverage": 3, "risk_per_trade_pct": 0.5}, PW, NOW
    )
    assert {c["key"] for c in changed} == {"max_leverage", "risk_per_trade_pct"}
    risk = load_risk_config(paths.risk)
    assert risk.max_leverage == 3 and risk.risk_per_trade_pct == 0.5
    text = paths.risk.read_text(encoding="utf-8")
    assert "max_leverage = 3                    # 杠杆 ≤ 5 倍（≥1" in text
    [bak] = list(paths.risk.parent.glob("risk.toml.bak-*"))
    assert bak.read_text(encoding="utf-8") == before
    rows = actions.recent_changes(data)
    assert {r["key"] for r in rows} == {"max_leverage", "risk_per_trade_pct"}
    assert all(r["file"] == "risk.toml" and r["section"] == "risk" for r in rows)


@pytest.mark.parametrize(
    ("section", "changes", "match"),
    [
        ("risk", {"max_leverage": 0}, "max_leverage"),  # 不合理：杠杆至少 1 倍
        ("risk", {"max_leverage": 126}, "最高 125 倍"),
        ("risk", {"max_leverage": 2.5}, "整数"),
        ("risk", {"day_reset_tz": 1}, "不能在网页上改"),
        ("category", {"major": 4.0}, "总敞口"),  # 超过总敞口 3 倍
        ("app", {"mode": 1}, "不能在网页上改"),
        ("app", {"data_dir": 1}, "不能在网页上改"),
        ("app", {"predictor_enabled": 1}, "开关"),
        ("fast_loop", {"jev_model": 1}, "不能在网页上改"),
        ("fast_loop", {"min_p_take": float("nan")}, "数字"),
        ("fast_loop", {"rule_gate_without_jev": "yes"}, "开关"),
    ],
)
def test_update_rejects_bad_values_without_writing(
    tmp_path: Path, section: str, changes: dict[str, object], match: str
) -> None:
    paths, data = _setup(tmp_path)
    risk_before = paths.risk.read_text(encoding="utf-8")
    app_before = paths.app.read_text(encoding="utf-8")
    with pytest.raises(ActionError, match=match):
        actions.update_config(paths, data, section, changes, PW, NOW)  # type: ignore[arg-type]
    assert paths.risk.read_text(encoding="utf-8") == risk_before
    assert paths.app.read_text(encoding="utf-8") == app_before
    assert actions.recent_changes(data) == []
    assert not list(paths.risk.parent.glob("*.bak-*"))


def test_update_needs_password(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    with pytest.raises(ActionError, match="密码不对"):
        actions.update_config(paths, data, "risk", {"max_leverage": 3}, "nope", NOW)
    assert load_risk_config(paths.risk).max_leverage == 5


def test_update_app_and_fast_loop_sections(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    actions.update_config(
        paths, data, "app", {"reconciler_close_unknown_positions": True, "pool_size": 30}, PW, NOW
    )
    actions.update_config(paths, data, "fast_loop", {"min_p_take": 0.65}, PW, NOW)
    actions.update_config(paths, data, "category", {"alt": 0.5}, PW, NOW)
    app = load_app_config(paths.app)
    assert app.reconciler_close_unknown_positions is True and app.pool_size == 30
    assert app.fast_loop.min_p_take == 0.65
    assert load_risk_config(paths.risk).category_exposure_x[Category.ALT] == 0.5
    # 原来注释掉的那行还在，新加的在顶层（不会跑进 [fast_loop]）
    assert tomllib.loads(paths.app.read_text())["reconciler_close_unknown_positions"] is True
    # 没变的值不算改动
    assert actions.update_config(paths, data, "app", {"pool_size": 30}, PW, NOW) == []


def test_config_pending_compares_runtime_fingerprint(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    assert actions.config_pending(paths, data) is None  # 主循环没跑过
    risk, app = load_risk_config(paths.risk), load_app_config(paths.app)
    write_runtime(
        data, config_fingerprint=config_fingerprint(risk, app, load_slow_config(paths.slow))
    )
    assert actions.config_pending(paths, data) is False
    actions.update_config(paths, data, "risk", {"max_open_positions": 3}, PW, NOW)
    assert actions.config_pending(paths, data) is True


def test_settings_endpoints(tmp_path: Path) -> None:
    paths, _ = _setup(tmp_path)
    with _client(paths) as client:
        page = client.get("/api/settings").json()
        assert page["writable"] is True and page["changes"] == []
        editable = {i["key"] for i in page["app"] if i["editable"]}
        assert "pool_size" in editable and "mode" not in editable and "data_dir" not in editable
        fl = {i["key"]: i for i in page["fast_loop"]}
        assert fl["atr_period_min"]["choices"] == [5, 15, 30, 60, 240]
        assert not fl["jev_model"]["editable"] and not fl["jev_provider"]["editable"]
        pred = {i["key"]: i for i in page["predictor"]}
        assert pred["mode"]["choices"] == ["on_demand", "all_symbols"]
        sched = {i["key"]: i for i in page["schedule"]}
        assert (
            sched["max_cards_per_round"]["nullable"]
            and sched["max_cards_per_round"]["value"] == 15  # slow.toml 没写：2h 档默认
        )
        body = {"section": "risk", "changes": {"max_leverage": 4}, "password": PW}
        r = client.post("/api/settings", json=body, headers=HEADERS)
        assert r.status_code == 200, r.text
        assert r.json()["changed"] == [{"key": "max_leverage", "old": 5, "new": 4}]
        page = client.get("/api/settings").json()
        lev = next(i for i in page["risk"] if i["key"] == "max_leverage")
        assert lev["value"] == 4  # 设置页显示文件里的现值
        assert page["changes"][0]["key"] == "max_leverage"
        bad = {"section": "risk", "changes": {"max_leverage": 0}, "password": PW}
        r = client.post("/api/settings", json=bad, headers=HEADERS)
        assert r.status_code == 400 and "不合格" in r.json()["detail"]
        assert PW not in client.get("/api/settings").text


def test_read_only_panel_without_paths(tmp_path: Path) -> None:
    paths, _ = _setup(tmp_path)
    hb = HbReader("http://hb", "u", None, account_name="a", connector_name="c")
    app = create_app(load_risk_config(paths.risk), load_app_config(paths.app), hb)
    with TestClient(app, client=("127.0.0.1", 5000), base_url="http://localhost") as client:
        assert client.get("/api/settings").json()["writable"] is False
        body = {"section": "risk", "changes": {"max_leverage": 4}, "password": PW}
        h = {"X-Jiaoyi-Panel": "1", "Origin": "http://localhost"}
        assert client.post("/api/settings", json=body, headers=h).status_code == 400


def test_choices_and_nullable_fields(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    actions.update_config(paths, data, "fast_loop", {"atr_period_min": 30}, PW, NOW)
    actions.update_config(
        paths, data, "predictor", {"mode": "all_symbols", "size_policy": "adaptive"}, PW, NOW
    )
    app = load_app_config(paths.app)
    assert app.fast_loop.atr_period_min == 30
    assert app.predictor.mode == "all_symbols" and app.predictor.size_policy == "adaptive"
    assert 'mode = "all_symbols"' in paths.app.read_text(encoding="utf-8")
    for bad in ({"atr_period_min": 20}, {"atr_period_min": True}):
        with pytest.raises(ActionError, match="只能选"):
            actions.update_config(paths, data, "fast_loop", bad, PW, NOW)
    with pytest.raises(ActionError, match="只能选"):
        actions.update_config(paths, data, "predictor", {"model_size": "huge"}, PW, NOW)
    with pytest.raises(ActionError, match="不能空着"):
        actions.update_config(paths, data, "predictor", {"backfill_bars": None}, PW, NOW)


def test_slow_schedule_created_and_cleared(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    assert not paths.slow.exists()
    actions.update_config(paths, data, "schedule", {"max_cards_per_round": 8}, PW, NOW)
    assert load_slow_config(paths.slow).schedule.max_cards_per_round == 8
    assert not list(paths.slow.parent.glob("slow.toml.bak-*"))  # 原来没有文件，不留备份
    actions.update_config(paths, data, "schedule", {"max_cards_per_round": None}, PW, NOW)
    assert load_slow_config(paths.slow).schedule.max_cards_per_round == 15  # 清掉 = 回到 2h 档默认
    assert "max_cards_per_round" not in paths.slow.read_text(encoding="utf-8")
    with pytest.raises(ActionError, match="concurrency"):
        actions.update_config(paths, data, "schedule", {"concurrency": 20}, PW, NOW)
    rows = actions.recent_changes(data)
    assert rows[0]["key"] == "max_cards_per_round" and rows[0]["new"] is None


def test_screener_rules_edited_from_settings(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    with _client(paths) as client:
        page = client.get("/api/settings").json()
        scr = {i["key"]: i for i in page["screener"]}
        assert scr["min_range_24h_pct"]["recommended"] == 1.5
        assert scr["max_range_24h_pct"]["recommended"] == 30
        assert scr["max_spread_bps"]["nullable"] and scr["max_spread_bps"]["value"] is None
        assert all(i["editable"] for i in scr.values()) and "weight_oi" in scr
        assert page["screener_cautions"] == []
    actions.update_config(paths, data, "screener", {"min_range_24h_pct": 1.5}, PW, NOW)
    actions.update_config(paths, data, "screener", {"max_spread_bps": 50}, PW, NOW)
    slow = load_slow_config(paths.slow)
    assert slow.screener.min_range_24h_pct == 1.5 and slow.screener.max_spread_bps == 50
    assert "[screener]" in paths.slow.read_text(encoding="utf-8")
    with _client(paths) as client:
        page = client.get("/api/settings").json()
        assert any("价差" in c for c in page["screener_cautions"])  # 比硬上限宽：只提示
    # 数学上不成立的才拒：下限 ≥ 上限、权重全 0
    with pytest.raises(ActionError, match="波动下限"):
        actions.update_config(paths, data, "screener", {"max_range_24h_pct": 1}, PW, NOW)
    zero = {k: 0 for k in ("weight_volatility", "weight_volume_surge", "weight_funding")}
    actions.update_config(paths, data, "screener", zero, PW, NOW)
    with pytest.raises(ActionError, match="权重"):
        actions.update_config(
            paths, data, "screener", {"weight_oi": 0, "weight_liquidations": 0}, PW, NOW
        )
    actions.update_config(paths, data, "screener", {"max_spread_bps": None}, PW, NOW)
    assert load_slow_config(paths.slow).screener.max_spread_bps is None
    # 分档交易：[screener.tiers] 子表，选项只能 S / M / L；中部挑得比中部还多只提示
    actions.update_config(
        paths, data, "tiers", {"mid_size_max": "M", "mid_daily_pick": 20}, PW, NOW
    )
    tiers = load_slow_config(paths.slow).screener.tiers
    assert (tiers.mid_size_max, tiers.mid_daily_pick) == ("M", 20)
    assert "[screener.tiers]" in paths.slow.read_text(encoding="utf-8")
    with pytest.raises(ActionError, match="只能选"):
        actions.update_config(paths, data, "tiers", {"mid_size_max": "XL"}, PW, NOW)
    with _client(paths) as client:
        page = client.get("/api/settings").json()
        assert {i["key"] for i in page["tiers"]} >= {"enabled", "head", "mid", "mid_size_max"}
        assert page["tiers_cautions"] and not any("中间档" in x for x in page["screener_cautions"])


def test_pool_categories_read_slow_toml(tmp_path: Path) -> None:
    paths, _ = _setup(tmp_path)
    paths.slow.write_text("[screener.categories.us_tech]\nenabled = false\n", encoding="utf-8")
    with _client(paths) as client:
        cats = {x["category"]: x for x in client.get("/api/pool").json()["categories"]}
        assert (cats["us_tech"]["enabled"], cats["us_tech"]["enabled_from"]) == (False, "slow")
        paths.slow.write_text("[screener]\nweight_oi = -1\n", encoding="utf-8")
        page = client.get("/api/pool").json()
        assert page["slow_error"] and all(x["enabled"] for x in page["categories"])


def test_update_validates_with_profile_defaults(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    text = paths.app.read_text(encoding="utf-8").replace(
        'meeting_interval = "2h"', 'meeting_interval = "1h"'
    )
    paths.app.write_text(text, encoding="utf-8")
    # 示例里 predictor_enabled 等档位键没写：照样能改别的设置
    actions.update_config(paths, data, "app", {"pool_size": 30}, PW, NOW)
    # 默认的 max_stop_atr = 3.0 也参与校验：存得下的值重启时也启动得了
    with pytest.raises(ActionError, match=r"min_stop_atr|max_stop_atr"):
        actions.update_config(paths, data, "fast_loop", {"min_stop_atr": 3.5}, PW, NOW)
    load_app_config(paths.app)


def test_config_pending_uses_profile_for_slow(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    text = paths.app.read_text(encoding="utf-8").replace(
        'meeting_interval = "2h"', 'meeting_interval = "1h"'
    )
    paths.app.write_text(text, encoding="utf-8")
    risk, app = load_risk_config(paths.risk), load_app_config(paths.app)
    slow = load_slow_config(paths.slow, app.profile)  # 主循环就是这样读的
    write_runtime(data, config_fingerprint=config_fingerprint(risk, app, slow))
    assert actions.config_pending(paths, data) is False


def test_schedule_validates_with_profile_defaults(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    actions.update_config(paths, data, "schedule", {"refresh_lead_min": 45}, PW, NOW)  # 2h 卡
    text = paths.app.read_text(encoding="utf-8").replace(
        'meeting_interval = "2h"', 'meeting_interval = "1h"'
    )
    paths.app.write_text(text, encoding="utf-8")
    # 60m 档卡有效期 80 分钟：开会间隔 90 分钟存不下（启动时同样会被拒）
    with pytest.raises(ActionError, match="min_refresh_gap_min"):
        actions.update_config(paths, data, "schedule", {"min_refresh_gap_min": 90}, PW, NOW)
    actions.update_config(paths, data, "schedule", {"refresh_lead_min": 20}, PW, NOW)
    cfg = load_slow_config(paths.slow, "60m").schedule
    assert cfg.refresh_lead_min == 20 and cfg.card_ttl_min == 80


def test_profile_switch_and_pinned_keys(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    info = actions.profile_info(paths)
    assert info.current == "2h" and info.choices == ["15m", "30m", "1h", "2h", "6h", "12h", "24h"]
    assert info.paper_only == []
    assert "keeper_interval_s" not in info.pinned.get("app", {})  # 示例里注释掉了，跟档位走
    actions.update_config(paths, data, "app", {"profile": "6h"}, PW, NOW)  # 旧面板写 profile
    app = load_app_config(paths.app)
    assert app.profile == "6h" and app.fast_loop.time_limit_s == 14400  # 不随开会间隔变
    assert actions.profile_info(paths).current == "6h"
    # 网页上填了档位键 = 写死；清空 = 删掉这一行，回到档位默认
    actions.update_config(paths, data, "app", {"keeper_interval_s": 40}, PW, NOW)
    assert actions.profile_info(paths).pinned["app"] == {"keeper_interval_s": 30}
    actions.update_config(paths, data, "app", {"keeper_interval_s": None}, PW, NOW)
    assert "keeper_interval_s" not in actions.profile_info(paths).pinned.get("app", {})
    assert load_app_config(paths.app).keeper_interval_s == 30
    # 不是档位键、也不是可空的，不能清空
    with pytest.raises(ActionError, match="不能空着"):
        actions.update_config(paths, data, "app", {"pool_size": None}, PW, NOW)


def test_paper_only_hold_rejected_outside_paper(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    text = paths.app.read_text(encoding="utf-8").replace('mode = "paper"', 'mode = "testnet"')
    paths.app.write_text(text, encoding="utf-8")
    with pytest.raises(ActionError, match="paper"):
        actions.update_config(paths, data, "app", {"hold": "30m"}, PW, NOW)
    assert load_app_config(paths.app).hold == "4h"


def test_settings_page_shows_profile(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    actions.update_config(paths, data, "fast_loop", {"time_limit_s": 5000}, PW, NOW)
    with _client(paths) as client:
        page = client.get("/api/settings").json()
    assert page["profile"]["current"] == "2h"
    assert page["profile"]["pinned"]["fast_loop"] == {"time_limit_s": 14400}  # 写死 = 自定义持仓
    fl = {i["key"]: i for i in page["fast_loop"]}
    assert fl["time_limit_s"]["pinned"] and fl["time_limit_s"]["profile_default"] == 14400
    assert fl["min_p_take"]["profiled"] and fl["signal_max_age_s"]["profiled"] is False
    app_items = {i["key"]: i for i in page["app"]}
    assert app_items["meeting_interval"]["editable"]
    assert app_items["meeting_interval"]["choices"][2] == "1h"


def test_profile_switch_revalidates_slow_toml(tmp_path: Path) -> None:
    """审查 C4：写死 refresh_lead_min=50，1h（卡 80 分钟）合格、15m（卡 35 分钟）不合格。"""
    paths, data = _setup(tmp_path)
    paths.slow.write_text("[schedule]\nrefresh_lead_min = 50\n", encoding="utf-8")
    before = paths.app.read_text(encoding="utf-8")
    with pytest.raises(ActionError, match=r"slow\.toml"):
        actions.update_config(paths, data, "app", {"meeting_interval": "15m"}, PW, NOW)
    assert paths.app.read_text(encoding="utf-8") == before
    paths.slow.write_text("[schedule]\nrefresh_lead_min = 30\n", encoding="utf-8")
    actions.update_config(paths, data, "app", {"meeting_interval": "15m"}, PW, NOW)
    assert load_slow_config(paths.slow, "15m").schedule.refresh_lead_min == 30


def test_overview_flags_panel_older_than_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git pull 了没重启面板：总览提示“面板版本与代码不一致”，不让页面无声地卡在读取中。"""
    from jiaoyi.panel import server

    paths, _ = _setup(tmp_path)
    monkeypatch.setattr(server, "code_fingerprint", lambda: "boot")
    with _client(paths) as client:
        assert client.get("/api/overview").json()["control"]["panel_stale"] is False
        monkeypatch.setattr(server, "code_fingerprint", lambda: "pulled")
        assert client.get("/api/overview").json()["control"]["panel_stale"] is True


def test_code_fingerprint_changes_when_a_file_changes(tmp_path: Path) -> None:
    from jiaoyi.panel.server import code_fingerprint

    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "__pycache__").mkdir()
    before = code_fingerprint(tmp_path)
    (tmp_path / "__pycache__" / "a.pyc.py").write_text("ignored")
    assert code_fingerprint(tmp_path) == before
    (tmp_path / "a.py").write_text("x = 22\n")
    assert code_fingerprint(tmp_path) != before


def test_llm_only_through_setup_wizard_budget_limit_in_settings(tmp_path: Path) -> None:
    """军师团通道只在首次设置里改；花费段设置页只放日上限，留空跟档位走。"""
    paths, data = _setup(tmp_path)
    with pytest.raises(ActionError, match="首次设置"):
        actions.update_config(paths, data, "llm", {"provider": "openrouter"}, PW, NOW)
    assert set(actions.editable_fields("budget")) == {"daily_cost_limit_usd"}
    assert actions.editable_fields("budget")["daily_cost_limit_usd"].profiled
    with pytest.raises(ActionError):
        actions.update_config(paths, data, "budget", {"failed_meeting_cost_usd": 1}, PW, NOW)
    actions.update_config(paths, data, "budget", {"daily_cost_limit_usd": 35}, PW, NOW)
    raw = tomllib.loads(paths.slow.read_text(encoding="utf-8"))
    assert raw["budget"]["daily_cost_limit_usd"] == 35
    info = actions.profile_info(paths)
    assert "daily_cost_limit_usd" in info.pinned["budget"]  # 写死了：显示档位默认可恢复
    with _client(paths) as client:
        items = client.get("/api/settings").json()["budget"]
    assert [(i["key"], i["editable"], i["profiled"]) for i in items] == [
        ("daily_cost_limit_usd", True, True)
    ]
    actions.update_config(paths, data, "budget", {"daily_cost_limit_usd": None}, PW, NOW)
    raw = tomllib.loads(paths.slow.read_text(encoding="utf-8"))
    assert "daily_cost_limit_usd" not in raw.get("budget", {})


def test_overview_rereads_config_files_when_they_change(tmp_path: Path) -> None:
    """主程序按新 risk.toml 重启了，面板总览不能还按启动时读的旧上限算“离熔断”。"""
    paths, data = _setup(tmp_path)
    with _client(paths) as client:
        old = client.get("/api/overview").json()["kpis"]["halt_pct"]
        text = paths.risk.read_text(encoding="utf-8")
        paths.risk.write_text(
            set_toml_value(text, None, "daily_loss_halt_pct", 15.0), encoding="utf-8"
        )
        assert client.get("/api/overview").json()["kpis"]["halt_pct"] == 15.0 != old
        paths.risk.write_text("broken = [", encoding="utf-8")  # 读坏了沿用上一份
        assert client.get("/api/overview").json()["kpis"]["halt_pct"] == 15.0
        assert client.get("/api/overview").json()["control"]["started_at"] is None
        (data / "runtime.json").write_text('{"started_at": "2026-10-22T06:00:00+00:00"}')
        ctl = client.get("/api/overview").json()["control"]
        assert ctl["started_at"] == "2026-10-22T06:00:00+00:00"


def test_profile_pins_listed_and_cleared(tmp_path: Path) -> None:
    """网页上写死的档位键要列出来、能一键清掉。"""
    paths, data = _setup(tmp_path)
    actions.update_config(paths, data, "app", {"keeper_interval_s": 40}, PW, NOW)
    pins = {x.key: x for x in actions.profile_info(paths).pins["15m"]}
    assert (pins["keeper_interval_s"].value, pins["keeper_interval_s"].profile_default) == (
        40,
        30,
    )  # 管家频率跟最长持仓档

    with _client(paths) as client:
        bad = client.post(
            "/api/settings/clear-profile-pins", json={"password": "wrong"}, headers=HEADERS
        )
        assert bad.status_code == 400
        res = client.post(
            "/api/settings/clear-profile-pins", json={"password": PW}, headers=HEADERS
        )
        assert res.status_code == 200
        keys = {c["key"] for c in res.json()["changed"]}
        assert "keeper_interval_s" in keys
        again = client.post(
            "/api/settings/clear-profile-pins", json={"password": PW}, headers=HEADERS
        )
        assert again.status_code == 400  # 没有写死项了
    assert actions.profile_info(paths).pins["15m"] == []
    assert list(paths.app.parent.glob("app.toml.bak-*"))
    actions.update_config(paths, data, "app", {"profile": "6h"}, PW, NOW)
    assert load_app_config(paths.app).keeper_interval_s == 30  # 现在跟档位走


def test_fast_loop_pin_defaults_follow_hold(tmp_path: Path) -> None:
    """止损 / ATR 周期等写死项的“默认值”按最长持仓档算，跟主程序读配置的口径一样。"""
    paths, data = _setup(tmp_path)
    actions.update_config(paths, data, "app", {"hold": "30m"}, PW, NOW)
    actions.update_config(paths, data, "fast_loop", {"atr_period_min": 15}, PW, NOW)
    info = actions.profile_info(paths)
    assert info.pinned["fast_loop"]["atr_period_min"] == 5  # 30m 档用 5 分钟 K 线
    assert {x.key: x for x in info.pins["2h"]}["atr_period_min"].profile_default == 5


def test_hold_follows_meeting_unless_custom(tmp_path: Path) -> None:
    """最长持仓：hold 留空 = 跟开会档位的推荐搭配；写了 = 自定义，切开会间隔也不变。"""
    paths, data = _setup(tmp_path)
    info = actions.profile_info(paths)
    assert (info.hold, info.hold_custom) == ("4h", True)  # 示例文件写了 hold = "4h"
    assert info.hold_choices == ["30m", "1h", "2h", "4h", "8h", "12h", "24h"]
    assert info.hold_paper_only == ["30m"]
    assert info.hold_specs["4h"].time_limit_s == 14400
    assert info.hold_specs["4h"].atr_period_min == 15
    assert actions.editable_fields("app")["hold"].profiled  # 可以留空

    actions.update_config(paths, data, "app", {"hold": None}, PW, NOW)
    assert "hold" not in tomllib.loads(paths.app.read_text(encoding="utf-8"))
    info = actions.profile_info(paths)
    assert (info.current, info.hold, info.hold_custom) == ("2h", "4h", False)

    actions.update_config(paths, data, "app", {"meeting_interval": "2h"}, PW, NOW)
    info = actions.profile_info(paths)
    assert (info.hold, info.hold_custom) == ("4h", False)  # 2h 开会推荐 4h
    assert load_app_config(paths.app).fast_loop.time_limit_s == 14400
    # 切到 15m 时写死项的默认值按跟着变的 30m 持仓算
    pins = {x.key: x for x in actions.profile_info(paths).pins["15m"]}
    assert all(x.section != "fast_loop" for x in pins.values())

    actions.update_config(paths, data, "app", {"hold": "1h"}, PW, NOW)
    info = actions.profile_info(paths)
    assert (info.hold, info.hold_custom) == ("1h", True)


def test_schedule_event_triggers_and_fixed_time(tmp_path: Path) -> None:
    """临时会触发事件是多选、开会定点是 HH:MM 文字；都写成合法 TOML，留空回到档位默认。"""
    paths, data = _setup(tmp_path)
    shutil.copy(ROOT / "config" / "slow.example.toml", paths.slow)
    specs = actions.editable_fields("schedule")
    assert specs["event_triggers"].multi and specs["schedule_at"].pattern
    actions.update_config(
        paths,
        data,
        "schedule",
        {"event_triggers": ["move_since_card", "key_level_break", "move_since_card"]},
        PW,
        NOW,
    )
    actions.update_config(paths, data, "schedule", {"schedule_at": " 09:30 "}, PW, NOW)
    sched = tomllib.loads(paths.slow.read_text(encoding="utf-8"))["schedule"]
    assert sched["event_triggers"] == ["key_level_break", "move_since_card"]  # 按选项顺序、去重
    assert sched["schedule_at"] == "09:30"
    with pytest.raises(ActionError, match="格式不对"):
        actions.update_config(paths, data, "schedule", {"schedule_at": "25:00"}, PW, NOW)
    with pytest.raises(ActionError, match="没有这些选项"):
        actions.update_config(paths, data, "schedule", {"event_triggers": ["boom"]}, PW, NOW)
    actions.update_config(paths, data, "schedule", {"schedule_at": None}, PW, NOW)
    assert "schedule_at" not in tomllib.loads(paths.slow.read_text(encoding="utf-8"))["schedule"]


def test_switch_mode_goes_through_live_gate(tmp_path: Path) -> None:
    """面板切模式和命令行同一套门槛：实盘 = 确认词 + 密码，只在本机；离开实盘上锁；都发重启。"""
    paths, data = _setup(tmp_path)
    gate = data / "live_gate.json"
    with _client(paths) as lan:
        info = lan.get("/api/mode").json()
        assert info["mode"] == "paper" and not info["local"] and not info["armed"]
        assert any("真钱" in x for x in info["cautions"]["live"])
        live = {"mode": "live", "password": PW, "confirm_word": CONFIRM_WORD, "acknowledged": True}
        r = lan.post("/api/mode", json=live, headers=HEADERS)
        assert r.status_code == 403 and "本机" in r.json()["detail"]
        bad = {"mode": "testnet", "password": "wrong-pass"}
        r = lan.post("/api/mode", json=bad, headers=HEADERS)
        assert r.status_code == 400 and "密码不对" in r.json()["detail"]
        r = lan.post("/api/mode", json={"mode": "testnet", "password": PW}, headers=HEADERS)
        assert r.status_code == 200 and r.json()["new"] == "testnet"
    assert tomllib.loads(paths.app.read_text(encoding="utf-8"))["mode"] == "testnet"
    assert actions.command_status(data, r.json()["command_id"]) is None  # 重启指令已排队
    assert list((data / "panel_commands").glob("*.json"))
    with _client(paths, ip="127.0.0.1") as mac:
        r = mac.post("/api/mode", json={**live, "acknowledged": False}, headers=HEADERS)
        assert r.status_code == 400 and "勾" in r.json()["detail"]
        r = mac.post("/api/mode", json={**live, "confirm_word": "确认"}, headers=HEADERS)
        assert r.status_code == 400 and "确认词" in r.json()["detail"]
        r = mac.post("/api/mode", json=live, headers=HEADERS)
        assert r.status_code == 200 and r.json()["armed_at"]
        assert LiveGate(gate).armed
        r = mac.post("/api/mode", json=live, headers=HEADERS)
        assert r.status_code == 400 and "已解锁" in r.json()["detail"]
        r = mac.post("/api/mode", json={"mode": "paper"}, headers=HEADERS)  # 切回不要密码
        assert r.status_code == 200
    assert not LiveGate(gate).armed
    assert tomllib.loads(paths.app.read_text(encoding="utf-8"))["mode"] == "paper"


def test_settings_meaning_lines() -> None:
    """R1 / R2：每笔实际风险、同时满仓数、按本金推荐开会档和日上限。"""
    from jiaoyi.panel.meaning import build_meaning, recommended_meeting

    risk = load_risk_config(ROOT / "config" / "risk.example.toml").model_copy(
        update={"risk_per_trade_pct": 5.0, "daily_loss_halt_pct": 15.0}
    )
    app = load_app_config(ROOT / "config" / "app.example.toml")
    m = build_meaning(risk, app, "2h", 20.0, 5000.0)
    text = {x.label: x.text for x in m.lines}
    assert "2.5%" in text["每笔实际风险"]
    assert "= 2 笔" in text["同时最多几笔满仓"] and "S 档时 4 笔" in text["同时最多几笔满仓"]
    assert "12h 档" in text["按本金推荐"] and "$2.50" in text["按本金推荐"]
    assert recommended_meeting(1000) is None and recommended_meeting(100_000) == "1h"
    assert "拿不到本金" in build_meaning(risk, app, "2h", None, None).lines[-1].text


def test_gate_calibration_shown_next_to_thresholds(tmp_path: Path) -> None:
    """有校准推荐就挂在拍板门槛的推荐上（不自动套用）；推荐为空时显示原因。"""
    paths, data = _setup(tmp_path)
    hold = load_app_config(paths.app).hold
    with _client(paths) as client:
        page = client.get("/api/settings").json()
        assert "还没有拍板门槛校准结果" in page["gate_cautions"][0]
        (data / "gate_calibration").mkdir()
        cal = {
            "hold": hold, "kind": "all", "template_fp": "x", "model": "m", "samples": 412,
            "min_above": 150, "min_samples": 300, "computed_at": "2026-10-24T03:00:00+07:00",
            "recommended": {"p_take": 0.35, "p_genuine": 0.4, "max_p_toxic": 0.7},
            "table": [], "note": "",
        }  # fmt: skip
        (data / "gate_calibration" / f"{hold}.json").write_text(json.dumps(cal))
        page = client.get("/api/settings").json()
        fl = {i["key"]: i for i in page["fast_loop"]}
        assert (
            fl["min_p_take"]["recommended"] == 0.35
            and "412 个样本" in fl["min_p_take"]["recommended_why"]
        )
        assert fl["max_p_toxic"]["recommended"] == 0.7 and page["gate_cautions"] == []
        cal["recommended"], cal["note"] = None, "样本不足：只有 120 个"
        (data / "gate_calibration" / f"{hold}.json").write_text(json.dumps(cal))
        page = client.get("/api/settings").json()
        assert "样本不足" in page["gate_cautions"][0]


def test_profile_info_tier_defaults_for_tier_page(tmp_path: Path) -> None:
    """档位设置页：每档全部档位键的值、止损约多宽；预测员和花费段写了也算自定义。"""
    paths, data = _setup(tmp_path)
    info = actions.profile_info(paths)
    assert set(info.defaults) == set(info.choices)
    for tier in info.defaults.values():
        assert set(tier) == {"app", "fast_loop", "predictor", "schedule", "budget"}
        assert "funding_against_max_bp" in tier["fast_loop"]  # 档位没给的用程序默认，每档齐全
    assert info.defaults["24h"]["budget"]["daily_cost_limit_usd"] == 5
    assert info.defaults["15m"]["schedule"]["card_ttl_min"] == 35
    hold_24h = info.hold if info.hold_custom else info.hold_recommended["24h"]  # 自定义的不跟档
    assert info.defaults["24h"]["predictor"] == profiles.defaults("24h", "predictor", hold_24h)
    paper = load_app_config(paths.app).mode == "paper"
    if paper:
        assert info.defaults["2h"]["fast_loop"]["min_p_take"] == profiles.PAPER_GATE["min_p_take"]
    assert info.stop_pct == profiles.BTC_STOP_PCT and info.stop_pct["4h"] == 0.64
    actions.update_config(paths, data, "predictor", {"pred_len": 30}, PW, NOW)
    assert "pred_len" in actions.profile_info(paths).pinned["predictor"]
    actions.update_config(paths, data, "predictor", {"pred_len": None}, PW, NOW)  # 恢复跟档
    assert "predictor" not in actions.profile_info(paths).pinned
