"""持仓管家列只显示判断，市场复核进度回执不覆盖结论；全部使用合成数据。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from jiaoyi.contracts import DecisionRecord, Outcome, PositionRecord, Role
from jiaoyi.panel.hb import HbSnapshot
from jiaoyi.panel.overview import position_views
from tests.conftest import load_example


def _position() -> PositionRecord:
    return PositionRecord.model_validate(load_example("position_record"))


def _records(position: PositionRecord, outcomes: list[Outcome]) -> list[DecisionRecord]:
    return [
        DecisionRecord(
            record_id=f"synthetic-keeper-{i}",
            ts=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=i),
            role=Role.KEEPER,
            symbol=position.symbol,
            position_id=position.position_id,
            outcome=outcome,
            summary="合成管家判断" if outcome is not Outcome.INFO else "合成市场复核回执",
            output={"market_review": "processed"} if outcome is Outcome.INFO else {},
        )
        for i, outcome in enumerate(outcomes)
    ]


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ([Outcome.HOLD, Outcome.INFO], "拿住"),
        ([Outcome.ERROR, Outcome.INFO], "出错"),
        ([Outcome.HOLD, Outcome.ERROR, Outcome.INFO], "出错"),
        ([Outcome.ERROR, Outcome.HOLD, Outcome.INFO], "拿住"),
        ([Outcome.CLOSE, Outcome.INFO], "平仓"),
        ([Outcome.INFO, Outcome.INFO], None),
        ([Outcome.INFO, Outcome.HOLD, Outcome.INFO, Outcome.INFO], "拿住"),
        ([], None),
    ],
)
def test_receipts_never_replace_latest_keeper_verdict(
    outcomes: list[Outcome], expected: str | None
) -> None:
    position = _position()
    records = _records(position, outcomes)
    # 不依赖 Jev 字段判定：到时等硬规则 CLOSE 也必须显示平仓。
    assert all("jev" not in record.output for record in records)
    [view] = position_views([position], records, HbSnapshot(ok=False))
    assert view.keeper == expected


def test_receipts_preserve_peak_and_trail_snapshot() -> None:
    position = _position()
    records = _records(position, [Outcome.HOLD, Outcome.INFO])
    records[0] = records[0].model_copy(
        update={
            "input": {
                "jev": {
                    "snapshot": json.dumps(
                        {
                            "pos": {"peak_bps": 120, "giveback_bps": 30, "peak_r": 2.5},
                            "trail": {"on": True, "active": True},
                        }
                    )
                }
            }
        }
    )
    [before] = position_views([position], records[:1], HbSnapshot(ok=False))
    [after] = position_views([position], records, HbSnapshot(ok=False))
    assert after.keeper == "拿住"
    assert after.keeper_peak_r == before.keeper_peak_r == 2.5
    assert after.keeper_peak_usdt == before.keeper_peak_usdt
    assert after.keeper_giveback_usdt == before.keeper_giveback_usdt
    assert after.keeper_trail_active is before.keeper_trail_active is True


def test_keeper_verdict_does_not_cross_position_id() -> None:
    position = _position()
    unrelated = position.model_copy(update={"position_id": "synthetic-other-position"})
    records = _records(unrelated, [Outcome.ERROR, Outcome.INFO])
    [view] = position_views([position], records, HbSnapshot(ok=False))
    assert view.keeper is None
