"""拍板回测：每次拍板调用，用当时之后的 1 分钟 K 线算「做了会怎样」，按拍板给的概率分桶看胜率。

不用等新数据：``data/decision_log`` 里每次拍板都记了完整快照（方向、价格、最长持仓）和每个选项的
概率，拿币安公开历史 K 线就能把以前所有信号都算一遍。算法：

- 入场价 = 快照里的 ``px``，方向 = 快照 ``side``；窗口 = 快照 ``hold_min``（q2 以前的快照没有，
  用当前配置的最长持仓）。
- 止损 / 止盈：同一信号保镖算出来了（``detail.stop_pct``，放行的还有 ``take_profit_pct``）就按它；
  没有就按 ``FALLBACK_STOP_ATR`` × 快照 ``atr_bps``、止盈 ``FALLBACK_TP_R`` R 估一个（标 ``est``）。
- 一根 1 分钟线里止损、止盈都碰到按先止损算（保守）；都没碰到按窗口最后一根收盘平。
- 净收益扣来回吃单费（``risk.taker_fee_bps`` × 2），R = 净收益 / 止损距离。

每个信号的结果缓存在 ``data/shadow/outcomes.jsonl``（算过的不重算、不重拉行情），汇总表写
``data/debug/jev_backtest.json``。不同模板版本（问法不同）的概率不能混着比，表按版本分开。
只读日志和公开行情，不下单。命令：``jiaoyi jev-backtest``。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jiaoyi.contracts import DecisionRecord, Kline, Outcome, Role

log = logging.getLogger(__name__)

OUTCOMES_FILE = Path("shadow") / "outcomes.jsonl"
REPORT_FILE = Path("debug") / "jev_backtest.json"
FALLBACK_STOP_ATR = 1.5
"""保镖没算出止损时（拍板拦了、没走到算仓位那步），按快照 atr_bps 的几倍估止损。"""
FALLBACK_TP_R = 1.5
MIN_N = 10
"""推荐门槛时，门槛以上至少要有几笔才算数。"""

KlineRange = Callable[[str, datetime, datetime], Awaitable[list[Kline]]]


@dataclass(frozen=True)
class Candidate:
    signal_id: str
    symbol: str
    side: str  # "long" / "short"
    ts: datetime
    entry: float
    hold_min: int
    template: str
    verdict: str
    """opened = 保镖放行；gate = 拍板拦下；guard = 保镖自己的规则拦下；none = 没有保镖记录。"""
    gate_would_block: bool
    stop_bps: float | None
    tp_bps: float | None
    stop_src: str  # guard / est / none
    p_take: float | None
    p_real: float | None
    p_toxic: float | None


@dataclass(frozen=True)
class Result:
    signal_id: str
    symbol: str
    side: str
    ts: str
    template: str
    verdict: str
    gate_would_block: bool
    p_take: float | None
    p_real: float | None
    p_toxic: float | None
    hold_min: int
    stop_src: str
    exit: str  # stop / tp / time
    gross_bps: float
    net_bps: float
    mfe_bps: float
    mae_bps: float
    r: float | None


def _num(x: Any) -> float | None:
    return float(x) if isinstance(x, int | float) and not isinstance(x, bool) else None


def _records(data_dir: Path, since: datetime) -> Iterator[DecisionRecord]:
    for f in sorted((data_dir / "decision_log").glob("*.jsonl")):
        if f.stem < since.date().isoformat():
            continue
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                rec = DecisionRecord.model_validate_json(line)
                if rec.ts >= since and rec.signal_id is not None:
                    yield rec


def _p(answers: dict[str, Any], q: str, choice: str) -> float | None:
    a = answers.get(q)
    return _num(a.get(choice)) if isinstance(a, dict) else None


def candidates(data_dir: str | Path, since: datetime, default_hold_min: int) -> list[Candidate]:
    """``since`` 以来每一次成功的开仓拍板（带快照和概率），配上同一信号的保镖结论。"""
    gates: list[tuple[DecisionRecord, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    guards: dict[str, DecisionRecord] = {}
    for rec in _records(Path(data_dir), since):
        if rec.role is Role.GATE:
            inp, out = rec.input.get("jev"), rec.output.get("jev")
            if not isinstance(inp, dict) or not isinstance(out, dict):
                continue
            if not inp.get("called") or not out.get("ok") or inp.get("kind") != "entry":
                continue
            try:
                snap = json.loads(str(inp.get("snapshot")))
            except json.JSONDecodeError:
                continue
            if isinstance(snap, dict):
                gates.append((rec, inp, out, snap))
        elif rec.role is Role.RISK_GUARD and rec.outcome in (Outcome.PASS, Outcome.REJECT):
            guards[str(rec.signal_id)] = rec
    res: list[Candidate] = []
    for rec, inp, out, snap in gates:
        sid = str(rec.signal_id)
        entry, side = _num(snap.get("px")), snap.get("side")
        if entry is None or entry <= 0 or side not in ("long", "short"):
            continue
        hold = _num(snap.get("hold_min"))
        hold_min = int(hold) if hold else default_hold_min
        answers = out.get("answers") or {}
        g = guards.get(sid)
        detail: dict[str, Any] = {}
        if g is not None and isinstance(g.output.get("detail"), dict):
            detail = g.output["detail"]
        stop_pct = _num(g.output.get("stop_loss_pct")) if g is not None else None
        stop_pct = stop_pct or _num(detail.get("stop_pct"))
        tp_pct = _num(g.output.get("take_profit_pct")) if g is not None else None
        stop_bps, tp_bps, src = None, None, "none"
        if stop_pct:
            stop_bps, src = stop_pct * 100.0, "guard"
            tp_bps = tp_pct * 100.0 if tp_pct else stop_bps * FALLBACK_TP_R
        elif atr := _num(snap.get("atr_bps")):
            stop_bps, src = atr * FALLBACK_STOP_ATR, "est"
            tp_bps = stop_bps * FALLBACK_TP_R
        if g is None:
            verdict = "none"
        elif g.outcome is Outcome.PASS:
            verdict = "opened"
        else:
            verdict = "gate" if detail.get("by") == "gate" else "guard"
        res.append(
            Candidate(
                signal_id=sid,
                symbol=str(snap.get("sym") or rec.symbol or ""),
                side=str(side),
                ts=rec.ts,
                entry=entry,
                hold_min=hold_min,
                template=str(inp.get("template")),
                verdict=verdict,
                gate_would_block=bool(detail.get("gate_would_block")),
                stop_bps=stop_bps,
                tp_bps=tp_bps,
                stop_src=src,
                p_take=_p(answers, "take", "take"),
                p_real=_p(answers, "genuine", "real"),
                p_toxic=_p(answers, "toxicity", "high"),
            )
        )
    return res


def simulate(c: Candidate, bars: list[Kline], fee_bps: float) -> Result | None:
    """按 1 分钟线走一遍：先看止损 / 止盈，没碰到就按窗口最后一根收盘算。"""
    end = c.ts + timedelta(minutes=c.hold_min)
    # 信号所在那一分钟的高低点可能在信号之前，从下一根整分钟开始算
    path = [b for b in bars if c.ts <= b.open_time < end]
    if not path:
        return None
    sign = 1.0 if c.side == "long" else -1.0

    def bps(px: float) -> float:
        return sign * (px / c.entry - 1.0) * 1e4

    mfe = mae = 0.0
    gross: float | None = None
    how = "time"
    for b in path:
        good, bad = (bps(b.high), bps(b.low)) if sign > 0 else (bps(b.low), bps(b.high))
        mfe, mae = max(mfe, good), min(mae, bad)
        if c.stop_bps is not None and bad <= -c.stop_bps:
            gross, how = -c.stop_bps, "stop"  # 同一根两边都碰到：按先止损算
            break
        if c.tp_bps is not None and good >= c.tp_bps:
            gross, how = c.tp_bps, "tp"
            break
    if gross is None:
        gross = bps(path[-1].close)
    net = gross - 2.0 * fee_bps
    return Result(
        signal_id=c.signal_id,
        symbol=c.symbol,
        side=c.side,
        ts=c.ts.isoformat(),
        template=c.template,
        verdict=c.verdict,
        gate_would_block=c.gate_would_block,
        p_take=c.p_take,
        p_real=c.p_real,
        p_toxic=c.p_toxic,
        hold_min=c.hold_min,
        stop_src=c.stop_src,
        exit=how,
        gross_bps=round(gross, 2),
        net_bps=round(net, 2),
        mfe_bps=round(mfe, 2),
        mae_bps=round(mae, 2),
        r=round(net / c.stop_bps, 3) if c.stop_bps else None,
    )


def read_results(data_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(data_dir) / OUTCOMES_FILE
    if not path.is_file():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


async def backfill(
    data_dir: str | Path,
    *,
    since: datetime,
    now: datetime,
    default_hold_min: int,
    fee_bps: float,
    fetch: KlineRange,
) -> tuple[int, int]:
    """算掉窗口已走完、还没算过的信号，追加进缓存。返回 (新算的, 拉行情失败下次再算的)。"""
    done = {r.get("signal_id") for r in read_results(data_dir)}
    todo = [
        c
        for c in candidates(data_dir, since, default_hold_min)
        if c.signal_id not in done and c.ts + timedelta(minutes=c.hold_min) <= now
    ]
    path = Path(data_dir) / OUTCOMES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    n = failed = 0
    for c in todo:
        try:
            bars = await fetch(
                c.symbol, c.ts - timedelta(minutes=1), c.ts + timedelta(minutes=c.hold_min)
            )
        except Exception as e:
            log.warning("%s 拉行情失败，下次再算：%s", c.symbol, e)
            failed += 1
            continue
        res = simulate(c, bars, fee_bps)
        if res is None:
            failed += 1
            continue
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(res), ensure_ascii=False) + "\n")
        n += 1
    return n, failed


# ---------------------------------------------------------------- 汇总
TAKE_BUCKETS = ((0.0, 0.3), (0.3, 0.4), (0.4, 0.5), (0.5, 0.6), (0.6, 1.01))


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rs = [float(r["r"]) for r in rows if r.get("r") is not None]
    nets = [float(r["net_bps"]) for r in rows]
    return {
        "n": len(rows),
        "win": round(sum(1 for x in nets if x > 0) / len(nets), 3) if nets else None,
        "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
        "sum_r": round(sum(rs), 2) if rs else None,
        "avg_net_bps": round(sum(nets) / len(nets), 1) if nets else None,
    }


def _bucket(p: float | None) -> str:
    if p is None:
        return "无"
    for lo, hi in TAKE_BUCKETS:
        if lo <= p < hi:
            return f"{lo:.1f}–{min(hi, 1.0):.1f}"
    return "无"


def _hi(p: float | None) -> str:
    return "无" if p is None else "高" if p >= 0.5 else "低"


def _best_threshold(rows: list[dict[str, Any]], key: str, above: bool) -> dict[str, Any] | None:
    """扫门槛，挑「门槛内总 R 最大」的那个（至少 MIN_N 笔）。above=True 表示 ≥ 门槛才做。"""
    best: dict[str, Any] | None = None
    for i in range(20, 81, 5):
        t = i / 100
        sel = [r for r in rows if r.get(key) is not None and (r[key] >= t if above else r[key] < t)]
        st = _stats(sel)
        if st["n"] < MIN_N or st["sum_r"] is None:
            continue
        if best is None or st["sum_r"] > best["sum_r"]:
            best = {"threshold": t, **st}
    return best


def report(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """按模板版本分开：做概率分桶 × 真信号 × 毒性（高/低）→ 笔数 / 胜率 / 平均 R，加门槛推荐。"""
    out: dict[str, Any] = {}
    for tpl in sorted({str(r.get("template")) for r in rows}):
        sel = [r for r in rows if str(r.get("template")) == tpl]
        cells: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for r in sel:
            k = (_bucket(r.get("p_take")), _hi(r.get("p_real")), _hi(r.get("p_toxic")))
            cells.setdefault(k, []).append(r)
        by_take: dict[str, list[dict[str, Any]]] = {}
        for r in sel:
            by_take.setdefault(_bucket(r.get("p_take")), []).append(r)
        all_r = [r for r in sel if r.get("r") is not None]
        base = _stats(all_r)
        take = _best_threshold(all_r, "p_take", above=True)
        real = _best_threshold(all_r, "p_real", above=True)
        toxic = _best_threshold(all_r, "p_toxic", above=False)
        out[tpl] = {
            "all": _stats(sel),
            "by_exit": {
                e: _stats([r for r in sel if r.get("exit") == e]) for e in ("stop", "tp", "time")
            },
            "by_take": {k: _stats(v) for k, v in sorted(by_take.items())},
            "table": [
                {"take": k[0], "real": k[1], "toxic": k[2], **_stats(v)}
                for k, v in sorted(cells.items())
            ],
            "recommend": {
                "baseline_sum_r": base["sum_r"],
                "min_p_take": take,
                "min_p_real": real,
                "toxicity_below": toxic,
            },
        }
    return out


def _fmt(st: dict[str, Any]) -> str:
    if not st["n"]:
        return "0 笔"
    r = f"平均 {st['avg_r']:+.2f}R" if st["avg_r"] is not None else "无 R"
    return f"{st['n']} 笔，胜率 {st['win']:.0%}，{r}，平均净 {st['avg_net_bps']:+.1f}bp"


def render(rep: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for tpl, t in rep.items():
        lines.append(f"== 模板 {tpl}：{_fmt(t['all'])}（已扣来回手续费）")
        ex = t["by_exit"]
        lines.append(f"   止损 {ex['stop']['n']} / 止盈 {ex['tp']['n']} / 到时平 {ex['time']['n']}")
        lines.append("   做概率分桶：")
        for k, st in t["by_take"].items():
            lines.append(f"     {k}：{_fmt(st)}")
        lines.append("   做概率 × 真信号 × 毒性：")
        for row in t["table"]:
            lines.append(f"     做 {row['take']} 真 {row['real']} 毒 {row['toxic']}：{_fmt(row)}")
        rec = t["recommend"]
        lines.append(f"   推荐（总 R 最大，至少 {MIN_N} 笔；全做总 R = {rec['baseline_sum_r']}）：")
        for name, key, op in (
            ("做概率", "min_p_take", "≥"),
            ("真信号概率", "min_p_real", "≥"),
            ("毒性", "toxicity_below", "<"),
        ):
            b = rec[key]
            if b is None:
                lines.append(f"     {name}：样本不够，先不改")
            else:
                lines.append(
                    f"     {name} {op} {b['threshold']:.2f}：{_fmt(b)}，总 {b['sum_r']:+.1f}R"
                )
    if not lines:
        lines.append("没有可算的拍板记录（窗口还没走完，或日志里没有拍板调用）")
    return lines


def write_report(data_dir: str | Path, rep: dict[str, Any], meta: dict[str, Any]) -> Path:
    path = Path(data_dir) / REPORT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({**meta, "templates": rep}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path
