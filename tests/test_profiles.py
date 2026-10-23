"""两套档位各选各的：meeting_interval 开会间隔 7 档、hold 最长持仓 4 档；hold 30m 只能 paper。"""

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
    text = (
        APP.read_text(encoding="utf-8")
        .replace('meeting_interval = "2h"', "")
        .replace('hold = "4h"', "")
    )
    text = text.replace('mode = "paper"', "\n".join(['mode = "paper"', *lines]))
    path = tmp_path / "app.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_every_tier_is_complete_and_valid(tmp_path: Path) -> None:
    assert set(profiles.MEETING) == set(profiles.PROFILES)
    assert set(profiles.HOLD) == set(profiles.HOLDS)
    for name in profiles.PROFILES:
        cfg = load_app_config(_app_toml(tmp_path, f'meeting_interval = "{name}"'))
        assert cfg.meeting_interval == name
        assert (
            cfg.slow_loop_interval_min == profiles.defaults(name, "app")["slow_loop_interval_min"]
        )
        hold = profiles.RECOMMENDED_HOLD[name]  # 没写 hold = 跟开会档位的推荐搭配
        assert cfg.hold == hold
        assert cfg.fast_loop.time_limit_s == profiles.HOLD[hold]["fast_loop"]["time_limit_s"]
    for name in profiles.HOLDS:
        cfg = load_app_config(_app_toml(tmp_path, f'hold = "{name}"'))
        want = profiles.HOLD[name]["fast_loop"]
        assert cfg.hold == name and cfg.meeting_interval == "2h"
        assert cfg.fast_loop.time_limit_s == want["time_limit_s"]
        assert cfg.fast_loop.atr_period_min == want["atr_period_min"]
        assert cfg.fast_loop.min_stop_fee_multiple == want["min_stop_fee_multiple"]
        assert cfg.keeper_interval_s == profiles.HOLD[name]["app"]["keeper_interval_s"]


def test_defaults_are_2h_meeting_and_4h_hold(tmp_path: Path) -> None:
    cfg = load_app_config(_app_toml(tmp_path))
    assert (cfg.meeting_interval, cfg.hold) == ("2h", "4h")
    assert cfg.slow_loop_interval_min == 120 and cfg.fast_loop.time_limit_s == 14400
    cfg = load_app_config(_app_toml(tmp_path, 'meeting_interval = "6h"', "keeper_interval_s = 30"))
    assert cfg.keeper_interval_s == 30 and cfg.slow_loop_interval_min == 360  # 写了的优先


def test_hold_defaults_to_recommended_pairing(tmp_path: Path) -> None:
    rec = {"15m": "1h", "30m": "2h", "1h": "4h", "2h": "4h"}
    for meeting, hold in rec.items():
        cfg = load_app_config(_app_toml(tmp_path, f'meeting_interval = "{meeting}"'))
        assert (
            cfg.hold == hold
            and cfg.fast_loop.time_limit_s == profiles.HOLD[hold]["fast_loop"]["time_limit_s"]
        )
    cfg = load_app_config(_app_toml(tmp_path, 'meeting_interval = "2h"', 'hold = "1h"'))
    assert cfg.hold == "1h"  # 显式写的按用户的
    assert profiles.hold_is_custom({"hold": "1h"}) and not profiles.hold_is_custom({})


def test_legacy_profile_key_maps_to_meeting_interval(tmp_path: Path) -> None:
    cfg = load_app_config(_app_toml(tmp_path, 'profile = "60m"'))
    assert cfg.meeting_interval == "1h" and cfg.slow_loop_interval_min == 60
    cfg = load_app_config(_app_toml(tmp_path, 'profile = "2h"', 'meeting_interval = "30m"'))
    assert cfg.meeting_interval == "30m"  # 新键优先
    assert profiles.legacy_notice({"profile": "60m"})
    assert profiles.legacy_notice({"profile": "60m", "meeting_interval": "1h"}) is None


def test_hold_30m_is_paper_only(tmp_path: Path) -> None:
    for mode in ("testnet", "live"):
        path = _app_toml(tmp_path, 'hold = "30m"')
        path.write_text(
            path.read_text(encoding="utf-8").replace('mode = "paper"', f'mode = "{mode}"')
        )
        with pytest.raises(ConfigError, match="只能在 paper"):
            load_app_config(path)
    load_app_config(_app_toml(tmp_path, 'hold = "30m"', 'meeting_interval = "15m"'))  # paper 可以


def test_unknown_tier_refuses(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="meeting_interval"):
        load_app_config(_app_toml(tmp_path, 'meeting_interval = "5m"'))
    with pytest.raises(ConfigError, match="hold"):
        load_app_config(_app_toml(tmp_path, 'hold = "9h"'))


def test_time_limit_alone_picks_hold_tier(tmp_path: Path) -> None:
    path = _app_toml(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace("[fast_loop]", "[fast_loop]\ntime_limit_s = 1800"),
        encoding="utf-8",
    )
    cfg = load_app_config(path)
    assert cfg.fast_loop.atr_period_min == 5


def test_tiers_reach_predictor_and_commander_settings(tmp_path: Path) -> None:
    path = _app_toml(
        tmp_path, 'meeting_interval = "15m"', 'hold = "4h"', f'data_dir = "{tmp_path}"'
    )
    path.write_text(path.read_text(encoding="utf-8").replace('data_dir = "data"', ""))
    cfg = load_app_config(path)
    app = App(load_risk_config(RISK), cfg, clock=lambda: datetime.now(UTC), offline=True)
    assert app.m.predictor._s.pred_len == 48  # type: ignore[attr-defined]  # 跟最长持仓走
    assert app.slow_cfg.schedule.card_ttl_min == 35  # 开会间隔 15m


def test_check_config_shows_tiers_and_overrides(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _app_toml(tmp_path, 'meeting_interval = "2h"', "keeper_interval_s = 40")
    args = ["check-config", "--risk", str(RISK), "--app", str(path), "--slow", str(MISSING_SLOW)]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "开会间隔 2h" in out and "最长持仓 4h" in out and "跟开会档位" in out
    assert "keeper_interval_s" in out

    path = _app_toml(tmp_path, 'hold = "30m"')
    args[4] = str(path)
    assert main(args) == 0
    assert "最长持仓 30m（仅模拟盘）" in capsys.readouterr().out


def test_long_hold_tiers(tmp_path: Path) -> None:
    for name, atr, patrol in (("8h", 30, 2.5), ("12h", 60, 3.0), ("24h", 240, 4.0)):
        cfg = load_app_config(_app_toml(tmp_path, f'hold = "{name}"'))
        assert cfg.fast_loop.atr_period_min == atr
        assert cfg.fast_loop.patrol_shock_pct_1m == patrol
        assert cfg.fast_loop.funding_against_max_bp == 5.0
    rec = {"6h": "12h", "12h": "24h", "24h": "24h"}
    for meeting, hold in rec.items():
        assert load_app_config(_app_toml(tmp_path, f'meeting_interval = "{meeting}"')).hold == hold
    assert profiles.hold_for_seconds(30000) == "12h"
