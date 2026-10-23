from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

import pytest

from jiaoyi import app as app_mod
from jiaoyi import contracts as c
from jiaoyi.app import App, Modules, is_stub
from jiaoyi.cli import main
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.interfaces import Predictor
from jiaoyi.market.binance_ws import BinanceFuturesFeed
from jiaoyi.predictor import KronosForecaster
from jiaoyi.predictor.stub import StubPredictor
from jiaoyi.slow.commander import BattleCardCommander
from jiaoyi.slow.news import NewsHub
from jiaoyi.slow.screener import CodeSymbolScreener
from jiaoyi.slow.stub import StubBattleCardProvider, StubSymbolScreener
from tests.conftest import ROOT

RISK = ROOT / "config" / "risk.example.toml"
APP = ROOT / "config" / "app.example.toml"


def _app(tmp_path: Path, now: datetime, **overrides: object) -> App:
    cfg = load_app_config(APP).model_copy(update={"data_dir": str(tmp_path), **overrides})
    return App(load_risk_config(RISK), cfg, clock=lambda: now, offline=True)


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
    assert status[c.Role.EXECUTOR].health == c.Health.DISABLED  # 桩补的
    assert "桩" in status[c.Role.EXECUTOR].headline
    assert status[c.Role.RISK_GUARD].health == c.Health.OK  # 真保镖，app 代发心跳
    assert "predictor" not in app.stub_fields()
    assert "risk" not in app.stub_fields()
    assert "executor" in app.stub_fields()
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


def test_cli_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    app_toml = tmp_path / "app.toml"
    app_toml.write_text(
        APP.read_text(encoding="utf-8").replace('data_dir = "data"', f'data_dir = "{tmp_path}"'),
        encoding="utf-8",
    )
    assert main(["status", "--risk", str(RISK), "--app", str(app_toml)]) == 0


class _Secrets:
    def __init__(self, **values: str) -> None:
        self._v = values

    def get(self, name: str) -> str | None:
        return self._v.get(name)


def _online(tmp_path: Path, now: datetime, secrets: _Secrets) -> App:
    cfg = load_app_config(APP).model_copy(update={"data_dir": str(tmp_path)})
    return App(load_risk_config(RISK), cfg, clock=lambda: now, secrets=secrets)


async def test_slow_loop_without_key_uses_stub_cards(tmp_path: Path, now: datetime) -> None:
    app = _online(tmp_path, now, _Secrets())
    assert isinstance(app.m.screener, CodeSymbolScreener)  # 选币不需要 key
    assert isinstance(app.m.cards, StubBattleCardProvider)
    assert app.m.cards.current(now) is None
    assert app.news is None
    await app.stop()  # 没启动也要关掉 httpx 连接


async def test_slow_loop_key_but_no_tradingagents_uses_stub(
    tmp_path: Path, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_mod.importlib.util, "find_spec", lambda name: None)
    app = _online(tmp_path, now, _Secrets(DEEPSEEK_API_KEY="x" * 16))
    assert isinstance(app.m.cards, StubBattleCardProvider)
    await app.stop()


async def test_slow_loop_with_key_uses_commander(
    tmp_path: Path, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, *args: object, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(app_mod.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(app_mod, "TradingAgentsEngine", FakeEngine)
    app = _online(tmp_path, now, _Secrets(DEEPSEEK_API_KEY="x" * 16))
    assert isinstance(app.m.cards, BattleCardCommander)
    assert isinstance(app.news, NewsHub)  # 真军师团才拉新闻
    assert app.m.cards.current(now) is None  # 还没开会就没有卡
    # 强平统计：行情层一份，选币和军师团共用
    assert isinstance(app.m.feed, BinanceFuturesFeed)
    assert seen["liquidations"] is app.m.feed.liquidations
    assert isinstance(app.m.screener, CodeSymbolScreener)
    assert app.m.screener._source._liq is app.m.feed.liquidations  # type: ignore[attr-defined]
    closers = list(app._closers)
    assert app.news.aclose in closers  # 退出时关新闻源连接
    await app.stop()


async def test_slow_config_and_brief_reach_commander_and_engine(
    tmp_path: Path, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from jiaoyi.slow.brief import MarketBriefService
    from jiaoyi.slow.settings import SlowConfig

    seen: dict[str, object] = {}

    class FakeEngine:
        def __init__(self, *args: object, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(app_mod.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(app_mod, "TradingAgentsEngine", FakeEngine)
    slow = SlowConfig.model_validate({"schedule": {"concurrency": 2, "card_ttl_hours": 3}})
    cfg = load_app_config(APP).model_copy(update={"data_dir": str(tmp_path)})
    app = App(
        load_risk_config(RISK),
        cfg,
        clock=lambda: now,
        secrets=_Secrets(DEEPSEEK_API_KEY="x" * 16),
        slow_cfg=slow,
    )
    cards = app.m.cards
    assert isinstance(cards, BattleCardCommander)
    brief = seen["brief"]
    assert isinstance(brief, MarketBriefService) and cards._brief is brief  # 一份简报两边共用
    assert seen["settings"] == slow.engine_settings()
    assert cards._ttl == timedelta(hours=3)
    assert cards._journal is not None
    assert cards._journal.path == tmp_path / "card_review" / "cards.jsonl"  # 复盘流水在 data_dir 下
    assert brief.aclose in list(app._closers)
    await app.stop()


def test_check_config_rejects_bad_slow_toml(tmp_path: Path) -> None:
    bad = tmp_path / "slow.toml"
    bad.write_text("[schedule]\nconcurrency = 99\n", encoding="utf-8")
    args = ["check-config", "--risk", str(RISK), "--app", str(APP), "--slow", str(bad)]
    assert main(args) == 2
    missing = tmp_path / "nope.toml"  # 没有文件 = 默认值
    assert (
        main(["check-config", "--risk", str(RISK), "--app", str(APP), "--slow", str(missing)]) == 0
    )


async def test_screen_failure_does_not_block_start(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)

    class Broken(StubSymbolScreener):
        async def screen(self, now: datetime) -> c.SymbolPool:
            raise RuntimeError("币安接口超时")

    app.m = dataclasses.replace(app.m, screener=Broken())
    await app.start()
    assert app.started and app.pool is None
    await app.stop()


async def test_execution_without_keys_falls_back_to_stubs(tmp_path: Path, now: datetime) -> None:
    from jiaoyi.alerting.log import LogAlerter

    app = _online(tmp_path, now, _Secrets())
    assert is_stub(app.m.executor) and is_stub(app.m.reconciler)
    assert isinstance(app.m.alerter, LogAlerter)
    await app.stop()


async def test_execution_with_testnet_keys_is_real(tmp_path: Path, now: datetime) -> None:
    from jiaoyi.alerting.telegram import TelegramAlerter
    from jiaoyi.execution.hb_executor import HbExecutorAdapter
    from jiaoyi.execution.lot_rules import ExchangeInfoLotRules
    from jiaoyi.execution.reconciler import ThreeWayReconciler

    keys = {
        "HUMMINGBOT_API_PASSWORD": "x" * 8,
        "BINANCE_TESTNET_API_KEY": "x" * 8,
        "BINANCE_TESTNET_API_SECRET": "x" * 8,
        "TELEGRAM_BOT_TOKEN": "x" * 8,
        "TELEGRAM_CHAT_ID": "1",
    }
    app = _online(tmp_path, now, _Secrets(**keys))
    assert isinstance(app.m.executor, HbExecutorAdapter)
    assert isinstance(app.m.reconciler, ThreeWayReconciler)
    assert isinstance(app.m.alerter, TelegramAlerter)
    assert isinstance(app.lot_rules, ExchangeInfoLotRules)
    await app.stop()


async def test_start_refreshes_lot_rules(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    calls: list[int] = []

    class Lots:
        async def refresh(self) -> bool:
            calls.append(1)
            return False  # 失败也照常启动，保镖拿不到精度就拒单

        def lot_rules(self, symbol: str) -> None:
            return None

    app.lot_rules = Lots()
    await app.start()
    assert calls == [1] and app.started
    await app.stop()


def test_live_without_unlock_refuses_to_build(tmp_path: Path, now: datetime) -> None:
    from jiaoyi.execution.live_gate import LiveGateError

    update = {"data_dir": str(tmp_path), "mode": c.RunMode.LIVE}
    cfg = load_app_config(APP).model_copy(update=update)
    with pytest.raises(LiveGateError):
        App(load_risk_config(RISK), cfg, clock=lambda: now, secrets=_Secrets())
