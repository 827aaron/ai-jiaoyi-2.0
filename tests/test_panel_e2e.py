"""审查 A6：面板和主循环两个“进程”只通过 data 目录对话。

真的面板（FastAPI）+ 真的 App/Runner（离线假装配），共用一份配置和 data 目录：
面板发指令 → 主循环 command_tick 执行 → paused.json / done/ / runtime.json 变化 → 面板读到。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from jiaoyi.app import App
from jiaoyi.core.commands import PAUSED_FILE, read_paused
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.loop import Runner
from jiaoyi.panel.hb import HbReader
from jiaoyi.panel.server import create_app
from tests import fastloop_helpers as h
from tests.test_loop import FakeCards, FakeExecutor, FakeGate, FakeRisk
from tests.test_panel_actions import HEADERS, PW, _setup

NOW = datetime(2026, 10, 22, 6, 30, tzinfo=UTC)


def _run_app(paths_app: Path, paths_risk: Path) -> tuple[App, FakeExecutor]:
    app = App(
        load_risk_config(paths_risk), load_app_config(paths_app), clock=lambda: NOW, offline=True
    )
    ex = FakeExecutor()
    app.m = dataclasses.replace(
        app.m, executor=ex, gate=FakeGate(), risk=FakeRisk(True), cards=FakeCards(h.card(NOW))
    )
    return app, ex


async def test_panel_and_run_talk_through_data_dir(tmp_path: Path) -> None:
    paths, data = _setup(tmp_path)
    app, ex = _run_app(paths.app, paths.risk)
    await app.start()  # 写 runtime.json（带配置指纹）
    runner = Runner(app)
    hb = HbReader(
        "http://hb",
        "u",
        None,
        account_name="a",
        connector_name="c",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    panel = create_app(
        load_risk_config(paths.risk),
        load_app_config(paths.app),
        hb,
        clock=lambda: NOW,
        config_paths=paths,
    )
    with TestClient(panel, client=("192.168.1.20", 1), base_url="http://localhost:8090") as client:

        def control() -> dict[str, object]:
            ctl: dict[str, object] = client.get("/api/overview").json()["control"]
            return ctl

        assert control()["paused"] is False and control()["config_pending"] is False

        # 面板暂停 → 主循环执行 → paused.json 落盘、面板看到结果和横幅
        r = client.post("/api/command", json={"kind": "pause"}, headers=HEADERS)
        cid = r.json()["id"]
        assert client.get(f"/api/command/{cid}").json()["status"] == "pending"
        await runner.command_tick()
        assert client.get(f"/api/command/{cid}").json()["status"] == "done"
        assert (data / PAUSED_FILE).exists() and control()["paused"] is True
        await runner.handle_signal(h.signal(NOW))
        assert ex.opened == []  # 暂停中不开新仓

        # 面板改配置 → 主循环没重启前提示“未生效”
        body = {"section": "app", "changes": {"signal_dedup_s": 120}, "password": PW}
        assert client.post("/api/settings", json=body, headers=HEADERS).status_code == 200
        assert control()["config_pending"] is True

        # 恢复要密码；主循环执行后 paused.json 消失
        r = client.post("/api/command", json={"kind": "resume"}, headers=HEADERS)
        assert r.status_code == 400
        r = client.post("/api/command", json={"kind": "resume", "password": PW}, headers=HEADERS)
        await runner.command_tick()
        assert client.get(f"/api/command/{r.json()['id']}").json()["status"] == "done"
        assert read_paused(data) is None and control()["paused"] is False
        await runner.handle_signal(h.signal(NOW))
        assert ex.opened == ["int-0001"]
        await app.stop()

        # 主循环按新配置重启 → runtime.json 指纹更新，提示消失
        app2, _ = _run_app(paths.app, paths.risk)
        await app2.start()
        assert app2.app_cfg.signal_dedup_s == 120
        assert control()["config_pending"] is False
        await app2.stop()
