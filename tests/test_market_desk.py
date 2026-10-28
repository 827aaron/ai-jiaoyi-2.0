"""大盘台（slow/market_desk.py）：全部离线替身，不调真模型、不连币安。"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
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
from jiaoyi.slow.desk_intra import INTRA_FIELDS, compose_intra
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
    intra_block,
    intra_status,
    mirror_bias,
    mirror_selected,
    normalize_probs,
    parse_sample,
    render_prompt,
    swap_words,
)
from jiaoyi.slow.market_dossier import (
    BARS_15M,
    DEEP_PER_RUN,
    SHALLOW_BARS,
    Dossier,
    DossierBuilder,
    DossierField,
)
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


def notices(desk: MarketDesk) -> list[str]:
    out = []
    while (n := desk.take_notice()) is not None:
        out.append(n)
    return out


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
    assert not fair.state.mirror_flag and not any("偏置" in n for n in notices(fair))

    biased = make_desk(
        tmp_path / "biased", FakePublisher(), FakeModel(answer(0.2, 0.3, 0.5)), clock,
        mirror_test_rate=1.0,
    )  # fmt: skip
    for k in range(3):
        clock[0] = T0 + timedelta(minutes=15 * k)
        await biased.run(pool(clock[0], entry("AAUSDT")), "scheduled")
    assert biased.state.mirror_flag
    assert any("偏置" in n for n in notices(biased))
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
    assert any("日上限" in n for n in notices(desk))
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


async def test_extra_run_needs_time_separated_confirmation(tmp_path: Path) -> None:
    commander, _, clock, candidates, _ = await commander_with_flash(tmp_path / "r", T0)
    script = Switch(answer(0.6, 0.25, 0.15))
    desk = make_desk(tmp_path, commander, FakeModel(script), clock)
    await desk.run(candidates, "scheduled")  # 10:02 接管，看涨
    script.text = answer(0.15, 0.25, 0.6)
    clock[0] = T0 + timedelta(minutes=14)  # 10:16 急动加跑
    assert (await desk.run(candidates, "move")).published is None
    clock[0] = T0 + timedelta(minutes=15)  # 10:17 定时轮：和加跑只隔 1 分钟，不算独立确认
    assert (await desk.run(candidates, "scheduled")).published is None
    clock[0] = T0 + timedelta(minutes=30)  # 10:32：离加跑 16 分钟 ≥ max(30/2, 15)
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
    nominated = card.symbols[0]
    token = nominated.research_token()

    async def pick(c: Any, n: int, prev: Any, pool: Any = ()) -> PickResult:
        return PickResult(
            "Flash：偏弱 BTC -1%",
            [Pick(e.symbol, "量 +10%") for e in c if e.symbol != nominated.symbol],
            cost_usd=0.01,
            regime_direction="看跌",
            review_candidates=[
                MarketReviewCandidate(
                    symbol=nominated.symbol,
                    research_token=token,
                    reason="新数字",
                    evidence=[
                        MarketReviewEvidence(field="filters.quote_volume_24h_usdt", value=1e8)
                    ],
                )
            ],
            reviews_valid=True,
        )

    engine.pick = pick  # type: ignore[method-assign]
    clock[0] += timedelta(minutes=30)
    await commander.refresh(candidates, "manual")
    assert commander._market_reviews == {}
    recs = [
        json.loads(x) for p in (tmp_path / "r").rglob("*.jsonl") for x in p.read_text().splitlines()
    ]
    assert any(r.get("output", {}).get("market_review") == "record_only" for r in recs)


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


async def test_owner_switch_is_edge_triggered_logged_and_no_ping_pong(tmp_path: Path) -> None:
    commander, _engine, clock, candidates, _log = await commander_with_flash(tmp_path / "r", T0)
    log = JsonlDecisionLog(tmp_path / "desk_log")
    desk = make_desk(tmp_path, commander, FakeModel(answer(0.6, 0.25, 0.15)), clock, log=log)
    await desk.run(candidates, "scheduled")
    assert desk.state.owner == "desk" and any("接管" in n for n in notices(desk))
    clock[0] += timedelta(minutes=80)
    for _ in range(5):  # 反复查只切一次、只记一次
        assert not desk.owns_market(clock[0])
        desk.check_owner(clock[0])
    changes = [
        r["output"]["market_desk"]["owner_change"]
        for r in desk_records(tmp_path / "desk_log")
        if "owner_change" in r["output"]["market_desk"]
    ]
    assert [c["to"] for c in changes] == ["desk", "flash"]
    assert [n for n in notices(desk) if "Flash" in n] and notices(desk) == []
    assert [r["to"] for r in log_rows(tmp_path) if r.get("event") == "owner_change"] == [
        "desk",
        "flash",
    ]
    await commander.refresh(candidates, "manual")  # 回退期间 Flash 发布
    flash = commander.latest_market_view
    assert flash is not None and flash.source == "scheduled_market"
    # 大盘台恢复，但接管发布恰好被更晚的发布取代：归属仍切回大盘台，Flash 不能紧接着再发
    original = commander.publish_desk_view
    commander.publish_desk_view = lambda **kw: None  # type: ignore[method-assign]
    clock[0] += timedelta(minutes=15)
    resumed = await desk.run(candidates, "scheduled")
    assert resumed.status == "ok" and resumed.published is None
    assert desk.owns_market(clock[0])
    clock[0] += timedelta(minutes=5)
    await commander.refresh(candidates, "manual")
    assert commander.latest_market_view is flash  # 没有再发一版 Flash（不来回抢）
    commander.publish_desk_view = original  # type: ignore[method-assign]
    clock[0] += timedelta(minutes=10)
    took = await desk.run(candidates, "scheduled")
    assert took.published is not None and took.published.source == DESK_SOURCE


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
        self.breadth: list[tuple[Any, ...]] = []

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

    def observe_breadth(self, *a: Any) -> bool:
        self.breadth.append(a)
        return True


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
    # 翻转检查刚取到的 1 分钟线顺手交给大盘台刷新短线现状（不另发请求）
    assert [(b[0], b[3]["BTCUSDT"]) for b in desk.breadth] == [(clock[0], pytest.approx(2.0))]
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


def wave(sym: str, t_ms: int) -> float:
    """默认合成价：每个币一条不同周期的正弦，只跟时间有关（不同 limit 拉到的同一根线一致）。"""
    k = sum(map(ord, sym)) % 7
    return 100 * (1 + 0.004 * math.sin(t_ms / (3_600_000 * (k + 3))))


class FakeBinance:
    """币安 / alternative.me 替身：按时间连续的合成行情，K 线带正在走的那一根。"""

    def __init__(
        self,
        fail: set[str] | None = None,
        price: Callable[[str, int], float] = wave,
        premium_lag_min: float = 0.0,
    ) -> None:
        self.fail = fail or set()
        self.price = price
        self.premium_lag_ms = int(premium_lag_min * 60_000)
        self.calls: list[tuple[str, Any]] = []
        self.limits: list[tuple[str, int]] = []
        self.now_ms = int(T0.timestamp() * 1000)

    def get(self, url: str, params: Any = None) -> Any:
        self.calls.append((url, params))
        path = url.rsplit("/", 1)[-1]
        if path in self.fail:
            raise RuntimeError("boom")
        now_ms = self.now_ms
        if path == "klines":
            sym, limit = params["symbol"], params["limit"]
            self.limits.append((params["interval"], limit))
            step = {"1h": 3_600_000, "15m": 900_000}[params["interval"]]
            k = sum(map(ord, sym)) % 7
            forming = now_ms // step * step
            rows = []
            for i in range(limit):
                o = forming - (limit - 1 - i) * step
                end = min(o + step, now_ms)
                po, pc = self.price(sym, o), self.price(sym, end)
                hi, lo = max(po, pc) * 1.001, min(po, pc) * 0.999
                rows.append([o, po, hi, lo, pc, 1, o + step - 1, 1e6, 1, 4.8e5 + k * 1e3, 0])
            return Resp(rows)
        if path == "premiumIndex":
            return Resp(
                [
                    {
                        "symbol": s,
                        "markPrice": "100.1",
                        "indexPrice": "100.0",
                        "lastFundingRate": "0.00012" if i % 2 else "0.00005",
                        "time": now_ms - self.premium_lag_ms,
                    }
                    for i, s in enumerate([*SYMS, "BTCUSDT", "ETHUSDT"])
                ]
            )
        if path == "fundingInfo":
            return Resp([{"symbol": SYMS[0], "fundingIntervalHours": 4}])
        period = {"1h": 3_600_000, "5m": 300_000}.get((params or {}).get("period", ""), 0)
        if path in ("globalLongShortAccountRatio", "openInterestHist"):
            n = params["limit"]
            last = now_ms // period * period - period  # 最新一段（已结束）的开始时间
            rows = []
            for i in range(n):
                ts = last - (n - 1 - i) * period
                a = 0.6 + 0.001 * ((ts // period) % 5)
                rows.append(
                    {
                        "longAccount": str(a),
                        "shortAccount": str(1 - a),
                        "longShortRatio": str(a / (1 - a)),
                        "sumOpenInterestValue": str(1e9 * (1 + 1e-6 * (ts // period % 1000))),
                        "timestamp": ts,
                    }
                )
            return Resp(rows)
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
        "pool_ret_1h_pct",
        "pool_taker_buy_share_4h",
    ):
        assert n in names, n
    assert all(f.source and f.source_ts.tzinfo for f in d.fields)
    total = sum(d.value(f"base_rate_{k}_4h") or 0 for k in ("up", "flat", "down"))
    assert total == pytest.approx(1.0)
    assert "fear" not in json.dumps(d.to_json()).lower().replace("fear_greed_index", "")
    # 同一份数据，哈希稳定；第二轮只补新 K 线（limit 很小）
    assert d.sha256() == d.sha256()
    assert {"pool_ret_15m_pct", "pool_up_count_15m", "btc_ret_15m_pct"} <= names
    assert {"pool_ret_30m_pct", "pool_ret_24h_pct", "btc_minus_pool_ret_24h_pct"} <= names
    # 同一小时里第二轮：没有新的 1h 收盘就不请求 1h 线（只有剩下没补全的长历史，最多 8 个）
    fake.limits.clear()
    fake.now_ms += 15 * 60_000
    b.build(p, T0 + timedelta(minutes=15))
    hourly = [n for iv, n in fake.limits if iv == "1h"]
    assert all(n == b.need_bars for n in hourly) and len(hourly) <= DEEP_PER_RUN
    assert [n for iv, n in fake.limits if iv == "15m"] == [BARS_15M] * 9
    fake.limits.clear()
    fake.now_ms += 60 * 60_000  # 新的一根 1h 收盘了：只补几根
    b.build(p, T0 + timedelta(minutes=75))
    assert [n for iv, n in fake.limits if iv == "1h"] and max(
        n for iv, n in fake.limits if iv == "1h"
    ) <= 4


def test_first_run_backfill_is_spread_over_cycles() -> None:
    syms = [f"D{i}USDT" for i in range(30)]
    fake = FakeBinance()
    b = DossierBuilder(get=fake.get, fng_get=fake.fng)
    p = pool(T0, *(entry(s) for s in syms))
    d = b.build(p, T0)
    deep = [n for iv, n in fake.limits if iv == "1h" and n > SHALLOW_BARS]
    assert len(deep) <= DEEP_PER_RUN  # 启动时大请求最多 8 个
    assert all(n <= SHALLOW_BARS for iv, n in fake.limits if iv == "1h") or deep
    assert all(n <= BARS_15M for iv, n in fake.limits if iv == "15m")
    assert "回填中" in d.missing["base_rate_4h"] and d.value("sigma_4h_pct") is not None
    for k in range(1, 6):
        fake.now_ms += 15 * 60_000
        d = b.build(p, T0 + timedelta(minutes=15 * k))
    assert d.value("base_rate_up_4h") is not None and "base_rate_4h" not in d.missing


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


# ------------------------------------------------------------------ 不能有延后信息


def test_windows_roll_to_run_time_not_last_closed_hour() -> None:
    """10:54 跑、10:00 起一路上涨：「近 1 小时」是 09:54–10:54，不是 09:00–10:00 那根收盘线。"""
    at = datetime(2026, 10, 28, 10, 54, tzinfo=UTC)
    rise_from = int(datetime(2026, 10, 28, 10, 0, tzinfo=UTC).timestamp() * 1000)

    def price(sym: str, t_ms: int) -> float:
        return 100.0 if t_ms <= rise_from else 100.0 + 2.0 * (t_ms - rise_from) / 3_240_000

    fake = FakeBinance(price=price)
    fake.now_ms = int(at.timestamp() * 1000)
    d = DossierBuilder(get=fake.get, fng_get=fake.fng).build(
        pool(at, *(entry(s) for s in SYMS)), at
    )
    assert d.value("pool_ret_1h_pct") == pytest.approx(2.0, abs=1e-6)  # 09:54 → 10:54
    assert d.value("btc_ret_1h_pct") == pytest.approx(2.0, abs=1e-6)
    assert d.value("pool_up_count_1h") == len(SYMS)
    assert d.value("pool_ret_15m_pct") == pytest.approx((102 / (100 + 2 * 39 / 54) - 1) * 100)
    f = d.get("pool_ret_1h_pct")
    assert f is not None and f.source_ts == at  # as_of = 运行时刻
    prompt = render_prompt(d, "up_first", 0.5, "c")
    assert "Run time (as_of): 2026-10-28 10:54:00Z" in prompt
    assert (
        "pool_ret_1h_pct = 2.000 [%; source klines_15m_live; as_of 2026-10-28 10:54:00Z]" in prompt
    )


def test_stale_live_inputs_are_dropped_not_shown() -> None:
    fake = FakeBinance(premium_lag_min=10)
    p = pool(T0, *(entry(s) for s in SYMS))
    d = DossierBuilder(get=fake.get, fng_get=fake.fng, max_input_age_min=5).build(p, T0)
    assert d.get("funding_excess_median_bp8h") is None
    assert "旧 10.0 分钟" in d.missing["funding_excess_median_bp8h"]
    assert d.get("pool_ret_1h_pct") is not None  # 现价是运行时刻的
    assert d.get("btc_long_account_pct") is not None  # 5 分钟统计在容差内
    assert d.get("sigma_4h_pct") is not None and d.get("fear_greed_index") is not None
    off = DossierBuilder(get=fake.get, fng_get=fake.fng, max_input_age_min=0).build(p, T0)
    assert off.get("funding_excess_median_bp8h") is not None  # 0 = 不查


def test_btc_minus_pool_uses_current_pool_only() -> None:
    fake = FakeBinance()
    b = DossierBuilder(get=fake.get, fng_get=fake.fng)
    b.build(pool(T0, *(entry(s) for s in [*SYMS, "Z1USDT", "Z2USDT", "Z3USDT"])), T0)
    d = b.build(pool(T0, *(entry(s) for s in SYMS[:6])), T0)
    btc, basket = d.value("btc_ret_24h_pct"), d.value("pool_ret_24h_pct")
    assert btc is not None and basket is not None
    assert d.value("btc_minus_pool_ret_24h_pct") == pytest.approx(btc - basket)


def test_kline_cache_survives_restart(tmp_path: Path) -> None:
    cache = tmp_path / "klines_1h.json"
    fake = FakeBinance()
    p = pool(T0, *(entry(s) for s in SYMS))
    first = DossierBuilder(get=fake.get, fng_get=fake.fng, cache_path=cache)
    assert first.build(p, T0).has_prior and cache.is_file()
    fake.limits.clear()
    fake.now_ms += 15 * 60_000
    again = DossierBuilder(get=fake.get, fng_get=fake.fng, cache_path=cache)
    d = again.build(p, T0 + timedelta(minutes=15))
    assert d.has_prior  # 重启后不用重新回填就有先验
    hourly = [n for iv, n in fake.limits if iv == "1h"]
    assert SHALLOW_BARS not in hourly  # 缓存里有的不重拉
    assert len(hourly) <= 2 and set(hourly) <= {again.need_bars}  # 只剩上次没轮到的两个补长历史
    cache.write_text("{broken", encoding="utf-8")
    assert not DossierBuilder(get=fake.get, cache_path=cache)._bars


async def test_run_without_prior_is_record_only(tmp_path: Path) -> None:
    commander, _engine, clock, candidates, _log = await commander_with_flash(tmp_path / "r", T0)
    flash = commander.latest_market_view
    no_prior = dataclasses.replace(
        make_dossier(),
        fields=tuple(f for f in make_dossier().fields if not f.name.startswith("base_rate")),
    )
    desk = make_desk(
        tmp_path,
        commander,
        FakeModel(answer(0.6, 0.25, 0.15)),
        clock,
        builder=FakeBuilder(no_prior),
    )
    run = await desk.run(candidates, "scheduled")
    assert run.status == "ok" and run.reason == "prior_missing" and run.published is None
    assert commander.latest_market_view is flash  # 不接管
    assert desk.state.owner == "flash" and desk.state.latest is None
    assert not desk.owns_market(clock[0])
    row = log_rows(tmp_path)[-1]
    assert row["prior_missing"] is True and row["probs_4h"]["up"] == pytest.approx(0.6)


@pytest.mark.parametrize("order", ["up_first", "down_first"])
def test_two_sided_notes_alternate_with_round_order(order: Any) -> None:
    d = DossierBuilder(get=FakeBinance().get, fng_get=FakeBinance().fng).build(
        pool(T0, *(entry(s) for s in SYMS)), T0
    )
    text = render_prompt(d, order, 0.5, "c")
    for pos, neg in (
        ("positive = longs pay shorts", "negative = shorts pay longs"),
        ("positive = more long than usual", "negative = more short than usual"),
    ):
        assert pos in text and neg in text
        assert (text.index(pos) < text.index(neg)) == (order == "up_first")


def test_desk_flip_target_requires_desk_on(tmp_path: Path) -> None:
    import argparse

    from jiaoyi.cli import _load

    off = load_slow_config(tmp_path / "missing.toml")
    assert off.market_desk.mode == "off"
    assert "mode 不是 off" in (market_desk_conflict(off, "desk") or "")
    assert market_desk_conflict(off, "flash_market_only") is None
    root = Path(__file__).resolve().parents[1] / "config"
    app = tmp_path / "app.toml"
    text = (root / "app.example.toml").read_text(encoding="utf-8")
    app.write_text(
        text.replace('regime_flip_target = "flash_market_only"', 'regime_flip_target = "desk"'),
        encoding="utf-8",
    )
    args = argparse.Namespace(
        risk=str(root / "risk.example.toml"), app=str(app), slow=str(tmp_path / "missing.toml")
    )
    with pytest.raises(ConfigError, match="mode 不是 off"):
        _load(args)


async def test_review_context_hides_desk_text_from_flash(tmp_path: Path) -> None:
    commander, _engine, clock, candidates, _log = await commander_with_flash(tmp_path / "r", T0)
    desk = make_desk(tmp_path, commander, FakeModel(answer(0.6, 0.25, 0.15)), clock)
    await desk.run(candidates, "scheduled")
    clock[0] += timedelta(minutes=30)
    await commander.refresh(candidates, "manual")  # 各币研究盖上了大盘台的观点
    view = commander.latest_market_view
    assert view is not None and view.source == DESK_SOURCE
    context = commander.market_review_context(candidates)
    assert context
    assert all(c.source_regime and c.source_regime.startswith("旧研究背景") for c in context)
    assert all(view.regime not in (c.source_regime or "") for c in context)


def test_panel_market_node_excludes_desk_records(tmp_path: Path) -> None:
    from jiaoyi.contracts import DecisionRecord, Outcome
    from jiaoyi.panel.files import PanelFiles
    from jiaoyi.panel.pages import _market_records

    log = JsonlDecisionLog(tmp_path)
    for i in range(3):
        log.write(
            DecisionRecord(
                record_id=f"d{i}",
                ts=T0,
                role=Role.COMMANDER,
                outcome=Outcome.INFO,
                summary="大盘台",
                output={"market_desk": {}},
            )
        )
    log.write(
        DecisionRecord(
            record_id="m",
            ts=T0,
            role=Role.COMMANDER,
            outcome=Outcome.ACTION,
            summary="独立大盘更新 v2",
            output={"market_view": {}},
        )
    )
    items = _market_records(PanelFiles(tmp_path, "UTC"), T0 + timedelta(minutes=1))
    assert [i.text for i in items] == ["独立大盘更新 v2"]


async def test_credit_error_raises_alarm(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from jiaoyi.slow.market_desk import DeskHTTPError

    class Broke:
        async def complete(self, prompt: str) -> ModelReply:
            raise DeskHTTPError(402, "OpenRouter HTTP 402: insufficient credits")

    alarms: list[int] = []
    clock = [T0]
    desk = MarketDesk(
        cfg(),
        FakePublisher(),
        Broke(),
        JsonlDecisionLog(tmp_path / "log"),
        tmp_path,
        builder=FakeBuilder(),  # type: ignore[arg-type]
        clock=lambda: clock[0],
        balance=lambda: 100.0,
        banned=lambda: 0.0,
        credit_alarm=lambda: alarms.append(1),
    )
    run = await desk.run(pool(T0, entry("AAUSDT")), "scheduled")
    assert run.status == "failed" and alarms
    with caplog.at_level("WARNING"):
        MarketDesk(
            cfg(),
            FakePublisher(),
            Broke(),
            JsonlDecisionLog(tmp_path / "log"),
            tmp_path / "x",
            builder=FakeBuilder(),  # type: ignore[arg-type]
        )
    assert "自停不生效" in caplog.text


# ------------------------------------------------------------------ 4 小时之内（publish_intra）


def intra_dossier(now: datetime = T0, ret1h: float = 0.768) -> Dossier:
    """make_dossier 加上「短线现状」用的数；BTC 1h 的 as_of 早一分钟（as_of 取最旧的）。"""
    d = make_dossier(now)

    def f(
        name: str, value: float, mirror: Any = "neg", partner: str | None = None, ago: int = 0
    ) -> DossierField:
        ts = now - timedelta(minutes=ago)
        return DossierField(name, value, "%", "n", "klines_15m_live", ts, mirror, partner, 3)

    extra = (
        f("pool_ret_15m_pct", 0.0186),
        f("pool_up_count_15m", 19.0, "pair", "pool_down_count_15m"),
        f("pool_down_count_15m", 19.0, "pair", "pool_up_count_15m"),
        f("pool_ret_1h_pct", ret1h),
        f("pool_up_count_1h", 34.0, "pair", "pool_down_count_1h"),
        f("pool_down_count_1h", 5.0, "pair", "pool_up_count_1h"),
        f("btc_ret_15m_pct", 0.0486),
        f("btc_ret_1h_pct", 0.383, ago=1),
    )
    return dataclasses.replace(d, fields=d.fields + extra)


class IntraBuilder:
    """每轮按开跑时刻造数据包（as_of 跟着走），``ret1h`` 可改。"""

    def __init__(self) -> None:
        self.ret1h = 0.768

    def build(self, pool: SymbolPool, now: datetime) -> Dossier:
        return intra_dossier(now, self.ret1h)


H1 = Probs(0.312, 0.459, 0.229)
UP_TEXT = (
    "未来 1 小时概率（大盘台 10:02 UTC 一轮）：涨 31% / 平 46% / 跌 23%。"
    "短线现状（截至 10:01 UTC）："
    "等权池 15 分钟 +0.02%（涨 19 / 跌 19 个），1 小时 +0.77%（涨 34 / 跌 5 个）；"
    "BTC 15 分钟 +0.05%，1 小时 +0.38%；1 小时成交里主动买入 47.0% / 主动卖出 53.0%。"
)


def _flip_signs(text: str) -> str:
    return re.sub(r"([+-])(?=\d)", lambda m: "-" if m.group(1) == "+" else "+", text)


def test_intra_text_has_1h_probs_and_short_term_numbers() -> None:
    block = intra_block(intra_dossier(), H1, "up_first", "c")
    assert block["as_of"] == (T0 - timedelta(minutes=1)).isoformat()
    assert set(block["values"]) == set(INTRA_FIELDS)
    assert compose_intra(block, T0 + timedelta(minutes=1), 5.0) == UP_TEXT
    down = compose_intra(intra_block(intra_dossier(), H1, "down_first", "c"), T0, 5.0)
    assert down == (
        "未来 1 小时概率（大盘台 10:02 UTC 一轮）：跌 23% / 平 46% / 涨 31%。"
        "短线现状（截至 10:01 UTC）："
        "等权池 15 分钟 +0.02%（跌 19 / 涨 19 个），1 小时 +0.77%（跌 5 / 涨 34 个）；"
        "BTC 15 分钟 +0.05%，1 小时 +0.38%；1 小时成交里主动卖出 53.0% / 主动买入 47.0%。"
    )


def test_intra_text_is_neutral_under_swap_and_mirror() -> None:
    assert frozenset(("买入", "卖出")) in {frozenset(p) for p in SWAP_PAIRS}
    d = intra_dossier()
    for order, other in (("up_first", "down_first"), ("down_first", "up_first")):
        text = compose_intra(intra_block(d, H1, order, "c"), T0, 5.0)
        mirrored = intra_block(d.mirror(), Probs(H1.down, H1.flat, H1.up), other, "c")
        # 数据涨跌取反、顺序对调后，文字正好是原文多空词对调、正负号对调
        assert compose_intra(mirrored, T0, 5.0) == _flip_signs(swap_words(text))
        assert_paired([s for s in re.split("[。；]", text) if s])


def test_intra_short_term_line_is_refused_when_stale() -> None:
    block = intra_block(intra_dossier(), H1, "up_first", "c")
    stale = compose_intra(block, T0 + timedelta(minutes=5), 5.0)  # as_of 早 1 分钟：已 6 分钟
    assert stale == (
        "未来 1 小时概率（大盘台 10:02 UTC 一轮）：涨 31% / 平 46% / 跌 23%。"
        "短线现状：缺失（截至 10:01 UTC 的数据已过 6 分钟，上限 5 分钟）。"
    )
    assert "等权池" in compose_intra(block, T0 + timedelta(minutes=5), 0.0)  # 0 = 不查
    late = compose_intra(block, T0 + timedelta(minutes=61), 0.0)
    assert late.startswith("未来 1 小时概率：缺失（10:02 UTC 那一轮已过 1 小时）。")
    missing = dict(block, values={k: v for k, v in block["values"].items() if "btc" not in k})
    assert "BTC 15 分钟 缺失，1 小时 缺失" in compose_intra(missing, T0, 5.0)


async def test_publish_intra_reads_latest_run_not_last_published_view(tmp_path: Path) -> None:
    commander, engine, clock, candidates, _ = await commander_with_flash(tmp_path / "r", T0)
    builder = IntraBuilder()
    desk = make_desk(
        tmp_path,
        commander,
        FakeModel(answer(0.6, 0.25, 0.15, h1=(0.312, 0.459, 0.229))),
        clock,
        builder=builder,  # type: ignore[arg-type]
        publish_intra=True,
    )
    await desk.run(candidates, "scheduled")
    view = commander.latest_market_view
    assert view is not None and view.source == DESK_SOURCE
    assert "短线现状" not in view.regime and "1 小时概率" not in view.regime  # 发布的观点不动
    latest = desk.state.latest
    assert latest is not None and latest["intra"]["max_age_min"] == 5.0
    text = desk.intra_text(clock[0])
    assert text is not None and text.endswith("。") and "（截至 10:01 UTC）" in text
    assert "涨 31%" in text and "+0.77%" in text
    assert commander.market_intra(clock[0], view) == text
    flash_view = view.model_copy(update={"source": "scheduled_market"})
    assert commander.market_intra(clock[0], flash_view) is None  # 只接在大盘台观点后面
    await commander._analyze_one(entry("AAUSDT"), view.regime, market_view=view)
    assert engine.symbol_calls[-1] == ("AAUSDT", f"{view.regime} {text}")

    # 下一轮方向没变不发布，但短线现状换成这一轮的数
    clock[0] += timedelta(minutes=15)
    builder.ret1h = -0.412
    await desk.run(candidates, "scheduled")
    assert commander.latest_market_view == view
    newer = desk.intra_text(clock[0])
    assert newer is not None and "-0.41%" in newer and "（截至 10:16 UTC）" in newer
    clock[0] += timedelta(minutes=6)
    stale = desk.intra_text(clock[0])
    assert stale is not None and "短线现状：缺失" in stale and "-0.41%" not in stale
    restored = make_desk(tmp_path, commander, FakeModel("x"), clock, publish_intra=True)
    assert restored.intra_text(clock[0]) == stale  # 重启后从状态文件接着用


async def test_publish_intra_off_is_byte_identical(tmp_path: Path) -> None:
    assert MarketDeskSection().publish_intra is False
    commander, engine, clock, candidates, _ = await commander_with_flash(tmp_path / "r", T0)
    model = FakeModel(answer(0.6, 0.25, 0.15))
    off_builder: Any = IntraBuilder()
    desk = make_desk(tmp_path, commander, model, clock, builder=off_builder)
    on_model = FakeModel(answer(0.6, 0.25, 0.15))
    on_builder: Any = IntraBuilder()
    on = make_desk(
        tmp_path / "on", FakePublisher(), on_model, clock, builder=on_builder, publish_intra=True
    )
    await desk.run(candidates, "scheduled")
    await on.run(candidates, "scheduled")
    assert model.prompts == on_model.prompts  # 模型看到的提示词一字不差，也不多问
    view = commander.latest_market_view
    assert view is not None and desk.state.latest is not None
    assert "intra" not in desk.state.latest and "intra" not in log_rows(tmp_path)[-1]
    assert desk.intra_text(clock[0]) is None and commander.market_intra(clock[0], view) is None
    await commander._analyze_one(entry("AAUSDT"), view.regime, market_view=view)
    assert engine.symbol_calls[-1] == ("AAUSDT", view.regime)
    from jiaoyi.slow import ta_prompts as tp

    p = tp.PromptParams.for_hold("4h")
    plain = tp.meeting_note(p, "AA-USD", "AAUSDT", "R", "why")
    assert plain == tp.meeting_note(p, "AA-USD", "AAUSDT", "R", "why", intra="")
    assert "said: R. This coin was picked because: why." in plain
    long_regime = "大" * 1000
    note = tp.meeting_note(p, "AA-USD", "AAUSDT", long_regime, "why", intra=UP_TEXT)
    assert f"…. Market desk, within 4 hours (latest desk run): {UP_TEXT} This coin" in note


def test_keeper_snapshot_and_panel_carry_intra(tmp_path: Path) -> None:
    from jiaoyi.contracts import MarketView
    from jiaoyi.gate.questions import hold_snapshot
    from jiaoyi.panel.files import PanelFiles
    from jiaoyi.panel.overview import _desk_latest
    from jiaoyi.panel.pages import build_desk
    from tests.fastloop_helpers import card, features
    from tests.test_keeper_patrol import position

    view = MarketView(
        market_view_id="v",
        version=3,
        observed_at=T0,
        regime="4h 中性",
        regime_direction="中性",
        source=DESK_SOURCE,
    )
    pos, feat, crd = position(T0), features(T0), card(T0)
    snap = json.loads(hold_snapshot(pos, feat, crd, T0, current_market=view, market_intra=UP_TEXT))
    assert snap["current_market"]["within_4h"] == UP_TEXT
    plain = json.loads(hold_snapshot(pos, feat, crd, T0, current_market=view))
    assert "within_4h" not in plain["current_market"]

    block = {**intra_block(intra_dossier(), H1, "up_first", "c"), "max_age_min": 5.0}
    state = {
        "latest": {
            "t0": T0.isoformat(),
            "probs_4h": {"up": 0.32, "flat": 0.437, "down": 0.243},
            "probs_1h": H1.as_dict(),
            "intra": block,
        },
        "last_run_at": T0.isoformat(),
    }
    (tmp_path / "market_desk_state.json").write_text(json.dumps(state), encoding="utf-8")
    got = _desk_latest(tmp_path, T0 + timedelta(minutes=1))
    assert got is not None and got.intra == UP_TEXT and got.probs_1h == H1.as_dict()
    assert got.probs_4h is not None and got.probs_4h["up"] == 0.32
    stale = _desk_latest(tmp_path, T0 + timedelta(minutes=10))
    assert stale is not None and "短线现状：缺失" in stale.intra
    info = build_desk(PanelFiles(tmp_path, "UTC"), T0, ("replace", 15, 2))
    assert info.probs_1h == H1.as_dict() and info.probs is not None
    del state["latest"]["intra"]
    (tmp_path / "market_desk_state.json").write_text(json.dumps(state), encoding="utf-8")
    assert _desk_latest(tmp_path, T0) is None  # 没开 publish_intra：面板不多显示


REFRESH_SYMS = ["AAUSDT", "BBUSDT", "CCUSDT", "DDUSDT", "EEUSDT", "PAXGUSDT"]


def refresh_rets(sign: float = 1.0) -> tuple[dict[str, float], dict[str, float]]:
    r15 = {"AAUSDT": 0.2, "BBUSDT": -0.1, "CCUSDT": 0.3, "DDUSDT": 0.1, "EEUSDT": -0.2}
    r60 = {"AAUSDT": -0.5, "BBUSDT": -0.3, "CCUSDT": 0.4, "DDUSDT": -0.6, "EEUSDT": -0.2}
    r15 |= {"PAXGUSDT": 9.0, "BTCUSDT": 0.12}  # PAXG 不在大盘台的篮子里
    r60 |= {"PAXGUSDT": 9.0, "BTCUSDT": -0.25}
    return (
        {k: sign * v for k, v in r15.items()},
        {k: sign * v for k, v in r60.items()},
    )


async def test_short_term_line_refreshes_every_minute_between_runs(tmp_path: Path) -> None:
    clock = [T0]
    desk = make_desk(
        tmp_path,
        FakePublisher(),
        FakeModel(answer(0.6, 0.25, 0.15, h1=(0.312, 0.459, 0.229))),
        clock,
        builder=IntraBuilder(),  # type: ignore[arg-type]
        publish_intra=True,
    )
    await desk.run(pool(T0, entry("AAUSDT")), "scheduled")
    r15, r60 = refresh_rets()
    assert not desk.observe_breadth(
        T0 - timedelta(minutes=2), REFRESH_SYMS, r15, r60
    )  # 不比这一轮新
    at = T0 + timedelta(minutes=3)  # 10:05
    assert desk.observe_breadth(at, REFRESH_SYMS, r15, r60)
    clock[0] = at + timedelta(seconds=20)
    text = desk.intra_text(clock[0])
    # 这一轮（10:02Z）的顺序是 down_first：刷新后的现状也照这一轮的顺序
    assert text == (
        "未来 1 小时概率（大盘台 10:02 UTC 一轮）：跌 23% / 平 46% / 涨 31%。"
        "短线现状（截至 10:05 UTC）：等权池 15 分钟 +0.06%（跌 2 / 涨 3 个），"
        "1 小时 -0.24%（跌 4 / 涨 1 个）；BTC 15 分钟 +0.12%，1 小时 -0.25%；"
        "1 小时成交里主动卖出 53.0% / 主动买入 47.0%（截至 10:01 UTC）。"
    )
    # 不到 intra_refresh_s 不重算；到了就换
    assert not desk.observe_breadth(at + timedelta(seconds=30), REFRESH_SYMS, r15, r60)
    assert desk.observe_breadth(at + timedelta(minutes=1), REFRESH_SYMS, *refresh_rets(-1.0))
    clock[0] = at + timedelta(minutes=1, seconds=10)
    later = desk.intra_text(clock[0])
    assert later is not None and "截至 10:06 UTC" in later and "+0.24%" in later
    # 面板读的是落盘的同一份
    from jiaoyi.panel.overview import _desk_latest

    panel = _desk_latest(tmp_path, clock[0])
    assert panel is not None and panel.intra == later
    # 买卖占比只有每轮那份：旧过上限就单独写缺失，其余照常
    clock[0] = T0 + timedelta(minutes=7)
    assert desk.observe_breadth(T0 + timedelta(minutes=7), REFRESH_SYMS, r15, r60)
    text = desk.intra_text(clock[0])
    assert text is not None and "截至 10:09 UTC" in text
    assert "1 小时主动买卖占比缺失（只在大盘台每轮取，上次截至 10:01 UTC）" in text
    # 刷新停了（比如翻转检查关了）：过了上限照样写缺失
    clock[0] = T0 + timedelta(minutes=13)
    stale = desk.intra_text(clock[0])
    assert stale is not None and "短线现状：缺失（截至 10:09 UTC 的数据已过 6 分钟" in stale
    # 新一轮：用这一轮的数，旧的刷新作废
    await desk.run(pool(clock[0], entry("AAUSDT")), "scheduled")
    latest = desk.state.latest
    assert latest is not None and "status" not in latest["intra"]


async def test_refresh_zero_keeps_desk_run_only(tmp_path: Path) -> None:
    clock = [T0]
    builder: Any = IntraBuilder()
    desk = make_desk(
        tmp_path,
        FakePublisher(),
        FakeModel(answer(0.6, 0.25, 0.15)),
        clock,
        builder=builder,
        publish_intra=True,
        intra_refresh_s=0,
    )
    await desk.run(pool(T0, entry("AAUSDT")), "scheduled")
    before = desk.intra_text(T0)
    assert not desk.observe_breadth(T0 + timedelta(minutes=1), REFRESH_SYMS, *refresh_rets())
    assert desk.intra_text(T0) == before and "status" not in desk.state.latest["intra"]  # type: ignore[index]
    off: Any = IntraBuilder()
    plain = make_desk(
        tmp_path / "off", FakePublisher(), FakeModel(answer(0.6, 0.25, 0.15)), clock, builder=off
    )
    await plain.run(pool(T0, entry("AAUSDT")), "scheduled")
    assert not plain.observe_breadth(T0 + timedelta(minutes=1), REFRESH_SYMS, *refresh_rets())


def test_refreshed_line_is_neutral_under_swap_and_mirror() -> None:
    d = intra_dossier()
    at = T0 + timedelta(minutes=3)
    r15, r60 = refresh_rets()
    m15, m60 = refresh_rets(-1.0)
    for order, other in (("up_first", "down_first"), ("down_first", "up_first")):
        block = intra_block(d, H1, order, "c")
        block["status"] = {
            "as_of": at.isoformat(),
            "values": intra_status(REFRESH_SYMS, ("PAXGUSDT",), r15, r60),
        }
        mirrored = intra_block(d.mirror(), Probs(H1.down, H1.flat, H1.up), other, "c")
        mirrored["status"] = {
            "as_of": at.isoformat(),
            "values": intra_status(REFRESH_SYMS, ("PAXGUSDT",), m15, m60),
        }
        text = compose_intra(block, at, 5.0)
        assert "截至 10:05 UTC" in text
        assert compose_intra(mirrored, at, 5.0) == _flip_signs(swap_words(text))
