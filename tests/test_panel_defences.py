"""总览「防线状态」：每条防线的开关、此刻、今天的次数、最近一次（经 build_overview）。"""

from __future__ import annotations

import json
import math
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi import contracts as c
from jiaoyi.core.market_trail import Leg, TrailSeries
from jiaoyi.core.recorder import local_day
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.panel.defences import market_trail_view
from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.overview import build_overview
from tests.test_panel import HB, NOW, RISK, _card_cs, _cfg, _client, _position, _rec

T = lambda h, m=0: datetime(2026, 10, 22, h, m, tzinfo=UTC)  # noqa: E731


def _native_leg(**kw: Any) -> dict[str, Any]:
    return {"kind": "trailing_stop", "purpose": "trailing", "generation": 1, "state": "confirmed",
            "algo_id": "1", "algo_status": "NEW", "child_state": "absent", "trigger_price": 1.0,
            **kw}  # fmt: skip


def _paper(pid: str, side: str, entry: float, stop: float, booked: datetime) -> dict[str, Any]:
    return {"position_id": pid, "symbol": "DOGEUSDT", "side": side, "quantity": 10.0,
            "entry_price": entry, "stop_price": stop, "stop_source": "ledger",
            "price_source": "mark", "cross_price": stop, "crossed_at": booked.isoformat(),
            "booked_at": booked.isoformat(), "slippage_bps": 0.0, "paper_exit_price": stop,
            "close_requested": True}  # fmt: skip


def _seed(tmp_path: Path) -> None:
    btc = _position(position_id="open1", symbol="BTCUSDT", opened_at="2026-10-22T05:00:00Z")
    btc["server_protection"] = {"stage": "armed", "legs": [_native_leg()]}
    btc["frozen_peak_exit"] = {"status": "frozen"}
    eth = _position(position_id="open2", symbol="ETHUSDT", side="short",
                    opened_at="2026-10-22T05:10:00Z")  # fmt: skip
    eth["server_protection"] = {"stage": "armed", "legs": [], "native_fallback": "交易所拒单"}
    eth["frozen_peak_exit"] = {"status": "collecting"}
    sol = _position(position_id="shut1", symbol="SOLUSDT", opened_at="2026-10-22T04:00:00Z",
                    status="closed", close_reason="trailing_stop", closed_at="2026-10-22T06:00:00Z",
                    realized_pnl_usdt=5.0, exit_price=68100.0)  # fmt: skip
    sol["server_protection"] = {"stage": "done", "legs": [_native_leg(algo_id="2",
                                algo_status="FINISHED", actual_order_id="9")]}  # fmt: skip
    sol["frozen_peak_exit"] = {"status": "triggered"}
    (tmp_path / "positions.json").write_text(
        json.dumps({"positions": {"open1": btc, "open2": eth, "shut1": sol}})
    )
    (tmp_path / "paper_stops.json").write_text(json.dumps({
        "pp1": _paper("pp1", "long", 1.0, 1.1, T(3)),  # 止损在盈利一侧
        "pp2": _paper("pp2", "short", 1.0, 1.05, T(4)),
        "pp3": _paper("pp3", "long", 1.0, 0.9, NOW - timedelta(hours=20)),  # 曼谷昨天
    }))  # fmt: skip

    series = TrailSeries(xs=deque([4.0, 4.012]), last_t=int(T(6, 29).timestamp()), count=2000,
                         atr=0.01, leg=Leg(side=-1, extreme=4.0, extreme_at=0, stopped=True,
                                           stopped_at=int(T(6, 18).timestamp())))  # fmt: skip
    (tmp_path / "market_trail.json").write_text(
        json.dumps({"version": 1, "series": {"btc": series.to_json()}})
    )

    def meet(sym: str, ts: datetime, bias: str) -> c.DecisionRecord:
        return _rec(c.Role.COMMANDER, c.Outcome.ACTION, f"{sym} 开会结论：{bias}", ts,
                    symbol=sym, output=_card_cs(sym, bias, f"{sym} {bias}"))  # fmt: skip

    def keeper(pid: str, ts: datetime, outcome: c.Outcome, out: dict[str, Any]) -> c.DecisionRecord:
        return _rec(c.Role.KEEPER, outcome, f"XRPUSDT 管家 {pid} {ts:%H%M}", ts,
                    symbol="XRPUSDT", position_id=pid, output=out)  # fmt: skip

    def held(pid: str, sym: str, ts: datetime, trigger: str, queued: bool) -> c.DecisionRecord:
        return _rec(c.Role.PATROL, c.Outcome.ACTION if queued else c.Outcome.INFO,
                    f"{sym} {trigger} {ts:%H%M}", ts, symbol=sym, position_id=pid,
                    output={"held_event": {"trigger": trigger, "queued": queued}})  # fmt: skip

    lock = {"market_trail": {"kind": "lock"}}
    log = JsonlDecisionLog(tmp_path)
    for r in [
        _rec(c.Role.RISK_GUARD, c.Outcome.INFO, "大盘移动止损：昨天锁的", NOW - timedelta(hours=14),
             output=lock),
        _rec(c.Role.RISK_GUARD, c.Outcome.INFO, "大盘移动止损：BTC 反弹 1.2 ATR，暂停新空单",
             T(6, 18), output=lock),
        _rec(c.Role.RISK_GUARD, c.Outcome.REJECT, "XRPUSDT 大盘移动止损：暂停新空单", T(6, 20),
             symbol="XRPUSDT", output={"approved": False, "reasons": ["大盘移动止损：暂停新空单"],
                                       "detail": {"by": "market_trail"}}),
        _rec(c.Role.RISK_GUARD, c.Outcome.REJECT, "XRPUSDT 点差太大", T(6, 21), symbol="XRPUSDT",
             output={"approved": False, "reasons": ["点差太大"]}),
        keeper("x1", T(4, 30), c.Outcome.CLOSE, {"frozen_peak_exit": {"triggered": True}}),
        keeper("x1", T(4, 31), c.Outcome.CLOSE, {"frozen_peak_exit": {"triggered": True}}),
        keeper("x2", T(3), c.Outcome.CLOSE, {"card_rule": {"kind": "opposite_card"}}),
        keeper("x3", T(3, 5), c.Outcome.HOLD, {"card_rule": {"kind": "opposite_card"}}),
        meet("ETHUSDT", T(4), "short"),
        held("open2", "ETHUSDT", T(5, 20), "held_reverse_loss", True),
        held("open1", "BTCUSDT", T(5, 25), "held_desk_flip", False),
        held("open1", "BTCUSDT", T(5, 26), "held_desk_flip", True),
        meet("ETHUSDT", T(6), "long"),
    ]:  # fmt: skip
        log.write(r)


def _rows(tmp_path: Path, **fast: Any) -> dict[str, Any]:
    cfg = _cfg(tmp_path)
    cfg = cfg.model_copy(update={"fast_loop": cfg.fast_loop.model_copy(update=fast)})
    ov = build_overview(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, cfg, NOW)
    return {r.key: r for r in ov.defences}


ON = {
    "signal_market_trail_on": True, "signal_market_trail_ref": "btc", "signal_market_trail_k": 1.0,
    "signal_market_ma_on": False, "profit_lock_on": False,
    "trailing_on": True, "trailing_native": True, "trail_start_r": 0.5,
    "frozen_peak_exit_on": True, "frozen_peak_exit_after_s": 10800,
    "opposite_card_action": "close", "patrol_held_event_loss_r": 0.1,
    "patrol_held_event_on_desk_flip": True, "paper_stop_on_mainnet": "mark",
}  # fmt: skip


def test_defences_count_todays_events_per_rule(tmp_path: Path) -> None:
    _seed(tmp_path)
    rows = _rows(tmp_path, **ON)
    assert list(rows) == ["market_trail", "trailing_native", "profit_lock", "frozen_peak",
                          "opposite_card",
                          "held_event", "paper_stop", "daily_halt"]  # fmt: skip

    def view(key: str) -> tuple[Any, ...]:
        r = rows[key]
        return (r.on, r.setting, r.now, r.level, r.today, r.last_ts, r.last_text, r.link)

    assert view("market_trail") == (
        True, "参照 BTC · 1 倍 ATR",
        "空单锁住中 · 已锁 12 分钟 · BTC 现在离低点 1.2 / 门槛 1.0 ATR", "warn",
        "锁 1 次（解锁：新极值 0 · 作废 0 · 新段 0） · "
        "拦下开仓 1 笔（均线 0 · 移动止损 1 · 两者 0）",
        T(6, 20), "XRPUSDT 大盘移动止损：暂停新空单", "#team/risk_guard",
    )  # fmt: skip
    assert view("trailing_native") == (
        True, "0.5R 启动", "币安跟着 1 笔；没用上、程序自己跟 1 笔", "warn",
        "挂上 2 · 没用上 1 · 移动止损平 1 笔", T(6), "SOLUSDT 移动止损平仓", "#positions",
    )  # fmt: skip
    assert view("frozen_peak") == (
        True, "持仓满 180 分钟冻结", "收集中 1 笔 · 已冻结 1 笔", "active", "到价平 1 笔",
        T(4, 31), "XRPUSDT 管家 x1 0431", "#positions",
    )  # fmt: skip
    assert view("opposite_card") == (
        True, "卡反就全平", "卡和持仓反着的 1 笔", "warn", "平 1 笔 · 收紧 1", T(3, 5),
        "XRPUSDT 管家 x3 0305", "#team/keeper",
    )  # fmt: skip
    assert view("held_event") == (
        True, "亏 ≥0.1R + 反向信号 · 大盘台转向 · 持仓在亏", "在亏的持仓 1 笔", "active",
        "申请 2 场（大盘台转向 1 · 反向信号 1 · 没开成 1）· 卡翻了 1", T(5, 26),
        "BTCUSDT held_desk_flip 0526", "#team/patrol",
    )  # fmt: skip
    assert view("paper_stop") == (
        True, "按主网标记价判", "固定止损 + 主网价同时盯着", "ok", "平 2 笔（盈利侧 1 · 亏损侧 1）",
        T(4), "DOGEUSDT 主网价 1.05 碰到亏损侧（止损） 1.05", "#history",
    )  # fmt: skip
    halt = rows["daily_halt"]
    assert (halt.on, halt.setting, halt.today, halt.last_ts) == (
        True,
        "日亏 3% 停开新仓",
        "未触发",
        None,
    )


def test_defences_switched_off_show_config_key_and_no_counts(tmp_path: Path) -> None:
    _seed(tmp_path)
    rows = _rows(
        tmp_path,
        **{**ON, "signal_market_trail_on": False, "trailing_native": False,
           "frozen_peak_exit_on": False, "patrol_held_event_loss_r": 0.0,
           "patrol_held_event_on_desk_flip": False, "paper_stop_on_mainnet": "off"},
    )  # fmt: skip
    off = {
        k: (r.config_key, r.level, r.now, r.today, r.last_ts) for k, r in rows.items() if not r.on
    }
    assert off == {
        "market_trail": ("fast_loop.signal_market_trail_on", "off", "", "", None),
        "trailing_native": ("fast_loop.trailing_native", "off", "", "", None),
        "profit_lock": ("fast_loop.profit_lock_on", "off", "", "", None),
        "frozen_peak": ("fast_loop.frozen_peak_exit_on", "off", "", "", None),
        "held_event": ("fast_loop.patrol_held_event_loss_r", "off", "", "", None),
        "paper_stop": ("fast_loop.paper_stop_on_mainnet", "off", "", "", None),
    }
    assert rows["opposite_card"].on and rows["daily_halt"].on


def test_defences_market_trail_without_state_file(tmp_path: Path) -> None:
    _seed(tmp_path)
    (tmp_path / "market_trail.json").unlink()
    r = _rows(tmp_path, **ON)["market_trail"]
    assert (r.now, r.level) == ("还没有状态（主程序没跑过这一版）", "idle")


def test_team_page_gate_line_and_status_bar(tmp_path: Path) -> None:
    """团队页风控员节点固定一行大盘闸门，详情顶上是完整状态（经 /api/team）。"""
    _seed(tmp_path)
    cfg = _cfg(tmp_path)
    on = cfg.model_copy(update={"fast_loop": cfg.fast_loop.model_copy(update=ON)})
    with _client(tmp_path, "10.0.0.2", cfg=on) as client:
        mt = client.get("/api/team?role=risk_guard").json()["market_trail"]
    assert {k: mt[k] for k in ("on", "state", "leg", "guards", "locked_min", "line", "level",
                               "locks_today", "blocked_today", "stale_at")} == {
        "on": True, "state": "locked", "leg": "跌段", "guards": "空单", "locked_min": 12,
        "line": "大盘闸门：空单暂停 · 移动止损锁 12 分钟", "level": "warn", "locks_today": 1,
        "blocked_today": 1, "stale_at": None,
    }  # fmt: skip
    assert round(mt["extreme_price"], 4) == 54.5982  # e^4.0
    assert mt["retrace_atr"] == 1.2

    with _client(tmp_path, "10.0.0.2", cfg=cfg) as client:
        off = client.get("/api/team").json()["market_trail"]
    assert (off["on"], off["line"]) == (False, "大盘闸门：未启用")


def test_market_trail_open_leg_line_and_stale_data(tmp_path: Path) -> None:
    series = TrailSeries(xs=deque([4.0, 3.993]), last_t=int(T(6, 0).timestamp()), count=2000,
                         atr=0.01, leg=Leg(side=1, extreme=4.0, extreme_at=0))  # fmt: skip
    (tmp_path / "market_trail.json").write_text(
        json.dumps({"version": 1, "series": {"eth": series.to_json()}})
    )
    cfg = _cfg(tmp_path)
    on = cfg.model_copy(update={"fast_loop": cfg.fast_loop.model_copy(
        update={"signal_market_trail_on": True, "signal_market_trail_ref": "eth",
                    "signal_market_trail_k": 1.0, "signal_market_ma_on": False})})  # fmt: skip
    v = market_trail_view(tmp_path, on, [], NOW, "Asia/Bangkok")
    assert (v.state, v.line, v.now, v.level) == (
        "open", "大盘闸门：多单放行 0.7/1 ATR",
        "ETH 涨段，多单放行（离高点 0.7 / 1 ATR）；数据停在 13:00", "ok",
    )  # fmt: skip
    assert v.stale_at == T(6, 0)


def _view(tmp_path: Path, records: list[c.DecisionRecord], series: TrailSeries) -> Any:
    (tmp_path / "market_trail.json").write_text(
        json.dumps({"version": 1, "series": {"btc": series.to_json()},
                    "today": {"day": local_day(NOW, "Asia/Bangkok").isoformat(),
                              "minutes": 100, "both": 25}})
    )  # fmt: skip
    cfg = _cfg(tmp_path)
    fast = cfg.fast_loop.model_copy(update={"signal_market_trail_on": True})
    return market_trail_view(
        tmp_path, cfg.model_copy(update={"fast_loop": fast}), records, NOW, "Asia/Bangkok"
    )


def _ema_above() -> TrailSeries:
    s = TrailSeries(atr=0.01, last_t=int(NOW.timestamp()), count=41)
    s.xs.extend([math.log(100.0)] * 40 + [math.log(100.3)])
    return s


def test_market_trail_view_shows_the_ema_while_the_trail_warms_up(tmp_path: Path) -> None:
    v = _view(tmp_path, [], _ema_above())
    assert (v.state, v.verdict, v.ema_block) == ("warmup", "空单暂停", "空单")
    assert v.line == "大盘闸门：空单暂停 · 均线"
    assert v.ema_text == "大盘均线：BTC 在 30 分钟均线上方 0.3 ATR，暂停新空单"
    assert v.both_pct == 25.0


def test_market_trail_view_ema_stale_says_so(tmp_path: Path) -> None:
    s = _ema_above()
    s.last_t = int(NOW.timestamp()) - 600
    v = _view(tmp_path, [], s)
    assert (v.verdict, v.ema_block) == ("不拦", None)
    assert v.ema_text.startswith("均线数据停在 ") and v.ema_text.endswith("，暂不拦")


def test_market_trail_view_counts_by_gate_and_reason_and_reads_old_records(tmp_path: Path) -> None:
    def block(reason: str, detail: dict[str, Any]) -> c.DecisionRecord:
        return _rec(c.Role.RISK_GUARD, c.Outcome.REJECT, f"XRPUSDT {reason}", T(6, 20),
                    symbol="XRPUSDT", output={"reasons": [reason], "detail": detail})  # fmt: skip

    def unlock(why: str) -> c.DecisionRecord:
        return _rec(c.Role.RISK_GUARD, c.Outcome.INFO, "大盘移动止损：解锁", T(6, 21),
                    output={"market_trail": {"kind": "unlock", "why": why}})  # fmt: skip

    records = [
        block("大盘均线：BTC 在 30 分钟均线上方 0.3 ATR，暂停新空单", {}),
        block("大盘均线：x", {"by": "market_trail", "gates": ["ema"]}),
        block("大盘移动止损：x", {"by": "market_trail", "gates": ["ema", "trail"]}),
        block("大盘移动止损：BTC 从跌段低点反弹 1.2 ATR，暂停新空单", {"by": "market_trail"}),
        unlock("expire"), unlock("expire"), unlock("extreme"), unlock("leg"),
        _rec(c.Role.PATROL, c.Outcome.ACTION, "XRPUSDT 大盘闸门锁住且持仓在亏", T(6, 22),
             symbol="XRPUSDT",
             output={"held_event": {"trigger": "held_market_trail", "queued": True}}),
        _rec(c.Role.PATROL, c.Outcome.INFO, "XRPUSDT 只记录", T(6, 23), symbol="XRPUSDT",
             output={"held_event": {"trigger": "held_market_trail", "queued": False}}),
    ]  # fmt: skip
    v = _view(tmp_path, records, _ema_above())
    assert (v.blocked_today, v.blocked_ema, v.blocked_trail, v.blocked_both) == (4, 2, 1, 1)
    assert v.unlocks_today == {"expire": 2, "extreme": 1, "leg": 1}
    assert (v.meetings_asked, v.meetings_held) == (1, 0)
    assert v.today_text == (
        "锁 0 次（解锁：新极值 1 · 作废 2 · 新段 1） · "
        "拦下开仓 4 笔（均线 2 · 移动止损 1 · 两者 1）"
        " · 临时会申请 1 场、开成 0 场 · 两边都不开 25% 的时间"
    )
