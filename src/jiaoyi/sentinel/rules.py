"""哨兵：特征 + 作战卡 → 候选信号。纯规则，不到 1 毫秒。

五类信号（设计文档 §七）：
- 放量突破：价格冲过（跌破）作战卡关键位，且 1 分钟成交额 ≥ N 倍均值。
- 量能突增：1 分钟成交额 ≥ N 倍均值，且主动买（卖）占比严重偏一边。
- 强平瀑布反弹：1 分钟多头（空头）爆仓额又大又异常，5 分钟被砸（拉）过头，最近 30 秒主动成交已掉头。
- 拥挤交易反向：资金费率极端，且价格和主动成交开始反向（集中平仓的苗头）。
  币安 WebSocket 没有持仓量，第一版只看资金费率；持仓量等选币那边的数据接进来再加。
- 时段与事件：资金费结算（UTC 0/8/16 点）、伦敦和纽约开盘前后几分钟，量能放大且 5 分钟走出
  一个标准差以上，顺势。CPI、议息这类事件日历以后接入。

规则只负责“这里可能有机会”，真假交给拍板人。
没有有效作战卡 → 不出信号；卡里没有这个标的、只看不做、不允许这类信号、方向和卡相反 → 不出。
同一个去重键（标的:信号:方向）冷却期内只发一次。
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from jiaoyi.contracts import (
    BattleCard,
    Bias,
    CandidateSignal,
    FastLoopConfig,
    FeatureSnapshot,
    Forecast,
    Outcome,
    Side,
    SignalKind,
)
from jiaoyi.core.recorder import RoleRecorder
from jiaoyi.interfaces import Predictor

# (时区, 当地时间, 名称)；夏令时由 zoneinfo 处理
_SESSION_OPENS = (
    ("Europe/London", time(8, 0), "伦敦开盘"),
    ("America/New_York", time(9, 30), "纽约开盘"),
)
_FUNDING_HOURS_UTC = (0, 8, 16)


def session_event(now: datetime, window_min: int) -> str | None:
    """``now`` 落在哪个时段窗口里（前后 ``window_min`` 分钟），不在返回 None。"""
    win = timedelta(minutes=window_min)
    utc = now.astimezone(UTC)
    for h in _FUNDING_HOURS_UTC:
        for day_shift in (-1, 0, 1):
            at = datetime.combine(utc.date() + timedelta(days=day_shift), time(h), UTC)
            if abs(utc - at) <= win:
                return f"资金费结算 {h:02d}:00 UTC"
    for tz, local, name in _SESSION_OPENS:
        z = ZoneInfo(tz)
        local_now = now.astimezone(z)
        if local_now.weekday() >= 5:
            continue
        at = datetime.combine(local_now.date(), local, z)
        if abs(now - at) <= win:
            return name
    return None


class RuleTrigger:
    """``Trigger`` 的实现。"""

    def __init__(
        self,
        cfg: FastLoopConfig,
        *,
        dedup_s: int = 30,
        recorder: RoleRecorder | None = None,
        predictor: Predictor | None = None,
    ) -> None:
        self._cfg = cfg
        self._dedup = timedelta(seconds=dedup_s)
        self._rec = recorder
        self._predictor = predictor
        self._prev_price: dict[str, float] = {}
        self._last_fired: dict[str, datetime] = {}

    def check(
        self, features: FeatureSnapshot, card: BattleCard | None, now: datetime
    ) -> list[CandidateSignal]:
        sym = features.symbol
        prev = self._prev_price.get(sym)
        self._prev_price[sym] = features.last_price
        if card is None or not card.is_valid(now):
            return []
        cs = card.for_symbol(sym)
        if cs is None or cs.bias == Bias.NO_TRADE or not cs.allowed_signals:
            return []

        out: list[CandidateSignal] = []
        for kind, side, desc in self._rules(
            features, prev, [lv.price for lv in cs.key_levels], now
        ):
            if kind not in cs.allowed_signals:
                continue
            if cs.bias == Bias.LONG and side != Side.LONG:
                continue
            if cs.bias == Bias.SHORT and side != Side.SHORT:
                continue
            key = f"{sym}:{kind.value}:{side.value}"
            last = self._last_fired.get(key)
            if last is not None and now - last < self._dedup:
                continue
            self._last_fired[key] = now
            sig = CandidateSignal(
                signal_id=f"sig-{sym}-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}",
                symbol=sym,
                kind=kind,
                side_hint=side,
                ts=now,
                trigger_price=features.last_price,
                trigger_desc=desc,
                dedup_key=key,
                card_id=card.card_id,
                features=features,
                forecast=self._forecast(sym, now),
            )
            out.append(sig)
            if self._rec is not None:
                self._rec.record(
                    now,
                    Outcome.ACTION,
                    f"{sym} {_KIND_CN[kind]}（{'多' if side == Side.LONG else '空'}）：{desc}",
                    symbol=sym,
                    signal_id=sig.signal_id,
                    card_id=card.card_id,
                    input={"values": features.values, "last_price": features.last_price},
                    output={"kind": kind.value, "side": side.value, "dedup_key": key},
                )
        return out

    def _forecast(self, symbol: str, now: datetime) -> Forecast | None:
        if self._predictor is None or not self._predictor.enabled:
            return None
        return self._predictor.latest(symbol, now)

    def _rules(
        self,
        f: FeatureSnapshot,
        prev: float | None,
        levels: list[float],
        now: datetime,
    ) -> list[tuple[SignalKind, Side, str]]:
        c = self._cfg
        v = f.values
        price = f.last_price
        vol_ratio = v.get("vol_ratio_1m", 0.0)
        buy_ratio = v.get("taker_buy_ratio_1m", 0.5)
        ret_1m = v.get("ret_1m", 0.0)
        ret_5m = v.get("ret_5m", 0.0)
        cvd_30s = v.get("cvd_30s_usdt", 0.0)
        hits: list[tuple[SignalKind, Side, str]] = []

        # 放量突破
        if prev is not None and vol_ratio >= c.breakout_vol_ratio:
            for lv in levels:
                if prev < lv <= price:
                    hits.append(
                        (
                            SignalKind.VOLUME_BREAKOUT,
                            Side.LONG,
                            f"冲过 {lv:g}，1 分钟量 {vol_ratio:.1f} 倍",
                        )
                    )
                elif prev > lv >= price:
                    hits.append(
                        (
                            SignalKind.VOLUME_BREAKOUT,
                            Side.SHORT,
                            f"跌破 {lv:g}，1 分钟量 {vol_ratio:.1f} 倍",
                        )
                    )

        # 量能突增
        if vol_ratio >= c.surge_vol_ratio:
            if buy_ratio >= c.surge_taker_ratio:
                hits.append(
                    (
                        SignalKind.VOLUME_SURGE,
                        Side.LONG,
                        f"1 分钟量 {vol_ratio:.1f} 倍，主动买 {buy_ratio:.0%}",
                    )
                )
            elif buy_ratio <= 1.0 - c.surge_taker_ratio:
                hits.append(
                    (
                        SignalKind.VOLUME_SURGE,
                        Side.SHORT,
                        f"1 分钟量 {vol_ratio:.1f} 倍，主动卖 {1 - buy_ratio:.0%}",
                    )
                )

        # 强平瀑布反弹
        liq_ratio = v.get("liq_ratio_1m", 0.0)
        drop = c.liq_drop_pct_5m / 100.0
        if liq_ratio >= c.liq_ratio:
            long_liq = v.get("liq_long_usdt_1m", 0.0)
            short_liq = v.get("liq_short_usdt_1m", 0.0)
            if long_liq >= c.liq_min_usdt_1m and ret_5m <= -drop and cvd_30s > 0:
                hits.append(
                    (
                        SignalKind.LIQUIDATION_REBOUND,
                        Side.LONG,
                        f"1 分钟多头爆仓 {long_liq / 1e4:.0f} 万 U，"
                        f"5 分钟跌 {ret_5m:.2%}，主动买已掉头",
                    )
                )
            elif short_liq >= c.liq_min_usdt_1m and ret_5m >= drop and cvd_30s < 0:
                hits.append(
                    (
                        SignalKind.LIQUIDATION_REBOUND,
                        Side.SHORT,
                        f"1 分钟空头爆仓 {short_liq / 1e4:.0f} 万 U，"
                        f"5 分钟涨 {ret_5m:.2%}，主动卖已掉头",
                    )
                )

        # 拥挤交易反向
        funding = v.get("funding_rate")
        if funding is not None:
            if funding >= c.crowded_funding_rate and ret_1m < 0 and buy_ratio < 0.5:
                hits.append(
                    (
                        SignalKind.CROWDED_REVERSAL,
                        Side.SHORT,
                        f"资金费率 {funding:.4%} 拥挤做多，开始回落",
                    )
                )
            elif funding <= -c.crowded_funding_rate and ret_1m > 0 and buy_ratio > 0.5:
                hits.append(
                    (
                        SignalKind.CROWDED_REVERSAL,
                        Side.LONG,
                        f"资金费率 {funding:.4%} 拥挤做空，开始回升",
                    )
                )

        # 时段与事件
        rv = v.get("realized_vol_5m")
        if rv and vol_ratio >= c.session_vol_ratio:
            name = session_event(now, c.session_window_min)
            sigma_5m = rv * math.sqrt(30)  # realized_vol_5m 是 10 秒收益的标准差
            if name is not None and abs(ret_5m) >= sigma_5m > 0:
                side = Side.LONG if ret_5m > 0 else Side.SHORT
                hits.append(
                    (
                        SignalKind.SESSION_EVENT,
                        side,
                        f"{name}，5 分钟 {ret_5m:+.2%}，量 {vol_ratio:.1f} 倍",
                    )
                )
        return hits


_KIND_CN = {
    SignalKind.VOLUME_BREAKOUT: "放量突破",
    SignalKind.VOLUME_SURGE: "量能突增",
    SignalKind.LIQUIDATION_REBOUND: "强平瀑布反弹",
    SignalKind.CROWDED_REVERSAL: "拥挤交易反向",
    SignalKind.SESSION_EVENT: "时段与事件",
}
