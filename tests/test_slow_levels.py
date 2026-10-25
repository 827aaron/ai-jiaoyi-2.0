"""关键位候选：由 K 线算、限定在现价 ±4×ATR 内、只能从候选里选。"""

from __future__ import annotations

import pytest

from jiaoyi.contracts import Bias, Category, HardFilters, LevelKind, SizeTier, SymbolPoolEntry
from jiaoyi.slow.commander import StopScale
from jiaoyi.slow.levels import (
    Candidate,
    LevelContext,
    build_candidates,
    describe_candidates,
    holding_span,
    nice_step,
    round_numbers,
    select_levels,
    swing_points,
)
from jiaoyi.slow.ta_prompts import PromptParams
from jiaoyi.slow.tradingagents_engine import (
    PERP_CONTEXT,
    _CardSymbolDraft,
    _LevelDraft,
    convert_prompt,
    describe_hold,
    draft_to_card_symbol,
    perp_context,
)


def row(o: float, h: float, lo: float, c: float, v: float = 10.0) -> list[object]:
    return [0, str(o), str(h), str(lo), str(c), str(v), 1, "0"]


def hourly_wave() -> list[list[object]]:
    """围绕 81600 来回摆动的 1h K 线，摆动高点 ~82200、低点 ~81000。"""
    rows = []
    for i in range(60):
        mid = 81600 + (600 if (i // 6) % 2 == 0 else -600)
        rows.append(row(mid, mid + 100, mid - 100, mid + 20, 10 + i % 5))
    return rows


def test_nice_step_and_round_numbers() -> None:
    assert nice_step(430) == 500
    assert nice_step(1600) == 2000
    nums = round_numbers(81600, 1600)
    assert nums and all(80000 <= n <= 83200 for n in nums)
    assert 82000 in nums or 82500 in nums or 80000 in nums


def test_swing_points_find_local_extremes() -> None:
    rows = [row(10, 11, 9, 10)] * 3 + [row(10, 15, 9, 10)] + [row(10, 11, 9, 10)] * 3
    highs, lows = swing_points(rows)
    assert (
        15.0 in highs and lows == []
    )  # 平的低点不算（要严格低于两侧才有意义，这里全等也不会产生新低点以外的值）


def test_candidates_stay_within_span_and_are_distinct() -> None:
    price, atr = 81600.0, 400.0
    span = holding_span(atr, 15, 120)  # 1.5×400×√8 ≈ 1697
    daily = [[0, "0", "86000", "72859.5", "0", "0", 1, "0"]] * 7  # 远得离谱的日线高低点
    cands = build_candidates(price, atr, hourly_wave(), daily, span)
    assert cands, "应该有候选"
    assert all(abs(c.price - price) <= span + 1e-9 for c in cands)
    assert all(c.price not in (86000.0, 72859.5) for c in cands)  # 超出 ±4×ATR 的不要
    prices = [c.price for c in cands]
    assert all(abs(a - b) >= 0.3 * atr for i, a in enumerate(prices) for b in prices[i + 1 :])
    assert [abs(p - price) for p in prices] == sorted(abs(p - price) for p in prices)


def test_prompt_lists_supports_and_resistances_sorted_by_distance() -> None:
    ctx = LevelContext(
        100.0,
        2.0,
        10.0,
        (Candidate(99.0, "昨日低点"), Candidate(103.0, "摆动高点"), Candidate(96.0, "整数关口")),
    )
    text = describe_candidates(ctx)
    assert "Support: 99 (昨日低点), 96 (整数关口)" in text
    assert "Resistance: 103 (摆动高点)" in text
    assert "never make one up from memory" in text
    assert "none" in describe_candidates(LevelContext())


def test_select_levels_snaps_to_candidates_drops_invented_and_groups() -> None:
    ctx = LevelContext(
        100.0,
        2.0,
        10.0,
        (
            Candidate(99.0, "昨日低点"),
            Candidate(97.0, "摆动低点"),
            Candidate(96.0, "整数关口"),
            Candidate(94.0, "7日低点"),
            Candidate(101.5, "摆动高点"),
            Candidate(103.0, "昨日高点"),
        ),
    )
    got = select_levels(
        [(103.2, "压力"), (72.0, "瞎编"), (98.8, "微调"), (96.0, ""), (97.0, ""), (94.0, "")],
        ctx,
    )
    assert [(x.price, x.kind) for x in got] == [
        (99.0, LevelKind.SUPPORT),  # 98.8 在容差内，对回 99.0
        (97.0, LevelKind.SUPPORT),
        (96.0, LevelKind.SUPPORT),  # 每边最多 3 个：94 被挤掉
        (103.0, LevelKind.RESISTANCE),
    ]
    assert got[0].note.startswith("昨日低点")
    assert select_levels([(99.0, "")], LevelContext()) == []  # 没有行情就不出关键位


def _entry() -> SymbolPoolEntry:
    return SymbolPoolEntry(
        symbol="BTCUSDT",
        category=Category.MAJOR,
        score=0.5,
        filters=HardFilters(
            quote_volume_24h_usdt=1e9, spread_bps=1, depth_usdt=1e7, listed_days=999, passed=True
        ),
        in_pool=True,
    )


def test_draft_levels_are_replaced_by_candidates_when_context_given() -> None:
    d = _CardSymbolDraft(
        bias=Bias.LONG,
        key_levels=[
            _LevelDraft(price=72859.5, kind=LevelKind.SUPPORT),
            _LevelDraft(price=81000.0, kind=LevelKind.RESISTANCE, note="回踩"),  # 类型也报错了
        ],
        allowed_signals=[],
        size_tier_max=SizeTier.S,
        notional_min=100,
        notional_max=200,
        leverage_min=1,
        leverage_max=2,
        stop_atr_min=1,
        stop_atr_max=2,
        take_profit_r_min=1.5,
        take_profit_r_max=3,
        reason="测试",
    )
    ctx = LevelContext(81600.0, 400.0, 2000.0, (Candidate(81000.0, "摆动低点"),))
    cs = draft_to_card_symbol(d, _entry(), ctx)
    assert [(x.price, x.kind) for x in cs.key_levels] == [(81000.0, LevelKind.SUPPORT)]
    assert len(draft_to_card_symbol(d, _entry()).key_levels) == 2  # 不给背景才照单全收


def test_convert_prompt_is_perp_flavoured_and_forbids_inventing_levels() -> None:
    from jiaoyi.core.config import load_risk_config
    from tests.conftest import ROOT

    risk = load_risk_config(ROOT / "config" / "risk.example.toml")
    ctx = LevelContext(100.0, 2.0, 10.0, (Candidate(99.0, "昨日低点"),))
    p = convert_prompt(_entry(), risk, "震荡", "", "结论", "ATR 说明", describe_candidates(ctx))
    assert "perpetual" in p and "trim" in p and "99 (昨日低点)" in p
    assert "财报" not in p


def test_convert_prompt_tells_the_model_to_fill_key_levels_from_candidates() -> None:
    from jiaoyi.core.config import load_risk_config
    from tests.conftest import ROOT

    risk = load_risk_config(ROOT / "config" / "risk.example.toml")
    ctx = LevelContext(
        100.0, 2.0, 10.0, (Candidate(99.0, "昨日低点"), Candidate(101.0, "整数关口"))
    )
    p = convert_prompt(_entry(), risk, "震荡", "", "结论", "ATR 说明", describe_candidates(ctx))
    assert "key_levels: fill it from the candidate key levels" in p
    assert "up to 3 supports and up to 3 resistances" in p
    assert "empty list disables" in p and "only when the candidates are none" in p
    # 候选在，会议按候选写 → 卡上有关键位（不空）
    d = _CardSymbolDraft(
        bias=Bias.LONG,
        key_levels=[_LevelDraft(price=99.0, kind=LevelKind.SUPPORT)],
        allowed_signals=[],
        size_tier_max=SizeTier.S,
        notional_min=100,
        notional_max=200,
        leverage_min=1,
        leverage_max=2,
        stop_atr_min=1,
        stop_atr_max=2,
        take_profit_r_min=1.5,
        take_profit_r_max=3,
        reason="测试",
    )
    assert draft_to_card_symbol(d, _entry(), ctx).key_levels


def test_perp_context_replaces_stock_rating_words() -> None:
    assert "PERPETUAL" in PERP_CONTEXT and "Underweight = lean short" in PERP_CONTEXT


def test_span_is_one_formula_scaled_by_own_volatility_and_holding_time() -> None:
    """同一条公式：σ×ATR×√(持仓时长/ATR 周期)，不分币种、不用百分比表。"""
    eth_like = holding_span(6.0, 15, 120)  # 现价 2390、15m ATR≈0.25%
    assert eth_like == pytest.approx(1.5 * 6 * 8**0.5)
    assert eth_like / 2390 < 0.015  # 约 1.1%，不是上次的 ±9.8%
    # 同一个币：持仓越久范围越宽，√ 时间比例
    assert holding_span(6.0, 15, 60) < holding_span(6.0, 15, 120)
    assert holding_span(6.0, 15, 120) / holding_span(6.0, 15, 30) == pytest.approx(2.0)
    # 波动大的币自动更宽：范围只跟自己的 ATR 成正比
    assert holding_span(60.0, 15, 120) == pytest.approx(10 * eth_like)


def test_hold_note_and_perp_context_tell_the_meeting_the_holding_time() -> None:
    note = describe_hold(120, 6.0, 2390.0, StopScale(period_min=15))
    assert "120 minutes" in note and "±" in note and "sqrt(120/15)" in note
    assert "days" in describe_hold(30, None, None, StopScale(period_min=5))
    assert "about 45 minutes" in perp_context(PromptParams(hold_minutes=45))


def test_level_context_end_to_end_stays_inside_holding_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ETH 量级：现价 2390、15m 振幅约 0.3%，2h 档范围约 ±1%，不会出现 −9.8% / +6.6% 这种价位。"""
    from datetime import UTC, datetime

    import jiaoyi.slow.tradingagents_engine as eng

    end = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    end_ms = int(end.timestamp() * 1000)

    def fake(symbol: str, interval: str, limit: int, end_dt: object = None) -> list[list[object]]:
        n = 300 if interval == "15m" else 12
        step = 900_000 if interval == "15m" else 86_400_000
        rows = []
        for i in range(n):
            t = end_ms - (n - i) * step
            mid = 2390 + (12 if (i // 8) % 2 == 0 else -12) + (i % 3)
            if interval == "1d":
                rows.append([t, "0", "2492", "2156", "2390", "1", t + step - 1, "0"])
            else:
                rows.append(
                    [t, str(mid), str(mid + 4), str(mid - 4), str(mid), "10", t + step - 1, "0"]
                )
        return rows

    monkeypatch.setattr(eng, "_klines", fake)
    ctx = eng.level_context("ETHUSDT", end, StopScale(period_min=15), 120)
    assert ctx.price and ctx.span and ctx.candidates
    assert ctx.span / ctx.price < 0.03
    assert all(abs(c.price - ctx.price) <= ctx.span + 1e-9 for c in ctx.candidates)
    assert all(c.price not in (2492.0, 2156.0) for c in ctx.candidates)  # 远处的日线高低点被滤掉


def test_atr_interval_uses_binance_names() -> None:
    from jiaoyi.slow.tradingagents_engine import atr_interval

    got = {m: atr_interval(m) for m in (1, 5, 15, 30, 60, 120, 240, 360, 720, 1440)}
    assert got == {
        1: "1m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 120: "2h", 240: "4h",
        360: "6h", 720: "12h", 1440: "1d",
    }  # fmt: skip


def test_level_context_logs_failure_instead_of_swallowing(caplog: pytest.LogCaptureFixture) -> None:
    from datetime import UTC, datetime

    import jiaoyi.slow.tradingagents_engine as te
    from jiaoyi.slow.commander import StopScale

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("400 bad interval")

    orig = te._klines
    te._klines = boom  # type: ignore[assignment]
    try:
        with caplog.at_level("WARNING"):
            ctx = te.level_context("ETHUSDT", datetime(2026, 10, 25, tzinfo=UTC), StopScale(), 240)
    finally:
        te._klines = orig
    assert not ctx.candidates and "ETHUSDT 算关键位候选失败" in caplog.text


def test_convert_schema_requires_key_levels_and_allowed_signals() -> None:
    """可选数组会被模型在工具调用里整个跳过（10-25 ENA 实测），所以两项必须在 required 里。"""
    required = _CardSymbolDraft.model_json_schema()["required"]
    assert "key_levels" in required and "allowed_signals" in required


def test_omitted_arrays_are_detected_and_do_not_lose_the_card() -> None:
    """模型漏填 key_levels / allowed_signals：解析不失败，引擎能发现并重问，最后仍出卡。"""
    from jiaoyi.slow.tradingagents_engine import omitted_fields, reask_prompt

    base = dict(
        bias="long", size_tier_max="S", notional_min=100, notional_max=200, leverage_min=1,
        leverage_max=2, stop_atr_min=1, stop_atr_max=2, take_profit_r_min=1.5,
        take_profit_r_max=3, reason="测试",
    )  # fmt: skip
    d = _CardSymbolDraft(**base)  # 两个数组都没写，照样解析
    assert omitted_fields(d, True) == ["key_levels", "allowed_signals"]
    assert omitted_fields(d, False) == ["allowed_signals"]  # 没候选就不用写 key_levels
    assert omitted_fields(_CardSymbolDraft(**base, key_levels=[], allowed_signals=[]), True) == []
    assert "left out key_levels and allowed_signals" in reask_prompt(
        "P", ["key_levels", "allowed_signals"]
    )
    cs = draft_to_card_symbol(d, _entry())  # 重问后仍漏：空列表照出卡（allowed_signals 走默认全部）
    assert cs.key_levels == [] and cs.allowed_signals
