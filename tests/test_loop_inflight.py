"""问 Jev 的几百毫秒里情况变了：在途信号按锁里这一刻的状态审。"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path
from typing import Any

from jiaoyi import contracts as c
from jiaoyi.contracts.common import Bias
from jiaoyi.loop import Runner
from tests import fastloop_helpers as h
from tests.test_loop import FakeCards, FakeExecutor, FakeGate, FakeRisk, _app


class SwapDuringJev(FakeGate):
    """问 Jev 时把卡换成 ``after``。"""

    def __init__(self, cards: FakeCards, after: c.BattleCard) -> None:
        super().__init__()
        self.cards, self.after = cards, after

    async def evaluate(
        self, signal: c.CandidateSignal, card: c.BattleCard, now: datetime
    ) -> c.JevResponse:
        self.cards.card = self.after
        return await super().evaluate(signal, card, now)


class SeenRisk(FakeRisk):
    def __init__(self) -> None:
        super().__init__(True)
        self.cards: list[c.BattleCard | None] = []

    def review(self, *args: Any, **kwargs: Any) -> c.RiskVerdict:
        self.cards.append(args[2])
        return super().review()


def _wire(
    tmp_path: Path, now: datetime, after: c.BattleCard
) -> tuple[Runner, FakeExecutor, SeenRisk]:
    app = _app(tmp_path, now)
    ex, risk, cards = FakeExecutor(), SeenRisk(), FakeCards(h.card(now))
    app.m = dataclasses.replace(
        app.m, executor=ex, gate=SwapDuringJev(cards, after), risk=risk, cards=cards
    )
    return Runner(app), ex, risk


async def test_new_card_version_during_jev_does_not_open(tmp_path: Path, now: datetime) -> None:
    old = h.card(now)
    r, ex, risk = _wire(tmp_path, now, old.model_copy(update={"version": old.version + 1}))
    await r.handle_signal(h.signal(now))
    assert ex.opened == [] and risk.cards == []  # 连风控员都不用问


async def test_block_during_jev_reaches_guard(tmp_path: Path, now: datetime) -> None:
    old = h.card(now)
    blocked = old.model_copy(
        update={
            "symbols": [
                s.model_copy(update={"bias": Bias.NO_TRADE, "allowed_signals": []})
                for s in old.symbols
            ]
        }
    )
    r, _, risk = _wire(tmp_path, now, blocked)
    await r.handle_signal(h.signal(now))
    # 封币不升版本：风控员拿到的是锁里重新取的卡（只看不做），由它拒绝
    assert risk.cards == [blocked]
