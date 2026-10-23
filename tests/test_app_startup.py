"""装配即崩的回归测试：真实执行层模块装进 App，start() 和心跳要跑通。

heartbeat_all 用 runtime_checkable Protocol 判断“有没有 heartbeat”，它只查方法名、不查签名；
2026-10-23 Mac mini 上 paper + 真实执行层一启动就 TypeError（executor.heartbeat 要 now）。
"""

from __future__ import annotations

import dataclasses
import inspect
import json
from datetime import datetime
from pathlib import Path

import pytest

from jiaoyi.alerting.stub import StubAlerter
from jiaoyi.contracts.common import Role
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution.hb_executor import HbExecutorAdapter
from jiaoyi.execution.lifeboat import BinanceLifeboat
from jiaoyi.execution.reconciler import ThreeWayReconciler
from jiaoyi.execution.registry import PositionRegistry
from jiaoyi.predictor.kronos import KronosForecaster
from tests.fakes import FakeExchange, FakeHbApi
from tests.test_loop import _app


@pytest.mark.parametrize("cls", [HbExecutorAdapter, BinanceLifeboat, KronosForecaster])
def test_module_heartbeat_takes_now(cls: type, now: datetime) -> None:
    # 和 App.heartbeat_all 的调用方式一致：heartbeat(now)，其余参数有默认值
    inspect.signature(cls.heartbeat).bind(object(), now)


async def test_paper_with_real_execution_starts_and_heartbeats(
    tmp_path: Path, now: datetime
) -> None:
    app = _app(tmp_path, now)
    log = JsonlDecisionLog(tmp_path)
    api, ex, alerts = FakeHbApi(), FakeExchange(), StubAlerter()
    clock = app.clock
    lifeboat = BinanceLifeboat(ex, clock=clock, recorder=RoleRecorder(Role.LIFEBOAT, log))
    registry = PositionRegistry(tmp_path / "positions.json")
    executor = HbExecutorAdapter(
        api,
        lifeboat,
        registry,
        alerts,
        account_name="acct",
        connector_name="binance_perpetual_testnet",
        fill_timeout_s=5,
        poll_s=1,
        clock=clock,
        recorder=RoleRecorder(Role.EXECUTOR, log),
        exchange=ex,
    )
    reconciler = ThreeWayReconciler(
        executor, api, ex, lifeboat, registry, alerts, account_name="acct"
    )
    app.m = dataclasses.replace(app.m, executor=executor, lifeboat=lifeboat, reconciler=reconciler)
    await app.start()
    app.heartbeat_all()
    status = json.loads((tmp_path / "module_status.json").read_text(encoding="utf-8"))
    assert {"executor", "lifeboat"} <= set(status)
    await app.stop()
