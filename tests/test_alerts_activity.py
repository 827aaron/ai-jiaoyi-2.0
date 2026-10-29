"""告警卡上报、开平仓推送、各角色每次检查留一句、保命止损按真实入场价重挂。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from jiaoyi import contracts as c
from jiaoyi.alerting.recorded import RecordedAlerter
from jiaoyi.alerting.telegram import TelegramAlerter
from jiaoyi.contracts import Alert, AlertLevel, Health, Role
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.execution.fills import TradeFill
from jiaoyi.execution.hb_executor import _open_text
from jiaoyi.execution.registry import ServerProtection
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
    await a.flush()
    a.heartbeat(START)
    st = log.read_status()["alerting"]
    assert st.today.calls == 1 and st.summary == "开仓成交（APTUSDT）：已发 Telegram"
    assert st.headline == "正常 · 今日推了 1 条"
    tg.last_skipped = True
    await a.send(_alert("开仓成交"))  # 冷却期内同一件事：不算一条
    await a.flush()
    tg.last_skipped, tg.last_error = False, "HTTP 401"
    await a.send(_alert("平仓完成"))
    await a.flush()
    a.heartbeat(START)
    st = log.read_status()["alerting"]
    assert st.today.calls == 2 and st.health is Health.DEGRADED and "HTTP 401" in st.headline
    no_tg = RecordedAlerter(_Tg(), RoleRecorder(Role.ALERTING, log), telegram=False)
    await no_tg.send(_alert("开仓成交"))
    await no_tg.flush()
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


async def test_open_text_names_who_places_a_missing_lifeboat(tmp_path: Path) -> None:
    rig = Rig(tmp_path, notices=True)
    pid = await open_filled(rig)
    tracked = rig.registry.get(pid)
    assert tracked is not None
    bare = tracked.model_copy(
        update={"record": tracked.record.model_copy(update={"lifeboat": None})}
    )
    assert _open_text(bare).endswith("保命止损还没挂上，对账员会补挂")
    hosted = bare.model_copy(
        update={
            "server_protection": ServerProtection(
                account_name="a", connector_name="c", intended_quantity=1.0
            )
        }
    )
    assert _open_text(hosted).endswith("保命单稍后由托管保护补挂")


async def test_notices_carry_their_kind(tmp_path: Path) -> None:
    rig = Rig(tmp_path, notices=True)
    pid = await open_filled(rig)
    rig.api.executors["ex1"]["cum_fees_quote"] = 0.5
    rig.api.terminate("ex1", "TAKE_PROFIT", pnl=2.0)
    await rig.adapter.positions()
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    await rig.adapter._notice("保命止损触发", "x", rec, "lifeboat_hit")
    assert [(a.title, a.kind) for a in rig.alerts.sent] == [
        ("开仓成交", "open_filled"),
        ("平仓完成", "close_done"),
        ("保命止损触发", "lifeboat_hit"),
    ]


async def test_telegram_skips_muted_kinds_only() -> None:
    posted: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        posted.append(json.loads(req.content)["text"])
        return httpx.Response(200, json={"ok": True})

    tg = TelegramAlerter(
        "t", "1", muted_kinds=frozenset({"open_filled"}), transport=httpx.MockTransport(handler)
    )

    def alert(title: str, kind: str) -> Alert:
        return Alert(
            ts=START,
            level=AlertLevel.INFO,
            role=Role.EXECUTOR,
            title=title,
            dedup_key="k",
            kind=kind,
        )

    await tg.send(alert("开仓成交", "open_filled"))
    assert posted == [] and tg.last_skipped is False  # 静音不占冷却，也不算「被跳过」
    await tg.send(alert("保命止损缺失", "lifeboat_missing"))
    await tg.send(alert("没分类的", ""))  # 同一个 dedup_key，冷却内第二条被吞
    assert [x.splitlines()[0] for x in posted] == ["ℹ️ 保命止损缺失"]
    await tg.close()


def _fill_lower(rig: Rig, qty: float) -> None:
    rig.ex.trades = [TradeFill(START + timedelta(seconds=1), "o1", "buy", 0.7487, qty, 0.05, 0.0)]


async def test_lifeboat_relaid_at_real_entry_place_before_cancel(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, price=0.7497, symbol="APTUSDT")
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    old = rec.lifeboat
    assert old is not None and rec.stop_loss_pct is not None
    seen: list[int] = []
    cancel = rig.ex.cancel_stop

    async def counting_cancel(symbol: str, order_id: str) -> None:
        seen.append(len(rig.ex.stops))  # 撤旧那一刻币安上有几张
        await cancel(symbol, order_id)

    rig.ex.cancel_stop = counting_cancel  # type: ignore[method-assign]
    _fill_lower(rig, rec.quantity)
    await rig.adapter.backfill_fills(rig.ex.user_trades)
    after = rig.registry.get(pid).record  # type: ignore[union-attr]
    new = after.lifeboat
    assert new is not None and new.stop_price < old.stop_price
    assert seen == [2]  # 先挂新再撤旧：中间一直有保命止损
    assert len(rig.ex.stops) == 1 and new.client_order_id.endswith("-r")
    # 风控员算的距离原样保留，不按回填价倒推
    assert after.stop_loss_pct == rec.stop_loss_pct
    assert after.lifeboat_stop_pct == rec.lifeboat_stop_pct
    # 对账员认带后缀的那张：不补挂、不当残留撤
    placed = len(rig.ex.placed)
    await rig.rec.reconcile(rig.t)
    assert len(rig.ex.stops) == 1 and len(rig.ex.placed) == placed


async def test_extra_lifeboat_after_failed_cancel_is_dropped(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig, price=0.7497, symbol="APTUSDT")
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    old = rec.lifeboat
    assert old is not None
    rig.ex.fail_cancel = True  # 新单挂上了，旧单撤不掉
    _fill_lower(rig, rec.quantity)
    await rig.adapter.backfill_fills(rig.ex.user_trades)
    new = rig.registry.get(pid).record.lifeboat  # type: ignore[union-attr]
    assert new is not None and new.client_order_id.endswith("-r")  # 账本指新的
    assert len(rig.ex.stops) == 2
    rig.ex.fail_cancel = False
    await rig.rec.reconcile(rig.t)  # 第一轮只记下（可能是重挂进行中），不撤
    assert len(rig.ex.stops) == 2
    report = await rig.rec.reconcile(rig.t)
    assert [s.client_order_id for s in rig.ex.stops.values()] == [new.client_order_id]
    assert any("多挂了一张" in i.detail and i.fixed for i in report.issues)


async def test_lifeboat_relay_failure_keeps_old_stop(tmp_path: Path) -> None:
    from jiaoyi.execution.exchange import ExchangeError

    rig = Rig(tmp_path)
    pid = await open_filled(rig, price=0.7497, symbol="APTUSDT")
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    old = rec.lifeboat
    assert old is not None
    rig.ex.fail_place = ExchangeError("拒单")
    _fill_lower(rig, rec.quantity)
    await rig.adapter.backfill_fills(rig.ex.user_trades)
    assert [s.stop_price for s in rig.ex.stops.values()] == [old.stop_price]
    assert rig.registry.get(pid).record.lifeboat == old  # type: ignore[union-attr]
    warn = rig.alerts.sent[-1]
    assert warn.level is AlertLevel.WARN and "还在" in warn.body


async def test_lifeboat_relay_past_new_stop_closes(tmp_path: Path) -> None:
    from jiaoyi.execution.exchange import ExchangeError

    rig = Rig(tmp_path)
    pid = await open_filled(rig, price=0.7497, symbol="APTUSDT")
    rec = rig.registry.get(pid).record  # type: ignore[union-attr]
    rig.ex.fail_place = ExchangeError("-2021 would immediately trigger", immediate_trigger=True)
    _fill_lower(rig, rec.quantity)
    await rig.adapter.backfill_fills(rig.ex.user_trades)
    assert rig.ex.market_closed  # 直连平掉
    closed = rig.registry.get(pid).record  # type: ignore[union-attr]
    assert closed.status is c.PositionStatus.CLOSED


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


async def test_alert_record_shows_event_and_sent_text(tmp_path: Path) -> None:
    from jiaoyi.alerting.telegram import TelegramAlerter

    log = JsonlDecisionLog(tmp_path)
    tg = _Tg()
    tg.format = TelegramAlerter.format  # type: ignore[attr-defined]
    a = RecordedAlerter(tg, RoleRecorder(Role.ALERTING, log), telegram=True)
    alert = _alert("平仓完成").model_copy(update={"body": "管家平 · 净利 +1.20 U · 持仓 9 分"})
    await a.send(alert)
    await a.flush()
    (rec,) = [r for r in log.read_day(START.date().isoformat()) if r.role is Role.ALERTING]
    assert rec.input == {
        "事件": "平仓完成",
        "级别": "通知",
        "币": "APTUSDT",
        "内容": "管家平 · 净利 +1.20 U · 持仓 9 分",
    }
    assert rec.output["Telegram 正文"].endswith("管家平 · 净利 +1.20 U · 持仓 9 分")
    assert rec.output["结果"] == "已发送" and rec.output["耗时"].endswith("ms")


class _GatedTransport(httpx.AsyncBaseTransport):
    """Telegram 接口：放行前每个请求都卡住，记下发出的正文顺序。"""

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.texts: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self.gate.wait()
        self.texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={"ok": True})


async def test_recorded_alerter_send_returns_before_telegram_answers(tmp_path: Path) -> None:
    transport = _GatedTransport()
    tg = TelegramAlerter("t", "c", transport=transport)
    a = RecordedAlerter(tg, RoleRecorder(Role.ALERTING, JsonlDecisionLog(tmp_path)), telegram=True)
    first = _alert("一").model_copy(update={"dedup_key": "k"})
    started = time.perf_counter()
    for alert in (first, _alert("二"), first, _alert("三")):
        await asyncio.wait_for(a.send(alert), 0.5)
    assert time.perf_counter() - started < 0.5
    assert transport.texts == []
    transport.gate.set()
    assert await a.flush(5.0)
    assert [t.split("\n")[0].split(" ", 1)[1] for t in transport.texts] == [
        "一 [APTUSDT]",
        "二 [APTUSDT]",
        "三 [APTUSDT]",
    ]
    await a.close()


async def test_recorded_alerter_close_flushes_within_its_bound(tmp_path: Path) -> None:
    transport = _GatedTransport()
    tg = TelegramAlerter("t", "c", transport=transport)
    log = JsonlDecisionLog(tmp_path)
    a = RecordedAlerter(tg, RoleRecorder(Role.ALERTING, log), telegram=True, flush_s=0.2)
    await asyncio.wait_for(a.send(_alert("卡住")), 0.5)
    started = time.perf_counter()
    await asyncio.wait_for(a.close(), 2.0)
    assert 0.2 <= time.perf_counter() - started < 2.0
    sent = RecordedAlerter(
        TelegramAlerter("t", "c", transport=(ok := _GatedTransport())),
        RoleRecorder(Role.ALERTING, log),
        telegram=True,
    )
    ok.gate.set()
    await sent.send(_alert("关前入队"))
    await sent.close()
    assert len(ok.texts) == 1 and "关前入队" in ok.texts[0]


async def test_reconciler_waits_one_round_for_a_closing_position(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    pid = await open_filled(rig)
    t = rig.registry.get(pid)
    assert t is not None
    closing = t.record.model_copy(update={"status": c.PositionStatus.CLOSING})
    rig.registry.put(t.model_copy(update={"record": closing}))
    rig.ex.set_position(closing.symbol, c.Side.LONG, 0)  # 币安已平，执行器还没结算

    async def none(*_: object) -> list[dict[str, object]]:
        return []

    async def no_sync() -> None:
        return None

    rig.api.running_executors = none  # type: ignore[method-assign]
    rig.adapter.sync_all = no_sync  # type: ignore[method-assign]
    first = await rig.rec.reconcile(rig.t)
    assert first.issues == []  # 平仓那几秒的差不报错
    second = await rig.rec.reconcile(rig.t)
    kinds = {i.kind for i in second.issues}
    assert c.ReconcileIssueKind.QTY_MISMATCH in kinds  # 连续 2 轮还没结算才报
