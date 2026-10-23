from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import Bias, Category
from jiaoyi.slow.review import (
    Bar,
    CardJournal,
    JournalRow,
    render,
    score_journal,
    score_row,
    summarize,
)
from tests.test_slow_commander import FakeEngine, entry, make, pool


def row(bias: str = "long", sym: str = "AAUSDT", at: datetime | None = None) -> JournalRow:
    t = at or datetime(2026, 10, 20, tzinfo=__import__("datetime").UTC)
    return JournalRow(
        card_id="card-x",
        version=1,
        created_at=t,
        expires_at=t + timedelta(hours=1),
        regime="震荡",
        refresh_reason="scheduled",
        symbol=sym,
        category="alt",
        bias=bias,
    )


def bars(r: JournalRow, closes: list[float], hi: float = 0, lo: float = 0) -> list[Bar]:
    out = []
    prev = 100.0
    for i, c in enumerate(closes):
        out.append(
            Bar(
                r.created_at + timedelta(minutes=5 * i),
                prev,
                max(prev, c, hi),
                min(prev, c, lo or c),
                c,
            )
        )
        prev = c
    return out


def test_score_long_and_short() -> None:
    r = row("long")
    o = score_row(r, bars(r, [101, 102, 103], hi=104, lo=99))
    assert o is not None and o.entry == 100.0
    assert o.ret_pct == pytest.approx(3.0)
    assert o.mfe_pct == pytest.approx(4.0) and o.mae_pct == pytest.approx(-1.0)
    assert o.dir_ret_pct == pytest.approx(3.0) and o.hit is True
    s = row("short")
    o2 = score_row(s, bars(s, [101, 102, 103]))
    assert o2 is not None and o2.dir_ret_pct == pytest.approx(-3.0) and o2.hit is False


def test_both_and_no_trade_have_no_hit() -> None:
    for b in ("both", "no_trade"):
        r = row(b)
        o = score_row(r, bars(r, [101, 102]))
        assert o is not None and o.dir_ret_pct is None and o.hit is None


def test_bars_outside_window_ignored_and_empty_is_none() -> None:
    r = row()
    late = [Bar(r.expires_at + timedelta(minutes=5), 100, 101, 99, 100)]
    assert score_row(r, late) is None
    assert score_row(r, []) is None


def test_summarize_hit_rate() -> None:
    r = row("long")
    up = score_row(r, bars(r, [101]))
    dn = score_row(r, bars(r, [99]))
    assert up and dn
    s = summarize([up, dn], lambda o: o.row.bias)[0]
    assert s.n == 2 and s.hit_rate == 0.5 and s.mean_dir_ret_pct == pytest.approx(0.0)
    assert "long" in render([up, dn])
    assert "还没有" in render([])


async def test_score_journal_only_expired_and_skips_fetch_errors() -> None:
    old = row("long", "AAUSDT")
    new = row("long", "BBUSDT", at=old.created_at + timedelta(days=1))
    bad = row("long", "CCUSDT")

    async def fetch(sym: str, a: datetime, b: datetime) -> list[Bar]:
        if sym == "CCUSDT":
            raise ValueError("boom")
        return bars(old, [101])

    now = old.expires_at + timedelta(hours=1)
    res = await score_journal([old, new, bad], fetch, now)
    assert [o.row.symbol for o in res] == ["AAUSDT"]  # 新卡没过期，坏的被跳过


def test_journal_roundtrip_and_tolerates_junk(tmp_path: Path) -> None:
    j = CardJournal(tmp_path)
    assert j.read() == []
    assert j.path == tmp_path / "card_review" / "cards.jsonl"
    r = row()
    j.path.parent.mkdir(parents=True)
    j.path.write_text(r.model_dump_json() + "\n\nnot json\n", encoding="utf-8")
    assert j.read() == [r]


async def test_commander_journals_only_fresh_symbols(tmp_path: Path, now: datetime) -> None:
    eng = FakeEngine(["AAUSDT", "BBUSDT"], fail={"BBUSDT"})
    j = CardJournal(tmp_path)
    c, _, _ = make(tmp_path, eng, now, journal=j)
    await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT", cat=Category.ALT)), "scheduled")
    rows = j.read()
    assert [r.symbol for r in rows] == ["AAUSDT"]  # 开会失败的不记
    assert rows[0].bias == Bias.LONG.value and rows[0].refresh_reason == "scheduled"
