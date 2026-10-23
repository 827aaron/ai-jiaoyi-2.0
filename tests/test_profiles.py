"""持仓档位：一个开关选档；写了的单项优先于档位默认；30m / 15m 只能 paper。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from jiaoyi.app import App
from jiaoyi.cli import main
from jiaoyi.core import profiles
from jiaoyi.core.config import ConfigError, load_app_config, load_risk_config
from tests.conftest import ROOT

RISK = ROOT / "config" / "risk.example.toml"
APP = ROOT / "config" / "app.example.toml"
MISSING_SLOW = ROOT / "config" / "no-such-slow.toml"


def _app_toml(tmp_path: Path, *lines: str) -> Path:
    text = APP.read_text(encoding="utf-8").replace('profile = "2h"', "")
    text = text.replace('mode = "paper"', "\n".join(['mode = "paper"', *lines]))
    path = tmp_path / "app.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_every_profile_is_complete_and_valid(tmp_path: Path) -> None:
    sections = {"app", "slow.schedule", "slow.budget"}
    assert set(profiles.DEFAULTS) == set(profiles.PROFILES)
    for name in profiles.PROFILES:
        assert set(profiles.DEFAULTS[name]) == sections
        cfg = load_app_config(_app_toml(tmp_path, f'profile = "{name}"'))
        assert cfg.profile == name
        assert cfg.fast_loop.time_limit_s == 7200  # 单笔最长持仓不随档位变
        assert cfg.fast_loop.atr_period_min == 15
        assert cfg.keeper_interval_s == profiles.defaults(name, "app")["keeper_interval_s"]


def test_default_is_2h_and_written_key_wins(tmp_path: Path) -> None:
    cfg = load_app_config(_app_toml(tmp_path))
    assert cfg.profile == "2h" and cfg.slow_loop_interval_min == 120
    cfg = load_app_config(_app_toml(tmp_path, 'profile = "60m"', "keeper_interval_s = 30"))
    assert cfg.keeper_interval_s == 30  # 写了的优先
    assert cfg.slow_loop_interval_min == 60 and cfg.fast_loop.time_limit_s == 7200


@pytest.mark.parametrize("profile", ["30m", "15m"])
@pytest.mark.parametrize("mode", ["testnet", "live"])
def test_short_profiles_are_paper_only(tmp_path: Path, profile: str, mode: str) -> None:
    path = _app_toml(tmp_path, f'profile = "{profile}"')
    path.write_text(path.read_text(encoding="utf-8").replace('mode = "paper"', f'mode = "{mode}"'))
    with pytest.raises(ConfigError, match="只能在 paper"):
        load_app_config(path)


def test_unknown_profile_refuses(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="profile"):
        load_app_config(_app_toml(tmp_path, 'profile = "5m"'))


def test_profile_reaches_predictor_and_commander_settings(tmp_path: Path) -> None:
    path = _app_toml(tmp_path, 'profile = "15m"', f'data_dir = "{tmp_path}"')
    path.write_text(path.read_text(encoding="utf-8").replace('data_dir = "data"', ""))
    cfg = load_app_config(path)
    app = App(load_risk_config(RISK), cfg, clock=lambda: datetime.now(UTC), offline=True)
    assert app.m.predictor._s.pred_len == 24  # 跟 time_limit_s（7200）走，不跟档位  # type: ignore[attr-defined]
    assert app.slow_cfg.schedule.card_ttl_min == 35


def test_check_config_shows_profile_and_overrides(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _app_toml(tmp_path, 'profile = "60m"', "keeper_interval_s = 30")
    args = ["check-config", "--risk", str(RISK), "--app", str(path), "--slow", str(MISSING_SLOW)]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "持仓档位 60m" in out and "keeper_interval_s" in out

    path = _app_toml(tmp_path, 'profile = "15m"')
    args[4] = str(path)
    assert main(args) == 0
    assert "持仓档位 15m（仅模拟盘）" in capsys.readouterr().out


def test_hold_params_follow_time_limit_not_profile(tmp_path: Path) -> None:
    path = _app_toml(tmp_path, 'profile = "30m"')
    path.write_text(
        path.read_text(encoding="utf-8").replace("[fast_loop]", "[fast_loop]\ntime_limit_s = 1800"),
        encoding="utf-8",
    )
    cfg = load_app_config(path)
    assert cfg.fast_loop.atr_period_min == 5
    cfg = load_app_config(_app_toml(tmp_path, 'profile = "15m"'))
    assert cfg.fast_loop.time_limit_s == 7200 and cfg.fast_loop.atr_period_min == 15
