"""偏空整改开关（slow/bias_fix.py）：默认一字不差、每个模式只改该改的、分组可复现并落盘。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jiaoyi.contracts import Category, HardFilters, SymbolPoolEntry
from jiaoyi.core.config import ConfigError
from jiaoyi.slow import binance_gate, crowd, ta_prompts
from jiaoyi.slow import tradingagents_engine as eng
from jiaoyi.slow.bias_fix import LEGACY_SETUP, BiasFix, MeetingSetup, resolve
from jiaoyi.slow.review import CardJournal, JournalRow
from jiaoyi.slow.settings import MeetingConfig, load_slow_config
from tests.test_slow_commander import FakeEngine, card_symbol, entry, make, pool

IDS = [f"S{i}USDT@2026-10-28T{i % 24:02d}:{i % 60:02d}:00Z" for i in range(2000)]
NEW = BiasFix("alternate", "neutral", "mirror", "relative")


# ---------------------------------------------------------------- 分组和先发言
def test_default_is_legacy_and_off() -> None:
    s = resolve(BiasFix(), "AAUSDT@x")
    assert (s.arm, s.debate_first, s.funding_label, s.bear_bull_symmetry, s.crowd_ratio_mode) == (
        "off",
        "bull",
        "legacy",
        "legacy",
        "raw",
    )
    assert BiasFix().is_legacy and not NEW.is_legacy


def test_alternate_is_deterministic_and_balanced() -> None:
    cfg = BiasFix(debate_first="alternate")
    firsts = [resolve(cfg, i).debate_first for i in IDS]
    assert firsts == [resolve(cfg, i).debate_first for i in IDS]  # 同一场会永远同一个结果
    share = firsts.count("bear") / len(firsts)
    assert 0.45 < share < 0.55
    # 同一个币连着几轮也两边都有
    one = [resolve(cfg, f"AAUSDT@2026-10-28T{h:02d}:00:00Z").debate_first for h in range(24)]
    assert set(one) == {"bull", "bear"}
    s = resolve(cfg, IDS[0])
    assert s.debate_first_setting == "alternate" and s.arm == "off"


def test_fixed_debate_first_is_used_as_is() -> None:
    assert {resolve(BiasFix(debate_first="bear"), i).debate_first for i in IDS[:50]} == {"bear"}


def test_ab_assignment_is_deterministic_and_legacy_arm_is_all_legacy() -> None:
    cfg = BiasFix("alternate", "neutral", "mirror", "relative", ab=0.5)
    setups = [resolve(cfg, i) for i in IDS]
    assert setups == [resolve(cfg, i) for i in IDS]
    arms = [s.arm for s in setups]
    assert set(arms) == {"new", "legacy"} and 0.45 < arms.count("new") / len(arms) < 0.55
    for s in setups:
        if s.arm == "legacy":
            assert (s.debate_first, s.funding_label, s.bear_bull_symmetry) == (
                "bull",
                "legacy",
                "legacy",
            )
            assert (s.crowd_ratio_mode, s.debate_first_setting) == ("raw", "bull")
        else:
            assert (s.funding_label, s.bear_bull_symmetry, s.crowd_ratio_mode) == (
                "neutral",
                "mirror",
                "relative",
            )
    # 分组和先发言互不相关：new 组里多空先发言两边都有
    assert {s.debate_first for s in setups if s.arm == "new"} == {"bull", "bear"}
    # 概率大致等于 ab；ab = 1 全部 new
    small = [resolve(BiasFix(funding_label="neutral", ab=0.2), i).arm for i in IDS]
    assert 0.16 < small.count("new") / len(small) < 0.24
    assert {resolve(BiasFix(funding_label="neutral", ab=1.0), i).arm for i in IDS} == {"new"}


def test_record_has_every_setting() -> None:
    rec = resolve(NEW, "AAUSDT@x").record()
    assert set(rec) == {
        "meeting_id",
        "arm",
        "debate_first",
        "debate_first_setting",
        "funding_label",
        "bear_bull_symmetry",
        "crowd_ratio_mode",
    }
    assert rec["meeting_id"] == "AAUSDT@x"
    assert "先发言" in resolve(NEW, "AAUSDT@x").describe()


# ---------------------------------------------------------------- 配置
def test_config_defaults_and_validation(tmp_path: Path) -> None:
    m = MeetingConfig()
    assert m.bias_fix() == BiasFix()
    assert load_slow_config(tmp_path / "nope.toml").engine_settings().bias_fix == BiasFix()
    p = tmp_path / "slow.toml"
    p.write_text(
        '[meeting]\ndebate_first = "alternate"\nfunding_label = "neutral"\n'
        'bear_bull_symmetry = "mirror"\ncrowd_ratio_mode = "relative"\nbias_fix_ab = 0.5\n',
        encoding="utf-8",
    )
    assert load_slow_config(p).engine_settings().bias_fix == BiasFix(
        "alternate", "neutral", "mirror", "relative", 0.5
    )
    for bad in (
        'debate_first = "both"',
        'funding_label = "soft"',
        'bear_bull_symmetry = "drop"',
        'crowd_ratio_mode = "zscore"',
        "bias_fix_ab = 1.5",
        "bias_fix_ab = -0.1",
        "bias_fix_ab = 0.5",  # 四项都是现状，两组一样
    ):
        p.write_text(f"[meeting]\n{bad}\n", encoding="utf-8")
        with pytest.raises(ConfigError):
            load_slow_config(p)


# ---------------------------------------------------------------- 提示词：默认不变，模式只改一处
P = ta_prompts.PromptParams.for_hold("2h")


def test_role_prompts_default_unchanged() -> None:
    for role in ("bull", "bear", "aggressive", "conservative", "neutral"):
        base = ta_prompts.role_prompt(role, P, "BTC-USD")
        assert base == ta_prompts.role_prompt(
            role, P, "BTC-USD", mirror=False, neutral_funding=False
        )
    bull = ta_prompts.role_prompt("bull", P, "BTC-USD")
    bear = ta_prompts.role_prompt("bear", P, "BTC-USD")
    assert "crowded short" not in bull and "flushed out" not in bull
    assert "a crowded long (high funding, open interest rising into resistance)" in bear


def test_mirror_adds_exactly_the_symmetric_line_to_the_bull() -> None:
    base = ta_prompts.role_prompt("bull", P, "BTC-USD")
    got = ta_prompts.role_prompt("bull", P, "BTC-USD", mirror=True)
    assert got == base.replace(ta_prompts.BULL_MIRROR_OLD, ta_prompts.BULL_MIRROR_NEW) != base
    assert (
        "a crowded short (deeply negative funding, open interest rising into support); longs "
        "that were already flushed out are no longer fuel for a drop." in got
    )
    # 只动多头；其他角色一字不差
    for role in ("bear", "aggressive", "conservative", "neutral"):
        assert ta_prompts.role_prompt(role, P, mirror=True) == ta_prompts.role_prompt(role, P)


def test_neutral_funding_states_the_baseline_rule_in_the_bear_line() -> None:
    base = ta_prompts.role_prompt("bear", P, "BTC-USD")
    got = ta_prompts.role_prompt("bear", P, "BTC-USD", neutral_funding=True)
    assert got == base.replace(ta_prompts.BEAR_FUNDING_OLD, ta_prompts.BEAR_FUNDING_NEW) != base
    assert "funding well above the Binance baseline of +0.01% per 8h, not merely positive" in got
    assert "high funding" not in got
    for role in ("bull", "aggressive", "conservative", "neutral"):
        assert ta_prompts.role_prompt(role, P, neutral_funding=True) == ta_prompts.role_prompt(
            role, P
        )


FUNDING = [{"fundingRate": "0.0001"}, {"fundingRate": "-0.0002"}, {"fundingRate": "0.0005"}]
OI = [{"sumOpenInterestValue": "100"}, {"sumOpenInterestValue": "112"}]


def test_funding_label_default_is_the_old_text_and_neutral_changes_only_that_line() -> None:
    legacy = eng.format_derivatives(FUNDING, OI)
    assert legacy == eng.format_derivatives(FUNDING, OI, "legacy")
    assert (
        "- Latest funding rate +0.0500% (positive: longs pay, crowded long; negative: the "
        "opposite)" in legacy.splitlines()
    )
    neutral = eng.format_derivatives(FUNDING, OI, "neutral")
    assert neutral == legacy.replace(eng.FUNDING_LABEL_LEGACY, eng.FUNDING_LABEL_NEUTRAL)
    assert (
        "positive: longs pay shorts; negative: shorts pay longs; Binance baseline is +0.01% per "
        "8h" in neutral
    )
    assert "crowded long;" not in neutral and "well above the baseline" in neutral


# ---------------------------------------------------------------- 多空比相对值
def _rows(field: str, vals: list[float]) -> list[dict[str, Any]]:
    return [{"timestamp": 1_761_000_000_000 + i, field: v} for i, v in enumerate(vals)]


class _Resp:
    def __init__(self, rows: list[dict[str, Any]], bad: bool = False) -> None:
        self._rows, self._bad = rows, bad

    def raise_for_status(self) -> None:
        if self._bad:
            raise RuntimeError("boom")

    def json(self) -> list[dict[str, Any]]:
        return self._rows


def _get(seen: list[dict[str, Any]], *, median_ok: bool = True) -> Any:
    def get(url: str, params: dict[str, Any]) -> _Resp:
        seen.append({"url": url, **params})
        path = url.rsplit("/", 1)[1]
        field = {
            "globalLongShortAccountRatio": "longShortRatio",
            "topLongShortPositionRatio": "longShortRatio",
            "takerlongshortRatio": "buySellRatio",
            "openInterestHist": "sumOpenInterestValue",
        }[path]
        if path == "globalLongShortAccountRatio" and params["period"] == "4h":
            return _Resp(_rows(field, [1.5, 2.0, 2.5]), bad=not median_ok)
        return _Resp(_rows(field, [1.0, 1.2, 1.8]))

    return get


@pytest.fixture(autouse=True)
def _clear_median_cache() -> None:
    crowd._MEDIANS.clear()


def test_crowd_raw_default_is_unchanged_and_makes_no_extra_request() -> None:
    seen: list[dict[str, Any]] = []
    blocks, ok = crowd.fetch_blocks("BTCUSDT", "1h", _get(seen))
    assert ok == 4 and len(seen) == 4
    assert "Relative" not in "\n".join(blocks) and "median" not in "\n".join(blocks)
    assert blocks[0] == crowd.summarize(
        _rows("longShortRatio", [1.0, 1.2, 1.8]), crowd.SERIES[0], "1h"
    )


def test_crowd_relative_adds_the_deviation_from_the_30_day_median() -> None:
    seen: list[dict[str, Any]] = []
    raw, _ = crowd.fetch_blocks("BTCUSDT", "1h", _get([]))
    crowd._MEDIANS.clear()
    blocks, ok = crowd.fetch_blocks("BTCUSDT", "1h", _get(seen), ratio_mode="relative")
    assert ok == 4 and blocks[1:] == raw[1:]  # 只改账户多空比那一块
    extra = blocks[0].removeprefix(raw[0] + "\n")
    assert extra == (
        "- Relative to this coin's own 30-day median 2.000: latest 1.800 is -10.0% versus that "
        "median; most coins' all-account ratio sits above 1 structurally, so a ratio above 1 "
        "alone does not show an unusually long crowd, so judge crowding by the deviation from "
        "this coin's own median, not by the ratio being above 1."
    )
    hist = [c for c in seen if c["period"] == "4h"]
    assert hist == [
        {
            "url": f"{crowd._BASE}/globalLongShortAccountRatio",
            "symbol": "BTCUSDT",
            "period": "4h",
            "limit": 180,
        }
    ]
    # 6 小时内同一个币不再拉
    again: list[dict[str, Any]] = []
    crowd.fetch_blocks("BTCUSDT", "1h", _get(again), ratio_mode="relative")
    assert len(again) == 4


def test_crowd_relative_falls_back_to_raw_with_a_note() -> None:
    blocks, _ = crowd.fetch_blocks(
        "BTCUSDT", "1h", _get([], median_ok=False), ratio_mode="relative"
    )
    assert "30-day median is unavailable this round, so only the raw ratio is shown" in blocks[0]
    assert "sits above 1 structurally" in blocks[0]
    assert "BTCUSDT" not in crowd._MEDIANS  # 失败不缓存


def test_crowd_relative_rate_limit_propagates() -> None:
    def get(url: str, params: dict[str, Any]) -> _Resp:
        if params["period"] == "4h":
            raise binance_gate.RateLimited("ban", 60.0)
        return _get([])(url, params)

    with pytest.raises(binance_gate.RateLimited):
        crowd.fetch_blocks("BTCUSDT", "1h", get, ratio_mode="relative")


def test_median_cache_expires() -> None:
    t = [0.0]
    seen: list[dict[str, Any]] = []
    assert crowd.account_ratio_median("XUSDT", _get(seen), clock=lambda: t[0]) == 2.0
    t[0] = crowd.MEDIAN_TTL_S - 1
    crowd.account_ratio_median("XUSDT", _get(seen), clock=lambda: t[0])
    assert len(seen) == 1
    t[0] = crowd.MEDIAN_TTL_S + 1
    crowd.account_ratio_median("XUSDT", _get(seen), clock=lambda: t[0])
    assert len(seen) == 2


# ---------------------------------------------------------------- 引擎：按会议定设置并交给图
def _pool_entry(sym: str) -> SymbolPoolEntry:
    return SymbolPoolEntry(
        symbol=sym,
        category=Category.ALT,
        score=0.5,
        filters=HardFilters(
            quote_volume_24h_usdt=1e8, spread_bps=1, depth_usdt=1e6, listed_days=100, passed=True
        ),
        in_pool=True,
    )


class _Stop(Exception):
    pass


def test_engine_resolves_the_setup_per_meeting_and_passes_it_to_the_graph() -> None:
    e = eng.TradingAgentsEngine.__new__(eng.TradingAgentsEngine)
    e._s = eng.TradingAgentsSettings(bias_fix=BiasFix(debate_first="alternate", ab=0.5))
    now = datetime(2026, 10, 28, 3, 0, tzinfo=UTC)
    e._today = lambda: now  # type: ignore[method-assign]
    e._ticks = SimpleNamespace(get=lambda sym: None)  # type: ignore[assignment]
    seen: list[MeetingSetup] = []

    def graph(*a: Any, **k: Any) -> Any:
        seen.append(a[4] if len(a) > 4 else k["setup"])
        raise _Stop

    e._graph = graph  # type: ignore[method-assign,assignment]
    with pytest.raises(_Stop):
        e._analyze_run(_pool_entry("AAUSDT"), "震荡", None, None)  # type: ignore[arg-type]
    assert seen == [resolve(e._s.bias_fix, "AAUSDT@2026-10-28T03:00:00Z")]


def test_build_config_carries_the_setup_and_defaults_to_legacy() -> None:
    s = eng.TradingAgentsSettings()
    legacy = eng.build_config({}, s)
    assert legacy[eng.ta_nodes.PROMPT_KEY] == {
        "hold": "2h",
        "hold_minutes": 120,
        "lean": "M",
        "lang": "Chinese",
    }
    # 对照的现状组：配置也和改动前一样
    ab = BiasFix(funding_label="neutral", ab=0.5)
    old_arm = next(resolve(ab, i) for i in IDS if resolve(ab, i).arm == "legacy")
    assert eng.build_config({}, s, setup=old_arm) == legacy
    setup = resolve(BiasFix("bear", "neutral", "mirror", "relative"), "x")
    got = eng.build_config({}, s, setup=setup)[eng.ta_nodes.PROMPT_KEY]
    assert [got[k] for k in eng.ta_nodes.SETUP_KEYS] == ["bear", "neutral", "mirror", "relative"]
    one = eng.build_config({}, s, setup=resolve(BiasFix(crowd_ratio_mode="relative"), "x"))
    assert set(one[eng.ta_nodes.PROMPT_KEY]) - set(legacy[eng.ta_nodes.PROMPT_KEY]) == {
        "crowd_ratio_mode"
    }
    assert LEGACY_SETUP.record()["arm"] == "off"


# ---------------------------------------------------------------- 落盘：决策日志和复盘流水
class _SetupEngine(FakeEngine):
    async def analyze(self, e: SymbolPoolEntry, regime: str, pick_reason: str = "") -> Any:
        from jiaoyi.slow.commander import Analysis

        await super().analyze(e, regime, pick_reason)
        return Analysis(card_symbol(e.symbol), 0.02, None, resolve(NEW, f"{e.symbol}@t").record())


async def test_setup_is_written_to_the_decision_log_and_the_card_journal(
    tmp_path: Path, now: datetime
) -> None:
    j = CardJournal(tmp_path)
    c, log, _ = make(tmp_path, _SetupEngine(["AAUSDT"]), now, journal=j)
    await c.refresh(pool(now, entry("AAUSDT")), "scheduled")
    want = resolve(NEW, "AAUSDT@t").record()
    rows = j.read()
    assert [r.meeting_setup for r in rows] == [want]
    recs = [r for r in log.read_day(now.date().isoformat()) if r.symbol == "AAUSDT"]
    assert [r.output.get("meeting_setup") for r in recs if "bias" in r.output] == [want]


def test_old_journal_rows_still_load(tmp_path: Path) -> None:
    j = CardJournal(tmp_path)
    j.path.parent.mkdir(parents=True)
    old = (
        '{"card_id":"c","version":1,"created_at":"2026-10-27T00:00:00Z",'
        '"expires_at":"2026-10-27T02:00:00Z","regime":"r","refresh_reason":"s",'
        '"symbol":"AAUSDT","category":"alt","bias":"short"}'
    )
    j.path.write_text(old + "\n", encoding="utf-8")
    (row,) = j.read()
    assert isinstance(row, JournalRow) and row.meeting_setup is None
