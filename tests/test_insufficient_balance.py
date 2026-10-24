"""保证金不够的假开仓（2026-10-24 BTC / ENA 4 笔）：保镖按可用保证金缩单，执行层只认真成交。

hummingbot 核保证金不够时不下单就结束执行器（INSUFFICIENT_BALANCE，order_ids 为空），
``custom_info.current_position_average_price`` 却照样有计划价。以前我们把它当成交：账本记开仓、
挂保命止损、推「开仓成交」，几秒后对账员见币安没仓又记成「手动平」。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.contracts import Outcome
from jiaoyi.contracts.position import CloseReason, PositionStatus
from jiaoyi.execution.hb_executor import filled_entry
from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.pages import build_history
from jiaoyi.risk.guard import RiskGuardImpl
from tests.fastloop_helpers import FAST, RISK, Lots, account, card, jev, signal
from tests.test_execution import START, Rig, intent, open_filled
from tests.test_ledger_fills import _t

# ---------------------------------------------------------------- 执行层：成交判定


def _detail(trading: bool, filled: float, ids: list[str]) -> dict[str, object]:
    info = {"current_position_average_price": 80672.0, "order_ids": ids}
    return {"is_trading": trading, "filled_amount_quote": filled, "custom_info": info}


def test_filled_entry_needs_orders_and_fill() -> None:
    assert filled_entry(_detail(False, 0.0, [])) is None  # 保证金不够：没下单
    assert filled_entry(_detail(False, 0.0, ["o1"])) is None  # 下了单还没成交
    assert filled_entry(_detail(True, 28695.0, ["o1"])) == 80672.0  # 成交了在持仓
    assert filled_entry(_detail(False, 28695.0, ["o1", "o2"])) == 80672.0  # 成交后已平


async def test_insufficient_balance_is_failed_without_lifeboat_or_notice(tmp_path: Path) -> None:
    rig = Rig(tmp_path, notices=True)
    original = rig.api.create_executor

    async def create_then_refuse(cfg: dict, acct: str, marker: str) -> dict:  # type: ignore[type-arg]
        out = await original(cfg, acct, marker)
        rig.api.insufficient_balance(out["executor_id"], planned_price=68044.0)
        return out

    rig.api.create_executor = create_then_refuse  # type: ignore[method-assign]
    records: list[tuple[Outcome, str]] = []
    real_record = rig.adapter._record

    def spy(outcome: Outcome, summary: str, rec: object, **kw: object) -> None:
        records.append((outcome, summary))
        real_record(outcome, summary, rec, **kw)  # type: ignore[arg-type]

    rig.adapter._record = spy  # type: ignore[method-assign]
    rec = await rig.adapter.open(intent())

    assert rec.status is PositionStatus.FAILED
    assert rec.close_reason is None and rec.entry_price is None
    assert rig.ex.placed == []  # 没挂保命止损
    titles = [a.title for a in rig.alerts.sent]
    assert "开仓成交" not in titles and "平仓完成" not in titles
    assert any("开仓没成交" in t for t in titles)
    assert any("保证金不够" in a.body for a in rig.alerts.sent)
    assert any(o is Outcome.ERROR and "保证金不够" in s for o, s in records)
    # 对账员：失败的不算在管持仓，不会再按「外部平仓」记成手动
    await rig.rec.reconcile(rig.t + timedelta(minutes=5))
    assert rig.registry.get(rec.position_id).record.status is PositionStatus.FAILED  # type: ignore[union-attr]


async def test_pending_open_margin_is_taken_off_available(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    before = (await rig.adapter.account()).available_usdt
    await rig.adapter.open(intent(order_type="limit", limit_price=68000.0))  # 还没成交
    it = intent()
    after = (await rig.adapter.account()).available_usdt
    assert after == pytest.approx(max(before - it.notional_usdt / it.leverage, 0.0))


# ---------------------------------------------------------------- 修旧账


async def test_old_phantom_manual_close_becomes_unfilled(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig)
    tracked = rig.registry.get(pid)
    assert tracked is not None
    # 旧代码留下的样子：4 秒后对账员按外部平仓记成手动，没平仓价、盈亏 0
    rig.adapter._set(
        tracked,
        status=PositionStatus.CLOSED,
        close_reason=CloseReason.MANUAL,
        closed_at=START + timedelta(seconds=4),
        realized_pnl_usdt=0.0,
    )
    rig.ex.trades = []
    rig.t = START + timedelta(minutes=2)
    assert await rig.adapter.backfill_fills(rig.ex.user_trades) == 0  # 成交明细可能有延迟，先等
    rig.t = START + timedelta(minutes=10)
    assert await rig.adapter.backfill_fills(rig.ex.user_trades) == 1
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert rec.status is PositionStatus.FAILED and rec.close_reason is None


async def test_real_manual_close_is_not_touched(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig)
    tracked = rig.registry.get(pid)
    assert tracked is not None
    q = tracked.record.quantity
    rig.adapter._set(
        tracked,
        status=PositionStatus.CLOSED,
        close_reason=CloseReason.MANUAL,
        closed_at=START + timedelta(minutes=3),
        realized_pnl_usdt=0.0,
    )
    rig.ex.trades = [_t(1, "buy", 68000.0, q / 2, 0.1)]  # 开仓只查到一半（凑不够数量）
    rig.t = START + timedelta(minutes=10)
    await rig.adapter.backfill_fills(rig.ex.user_trades)
    assert rig.registry.get(pid).record.status is PositionStatus.CLOSED  # type: ignore[union-attr]


# ---------------------------------------------------------------- 保镖：按可用保证金缩单


def test_guard_shrinks_to_available_margin(now: datetime) -> None:
    g = RiskGuardImpl(RISK, FAST, Lots())
    acct = account(now, available_usdt=300.0)
    v = g.review(signal(now), jev(), card(now), acct, now, market=signal(now).features)
    assert v.approved, v.reasons
    it = v.intent
    assert it is not None
    need = it.notional_usdt / it.leverage + it.notional_usdt * RISK.taker_fee_bps / 1e4
    assert need <= 300.0 * (1 - FAST.margin_safety_pct / 100.0) + 1e-9
    # 只缩不拒：缩到上限附近（差不到一个下单步长）
    cap = 300.0 * 0.95 / (1 / it.leverage + RISK.taker_fee_bps / 1e4)
    assert it.notional_usdt > cap - 0.001 * 68_000


def test_guard_rejects_only_below_min_notional(now: datetime) -> None:
    g = RiskGuardImpl(RISK, FAST, Lots())
    v = g.review(
        signal(now), jev(), card(now), account(now, available_usdt=0.5), now,
        market=signal(now).features,
    )  # fmt: skip
    assert not v.approved
    assert any("最小" in r for r in v.reasons), v.reasons


# ---------------------------------------------------------------- 历史页


def test_history_lists_unfilled_but_does_not_count_it(tmp_path: Path) -> None:
    from tests.test_panel import NOW, _rec, _seed

    _seed(tmp_path)
    data = json.loads((tmp_path / "positions.json").read_text())
    data["positions"]["pos-0000"]["record"].update(
        status="failed", close_reason=None, realized_pnl_usdt=0.0
    )
    (tmp_path / "positions.json").write_text(json.dumps(data))
    from jiaoyi.contracts import Role
    from jiaoyi.core.store import JsonlDecisionLog

    sig = data["positions"]["pos-0000"]["record"]["signal_id"]
    JsonlDecisionLog(tmp_path).write(
        _rec(Role.SENTINEL, Outcome.ACTION, "信号", NOW - timedelta(hours=2), signal_id=sig,
             output={"kind": "volume_breakout"})
    )  # fmt: skip
    page = build_history(PanelFiles(tmp_path, RISK.day_reset_tz), RISK, NOW, "today")
    [row] = page.rows
    assert row.unfilled and not row.pnl_missing
    assert page.trades == 0 and page.curve == [] and page.win_rate is None and page.by_kind == []
