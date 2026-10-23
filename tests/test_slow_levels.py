"""关键位候选：由 K 线算、限定在现价 ±4×ATR 内、只能从候选里选。"""

from __future__ import annotations

from jiaoyi.contracts import Bias, Category, HardFilters, LevelKind, SizeTier, SymbolPoolEntry
from jiaoyi.slow.levels import (
    Candidate,
    LevelContext,
    build_candidates,
    describe_candidates,
    nice_step,
    round_numbers,
    select_levels,
    swing_points,
)
from jiaoyi.slow.tradingagents_engine import (
    PERP_CONTEXT,
    _CardSymbolDraft,
    _LevelDraft,
    convert_prompt,
    draft_to_card_symbol,
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
    daily = [[0, "0", "86000", "72859.5", "0", "0", 1, "0"]] * 7  # 远得离谱的日线高低点
    cands = build_candidates(price, atr, hourly_wave(), daily)
    assert cands, "应该有候选"
    assert all(abs(c.price - price) <= 4 * atr + 1e-9 for c in cands)
    assert all(c.price not in (86000.0, 72859.5) for c in cands)  # 超出 ±4×ATR 的不要
    prices = [c.price for c in cands]
    assert all(abs(a - b) >= 0.3 * atr for i, a in enumerate(prices) for b in prices[i + 1 :])
    assert [abs(p - price) for p in prices] == sorted(abs(p - price) for p in prices)


def test_prompt_lists_supports_and_resistances_sorted_by_distance() -> None:
    ctx = LevelContext(
        100.0,
        2.0,
        (Candidate(99.0, "昨日低点"), Candidate(103.0, "摆动高点"), Candidate(96.0, "整数关口")),
    )
    text = describe_candidates(ctx)
    assert "支撑：99（昨日低点）、96（整数关口）" in text
    assert "压力：103（摆动高点）" in text
    assert "不要凭印象编价位" in text
    assert "暂无" in describe_candidates(LevelContext())


def test_select_levels_snaps_to_candidates_drops_invented_and_groups() -> None:
    ctx = LevelContext(
        100.0,
        2.0,
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
    ctx = LevelContext(81600.0, 400.0, (Candidate(81000.0, "摆动低点"),))
    cs = draft_to_card_symbol(d, _entry(), ctx)
    assert [(x.price, x.kind) for x in cs.key_levels] == [(81000.0, LevelKind.SUPPORT)]
    assert len(draft_to_card_symbol(d, _entry()).key_levels) == 2  # 不给背景才照单全收


def test_convert_prompt_is_perp_flavoured_and_forbids_inventing_levels() -> None:
    from jiaoyi.core.config import load_risk_config
    from tests.conftest import ROOT

    risk = load_risk_config(ROOT / "config" / "risk.example.toml")
    ctx = LevelContext(100.0, 2.0, (Candidate(99.0, "昨日低点"),))
    p = convert_prompt(_entry(), risk, "震荡", "", "结论", "ATR 说明", describe_candidates(ctx))
    assert "永续合约" in p and "减持" in p and "99（昨日低点）" in p
    assert "财报" not in p


def test_perp_context_replaces_stock_rating_words() -> None:
    assert "PERPETUAL" in PERP_CONTEXT and "Underweight = lean short" in PERP_CONTEXT
