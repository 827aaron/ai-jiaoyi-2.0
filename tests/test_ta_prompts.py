"""7 档词表喂四处的提示词（tier-prompts-spec v2 §2–§4）：纯文本测试，不需要 tradingagents。"""

from __future__ import annotations

import re

import pytest

from jiaoyi.contracts import (
    Bias,
    CardSymbol,
    Category,
    Range,
    SizeTier,
)
from jiaoyi.core import profiles
from jiaoyi.slow import ta_prompts as tp
from jiaoyi.slow import tradingagents_engine as eng
from jiaoyi.slow.ticks import CACHE_S, TickSizes

HOLDS = profiles.HOLDS


def _all_texts(p: tp.PromptParams) -> list[str]:
    return [
        tp.perp_context(p),
        tp.pick_prompt(p, "2026-10-24 12:00", 8, "上一轮", "MARKET", "TABLE"),
        tp.meeting_note(p, "BTC-USD", "BTCUSDT", "震荡", "反弹", "0.10"),
        tp.position_note(p, "BTCUSDT"),
        tp.news_prompt(p, "BTCUSDT"),
        *(tp.role_prompt(r, p, "BTC-USD") for r in ("bull", "bear")),
        *(tp.role_prompt(r, p) for r in ("aggressive", "conservative", "neutral")),
    ]


@pytest.mark.parametrize("hold", HOLDS)
def test_every_tier_renders_with_all_slots_filled(hold: str) -> None:
    p = tp.PromptParams.for_hold(hold)
    for text in _all_texts(p):
        assert text.strip()
        assert not re.search(r"\{[A-Za-z_]+\}", text), text[:200]  # 没有残留的槽位
    shared = tp.perp_context(p)
    assert p.horizon in shared and f"{p.fee_pct}%" in shared and f"{p.indicator} bars" in shared
    assert f"about {p.hold_minutes} minutes" in shared
    assert "BTCUSDT" in shared and "Binance USDT-margined PERPETUAL" in shared
    w = tp.words(p)
    for field in w.brief_main:  # 挑币提示词里的依据字段 = 这一档简报显示的字段
        assert tp.BRIEF_FIELDS_ZH[field] in tp.pick_prompt(p, "t", 3)
    assert shared.rstrip().endswith("Apply all of this to every sentence you write")


def test_styles_follow_the_hold_tier() -> None:
    assert [profiles.prompt_style(h) for h in HOLDS] == ["short"] * 3 + ["mid"] + ["long"] * 3
    short, mid, long_ = (tp.perp_context(tp.PromptParams.for_hold(h)) for h in ("2h", "4h", "12h"))
    for text in (short, mid):
        assert "tend to keep going" not in text
    assert "gives back part of it" in short and "Re-judge every round" not in short
    assert "both effects are weak" in mid
    assert "Re-judge every round" in long_ and "are not reversal signals on their own" in long_
    assert "not today or this week" in short and "not this week" in mid
    assert "not today" not in long_ and "not this week" not in long_


def test_two_hour_shared_text_snapshot() -> None:
    p = tp.PromptParams.for_hold("2h", 120)
    text = tp.perp_context(p)
    assert (
        "Horizon: every position is held at most about 120 minutes (2 hours) and then "
        "force-closed. You are judging the next 2 hours for this coin, not today or this week; "
        "size targets, stops and levels to moves plausible within 2 hours." in text
    )
    assert (
        "Evidence that matters at this horizon is the last 15 minutes to 5 hours: the % change "
        "over the last 15m, 1h and 4h" in text
    )
    assert "On 15m bars, an extreme RSI or a Bollinger-band breach" in text
    assert "a fee round trip is about 35% of a typical 2 hours move" in text
    assert "the system enforces this in code" in text
    assert "Role notes. Market analyst: compute indicators on 15m bars" in text
    assert "ignore any note" not in text and "keeper" not in text  # 持仓说明改由 position_note 给


def test_custom_hold_minutes_use_the_tier_words() -> None:
    p = tp.PromptParams.for_hold("2h", 90)  # 自定义 90 分钟取不短于它的 2h 档，{HM} 写实际的
    text = tp.perp_context(p)
    assert "about 90 minutes (2 hours)" in text and "35%" in text


def test_pick_prompt_snapshot_and_previous_round_line() -> None:
    p = tp.PromptParams.for_hold("2h")
    text = tp.pick_prompt(p, "2026-10-24 12:00", 8, "BTC 压在 20 日线下 {x}", "【简报】", "表")
    assert "本系统每笔单最多拿 2 小时，你判断的是未来 2 小时 的大盘，不是今天、也不是这周" in text
    assert "上一轮的大盘判断只作参考，不要照抄" in text and "BTC 压在 20 日线下 {x}" in text
    assert "20 日线、日线 RSI、7 天涨跌只作背景，不能单独决定方向" in text
    assert "再挑最多 8 个" in text and "【简报】" in text and text.rstrip().endswith("表")
    assert "上一轮" not in tp.pick_prompt(p, "t", 8)  # 没有上一轮：整行去掉


def test_meeting_note_carries_regime_reason_and_tick() -> None:
    p = tp.PromptParams.for_hold("2h")
    note = tp.meeting_note(p, "STRK-USD", "STRKUSDT", "偏空震荡", "反弹标的", "0.00010000")
    assert "`STRK-USD` is the Binance USDT-M perpetual STRKUSDT" in note
    assert "price tick is 0.00010000, so quote prices to 4 decimals" in note
    assert "偏空震荡" in note and "反弹标的" in note and "next 2 hours decides" in note
    no_tick = tp.meeting_note(p, "STRK-USD", "STRKUSDT", "r", "x", None)
    assert "tick" not in no_tick
    forced = tp.meeting_note(p, "BTC-USD", "BTCUSDT", "r", "", None)
    assert tp.FORCED_REASON in forced and "position" not in forced.lower()  # 不提持仓


def test_position_note_replaces_the_framework_flat_book_warning() -> None:
    note = tp.position_note(tp.PromptParams.for_hold("4h"), "ETHUSDT")
    assert "ETHUSDT within the next 4 hours" in note and "from scratch" in note
    assert "do not assume a flat book" not in note


@pytest.mark.parametrize("hold", HOLDS)
def test_roles_have_no_stock_talk(hold: str) -> None:
    p = tp.PromptParams.for_hold(hold)
    for role in ("bull", "bear"):
        text = tp.role_prompt(role, p, "BTC-USD")
        # 「不要用股票式论点」的禁用清单里出现这些词没关系，别的地方不能
        body = re.sub(r"Do not use equity-style arguments \([^)]*\)", "", text)
        for word in ("growth", "revenue", "branding", "diversification", "competitive adv"):
            assert word not in body.replace("long-term adoption", "")
    cons = tp.role_prompt("conservative", p)
    assert "leverage" in cons and "funding" in cons


def test_researcher_price_line_follows_the_tier_words() -> None:
    short = tp.role_prompt("bull", tp.PromptParams.for_hold("2h"))
    mid = tp.role_prompt("bear", tp.PromptParams.for_hold("4h"))
    long_ = tp.role_prompt("bull", tp.PromptParams.for_hold("24h"))
    assert "last 15m / 1h / 4h" in short and "higher lows" in short
    assert "last 1h / 4h / 24h" in mid and "lower highs" in mid
    assert "daily and 4h trend" in long_ and "7-day change" in long_


# ---------------------------------------------------------------- 评级兜底（§4.6）
def _card(bias: Bias = Bias.SHORT, tier: SizeTier = SizeTier.L) -> CardSymbol:
    return CardSymbol(
        symbol="BTCUSDT",
        category=Category.MAJOR,
        bias=bias,
        allowed_signals=[],
        size_tier_max=tier,
        notional_usdt=Range(min=1, max=2),
        leverage=Range(min=1, max=2),
        stop_atr=Range(min=1, max=2),
        take_profit_r=Range(min=1, max=2),
        reason="理由",
    )


def test_rating_guard_table() -> None:
    g = eng.rating_guard
    # 两人方向相反 → 观望
    out = g(_card(), "overweight", "sell")
    assert out.bias is Bias.NO_TRADE and "方向相反" in out.reason and out.allowed_signals == []
    # 任何一个是 Hold → 观望，注明是谁
    assert g(_card(), "hold", "sell").bias is Bias.NO_TRADE
    assert "组合经理是观望，这一轮不做" in g(_card(), "sell", "hold").reason
    # 同方向、较弱的是偏向 → S 仓（可配置），注明
    for rm, pm in (
        ("sell", "underweight"),
        ("underweight", "sell"),
        ("underweight", "underweight"),
    ):
        out = g(_card(), rm, pm)
        assert out.bias is Bias.SHORT and out.size_tier_max is SizeTier.M, (rm, pm)
        assert "只是偏空，仓位最多 M、等区间" in out.reason
    assert g(_card(Bias.LONG), "overweight", "overweight").reason.endswith("等区间）")
    # 偏向仓位档可配置，且只压低不抬高
    assert g(_card(), "underweight", "sell", lean_tier=SizeTier.S).size_tier_max is SizeTier.S
    assert g(_card(tier=SizeTier.S), "underweight", "sell").size_tier_max is SizeTier.S  # 不抬高
    assert g(_card(tier=SizeTier.M), "underweight", "sell").size_tier_max is SizeTier.M
    assert g(same_strong := _card(tier=SizeTier.L), "sell", "sell").size_tier_max is SizeTier.L
    assert same_strong.size_tier_max is SizeTier.L
    # 要区间的档没有区间 → 观望
    assert g(_card(), "underweight", "sell", zone_ok=False).bias is Bias.NO_TRADE
    assert g(_card(), "sell", "sell", zone_ok=False).bias is Bias.SHORT  # 两个都是 Sell：照转卡
    # 两个都是 Buy / Sell → 照转卡结果
    same = _card()
    assert g(same, "sell", "sell") == same
    # 转卡方向比结果强（和评级相反）→ 观望
    assert g(_card(Bias.LONG), "sell", "sell").bias is Bias.NO_TRADE
    # 解析不出评级：不动；本来就是观望：不动
    assert g(same, None, None) == same
    nt = _card(Bias.NO_TRADE)
    assert g(nt, "sell", "sell") is nt


def test_rating_guard_never_makes_a_card_stronger() -> None:
    order = {Bias.NO_TRADE: 0, Bias.SHORT: 1, Bias.LONG: 1}
    ratings = ["buy", "overweight", "hold", "underweight", "sell", None]
    for bias in (Bias.LONG, Bias.SHORT, Bias.NO_TRADE):
        for rm in ratings:
            for pm in ratings:
                out = eng.rating_guard(_card(bias, SizeTier.M), rm, pm)
                assert order[out.bias] <= order[bias]
                assert out.bias is bias or out.bias is Bias.NO_TRADE
                tiers = {SizeTier.S: 0, SizeTier.M: 1, SizeTier.L: 2}
                assert tiers[out.size_tier_max] <= 1  # 偏向最多 M；起点就是 M


def test_research_recommendation_is_parsed_from_the_plan() -> None:
    plan = "**Recommendation**: Underweight\n\n**Rationale**: x"
    assert eng.research_recommendation(plan) == "underweight"
    assert eng.research_recommendation("**Recommendation**: **Sell**") == "sell"
    assert eng.research_recommendation("nothing") is None
    assert eng.research_recommendation(None) is None


# ---------------------------------------------------------------- 配置 / 档位
def test_build_config_has_tier_context_and_stops_the_original_memory() -> None:
    s = eng.TradingAgentsSettings(hold="12h", hold_minutes=720)
    cfg = eng.build_config({"data_vendors": {}}, s)
    assert cfg["memory_log_path"] == ""
    assert "Re-judge every round" in cfg["output_language"]
    assert cfg["output_language"].startswith("Chinese. ")
    assert cfg["jiaoyi_prompt"] == {"hold": "12h", "hold_minutes": 720}
    # output_language 只跟档位有关：每场会一样，每场会不同的东西走 state
    assert cfg["output_language"] == eng.build_config({"data_vendors": {}}, s)["output_language"]


def test_profile_tables_cover_every_tier() -> None:
    for h in HOLDS:
        assert h in profiles.ANALYST_KLINES and h in profiles.INDICATOR_INTERVAL
        assert profiles.fee_share_pct(h) > 0 and profiles.HORIZON_EN[h] and profiles.HORIZON_ZH[h]
    assert [profiles.fee_share_pct(h) for h in HOLDS] == [70, 50, 35, 25, 17, 14, 9]
    assert [profiles.indicator_interval(h) for h in HOLDS] == [
        "5m", "5m", "15m", "1h", "4h", "4h", "4h",
    ]  # fmt: skip
    assert [profiles.zone_required(h) for h in HOLDS] == [True] * 4 + [False] * 3


def test_prompts_active_file_is_written(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json

    from jiaoyi.core.config import load_risk_config
    from tests.conftest import ROOT

    e = eng.TradingAgentsEngine.__new__(eng.TradingAgentsEngine)
    e._risk = load_risk_config(ROOT / "config" / "risk.example.toml")
    s = eng.TradingAgentsSettings(results_dir=str(tmp_path / "ta"), hold="4h", hold_minutes=240)
    e._write_active_prompts(s)
    doc = json.loads((tmp_path / "prompts_active.json").read_text())
    assert doc["hold"] == "4h" and doc["style"] == "mid" and doc["indicator_interval"] == "1h"
    assert "both effects are weak" in doc["shared"] and "{SYMBOL}" in doc["position_note"]
    assert doc["brief_fields"]["main"][0] == "chg_4h"
    assert "最多 {max_n} 个" in doc["pick"]  # 预览留占位符，不写成「最多 0 个」


# ---------------------------------------------------------------- tickSize
def test_tick_sizes_are_cached_and_retry_after_a_failure() -> None:
    now = [0.0]
    calls: list[int] = []
    boom = [True]

    def fetch() -> dict[str, str]:
        calls.append(1)
        if boom[0]:
            raise RuntimeError("down")
        return {"STRKUSDT": "0.00010000"}

    t = TickSizes(fetch, lambda: now[0])
    assert t.get("STRKUSDT") is None and len(calls) == 1
    assert t.get("STRKUSDT") is None and len(calls) == 1  # 一分钟内不重打
    boom[0], now[0] = False, 61.0
    assert t.get("STRKUSDT") == "0.00010000" and len(calls) == 2
    now[0] += CACHE_S - 1
    assert t.get("STRKUSDT") == "0.00010000" and len(calls) == 2  # 6 小时内走缓存


def test_entry_zone_rules() -> None:
    from jiaoyi.slow.tradingagents_engine import (
        _CardSymbolDraft,
        convert_instructions,
        entry_zone_of,
    )

    base = dict(
        bias="short",
        size_tier_max="M",
        notional_min=0,
        notional_max=1,
        leverage_min=1,
        leverage_max=2,
        stop_atr_min=1,
        stop_atr_max=2,
        take_profit_r_min=1,
        take_profit_r_max=2,
        reason="x",
    )
    ok = _CardSymbolDraft(**base, entry_min=100, entry_max=101)
    assert entry_zone_of(ok, None) is not None
    assert entry_zone_of(_CardSymbolDraft(**base, entry_min=102, entry_max=101), None) is None
    assert entry_zone_of(_CardSymbolDraft(**base), None) is None
    from jiaoyi.core.config import load_risk_config
    from jiaoyi.slow import ta_prompts
    from tests.conftest import ROOT

    risk = load_risk_config(ROOT / "config" / "risk.example.toml")
    text = convert_instructions(risk, ta_prompts.PromptParams.for_hold("2h"))
    assert "必须给 entry_min" in text
    assert "尽量给" in convert_instructions(risk, ta_prompts.PromptParams.for_hold("24h"))


def test_zone_required_turns_card_into_no_trade() -> None:
    from jiaoyi.contracts import SymbolPoolEntry
    from jiaoyi.slow.tradingagents_engine import _CardSymbolDraft, draft_to_card_symbol

    d = _CardSymbolDraft(
        bias="short",
        size_tier_max="M",
        notional_min=0,
        notional_max=1,
        leverage_min=1,
        leverage_max=2,
        stop_atr_min=1,
        stop_atr_max=2,
        take_profit_r_min=1,
        take_profit_r_max=2,
        reason="x",
        allowed_signals=["volume_breakout"],
    )
    entry = SymbolPoolEntry.model_construct(symbol="BTCUSDT", category="major")
    c = draft_to_card_symbol(d, entry, None, zone_required=True)
    assert c.bias.value == "no_trade" and "会议没给进场区间" in c.reason
    assert draft_to_card_symbol(d, entry, None, zone_required=False).bias.value == "short"


def test_data_window_comes_from_one_constant() -> None:
    from jiaoyi.core import profiles

    p = tp.PromptParams.for_hold("2h")
    assert profiles.DATA_WINDOW[0] in tp.perp_context(p) and "{DATA_WINDOW}" not in tp.perp_context(
        p
    )
    assert profiles.DATA_WINDOW[1] in tp.pick_prompt(p, "t", 3)
