from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from jiaoyi.core.config import ConfigError
from jiaoyi.slow.settings import SlowConfig, load_slow_config
from jiaoyi.slow.tradingagents_engine import TradingAgentsSettings

ROOT = Path(__file__).resolve().parents[1]


def test_missing_file_equals_current_defaults(tmp_path: Path) -> None:
    cfg = load_slow_config(tmp_path / "nope.toml")
    assert cfg.engine_settings() == TradingAgentsSettings()
    kw = cfg.commander_kwargs()
    assert kw == {
        "concurrency": 4,
        "ttl": timedelta(hours=2),
        "symbol_min_interval": timedelta(hours=2),
    }


def test_example_file_is_valid_and_matches_defaults() -> None:
    cfg = load_slow_config(ROOT / "config" / "slow.example.toml")
    assert cfg == SlowConfig()


def test_overrides(tmp_path: Path) -> None:
    p = tmp_path / "slow.toml"
    p.write_text(
        '[meeting]\nanalysts_crypto = ["market"]\ndebate_rounds = 0\npick_tier = "deep"\n'
        "[schedule]\nconcurrency = 2\n",
        encoding="utf-8",
    )
    cfg = load_slow_config(p)
    s = cfg.engine_settings()
    assert s.analysts_crypto == ("market",)
    assert s.debate_rounds == 0 and s.pick_tier == "deep" and s.convert_tier == "quick"
    assert cfg.commander_kwargs()["concurrency"] == 2


@pytest.mark.parametrize(
    "body",
    [
        "[meeting]\nanalysts_crypto = []\n",
        '[meeting]\nanalysts_crypto = ["market", "market"]\n',
        '[meeting]\nanalysts_crypto = ["astrology"]\n',
        "[meeting]\ndebate_rounds = 9\n",
        '[meeting]\npick_tier = "huge"\n',
        "[schedule]\nconcurrency = 99\n",
        "[schedule]\ncard_ttl_hours = 24\n",
        "[meeting]\ntypo_field = 1\n",
        "not = [valid",
    ],
)
def test_invalid_refuses(tmp_path: Path, body: str) -> None:
    p = tmp_path / "slow.toml"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_slow_config(p)


def test_engine_tier_selection() -> None:
    from types import SimpleNamespace

    from jiaoyi.slow.tradingagents_engine import TradingAgentsEngine

    g = SimpleNamespace(quick_thinking_llm="flash", deep_thinking_llm="pro")
    assert TradingAgentsEngine._llm(g, "quick") == "flash"
    assert TradingAgentsEngine._llm(g, "deep") == "pro"
