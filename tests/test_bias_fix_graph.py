"""偏空整改开关在 TradingAgents 图里生效（需要 tradingagents；CI 没装就跳过）。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("tradingagents")
pytest.importorskip("langchain_core")

from tradingagents.dataflows.config import run_config

from jiaoyi.slow import ta_nodes, ta_prompts, ta_roles
from jiaoyi.slow import tradingagents_engine as eng
from jiaoyi.slow.bias_fix import LEGACY_SETUP, BiasFix, MeetingSetup, resolve


class Llm:
    """记下收到的提示词，按顺序回「发言 n」。"""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(self, prompt: Any, *a: Any, **k: Any) -> Any:
        self.prompts.append(str(prompt))
        return SimpleNamespace(content=f"发言{len(self.prompts)}" + "。" * 60)


def _cfg(
    tmp_path: Path, setup: MeetingSetup = LEGACY_SETUP, debate_rounds: int = 1
) -> dict[str, Any]:
    from tradingagents.default_config import build_default_config

    s = eng.TradingAgentsSettings(results_dir=str(tmp_path), debate_rounds=debate_rounds)
    return eng.build_config(build_default_config(), s, setup=setup)


def _setup(**kw: Any) -> MeetingSetup:
    return resolve(BiasFix(**kw), "AAUSDT@2026-10-28T03:00:00Z")


def _state() -> dict[str, Any]:
    return {
        "company_of_interest": "BTC-USD",
        "asset_type": "crypto",
        "trade_date": "2026-10-28",
        "market_report": "行情报告",
        "sentiment_report": "",
        "news_report": "",
        "fundamentals_report": "",
        "investment_debate_state": {
            "history": "",
            "bull_history": "",
            "bear_history": "",
            "current_response": "",
            "count": 0,
        },
    }


def _prompt(tmp_path: Path, role: str, cfg: dict[str, Any]) -> str:
    llm = Llm()
    node = getattr(ta_nodes, f"create_{role}_researcher")(llm)
    with run_config(cfg):
        node(_state())
    return llm.prompts[0]


# ---------------------------------------------------------------- 研究员提示词
def test_researcher_prompts_are_unchanged_by_default(tmp_path: Path) -> None:
    """默认设置不往配置里写任何东西（和改动前一样）；显式写成现状值，提示词也一字不差。"""
    legacy = _cfg(tmp_path)
    assert not set(ta_nodes.SETUP_KEYS) & set(legacy[ta_nodes.PROMPT_KEY])
    spelled = _cfg(tmp_path)
    spelled[ta_nodes.PROMPT_KEY].update(
        debate_first="bull",
        funding_label="legacy",
        bear_bull_symmetry="legacy",
        crowd_ratio_mode="raw",
    )
    for role in ("bull", "bear"):
        assert _prompt(tmp_path, role, legacy) == _prompt(tmp_path, role, spelled)


def test_mirror_and_neutral_funding_change_only_their_line(tmp_path: Path) -> None:
    legacy = {r: _prompt(tmp_path, r, _cfg(tmp_path)) for r in ("bull", "bear")}
    mirror = _cfg(tmp_path, _setup(bear_bull_symmetry="mirror"))
    p = ta_prompts.PromptParams.for_hold("2h")
    change = ta_prompts.CHANGE_MIRROR["short"]
    for role in ("bull", "bear"):
        want = (
            legacy[role]
            .replace(
                ta_prompts.role_prompt(role, p, "BTC-USD"),
                ta_prompts.role_prompt(role, p, "BTC-USD", mirror=True),
            )
            .replace(*change)  # 共用说明里「行情变了要改判断」的例子
        )
        assert _prompt(tmp_path, role, mirror) == want != legacy[role]
    neutral = _cfg(tmp_path, _setup(funding_label="neutral"))
    assert _prompt(tmp_path, "bear", neutral) == legacy["bear"].replace(
        ta_prompts.BEAR_FUNDING_OLD, ta_prompts.BEAR_FUNDING_NEW
    )
    assert _prompt(tmp_path, "bear", neutral) != legacy["bear"]
    assert _prompt(tmp_path, "bull", neutral) == legacy["bull"]


# ---------------------------------------------------------------- 资金费率（数据工具里拼的）
def test_derivatives_block_reads_the_funding_label_of_this_meeting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    def get(url: str, params: dict[str, Any]) -> Any:
        rows = (
            [{"fundingRate": "0.00005"}]
            if "fundingRate" in url
            else [{"sumOpenInterestValue": "100"}, {"sumOpenInterestValue": "110"}]
        )
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: rows)

    monkeypatch.setattr(eng.binance_gate, "get", get)
    end = datetime(2026, 10, 28, tzinfo=UTC)
    outside = eng.derivatives_context("BTCUSDT", end)  # 不在会议里 = 现状
    with run_config(_cfg(tmp_path)):
        legacy = eng.derivatives_context("BTCUSDT", end)
    with run_config(_cfg(tmp_path, _setup(funding_label="neutral"))):
        neutral = eng.derivatives_context("BTCUSDT", end)
    assert outside == legacy and eng.FUNDING_LABEL_LEGACY in legacy
    assert neutral == legacy.replace(eng.FUNDING_LABEL_LEGACY, eng.neutral_funding_label(None))


# ---------------------------------------------------------------- 多空比
def test_sentiment_analyst_asks_for_relative_only_when_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from jiaoyi.slow import crowd

    ta_nodes.install()
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fetch(*a: Any, **k: Any) -> tuple[list[str], int]:
        calls.append((a, k))
        return ["### Global long/short account ratio\n- Latest 1.8"], 1

    monkeypatch.setattr(crowd, "fetch_blocks", fetch)
    monkeypatch.setattr(ta_nodes, "_NEWS_HUB", None)
    for setup in (LEGACY_SETUP, _setup(crowd_ratio_mode="relative")):
        with run_config(_cfg(tmp_path, setup)):
            ta_nodes.create_sentiment_analyst(Llm())(
                {**_state(), "messages": [], "instrument_context": ""}
            )
    assert calls == [(("BTCUSDT", "15m"), {}), (("BTCUSDT", "15m"), {"ratio_mode": "relative"})]


# ---------------------------------------------------------------- 谁先开口（真跑一遍图）
def _run_debate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setup: MeetingSetup, rounds: int = 1
) -> tuple[Any, Llm, dict[str, Any]]:
    from langchain_core.messages import AIMessage
    from tradingagents.graph import setup as ta_setup

    monkeypatch.setenv("DEEPSEEK_API_KEY", "x" * 16)
    ta_nodes.install()
    llm = Llm()

    def analyst(_llm: Any) -> Any:
        return lambda st: {"messages": [AIMessage(content="r")], "market_report": "行情报告"}

    def fixed(out: dict[str, Any]) -> Any:
        return lambda _llm: lambda st: out

    risk_done = {
        "risk_debate_state": {
            "history": "",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "latest_speaker": "Neutral",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 99,
        }
    }
    monkeypatch.setattr(ta_setup, "create_market_analyst", analyst)
    monkeypatch.setattr(
        ta_setup, "create_bull_researcher", lambda _l: ta_nodes.create_bull_researcher(llm)
    )
    monkeypatch.setattr(
        ta_setup, "create_bear_researcher", lambda _l: ta_nodes.create_bear_researcher(llm)
    )
    monkeypatch.setattr(ta_setup, "create_research_manager", fixed({"investment_plan": "p"}))
    monkeypatch.setattr(ta_setup, "create_trader", fixed({"trader_investment_plan": "t"}))
    for name in ("aggressive", "conservative", "neutral"):
        monkeypatch.setattr(ta_setup, f"create_{name}_debator", fixed(risk_done))
    monkeypatch.setattr(ta_setup, "create_portfolio_manager", fixed({"final_trade_decision": "x"}))
    g = ta_nodes.perp_graph_class()(
        selected_analysts=("market",),
        config=_cfg(tmp_path, setup, rounds),
        callbacks=[],
    )
    with run_config(g.config):
        state = g.create_run_state("BTC-USD", "2026-10-28", "crypto")
        final = g.graph.invoke(state, **g.propagator.get_graph_args())
    return g, llm, final


def _speakers(llm: Llm) -> list[str]:
    return ["bull" if p.startswith("You are the Bull") else "bear" for p in llm.prompts]


def test_default_debate_order_is_bull_then_bear(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    g, llm, final = _run_debate(monkeypatch, tmp_path, LEGACY_SETUP)
    assert g.debate_first == "bull" and _speakers(llm) == ["bull", "bear"]
    assert final["investment_debate_state"]["current_response"].startswith("Bear Analyst:")


def test_bear_first_gives_the_bull_the_last_word(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    g, llm, final = _run_debate(monkeypatch, tmp_path, _setup(debate_first="bear"))
    assert g.debate_first == "bear" and _speakers(llm) == ["bear", "bull"]
    debate = final["investment_debate_state"]
    assert debate["current_response"].startswith("Bull Analyst:") and debate["count"] == 2
    assert debate["history"].index("Bear Analyst:") < debate["history"].index("Bull Analyst:")
    # 多头这次是在回应空头刚说的
    assert "Last bear argument: Bear Analyst: 发言1" in llm.prompts[1]
    assert final["final_trade_decision"] == "x"  # 后面的流程照常走完


def test_bear_first_with_two_rounds_alternates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, llm, _ = _run_debate(monkeypatch, tmp_path, _setup(debate_first="bear"), rounds=2)
    assert _speakers(llm) == ["bear", "bull", "bear", "bull"]
    _, llm, _ = _run_debate(monkeypatch, tmp_path, LEGACY_SETUP, rounds=2)
    assert _speakers(llm) == ["bull", "bear", "bull", "bear"]


def test_open_with_bear_refuses_an_unexpected_graph() -> None:
    from langgraph.graph import StateGraph
    from tradingagents.agents.state import AgentState

    wf = StateGraph(AgentState)
    with pytest.raises(RuntimeError, match="版本对不上"):
        ta_nodes.open_debate_with_bear(wf)


# ------------------------------------------------------------ mirror：经理 / 交易员 / 行情分析师
class _Structured(Llm):
    def __init__(self) -> None:
        super().__init__()
        self.schemas: list[Any] = []

    def with_structured_output(self, schema: Any, **k: Any) -> Any:
        self.schemas.append(schema)
        outer = self

        class Bound:
            def invoke(self, prompt: Any) -> dict[str, Any]:  # 解析失败、用模型原文
                outer.prompts.append(str(prompt))
                return {"parsed": None, "raw": SimpleNamespace(content="Hold")}

        return Bound()


def _desc(schema: Any) -> str:
    return str(schema.model_json_schema()["properties"]["entry_zone"]["description"])


def test_mirror_reaches_the_market_analyst_and_the_managers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tradingagents.agents.analysts import turn

    seen: list[str] = []

    def take_turn(prompt: Any, llm: Any, tools: Any, messages: Any) -> tuple[str, str]:
        seen.append(prompt.format_messages(messages=[])[0].content)
        return "r", "行情报告"

    monkeypatch.setattr(turn, "take_turn", take_turn)
    state = {
        **_state(),
        "messages": [],
        "instrument_context": "",
        "portfolio_context": "",
        "investment_plan": "plan",
        "trader_investment_plan": "t",
        "past_context": "",
        "risk_debate_state": {
            "history": "h",
            "aggressive_history": "",
            "conservative_history": "",
            "neutral_history": "",
            "latest_speaker": "Neutral",
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
            "count": 3,
        },
    }
    out: dict[str, dict[str, str]] = {}
    schemas: dict[str, list[Any]] = {}
    for name, setup in (("legacy", LEGACY_SETUP), ("mirror", _setup(bear_bull_symmetry="mirror"))):
        llms = {k: _Structured() for k in ("rm", "trader", "pm")}
        rm = ta_nodes.create_research_manager(llms["rm"])
        trader = ta_nodes.create_trader(llms["trader"])
        pm = ta_nodes.create_portfolio_manager(llms["pm"])
        with run_config(_cfg(tmp_path, setup)):
            ta_nodes.create_market_analyst(Llm())(state)
            rm(state)
            trader(state)
            pm(state)
        out[name] = {
            "market": seen.pop(),
            "rm": llms["rm"].prompts[-1],
            "trader": llms["trader"].prompts[-1],
            "pm": llms["pm"].prompts[-1],
        }
        schemas[name] = llms["pm"].schemas + llms["trader"].schemas
    legacy, mirror = out["legacy"], out["mirror"]
    assert ta_roles.MARKET_MIRROR_NEW in mirror["market"]
    assert ta_roles.MARKET_MIRROR_OLD in legacy["market"]
    assert "The short case wins on fresh evidence" in mirror["rm"]
    assert "the mirror of Buy" in legacy["rm"]
    assert "waits for a pullback or a bounce" in mirror["pm"]
    assert "waits for a pullback or a bounce" not in legacy["pm"]
    assert "a bounce off the low or a pullback from the high" in mirror["trader"]
    assert "a bounce off the low" not in legacy["trader"]
    # 结构化字段：建节点时绑的还是原来那份，mirror 的会议另绑一份
    assert all("pullback or a bounce" not in _desc(s) for s in schemas["legacy"])
    assert sum("pullback or a bounce" in _desc(s) for s in schemas["mirror"]) == 2


def test_mirror_output_language_and_default_config_unchanged(tmp_path: Path) -> None:
    legacy = _cfg(tmp_path)
    mirror = _cfg(tmp_path, _setup(bear_bull_symmetry="mirror"))
    want = legacy["output_language"].replace(*ta_prompts.CHANGE_MIRROR["short"])
    assert mirror["output_language"] == want != legacy["output_language"]


def test_meeting_setting_reads_only_the_per_run_config(tmp_path: Path) -> None:
    """建图时 set_config 把配置逐键合并进进程全局，上一场 mirror 的键会留着：只读按场那份。"""
    from tradingagents.dataflows.config import set_config

    set_config(_cfg(tmp_path, _setup(bear_bull_symmetry="mirror", funding_label="neutral")))
    try:
        assert ta_nodes.meeting_setting("bear_bull_symmetry", "legacy") == "legacy"
        assert ta_nodes.meeting_setting("funding_label", "legacy") == "legacy"
        with run_config(_cfg(tmp_path)):  # 这场是现状组：全局里那份 mirror 不能漏进来
            assert ta_nodes.meeting_setting("bear_bull_symmetry", "legacy") == "legacy"
        with run_config(_cfg(tmp_path, _setup(bear_bull_symmetry="mirror"))):
            assert ta_nodes.meeting_setting("bear_bull_symmetry", "legacy") == "mirror"
    finally:
        set_config(_cfg(tmp_path))
        from tradingagents.dataflows import config as ta_config

        ta_config._config = None
        ta_config.initialize_config()
