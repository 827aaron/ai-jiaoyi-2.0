"""大盘台（slow/market_desk.py）：全部离线替身，不调真模型、不连币安。"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.contracts import (
    BattleCard,
    FastLoopConfig,
    MarketReviewCandidate,
    MarketReviewEvidence,
    QueuedMarketReview,
    Role,
    SymbolPool,
    SymbolPoolEntry,
)
from jiaoyi.core.config import ConfigError
from jiaoyi.core.hotconfig import HOT_FAST, patch_inplace
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.loop import Runner
from jiaoyi.slow.commander import BattleCardCommander, CardRefreshError, Pick, PickResult
from jiaoyi.slow.market_desk import (
    DESK_SOURCE,
    SWAP_PAIRS,
    MarketDesk,
    ModelReply,
    Probs,
    average,
    check_drivers,
    compose_regime,
    cycle_order,
    derive_direction,
    extract_json,
    instruction_lines,
    mirror_bias,
    mirror_selected,
    normalize_probs,
    parse_sample,
    render_prompt,
    swap_words,
)
from jiaoyi.slow.market_dossier import Dossier, DossierBuilder, DossierField
from jiaoyi.slow.settings import MarketDeskSection, load_slow_config, market_desk_conflict
from tests.test_loop import _app
from tests.test_market_refresh_independent import OfflineResearch, research
from tests.test_regime_flip import bar, records
from tests.test_slow_commander import entry, pool

T0 = datetime(2026, 10, 28, 10, 2, tzinfo=UTC)


# ------------------------------------------------------------------ 替身


def make_dossier(now: datetime = T0, ret4: float = -0.5, up: int = 12, down: int = 28) -> Dossier:
    def f(
        name: str, value: float, mirror: Any = "same", partner: str | None = None
    ) -> DossierField:
        return DossierField(name, value, "%", f"{name} note.", "test", now, mirror, partner, 3)

    return Dossier(
        t0=now,
        fields=(
            f("pool_ret_4h_pct", ret4, "neg"),
            f("sigma_4h_pct", 0.8),
            f("sigma_1h_pct", 0.4),
            f("pool_up_count_4h", float(up), "pair", "pool_down_count_4h"),
            f("pool_down_count_4h", float(down), "pair", "pool_up_count_4h"),
            f("pool_taker_buy_share_1h", 47.0, "pair", "pool_taker_sell_share_1h"),
            f("pool_taker_sell_share_1h", 53.0, "pair", "pool_taker_buy_share_1h"),
            f("fear_greed_index", 30.0, "comp100"),
            f("base_rate_up_4h", 0.29, "pair", "base_rate_down_4h"),
            f("base_rate_flat_4h", 0.47),
            f("base_rate_down_4h", 0.24, "pair", "base_rate_up_4h"),
        ),
        symbols=("AAUSDT", "BBUSDT"),
        pool_id="p",
        pool_ts=now,
        sigma_4h_pct=0.8,
    )


class FakeBuilder:
    def __init__(self, dossier: Dossier | None = None) -> None:
        self.dossier = dossier or make_dossier()
        self.calls = 0

    def build(self, pool: SymbolPool, now: datetime) -> Dossier:
        self.calls += 1
        return dataclasses.replace(self.dossier, t0=now)


def answer(
    up: float,
    flat: float,
    down: float,
    *,
    h1: tuple[float, float, float] = (0.3, 0.4, 0.3),
    regime: str = "等权池 4h -0.500%，σ 0.800%，偏离不大",
    drivers: list[dict[str, Any]] | None = None,
) -> str:
    return json.dumps(
        {
            "p_up": up,
            "p_flat": flat,
            "p_down": down,
            "p1h_up": h1[0],
            "p1h_flat": h1[1],
            "p1h_down": h1[2],
            "median_ret_4h_bp": -5,
            "drivers": drivers or [{"field": "pool_ret_4h_pct", "value": -0.5}],
            "regime": regime,
            "bull_case": "上涨家数 12",
            "bear_case": "下跌家数 28",
        },
        ensure_ascii=False,
    )


class FakeModel:
    """按提示词回答；``script`` 返回回答文本（或抛异常）。每次 0.001 美元。"""

    def __init__(self, script: Callable[[str], str] | str) -> None:
        self.script = script
        self.prompts: list[str] = []

    async def complete(self, prompt: str) -> ModelReply:
        self.prompts.append(prompt)
        text = self.script if isinstance(self.script, str) else self.script(prompt)
        return ModelReply(text, 0.001, True, 12.0, 3000, 500)


def symmetric(prompt: str) -> str:
    """没有方向偏置的模型：只看 pool_ret_4h_pct 的符号。"""
    m = re.search(r"pool_ret_4h_pct = (-?[0-9.]+)", prompt)
    r = float(m.group(1)) if m else 0.0
    return answer(0.2, 0.3, 0.5) if r < 0 else answer(0.5, 0.3, 0.2)


def cfg(**kw: Any) -> MarketDeskSection:
    base: dict[str, Any] = {
        "mode": "replace",
        "cadence_min": 15,
        "offset_min": 2,
        "mirror_test_rate": 0.0,
        "balance_floor_usd": 10.0,
    }
    return MarketDeskSection(**{**base, **kw})


def make_desk(
    tmp: Path,
    publisher: Any,
    model: FakeModel,
    clock: list[datetime],
    *,
    builder: FakeBuilder | None = None,
    balance: list[float | None] | None = None,
    log: JsonlDecisionLog | None = None,
    **kw: Any,
) -> MarketDesk:
    bal = balance if balance is not None else [100.0]
    return MarketDesk(
        cfg(**kw),
        publisher,
        model,
        log or JsonlDecisionLog(tmp / "desk_log"),
        tmp,
        builder=builder or FakeBuilder(),  # type: ignore[arg-type]
        clock=lambda: clock[0],
        balance=lambda: bal[0],
        banned=lambda: 0.0,
    )


def desk_records(log_dir: Path) -> list[dict[str, Any]]:
    out = []
    for p in sorted(log_dir.rglob("*.jsonl")):
        out += [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
    return [r for r in out if "market_desk" in r.get("output", {})]


async def commander_with_flash(
    tmp: Path, now: datetime
) -> tuple[BattleCardCommander, OfflineResearch, list[datetime], SymbolPool, JsonlDecisionLog]:
    commander, engine, clock, candidates, log = await research(tmp, now)
    return commander, engine, clock, candidates, log


# ------------------------------------------------------------------ 设置 / 校验


def test_defaults_are_today() -> None:
    assert MarketDeskSection().mode == "off"
    assert FastLoopConfig().regime_flip_target == "flash_market_only"
    assert "regime_flip_target" in HOT_FAST


def test_config_validation(tmp_path: Path) -> None:
    slow = tmp_path / "slow.toml"
    slow.write_text('[market_desk]\nmode = "replace"\ncadence_min = 15\n', encoding="utf-8")
    loaded = load_slow_config(slow)
    assert loaded.market_desk.mode == "replace" and loaded.market_desk.cadence_min == 15
    assert market_desk_conflict(loaded, "flash_market_only") is not None
    assert market_desk_conflict(loaded, "desk") is None
    assert market_desk_conflict(loaded, "off") is None
    assert (
        market_desk_conflict(load_slow_config(tmp_path / "missing.toml"), "flash_market_only")
        is None
    )
    MarketDeskSection(cadence_min=5, offset_min=2)
    for bad in (
        {"cadence_min": 4},
        {"cadence_min": 15, "offset_min": 15},
        {"cadence_min": 60, "max_age_min": 60},
        {"samples": 2, "min_valid_samples": 3},
        {"mode": "supplement"},
    ):
        with pytest.raises(ValueError):
            MarketDeskSection(**bad)
    slow.write_text("[market_desk]\nbogus = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_slow_config(slow)


def test_cli_load_refuses_replace_with_flash_flip_target(tmp_path: Path) -> None:
    import argparse

    from jiaoyi.cli import _load

    root = Path(__file__).resolve().parents[1] / "config"
    app = tmp_path / "app.toml"
    app.write_text((root / "app.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    slow = tmp_path / "slow.toml"
    slow.write_text('[market_desk]\nmode = "replace"\n', encoding="utf-8")
    args = argparse.Namespace(risk=str(root / "risk.example.toml"), app=str(app), slow=str(slow))
    with pytest.raises(ConfigError, match="regime_flip_target"):
        _load(args)
    text = app.read_text(encoding="utf-8").replace(
        'regime_flip_target = "flash_market_only"', 'regime_flip_target = "desk"'
    )
    app.write_text(text, encoding="utf-8")
    _, app_cfg, slow_cfg = _load(args)
    assert app_cfg.fast_loop.regime_flip_target == "desk"
    assert slow_cfg.market_desk.mode == "replace"


# ------------------------------------------------------------------ 中立性


def _bag(s: str) -> Counter[str]:
    return Counter(re.findall(r"[A-Za-z_0-9]+|[^\sA-Za-z_0-9,.;:()\[\]{}\"'/=+\-]", s))


def assert_paired(sentences: Sequence[str]) -> None:
    """对调表逐句查：每一句要么自身两边对称（对调后词袋不变），要么有对调后的另一句、长度相当。"""
    pool = list(sentences)
    for s in pool:
        swapped = swap_words(s)
        if swapped == s:
            continue
        if _bag(swapped) == _bag(s):
            continue
        assert swapped in pool, f"偏向一边的句子没有对调后的另一句：{s!r}"
        assert 0.8 <= len(swapped) / len(s) <= 1.25


def test_swap_table_covers_required_pairs() -> None:
    pairs = {frozenset(p) for p in SWAP_PAIRS}
    for a, b in (("多", "空"), ("涨", "跌"), ("支撑", "阻力"), ("反弹", "回落"), ("up", "down")):
        assert frozenset((a, b)) in pairs
    assert swap_words("p_up above 0, longs pay") == "p_down below 0, shorts pay"
    assert swap_words("update") == "update"  # 词边界：不拆单词


@pytest.mark.parametrize("order", ["up_first", "down_first"])
def test_instruction_wording_is_paired(order: Any) -> None:
    assert_paired(instruction_lines(order, 0.5))


def test_dossier_notes_are_paired() -> None:
    builder = DossierBuilder(get=FakeBinance().get, fng_get=FakeBinance().fng)
    d = builder.build(pool(T0, *(entry(s) for s in SYMS)), T0)
    assert len(d.fields) > 30
    assert_paired([f.note for f in d.fields])
    text = render_prompt(d, "up_first", 0.5, "c") + render_prompt(d, "down_first", 0.5, "c")
    plain = text.lower().replace("fear_greed_index", "")
    for word in ("crowded", "panic", "greed", "fear ", "squeeze", "capitulation", "拥挤", "恐慌"):
        assert word not in plain, word  # 只给数字，不给定性标签


def test_order_alternates_by_hash_and_is_recorded() -> None:
    ids = [f"2026-10-28T{h:02d}:{m:02d}Z" for h in range(24) for m in (2, 17, 32, 47)]
    orders = Counter(cycle_order(i) for i in ids)
    assert set(orders) == {"up_first", "down_first"}
    assert 0.3 < orders["up_first"] / len(ids) < 0.7
    assert all(cycle_order(i) == cycle_order(i) for i in ids)  # 可复现
    d = make_dossier()
    up = render_prompt(d, "up_first", 0.5, "c")
    down = render_prompt(d, "down_first", 0.5, "c")
    assert up.index("pool_up_count_4h =") < up.index("pool_down_count_4h =")
    assert down.index("pool_down_count_4h =") < down.index("pool_up_count_4h =")
    assert up.index("pool_taker_buy_share") < up.index("pool_taker_sell_share")
    assert down.index("pool_taker_sell_share") < down.index("pool_taker_buy_share")
    assert "Keys: p_up, p_flat, p_down" in up and "Keys: p_down, p_flat, p_up" in down
    assert up.index("bull_case:") < up.index("bear_case:")
    assert down.index("bear_case:") < down.index("bull_case:")


def test_mirror_flips_signs_and_swaps_pairs() -> None:
    d = make_dossier()
    m = d.mirror()
    assert m.value("pool_ret_4h_pct") == 0.5
    assert m.value("pool_up_count_4h") == 28 and m.value("pool_down_count_4h") == 12
    assert m.value("pool_taker_buy_share_1h") == 53 and m.value("pool_taker_sell_share_1h") == 47
    assert m.value("fear_greed_index") == 70
    assert m.value("base_rate_up_4h") == 0.24 and m.value("base_rate_down_4h") == 0.29
    assert m.value("sigma_4h_pct") == 0.8 and m.value("base_rate_flat_4h") == 0.47
    assert m.mirrored and m.mirror().to_json() == d.to_json()


def test_mirror_selection_rate() -> None:
    ids = [f"c{i}" for i in range(600)]
    hits = sum(mirror_selected(i, 1 / 6) for i in ids)
    assert 70 <= hits <= 130
    assert not any(mirror_selected(i, 0.0) for i in ids)
    assert all(mirror_selected(i, 1.0) for i in ids)


def test_mirror_bias_math() -> None:
    assert mirror_bias([], 0.05) == (False, 0.0)
    two = [{"gap": 0.2}, {"gap": 0.2}]
    assert mirror_bias(two, 0.05)[0] is False  # 次数不够不判
    flag, mean = mirror_bias([*two, {"gap": 0.05}], 0.05)
    assert flag and mean == pytest.approx(0.15)
    assert mirror_bias([{"gap": 0.01}] * 5, 0.05)[0] is False


async def test_mirror_test_detects_biased_model(tmp_path: Path) -> None:
    clock = [T0]
    pub = FakePublisher()
    fair = make_desk(tmp_path / "fair", pub, FakeModel(symmetric), clock, mirror_test_rate=1.0)
    for k in range(4):
        clock[0] = T0 + timedelta(minutes=15 * k)
        await fair.run(pool(clock[0], entry("AAUSDT")), "scheduled")
    assert fair.state.mirror_gaps and all(g["gap"] < 1e-9 for g in fair.state.mirror_gaps)
    assert not fair.state.mirror_flag and fair.take_notice() is None

    biased = make_desk(
        tmp_path / "biased", FakePublisher(), FakeModel(answer(0.2, 0.3, 0.5)), clock,
        mirror_test_rate=1.0,
    )  # fmt: skip
    for k in range(3):
        clock[0] = T0 + timedelta(minutes=15 * k)
        await biased.run(pool(clock[0], entry("AAUSDT")), "scheduled")
    assert biased.state.mirror_flag
    note = biased.take_notice()
    assert note is not None and "偏置" in note
    rows = log_rows(tmp_path / "biased")
    assert rows[-1]["mirror"]["gap_up_vs_mirror_down"] == pytest.approx(0.3)


async def test_prompt_has_no_anchors(tmp_path: Path) -> None:
    commander, *_ = await commander_with_flash(tmp_path / "r", T0)
    model = FakeModel(answer(0.6, 0.25, 0.15))
    clock = [T0]
    desk = make_desk(tmp_path, commander, model, clock)
    await desk.run(pool(T0, entry("AAUSDT")), "scheduled")
    flash = commander.latest_market_view
    clock[0] += timedelta(minutes=15)
    await desk.run(pool(clock[0], entry("AAUSDT")), "scheduled")
    assert flash is not None
    for p in model.prompts:
        assert "旧研究背景" not in p  # 总盘会文字
        assert "上涨家数 12" not in p and "未来 4 小时概率" not in p  # 自己上一轮的观点
        assert "lesson" not in p.lower().replace("lessons or positions", "")
        assert "position" not in p.lower().replace("lessons or positions", "")


# ------------------------------------------------------------------ 解析 / 平均 / 方向


def test_normalize_probs() -> None:
    p = normalize_probs(30, 40, 30)
    assert (p.up, p.flat, p.down) == pytest.approx((0.3, 0.4, 0.3))
    p = normalize_probs(0.0, 0.0, 1.0)
    assert p.up >= 0.0199 and p.down <= 0.961 and sum((p.up, p.flat, p.down)) == pytest.approx(1)
    for bad in ((0.5, 0.5, 0.5), (-0.1, 0.6, 0.5), ("0.3", 0.4, 0.3), (None, 0.5, 0.5)):
        with pytest.raises(ValueError):
            normalize_probs(*bad)


def test_extract_json_and_parse_failures() -> None:
    d = make_dossier()
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('好的：{"a": 2} 完') == {"a": 2}
    with pytest.raises(ValueError):
        extract_json("没有 JSON")

    def p(text: str) -> Any:
        return parse_sample(ModelReply(text, 0.001, True, 1.0), 0, d, mirrored=False)

    assert p(answer(0.5, 0.3, 0.2)).probs is not None
    assert p("not json").probs is None and p("not json").error
    no_1h = json.loads(answer(0.5, 0.3, 0.2))
    del no_1h["p1h_up"]
    bad = p(json.dumps(no_1h))
    assert bad.probs is None and "1 小时" in bad.error
    assert p(answer(0.9, 0.9, 0.9)).probs is None


def test_drivers_must_match_dossier() -> None:
    d = make_dossier()
    ok, bad = check_drivers(
        [
            {"field": "pool_ret_4h_pct", "value": -0.5},
            {"field": "pool_ret_4h_pct", "value": -0.7},
            {"field": "made_up", "value": 1},
            {"field": "sigma_4h_pct", "value": "0.8"},
        ],
        d,
    )
    assert ok == [{"field": "pool_ret_4h_pct", "value": -0.5}]
    assert [b["why"][:6] for b in bad] == ["值和数据包不", "数据包里没有", "值不是数字"]


def test_average_and_derived_direction() -> None:
    avg = average([Probs(0.6, 0.2, 0.2), Probs(0.4, 0.4, 0.2)])
    assert (avg.up, avg.flat, avg.down) == pytest.approx((0.5, 0.3, 0.2))
    assert derive_direction(avg, 0.45, 0.15) == "看涨"
    assert derive_direction(Probs(0.2, 0.3, 0.5), 0.45, 0.15) == "看跌"
    assert derive_direction(Probs(0.44, 0.36, 0.2), 0.45, 0.15) == "中性"  # 不到 θ
    assert derive_direction(Probs(0.46, 0.14, 0.4), 0.45, 0.15) == "中性"  # 不到 δ


def test_compose_regime_alternates_and_cites_numbers() -> None:
    d = make_dossier()
    s = parse_sample(ModelReply(answer(0.5, 0.3, 0.2), 0, True, 1), 0, d, mirrored=False)
    up = compose_regime(s, Probs(0.5, 0.3, 0.2), d, "up_first")
    down = compose_regime(s, Probs(0.5, 0.3, 0.2), d, "down_first")
    assert up.index("看涨理由") < up.index("看跌理由")
    assert down.index("看跌理由") < down.index("看涨理由")
    assert up.index("涨 50%") < up.index("跌 20%") and down.index("跌 20%") < down.index("涨 50%")
    plain = compose_regime(None, Probs(0.3, 0.4, 0.3), d, "up_first")
    assert "等权池近 4 小时 -0.50%" in plain


async def test_samples_are_averaged_and_failures_tolerated(tmp_path: Path) -> None:
    replies = iter(
        [answer(0.6, 0.2, 0.2), "garbage", answer(0.4, 0.4, 0.2), "{}", answer(0.5, 0.3, 0.2)]
    )
    model = FakeModel(lambda _: next(replies))
    clock = [T0]
    pub = FakePublisher()
    desk = make_desk(tmp_path, pub, model, clock)
    run = await desk.run(pool(T0, entry("AAUSDT")), "scheduled")
    assert run.status == "ok" and len(model.prompts) == 5
    assert run.probs is not None and run.probs.up == pytest.approx(0.5)
    assert desk.state.latest is not None and desk.state.latest["valid_samples"] == 3
    row = log_rows(tmp_path)[-1]
    assert [s["valid"] for s in row["samples"]] == [True, False, True, False, True]


async def test_too_few_valid_samples_fails_without_publishing(tmp_path: Path) -> None:
    replies = iter(["x", "y", answer(0.6, 0.2, 0.2), "z", "w"])
    clock = [T0]
    pub = FakePublisher()
    desk = make_desk(tmp_path, pub, FakeModel(lambda _: next(replies)), clock)
    run = await desk.run(pool(T0, entry("AAUSDT")), "scheduled")
    assert run.status == "failed" and "合格回答只有 1/5" in run.reason
    assert pub.published == [] and desk.state.latest is None
    assert run.cost_usd == pytest.approx(0.005)  # 失败也照算钱
    recs = desk_records(tmp_path / "desk_log")
    assert recs[-1]["outcome"] == "error" and recs[-1]["cost_usd"] == pytest.approx(0.005)


# ------------------------------------------------------------------ off / 花费 / 自停


async def test_mode_off_makes_zero_model_calls(tmp_path: Path) -> None:
    model = FakeModel(answer(0.6, 0.2, 0.2))
    builder = FakeBuilder()
    clock = [T0]
    desk = make_desk(tmp_path, FakePublisher(), model, clock, builder=builder, mode="off")
    desk.request_run("regime_flip")
    desk.observe_prices(T0, 100.0, {})
    assert desk.due(T0) is None
    run = await desk.run(pool(T0, entry("AAUSDT")), "scheduled")
    assert run.status == "skipped" and model.prompts == [] and builder.calls == 0
    assert not desk.owns_market(T0)


async def test_cost_records_daily_limit_and_balance_floor(tmp_path: Path) -> None:
    clock = [T0]
    bal: list[float | None] = [100.0]
    model = FakeModel(answer(0.6, 0.2, 0.2))
    log = JsonlDecisionLog(tmp_path / "desk_log")
    desk = make_desk(
        tmp_path, FakePublisher(), model, clock, balance=bal, log=log,
        daily_cost_limit_usd=0.008, mirror_test_rate=1.0,
    )  # fmt: skip
    run = await desk.run(pool(T0, entry("AAUSDT")), "scheduled")
    assert run.cost_usd == pytest.approx(0.010)  # 5 次 + 镜像 5 次
    rec = desk_records(tmp_path / "desk_log")[-1]
    assert rec["role"] == Role.COMMANDER.value and rec["symbol"] is None
    assert rec["cost_usd"] == pytest.approx(0.010)
    assert rec["output"]["market_desk"]["status"] == "ok"
    clock[0] += timedelta(minutes=15)
    skipped = await desk.run(pool(clock[0], entry("AAUSDT")), "scheduled")
    assert skipped.status == "skipped" and "日上限" in skipped.reason
    assert len(model.prompts) == 10
    assert "日上限" in (desk.take_notice() or "")
    clock[0] += timedelta(days=1)  # 曼谷换日清零
    assert (await desk.run(pool(clock[0], entry("AAUSDT")), "scheduled")).status == "ok"
    desk.state.cost_today_usd = 0.0  # 只看余额低线
    bal[0] = 5.0
    clock[0] += timedelta(minutes=15)
    low = await desk.run(pool(clock[0], entry("AAUSDT")), "scheduled")
    assert low.status == "skipped" and "余额" in low.reason and len(model.prompts) == 20
    bal[0] = None  # 余额未知：不拦
    desk.state.cost_today_usd = 0.0
    clock[0] += timedelta(minutes=15)
    assert (await desk.run(pool(clock[0], entry("AAUSDT")), "scheduled")).status == "ok"


def test_panel_counts_desk_cost_separately() -> None:
    from jiaoyi.contracts import DecisionRecord, Outcome
    from jiaoyi.panel.pages import _ai_call

    rec = DecisionRecord(
        record_id="r",
        ts=T0,
        role=Role.COMMANDER,
        outcome=Outcome.INFO,
        summary="大盘台",
        output={"market_desk": {}},
        cost_usd=0.01,
    )
    call = _ai_call(rec)
    assert call is not None and call.who == "大盘台" and call.cost_usd == 0.01


# ------------------------------------------------------------------ 定时 / 加跑


def test_slots_and_requested_runs_respect_gap(tmp_path: Path) -> None:
    clock = [T0]
    desk = make_desk(tmp_path, FakePublisher(), FakeModel("x"), clock, min_run_gap_min=5)
    assert desk.due(T0) == "scheduled"
    desk.state.last_slot = desk.slot(T0)
    desk.state.last_run_at = T0
    assert desk.due(T0 + timedelta(minutes=10)) is None
    assert desk.due(T0 + timedelta(minutes=15)) == "scheduled"  # HH:17 下一格
    desk.request_run("regime_flip")
    assert desk.due(T0 + timedelta(minutes=3)) is None  # 不到 5 分钟
    assert desk.due(T0 + timedelta(minutes=6)) == "regime_flip"


def test_move_trigger_counts_only_after_last_run(tmp_path: Path) -> None:
    clock = [T0]
    desk = make_desk(
        tmp_path, FakePublisher(), FakeModel("x"), clock, move_trigger_pct=1.0,
        move_trigger_window_min=15, min_run_gap_min=5,
    )  # fmt: skip
    desk.state.last_slot = desk.slot(T0)
    desk.state.last_run_at = T0
    syms = {f"S{i}USDT": 10.0 for i in range(6)}
    desk.observe_prices(T0 + timedelta(minutes=1), 100.0, syms)
    desk.observe_prices(T0 + timedelta(minutes=4), 100.5, syms)
    assert desk.due(T0 + timedelta(minutes=6)) is None  # 0.5% 不够
    desk.observe_prices(T0 + timedelta(minutes=6), 101.2, syms)
    assert desk.due(T0 + timedelta(minutes=6)) == "move"
    desk._consume(T0 + timedelta(minutes=6), "move")  # 跑过了：之前的价不再算
    desk.observe_prices(T0 + timedelta(minutes=7), 101.3, syms)
    assert desk.due(T0 + timedelta(minutes=12)) is None
    drop = {s: 9.8 for s in syms}  # 池中位数 -2%
    desk.observe_prices(T0 + timedelta(minutes=12), 101.3, drop)
    assert desk.due(T0 + timedelta(minutes=12)) == "move"


# ------------------------------------------------------------------ 接管发布


class FakePublisher:
    """只记录发布（不验证代数），给不关心指挥官的测试用。"""

    def __init__(self) -> None:
        from jiaoyi.contracts import MarketView

        self.MarketView = MarketView
        self.latest_market_view: Any = None
        self.published: list[Any] = []
        self.generation = 0

    def new_market_generation(self) -> int:
        self.generation += 1
        return self.generation

    def set_market_desk(self, desk: Any) -> None:
        self.desk = desk

    def publish_desk_view(self, **kw: Any) -> Any:
        view = self.MarketView(
            market_view_id=f"v{len(self.published) + 1}",
            version=len(self.published) + 1,
            observed_at=T0,
            regime=kw["regime"],
            regime_direction=kw["direction"],
            source=DESK_SOURCE,
            refresh_reason=kw["reason"],
        )
        self.published.append(view)
        self.latest_market_view = view
        return view


def log_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in sorted((root / "market_desk").glob("*.jsonl")):
        rows += [json.loads(line) for line in p.read_text().splitlines()]
    return rows


class Switch:
    """可切换回答的模型脚本。"""

    def __init__(self, text: str) -> None:
        self.text = text

    def __call__(self, prompt: str) -> str:
        return self.text


async def test_replace_takeover_publishes_via_commander_with_shared_versions(
    tmp_path: Path,
) -> None:
    commander, _engine, clock, candidates, _log = await commander_with_flash(tmp_path / "r", T0)
    flash = commander.latest_market_view
    assert flash is not None and flash.source == "scheduled_market" and flash.version == 1
    notified: list[Any] = []
    commander.set_on_market(notified.append)
    desk = make_desk(tmp_path, commander, FakeModel(answer(0.6, 0.25, 0.15)), clock)
    run = await desk.run(candidates, "scheduled")
    view = commander.latest_market_view
    assert run.published is view and view is not None
    assert view.source == DESK_SOURCE and view.version == 2 and view.regime_direction == "看涨"
    assert notified == [view]  # 管家复核照常排队（#307）
    ref = commander.regime_reference
    assert ref is not None and ref.direction == "看涨"  # 翻转参照由大盘台设
    assert desk.owns_market(clock[0])
    assert desk.state.latest is not None
    assert desk.state.latest["published_view_id"] == view.market_view_id
    assert desk.state.latest["probs_4h"]["up"] == pytest.approx(0.6)
    row = log_rows(tmp_path)[-1]
    assert (
        row["dossier"]["fields"]
        and row["prompt_version"]
        and row["order"]
        in (
            "up_first",
            "down_first",
        )
    )
    assert row["score_targets"]["1h"]["resolve_at"] == (clock[0] + timedelta(hours=1)).isoformat()
    assert row["score_targets"]["4h"]["resolve_at"] == (clock[0] + timedelta(hours=4)).isoformat()
    assert row["score_targets"]["4h"]["flat_band_pct"] == pytest.approx(0.4)
    assert row["probs_1h"] is not None and len(row["samples"]) == 5
    # 契约没加字段：MarketView 原样能读
    assert set(view.model_dump()) == set(flash.model_dump())


async def test_hysteresis_needs_consecutive_runs_and_minutes(tmp_path: Path) -> None:
    commander, _, clock, candidates, _ = await commander_with_flash(tmp_path / "r", T0)
    script = Switch(answer(0.6, 0.25, 0.15))
    desk = make_desk(tmp_path, commander, FakeModel(script), clock)
    await desk.run(candidates, "scheduled")
    first = commander.latest_market_view
    assert first is not None and first.version == 2
    for _k in range(3):  # 同方向：不再发布
        clock[0] += timedelta(minutes=15)
        await desk.run(candidates, "scheduled")
    assert commander.latest_market_view is first
    script.text = answer(0.15, 0.25, 0.6)
    clock[0] += timedelta(minutes=15)
    assert (await desk.run(candidates, "scheduled")).published is None  # 第 1 轮
    clock[0] += timedelta(minutes=15)
    assert (await desk.run(candidates, "scheduled")).published is None  # 2 轮但只 15 分钟
    clock[0] += timedelta(minutes=15)
    third = await desk.run(candidates, "scheduled")  # 3 轮、30 分钟
    assert third.published is not None and third.published.version == 3
    assert commander.regime_reference is not None
    assert commander.regime_reference.direction == "看跌"
    # 一轮不一致就重来
    script.text = answer(0.6, 0.25, 0.15)
    clock[0] += timedelta(minutes=15)
    await desk.run(candidates, "scheduled")
    script.text = answer(0.15, 0.25, 0.6)
    clock[0] += timedelta(minutes=15)
    await desk.run(candidates, "scheduled")
    assert commander.latest_market_view.version == 3  # type: ignore[union-attr]


async def test_event_run_plus_next_scheduled_run_publish(tmp_path: Path) -> None:
    commander, _, clock, candidates, _ = await commander_with_flash(tmp_path / "r", T0)
    script = Switch(answer(0.6, 0.25, 0.15))
    desk = make_desk(tmp_path, commander, FakeModel(script), clock)
    await desk.run(candidates, "scheduled")
    script.text = answer(0.15, 0.25, 0.6)
    clock[0] += timedelta(minutes=8)
    assert (await desk.run(candidates, "move")).published is None
    clock[0] += timedelta(minutes=7)
    published = (await desk.run(candidates, "scheduled")).published
    assert published is not None and published.regime_direction == "看跌"
    assert published.refresh_reason == DESK_SOURCE


async def test_flash_full_round_is_record_only_while_desk_owns(tmp_path: Path) -> None:
    commander, engine, clock, candidates, _log = await commander_with_flash(tmp_path / "r", T0)
    previous: list[BattleCard | None] = []
    original_pick = engine.pick

    async def pick(c: Sequence[SymbolPoolEntry], n: int, prev: BattleCard | None, pool: Any = ()):
        previous.append(prev)
        return await original_pick(c, n, prev, pool)

    engine.pick = pick  # type: ignore[method-assign]
    desk = make_desk(tmp_path, commander, FakeModel(answer(0.6, 0.25, 0.15)), clock)
    await desk.run(candidates, "scheduled")
    desk_view = commander.latest_market_view
    desk_ref = commander.regime_reference
    assert desk_view is not None and desk_ref is not None
    engine.symbol_calls.clear()
    clock[0] += timedelta(minutes=30)
    card = await commander.refresh(candidates, "manual")
    assert commander.latest_market_view is desk_view  # Flash 看跌没发布
    assert commander.regime_reference == desk_ref  # 参照只由大盘台设
    assert card.regime == desk_view.regime and card.regime_direction == "看涨"
    assert {r for _, r in engine.symbol_calls} == {desk_view.regime}  # 单币会议看大盘台
    assert all(s.source_market_view_id == desk_view.market_view_id for s in card.symbols)
    assert previous[-1] is not None and previous[-1].regime.startswith("旧研究背景")  # 不被锚定
    assert "大盘台接管" in (commander.regime_flip_blocked(clock[0]) or "")
    with pytest.raises(CardRefreshError):
        await commander.refresh_market(candidates)
    assert engine.market_calls == 0


async def test_flash_nominations_do_not_enter_queue_while_desk_owns(tmp_path: Path) -> None:
    commander, engine, clock, candidates, _log = await commander_with_flash(tmp_path / "r", T0)
    desk = make_desk(tmp_path, commander, FakeModel(answer(0.6, 0.25, 0.15)), clock)
    await desk.run(candidates, "scheduled")
    card = commander.current(clock[0])
    assert card is not None
    token = card.symbols[0].research_token()

    async def pick(c: Any, n: int, prev: Any, pool: Any = ()) -> PickResult:
        return PickResult(
            "Flash：偏弱 BTC -1%",
            [Pick(e.symbol, "量 +10%") for e in c[:n]],
            cost_usd=0.01,
            regime_direction="看跌",
            review_candidates=[
                MarketReviewCandidate(
                    symbol=card.symbols[0].symbol,
                    research_token=token,
                    reason="新数字",
                    evidence=[MarketReviewEvidence(field="metrics.x", value=1)],
                )
            ],
            reviews_valid=True,
        )

    engine.pick = pick  # type: ignore[method-assign]
    clock[0] += timedelta(minutes=30)
    await commander.refresh(candidates, "manual")
    assert commander._market_reviews == {}


async def test_desk_publish_migrates_valid_nominations(tmp_path: Path) -> None:
    commander, _engine, clock, candidates, _log = await commander_with_flash(tmp_path / "r", T0)
    card = commander.current(clock[0])
    assert card is not None and commander.latest_market_view is not None
    good, stale = card.symbols[0], card.symbols[1]

    def queued(sym: str, token: str) -> QueuedMarketReview:
        return QueuedMarketReview(
            candidate=MarketReviewCandidate(
                symbol=sym,
                research_token=token,
                reason="r",
                evidence=[MarketReviewEvidence(field="metrics.x", value=1)],
            ),
            market_view_id=commander.latest_market_view.market_view_id,  # type: ignore[union-attr]
            first_seen=clock[0],
            eligible_at=clock[0],
            expires_at=clock[0] + timedelta(minutes=30),
        )

    commander._market_reviews = {
        good.symbol: queued(good.symbol, good.research_token()),
        stale.symbol: queued(stale.symbol, "old-token"),
    }
    commander._pending = {s: ("market_review", clock[0]) for s in commander._market_reviews}
    desk = make_desk(tmp_path, commander, FakeModel(answer(0.6, 0.25, 0.15)), clock)
    await desk.run(candidates, "scheduled")
    view = commander.latest_market_view
    assert view is not None and view.source == DESK_SOURCE
    assert set(commander._market_reviews) == {good.symbol}
    assert commander._market_reviews[good.symbol].market_view_id == view.market_view_id


async def test_fallback_flash_after_expiry_then_takeover_again(tmp_path: Path) -> None:
    commander, _engine, clock, candidates, _log = await commander_with_flash(tmp_path / "r", T0)
    desk = make_desk(tmp_path, commander, FakeModel(answer(0.6, 0.25, 0.15)), clock)
    await desk.run(candidates, "scheduled")
    assert desk.owns_market(clock[0])
    clock[0] += timedelta(minutes=80)  # > max_age 75，期间大盘台没出结果
    assert not desk.available(clock[0]) and not desk.owns_market(clock[0])
    await commander.refresh(candidates, "manual")
    flash = commander.latest_market_view
    assert flash is not None and flash.source == "scheduled_market"
    assert flash.regime_direction == "看跌" and commander.regime_reference is not None
    assert commander.regime_reference.direction == "看跌"
    run = await desk.run(candidates, "scheduled")  # 恢复后第一轮立即接管
    assert run.published is not None and run.published.version == flash.version + 1


async def test_hold_fallback_keeps_desk_view(tmp_path: Path) -> None:
    commander, _engine, clock, candidates, _log = await commander_with_flash(tmp_path / "r", T0)
    desk = make_desk(
        tmp_path, commander, FakeModel(answer(0.6, 0.25, 0.15)), clock, fallback="hold"
    )
    await desk.run(candidates, "scheduled")
    view = commander.latest_market_view
    clock[0] += timedelta(hours=3)
    assert desk.owns_market(clock[0])
    await commander.refresh(candidates, "manual")
    assert commander.latest_market_view is view


async def test_state_persists_across_restart(tmp_path: Path) -> None:
    commander, engine, clock, candidates, log = await commander_with_flash(tmp_path / "r", T0)
    desk = make_desk(tmp_path, commander, FakeModel(answer(0.6, 0.25, 0.15)), clock)
    await desk.run(candidates, "scheduled")
    view = commander.latest_market_view
    assert view is not None
    # 重启：指挥官从 market_view.json 恢复，大盘台从 market_desk_state.json 恢复
    again = BattleCardCommander(
        engine,
        commander._risk,
        log,
        clock=lambda: clock[0],
        card_path=tmp_path / "r" / "battle_card.json",
        budget_path=tmp_path / "r" / "budget.json",
    )
    assert again.latest_market_view == view
    restored = make_desk(tmp_path, again, FakeModel("x"), clock)
    assert restored.state.latest == desk.state.latest
    assert restored.state.last_slot == desk.slot(clock[0])
    assert restored.due(clock[0]) is None  # 这一格跑过了，不重跑
    assert restored.owns_market(clock[0])
    clock[0] += timedelta(minutes=76)
    assert not restored.available(clock[0])
    (tmp_path / "market_desk_state.json").write_text("{bad", encoding="utf-8")
    broken = make_desk(tmp_path, again, FakeModel("x"), clock)
    assert broken.state.latest is None and not broken.owns_market(clock[0])


async def test_superseded_desk_result_does_not_overwrite(tmp_path: Path) -> None:
    commander, _engine, _clock, candidates, _log = await commander_with_flash(tmp_path / "r", T0)
    gen = commander.new_market_generation()
    commander._market_published_generation = gen + 1  # 一次更晚开始的发布已生效
    before = commander.latest_market_view
    out = commander.publish_desk_view(
        regime="x 1 2",
        direction="看涨",
        reason=DESK_SOURCE,
        pool=candidates,
        generation=gen,
        desk_id="d",
        probs={},
    )
    assert out is None and commander.latest_market_view is before


# ------------------------------------------------------------------ 主循环：翻转目标


class LoopDesk:
    def __init__(self) -> None:
        self.requests: list[str] = []
        self.runs: list[str] = []
        self.pending: str | None = None

    def request_run(self, reason: str = "regime_flip") -> None:
        self.requests.append(reason)
        self.pending = reason

    def due(self, now: datetime) -> str | None:
        return self.pending

    async def run(self, pool: Any, reason: str = "scheduled") -> None:
        self.pending = None
        self.runs.append(reason)

    def take_notice(self) -> str | None:
        return None

    def observe_prices(self, *a: Any) -> None:
        return None


@pytest.mark.parametrize("target", ["off", "desk"])
async def test_flip_target_off_and_desk_never_call_flash(tmp_path: Path, target: str) -> None:
    app = _app(tmp_path, T0)
    commander, engine, clock, candidates, _ = await research(tmp_path / "research", T0)
    app.clock = lambda: clock[0]
    app.pool = candidates
    app.m = dataclasses.replace(app.m, cards=commander)
    desk = LoopDesk()
    app.desk = desk  # type: ignore[attr-defined]

    async def fetch(symbol: str, start: datetime, end: datetime) -> Any:
        return [bar(symbol, end - timedelta(hours=1), 100), bar(symbol, end, 102)]

    fl = app.app_cfg.fast_loop
    patch_inplace(
        fl,
        fl.model_copy(
            update={
                "regime_flip_confirm": 1,
                "regime_flip_min_symbols": 1,
                "regime_flip_target": target,
            }
        ),
        HOT_FAST,
    )
    runner = Runner(app, fetch_regime_klines=fetch)
    commander.set_regime_price(lambda: 102.0)
    clock[0] += timedelta(minutes=5)
    await runner.regime_flip_tick()
    if runner._tasks:
        await asyncio.gather(*tuple(runner._tasks))
    assert engine.market_calls == 0  # 从不叫 Flash 独立刷新
    rows = records(tmp_path / "regime_flip")
    if target == "off":
        assert desk.requests == [] and desk.runs == []
        assert any("regime_flip_target = off" in r.get("blocked_reason", "") for r in rows)
        assert commander.last_regime_flip_at is None
    else:
        assert desk.requests == ["regime_flip"] and desk.runs == ["regime_flip"]
        assert any(r.get("scope") == "market_desk" and r.get("rerun") for r in rows)
        assert commander.last_regime_flip_at == clock[0]


# ------------------------------------------------------------------ 数据包


SYMS = [f"C{i}USDT" for i in range(8)]


class FakeBinance:
    """币安 / alternative.me 替身：确定的合成行情。"""

    def __init__(self, fail: set[str] | None = None) -> None:
        self.fail = fail or set()
        self.calls: list[tuple[str, Any]] = []

    def get(self, url: str, params: Any = None) -> Any:
        self.calls.append((url, params))
        path = url.rsplit("/", 1)[-1]
        if path in self.fail:
            raise RuntimeError("boom")
        now_ms = int(T0.timestamp() * 1000)
        if path == "klines":
            sym, limit = params["symbol"], params["limit"]
            k = sum(map(ord, sym)) % 7
            last = now_ms // 3_600_000 * 3_600_000 - 3_600_000
            rows = []
            for i in range(limit):
                o = last - (limit - 1 - i) * 3_600_000
                px = 100 * (1 + 0.004 * ((i * (k + 3)) % 11 - 5) / 5)
                rows.append(
                    [o, px, px * 1.01, px * 0.99, px, 1, o + 3_599_999, 1e6, 1, 4.8e5 + k * 1e3, 0]
                )
            return Resp(rows)
        if path == "premiumIndex":
            return Resp(
                [
                    {
                        "symbol": s,
                        "markPrice": "100.1",
                        "indexPrice": "100.0",
                        "lastFundingRate": "0.00012" if i % 2 else "0.00005",
                        "time": now_ms,
                    }
                    for i, s in enumerate([*SYMS, "BTCUSDT", "ETHUSDT"])
                ]
            )
        if path == "fundingInfo":
            return Resp([{"symbol": SYMS[0], "fundingIntervalHours": 4}])
        if path == "globalLongShortAccountRatio":
            return Resp(
                [
                    {
                        "longAccount": str(0.6 + 0.001 * (i % 5)),
                        "shortAccount": str(0.4 - 0.001 * (i % 5)),
                        "longShortRatio": str((0.6 + 0.001 * (i % 5)) / (0.4 - 0.001 * (i % 5))),
                        "timestamp": now_ms - (167 - i) * 3_600_000,
                    }
                    for i in range(168)
                ]
            )
        if path == "openInterestHist":
            return Resp(
                [
                    {"sumOpenInterestValue": str(1e9 * (1 + 0.001 * i)), "timestamp": now_ms}
                    for i in range(25)
                ]
            )
        raise AssertionError(url)

    def fng(self, url: str, params: Any = None) -> Any:
        if "fng" in self.fail:
            raise RuntimeError("down")
        return Resp(
            {"data": [{"value": "31", "value_classification": "Fear", "timestamp": "1793145600"}]}
        )


class Resp:
    def __init__(self, data: Any) -> None:
        self.data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.data


def test_dossier_builder_fields_sources_and_base_rates() -> None:
    fake = FakeBinance()
    b = DossierBuilder(get=fake.get, fng_get=fake.fng, exclude_symbols=["C7USDT"])
    p = pool(T0, *(entry(s) for s in SYMS))
    d = b.build(p, T0)
    assert "C7USDT" not in d.symbols and len(d.symbols) == 7
    names = {f.name for f in d.fields}
    for n in (
        "pool_ret_4h_pct",
        "pool_up_count_4h",
        "sigma_4h_pct",
        "base_rate_up_4h",
        "base_rate_up_1h",
        "funding_excess_median_bp8h",
        "btc_long_account_pct",
        "btc_long_short_log_ratio_z_7d",
        "btc_oi_chg_4h_pct",
        "fear_greed_index",
        "pool_ret_since_hour_close_pct",
        "pool_taker_buy_share_4h",
    ):
        assert n in names, n
    assert all(f.source and f.source_ts.tzinfo for f in d.fields)
    total = sum(d.value(f"base_rate_{k}_4h") or 0 for k in ("up", "flat", "down"))
    assert total == pytest.approx(1.0)
    assert "fear" not in json.dumps(d.to_json()).lower().replace("fear_greed_index", "")
    # 同一份数据，哈希稳定；第二轮只补新 K 线（limit 很小）
    assert d.sha256() == d.sha256()
    fake.calls.clear()
    b.build(p, T0 + timedelta(minutes=15))
    limits = [c[1]["limit"] for c in fake.calls if c[0].endswith("klines")]
    assert limits and max(limits) <= 3


def test_dossier_skips_optional_sources_with_reason() -> None:
    fake = FakeBinance(fail={"premiumIndex", "globalLongShortAccountRatio", "fng", "fundingInfo"})
    d = DossierBuilder(get=fake.get, fng_get=fake.fng).build(
        pool(T0, *(entry(s) for s in SYMS)), T0
    )
    assert "funding" in d.missing and "btc_long_short_account" in d.missing
    assert "fear_greed_index" in d.missing and "news" in d.missing
    assert d.value("pool_ret_4h_pct") is not None


def test_dossier_rate_limit_propagates() -> None:
    from jiaoyi.slow import binance_gate

    def banned(url: str, params: Any = None) -> Any:
        raise binance_gate.RateLimited(418, time.time() + 60)

    with pytest.raises(binance_gate.RateLimited):
        DossierBuilder(get=banned).build(pool(T0, *(entry(s) for s in SYMS)), T0)


async def test_rate_limited_run_is_skipped_and_slot_not_consumed(tmp_path: Path) -> None:
    from jiaoyi.slow import binance_gate

    class Banned(FakeBuilder):
        def build(self, pool: SymbolPool, now: datetime) -> Dossier:
            raise binance_gate.RateLimited(418, time.time() + 60)

    clock = [T0]
    model = FakeModel(answer(0.6, 0.2, 0.2))
    desk = make_desk(tmp_path, FakePublisher(), model, clock, builder=Banned())
    run = await desk.run(pool(T0, entry("AAUSDT")), "scheduled")
    assert run.status == "skipped" and model.prompts == []
    assert desk.due(T0) == "scheduled"
