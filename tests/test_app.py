from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

from jiaoyi import contracts as c
from jiaoyi.app import App, Modules, is_stub
from jiaoyi.cli import main
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.interfaces import Predictor
from jiaoyi.predictor import KronosForecaster
from jiaoyi.predictor.stub import StubPredictor
from tests.conftest import ROOT

RISK = ROOT / "config" / "risk.example.toml"
APP = ROOT / "config" / "app.example.toml"


def _app(tmp_path: Path, now: datetime, **overrides: object) -> App:
    cfg = load_app_config(APP).model_copy(update={"data_dir": str(tmp_path), **overrides})
    return App(load_risk_config(RISK), cfg, clock=lambda: now)


def test_predictor_is_wired_and_follows_switch(tmp_path: Path, now: datetime) -> None:
    on = _app(tmp_path / "on", now, predictor_enabled=True)
    off = _app(tmp_path / "off", now, predictor_enabled=False)
    for app in (on, off):
        assert isinstance(app.m.predictor, KronosForecaster)
        assert isinstance(app.m.predictor, Predictor)
    assert on.m.predictor.enabled
    assert not off.m.predictor.enabled
    assert off.m.predictor.latest("BTCUSDT", now) is None


async def test_start_stop_and_status(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now, predictor_enabled=False)
    await app.start()
    assert app.started
    status = app.status()
    assert status[c.Role.PREDICTOR].health == c.Health.DISABLED  # 预测员自己上报
    assert status[c.Role.RISK_GUARD].health == c.Health.DISABLED  # 桩补的
    assert "桩" in status[c.Role.RISK_GUARD].headline
    assert "predictor" not in app.stub_fields()
    assert "risk" in app.stub_fields()
    await app.stop()
    assert not app.started


def test_real_module_without_heartbeat_shows_starting(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)

    class RealPredictor(StubPredictor):
        pass

    RealPredictor.__module__ = "jiaoyi.predictor.fake"
    app.m = dataclasses.replace(app.m, predictor=RealPredictor())
    assert not is_stub(app.m.predictor)
    assert app.status()[c.Role.PREDICTOR].health == c.Health.STARTING


def test_modules_cover_every_role_field() -> None:
    from jiaoyi.app import ROLE_OF

    assert {f.name for f in dataclasses.fields(Modules)} == set(ROLE_OF)


def test_cli_status(tmp_path: Path, capsys: object) -> None:
    app_toml = tmp_path / "app.toml"
    app_toml.write_text(
        APP.read_text(encoding="utf-8").replace('data_dir = "data"', f'data_dir = "{tmp_path}"'),
        encoding="utf-8",
    )
    assert main(["status", "--risk", str(RISK), "--app", str(app_toml)]) == 0
