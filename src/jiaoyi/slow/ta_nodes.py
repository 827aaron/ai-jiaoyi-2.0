"""换掉 TradingAgents 里写死股票口径的角色节点，以及每场会不同内容的 ``PerpGraph``。

做法：``tradingagents.graph.setup`` 是按名字引用 ``create_*`` 的，所以替换模块属性就生效，
不改开源代码。新节点的状态读写和原版（0.7.0，e8f5503）完全一样，只换提示词正文；
持仓档的参数（{H} 等）从这场会的运行配置里读（``jiaoyi_prompt``，由引擎的 ``build_config`` 写入，
只跟档位有关，所有会议一样）。每场会不同的东西（总盘会的判断、挑币理由、持仓说明）走
``PerpGraph`` 的 state，不进配置，所以同时开几场也不会串。

替换的角色：多空研究员、风控三人、新闻分析师（加密币；美股永续照旧走原版）。
只在用到时才导入 tradingagents。
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from jiaoyi.slow import ta_prompts

PROMPT_KEY = "jiaoyi_prompt"

Node = Callable[[Any], dict[str, Any]]
Factory = Callable[[Any], Node]


def params_from_config() -> ta_prompts.PromptParams:
    from tradingagents.dataflows.config import get_config

    raw = get_config().get(PROMPT_KEY) or {}
    return ta_prompts.PromptParams.for_hold(
        str(raw.get("hold") or "2h"), int(raw["hold_minutes"]) if raw.get("hold_minutes") else None
    )


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
            from tradingagents.agents.context import (
                get_language_instruction,
                opponent_argument_or_opening,
            )

            debate = state["investment_debate_state"]
            history = debate.get("history", "")
            own_history = debate.get(f"{role}_history", "")
            reply = opponent_argument_or_opening(
                debate.get("current_response", ""), f"{other} analyst"
            )
            p = params_from_config()
            prompt = (
                ta_prompts.role_prompt(role, p, str(state.get("company_of_interest", "asset")))
                + "\n\nResources available:\n"
                + _resources(state, p)
                + f"Conversation history of the debate: {history}\n"
                + f"Last {other} argument: {reply}\n"
                + f"Use this information to deliver your argument and engage directly with the "
                f"{other}'s latest points." + get_language_instruction()
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
                get_language_instruction,
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
                "conversational and without special formatting; name the size, the entry zone "
                "and the stop you would accept."
            )
            response = llm.invoke("\n".join(lines) + get_language_instruction())
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
            get_language_instruction,
        )
        from tradingagents.agents.tools import (
            get_global_news,
            get_news,
            get_prediction_markets,
        )

        tools = (get_news, get_global_news, get_prediction_markets)
        p = params_from_config()
        system_message = (
            ta_prompts.news_prompt(p, binance_symbol_of(str(state["company_of_interest"])))
            + " Make sure to append a short Markdown table at the end of the report."
            + get_language_instruction()
        )
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


_REPLACEMENTS: dict[str, Factory] = {
    "create_bull_researcher": create_bull_researcher,
    "create_bear_researcher": create_bear_researcher,
    "create_aggressive_debator": create_aggressive_debator,
    "create_conservative_debator": create_conservative_debator,
    "create_neutral_debator": create_neutral_debator,
    "create_news_analyst": create_news_analyst,
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
"""建图加一把锁：TradingAgentsGraph 建图会把配置就地合并进进程全局（``set_config``）。今天并发建图
不会读错（角色读配置都在开会时，按场用 ContextVar），锁防的是以后有人把每场会不同的东西放回配置。"""

_PERP_GRAPH: type | None = None


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
