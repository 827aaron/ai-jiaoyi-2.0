"""告警卡上报、开平仓推送、各角色每次检查留一句、保命止损按真实入场价重挂。"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from jiaoyi import contracts as c
from jiaoyi.alerting.recorded import RecordedAlerter
from jiaoyi.contracts import Alert, AlertLevel, Health, Role
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution.fills import TradeFill
from jiaoyi.execution.user_stream import mask
from jiaoyi.patrol.rules import RulePatrol
from tests.fastloop_helpers import FAST
from tests.test_execution import START, Rig, open_filled
from tests.test_keeper_patrol import _trade, position


class _Tg:
    """像 TelegramAlerter 一样带 last_error / last_skipped。"""

    def __init__(self) -> None:
        self.last_error: str | None = None
        self.last_skipped = False
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.sent.append(alert)


def _alert(title: str) -> Alert:
    return Alert(ts=START, level=AlertLevel.INFO, role=Role.EXECUTOR, title=title, symbol="APTUSDT")


async def test_alert_card_counts_pushes_and_reports_failures(tmp_path: Path) -> None:
    log = JsonlDecisionLog(tmp_path)
    rec = RoleRecorder(Role.ALERTING, log)
    tg = _Tg()
    a = RecordedAlerter(tg, rec, telegram=True)
    a.heartbeat(START)
    assert log.read_status()["alerting"].headline == "待命 · Telegram 已配置"
    await a.send(_alert("开仓成交"))
    a.heartbeat(START)
    st = log.read_status()["alerting"]
    assert st.today.calls == 1 and st.summary == "开仓成交（APTUSDT）：已发 Telegram"
    assert st.headline == "正常 · 今日推了 1 条"
    tg.last_skipped = True
    await a.send(_alert("开仓成交"))  # 冷却期内同一件事：不算一条
    tg.last_skipped, tg.last_error = False, "HTTP 401"
    await a.send(_alert("平仓完成"))
    a.heartbeat(START)
    st = log.read_status()["alerting"]
    assert st.today.calls == 2 and st.health is Health.DEGRADED and "HTTP 401" in st.headline
    no_tg = RecordedAlerter(_Tg(), RoleRecorder(Role.ALERTING, log), telegram=False)
    await no_tg.send(_alert("开仓成交"))
    no_tg.heartbeat(START)
    assert "未配置" in log.read_status()["alerting"].headline


async def test_open_and_close_push_plain_text(tmp_path: Path) -> None:
    rig = Rig(tmp_path, notices=True)
    await open_filled(rig)
    opened = rig.alerts.sent[-1]
    assert opened.title == "开仓成交" and opened.level is AlertLevel.INFO
    assert "做多" in opened.body and "杠杆" in opened.body and "保命止损" in opened.body
    rig.t = START + timedelta(minutes=75)
    rig.api.executors["ex1"]["cum_fees_quote"] = 0.5
    rig.api.terminate("ex1", "TAKE_PROFIT", pnl=2.0)
    await rig.adapter.positions()
    closed = rig.alerts.sent[-1]
    assert closed.title == "平仓完成" and "止盈" in closed.body
    assert "毛利 +2.50 U，手续费 0.50 U，净利 +2.00 U" in closed.body
    assert "持仓 1 小时 15 分" in closed.body


async def test_lifeboat_relaid_at_real_entry(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, price=0.7497, symbol="APTUSDT")
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    old = rec.lifeboat
    assert old is not None
    rig.ex.trades = [
        TradeFill(START + timedelta(seconds=1), "o1", "buy", 0.7487, rec.quantity, 0.05, 0.0)
    ]
    await rig.adapter.backfill_fills(rig.ex.user_trades)
    new = rig.registry.get(pid).record.lifeboat  # type: ignore[union-attr]
    assert new is not None and new.stop_price < old.stop_price
    assert len(rig.ex.stops) == 1  # 撤旧挂新，只剩一张


def test_patrol_leaves_a_note_every_round(tmp_path: Path, now: datetime) -> None:
    log = JsonlDecisionLog(tmp_path)
    rec = RoleRecorder(Role.PATROL, log)
    p = RulePatrol(FAST, rec)
    pos = [position(now)]
    p.check(_trade(now, 68000.0), pos)
    p.check(_trade(now + timedelta(seconds=3), 67990.0), pos)  # 同一轮，不再记
    p.check(_trade(now + timedelta(seconds=11), 67990.0), pos)
    rec.heartbeat(now + timedelta(seconds=11), c.Health.OK)
    st = log.read_status()["patrol"]
    assert st.today.calls == 2 and st.summary is not None and st.summary.startswith("查了 BTC 1 笔")
    assert "没异常" in st.summary and st.next_run_at is not None
    assert log.read_day(now.date().isoformat()) == []  # 巡检不写决策记录


def test_listen_key_is_masked() -> None:
    key = "abcdef1234567890"
    assert mask(f"wss://x/ws/{key}", key) == "wss://x/ws/abcd…"
    assert key not in mask(f"InvalidURI: wss://x/private/ws?listenKey={key}", key)


async def test_lifeboat_card_counts_place_cancel_replace(tmp_path: Path) -> None:
    from jiaoyi.execution.lifeboat import BinanceLifeboat
    from tests.fakes import FakeExchange

    log = JsonlDecisionLog(tmp_path)
    lb = BinanceLifeboat(
        FakeExchange(), clock=lambda: START, recorder=RoleRecorder(Role.LIFEBOAT, log)
    )
    pos = position(START)
    await lb.place(pos.model_copy(update={"lifeboat": None}), 67000.0)
    await lb.cancel(pos)
    await lb.place(pos.model_copy(update={"lifeboat": None}), 67000.0)  # 同一笔再挂 = 补挂
    lb.heartbeat(START)
    assert log.read_status()["lifeboat"].headline.endswith("今日 挂 1 · 撤 1 · 补挂 1")
    fresh = BinanceLifeboat(
        FakeExchange(), clock=lambda: START, recorder=RoleRecorder(Role.LIFEBOAT, log)
    )
    fresh.heartbeat(START)  # 重启后从当天决策日志数回来
    assert log.read_status()["lifeboat"].headline.endswith("今日 挂 1 · 撤 1 · 补挂 1")
