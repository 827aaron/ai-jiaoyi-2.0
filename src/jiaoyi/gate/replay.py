"""拍板回放测试：用币安历史行情重建过去几天的信号，真问一遍 Jev，再按之后的 K 线算「做了会怎样」。

和 ``jiaoyi jev-backtest`` 的区别：回测只能算以前实盘里真出现过的信号（数量少）；回放自己造信号，
一次能攒几百个，用来尽快校准拍板门槛。流程：

1. 选币：默认取 ``data/card_review/cards.jsonl`` 里总盘会最常选的几个币，也可以 ``--symbols`` 指定。
2. 每个币每天从 data.binance.vision 下载 1 分钟 K 线和逐笔成交（aggTrades，
   缓存在 ``data/history``），先用前几天的 K 线把 ATR 预热好，
   再按时间顺序灌进和实盘同一个特征引擎、同一个哨兵规则。
3. 作战卡是回放用的：每个币一张、方向两边都行、所有信号类型都开；关键位取前 24 小时和前 4 小时的
   高低点；止损 ATR 倍数 / 止盈 R 用当前最长持仓档的推荐范围。
4. 所有信号里均匀抽 ``n`` 个，按当前模板（``TEMPLATE_VERSION``）和最长持仓问 Jev
   （并发 ``concurrency``）。
5. 止损 = Jev 选的止损档（窄 / 中 / 宽）× 和风控员同一个止损 ATR；止盈 = 止损 × 卡上最小 R；
   之后的 1 分钟线算结果，和回测同一套算法（``outcomes.simulate``）。

历史数据缺的：盘口（快照里 ``book`` 写 null）、爆仓（为 0，所以不会出「强平瀑布反弹」）、
资金费率（没有，所以不会出「拥挤交易反向」）、预测员（不跑）。报告里会写明。
结果：``data/replay/jev/<时间>/``（calls.jsonl 每次调用的快照和概率、outcomes.jsonl、report.json），
汇总另存 ``data/debug/jev_replay.json``。
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi.backtest.data import (
    BASE_URL,
    DEFAULT_CACHE,
    download,
    fetch_day,
    iter_agg_trades,
    load_klines,
    merge_events,
)
from jiaoyi.contracts import (
    BattleCard,
    Bias,
    CandidateSignal,
    CardSymbol,
    Category,
    EntryQuestion,
    FastLoopConfig,
    Interval,
    JevRequest,
    JevResponse,
    KeyLevel,
    Kline,
    LevelKind,
    Range,
    SignalKind,
    SizeTier,
    Trade,
)
from jiaoyi.core import profiles
from jiaoyi.gate import outcomes
from jiaoyi.gate.questions import (
    TEMPLATE_VERSION,
    entry_questions,
    entry_snapshot,
    template_fingerprint,
)
from jiaoyi.interfaces import DecisionModel
from jiaoyi.market.features import RollingFeatureEngine, backfill_minutes
from jiaoyi.market.rest import RateLimited
from jiaoyi.risk.guard import capped_tp_r, stop_atr
from jiaoyi.sentinel.rules import RuleTrigger

log = logging.getLogger(__name__)

REPLAY_DIR = Path("replay") / "jev"
REPORT_FILE = Path("debug") / "jev_replay.json"
DEFAULT_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT")
EVAL_EVERY = timedelta(milliseconds=250)
"""每个币最多隔这么久看一次哨兵（和回放引擎一致，不用逐笔都看）。"""
MISSING = "回放没有盘口（book=null）、爆仓（为 0）、资金费率和预测员数据"

KlineRange = Callable[[str, datetime, datetime], Awaitable[list[Kline]]]


@dataclass(frozen=True)
class Found:
    signal: CandidateSignal
    card: BattleCard


def default_symbols(data_dir: str | Path, k: int = 8) -> list[tuple[str, Category]]:
    """总盘会最常选的 ``k`` 个币（按 cards.jsonl 出现次数）；没有流水就用几个主流币。"""
    path = Path(data_dir) / "card_review" / "cards.jsonl"
    count: Counter[str] = Counter()
    cat: dict[str, Category] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                sym = str(row["symbol"])
                cat[sym] = Category(row.get("category", "alt"))
            except (ValueError, KeyError, TypeError):
                continue
            count[sym] += 1
    if not count:
        return [(s, Category.MAJOR) for s in DEFAULT_SYMBOLS]
    return [(s, cat[s]) for s, _ in count.most_common(k)]


def key_levels(bars: list[Kline]) -> list[KeyLevel]:
    """前 24 小时和前 4 小时的高低点（``bars`` 为按时间排好的 1 分钟线）。"""
    out: list[KeyLevel] = []
    for minutes, name in ((1440, "24h"), (240, "4h")):
        seg = bars[-minutes:]
        if not seg:
            continue
        hi, lo = max(b.high for b in seg), min(b.low for b in seg)
        out.append(KeyLevel(price=hi, kind=LevelKind.RESISTANCE, note=f"前 {name} 高点"))
        out.append(KeyLevel(price=lo, kind=LevelKind.SUPPORT, note=f"前 {name} 低点"))
    return out


def replay_card(
    symbol: str, category: Category, levels: list[KeyLevel], fast: FastLoopConfig, day: date
) -> BattleCard:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    cs = CardSymbol(
        symbol=symbol,
        category=category,
        bias=Bias.BOTH,
        key_levels=levels,
        allowed_signals=list(SignalKind),
        size_tier_max=SizeTier.L,
        notional_usdt=Range(min=100.0, max=2000.0),
        leverage=Range(min=1.0, max=5.0),
        stop_atr=Range(min=fast.min_stop_atr, max=fast.max_stop_atr),
        take_profit_r=Range(min=fast.min_tp_r, max=fast.max_tp_r),
        reason="回放测试用的卡：两边都可以，关键位取前 24h / 4h 高低点",
    )
    return BattleCard(
        card_id=f"replay-{symbol}-{day.isoformat()}",
        version=1,
        created_at=start,
        expires_at=start + timedelta(days=1, hours=1),
        source="replay",
        regime="历史回放（没有军师判断）",
        symbols=[cs],
    )


def _klines_only(symbol: str, day: date, cache: Path) -> list[Kline]:
    name = f"{symbol}-1m-{day.isoformat()}.zip"
    path = download(f"{BASE_URL}/klines/{symbol}/1m/{name}", cache / symbol / name)
    return load_klines(path, symbol, Interval.M1)


def scan_symbol(
    symbol: str,
    category: Category,
    days: list[date],
    fast: FastLoopConfig,
    *,
    dedup_s: int,
    cache: Path = DEFAULT_CACHE,
) -> Iterator[Found]:
    """一个币逐天回放，产出哨兵发出的每个信号（和当天的回放卡）。下载失败的那天跳过。"""
    engine = RollingFeatureEngine(
        atr_period_min=fast.atr_period_min, atr_long_period_min=fast.atr_long_period_min
    )
    trigger = RuleTrigger(fast, dedup_s=dedup_s)
    warm = -(-backfill_minutes(fast.atr_period_min, fast.atr_long_period_min) // 1440)
    history: list[Kline] = []
    for i in range(max(warm, 1), 0, -1):
        try:
            bars = _klines_only(symbol, days[0] - timedelta(days=i), cache)
        except OSError as e:
            log.warning("%s 预热 K 线下载失败：%s", symbol, e)
            continue
        for k in bars:
            engine.update(k)
        history.extend(bars)
    for day in days:
        try:
            k_path, t_path = fetch_day(symbol, day, cache)
        except OSError as e:
            log.warning("%s %s 历史数据下载失败，跳过：%s", symbol, day, e)
            continue
        card = replay_card(symbol, category, key_levels(history), fast, day)
        day_bars = load_klines(k_path, symbol)
        last: datetime | None = None
        for ev in merge_events(day_bars, iter_agg_trades(t_path, symbol)):
            engine.update(ev)
            if not isinstance(ev, Trade):
                continue
            if last is not None and ev.ts - last < EVAL_EVERY:
                continue
            last = ev.ts
            snap = engine.snapshot(symbol)
            if snap is None or snap.atr <= 0:
                continue
            for sig in trigger.check(snap, card, ev.ts):
                yield Found(sig, card)
        history = (history + day_bars)[-1440:]


def _scan_all(
    symbol: str,
    category: Category,
    days: list[date],
    fast: FastLoopConfig,
    dedup_s: int,
    cache: Path,
) -> list[Found]:
    return list(scan_symbol(symbol, category, days, fast, dedup_s=dedup_s, cache=cache))


def pick(found: list[Found], n: int, seed: int = 0) -> list[Found]:
    """多于 ``n`` 个就随机均匀抽（固定种子，可复现），按时间排好。"""
    if len(found) > n:
        found = random.Random(seed).sample(found, n)
    return sorted(found, key=lambda f: f.signal.ts)


def _snapshot(f: Found, hold_min: int) -> str:
    d = json.loads(entry_snapshot(f.signal, f.card, hold_min))
    d.setdefault("book", None)  # 历史数据没有盘口
    return json.dumps(d, ensure_ascii=False, separators=(",", ":"))


async def ask_all(
    model: DecisionModel, picks: list[Found], hold_min: int, concurrency: int
) -> list[tuple[Found, JevRequest, JevResponse]]:
    sem = asyncio.Semaphore(concurrency)

    async def one(f: Found) -> tuple[Found, JevRequest, JevResponse]:
        req = JevRequest(
            request_id=f"replay-{f.signal.signal_id}-{uuid.uuid4().hex[:6]}",
            kind="entry",
            symbol=f.signal.symbol,
            ts=f.signal.ts,
            signal_id=f.signal.signal_id,
            snapshot=_snapshot(f, hold_min),
            questions=entry_questions(),
        )
        async with sem:
            return f, req, await model.ask(req)

    return list(await asyncio.gather(*(one(f) for f in picks)))


def candidate(
    f: Found, resp: JevResponse, fast: FastLoopConfig, hold_min: int
) -> outcomes.Candidate | None:
    """Jev 的回答 → 和风控员同样算法的止损 / 止盈（不含关键位外推），再交给 ``simulate``。"""
    if not resp.ok:
        return None
    sig = f.signal
    cs = f.card.symbols[0]
    stop_ans = resp.answer(EntryQuestion.STOP_WIDTH.value)
    tier = stop_ans.top()[0] if stop_ans is not None else "mid"
    lo, hi = cs.stop_atr.min, cs.stop_atr.max
    mult = {"narrow": lo, "mid": (lo + hi) / 2.0, "wide": hi}.get(tier, (lo + hi) / 2.0)
    atr = stop_atr(sig.features, fast, sig.ts, {})
    stop_bps = mult * atr / sig.trigger_price * 1e4
    if stop_bps <= 0:
        return None

    def p(q: EntryQuestion, choice: str) -> float | None:
        a = resp.answer(q.value)
        return a.probs.get(choice) if a is not None else None

    p_take = p(EntryQuestion.TAKE, "take")
    p_real = p(EntryQuestion.GENUINE, "real")
    p_toxic = p(EntryQuestion.TOXICITY, "high")
    own_floor = fast.min_p_take_by_kind.get(sig.kind)
    floor = fast.min_p_take if own_floor is None else own_floor
    passes = (p_take or 0.0) >= floor
    if resp.answer(EntryQuestion.GENUINE.value) is not None:  # 旧 6 道题：真 / 毒门槛也看
        passes = (
            passes
            and (p_real or 0.0) >= fast.min_p_genuine
            and (p_toxic if p_toxic is not None else 1.0) <= fast.max_p_toxic
        )
    return outcomes.Candidate(
        signal_id=sig.signal_id,
        symbol=sig.symbol,
        side=sig.side_hint.value,
        ts=sig.ts,
        entry=sig.trigger_price,
        hold_min=hold_min,
        template=TEMPLATE_VERSION,
        verdict=sig.kind.value,  # 回放里没有风控员结论，这里放信号类型
        gate_would_block=not passes,
        stop_bps=stop_bps,
        tp_bps=stop_bps * capped_tp_r(cs.take_profit_r.min, fast.tp_r_cap),
        stop_src=f"jev:{tier}",
        p_take=p_take,
        p_real=p_real,
        p_toxic=p_toxic,
        template_fp=template_fingerprint(),
        model=resp.model_version,
        sig=sig.kind.value,
        take_floor_kind=sig.kind.value if own_floor is not None else None,
    )


async def run(
    *,
    data_dir: str | Path,
    model: DecisionModel,
    fast: FastLoopConfig,
    symbols: list[tuple[str, Category]],
    days: list[date],
    n: int,
    fee_bps: float,
    dedup_s: int,
    concurrency: int,
    fetch: KlineRange,
    now: datetime,
    cache: Path = DEFAULT_CACHE,
    seed: int = 0,
    echo: Callable[[str], None] = print,
    live_timeout_ms: int | None = None,
    hold: str | None = None,
) -> dict[str, Any]:
    """``live_timeout_ms``：实盘的 Jev 预算，报告里算「按实盘预算会超时几成」。"""
    hold_min = max(fast.time_limit_s // 60, 1)
    found: list[Found] = []
    for sym, cat in symbols:
        got = await asyncio.to_thread(_scan_all, sym, cat, days, fast, dedup_s, cache)
        echo(f"{sym}：{len(got)} 个信号")
        found.extend(got)
    # 窗口要在已收盘的行情里走完
    found = [f for f in found if f.signal.ts + timedelta(minutes=hold_min) <= now]
    picks = pick(found, n, seed)
    echo(
        f"共 {len(found)} 个信号，抽 {len(picks)} 个问 Jev"
        f"（模板 {TEMPLATE_VERSION}，持仓 {hold_min} 分钟）"
    )
    answered = await ask_all(model, picks, hold_min, concurrency)

    out = Path(data_dir) / REPLAY_DIR / now.strftime("%Y%m%dT%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    failed = cost = 0.0
    calls_n = {"ok": 0, "timeout": 0, "error": 0}
    latencies: list[float] = []
    with (out / "calls.jsonl").open("w", encoding="utf-8") as calls:
        for f, req, resp in answered:
            cost += resp.cost_usd or 0.0
            if resp.ok:
                calls_n["ok"] += 1
                latencies.append(resp.latency_ms)
            else:
                calls_n["timeout" if "超时" in (resp.error or "") else "error"] += 1
            calls.write(
                json.dumps(
                    {
                        "signal_id": f.signal.signal_id,
                        "kind": f.signal.kind.value,
                        "snapshot": req.snapshot,
                        "ok": resp.ok,
                        "error": resp.error,
                        "answers": {a.id: a.probs for a in resp.answers} if resp.ok else {},
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            if not resp.ok:
                continue  # 超时 / 出错的不进统计，只记在 calls.jsonl 和汇总里
            c = candidate(f, resp, fast, hold_min)
            if c is None:
                failed += 1
                continue
            try:
                bars = await fetch(
                    c.symbol, c.ts - timedelta(minutes=1), c.ts + timedelta(minutes=hold_min)
                )
            except RateLimited:  # 被币安限流：不逐笔刷屏，命令行最后统一说一句
                failed += 1
                continue
            except Exception as e:
                log.warning("%s 拉结果 K 线失败：%s", c.symbol, e)
                failed += 1
                continue
            res = outcomes.simulate(c, bars, fee_bps)
            if res is None:
                failed += 1
                continue
            rows.append(asdict(res))
    with (out / "outcomes.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    own = [k.value for k in fast.min_p_take_by_kind]
    rep = outcomes.report(rows, own)
    passed = [r for r in rows if not r["gate_would_block"]]
    meta: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "days": [d.isoformat() for d in days],
        "symbols": [s for s, _ in symbols],
        "hold_min": hold_min,
        "hold": hold,
        "grid": grid(outcomes.global_floor_rows(rows, own)),
        "signals_found": len(found),
        "asked": len(answered),
        "jev_calls": calls_n,
        "latency_ms": _latency(latencies, live_timeout_ms),
        "failed": int(failed),
        "jev_cost_usd": round(cost, 4),
        "fee_bps": fee_bps,
        "current_thresholds": {
            "min_p_take": fast.min_p_take,
            "min_p_genuine": fast.min_p_genuine,
            "max_p_toxic": fast.max_p_toxic,
            "would_pass": len(passed),
            "would_pass_stats": outcomes.stats(passed),
        },
        "by_kind": {
            k: outcomes.stats([r for r in rows if r["verdict"] == k])
            for k in sorted({r["verdict"] for r in rows})
        },
        "missing": MISSING,
    }
    (out / "report.json").write_text(
        json.dumps({**meta, "templates": rep}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 每次的结果另存一份 data/replay/jev/<时间>.json，跑多个持仓档 / 天数时方便对比
    (out.parent / f"{out.name}.json").write_text(
        json.dumps({**meta, "dir": str(out), "templates": rep}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    path = Path(data_dir) / REPORT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({**meta, "dir": str(out), "templates": rep}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {**meta, "dir": str(out), "templates": rep}


_GATE_KEYS = {"min_p_genuine", "min_p_take", "min_p_direction", "max_p_toxic"}


def hold_tier(text: str) -> str:
    """``4h`` / ``240``（分钟）→ 持仓档名；不认识就抛 ValueError（列出可选的）。"""
    t = text.strip().lower()
    if t in profiles.HOLD:
        return t
    if t.isdigit():
        for name, sections in profiles.HOLD.items():
            if sections["fast_loop"]["time_limit_s"] == int(t) * 60:
                return name
    raise ValueError(f"不认识的持仓档 {text!r}，可选：{' / '.join(profiles.HOLD)}（或对应分钟数）")


def fast_for_hold(fast: FastLoopConfig, hold: str) -> tuple[FastLoopConfig, int]:
    """把这个持仓档的止损 / 止盈 / ATR 周期等盖到现在的配置上（拍板门槛不动），附它的去重冷却。"""
    tier = profiles.HOLD[hold]
    update = {k: v for k, v in tier["fast_loop"].items() if k not in _GATE_KEYS}
    dedup = int(tier.get("app", {}).get("signal_dedup_s", 30))
    return FastLoopConfig.model_validate({**fast.model_dump(), **update}), dedup


GRID_MIN_N = 30
TAKE_GRID = [round(0.05 * i, 2) for i in range(0, 19)]  # 做 ≥ 0–0.9
TOXIC_GRID = [round(0.3 + 0.05 * i, 2) for i in range(0, 15)]  # 毒 ≤ 0.3–1.0


def grid(rows: list[dict[str, Any]], min_n: int = GRID_MIN_N) -> dict[str, Any]:
    """做概率 × 毒性两维一起扫：每组门槛下放行的样本至少 ``min_n`` 笔，取总 R 最大的；附前 5 名。"""
    cells: list[dict[str, Any]] = []
    for take in TAKE_GRID:
        for tox in TOXIC_GRID:
            sel = [
                r
                for r in rows
                if (r.get("p_take") or 0.0) >= take
                and (r["p_toxic"] if r.get("p_toxic") is not None else 1.0) <= tox
            ]
            st = outcomes.stats(sel)
            if st["n"] >= min_n and st["sum_r"] is not None:
                cells.append({"min_p_take": take, "max_p_toxic": tox, **st})
    cells.sort(key=lambda c: (-c["sum_r"], -c["n"]))
    return {"min_n": min_n, "best": cells[0] if cells else None, "top": cells[:5]}


def _grid_lines(result: dict[str, Any]) -> list[str]:
    g = result.get("grid") or {}
    if not g.get("top"):
        return [f"二维推荐（做 × 毒）：没有一组门槛放行够 {g.get('min_n', GRID_MIN_N)} 笔"]
    out = [f"二维推荐（做 × 毒，至少 {g['min_n']} 笔，按总 R 排）："]
    for c in g["top"]:
        out.append(
            f"   做 ≥ {c['min_p_take']:.2f} 且 毒 ≤ {c['max_p_toxic']:.2f}："
            f"{outcomes.fmt(c)}，总 {c['sum_r']:+.1f}R"
        )
    return out


def _latency(ms: list[float], budget_ms: int | None) -> dict[str, Any]:
    """成功调用的延迟：p50 / p95 / 最大，以及按实盘预算会超时的比例。"""
    if not ms:
        return {"n": 0}
    xs = sorted(ms)

    def q(p: float) -> float:
        return round(xs[min(len(xs) - 1, int(p * len(xs)))], 1)

    out: dict[str, Any] = {"n": len(xs), "p50": q(0.5), "p95": q(0.95), "max": round(xs[-1], 1)}
    if budget_ms is not None:
        out["live_budget_ms"] = budget_ms
        out["over_live_budget"] = round(sum(1 for x in xs if x > budget_ms) / len(xs), 3)
    return out


def _calls_line(result: dict[str, Any]) -> str:
    c = result.get("jev_calls") or {}
    lat = result.get("latency_ms") or {}
    line = f"Jev 成功 {c.get('ok', 0)} / 超时 {c.get('timeout', 0)} / 出错 {c.get('error', 0)}"
    if lat.get("n"):
        line += f"；延迟 p50 {lat['p50']:.0f}ms、p95 {lat['p95']:.0f}ms、最大 {lat['max']:.0f}ms"
        if "over_live_budget" in lat:
            line += f"；按实盘预算 {lat['live_budget_ms']}ms 会超时 {lat['over_live_budget']:.0%}"
    return line


def render(result: dict[str, Any]) -> list[str]:
    th = result["current_thresholds"]
    tier = (
        f"持仓档 {result['hold']}（{result['hold_min']} 分钟）"
        if result.get("hold")
        else (f"持仓 {result['hold_min']} 分钟")
    )
    lines = [
        f"回放 {result['days'][0]}–{result['days'][-1]}（{len(result['days'])} 天），"
        f"{len(result['symbols'])} 个币，{tier}：找到 {result['signals_found']} 个信号，"
        f"问了 {result['asked']} 个，"
        f"没算出结果 {result['failed']} 个，Jev 花费 ${result['jev_cost_usd']:.4f}",
        _calls_line(result),
        f"注意：{MISSING}",
        f"按现在门槛（做 ≥ {th['min_p_take']}、真 ≥ {th['min_p_genuine']}、"
        f"毒 ≤ {th['max_p_toxic']}）"
        f"会放行：{outcomes.fmt(th['would_pass_stats'])}",
        "按信号类型：",
    ]
    for k, st in result["by_kind"].items():
        lines.append(f"   {k}：{outcomes.fmt(st)}")
    lines.extend(_grid_lines(result))
    lines.extend(outcomes.render(result["templates"]))
    lines.append(f"明细：{result['dir']}")
    return lines
