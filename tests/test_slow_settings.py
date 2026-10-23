from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from jiaoyi.core.config import ConfigError
from jiaoyi.slow.settings import SlowConfig, load_slow_config, slow_defaults
from jiaoyi.slow.tradingagents_engine import TradingAgentsSettings

ROOT = Path(__file__).resolve().parents[1]


def test_missing_file_uses_profile_defaults(tmp_path: Path) -> None:
    cfg = load_slow_config(tmp_path / "nope.toml")  # 默认 2h 档
    assert cfg.engine_settings() == TradingAgentsSettings()
    kw = cfg.commander_kwargs()
    assert kw["concurrency"] == 4
    assert kw["ttl"] == timedelta(hours=2)
    assert kw["symbol_min_interval"] == timedelta(hours=1.67)
    assert kw["expiry_lead"] == timedelta(minutes=20)
    # 2h 档 = 2 小时一轮会：关键位突破 / 新入池也要隔 100 分钟
    assert kw["symbol_timeout_s"] == 900 and kw["min_refresh_gap"] == timedelta(minutes=100)
    assert cfg.effective_pick_max(20) == 10

    one_hour = load_slow_config(tmp_path / "nope.toml", "60m").commander_kwargs()
    assert one_hour["ttl"] == timedelta(hours=1) and one_hour["concurrency"] == 5
    assert one_hour["expiry_lead"] == timedelta(minutes=15)
    assert one_hour["fail_backoff"][-1] == timedelta(minutes=15)
    assert one_hour["min_refresh_gap"] == timedelta(minutes=45)


def test_example_file_is_valid_and_matches_defaults() -> None:
    cfg = load_slow_config(ROOT / "config" / "slow.example.toml")
    assert cfg == slow_defaults("2h")
    assert SlowConfig().schedule.refresh_lead_min == 15  # 不在档位里时的代码默认


def test_written_key_beats_profile(tmp_path: Path) -> None:
    p = tmp_path / "slow.toml"
    p.write_text("[schedule]\nconcurrency = 2\nrefresh_lead_min = 30\n", encoding="utf-8")
    cfg = load_slow_config(p, "60m")
    assert cfg.schedule.concurrency == 2 and cfg.schedule.refresh_lead_min == 30
    assert cfg.schedule.card_ttl_hours == 1.0  # 没写的仍取档位


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


def test_max_cards_per_round(tmp_path: Path) -> None:
    assert SlowConfig().effective_pick_max(20) == 20  # 不写 = 用 card_pick_max
    p = tmp_path / "slow.toml"
    p.write_text("[schedule]\nmax_cards_per_round = 8\n", encoding="utf-8")
    cfg = load_slow_config(p)
    assert cfg.effective_pick_max(20) == 8
    assert cfg.effective_pick_max(5) == 5  # 不会超过 card_pick_max
    for bad in ("0", "51"):
        p.write_text(f"[schedule]\nmax_cards_per_round = {bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_slow_config(p)


def test_missing_slow_stack_only_when_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiaoyi.slow.settings import missing_slow_stack

    class S:
        def __init__(self, d: dict[str, str]) -> None:
            self.d = d

        def get(self, n: str) -> str | None:
            return self.d.get(n)

    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    cfg = SlowConfig()
    assert missing_slow_stack(cfg, S({})) is None  # 没 key：M0/M1 允许用桩
    msg = missing_slow_stack(cfg, S({"DEEPSEEK_API_KEY": "k"}))
    assert msg is not None and "make install" in msg
    orc = SlowConfig.model_validate({"llm": {"provider": "openrouter"}})
    assert missing_slow_stack(orc, S({"DEEPSEEK_API_KEY": "k"})) is None  # 只看所选 provider 的 key
    assert "OPENROUTER_API_KEY" in (missing_slow_stack(orc, S({"OPENROUTER_API_KEY": "k"})) or "")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: object())
    assert missing_slow_stack(cfg, S({"DEEPSEEK_API_KEY": "k"})) is None


def test_refresh_gap_must_be_shorter_than_card_ttl(tmp_path: Path) -> None:
    path = tmp_path / "slow.toml"
    path.write_text("[schedule]\nmin_refresh_gap_min = 120\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="min_refresh_gap_min"):
        load_slow_config(path)
