"""Jev 问题模板：改措辞必须升版本；每次调用的请求和回答带版本号写进决策日志，可以复盘。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from jiaoyi import contracts as c
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.gate.entry import JevEntryGate
from jiaoyi.gate.jev import JevDecisionModel, MockJevTransport
from jiaoyi.gate.questions import (
    KNOWN_TEMPLATES,
    TEMPLATE_VERSION,
    KronosSnapshot,
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
    ("mode", "version"), [("direction", "q9"), ("rank", "q9-krank"), ("off", "q9-koff")]
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
