"""会议输入的新鲜度开关（slow.toml [meeting]）：默认一字不差、开了才换成新鲜 / 写明时间的输入。

起因 stale-input-audit-2026-10-28：PUMPUSDT 临时会开会前失效价就已被穿过，分析师把 21:15 的
15 分钟收盘当现价；总盘会把上一个已收盘整点叫成「近 1 小时」。
"""

from __future__ import annotations

import asyncio
import json
import re
import tomllib
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.contracts import Bias, MarketView, Range, SymbolPoolEntry
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.slow import market_table, ta_prompts, ta_roles
from jiaoyi.slow import tradingagents_engine as te
from jiaoyi.slow.commander import Analysis, BattleCardCommander, Pick, PickResult
from jiaoyi.slow.levels import LevelContext
from jiaoyi.slow.market_audit import CURRENT_AUDIT, MarketAudit
from jiaoyi.slow.settings import MeetingConfig, SlowConfig, load_slow_config
from tests.test_bias_fix import _one_sided
from tests.test_card_outlook import _engine, _Graph, _Llm
from tests.test_slow_commander import RISK, card_symbol, entry, pool

ROOT = Path(__file__).resolve().parents[1]
FRESH_KEYS = (
    "snapshot_interval",
    "snapshot_label",
    "live_price_line",
    "stale_on_arrival",
    "stale_on_arrival_action",
    "input_time_labels",
    "market_table_rolling",
    "latest_market_regime",
    "event_brief_refresh",
    "regime_time_label",
    "regime_max_age_min",
    "pool_scope_label",
)
ALL_ON = {
    "snapshot_interval": "15m",
    "snapshot_label": "neutral",
    "live_price_line": True,
    "stale_on_arrival": True,
    "stale_on_arrival_action": "no_trade",
    "input_time_labels": "explicit",
    "market_table_rolling": True,
    "latest_market_regime": True,
    "event_brief_refresh": True,
}


def _ms(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def _settings(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    """按场设置（tradingagents 的 run_config）在 CI 里没装：直接替身 meeting_setting。"""
    monkeypatch.setattr(te.ta_nodes, "meeting_setting", lambda k, d: values.get(k, d))


# ---------------------------------------------------------------- 设置
def test_defaults_are_current_behaviour_and_example_lists_every_key() -> None:
    m = MeetingConfig()
    assert (m.snapshot_interval, m.snapshot_label, m.input_time_labels) == (
        None,
        "legacy",
        "legacy",
    )
    assert not any(
        (
            m.live_price_line,
            m.stale_on_arrival,
            m.market_table_rolling,
            m.latest_market_regime,
            m.event_brief_refresh,
        )
    )
    s = SlowConfig().engine_settings()
    assert te.fresh_overrides(s) == {}
    assert set(te.build_config({}, s)[te.ta_nodes.PROMPT_KEY]) == {
        "hold",
        "hold_minutes",
        "lean",
        "lang",
    }
    kw = SlowConfig().commander_kwargs()
    assert kw["latest_market_regime"] is False and kw["event_brief_refresh"] is False
    example = (ROOT / "config" / "slow.example.toml").read_text(encoding="utf-8")
    for key in FRESH_KEYS:
        assert re.search(rf"^# {key} = ", example, re.M), key


def test_all_switches_load_and_reach_engine_and_commander(tmp_path: Path) -> None:
    lines = [f"{k} = {v!r}".replace("True", "true").replace("'", '"') for k, v in ALL_ON.items()]
    p = tmp_path / "slow.toml"
    p.write_text("[meeting]\n" + "\n".join(lines) + "\n", encoding="utf-8")
    cfg = load_slow_config(p)
    s = cfg.engine_settings()
    assert (s.snapshot_interval, s.snapshot_label, s.input_time_labels) == (
        "15m",
        "neutral",
        "explicit",
    )
    assert s.live_price_line and s.stale_on_arrival and s.market_table_rolling
    assert s.stale_on_arrival_action == "no_trade"
    kw = cfg.commander_kwargs()
    assert kw["latest_market_regime"] and kw["event_brief_refresh"]
    BattleCardCommander(object(), RISK, JsonlDecisionLog(tmp_path), **kw)  # type: ignore[arg-type]
    # 快照周期覆盖档位：4h 档默认 1h，写了 15m 就是 15m，提示词 {IND} 跟着换
    four = replace(s, hold="4h", hold_minutes=240)
    assert te.prompt_params(replace(four, snapshot_interval=None)).indicator == "1h"
    assert te.prompt_params(four).indicator == "15m"
    assert te.build_config({}, four)[te.ta_nodes.PROMPT_KEY]["indicator"] == "15m"


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("snapshot_interval", '"2m"'),
        ("snapshot_interval", '"1d"'),
        ("snapshot_label", '"truth"'),
        ("input_time_labels", '"loud"'),
        ("live_price_line", '"maybe"'),
    ],
)
def test_bad_values_are_rejected(tmp_path: Path, key: str, bad: str) -> None:
    from jiaoyi.core.config import ConfigError

    p = tmp_path / "slow.toml"
    p.write_text(f"[meeting]\n{key} = {bad}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=key):
        load_slow_config(p)


def test_analyst_klines_description_matches_profiles() -> None:
    from jiaoyi.core import profiles

    text = MeetingConfig.model_fields["analyst_klines"].description or ""
    for hold in ("30m", "2h", "4h", "8h"):
        want = " + ".join(s.replace(" x ", "×") for s in profiles.analyst_klines(hold))
        assert want in text, hold


# ---------------------------------------------------------------- b：快照措辞
def _snapshot(monkeypatch: pytest.MonkeyPatch, **values: str) -> str:
    _settings(monkeypatch, **values)
    day = datetime(2026, 10, 20, tzinfo=UTC)  # as_of 对过去的日子取 0 点
    hour = 3_600_000
    opens = [_ms(day) - (31 - i) * hour for i in range(30)]  # 最后一根 22:00–23:00，距 0 点 60 分钟
    rows = [[t, "1", "2", "0.5", "1.5", "10", t + hour - 1, "15"] for t in opens]
    monkeypatch.setattr(te, "_klines", lambda *a, **k: rows)
    monkeypatch.setattr(te.TICKS, "get", lambda s: None)
    return te.build_binance_snapshot("PUMP", "2026-10-20")


def test_snapshot_says_how_old_the_last_closed_bar_is_only_when_neutral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _snapshot(monkeypatch)
    assert "source of truth" in legacy and "min before" not in legacy
    neutral = _snapshot(monkeypatch, snapshot_label="neutral")
    assert "source of truth" not in neutral
    assert "That bar closed at 23:00 UTC, 60 min before the requested analysis time" in neutral
    assert "closed at 23:00 UTC, 60 min before now" in neutral
    # 表格一字不差：只换了说法
    assert (
        legacy.split("### Latest")[1].split("### Recent")[0]
        == neutral.split("### Latest")[1].split("### Recent")[0]
    )


def test_market_analyst_wording_drops_source_of_truth() -> None:
    p = ta_prompts.PromptParams.for_hold("4h")
    for mirror in (False, True):
        text = ta_roles.fill(ta_roles.MARKET, p, mirror=mirror)
        assert text.count(ta_roles.SNAPSHOT_TRUTH) == 1
    neutral = ta_prompts.fill(ta_roles.SNAPSHOT_NEUTRAL, p)
    assert "source of truth" not in neutral and "last closed 1h bar" in neutral
    assert "{" not in neutral


# ---------------------------------------------------------------- g：标签和滚动涨跌家数
def test_derivatives_explicit_labels_say_when_funding_settled_and_add_live_estimate() -> None:
    settle = datetime(2026, 10, 27, 16, tzinfo=UTC)
    funding = [
        {"fundingRate": "0.0001", "fundingTime": _ms(settle - timedelta(hours=8))},
        {"fundingRate": "0.0002", "fundingTime": _ms(settle)},
    ]
    oi = [
        {"sumOpenInterestValue": "100", "timestamp": _ms(settle + timedelta(hours=4))},
        {"sumOpenInterestValue": "110", "timestamp": _ms(settle + timedelta(hours=5))},
    ]
    premium = {
        "lastFundingRate": "-0.00005",
        "nextFundingTime": _ms(settle + timedelta(hours=8)),
        "time": _ms(settle + timedelta(hours=5, minutes=41)),
    }
    legacy = te.format_derivatives(funding, oi)
    assert te.format_derivatives(funding, oi, "legacy", "legacy", premium) == legacy
    assert "Latest funding rate +0.0200%" in legacy
    text = te.format_derivatives(funding, oi, "legacy", "explicit", premium)
    assert "Last settled funding rate (settled at 16:00 UTC) +0.0200%" in text
    assert "Latest funding rate" not in text
    assert (
        "Live funding estimate -0.0050% (Binance premiumIndex read at 21:41 UTC, for the "
        "settlement at 00:00 UTC" in text
    )
    assert "change over 1h (hourly snapshots 20:00 -> 21:00 UTC) +10.00%" in text
    assert "Live funding" not in te.format_derivatives(funding, oi, "legacy", "explicit", None)


def test_table_labels_name_the_completed_hour_only_when_explicit() -> None:
    e = entry("AAUSDT").model_copy(update={"metrics": {"taker_buy_1h": 55.0, "oi_chg_1h": 1.5}})
    meta = {
        "completed_hour_start": "2026-10-27T09:00:00+00:00",
        "completed_hour_end": "2026-10-27T09:59:59.999000+00:00",
        "oi_window_start": "2026-10-27T09:00:00+00:00",
        "oi_window_end": "2026-10-27T10:00:00+00:00",
    }
    legacy = market_table.format_row(e, "2h")
    assert "takerBuy1h 55%" in legacy and "OI1h +1.5%" in legacy
    assert market_table.format_row(e, "2h", "", "legacy", meta) == legacy
    new = market_table.format_row(e, "2h", "", "explicit", meta)
    assert "takerBuy_closedHour[09:00-10:00Z] 55%" in new
    assert "OI_closedHour[09:00-10:00Z] +1.5%" in new
    assert "takerBuy_closedHour[time unknown]" in market_table.format_row(e, "2h", "", "explicit")


def test_rolling_breadth_at_1054_covers_0954_to_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归：10:54 开会，滚动涨跌家数的窗口是 09:54–10:54，不是上一个已收盘整点 09:00–10:00。"""
    now = datetime(2026, 10, 27, 10, 54, 30, tzinfo=UTC)
    start = datetime(2026, 10, 27, 9, 54, tzinfo=UTC)
    moves = {"AAUSDT": 1.02, "BBUSDT": 1.01, "CCUSDT": 0.99}
    asked: list[dict[str, Any]] = []

    class _Resp:
        def __init__(self, rows: list[list[Any]]) -> None:
            self.rows = rows

        def raise_for_status(self) -> None:
            return None

        def json(self) -> list[list[Any]]:
            return self.rows

    def get(url: str, params: dict[str, Any]) -> _Resp:
        asked.append(params)
        sym = params["symbol"]
        if sym == "DDUSDT":  # 新币：起点那一分钟不在
            return _Resp([[params["startTime"] + 60_000, "1", "1", "1", "1"]])
        t0 = params["startTime"]
        return _Resp(
            [[t0, "100", "100", "100", "100"]]
            + [[t0 + i * 60_000, "100", "100", "100", str(100 * moves[sym])] for i in range(1, 61)]
        )

    monkeypatch.setattr(te.binance_gate, "get", get)
    eng = te.TradingAgentsEngine.__new__(te.TradingAgentsEngine)
    eng._today = lambda: now
    line = eng._rolling_breadth(["AAUSDT", "BBUSDT", "CCUSDT", "DDUSDT"])
    assert {p["startTime"] for p in asked} == {_ms(start)}
    assert {(p["interval"], p["limit"]) for p in asked} == {("1m", 61)}
    assert "rolling_60m (09:54-10:54 UTC" in line
    assert "up 2 / down 1 / flat 0 / missing 1, median +1.0%" in line


def test_pick_prompt_gets_rolling_line_and_hour_labels_only_when_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 10, 27, 10, 54, tzinfo=UTC)
    e = entry("AAUSDT").model_copy(update={"metrics": {"taker_buy_1h": 55.0, "chg_1h": 1.0}})
    prompts: list[str] = []

    class _PickLlm:
        def with_structured_output(self, schema: Any) -> _PickLlm:
            return self

        def bind(self, **kw: Any) -> _PickLlm:
            return self

        def invoke(self, prompt: str, config: Any = None) -> Any:
            prompts.append(prompt)
            return te._PickDraft(
                regime="震荡 1 2",
                regime_direction="中性",
                decisions=[{"symbol": "AAUSDT", "pick": False, "reason": "涨 1% 量 2 倍"}],
            )

    class _G:
        quick_thinking_llm = deep_thinking_llm = _PickLlm()

    def run(**kw: Any) -> str:
        eng = te.TradingAgentsEngine.__new__(te.TradingAgentsEngine)
        eng._s = te.TradingAgentsSettings(**kw)
        eng._today = lambda: now
        eng._news = None
        eng._graph = lambda *a, **k: _G()  # type: ignore[method-assign]
        eng._rolling_breadth = lambda syms: f"ROLLING {list(syms)}"  # type: ignore[method-assign]
        audit = MarketAudit(
            None,
            {
                "pool_source_metadata": {
                    "AAUSDT": {
                        "completed_hour_start": "2026-10-27T09:00:00+00:00",
                        "completed_hour_end": "2026-10-27T09:59:59.999000+00:00",
                    }
                }
            },
        )
        token = CURRENT_AUDIT.set(audit)
        try:
            eng._pick_run([e], 1, None, object(), te.CostMeter({}), [e], "", [])
        finally:
            CURRENT_AUDIT.reset(token)
        return prompts[-1]

    legacy = run()
    assert "ROLLING" not in legacy and "takerBuy1h 55%" in legacy
    new = run(market_table_rolling=True, input_time_labels="explicit")
    assert "ROLLING ['AAUSDT']" in new
    assert "takerBuy_closedHour[09:00-10:00Z] 55%" in new


# ---------------------------------------------------------------- a：实时价一行
def _quote_rows(now: datetime, minutes: int = 15) -> list[list[Any]]:
    cur = now.replace(minute=now.minute - now.minute % minutes, second=0, microsecond=0)
    prev = cur - timedelta(minutes=minutes)
    step = minutes * 60_000
    return [
        [_ms(prev), "0.004270", "0.004290", "0.004260", "0.004281", "1", _ms(prev) + step - 1],
        [_ms(cur), "0.004338", "0.004375", "0.004330", "0.004371", "1", _ms(cur) + step - 1],
    ]


def test_live_quote_marks_the_open_bar_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 27, 21, 41, 40, tzinfo=UTC)

    class _R:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"symbol": "PUMPUSDT", "price": "0.004375", "time": _ms(now)}

    monkeypatch.setattr(te.binance_gate, "get", lambda url, params: _R())
    monkeypatch.setattr(te, "_klines", lambda *a, **k: _quote_rows(now))
    q = te.fetch_live_quote("PUMP-USD", "15m", now)
    assert q is not None
    assert (q.price, q.closed_close, q.partial_ohl) == (
        0.004375,
        0.004281,
        (0.004338, 0.004375, 0.00433),
    )
    assert q.closed_at == datetime(2026, 10, 27, 21, 30, tzinfo=UTC)
    line = te.live_price_line(q, now, "{:.6f}".format)
    assert "Live price at 21:41:40 UTC (Binance last trade): 0.004375." in line
    assert "last closed 15m bar closed at 21:30 UTC, 12 min before now, at 0.004281" in line
    assert "+2.20% from that close" in line
    assert "opened at 21:30 UTC has not closed (partial bar, not a closed bar)" in line
    assert _one_sided(line) == []


def test_new_prompt_texts_are_long_short_neutral() -> None:
    """延伸偏空整改的对称检查：新加的说明里每个方向词都有对面那个，多空两句互为镜像。"""
    now = datetime(2026, 10, 27, 21, 41, tzinfo=UTC)
    q = te.LiveQuote(0.004375, now, "15m", 0.004281, now - timedelta(minutes=11), now, (1, 2, 1))
    note = te.converter_live_note(q, replace(q, price=0.004338), "{:.6f}".format)
    p = ta_prompts.PromptParams.for_hold("4h")
    texts = {
        "live": te.live_price_line(q, now, "{:.6f}".format),
        "convert": note,
        "snapshot": ta_prompts.fill(ta_roles.SNAPSHOT_NEUTRAL, p),
        "rolling": market_table.rolling_breadth_line({"A": 1.0}, now, now),
    }
    assert {k: _one_sided(t) for k, t in texts.items() if _one_sided(t)} == {}
    long_part = "at or below it for a long"
    short_part = "at or above it for a short"
    assert long_part in note and short_part in note
    assert ta_prompts.mirror_swap(long_part) == short_part
    assert ta_prompts.mirror_swap("entirely above it for a long") == "entirely below it for a short"
    assert "entirely above it for a long, entirely below it for a short" in note


# ---------------------------------------------------------------- d：卡到手就作废
PUMP_DRAFT: dict[str, Any] = dict(
    bias="short", size_tier_max="M", notional_min=100, notional_max=400, leverage_min=1,
    leverage_max=3, stop_atr_min=1, stop_atr_max=2, take_profit_r_min=1.5, take_profit_r_max=2,
    entry_min=0.004300, entry_max=0.004335, key_levels=[], allowed_signals=["volume_breakout"],
    reason="反抽到 0.0043 一带做空，站上 0.004341 则偏空作废",
)  # fmt: skip
PUMP_DECISION = (
    "**Rating**: Sell\n\n**Executive Summary**: 反抽做空\n\n"
    "**Entry Zone**: 0.004300–0.004335\n\n**Invalidation**: 0.004341\n\n**Size Tier**: M"
)


def _meet(
    monkeypatch: pytest.MonkeyPatch,
    draft: dict[str, Any],
    decision: str,
    price: float | None,
    *,
    ratings: tuple[str, str, str] = ("Sell", "Sell", "SELL"),
    levels: LevelContext | None = None,
    start_price: float | None = None,
    **settings: Any,
) -> tuple[te.Analysis, list[str], list[str]]:
    llm = _Llm(te._CardSymbolDraft.model_validate(draft))
    graph = _Graph(llm, *ratings)
    graph.state["final_trade_decision"] = decision
    eng = _engine(monkeypatch, graph)
    if levels is not None:
        monkeypatch.setattr(te, "level_context", lambda *a, **k: levels)
    eng._s = te.TradingAgentsSettings(**settings)
    notes: list[str] = []
    eng._graph = lambda *a, **k: notes.append(a[2]) or graph  # type: ignore[method-assign]
    now = datetime(2026, 10, 27, 21, 41, 40, tzinfo=UTC)
    quote = None if price is None else te.LiveQuote(price, now, "15m")
    start = quote if start_price is None else te.LiveQuote(start_price, now, "15m")
    quotes = iter([start, quote])  # 开会开始一次、转卡前一次
    monkeypatch.setattr(te, "fetch_live_quote", lambda *a, **k: next(quotes))
    meter = te.CostMeter({"flash": (0.3, 1.2), "pro": (0.5, 2.0)})
    res = eng._analyze_run(entry("PUMPUSDT"), "震荡", object(), meter)
    return res, notes, llm.prompts


def test_pump_regression_invalidation_crossed_before_meeting_is_voided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PUMPUSDT 10-27 21:41：卡写「站上 0.004341 则偏空作废」，开会前价格已到 0.004375。
    默认 watch：方向不动、只盯不开（清信号和区间）。"""
    res, _, _ = _meet(monkeypatch, PUMP_DRAFT, PUMP_DECISION, 0.004375, stale_on_arrival=True)
    cs = res.card_symbol
    assert cs.bias is Bias.SHORT and cs.allowed_signals == [] and cs.entry_zone is None
    assert "stale_on_arrival" in cs.reason and "0.004341" in cs.reason
    assert res.setup is not None and "0.004341" in res.setup["stale_on_arrival"]
    assert res.setup["stale_guard_price_source"] == "live_ticker"
    no_trade, _, _ = _meet(
        monkeypatch,
        PUMP_DRAFT,
        PUMP_DECISION,
        0.004375,
        stale_on_arrival=True,
        stale_on_arrival_action="no_trade",
    )
    assert no_trade.card_symbol.bias is Bias.NO_TRADE and no_trade.card_symbol.entry_zone is None
    # 开关关着：照旧出空卡，不记价格来源
    off, _, _ = _meet(monkeypatch, PUMP_DRAFT, PUMP_DECISION, 0.004375)
    assert off.card_symbol.bias is Bias.SHORT and off.card_symbol.allowed_signals
    assert "stale_on_arrival" not in (off.setup or {})
    assert "stale_guard_price_source" not in (off.setup or {})
    # 价格还在失效价以下、区间在价格上方（做空的正确一侧）：照常出卡
    ok, _, _ = _meet(monkeypatch, PUMP_DRAFT, PUMP_DECISION, 0.004290, stale_on_arrival=True)
    assert ok.card_symbol.allowed_signals and ok.card_symbol.entry_zone is not None
    assert ok.setup is not None and "stale_on_arrival" not in ok.setup


def test_pump_mirror_long_is_treated_the_same(monkeypatch: pytest.MonkeyPatch) -> None:
    long_draft = {
        **PUMP_DRAFT,
        "bias": "long",
        "entry_min": 0.004415,
        "entry_max": 0.004450,
        "reason": "回踩做多",
    }
    decision = (
        PUMP_DECISION.replace("Sell", "Buy")
        .replace("0.004300–0.004335", "0.004415–0.004450")
        .replace("0.004341", "0.004409")
    )
    buy = ("Buy", "Buy", "BUY")
    res, _, _ = _meet(
        monkeypatch, long_draft, decision, 0.004375, ratings=buy, stale_on_arrival=True
    )
    cs = res.card_symbol
    assert cs.bias is Bias.LONG and cs.allowed_signals == [] and "stale_on_arrival" in cs.reason
    assert "0.004409" in cs.reason
    ok, _, _ = _meet(
        monkeypatch, long_draft, decision, 0.004460, ratings=buy, stale_on_arrival=True
    )
    assert ok.card_symbol.allowed_signals


def test_zone_voids_only_when_crossed_after_the_meeting_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """区间 0.004300–0.004335、失效价调高到 0.0050（不碰失效价规则）。"""
    decision = PUMP_DECISION.replace("0.004341", "0.0050")
    # 开会时 0.004320 在区间里，转卡时 0.004360 已涨穿整个区间 → 作废
    crossed, _, _ = _meet(
        monkeypatch, PUMP_DRAFT, decision, 0.004360, start_price=0.004320, stale_on_arrival=True
    )
    assert crossed.card_symbol.allowed_signals == []
    assert "涨穿进场区间" in crossed.card_symbol.reason
    # 开会时就已经在区间上方（等价格回来做空的挂法）→ 不作废
    above, _, _ = _meet(
        monkeypatch, PUMP_DRAFT, decision, 0.004360, start_price=0.004350, stale_on_arrival=True
    )
    assert above.card_symbol.allowed_signals and above.card_symbol.entry_zone is not None


def test_live_price_reaches_meeting_note_and_converter(monkeypatch: pytest.MonkeyPatch) -> None:
    _, notes, prompts = _meet(monkeypatch, PUMP_DRAFT, PUMP_DECISION, 0.004375)
    assert "Live price" not in notes[0] and "just before this card" not in prompts[0]
    _, notes, prompts = _meet(
        monkeypatch, PUMP_DRAFT, PUMP_DECISION, 0.004375, live_price_line=True
    )
    assert "Live price at 21:41:40 UTC (Binance last trade): " in notes[0]
    assert "just before this card is written" in prompts[0]
    assert "+0.00% from" in prompts[0]  # 开会开始和转卡前两次取价（替身两次同价）


@pytest.mark.parametrize(
    ("start", "price", "inv", "zone", "stale"),
    [
        (100.0, 100.0, 99.0, (99.5, 99.8), False),  # 做多：价在失效价上、区间在下方 → 正常
        (100.0, 98.9, 99.0, (99.5, 99.8), True),  # 跌破失效价
        (None, 99.0, 99.0, None, True),  # 正好到失效价（不需要开会时的价）
        (100.0, 99.4, 98.0, (99.5, 99.8), True),  # 开会后跌穿整个回踩区
        (99.6, 99.4, 98.0, (99.5, 99.8), True),  # 开会时在区间里，之后跌穿
        (99.4, 99.3, 98.0, (99.5, 99.8), False),  # 开会时就在区间下方：不按区间作废
        (None, 99.4, 98.0, (99.5, 99.8), False),  # 不知道开会时的价：不按区间作废
        (100.0, 99.6, 98.0, (99.5, 99.8), False),  # 价在区间里
        (100.0, 100.0, None, None, False),  # 没有失效价也没有区间
    ],
)
def test_stale_on_arrival_is_a_mirror_for_long_and_short(
    start: float | None,
    price: float,
    inv: float | None,
    zone: tuple[float, float] | None,
    stale: bool,
) -> None:
    def flip(x: float) -> float:
        return 200.0 - x

    long_zone = Range(min=zone[0], max=zone[1]) if zone else None
    short_zone = Range(min=flip(zone[1]), max=flip(zone[0])) if zone else None
    long_why = te.stale_on_arrival_reason(Bias.LONG, long_zone, inv, price, start)
    short_why = te.stale_on_arrival_reason(
        Bias.SHORT,
        short_zone,
        flip(inv) if inv is not None else None,
        flip(price),
        flip(start) if start is not None else None,
    )
    assert (long_why is not None, short_why is not None) == (stale, stale)
    assert te.stale_on_arrival_reason(Bias.NO_TRADE, long_zone, inv, price, start) is None


def test_invalidation_price_parsing() -> None:
    assert te.invalidation_price(PUMP_DECISION) == pytest.approx(0.004341)
    assert te.invalidation_price("**Invalidation**: 站上 1,234.5 作废") == pytest.approx(1234.5)
    assert te.invalidation_price("**Invalidation**: none") is None
    assert te.invalidation_price("no line") is None


def test_stale_guard_falls_back_to_level_price(monkeypatch: pytest.MonkeyPatch) -> None:
    res, _, _ = _meet(
        monkeypatch,
        PUMP_DRAFT,
        PUMP_DECISION,
        None,
        levels=LevelContext(price=0.0044),
        stale_on_arrival=True,
    )
    assert res.setup is not None and res.setup["stale_guard_price_source"] == "levels_fallback"
    assert res.card_symbol.allowed_signals == []


def test_rolling_breadth_failure_never_fails_the_round(monkeypatch: pytest.MonkeyPatch) -> None:
    def limited(*a: Any, **k: Any) -> Any:
        raise te.binance_gate.RateLimited(418, 0.0)

    monkeypatch.setattr(te.binance_gate, "get", limited)
    eng = te.TradingAgentsEngine.__new__(te.TradingAgentsEngine)
    eng._today = lambda: datetime(2026, 10, 27, 10, 54, tzinfo=UTC)
    assert eng._rolling_breadth(["AAUSDT"]) == "Pool breadth rolling_60m unavailable (RateLimited)"


# ---------------------------------------------------------------- c / e：指挥官
class _Brief:
    def __init__(self) -> None:
        self.text = "旧简报"
        self.calls = 0

    async def refresh_with_provenance(self) -> tuple[str, dict[str, Any]]:
        self.calls += 1
        self.text = f"新简报 {self.calls}"
        return self.text, {"status": "fresh"}


class _Engine:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str, str]] = []  # (币, 大盘判断, 简报)
        self.commander: BattleCardCommander | None = None
        self.publish_after: str | None = None

    async def pick(
        self,
        candidates: Sequence[SymbolPoolEntry],
        max_n: int,
        previous: Any,
        pool: Sequence[SymbolPoolEntry] = (),
    ) -> PickResult:
        return PickResult(
            "本轮开始：看跌 1 2",
            [Pick(e.symbol, "r") for e in candidates[:max_n]],
            0.01,
            regime_direction="看跌",
        )

    async def analyze_with_context(
        self, e: SymbolPoolEntry, regime: str, pick_reason: str = "", *, brief_text: str
    ) -> Analysis:
        self.seen.append((e.symbol, regime, brief_text))
        if e.symbol == self.publish_after and self.commander is not None:
            # 本轮中途 Flash 发布了新大盘（单币会还在排队）
            self.commander.latest_market_view = MarketView(
                market_view_id="mv-mid-round",
                version=9,
                observed_at=self.commander._clock(),
                regime="中途更新：看涨 3 4",
                regime_direction="看涨",
            )
        await asyncio.sleep(0)
        return Analysis(card_symbol(e.symbol), 0.02, setup={"meeting_id": e.symbol})

    async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Analysis:
        raise AssertionError("应走 analyze_with_context")


def _commander(tmp: Path, now: datetime, **kw: Any) -> tuple[BattleCardCommander, _Engine, _Brief]:
    from jiaoyi.slow.commander import EventRules

    engine, brief = _Engine(), _Brief()
    c = BattleCardCommander(
        engine,  # type: ignore[arg-type]
        RISK,
        JsonlDecisionLog(tmp / "log"),
        clock=lambda: now,
        brief=brief,  # type: ignore[arg-type]
        concurrency=1,
        events=EventRules(enabled=True, triggers=frozenset({"signal_against_card"})),
        **kw,
    )
    engine.commander = c
    return c, engine, brief


@pytest.mark.parametrize("on", [False, True])
async def test_symbol_meeting_uses_latest_published_market_view(
    tmp_path: Path, now: datetime, on: bool
) -> None:
    c, engine, _ = _commander(tmp_path, now, latest_market_regime=on)
    engine.publish_after = "AAUSDT"
    card = await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    regimes = {sym: regime for sym, regime, _ in engine.seen}
    assert regimes["AAUSDT"] == "本轮开始：看跌 1 2"
    bb = card.for_symbol("BBUSDT")
    assert bb is not None
    rows = [
        r.output
        for r in JsonlDecisionLog(tmp_path / "log").read_day(now.date().isoformat())
        if r.symbol == "BBUSDT" and r.output and "meeting_setup" in r.output
    ]
    if on:
        assert regimes["BBUSDT"] == "中途更新：看涨 3 4"
        assert bb.source_market_view_id == "mv-mid-round"
        assert rows[-1]["meeting_setup"]["regime_source"] == "latest"
        assert rows[-1]["meeting_setup"]["regime_market_view_id"] == "mv-mid-round"
    else:
        assert regimes["BBUSDT"] == "本轮开始：看跌 1 2"
        assert bb.source_market_view_id != "mv-mid-round"
        assert rows[-1]["meeting_setup"] == {"meeting_id": "BBUSDT"}


@pytest.mark.parametrize("on", [False, True])
async def test_event_meeting_refreshes_the_brief(tmp_path: Path, now: datetime, on: bool) -> None:
    c, engine, brief = _commander(tmp_path, now, event_brief_refresh=on)
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    calls = brief.calls  # 整轮会本来就刷新一次
    brief.text = "上一轮留下的简报"
    await c.refresh_symbol(p, "AAUSDT", "signal_against_card")
    assert engine.seen[-1][0] == "AAUSDT"
    if on:
        assert brief.calls == calls + 1
        assert engine.seen[-1][2] == f"新简报 {calls + 1}"
    else:
        assert brief.calls == calls and engine.seen[-1][2] == "上一轮留下的简报"


@pytest.mark.parametrize("table", ["legacy table", "Pool breadth rolling_60m (09:54-10:54 UTC"])
async def test_live_view_shows_the_table_the_model_actually_got(
    tmp_path: Path, now: datetime, table: str
) -> None:
    from jiaoyi.slow.commander import EventRules

    class _TableEngine(_Engine):
        async def pick(self, candidates: Any, max_n: int, previous: Any, pool: Any = ()) -> Any:
            got = await super().pick(candidates, max_n, previous, pool)
            return replace(got, table=table)

    engine = _TableEngine()
    c = BattleCardCommander(
        engine,  # type: ignore[arg-type]
        RISK,
        JsonlDecisionLog(tmp_path / "log"),
        clock=lambda: now,
        live_dir=tmp_path / "live",
        events=EventRules(enabled=False),
    )
    await c.refresh(pool(now, entry("AAUSDT")), "scheduled")
    text = (tmp_path / "live" / "_MARKET.json").read_text(encoding="utf-8")
    assert ("模型实际收到的池子概况和候选表" in text) is (table != "legacy table")


def test_example_toml_meeting_section_parses_with_every_key_uncommented() -> None:
    text = (ROOT / "config" / "slow.example.toml").read_text(encoding="utf-8")
    on = re.sub(rf"^# ({'|'.join(FRESH_KEYS)}) = ", r"\1 = ", text, flags=re.M)
    data = tomllib.loads(on)
    assert set(FRESH_KEYS) <= set(data["meeting"])
    SlowConfig.model_validate(data)


# ---------------------------------------------------------------- 大盘判断的时间和口径
# 2026-10-28：方向没变就不重发，01:47–04:09 的单币会拿到 23:32 那段话，没有时间
LABELS = [
    ({}, "本轮开始：看跌 1 2"),
    (
        {"regime_time_label": True},
        "（大盘判断 06:30 UTC 发布，距此刻 157 分钟）本轮开始：看跌 1 2",
    ),
    (
        {"regime_max_age": timedelta(minutes=120)},
        "（大盘判断 06:30 UTC 发布，距此刻 157 分钟，已过期，仅作参考）本轮开始：看跌 1 2",
    ),
    (
        {"regime_max_age": timedelta(minutes=180)},
        "本轮开始：看跌 1 2",
    ),
    (
        {"pool_scope_label": True},
        "（其中的涨跌、主动买卖占比等数字是全池口径，不是这个币自己的）本轮开始：看跌 1 2",
    ),
]


@pytest.mark.parametrize(("kw", "expected"), LABELS)
async def test_commander_refresh_symbol_labels_regime_age_and_scope(
    tmp_path: Path, now: datetime, kw: dict[str, Any], expected: str
) -> None:
    from jiaoyi.slow.commander import EventRules

    clock = [now]
    engine = _Engine()
    c = BattleCardCommander(
        engine,  # type: ignore[arg-type]
        RISK,
        JsonlDecisionLog(tmp_path / "log"),
        clock=lambda: clock[0],
        ttl=timedelta(hours=4),
        events=EventRules(enabled=True, triggers=frozenset({"signal_against_card"})),
        **kw,
    )
    engine.commander = c
    p = pool(now, entry("AAUSDT"))
    await c.refresh(p, "scheduled")
    clock[0] = now + timedelta(minutes=157)
    assert await c.refresh_symbol(p, "AAUSDT", "signal_against_card") is not None
    assert engine.seen[-1][1] == expected
    rows = [
        r.output["meeting_setup"]
        for r in JsonlDecisionLog(tmp_path / "log").read_day(now.date().isoformat())
        if r.symbol == "AAUSDT" and r.output and "meeting_setup" in r.output
    ]
    labelled = "距此刻" in expected
    assert ("regime_age_min" in rows[-1]) is labelled
    assert ("regime_stale" in rows[-1]) is ("已过期" in expected)


def test_regime_labels_are_neutral() -> None:
    from jiaoyi.slow.commander import POOL_SCOPE, REGIME_STALE
    from tests.test_market_desk import assert_paired

    labels = [POOL_SCOPE, REGIME_STALE, "全池口径：", "大盘判断 06:30 UTC 发布，距此刻 157 分钟"]
    assert_paired(labels)


def _aged_view(now: datetime) -> MarketView:
    return MarketView(
        market_view_id="mv-2332",
        version=4,
        observed_at=now - timedelta(minutes=157),
        regime="23:32 的判断",
        regime_direction="中性",
    )


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({}, {}),
        ({"regime_time_label": True}, {"age_min": 157.0}),
        (
            {"regime_max_age": timedelta(minutes=120)},
            {"age_min": 157.0, "stale": "已过期，仅作参考"},
        ),
    ],
)
async def test_keeper_and_entry_snapshots_carry_market_age(
    tmp_path: Path, now: datetime, kw: dict[str, Any], expected: dict[str, Any]
) -> None:
    from jiaoyi.gate.entry import JevEntryGate
    from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
    from jiaoyi.keeper.keeper import JevKeeper
    from tests.fastloop_helpers import FAST, GOOD_LONG, answer_with, card, features, signal
    from tests.test_keeper_patrol import close_p, position

    c = BattleCardCommander(
        _Engine(),  # type: ignore[arg-type]
        RISK,
        JsonlDecisionLog(tmp_path / "log"),
        clock=lambda: now,
        **kw,
    )
    view = _aged_view(now)
    cfg = FAST.model_copy(update={"giveback_on": False})
    t = MockJevTransport(answer=close_p(0.1))
    keeper = JevKeeper(
        JevDecisionModel(t, cfg, timeout_ms=200),
        cfg,
        lambda s: features(now),
        lambda n: card(now),
        current_market=lambda: view,
        market_age=c.market_age,
    )
    await keeper.review(position(now), now)
    held = json.loads(t.calls[-1])["current_market"]
    assert {k: held[k] for k in ("age_min", "stale") if k in held} == expected
    assert held["market_view_id"] == "mv-2332"

    t2 = MockJevTransport(answer=answer_with(GOOD_LONG))
    gate = JevEntryGate(JevDecisionModel(t2, FAST, timeout_ms=200), market_age=c.market_age)
    await gate.evaluate(signal(now), card(now).model_copy(update={"current_market": view}), now)
    got = json.loads(t2.calls[-1])["current_market"]
    assert {k: got[k] for k in ("age_min", "stale") if k in got} == expected
