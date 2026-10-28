"""快环参数：不写用默认值，写了就按范围校验，超范围拒绝启动。"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiaoyi.contracts import FastLoopConfig
from jiaoyi.core import profiles
from jiaoyi.core.config import ConfigError, load_app_config
from tests.conftest import ROOT

APP_EXAMPLE = ROOT / "config" / "app.example.toml"


TWO_HOURS = FastLoopConfig(**{**profiles.defaults("2h", "fast_loop"), **profiles.PAPER_GATE})
"""示例是 paper：拍板门槛按 paper 的临时推荐。"""


def test_example_matches_defaults() -> None:
    # 示例里档位管的键都注释掉了：代码默认 + 2h 档默认
    assert load_app_config(APP_EXAMPLE).fast_loop == TWO_HOURS


def test_section_is_optional(tmp_path: Path) -> None:
    text = APP_EXAMPLE.read_text(encoding="utf-8").split("[fast_loop]")[0]
    p = tmp_path / "app.toml"
    p.write_text(text, encoding="utf-8")
    assert load_app_config(p).fast_loop == TWO_HOURS


@pytest.mark.parametrize(
    "line",
    [
        "min_p_take = 1.3",
        "keeper_confirmations = 0",
        "keeper_close_p_strong = 0.55",
        "patrol_alert_pct_1m = 5.0",
        "max_stop_atr = 50",
        "unknown_knob = 1",
        'jev_model = "jev-latest"',
        "liq_min_usdt_1m = 1e12",
    ],
)
def test_out_of_range_refuses(tmp_path: Path, line: str) -> None:
    key = line.split(" = ")[0]
    lines = [
        ln
        for ln in APP_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if not ln.startswith(key + " ")
    ]
    p = tmp_path / "app.toml"
    p.write_text("\n".join([*lines, line]) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_app_config(p)


def test_rule_gate_switch_only_in_paper(tmp_path: Path) -> None:
    text = APP_EXAMPLE.read_text(encoding="utf-8").replace(
        "rule_gate_without_jev = false", "rule_gate_without_jev = true"
    )
    p = tmp_path / "app.toml"
    p.write_text(text, encoding="utf-8")
    assert load_app_config(p).fast_loop.rule_gate_without_jev
    assert load_app_config(p).ignored_paper_switches == []
    for mode in ("testnet", "live"):  # 写了也不拒绝启动：当关，启动时警告
        p.write_text(text.replace('mode = "paper"', f'mode = "{mode}"'), encoding="utf-8")
        assert load_app_config(p).ignored_paper_switches == ["没有拍板人时用规则代替"]


def test_gate_record_only_default_off_and_never_live() -> None:
    from jiaoyi.contracts import AppConfig

    base = load_app_config(APP_EXAMPLE)
    assert base.mode.value == "paper" and not base.gate_record_only  # 不写 = 关，拍板一票否决
    raw = base.model_dump(mode="json")
    raw["fast_loop"]["gate_record_only"] = True
    assert AppConfig.model_validate(raw).gate_record_only
    raw["mode"] = "testnet"
    raw["fast_loop"]["gate_record_only"] = None
    assert not AppConfig.model_validate(raw).gate_record_only  # testnet / live 永远关
    raw["fast_loop"]["gate_record_only"] = True
    raw["fast_loop"]["rule_gate_without_jev"] = True
    for mode in ("testnet", "live"):  # 不拒绝启动，只是当关；传给风控员的 record_only 也是关
        raw["mode"] = mode
        cfg = AppConfig.model_validate(raw)
        assert not cfg.gate_record_only
        assert cfg.ignored_paper_switches == ["拍板只记不拦", "没有拍板人时用规则代替"]


def test_paper_gate_thresholds_only_in_paper(tmp_path: Path) -> None:
    text = APP_EXAMPLE.read_text(encoding="utf-8")
    paper = load_app_config(APP_EXAMPLE).fast_loop
    gate = (paper.min_p_take, paper.min_p_genuine, paper.max_p_toxic, paper.min_p_direction)
    assert gate == (0.55, 0.35, 0.60, 0.45)  # 不写就跟档位走（不写死）；做 0.55 = 一道题门槛
    p = tmp_path / "app.toml"
    p.write_text(text.replace("[fast_loop]", "[fast_loop]\nmin_p_take = 0.6", 1), encoding="utf-8")
    assert load_app_config(p).fast_loop.min_p_take == 0.6  # 写了的优先
    base = profiles.defaults("2h", "fast_loop")
    p.write_text(text.replace('mode = "paper"', 'mode = "testnet"', 1), encoding="utf-8")
    assert load_app_config(p).fast_loop.min_p_take == base["min_p_take"]  # testnet 用档位推荐


def test_keeper_assistant_defaults_off_and_rejects_bad_values() -> None:
    cfg = FastLoopConfig()
    assert (cfg.keeper_assistant_on, cfg.keeper_assistant_close) == (False, False)
    assert cfg.keeper_assistant_b_record_only and not cfg.keeper_assistant_flip_confirm
    assert not cfg.keeper_assistant_flip_neutral and cfg.card_flip_confirm_by == "meeting"
    assert cfg.card_neutral_flip == "flip"
    assert (cfg.card_ignore_max_age_min, cfg.card_flip_assistant_fallback_min) == (0.0, 0.0)
    assert (cfg.keeper_assistant_b_model, cfg.keeper_assistant_min_p) == (None, 0.6)
    assert (cfg.keeper_assistant_gap_s, cfg.keeper_assistant_horizon) == (120, "1–4 小时")
    assert (cfg.keeper_assistant_concurrency, cfg.keeper_assistant_key_level) == (2, False)
    assert cfg.keeper_assistant_timeout_s == 8.0
    for bad in (
        {"keeper_assistant_min_p": 0.4},
        {"keeper_assistant_gap_s": -1},
        {"keeper_assistant_concurrency": 3},
        {"keeper_assistant_concurrency": 0},
        {"keeper_assistant_horizon": ""},
    ):
        with pytest.raises(ValueError):
            FastLoopConfig(**bad)  # type: ignore[arg-type]


def test_rest_warm_start_defaults_off_and_is_hot() -> None:
    from jiaoyi.core.hotconfig import HOT_FAST

    cfg = FastLoopConfig()
    assert cfg.rest_warm_start is False and cfg.market_rest_concurrency == 4
    assert "rest_warm_start" in HOT_FAST and "market_rest_concurrency" not in HOT_FAST
    with pytest.raises(ValueError):
        FastLoopConfig(market_rest_concurrency=0)
