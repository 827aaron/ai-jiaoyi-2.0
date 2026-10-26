#!/usr/bin/env python3
"""只读大盘翻转记录，默认汇总最近 3 天，Markdown 写到 stdout。不联网、不读密钥。"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def read_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("????-??-??.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except ValueError:
                continue
    return rows


def percent(n: int, total: int) -> str:
    return f"{n / total:.1%}（{n}/{total}）" if total else "—（无有效样本）"


def distribution(values: list[float]) -> str:
    if not values:
        return "— | —"
    return f"{statistics.mean(values):+.3f}% | {statistics.median(values):+.3f}%"


def report(rows: list[dict[str, Any]], now: datetime, days: int = 3) -> str:
    start = now - timedelta(days=days)
    events: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[str, Any]] = {}
    outcomes: dict[tuple[str, int], dict[str, Any]] = {}
    for row in rows:
        try:
            ts = datetime.fromisoformat(row["ts"])
            if ts.tzinfo is None or ts > now:
                continue
            kind, eid = row["kind"], row["event_id"]
            if kind == "rerun_result":
                results[eid] = row
            elif kind == "outcome":
                outcomes[eid, int(row["horizon_h"])] = row
            elif ts >= start:
                events[eid] = row
        except (ValueError, TypeError, KeyError):
            continue
    daily: dict[str, Counter[str]] = {}
    blocked: Counter[str] = Counter()
    daily_blocked: Counter[tuple[str, str]] = Counter()
    confirmed: dict[str, dict[str, Any]] = {}
    for eid, row in events.items():
        day = datetime.fromisoformat(row["ts"]).astimezone(UTC).date().isoformat()
        daily.setdefault(day, Counter())[row["kind"]] += 1
        if row["kind"] == "blocked":
            reason = row.get("blocked_reason") or "未注明"
            reason = reason.split("：")[0].split(" ")[0]
            blocked[reason] += 1
            daily_blocked[day, reason] += 1
        elif row["kind"] == "confirmed":
            confirmed[eid] = row
    lines = [
        f"# 大盘翻转最近 {days} 天汇总",
        "",
        f"时间范围（UTC）：{start.isoformat()} 至 {now.isoformat()}",
        "",
        "| UTC 日期 | 仅池面疑似 | 仅 BTC 疑似 | 确认 | 被拦 |",
        "|---|---:|---:|---:|---:|",
    ]
    for day, counts in sorted(daily.items()):
        lines.append(
            f"| {day} | {counts['suspect_breadth']} | {counts['suspect_btc']} | "
            f"{counts['confirmed']} | {counts['blocked']} |"
        )
    if not daily:
        lines.append("| 无记录 | 0 | 0 | 0 | 0 |")
    lines += ["", "被拦原因："]
    lines += [f"- {reason}：{n} 次" for reason, n in sorted(blocked.items())] or ["- 无"]
    if daily_blocked:
        lines += ["", "分日被拦原因："]
        lines += [
            f"- {day} / {reason}：{n} 次" for (day, reason), n in sorted(daily_blocked.items())
        ]
    lines += [
        "",
        "| 确认后 | 有效样本 | 涨跌均值 | 涨跌中位数 | 延续率 | 误报率 | 持平 |",
        "|---|---:|---:|---:|---|---|---:|",
    ]
    for hours in (1, 2):
        for name, field in (("池面", "pool_median"), ("BTC", "btc")):
            values: list[float] = []
            continued = reversed_ = flat = 0
            for eid, row in confirmed.items():
                outcome = outcomes.get((eid, hours), {})
                raw = outcome.get(f"fwd_{hours}h_{field}_pct")
                direction = row.get("candidate_direction")
                if direction not in ("看涨", "看跌"):
                    old = row.get("regime_dir")
                    direction = "看涨" if old == "看跌" else "看跌" if old == "看涨" else None
                if not isinstance(raw, (int, float)) or direction not in ("看涨", "看跌"):
                    continue
                value = float(raw)
                values.append(value)
                signed = value if direction == "看涨" else -value
                continued += signed > 0
                reversed_ += signed < 0
                flat += signed == 0
            lines.append(
                f"| {hours}h {name} | {len(values)} | {distribution(values)} | "
                f"{percent(continued, len(values))} | {percent(reversed_, len(values))} | {flat} |"
            )
    finished = [results[eid] for eid in confirmed if eid in results]
    judged = [r for r in finished if isinstance(r.get("regime_changed"), bool)]
    changed = sum(r["regime_changed"] for r in judged)
    cost = sum(float(r.get("cost_usd") or 0) for r in finished)
    failed = sum(r.get("result") == "flip_failed" for r in finished)
    lines += [
        "",
        f"重跑后方向改变：{percent(changed, len(judged))}；"
        f"失败 {failed} 次，尚无结果 {len(confirmed) - len(finished)} 次。",
        f"市场更新费用合计（含历史整轮、失败费用及账本估算）：${cost:.6f}。未回报的费用不计为零成本成功。",
        "",
        "口径：延续率 = 朝预期翻转方向继续走的样本比例；误报率 = 向相反方向走的比例；"
        "持平单列。缺失、尚未到期的结果不进分母。方向不变率与价格误报率分别统计。",
        "池面结果固定使用事件发生时的币及价格，计算各币未来收益的中位数；"
        "个别币缺线时只计算有数据的币，覆盖数见 outcome 的 pool_observed / pool_expected。",
        "疑似次数按独立观测文件统计：连续同类默认仅首次和每满 30 分钟记录，"
        "间隔可热更新，0 表示每次都记；"
        "决策日志与面板仍保留每次检查发现的疑似、确认和被拦。",
        "默认值无数据支持；这些是观测统计，不是盈利证明。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data/regime_flip"))
    parser.add_argument("--days", type=int, default=3)
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days 至少为 1")
    print(report(read_rows(args.data_dir), datetime.now(UTC), args.days), end="")


if __name__ == "__main__":
    main()
