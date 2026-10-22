"""回放默认接线：真实现没做好的部分用假实现。

``python -m jiaoyi.backtest`` 默认从这里取接线。快环工程师的模块做好后，复制一份改成
``from jiaoyi.market import ...``，用 ``--wiring 你的模块:build`` 指过去即可。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from jiaoyi.backtest.engine import CardSource
from jiaoyi.contracts import RiskConfig
from jiaoyi.interfaces import EntryGate, FeatureEngine, RiskGuard, Trigger
from tests.support import builders as b
from tests.support.fakes import FakeEntryGate, RecordedDecisionModel, RuleDecisionModel
from tests.support.reference_risk import ReferenceRiskGuard
from tests.support.simple_pipeline import SimpleFeatureEngine, SimpleTrigger


@dataclass
class Wiring:
    features: FeatureEngine
    trigger: Trigger
    gate: EntryGate
    guard: RiskGuard
    cards: CardSource


def build(
    risk: RiskConfig,
    symbol: str,
    start: datetime,
    *,
    jev: str = "rule:0.5",
    jev_latency_ms: float = 350.0,
) -> Wiring:
    """``jev`` 取值：``rule:<做的概率>``（mock）、``recorded:<jsonl 路径>``（录制答复）。"""
    kind, _, arg = jev.partition(":")
    if kind == "recorded":
        model: object = RecordedDecisionModel(Path(arg))
    else:
        model = RuleDecisionModel(take_prob=float(arg or 0.5), latency_ms=jev_latency_ms)
    card = b.card([b.card_symbol(symbol)], now=start, ttl=timedelta(days=2))
    return Wiring(
        features=SimpleFeatureEngine(),
        trigger=SimpleTrigger(),
        gate=FakeEntryGate(model),
        guard=ReferenceRiskGuard(risk),
        cards=lambda now: card if card.is_valid(now) else None,
    )
