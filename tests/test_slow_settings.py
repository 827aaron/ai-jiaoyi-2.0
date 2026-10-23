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
    assert kw["concurrency"] == 4
    assert kw["ttl"] == timedelta(hours=2)
    assert kw["symbol_min_interval"] == timedelta(hours=2)


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


def test_openrouter_provider_picks_namespaced_models(tmp_path: Path) -> None:
    p = tmp_path / "slow.toml"
    p.write_text('[llm]\nprovider = "openrouter"\n', encoding="utf-8")
    s = load_slow_config(p).engine_settings()
    assert s.provider == "openrouter"
    assert (s.quick_model, s.deep_model) == (
        "deepseek/deepseek-v4.1-flash",
        "deepseek/deepseek-v4.1-flash",
    )
    p.write_text('[llm]\nprovider = "openrouter"\n[meeting]\ndeep_model = "x/y-pro"\n', "utf-8")
    assert load_slow_config(p).engine_settings().deep_model == "x/y-pro"


def test_unknown_provider_refuses(tmp_path: Path) -> None:
    p = tmp_path / "slow.toml"
    p.write_text('[llm]\nprovider = "openai"\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_slow_config(p)


def test_engine_reads_key_for_its_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiaoyi.slow.tradingagents_engine import TradingAgentsEngine

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # CI 没装 tradingagents（可选依赖），只测密钥选择
    monkeypatch.setattr("jiaoyi.slow.tradingagents_engine.register_binance_vendor", lambda: None)

    class S:
        def __init__(self, d: dict[str, str]) -> None:
            self.d = d

        def get(self, n: str) -> str | None:
            return self.d.get(n)

    cfg = SlowConfig.model_validate({"llm": {"provider": "openrouter"}})
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        TradingAgentsEngine(None, S({"DEEPSEEK_API_KEY": "k"}), cfg.engine_settings())  # type: ignore[arg-type]
    TradingAgentsEngine(None, S({"OPENROUTER_API_KEY": "k"}), cfg.engine_settings())  # type: ignore[arg-type]
    import os

    assert os.environ["OPENROUTER_API_KEY"] == "k"
    monkeypatch.delenv("OPENROUTER_API_KEY")


def test_budget_defaults_and_validation(tmp_path: Path) -> None:
    kw = SlowConfig().commander_kwargs()
    assert kw["daily_cost_limit_usd"] == 10.0
    assert kw["fail_backoff"] == (
        timedelta(minutes=5),
        timedelta(minutes=10),
        timedelta(minutes=30),
    )
    for body in ("[budget]\ndaily_cost_limit_usd = 0\n", "[budget]\nfail_backoff_minutes = []\n"):
        p = tmp_path / "slow.toml"
        p.write_text(body, encoding="utf-8")
        with pytest.raises(ConfigError):
            load_slow_config(p)
