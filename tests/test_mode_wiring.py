"""paper / testnet / live 三种模式的真实装配：connector、地址、key，以及 live 未解锁必须拒绝。

走的是 app 里真实的 `_build_execution`（连同 `execution.factory.build_execution`），
只把网络两端换成假的：hummingbot-api 换成 FakeHbApi（记下连接参数），
ccxt 换成 FakeExchange（记下 key 和模式）。
再用同样的参数造一个真的 ccxt 对象（不联网），核对它指向的地址。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.app import _build_execution
from jiaoyi.contracts import OrderIntent, RunMode
from jiaoyi.core.commands import config_fingerprint
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution import factory
from jiaoyi.execution.exchange import CcxtBinanceFutures, ExchangeBracket, ExchangeError
from jiaoyi.execution.factory import MissingSecret, SimEnvironmentMismatch
from jiaoyi.execution.live_gate import CONFIRM_WORD, LiveGate, LiveGateError
from jiaoyi.execution.stub import StubExecutorAdapter
from tests.conftest import ROOT, load_example
from tests.fakes import FakeExchange, FakeHbApi

RISK = load_risk_config(ROOT / "config" / "risk.example.toml")
APP = load_app_config(ROOT / "config" / "app.example.toml")

SECRETS = {
    "HUMMINGBOT_API_PASSWORD": "hb-pass",
    "BINANCE_TESTNET_API_KEY": "TESTNET-KEY",
    "BINANCE_TESTNET_API_SECRET": "TESTNET-SECRET",
    "BINANCE_API_KEY": "MAINNET-KEY",
    "BINANCE_API_SECRET": "MAINNET-SECRET",
}
PANEL_PASSWORD = "panel-password-1"


class _Secrets:
    def __init__(self, values: dict[str, str]) -> None:
        self._v = values

    def get(self, name: str) -> str | None:
        return self._v.get(name)


class _WiringExchange(FakeExchange):
    """仅本文件的初始空账户证据；模式测试不把未知订单伪造成完整保护。"""

    async def open_brackets(self, symbol: str | None = None) -> list[ExchangeBracket]:
        # 这个装配假件从未提交普通条件单；不复用生产端点或全局fake的兼容放行。
        return []

    async def assert_entry_clear(self, symbol: str) -> None:
        if self.stops or self.entry_orders or self.close_submissions:
            raise ExchangeError("模式接线假件已非明确空账户，禁止继续新开")


@dataclass
class Spy:
    hb_calls: list[tuple[str, str, str]] = field(default_factory=list)
    ccxt_calls: list[dict[str, Any]] = field(default_factory=list)
    api: FakeHbApi = field(default_factory=FakeHbApi)
    exchange: FakeExchange = field(default_factory=_WiringExchange)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Spy:
    s = Spy()

    def fake_hb(url: str, username: str, password: str, **_: Any) -> FakeHbApi:
        s.hb_calls.append((url, username, password))
        return s.api

    def fake_ccxt(
        api_key: str, api_secret: str, *, testnet: bool, demo: bool = True, **_: Any
    ) -> FakeExchange:
        s.ccxt_calls.append(
            {"key": api_key, "secret": api_secret, "testnet": testnet, "demo": demo}
        )
        return s.exchange

    monkeypatch.setattr(factory, "HttpHummingbotApi", fake_hb)
    monkeypatch.setattr(factory, "CcxtBinanceFutures", fake_ccxt)
    return s


def _cfg(tmp_path: Path, mode: RunMode, **over: Any) -> Any:
    return APP.model_copy(update={"mode": mode.value, "data_dir": str(tmp_path)} | over)


def _build(
    tmp_path: Path, mode: RunMode, secrets: dict[str, str] | None = None, risk: Any = RISK
) -> Any:
    return _build_cfg(_cfg(tmp_path, mode), secrets, risk, tmp_path)


def _build_cfg(app_cfg: Any, secrets: dict[str, str] | None, risk: Any, tmp_path: Path) -> Any:
    closers: list[Any] = []
    checks: list[Any] = []
    out = _build_execution(
        risk,
        app_cfg,
        _Secrets(SECRETS if secrets is None else secrets),
        StubAlerter(),
        lambda: __import__("datetime").datetime.now(__import__("datetime").UTC),
        closers,
        JsonlDecisionLog(tmp_path),
        checks,
    )
    return out, closers, checks


def _mark_sim(tmp_path: Path, env: str = "demo") -> None:
    (tmp_path / factory.HB_SIM_MARKER).write_text(env)


def _unlock(tmp_path: Path, risk: Any = RISK) -> None:
    gate = LiveGate(tmp_path / "live_gate.json")
    gate.set_password(PANEL_PASSWORD)
    gate.unlock(CONFIRM_WORD, PANEL_PASSWORD, config_fingerprint(risk))


def _fapi(call: dict[str, Any]) -> str:
    real = CcxtBinanceFutures(
        call["key"], call["secret"], testnet=call["testnet"], demo=call["demo"]
    )
    return str(real._ex.urls["api"]["fapiPublic"])


async def _open_connector(spy: Spy, adapter: Any) -> str:
    original = spy.api.create_executor

    async def create_and_fill(cfg: dict[str, Any], acct: str, marker: str) -> dict[str, Any]:
        out = await original(cfg, acct, marker)
        spy.api.fill(out["executor_id"], 68044.0)
        return out

    spy.api.create_executor = create_and_fill  # type: ignore[method-assign,assignment]
    await adapter.open(OrderIntent.model_validate(load_example("order_intent")))
    return str(spy.api.created[0]["connector_name"])


@pytest.mark.parametrize("mode", [RunMode.PAPER, RunMode.TESTNET])
async def test_paper_and_testnet_use_demo_connector_and_testnet_key(
    tmp_path: Path, spy: Spy, mode: RunMode
) -> None:
    _mark_sim(tmp_path)
    (adapter, *_), closers, checks = _build(tmp_path, mode)
    assert adapter._protection is not None  # 本轮仅Demo新仓启用交易所托管。

    assert spy.hb_calls == [("http://127.0.0.1:8000", "admin", "hb-pass")]
    (call,) = spy.ccxt_calls
    assert (call["key"], call["secret"]) == ("TESTNET-KEY", "TESTNET-SECRET")
    assert call["testnet"] is True and call["demo"] is True
    assert "demo-fapi.binance.com" in _fapi(call)
    assert await _open_connector(spy, adapter) == "binance_perpetual_testnet"
    enrolled = next(iter(adapter._registry.data.positions.values())).frozen_peak_exit
    assert enrolled is not None and enrolled.after_s == 3600
    assert len(closers) == 1 and len(checks) == 1  # 关闭与启动时同账户校验都登记了


async def test_live_uses_mainnet_connector_url_and_mainnet_key(tmp_path: Path, spy: Spy) -> None:
    _unlock(tmp_path)
    (adapter, *_), _, _ = _build(tmp_path, RunMode.LIVE)
    assert adapter._protection is None  # LIVE保留原管理，不能随本轮自动切换。

    (call,) = spy.ccxt_calls
    assert (call["key"], call["secret"]) == ("MAINNET-KEY", "MAINNET-SECRET")
    assert call["testnet"] is False
    url = _fapi(call)
    assert "fapi.binance.com" in url and "demo" not in url and "testnet" not in url
    assert await _open_connector(spy, adapter) == "binance_perpetual"
    assert next(iter(adapter._registry.data.positions.values())).frozen_peak_exit is None


async def test_legacy_testnet_environment_is_honoured(tmp_path: Path, spy: Spy) -> None:
    _mark_sim(tmp_path, "legacy_testnet")
    app_cfg = _cfg(tmp_path, RunMode.TESTNET, sim_environment="legacy_testnet")
    (adapter, *_), _, _ = _build_cfg(app_cfg, None, RISK, tmp_path)
    assert adapter._protection is None  # legacy testnet同样保留原管理。

    (call,) = spy.ccxt_calls
    assert call["demo"] is False
    assert "testnet.binancefuture.com" in _fapi(call)
    assert await _open_connector(spy, adapter) == "binance_perpetual_testnet"
    assert next(iter(adapter._registry.data.positions.values())).frozen_peak_exit is None


def test_live_not_unlocked_is_refused_and_connects_to_nothing(tmp_path: Path, spy: Spy) -> None:
    with pytest.raises(LiveGateError):
        _build(tmp_path, RunMode.LIVE)
    assert spy.hb_calls == [] and spy.ccxt_calls == []


def test_live_unlock_is_bound_to_the_risk_file(tmp_path: Path, spy: Spy) -> None:
    _unlock(tmp_path)
    changed = RISK.model_copy(update={"max_leverage": RISK.max_leverage - 1})
    with pytest.raises(LiveGateError):
        _build(tmp_path, RunMode.LIVE, risk=changed)
    assert spy.hb_calls == [] and spy.ccxt_calls == []


def test_live_never_falls_back_to_testnet_keys(tmp_path: Path, spy: Spy) -> None:
    _unlock(tmp_path)
    only_testnet = {
        k: v for k, v in SECRETS.items() if k.startswith(("BINANCE_TESTNET", "HUMMING"))
    }
    with pytest.raises(MissingSecret):
        _build(tmp_path, RunMode.LIVE, secrets=only_testnet)
    assert spy.ccxt_calls == []


@pytest.mark.parametrize("mode", [RunMode.PAPER, RunMode.TESTNET])
def test_simulated_modes_never_use_mainnet_keys(tmp_path: Path, spy: Spy, mode: RunMode) -> None:
    _mark_sim(tmp_path)
    only_mainnet = {k: v for k, v in SECRETS.items() if not k.startswith("BINANCE_TESTNET")}
    (adapter, *_), _, _ = _build(tmp_path, mode, secrets=only_mainnet)
    # 缺测试网密钥：退成桩，不开仓，更不会拿主网 key 去连
    assert isinstance(adapter, StubExecutorAdapter)
    assert spy.ccxt_calls == []


def test_simulated_mode_refuses_when_hummingbot_env_differs(tmp_path: Path, spy: Spy) -> None:
    _mark_sim(tmp_path, "legacy_testnet")  # hummingbot 连旧测试网，配置却是 demo
    with pytest.raises(SimEnvironmentMismatch):
        _build(tmp_path, RunMode.TESTNET)
    assert spy.ccxt_calls == []
