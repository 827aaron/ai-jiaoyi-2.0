"""保镖拒绝详情的「重新检查 / 放行」（Aaron 10-25）：保镖的人工放行边界，和主循环的两条面板指令。"""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi import contracts as c
from jiaoyi.core.commands import write_command
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.loop import Runner
from jiaoyi.risk.guard import RiskGuardImpl
from tests import fastloop_helpers as h
from tests.fastloop_helpers import BTC_LOT, FAST, RISK, Lots, account, card, jev, signal
from tests.test_commands import _app, _cmd, _result
from tests.test_loop import FakeCards, FakeExecutor, FakeGate, FakeRisk


def _manual(now: datetime, release: bool, *, fast: c.FastLoopConfig = FAST, **kw: Any):  # type: ignore[no-untyped-def]
    acct = kw.pop("acct", account(now))
    j = kw.pop("jev", jev())
    market = kw.pop("market", signal(now).features)
    g = RiskGuardImpl(RISK, fast, kw.pop("lots", Lots()), **kw)
    return g.review_manual(signal(now), j, card(now), acct, now, market=market, release=release)


def test_release_skips_adl_and_recheck_does_not(now: datetime) -> None:
    high = {"adl_risk": lambda s: "high"}
    assert not _manual(now, False, **high).approved
    v = _manual(now, True, **high)
    assert v.approved and v.intent is not None


def test_release_skips_new_listing_and_gate_threshold(now: datetime) -> None:
    fresh = dataclasses.replace(BTC_LOT, onboard_at=now - timedelta(hours=10))
    assert not _manual(now, False, lots=Lots(fresh)).approved
    assert _manual(now, True, lots=Lots(fresh)).approved
    low_take = jev(take={"take": 0.2, "skip": 0.8})
    assert not _manual(now, False, jev=low_take).approved
    assert _manual(now, True, jev=low_take).approved


def test_release_skips_wide_spread(now: datetime) -> None:
    f = signal(now).features
    assert f.book is not None
    wide = f.model_copy(update={"book": f.book.model_copy(update={"spread_bps": 50.0})})
    assert not _manual(now, False, market=wide).approved
    assert _manual(now, True, market=wide).approved


def test_release_keeps_the_hard_rules(now: datetime) -> None:
    soon = dataclasses.replace(BTC_LOT, delist_at=now + timedelta(days=3))
    assert not _manual(now, True, lots=Lots(soon)).approved  # 要下架
    full = account(now, open_positions=RISK.max_open_positions)
    assert not _manual(now, True, acct=full).approved  # 笔数上限
    held = account(now, open_symbols=["BTCUSDT"], open_positions=1)
    assert not _manual(now, True, acct=held).approved  # 同币已有仓
    assert not _manual(now, True, lots=Lots(None)).approved  # 没有交易所精度
    assert not _manual(now, True, jev=jev(ok=False)).approved  # 拍板人用不了没法算止损和仓位
    blocked = RiskGuardImpl(RISK, FAST, Lots(), blocked=lambda: {"BTCUSDT": "币安公告要下架"})
    v = blocked.review_manual(
        signal(now), jev(), card(now), account(now), now, market=signal(now).features, release=True
    )
    assert not v.approved


def test_manual_modes_are_logged(tmp_path: Path, now: datetime) -> None:
    log = JsonlDecisionLog(tmp_path)
    rec = RoleRecorder(c.Role.RISK_GUARD, log)
    kw: dict[str, Any] = {"recorder": rec, "adl_risk": lambda s: "high"}
    _manual(now, False, **kw)
    _manual(now, True, **kw)
    rows = [r for r in log.scan_day(now.date().isoformat(), "sig-test-1")]
    assert len(rows) == 2
    again, rel = rows
    assert again["outcome"] == "reject" and "重新检查：现在仍拒绝" in again["summary"]
    assert again["output"]["detail"]["by"] == "recheck"
    assert rel["outcome"] == "pass" and "人工放行，跳过：" in rel["summary"]
    assert rel["output"]["detail"]["by"] == "release"
    assert "自动减仓" in rel["output"]["detail"]["waived"]
    json.dumps(rel)


# ---- 主循环的两条面板指令 ----


class ManualRisk(FakeRisk):
    def __init__(self, approve: bool) -> None:
        super().__init__(approve)
        self.manual: list[bool] = []

    def review_manual(self, *args: object, release: bool, **kw: object) -> c.RiskVerdict:
        self.manual.append(release)
        return self.review()


class Feats:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def snapshot(self, symbol: str) -> c.FeatureSnapshot | None:
        return h.features(self.now) if symbol == "BTCUSDT" else None


async def _runner(
    tmp_path: Path, now: datetime, *, approve: bool = True, fail_open: bool = False
) -> tuple[Runner, FakeExecutor, ManualRisk, list[str]]:
    app = _app(tmp_path, now)
    ex, risk = FakeExecutor(fail_open=fail_open), ManualRisk(approve)
    app.m = dataclasses.replace(
        app.m,
        executor=ex,
        gate=FakeGate(),
        risk=risk,
        cards=FakeCards(h.card(now)),
        features=Feats(now),  # type: ignore[arg-type]
    )
    alerts: list[str] = []

    async def alert(level: object, title: str, body: str, kind: str) -> None:
        alerts.append(f"{kind}:{title}")

    app._alert = alert  # type: ignore[method-assign]
    r = Runner(app)
    await r.handle_signal(h.signal(now))  # 信号进缓存
    ex.opened.clear()
    alerts.clear()
    return r, ex, risk, alerts


async def _run(
    r: Runner, tmp_path: Path, now: datetime, kind: str, n: int = 1, **p: str
) -> dict[str, str]:
    p = {"signal_id": "sig-test-1", "symbol": "BTCUSDT", **p}
    write_command(tmp_path, _cmd(now, kind, n, **p))
    await r.command_tick()
    return _result(tmp_path, f"cmd-{kind}-{n:04d}")


async def test_recheck_is_a_dry_run(tmp_path: Path, now: datetime) -> None:
    r, ex, risk, alerts = await _runner(tmp_path, now)
    res = await _run(r, tmp_path, now, "recheck_signal")
    assert res["status"] == "done" and "现在放行" in res["detail"]
    assert ex.opened == [] and risk.manual == [False] and alerts == []


async def test_recheck_still_rejected(tmp_path: Path, now: datetime) -> None:
    r, ex, _risk, _ = await _runner(tmp_path, now, approve=False)
    res = await _run(r, tmp_path, now, "recheck_signal")
    assert res["status"] == "done" and res["detail"].startswith("现在仍拒绝：")
    assert ex.opened == []


async def test_release_opens_and_alerts(tmp_path: Path, now: datetime) -> None:
    r, ex, risk, alerts = await _runner(tmp_path, now)
    res = await _run(r, tmp_path, now, "release_signal")
    assert res["status"] == "done" and "已人工放行开仓" in res["detail"]
    assert len(ex.opened) == 1 and risk.manual == [True]
    assert alerts == ["control:人工放行 BTCUSDT"]
    assert len(r.positions) == 1


async def test_release_refused_by_guard_or_executor(tmp_path: Path, now: datetime) -> None:
    r, ex, _, _ = await _runner(tmp_path, now, approve=False)
    res = await _run(r, tmp_path, now, "release_signal")
    assert res["status"] == "rejected" and "人工放行没成功" in res["detail"] and ex.opened == []
    r2, _ex2, _, alerts2 = await _runner(tmp_path / "b", now, fail_open=True)
    res2 = await _run(r2, tmp_path / "b", now, "release_signal")
    assert res2["status"] == "rejected" and "开仓失败" in res2["detail"]
    assert alerts2 == ["order_failed:BTCUSDT 人工放行开仓失败"]


async def test_manual_preconditions(tmp_path: Path, now: datetime) -> None:
    r, ex, _, _ = await _runner(tmp_path, now)
    res = await _run(r, tmp_path, now, "release_signal", 1, signal_id="sig-unknown")
    assert res["status"] == "rejected" and "信号记录已过期" in res["detail"]
    ex.held = [h_pos for h_pos in [_held(now)]]
    res = await _run(r, tmp_path, now, "release_signal", 2)
    assert res["status"] == "rejected" and "已经有持仓" in res["detail"]
    ex.held = []
    r.m.cards.card = None  # type: ignore[attr-defined]
    res = await _run(r, tmp_path, now, "recheck_signal", 3)
    assert res["status"] == "rejected" and "作战卡" in res["detail"]
    r.m.cards.card = h.card(now)  # type: ignore[attr-defined]
    write_command(tmp_path, _cmd(now, "pause", 4, reason="x"))
    await r.command_tick()
    res = await _run(r, tmp_path, now, "release_signal", 5)
    assert res["status"] == "rejected" and "暂停" in res["detail"]
    assert ex.opened == []


def _held(now: datetime) -> c.PositionRecord:
    from tests.test_loop import _position

    return _position()


def test_signal_cache_is_bounded(now: datetime) -> None:
    from jiaoyi.loop import SIGNAL_CACHE

    assert SIGNAL_CACHE >= 200
