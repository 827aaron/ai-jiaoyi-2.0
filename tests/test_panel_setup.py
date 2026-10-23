"""首次设置向导：只对本机开放、密钥只写不读、面板密码只能首次设、基本配置、自检。"""

from __future__ import annotations

import json
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.core.secrets import KeychainWriter, SecretError, check_secret_value
from jiaoyi.panel.hb import HbReader
from jiaoyi.panel.server import create_app
from jiaoyi.panel.setup import is_local
from tests.test_panel_actions import _setup

NOW = datetime(2026, 10, 22, 6, 30, tzinfo=UTC)
PW = "first-panel-pw"
LOCAL = {"X-Jiaoyi-Panel": "1", "Origin": "http://127.0.0.1:8090"}
LAN = {"X-Jiaoyi-Panel": "1", "Origin": "http://192.168.31.94:8090"}
SECRET = "sk-or-v1-abcdef0123456789abcdef"

ALL = {
    "OPENROUTER_API_KEY": SECRET,
    "BINANCE_TESTNET_API_KEY": "demoKEY123456789",
    "BINANCE_TESTNET_API_SECRET": "demoSECRET123456789",
    "TELEGRAM_BOT_TOKEN": "123456:ABCdefGhIJKlmnopQRstuvWXyz",
    "TELEGRAM_CHAT_ID": "5042319800",
    "HUMMINGBOT_API_PASSWORD": "hb-pass-123",
}


class FakeKeychain:
    def __init__(self, fail: str | None = None) -> None:
        self.values: dict[str, str] = {}
        self.fail = fail

    def set(self, name: str, value: str) -> None:
        if name == self.fail:
            raise SecretError(f"{name} 写入钥匙串失败")
        self.values[name] = value

    def get(self, name: str) -> str | None:
        return self.values.get(name)


def _hb_requests() -> tuple[HbReader, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(r: httpx.Request) -> httpx.Response:
        seen.append(r)
        if r.url.path == "/portfolio/state":
            return httpx.Response(200, json={"a": {"c": [{"token": "USDT", "units": 5000}]}})
        return httpx.Response(200, json={"data": []})

    hb = HbReader(
        "http://hb",
        "admin",
        None,
        account_name="a",
        connector_name="c",
        cache_s=0,
        transport=httpx.MockTransport(handler),
    )
    return hb, seen


def _client(
    tmp_path: Path,
    *,
    ip: str = "127.0.0.1",
    host: str = "127.0.0.1:8090",
    password: bool = False,
    keychain: FakeKeychain | None = None,
    hb: HbReader | None = None,
) -> tuple[TestClient, Path, Any]:
    paths, data = _setup(tmp_path, password=False)
    if password:
        from jiaoyi.execution.live_gate import LiveGate

        LiveGate(data / "live_gate.json").set_password(PW)
    kc = keychain if keychain is not None else FakeKeychain()
    if hb is None:
        hb, _ = _hb_requests()
    app = create_app(
        load_risk_config(paths.risk),
        load_app_config(paths.app),
        hb,
        clock=lambda: NOW,
        key_status={},
        config_paths=paths,
        secret_writer=kc.set,
        secret_reader=kc.get,
    )
    client = TestClient(app, client=(ip, 5000), base_url=f"http://{host}")
    return client, data, (paths, kc)


# ---------------------------------------------------------------- 只对本机


@pytest.mark.parametrize(
    ("ip", "host", "ok"),
    [
        ("127.0.0.1", "127.0.0.1:8090", True),
        ("127.0.0.1", "localhost:8090", True),
        ("::1", "[::1]:8090", True),
        ("::ffff:127.0.0.1", "localhost", True),
        ("127.0.0.1", "192.168.31.94:8090", False),  # 本机但用局域网地址打开
        ("192.168.31.20", "localhost:8090", False),  # 局域网来的，Host 伪造成 localhost
        ("192.168.31.20", "192.168.31.94:8090", False),
        ("127.0.0.1", "aarons-mac-mini.local:8090", False),
        (None, "localhost", False),
        ("127.0.0.1", None, False),
    ],
)
def test_is_local(ip: str | None, host: str | None, ok: bool) -> None:
    assert is_local(ip, host) is ok


def test_lan_cannot_write_anything(tmp_path: Path) -> None:
    client, data, (_, kc) = _client(tmp_path, ip="192.168.31.20", host="192.168.31.94:8090")
    with client:
        state = client.get("/api/setup").json()
        assert state["local"] is False and state["basic"] == {}
        r = client.post("/api/setup/password", json={"password": PW, "confirm": PW}, headers=LAN)
        assert r.status_code == 403 and "本机" in r.json()["detail"]
        assert not (data / "live_gate.json").exists()
        body = {"values": ALL, "password": PW}
        assert client.post("/api/setup/keys", json=body, headers=LAN).status_code == 403
        assert (
            client.post("/api/setup/check", json={"password": PW}, headers=LAN).status_code == 403
        )
        assert client.post("/api/setup/basic", json=body, headers=LAN).status_code == 403
        # 局域网打开首页不跳转（没配齐也照常看总览）
        assert client.get("/", follow_redirects=False).status_code == 200
    assert kc.values == {}


def test_local_posts_still_need_same_origin(tmp_path: Path) -> None:
    client, data, _ = _client(tmp_path)
    with client:
        body = {"password": PW, "confirm": PW}
        assert client.post("/api/setup/password", json=body).status_code == 403
        evil = {"X-Jiaoyi-Panel": "1", "Origin": "http://evil.example.com"}
        assert client.post("/api/setup/password", json=body, headers=evil).status_code == 403
    assert not (data / "live_gate.json").exists()


# ---------------------------------------------------------------- 流程


def test_full_wizard_flow(tmp_path: Path) -> None:
    hb, seen = _hb_requests()
    tg: list[httpx.Request] = []
    client, _data, (paths, kc) = _client(tmp_path, hb=hb)
    with client:
        # 没配齐：本机打开首页直接跳到 /setup
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/setup"
        assert client.get("/setup").status_code == 200
        state = client.get("/api/setup").json()
        assert state["local"] and not state["password_set"] and not state["done"]
        assert state["basic"]["profile"] == "2h" and state["keychain"] is True

        # 1. 面板密码（第一次不要旧密码）
        bad = {"password": PW, "confirm": PW + "x"}
        assert client.post("/api/setup/password", json=bad, headers=LOCAL).status_code == 400
        ok = {"password": PW, "confirm": PW}
        assert client.post("/api/setup/password", json=ok, headers=LOCAL).json()["password_set"]
        # 设过了就不能再从网页设（不然谁坐到 Mac 前都能改）
        again = {"password": "another-pass", "confirm": "another-pass"}
        r = client.post("/api/setup/password", json=again, headers=LOCAL)
        assert r.status_code == 400 and "set-panel-password" in r.json()["detail"]

        # 2. 密钥：要面板密码；返回只有名字和配没配
        r = client.post("/api/setup/keys", json={"values": ALL, "password": "wrong"}, headers=LOCAL)
        assert r.status_code == 400 and kc.values == {}
        r = client.post("/api/setup/keys", json={"values": ALL, "password": PW}, headers=LOCAL)
        assert r.status_code == 200, r.text
        assert sorted(r.json()["written"]) == sorted(ALL)
        assert all(v not in r.text for v in ALL.values())
        assert kc.values == ALL
        state = r.json()["state"]
        assert state["done"] and all(state["keys"][k] for k in ALL)
        # 设置页的“配没配”跟着变，不用重启
        settings = client.get("/api/settings").text
        assert SECRET not in settings
        # 首页不再跳转
        assert client.get("/", follow_redirects=False).status_code == 200

        # 3. 基本配置
        values = {
            "jev_provider": "openrouter",
            "llm_provider": "openrouter",
            "daily_cost_limit_usd": 8,
        }
        r = client.post("/api/setup/basic", json={"values": values, "password": PW}, headers=LOCAL)
        assert r.status_code == 200, r.text
        app = tomllib.loads(paths.app.read_text(encoding="utf-8"))
        slow = tomllib.loads(paths.slow.read_text(encoding="utf-8"))
        assert app["fast_loop"]["jev_provider"] == "openrouter"
        assert slow["llm"]["provider"] == "openrouter"
        assert slow["budget"]["daily_cost_limit_usd"] == 8.0
        r = client.post(
            "/api/setup/basic", json={"values": {"mode": "live"}, "password": PW}, headers=LOCAL
        )
        assert r.status_code == 400  # 模式不在向导里改（实盘走 go-live）

        # 4. 自检：hummingbot-api 用刚写的密码；Telegram 发测试消息
        import jiaoyi.panel.setup as setup_mod

        def fake_send(token: str, chat: str, alert: Any, transport: Any = None) -> bool:
            tg.append(httpx.Request("POST", f"https://tg/{chat}"))
            assert token == ALL["TELEGRAM_BOT_TOKEN"]
            return True

        orig = setup_mod.send_telegram
        setup_mod.send_telegram = fake_send
        try:
            r = client.post("/api/setup/check", json={"password": PW}, headers=LOCAL)
        finally:
            setup_mod.send_telegram = orig
        assert r.status_code == 200, r.text
        items = {i["name"]: i for i in r.json()}
        assert all(i["ok"] for i in items.values()), items
        assert "5,000.00" in items["hummingbot-api"]["detail"]
        assert ALL["TELEGRAM_BOT_TOKEN"] not in r.text
        assert len(tg) == 1
        auth = httpx.BasicAuth("admin", ALL["HUMMINGBOT_API_PASSWORD"])
        expected = next(auth.auth_flow(httpx.Request("GET", "http://hb"))).headers["Authorization"]
        assert seen and seen[-1].headers["Authorization"] == expected


def test_keys_are_validated_before_any_write(tmp_path: Path) -> None:
    client, _, (_, kc) = _client(tmp_path, password=True)
    with client:
        bad = {**ALL, "TELEGRAM_CHAT_ID": "not-a-number"}
        r = client.post("/api/setup/keys", json={"values": bad, "password": PW}, headers=LOCAL)
        assert r.status_code == 400 and kc.values == {}
        quoted = {"OPENROUTER_API_KEY": 'sk-or "x'}
        r = client.post("/api/setup/keys", json={"values": quoted, "password": PW}, headers=LOCAL)
        assert r.status_code == 400 and 'sk-or "x' not in r.text
        unknown = {"AWS_SECRET": "x" * 20}
        r = client.post("/api/setup/keys", json={"values": unknown, "password": PW}, headers=LOCAL)
        assert r.status_code == 400
        empty = {k: " " for k in ALL}
        r = client.post("/api/setup/keys", json={"values": empty, "password": PW}, headers=LOCAL)
        assert r.status_code == 400 and kc.values == {}
        # 空着的项 = 不改；其余照写，前后空白去掉
        some = {"OPENROUTER_API_KEY": f"  {SECRET}\n", "BINANCE_API_KEY": ""}
        r = client.post("/api/setup/keys", json={"values": some, "password": PW}, headers=LOCAL)
        assert r.json()["written"] == ["OPENROUTER_API_KEY"] and kc.values == {
            "OPENROUTER_API_KEY": SECRET
        }


def test_keys_need_panel_password_first(tmp_path: Path) -> None:
    client, _, (_, kc) = _client(tmp_path)
    with client:
        r = client.post("/api/setup/keys", json={"values": ALL, "password": PW}, headers=LOCAL)
        assert r.status_code == 400 and "面板密码" in r.json()["detail"]
    assert kc.values == {}


def test_keychain_failure_reports_partial_write(tmp_path: Path) -> None:
    client, _, (_, kc) = _client(
        tmp_path, password=True, keychain=FakeKeychain("TELEGRAM_BOT_TOKEN")
    )
    with client:
        r = client.post("/api/setup/keys", json={"values": ALL, "password": PW}, headers=LOCAL)
    assert r.status_code == 400
    assert "OPENROUTER_API_KEY" in r.json()["detail"] and SECRET not in r.text
    assert "TELEGRAM_BOT_TOKEN" not in kc.values


def test_no_keychain_means_read_only(tmp_path: Path) -> None:
    paths, _data = _setup(tmp_path)
    hb, _ = _hb_requests()
    app = create_app(
        load_risk_config(paths.risk), load_app_config(paths.app), hb, config_paths=paths
    )
    with TestClient(app, client=("127.0.0.1", 1), base_url="http://127.0.0.1:8090") as client:
        assert client.get("/api/setup").json()["keychain"] is False
        body = {"values": ALL, "password": "correct horse"}
        r = client.post("/api/setup/keys", json=body, headers=LOCAL)
        assert r.status_code == 400 and "setup-keys" in r.json()["detail"]


def test_self_check_needs_password_and_reports_missing(tmp_path: Path) -> None:
    client, _, _ = _client(tmp_path, password=True)
    with client:
        r = client.post("/api/setup/check", json={"password": "wrong"}, headers=LOCAL)
        assert r.status_code == 400
        r = client.post("/api/setup/check", json={"password": PW}, headers=LOCAL)
        items = {i["name"]: i for i in r.json()}
        assert items["配置文件"]["ok"]
        assert not items["hummingbot-api"]["ok"]  # 还没配密码
        assert not items["Telegram"]["ok"] and "还没配" in items["Telegram"]["detail"]


# ---------------------------------------------------------------- 钥匙串写入


def test_check_secret_value() -> None:
    assert check_secret_value("TELEGRAM_CHAT_ID", " -100123 ") == "-100123"
    for name, bad in [
        ("TELEGRAM_CHAT_ID", "12a"),
        ("OPENROUTER_API_KEY", "two words"),
        ("OPENROUTER_API_KEY", 'a"b'),
        ("OPENROUTER_API_KEY", "a\\b"),
        ("OPENROUTER_API_KEY", "x" * 513),
        ("NOT_A_KEY", "abc"),
    ]:
        with pytest.raises(SecretError):
            check_secret_value(name, bad)


def test_keychain_writer_uses_stdin_and_reads_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/security")
    calls: list[tuple[list[str], str]] = []
    stored: dict[str, str] = {}

    def run(args: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append((args, kw["input"]))
        stored["OPENROUTER_API_KEY"] = SECRET
        return subprocess.CompletedProcess(args, 0, "", "")

    KeychainWriter(run, stored.get).set("OPENROUTER_API_KEY", SECRET)
    args, stdin = calls[0]
    assert args == ["security", "-i"] and SECRET not in json.dumps(args)  # 值不进进程参数
    assert stdin == f'add-generic-password -U -s jiaoyi -a OPENROUTER_API_KEY -w "{SECRET}"\n'

    with pytest.raises(SecretError) as e:  # 退出码 0 但读回来不对，也算失败
        KeychainWriter(run, lambda _: "something-else").set("OPENROUTER_API_KEY", SECRET)
    assert SECRET not in str(e.value)


def test_key_needs_follow_mode_and_providers() -> None:
    from jiaoyi.core.secrets import KNOWN_KEYS, key_needs

    paper = key_needs("paper", "openrouter", "openrouter")
    assert set(paper) == set(KNOWN_KEYS)
    assert paper["OPENROUTER_API_KEY"] == "required"
    assert paper["JEV_API_KEY"] == paper["DEEPSEEK_API_KEY"] == "unused"
    assert paper["BINANCE_API_KEY"] == "unused" and paper["BINANCE_TESTNET_API_KEY"] == "required"
    assert paper["FRED_API_KEY"] == "optional"
    live = key_needs("live", "typesafe", "deepseek")
    assert live["BINANCE_API_SECRET"] == "required" and live["BINANCE_TESTNET_API_KEY"] == "unused"
    assert live["JEV_API_KEY"] == live["DEEPSEEK_API_KEY"] == "required"
    assert live["OPENROUTER_API_KEY"] == "unused"
