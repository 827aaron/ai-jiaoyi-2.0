"""TradingAgents 角色替换和 ``PerpGraph``（需要 tradingagents；CI 没装就跳过）。"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("tradingagents")
pytest.importorskip("langchain_core")

from tradingagents.dataflows.config import run_config

from jiaoyi.slow import ta_nodes
from jiaoyi.slow import tradingagents_engine as eng


class Llm:
    """记下收到的提示词，固定回一句话。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, prompt: Any, *a: Any, **k: Any) -> Any:
        self.prompts.append(str(prompt))
        return SimpleNamespace(content="发言")


def _cfg(tmp_path: Path, hold: str = "2h", minutes: int = 120) -> dict[str, Any]:
    from tradingagents.default_config import build_default_config

    s = eng.TradingAgentsSettings(hold=hold, hold_minutes=minutes, results_dir=str(tmp_path))
    return eng.build_config(build_default_config(), s)


def _state() -> dict[str, Any]:
    return {
        "company_of_interest": "BTC-USD",
        "asset_type": "crypto",
        "trade_date": "2026-10-24",
        "market_report": "行情报告",
        "sentiment_report": "",
        "news_report": "新闻报告",
        "fundamentals_report": "",
        "trader_investment_plan": "FINAL TRANSACTION PROPOSAL: **SELL**",
        "messages": [],
        "portfolio_context": "POSITION NOTE",  # PerpGraph 放进 state 的持仓说明
        "investment_debate_state": {
            "history": "旧",
            "bull_history": "多旧",
            "bear_history": "空旧",
            "current_response": "Bear Analyst: 空旧",
            "count": 1,
        },
        "risk_debate_state": {
            "history": "旧",
            "aggressive_history": "激旧",
            "conservative_history": "保旧",
            "neutral_history": "中旧",
            "latest_speaker": "Neutral",
            "current_aggressive_response": "A",
            "current_conservative_response": "C",
            "current_neutral_response": "N",
            "count": 3,
        },
    }


ROLES = [
    ("create_bull_researcher", "tradingagents.agents.researchers.bull_researcher"),
    ("create_bear_researcher", "tradingagents.agents.researchers.bear_researcher"),
    ("create_aggressive_debator", "tradingagents.agents.risk_mgmt.aggressive_debator"),
    ("create_conservative_debator", "tradingagents.agents.risk_mgmt.conservative_debator"),
    ("create_neutral_debator", "tradingagents.agents.risk_mgmt.neutral_debator"),
]


@pytest.mark.parametrize(("name", "module"), ROLES)
def test_replaced_roles_read_and_write_state_like_the_original(
    name: str, module: str, tmp_path: Path
) -> None:
    import importlib

    original = getattr(importlib.import_module(module), name)
    ours = getattr(ta_nodes, name)
    with run_config(_cfg(tmp_path)):
        want = original(Llm())(_state())
        llm = Llm()
        got = ours(llm)(_state())
    assert got == want  # 状态字段、前缀、计数完全一样，只是提示词正文不同
    prompt = re.sub(r"Do not use equity-style arguments \([^)]*\)", "", llm.prompts[0])
    prompt = re.sub(r"Do not argue from [^.]*\.", "", prompt)
    for word in ("growth", "revenue", "branding", "diversification", "steady growth"):
        assert word not in prompt.replace("long-term growth or innovation", "")
    assert "Binance" in prompt and "2 hours" in prompt
    assert "do not assume a flat book" not in prompt


def test_install_replaces_the_factories_the_graph_builder_uses() -> None:
    from tradingagents.graph import setup

    ta_nodes.install()
    ta_nodes.install()  # 重复调用无害
    for name, _ in ROLES:
        assert getattr(setup, name) is getattr(ta_nodes, name)
    assert setup.create_news_analyst is ta_nodes.create_news_analyst


def test_news_analyst_crypto_has_no_macro_tool_and_stock_uses_the_original(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tradingagents.agents.analysts import turn

    ta_nodes.install()
    seen: dict[str, Any] = {}

    def take_turn(prompt: Any, llm: Any, tools: Any, messages: Any) -> tuple[str, str]:
        seen["tools"] = [t.name for t in tools]
        seen["prompt"] = prompt.format_messages(messages=[])[0].content
        return "r", "新闻报告"

    monkeypatch.setattr(turn, "take_turn", take_turn)
    monkeypatch.setitem(
        ta_nodes._ORIGINALS, "create_news_analyst", lambda llm: lambda st: {"news_report": "orig"}
    )
    node = ta_nodes.create_news_analyst(Llm())
    with run_config(_cfg(tmp_path)):
        out = node(_state())
        assert out["news_report"] == "新闻报告"
        assert seen["tools"] == ["get_news", "get_global_news", "get_prediction_markets"]
        assert "BTCUSDT" in seen["prompt"] and "within 2 hours" in seen["prompt"]
        assert "get_macro_indicators" not in seen["prompt"]
        assert node({**_state(), "asset_type": "stock"}) == {"news_report": "orig"}


def _graph(tmp_path: Path, **kw: Any) -> Any:
    import os

    os.environ.setdefault("DEEPSEEK_API_KEY", "x" * 16)
    cfg = _cfg(tmp_path)
    return ta_nodes.perp_graph_class()(
        selected_analysts=("market",), config=cfg, callbacks=[], **kw
    )


def test_perp_graph_puts_the_meeting_and_position_notes_in_state(tmp_path: Path) -> None:
    g = _graph(
        tmp_path,
        meeting_note="MEETING NOTE",
        position_note="POSITION NOTE",
        lessons="LESSONS",
    )
    with run_config(g.config):
        state = g.create_run_state("BTC-USD", "2026-10-24", "crypto")
    assert state["instrument_context"].endswith("MEETING NOTE")
    assert state["portfolio_context"] == "POSITION NOTE"
    assert "not provided" not in state["portfolio_context"]
    assert g._memory_step(state) == {"past_context": "LESSONS", "memory_note": ""}


def test_concurrent_graphs_keep_their_own_notes(tmp_path: Path) -> None:
    results: dict[int, tuple[str, str]] = {}
    errors: list[BaseException] = []

    def run(i: int) -> None:
        try:
            g = _graph(tmp_path, meeting_note=f"MEETING-{i}", position_note=f"POSITION-{i}")
            with run_config(g.config):
                st = g.create_run_state("BTC-USD", "2026-10-24", "crypto")
            results[i] = (st["instrument_context"], st["portfolio_context"])
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    for i, (ctx, pos) in results.items():
        assert ctx.endswith(f"MEETING-{i}") and pos == f"POSITION-{i}"
    assert len(results) == 8


def test_original_memory_log_is_off(tmp_path: Path) -> None:
    g = _graph(tmp_path)
    assert g.config["memory_log_path"] == ""
    g.memory_log.store_decision("BTC-USD", "2026-10-24", "x", "Sell")  # 不写任何文件
    assert not list(Path(tmp_path).rglob("memory.md"))


def test_sentiment_analyst_crypto_reads_positioning_and_stock_uses_the_original(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jiaoyi.slow import crowd

    ta_nodes.install()
    monkeypatch.setitem(
        ta_nodes._ORIGINALS, "create_sentiment_analyst", lambda llm: lambda st: {"x": "orig"}
    )
    seen: dict[str, Any] = {}

    def fetch(symbol: str, period: str) -> tuple[list[str], int]:
        seen["args"] = (symbol, period)
        return ["### Global long/short account ratio\n- Latest 1.8"], 1

    monkeypatch.setattr(crowd, "fetch_blocks", fetch)
    llm = Llm()
    node = ta_nodes.create_sentiment_analyst(llm)
    with run_config(_cfg(tmp_path)):
        out = node(_state())
        assert seen["args"] == ("BTCUSDT", "15m")  # 周期跟档位
        assert out["sentiment_report"] == "发言" and out["messages"][0].content == "发言"
        prompt = llm.prompts[0]
        assert "crowd-positioning analyst for BTCUSDT" in prompt and "Latest 1.8" in prompt
        assert "StockTwits" not in prompt
        assert node({**_state(), "asset_type": "stock"}) == {"x": "orig"}


def test_sentiment_analyst_says_unavailable_instead_of_returning_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jiaoyi.slow import crowd

    ta_nodes.install()
    monkeypatch.setitem(ta_nodes._ORIGINALS, "create_sentiment_analyst", lambda llm: lambda st: {})
    monkeypatch.setattr(crowd, "fetch_blocks", lambda sym, period: (["(unavailable)"], 0))
    monkeypatch.setattr(ta_nodes, "_NEWS_HUB", None)
    llm = Llm()
    with run_config(_cfg(tmp_path)):
        out = ta_nodes.create_sentiment_analyst(llm)(_state())
    assert "unavailable this round" in out["sentiment_report"] and not llm.prompts
    assert out["messages"][0].content == out["sentiment_report"]
