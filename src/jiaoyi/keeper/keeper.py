"""持仓管家：每个持仓每 15–30 秒（巡逻兵报警时立刻）问一次决策模型 3 个问题，判断拿住还是全平。

规则（设计文档 §五）：
- 只能让仓位更安全：v1 只有 hold / close（全平），不加仓、不放宽止损、不部分减仓。
- 决策模型失败、没有行情 → hold（维持现状），并且连续计数清零，不拿旧结论凑次数。
  Jev 服务方繁忙（5xx / 529）或超时：本轮隔 ``RETRY_AFTER_S`` 秒再问 1 次，还不行才维持现状
  （两次加起来几秒，赶得上下一轮）。面板上写大白话，原始报错留在决策记录的 output 里。
- 平仓要概率明显过线（``keeper_close_p_strong``），或者过线（``keeper_close_p``）且连续
  ``keeper_confirmations`` 次一致，防止反复横跳。
- 有效最新作战卡明确单向反着持仓：不问模型、不等行情和连续确认，直接返回全平规则
  （``opposite_card_action = "close"``，默认）。``confirm_or_tighten``（#314）：价格确认才全平，
  否则返回「拿住」并要求接线代码把交易所托管止损收紧到保本附近，模型照常复查；之后价格确认了
  （卡还有效）再平。确认规则见 ``opposite_confirmation``，抽签见 ``opposite_ab_draw``。
  新卡改成只看不做、或卡里没有这个币：不投票，持仓交给决策模型 / 止损 / 最长持仓时间处理
  （不开新仓由拍板人和风控员按卡管）。例外：被封的币（币安下架公告等，``blocked``）照样投票，
  要下架的币留仓更危险。规则只会推向更安全，不会把模型的“平”改成“拿”。
- 回吐保护（Aaron 10-25，代码规则，不问模型）：这笔仓最高浮盈到过 ``giveback_arm_r`` 倍止损距离后，
  浮盈从最高点吐掉 ``giveback_close_pct`` % 就全平（``giveback_on`` 开关）。
  每次复查都先查这条，查过才问模型；
  最高浮盈用每次复查的价格单独记（不受 ``MAX_HISTORY`` 截断），重启后从决策日志找回的历史接上。
  浮盈和快照同口径（基点、未扣手续费，R = 开仓价到止损价的距离）。
- 快照带这笔仓的来历（管家方案 E，Aaron 10-24）：之前历次复查的浮盈（算最高浮盈、回吐、走势）、
  进场拍板（信号种类、触发说明、「真」「做」概率）、移动止损状态、预测员。历次浮盈和进场拍板
  第一次复查这笔仓时从决策日志里找回（重启后也接得上），找不到就从这次开始记。
- 和卡相反的信号（``note_against``，主循环按账本持仓判断后叫来的急查）：信号写进这次的快照
  （``against``）；强反向信号或已亏到一定倍数止损时，这次过线（``keeper_close_p``）就平，
  不等连续确认。查成了只用一次；没查成（没行情 / Jev 出错）留给下一次复查，不算查过。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from jiaoyi.contracts import (
    AgainstSignalEvent,
    BattleCard,
    Bias,
    CardSymbol,
    FastLoopConfig,
    FeatureSnapshot,
    Forecast,
    HoldQuestion,
    JevRequest,
    JevResponse,
    KeeperVerdict,
    LevelKind,
    MarketView,
    Outcome,
    PositionRecord,
    Role,
    Side,
)
from jiaoyi.core.observations import aware, freeze_values
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.gate.jev import error_kind
from jiaoyi.gate.questions import hold_pnl_bps, hold_questions, hold_snapshot, jev_log_fields
from jiaoyi.interfaces import DecisionModel

log = logging.getLogger(__name__)

RETRY_AFTER_S = 1.0
MAX_HISTORY = 400  # 每笔仓最多记这么多次复查的浮盈（20 秒一次 ≈ 2 小时多）
_LOOKBACK_DAYS = 3  # 找回历史最多往前翻几天的决策日志
_FAIL_TEXT = {
    "busy": "Jev 服务方繁忙（已重试 1 次），本轮维持现状",
    "timeout": "Jev 超时（已重试 1 次），本轮维持现状",
    "cooldown": "Jev 连续出错在冷却，本轮维持现状",
    "other": "Jev 调用出错，本轮维持现状",
}


class JevKeeper:
    """``Keeper`` 的实现。行情和作战卡通过回调拿，免得管家依赖具体模块。"""

    def __init__(
        self,
        model: DecisionModel,
        cfg: FastLoopConfig,
        features: Callable[[str], FeatureSnapshot | None],
        card: Callable[[datetime], BattleCard | None],
        recorder: RoleRecorder | None = None,
        blocked: Callable[[], Mapping[str, str]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        *,
        fee_bps: float = 5.0,
        forecast: Callable[[str, datetime], Forecast | None] | None = None,
        scan_day: Callable[[str, str], list[dict[str, Any]]] | None = None,
        current_market: Callable[[], MarketView | None] | None = None,
        frozen_peak_review: Callable[[str, datetime], Awaitable[dict[str, Any] | None]]
        | None = None,
        observation_source: Callable[[str, datetime], Mapping[str, Any] | None] | None = None,
        observation_sink: Callable[[dict[str, Any]], None] | None = None,
        observation_wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        opposite_state_path: Path | None = None,
        review_interval_s: float | None = None,
    ) -> None:
        """``blocked``：被封禁新开仓的币 → 原因（军师团 ``blocked_symbols``），这些币的持仓要平。

        ``fee_bps``：单边吃单手续费（快照的扣费后浮盈）；``forecast``：预测员最新预测；
        ``scan_day(UTC 日期, 关键字)``：读决策日志里含关键字的记录，找回复查历史和进场拍板。
        ``opposite_state_path``：反向卡确认的起点（每条指令第一次看到时的价和时间、这笔仓第一次
        碰到反向卡的时间和价）落盘的位置，重启后接着用；None = 只在内存。
        ``review_interval_s``：例行复查间隔（app.toml ``keeper_interval_s``）。行情过旧超过它的
        2 倍就丢掉断流前的连续「该平」计数；None = 一遇到过旧就丢。"""
        self._frozen_peak_review = frozen_peak_review
        self._model = model
        self._sleep = sleep
        self._blocked = blocked or dict
        self._cfg = cfg
        self._features = features
        self._card = card
        self._market_source = current_market
        self._observation_source = observation_source
        self._observation_sink = observation_sink
        self._observation_wall = observation_wall
        self._rec = recorder
        self._streak: dict[str, int] = {}  # 每个持仓连续“该平”的次数
        self._against: dict[str, tuple[AgainstSignalEvent, bool]] = {}
        self._fee_bps = fee_bps
        self._forecast = forecast
        self._scan_day = scan_day
        self._history: dict[str, list[float]] = {}  # 每个持仓之前历次复查的浮盈（基点）
        self._peak: dict[str, float] = {}  # 每个持仓复查到过的最高浮盈（基点），不被历史截断
        self._entry: dict[str, dict[str, Any] | None] = {}  # 每个持仓的进场拍板
        # 后台模式的冻结盈利价证据：pid → (这次读开始的时刻, 结果)
        self._fp_cache: dict[str, tuple[datetime, dict[str, Any] | None]] = {}
        self._fp_started: dict[str, datetime] = {}
        self._fp_inflight: set[str] = set()
        self._fp_stale: set[str] = set()
        # 反向卡（confirm_or_tighten）：每个持仓每条反向指令（币|issued_at|方向）第一次看到时的
        # 价格和时间（ATR 走势起点），和这笔仓第一次碰到反向卡的时间和价格（反事实）
        self._opposite_ref: dict[str, dict[str, tuple[float, datetime]]] = {}
        self._opposite_first: dict[str, tuple[datetime, float | None]] = {}
        self._opposite_logged: dict[str, tuple[str, str]] = {}  # 已记过的（指令, 分支），不刷屏
        self._opposite_path = opposite_state_path
        self._review_interval_s = review_interval_s
        self._stale_logged: set[str] = set()  # 这段断流已记过「行情已旧」的持仓，不刷屏
        self._load_opposite_state()

    def set_market_source(self, source: Callable[[], MarketView | None]) -> None:
        """市场背景不依赖是否还有有效开仓卡；只读，不合成交易授权。"""
        self._market_source = source

    def forget(self, position_id: str) -> None:
        """持仓平掉后调用，清掉它的计数。"""
        self._streak.pop(position_id, None)
        self._stale_logged.discard(position_id)
        self._against.pop(position_id, None)
        self._history.pop(position_id, None)
        self._peak.pop(position_id, None)
        self._entry.pop(position_id, None)
        self._fp_cache.pop(position_id, None)
        self._fp_started.pop(position_id, None)
        self._fp_inflight.discard(position_id)
        self._fp_stale.discard(position_id)
        known = position_id in self._opposite_ref or position_id in self._opposite_first
        self._opposite_ref.pop(position_id, None)
        self._opposite_first.pop(position_id, None)
        self._opposite_logged.pop(position_id, None)
        if known:
            self._save_opposite_state()

    def _load_opposite_state(self) -> None:
        if self._opposite_path is None or not self._opposite_path.is_file():
            return
        try:
            raw = json.loads(self._opposite_path.read_text(encoding="utf-8"))
            for pid, row in raw.items():
                at, price = row["first"]
                self._opposite_first[pid] = (
                    datetime.fromisoformat(at),
                    None if price is None else float(price),
                )
                self._opposite_ref[pid] = {
                    key: (float(ref_price), datetime.fromisoformat(ref_at))
                    for key, (ref_price, ref_at) in row.get("refs", {}).items()
                }
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            # 读坏了就从这次起重新量（偏保守：更难确认），不拦管家。
            log.warning("反向卡确认起点 %s 读不了，从这次起重新记", self._opposite_path)
            self._opposite_first.clear()
            self._opposite_ref.clear()

    def _save_opposite_state(self) -> None:
        if self._opposite_path is None:
            return
        data = {
            pid: {
                "first": [first[0].isoformat(), first[1]],
                "refs": {
                    key: [price, at.isoformat()]
                    for key, (price, at) in self._opposite_ref.get(pid, {}).items()
                },
            }
            for pid, first in self._opposite_first.items()
        }
        try:
            self._opposite_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._opposite_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._opposite_path)
        except OSError:
            log.warning("反向卡确认起点写不进 %s", self._opposite_path, exc_info=True)

    def history(self, position_id: str) -> list[float]:
        """这笔仓之前历次复查的浮盈（基点，从早到晚）。"""
        return list(self._history.get(position_id, ()))

    async def _recall(self, pos: PositionRecord, now: datetime) -> None:
        """第一次复查这笔仓：从决策日志找回历次浮盈和进场拍板（放线程里读，不卡事件循环）。"""
        pid = pos.position_id
        history: list[float] = []
        entry: dict[str, Any] | None = None
        if self._scan_day is not None:
            try:
                history, entry = await asyncio.to_thread(self._read_log, pos, now)
            except Exception:  # 日志读不了就从这次开始记，不影响复查
                log.warning("管家找回 %s 的复查历史失败", pid, exc_info=True)
        self._history[pid] = history[-MAX_HISTORY:]
        if history:
            self._peak[pid] = max(history)
        self._entry[pid] = entry

    def _read_log(
        self, pos: PositionRecord, now: datetime
    ) -> tuple[list[float], dict[str, Any] | None]:
        assert self._scan_day is not None
        first = (pos.opened_at - timedelta(minutes=5)).date()
        last = now.date()
        days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
        days = days[-_LOOKBACK_DAYS:]
        checks: list[tuple[str, float]] = []
        entry: dict[str, Any] | None = None
        for day in days:
            iso = day.isoformat()
            for rec in self._scan_day(iso, pos.position_id):
                mine = rec.get("position_id") == pos.position_id
                if rec.get("role") != Role.KEEPER.value or not mine:
                    continue
                pnl = _snapshot_pnl(rec)
                if pnl is not None:
                    checks.append((str(rec.get("ts", "")), pnl))
            for rec in self._scan_day(iso, pos.signal_id):
                if rec.get("role") == Role.GATE.value and rec.get("signal_id") == pos.signal_id:
                    entry = _entry_block(rec) or entry
        checks.sort(key=lambda c: c[0])
        return [pnl for _, pnl in checks], entry

    def note_against(self, position_id: str, sig: AgainstSignalEvent, one_shot: bool) -> None:
        """下一次复查带上这个反向信号；``one_shot`` = 这次过线就平，不等连续确认。"""
        self._against[position_id] = (sig, one_shot)

    def pending_against(self, position_id: str) -> bool:
        """还有没查成的反向信号（急查时没行情 / Jev 出错，留给下一次复查）。"""
        return position_id in self._against

    def _keep_against(
        self, pid: str, against: tuple[AgainstSignalEvent, bool] | None, now: datetime
    ) -> None:
        """这次没查成：反向信号留给下一次复查（主循环马上再排急查，最晚下一轮例行复查），
        不算查过。信号超过 ``against_recheck_cooldown_s`` 就不再留（太旧了）。"""
        if against is None or pid in self._against:
            return
        if (now - against[0].ts).total_seconds() < self._cfg.against_recheck_cooldown_s:
            self._against[pid] = against

    def frozen_peak_background(self) -> bool:
        return (
            self._frozen_peak_review is not None
            and self._cfg.frozen_peak_evidence_mode == "background"
        )

    def frozen_peak_due(self, position_id: str, now: datetime) -> bool:
        """Background mode: no read in flight and the last one started an interval ago."""
        if position_id in self._fp_inflight:
            return False
        last = self._fp_started.get(position_id)
        interval = self._cfg.frozen_peak_evidence_interval_s
        return last is None or (now - last).total_seconds() >= interval

    async def refresh_frozen_peak(self, position_id: str, now: datetime) -> bool:
        """One background evidence read; True when it latched a trigger, so the caller can
        review the position now instead of a keeper interval later. ``now`` is stored as the
        result's as-of: the read started then, so the cached age is an upper bound on the
        evidence age."""
        if position_id in self._fp_inflight:
            return False
        self._fp_inflight.add(position_id)
        self._fp_started[position_id] = now
        try:
            result = await self._read_frozen_peak(position_id, now)
        finally:
            closed = position_id not in self._fp_started  # forget() ran while reading
            self._fp_inflight.discard(position_id)
        if closed:
            return False
        self._fp_cache[position_id] = (now, result)
        return bool(result and result.get("triggered") is True)

    async def _read_frozen_peak(self, pid: str, now: datetime) -> dict[str, Any] | None:
        assert self._frozen_peak_review is not None
        try:
            return await self._frozen_peak_review(pid, now)
        except Exception:
            log.warning("Frozen peak evidence unavailable for %s", pid, exc_info=True)
            return {"triggered": False, "detail": "evidence_reader_failed"}

    def _cached_frozen_peak(self, pid: str, now: datetime) -> dict[str, Any] | None:
        cached = self._fp_cache.get(pid)
        if pid not in self._fp_started or (cached is not None and cached[1] is None):
            return None  # 还没读过，或这笔仓没开冻结盈利价（不会再开）：不算过期，不记日志
        max_age = self._cfg.frozen_peak_evidence_max_age_s
        if cached is not None and (max_age == 0 or (now - cached[0]).total_seconds() <= max_age):
            self._fp_stale.discard(pid)
            return cached[1]
        as_of = cached[0].isoformat() if cached is not None else None
        if pid not in self._fp_stale:
            self._fp_stale.add(pid)
            log.warning(
                "Frozen peak evidence for %s is stale (as of %s); rule skipped until a fresh read",
                pid,
                as_of,
            )
        return {"triggered": False, "detail": "evidence_stale", "evidence_as_of": as_of}

    async def review(self, position: PositionRecord, now: datetime) -> KeeperVerdict:
        direct = self.review_card_rule(position, now)
        if direct is not None and direct.action == "close":
            return direct  # 「拿住 + 收紧」只交给作战卡规则通道，这里照常问模型
        pid = position.position_id
        frozen_peak = None
        if self.frozen_peak_background():
            frozen_peak = self._cached_frozen_peak(pid, now)
        elif self._frozen_peak_review is not None:
            frozen_peak = await self._read_frozen_peak(pid, now)
        if frozen_peak and frozen_peak.get("triggered") is True:
            self._streak.pop(pid, None)
            return self._verdict(
                position,
                now,
                "close",
                1.0,
                1,
                f"冻结盈利价回访：同环境可成交价再次到达 {frozen_peak['target_price']:g}",
                None,
                None,
                frozen_peak_rule=frozen_peak,
            )
        against = self._against.pop(pid, None)
        f = self._features(position.symbol)
        observation = self._observation_start(position, now, f)
        verdict = partial(
            self._verdict, against=against, observation=observation, frozen_peak_rule=frozen_peak
        )
        if f is None:
            self._streak.pop(pid, None)
            self._keep_against(pid, against, now)
            return verdict(position, now, "hold", 0.0, 1, "没有行情数据，维持现状", None, None)
        age = self._stale_age(f, now)
        if age is not None:
            # 短暂断流：这一轮当没复查，连续计数不动；断得久了，断流前那一票不能和恢复后的
            # 一票凑成「连续两次」
            limit = self._review_interval_s
            if limit is None or age > 2 * limit:
                self._streak.pop(pid, None)
            self._keep_against(pid, against, now)
            first = pid not in self._stale_logged
            self._stale_logged.add(pid)
            return verdict(
                position,
                now,
                "hold",
                0.0,
                1,
                f"行情已旧 {age:.0f} 秒，这轮跳过、维持现状",
                None,
                None,
                record=first,
            )
        self._stale_logged.discard(pid)
        card = self._card(now)
        market = (
            self._market_source()
            if self._market_source is not None
            else (card.current_market if card else None)
        )
        if pid not in self._history:
            await self._recall(position, now)
        history = self._history.setdefault(pid, [])
        pnl = hold_pnl_bps(position, f.last_price)
        peak = max(self._peak.get(pid, pnl), pnl)
        self._peak[pid] = peak
        guard = self._giveback_guard(position, pnl, peak)
        if guard is not None:  # 代码规则先于模型：回吐保护不问 Jev，直接全平
            history.append(pnl)
            del history[:-MAX_HISTORY]
            self._streak.pop(pid, None)
            self._keep_against(pid, against, now)
            text, detail = guard
            return verdict(position, now, "close", 1.0, 1, text, None, None, extra=detail)
        fc = self._forecast(position.symbol, now) if self._forecast is not None else None
        request = JevRequest(
            request_id=f"hold-{pid}-{uuid.uuid4().hex[:6]}",
            kind="hold",
            symbol=position.symbol,
            ts=now,
            position_id=pid,
            snapshot=hold_snapshot(
                position,
                f,
                card,
                now,
                against=against[0] if against is not None else None,
                history=history,
                entry=self._entry.get(pid),
                forecast=fc,
                fee_bps=self._fee_bps,
                current_market=market,
            ),
            questions=hold_questions(),
        )
        history.append(hold_pnl_bps(position, f.last_price))
        del history[:-MAX_HISTORY]
        resp = await self._model.ask(request)
        attempts = 1
        if not resp.ok and error_kind(resp.error) in ("busy", "timeout"):
            await self._sleep(RETRY_AFTER_S)  # 服务方一时过载 / 超时：本轮再问一次
            request = request.model_copy(update={"request_id": f"{request.request_id}-r"})
            resp = await self._model.ask(request)
            attempts = 2
        self._observation_response(observation, position.symbol, request, resp, attempts)
        if not resp.ok:
            self._streak.pop(pid, None)
            self._keep_against(pid, against, now)
            return verdict(
                position,
                now,
                "hold",
                0.0,
                1,
                _FAIL_TEXT[error_kind(resp.error)],
                request,
                resp,
            )
        ans = resp.answer(HoldQuestion.ACTION.value)
        if ans is None:
            self._streak.pop(pid, None)
            self._keep_against(pid, against, now)
            return verdict(
                position, now, "hold", 0.0, 1, "决策模型没回答 action，维持现状", request, resp
            )

        p_close = ans.probs.get("close", 0.0)
        why = [f"平仓概率 {p_close:.2f}"]
        why_blocked = self._blocked().get(position.symbol)
        rule = f"已禁止新开仓（{why_blocked}）" if why_blocked is not None else None
        if rule is not None:
            p_close = max(p_close, self._cfg.keeper_close_p)
            why.append(rule)
        one_shot = against is not None and against[1]
        if against is not None:
            why.append(f"反向信号急查（{against[0].desc}）" + ("，过线即平" if one_shot else ""))

        if p_close >= self._cfg.keeper_close_p:
            n = self._streak.get(pid, 0) + 1
            self._streak[pid] = n
            if (
                one_shot
                or p_close >= self._cfg.keeper_close_p_strong
                or n >= self._cfg.keeper_confirmations
            ):
                return verdict(position, now, "close", p_close, n, "，".join(why), request, resp)
            why.append(f"第 {n} 次，等确认")
            return verdict(position, now, "hold", p_close, n, "，".join(why), request, resp)
        self._streak.pop(pid, None)
        return verdict(position, now, "hold", p_close, 1, "，".join(why), request, resp)

    def card_rule_applies(self, position: PositionRecord, now: datetime) -> bool:
        """只读预检供调度去重；真正执行时仍需再次读取最新卡，不在预检记判定日志。"""
        return self._card_rule(position, self._card(now), now) is not None

    def card_rule_watch(self, position: PositionRecord, now: datetime) -> bool:
        """只读：反向卡这次走「价格确认或收紧」新规则（不是立刻全平），模型复查照常进行。"""
        return self.card_rule_key(position, now) is not None

    def card_rule_key(self, position: PositionRecord, now: datetime) -> str | None:
        """只读：走新规则时这条反向指令的身份（币|issued_at|方向），否则 None。接线代码按它缓存
        「已收紧 / 已够紧」，同一条指令不再每轮进执行器的锁。"""
        card = self._card(now)
        if self._cfg.opposite_card_action != "confirm_or_tighten" or card is None:
            return None
        if self._card_rule(position, card, now) is None:
            return None
        cs = card.for_symbol(position.symbol)
        assert cs is not None
        key = opposite_instruction_key(cs, card)
        return key if self._opposite_arm(position, key)[0] == "confirm_or_tighten" else None

    def _opposite_arm(self, position: PositionRecord, key: str) -> tuple[str, float]:
        """（这次走哪组, 抽签数）。``opposite_card_ab`` = 0 不分组，全走新规则。"""
        ab = self._cfg.opposite_card_ab
        draw = opposite_ab_draw(position.position_id, key)
        return ("confirm_or_tighten" if ab <= 0 or draw < ab else "close"), draw

    def review_card_rule(self, position: PositionRecord, now: datetime) -> KeeperVerdict | None:
        """最新有效卡明确反向时直接交执行器全平；无行情/模型故障不构成关卡。

        ``opposite_card_action = "confirm_or_tighten"``：价格确认才返回全平；没确认返回「拿住」，
        理由交给接线代码收紧托管止损（每次都重新判，卡还有效时之后确认了就平）。"""
        card = self._card(now)
        reason = self._card_rule(position, card, now)
        if reason is None or card is None:
            return None
        cs = card.for_symbol(position.symbol)
        assert cs is not None
        rule: dict[str, Any] = {
            "kind": "opposite_card",
            "card_id": card.card_id,
            "entry_card_id": position.card_id,
            "card_version": card.version,
            "position_side": position.side.value,
            "card_bias": cs.bias.value,
            "issued_at": (cs.issued_at or card.created_at).isoformat(),
            "expires_at": (cs.expires_at or card.expires_at).isoformat(),
            "model_called": False,
            "requires_confirmation": False,
        }
        if self._cfg.opposite_card_action == "confirm_or_tighten":
            return self._opposite_review(position, card, cs, reason, rule, now)
        self._streak.pop(position.position_id, None)
        text = f"作战卡反向硬规则：{reason}，不等模型或确认"
        return self._verdict(position, now, "close", 1.0, 1, text, None, None, card_rule=rule)

    def _opposite_review(
        self,
        position: PositionRecord,
        card: BattleCard,
        cs: CardSymbol,
        reason: str,
        rule: dict[str, Any],
        now: datetime,
    ) -> KeeperVerdict:
        """#314 新规则：记下抽签、价格确认的每一条输入和走了哪支，反事实一并记。

        没有行情快照（比如刚重启）：不平、只要求收紧（执行器自己按触发价源核对能不能挂；
        已收紧过就是维持），有行情了再判确认。"""
        pid = position.position_id
        key = opposite_instruction_key(cs, card)
        arm, draw = self._opposite_arm(position, key)
        f = self._features(position.symbol)
        stale = self._stale_age(f, now) if f is not None else None
        if stale is not None:
            f = None  # 旧价不能确认反向卡，也不能当起点记下来：按没有行情处理（只收紧）
        changed = False
        first = self._opposite_first.get(pid)
        if first is None or (first[1] is None and f is not None):
            first = (first[0] if first else now, f.last_price if f is not None else None)
            self._opposite_first[pid] = first  # 这笔仓第一次碰到反向卡：原规则会在这时这价平
            changed = True
        refs = self._opposite_ref.setdefault(pid, {})
        ref = refs.get(key)
        if f is not None and ref is None:
            ref = (f.last_price, now)  # 第一次看到这条指令：之后的 ATR 走势从这里量
            refs[key] = ref
            changed = True
        if changed:
            self._save_opposite_state()
        confirm = (
            opposite_confirmation(position, cs, f.last_price, f.atr, ref[0], self._cfg)
            if f is not None and ref is not None
            else None
        )
        rule.update(
            opposite_card_action="confirm_or_tighten",
            instruction_key=key,
            ab=self._cfg.opposite_card_ab,
            ab_draw=round(draw, 6),
            arm=arm,
            price=f.last_price if f is not None else None,
            atr=f.atr if f is not None else None,
            ref_price=ref[0] if ref is not None else None,
            ref_at=ref[1].isoformat() if ref is not None else None,
            confirm=confirm,
            price_stale_s=round(stale, 1) if stale is not None else None,
        )
        if arm == "close":
            branch, tag = "ab_legacy_close", "（A/B 原规则组）"
            rule["counterfactual"] = {
                "new_rule": None
                if confirm is None
                else ("close" if confirm["confirmed"] else "tighten")
            }
        elif confirm is None:
            branch, tag = "no_market_data_tighten", ""
        elif confirm["confirmed"]:
            branch, tag = "confirmed_close", f"（价格确认：{confirm['why']}）"
        else:
            branch, tag = "tighten", ""
        rule["branch"] = branch
        if branch in ("ab_legacy_close", "confirmed_close"):
            rule["requires_confirmation"] = arm == "confirm_or_tighten"
            self._streak.pop(pid, None)
            self._opposite_logged.pop(pid, None)
            text = f"作战卡反向硬规则：{reason}，不等模型或确认{tag}"
            return self._verdict(position, now, "close", 1.0, 1, text, None, None, card_rule=rule)
        rule["requires_confirmation"] = True
        rule["counterfactual"] = {
            "legacy_action": "close",
            "would_close_at": first[0].isoformat(),
            "would_close_price": first[1],
        }
        why = confirm["why"] if confirm is not None else "没有行情快照，先不判确认"
        how = (
            "在现价启动移动止损"
            if self._cfg.opposite_profit_mode == "trail"
            else "收紧止损到保本附近"
        )
        text = f"作战卡反向（{key}，{reason}）价格未确认（{why}）：{how}，继续盯"
        log_first = self._opposite_logged.get(pid) != (key, branch)
        self._opposite_logged[pid] = (key, branch)
        return self._verdict(
            position, now, "hold", 0.0, 1, text, None, None, card_rule=rule, record=log_first
        )

    def _stale_age(self, f: FeatureSnapshot, now: datetime) -> float | None:
        """快照比 ``keeper_price_max_age_s`` 旧就返回它旧了几秒；没超（或这项关着）返回 None。"""
        limit = self._cfg.keeper_price_max_age_s
        age = (now - f.ts).total_seconds()
        return age if limit > 0 and age > limit else None

    def _giveback_guard(
        self, position: PositionRecord, pnl: float, peak: float
    ) -> tuple[str, dict[str, Any]] | None:
        """回吐保护：最高浮盈到过 ``giveback_arm_r`` R，且已从最高点吐掉 ``giveback_close_pct`` %。
        返回（理由, 决策记录里的数字）；没触发、关了、算不出 R 返回 None。"""
        pct = self._cfg.giveback_close_pct
        if not self._cfg.giveback_on or not position.entry_price or not position.stop_loss_price:
            return None
        stop_bps = abs(position.entry_price / position.stop_loss_price - 1.0) * 1e4
        if stop_bps <= 0 or peak <= 0:
            return None
        peak_r = peak / stop_bps
        if peak_r < self._cfg.giveback_arm_r:
            return None
        gave = (peak - pnl) / peak * 100.0
        if gave < pct:
            return None
        detail = {
            "peak_bps": round(peak, 1),
            "peak_r": round(peak_r, 2),
            "pnl_bps": round(pnl, 1),
            "pnl_r": round(pnl / stop_bps, 2),
            "giveback_pct": round(gave, 1),
            "close_pct": pct,
            "arm_r": self._cfg.giveback_arm_r,
        }
        text = (
            f"回吐保护：最高浮盈 {peak:+.0f} bps（{peak_r:.2f}R），现在 {pnl:+.0f} bps，"
            f"已回吐 {gave:.0f}%（线 {pct:g}%，{self._cfg.giveback_arm_r:g}R 起算）"
        )
        return text, detail

    @staticmethod
    def _card_rule(position: PositionRecord, card: BattleCard | None, now: datetime) -> str | None:
        if card is None or not card.is_valid(now):
            return None  # 卡过期只停开新仓，已有持仓照常盯
        cs = card.for_symbol(position.symbol)
        # 卡里没有 / 只看不做：不投平仓票（研究 v2：只有方向反了才投）
        if cs is None or cs.bias not in (Bias.LONG, Bias.SHORT) or not cs.allowed_signals:
            return None
        if not (cs.issued_at or card.created_at) <= now < (cs.expires_at or card.expires_at):
            return None
        if cs.bias.value != position.side.value:
            return f"新作战卡方向改成 {cs.bias.value}"
        return None

    def _verdict(
        self,
        position: PositionRecord,
        now: datetime,
        action: str,
        p_close: float,
        consecutive: int,
        reason: str,
        request: JevRequest | None,
        r: JevResponse | None,
        *,
        against: tuple[AgainstSignalEvent, bool] | None = None,
        extra: Mapping[str, Any] | None = None,
        observation: dict[str, Any] | None = None,
        card_rule: Mapping[str, Any] | None = None,
        frozen_peak_rule: Mapping[str, Any] | None = None,
        record: bool = True,
    ) -> KeeperVerdict:
        """``record=False``：不写决策记录（反向卡收紧同一张卡只记第一次）。

        ``against``：反向信号叫来的急查，决策记录 output 里写 ``against``（不是就不写）；
        ``extra``：回吐保护的数字，写进 output 的 ``giveback_guard``。"""
        actual_market: dict[str, Any] = {}
        if request is not None:
            try:
                snapshot = json.loads(request.snapshot)
                actual_market = snapshot.get("current_market") or {}
            except (ValueError, TypeError, AttributeError):
                pass
        succeeded = bool(r is not None and r.ok and r.answer(HoldQuestion.ACTION.value) is not None)
        v = KeeperVerdict(
            position_id=position.position_id,
            ts=now,
            action="close" if action == "close" else "hold",
            p_close=min(max(p_close, 0.0), 1.0),
            consecutive=max(consecutive, 1),
            reason=reason,
            jev_request_id=request.request_id if request is not None else None,
            source_market_view_id=actual_market.get("market_view_id"),
            source_market_version=actual_market.get("version"),
            review_succeeded=succeeded,
        )
        if self._rec is not None and record:
            outcome = Outcome.CLOSE if v.action == "close" else Outcome.HOLD
            if r is not None and not r.ok:
                outcome = Outcome.ERROR
            jev_in, jev_out = jev_log_fields(request, r) if r is not None else ({}, {})
            self._rec.record(
                now,
                outcome,
                (
                    f"{position.symbol} 规则判平，等待执行：{reason}"
                    if card_rule and v.action == "close"
                    else f"{position.symbol} 反向卡未确认，收紧止损：{reason}"
                    if card_rule
                    else f"{position.symbol} {'全平' if v.action == 'close' else '拿住'}：{reason}"
                ),
                symbol=position.symbol,
                signal_id=position.signal_id,
                position_id=position.position_id,
                card_id=str(card_rule["card_id"]) if card_rule else position.card_id,
                input=jev_in,
                output={
                    "action": v.action,
                    "p_close": None if card_rule else v.p_close,
                    **({"model_called": False} if card_rule else {}),
                    "consecutive": v.consecutive,
                    "source_market_view_id": v.source_market_view_id,
                    "source_market_version": v.source_market_version,
                    "review_succeeded": v.review_succeeded,
                    **jev_out,
                    **({"giveback_guard": dict(extra)} if extra else {}),
                    **({"card_rule": dict(card_rule)} if card_rule else {}),
                    **({"frozen_peak_exit": dict(frozen_peak_rule)} if frozen_peak_rule else {}),
                    **(
                        {
                            "against": {
                                "ts": against[0].ts.isoformat(),
                                "kind": against[0].kind.value,
                                "side": against[0].side.value,
                                "desc": against[0].desc,
                                "strong": against[0].strong,
                                "one_shot": against[1],
                            }
                        }
                        if against is not None
                        else {}
                    ),
                },
                latency_ms=r.latency_ms if r is not None else 0.0,
                cost_usd=r.cost_usd if r is not None else 0.0,
                model_version=r.model_version if r is not None else None,
            )
        if observation is not None and self._observation_sink is not None:
            with contextlib.suppress(Exception):
                self._observation_sink({**observation, "action": v.action})
        return v

    def _observation_time(self) -> datetime | None:
        try:
            value = self._observation_wall()
            return value if value.tzinfo is not None and value.utcoffset() is not None else None
        except Exception:
            return None

    def _observation_view(self, symbol: str, at: datetime | None) -> dict[str, Any] | None:
        if at is None or self._observation_source is None:
            return None
        try:
            value = self._observation_source(symbol, at)
            return freeze_values(value) if isinstance(value, Mapping) else None
        except Exception as error:
            return {"capture_status": "unavailable", "error_kind": type(error).__name__}

    def _observation_start(
        self, position: PositionRecord, now: datetime, feature: FeatureSnapshot | None
    ) -> dict[str, Any] | None:
        if self._observation_source is None and self._observation_sink is None:
            return None
        at = self._observation_time()
        view = self._observation_view(position.symbol, at)
        if view is not None and feature is not None and "feature_ts" in view:
            price = view.get("price")
            view["matches_request_feature"] = (
                view["feature_ts"] == feature.ts.isoformat()
                and isinstance(price, Mapping)
                and price.get("value") == feature.last_price
            )
        return {
            "schema_version": 1,
            "kind": "keeper_observation",
            "symbol": position.symbol,
            "position_id": position.position_id,
            "review_started_at": now.isoformat(),
            "request_view": view,
            "response_completed_at": None,
            "post_response_view": None,
            "observed_at": at.isoformat() if at else None,
            "attempt_count": 0,
            "model_ok": None,
            "is_execution_fill": False,
        }

    def _observation_response(
        self,
        observation: dict[str, Any] | None,
        symbol: str,
        request: JevRequest,
        response: JevResponse,
        attempts: int,
    ) -> None:
        if observation is None:
            return
        completed = self._observation_time()
        observed = self._observation_time()
        view = self._observation_view(symbol, observed)
        clock_consistent = (
            observed >= completed if observed is not None and completed is not None else None
        )
        if view is not None:
            for name in ("price", "book", "mark"):
                source = view.get(name)
                if isinstance(source, dict):
                    event_at = aware(source.get("source_ts"))
                    source["source_event_after_response"] = (
                        event_at > completed
                        if event_at is not None and completed is not None
                        else None
                    )
        observation.update(
            request_id=request.request_id,
            attempt_count=attempts,
            model_ok=response.ok,
            response_completed_at=completed.isoformat() if completed else None,
            observed_at=observed.isoformat() if observed else None,
            clock_consistent=clock_consistent,
            post_response_view=view,
        )


def opposite_instruction_key(cs: CardSymbol, card: BattleCard) -> str:
    """这条币级反向指令的身份：币|issued_at|方向。整卡换版本（别的币变了）不改它，
    这个币重新开会出了新指令才变。"""
    return f"{cs.symbol}|{(cs.issued_at or card.created_at).isoformat()}|{cs.bias.value}"


def opposite_ab_draw(position_id: str, instruction_key: str) -> float:
    """反向卡 A/B 抽签：持仓号|指令身份 的 SHA-256 前 8 字节 → [0, 1)，重启、重算、
    整卡换版本都一样。"""
    digest = hashlib.sha256(f"{position_id}|{instruction_key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def opposite_confirmation(
    position: PositionRecord,
    cs: CardSymbol,
    price: float,
    atr: float,
    ref_price: float,
    cfg: FastLoopConfig,
) -> dict[str, Any]:
    """反向卡的价格确认（#314），三条任一成立就算确认；每条都记输入，关掉的那条记 ``on=False``。

    ① 亏损：按行情价 ``price`` 已亏到 ``opposite_confirm_loss_r`` 倍 R，R = 账本开仓价到账本止损价
       （开仓时的原始止损距离；收紧价另存，不改它）。
    ② 走势：从第一次看到这张卡时的价格 ``ref_price`` 起，朝不利方向走了 ``opposite_confirm_atr``
       倍 ``atr``（行情快照里止损用的那个 ATR）。
    ③ 关键位（``opposite_confirm_key_level``）：反向卡自己的关键位里，开仓价之外最近的一个——
       卡做空（持多）取开仓价下方最高的支撑 / 枢轴，价格跌破它；卡做多（持空）取开仓价上方最低的
       阻力 / 枢轴，价格涨破它。没有这样的关键位，这条不成立。
    """
    sign = 1.0 if position.side is Side.LONG else -1.0
    entry, stop = position.entry_price, position.stop_loss_price
    r_dist = abs(entry - stop) if entry and stop else 0.0
    pnl_r = sign * (price - entry) / r_dist if entry and r_dist > 0 else None
    loss_on = cfg.opposite_confirm_loss_r > 0
    loss_hit = loss_on and pnl_r is not None and pnl_r <= -cfg.opposite_confirm_loss_r
    against_atr = sign * (ref_price - price) / atr if atr > 0 else None
    atr_on = cfg.opposite_confirm_atr > 0
    atr_hit = atr_on and against_atr is not None and against_atr >= cfg.opposite_confirm_atr
    level: float | None = None
    if entry:
        if cs.bias is Bias.SHORT:
            below = [
                lv.price
                for lv in cs.key_levels
                if lv.kind in (LevelKind.SUPPORT, LevelKind.PIVOT) and lv.price < entry
            ]
            level = max(below) if below else None
        else:
            above = [
                lv.price
                for lv in cs.key_levels
                if lv.kind in (LevelKind.RESISTANCE, LevelKind.PIVOT) and lv.price > entry
            ]
            level = min(above) if above else None
    level_on = cfg.opposite_confirm_key_level
    level_hit = (
        level_on
        and level is not None
        and (price < level if cs.bias is Bias.SHORT else price > level)
    )
    why: list[str] = []
    if loss_hit and pnl_r is not None:
        why.append(f"已亏 {-pnl_r:.2f}R ≥ {cfg.opposite_confirm_loss_r:g}R")
    if atr_hit and against_atr is not None:
        why.append(f"逆向走了 {against_atr:.2f} 倍 ATR ≥ {cfg.opposite_confirm_atr:g}")
    if level_hit and level is not None:
        why.append(f"越过反向卡关键位 {level:g}")
    if not why:
        parts = []
        if loss_on:
            parts.append("浮盈 " + ("未知" if pnl_r is None else f"{pnl_r:+.2f}R"))
        if atr_on:
            parts.append("逆向 " + ("未知" if against_atr is None else f"{against_atr:.2f} ATR"))
        if level_on:
            parts.append("关键位 " + ("无" if level is None else f"{level:g} 未破"))
        why.append("，".join(parts) or "三条确认都关了")
    return {
        "confirmed": bool(loss_hit or atr_hit or level_hit),
        "why": "；".join(why),
        "loss_r": {
            "on": loss_on,
            "threshold": cfg.opposite_confirm_loss_r,
            "r_distance": r_dist or None,
            "pnl_r": None if pnl_r is None else round(pnl_r, 4),
            "hit": bool(loss_hit),
        },
        "atr": {
            "on": atr_on,
            "threshold": cfg.opposite_confirm_atr,
            "atr": atr,
            "against_atr": None if against_atr is None else round(against_atr, 4),
            "hit": bool(atr_hit),
        },
        "key_level": {"on": level_on, "level": level, "hit": bool(level_hit)},
    }


def _snapshot_pnl(rec: Mapping[str, Any]) -> float | None:
    """管家决策记录 ``input.jev.snapshot`` 里的 ``pos.pnl_bps``（没有就 None）。"""
    try:
        snap = json.loads(rec["input"]["jev"]["snapshot"])
        return float(snap["pos"]["pnl_bps"])
    except (KeyError, TypeError, ValueError):
        return None


def _entry_block(rec: Mapping[str, Any]) -> dict[str, Any] | None:
    """拍板记录 → ``{sig, why, p_real, p_take}``；没真正问过拍板人（排队那条）返回 None。

    一道题问法（q6 起）没有「真」，就不写 ``p_real``。"""
    try:
        snap = json.loads(rec["input"]["jev"]["snapshot"])
        answers = rec["output"]["jev"]["answers"]
        out: dict[str, Any] = {"sig": snap["sig"], "why": str(snap.get("why", ""))[:100]}
        if "genuine" in answers:
            out["p_real"] = round(float(answers["genuine"]["real"]), 2)
        out["p_take"] = round(float(answers["take"]["take"]), 2)
        return out
    except (KeyError, TypeError, ValueError):
        return None
