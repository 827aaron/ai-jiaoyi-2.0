"""作战卡方向记分：t0 取发布时刻、超额和基线的算法、时段聚类 bootstrap、只追加且重跑幂等，
以及在复盘样本切片上复现 2026-10-28 方向准确性复盘的数字。"""

from __future__ import annotations

import json
import random
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.slow import card_score as C
from jiaoyi.slow import scoring_common as SC
from jiaoyi.slow.review import JournalRow

T = datetime(2026, 10, 25, 9, 41, 41, 500000, tzinfo=UTC)
FIXTURE = Path(__file__).parent / "fixtures" / "card_score_audit_slice.json"


def jrow(
    sym: str = "SOLUSDT",
    bias: str = "short",
    at: datetime = T,
    version: int = 44,
    card: str = "card-20261025",
    valid: timedelta = timedelta(minutes=55),
    reason: str = "event:key_level_break",
) -> JournalRow:
    return JournalRow(
        card_id=card,
        version=version,
        created_at=at,
        expires_at=at + valid,
        regime="",
        refresh_reason=reason,
        symbol=sym,
        category="alt",
        bias=bias,
    )


def concl(sym: str, bias: str, at: datetime, rid: str, issued: datetime | None = None) -> dict:
    return {
        "ts": at.isoformat(),
        "role": "commander",
        "symbol": sym,
        "record_id": rid,
        "summary": f"{sym} 开会结论：{bias}",
        "output": {"bias": bias, "issued_at": issued.isoformat() if issued else None},
    }


def publish(card: str, version: int, at: datetime, word: str = "出作战卡") -> dict:
    return {
        "ts": at.isoformat(),
        "role": "commander",
        "symbol": None,
        "card_id": card,
        "record_id": f"pub-{version}-{at:%H%M%S}",
        "summary": f"{word} v{version}：3 个标的",
        "output": {"version": version, "symbols": ["SOLUSDT"]},
    }


def write_log(data: Path, recs: list[dict]) -> None:
    d = data / "decision_log"
    d.mkdir(parents=True, exist_ok=True)
    by_day: dict[str, list[dict]] = {}
    for r in recs:
        by_day.setdefault(r["ts"][:10], []).append(r)
    for day, rs in by_day.items():
        with (d / f"{day}.jsonl").open("a", encoding="utf-8") as f:
            for r in rs:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_journal(data: Path, rows: list[JournalRow]) -> None:
    d = data / "card_review"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "cards.jsonl").open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(r.model_dump_json() + "\n")


# ---------------------------------------------------------------- t0


def test_t0_is_first_publish_of_that_version_then_conclusion_then_round_start(
    tmp_path: Path,
) -> None:
    m = timedelta(minutes=1)
    write_log(
        tmp_path,
        [
            concl("SOLUSDT", "long", T + 1 * m, "wrong-bias"),  # 方向不同，不配
            concl("SOLUSDT", "short", T + 3 * m, "c-sol"),
            publish("card-20261025", 44, T - 10 * m),  # 早于结论 5 秒以上：不算
            publish("card-20261025", 44, T + 6 * m, "作战卡更新"),
            publish("card-20261025", 44, T + 4 * m),  # 同一版本最早的一条
            publish("card-20261025", 45, T + 3 * m + timedelta(seconds=10)),  # 别的版本
            concl("ENAUSDT", "short", T + 2 * m, "c-ena"),
            concl("ENAUSDT", "short", T + 9 * m, "c-ena-later"),
        ],
    )
    idx = C.read_log_index(tmp_path / "decision_log")
    used: set[str] = set()
    t0, src, rid = C.resolve_t0(jrow(), idx, used)
    assert (t0, src, rid) == (T + 4 * m, "card_publish", "c-sol")
    # 版本 46 没有发布记录 → 会议结论时间；同一条结论不配第二张卡
    t0, src, rid = C.resolve_t0(jrow("ENAUSDT", version=46), idx, used)
    assert (t0, src, rid) == (T + 2 * m, "conclusion", "c-ena")
    t0, src, rid = C.resolve_t0(jrow("ENAUSDT", version=46), idx, used)
    assert (t0, src, rid) == (T + 9 * m, "conclusion", "c-ena-later")
    # 什么都对不上 → 整轮起点
    assert C.resolve_t0(jrow("BTCUSDT", version=46), idx, used) == (T, "created_at", None)
    assert used == {"c-sol", "c-ena", "c-ena-later"}


def test_t0_conclusion_before_round_start_is_clamped_and_issued_at_matches_exactly(
    tmp_path: Path,
) -> None:
    m = timedelta(minutes=1)
    write_log(
        tmp_path,
        [
            concl("SOLUSDT", "short", T - 1 * m, "early"),  # 起点前 1 分钟：t0 取起点
            concl("ENAUSDT", "short", T + 1 * m, "near"),
            concl("ENAUSDT", "short", T + 20 * m, "exact", issued=T),  # issued_at 对上优先
        ],
    )
    idx = C.read_log_index(tmp_path / "decision_log")
    assert C.resolve_t0(jrow(version=7), idx, set()) == (T, "conclusion", "early")
    assert C.resolve_t0(jrow("ENAUSDT", version=7), idx, set()) == (
        T + 20 * m,
        "conclusion",
        "exact",
    )


def test_pool_is_the_one_in_force_at_t0(tmp_path: Path) -> None:
    mm = tmp_path / "market_meeting"
    mm.mkdir()
    rounds = [
        {"ts": T.isoformat(), "pool_id": "p1", "pool": [{"symbol": "SOLUSDT"}]},
        {"ts": (T + timedelta(hours=2)).isoformat(), "pool_id": "p2", "pool": [{"symbol": "X"}]},
    ]
    (mm / "2026-10-25.jsonl").write_text("\n".join(json.dumps(r) for r in rounds) + "\n")
    (mm / "outcomes.jsonl").write_text('{"not": "a round"}\n')
    tl = SC.PoolTimeline.load(tmp_path)
    at = tl.at
    assert tl.first == T
    assert at(T - timedelta(hours=1)) is None  # 最早一轮之前没有记录：不拿后来的池顶替
    assert at(T) == at(T + timedelta(hours=1, minutes=59)) == SC.Pool("p1", ("SOLUSDT",))
    assert at(T + timedelta(hours=2)) == SC.Pool("p2", ("X",))
    assert SC.PoolTimeline([]).at(T) is None


def test_bar_open_is_last_closed_minute() -> None:
    at = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    assert C.bar_open(at) == at - timedelta(minutes=1)  # 12:00:00 → 11:59 那根刚收盘
    assert C.bar_open(at + timedelta(seconds=30)) == at  # 12:00:30 → 12:00 那根 12:01 才收
    assert C.bar_open(at + timedelta(microseconds=1)) == at


# ---------------------------------------------------------------- 超额、分量、基线


def book_of(px: dict[str, dict[datetime, float]]) -> C.CloseBook:
    b = C.CloseBook()
    for s, pts in px.items():
        for t, v in pts.items():
            b.put(s, C.bar_open(t), v)
    return b


def test_excess_market_and_coin_components_exact() -> None:
    t0 = T
    h4 = t0 + timedelta(hours=4)
    prior = t0 - timedelta(hours=4)
    book = book_of(
        {
            "SOLUSDT": {prior: 196.0, t0: 200.0, h4: 196.0},  # 过去 +2.04%，之后 −2%
            "AAVEUSDT": {t0: 100.0, h4: 99.0},  # −1%
            "ADAUSDT": {t0: 0.5, h4: 0.5},  # 0
            "DOGEUSDT": {t0: 0.2},  # 4h 没价格：不进等权
            "BTCUSDT": {prior: 100.0, t0: 99.0, h4: 98.0},
        }
    )
    pool = C.Pool("p1", ("SOLUSDT", "AAVEUSDT", "ADAUSDT", "DOGEUSDT"))
    s = C.score_card(jrow(), t0, "card_publish", None, book, pool, h4)
    assert s.coin.h4 == pytest.approx(-0.02, abs=1e-15)
    assert s.pool.h4 == pytest.approx(-0.01, abs=1e-15)  # (−2% −1% + 0) / 3
    assert (s.pool_size, s.pool_n_4h) == (4, 3)
    assert s.coin.prior_4h == pytest.approx(200 / 196 - 1)
    assert s.btc.prior_4h == pytest.approx(-0.01)
    assert s.pool.prior_4h == pytest.approx(200 / 196 - 1)  # 过去 4h 只有 SOL 有价
    assert s.coin.h1 is None and s.block == "2026-10-25T08"
    r = C.summarize([s], h4)
    h = r.horizons["4h"]
    # 做空：原始 +200bp = 大盘 +100bp + 个币（超额）+100bp
    assert h.raw.mean_bp == pytest.approx(200.0)
    assert h.market.mean_bp == pytest.approx(100.0)
    assert h.excess.mean_bp == pytest.approx(100.0)
    assert (h.raw.hit, h.excess.hit, h.excess.n) == (1.0, 1.0, 1)
    assert h.raw.hit_ci is None  # 不到 3 个时段不给区间


def card(
    i: int,
    bias: str,
    coin: float | None,
    pool: float | None,
    *,
    btc_prior: float | None = None,
    coin_prior: float | None = None,
    pool_prior: float | None = None,
    reason: str = "scheduled",
    hours: float = 0.0,
) -> C.CardScore:
    t0 = T + timedelta(hours=hours, minutes=i)
    return C.CardScore(
        card_id="c",
        version=i,
        symbol="SOLUSDT",
        category="alt",
        bias=bias,
        refresh_reason=reason,
        created_at=t0,
        expires_at=t0 + timedelta(hours=1),
        t0=t0,
        t0_src="card_publish",
        coin=C.Leg(prior_4h=coin_prior, h4=coin, h1=coin),
        pool=C.Leg(prior_4h=pool_prior, h4=pool, h1=pool),
        btc=C.Leg(prior_4h=btc_prior),
        legacy_ret=coin,
        scored_at=t0,
    )


def test_baselines_on_the_same_sample() -> None:
    rows = [
        card(1, "short", -0.02, -0.01, btc_prior=-0.005, coin_prior=0.01, pool_prior=-0.002),
        card(2, "short", 0.01, 0.015, btc_prior=0.003, coin_prior=0.0, pool_prior=0.004),
        card(3, "long", 0.03, 0.01, btc_prior=0.0, coin_prior=-0.02, pool_prior=None),
        card(4, "no_trade", 0.05, 0.0),  # 观望：只计数
        card(5, "long", None, 0.01),  # 没价格：不进样本
    ]
    r = C.summarize(rows, T)
    assert (r.n_cards, r.n_long, r.n_short, r.n_neutral, r.n_unpriced) == (5, 2, 2, 1, 1)
    b = {x.key: x for x in r.baselines["4h"]}
    assert b["card"].raw.n == 3 and b["card"].raw.hit == pytest.approx(2 / 3)
    assert b["card"].raw.mean_bp == pytest.approx((200 - 100 + 300) / 3)
    assert b["always_short"].raw.hit == pytest.approx(1 / 3)
    assert b["always_short"].raw.mean_bp == pytest.approx((200 - 100 - 300) / 3)
    assert b["always_long"].raw.hit == pytest.approx(2 / 3)
    # BTC 过去 4h 为 0 的那张没有方向，不进这条基线：卡 1 空（对）、卡 2 多（对）
    assert (b["btc_4h"].raw.n, b["btc_4h"].raw.hit) == (2, 1.0)
    # 自身动量：卡 1 多（错）、卡 2 动量为 0 不算、卡 3 空（错）
    assert (b["mom_4h"].raw.n, b["mom_4h"].raw.hit) == (2, 0.0)
    assert (b["pool_4h"].raw.n, b["pool_4h"].raw.mean_bp) == (2, pytest.approx((200 + 100) / 2))
    # 超额基线：一律做空 = −(币 − 池)：+100、+50、−200
    assert b["always_short"].excess.mean_bp == pytest.approx((100 + 50 - 200) / 3)
    assert r.horizons["4h"].excess.mean_bp == pytest.approx((100 + 50 + 200) / 3)
    by_dir = {g.key: g for g in r.by_direction}
    assert by_dir["做空"].n_cards == 2 and by_dir["做多"].raw.n == 1
    [g] = r.by_refresh_reason
    assert (g.key, g.n_cards, g.n_neutral) == ("scheduled", 5, 1)


def test_periods_follow_marks() -> None:
    marks = (("#A", T), ("#B", T + timedelta(hours=8)))
    assert C.period_of(T - timedelta(seconds=1), marks) == "#A 之前"
    assert C.period_of(T, marks) == "#A – #B"
    assert C.period_of(T + timedelta(hours=9), marks) == "#B 之后"
    rows = [card(1, "short", -0.01, 0.0, hours=-1), card(2, "long", 0.01, 0.0, hours=9)]
    r = C.summarize(rows, T, marks)
    assert [(g.key, g.n_cards) for g in r.periods] == [("#A 之前", 1), ("#B 之后", 1)]


# ---------------------------------------------------------------- bootstrap


def naive_cluster_boot(values: list[float], blocks: list[str], b: int = 2000, seed: int = 7):
    """复盘脚本 common.cluster_boot 的写法：整块抽回来拼成一个样本再求均值。"""
    rng = random.Random(seed)
    groups: dict[str, list[float]] = {}
    for v, k in zip(values, blocks, strict=True):
        groups.setdefault(k, []).append(v)
    keys = list(groups)
    sims = []
    for _ in range(b):
        s: list[float] = []
        for _ in keys:
            s.extend(groups[keys[rng.randrange(len(keys))]])
        sims.append(statistics.fmean(s))
    sims.sort()
    return sims[int(0.025 * b)], sims[int(0.975 * b)], len(keys)


def test_cluster_bootstrap_is_seeded_and_matches_naive_block_resampling() -> None:
    rng = random.Random(1)
    blocks = [f"b{rng.randrange(9)}" for _ in range(120)]
    vals = [rng.gauss(0.0, 1.0) + (0.5 if k in ("b1", "b2") else 0.0) for k in blocks]
    got = SC.cluster_ci(vals, blocks)
    assert got == SC.cluster_ci(vals, blocks)  # 同种子同结果
    lo, hi, n = naive_cluster_boot(vals, blocks)
    assert n == got[2] == 9
    assert got[0] == pytest.approx(lo, abs=1e-12) and got[1] == pytest.approx(hi, abs=1e-12)
    assert C.cluster_ci(vals, blocks, seed=8) != got
    # 同一时段内的顺序不影响结果（按整块抽）
    order = sorted(range(120), key=lambda i: (blocks.index(blocks[i]), -i))
    again = C.cluster_ci([vals[i] for i in order], [blocks[i] for i in order])
    assert again[0] == pytest.approx(got[0], abs=1e-12)
    assert again[1] == pytest.approx(got[1], abs=1e-12)
    assert C.cluster_ci([1.0, 0.0], ["a", "b"]) == (None, None, 2)


def test_cluster_ci_is_wider_than_iid_when_blocks_move_together() -> None:
    blocks = [f"b{i // 20}" for i in range(200)]  # 10 个时段，每段 20 张同涨同跌
    vals = [1.0 if (i // 20) % 2 else 0.0 for i in range(200)]
    lo, hi, n = C.cluster_ci(vals, blocks)
    assert n == 10 and lo is not None and hi is not None
    assert hi - lo > 0.4  # 逐张算的 Wilson 区间只有 ±7 个百分点


# ---------------------------------------------------------------- 落盘：只追加、幂等


@dataclass
class Bar:
    open_time: datetime
    close: float


class FakeFetch:
    """每分钟一根，价格 = 100 + 分钟序号 × 每币的斜率（BTC 平）。"""

    def __init__(
        self,
        slopes: dict[str, float],
        fail: set[str] | None = None,
        gone: set[str] | None = None,
        badzip: int = 0,
    ) -> None:
        self.slopes = slopes
        self.fail = fail or set()
        self.gone = gone or set()  # 币安没有这个币：接口回 400
        self.badzip = badzip  # 前几次调用抛 BadZipFile（缓存里的日包坏了）
        self.calls: list[tuple[str, datetime, datetime]] = []

    async def __call__(self, sym: str, a: datetime, b: datetime) -> list[Bar]:
        import urllib.error
        import zipfile

        self.calls.append((sym, a, b))
        if self.badzip:
            self.badzip -= 1
            raise zipfile.BadZipFile("File is not a zip file")
        if sym in self.fail:
            raise OSError("down")
        if sym in self.gone:
            raise urllib.error.HTTPError("u", 400, "Invalid symbol", None, None)  # type: ignore[arg-type]
        out, t = [], a
        while t <= b:
            k = (t - T.replace(second=0, microsecond=0)).total_seconds() / 60
            out.append(Bar(t, 100.0 + k * self.slopes.get(sym, 0.0)))
            t += timedelta(minutes=1)
        return out


def seed_data(tmp_path: Path) -> list[JournalRow]:
    rows = [
        jrow("SOLUSDT", "short", T, version=1),
        jrow("ENAUSDT", "long", T + timedelta(minutes=5), version=2),
        jrow("ENAUSDT", "no_trade", T + timedelta(hours=1), version=3),
    ]
    # 同一 卡 / 版本 / 币 写了几行（方向还互相矛盾）：只算一张，按最后一行
    write_journal(tmp_path, [rows[0].model_copy(update={"bias": "long"}), *rows, rows[0]])
    write_log(
        tmp_path,
        [publish("card-20261025", 1, T + timedelta(minutes=2))],
    )
    mm = tmp_path / "market_meeting"
    mm.mkdir()
    pool = [{"symbol": s} for s in ("SOLUSDT", "ENAUSDT", "AAVEUSDT")]
    (mm / "2026-10-25.jsonl").write_text(
        json.dumps({"ts": (T - timedelta(hours=1)).isoformat(), "pool_id": "p", "pool": pool})
        + "\n",
        encoding="utf-8",
    )
    return rows


async def test_update_appends_only_finished_cards_and_reruns_are_idempotent(
    tmp_path: Path,
) -> None:
    seed_data(tmp_path)
    fetch = FakeFetch({"SOLUSDT": -0.01, "ENAUSDT": 0.02, "AAVEUSDT": 0.0})
    store = C.ScoreStore(tmp_path)
    # 按整轮起点算 4h 已经走完，但卡 2 分钟后才发出：还不能记
    early = T + timedelta(minutes=1, hours=4) + C.SETTLE
    r0 = await C.update(tmp_path, early, fetch)
    assert (r0.new, r0.waiting) == (0, 3) and not store.read() and not fetch.calls
    # 第一张卡 4h 窗口刚好走完（t0 = 发布时刻 +2 分钟），第二张差 3 分钟，第三张还早
    now = T + timedelta(minutes=2, hours=4) + C.SETTLE
    r1 = await C.update(tmp_path, now, fetch)
    assert (r1.new, r1.waiting, r1.failed) == (1, 2, 0)
    [s] = store.read()
    assert (s.symbol, s.bias, s.t0, s.t0_src, s.pool_id) == (
        "SOLUSDT",
        "short",
        T + timedelta(minutes=2),
        "card_publish",
        "p",
    )
    assert s.coin.h4 is not None and s.coin.h4 < 0 and s.pool.h4 is not None
    lines = store.scored_path.read_text().splitlines()
    assert len(lines) == 1
    # 按 (币, UTC 日) 合并：每个币只拉一段
    assert sorted(c[0] for c in fetch.calls) == ["AAVEUSDT", "BTCUSDT", "ENAUSDT", "SOLUSDT"]

    # 同一时刻再跑：不新增、不拉行情、文件一字不差
    fetch.calls.clear()
    r2 = await C.update(tmp_path, now, fetch)
    assert (r2.new, fetch.calls) == (0, [])
    assert store.scored_path.read_text().splitlines() == lines
    assert r2.report == r1.report.model_copy(update={"generated_at": now})

    # 时间往后走：剩下两张补上，旧行不动
    later = T + timedelta(hours=6)
    r3 = await C.update(tmp_path, later, fetch)
    assert r3.new == 2
    after = store.scored_path.read_text().splitlines()
    assert after[:1] == lines and len(after) == 3
    assert C.ScoreStore(tmp_path).read_summary() == r3.report
    assert r3.report.n_neutral == 1 and r3.report.t0_src == {
        "card_publish": 1,
        "created_at": 2,
    }


def test_append_is_idempotent_and_first_line_wins(tmp_path: Path) -> None:
    a = card(1, "short", -0.01, 0.0)
    b = card(2, "long", 0.01, 0.0)
    store = C.ScoreStore(tmp_path)
    assert store.append([a, b, a]) == 2
    lines = store.scored_path.read_text().splitlines()
    assert store.append([b, a]) == 0
    assert (
        store.scored_path.read_text().splitlines()
        == lines
        == [
            a.model_dump_json(),
            b.model_dump_json(),
        ]
    )
    # 文件里万一有同一张卡的两行（比如两个进程同时写），读的时候以第一行为准
    with store.scored_path.open("a") as f:
        f.write(a.model_copy(update={"bias": "long"}).model_dump_json() + "\n")
        f.write("not json\n")
    assert store.read() == [a, b]


async def test_pool_member_down_does_not_block_cards(tmp_path: Path) -> None:
    """池里个别币一时拉不到：卡照样记，等权只算拉到的币。"""
    seed_data(tmp_path)
    now = T + timedelta(hours=6)
    r = await C.update(tmp_path, now, FakeFetch({"SOLUSDT": -0.01}, fail={"AAVEUSDT"}))
    assert (r.new, r.failed) == (3, 0)
    sol = next(s for s in C.ScoreStore(tmp_path).read() if s.symbol == "SOLUSDT")
    assert (sol.pool_size, sol.pool_n_4h) == (3, 2)  # SOL、ENA 进了等权，AAVE 没有
    assert sol.coin.h4 is not None and sol.pool.h4 == pytest.approx(sol.coin.h4 / 2)


async def test_own_coin_or_btc_down_is_retried_next_run_not_written(tmp_path: Path) -> None:
    seed_data(tmp_path)
    now = T + timedelta(hours=6)
    r = await C.update(tmp_path, now, FakeFetch({}, fail={"ENAUSDT"}))
    assert (r.new, r.failed) == (1, 2)  # 只有 SOL 那张记上
    assert [s.symbol for s in C.ScoreStore(tmp_path).read()] == ["SOLUSDT"]
    assert not C.ScoreStore(tmp_path).unavailable()  # 一时拉不到不算「没有」
    r = await C.update(tmp_path, now, FakeFetch({}, fail={"BTCUSDT"}))
    assert (r.new, r.failed) == (0, 2)
    r = await C.update(tmp_path, now, FakeFetch({}))
    assert r.new == 2


async def test_symbol_binance_does_not_have_is_cached_and_counted_unpriced(
    tmp_path: Path,
) -> None:
    seed_data(tmp_path)
    now = T + timedelta(hours=6)
    fetch = FakeFetch({}, gone={"ENAUSDT"})
    r = await C.update(tmp_path, now, fetch)
    assert (r.new, r.failed, r.unavailable) == (3, 0, frozenset({"ENAUSDT"}))
    store = C.ScoreStore(tmp_path)
    assert store.unavailable() == {"ENAUSDT"}
    ena = next(s for s in store.read() if s.version == 2)
    assert ena.coin.h4 is None and r.report.n_unpriced == 1
    # 之后不再去拉它
    write_journal(tmp_path, [jrow("ENAUSDT", "short", T + timedelta(minutes=30), version=9)])
    fetch.calls.clear()
    r = await C.update(tmp_path, now, fetch)
    assert r.new == 1 and "ENAUSDT" not in {c[0] for c in fetch.calls}
    assert r.unavailable == frozenset()


async def test_bad_daily_pack_is_deleted_and_fetched_again(tmp_path: Path) -> None:
    seed_data(tmp_path)
    packs = tmp_path / "history"
    day = (T + timedelta(minutes=2) - timedelta(hours=4)).date()  # 第一个要拉的：AAVE 那天
    bad = packs / "AAVEUSDT" / f"AAVEUSDT-1m-{day.isoformat()}.zip"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"<html>not a zip</html>")
    fetch = FakeFetch({}, badzip=1)
    r = await C.update(tmp_path, T + timedelta(hours=6), fetch, packs=packs)
    assert (r.new, r.failed) == (3, 0) and not bad.exists()
    assert [c[0] for c in fetch.calls[:2]] == ["AAVEUSDT", "AAVEUSDT"]


async def test_cards_before_first_pool_round_get_no_pool(tmp_path: Path) -> None:
    """最早一轮总盘会之前的卡：只算原始，不进超额（不拿后来的池顶替）。"""
    seed_data(tmp_path)
    mm = tmp_path / "market_meeting" / "2026-10-25.jsonl"
    mm.write_text(
        json.dumps(
            {
                "ts": (T + timedelta(minutes=30)).isoformat(),
                "pool_id": "later",
                "pool": [{"symbol": "SOLUSDT"}, {"symbol": "AAVEUSDT"}],
            }
        )
        + "\n"
    )
    r = await C.update(tmp_path, T + timedelta(hours=6), FakeFetch({"SOLUSDT": -0.01}))
    rows = {s.version: s for s in C.ScoreStore(tmp_path).read()}
    assert rows[1].pool_id is None and rows[1].pool.h4 is None and rows[1].pool_size == 0
    assert rows[3].pool_id == "later"
    assert r.report.n_no_pool == 2  # 两张方向卡都在 T+30 分钟之前
    h = r.report.horizons["4h"]
    assert (h.raw.n, h.excess.n, h.market.n) == (2, 0, 0)


def test_dedup_never_hashes_journal_rows() -> None:
    """流水行以后可能带 dict 字段（不能哈希）：去重只按 (卡, 版本, 币)。"""

    class Unhashable(JournalRow):
        def __hash__(self) -> int:
            raise TypeError("unhashable")

    a = Unhashable.model_validate(jrow(version=1).model_dump())
    b = Unhashable.model_validate(jrow(version=2).model_dump())
    a2 = Unhashable.model_validate(jrow(version=1, bias="long").model_dump())
    assert C.dedup([a, b, a2]) == [a2, b]


def test_summary_window_only_counts_recent_cards() -> None:
    rows = [card(1, "short", -0.01, 0.0, hours=-48), card(2, "long", 0.01, 0.0)]
    r = C.summarize(rows, T, since=T - timedelta(hours=24))
    assert (r.n_cards, r.n_long, r.n_short, r.since) == (1, 1, 0, T - timedelta(hours=24))
    assert C.summarize(rows, T).n_cards == 2


def test_summary_tmp_file_is_per_process(tmp_path: Path) -> None:
    """别的进程正在写（这里用占住老的固定临时名模拟）也不影响这次写汇总。"""
    store = C.ScoreStore(tmp_path)
    (store.dir / "summary.tmp").mkdir(parents=True)
    rep = C.Report(generated_at=T, n_cards=1)
    store.write_summary(rep)
    assert store.read_summary() == rep
    assert [p.name for p in store.dir.iterdir() if p.suffix == ".tmp"] == ["summary.tmp"]


async def test_rate_limit_aborts_the_run(tmp_path: Path) -> None:
    from jiaoyi.market.rest import RateLimited

    seed_data(tmp_path)

    async def limited(sym: str, a: datetime, b: datetime) -> list[Bar]:
        raise RateLimited(429, 0.0)

    with pytest.raises(RateLimited):
        await C.update(tmp_path, T + timedelta(hours=6), limited)
    assert not C.ScoreStore(tmp_path).read()


# ---------------------------------------------------------------- 复现复盘的数字


def load_slice(tmp_path: Path) -> dict[str, Any]:
    fx: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    write_journal(tmp_path, [JournalRow.model_validate(r) for r in fx["journal"]])
    write_log(tmp_path, fx["decision_log"])
    return fx


def r3(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


def r1(x: float | None) -> float | None:
    return None if x is None else round(x, 1)


async def test_reproduces_audit_numbers_on_a_slice(tmp_path: Path) -> None:
    """复盘切片：new_head_symbol 的 20 张方向卡 + SOL 的 27 张（各自 4h 有价的），
    用复盘拉的主网 1 分钟收盘价（只留 t0 和 t0+4h 两个点）、复盘的 59 币等权池。
    期望值抄自复盘的 outputs/cards_scored.csv 和 analysis_cards.json。"""
    fx = load_slice(tmp_path)
    closes = {
        s: {datetime.fromtimestamp(int(k) / 1000, UTC): v for k, v in pts.items()}
        for s, pts in fx["closes"].items()
    }

    async def fetch(sym: str, a: datetime, b: datetime) -> list[Bar]:
        return [Bar(t, v) for t, v in closes.get(sym, {}).items() if a <= t <= b]

    pools = C.PoolTimeline(
        [(datetime(2026, 1, 1, tzinfo=UTC), C.Pool("audit", tuple(fx["universe"])))]
    )
    res = await C.update(tmp_path, datetime(2026, 10, 27, 20, 25, tzinfo=UTC), fetch, pools=pools)
    assert res.new == len(fx["expect"]) == 47
    got = {s.key: s for s in C.ScoreStore(tmp_path).read()}
    for e in fx["expect"]:
        s = got[(e["card_id"], e["version"], e["symbol"])]
        assert s.t0 == datetime.fromisoformat(e["t0"])
        assert (s.t0_src, s.conclusion_id) == (e["t0_src"], e["conclusion_id"])
        assert s.coin.h4 == pytest.approx(e["ret_4h"], rel=1e-9)
        if s.refresh_reason == "new_head_symbol":
            assert s.pool.h4 == pytest.approx(e["ew_4h"], rel=1e-9)

    # analysis_cards.json → breakdowns.refresh_reason.new_head_symbol
    nh = C.summarize(
        [s for s in got.values() if s.refresh_reason == "new_head_symbol"], res.report.generated_at
    )
    x, raw = nh.horizons["4h"].excess, nh.horizons["4h"].raw
    assert (x.n, x.blocks, r3(x.hit), r1(x.mean_bp)) == (20, 8, 0.55, -29.7)
    assert x.hit_ci is not None and x.mean_bp_ci is not None
    assert (r3(x.hit_ci[0]), r3(x.hit_ci[1])) == (0.4, 0.667)
    assert (r1(x.mean_bp_ci[0]), r1(x.mean_bp_ci[1])) == (-116.8, 13.2)
    assert (raw.n, r3(raw.hit), r1(raw.mean_bp)) == (20, 0.3, -73.1)
    assert raw.hit_ci is not None and (r3(raw.hit_ci[0]), r3(raw.hit_ci[1])) == (0.136, 0.529)

    # analysis_cards.json → by_symbol.SOLUSDT（4h 原始）
    sol = C.summarize([s for s in got.values() if s.symbol == "SOLUSDT"], res.report.generated_at)
    h = sol.horizons["4h"].raw
    assert (h.n, h.blocks, r3(h.hit), r1(h.mean_bp)) == (27, 9, 0.815, 55.5)
    assert h.hit_ci is not None and h.mean_bp_ci is not None
    assert (r3(h.hit_ci[0]), r3(h.hit_ci[1])) == (0.647, 0.929)
    assert (r1(h.mean_bp_ci[0]), r1(h.mean_bp_ci[1])) == (18.1, 85.2)
