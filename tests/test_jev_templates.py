"""Jev 问题模板：改措辞必须升版本；每次调用的请求和回答带版本号写进决策日志，可以复盘。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.entry import JevEntryGate
from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
from jiaoyi.gate.questions import (
    CRITERIA,
    KNOWN_TEMPLATES,
    MARKET_GLOSSARY,
    TEMPLATE_VERSION,
    KronosSnapshot,
    direction_snapshot,
    hold_questions,
    hold_snapshot,
    template_dump,
    template_fingerprint,
    template_version,
)
from jiaoyi.gate.review import iter_jev_calls
from jiaoyi.keeper.keeper import JevKeeper
from tests.fastloop_helpers import FAST, GOOD_LONG, answer_with, card, features, signal
from tests.test_keeper_patrol import close_p, position


def test_fingerprint_registered_for_current_version() -> None:
    fp = template_fingerprint()
    assert KNOWN_TEMPLATES.get(TEMPLATE_VERSION) == fp, (
        f"Jev 问题模板变了（指纹 {fp}）：请把 TEMPLATE_VERSION 升一版，"
        "并在 KNOWN_TEMPLATES 里登记新指纹（旧版本保留）"
    )
    assert len(set(KNOWN_TEMPLATES.values())) == len(KNOWN_TEMPLATES)


def test_dump_has_full_text() -> None:
    d = template_dump()
    assert d["version"] == TEMPLATE_VERSION and d["fingerprint"] == template_fingerprint()
    assert set(d["entry"]) == {q.value for q in c.EntryQuestion}
    assert set(d["hold"]) == {q.value for q in c.HoldQuestion}


async def test_every_call_logged_with_template_and_reviewable(
    tmp_path: Path, now: datetime
) -> None:
    log = JsonlDecisionLog(tmp_path)
    t = MockJevTransport(answer=answer_with(GOOD_LONG))
    model = JevDecisionModel(t, FAST, timeout_ms=200)
    gate = JevEntryGate(model, RoleRecorder(c.Role.GATE, log))
    await gate.evaluate(signal(now), card(now), now)  # 成功
    t.fail = RuntimeError("down")
    await gate.evaluate(signal(now), card(now), now)  # 失败
    await gate.evaluate(
        signal(now),
        card(now, bias="no_trade").model_copy(update={"symbols": card(now).symbols[1:]}),
        now,
    )  # 没有作战卡里的标的 → 不调用

    hold = MockJevTransport(answer=close_p(0.3))
    snap = features(now)
    keeper = JevKeeper(
        JevDecisionModel(hold, FAST, timeout_ms=200),
        FAST,
        lambda s: snap,
        lambda n: card(now),
        RoleRecorder(c.Role.KEEPER, log),
    )
    await keeper.review(position(now), now)

    recs = log.read_day(now.date().isoformat())
    assert len(recs) == 4
    for r in recs:
        assert r.input["jev"]["template"] == TEMPLATE_VERSION
        assert r.input["jev"]["template_fp"] == template_fingerprint()

    calls = list(iter_jev_calls(tmp_path))
    assert [(x.record.role, x.kind, x.ok) for x in calls] == [
        (c.Role.GATE, "entry", True),
        (c.Role.GATE, "entry", False),
        (c.Role.KEEPER, "hold", True),
    ]
    assert calls[0].p("take", "take") == 0.75 and calls[0].model == "mock-jev"
    assert calls[1].answers == {}
    assert calls[2].p("action", "close") == 0.3
    assert calls[0].record.input["jev"]["snapshot"]  # 完整快照可以重放
    assert "kronos_snapshot" not in calls[0].record.input["jev"]  # entry gate: plain template
    assert calls[2].record.input["jev"]["kronos_snapshot"] == "direction"


def test_keeper_kronos_variants_are_registered_and_distinct() -> None:
    assert template_version("direction") == TEMPLATE_VERSION
    assert template_fingerprint("direction") == template_fingerprint()
    variants: tuple[KronosSnapshot, ...] = ("rank", "off")
    for mode in variants:
        v = template_version(mode)
        assert v != TEMPLATE_VERSION
        assert KNOWN_TEMPLATES.get(v) == template_fingerprint(mode)
        assert template_dump(mode)["version"] == v
    assert len({template_fingerprint(m) for m in ("direction", *variants)}) == 3


@pytest.mark.parametrize(
    ("mode", "version"), [("direction", "q11"), ("rank", "q11-krank"), ("off", "q11-koff")]
)
async def test_keeper_record_carries_kronos_mode_and_template_variant(
    tmp_path: Path, now: datetime, mode: Any, version: str
) -> None:
    log = JsonlDecisionLog(tmp_path)
    keeper = JevKeeper(
        JevDecisionModel(MockJevTransport(answer=close_p(0.3)), FAST, timeout_ms=200),
        FAST,
        lambda s: features(now),
        lambda n: card(now),
        RoleRecorder(c.Role.KEEPER, log),
        kronos_snapshot=mode,
    )
    await keeper.review(position(now), now)
    (rec,) = log.read_day(now.date().isoformat())
    jev = rec.input["jev"]
    assert jev["kronos_snapshot"] == mode and jev["template"] == version
    assert jev["template_fp"] == KNOWN_TEMPLATES[version]


def _keys(d: Any) -> set[str]:
    if not isinstance(d, dict):
        return set()
    return set(d) | {k for v in d.values() for k in _keys(v)}


def _covered(key: str, text: str) -> bool:
    """``key`` is named in backticks, alone or as a dotted part (`kronos.rank`, `*_bps`)."""
    return re.search(rf"[`.]{re.escape(key)}[`.]", text) is not None


def _fullest_hold_snapshots(now: datetime, kronos: KronosSnapshot) -> list[dict[str, Any]]:
    pos = position(now, trailing_activation_pct=0.2, trailing_stop_pct=0.1)
    f = features(
        now,
        price=68204.0,
        funding_rate=0.0001,
        vol_ratio_10s=3.0,
        oi15=0.5,
        oi60=1.0,
        oi4h=2.0,
        top_ls=1.2,
        crowd_ls=0.9,
        div4h=0.3,
        btc5z=1.5,
        liq_long_usdt_1m=1000.0,
        liq_short_usdt_1m=2000.0,
    )
    view = c.MarketView(
        market_view_id="v",
        version=3,
        observed_at=now,
        source_ts=now,
        regime="4h 中性",
        regime_direction="中性",
        source="market_desk",
    )
    fc = c.Forecast(
        symbol="BTCUSDT",
        ts=now,
        horizon_min=60,
        price_low=67000.0,
        price_high=69000.0,
        p_up=0.7,
        rank_pct=0.9,
        rank_n=21,
        rank_view="top",
        model="Kronos-small",
        stale_after=now + timedelta(minutes=5),
    )
    sig = c.AgainstSignalEvent(
        symbol="BTCUSDT", ts=now, kind=c.SignalKind.VOLUME_BREAKOUT, side=c.Side.SHORT,
        vol_ratio=4.0, strong=True,
    )  # fmt: skip
    full_card = card(now, source_market_view_id="v", source_regime="r")
    other_card = full_card.model_copy(update={"symbols": full_card.symbols[1:]})
    return [
        json.loads(
            hold_snapshot(
                pos,
                f,
                crd,
                now,
                against=sig,
                history=[0.0, 10.0, 35.0, 30.0],
                entry={"sig": "volume_surge", "why": "x", "p_real": 0.4, "p_take": 0.3},
                forecast=fc,
                current_market=view,
                market_intra="up",
                market_age={"age_min": 12.0, "stale": "stale"},
                kronos=kronos,
            )
        )
        for crd in (full_card, other_card, None)
    ]


@pytest.mark.parametrize("kronos", ["direction", "rank", "off"])
def test_hold_prompt_glossary_explains_every_hold_snapshot_key(
    now: datetime, kronos: KronosSnapshot
) -> None:
    text = " ".join(q.prompt for q in hold_questions(kronos))
    snaps = _fullest_hold_snapshots(now, kronos)
    keys = set().union(*(_keys(s) for s in snaps))
    assert {"against", "trail", "path_bps", "oi4h", "within_4h", "age_min", "status"} <= keys
    if kronos != "off":
        assert "kronos" in keys
    assert sorted(k for k in keys if not _covered(k, text)) == []


def test_market_glossary_explains_every_direction_snapshot_key(now: datetime) -> None:
    f = features(now, funding_rate=0.0001, vol_ratio_10s=3.0, oi15=0.5, top_ls=1.2, btc5z=1.5)
    keys = _keys(json.loads(direction_snapshot("BTCUSDT", f)))
    assert {"book", "fund_bps", "vol10_x", "btc5z"} <= keys
    assert sorted(k for k in keys if not _covered(k, MARKET_GLOSSARY)) == []


NUDGES = (
    "agree",
    "p_real",
    "p_take",
    "not by itself a reason",
    "favours",
    "OPPOSITE",
    "supports the trade",
    "weigh",
    "rely on",
    "keep holding or close",
    "positive when up",
)


@pytest.mark.parametrize("kronos", ["direction", "rank", "off"])
def test_hold_question_and_snapshot_carry_no_nudges(now: datetime, kronos: KronosSnapshot) -> None:
    qs = hold_questions(kronos)
    text = " ".join(q.prompt for q in qs) + json.dumps([CRITERIA[q.id] for q in qs])
    snaps = [json.dumps(s) for s in _fullest_hold_snapshots(now, kronos)]
    assert [w for w in NUDGES if w in text or any(w in s for s in snaps)] == []
    if kronos == "direction":
        assert json.loads(snaps[0])["kronos"] == {"p_up": 0.7, "rank": "top"}
    assert json.loads(snaps[0])["entry"] == {"sig": "volume_surge", "why": "x"}


def test_hold_questions_choice_order_is_seeded_by_the_snapshot() -> None:
    def order(seed: str | None) -> tuple[str, ...]:
        return {q.id: q.choices for q in hold_questions("direction", seed)}["action"]

    assert order(None) == ("hold", "close")
    orders = {order(f"snapshot-{i}") for i in range(20)}
    assert orders == {("hold", "close"), ("close", "hold")}
    assert all(order(f"snapshot-{i}") == order(f"snapshot-{i}") for i in range(20))
    flipped = next(f"snapshot-{i}" for i in range(20) if order(f"snapshot-{i}")[0] == "close")
    assert [q.choices for q in hold_questions("direction", flipped)] == [
        ("no", "yes"),
        ("high", "low"),
        ("close", "hold"),
    ]
