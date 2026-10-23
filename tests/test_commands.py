from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from jiaoyi.app import App
from jiaoyi.contracts.position import CloseReason
from jiaoyi.core.commands import (
    COMMANDS_DIR,
    PAUSED_FILE,
    RUNTIME_FILE,
    CommandInbox,
    PanelCommand,
    config_fingerprint,
    read_paused,
    write_command,
)
from jiaoyi.core.config import load_app_config, load_risk_config
from jiaoyi.loop import Runner
from jiaoyi.slow.settings import slow_defaults
from tests import fastloop_helpers as h
from tests.test_loop import APP, RISK, FakeCards, FakeExecutor, FakeGate, FakeRisk, _position


def _app(tmp_path: Path, now: datetime) -> App:
    cfg = load_app_config(APP).model_copy(update={"data_dir": str(tmp_path)})
    return App(load_risk_config(RISK), cfg, clock=lambda: now, offline=True)


def _cmd(now: datetime, kind: str, n: int = 1, **params: str) -> PanelCommand:
    return PanelCommand.model_validate(
        {
            "id": f"cmd-{kind}-{n:04d}",
            "ts": now.isoformat(),
            "expires_at": (now + timedelta(minutes=1)).isoformat(),
            "kind": kind,
            "params": params,
        }
    )


def _result(tmp_path: Path, cmd_id: str) -> dict[str, str]:
    raw = (tmp_path / COMMANDS_DIR / "done" / f"{cmd_id}.json").read_text(encoding="utf-8")
    data: dict[str, str] = json.loads(raw)
    return data


def test_command_ttl_and_id_are_checked(now: datetime) -> None:
    base = {"id": "cmd-00000001", "ts": now.isoformat(), "kind": "pause"}
    with pytest.raises(ValidationError):  # 有效期超过 5 分钟
        PanelCommand.model_validate(
            {**base, "expires_at": (now + timedelta(minutes=6)).isoformat()}
        )
    with pytest.raises(ValidationError):  # 过期时间不晚于 ts
        PanelCommand.model_validate({**base, "expires_at": now.isoformat()})
    with pytest.raises(ValidationError):  # id 不能带路径
        PanelCommand.model_validate(
            {**base, "id": "../../x", "expires_at": (now + timedelta(minutes=1)).isoformat()}
        )


async def test_pause_blocks_new_entries_and_persists(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex, gate = FakeExecutor(), FakeGate()
    app.m = dataclasses.replace(
        app.m, executor=ex, gate=gate, risk=FakeRisk(True), cards=FakeCards(h.card(now))
    )
    r = Runner(app)
    write_command(tmp_path, _cmd(now, "pause", reason="看不懂行情"))
    await r.command_tick()
    assert _result(tmp_path, "cmd-pause-0001")["status"] == "done"
    assert read_paused(tmp_path) is not None
    await r.handle_signal(h.signal(now))
    assert gate.asked == 0 and ex.opened == []  # 暂停时连 Jev 都不问
    assert _app(tmp_path, now).paused is not None  # 重启后仍暂停

    write_command(tmp_path, _cmd(now, "resume"))
    await r.command_tick()
    assert read_paused(tmp_path) is None
    await r.handle_signal(h.signal(now))
    assert ex.opened == ["int-0001"]


async def test_replay_expired_and_malformed_are_not_executed(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    r = Runner(app)
    cmd = _cmd(now, "pause")
    write_command(tmp_path, cmd)
    await r.command_tick()
    app.set_paused(False)
    write_command(tmp_path, cmd)  # 同一个 id 再来一次：不执行
    await r.command_tick()
    assert app.paused is None
    assert _result(tmp_path, cmd.id)["status"] == "done"  # 原结果不被覆盖

    old = PanelCommand.model_validate(
        {
            "id": "cmd-old-0001",
            "ts": (now - timedelta(minutes=3)).isoformat(),
            "expires_at": (now - timedelta(minutes=1)).isoformat(),
            "kind": "pause",
        }
    )
    write_command(tmp_path, old)
    bad = tmp_path / COMMANDS_DIR / "garbage-0001.json"
    bad.write_text("{not json", encoding="utf-8")
    await r.command_tick()
    assert app.paused is None
    assert _result(tmp_path, "cmd-old-0001")["status"] == "expired"
    assert _result(tmp_path, "garbage-0001")["status"] == "rejected"
    assert not list((tmp_path / COMMANDS_DIR).glob("*.json"))  # 收件箱清空


async def test_close_commands_go_through_executor(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex = FakeExecutor()
    ex.held = [_position(), _position(position_id="pos-0002", symbol="ETHUSDT")]
    app.m = dataclasses.replace(app.m, executor=ex)
    r = Runner(app)
    write_command(tmp_path, _cmd(now, "close_position", position_id="pos-0002"))
    write_command(tmp_path, _cmd(now, "close_position", 2, position_id="pos-nope"))
    await r.command_tick()
    await asyncio.gather(*r._tasks)
    assert ex.closed == [("pos-0002", CloseReason.MANUAL)]
    assert _result(tmp_path, "cmd-close_position-0002")["status"] == "rejected"

    write_command(tmp_path, _cmd(now, "close_all"))
    await r.command_tick()
    await asyncio.gather(*r._tasks)
    assert sorted(p for p, _ in ex.closed) == ["pos-0001", "pos-0002", "pos-0002"]
    assert all(reason is CloseReason.MANUAL for _, reason in ex.closed)


async def test_refresh_on_stub_cards_is_rejected(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    r = Runner(app)
    write_command(tmp_path, _cmd(now, "refresh_cards"))
    write_command(tmp_path, _cmd(now, "unblock_symbol", symbol="ETHUSDT"))
    await r.command_tick()
    assert _result(tmp_path, "cmd-refresh_cards-0001")["status"] == "rejected"
    assert _result(tmp_path, "cmd-unblock_symbol-0001")["status"] == "rejected"


async def test_runtime_json_has_config_fingerprint(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    await app.start()
    runtime = json.loads((tmp_path / RUNTIME_FILE).read_text(encoding="utf-8"))
    # 面板用同一个函数、同样顺序算 config/*.toml 的指纹，不一致就提示「配置已改、未生效」
    assert runtime["config_fingerprint"] == config_fingerprint(
        app.risk_cfg, app.app_cfg, slow_defaults(app.app_cfg.profile)
    )
    assert runtime["mode"] == app.app_cfg.mode.value
    changed = app.risk_cfg.model_copy(update={"max_leverage": app.risk_cfg.max_leverage - 1})
    slow = slow_defaults(app.app_cfg.profile)
    assert config_fingerprint(changed, app.app_cfg, slow) != runtime["config_fingerprint"]
    await app.stop()


async def test_pause_during_jev_blocks_in_flight_signal(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex = FakeExecutor()

    class PausingGate(FakeGate):
        async def evaluate(self, signal, card, now):  # type: ignore[no-untyped-def]
            app.set_paused(True, reason="问 Jev 期间暂停")
            return await super().evaluate(signal, card, now)

    app.m = dataclasses.replace(
        app.m, executor=ex, gate=PausingGate(), risk=FakeRisk(True), cards=FakeCards(h.card(now))
    )
    await Runner(app).handle_signal(h.signal(now))
    assert ex.opened == []


async def test_close_all_pauses_first(tmp_path: Path, now: datetime) -> None:
    app = _app(tmp_path, now)
    ex = FakeExecutor()
    ex.held = [_position()]
    app.m = dataclasses.replace(app.m, executor=ex)
    r = Runner(app)
    write_command(tmp_path, _cmd(now, "close_all"))
    await r.command_tick()
    await asyncio.gather(*r._tasks)
    assert ex.closed == [("pos-0001", CloseReason.MANUAL)]
    assert read_paused(tmp_path) is not None


def test_corrupt_paused_file_counts_as_paused(tmp_path: Path, now: datetime) -> None:
    assert read_paused(tmp_path) is None  # 没有文件 = 没暂停
    (tmp_path / PAUSED_FILE).write_text("{broken", encoding="utf-8")
    paused = read_paused(tmp_path)
    assert paused is not None and paused["corrupt"] is True
    (tmp_path / PAUSED_FILE).write_text('"yes"', encoding="utf-8")
    assert read_paused(tmp_path) is not None
    (tmp_path / PAUSED_FILE).write_text('{"paused": false}', encoding="utf-8")
    assert read_paused(tmp_path) is None
    assert _app(tmp_path, now).paused is None


def test_done_results_are_pruned_after_7_days(tmp_path: Path, now: datetime) -> None:
    done = tmp_path / COMMANDS_DIR / "done"
    done.mkdir(parents=True)
    old, new = done / "old-00001.json", done / "new-00001.json"
    old.write_text("{}", encoding="utf-8")
    new.write_text("{}", encoding="utf-8")
    eight_days_ago = (now - timedelta(days=8)).timestamp()
    os.utime(old, (eight_days_ago, eight_days_ago))
    os.utime(new, (now.timestamp(), now.timestamp()))
    assert CommandInbox(tmp_path).prune(now) == 1
    assert not old.exists() and new.exists()


# ---------------------------------------------------------------- 面板“重启生效”


def _restart_runner(
    tmp_path: Path, now: datetime, check: object = None
) -> tuple[Runner, FakeExecutor]:
    app = _app(tmp_path, now)
    ex = FakeExecutor()
    app.m = dataclasses.replace(
        app.m, executor=ex, gate=FakeGate(), risk=FakeRisk(True), cards=FakeCards(h.card(now))
    )
    return Runner(app, restart_check=check), ex  # type: ignore[arg-type]


async def test_restart_rejected_without_supervisor_or_bad_config(
    tmp_path: Path, now: datetime
) -> None:
    r, ex = _restart_runner(tmp_path, now)  # 没给检查 = 不支持
    write_command(tmp_path, _cmd(now, "restart"))
    await r.command_tick()
    assert _result(tmp_path, "cmd-restart-0001")["status"] == "rejected"
    assert not r._restart.is_set()

    r, ex = _restart_runner(tmp_path, now, lambda: "不是 launchd 托管")
    write_command(tmp_path, _cmd(now, "restart", 2))
    await r.command_tick()
    res = _result(tmp_path, "cmd-restart-0002")
    assert res["status"] == "rejected" and "launchd" in res["detail"]
    assert not r._restart.is_set()
    await r.handle_signal(h.signal(now))
    assert ex.opened == ["int-0001"]  # 被拒后照常开仓


async def test_restart_waits_for_in_flight_open_then_exits(tmp_path: Path, now: datetime) -> None:
    r, ex = _restart_runner(tmp_path, now, lambda: None)
    await r._open_lock.acquire()  # 模拟一笔开仓正在下单
    write_command(tmp_path, _cmd(now, "restart"))
    tick = asyncio.create_task(r.command_tick())
    await asyncio.sleep(0.05)
    assert not tick.done() and r._restarting
    await r.handle_signal(h.signal(now))  # 重启中：新信号不开仓
    assert ex.opened == []
    r._open_lock.release()
    await tick
    assert r._restart.is_set()
    # 回执在进程真要退出前一刻才写，面板看到“完成”时进程确实在退
    assert not (tmp_path / "panel_commands" / "done" / "cmd-restart-0001.json").exists()
    r._exit_for_restart()
    assert _result(tmp_path, "cmd-restart-0001")["status"] == "done"
    assert read_paused(tmp_path) is None  # 不落盘暂停：重启后照旧


async def test_restart_rolls_back_when_close_does_not_finish(
    tmp_path: Path, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jiaoyi.loop.RESTART_WAIT_CLOSE_S", 0.3)
    r, ex = _restart_runner(tmp_path, now, lambda: None)
    r._closing.add("pos-0001")
    write_command(tmp_path, _cmd(now, "restart"))
    await r.command_tick()
    res = _result(tmp_path, "cmd-restart-0001")
    assert res["status"] == "rejected" and "平仓" in res["detail"]
    assert not r._restarting and not r._restart.is_set()
    await r.handle_signal(h.signal(now))
    assert ex.opened == ["int-0001"]


async def test_run_returns_cleanly_on_restart(
    tmp_path: Path, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jiaoyi.loop.COMMAND_POLL_EVERY_S", 0.05)
    r, _ = _restart_runner(tmp_path, now, lambda: None)

    async def idle() -> None:
        await asyncio.sleep(3600)

    async def noop() -> None:
        return None

    r._market_loop = idle  # type: ignore[method-assign]
    r.news_tick = noop  # type: ignore[method-assign]
    write_command(tmp_path, _cmd(now, "restart"))
    await asyncio.wait_for(r.run(), timeout=5)
    assert r.restart_requested
    assert _result(tmp_path, "cmd-restart-0001")["status"] == "done"
    assert r._loops == [] and r._tasks == set()


async def test_after_restart_is_decided_no_new_close_or_reconcile_starts(
    tmp_path: Path, now: datetime
) -> None:
    """审计 X1：重启已定后，管家 / 巡逻兵 / 对账不再发起会被退出打断的动作。"""
    r, ex = _restart_runner(tmp_path, now, lambda: None)
    calls: list[datetime] = []

    class Reconciler:
        async def reconcile(self, at: datetime) -> None:
            calls.append(at)

    r.m = dataclasses.replace(r.m, reconciler=Reconciler())  # type: ignore[arg-type]
    await r.reconcile_tick()
    assert len(calls) == 1
    write_command(tmp_path, _cmd(now, "restart"))
    await r.command_tick()
    assert r._restart.is_set()
    await r.close(_position(), CloseReason.PATROL)  # 巡逻兵 / 管家此刻要平
    assert ex.closed == []  # 不发起：交给重启后的进程，期间靠保命止损
    await r.reconcile_tick()
    assert len(calls) == 1


async def test_restart_exit_waits_for_close_already_running(
    tmp_path: Path, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已经在跑的平仓 / 对账：退出前等它做完，不取消到一半。"""
    r, ex = _restart_runner(tmp_path, now, lambda: None)
    gate = asyncio.Event()
    finished: list[str] = []

    async def slow_close(position_id: str, reason: CloseReason) -> None:
        await gate.wait()
        finished.append(position_id)

    ex.close = slow_close  # type: ignore[method-assign]
    closing = asyncio.create_task(r.close(_position(), CloseReason.KEEPER))
    await asyncio.sleep(0.01)
    r._reconciling = True
    drain = asyncio.create_task(r._drain(5))
    await asyncio.sleep(0.2)
    assert not drain.done()  # 平仓、对账都还在跑
    gate.set()
    await closing
    await asyncio.sleep(0.2)
    assert not drain.done()  # 平仓完了，对账还没完
    r._reconciling = False
    await asyncio.wait_for(drain, 1)
    assert finished == [_position().position_id]

    monkeypatch.setattr("jiaoyi.loop.RESTART_DRAIN_S", 0.2)
    r._closing.add("stuck")  # 卡住的平仓：限时到了照样退
    await asyncio.wait_for(r._drain(0.2), 1)


def test_restart_blocker_needs_launchd_and_valid_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import argparse
    import shutil

    from jiaoyi.cli import _restart_blocker

    shutil.copy(RISK, tmp_path / "risk.toml")
    shutil.copy(APP, tmp_path / "app.toml")
    args = argparse.Namespace(
        risk=str(tmp_path / "risk.toml"), app=str(tmp_path / "app.toml"), slow=str(tmp_path / "x")
    )
    monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
    assert "launchd" in (_restart_blocker(args) or "")
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.jiaoyi.run")
    assert _restart_blocker(args) is None
    (tmp_path / "risk.toml").write_text("max_leverage = 999\n", encoding="utf-8")
    assert "不合格" in (_restart_blocker(args) or "")


def test_restart_blocker_live_needs_rearm_after_risk_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """面板改了硬上限（risk 指纹变了）：live 新进程会被实盘门槛拒绝，所以重启前就拒绝。"""
    import argparse
    import shutil

    from jiaoyi.cli import _restart_blocker, _startup_problem
    from jiaoyi.execution.live_gate import CONFIRM_WORD, LiveGate

    data = tmp_path / "data"
    data.mkdir()
    shutil.copy(RISK, tmp_path / "risk.toml")
    text = APP.read_text(encoding="utf-8")
    text = text.replace('mode = "paper"', 'mode = "live"', 1).replace(
        'data_dir = "data"', f'data_dir = "{data}"'
    )
    (tmp_path / "app.toml").write_text(text, encoding="utf-8")
    args = argparse.Namespace(
        risk=str(tmp_path / "risk.toml"), app=str(tmp_path / "app.toml"), slow=str(tmp_path / "x")
    )
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.jiaoyi.run")
    monkeypatch.setattr("jiaoyi.cli.missing_slow_stack", lambda *_: None)

    assert "go-live" in (_restart_blocker(args) or "")  # 还没解锁
    gate = LiveGate(data / "live_gate.json")
    gate.set_password("correct horse")
    gate.unlock(CONFIRM_WORD, "correct horse", config_fingerprint(load_risk_config(RISK)))
    assert _restart_blocker(args) is None and _startup_problem(args) is None

    risk_text = (tmp_path / "risk.toml").read_text(encoding="utf-8")
    (tmp_path / "risk.toml").write_text(
        risk_text.replace("max_leverage = 5", "max_leverage = 4"), encoding="utf-8"
    )
    assert load_risk_config(tmp_path / "risk.toml").max_leverage == 4
    why = _restart_blocker(args) or ""
    assert "起不来" in why and "go-live" in why


def test_restart_blocker_checks_slow_stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import argparse
    import shutil

    from jiaoyi.cli import _restart_blocker

    shutil.copy(RISK, tmp_path / "risk.toml")
    shutil.copy(APP, tmp_path / "app.toml")
    args = argparse.Namespace(
        risk=str(tmp_path / "risk.toml"), app=str(tmp_path / "app.toml"), slow=str(tmp_path / "x")
    )
    monkeypatch.setenv("XPC_SERVICE_NAME", "com.jiaoyi.run")
    monkeypatch.setattr("jiaoyi.cli.missing_slow_stack", lambda *_: "没装 tradingagents")
    assert "tradingagents" in (_restart_blocker(args) or "")


def _idle_runner(r: Runner) -> None:
    async def idle() -> None:
        await asyncio.sleep(3600)

    async def noop() -> None:
        return None

    r._market_loop = idle  # type: ignore[method-assign]
    r.news_tick = noop  # type: ignore[method-assign]


async def test_restart_exits_even_when_a_task_ignores_cancel(
    tmp_path: Path, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """线上 bug：回执写了“完成”，进程却没退（开会的工作线程 / 不肯停的任务把退出卡住）。"""
    monkeypatch.setattr("jiaoyi.loop.COMMAND_POLL_EVERY_S", 0.05)
    monkeypatch.setattr("jiaoyi.loop.TASK_CANCEL_WAIT_S", 0.1)
    exits: list[bool] = []
    r, _ = _restart_runner(tmp_path, now, lambda: None)
    receipt_at_exit: list[bool] = []

    def fake_exit() -> None:
        receipt_at_exit.append(
            (tmp_path / "panel_commands" / "done" / "cmd-restart-0001.json").exists()
        )
        exits.append(True)

    r._restart_exit = fake_exit
    _idle_runner(r)
    release = asyncio.Event()

    async def stubborn() -> None:  # 吞掉取消，直到放行
        while not release.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(10)

    r._spawn(stubborn())
    write_command(tmp_path, _cmd(now, "restart"))
    await asyncio.wait_for(r.run(), timeout=5)
    assert exits == [True] and receipt_at_exit == [True]  # 先写回执再退
    release.set()
    await asyncio.sleep(0)


async def test_restart_hard_exit_when_shutdown_hangs(
    tmp_path: Path, now: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jiaoyi.loop.COMMAND_POLL_EVERY_S", 0.05)
    monkeypatch.setattr("jiaoyi.loop.RESTART_HARD_EXIT_S", 0.2)
    r, _ = _restart_runner(tmp_path, now, lambda: None)
    exited = threading.Event()
    r._restart_exit = exited.set
    _idle_runner(r)
    release = asyncio.Event()

    async def hung_shutdown() -> None:  # 收尾卡住，连 wait_for 的取消都吞掉
        while not release.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0.05)

    r.shutdown = hung_shutdown  # type: ignore[method-assign]
    write_command(tmp_path, _cmd(now, "restart"))
    run = asyncio.create_task(r.run())
    for _ in range(100):
        if exited.is_set():
            break
        await asyncio.sleep(0.05)
    assert exited.is_set()  # 超时线程兜底退出
    assert _result(tmp_path, "cmd-restart-0001")["status"] == "done"
    release.set()
    await asyncio.wait_for(run, timeout=5)


async def test_restart_command_older_than_process_is_not_rerun(
    tmp_path: Path, now: datetime
) -> None:
    """上个进程退出前没写成回执：重启后的新进程只补回执，不再重启一次。"""
    r, _ = _restart_runner(tmp_path, now, lambda: None)
    r._started_at = now + timedelta(seconds=10)
    write_command(tmp_path, _cmd(now, "restart"))
    await r.command_tick()
    res = _result(tmp_path, "cmd-restart-0001")
    assert res["status"] == "done" and res["detail"] == "已重启"
    assert not r._restarting and not r._restart.is_set()
