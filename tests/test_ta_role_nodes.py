"""行情分析师 / 研究经理 / 交易员 / 组合经理的节点（需要 tradingagents；CI 没装就跳过）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("tradingagents")
pytest.importorskip("langchain_core")

from tradingagents.dataflows.config import run_config

from jiaoyi.slow import ta_nodes
from jiaoyi.slow import tradingagents_engine as eng

from .test_perp_graph import Llm, _cfg, _state

PLAN = "**Recommendation**: Overweight\n\n**Rationale**: x\n\n**Strategic Actions**: y"


def _full_state() -> dict[str, Any]:
    st = _state()
    st["investment_plan"] = PLAN
    st["past_context"] = "Lessons from earlier cards (5 days):\n- fade the first spike"
    return st


class Structured:
    """with_structured_output(include_raw=True) 的替身：先给定回答，再记调用次数。"""

    def __init__(self, answer: dict[str, Any] | Exception, plain: str = "普通回答") -> None:
        self.answer, self.plain = answer, plain
        self.structured_calls = 0
        self.plain_calls = 0
        self.prompts: list[Any] = []
        self.kwargs: dict[str, Any] = {}

    def with_structured_output(self, schema: Any, **kw: Any) -> Any:
        self.kwargs = kw
        outer = self

        class Bound:
            def invoke(self, prompt: Any) -> Any:
                outer.structured_calls += 1
                outer.prompts.append(prompt)
                if isinstance(outer.answer, Exception):
                    raise outer.answer
                return outer.answer

        return Bound()

    def invoke(self, prompt: Any, *a: Any, **k: Any) -> Any:
        self.plain_calls += 1
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.plain)


@pytest.mark.parametrize(
    ("name", "module", "extra"),
    [
        ("create_research_manager", "tradingagents.agents.managers.research_manager", {}),
        ("create_trader", "tradingagents.agents.trader.trader", {}),
        ("create_portfolio_manager", "tradingagents.agents.managers.portfolio_manager", {}),
    ],
)
def test_managers_read_and_write_state_like_the_original(
    name: str, module: str, extra: dict[str, Any], tmp_path: Path
) -> None:
    import importlib

    original = getattr(importlib.import_module(module), name)
    with run_config(_cfg(tmp_path)):
        want = original(Llm())(_full_state())
        got = getattr(ta_nodes, name)(Llm())(_full_state())
    assert got.keys() == want.keys()
    assert got == want  # 没有结构化输出时：状态字段、计数、说话人和原版一样，正文取模型原话


def test_install_adds_the_four_roles() -> None:
    from tradingagents.graph import setup

    ta_nodes.install()
    for name in (
        "create_market_analyst",
        "create_research_manager",
        "create_trader",
        "create_portfolio_manager",
    ):
        assert getattr(setup, name) is getattr(ta_nodes, name)


def test_market_analyst_prompt_and_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from tradingagents.agents.analysts import turn

    seen: dict[str, Any] = {}

    def take_turn(prompt: Any, llm: Any, tools: Any, messages: Any) -> tuple[str, str]:
        seen["tools"] = [t.name for t in tools]
        seen["prompt"] = prompt.format_messages(messages=[])[0].content
        return "r", "行情报告"

    monkeypatch.setattr(turn, "take_turn", take_turn)
    with run_config(_cfg(tmp_path)):
        out = ta_nodes.create_market_analyst(Llm())(_state())
    assert out == {"messages": ["r"], "market_report": "行情报告"}
    assert seen["tools"] == ["get_stock_data", "get_indicators", "get_verified_market_snapshot"]
    prompt = seen["prompt"]
    assert "market analyst for BTC-USD" in prompt and "look_back_days=1" in prompt
    assert "closed 15m bars" in prompt and "{" not in prompt.replace("{{", "")
    assert "This text has no labelled decision lines" in prompt
    assert "except the labelled lines" not in prompt


def test_language_sentence_names_only_the_roles_own_labels(tmp_path: Path) -> None:
    with run_config(_cfg(tmp_path)):
        rm, trader, pm = Structured({}), Structured({}), Structured({})
        for name, llm in (
            ("create_research_manager", rm),
            ("create_trader", trader),
            ("create_portfolio_manager", pm),
        ):
            llm.with_structured_output = None  # type: ignore[assignment]  # 走自由文本，只看提示词
            getattr(ta_nodes, name)(llm)(_full_state())
        bull = Llm()
        ta_nodes.create_bull_researcher(bull)(_state())
    assert '"**Recommendation**:"' in rm.prompts[0]
    assert '"FINAL TRANSACTION PROPOSAL:"' in trader.prompts[0][0]["content"]
    assert '"**Rating**:"' in pm.prompts[0]
    assert '"FINAL TRANSACTION PROPOSAL:"' not in pm.prompts[0]  # 状态里的交易员原文不算
    assert "do not add a rating" in bull.prompts[0]
    assert 'the "**Rating**:" line' not in bull.prompts[0]


def test_trader_structured_answer_is_rendered_with_the_closing_line(tmp_path: Path) -> None:
    from tradingagents.agents.schemas import TraderAction

    _, trader_schema, _ = ta_nodes._schemas()
    parsed = trader_schema(
        action=TraderAction.SELL,
        reasoning="反弹区",
        entry_zone=[101.5, 103.0],
        invalidation=104.2,
        take_profit="98",
        size_tier="size M (reduced)",
    )
    llm = Structured({"raw": None, "parsed": parsed, "parsing_error": None})
    with run_config(_cfg(tmp_path)):
        out = ta_nodes.create_trader(llm)(_full_state())
    plan = out["trader_investment_plan"]
    assert "**Entry Zone**: 101.5–103" in plan and "**Size Tier**: M" in plan
    assert plan.splitlines()[-1] == "FINAL TRANSACTION PROPOSAL: **SELL**"
    assert out["messages"][0].content == plan
    assert llm.kwargs == {"include_raw": True} and llm.plain_calls == 0


def test_failed_parse_uses_the_raw_answer_without_a_second_call(tmp_path: Path) -> None:
    raw = SimpleNamespace(content="**Recommendation**: Sell\n\n**Rationale**: 弱")
    llm = Structured({"raw": raw, "parsed": None, "parsing_error": ValueError("bad")})
    with run_config(_cfg(tmp_path)):
        out = ta_nodes.create_research_manager(llm)(_full_state())
    assert out["investment_plan"] == raw.content
    assert llm.structured_calls == 1 and llm.plain_calls == 0


def test_empty_raw_answer_or_error_falls_back_to_one_plain_call(tmp_path: Path) -> None:
    empty = {"raw": SimpleNamespace(content=""), "parsed": None, "parsing_error": ValueError("x")}
    for answer in (empty, RuntimeError("503")):
        llm = Structured(answer, plain="**Rating**: Hold\n\n**Executive Summary**: 观望")
        with run_config(_cfg(tmp_path)):
            out = ta_nodes.create_portfolio_manager(llm)(_full_state())
        assert llm.plain_calls == 1
        assert out["final_rating"] == "Hold" and out["final_trade_decision"] == llm.plain


def test_pm_structured_rating_and_trade_lines(tmp_path: Path) -> None:
    from tradingagents.agents.schemas import PortfolioRating

    _, _, pm_schema = ta_nodes._schemas()
    parsed = pm_schema(
        rating=PortfolioRating.UNDERWEIGHT,
        executive_summary="做空",
        investment_thesis="论点",
        entry_zone="101.5–103.0",
        invalidation="104.2",
        take_profit="98.0",
        size_tier="S",
    )
    llm = Structured({"raw": None, "parsed": parsed, "parsing_error": None})
    with run_config(_cfg(tmp_path)):
        out = ta_nodes.create_portfolio_manager(llm)(_full_state())
    assert out["final_rating"] == "Underweight"
    text = out["final_trade_decision"]
    assert text.startswith("**Rating**: Underweight")
    assert "**Entry Zone**: 101.5–103.0" in text and "**Size Tier**: S" in text
    assert "fade the first spike" in llm.prompts[0]  # 教训进了组合经理的提示词


def test_trader_drops_the_grounding_line_without_a_market_report(tmp_path: Path) -> None:
    llm = Structured({})
    llm.with_structured_output = None  # type: ignore[assignment]
    st = _full_state()
    st["market_report"] = ""
    with run_config(_cfg(tmp_path)):
        ta_nodes.create_trader(llm)(st)
    system = llm.prompts[0][0]["content"]
    assert "Take the price levels" not in system and "{GROUNDING}" not in system
    assert "Technical Market Report" not in llm.prompts[0][1]["content"]


def test_runtime_values_with_braces_are_not_re_expanded(tmp_path: Path) -> None:
    llm = Structured({})
    llm.with_structured_output = None  # type: ignore[assignment]
    st = _full_state()
    st["investment_debate_state"] = {**st["investment_debate_state"], "history": "see {target}"}
    with run_config(_cfg(tmp_path)):
        ta_nodes.create_research_manager(llm)(st)
    assert "see {target}" in llm.prompts[0]


def test_build_config_carries_the_lean_size(tmp_path: Path) -> None:
    from tradingagents.default_config import build_default_config

    s = eng.TradingAgentsSettings(results_dir=str(tmp_path), lean_size_tier=eng.SizeTier.S)
    cfg = eng.build_config(build_default_config(), s)
    with run_config(cfg):
        assert ta_nodes.lean_from_config() == "S"
        rm = Structured({})
        rm.with_structured_output = None  # type: ignore[assignment]
        ta_nodes.create_research_manager(rm)(_full_state())
    assert "size S or smaller" in rm.prompts[0]
