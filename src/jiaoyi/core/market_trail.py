"""大盘移动止损（方向闸门，2026-10-30，Aaron 批准）。

参照序列 x 每根收盘的 1 分钟算一次：``btc`` = ln(BTC 永续收盘价)，``eth`` = ln(ETH)，``btc_eth`` =
两者 ln 的平均（等权 2 币指数），``alts`` = 候选池山寨（去掉 BTC / ETH / BNB / 黄金）每分钟对数
收益的等权累加，只做影子。

1. z 分数：r4(t) = x(t) − x(t−240 分钟)；取过去 72 小时里每隔 15 分钟（t−15、t−30 …）的 r4，
   算均值 m、总体标准差 s，z = (r4(t) − m) / s。x 不满 24 小时不算 z，闸门不生效。
2. z 从 [−1, +1] 里面出到 < −1 → 开一段「跌段」（管空单），出到 > +1 → 「涨段」（管多单）。
   没有段、方向和当前段相反、或当前段已止损，才开新段；开段时极值 = 当前 x，这个方向放行。
   z 回到 ±1 里面不结束这一段。
3. ATR：用 1 分钟收盘拼 15 分钟 K 线（开盘 :00 / :15 / :30 / :45，最后一根 1 分钟线收盘时这根
   15 分钟线收盘），Wilder ATR(14)，只用最近一根已收盘的。
4. 每分钟：跌段里 x 创新低 → 极值跟着走、放行；否则 x − 极值 ≥ k × ATR → 止损，锁住新空单。
   涨段对称，锁新多单。锁住到：同方向再创新极值，或按第 2 条开了新段。
5. 锁住时：配置选中的参照（``signal_market_trail_ref``）锁住的方向，所有币都不开新仓（问 Jev 前、
   拿到开仓锁后、读完账户后、挂单改市价追进、面板「重新检查」都查）。另一个方向不受影响；持仓、
   止损、移动止损不动；面板「放行」照旧绕过。

数据只用主循环已经在收的：实时流的 1 分钟 K 线（BTC 一直订，ETH 在盯的时候）、大盘翻转每分钟
那次检查拿到的 1 分钟收盘（BTC、候选池里的币）、启动回填 K 线时顺带拿到的那段；不另外请求币安。
第一次启动 / 重启后，用 ``data/regime_checks/`` 里已经落盘的每分钟收盘补历史。缺的分钟按上一分钟
的值补（最多 6 小时，再长就清空重攒）；数据断了时锁住的方向一直锁着，等数据回来再按规则走。

四条序列一直在算（开关关着也算，打开时不用重攒 24 小时）。锁住 / 放行写风控员的决策记录
（只写选中的参照、开关开着时）；``signal_market_trail_shadow`` 开着时，每条序列每分钟一行写进
``data/market_trail_shadow/<UTC 日期>.jsonl``，过几天比较换哪个参照会拦掉哪些真实开仓。
状态存 ``data/market_trail.json``，重启接着算。
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jiaoyi.contracts import FastLoopConfig, Interval, Kline, Outcome, Side
from jiaoyi.core.recorder import RoleRecorder

logger = logging.getLogger(__name__)

STATE_FILE = "market_trail.json"
SHADOW_DIR = "market_trail_shadow"
REGIME_CHECKS_DIR = "regime_checks"
BTC, ETH = "BTCUSDT", "ETHUSDT"
REFS = ("btc", "eth", "btc_eth")
SERIES = (*REFS, "alts")
LABEL = {"btc": "BTC", "eth": "ETH", "btc_eth": "BTC+ETH", "alts": "山寨篮子"}
_INPUTS: dict[str, tuple[str, ...]] = {
    "btc": (BTC,),
    "eth": (ETH,),
    "btc_eth": (BTC, ETH),
    "alts": ("sample",),
}
ALT_EXCLUDE = frozenset({BTC, ETH, "BNBUSDT", "PAXGUSDT", "XAUTUSDT"})
ALT_MAX_RET = 0.1
"""单币一分钟对数收益超过这个当坏数据，不进篮子（和离线回放一致）。"""

MIN_HISTORY = 1440
WINDOW = 4320
R4 = 240
STEP = 15
KEEP = WINDOW + R4 + 1
ATR_N = 14
BAR_S = 900
GRACE_S = 90
"""一分钟收盘后最多等几秒凑齐另一路数据（大盘翻转检查每分钟一次、相位不定）。"""
WARMUP_S = 600
"""启动后先等 BTC 回填 K 线（``release_fill``）再按上一分钟补缺、判断长缺口；最多等这么久。"""
MAX_FILL_MIN = 360
SAVE_EVERY_S = 60
STALE_S = 300
SEED_HOURS = 80

_SIDE = {-1: Side.SHORT, 1: Side.LONG}
_SIDE_CN = {-1: "空单", 1: "多单"}
_LEG_CN = {-1: "跌段", 1: "涨段"}
_EXT_CN = {-1: "低点", 1: "高点"}
_MOVE_CN = {-1: "反弹", 1: "回落"}
_NEW_CN = {-1: "创新低", 1: "创新高"}


def minute_end(k: Kline) -> int:
    return int(k.open_time.timestamp()) + 60


@dataclass
class Leg:
    side: int
    """−1 = 跌段（管空单），+1 = 涨段（管多单）。"""
    extreme: float
    extreme_at: int
    stopped: bool = False
    stopped_at: int | None = None


@dataclass
class TrailSeries:
    """一条参照序列的逐分钟状态机。时间都是 1 分钟线的收盘时刻（UTC 秒，整分）。"""

    xs: deque[float] = field(default_factory=lambda: deque(maxlen=KEEP))
    last_t: int | None = None
    count: int = 0
    """累计多少分钟（超过 KEEP 也接着数，只用来判断满没满 24 小时）。"""
    bar: list[float] | None = None
    """正在拼的 15 分钟线 [开盘时刻, 高, 低, 收]。"""
    prev_close: float | None = None
    atr: float | None = None
    zone: int = 0
    z: float | None = None
    leg: Leg | None = None
    fill: dict[str, float] = field(default_factory=dict)
    """补缺用的上一分钟值：btc / eth 是各币 ln 价，alts 是各币上一分钟价格。"""

    @property
    def warm(self) -> bool:
        return self.count > MIN_HISTORY

    @property
    def locked(self) -> int | None:
        return self.leg.side if self.leg is not None and self.leg.stopped else None

    def retrace_atr(self) -> float | None:
        if self.leg is None or not self.atr or not self.xs:
            return None
        return self.leg.side * (self.leg.extreme - self.xs[-1]) / self.atr

    def step(self, t: int, x: float, k: float) -> list[dict[str, Any]]:
        """收进 t 这一分钟的 x，返回这一分钟发生的事（开段 / 锁住 / 放行）。"""
        self.xs.append(x)
        self.last_t = t
        self.count += 1
        self._bar(t, x)
        self.z = self._z()
        events: list[dict[str, Any]] = []
        if self.z is not None:
            zone = 1 if self.z > 1 else -1 if self.z < -1 else 0
            if zone and zone != self.zone:
                old = self.leg
                if old is None or old.side != zone or old.stopped:
                    if old is not None and old.stopped:
                        events.append(self._event("unlock", t, old, why="leg"))
                    self.leg = Leg(zone, x, t)
                    events.append(self._event("leg", t, self.leg))
            self.zone = zone
        leg = self.leg
        if leg is not None:
            if leg.side * (x - leg.extreme) > 0:
                if leg.stopped:
                    events.append(self._event("unlock", t, leg, why="extreme"))
                leg.extreme, leg.extreme_at = x, t
                leg.stopped, leg.stopped_at = False, None
            elif (
                not leg.stopped
                and self.atr is not None
                and self.atr > 0
                and leg.side * (leg.extreme - x) >= k * self.atr
            ):
                leg.stopped, leg.stopped_at = True, t
                events.append(self._event("lock", t, leg))
        return events

    def _event(self, kind: str, t: int, leg: Leg, *, why: str = "") -> dict[str, Any]:
        out: dict[str, Any] = {
            "kind": kind,
            "t": t,
            "side": leg.side,
            "extreme": leg.extreme,
            "x": self.xs[-1],
            "atr": self.atr,
            "z": self.z,
            "retrace_atr": (
                leg.side * (leg.extreme - self.xs[-1]) / self.atr if self.atr else None
            ),
        }
        if kind == "unlock":
            out["why"] = why
            out["locked_min"] = (t - (leg.stopped_at or t)) // 60
        return out

    def _bar(self, t: int, x: float) -> None:
        start = (t - 60) // BAR_S * BAR_S
        if self.bar is not None and self.bar[0] != start:
            self._close_bar()  # 断档后接上：上一根没收齐也收掉
        if self.bar is None:
            self.bar = [float(start), x, x, x]
        else:
            self.bar[1] = max(self.bar[1], x)
            self.bar[2] = min(self.bar[2], x)
            self.bar[3] = x
        if t % BAR_S == 0:
            self._close_bar()

    def _close_bar(self) -> None:
        assert self.bar is not None
        _, h, lo, c = self.bar
        pc = self.prev_close
        tr = h - lo if pc is None else max(h, pc) - min(lo, pc)
        self.atr = tr if self.atr is None else self.atr + (tr - self.atr) / ATR_N
        self.prev_close = c
        self.bar = None

    def _z(self) -> float | None:
        if not self.warm:
            return None
        xs = self.xs
        i = len(xs) - 1
        window = [xs[j] - xs[j - R4] for j in range(i - STEP, max(R4, i - WINDOW) - 1, -STEP)]
        if not window:
            return None
        m = sum(window) / len(window)
        s = math.sqrt(sum((w - m) ** 2 for w in window) / len(window))
        if s <= 0:
            return None
        return (xs[i] - xs[i - R4] - m) / s

    def to_json(self) -> dict[str, Any]:
        leg = self.leg
        return {
            "xs": list(self.xs),
            "last_t": self.last_t,
            "count": self.count,
            "bar": self.bar,
            "prev_close": self.prev_close,
            "atr": self.atr,
            "zone": self.zone,
            "z": self.z,
            "leg": None if leg is None else leg.__dict__,
            "fill": self.fill,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any]) -> TrailSeries:
        leg = d.get("leg")
        return cls(
            xs=deque((float(v) for v in d["xs"]), maxlen=KEEP),
            last_t=d["last_t"],
            count=int(d["count"]),
            bar=d["bar"],
            prev_close=d["prev_close"],
            atr=d["atr"],
            zone=int(d["zone"]),
            z=d["z"],
            leg=None if leg is None else Leg(**leg),
            fill={str(s): float(v) for s, v in d["fill"].items()},
        )


class MarketTrail:
    """见模块说明。``cfg`` 是热生效时原地改的那份 ``FastLoopConfig``，每次现读。"""

    def __init__(
        self,
        cfg: FastLoopConfig,
        data_dir: Path,
        *,
        tz: str = "Asia/Bangkok",
        recorder: RoleRecorder | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        seed: bool = True,
    ) -> None:
        self._cfg = cfg
        self._dir = data_dir
        self._path = data_dir / STATE_FILE
        self._tz = ZoneInfo(tz)
        self._rec = recorder
        self._clock = clock
        self._started = clock().timestamp()
        self._released = False
        self._shadow_rows: list[tuple[Path, str]] = []
        self._series: dict[str, TrailSeries] = {name: TrailSeries() for name in SERIES}
        self._obs: dict[str, dict[int, float]] = {BTC: {}, ETH: {}}
        self._samples: dict[int, dict[str, float]] = {}
        self._saved_at = 0.0
        self._dirty = False
        self._active: tuple[bool, str] | None = None
        self._floor: dict[str, int] = {}  # 清空重攒的序列从这一分钟起
        self._load()
        if seed:
            self._seed_from_regime_checks()

    # ------------------------------------------------------------ 收数据
    def observe_kline(self, k: Kline) -> None:
        if k.interval is Interval.M1 and k.symbol in self._obs:
            self.observe_close(k.symbol, minute_end(k), k.close)

    def observe_klines(self, klines: Iterable[Kline]) -> None:
        for k in klines:
            self.observe_kline(k)

    def observe_close(self, symbol: str, t: int, close: float) -> None:
        if close > 0 and symbol in self._obs and t % 60 == 0:
            self._obs[symbol].setdefault(t, close)

    def observe_sample(
        self, at: datetime, btc_px: float | None, prices: Mapping[str, float]
    ) -> None:
        """大盘翻转检查的一次取样：``at`` 是那根 1 分钟线的收盘时刻，``prices`` 是候选池各币。"""
        t = int(at.timestamp())
        if t % 60:
            return
        if btc_px:
            self.observe_close(BTC, t, btc_px)
        elif prices.get(BTC):
            self.observe_close(BTC, t, prices[BTC])
        if prices.get(ETH):
            self.observe_close(ETH, t, prices[ETH])
        alts = {s: float(p) for s, p in prices.items() if s not in ALT_EXCLUDE and p and p > 0}
        if alts:
            self._samples.setdefault(t, alts)

    # ------------------------------------------------------------ 每分钟推进
    def tick(self, now: datetime | None = None) -> None:
        now = now or self._clock()
        changed = self._advance(now.timestamp(), live=True)
        self._flush_shadow()
        self._note_switch(now)
        if changed:
            self._dirty = True
        if self._dirty and (now.timestamp() - self._saved_at >= SAVE_EVERY_S or changed == 2):
            self._save(now.timestamp())

    def _advance(self, now_s: float, *, live: bool) -> int:
        """把能定下来的分钟都算掉。返回 0 = 没动，1 = 算了分钟，2 = 选中的参照锁 / 放行了。"""
        out = 0
        for name in SERIES:
            out = max(out, self._advance_one(name, now_s, live=live))
        self._prune()
        return out

    def _latest(self, inp: str) -> int | None:
        src = self._samples if inp == "sample" else self._obs[inp]
        return max(src) if src else None

    def _has(self, inp: str, t: int) -> bool:
        return t in (self._samples if inp == "sample" else self._obs[inp])

    def _advance_one(self, name: str, now_s: float, *, live: bool) -> int:
        s = self._series[name]
        inputs = _INPUTS[name]
        latest = [v for v in (self._latest(i) for i in inputs) if v is not None]
        if not latest:
            return 0
        top = max(latest)
        if s.last_t is None:
            first = self._first_complete(inputs, top, self._floor.get(name, 0))
            if first is None:
                return 0
            t = first
        else:
            nxt = min((k for i in inputs for k in self._keys(i) if k > s.last_t), default=None)
            if nxt is None:
                return 0
            if nxt - s.last_t > MAX_FILL_MIN * 60 and not (live and self._holding(now_s)):
                self._reset(name, s.last_t, nxt, live=live)
                return self._advance_one(name, now_s, live=live)
            t = s.last_t + 60
        out = 0
        while t <= top:
            complete = all(self._has(i, t) for i in inputs)
            if not complete and live and (now_s < t + GRACE_S or self._holding(now_s)):
                break
            if not complete:
                nxt = min((k for i in inputs for k in self._keys(i) if k > t), default=t)
                if nxt - (t - 60) > MAX_FILL_MIN * 60:
                    self._reset(name, t - 60, nxt, live=live)
                    return max(out, self._advance_one(name, now_s, live=live))
            events = self._step(name, s, t)
            out = max(out, 1)
            if live:
                out = max(out, self._emit(name, s, events, t))
            t += 60
        return out

    def release_fill(self) -> None:
        """启动回填的 BTC K 线已经收下（成功失败都算）：之后缺的分钟可以按上一分钟补。"""
        self._released = True

    def _holding(self, now_s: float) -> bool:
        return not self._released and now_s < self._started + WARMUP_S

    def _keys(self, inp: str) -> Iterable[int]:
        return self._samples if inp == "sample" else self._obs[inp]

    def _first_complete(self, inputs: tuple[str, ...], top: int, floor: int) -> int | None:
        keys = sorted(t for t in self._keys(inputs[0]) if floor <= t <= top)
        return next((t for t in keys if all(self._has(i, t) for i in inputs)), None)

    def _step(self, name: str, s: TrailSeries, t: int) -> list[dict[str, Any]]:
        if name == "alts":
            x = self._alts_value(s, t)
        else:
            for sym in _INPUTS[name]:
                if (px := self._obs[sym].get(t)) is not None:
                    s.fill[sym] = math.log(px)
            x = sum(s.fill[sym] for sym in _INPUTS[name]) / len(_INPUTS[name])
        return s.step(t, x, self._cfg.signal_market_trail_k)

    def _alts_value(self, s: TrailSeries, t: int) -> float:
        x = s.xs[-1] if s.xs else 0.0
        sample = self._samples.get(t)
        if sample is None:
            return x
        rets = []
        for sym, px in sample.items():
            prev = s.fill.get(sym)
            if prev:
                r = math.log(px / prev)
                if abs(r) < ALT_MAX_RET:
                    rets.append(r)
        s.fill = dict(sample)  # 掉出候选池的币不留旧价：回来时从那一分钟重新算
        return x + sum(rets) / len(rets) if rets else x

    def _reset(self, name: str, last: int, nxt: int, *, live: bool) -> None:
        old = self._series[name]
        self._series[name] = TrailSeries()
        self._floor[name] = nxt
        if live and self._is_active(name) and old.locked is not None:
            self._note(
                self._clock(),
                Outcome.INFO,
                f"大盘移动止损：{LABEL[name]} 数据断了 {(nxt - last) / 3600:.1f} 小时，"
                f"清空重攒，恢复新{_SIDE_CN[old.locked]}（要再攒 24 小时才生效）",
                {"kind": "reset", "ref": name, "gap_min": (nxt - last) // 60},
            )

    def _prune(self) -> None:
        stores: list[dict[int, Any]] = [*self._obs.values(), self._samples]
        newest = max((t for src in stores for t in src), default=None)
        if newest is None:
            return
        floor = newest - (MAX_FILL_MIN + 30) * 60
        cursors = [s.last_t for s in self._series.values()]
        if all(c is not None for c in cursors):
            floor = max(floor, min(c for c in cursors if c is not None))
        for src in stores:
            for t in [t for t in src if t < floor]:
                del src[t]

    # ------------------------------------------------------------ 记录
    def _is_active(self, name: str) -> bool:
        return bool(self._cfg.signal_market_trail_on) and name == self._cfg.signal_market_trail_ref

    def _emit(self, name: str, s: TrailSeries, events: list[dict[str, Any]], t: int) -> int:
        if self._cfg.signal_market_trail_shadow:
            self._shadow(name, s, t)
        if not self._is_active(name):
            return 1
        out = 1
        for ev in events:
            if ev["kind"] == "leg":
                continue
            out = 2
            side = ev["side"]
            label = LABEL[name]
            at = datetime.fromtimestamp(ev["t"], UTC)
            if ev["kind"] == "lock":
                summary = (
                    f"大盘移动止损：{label} 从{_LEG_CN[side]}{_EXT_CN[side]}{_MOVE_CN[side]} "
                    f"{ev['retrace_atr']:.1f} ATR（门槛 {self._cfg.signal_market_trail_k:.1f}），"
                    f"暂停新{_SIDE_CN[side]}，{_NEW_CN[side]}才恢复"
                )
            else:
                why = (
                    f"创{_LEG_CN[side]}新{'低' if side < 0 else '高'}"
                    if ev["why"] == "extreme"
                    else f"开了新的{_LEG_CN[s.leg.side if s.leg else side]}（z {ev['z']:+.2f}）"
                )
                summary = (
                    f"大盘移动止损：{label} {why}，恢复新{_SIDE_CN[side]}"
                    f"（锁了 {ev['locked_min']} 分钟）"
                )
            self._note(
                at,
                Outcome.INFO,
                summary,
                {
                    **ev,
                    "ref": name,
                    "direction": _SIDE[side].value,
                    "minute": at.isoformat(),
                    "k": self._cfg.signal_market_trail_k,
                },
            )
        return out

    def _shadow(self, name: str, s: TrailSeries, t: int) -> None:
        at = datetime.fromtimestamp(t, UTC)
        row = {
            "t": at.strftime("%Y-%m-%dT%H:%MZ"),
            "ref": name,
            "lock": _SIDE[s.locked].value if s.locked is not None else None,
            "leg": _SIDE[s.leg.side].value if s.leg is not None else None,
            "z": round(s.z, 3) if s.z is not None else None,
            "rt": round(r, 3) if (r := s.retrace_atr()) is not None else None,
        }
        path = self._dir / SHADOW_DIR / f"{at.date().isoformat()}.jsonl"
        self._shadow_rows.append((path, json.dumps(row, separators=(",", ":"))))

    def _flush_shadow(self) -> None:
        rows, self._shadow_rows = self._shadow_rows, []
        by_path: dict[Path, list[str]] = {}
        for path, line in rows:
            by_path.setdefault(path, []).append(line)
        for path, lines in by_path.items():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            except OSError:
                logger.exception("写 %s 失败", path)

    def _note_switch(self, now: datetime) -> None:
        active = (bool(self._cfg.signal_market_trail_on), self._cfg.signal_market_trail_ref)
        if active == self._active:
            return
        first, self._active = self._active is None, active
        if active[0] and not first:
            self._note(
                now, Outcome.INFO, f"{self.status_line(now)}（设置刚改）", {"kind": "switch"}
            )

    def _note(self, now: datetime, outcome: Outcome, summary: str, out: dict[str, Any]) -> None:
        if self._rec is not None:
            self._rec.record(now, outcome, summary, output={"market_trail": out})

    # ------------------------------------------------------------ 开仓前
    def holds(self, side: Side, now: datetime | None = None) -> str | None:
        """这个方向的新仓此刻锁不锁：锁就返回拒绝理由（只看不动）。"""
        if not self._cfg.signal_market_trail_on:
            return None
        name = self._cfg.signal_market_trail_ref
        s = self._series[name]
        if not s.warm or s.locked is None or _SIDE[s.locked] is not side or s.leg is None:
            return None
        now = now or self._clock()
        mins = max(0, int(now.timestamp() - (s.leg.stopped_at or now.timestamp())) // 60)
        rt = s.retrace_atr() or 0.0
        d, k = s.locked, self._cfg.signal_market_trail_k
        return (
            f"大盘移动止损：{_SIDE_CN[d]}锁住中（{LABEL[name]} {_MOVE_CN[d]}过 {k:.1f} ATR 门槛，"
            f"现在离{_EXT_CN[d]} {rt:.1f} ATR，已锁 {mins} 分钟；{_NEW_CN[d]}才解锁）"
        )

    def status_line(self, now: datetime | None = None) -> str:
        """团队页风控员一行，例「大盘移动止损：空单锁住中（BTC 现在离低点 0.5 ATR · 门槛 1.0 ·
        已锁 45 分钟）」。"""
        now = now or self._clock()
        line = self._status(now)
        last = self._series[self._cfg.signal_market_trail_ref].last_t
        if last is not None and now.timestamp() - last > STALE_S:
            at = datetime.fromtimestamp(last, UTC).astimezone(self._tz)
            line += f"；数据停在 {at:%H:%M}，状态不变"
        return line

    def _status(self, now: datetime) -> str:
        name = self._cfg.signal_market_trail_ref
        label = LABEL[name]
        s = self._series[name]
        if not s.warm:
            return (
                f"大盘移动止损（{label}）：历史不足 24 小时（已有 {s.count / 60:.0f} 小时），暂不拦"
            )
        if s.leg is None:
            return f"大盘移动止损（{label}）：还没有跌段 / 涨段，不拦"
        side = s.leg.side
        rt = s.retrace_atr() or 0.0
        if s.locked is not None:
            mins = max(0, int(now.timestamp() - (s.leg.stopped_at or now.timestamp())) // 60)
            return (
                f"大盘移动止损：{_SIDE_CN[side]}锁住中（{label} 现在离{_EXT_CN[side]} {rt:.1f} ATR"
                f" · 门槛 {self._cfg.signal_market_trail_k:.1f} · 已锁 {mins} 分钟）"
            )
        return (
            f"大盘移动止损：{label} {_LEG_CN[side]}，{_SIDE_CN[side]}放行"
            f"（离{_EXT_CN[side]} {rt:.1f} / {self._cfg.signal_market_trail_k:g} ATR）"
        )

    def state(self, name: str) -> TrailSeries:
        return self._series[name]

    # ------------------------------------------------------------ 存档
    def _seed_from_regime_checks(self) -> None:
        """用已经落盘的每分钟检查补历史（只读本机文件）；补出来的锁 / 放行不写记录。"""
        folder = self._dir / REGIME_CHECKS_DIR
        if not folder.is_dir():
            return
        now = self._clock()
        cursors = [s.last_t for s in self._series.values() if s.last_t is not None]
        since = now - timedelta(hours=SEED_HOURS)
        if len(cursors) == len(SERIES):
            since = max(since, datetime.fromtimestamp(min(cursors), UTC))
        first_day = since.date()
        seen = 0
        for path in sorted(folder.glob("*.jsonl")):
            try:
                if date.fromisoformat(path.stem) < first_day:
                    continue
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, ValueError):
                continue
            for line in lines:
                try:
                    row = json.loads(line)
                    at = row.get("sample_at")
                    if not at:
                        continue
                    ts = datetime.fromisoformat(at)
                    if ts < since:
                        continue
                    self.observe_sample(ts, row.get("btc_px"), row.get("prices") or {})
                    seen += 1
                except (ValueError, TypeError, AttributeError):
                    continue
        if seen:
            self._advance(now.timestamp(), live=False)
            self._dirty = True
            logger.info("大盘移动止损：用 regime_checks 补了 %d 分钟", seen)

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for name in SERIES:
                if (d := raw.get("series", {}).get(name)) is not None:
                    self._series[name] = TrailSeries.from_json(d)
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            logger.exception("读 %s 失败，从头攒历史", self._path)
            self._series = {name: TrailSeries() for name in SERIES}

    def save(self) -> None:
        self._save(self._clock().timestamp())

    def _save(self, now_s: float) -> None:
        data = {"version": 1, "series": {n: s.to_json() for n, s in self._series.items()}}
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self._path)
            self._saved_at, self._dirty = now_s, False
        except OSError:
            logger.exception("写 %s 失败，重启后要重新攒历史", self._path)
