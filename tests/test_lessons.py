"""加密版记忆：打分、5 天一批、少于 30 张不写、自动替换、从上线之后攒。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jiaoyi.slow import lessons as L
from jiaoyi.slow.review import Bar, JournalRow

T0 = datetime(2026, 10, 24, 5, 0, tzinfo=UTC)
HOLD = timedelta(minutes=120)


def row(sym="SOLUSDT", bias="short", at=T0, cat="alt") -> JournalRow:
    return JournalRow(
        card_id="c",
        version=1,
        created_at=at,
        expires_at=at + HOLD,
        regime="r",
        refresh_reason="x",
        symbol=sym,
        category=cat,
        bias=bias,
    )


def bars(start: datetime, o: float, c: float, n=24, hi=None, lo=None) -> list[Bar]:
    return [
        Bar(
            start + timedelta(minutes=5 * i),
            o,
            hi or max(o, c),
            lo or min(o, c),
            c if i == n - 1 else o,
        )
        for i in range(n)
    ]


def test_score_short_net_of_fee_and_vs_btc() -> None:
    s = L.score_card(row(), bars(T0, 100, 98), HOLD, 0.08, bars(T0, 100, 99))
    assert s is not None
    assert round(s.dir_net_pct or 0, 2) == 1.92  # 空头，跌 2%，扣 0.08
    assert round(s.vs_btc_pct or 0, 2) == 1.0  # 比 BTC 多跌 1%
    assert L.score_card(row(), [], HOLD, 0.08) is None


def test_no_trade_scored_by_missed_move() -> None:
    s = L.score_card(row(bias="no_trade"), bars(T0, 100, 103, hi=104), HOLD, 0.08)
    assert s and s.dir_net_pct is None and round(s.missed_pct, 1) == 4.0


calls: list[str] = []


def mk_book(tmp_path: Path, rows: int, now: datetime) -> L.LessonBook:
    calls.clear()
    j = tmp_path / "card_review"
    j.mkdir(parents=True)
    with (j / "cards.jsonl").open("w") as f:
        for i in range(rows):
            f.write(row(at=T0 + timedelta(minutes=i)).model_dump_json() + "\n")

    def fetch(sym: str, a: datetime, b: datetime) -> list[Bar]:
        calls.append(sym)
        return bars(a, 100, 99, n=int((b - a) / timedelta(minutes=5)) + 1)

    return L.LessonBook(
        tmp_path,
        tz="Asia/Bangkok",
        hold_minutes=120,
        horizon_zh="2 小时",
        fee_pct=0.08,
        fetch=fetch,
    )


def ask_ok(_p: str) -> list[L.Lesson]:
    return [L.Lesson("别追空", "做空·其他币 40 张，平均 +0.5%"), L.Lesson("", "x")]


def test_due_after_five_bangkok_days_and_threshold(tmp_path: Path) -> None:
    now = T0 + timedelta(days=0)
    b = mk_book(tmp_path, 40, now)
    assert not b.due(now)  # 刚上线，记下 since
    later = now + timedelta(days=5)
    assert b.due(later)
    batch = b.refresh(later, ask_ok)
    assert batch and len(batch.lessons) == 1  # 空文本的丢掉
    files = list((tmp_path / "memory" / "lessons").glob("*.json"))
    assert len(files) == 1 and json.loads(files[0].read_text())["n_cards"] == 40
    assert "别追空" in b.text() and "依据" in b.text()
    assert not b.due(later + timedelta(days=1))  # 5 天内不再来


def test_under_30_cards_writes_nothing_and_keeps_previous(tmp_path: Path) -> None:
    b = mk_book(tmp_path, 10, T0)
    b.since(T0)
    later = T0 + timedelta(days=5)
    assert b.refresh(later, ask_ok) is None
    assert b.text() == ""
    assert not b.due(later)  # 今天已试过，不重复拉 K 线


def test_cards_before_launch_are_ignored(tmp_path: Path) -> None:
    b = mk_book(tmp_path, 40, T0)
    b.since(T0 + timedelta(hours=3))  # 上线晚于所有卡
    assert b.refresh(T0 + timedelta(days=9), ask_ok) is None


def test_new_batch_replaces_old(tmp_path: Path) -> None:
    b = mk_book(tmp_path, 40, T0)
    b.since(T0)
    b.refresh(T0 + timedelta(days=5), ask_ok)
    b.refresh(T0 + timedelta(days=10), lambda _p: [L.Lesson("新教训", "依据")])
    assert "新教训" in b.text() and "别追空" not in b.text()
    assert len(list((tmp_path / "memory" / "lessons").glob("*.json"))) == 2


def test_engine_lessons_and_refresh_failure_is_harmless(tmp_path: Path) -> None:
    from jiaoyi.slow import tradingagents_engine as eng

    e = eng.TradingAgentsEngine.__new__(eng.TradingAgentsEngine)
    assert e._lessons() == ""  # 没有教训本也不出错
    b = mk_book(tmp_path, 40, T0)
    b.since(T0)
    e._book = b
    e._today = lambda: T0 + timedelta(days=5)
    e._s = eng.TradingAgentsSettings()

    def boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("模型挂了")

    e._graph = boom  # type: ignore[method-assign]
    e._refresh_lessons(object())  # 吞掉异常，不影响开会
    assert e._lessons() == ""


def test_one_fetch_per_symbol_and_btc_once(tmp_path: Path) -> None:
    b = mk_book(tmp_path, 40, T0)
    b.since(T0)
    assert b.refresh(T0 + timedelta(days=5), ask_ok) is not None
    assert sorted(calls) == ["BTCUSDT", "SOLUSDT"]  # 40 张卡只拉 2 次


def test_cards_are_capped(tmp_path: Path) -> None:
    b = mk_book(tmp_path, 450, T0)
    b.since(T0)
    sc = b._scored(T0 + timedelta(days=5), T0)
    assert len(sc) == L.MAX_CARDS


def test_rate_limited_aborts_and_writes_nothing(tmp_path: Path) -> None:
    b = mk_book(tmp_path, 40, T0)
    b.since(T0)
    n = []

    def limited(sym: str, a: datetime, c: datetime) -> list[Bar]:
        n.append(sym)
        raise L.binance_gate.RateLimited(429, 0.0)

    b._fetch = limited
    later = T0 + timedelta(days=5)
    try:
        b.refresh(later, ask_ok)
        raise AssertionError("应该抛 RateLimited")
    except L.binance_gate.RateLimited:
        pass
    assert len(n) == 1  # 限流后不再发请求
    assert b.text() == "" and not b.due(later)  # 今天不再试，留到明天


def test_gate_bars_pages_and_sizes_limit() -> None:
    calls_: list[int] = []

    class R:
        def __init__(self, rows: list[list[float]]) -> None:
            self.rows = rows

        def raise_for_status(self) -> None: ...

        def json(self) -> list[list[float]]:
            return self.rows

    def get(url: str, params: dict[str, object]) -> R:
        calls_.append(int(params["limit"]))  # type: ignore[call-overload]
        t0 = int(params["startTime"])  # type: ignore[call-overload]
        n = int(params["limit"])  # type: ignore[call-overload]
        return R([[t0 + i * 300_000, 1, 1, 1, 1] for i in range(n)])

    out = L.gate_bars("SOLUSDT", T0, T0 + timedelta(minutes=120), get)
    assert calls_ == [25] and len(out) == 25  # 2 小时只要 25 根
    calls_.clear()
    L.gate_bars("SOLUSDT", T0, T0 + timedelta(days=5), get)
    assert calls_[0] == 1000 and len(calls_) == 2
