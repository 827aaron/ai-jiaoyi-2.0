"""只记录的大盘 / 相对判断（CardOutlook）：解析宽松、记 None 带原因、卡一字不变、落盘向后兼容。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from jiaoyi import contracts as c
from jiaoyi.contracts import BattleCard, Bias, CardOutlook, CardSymbol, MarketView
from jiaoyi.core.config import load_risk_config
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.slow import card_outlook, ta_prompts
from jiaoyi.slow import tradingagents_engine as te
from jiaoyi.slow.card_outlook import build_outlook, card_from_output, with_board
from jiaoyi.slow.commander import DEFAULT_STOP_SCALE, BattleCardCommander
from jiaoyi.slow.levels import LevelContext
from jiaoyi.slow.review import CardJournal, JournalRow
from tests.conftest import ROOT
from tests.test_slow_commander import FakeEngine, card_symbol, entry, make, pool

RISK = load_risk_config(ROOT / "config" / "risk.example.toml")
NOW = datetime(2026, 10, 28, 6, 0, tzinfo=UTC)
OUTLOOK = {
    "market_dir": "down",
    "market_dir_evidence": "池内近 4 小时 10 涨 30 跌，中位 -0.8%",
    "rel_dir": "outperform",
    "rel_dir_evidence": "本币 4h +1.2%，高于池中位 -0.8%",
}
DRAFT: dict[str, Any] = dict(
    bias="short", size_tier_max="M", notional_min=100, notional_max=400, leverage_min=1,
    leverage_max=3, stop_atr_min=1, stop_atr_max=2, take_profit_r_min=1.5, take_profit_r_max=2,
    entry_min=99.0, entry_max=101.0, key_levels=[], allowed_signals=["volume_breakout"],
    reason="反抽做空",
)  # fmt: skip


# ---------------------------------------------------------------- 结构化输出
def test_schema_requires_the_four_fields_as_enums_after_reason() -> None:
    schema = te._CardSymbolDraft.model_json_schema()
    props = list(schema["properties"])
    assert props[-5:] == ["reason", "market_dir", "market_dir_evidence", "rel_dir",
                          "rel_dir_evidence"]  # fmt: skip
    assert {"market_dir", "market_dir_evidence", "rel_dir", "rel_dir_evidence"} <= set(
        schema["required"]
    )
    assert schema["properties"]["market_dir"]["enum"] == ["up", "down", "flat"]
    assert schema["properties"]["rel_dir"]["enum"] == ["outperform", "underperform", "inline"]
    assert schema["properties"]["rel_dir_evidence"]["type"] == "string"


@pytest.mark.parametrize(
    "extra",
    [
        {},  # 全没写
        {"market_dir": "bullish", "rel_dir": "stronger"},  # 不认识的词
        {"market_dir": 3, "rel_dir": ["up"], "market_dir_evidence": {"a": 1}},  # 类型都不对
        {"market_dir": None, "rel_dir": "", "rel_dir_evidence": "   "},
    ],
)
def test_bad_or_missing_fields_never_fail_parsing(extra: dict[str, Any]) -> None:
    d = te._CardSymbolDraft.model_validate({**DRAFT, **extra})
    o = build_outlook(
        bias=Bias.SHORT, market_dir=d.market_dir, market_dir_evidence=d.market_dir_evidence,
        rel_dir=d.rel_dir, rel_dir_evidence=d.rel_dir_evidence, rm=None, pm=None, trader=None,
    )  # fmt: skip
    assert o.market_dir is None and o.rel_dir is None
    assert o.missing["market_dir"] in ("omitted", f"invalid: {str(d.market_dir)[:40]!r}")
    assert "rel_dir" in o.missing


def test_values_are_normalised_and_invalid_ones_keep_the_raw_text() -> None:
    o = build_outlook(
        bias=Bias.LONG, market_dir=" UP ", market_dir_evidence=" BTC 4h +1% ",
        rel_dir="Underperform", rel_dir_evidence=None, rm="Buy", pm="REVIEW", trader="hold",
    )  # fmt: skip
    assert (o.market_dir, o.market_dir_evidence, o.rel_dir) == ("up", "BTC 4h +1%", "underperform")
    assert (o.rm_rating, o.pm_rating, o.trader_action) == ("buy", None, "hold")
    assert o.missing == {"rel_dir_evidence": "omitted", "pm_rating": "invalid: 'REVIEW'"}
    assert o.no_trade_lean is None and "no_trade_lean" not in o.missing  # 方向卡不记倾向
    bad = build_outlook(
        bias=Bias.LONG, market_dir="sideways", market_dir_evidence="x" * 999, rel_dir="inline",
        rel_dir_evidence="y", rm=None, pm=None, trader=None,
    )  # fmt: skip
    assert bad.missing["market_dir"] == "invalid: 'sideways'"
    assert len(bad.market_dir_evidence or "") == 300
    assert bad.missing["rm_rating"] == "unparsed"


@pytest.mark.parametrize(
    ("rm", "pm", "trader", "lean", "why"),
    [
        ("overweight", "hold", "buy", "long", None),
        ("underweight", "hold", "hold", "short", None),
        ("sell", "sell", "sell", "short", None),  # 评级都是卖，卡因为没区间成了观望
        ("hold", "hold", "hold", None, "no_lean"),
        (None, None, None, None, "no_lean"),
        ("overweight", "underweight", None, None, "conflict"),
    ],
)
def test_no_trade_lean_comes_only_from_existing_ratings(
    rm: str | None, pm: str | None, trader: str | None, lean: str | None, why: str | None
) -> None:
    o = build_outlook(
        bias=Bias.NO_TRADE, market_dir="flat", market_dir_evidence="e", rel_dir="inline",
        rel_dir_evidence="e", rm=rm, pm=pm, trader=trader,
    )  # fmt: skip
    assert o.no_trade_lean == lean
    assert o.missing.get("no_trade_lean") == why


def test_prompt_addition_is_appended_and_neutral() -> None:
    p = ta_prompts.PromptParams.for_hold("2h")
    text = te.convert_instructions(RISK, p)
    note = ta_prompts.fill(ta_prompts.OUTLOOK_NOTE, p)
    assert text.endswith(note)
    before = text.removesuffix(note)
    assert "market_dir" not in before and "rel_dir" not in before  # 原有规则一字没动
    words = set(ta_prompts.OUTLOOK_NOTE.lower().replace(",", " ").replace(".", " ").split())
    assert not words & {"long", "short", "bull", "bullish", "bear", "bearish", "buy", "sell"}
    assert {"up", "down", "flat", "outperform", "underperform", "inline"} <= words


@pytest.mark.parametrize("hold", ["30m", "2h", "4h", "24h"])
def test_outlook_window_is_a_fixed_4h_separate_from_the_hold(hold: str) -> None:
    p = ta_prompts.PromptParams.for_hold(hold)
    note = ta_prompts.fill(ta_prompts.OUTLOOK_NOTE, p)
    assert "fixed window of the next 4 hours" in note
    assert f"independent of the {p.horizon} holding window used above" in note
    assert "{" not in note
    assert card_outlook.HORIZON_MIN == 240 == CardOutlook().horizon_min
    o = build_outlook(
        bias=Bias.LONG, market_dir="up", market_dir_evidence="e", rel_dir="inline",
        rel_dir_evidence="e", rm=None, pm=None, trader=None,
    )  # fmt: skip
    assert o.horizon_min == 240
    props = te._CardSymbolDraft.model_json_schema()["properties"]
    for name in ("market_dir", "rel_dir"):
        assert props[name]["description"].startswith("Fixed next-4-hour window, independent")


# ---------------------------------------------------------------- 引擎：卡一字不变
class _Llm:
    def __init__(self, draft: te._CardSymbolDraft) -> None:
        self.draft, self.prompts = draft, []  # type: list[str]

    def with_structured_output(self, schema: Any) -> _Llm:
        assert schema is te._CardSymbolDraft
        return self

    def bind(self, **kw: Any) -> _Llm:
        return self

    def invoke(self, prompt: str, config: Any = None) -> te._CardSymbolDraft:
        self.prompts.append(prompt)
        return self.draft


class _Graph:
    def __init__(self, llm: _Llm, rating: str, rm: str, trader: str) -> None:
        self.quick_thinking_llm = self.deep_thinking_llm = llm
        talk = "论点" * 40
        self.state = {
            "final_trade_decision": f"**Rating**: {rating}\n\n**Executive Summary**: 反抽做空",
            "investment_plan": f"**Recommendation**: {rm}\n\n{talk}",
            "trader_investment_plan": f"FINAL TRANSACTION PROPOSAL: **{trader}**",
            "market_report": talk, "sentiment_report": talk, "news_report": talk,
            "investment_debate_state": {"bull_history": talk, "bear_history": talk},
            "risk_debate_state": {
                "aggressive_history": talk, "conservative_history": talk, "neutral_history": talk,
            },
        }  # fmt: skip
        self.rating = rating

    def propagate(self, ticker: str, day: str, asset_type: str) -> tuple[dict[str, Any], str]:
        return self.state, self.rating


def _engine(monkeypatch: pytest.MonkeyPatch, graph: _Graph) -> te.TradingAgentsEngine:
    eng = te.TradingAgentsEngine.__new__(te.TradingAgentsEngine)
    eng._s = te.TradingAgentsSettings()
    eng._risk, eng._stop, eng._brief = RISK, DEFAULT_STOP_SCALE, None
    eng._today = lambda: NOW

    class _Ticks:
        def get(self, symbol: str) -> None:
            return None

    eng._ticks = _Ticks()  # type: ignore[assignment]
    eng._graph = lambda *a, **k: graph  # type: ignore[method-assign]
    monkeypatch.setattr(te, "level_context", lambda *a, **k: LevelContext())
    monkeypatch.setattr(te, "atr_context", lambda *a, **k: "ATR")
    return eng


def _run(
    monkeypatch: pytest.MonkeyPatch, draft: dict[str, Any], rating: str = "Underweight",
    rm: str = "Sell", trader: str = "SELL",
) -> CardSymbol:  # fmt: skip
    graph = _Graph(_Llm(te._CardSymbolDraft.model_validate(draft)), rating, rm, trader)
    eng = _engine(monkeypatch, graph)
    meter = te.CostMeter({"flash": (0.3, 1.2), "pro": (0.5, 2.0)})
    res = eng._analyze_run(entry("AAUSDT"), "区间震荡", object(), meter)
    return res.card_symbol


@pytest.mark.parametrize(
    "extra",
    [
        OUTLOOK,
        {"market_dir": "bullish", "rel_dir": 7, "market_dir_evidence": None},
        {"market_dir": "up"},
    ],
)
@pytest.mark.parametrize(
    ("rating", "rm", "trader"),
    [("Underweight", "Sell", "SELL"), ("Sell", "Sell", "SELL"), ("Hold", "Underweight", "HOLD")],
)
def test_card_is_identical_with_or_without_the_new_fields(
    monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any], rating: str, rm: str, trader: str
) -> None:
    old = _run(monkeypatch, DRAFT, rating, rm, trader)
    new = _run(monkeypatch, {**DRAFT, **extra}, rating, rm, trader)
    assert new.model_copy(update={"outlook": None}) == old.model_copy(update={"outlook": None})
    # 和改动前的流水线（转卡 → 评级兜底）算出来的一样
    d = te._CardSymbolDraft.model_validate(DRAFT)
    expected = te.rating_guard(
        te.draft_to_card_symbol(d, entry("AAUSDT"), LevelContext(), True),
        rm.lower(), rating.lower(), zone_ok=True,
    )  # fmt: skip
    assert old.model_copy(update={"outlook": None}) == expected
    assert new.outlook is not None and new.research_token() == old.research_token()


def test_engine_records_outlook_ratings_and_lean(monkeypatch: pytest.MonkeyPatch) -> None:
    cs = _run(monkeypatch, {**DRAFT, **OUTLOOK})
    assert cs.bias is Bias.SHORT and cs.outlook is not None
    o = cs.outlook
    assert (o.market_dir, o.rel_dir) == ("down", "outperform")
    assert o.market_dir_evidence == OUTLOOK["market_dir_evidence"]
    assert (o.rm_rating, o.pm_rating, o.trader_action) == ("sell", "underweight", "sell")
    assert o.missing == {} and o.no_trade_lean is None
    hold = _run(monkeypatch, {**DRAFT, **OUTLOOK}, "Hold", "Underweight", "HOLD")
    assert hold.bias is Bias.NO_TRADE and hold.outlook is not None
    assert (hold.outlook.no_trade_lean, hold.outlook.pm_rating) == ("short", "hold")


def test_outlook_failure_still_produces_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(**kw: Any) -> CardOutlook:
        raise RuntimeError("坏了")

    plain = _run(monkeypatch, DRAFT)
    monkeypatch.setattr(te.card_outlook, "build_outlook", boom)
    cs = _run(monkeypatch, {**DRAFT, **OUTLOOK})
    assert cs.outlook is None and cs == plain.model_copy(update={"outlook": None})


# ---------------------------------------------------------------- 总盘会方向
def _market(direction: str) -> MarketView:
    return MarketView(
        market_view_id="mv-1", version=1, observed_at=NOW, regime="池内 10 涨 30 跌，中位 -0.8%",
        regime_direction=direction,  # type: ignore[arg-type]
    )  # fmt: skip


@pytest.mark.parametrize(("zh", "en"), [("看涨", "up"), ("看跌", "down"), ("中性", "flat")])
def test_stamp_adds_board_direction_in_the_same_vocabulary(zh: str, en: str) -> None:
    cs = card_symbol("AAUSDT", outlook=CardOutlook(market_dir="down"))
    out = BattleCardCommander._stamp(cs, NOW, NOW + timedelta(hours=1), _market(zh)).outlook
    assert out is not None
    assert (out.board_market_dir, out.board_regime_direction) == (en, zh)
    assert out.board_market_view_id == "mv-1" and out.market_dir == "down"
    none = BattleCardCommander._stamp(cs, NOW, NOW + timedelta(hours=1), None).outlook
    assert none is not None and none.board_market_dir is None
    assert none.missing == {"board_market_dir": "no_market_view"}
    assert with_board(card_symbol("AAUSDT"), _market(zh)).outlook is None  # 没有 outlook 不造


def test_with_board_errors_leave_the_card_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    cs = card_symbol("AAUSDT", outlook=CardOutlook(market_dir="down"))
    monkeypatch.setattr(card_outlook, "BOARD_DIR", {})  # 映射表坏了
    assert with_board(cs, _market("看涨")) == cs
    stamped = BattleCardCommander._stamp(cs, NOW, NOW + timedelta(hours=1), _market("看涨"))
    assert stamped.outlook == cs.outlook and stamped.issued_at == NOW


def test_research_token_ignores_outlook() -> None:
    cs = card_symbol("AAUSDT")
    with_o = cs.model_copy(update={"outlook": CardOutlook(market_dir="up")})
    assert with_o.research_token() == cs.research_token()
    legacy = json.dumps(
        cs.model_dump(mode="json", exclude={"outlook"}), ensure_ascii=False, separators=(",", ":")
    )
    import hashlib

    assert cs.research_token() == hashlib.sha256(legacy.encode()).hexdigest()[:24]


# ---------------------------------------------------------------- 落盘
class _OutlookEngine(FakeEngine):
    async def analyze(self, e: Any, regime: str, pick_reason: str = "") -> Any:
        res = await super().analyze(e, regime, pick_reason)
        o = build_outlook(
            bias=Bias.LONG, market_dir="up", market_dir_evidence="池中位 +0.7%", rel_dir="inline",
            rel_dir_evidence="本币 +0.6%", rm="Buy", pm="Overweight", trader="BUY",
        )  # fmt: skip
        return type(res)(res.card_symbol.model_copy(update={"outlook": o}), res.cost_usd)


async def test_commander_persists_outlook_to_journal_and_meeting_record(
    tmp_path: Path, now: datetime
) -> None:
    j = CardJournal(tmp_path)
    saved_path = tmp_path / "battle_card.json"
    cmd, log, _ = make(tmp_path, _OutlookEngine(["AAUSDT"]), now, journal=j, card_path=saved_path)
    card = await cmd.refresh(pool(now, entry("AAUSDT")), "scheduled")
    [row] = [json.loads(x) for x in j.path.read_text().splitlines()]
    o = row["outlook"]
    assert (o["market_dir"], o["rel_dir"], o["board_market_dir"]) == ("up", "inline", "flat")
    assert (o["rm_rating"], o["pm_rating"], o["trader_action"]) == ("buy", "overweight", "buy")
    assert o["board_market_view_id"] == card.symbols[0].source_market_view_id
    assert o["horizon_min"] == 240
    [rec] = [r for r in log.read_day(now.date().isoformat()) if r.symbol == "AAUSDT"]
    assert rec.output is not None and rec.output["outlook"] == o  # 开会记录里同一份
    # 存档不带 outlook（回退旧版本仍能读），重启后照常恢复
    saved = json.loads(saved_path.read_text())
    assert "outlook" not in saved["card"]["symbols"][0]
    assert BattleCard.model_validate(saved["card"]).symbols[0].outlook is None
    again, _, _ = make(tmp_path, FakeEngine([]), now, card_path=saved_path)
    restored = again.current(now)
    assert restored is not None and restored.symbols[0].outlook is None
    assert restored.symbols[0].research_token() == card.symbols[0].research_token()


_LEGACY_KEYS = (
    "card_id", "version", "created_at", "expires_at", "regime", "refresh_reason", "symbol",
    "category", "bias",
)  # fmt: skip


class _OldRow(BaseModel):
    """改动前的 ``JournalRow``（旧代码读新行时用的就是它）。"""

    model_config = ConfigDict(frozen=True)

    card_id: str
    version: int
    created_at: datetime
    expires_at: datetime
    regime: str
    refresh_reason: str
    symbol: str
    category: str
    bias: str


def test_journal_rows_keep_old_keys_byte_identical_and_append_outlook(tmp_path: Path) -> None:
    o = CardOutlook(market_dir="up", missing={"rel_dir": "omitted"})
    card = BattleCard(
        card_id="card-1", version=3, created_at=NOW, expires_at=NOW + timedelta(hours=2),
        regime="震荡", regime_direction="中性", refresh_reason="scheduled",
        symbols=[card_symbol("AAUSDT", outlook=o), card_symbol("BBUSDT")],
    )  # fmt: skip
    j = CardJournal(tmp_path)
    j.record(card, ["AAUSDT", "BBUSDT"])
    lines = j.path.read_text(encoding="utf-8").splitlines()
    for line, s in zip(lines, card.symbols, strict=True):
        old = _OldRow(
            card_id=card.card_id, version=card.version, created_at=card.created_at,
            expires_at=card.expires_at, regime=card.regime, refresh_reason=card.refresh_reason,
            symbol=s.symbol, category=s.category.value, bias=s.bias.value,
        ).model_dump_json()  # fmt: skip
        assert line.startswith(old[:-1] + ',"outlook":')  # 旧格式原样，末尾多一个键
        assert list(json.loads(line)) == [*_LEGACY_KEYS, "outlook", "meeting_setup"]
    rows = [json.loads(x) for x in lines]
    assert rows[0]["outlook"] == o.model_dump(mode="json") and rows[1]["outlook"] is None
    j.record(card, [])  # 没有新开会的币：什么都不写
    assert len(j.path.read_text().splitlines()) == 2


def test_every_journal_reader_accepts_old_and_new_rows(tmp_path: Path) -> None:
    from jiaoyi.gate.replay import default_symbols
    from jiaoyi.panel.files import PanelFiles
    from jiaoyi.panel.pages import _journal_tail

    card = BattleCard(
        card_id="card-1", version=1, created_at=NOW, expires_at=NOW + timedelta(hours=2),
        regime="震荡", symbols=[card_symbol("AAUSDT", outlook=CardOutlook(rel_dir="inline"))],
    )  # fmt: skip
    j = CardJournal(tmp_path)
    j.record(card, ["AAUSDT"])  # 新格式的行
    new_line = j.path.read_text(encoding="utf-8")
    legacy = {k: v for k, v in json.loads(new_line).items() if k != "outlook"}
    with j.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(legacy, ensure_ascii=False) + "\n")  # 改动前写的旧行
    new, old = j.read()
    assert new.outlook is not None and new.outlook["rel_dir"] == "inline"
    assert old.outlook is None and old.model_copy(update={"outlook": None}) == old
    assert new.model_copy(update={"outlook": None}) == old
    assert _OldRow.model_validate_json(new_line).symbol == "AAUSDT"  # 旧代码读新行不崩
    assert all(isinstance(r, JournalRow) for r in _journal_tail(PanelFiles(tmp_path, "UTC")))
    assert len(_journal_tail(PanelFiles(tmp_path, "UTC"))) == 2
    assert default_symbols(tmp_path)[0][0] == "AAUSDT"


# ---------------------------------------------------------------- 面板（只读显示）
BAD_OUTLOOK = {"market_dir": "sideways", "surprise": 1}  # 只记录的部分坏了


def _symbol_records(log: JsonlDecisionLog, now: datetime) -> None:
    with_o = card_symbol(
        "AAUSDT",
        outlook=CardOutlook(market_dir="down", rel_dir="outperform", board_market_dir="flat"),
    )
    legacy = card_symbol("BBUSDT").model_dump(mode="json", exclude={"outlook"})  # 旧记录没有这个键
    broken = {**card_symbol("CCUSDT").model_dump(mode="json"), "outlook": BAD_OUTLOOK}
    for sym, out in (("AAUSDT", with_o.model_dump(mode="json")), ("BBUSDT", legacy),
                     ("CCUSDT", broken)):  # fmt: skip
        log.write(
            c.DecisionRecord(
                record_id=f"r-{sym}", ts=now - timedelta(minutes=5), role=c.Role.COMMANDER,
                outcome=c.Outcome.ACTION, summary=f"{sym} 开会结论", symbol=sym,
                output={**out, "usage": {"input_tokens": 1}},
            )
        )  # fmt: skip


def test_panel_card_rows_show_outlook_and_tolerate_legacy_and_bad_records(
    tmp_path: Path,
) -> None:
    from jiaoyi.panel.files import PanelFiles
    from jiaoyi.panel.overview import build_overview
    from tests.test_panel import HB, _cfg

    log = JsonlDecisionLog(tmp_path)
    now = datetime.now(UTC)
    _symbol_records(log, now)
    log.write(
        c.DecisionRecord(
            record_id="card", ts=now - timedelta(minutes=4), role=c.Role.COMMANDER,
            outcome=c.Outcome.ACTION, summary="出作战卡 v1", card_id="card-1",
            output={"regime": "震荡", "version": 1, "symbols": ["AAUSDT", "BBUSDT", "CCUSDT"],
                    "expires_at": (now + timedelta(hours=1)).isoformat()},
        )
    )  # fmt: skip
    ov = build_overview(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, _cfg(tmp_path), now)
    assert ov.card is not None
    rows = {s.symbol: s for s in ov.card.symbols}
    assert (rows["AAUSDT"].market_dir, rows["AAUSDT"].rel_dir) == ("down", "outperform")
    assert rows["AAUSDT"].board_market_dir == "flat"
    assert rows["BBUSDT"].bias == "long" and rows["BBUSDT"].market_dir is None
    assert rows["CCUSDT"].bias == "long" and rows["CCUSDT"].market_dir is None  # 卡照样显示


def test_card_history_keeps_cards_whose_outlook_is_bad(tmp_path: Path) -> None:
    from jiaoyi.panel.card_history import _conclusion

    log = JsonlDecisionLog(tmp_path)
    now = datetime.now(UTC)
    _symbol_records(log, now)
    recs = log.read_day(now.date().isoformat())
    for sym, want in (("AAUSDT", "down"), ("BBUSDT", None), ("CCUSDT", None)):
        row = JournalRow(
            card_id="card-1", version=1, created_at=now, expires_at=now + timedelta(hours=1),
            regime="震荡", refresh_reason="scheduled", symbol=sym, category="alt", bias="long",
        )  # fmt: skip
        cs = _conclusion(recs, row)
        assert cs is not None and cs.symbol == sym
        assert (cs.outlook.market_dir if cs.outlook else None) == want


def test_card_from_output_validates_the_card_first() -> None:
    raw = card_symbol("AAUSDT").model_dump(mode="json")
    assert card_from_output({**raw, "outlook": BAD_OUTLOOK}).outlook is None
    assert card_from_output({**raw, "outlook": "nonsense"}).outlook is None
    good = card_from_output({**raw, "outlook": {"rel_dir": "inline"}})
    assert good.outlook is not None and good.outlook.rel_dir == "inline"
    with pytest.raises(ValueError):
        card_from_output({**raw, "bias": "sideways", "outlook": {"rel_dir": "inline"}})
