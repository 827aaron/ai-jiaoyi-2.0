"""管家方案 E（Aaron 10-24）：持仓快照带最高浮盈、回吐、进场理由、移动止损、预测员。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
from jiaoyi.gate.questions import (
    HOLD_PROMPTS,
    MAX_SNAPSHOT_CHARS,
    hold_snapshot,
)
from jiaoyi.keeper.keeper import JevKeeper
from tests.fastloop_helpers import FAST, card, features
from tests.test_keeper_patrol import close_p, position


def _fc(now: datetime, p_up: float, *, stale: bool = False) -> c.Forecast:
    return c.Forecast(
        symbol="BTCUSDT",
        ts=now - timedelta(minutes=5),
        horizon_min=60,
        price_low=67000.0,
        price_high=69000.0,
        p_up=p_up,
        model="Kronos-small",
        stale_after=now + timedelta(minutes=-1 if stale else 5),
    )


def _snap(s: str) -> dict[str, Any]:
    d: dict[str, Any] = json.loads(s)
    return d


def test_snapshot_new_fields_with_stop_trailing_entry_and_kronos(now: datetime) -> None:
    pos = position(now, trailing_activation_pct=0.2, trailing_stop_pct=0.1)  # 启动 20bp、回撤 10bp
    f = features(now, price=68204.0)  # 浮盈 30bp
    entry = {"sig": "volume_surge", "why": "量能突增", "p_real": 0.4, "p_take": 0.3}
    d = _snap(
        hold_snapshot(
            pos,
            f,
            card(now),
            now,
            history=[0.0, 10.0, 35.0, 30.0],
            entry=entry,
            forecast=_fc(now, 0.75),
            fee_bps=5.0,
        )
    )
    p = d["pos"]
    assert p["entry_px"] == 68000.0 and p["pnl_bps"] == 30.0
    assert p["peak_bps"] == 35.0 and p["giveback_bps"] == 5.0
    stop = abs(68000 / 67880 - 1) * 1e4  # ≈ 17.7bp
    assert p["pnl_r"] == round(30 / stop, 2) and p["peak_r"] == round(35 / stop, 2)
    assert p["be_bps"] == 20.0 and p["path_bps"] == [0, 10, 35, 30]
    assert d["entry"] == entry
    assert d["trail"] == {"on": True, "active": True, "stop_bps": 5.0}  # 30 −（35 − 10）
    assert d["kronos"] == {"agree": 1}  # 平均路径收在现价之上，做多同向
    assert "imb" not in d["book"] and "micro_bps" not in d["book"] and "spread_bps" in d["book"]


def test_snapshot_without_stop_entry_forecast_or_trailing(now: datetime) -> None:
    pos = position(now, stop_loss_price=None, side="short")
    d = _snap(
        hold_snapshot(pos, features(now), card(now), now, forecast=_fc(now, 0.75, stale=True))
    )
    p = d["pos"]
    assert "pnl_r" not in p and "peak_r" not in p and "path_bps" not in p
    assert p["peak_bps"] == p["pnl_bps"] and p["giveback_bps"] == 0.0
    assert "entry" not in d and "kronos" not in d
    assert d["trail"] == {"on": False, "active": False}
    pos2 = position(now, trailing_activation_pct=0.5, trailing_stop_pct=0.2)
    d2 = _snap(hold_snapshot(pos2, features(now, price=68068.0), card(now), now))
    assert d2["trail"] == {"on": True, "active": False}  # 浮盈 10bp，没到 50bp


def test_path_samples_five_points_oldest_first(now: datetime) -> None:
    hist = [float(i) for i in range(9)]
    d = _snap(hold_snapshot(position(now), features(now), card(now), now, history=hist))
    assert d["pos"]["path_bps"] == [0, 2, 4, 6, 8]


def test_snapshot_stays_valid_json_under_limit(now: datetime) -> None:
    entry = {"sig": "volume_surge", "why": "很长" * 50, "p_real": 0.4, "p_take": 0.3}
    against = c.AgainstSignalEvent.model_validate(
        {
            "ts": now.isoformat(),
            "symbol": "BTCUSDT",
            "kind": "volume_surge",
            "side": "short",
            "desc": "反向",
            "strong": True,
            "vol_ratio": 9.0,
            "buy_ratio": 0.2,
        }
    )
    s = hold_snapshot(
        position(now, trailing_activation_pct=0.2, trailing_stop_pct=0.1),
        features(now, oi15=1.0, oi60=2.0, oi4h=3.0, top_ls=1.1, crowd_ls=0.9, div4h=1.0),
        card(now), now, against=against, history=[float(i) for i in range(400)],
        entry=entry, forecast=_fc(now, 0.5),
    )  # fmt: skip
    assert len(s) <= MAX_SNAPSHOT_CHARS and json.loads(s)["pos"]["peak_bps"] == 399.0


def test_prompts_use_spec_wording() -> None:
    action = HOLD_PROMPTS[c.HoldQuestion.ACTION]
    assert "If the position is in profit after fees (`pos.be_bps` > 0)" in action
    assert "is not by itself a reason to close: the stop already limits that loss." in action
    thesis = HOLD_PROMPTS[c.HoldQuestion.THESIS_INTACT]
    assert thesis.startswith("Is the reason we entered this position (`entry.sig`, `entry.why`)")
    assert "`against` (only on urgent checks)" in thesis


NO_GIVEBACK = FAST.model_copy(update={"giveback_on": False})  # 这些测试看模型路径


def _keeper(
    t: MockJevTransport, now: datetime, prices: list[float], cfg: c.FastLoopConfig = NO_GIVEBACK,
    **kw: Any,
) -> JevKeeper:  # fmt: skip
    it = iter(prices)
    return JevKeeper(
        JevDecisionModel(t, cfg, timeout_ms=200),
        cfg,
        lambda s: features(now, price=next(it)),
        lambda n: card(now),
        **kw,
    )


async def test_keeper_tracks_peak_and_forgets(now: datetime) -> None:
    t = MockJevTransport(answer=close_p(0.1))
    k = _keeper(t, now, [68068.0, 68204.0, 68034.0])  # 浮盈 10 / 30 / 5bp
    pos = position(now)
    for _ in range(3):
        assert (await k.review(pos, now)).action == "hold"
    last = _snap(t.calls[-1])["pos"]
    assert last["peak_bps"] == 30.0 and last["giveback_bps"] == 25.0
    assert last["path_bps"] == [10, 30]
    assert k.history("pos-1") == [10.0, 30.0, 5.0]
    k.forget("pos-1")
    assert k.history("pos-1") == []


async def test_keeper_recalls_history_and_entry_after_restart(
    tmp_path: Path, now: datetime
) -> None:
    log = JsonlDecisionLog(tmp_path)
    pos = position(now)
    gate = RoleRecorder(c.Role.GATE, log)
    gate.record(
        now - timedelta(minutes=5), c.Outcome.PASS, "拍板",
        symbol="BTCUSDT", signal_id=pos.signal_id,
        input={"jev": {"snapshot": json.dumps({"sig": "volume_surge", "why": "量能突增 5 倍"})}},
        output={"jev": {"answers": {"genuine": {"real": 0.41, "fake": 0.59},
                                    "take": {"take": 0.33, "skip": 0.67}}}},
    )  # fmt: skip
    t1 = MockJevTransport(answer=close_p(0.1))
    k1 = _keeper(
        t1, now, [68204.0, 68102.0], recorder=RoleRecorder(c.Role.KEEPER, log),
        scan_day=log.scan_day,
    )  # fmt: skip
    await k1.review(pos, now)
    await k1.review(pos, now)
    assert _snap(t1.calls[0])["entry"]["sig"] == "volume_surge"

    t2 = MockJevTransport(answer=close_p(0.1))  # 重启：新管家从日志找回
    k2 = _keeper(t2, now, [68034.0], scan_day=log.scan_day)
    await k2.review(pos, now)
    d = _snap(t2.calls[0])
    assert d["pos"]["peak_bps"] == 30.0 and d["pos"]["path_bps"] == [30, 15]
    assert d["entry"] == {"sig": "volume_surge", "why": "量能突增 5 倍", "p_real": 0.41,
                          "p_take": 0.33}  # fmt: skip


async def test_keeper_passes_forecast_and_fee(now: datetime) -> None:
    t = MockJevTransport(answer=close_p(0.1))
    k = _keeper(t, now, [68068.0], forecast=lambda s, n: _fc(now, 0.0), fee_bps=2.0)
    await k.review(position(now), now)
    d = _snap(t.calls[0])
    assert d["kronos"] == {"agree": 0} and d["pos"]["be_bps"] == 6.0


def test_keeper_sees_forecast_rank(now: datetime) -> None:
    """官方用法：管家看到平均路径是否同向 + 全池名次（做空碰上看坏 = 同向）。"""
    fc = _fc(now, 0.0).model_copy(update={"score": -0.01, "rank_pct": 0.0, "rank_n": 12,
                                          "rank_view": "bottom"})  # fmt: skip
    pos = position(now, side=c.Side.SHORT)
    d = _snap(hold_snapshot(pos, features(now), card(now), now, forecast=fc))
    assert d["kronos"] == {"agree": 1, "rank": "bottom"}


def _ranked(now: datetime, p_up: float, view: str) -> c.Forecast:
    score = {"top": 0.01, "mid": 0.0, "bottom": -0.01}[view]
    pct = {"top": 1.0, "mid": 0.5, "bottom": 0.0}[view]
    return _fc(now, p_up).model_copy(
        update={"score": score, "rank_pct": pct, "rank_n": 12, "rank_view": view}
    )


def test_keeper_snapshot_rank_mode_carries_only_the_rank(now: datetime) -> None:
    pos = position(now)
    for p_up in (0.0, 1.0):  # p_up is ignored: only the pool rank goes in
        d = _snap(
            hold_snapshot(pos, features(now), card(now), now,
                          forecast=_ranked(now, p_up, "top"), kronos="rank")
        )  # fmt: skip
        assert d["kronos"] == {"rank": "top"}
    no_rank = _snap(
        hold_snapshot(pos, features(now), card(now), now, forecast=_fc(now, 1.0), kronos="rank")
    )
    assert "kronos" not in no_rank


def test_keeper_snapshot_off_mode_omits_kronos(now: datetime) -> None:
    d = _snap(
        hold_snapshot(position(now), features(now), card(now), now,
                      forecast=_ranked(now, 1.0, "top"), kronos="off")
    )  # fmt: skip
    assert "kronos" not in d


def test_keeper_snapshot_direction_mode_is_skewed_by_binary_p_up(now: datetime) -> None:
    """Default: with the observed p_up = 0 skew, a long reads "against" and a short "with"."""
    fc = _ranked(now, 0.0, "mid")
    long_d = _snap(hold_snapshot(position(now), features(now), card(now), now, forecast=fc))
    short_d = _snap(
        hold_snapshot(position(now, side="short"), features(now), card(now), now, forecast=fc)
    )
    assert long_d["kronos"] == {"agree": 0, "rank": "mid"}
    assert short_d["kronos"] == {"agree": 1, "rank": "mid"}


@pytest.mark.parametrize("mode", ["direction", "rank", "off"])
@pytest.mark.parametrize("p_up", [0.0, 1.0])
def test_keeper_snapshot_kronos_is_long_short_mirrored(
    now: datetime, mode: Any, p_up: float
) -> None:
    """Long vs short with a mirrored forecast (p_up flipped, rank top <-> bottom)."""
    flip = {"top": "bottom", "bottom": "top", "mid": "mid"}
    for view in ("top", "mid", "bottom"):
        long_k = _snap(
            hold_snapshot(position(now), features(now), card(now), now,
                          forecast=_ranked(now, p_up, view), kronos=mode)
        ).get("kronos")  # fmt: skip
        short_k = _snap(
            hold_snapshot(position(now, side="short"), features(now), card(now), now,
                          forecast=_ranked(now, 1.0 - p_up, flip[view]), kronos=mode)
        ).get("kronos")  # fmt: skip
        if mode == "off":
            assert long_k is None and short_k is None
            continue
        assert long_k is not None and short_k is not None
        assert (
            set(long_k) == set(short_k) == ({"agree", "rank"} if mode == "direction" else {"rank"})
        )
        assert long_k.get("agree") == short_k.get("agree")
        assert long_k["rank"] == view and short_k["rank"] == flip[view]


async def test_keeper_passes_kronos_snapshot_mode(now: datetime) -> None:
    fc = _ranked(now, 0.0, "top")
    t = MockJevTransport(answer=close_p(0.1))
    k = _keeper(t, now, [68068.0], forecast=lambda s, n: fc, kronos_snapshot="rank")
    await k.review(position(now), now)
    assert _snap(t.calls[0])["kronos"] == {"rank": "top"}
    t2 = MockJevTransport(answer=close_p(0.1))
    k2 = _keeper(t2, now, [68068.0], forecast=lambda s, n: fc)
    await k2.review(position(now), now)
    assert _snap(t2.calls[0])["kronos"] == {"agree": 0, "rank": "top"}


def test_positions_page_shows_keeper_peak_and_giveback(now: datetime) -> None:
    from jiaoyi.panel.hb import HbSnapshot
    from jiaoyi.panel.overview import position_views

    pos = position(now, trailing_activation_pct=0.2, trailing_stop_pct=0.1)
    snap = hold_snapshot(
        pos, features(now, price=68204.0), card(now), now, history=[35.0]
    )  # 最高 35bp、现在 30bp
    old = json.dumps({"sym": "BTCUSDT", "pos": {"pnl_bps": 1.0}})  # 方案 E 之前的快照
    recs = [
        c.DecisionRecord(
            record_id=f"k{i}",
            ts=now,
            role=c.Role.KEEPER,
            outcome=c.Outcome.HOLD,
            summary="拿住",
            position_id="pos-1",
            input={"jev": {"snapshot": s}},
        )
        for i, s in enumerate([old, snap])
    ]
    [v] = position_views([pos], recs, HbSnapshot(ok=False))
    base = 68000.0 * pos.quantity
    assert v.keeper_peak_usdt == pytest.approx(base * 35 / 1e4)
    assert v.keeper_giveback_usdt == pytest.approx(base * 5 / 1e4)
    assert v.keeper_peak_r is not None and v.keeper_trail_active is True
    [v] = position_views([pos], recs[:1], HbSnapshot(ok=False))
    assert v.keeper_peak_usdt is None and v.keeper_trail_active is None


# ---- 回吐保护（Aaron 10-25）：代码规则，不问模型。止损距离 ≈ 17.7bp，0.5R ≈ 8.8bp ----


async def test_giveback_guard_closes_without_asking_model(tmp_path: Path, now: datetime) -> None:
    log = JsonlDecisionLog(tmp_path)
    t = MockJevTransport(answer=close_p(0.0))  # 模型一直说拿住
    # 浮盈 30bp（1.7R）→ 吐到 16bp（吐 47%）
    k = _keeper(t, now, [68204.0, 68109.0], FAST, recorder=RoleRecorder(c.Role.KEEPER, log))
    pos = position(now)
    assert (await k.review(pos, now)).action == "hold"
    calls = len(t.calls)
    v = await k.review(pos, now)
    assert v.action == "close" and "回吐保护" in v.reason and v.jev_request_id is None
    assert len(t.calls) == calls  # 没问模型
    rec = [r for r in log.scan_day(now.date().isoformat(), "pos-1") if r.get("outcome") == "close"]
    g = rec[-1]["output"]["giveback_guard"]
    assert g["peak_bps"] == 30.0 and g["pnl_bps"] == 16.0 and g["close_pct"] == 45.0


async def test_giveback_guard_needs_arm_and_threshold(now: datetime) -> None:
    t = MockJevTransport(answer=close_p(0.0))
    # 最高 6bp（0.34R）没到 0.5R：吐光也不平；最高 30bp 只吐 40% 也不平
    k = _keeper(t, now, [68041.0, 68000.0, 68204.0, 68122.0], FAST)
    pos = position(now)
    for _ in range(4):
        assert (await k.review(pos, now)).action == "hold"


async def test_giveback_guard_switch_off_and_peak_survives_history_cap(now: datetime) -> None:
    t = MockJevTransport(answer=close_p(0.0))
    k = _keeper(t, now, [68204.0, 68000.0], NO_GIVEBACK)
    for _ in range(2):
        assert (await k.review(position(now), now)).action == "hold"
    # 历史被截断也不丢最高浮盈
    k2 = _keeper(t, now, [68204.0, *([68150.0] * 5), 68100.0], FAST)
    pos = position(now)
    await k2.review(pos, now)
    for _ in range(5):
        await k2.review(pos, now)
    k2._history[pos.position_id].clear()
    assert (await k2.review(pos, now)).action == "close"  # 峰 30bp，现在 10bp
    k2.forget(pos.position_id)
    assert pos.position_id not in k2._peak


async def test_giveback_peak_recalled_after_restart(tmp_path: Path, now: datetime) -> None:
    log = JsonlDecisionLog(tmp_path)
    pos = position(now)
    t1 = MockJevTransport(answer=close_p(0.0))
    k1 = _keeper(t1, now, [68204.0], NO_GIVEBACK, recorder=RoleRecorder(c.Role.KEEPER, log),
                 scan_day=log.scan_day)  # fmt: skip
    await k1.review(pos, now)
    t2 = MockJevTransport(answer=close_p(0.0))
    k2 = _keeper(t2, now, [68109.0], FAST, scan_day=log.scan_day)  # 重启后吐到 15bp
    assert (await k2.review(pos, now)).action == "close"
