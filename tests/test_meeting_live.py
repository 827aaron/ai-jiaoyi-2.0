from __future__ import annotations

import contextlib
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


def test_note_call_records_finish_reason_and_truncation(tmp_path: Path) -> None:
    live = MeetingLive(tmp_path, "SOLUSDT", now=Clock())
    live.note_call("stop", 1500)
    live.note_call("length", 4096)
    doc = read(tmp_path)
    assert doc["truncated"] == 1
    assert doc["llm_calls"] == [
        {"finish_reason": "stop", "output_tokens": 1500, "reasoning_tokens": 0, "retries": 0},
        {"finish_reason": "length", "output_tokens": 4096, "reasoning_tokens": 0, "retries": 0},
    ]


def test_note_retry_and_reasoning_tokens(tmp_path: Path) -> None:
    live = MeetingLive(tmp_path, "ETHUSDT")
    live.note_call("length", 8192, 8000)
    live.note_retry("length")
    live.note_call("stop", 300, 0)
    doc = json.loads((tmp_path / "ETHUSDT.json").read_text(encoding="utf-8"))
    assert doc["retries"] == 1 and doc["retry_reasons"][0]["why"] == "length"
    assert doc["llm_calls"][0]["reasoning_tokens"] == 8000


def test_fail_marks_estimated_cost(tmp_path: Path) -> None:
    live = MeetingLive(tmp_path, "LINKUSDT")
    live.fail("超时取消，会议没有结论", 0.31, estimated=True)
    doc = json.loads((tmp_path / "LINKUSDT.json").read_text(encoding="utf-8"))
    assert doc["cost_usd"] == 0.31 and doc["cost_estimated"] is True


def test_note_model_marks_the_last_call(tmp_path: Path) -> None:
    live = MeetingLive(tmp_path, "SOLUSDT", now=Clock())
    live.note_model("ignored")  # 还没有调用记录：不报错
    live.note_call("stop", 100)
    live.note_model("deepseek/deepseek-v4.1-flash-1017")
    assert read(tmp_path)["llm_calls"][0]["model"] == "deepseek/deepseek-v4.1-flash-1017"


def _write(folder: Path, name: str, **doc: Any) -> None:
    folder.mkdir(exist_ok=True)
    (folder / f"{name}.json").write_text(json.dumps({"symbol": name, **doc}), encoding="utf-8")


def test_sweep_marks_previous_process_running_meetings_interrupted(tmp_path: Path) -> None:
    from jiaoyi.slow.meeting_live import PROCESS_ID, sweep_interrupted

    folder = tmp_path / "meeting_live"
    old = {"started_at": T0.isoformat(), "updated_at": T0.isoformat(), "finished_at": None}
    _write(folder, "HYPEUSDT", status="running", process="dead-process", **old)
    _write(folder, "ZECUSDT", status="running", **old)  # 旧格式没有 process 字段
    _write(folder, "DONEUSDT", status="done", process="dead-process", **old)
    _write(folder, "MINEUSDT", status="running", process=PROCESS_ID, **old)
    (folder / "BAD.json").write_text("{broken")
    swept = sweep_interrupted(folder, now=lambda: T0 + timedelta(minutes=6))
    assert swept == ["HYPEUSDT", "ZECUSDT"]
    now = T0 + timedelta(minutes=7)  # 离开始才 7 分钟，还没到 15 分钟的兜底
    m = read_meeting(tmp_path, "HYPEUSDT", now)
    assert m and m.status == "interrupted" and m.error == "主程序重启，会议中断"
    assert m.finished_at == T0 + timedelta(minutes=6)
    m = read_meeting(tmp_path, "ZECUSDT", now)
    assert m and m.status == "interrupted"
    m = read_meeting(tmp_path, "DONEUSDT", now)
    assert m and m.status == "done"
    m = read_meeting(tmp_path, "MINEUSDT", now)
    assert m and m.status == "running"
    assert (folder / "BAD.json").read_text() == "{broken"
    assert sweep_interrupted(folder) == []  # 再扫一遍没有可改的


def test_sweep_missing_directory_is_noop(tmp_path: Path) -> None:
    from jiaoyi.slow.meeting_live import sweep_interrupted

    assert sweep_interrupted(tmp_path / "nope") == []


def test_interrupt_open_stops_later_writes_from_reviving_the_meeting(tmp_path: Path) -> None:
    from jiaoyi.slow.meeting_live import interrupt_open

    live = MeetingLive(tmp_path, "SOLUSDT", now=Clock())
    assert interrupt_open() >= 1
    doc = read(tmp_path)
    assert doc["status"] == "interrupted" and doc["error"] == "主程序重启，会议中断"
    assert doc["finished_at"] is not None
    live.step("analysts", "market", "线程取消不掉，之后还会写")
    live.fail("超时", 0.1)
    live.finish("long", "r", "t", 0.1)
    assert read(tmp_path)["status"] == "interrupted" and read(tmp_path)["steps"] == []
    assert interrupt_open() == 0  # 已收尾的不再算


def _slow_replace(monkeypatch: Any, writers: int) -> Any:
    """让 ``os.replace`` 等到 ``writers`` 个线程都走到这一步（最多等 0.5 秒）才换文件：
    共用一个临时文件名时必有一个线程的临时文件被别人换走；换文件串行时等不齐就照常往下走。"""
    import os
    import threading

    import jiaoyi.slow.meeting_live as ml

    real = os.replace
    gate = threading.Barrier(writers)
    entered = threading.Event()

    def replace(src: Any, dst: Any) -> None:
        entered.set()
        with contextlib.suppress(threading.BrokenBarrierError):
            gate.wait(timeout=0.5)
        real(src, dst)

    monkeypatch.setattr(ml.os, "replace", replace)
    return entered


def test_concurrent_writers_of_one_meeting_never_lose_a_flush(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    import threading

    live = MeetingLive(tmp_path, "SOLUSDT", now=Clock())
    _slow_replace(monkeypatch, 4)
    start = threading.Barrier(4)

    def analyst(i: int) -> None:  # 并行的分析师：各自线程里回调 note_call
        start.wait(timeout=5)
        live.note_call("stop", 100 + i)

    threads = [threading.Thread(target=analyst, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not any(t.is_alive() for t in threads)
    assert "会议实况写盘失败" not in caplog.text
    doc = read(tmp_path)
    assert sorted(c["output_tokens"] for c in doc["llm_calls"]) == [100, 101, 102, 103]
    assert list(tmp_path.iterdir()) == [tmp_path / "SOLUSDT.json"]  # 没有留下临时文件


def test_abort_from_another_thread_while_the_meeting_writes(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    import threading

    live = MeetingLive(tmp_path, "SOLUSDT", now=Clock())
    entered = _slow_replace(monkeypatch, 2)
    worker = threading.Thread(target=live.step, args=("analysts", "market", "行情报告"))
    worker.start()
    assert entered.wait(timeout=5)  # 会议线程正在换文件时，事件循环线程叫停
    live.fail("超时取消，会议没有结论", 0.2)
    worker.join(timeout=10)
    assert "会议实况写盘失败" not in caplog.text
    doc = read(tmp_path)
    assert doc["status"] == "failed" and doc["finished_at"] is not None


def test_a_leftover_meeting_thread_cannot_overwrite_the_newer_meeting(tmp_path: Path) -> None:
    old = MeetingLive(tmp_path, "SOLUSDT", now=Clock())  # 超时取消后后台线程还在跑
    later = Clock()
    later.t = T0 + timedelta(hours=1)
    new = MeetingLive(tmp_path, "SOLUSDT", now=later)
    new.step("analysts", "market", "新一场")
    old.step("analysts", "market", "旧一场晚到的发言")
    old.finish("long", "旧结论", "旧结论", 0.5)
    doc = read(tmp_path)
    assert doc["meeting_id"] == "SOLUSDT-20261023T090010"
    assert doc["status"] == "running"
    assert [s["text"] for s in doc["steps"]] == ["新一场"]
    new.finish("short", "新结论", "新结论", 0.3)
    doc = read(tmp_path)
    assert doc["status"] == "done" and doc["result"] == {"bias": "short", "reason": "新结论"}


def test_serialization_and_write_of_one_flush_are_not_split_by_another_thread(
    tmp_path: Path, monkeypatch: Any, caplog: Any
) -> None:
    import threading

    import jiaoyi.slow.meeting_live as ml

    live = MeetingLive(tmp_path, "SOLUSDT", now=Clock())
    real = json.dumps
    paused = threading.Event()
    other_serialized = threading.Event()

    def dumps(obj: Any, **kw: Any) -> str:
        text = real(obj, **kw)
        if threading.current_thread().name == "first":
            paused.set()  # 序列化完、还没写盘：另一线程这时记一次调用
            other_serialized.wait(timeout=0.5)
        else:
            other_serialized.set()
        return text

    monkeypatch.setattr(ml.json, "dumps", dumps)
    first = threading.Thread(target=live.note_call, args=("stop", 1), name="first")
    first.start()
    assert paused.wait(timeout=5)
    second = threading.Thread(target=live.note_call, args=("stop", 2), name="second")
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)
    assert "会议实况写盘失败" not in caplog.text
    assert [c["output_tokens"] for c in read(tmp_path)["llm_calls"]] == [1, 2]


def test_live_file_is_world_readable_and_closed_meeting_releases_it(tmp_path: Path) -> None:
    import jiaoyi.slow.meeting_live as ml

    live = MeetingLive(tmp_path, "SOLUSDT", now=Clock())
    assert (tmp_path / "SOLUSDT.json").stat().st_mode & 0o777 == 0o644
    assert ml._OWNERS.get((tmp_path / "SOLUSDT.json").absolute()) is live
    live.finish("long", "r", "t", 0.1)
    assert (tmp_path / "SOLUSDT.json").absolute() not in ml._OWNERS
    live.step("analysts", "market", "收尾后再写")
    assert read(tmp_path)["status"] == "done" and read(tmp_path)["steps"][-1]["who"] == "convert"


def test_sweep_removes_leftover_temp_files_but_not_an_open_meetings(tmp_path: Path) -> None:
    from jiaoyi.slow.meeting_live import sweep_interrupted

    folder = tmp_path / "meeting_live"
    folder.mkdir()
    (folder / ".HYPEUSDT.json.k3j2x9.tmp").write_text("{半截")
    (folder / "ZECUSDT.json.tmp").write_text("{旧版本留下的")
    live = MeetingLive(folder, "SOLUSDT", now=Clock())
    (folder / ".SOLUSDT.json.a1b2c3.tmp").write_text("{正在写")
    assert sweep_interrupted(folder) == []
    assert sorted(p.name for p in folder.iterdir()) == [".SOLUSDT.json.a1b2c3.tmp", "SOLUSDT.json"]
    assert read(folder)["status"] == "running"
    live.fail("x", 0.0)


def test_sweep_leaves_an_open_meeting_of_this_process_alone(tmp_path: Path) -> None:
    from jiaoyi.slow.meeting_live import sweep_interrupted

    live = MeetingLive(tmp_path, "SOLUSDT", now=Clock())
    doc = read(tmp_path)
    doc["process"] = "dead-process"  # 哪怕文件看着像上个进程的，归属者还在开就不碰
    (tmp_path / "SOLUSDT.json").write_text(json.dumps(doc), encoding="utf-8")
    assert sweep_interrupted(tmp_path) == []
    assert read(tmp_path)["status"] == "running"
    live.fail("x", 0.0)
    assert read(tmp_path)["status"] == "failed"
