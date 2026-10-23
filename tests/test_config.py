from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.cli import main
from jiaoyi.core.config import ConfigError, load_app_config, load_risk_config
from tests.conftest import ROOT

RISK_EXAMPLE = ROOT / "config" / "risk.example.toml"
APP_EXAMPLE = ROOT / "config" / "app.example.toml"


def _write(tmp_path: Path, data: dict[str, Any]) -> Path:
    lines: list[str] = []
    tables: list[str] = []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append(f"[{k}]")
            tables += [f"{kk} = {vv!r}" for kk, vv in v.items()]
        else:
            lines.append(f"{k} = {_toml(v)}")
    path = tmp_path / "risk.toml"
    path.write_text("\n".join(lines + tables) + "\n", encoding="utf-8")
    return path


def _toml(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    return repr(v)


def _example() -> dict[str, Any]:
    with RISK_EXAMPLE.open("rb") as f:
        return tomllib.load(f)


def test_examples_load_with_conservative_defaults() -> None:
    risk = load_risk_config(RISK_EXAMPLE)
    assert risk.risk_per_trade_pct == 1.0
    assert risk.max_leverage == 5
    assert risk.max_total_exposure_x == 3.0
    assert risk.daily_loss_halt_pct == 3.0
    assert load_app_config(APP_EXAMPLE).mode == "paper"


def test_missing_file_refuses(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="找不到配置文件"):
        load_risk_config(tmp_path / "nope.toml")


@pytest.mark.parametrize("field", ["risk_per_trade_pct", "max_leverage", "daily_loss_halt_pct"])
def test_missing_field_refuses(tmp_path: Path, field: str) -> None:
    data = _example()
    del data[field]
    with pytest.raises(ConfigError, match=f"{field}: 缺少这一项"):
        load_risk_config(_write(tmp_path, data))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_per_trade_pct", 150.0),
        ("max_leverage", 126),
        ("max_total_exposure_x", 200.0),
        ("daily_loss_halt_pct", 0.0),
        ("lifeboat_stop_multiplier", 1.0),
        ("day_reset_tz", "Asia/Tokyo"),
    ],
)
def test_out_of_range_refuses(tmp_path: Path, field: str, value: Any) -> None:
    data = {**_example(), field: value}
    with pytest.raises(ConfigError, match=field):
        load_risk_config(_write(tmp_path, data))


def test_unknown_field_refuses(tmp_path: Path) -> None:
    data = {**_example(), "max_levarage": 5}
    with pytest.raises(ConfigError, match="max_levarage"):
        load_risk_config(_write(tmp_path, data))


def test_category_cap_above_total_refuses(tmp_path: Path) -> None:
    data = _example()
    data["category_exposure_x"]["alt"] = 4.0
    with pytest.raises(ConfigError, match="alt"):
        load_risk_config(_write(tmp_path, data))


def test_missing_category_refuses(tmp_path: Path) -> None:
    data = _example()
    del data["category_exposure_x"]["precious_metal"]
    with pytest.raises(ConfigError, match="precious_metal"):
        load_risk_config(_write(tmp_path, data))


def test_cli_check_config(tmp_path: Path) -> None:
    assert main(["check-config", "--risk", str(RISK_EXAMPLE), "--app", str(APP_EXAMPLE)]) == 0
    assert main(["check-config", "--risk", str(tmp_path / "x"), "--app", str(APP_EXAMPLE)]) == 2


def test_cli_check_config_cautions_but_accepts_aggressive_caps(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = {
        **_example(),
        "risk_per_trade_pct": 5.0,
        "daily_loss_halt_pct": 15.0,
        "max_leverage": 10,
    }
    path = _write(tmp_path, data)
    assert main(["check-config", "--risk", str(path), "--app", str(APP_EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "比推荐值冒险" in out and "max_leverage = 10（推荐 5）" in out
