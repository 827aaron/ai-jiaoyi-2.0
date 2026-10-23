from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi.panel.meetings import read_meeting
from jiaoyi.slow.meeting_live import MeetingLive, tap_graph

T0 = datetime(2026, 10, 23, 8, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> datetime:
        self.t += timedelta(seconds=10)
        return self.t


def read(d: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((d / "SOLUSDT.json").read_text(encoding="utf-8"))
    return data


class FakeCompiled:
    """像 LangGraph 编译图：stream 先吐子图任务结果（分析师报告），再吐顶层状态。"""

    def __init__(self) -> None:
        self.stream_calls = 0

    def stream(self, graph_input: Any, subgraphs: bool, stream_mode: list[str], **kw: Any) -> Any:
        assert subgraphs and stream_mode == ["values", "tasks"]
        self.stream_calls += 1
        yield (("Market Analyst:1",), "tasks", {"result": {"market_report": "行情报告"}})
        yield (("Sentiment Analyst:2",), "tasks", {"result": {"sentiment_report": "情绪报告"}})
        yield (("News Analyst:3",), "tasks", {"result": {"news_report": "新闻报告"}})
        yield (("Market Analyst:1",), "tasks", {"input": {}})  # 任务开始事件：没有 result，忽略
        state: dict[str, Any] = {
            "market_report": "行情报告",
            "sentiment_report": "情绪报告",
            "news_report": "新闻报告",
            "investment_debate_state": {"bull_history": "\nBull 1", "bear_history": ""},
        }
        yield ((), "values", dict(state))
        state["investment_debate_state"] = {"bull_history": "\nBull 1", "bear_history": "\nBear 1"}
        yield ((), "values", dict(state))
        state["investment_plan"] = "研究经理：偏多"
        state["trader_investment_plan"] = "交易员：做多"
        state["risk_debate_state"] = {
            "aggressive_history": "\n激进",
            "conservative_history": "\n保守",
            "neutral_history": "\n中性",
        }
        yield ((), "values", dict(state))
        state["final_trade_decision"] = "组合经理：Buy"
        yield ((), "values", dict(state))


class FakeTA:
    def __init__(self) -> None:
        self.graph: Any = FakeCompiled()


def test_tap_records_every_stage_in_order_and_keeps_final_state(tmp_path: Path) -> None:
    live = MeetingLive(tmp_path, "SOLUSDT", now=Clock())
    ta = FakeTA()
    tap_graph(ta, live)
    final = ta.graph.invoke({"x": 1}, config={})
    assert final["final_trade_decision"] == "组合经理：Buy"  # 与 invoke 的返回一致
    assert ta.graph.stream_calls == 1  # 不多调一次图

    live.finish("long", "理由", "理由", 0.12)
    doc = read(tmp_path)
    assert doc["status"] == "done" and doc["result"] == {"bias": "long", "reason": "理由"}
    assert doc["cost_usd"] == 0.12 and doc["finished_at"] and doc["stage"] == "decision"
    who = [(s["stage"], s["who"]) for s in doc["steps"]]
    # 分析师报告先到（子图结果），顶层状态再重复出现时不重复记
    assert who[:3] == [("analysts", "market"), ("analysts", "social"), ("analysts", "news")]
    assert who[3:] == [
        ("debate", "bull"),
        ("debate", "bear"),
        ("debate", "research_manager"),
        ("trader", "trader"),
        ("risk", "aggressive"),
        ("risk", "conservative"),
        ("risk", "neutral"),
        ("risk", "risk_manager"),
        ("decision", "convert"),
    ]
    assert [s["text"] for s in doc["steps"]][3:5] == ["Bull 1", "Bear 1"]  # 只取新增的一段


def test_panel_reader_reads_it_and_fail_is_final(tmp_path: Path) -> None:
    folder = tmp_path / "meeting_live"
    live = MeetingLive(folder, "SOLUSDT", now=Clock())
    live.observe({"market_report": "行情"})
    live.fail("超时取消，会议没有结论", 0.05)
    live.finish("long", "r", "r", 9.9)  # 超时后后台线程才结束：不能改回成功
    live.observe({"news_report": "迟到的报告"})
    m = read_meeting(tmp_path, "SOLUSDT", T0 + timedelta(minutes=1))
    assert m is not None
    assert m.status == "failed" and m.error == "超时取消，会议没有结论"
    assert m.step_count == 1 and m.cost_usd == 0.05 and m.bias is None


def test_write_failure_never_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "meeting_live"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")
    live = MeetingLive(blocker, "SOLUSDT", now=Clock())  # 建目录失败只记日志
    live.observe({"market_report": "行情"})
    live.finish("flat", "r", "r", 0.0)
