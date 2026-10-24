"""军师团四个角色（行情分析师、研究经理、交易员、组合经理）的加密版提示词（纯文本）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jiaoyi.core import profiles
from jiaoyi.slow import ta_prompts as tp
from jiaoyi.slow import ta_roles as r

HOLDS = ("30m", "1h", "2h", "4h", "8h", "12h", "24h")
DATA = Path(__file__).parent / "data" / "ta_roles"
RUNTIME = {
    "{target}",
    "{instrument_context}",
    "{history}",
    "{ticker}",
    "{report_section}",
    "{portfolio_context}",
    "{investment_plan}",
    "{research_plan}",
    "{trader_plan}",
    "{lessons_line}",
    "{past_context}",
}
STOCK_WORDS = (
    "exposure",
    "allocation",
    "trim",
    "earnings",
    "company",
    "3-6 months",
    "% of portfolio",
    "growing the position",
    "maintaining the current position",
)
TEXTS = {
    "market": r.MARKET,
    "rm": r.RM,
    "trader_system": r.TRADER_SYSTEM,
    "trader_user": r.TRADER_USER,
    "pm": r.PM,
    "lessons_line": r.LESSONS_LINE,
}


def _render(hold: str, **kw: str) -> str:
    p = tp.PromptParams.for_hold(hold)
    return "\n".join(f"===== {n} ({hold})\n{r.fill(t, p, **kw)}\n" for n, t in TEXTS.items())


@pytest.mark.parametrize("hold", HOLDS)
def test_seven_tier_snapshots(hold: str) -> None:
    assert _render(hold) == (DATA / f"rendered_{hold}.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("hold", HOLDS)
def test_no_open_slots_and_no_stock_words(hold: str) -> None:
    p = tp.PromptParams.for_hold(hold)
    for name, text in TEXTS.items():
        out = r.fill(text, p)
        assert set(re.findall(r"\{[A-Za-z_]+\}", out)) <= RUNTIME, (hold, name)
        low = out.lower()
        assert not [w for w in STOCK_WORDS if w in low], (hold, name)


@pytest.mark.parametrize("hold", HOLDS)
def test_zone_rule_lookback_and_lean_follow_the_tier(hold: str) -> None:
    p = tp.PromptParams.for_hold(hold)
    rm = r.fill(r.RM, p, "S")
    if profiles.zone_required(hold):  # 30m–4h
        assert "needs an entry zone that price can reach" in rm
    else:  # 8h–24h
        assert "Give an entry zone when there is a sensible one" in rm
    days = {"short": 1, "mid": 2, "long": 10}[p.style]
    assert f"look_back_days={days}" in r.fill(r.MARKET, p)
    assert "size S or smaller" in rm
    assert "size M or smaller" in r.fill(r.PM, p, "M")


def test_lean_default_is_m_and_follows_the_argument() -> None:
    p = tp.PromptParams.for_hold("2h")
    assert "size M or smaller" in r.fill(r.RM, p)
    assert "size L or smaller" in r.fill(r.RM, p, "L")


def test_language_line_per_role() -> None:
    rm = r.language_line("Chinese", "rm")
    assert '"**Recommendation**:"' in rm and "FINAL TRANSACTION PROPOSAL" not in rm
    trader = r.language_line("Chinese", "trader")
    for label in ("**Action**:", "**Entry Zone**:", "**Invalidation**:", "**Take Profit**:"):
        assert label in trader
    assert "**Size Tier**:" in trader and "FINAL TRANSACTION PROPOSAL:" in trader
    pm = r.language_line("Chinese", "pm")
    assert "**Rating**:" in pm and "**Size Tier**:" in pm and "FINAL TRANSACTION" not in pm
    plain = r.language_line("Chinese", None)
    assert "do not add a rating, a recommendation or a FINAL TRANSACTION PROPOSAL line" in plain
    assert "except the labelled lines" not in plain


def test_renders_are_read_by_the_engine_parsers() -> None:
    from jiaoyi.contracts.common import Bias
    from jiaoyi.slow.tradingagents_engine import (
        direction_conflict,
        research_recommendation,
    )

    trader = r.render_trader("Sell", "弱", "101.5–103.0", "104.2", "98.0", "M")
    assert trader.splitlines()[-1] == "FINAL TRANSACTION PROPOSAL: **SELL**"
    assert direction_conflict(Bias.LONG, "Hold", trader) is not None
    pm = r.render_pm("Underweight", "摘要", "论点", "101.5–103.0", "104.2", "98.0", "S")
    assert pm.startswith("**Rating**: Underweight")
    hold = r.render_trader("Hold", "没有优势")
    assert "**Entry Zone**: none" in hold and "**Size Tier**: none" in hold
    # 研究经理正文里先写了推荐，后面引用别的评级不能盖过它
    plan = "**Recommendation**: Sell\n\n**Rationale**: x\n\nexample **Recommendation**: Buy"
    assert research_recommendation(plan) == "sell"
