"""换掉 TradingAgents 里写死股票口径的角色节点，以及每场会不同内容的 ``PerpGraph``。

做法：``tradingagents.graph.setup`` 是按名字引用 ``create_*`` 的，所以替换模块属性就生效，
不改开源代码。新节点的状态读写和原版（0.7.0，e8f5503）完全一样，只换提示词正文；
持仓档的参数（{H} 等）从这场会的运行配置里读（``jiaoyi_prompt``，由引擎的 ``build_config`` 写入，
只跟档位有关，所有会议一样；偏空整改设置例外，见 ``SETUP_KEYS``）。每场会不同的东西
（总盘会的判断、挑币理由、持仓说明）走 ``PerpGraph`` 的 state，不进配置，所以同时开几场也不会串。

替换的角色：多空研究员、风控三人、新闻分析师、情绪分析师（加密币；美股永续照旧走原版）。
只在用到时才导入 tradingagents。
"""

from __future__ import annotations

import functools
import logging
import re
import threading
from collections.abc import Callable, Mapping
from typing import Any

from jiaoyi.slow import ta_prompts, ta_roles

logger = logging.getLogger(__name__)

PROMPT_KEY = "jiaoyi_prompt"

Node = Callable[[Any], dict[str, Any]]
Factory = Callable[[Any], Node]


def params_from_config() -> ta_prompts.PromptParams:
    from tradingagents.dataflows.config import get_config

    raw = get_config().get(PROMPT_KEY) or {}
    return ta_prompts.PromptParams.for_hold(
        str(raw.get("hold") or "2h"), int(raw["hold_minutes"]) if raw.get("hold_minutes") else None
    )


def lean_from_config() -> str:
    """偏向（Overweight / Underweight）最多给到的仓位档（S / M / L），由 ``build_config`` 写入。"""
    from tradingagents.dataflows.config import get_config

    raw = get_config().get(PROMPT_KEY) or {}
    lean = str(raw.get("lean") or "M").upper()
    return lean if lean in ("S", "M", "L") else "M"


SETUP_KEYS = ("debate_first", "funding_label", "bear_bull_symmetry", "crowd_ratio_mode")
"""这场会实际用的偏空整改设置（bias_fix.MeetingSetup），由 ``build_config`` 写进 ``jiaoyi_prompt``。

和档位参数不同，这几项每场会可能不一样（对照分组 / 交替先发言）。照样放配置是因为资金费率那段
是数据工具（``get_stock_data``）里拼的，工具只看得到配置、看不到 state；开会时 TradingAgents
按场用 ContextVar 给每张图自己的配置（``run_config``），同时开几场不会串。

只从这个按场的 ContextVar 读（``meeting_setting``）：建图时 ``set_config`` 会把配置合并进进程全局，
而且字典是逐键合并的，上一场 mirror 写进去的键下一场现状组不会清掉，所以全局那份不能信。"""


def _run_config_var() -> Any:
    """TradingAgents 按场配置的 ContextVar。版本变了找不到就报错：宁可开不了会，也不能悄悄按
    现状开、却在日志里记成 mirror / neutral。"""
    from tradingagents.dataflows import config as ta_config

    var = getattr(ta_config, "_run_config", None)
    if var is None or not callable(getattr(var, "get", None)):
        raise RuntimeError(
            "TradingAgents 里没有按场配置 _run_config，版本对不上，先核对 pyproject 里钉的提交"
        )
    return var


def meeting_setting(key: str, default: str) -> str:
    """读这场会的一项偏空整改设置，只看这场会自己的运行配置；不在会议里（或没装
    tradingagents）就是默认值 = 现状。TradingAgents 装了却找不到按场配置就报错。"""
    try:
        import tradingagents.dataflows.config  # noqa: F401
    except ImportError:
        return default
    scoped = _run_config_var().get()
    if not isinstance(scoped, Mapping):
        return default
    raw = scoped.get(PROMPT_KEY) or {}
    return str(raw.get(key) or default)


def check_meeting_settings() -> None:
    """启动自检（开了任何偏空整改开关时由引擎调用）：在一场假的按场配置里读回设置，读不回就
    拒绝启动，免得记录里写着 mirror / neutral 而提示词其实没用上。"""
    from tradingagents.dataflows.config import run_config

    _run_config_var()
    probe = {k: f"probe-{k}" for k in SETUP_KEYS}
    with run_config({PROMPT_KEY: probe}):
        got = {k: meeting_setting(k, "") for k in SETUP_KEYS}
    if got != probe or meeting_setting(SETUP_KEYS[0], "legacy") != "legacy":
        raise RuntimeError(f"偏空整改设置读不回这场会的配置（读到 {got}），拒绝启动")


def _mirror() -> bool:
    """这场会是否 bear_bull_symmetry = mirror（多空写法逐句对称）。"""
    return meeting_setting("bear_bull_symmetry", "legacy") == "mirror"


def language(role: str | None = None) -> str:
    """替换 TradingAgents 的 ``get_language_instruction()``：按角色说清哪些标签行保持英文，
    共用说明也按角色组给（新闻 / 情绪不收评级和进场规则，见 ``ta_prompts.ROLE_GROUP``）。"""
    from tradingagents.dataflows.config import get_config

    cfg = get_config()
    base = (cfg.get(PROMPT_KEY) or {}).get("lang")
    if base:
        if str(base).strip().lower() == "english":
            return ""
        group = ta_prompts.ROLE_GROUP.get(role or "", "manager")
        lang = f"{base}. {ta_prompts.perp_context(params_from_config(), group, mirror=_mirror())}"
    else:  # 老配置：整段 output_language
        lang = str(cfg.get("output_language", "English"))
        if lang.strip().lower() == "english":
            return ""
    return ta_roles.language_line(lang, role)


def _reports(state: Any) -> dict[str, str]:
    from tradingagents.agents.context import report_or_absent

    return {
        "market": report_or_absent(state["market_report"], "market"),
        "sentiment": report_or_absent(state["sentiment_report"], "sentiment"),
        "news": report_or_absent(state["news_report"], "news"),
        "fundamentals": report_or_absent(state["fundamentals_report"], "fundamentals"),
    }


def _resources(state: Any, p: ta_prompts.PromptParams) -> str:
    from tradingagents.agents.context import get_instrument_context_from_state

    r = _reports(state)
    return (
        f"{get_instrument_context_from_state(state)}\n"
        f"Market research report: {r['market']}\n"
        f"Crowd-positioning / sentiment report: {r['sentiment']}\n"
        f"News report (what can move this coin within {p.horizon}): {r['news']}\n"
        f"Asset fundamentals report (usually unavailable for crypto): {r['fundamentals']}\n"
    )


def _researcher(role: str, label: str, other: str) -> Factory:
    """多空研究员节点工厂（role = bull / bear，other 是对手）。"""

    def create(llm: Any) -> Node:
        def node(state: Any) -> dict[str, Any]:
            from tradingagents.agents.context import opponent_argument_or_opening

            debate = state["investment_debate_state"]
            history = debate.get("history", "")
            own_history = debate.get(f"{role}_history", "")
            reply = opponent_argument_or_opening(
                debate.get("current_response", ""), f"{other} analyst"
            )
            p = params_from_config()
            prompt = (
                ta_prompts.role_prompt(
                    role,
                    p,
                    str(state.get("company_of_interest", "asset")),
                    mirror=_mirror(),
                    neutral_funding=meeting_setting("funding_label", "legacy") == "neutral",
                )
                + "\n\nResources available:\n"
                + _resources(state, p)
                + f"Conversation history of the debate: {history}\n"
                + f"Last {other} argument: {reply}\n"
                + f"Use this information to deliver your argument and engage directly with the "
                f"{other}'s latest points." + language(role)
            )
            response = llm.invoke(prompt)
            argument = f"{label} Analyst: {response.content}"
            new = {
                "history": history + "\n" + argument,
                "bull_history": debate.get("bull_history", ""),
                "bear_history": debate.get("bear_history", ""),
                "current_response": argument,
                "count": debate["count"] + 1,
            }
            new[f"{role}_history"] = own_history + "\n" + argument
            return {"investment_debate_state": new}

        return node

    return create


def _debator(key: str, label: str) -> Factory:
    """风控三人节点工厂；key = aggressive / conservative / neutral。"""
    others = [k for k in ("aggressive", "conservative", "neutral") if k != key]

    def create(llm: Any) -> Node:
        def node(state: Any) -> dict[str, Any]:
            from tradingagents.agents.context import (
                get_portfolio_context_from_state,
                opponent_argument_or_opening,
            )

            rs = state["risk_debate_state"]
            history = rs.get("history", "")
            p = params_from_config()
            lines = [
                ta_prompts.role_prompt(key, p),
                "",
                "Here is the trader's decision:",
                str(state["trader_investment_plan"]),
                "",
                "Resources available:",
                get_portfolio_context_from_state(state),
                _resources(state, p),
                f"Here is the current conversation history: {history}",
            ]
            for o in others:
                said = opponent_argument_or_opening(
                    rs.get(f"current_{o}_response", ""), f"{o} analyst"
                )
                lines.append(f"Last argument from the {o} analyst: {said}")
            lines.append(
                "Respond directly to the other analysts' latest points with data. Keep it "
                "conversational and without special formatting; name the size tier (S, M or L), "
                "the entry zone and the stop you would accept."
            )
            response = llm.invoke("\n".join(lines) + language(key))
            argument = f"{label} Analyst: {response.content}"
            new = {
                "history": history + "\n" + argument,
                "aggressive_history": rs.get("aggressive_history", ""),
                "conservative_history": rs.get("conservative_history", ""),
                "neutral_history": rs.get("neutral_history", ""),
                "latest_speaker": label,
                "current_aggressive_response": rs.get("current_aggressive_response", ""),
                "current_conservative_response": rs.get("current_conservative_response", ""),
                "current_neutral_response": rs.get("current_neutral_response", ""),
                "count": rs["count"] + 1,
            }
            new[f"{key}_history"] = rs.get(f"{key}_history", "") + "\n" + argument
            new[f"current_{key}_response"] = argument
            return {"risk_debate_state": new}

        return node

    return create


create_bull_researcher = _researcher("bull", "Bull", "bear")
create_bear_researcher = _researcher("bear", "Bear", "bull")
create_aggressive_debator = _debator("aggressive", "Aggressive")
create_conservative_debator = _debator("conservative", "Conservative")
create_neutral_debator = _debator("neutral", "Neutral")

_ORIGINALS: dict[str, Any] = {}  # 被我们替换掉的原版工厂（第一次 install 时记下）


def binance_symbol_of(ticker: str) -> str:
    t = ticker.upper().removesuffix("-USD").removesuffix("USDT")
    return f"{t}USDT"


def create_news_analyst(llm: Any) -> Node:
    """新闻分析师：加密币用加密版提示词（不绑宏观工具），美股永续走原版。"""
    original = _ORIGINALS["create_news_analyst"](llm)

    def node(state: Any) -> dict[str, Any]:
        if state.get("asset_type", "stock") != "crypto":
            out: dict[str, Any] = original(state)
            return out
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
        from tradingagents.agents.analysts.turn import take_turn
        from tradingagents.agents.context import (
            get_instrument_context_from_state,
        )
        from tradingagents.agents.tools import (
            get_global_news,
            get_news,
            get_prediction_markets,
        )

        tools = (get_news, get_global_news, get_prediction_markets)
        p = params_from_config()
        system_message = ta_prompts.news_prompt(
            p, binance_symbol_of(str(state["company_of_interest"]))
        ) + language("news")
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with"
                    " different tools will help where you left off."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and"
                    " tool-call date ranges. {instrument_context}\n{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )
        prompt = prompt.partial(
            system_message=system_message,
            tool_names=", ".join(t.name for t in tools),
            current_date=state["trade_date"],
            instrument_context=get_instrument_context_from_state(state),
        )
        result, report = take_turn(prompt, llm, tools, state["messages"])
        return {"messages": [result], "news_report": report}

    return node


_NEWS_HUB: Any = None


def set_news_hub(hub: Any) -> None:
    """情绪分析师读这个币最近 24 小时的新闻标题用（引擎启动时设置；没有就不加这一块）。"""
    global _NEWS_HUB
    _NEWS_HUB = hub


CROWD_MAX = 600
_CROWD = (
    "You are the crowd-positioning analyst for {SYMBOL} perpetual. Report who is positioned "
    "which way over the last hours and whether that crowd is likely to be squeezed within the "
    "next {H}. End with a one-line verdict: crowd leaning long / short / neutral, and whether "
    "that positioning is fuel for a move against it. The verdict is about the crowd, not a "
    "trade: do not recommend one (no long, short or flat call for this coin, no size, entry, "
    "stop or target); later roles decide those. Keep the text within about {CROWD_MAX} Chinese "
    "characters before the short table."
)
CROWD_UNAVAILABLE = (
    "Crowd-positioning data for {SYMBOL} is unavailable this round (the Binance positioning "
    "endpoints returned nothing and there are no recent headlines). Do not infer the crowd's "
    "positioning from other reports."
)


def create_sentiment_analyst(llm: Any) -> Node:
    """情绪分析师：加密币看币安持仓人群（谁站哪边、会不会被挤），美股永续走原版。"""
    original = _ORIGINALS["create_sentiment_analyst"](llm)

    def node(state: Any) -> dict[str, Any]:
        if state.get("asset_type", "stock") != "crypto":
            out: dict[str, Any] = original(state)
            return out
        from langchain_core.messages import AIMessage
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
        from tradingagents.agents.context import (
            get_instrument_context_from_state,
        )

        from jiaoyi.slow import crowd
        from jiaoyi.slow.news import format_items

        p = params_from_config()
        sym = binance_symbol_of(str(state["company_of_interest"]))
        if meeting_setting("crowd_ratio_mode", "raw") == "relative":
            blocks, ok = crowd.fetch_blocks(sym, p.indicator, ratio_mode="relative")
        else:
            blocks, ok = crowd.fetch_blocks(sym, p.indicator)
        items = _NEWS_HUB.for_symbol(sym, hours=24, limit=8) if _NEWS_HUB is not None else []
        if ok == 0 and not items:
            text = ta_prompts.fill(CROWD_UNAVAILABLE.replace("{SYMBOL}", sym), p)
            return {"messages": [AIMessage(content=text)], "sentiment_report": text}
        headlines = format_items(items)
        system_message = (
            ta_prompts.fill(
                _CROWD.replace("{SYMBOL}", sym).replace("{CROWD_MAX}", str(CROWD_MAX)), p
            )
            + "\n\nThe data below was collected for you from Binance public endpoints (each "
            "block is a time series summarized for the last 24 hours) and from the exchange/news "
            "feed; report on it and do not invent numbers. If a block says unavailable, say so.\n\n"
            + "\n\n".join(blocks)
            + f"\n\n### Headlines mentioning this coin, last 24 hours\n{headlines}"
            + "\n\nMake sure to append a short Markdown table at the end of the report."
            + language("sentiment")
        )
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Report what your data supports; another agent decides the trade."
                    " Today's date is {current_date}; treat it as 'now'. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )
        prompt = prompt.partial(
            system_message=system_message,
            current_date=state["trade_date"],
            instrument_context=get_instrument_context_from_state(state),
        )
        report = llm.invoke(prompt.format_messages(messages=state["messages"]))
        text = str(report.content)
        return {"messages": [AIMessage(content=text)], "sentiment_report": text}

    return node


# ------------------------------------------------------- 另外四个角色（行情分析师、三位经理）
_SLOT = re.compile(
    r"\{(target|ticker|instrument_context|history|report_section|portfolio_context|"
    r"investment_plan|research_plan|trader_plan|lessons_line)\}"
)


def _subst(text: str, values: Mapping[str, str]) -> str:
    """一次替换运行时槽位：填进去的内容（辩论记录等）里再有花括号也不会被当成槽位二次替换。"""
    return _SLOT.sub(lambda m: values.get(m.group(1), m.group(0)), text)


def _tier_letter(v: Any) -> str | None:
    m = re.search(r"\b([SML])\b", str(v).upper()) if v is not None else None
    return m.group(1) if m else None


def _as_text(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        return "–".join(f"{x:g}" if isinstance(x, (int, float)) else str(x) for x in v)
    return str(v).strip() or None


@functools.cache
def _schemas(mirror: bool = False) -> tuple[Any, Any, Any]:
    """研究经理 / 交易员 / 组合经理的结构化输出模型（字段说明见 ``ta_roles``）。
    ``mirror``：交易字段说明用 mirror 版（「等回调」写成「等回调或反弹」）。"""
    trade_fields = ta_roles.TRADE_FIELDS_MIRROR if mirror else ta_roles.TRADE_FIELDS
    from pydantic import BaseModel, Field, field_validator
    from tradingagents.agents.schemas import (
        PortfolioRating,
        ResearchPlan,
        TraderAction,
    )

    class PerpResearchPlan(ResearchPlan):  # type: ignore[misc]
        recommendation: PortfolioRating = Field(description=ta_roles.RM_FIELDS["recommendation"])
        rationale: str = Field(description=ta_roles.RM_FIELDS["rationale"])
        strategic_actions: str = Field(description=ta_roles.RM_FIELDS["strategic_actions"])

    class _Trade(BaseModel):
        entry_zone: str | None = Field(default=None, description=trade_fields["entry_zone"])
        invalidation: str | None = Field(
            default=None, description=ta_roles.TRADE_FIELDS["invalidation"]
        )
        take_profit: str | None = Field(
            default=None, description=ta_roles.TRADE_FIELDS["take_profit"]
        )
        size_tier: str | None = Field(default=None, description=ta_roles.TRADE_FIELDS["size_tier"])

        @field_validator("entry_zone", "invalidation", "take_profit", mode="before")
        @classmethod
        def _text(cls, v: Any) -> str | None:
            return _as_text(v)

        @field_validator("size_tier", mode="before")
        @classmethod
        def _tier(cls, v: Any) -> str | None:
            return _tier_letter(v)

    class PerpTraderProposal(_Trade):
        action: TraderAction = Field(description=ta_roles.TRADER_FIELDS["action"])
        reasoning: str = Field(description=ta_roles.TRADER_FIELDS["reasoning"])

    class PerpPortfolioDecision(_Trade):
        rating: PortfolioRating = Field(description=ta_roles.PM_FIELDS["rating"])
        executive_summary: str = Field(description=ta_roles.PM_FIELDS["executive_summary"])
        investment_thesis: str = Field(description=ta_roles.PM_FIELDS["investment_thesis"])

    return PerpResearchPlan, PerpTraderProposal, PerpPortfolioDecision


def _bind(llm: Any, schema: Any, name: str) -> Any | None:
    """结构化输出绑定（带原始回答，解析失败时直接用原始回答，不再多发一次请求）。"""
    try:
        return llm.with_structured_output(schema, include_raw=True)
    except (NotImplementedError, AttributeError, TypeError) as exc:
        logger.warning("%s: no structured output (%s); using free text", name, exc)
        return None


class _Structured:
    """交易员 / 组合经理的结构化绑定：默认那份建节点时就绑好（和原来一样），mirror 那份用到再绑。"""

    def __init__(self, llm: Any, index: int, name: str) -> None:
        self._llm, self._index, self._name = llm, index, name
        self._bound = {False: _bind(llm, _schemas()[index], name)}

    def get(self, mirror: bool) -> Any | None:
        if mirror not in self._bound:
            self._bound[mirror] = _bind(self._llm, _schemas(mirror)[self._index], self._name)
        return self._bound[mirror]


def _ask(structured: Any | None, plain: Any, prompt: Any, name: str) -> tuple[Any | None, str]:
    """(解析好的对象, "") 或 (None, 文字)。结构化解析失败但模型答了文字，就直接用这段文字；
    只有一个字都没答（或请求本身出错）才再发一次普通请求。"""
    if structured is not None:
        try:
            out = structured.invoke(prompt)
        except Exception as exc:
            logger.warning("%s: structured call failed (%s); retrying as free text", name, exc)
            out = None
        if isinstance(out, dict):
            if out.get("parsed") is not None:
                return out["parsed"], ""
            raw = out.get("raw")
            text = getattr(raw, "content", "") if raw is not None else ""
            if isinstance(text, str) and text.strip():
                logger.info("%s: structured parse failed; using the model's own text", name)
                return None, text
        elif out is not None:  # 测试替身直接返回解析后的对象
            return out, ""
    return None, str(plain.invoke(prompt).content)


def create_market_analyst(llm: Any) -> Node:
    """行情分析师：加密永续版提示词；状态读写和原版一样（写 messages 和 market_report）。"""

    def node(state: Any) -> dict[str, Any]:
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
        from tradingagents.agents.analysts.market_analyst import TOOLS
        from tradingagents.agents.analysts.turn import take_turn
        from tradingagents.agents.context import get_instrument_context_from_state

        p = params_from_config()
        text = ta_roles.fill(ta_roles.MARKET, p, lean_from_config(), mirror=_mirror())
        system_message = _subst(text, {"target": str(state["company_of_interest"])}) + language(
            "market"
        )
        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with"
                    " different tools will help where you left off. Execute what you can to make"
                    " progress. Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and"
                    " tool-call date ranges. {instrument_context}\n{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )
        prompt = prompt.partial(
            system_message=system_message,
            tool_names=", ".join(t.name for t in TOOLS),
            current_date=state["trade_date"],
            instrument_context=get_instrument_context_from_state(state),
        )
        result, report = take_turn(prompt, llm, TOOLS, state["messages"])
        return {"messages": [result], "market_report": report}

    return node


def create_research_manager(llm: Any) -> Node:
    """研究经理：写 investment_debate_state 和 investment_plan，和原版一样。"""
    schema = _schemas()[0]
    structured = _bind(llm, schema, "Research Manager")

    def node(state: Any) -> dict[str, Any]:
        from tradingagents.agents.context import get_instrument_context_from_state
        from tradingagents.agents.schemas import render_research_plan

        debate = state["investment_debate_state"]
        p = params_from_config()
        prompt = _subst(
            ta_roles.fill(ta_roles.RM, p, lean_from_config(), mirror=_mirror()),
            {
                "target": str(state["company_of_interest"]),
                "instrument_context": get_instrument_context_from_state(state),
                "history": debate.get("history", ""),
            },
        ) + language("rm")
        parsed, text = _ask(structured, llm, prompt, "Research Manager")
        plan = render_research_plan(parsed) if parsed is not None else text
        new = {
            "history": debate.get("history", ""),
            "bear_history": debate.get("bear_history", ""),
            "bull_history": debate.get("bull_history", ""),
            "current_response": plan,
            "count": debate["count"],
        }
        return {"investment_debate_state": new, "investment_plan": plan}

    return node


def create_trader(llm: Any) -> Node:
    """交易员：写 messages（一条 AIMessage）和 trader_investment_plan，和原版一样。"""
    structured = _Structured(llm, 1, "Trader")

    def node(state: Any) -> dict[str, Any]:
        from langchain_core.messages import AIMessage
        from tradingagents.agents.context import (
            get_instrument_context_from_state,
            get_portfolio_context_from_state,
        )

        p = params_from_config()
        lean = lean_from_config()
        mirror = _mirror()
        report = (state["market_report"] or "").strip()
        system = ta_roles.TRADER_SYSTEM
        if not report:  # 没有行情报告就不让它「从报告里取价位」
            system = system.replace("{GROUNDING}", "")
        values = {
            "target": str(state["company_of_interest"]),
            "ticker": str(state["company_of_interest"]),
            "instrument_context": get_instrument_context_from_state(state),
            "report_section": f"Technical Market Report:\n{report}\n\n" if report else "",
            "portfolio_context": get_portfolio_context_from_state(state),
            "investment_plan": state["investment_plan"],
        }
        messages = [
            {
                "role": "system",
                "content": _subst(ta_roles.fill(system, p, lean, mirror=mirror), values)
                + language("trader"),
            },
            {
                "role": "user",
                "content": _subst(
                    ta_roles.fill(ta_roles.TRADER_USER, p, lean, mirror=mirror), values
                ),
            },
        ]
        parsed, text = _ask(structured.get(mirror), llm, messages, "Trader")
        if parsed is not None:
            text = ta_roles.render_trader(
                parsed.action.value,
                parsed.reasoning,
                parsed.entry_zone,
                parsed.invalidation,
                parsed.take_profit,
                parsed.size_tier,
            )
        return {"messages": [AIMessage(content=text)], "trader_investment_plan": text}

    return node


def create_portfolio_manager(llm: Any) -> Node:
    """组合经理：写 risk_debate_state、final_trade_decision、final_rating，和原版一样。"""
    structured = _Structured(llm, 2, "Portfolio Manager")

    def node(state: Any) -> dict[str, Any]:
        from tradingagents.agents.context import (
            get_instrument_context_from_state,
            get_portfolio_context_from_state,
        )
        from tradingagents.agents.rating import parse_rating

        rs = state["risk_debate_state"]
        p = params_from_config()
        mirror = _mirror()
        past = state.get("past_context", "")
        lessons = ta_roles.LESSONS_LINE.replace("{past_context}", str(past)) if past else ""
        prompt = _subst(
            ta_roles.fill(ta_roles.PM, p, lean_from_config(), mirror=mirror),
            {
                "target": str(state["company_of_interest"]),
                "instrument_context": get_instrument_context_from_state(state),
                "portfolio_context": get_portfolio_context_from_state(state),
                "research_plan": str(state["investment_plan"]),
                "trader_plan": str(state["trader_investment_plan"]),
                "lessons_line": lessons,
                "history": rs["history"],
            },
        ) + language("pm")
        parsed, text = _ask(structured.get(mirror), llm, prompt, "Portfolio Manager")
        if parsed is not None:
            decision = ta_roles.render_pm(
                parsed.rating.value,
                parsed.executive_summary,
                parsed.investment_thesis,
                parsed.entry_zone,
                parsed.invalidation,
                parsed.take_profit,
                parsed.size_tier,
            )
            rating = parsed.rating.value
        else:
            decision, rating = text, parse_rating(text)
        new = {
            "history": rs["history"],
            "aggressive_history": rs["aggressive_history"],
            "conservative_history": rs["conservative_history"],
            "neutral_history": rs["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": rs["current_aggressive_response"],
            "current_conservative_response": rs["current_conservative_response"],
            "current_neutral_response": rs["current_neutral_response"],
            "count": rs["count"],
        }
        return {
            "risk_debate_state": new,
            "final_trade_decision": decision,
            "final_rating": rating,
        }

    return node


_REPLACEMENTS: dict[str, Factory] = {
    "create_bull_researcher": create_bull_researcher,
    "create_bear_researcher": create_bear_researcher,
    "create_aggressive_debator": create_aggressive_debator,
    "create_conservative_debator": create_conservative_debator,
    "create_neutral_debator": create_neutral_debator,
    "create_news_analyst": create_news_analyst,
    "create_sentiment_analyst": create_sentiment_analyst,
    "create_market_analyst": create_market_analyst,
    "create_research_manager": create_research_manager,
    "create_trader": create_trader,
    "create_portfolio_manager": create_portfolio_manager,
}


def install() -> None:
    """替换 ``tradingagents.graph.setup`` 里的角色工厂。重复调用无害。"""
    from tradingagents.graph import setup

    for name, fn in _REPLACEMENTS.items():
        if not hasattr(setup, name):
            raise RuntimeError(
                f"TradingAgents 里没有 {name}，版本对不上，先核对 pyproject 里钉的提交"
            )
        _ORIGINALS.setdefault(name, getattr(setup, name))
        setattr(setup, name, fn)


# ---------------------------------------------------------------- 每场会不同的内容
_BUILD_LOCK = threading.Lock()
"""建图加一把锁：TradingAgentsGraph 建图会把配置就地合并进进程全局（``set_config``）。角色读配置都在
开会时、按场用 ContextVar（``run_config``），并发建图不会读错。每场会不同的偏空整改设置
（``SETUP_KEYS``）也在配置里，但只经 ``meeting_setting`` 从按场的 ContextVar 读，不读全局那份
（全局是逐键合并的，会留着上一场的值）；谁先开口由 ``PerpGraph`` 读它自己的 ``self.config``。"""

_PERP_GRAPH: type | None = None

BULL_NODE, BEAR_NODE = "Bull Researcher", "Bear Researcher"


def open_debate_with_bear(workflow: Any) -> None:
    """让空头先开口（多头最后发言）：把分析师之后进辩论的那条边从多头改接到空头。

    TradingAgents 的建图把「分析师都交完报告 → Bull Researcher」写死了；之后谁接着说由
    ``should_continue_debate`` 按上一位是谁来定（Bull 之后 Bear，否则 Bull），轮数到了交研究经理，
    所以只改入口这一条边，轮流和收尾照原版。只改这张图自己的 workflow，不动全局。"""
    moved = 0
    for starts, end in list(workflow.waiting_edges):
        if end == BULL_NODE:
            workflow.waiting_edges.discard((starts, end))
            workflow.waiting_edges.add((starts, BEAR_NODE))
            moved += 1
    for start, end in list(workflow.edges):
        if end == BULL_NODE and start not in (BULL_NODE, BEAR_NODE):
            workflow.edges.discard((start, end))
            workflow.edges.add((start, BEAR_NODE))
            moved += 1
    if moved != 1:  # 版本变了对不上：报错，不悄悄按原顺序开
        raise RuntimeError(
            f"TradingAgents 进辩论的边有 {moved} 条（应为 1），版本对不上，"
            "先核对 pyproject 里钉的提交"
        )


def perp_graph_class() -> type:
    """``TradingAgentsGraph`` 的子类：把这场会的说明、持仓说明、教训放进它自己的 state。"""
    global _PERP_GRAPH
    if _PERP_GRAPH is None:
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        class PerpGraph(TradingAgentsGraph):  # type: ignore[misc]
            def __init__(
                self,
                *a: Any,
                meeting_note: str = "",
                position_note: str = "",
                lessons: str = "",
                **kw: Any,
            ) -> None:
                self.meeting_note = meeting_note
                self.position_note = position_note
                self.lessons = lessons
                with _BUILD_LOCK:
                    super().__init__(*a, **kw)
                # 谁先开口（bias_fix.debate_first，已解析成 bull / bear）；默认 bull = 原版
                self.debate_first = str(
                    (self.config.get(PROMPT_KEY) or {}).get("debate_first") or "bull"
                )
                if self.debate_first == "bear":
                    open_debate_with_bear(self.workflow)
                    self.graph = self.workflow.compile()

            def resolve_instrument_context(
                self, ticker: str, asset_type: str = "stock", trade_date: str | None = None
            ) -> str:
                base = super().resolve_instrument_context(ticker, asset_type, trade_date)
                return f"{base} {self.meeting_note}".strip()

            def create_run_state(
                self,
                company_name: str,
                trade_date: str,
                asset_type: str = "stock",
                portfolio: Any = None,
            ) -> dict[str, Any]:
                state: dict[str, Any] = super().create_run_state(
                    company_name, trade_date, asset_type, portfolio
                )
                if self.position_note:
                    state["portfolio_context"] = self.position_note
                return state

            def _memory_step(self, state: Any) -> dict[str, Any]:
                # 原版的记忆是股票口径（按天结算、跟 SPY 比）；这里只交当前这一批教训
                return {"past_context": self.lessons, "memory_note": ""}

        _PERP_GRAPH = PerpGraph
    return _PERP_GRAPH
