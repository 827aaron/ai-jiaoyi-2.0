"""一键关闭 / 不限：软上限填 0 = 关，配置收、各处按「关」处理、默认值不变；面板开关表守硬上限。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from jiaoyi.contracts import Category
from jiaoyi.contracts.risk_config import AppConfig, RiskConfig
from jiaoyi.core.config import load_app_config
from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.pages import OFF_SWITCHES, build_pool, build_settings
from jiaoyi.slow.screener import CodeSymbolScreener, ScreenerConfig, UserOverrides
from jiaoyi.slow.settings import (
    BudgetConfig,
    ScheduleConfig,
    ScreenerSection,
    SlowConfig,
    slow_defaults,
)
from tests.conftest import ROOT
from tests.test_panel import RISK, _cfg
from tests.test_slow_commander import FakeEngine, entry, make, pool
from tests.test_slow_events import first_card, rules
from tests.test_slow_screener import FakeSource, stat

# ---------------------------------------------------------------- 配置：0 收，默认不变


def test_config_accepts_off_and_defaults_unchanged() -> None:
    assert BudgetConfig(daily_cost_limit_usd=0).daily_cost_limit_usd == 0
    assert ScheduleConfig(max_cards_per_round=0).max_cards_per_round == 0
    assert ScheduleConfig(event_symbol_gap_min=0).event_symbol_gap_min == 0
    assert ScreenerSection(default_category_cap=0).default_category_cap == 0
    # 默认值一个都没动
    assert BudgetConfig().daily_cost_limit_usd == 10.0
    assert ScheduleConfig().max_cards_per_round is None
    assert ScheduleConfig().event_symbol_gap_min == 60.0
    assert ScreenerSection().default_category_cap == 30
    assert slow_defaults("2h").budget.daily_cost_limit_usd == 20.0
    assert slow_defaults("2h").schedule.max_cards_per_round == 15
    # 负数、超上限照旧拒
    for bad in (
        lambda: BudgetConfig(daily_cost_limit_usd=-1),
        lambda: BudgetConfig(daily_cost_limit_usd=1001),
        lambda: ScheduleConfig(max_cards_per_round=-1),
        lambda: ScheduleConfig(event_symbol_gap_min=-1),
        lambda: ScreenerSection(default_category_cap=-1),
    ):
        with pytest.raises(ValidationError):
            bad()


def test_commander_kwargs_turn_zero_budget_into_no_limit() -> None:
    assert SlowConfig().commander_kwargs()["daily_cost_limit_usd"] == 10.0
    off = SlowConfig(budget=BudgetConfig(daily_cost_limit_usd=0))
    assert off.commander_kwargs()["daily_cost_limit_usd"] is None
    gap = SlowConfig(schedule=ScheduleConfig(event_symbol_gap_min=0))
    assert gap.commander_kwargs()["events"].symbol_gap == timedelta(0)  # type: ignore[union-attr]


# ---------------------------------------------------------------- 各处按「关」处理


async def test_budget_off_never_stops_and_never_slows(tmp_path: Path, now: datetime) -> None:
    slow = (timedelta(minutes=80), timedelta(minutes=60))
    for limit in (0.0, None):  # 配置里的 0 和代码里的 None 都是不限
        c, _, clock = make(
            tmp_path / str(limit),
            FakeEngine(["AAUSDT"]),
            now,
            daily_cost_limit_usd=limit,
            slowdown=slow,
            ttl=timedelta(minutes=50),
        )
        p = pool(now, entry("AAUSDT"))
        await c.refresh(p, "scheduled")
        c._charge(10_000.0)  # 花多少都没有上限
        base = timedelta(minutes=30)
        assert c.may_refresh(clock[0])  # 不停开会
        assert c.schedule_interval(base) == base  # 不放慢
        card = await c.refresh(p, "scheduled")
        assert card.expires_at - card.created_at == timedelta(minutes=50)  # 有效期也没换成放慢的
        assert c.take_notice() is None  # 没有「到上限 / 80%」提醒


async def test_budget_limit_still_stops_when_set(tmp_path: Path, now: datetime) -> None:
    c, _, clock = make(tmp_path, FakeEngine(["AAUSDT"]), now, daily_cost_limit_usd=1.0)
    await c.refresh(pool(now, entry("AAUSDT")), "scheduled")
    c._charge(5.0)
    assert not c.may_refresh(clock[0])


def test_max_cards_off_means_no_per_round_cap() -> None:
    capped = SlowConfig(schedule=ScheduleConfig(max_cards_per_round=8))
    off = SlowConfig(schedule=ScheduleConfig(max_cards_per_round=0))
    assert capped.effective_pick_max(20) == 8
    assert off.effective_pick_max(20) == 20  # 只剩 card_pick_max
    assert off.effective_pick_max(5) == 5


async def test_event_gap_off_has_no_per_symbol_gap(tmp_path: Path, now: datetime) -> None:
    for gap, minutes_later, expect in (
        (timedelta(minutes=60), 5, None),  # 对照：间隔内不开
        (timedelta(minutes=60), 70, "AAUSDT"),
        (timedelta(0), 0, "AAUSDT"),  # 不限：刚开完正式会也能立刻开
    ):
        c, _, clock = make(
            tmp_path / f"{gap}-{minutes_later}",
            FakeEngine(["AAUSDT", "BBUSDT"]),
            now,
            events=rules(symbol_gap=gap),
        )
        p = await first_card(c, now)
        clock[0] = t = now + timedelta(minutes=minutes_later)
        c.request_event("AAUSDT", "predictor_against_card")
        got = c.pop_event(t, p)
        assert (got[0] if got else None) == expect


async def test_default_category_cap_off_has_no_cap(now: datetime) -> None:
    src = FakeSource([stat(f"A{i}USDT", range_24h_pct=1 + i) for i in range(6)])
    for cap, n_alt in ((2, 2), (0, 6)):  # 默认上限 2 只进 2 个；0 = 不限，进满
        cfg = SlowConfig(screener=ScreenerSection(default_category_cap=cap)).screener_config(
            pool_size=40, risk_max_spread_bps=None, round_trip_fee_bps=10.0
        )
        got = await CodeSymbolScreener(src, cfg).screen(now)
        assert len(got.in_pool) == n_alt
    # 单类自己写的 0 仍是「一个都不要」（不是不限）；单类上限在默认关了时照样管
    cfg = ScreenerConfig(default_category_cap=0, category_caps={Category.ALT: 0})
    assert not (await CodeSymbolScreener(src, cfg).screen(now)).in_pool
    cfg = ScreenerConfig(default_category_cap=0, category_caps={Category.ALT: 3})
    assert len((await CodeSymbolScreener(src, cfg).screen(now)).in_pool) == 3
    ov = UserOverrides(category_caps={Category.ALT: 1})
    cfg = ScreenerConfig(default_category_cap=0)
    assert len((await CodeSymbolScreener(src, cfg, ov).screen(now)).in_pool) == 1


def test_pool_page_shows_unlimited_category_cap(tmp_path: Path) -> None:
    files = PanelFiles(tmp_path, RISK.day_reset_tz)
    scr = ScreenerSection(
        default_category_cap=0,
        categories={Category.US_TECH: {"cap": 0}},  # type: ignore[dict-item]
    )
    cats = {x.category: x for x in build_pool(files, scr).categories}
    assert cats["alt"].cap is None and cats["alt"].cap_base is None  # 不限
    assert cats["us_tech"].cap == 0  # 单类写 0 = 一个都不要，不混
    assert {x.cap for x in build_pool(files).categories} == {30}  # 默认不变


# ---------------------------------------------------------------- 面板开关表


def _models() -> dict[str, type[BaseModel]]:
    from jiaoyi.contracts.risk_config import FastLoopConfig

    return {
        "budget": BudgetConfig,
        "schedule": ScheduleConfig,
        "screener": ScreenerSection,
        "app": AppConfig,
        "fast_loop": FastLoopConfig,
    }


def test_switch_table_lists_exactly_the_agreed_soft_limits() -> None:
    assert set(OFF_SWITCHES) == {
        "budget.daily_cost_limit_usd",
        "schedule.max_cards_per_round",
        "schedule.event_symbol_gap_min",
        "schedule.event_max_per_day",
        "schedule.entry_max_card_age_min",
        "screener.default_category_cap",
        "screener.min_range_24h_pct",
        "screener.max_range_24h_pct",
        "screener.major_top_n",
        "app.protection_stale_s",
        "app.protection_readback_grace_s",
        "app.protection_prearm_grace_s",
        "fast_loop.regime_flip_min_gap_min",
        "fast_loop.regime_flip_min_symbols",
        "fast_loop.regime_flip_price_max_age_min",
        "fast_loop.regime_flip_check_min",
    }


def test_every_switch_key_is_a_real_field_that_accepts_zero() -> None:
    models = _models()
    for ident in OFF_SWITCHES:
        sec, key = ident.split(".")
        field = models[sec].model_fields[key]
        # 0 必须在合法范围里：有下界的话下界 ≤ 0，且不是 gt
        assert not any(hasattr(m, "gt") and m.gt is not None for m in field.metadata), ident
        lows = [m.ge for m in field.metadata if getattr(m, "ge", None) is not None]
        assert lows and lows[0] <= 0, ident
        highs = [m.le for m in field.metadata if getattr(m, "le", None) is not None]
        assert all(OFF_SWITCHES[ident].back <= h for h in highs), ident  # 关回去的数也合法


def test_zero_is_accepted_end_to_end_for_app_and_fast_loop_switches() -> None:
    cfg = load_app_config(ROOT / "config" / "app.example.toml").model_dump(mode="json")
    for ident in OFF_SWITCHES:
        sec, key = ident.split(".")
        if sec == "app":
            assert AppConfig.model_validate({**cfg, key: 0}) is not None
        elif sec == "fast_loop":
            fl = {**cfg["fast_loop"], key: 0}
            assert AppConfig.model_validate({**cfg, "fast_loop": fl}) is not None


def test_hard_risk_limits_never_get_a_switch() -> None:
    risk_fields = set(RiskConfig.model_fields)
    for ident in OFF_SWITCHES:
        sec, key = ident.split(".")
        assert sec != "risk" and sec != "category", ident
        assert key not in risk_fields, ident
    for name in (
        "daily_loss_halt_pct",
        "max_open_positions",
        "risk_per_trade_pct",
        "max_leverage",
        "max_total_exposure_x",
        "max_notional_per_trade_x",
        "max_spread_bps",
        "max_slippage_bps",
    ):
        assert not any(i.endswith("." + name) for i in OFF_SWITCHES), name


def test_settings_page_carries_the_switch_only_where_the_table_says(tmp_path: Path) -> None:
    page = build_settings(PanelFiles(tmp_path, RISK.day_reset_tz), RISK, _cfg(tmp_path), {})
    seen: dict[str, Any] = {}
    for sec in ("risk", "app", "fast_loop", "predictor", "schedule", "budget", "screener", "tiers"):
        for item in getattr(page, sec):
            if item.off is not None:
                seen[f"{sec}.{item.key}"] = item.off
    assert set(seen) == set(OFF_SWITCHES)
    assert not any(i.off for i in page.risk)  # 硬上限一项都没有
    assert seen["budget.daily_cost_limit_usd"].label == "不限"
    assert seen["schedule.entry_max_card_age_min"].shown == "已关闭"
