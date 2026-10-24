"""哨兵：特征 + 作战卡 → 候选信号。纯规则，不到 1 毫秒。

五类信号（设计文档 §七）：
- 放量突破：价格冲过（跌破）作战卡关键位，且 1 分钟成交额 ≥ N 倍均值。
- 量能突增：1 分钟成交额 ≥ N 倍均值，且主动买（卖）占比严重偏一边。
- 强平瀑布反弹：1 分钟多头（空头）爆仓额又大又异常，5 分钟被砸（拉）过头，最近 30 秒主动成交已掉头。
- 拥挤交易反向：资金费率极端，且价格和主动成交开始反向（集中平仓的苗头）。
  币安 WebSocket 没有持仓量，第一版只看资金费率；持仓量等选币那边的数据接进来再加。
- 时段与事件：资金费结算（UTC 0/8/16 点）、伦敦和纽约开盘前后几分钟，量能放大且 5 分钟走出
  一个标准差以上，顺势。CPI、议息这类事件日历以后接入。

放量提前发现（``burst_10s_on``，默认开）：放量突破、量能突增除了看 1 分钟量，最近 10 秒成交额
折算成每分钟够倍数也算（主动买卖占比用同一段 10 秒），放量开头几秒就能出。描述里写是哪一条触发的。

新信号（研究 2026-10-24，跟档位开；testnet / live 各一个开关默认关，模拟盘照出）：
- 持仓量变化：W 分钟持仓量涨 ≥ th%，同一段价格走 ≥ z 倍平时波动，跟价格方向。
- 大户散户分歧（反着大户）：大户持仓多空比涨 ≥ th%、全市场人数多空比跌 ≥ th% → 空，反过来 → 多
  （论文专题 + 自己数据：跟大户两年都亏，反着做 8h–24h 扣费后打平略赚）。
- BTC 先动、小币跟：BTC 5 分钟走 ≥ z 倍平时波动，这个币 5 分钟还没往同方向走，按 BTC 方向。
这三个慢：去重按各自窗口（BTC 先动 15 分钟）；和卡方向相反只记一行，不发反向信号事件。

规则只负责“这里可能有机会”，真假交给拍板人。
没有有效作战卡 → 不出信号；卡里没有这个标的、只看不做、不允许这类信号、方向和卡相反 → 不出。
同一个去重键（标的:信号:方向）冷却期内只发一次。

和卡方向相反（也在「卡上没允许这类信号」之前判）：不出交易信号，但发一个「反向信号」事件（:meth:`RuleTrigger.pop_against`，
同一个币同一种信号 ``against_dedup_s`` 内只发 1 次）。哨兵不看持仓：有没有仓、叫不叫管家、
开不开临时会，由主循环按账本里此刻的持仓判断（``jiaoyi.core.against``），结果再写回这里
（:meth:`RuleTrigger.note_against_result`），面板那一行「和卡上方向相反」后面接着显示。
卡上信号被清空但方向还在（只盯不开）照样判反向；卡上方向是不做（NO_TRADE）的不判。

为什么没信号：每个币记最近一次的量比 / 主动买卖占比、最近 1 小时量比最高的那一刻、最近一次被
跳过的原因；卡上有但一直没收到成交的币标「没收到行情」。心跳时每币一行写进 module_status 的
``detail``（:meth:`RuleTrigger.detail_lines`）。
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from jiaoyi.contracts import (
    AgainstSignalEvent,
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
from jiaoyi.market.features import BTC

# (时区, 当地时间, 名称)；夏令时由 zoneinfo 处理
_SESSION_OPENS = (
    ("Europe/London", time(8, 0), "伦敦开盘"),
    ("America/New_York", time(9, 30), "纽约开盘"),
)
_FUNDING_HOURS_UTC = (0, 8, 16)
_CME_OPEN = ("America/Chicago", time(17, 0))
"""CME 比特币期货周日 17:00（芝加哥）开盘：周末憋着的价格一次补上（时段研究 10-24）。"""


def session_event(now: datetime, window_min: int, *, cme: bool = False) -> str | None:
    """``now`` 落在哪个时段窗口里（前后 ``window_min`` 分钟），不在返回 None。

    ``cme``：也看 CME 周日开盘（档位 / 模式开关决定）。"""
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
    if cme:
        z = ZoneInfo(_CME_OPEN[0])
        local_now = now.astimezone(z)
        at = datetime.combine(local_now.date(), _CME_OPEN[1], z)
        if local_now.weekday() == 6 and abs(now - at) <= win:
            return "CME 周日开盘"
    return None


_SLOW_KINDS = frozenset({SignalKind.OI_FOLLOW, SignalKind.TOP_VS_CROWD, SignalKind.BTC_LEAD})
"""新信号：反应慢，和卡相反只记不发反向事件（用来反向平仓太敏感）。"""
BTC_LEAD_DEDUP = timedelta(minutes=15)

PEAK_WINDOW = timedelta(hours=1)
QUIET_AFTER = timedelta(minutes=2)
"""卡上的币这么久没来一笔成交，就算「没收到行情」。"""


@dataclass
class _Watch:
    """一个币最近的情况，给「为什么没信号」用。"""

    seen_at: datetime
    vol_ratio: float | None
    buy_ratio: float
    peak_at: datetime
    peak_vol: float | None
    peak_buy: float
    skip: str = ""
    skip_at: datetime | None = None
    extra: str = ""  # 新信号 / 10 秒放量的最近读数（「为什么没信号」那一行后面）


class RuleTrigger:
    """``Trigger`` 的实现。"""

    def __init__(
        self,
        cfg: FastLoopConfig,
        *,
        dedup_s: int = 30,
        recorder: RoleRecorder | None = None,
        predictor: Predictor | None = None,
        live: bool = False,
    ) -> None:
        self._cfg = cfg
        # volume_surge_live 关掉时 testnet / live 不出量能突增；paper 照出攒样本
        self._skip: frozenset[SignalKind] = (
            frozenset({SignalKind.VOLUME_SURGE})
            if live and not cfg.volume_surge_live
            else frozenset()
        )
        # CME 周日开盘：档位开了才看；testnet / live 还要 session_cme_open_live
        self._cme = cfg.session_cme_open and (not live or cfg.session_cme_open_live)
        # 新信号同理：档位开了才判；testnet / live 还要各自的「实盘也出」
        self._oi = cfg.oi_follow_on and (not live or cfg.oi_follow_live)
        self._crowd = cfg.top_crowd_on and (not live or cfg.top_crowd_live)
        self._btc = cfg.btc_lead_on and (not live or cfg.btc_lead_live)
        self._kind_dedup = {
            SignalKind.OI_FOLLOW: timedelta(minutes=cfg.oi_window_min),
            SignalKind.TOP_VS_CROWD: timedelta(minutes=cfg.top_crowd_window_min),
            SignalKind.BTC_LEAD: BTC_LEAD_DEDUP,
        }
        self._dedup = timedelta(seconds=dedup_s)
        self._rec = recorder
        self._predictor = predictor
        self._prev_price: dict[str, float] = {}
        self._last_fired: dict[str, datetime] = {}
        self._watch: dict[str, _Watch] = {}
        self._card: BattleCard | None = None
        self._against: list[AgainstSignalEvent] = []  # 还没交给主循环的反向信号
        self._against_at: dict[str, datetime] = {}  # 去重键 → 上次发反向信号的时间
        self._against_result: dict[str, str] = {}  # 去重键 → 主循环给的处理结果

    def _observe(self, f: FeatureSnapshot, now: datetime) -> _Watch:
        vr = f.values.get("vol_ratio_1m")
        br = f.values.get("taker_buy_ratio_1m", 0.5)
        w = self._watch.get(f.symbol)
        if w is None:
            w = self._watch[f.symbol] = _Watch(now, vr, br, now, vr, br)
        w.seen_at, w.vol_ratio, w.buy_ratio = now, vr, br
        w.extra = self._extra_text(f)
        if now - w.peak_at > PEAK_WINDOW or (vr or 0.0) >= (w.peak_vol or 0.0):
            w.peak_at, w.peak_vol, w.peak_buy = now, vr, br
        return w

    def _extra_text(self, f: FeatureSnapshot) -> str:
        v, c, parts = f.values, self._cfg, []
        vr10 = v.get("vol_ratio_10s")
        if c.burst_10s_on and vr10 is not None:
            parts.append(f"10 秒折算 {vr10:.1f} 倍")
        oi = v.get("oi_w_pct")
        if oi is not None:
            parts.append(f"持仓量 {c.oi_window_min} 分钟 {oi:+.1f}%")
        top, crowd = v.get("top_w_pct"), v.get("crowd_w_pct")
        if top is not None and crowd is not None:
            parts.append(f"大户比 {top:+.1f}% 散户比 {crowd:+.1f}%")
        z = v.get("btc5z")
        if z is not None and f.symbol != BTC:
            parts.append(f"BTC 5 分钟 {z:+.1f} 倍波动")
        return "、".join(parts)

    @staticmethod
    def _note_skip(w: _Watch, reason: str, now: datetime) -> None:
        w.skip, w.skip_at = reason, now

    def check(
        self, features: FeatureSnapshot, card: BattleCard | None, now: datetime
    ) -> list[CandidateSignal]:
        sym = features.symbol
        prev = self._prev_price.get(sym)
        self._prev_price[sym] = features.last_price
        w = self._observe(features, now)
        self._card = card
        if card is None or not card.is_valid(now):
            self._note_skip(w, "没有有效作战卡", now)
            return []
        cs = card.for_symbol(sym)
        if cs is None:
            self._note_skip(w, "不在作战卡上", now)
            return []
        if cs.bias == Bias.NO_TRADE:
            self._note_skip(w, "卡上是只看不做", now)
            return []
        if not cs.allowed_signals:
            self._note_skip(w, "卡上是只看不做", now)  # 方向还在：下面照样判反向

        out: list[CandidateSignal] = []
        for kind, side, desc in self._rules(
            features, prev, [lv.price for lv in cs.key_levels], now
        ):
            name = f"{_KIND_CN[kind]}（{'多' if side == Side.LONG else '空'}）"
            key = f"{sym}:{kind.value}:{side.value}"
            # 反向先于种类过滤判：卡上没列这类信号、或卡上信号被清空（只盯不开）也要能叫管家
            # （只发反向事件，不放开开仓）
            if (cs.bias == Bias.LONG and side != Side.LONG) or (
                cs.bias == Bias.SHORT and side != Side.SHORT
            ):
                if kind in _SLOW_KINDS:
                    self._note_skip(w, f"{name}：方向和作战卡相反，没出", now)
                    continue
                self._against_hit(features, kind, side, desc, key, now)
                done = self._against_result.get(key)
                self._note_skip(w, f"{name}：和卡上方向相反" + (f"，{done}" if done else ""), now)
                continue
            if not cs.allowed_signals:
                self._note_skip(w, "卡上是只看不做", now)
                continue
            if kind in self._skip:
                self._note_skip(w, f"{name}：测试网 / 实盘关了量能突增", now)
                continue
            if kind not in cs.allowed_signals:
                self._note_skip(w, f"{name}：卡上没允许这类信号", now)
                continue
            last = self._last_fired.get(key)
            if last is not None and now - last < max(
                self._dedup, self._kind_dedup.get(kind, self._dedup)
            ):
                self._note_skip(w, f"{name}：冷却中（上次 {last:%H:%M:%S}）", now)
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
                card_version=card.version,
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

    def _against_hit(
        self,
        f: FeatureSnapshot,
        kind: SignalKind,
        side: Side,
        desc: str,
        key: str,
        now: datetime,
    ) -> None:
        """和卡相反：同一个去重键 ``against_dedup_s`` 内只发 1 次反向信号事件。"""
        last = self._against_at.get(key)
        if last is not None and (now - last).total_seconds() < self._cfg.against_dedup_s:
            return
        self._against_at[key] = now
        self._against_result.pop(key, None)
        vr = f.values.get("vol_ratio_1m")
        # 强平瀑布反弹不再一律算强（哨兵研究 10-24：一次就平只在已亏半个止损时才划算）
        strong = (
            kind in (SignalKind.VOLUME_BREAKOUT, SignalKind.LIQUIDATION_REBOUND)
            and (vr or 0.0) >= self._cfg.against_strong_breakout_vol_ratio
        )
        self._against.append(
            AgainstSignalEvent(
                symbol=f.symbol,
                ts=now,
                kind=kind,
                side=side,
                desc=desc,
                price=f.last_price if f.last_price > 0 else None,
                vol_ratio=vr,
                buy_ratio=min(max(f.values.get("taker_buy_ratio_1m", 0.5), 0.0), 1.0),
                strong=strong,
            )
        )

    def pop_against(self) -> list[AgainstSignalEvent]:
        """取走还没处理的反向信号（主循环每笔成交后调用）。"""
        out, self._against = self._against, []
        return out

    def note_against_result(self, sig: AgainstSignalEvent, result: str) -> None:
        """主循环处理完一个反向信号：结果接在面板那一行「和卡上方向相反」后面。"""
        key = f"{sig.symbol}:{sig.kind.value}:{sig.side.value}"
        self._against_result[key] = result
        w = self._watch.get(sig.symbol)
        if w is not None and w.skip.endswith("和卡上方向相反"):
            w.skip = f"{w.skip}，{result}"

    def brief(self, now: datetime) -> str:
        """团队页一句话：卡上能出信号的有几个币、各只做哪边、离阈值最近的是谁。"""
        card = self._card if self._card is not None and self._card.is_valid(now) else None
        if card is None:
            return "没有有效作战卡，不出信号"
        live = [s for s in card.symbols if s.bias is not Bias.NO_TRADE and s.allowed_signals]
        if not live:
            return f"卡上 {len(card.symbols)} 个币都是只看不做，不出信号"
        sides = {b: sum(1 for s in live if s.bias is b) for b in (Bias.LONG, Bias.SHORT)}
        parts = [f"只做多 {sides[Bias.LONG]}" if sides[Bias.LONG] else ""]
        parts.append(f"只做空 {sides[Bias.SHORT]}" if sides[Bias.SHORT] else "")
        both = len(live) - sides[Bias.LONG] - sides[Bias.SHORT]
        parts.append(f"两边都可 {both}" if both else "")
        text = f"卡上能出信号的 {len(live)} 个币（{'、'.join(p for p in parts if p)}）"
        fresh = [
            (w.vol_ratio, sym)
            for sym, w in self._watch.items()
            if sym in {s.symbol for s in live}
            and w.vol_ratio is not None
            and now - w.seen_at <= QUIET_AFTER
        ]
        if fresh:
            vr, sym = max(fresh)
            text += f"，现在量比最高 {sym} {vr:.1f} 倍（突增要 {self._cfg.surge_vol_ratio:g}）"
        return text

    def detail_lines(self, now: datetime, tz: str = "UTC") -> list[str]:
        """每币一行：卡上的币 + 最近看过的币。量比 / 主动占比 / 离阈值多远 / 最近 1 小时最高 /
        最近一次跳过原因；卡上有但没来成交的标「没收到行情」。"""
        z = ZoneInfo(tz)
        c = self._cfg
        card = self._card if self._card is not None and self._card.is_valid(now) else None
        on_card = {s.symbol for s in card.symbols} if card is not None else set()
        lines: list[str] = []
        for sym in sorted(on_card | set(self._watch)):
            w = self._watch.get(sym)
            if w is None or now - w.seen_at > QUIET_AFTER:
                if sym in on_card:
                    since = "" if w is None else f"（最后一笔 {w.seen_at.astimezone(z):%H:%M}）"
                    lines.append(f"{sym} 没收到行情{since}：没订上或没人成交，不会出信号")
                continue
            side = "买" if w.buy_ratio >= 0.5 else "卖"
            share = max(w.buy_ratio, 1.0 - w.buy_ratio)
            if w.vol_ratio is None:
                head = f"{sym} K 线还不够算均量（主动{side} {share:.0%}）"
            else:
                gap = c.surge_vol_ratio - w.vol_ratio
                need = (
                    f"量能突增还差 {gap:.1f} 倍"
                    if gap > 0
                    else (
                        f"量够了，主动{side}还差 {c.surge_taker_ratio - share:.0%}"
                        if share < c.surge_taker_ratio
                        else "够量能突增"
                    )
                )
                head = (
                    f"{sym} 量 {w.vol_ratio:.1f} 倍（突增要 {c.surge_vol_ratio:g}、"
                    f"突破要 {c.breakout_vol_ratio:g}）主动{side} {share:.0%}，{need}"
                )
            if w.peak_vol is not None and now - w.peak_at <= PEAK_WINDOW:
                pside = "买" if w.peak_buy >= 0.5 else "卖"
                pshare = max(w.peak_buy, 1.0 - w.peak_buy)
                head += (
                    f"；1 小时内最高 {w.peak_vol:.1f} 倍 主动{pside} {pshare:.0%}"
                    f"（{w.peak_at.astimezone(z):%H:%M}）"
                )
            if w.extra:
                head += f"；{w.extra}"
            if sym not in on_card and card is not None:
                head += "；不在作战卡上"
            elif w.skip and w.skip_at is not None and now - w.skip_at <= PEAK_WINDOW:
                head += f"；上次跳过 {w.skip_at.astimezone(z):%H:%M}：{w.skip}"
            lines.append(head)
        return lines

    def _forecast(self, symbol: str, now: datetime) -> Forecast | None:
        if self._predictor is None or not self._predictor.enabled:
            return None
        fc = self._predictor.latest(symbol, now)
        if fc is None:
            self._predictor.request(symbol)  # 后台补算，这次信号不等它
        return fc

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
        # 最近 10 秒折算每分钟（关掉时当没有）
        vr10 = v.get("vol_ratio_10s", 0.0) if c.burst_10s_on else 0.0
        br10 = v.get("taker_buy_ratio_10s", 0.5)
        ret_1m = v.get("ret_1m", 0.0)
        ret_5m = v.get("ret_5m", 0.0)
        cvd_30s = v.get("cvd_30s_usdt", 0.0)
        hits: list[tuple[SignalKind, Side, str]] = []

        # 放量突破：1 分钟量够，或最近 10 秒折算每分钟够
        if vol_ratio >= c.breakout_vol_ratio:
            vol_desc = f"1 分钟量 {vol_ratio:.1f} 倍"
        elif vr10 >= c.breakout_vol_ratio:
            vol_desc = f"最近 10 秒折算每分钟量 {vr10:.1f} 倍"
        else:
            vol_desc = ""
        if prev is not None and vol_desc:
            for lv in levels:
                if prev < lv <= price:
                    hits.append((SignalKind.VOLUME_BREAKOUT, Side.LONG, f"冲过 {lv:g}，{vol_desc}"))
                elif prev > lv >= price:
                    hits.append(
                        (SignalKind.VOLUME_BREAKOUT, Side.SHORT, f"跌破 {lv:g}，{vol_desc}")
                    )

        # 量能突增：先看 1 分钟；不够再看最近 10 秒（量和主动占比都用这 10 秒）
        for ratio, buy, span in (
            (vol_ratio, buy_ratio, "1 分钟"),
            (vr10, br10, "最近 10 秒折算每分钟"),
        ):
            if ratio < c.surge_vol_ratio:
                continue
            if buy >= c.surge_taker_ratio:
                hits.append(
                    (
                        SignalKind.VOLUME_SURGE,
                        Side.LONG,
                        f"{span}量 {ratio:.1f} 倍，主动买 {buy:.0%}",
                    )
                )
                break
            if buy <= 1.0 - c.surge_taker_ratio:
                hits.append(
                    (
                        SignalKind.VOLUME_SURGE,
                        Side.SHORT,
                        f"{span}量 {ratio:.1f} 倍，主动卖 {1 - buy:.0%}",
                    )
                )
                break

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
            name = session_event(now, c.session_window_min, cme=self._cme)
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

        if self._oi:
            hits.extend(self._oi_rule(v))
        if self._crowd:
            hits.extend(self._crowd_rule(v))
        if self._btc and f.symbol != BTC:
            hits.extend(self._btc_rule(v))
        return hits

    def _oi_rule(self, v: dict[str, float]) -> list[tuple[SignalKind, Side, str]]:
        """持仓量变化：W 分钟持仓量涨够、价格同一段走够几倍平时波动，跟价格方向。"""
        c = self._cfg
        oi, ret, vol = v.get("oi_w_pct"), v.get("ret_oiw"), v.get("vol_1m_24h")
        if oi is None or ret is None or not vol or oi < c.oi_min_pct or ret == 0:
            return []
        z = abs(ret) / (vol * math.sqrt(c.oi_window_min))
        if z < c.oi_price_z:
            return []
        side = Side.LONG if ret > 0 else Side.SHORT
        who = "新多头" if side == Side.LONG else "新空头"
        desc = (
            f"{c.oi_window_min} 分钟持仓量涨 {oi:.1f}%，价格 {ret:+.2%}"
            f"（{z:.1f} 倍平时波动），{who}进场"
        )
        return [(SignalKind.OI_FOLLOW, side, desc)]

    def _crowd_rule(self, v: dict[str, float]) -> list[tuple[SignalKind, Side, str]]:
        """大户散户分歧：大户比和散户比往相反方向各变够，反着大户做。"""
        c = self._cfg
        top, crowd = v.get("top_w_pct"), v.get("crowd_w_pct")
        if top is None or crowd is None:
            return []
        th = c.top_crowd_min_pct
        if top >= th and crowd <= -th:
            side, act = Side.SHORT, "大户加多、散户减多"
        elif top <= -th and crowd >= th:
            side, act = Side.LONG, "大户减多、散户加多"
        else:
            return []
        desc = (
            f"{c.top_crowd_window_min} 分钟大户多空比 {top:+.1f}%、"
            f"散户多空比 {crowd:+.1f}%，{act}，反着大户做"
        )
        return [(SignalKind.TOP_VS_CROWD, side, desc)]

    def _btc_rule(self, v: dict[str, float]) -> list[tuple[SignalKind, Side, str]]:
        """BTC 先动：BTC 5 分钟大动，这个币 5 分钟还没往同方向走，按 BTC 方向。"""
        z, ret = v.get("btc5z"), v.get("ret_5m")
        if z is None or ret is None or abs(z) < self._cfg.btc_lead_z:
            return []
        if z > 0 and ret <= 0:
            side = Side.LONG
        elif z < 0 and ret >= 0:
            side = Side.SHORT
        else:
            return []
        desc = f"BTC 5 分钟走了 {z:+.1f} 倍平时波动，这个币 5 分钟 {ret:+.2%} 还没跟"
        return [(SignalKind.BTC_LEAD, side, desc)]


_KIND_CN = {
    SignalKind.VOLUME_BREAKOUT: "放量突破",
    SignalKind.VOLUME_SURGE: "量能突增",
    SignalKind.LIQUIDATION_REBOUND: "强平瀑布反弹",
    SignalKind.CROWDED_REVERSAL: "拥挤交易反向",
    SignalKind.SESSION_EVENT: "时段与事件",
    SignalKind.OI_FOLLOW: "持仓量变化",
    SignalKind.TOP_VS_CROWD: "大户散户分歧",
    SignalKind.BTC_LEAD: "BTC 先动",
}
