"""大盘台记分（slow/market_score.py）：合成的大盘台日志 + 合成 K 线，全部离线。"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from jiaoyi.slow import market_score as ms
from jiaoyi.slow.market_desk import Probs
from tests.test_market_desk import FakeModel, FakePublisher, Switch, answer, make_desk
from tests.test_slow_commander import entry, pool

T0 = datetime(2026, 10, 28, 10, 2, tzinfo=UTC)
MIN = 60_000
HOUR = 3_600_000

Price = Callable[[str, int], float]
"""(币, 时刻毫秒) → 价；1 分钟线 / 1 小时线的收盘价 = 收盘那一刻的价。"""


class FakeKlines:
    """``/fapi/v1/klines`` 替身：按 startTime / endTime / limit 回 1m 或 1h 线（含还没收盘的那根，
    和真币安一样）。``gone`` 里的币回 400。"""

    def __init__(self, price: Price, now: datetime, gone: tuple[str, ...] = ()) -> None:
        self.price = price
        self.now_ms = int(now.timestamp() * 1000)
        self.gone = gone
        self.calls: list[dict[str, Any]] = []

    def __call__(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        p = dict(params or {})
        self.calls.append(p)
        req = httpx.Request("GET", url)
        if p["symbol"] in self.gone:
            return httpx.Response(400, json={"code": -1121, "msg": "Invalid symbol."}, request=req)
        step = MIN if p["interval"] == "1m" else HOUR
        start = -(-int(p["startTime"]) // step) * step
        rows = []
        t = start
        while t <= int(p["endTime"]) and t <= self.now_ms and len(rows) < int(p["limit"]):
            c = self.price(p["symbol"], t + step)
            rows.append([t, c, c, c, c, 1, t + step - 1, 1, 1, 1, 1, 0])
            t += step
        return httpx.Response(200, json=rows, request=req)


def ms_of(t: datetime) -> int:
    return int(t.timestamp() * 1000)


def ramp(t0: datetime, pct: dict[str, float], hours: float = 4.0) -> Price:
    """每个币从 t0 起 ``hours`` 小时内线性涨 / 跌 pct[币]%，之前和之后都是平的；没列的币不动。"""
    a = ms_of(t0)

    def price(sym: str, t: int) -> float:
        frac = min(max((t - a) / (hours * HOUR), 0.0), 1.0)
        return 100.0 * (1 + pct.get(sym, 0.0) / 100 * frac)

    return price


SETTINGS = {
    "band_sigma": 0.5,
    "cadence_min": 15,
    "publish_confirm": 2,
    "publish_confirm_min": 30.0,
    "publish_min_gap_min": 0.0,
    "exclude_symbols": ["PAXGUSDT"],
}


def desk_row(
    t0: datetime,
    p4: tuple[float, float, float] = (0.3, 0.4, 0.3),
    *,
    p1: tuple[float, float, float] = (0.3, 0.4, 0.3),
    direction: str = "中性",
    basket: tuple[str, ...] = ("AAUSDT", "BBUSDT", "CCUSDT"),
    band4: float = 0.4,
    band1: float = 0.2,
    base4: tuple[float, float, float] | None = (0.25, 0.5, 0.25),
    base1: tuple[float, float, float] | None = (0.25, 0.5, 0.25),
    published: str | None = None,
    reason: str = "scheduled",
    **extra: Any,
) -> dict[str, Any]:
    def probs(p: tuple[float, float, float] | None) -> dict[str, float] | None:
        return None if p is None else {"up": p[0], "flat": p[1], "down": p[2]}

    def target(band: float, base: tuple[float, float, float] | None) -> dict[str, Any]:
        b = probs(base) or {"up": None, "flat": None, "down": None}
        return {"sigma_pct": band / 0.5, "flat_band_pct": band, "base_rate": b}

    return {
        "cycle_id": f"{t0:%Y-%m-%dT%H:%MZ}",
        "t0": t0.isoformat(),
        "reason": reason,
        "settings": SETTINGS,
        "mirror_test": False,
        "status": "ok",
        "probs_4h": probs(p4),
        "probs_1h": probs(p1),
        "direction": direction,
        "score_targets": {
            "t0": t0.isoformat(),
            "basket": list(basket),
            "1h": target(band1, base1),
            "4h": target(band4, base4),
        },
        "published_view": {"regime_direction": published} if published else None,
        "latency_ms": 1000.0,
        **extra,
    }


def write_log(root: Path, rows: list[dict[str, Any]]) -> None:
    d = root / "market_desk"
    d.mkdir(parents=True, exist_ok=True)
    for r in rows:
        at = r.get("t0") or r["at"]
        day = datetime.fromisoformat(at).astimezone(UTC).date().isoformat()
        with (d / f"{day}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def run(root: Path, now: datetime, fake: FakeKlines, **kw: Any) -> dict[str, Any]:
    return ms.update(root, now, get=fake, **kw)


# ------------------------------------------------------------------ 结局口径


def test_outcome_is_equal_weight_pool_at_t0_against_logged_band(tmp_path: Path) -> None:
    # 池里三个币 +2% / 0 / −0.5%，等权 +0.5%；池外的 ZZ 暴涨不算
    price = ramp(T0, {"AAUSDT": 2.0, "CCUSDT": -0.5, "ZZUSDT": 50.0})
    now = T0 + timedelta(hours=5)
    write_log(tmp_path, [desk_row(T0, band4=0.4), desk_row(T0 + timedelta(seconds=30), band4=0.6)])
    rep = run(tmp_path, now, FakeKlines(price, now))
    a, b = rep["runs"]
    assert a["out"]["4h"]["ret_pct"] == pytest.approx(0.5, abs=1e-9)
    assert a["out"]["4h"]["cls"] == "up" and a["out"]["4h"]["priced"] == 3
    assert b["out"]["4h"]["cls"] == "flat"  # 同样的收益，带宽 0.6 就是平
    # 1h：线性涨跌的四分之一，+0.125% 落在 ±0.2 以内
    assert a["out"]["1h"]["ret_pct"] == pytest.approx(0.125, abs=1e-9)
    assert a["out"]["1h"]["cls"] == "flat"
    assert rep["counts"]["scored_4h"] == 2 and rep["counts"]["pending_4h"] == 0


def test_price_is_close_of_the_minute_bar_ending_at_or_after_t(tmp_path: Path) -> None:
    # t0 = 10:02:30：用 10:02 开盘、10:03 收盘那根的收盘价（和 card_score.bar_open 一样）
    t0 = T0 + timedelta(seconds=30)
    jump = ms_of(T0) + MIN  # 10:03 起价格跳 +1%

    def price(sym: str, t: int) -> float:
        return 101.0 if t >= jump else 100.0

    now = t0 + timedelta(hours=5)
    write_log(tmp_path, [desk_row(t0, band4=0.4)])
    rep = run(tmp_path, now, FakeKlines(price, now))
    assert rep["runs"][0]["out"]["4h"]["ret_pct"] == pytest.approx(0.0, abs=1e-12)


def test_runs_wait_until_horizon_plus_settle(tmp_path: Path) -> None:
    price = ramp(T0, {"AAUSDT": 3.0})
    now = T0 + timedelta(hours=4, minutes=1)  # 4h 到点但没过 2 分钟
    write_log(tmp_path, [desk_row(T0)])
    rep = run(tmp_path, now, FakeKlines(price, now))
    assert rep["counts"]["pending_4h"] == 1 and rep["counts"]["scored_1h"] == 1
    assert "4h" not in rep["runs"][0]["out"] and rep["desk"]["4h"] == {"n": 0}


def test_too_few_priced_symbols_is_unpriced_not_scored(tmp_path: Path) -> None:
    price = ramp(T0, {"AAUSDT": 3.0})
    now = T0 + timedelta(hours=5)
    basket = ("AAUSDT", "BBUSDT", "GONEUSDT", "GONE2USDT")  # 只有一半有价 < 80%
    write_log(tmp_path, [desk_row(T0, basket=basket)])
    rep = run(tmp_path, now, FakeKlines(price, now, gone=("GONEUSDT", "GONE2USDT")))
    assert rep["counts"]["unpriced_4h"] == 1 and rep["counts"]["scored_4h"] == 0
    assert rep["fetch"]["gone"] == ["GONE2USDT", "GONEUSDT"]


def test_klines_are_cached_and_only_new_minutes_fetched(tmp_path: Path) -> None:
    price = ramp(T0, {"AAUSDT": 2.0})
    now = T0 + timedelta(hours=5)
    write_log(tmp_path, [desk_row(T0)])
    first = FakeKlines(price, now)
    rep1 = run(tmp_path, now, first, flash=False)
    assert first.calls and all(c["limit"] <= 1000 for c in first.calls)
    assert (tmp_path / "market_desk" / "score_cache" / "1m" / "AAUSDT").is_dir()

    def boom(url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        raise AssertionError("缓存里有的不该再请求")

    rep2 = ms.update(tmp_path, now, get=boom, flash=False)
    assert rep2["runs"] == rep1["runs"]
    # 一小时后：新一轮要 t0 + 1h 的价，只补之前没覆盖的那几十分钟
    t1 = now
    write_log(tmp_path, [desk_row(t1)])
    later = now + timedelta(hours=1, minutes=3)
    fresh = FakeKlines(price, later)
    run(tmp_path, later, fresh, flash=False)
    assert fresh.calls and all(int(c["startTime"]) >= ms_of(now) - MIN for c in fresh.calls)
    assert all(c["limit"] <= 100 for c in fresh.calls)  # 权重 1 的小请求


def test_custom_out_and_cache_dirs_leave_data_dir_untouched(tmp_path: Path) -> None:
    data = tmp_path / "data"
    write_log(data, [desk_row(T0)])
    before = sorted(p.relative_to(data) for p in data.rglob("*"))
    now = T0 + timedelta(hours=5)
    out = tmp_path / "scratch" / "score.json"
    ms.update(data, now, get=FakeKlines(ramp(T0, {}), now), out_path=out, cache_dir=tmp_path / "c")
    assert json.loads(out.read_text())["counts"]["runs_ok"] == 1
    assert sorted(p.relative_to(data) for p in data.rglob("*")) == before


# ------------------------------------------------------------------ 指标


def test_scores_known_values() -> None:
    p = Probs(0.5, 0.3, 0.2)
    assert ms.brier(p, "up") == pytest.approx(0.25 + 0.09 + 0.04)
    assert ms.logloss(p, "down") == pytest.approx(-math.log(0.2))
    assert ms.logloss(Probs(0.99, 0.01, 0.0), "down") == pytest.approx(-math.log(0.02))
    # RPS（跌 < 平 < 涨）：结局涨，累积 (0.2, 0.5) 对 (0, 0)
    assert ms.rps(p, "up") == pytest.approx((0.04 + 0.25) / 2)
    assert ms.rps(p, "down") == pytest.approx((0.64 + 0.25) / 2)
    assert ms.as_tuple(ms.sharp("down")) == pytest.approx((0.225, 0.225, 0.55))


def test_desk_vs_base_vs_persistence_brier_and_direction(tmp_path: Path) -> None:
    # 两轮：池先涨 1%（近 1 小时延续 = 涨），之后 4h 再涨 1% → 结局涨
    pre = T0 - timedelta(hours=1)
    t1 = T0 + timedelta(hours=5)

    def price(sym: str, t: int) -> float:
        rise1 = min(max((t - ms_of(pre)) / HOUR, 0.0), 1.0)  # 9:02–10:02 +1%
        rise2 = min(max((t - ms_of(T0)) / (4 * HOUR), 0.0), 1.0)  # 10:02–14:02 +1%
        drop = min(max((t - ms_of(t1)) / (4 * HOUR), 0.0), 1.0)  # 15:02–19:02 −2%
        return 100.0 * (1 + 0.01 * rise1 + 0.01 * rise2 - 0.02 * drop)

    now = t1 + timedelta(hours=5)
    write_log(
        tmp_path,
        [
            desk_row(T0, (0.6, 0.3, 0.1), direction="看涨"),
            desk_row(t1, (0.6, 0.3, 0.1), direction="看涨"),
        ],
    )
    rep = run(tmp_path, now, FakeKlines(price, now), flash=False)
    m = rep["desk"]["4h"]
    assert [r["out"]["4h"]["cls"] for r in rep["runs"]] == ["up", "down"]
    assert m["n"] == 2 and m["outcome_freq"] == {"up": 0.5, "flat": 0.0, "down": 0.5}
    desk_b = (0.16 + 0.09 + 0.01 + 0.36 + 0.09 + 0.81) / 2
    base_b = (0.5625 + 0.25 + 0.0625) * 2 / 2
    assert m["brier"]["desk"] == pytest.approx(desk_b, abs=1e-4)
    assert m["brier"]["base"] == pytest.approx(base_b, abs=1e-4)
    assert m["brier"]["desk_minus_base"] == pytest.approx(desk_b - base_b, abs=1e-4)
    # 延续：第一轮近 1 小时 +1% > 0.2 → 叫涨（对），第二轮近 1 小时平 → 叫平（错）
    assert [r["trail_1h_pct"] for r in rep["runs"]] == [pytest.approx(1.0, abs=1e-3), 0.0]
    pers = (ms.brier(ms.sharp("up"), "up") + ms.brier(ms.sharp("flat"), "down")) / 2
    assert m["brier"]["persist"] == pytest.approx(pers, abs=1e-4)
    assert m["direction"]["hit"] == 0.5 and m["direction"]["coverage"] == 1.0
    assert m["confusion"]["up"] == {"up": 1, "flat": 0, "down": 1}
    up_bins = {b["bin"]: b for b in m["reliability"]["up"]}
    assert up_bins["0.6-0.7"] == {"bin": "0.6-0.7", "n": 2, "mean_p": 0.6, "freq": 0.5}


def test_prior_missing_runs_are_counted_but_not_scored(tmp_path: Path) -> None:
    now = T0 + timedelta(hours=5)
    write_log(tmp_path, [desk_row(T0, base4=None, base1=None, prior_missing=True)])
    rep = run(tmp_path, now, FakeKlines(ramp(T0, {}), now), flash=False)
    assert rep["counts"]["prior_missing"] == 1 and rep["desk"]["4h"] == {"n": 0}


def test_failed_and_skipped_runs_and_owner_events_are_read(tmp_path: Path) -> None:
    now = T0 + timedelta(hours=5)
    write_log(
        tmp_path,
        [
            desk_row(T0),
            {"t0": T0.isoformat(), "status": "failed", "error": "x"},
            {"t0": T0.isoformat(), "status": "skipped"},
            {"event": "owner_change", "at": (T0 + timedelta(minutes=1)).isoformat(),
             "from": "desk", "to": "flash"},
        ],
    )  # fmt: skip
    log = ms.read_desk_log(tmp_path, T0 - timedelta(days=1), now)
    assert (len(log.runs), log.failed, log.skipped) == (1, 1, 1)
    assert log.owner_flash_at == [T0 + timedelta(minutes=1)]


# ------------------------------------------------------------------ 发布：命中、重放、延迟


def lite(t0: datetime, direction: str, reason: str = "scheduled", **kw: Any) -> ms.LoggedRun:
    row = desk_row(t0, direction=direction, reason=reason, **kw)
    got = ms._desk_run(row, t0)
    assert got is not None
    return got


def test_published_direction_scored_per_run_by_what_was_in_force(tmp_path: Path) -> None:
    rows = [
        desk_row(T0, direction="看涨", published="看涨"),
        desk_row(T0 + timedelta(minutes=15), direction="看涨"),
        desk_row(T0 + timedelta(minutes=30), direction="看涨"),
    ]
    write_log(tmp_path, rows)
    now = T0 + timedelta(hours=5)
    rep = run(tmp_path, now, FakeKlines(ramp(T0, {"AAUSDT": 9.0}, hours=0.5), now), flash=False)
    # 第一轮的发布在它完成之后才生效：它自己 t0 时还没有大盘台方向
    assert [r["in_force"] for r in rep["runs"]] == [None, "看涨", "看涨"]
    p = rep["published"]["4h"]
    assert p["n"] == 2 and p["hit"] == 0.5  # 10:17 起 +2.25%（剩下的半程）算涨，10:32 起不动算平


def test_replay_publishes_matches_market_desk_hysteresis() -> None:
    q = timedelta(minutes=15)
    runs = [
        lite(T0, "看涨"),  # 接管，立即发布
        lite(T0 + q, "看涨"),
        lite(T0 + 2 * q, "看跌"),  # 候选 1
        lite(T0 + 3 * q, "看跌"),  # 2 轮但只 15 分钟
        lite(T0 + 4 * q, "看跌"),  # 30 分钟：发布
        lite(T0 + 4 * q + timedelta(minutes=2), "看涨", reason="move"),
        lite(T0 + 5 * q + timedelta(minutes=1), "看涨"),  # 离加跑 14 分钟：不够
        lite(T0 + 5 * q + timedelta(minutes=2), "看涨"),  # 15 分钟 = max(30/2, 15)：发布
    ]
    got = ms.replay_publishes(runs, [r.direction for r in runs], [])
    assert [(at - T0, d) for at, d in got] == [
        (timedelta(seconds=1), "看涨"),
        (4 * q + timedelta(seconds=1), "看跌"),
        (5 * q + timedelta(minutes=2, seconds=1), "看涨"),
    ]
    # 确认 1 轮也要等时间分开（cadence 15 分钟）：结果和 2 轮一样
    one = ms.replay_publishes(runs, [r.direction for r in runs], [], confirm=1)
    assert [d for _, d in one] == ["看涨", "看跌", "看涨"]
    # 交回 Flash 之后的第一轮是接管：立即发布
    back = ms.replay_publishes(runs[:4], [r.direction for r in runs[:4]], [T0 + 2 * q])
    assert [(at - T0, d) for at, d in back][-2:] == [
        (2 * q, None),
        (2 * q + timedelta(seconds=1), "看跌"),
    ]


async def test_replay_reproduces_real_market_desk_log(tmp_path: Path) -> None:
    """拿真的 MarketDesk（替身模型）跑一串轮次，再从它写的日志重放：发布的方向和轮次要一模一样。"""
    clock = [T0]
    script = Switch(answer(0.6, 0.25, 0.15))
    publisher = FakePublisher()
    desk = make_desk(tmp_path, publisher, FakeModel(script), clock)
    plan = [
        (0, "scheduled", (0.6, 0.25, 0.15)),
        (15, "scheduled", (0.15, 0.25, 0.6)),
        (29, "move", (0.15, 0.25, 0.6)),
        (30, "scheduled", (0.15, 0.25, 0.6)),
        (45, "scheduled", (0.15, 0.25, 0.6)),
        (60, "scheduled", (0.3, 0.4, 0.3)),
        (75, "scheduled", (0.3, 0.4, 0.3)),
        (90, "scheduled", (0.3, 0.4, 0.3)),
        (105, "scheduled", (0.6, 0.25, 0.15)),
    ]
    candidates = pool(T0, entry("AAUSDT"), entry("BBUSDT"))
    for minute, reason, p in plan:
        clock[0] = T0 + timedelta(minutes=minute)
        script.text = answer(*p)
        await desk.run(candidates, reason)
    log = ms.read_desk_log(tmp_path, T0 - timedelta(hours=1), T0 + timedelta(hours=3))
    assert len(log.runs) == len(plan)
    real = [(r.cycle_id, r.published) for r in log.runs if r.published]
    replay = ms.replay_publishes(log.runs, [r.direction for r in log.runs], log.owner_flash_at)
    by_time = {r.completed: r.cycle_id for r in log.runs}
    assert [(by_time[at], d) for at, d in replay if d] == real
    assert [d for _, d in real] == [v.regime_direction for v in publisher.published]
    assert len(real) >= 3


def test_latency_counts_minutes_from_sustained_move_to_same_direction_publish() -> None:
    q = timedelta(minutes=15)
    rows = []
    for k, cls in enumerate(["flat", "up", "up", "up", "flat", "down", "flat"]):
        s = ms.Scored(lite(T0 + k * q, "中性"))
        s.trail_cls = cls
        rows.append(s)
    eps = ms.episodes(rows)
    assert eps == [(T0 + q, T0 + 3 * q, "up")]  # 跌的那段只有一轮，不算持续
    timeline = [(T0 + 3 * q, "看涨")]
    got = ms.latency(eps, timeline)
    assert got["caught"] == 1 and got["median_min"] == 30.0 and got["missed"] == 0
    assert ms.latency(eps, [(T0, "看涨")])["already"] == 1
    assert ms.latency(eps, [(T0 + 6 * q, "看涨")])["missed"] == 1


def test_sweep_rederives_directions_without_model_calls(tmp_path: Path) -> None:
    now = T0 + timedelta(hours=5)
    write_log(tmp_path, [desk_row(T0, (0.40, 0.35, 0.25), direction="中性")])
    rep = run(tmp_path, now, FakeKlines(ramp(T0, {"AAUSDT": 3.0}), now), flash=False)
    table = {(r["theta"], r["delta"], r["confirm"]): r for r in rep["sweep"]}
    assert len(table) == len(ms.THETAS) * len(ms.DELTAS) * len(ms.CONFIRMS)
    # +1% 等权 > 0.4 → 涨；θ 0.35 δ 0.15 叫涨（命中），θ 0.45 叫平（不中），δ 0.20 也叫平
    assert table[(0.35, 0.15, 1)]["call_4h"]["hit"] == 1.0
    assert table[(0.35, 0.15, 1)]["call_4h"]["coverage"] == 1.0
    assert table[(0.45, 0.15, 1)]["call_4h"]["hit"] == 0.0
    assert table[(0.35, 0.20, 2)]["call_4h"]["coverage"] == 0.0
    assert table[(0.35, 0.15, 2)]["publishes"] == 1  # 接管那一次


def test_mirror_runs_reported_separately(tmp_path: Path) -> None:
    now = T0 + timedelta(hours=5)
    row = desk_row(T0, (0.5, 0.3, 0.2), mirror_test=True)
    row["mirror"] = {"probs_4h": {"up": 0.1, "flat": 0.3, "down": 0.6}}
    write_log(tmp_path, [row])
    rep = run(tmp_path, now, FakeKlines(ramp(T0, {"AAUSDT": 3.0}), now), flash=False)
    m = rep["mirror"]
    assert m["n"] == 1 and m["brier_main"] == pytest.approx(0.25 + 0.09 + 0.04)
    # 镜像概率涨跌对调回来（0.6, 0.3, 0.1）再对实际记分
    assert m["brier_mirror_swapped"] == pytest.approx(0.16 + 0.09 + 0.01)


# ------------------------------------------------------------------ Flash 旧口径


def test_flash_rounds_scored_with_own_pool_excluding_gold_and_desk_sigma_method(
    tmp_path: Path,
) -> None:
    tf = T0 + timedelta(days=8)

    def price(sym: str, t: int) -> float:
        if sym == "PAXGUSDT":
            return 100.0 * (0.5 if t > ms_of(tf) else 1.0)  # 被排除：暴跌不该拖累结局
        k = t // HOUR
        wiggle = 0.01 if k % 2 else 0.0  # 每小时 ±1%：σ_1h 约 1%
        jump = min(max((t - ms_of(tf)) / HOUR, 0.0), 1.0) * 0.05
        return 100.0 * (1 + wiggle + jump)

    d = tmp_path / "market_meeting"
    d.mkdir()
    pool_rows = [{"symbol": s} for s in ("AAUSDT", "BBUSDT", "PAXGUSDT")]
    rounds = [
        {"ts": tf.isoformat(), "regime_direction": "看涨", "pool": pool_rows},
        {"ts": (tf + timedelta(seconds=1)).isoformat(), "regime_direction": "看跌",
         "pool": pool_rows},
        {"ts": (tf + timedelta(seconds=2)).isoformat(), "regime_direction": None,
         "pool": pool_rows},
    ]  # fmt: skip
    (d / f"{tf.date().isoformat()}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rounds), "utf-8"
    )
    now = tf + timedelta(hours=1, minutes=5)
    write_log(tmp_path, [desk_row(tf - timedelta(minutes=10), basket=("AAUSDT", "BBUSDT"))])
    rep = run(tmp_path, now, FakeKlines(price, now), days=1)
    f1 = rep["flash"]["1h"]
    assert rep["flash"]["rounds"] == 2 and f1["n"] == 2
    assert f1["hit"] == 0.5 and f1["confusion"]["up"]["up"] == 1
    assert f1["confusion"]["down"]["up"] == 1
    assert f1["brier_sharp"] == pytest.approx(
        (ms.brier(ms.sharp("up"), "up") + ms.brier(ms.sharp("down"), "up")) / 2, abs=1e-4
    )
    assert rep["flash"]["4h"]["pending"] == 2


# ------------------------------------------------------------------ 面板


def test_panel_line_and_desk_node_accuracy(tmp_path: Path) -> None:
    from jiaoyi.panel.files import PanelFiles
    from jiaoyi.panel.pages import build_team

    now = T0 + timedelta(hours=5)
    write_log(tmp_path, [desk_row(T0, (0.6, 0.3, 0.1), direction="看涨", published="看涨")])
    rep = run(tmp_path, now, FakeKlines(ramp(T0, {"AAUSDT": 3.0}), now), flash=False)
    p = rep["panel"]
    assert p["24h"]["n"] == 1 and p["24h"]["hit"] == 1.0
    assert p["text"].startswith("近 24 小时：4h Brier 0.260 vs 先验 0.875")
    files = PanelFiles(tmp_path, "Asia/Bangkok")
    desk = build_team(files, now, None, desk_schedule=("replace", 15, 2)).desk
    assert desk is not None and desk.accuracy is not None
    assert desk.accuracy.startswith(p["text"]) and "（算于 10-28 22:02）" in desk.accuracy
    (tmp_path / "market_desk" / "score.json").unlink()
    desk = build_team(files, now, None, desk_schedule=("replace", 15, 2)).desk
    assert desk is not None and desk.accuracy is None


def test_paced_getter_spaces_requests() -> None:
    slept: list[float] = []
    calls: list[str] = []

    def get(url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        calls.append(url)
        return httpx.Response(200, json=[])

    g = ms.paced(get, 5.0, sleep=slept.append)
    g("a")
    g("b")
    assert calls == ["a", "b"] and len(slept) == 1 and 4.0 < slept[0] <= 5.0
