"""实盘门槛：确认词 + 面板密码，5 次错误锁 15 分钟，没解锁不能建立实盘连接。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.contracts import RiskConfig, RunMode
from jiaoyi.core.config import load_risk_config
from jiaoyi.core.secrets import EnvSecretStore
from jiaoyi.execution.factory import MissingSecret, build_execution
from jiaoyi.execution.live_gate import CONFIRM_WORD, LiveGate, LiveGateError
from tests.conftest import ROOT

T0 = datetime(2026, 10, 22, 6, 0, tzinfo=UTC)
PW = "correct-horse"
KEYS = (
    "HUMMINGBOT_API_PASSWORD",
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
)


class Clk:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def clk() -> Clk:
    return Clk()


@pytest.fixture
def gate(tmp_path: Path, clk: Clk) -> LiveGate:
    g = LiveGate(tmp_path / "gate.json", clock=clk)
    g.set_password(PW)
    return g


def test_default_is_not_armed(gate: LiveGate) -> None:
    assert not gate.armed
    with pytest.raises(LiveGateError):
        gate.require_armed()


def test_unlock_needs_word_and_password(gate: LiveGate) -> None:
    with pytest.raises(LiveGateError, match="确认词"):
        gate.unlock("好的", PW)
    assert not gate.armed
    with pytest.raises(LiveGateError, match="密码不对"):
        gate.unlock(CONFIRM_WORD, "wrong")
    assert not gate.armed
    gate.unlock(CONFIRM_WORD, PW)
    assert gate.armed
    gate.require_armed()


def test_five_wrong_passwords_lock_for_15_minutes(gate: LiveGate, clk: Clk) -> None:
    for _ in range(5):
        with pytest.raises(LiveGateError):
            gate.unlock(CONFIRM_WORD, "wrong")
    # 锁定期间连对的密码也不行
    with pytest.raises(LiveGateError, match="锁定"):
        gate.unlock(CONFIRM_WORD, PW)
    clk.t = T0 + timedelta(minutes=14)
    with pytest.raises(LiveGateError, match="锁定"):
        gate.unlock(CONFIRM_WORD, PW)
    clk.t = T0 + timedelta(minutes=16)
    gate.unlock(CONFIRM_WORD, PW)
    assert gate.armed


def test_wrong_word_does_not_count_as_password_failure(gate: LiveGate) -> None:
    for _ in range(10):
        with pytest.raises(LiveGateError, match="确认词"):
            gate.unlock("nope", PW)
    gate.unlock(CONFIRM_WORD, PW)


def test_lock_survives_restart(tmp_path: Path, clk: Clk) -> None:
    path = tmp_path / "gate.json"
    g = LiveGate(path, clock=clk)
    g.set_password(PW)
    for _ in range(5):
        with pytest.raises(LiveGateError):
            g.unlock(CONFIRM_WORD, "wrong")
    with pytest.raises(LiveGateError, match="锁定"):
        LiveGate(path, clock=clk).unlock(CONFIRM_WORD, PW)


def test_password_stored_hashed(gate: LiveGate, tmp_path: Path) -> None:
    assert PW not in (tmp_path / "gate.json").read_text(encoding="utf-8")


def test_change_password_needs_old_and_disarms(gate: LiveGate) -> None:
    gate.unlock(CONFIRM_WORD, PW)
    with pytest.raises(LiveGateError):
        gate.set_password("another-pass1", "wrong")
    gate.set_password("another-pass1", PW)
    assert not gate.armed


def test_short_password_refused(tmp_path: Path) -> None:
    with pytest.raises(LiveGateError):
        LiveGate(tmp_path / "g.json").set_password("short")


def test_unlock_without_password_set(tmp_path: Path) -> None:
    with pytest.raises(LiveGateError, match="还没有设置"):
        LiveGate(tmp_path / "g.json").unlock(CONFIRM_WORD, "x")


@pytest.fixture
def risk() -> RiskConfig:
    return load_risk_config(ROOT / "config" / "risk.example.toml")


def test_live_mode_refused_without_gate_or_unlock(
    risk: RiskConfig, gate: LiveGate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in KEYS:
        monkeypatch.setenv(name, "x" * 8)
    args = (RunMode.LIVE, risk, tmp_path, EnvSecretStore(), StubAlerter())
    with pytest.raises(LiveGateError):
        build_execution(*args)
    with pytest.raises(LiveGateError):
        build_execution(*args, live_gate=gate)  # 有门槛对象但没解锁


async def test_live_mode_allowed_after_unlock_and_paper_needs_no_gate(
    risk: RiskConfig, gate: LiveGate, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in KEYS:
        monkeypatch.setenv(name, "x" * 8)
    paper = build_execution(RunMode.PAPER, risk, tmp_path, EnvSecretStore(), StubAlerter())
    await paper.close()
    gate.unlock(CONFIRM_WORD, PW)
    live = build_execution(
        RunMode.LIVE, risk, tmp_path, EnvSecretStore(), StubAlerter(), live_gate=gate
    )
    await live.close()


def test_missing_secret_names_the_key_not_the_value(
    risk: RiskConfig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in KEYS:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(MissingSecret, match="HUMMINGBOT_API_PASSWORD"):
        build_execution(RunMode.PAPER, risk, tmp_path, EnvSecretStore(), StubAlerter())
