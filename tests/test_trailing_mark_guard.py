"""移动止损防插针：峰值前核对标记价、挂新止损前核对标记价一侧、最小换代步长、持续缺盘口升级告警。

只有合成订单、合成回包和假时钟，不连接任何服务。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from jiaoyi.contracts import Side
from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.execution import hb_executor
from jiaoyi.execution.brackets import BracketManager
from jiaoyi.execution.exchange import ExchangeError, MarkEvidence
from jiaoyi.execution.trailing import TrailingBookUnavailable
from jiaoyi.panel.protection import parse_protection
from tests.conftest import ROOT
from tests.test_exchange_execution_evidence import NOW as RAW_NOW
from tests.test_exchange_execution_evidence import client, freeze
from tests.test_execution import START
from tests.test_panel import NOW as PANEL_NOW
from tests.test_panel_server_protection import _armed
from tests.test_server_protection_regression import Harness
from tests.test_trailing_book_fresh import SlowHarness

LONG_SHORT = pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])


async def _observe(h: SlowHarness) -> bool:
    basis = await h.entry_basis("BTCUSDT", "entry-1")
    return await h.manager.observe_trailing("pos-1", basis, h.execution_book)


def _trail(h: Harness, stop: float) -> None:
    h.manager._save("pos-1", trailing_activated=True, trailing_stop_price=stop)


def _stop_cid(h: Harness) -> str:
    return next(cid for cid in h.exchange.orders if cid.startswith("br-sl-"))


# --- (a) 算峰值前核对标记价 -------------------------------------------------------------


@LONG_SHORT
async def test_book_spike_against_mark_cannot_activate_or_lift_the_peak(
    tmp_path: Path, side: Side
) -> None:
    h = SlowHarness(tmp_path, side, latency=0.0)
    await h.armed()
    # Demo 插针：多仓买一冲到标记价上方约 2%，空仓卖一砸到下方约 2%；标记价 100.45 没动。
    h.bid, h.ask = (102.5, 102.6) if side is Side.LONG else (98.3, 98.4)
    with pytest.raises(TrailingBookUnavailable, match="疑似插针"):
        await _observe(h)
    state = h.state()
    assert not state.trailing_activated and state.trailing_peak_net is None
    assert state.trailing_stop_price is None and state.trailing_source_at is None
    assert state.trailing_last_error is not None and "疑似插针" in state.trailing_last_error


async def test_mark_check_zero_is_the_old_unchecked_behaviour(tmp_path: Path) -> None:
    h = SlowHarness(tmp_path, Side.LONG, latency=0.0, trailing_book_mark_max_dev_bps=0)
    await h.armed()
    h.bid, h.ask = 102.5, 102.6
    assert await _observe(h)  # 不比对：插针照样把峰值抬上去（所以默认不是 0）
    assert h.state().trailing_activated


@LONG_SHORT
async def test_wide_spread_in_the_conservative_direction_is_not_blocked(
    tmp_path: Path, side: Side
) -> None:
    h = SlowHarness(tmp_path, side, latency=0.0)
    await h.armed()
    h.bid, h.ask = 99.9, 101.0  # 价差约 110 基点，但买一低于 / 卖一高于标记价：峰值只会更保守
    await _observe(h)
    assert h.state().trailing_source_at is not None and h.state().trailing_last_error is None


@pytest.mark.parametrize("problem", ["error", "stale", "future"])
async def test_unknown_mark_skips_the_round_without_touching_the_peak(
    tmp_path: Path, problem: str
) -> None:
    h = SlowHarness(tmp_path, Side.LONG, latency=0.0)
    await h.armed()
    if problem == "error":
        h.mark_error = ExchangeError("同环境标记价证据未知")
    else:
        shift = timedelta(seconds=-6 if problem == "stale" else 1)

        async def bad_mark(symbol: str) -> MarkEvidence:
            return MarkEvidence(symbol, 100.45, h.now + shift, h.now + shift)

        h.exchange.execution_mark = bad_mark
    with pytest.raises(TrailingBookUnavailable, match="标记价不可用"):
        await _observe(h)
    assert h.state().trailing_peak_net is None and h.state().trailing_source_at is None


# --- (b) 挂新一代止损前核对标记价一侧 -----------------------------------------------------


@LONG_SHORT
async def test_new_trailing_stop_beyond_mark_is_not_submitted(tmp_path: Path, side: Side) -> None:
    h = Harness(tmp_path, side)
    long = side is Side.LONG
    assert await h.manager.sync("pos-1") == "armed"
    old_sl = _stop_cid(h)
    _trail(h, 101.5 if long else 98.5)
    for mark in (101.4, 101.53) if long else (98.6, 98.47):  # 已越过；没越过但在 5 基点缓冲内
        h.exchange.mark = mark
        assert await h.manager.sync("pos-1") == "armed"
        assert len(h.creates()) == 3 and not h.cancels()  # 没靠交易所拒单（-2021）去平仓
        assert h.exchange.orders[old_sl].algo_status == "NEW"
        assert h.state().stage == "armed" and "安全一侧" in (h.state().last_error or "")
    h.exchange.mark = 102.5 if long else 97.5
    assert await h.manager.sync("pos-1") == "armed"
    new_sl = h.creates()[-1]
    assert new_sl.startswith("br-sl-") and h.cancels() == [old_sl]
    assert h.exchange.orders[new_sl].trigger_price == (101.5 if long else 98.5)
    assert h.state().last_error is None


async def test_unreadable_mark_keeps_the_live_stop(tmp_path: Path) -> None:
    h = Harness(tmp_path, Side.LONG)
    assert await h.manager.sync("pos-1") == "armed"
    _trail(h, 101.5)

    async def unknown(symbol: str) -> MarkEvidence:
        raise ExchangeError("同环境标记价证据未知")

    h.exchange.execution_mark = unknown
    assert await h.manager.sync("pos-1") == "armed"
    assert len(h.creates()) == 3 and "标记价不可用" in (h.state().last_error or "")


# --- 最小换代步长 ------------------------------------------------------------------------


@LONG_SHORT
async def test_tiny_trailing_improvement_does_not_replace_the_stop(
    tmp_path: Path, side: Side
) -> None:
    h = Harness(tmp_path, side)
    long = side is Side.LONG
    assert await h.manager.sync("pos-1") == "armed"  # 固定止损 99 / 101
    _trail(h, 99.03 if long else 100.97)  # 只收紧 3 基点 < 默认 5
    assert await h.manager.sync("pos-1") == "armed"
    assert len(h.creates()) == 3 and not h.cancels() and h.exchange.mark_reads == 0
    _trail(h, 99.06 if long else 100.94)  # 6 基点
    assert await h.manager.sync("pos-1") == "armed"
    assert len(h.creates()) == 4
    assert h.exchange.orders[h.creates()[-1]].trigger_price == (99.06 if long else 100.94)


async def test_min_step_zero_replaces_on_any_change(tmp_path: Path) -> None:
    h = Harness(tmp_path, Side.LONG)
    h.manager = BracketManager(
        h.exchange,
        h.registry,
        account_name="synthetic-account",
        connector_name="synthetic-demo",
        clock=lambda: START,
        trailing_min_step_bps=0,
    )
    assert await h.manager.sync("pos-1") == "armed"
    _trail(h, 99.01)
    assert await h.manager.sync("pos-1") == "armed"
    assert len(h.creates()) == 4  # 旧行为：一变就换


# --- 持续缺盘口升级告警 ------------------------------------------------------------------


async def test_persistent_book_failure_escalates_with_last_success_time(tmp_path: Path) -> None:
    h = SlowHarness(tmp_path, Side.LONG, latency=0.0)
    await h.armed()
    last_ok = START - timedelta(hours=1)
    h.manager._save("pos-1", trailing_activated=True, trailing_observed_at=last_ok)
    h.book_error = ExchangeError("同环境执行盘口证据未知")
    await h.adapter.protection_tick()
    h.now += timedelta(seconds=299)
    await h.adapter.protection_tick()
    assert [a.level for a in h.alerter.sent] == [AlertLevel.WARN, AlertLevel.WARN]
    assert h.state().trailing_failing_since == START  # 从第一次失败算，不随每轮刷新
    h.now += timedelta(seconds=2)
    await h.adapter.protection_tick()
    stuck = h.alerter.sent[-1]
    assert stuck.level is AlertLevel.CRITICAL
    assert stuck.dedup_key == "server_trailing_book_stuck:pos-1"
    assert last_ok.strftime("%m-%d %H:%M:%S UTC") in stuck.body
    h.book_error = None
    await h.adapter.protection_tick()
    assert h.state().trailing_failing_since is None and h.state().trailing_last_error is None


@pytest.mark.parametrize("activated,after", [(False, 300.0), (True, 0.0)])
async def test_inactive_trailing_or_zero_setting_stays_warn(
    tmp_path: Path, activated: bool, after: float
) -> None:
    h = SlowHarness(tmp_path, Side.LONG, latency=0.0, trailing_book_critical_after_s=after)
    await h.armed()
    h.manager._save("pos-1", trailing_activated=activated)
    h.book_error = ExchangeError("同环境执行盘口证据未知")
    await h.adapter.protection_tick()
    h.now += timedelta(hours=2)
    await h.adapter.protection_tick()
    assert {a.level for a in h.alerter.sent} == {AlertLevel.WARN}


# --- 面板与退出清理 ----------------------------------------------------------------------


async def test_leaving_armed_clears_the_trailing_error(tmp_path: Path) -> None:
    h = Harness(tmp_path, Side.LONG)
    assert await h.manager.sync("pos-1") == "armed"
    h.manager._save("pos-1", trailing_last_error="x", trailing_failing_since=START)
    await h.manager.prepare_exit("pos-1")
    assert h.state().trailing_last_error is None and h.state().trailing_failing_since is None


def test_panel_shows_trailing_error_only_while_armed_and_flags_book_errors() -> None:
    book = "TrailingBookUnavailable: 移动止损盘口读取失败"
    view = parse_protection(_armed(trailing_last_error=book), PANEL_NOW, 120)
    assert view is not None and view.trailing_error == book and view.trailing_error_book
    other = "ProtectionIncomplete: 入场基准身份或完整性不一致"
    view = parse_protection(_armed(trailing_last_error=other), PANEL_NOW, 120)
    assert view is not None and view.trailing_error == other and not view.trailing_error_book
    view = parse_protection(_armed(stage="exiting", trailing_last_error=book), PANEL_NOW, 120)
    assert view is not None and view.trailing_error is None


def test_js_says_protection_unaffected_only_for_book_errors() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("JavaScript 渲染回归需要 Node")
    source = (ROOT / "src/jiaoyi/panel/static/app.js").read_text()
    helpers = source[source.index("const TRAIL_TIP =") : source.index("const cardPos =")]
    blocks = {}
    for name, text in (
        ("book", "TrailingBookUnavailable: 移动止损盘口读取失败"),
        ("other", "ProtectionIncomplete: 入场基准身份或完整性不一致"),
    ):
        view = parse_protection(_armed(trailing_last_error=text), PANEL_NOW, 120)
        assert view is not None
        blocks[name] = {"symbol": "BTCUSDT", "protection": json.loads(view.model_dump_json())}
    script = (
        """
const assert = require('node:assert/strict');
const esc = value => String(value);
const price = value => value == null ? '—' : String(value);
const qtyFmt = value => String(value);
const TZ = 'Asia/Bangkok';
const dur = ms => Math.round(ms / 60000) + ' 分';
"""
        + helpers
        + "\nconst P = "
        + json.dumps(blocks)
        + """;
const book = protBlock(P.book), other = protBlock(P.other);
assert(book.includes('已挂的保护不受影响'), book);
assert(other.includes('入场基准身份') && !other.includes('已挂的保护不受影响'), other);
"""
    )
    subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)


# --- 冻结峰值读盘口超时 ------------------------------------------------------------------


async def test_frozen_peak_book_timeout_is_labelled_and_uses_the_book_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_execution import START as RIG_START
    from tests.test_execution import Rig
    from tests.test_frozen_peak_exit_integration import install

    rig = Rig(tmp_path)
    rec = install(rig)
    assert hb_executor.FROZEN_BOOK_MAX_AGE_S == 5.0  # 读盘口最多等 5 秒，不是 12 秒总预算
    monkeypatch.setattr(hb_executor, "FROZEN_BOOK_MAX_AGE_S", 0.02)

    async def never(_symbol: str) -> Any:
        await asyncio.Event().wait()

    rig.adapter._exchange.execution_book.side_effect = never
    result = await asyncio.wait_for(
        rig.adapter.frozen_peak_review(rec.position_id, RIG_START), timeout=1
    )
    assert result is not None and result["detail"] == "evidence_unavailable:book_timeout:"


# --- 交易所标记价读取 --------------------------------------------------------------------


def _premium(**change: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "symbol": "SYNTHUSDT",
        "markPrice": "1.1",
        "indexPrice": "1.1",
        "time": int(RAW_NOW.timestamp() * 1000) - 100,
    }
    raw.update(change)
    return raw


async def test_mark_uses_exchange_time(monkeypatch: pytest.MonkeyPatch) -> None:
    obj, ex = client()
    freeze(monkeypatch)
    ex.fapiPublicGetPremiumIndex.return_value = _premium()
    result = await obj.execution_mark("SYNTHUSDT")
    assert result == MarkEvidence("SYNTHUSDT", 1.1, RAW_NOW - timedelta(milliseconds=100), RAW_NOW)
    ex.fapiPublicGetPremiumIndex.assert_awaited_once_with({"symbol": "SYNTHUSDT"})


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "OTHERUSDT"},
        {"markPrice": "0"},
        {"markPrice": "NaN"},
        {"time": None},
        {"time": RAW_NOW.timestamp() * 1000 + 1},
        {"time": RAW_NOW.timestamp() * 1000 - 5001},
    ],
)
async def test_unknown_or_unfresh_mark_is_rejected(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    obj, ex = client()
    freeze(monkeypatch)
    ex.fapiPublicGetPremiumIndex.return_value = _premium(**change)
    with pytest.raises(ExchangeError, match="标记价证据未知"):
        await obj.execution_mark("SYNTHUSDT")
