"""拍板人一道「做不做」（Aaron 10-24）：题目按持仓档、技术面、额外快照、风控员只看「做」。"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from jiaoyi import contracts as c
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.context import (
    EntryContextSource,
    EntryExtras,
    horizon_for,
    rsi14,
    tech_block,
)
from jiaoyi.gate.entry import JevEntryGate, summarize
from jiaoyi.gate.jev import MockJevTransport
from jiaoyi.gate.questions import (
    MAX_SNAPSHOT_CHARS,
    ONE_TAKE_PROMPT,
    entry_questions,
    entry_snapshot_one,
)
from jiaoyi.gate.replay import Found, candidate
from jiaoyi.keeper.keeper import _entry_block
from jiaoyi.market.features import EntryContext, RollingFeatureEngine
from tests.fastloop_helpers import (
    FAST,
    GOOD_LONG,
    account,
    answer_with,
    card,
    jev,
    signal,
)
from tests.test_gate import model
from tests.test_risk_guard import guard

ONE = {k: GOOD_LONG[k] for k in ("take", "stop_width", "size")}

# ---------------------------------------------------------------------- 题目


def test_take_prompt_is_the_old_wording_without_kronos() -> None:
    """10-25 A/B：「Take it only if all of these hold together」把「做」压到 0.08，改回旧措辞。"""
    p = ONE_TAKE_PROMPT
    assert p.startswith("Considering everything, should we take this trade in direction `side`")
    assert "all of these hold together" not in p
    # 和 A/B 测过的原文一字不差（目标说明里顺带提到 kronos，但快照里不放预测员）
    take = next(q for q in entry_questions("one", 240) if q.id == "take")
    assert take.prompt == p


def test_one_question_request_validates(now: datetime) -> None:
    qs = entry_questions("one", 120)
    assert [q.id for q in qs] == ["take", "stop_width", "size"]
    sig = signal(now)
    req = c.JevRequest(
        request_id="r",
        kind="entry",
        symbol="BTCUSDT",
        ts=now,
        signal_id=sig.signal_id,
        snapshot="{}",
        questions=qs,
    )
    assert req.questions[0].prompt.startswith("Considering everything")
    with pytest.raises(ValidationError):  # 别的组合（例如只有 take）不收
        c.JevRequest(
            request_id="r",
            kind="entry",
            symbol="BTCUSDT",
            ts=now,
            signal_id=sig.signal_id,
            snapshot="{}",
            questions=qs[:1],
        )
    assert len(entry_questions("six")) == 6


# ---------------------------------------------------------------------- 技术面


def _bars(
    now: datetime, n: int, start: float = 100.0, step: float = 0.01, vol: float = 10.0
) -> list[tuple[int, float, float, float, float, float]]:
    """到 ``now`` 前一分钟为止的 ``n`` 根 1 分钟线，每分钟涨 ``step``。"""
    end = int(now.timestamp() // 60)
    out = []
    for i in range(n):
        o = start + i * step
        out.append((end - n + i, o, o + step, o - step / 2, o + step, vol))
    return out


def _ctx(now: datetime, bars: list[Any], **kw: Any) -> EntryContext:
    return EntryContext(
        now=now.timestamp(),
        price=bars[-1][4] if bars else 100.0,
        bars=bars,
        btc_bars=kw.pop("btc", bars),
        flow=kw.pop("flow", {}),
        imb=kw.pop("imb", {}),
    )


def test_tech_block_two_hour_tier(now: datetime) -> None:
    bars = _bars(now, 1500, step=0.02)  # 每分钟约 2 bp：15 分钟 > 15 bp 算 up
    t = tech_block(
        _ctx(now, bars), horizon_for(120), atr_bps=50.0, levels_bps=[100.0, -200.0, 300.0]
    )
    assert t is not None
    for k in ("15m", "1h", "4h"):
        assert t[f"chg_{k}_bps"] > 0 and t[f"trend_{k}"] == "up"
    px, op = bars[-1][4], bars[-60][1]
    assert t["chg_1h_bps"] == pytest.approx(round((px / op - 1) * 1e4, 1))
    assert t["rsi14_5m"] == 100 and t["rsi14_1h"] == 100  # 一路涨
    assert t["pos_in_range_pct"] == 100 and 23.9 <= t["range_hours"] <= 24.0
    assert t["vwap_dist_bps"] > 0
    assert t["level_above_atr"] == 2.0 and t["level_below_atr"] == 4.0
    assert t["btc_1h_bps"] == t["chg_1h_bps"]


def test_tech_block_skips_what_it_cannot_compute(now: datetime) -> None:
    short = _bars(now, 90)  # 1.5 小时：没有 4 小时涨跌，1 小时 RSI 不够 15 个点
    t = tech_block(_ctx(now, short), horizon_for(120), atr_bps=50.0)
    assert t is not None
    assert "chg_4h_bps" not in t and "rsi14_1h" not in t and "chg_1h_bps" in t
    assert t["level_above_atr"] is None and t["level_below_atr"] is None
    stale = _bars(now - timedelta(minutes=30), 1500)  # 最后一根收盘超过 10 分钟：整块不写
    assert tech_block(_ctx(now, stale), horizon_for(120), atr_bps=50.0) is None
    assert tech_block(_ctx(now, []), horizon_for(120), atr_bps=50.0) is None


def test_long_tier_uses_rest_bars(now: datetime) -> None:
    bars = _bars(now, 1500)
    end = int(now.timestamp() // 60)
    h4 = [(end - 240 * (20 - i), 90.0 + i, 91.0 + i, 89.0 + i, 90.5 + i, 1.0) for i in range(20)]
    t = tech_block(_ctx(now, bars), horizon_for(240), atr_bps=50.0, rest={"4h": h4})
    assert t is not None and t["rsi14_4h"] == 100 and "rsi14_15m" in t and "chg_1d_bps" in t
    t = tech_block(_ctx(now, bars), horizon_for(240), atr_bps=50.0)
    assert t is not None and "rsi14_4h" not in t  # 没拉到就不写


def test_rsi() -> None:
    assert rsi14([1.0] * 14) is None
    assert rsi14([1.0] * 15) == 50
    assert rsi14([float(i % 2) for i in range(15)]) == 50


# ---------------------------------------------------------------------- 快照


def test_one_snapshot_has_full_card_and_no_predictor(now: datetime) -> None:
    sig = signal(now)
    fc = c.Forecast(
        symbol="BTCUSDT",
        ts=now,
        horizon_min=120,
        price_low=60000.0,
        price_high=70000.0,
        p_up=1.0,
        model="Kronos-small",
        stale_after=now + timedelta(minutes=10),
    )
    sig = sig.model_copy(update={"forecast": fc})
    ex = EntryExtras(
        {
            "tech": {"chg_1h_bps": 12.0},
            "book_imb": {"1m": 0.2, "5m": 0.1},
            "flow": {"cvd_5m_k": 3.0},
            "deriv": {"adl": "low"},
            "day": {"chg_pct": 1.2},
        }
    )
    d = json.loads(entry_snapshot_one(sig, card(now), 120, ex))
    assert "kronos" not in d
    cd = d["card"]
    # 卡只给方向、关键位、止损止盈、仓位上限、大盘判断（10-25 A/B：理由 / 区间 / 卡龄压低「做」）
    assert set(cd) == {"bias", "levels", "stop_atr", "tp_r", "size_max", "regime"}
    assert all(isinstance(lv, list) and len(lv) == 2 for lv in cd["levels"])
    assert d["book"]["imb_1m"] == 0.2 and "imb" not in d["book"]
    assert d["tech"] == {"chg_1h_bps": 12.0} and d["deriv"] == {"adl": "low"}


def test_one_snapshot_trims_in_order_and_stays_valid(now: datetime) -> None:
    big = {f"k{i}": 1234.5 for i in range(80)}
    ex = EntryExtras(
        {
            "tech": big,
            "flow": {"big_15m": {"buy": 1, "sell": 2}, **big},
            "day": {"chg_pct": 1.0},
            "deriv": big,
        }
    )
    long_card = card(now, reason="很长的理由" * 60)
    long_card = long_card.model_copy(update={"regime": "大盘" * 200})
    s = entry_snapshot_one(signal(now), long_card, 120, ex)
    assert len(s) <= MAX_SNAPSHOT_CHARS
    d = json.loads(s)
    assert "regime" not in d["card"] and "day" not in d  # 先删大盘判断，再删 24 小时
    assert ex.blocks["flow"]["big_15m"] == {"buy": 1, "sell": 2}  # 日志里的原值不动


# ---------------------------------------------------------------------- 额外数据和补拉


class _Fetch:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.calls: list[str] = []

    async def __call__(self, path: str, params: dict[str, Any], weight: int) -> Any:
        self.calls.append(path)
        await asyncio.sleep(self.delay)
        if "topLongShortAccountRatio" in path:
            return [{"timestamp": 1, "longShortRatio": "1.8"}]
        if "takerlongshortRatio" in path:
            return [{"timestamp": i, "buyVol": "2", "sellVol": "1"} for i in range(48)]
        if "fundingRate" in path:
            return [{"fundingTime": i, "fundingRate": "0.0001"} for i in range(3)]
        if "ticker/24hr" in path:
            return {
                "priceChangePercent": "2.5",
                "quoteVolume": "123000000",
                "highPrice": "69000",
                "lowPrice": "67000",
            }
        return []


def _local(now: datetime) -> Any:
    bars = _bars(now, 1500, start=67000.0, step=1.0)
    ctx = _ctx(
        now,
        bars,
        flow={
            "cvd_5m": 5000.0,
            "buy_5m": 0.6,
            "big_buy_n": 2.0,
            "big_sell_n": 1.0,
            "big_pct": 12.0,
        },
        imb={"1m": 0.3},
    )
    return lambda sym: ctx if sym == "BTCUSDT" else None


async def test_context_source_merges_rest_and_caches(now: datetime) -> None:
    f = _Fetch()
    src = EntryContextSource(_local(now), adl=lambda s: "low", fetch=f)
    values = {"mark_premium_bps": 1.234, "funding_in_min": 42.4}
    ex = await src("BTCUSDT", 120, 50.0, [100.0], values)
    b = ex.blocks
    assert b["deriv"] == {
        "premium_bps": 1.23,
        "next_funding_min": 42,
        "funding_prev": [1.0, 1.0, 1.0],
        "top_acct_ls": 1.8,
        "adl": "low",
    }
    assert b["flow"]["taker_ratio_1h"] == 2.0 and b["flow"]["big_15m"] == {
        "buy": 2,
        "sell": 1,
        "pct": 12.0,
    }
    assert b["day"]["chg_pct"] == 2.5 and b["day"]["qv_m"] == 123.0
    assert "tech" in b and b["book_imb"] == {"1m": 0.3} and ex.missing == ()
    n = len(f.calls)
    await src("BTCUSDT", 120, 50.0, [], values)
    assert len(f.calls) == n  # 按币缓存，不重复拉


async def test_context_source_does_not_wait_for_slow_rest(now: datetime) -> None:
    src = EntryContextSource(_local(now), fetch=_Fetch(delay=1.0), timeout_s=0.05)
    t0 = asyncio.get_running_loop().time()
    ex = await src("BTCUSDT", 120, 50.0, [], {})
    assert asyncio.get_running_loop().time() - t0 < 0.5
    assert set(ex.missing) == {"top_acct", "taker", "funding", "day"}
    assert "tech" in ex.blocks and "day" not in ex.blocks


async def test_gate_asks_one_question_and_logs_extra(tmp_path: Path, now: datetime) -> None:
    log = JsonlDecisionLog(tmp_path)
    t = MockJevTransport(answer=answer_with(GOOD_LONG))
    src = EntryContextSource(_local(now), fetch=_Fetch())
    gate = JevEntryGate(model(t), RoleRecorder(c.Role.GATE, log), questions="one", context=src)
    r = await gate.evaluate(signal(now), card(now), now)
    assert r.ok and {a.id for a in r.answers} == {"take", "stop_width", "size"}
    [rec] = log.read_day(now.date().isoformat())
    jv = rec.input["jev"]
    assert jv["questions"] == ["take", "stop_width", "size"] and jv["template"] == "q9"
    assert "tech" in jv["extra"] and "rest_ms" in jv["extra"]
    assert "tech" in json.loads(jv["snapshot"])
    assert summarize(r).startswith("做 take")


async def test_six_mode_keeps_old_questions(now: datetime) -> None:
    t = MockJevTransport(answer=answer_with(GOOD_LONG))
    gate = JevEntryGate(model(t), questions="six", context=EntryContextSource(_local(now)))
    r = await gate.evaluate(signal(now), card(now), now)
    assert r.ok and len(r.answers) == 6


# ---------------------------------------------------------------------- 风控员、旧日志


def test_guard_one_question_only_checks_take(now: datetime) -> None:
    g = guard()  # 默认门槛 0.55
    ok = g.review(
        signal(now), jev(ONE, take={"take": 0.56, "skip": 0.44}), card(now), account(now), now
    )
    assert ok.approved, ok.reasons
    no = g.review(
        signal(now), jev(ONE, take={"take": 0.5, "skip": 0.5}), card(now), account(now), now
    )
    assert any("“做”概率 0.50 低于门槛 0.55" in r for r in no.reasons), no.reasons
    assert not any("真信号" in r or "方向" in r or "有毒" in r for r in no.reasons)
    part = {k: v for k, v in ONE.items() if k != "size"}
    bad = g.review(signal(now), jev(part), card(now), account(now), now)
    assert any("没回答“size”" in r for r in bad.reasons)


def test_guard_six_questions_still_check_all(now: datetime) -> None:
    v = guard(gate_questions="six").review(
        signal(now), jev(genuine={"real": 0.1, "fake": 0.9}), card(now), account(now), now
    )
    assert any("真信号" in r for r in v.reasons)


def test_keeper_entry_block_without_genuine() -> None:
    rec = {
        "input": {"jev": {"snapshot": json.dumps({"sig": "volume_surge", "why": "x"})}},
        "output": {"jev": {"answers": {"take": {"take": 0.61, "skip": 0.39}}}},
    }
    assert _entry_block(rec) == {"sig": "volume_surge", "why": "x", "p_take": 0.61}


def test_replay_candidate_one_question(now: datetime) -> None:
    f = Found(signal=signal(now), card=card(now))
    r = jev(ONE, take={"take": 0.6, "skip": 0.4})
    cand = candidate(f, r, FAST, 120)
    assert cand is not None and not cand.gate_would_block and cand.p_real is None


# ---------------------------------------------------------------------- 特征引擎


def test_engine_entry_context(now: datetime) -> None:
    eng = RollingFeatureEngine(atr_period_min=1)
    t0 = now - timedelta(minutes=20)
    for i in range(20):
        ot = t0 + timedelta(minutes=i)
        eng.update(
            c.Kline(
                symbol="BTCUSDT",
                interval=c.Interval.M1,
                open_time=ot,
                close_time=ot + timedelta(seconds=59.999),
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.5,
                volume=5.0,
            )
        )
    trades = [
        (now - timedelta(minutes=30), 20_000.0, c.Side.LONG),
        (now - timedelta(minutes=10), 200.0, c.Side.SHORT),
        (now - timedelta(seconds=30), 15_000.0, c.Side.SHORT),
    ]
    for ts, usdt, side in trades:
        eng.update(c.Trade(symbol="BTCUSDT", ts=ts, price=100.0, qty=usdt / 100.0, taker_side=side))
    for s in range(3):
        eng.update(
            c.BookTop(
                symbol="BTCUSDT",
                ts=now - timedelta(seconds=2 - s),
                bids=[(99.9, 10.0 + s)],
                asks=[(100.1, 10.0)],
            )
        )
    ctx = eng.entry_context("BTCUSDT")
    assert ctx is not None and len(ctx.bars) == 20 and ctx.bars[-1][4] == 100.5
    f = ctx.flow
    assert f["cvd_5m"] == pytest.approx(-15_000.0)
    assert f["cvd_15m"] == pytest.approx(-15_200.0)
    assert f["cvd_1h"] == pytest.approx(4_800.0)
    assert f["big_buy_n"] == 0 and f["big_sell_n"] == 1  # 30 分钟前那笔大买单不在 15 分钟里
    assert "1m" in ctx.imb and "5m" in ctx.imb
    assert eng.entry_context("ETHUSDT") is None


def test_now_fixture_is_utc(now: datetime) -> None:
    assert now.tzinfo == UTC


async def test_realistic_snapshot_length(now: datetime) -> None:
    """完整的额外块（技术面、成交流、衍生品、24 小时）加上完整作战卡，不用删就放得下。"""
    src = EntryContextSource(_local(now), adl=lambda s: "low", fetch=_Fetch())
    ex = await src(
        "BTCUSDT", 120, 50.0, [100.0, -80.0], {"mark_premium_bps": 1.0, "funding_in_min": 30.0}
    )
    s = entry_snapshot_one(signal(now), card(now), 120, ex)
    d = json.loads(s)
    assert {"tech", "flow", "deriv", "day"} <= set(d) and "regime" in d["card"]
    assert len(s) <= 2400, len(s)


def test_guard_question_set_comes_from_config_not_the_answer(now: datetime) -> None:
    """six 下模型漏答 genuine：拦（不能当成一道题把真 / 毒 / 方向门槛跳过去）。"""
    v = guard(gate_questions="six").review(signal(now), jev(ONE), card(now), account(now), now)
    assert not v.approved and any("没回答“genuine”" in r for r in v.reasons)
    # one 下多答了别的题也只看「做」
    v = guard().review(
        signal(now), jev(genuine={"real": 0.0, "fake": 1.0}), card(now), account(now), now
    )
    assert v.approved, v.reasons
