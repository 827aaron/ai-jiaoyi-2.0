"""快环只在卡的进场区间内开仓（Aaron 10-24「让快环尊重卡的进场区间」）。"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jiaoyi import contracts as c
from jiaoyi.loop import Runner
from jiaoyi.risk.guard import entry_zone_miss
from tests import fastloop_helpers as h
from tests.test_loop import FakeCards, FakeExecutor, FakeGate, FakeRisk, _app

ZONE = {"min": 67800.0, "max": 67950.0}
ON = h.FAST.model_copy(update={"entry_zone_only": True})  # 默认关（Aaron 10-25），这里打开测
# 测试特征：ATR 80、卡上止损 1–2 倍 → 中值 1.5 × 80 = 120，0.1R = 12


def _sig(now: datetime, price: float, side: c.Side = c.Side.LONG) -> c.CandidateSignal:
    return h.signal(now, side=side, f=h.features(now, price=price))


def test_long_inside_or_just_above_zone_passes(now: datetime) -> None:
    cd = h.card(now, entry_zone=ZONE, bias="both")
    fast = ON
    assert entry_zone_miss(_sig(now, 67900.0), cd, fast, now) is None
    assert entry_zone_miss(_sig(now, 67960.0), cd, fast, now) is None  # 上沿 + 10 < 0.1R
    why = entry_zone_miss(_sig(now, 68000.0), cd, fast, now)
    assert why is not None and why.startswith("没到区间")
    assert entry_zone_miss(_sig(now, 67700.0), cd, fast, now) is not None  # 下沿外不放宽


def test_short_slack_is_below_the_zone(now: datetime) -> None:
    cd = h.card(now, entry_zone=ZONE, bias="both")
    s = c.Side.SHORT
    assert entry_zone_miss(_sig(now, 67790.0, s), cd, ON, now) is None  # 下沿 − 10
    assert entry_zone_miss(_sig(now, 67780.0, s), cd, ON, now) is not None
    assert entry_zone_miss(_sig(now, 67960.0, s), cd, ON, now) is not None  # 上沿外不放宽


def test_no_zone_or_switch_off_does_not_block(now: datetime) -> None:
    far = _sig(now, 70000.0)
    assert entry_zone_miss(far, h.card(now), ON, now) is None
    off = h.FAST  # 默认关
    assert not off.entry_zone_only
    assert entry_zone_miss(far, h.card(now, entry_zone=ZONE), off, now) is None


async def test_loop_rejects_before_asking_jev(tmp_path: Path, now: datetime) -> None:
    app = _on(tmp_path, now)
    ex, gate = FakeExecutor(), FakeGate()
    cd = h.card(now, entry_zone=ZONE)
    app.m = dataclasses.replace(
        app.m, executor=ex, gate=gate, risk=FakeRisk(True), cards=FakeCards(cd)
    )
    r = Runner(app)
    await r.handle_signal(h.signal(now))  # 68000 > 67950 + 12
    assert gate.asked == 0 and ex.opened == []
    recs = [x for x in app.log.read_day(now.date().isoformat()) if x.role == c.Role.RISK_GUARD]
    assert recs and "没到区间" in recs[-1].summary and recs[-1].outcome == c.Outcome.REJECT
    assert recs[-1].output["detail"] == {"by": "entry_zone"}


def _on(tmp_path: Path, now: datetime) -> Any:
    app = _app(tmp_path, now)
    app.app_cfg = app.app_cfg.model_copy(update={"fast_loop": ON})
    return app


def test_default_is_off(tmp_path: Path, now: datetime) -> None:
    """Aaron 10-25：默认关（10-24 数据显示会挡掉好信号）；关着时区间外也照常走。"""
    assert not c.FastLoopConfig().entry_zone_only
    r = Runner(_app(tmp_path, now))
    cd = h.card(now, entry_zone=ZONE, bias="both")
    assert r._chase_zone(SimpleNamespace(symbol="BTCUSDT", side=c.Side.LONG), cd, now) == []  # type: ignore[arg-type]


class _Feats:
    def __init__(self, snap: c.FeatureSnapshot | None) -> None:
        self.snap = snap

    def snapshot(self, symbol: str) -> c.FeatureSnapshot | None:
        return self.snap


def _intent(side: c.Side) -> SimpleNamespace:
    return SimpleNamespace(symbol="BTCUSDT", side=side)


def test_chase_rechecks_zone_at_current_price(tmp_path: Path, now: datetime) -> None:
    """挂单没成交改市价追进：按此刻价格再查区间（评审 #265）。"""
    app = _on(tmp_path, now)
    r = Runner(app)
    cd = h.card(now, entry_zone=ZONE, bias="both")
    r.m = dataclasses.replace(r.m, features=_Feats(h.features(now, price=67900.0)))
    assert r._chase_zone(_intent(c.Side.LONG), cd, now) == []  # type: ignore[arg-type]
    r.m = dataclasses.replace(r.m, features=_Feats(h.features(now, price=68100.0)))
    why = r._chase_zone(_intent(c.Side.LONG), cd, now)  # type: ignore[arg-type]
    assert len(why) == 1 and why[0].startswith("没到区间")
    r.m = dataclasses.replace(r.m, features=_Feats(None))
    assert r._chase_zone(_intent(c.Side.LONG), cd, now)[0].startswith("没到区间")  # type: ignore[arg-type]
    assert r._chase_zone(_intent(c.Side.LONG), h.card(now), now) == []  # type: ignore[arg-type]


def test_chase_skips_on_stale_price_only_when_age_limit_set(tmp_path: Path, now: datetime) -> None:
    """追进前行情断流：价格超过 chase_price_max_age_s 就这次不追；0（默认）= 现状不查。"""
    from datetime import timedelta

    app = _on(tmp_path, now)
    r = Runner(app)
    cd = h.card(now, entry_zone=ZONE, bias="both")
    old = h.features(now - timedelta(seconds=30), price=67900.0)  # 区间里，但 30 秒前的价
    r.m = dataclasses.replace(r.m, features=_Feats(old))
    assert c.FastLoopConfig().chase_price_max_age_s == 0
    from jiaoyi.core.hotconfig import HOT_FAST

    assert {"chase_price_max_age_s", "keeper_price_max_age_s"} <= HOT_FAST  # 面板改了不用重启
    assert r._chase_zone(_intent(c.Side.LONG), cd, now) == []  # type: ignore[arg-type]
    app.app_cfg = app.app_cfg.model_copy(
        update={"fast_loop": ON.model_copy(update={"chase_price_max_age_s": 5.0})}
    )
    why = r._chase_zone(_intent(c.Side.LONG), cd, now)  # type: ignore[arg-type]
    assert why == ["行情已旧 30.0 秒（上限 5 秒），这次不追"]
    fresh = h.features(now - timedelta(seconds=2), price=67900.0)
    r.m = dataclasses.replace(r.m, features=_Feats(fresh))
    assert r._chase_zone(_intent(c.Side.LONG), cd, now) == []  # type: ignore[arg-type]
