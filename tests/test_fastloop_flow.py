"""快环串起来跑一遍：行情 → 特征 → 哨兵 → 拍板人（mock Jev）→ 保镖 → OrderIntent。

不连网络，验证各模块接得上、每一步都写了决策日志，以及大脑自身的耗时远低于 1–2 秒的预算
（真实延迟主要是 Jev 0.1–0.5 秒和网络，上线前实测）。
"""

from __future__ import annotations

import time
from datetime import timedelta
from pathlib import Path

from jiaoyi import contracts as c
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.entry import JevEntryGate
from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
from jiaoyi.market.features import RollingFeatureEngine
from jiaoyi.risk.guard import RiskGuardImpl
from jiaoyi.sentinel.rules import RuleTrigger
from tests.fastloop_helpers import FAST, GOOD_LONG, RISK, Lots, account, answer_with, card
from tests.test_market import T0, _book, warm_engine


async def test_breakout_to_order_intent(tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path)
    tz = RISK.day_reset_tz
    eng: RollingFeatureEngine = warm_engine(n=240, rng=80.0, period=15)  # 15 分钟 ATR 约 80
    trigger = RuleTrigger(FAST, recorder=RoleRecorder(c.Role.SENTINEL, log, day_reset_tz=tz))
    gate = JevEntryGate(
        JevDecisionModel(MockJevTransport(answer=answer_with(GOOD_LONG)), FAST, timeout_ms=1000),
        RoleRecorder(c.Role.GATE, log, day_reset_tz=tz),
    )
    guard = RiskGuardImpl(RISK, FAST, Lots(), RoleRecorder(c.Role.RISK_GUARD, log, day_reset_tz=tz))

    t = T0 + timedelta(minutes=241)
    cd = card(t)
    # 价格在 67990 附近，然后放量冲过作战卡的 68000 压力位
    eng.update(c.Trade(symbol="BTCUSDT", ts=t, price=67990.0, qty=0.1, taker_side=c.Side.LONG))
    eng.update(_book(t, 67989.9, 2.0, 67990.1, 1.0))
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None
    assert trigger.check(snap, cd, t) == []

    t2 = t + timedelta(seconds=40)
    for s in range(20):
        eng.update(
            c.Trade(
                symbol="BTCUSDT",
                ts=t2 + timedelta(milliseconds=50 * s),
                price=68010.0,
                qty=3.0,
                taker_side=c.Side.LONG,
            )
        )
    eng.update(_book(t2 + timedelta(seconds=1), 68009.9, 3.0, 68010.1, 1.0))
    now = t2 + timedelta(seconds=1)

    started = time.perf_counter()
    snap = eng.snapshot("BTCUSDT")
    assert snap is not None
    signals = trigger.check(snap, cd, now)
    assert [s.kind for s in signals] == [c.SignalKind.VOLUME_BREAKOUT]
    resp = await gate.evaluate(signals[0], cd, now)
    verdict = guard.review(signals[0], resp, cd, account(now), now, market=eng.snapshot("BTCUSDT"))
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert verdict.approved, verdict.reasons
    assert verdict.intent is not None and verdict.intent.side == c.Side.LONG
    assert elapsed_ms < 100, f"大脑本身耗时 {elapsed_ms:.1f}ms"

    roles = [r.role for r in log.read_day(now.date().isoformat())]
    assert roles == [c.Role.SENTINEL, c.Role.GATE, c.Role.RISK_GUARD]
