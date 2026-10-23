"""拍板门槛校准：拿影子结果（每个信号「做了会怎样」）扫门槛，给一组推荐值，不自动套用。

样本来源：

- ``data/shadow/outcomes.jsonl``：实盘决策日志里每次拍板的影子结果（``jiaoyi jev-backtest`` /
  本命令先补算）；
- ``data/replay/jev/<时间>/outcomes.jsonl``：``jiaoyi jev-replay`` 用历史行情真问 Jev 的结果
  （``--no-replay`` 不用）。

只用同一个持仓档（``hold_min`` 相同）的样本；模板版本 / 指纹 / 模型不同的概率不能混着比，按
（模板, 指纹, 模型）分组，取样本最多的那组出推荐，其余组只列出来。

扫法：做 ≥ 0–0.9、真信号 ≥ 0–0.9（步长 0.05），毒性 ≤ 0.3–1.0（步长 0.05）。一组门槛算数要同时
满足：放行的 ≥ ``min_above`` 笔，且放行样本平均净 R 的 90% 单边置信下沿 > 0；在这些里取「每天合计
净 R」最大的。总样本 < ``min_samples`` 只出表不给推荐；没有一组能过就写明「过不了成本线」。

成本：影子结果已扣来回吃单费，这里再扣 1bp 不利滑点（按每笔止损距离折成 R）。资金费没算。
结果写 ``data/gate_calibration/<hold>.json``，设置页拍板门槛旁边显示推荐值、样本数和日期。
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from jiaoyi.core import profiles
from jiaoyi.gate import outcomes

CALIBRATION_DIR = Path("gate_calibration")
REPLAY_DIR = Path("replay") / "jev"
MIN_ABOVE = 150
MIN_SAMPLES = 300
SLIPPAGE_BPS = 1.0
Z90 = 1.2816
"""正态 90% 单边分位数。"""
TAKE_GRID = [round(0.05 * i, 2) for i in range(0, 19)]  # 做 / 真信号 ≥ 0–0.9
TOXIC_GRID = [round(0.3 + 0.05 * i, 2) for i in range(0, 15)]  # 毒 ≤ 0.3–1.0
BREAKDOWN = {
    "sig": "信号种类",
    "stop_tier": "止损宽窄",
    "size_tier": "仓位档",
    "exit": "怎么出场",
    "source": "样本来源",
}


def hold_minutes(hold: str) -> int:
    return int(profiles.HOLD[hold]["fast_loop"]["time_limit_s"]) // 60


def _jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def load_rows(data_dir: str | Path, hold_min: int, *, replay: bool = True) -> list[dict[str, Any]]:
    """这个持仓档的影子结果（实盘 + 回放），按（来源, signal_id）去重，带 ``source``。"""
    root = Path(data_dir)
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []

    def add(rows: Iterable[dict[str, Any]], source: str) -> None:
        for r in rows:
            key = (source, str(r.get("signal_id")))
            if r.get("hold_min") != hold_min or r.get("r") is None or key in seen:
                continue
            seen.add(key)
            out.append({**r, "source": source})

    add(outcomes.read_results(root), "live")
    if replay:
        for path in sorted((root / REPLAY_DIR).glob("*/outcomes.jsonl")):
            add(_jsonl(path), "replay")
    return out


def net_r(row: dict[str, Any], slippage_bps: float = SLIPPAGE_BPS) -> float:
    """影子 R 再扣不利滑点：止损距离有记录就按它折，没记录（旧数据）按 净bp ÷ R 倒推。"""
    r = float(row["r"])
    stop = row.get("stop_bps")
    if not stop and r:
        stop = abs(float(row.get("net_bps") or 0.0) / r) or None
    return r - slippage_bps / float(stop) if stop else r


def _group_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(row.get("template")),
        str(row.get("template_fp") or ""),
        str(row.get("model") or ""),
    )


def _days(rows: list[dict[str, Any]]) -> int:
    return max(1, len({str(r.get("ts", ""))[:10] for r in rows}))


def _cell(rs: list[float], days: int) -> dict[str, Any]:
    n = len(rs)
    mean = sum(rs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in rs) / (n - 1)) if n > 1 else 0.0
    return {
        "n": n,
        "avg_r": round(mean, 4),
        "lcb90_r": round(mean - Z90 * sd / math.sqrt(n), 4),
        "sum_r": round(sum(rs), 2),
        "r_per_day": round(sum(rs) / days, 3),
        "win": round(sum(1 for x in rs if x > 0) / n, 3),
    }


def scan(rows: list[dict[str, Any]], min_above: int) -> list[dict[str, Any]]:
    """三维扫门槛，返回所有过线（够笔数、置信下沿 > 0）的组合，按每天净 R 从大到小。"""
    days = _days(rows)
    data = [
        (
            r.get("p_take") or 0.0,
            r.get("p_real") or 0.0,
            r["p_toxic"] if r.get("p_toxic") is not None else 1.0,
            net_r(r),
        )
        for r in rows
    ]
    ok: list[dict[str, Any]] = []
    for take in TAKE_GRID:
        a = [d for d in data if d[0] >= take]
        if len(a) < min_above:
            break  # 门槛越高样本越少
        for real in TAKE_GRID:
            b = [d for d in a if d[1] >= real]
            if len(b) < min_above:
                break
            for tox in TOXIC_GRID:
                c = [d[3] for d in b if d[2] <= tox]
                if len(c) < min_above:
                    continue
                cell = _cell(c, days)
                if cell["lcb90_r"] > 0:
                    ok.append({"p_take": take, "p_genuine": real, "max_p_toxic": tox, **cell})
    ok.sort(key=lambda c: (-c["r_per_day"], -c["n"]))
    return ok


def _breakdown(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for key in BREAKDOWN:
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            groups.setdefault(str(r.get(key) or "未知"), []).append(r)
        out[key] = {k: outcomes.stats(v) for k, v in sorted(groups.items())}
    return out


def calibrate(
    rows: list[dict[str, Any]],
    *,
    hold: str,
    now: datetime,
    min_above: int = MIN_ABOVE,
    min_samples: int = MIN_SAMPLES,
) -> dict[str, Any]:
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(_group_key(r), []).append(r)
    base: dict[str, Any] = {
        "hold": hold,
        "hold_min": hold_minutes(hold),
        "computed_at": now.isoformat(),
        "min_above": min_above,
        "min_samples": min_samples,
        "slippage_bps": SLIPPAGE_BPS,
        "recommended": None,
    }
    if not groups:
        return {
            **base,
            "samples": 0,
            "note": f"{hold} 档还没有影子结果，先跑 jev-backtest 或 jev-replay",
        }
    key, sel = max(groups.items(), key=lambda kv: len(kv[1]))
    top = scan(sel, min_above)
    res: dict[str, Any] = {
        **base,
        "samples": len(sel),
        "sources": {s: sum(1 for r in sel if r["source"] == s) for s in ("live", "replay")},
        "group": {"template": key[0], "template_fp": key[1], "model": key[2]},
        "other_groups": [
            {"template": k[0], "template_fp": k[1], "model": k[2], "n": len(v)}
            for k, v in groups.items()
            if k != key
        ],
        "days": _days(sel),
        "baseline": _cell([net_r(r) for r in sel], _days(sel)),
        "best": top[0] if top else None,
        "top": top[:5],
        "breakdown": _breakdown(sel),
    }
    if len(sel) < min_samples:
        res["note"] = f"样本不足：{len(sel)} 个（至少 {min_samples} 个才给推荐），只出表"
    elif not top:
        res["note"] = (
            f"这个模板过不了成本线：没有一组门槛放行 ≥ {min_above} 笔且平均净 R 的 90% 下沿 > 0，"
            "不建议 testnet / live"
        )
    else:
        best = top[0]
        res["recommended"] = {k: best[k] for k in ("p_take", "p_genuine", "max_p_toxic")}
        res["note"] = (
            f"推荐 做 ≥ {best['p_take']:.2f}、真 ≥ {best['p_genuine']:.2f}、"
            f"毒 ≤ {best['max_p_toxic']:.2f}（不自动套用）"
        )
    return res


def write(data_dir: str | Path, result: dict[str, Any]) -> Path:
    path = Path(data_dir) / CALIBRATION_DIR / f"{result['hold']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _fmt(c: dict[str, Any]) -> str:
    return (
        f"{c['n']} 笔，胜率 {c['win']:.0%}，平均 {c['avg_r']:+.3f}R"
        f"（90% 下沿 {c['lcb90_r']:+.3f}R），每天 {c['r_per_day']:+.2f}R"
    )


def render(result: dict[str, Any]) -> list[str]:
    lines = [f"{result['hold']} 档（{result['hold_min']} 分钟）：{result['samples']} 个样本"]
    if not result["samples"]:
        return [*lines, result["note"]]
    src, g = result["sources"], result["group"]
    lines.append(
        f"   实盘 {src['live']} / 回放 {src['replay']}，{result['days']} 天；"
        f"模板 {g['template']}，模型 {g['model'] or '未记'}"
    )
    for o in result["other_groups"]:
        lines.append(
            f"   另有模板 {o['template']} / 模型 {o['model'] or '未记'} {o['n']} 个（没混进来）"
        )
    lines.append(f"全做：{_fmt(result['baseline'])}（已扣来回手续费 + {SLIPPAGE_BPS:g}bp 滑点）")
    if result["top"]:
        lines.append(
            f"过线的门槛（放行 ≥ {result['min_above']} 笔且 90% 下沿 > 0，按每天净 R 排）："
        )
        for c in result["top"]:
            lines.append(
                f"   做 ≥ {c['p_take']:.2f} 真 ≥ {c['p_genuine']:.2f} 毒 ≤ {c['max_p_toxic']:.2f}："
                + _fmt(c)
            )
    for key, title in BREAKDOWN.items():
        parts = [f"{k} {outcomes.fmt(st)}" for k, st in result["breakdown"][key].items()]
        lines.append(f"按{title}：" + "；".join(parts))
    lines.append(result["note"])
    return lines
