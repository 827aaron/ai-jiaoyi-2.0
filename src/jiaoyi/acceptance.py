"""24 小时纸面验收：读决策日志和模块状态，逐条核对“该有的有、不该有的没有”。

用法（在 Mac mini 上，系统跑满 24 小时之后）::

    python -m jiaoyi.acceptance --stage m1 --hours 24

只读文件，不连交易所、不连网络。退出码 0 = 全部通过，1 = 有失败项。
清单和每一项的含义见 docs/acceptance-M0-M1.md。

依赖的日志约定（各角色写 ``DecisionRecord`` 时遵守，回放框架写的也是这一套）：

- 保镖放行：``role=risk_guard, outcome=pass``，``output`` 是 ``OrderIntent`` 的 JSON，
  带 ``signal_id``。
- 拍板人：``role=gate``，Jev 失败时 ``outcome=error``，带 ``signal_id``。
- 开仓成交：``role=executor, outcome=action``，带 ``signal_id`` 和 ``position_id``。
- 平仓：``role=executor, outcome=close``，带 ``position_id``，``output.net`` 是净盈亏。
- 保命止损：``role=lifeboat, outcome=action`` 是挂上，带 ``position_id``。
- 对账员发现且没修好的问题：``role=reconciler, outcome=error``。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jiaoyi.contracts import DecisionRecord, ModuleStatus, RiskConfig, Role
from jiaoyi.contracts.decision_log import Outcome
from jiaoyi.contracts.module_status import Health
from jiaoyi.core.config import ConfigError, load_risk_config

STAGE_ROLES: dict[str, tuple[Role, ...]] = {
    "m0": (Role.EXECUTOR, Role.LIFEBOAT, Role.RECONCILER),
    "m1": (
        Role.SENTINEL,
        Role.GATE,
        Role.RISK_GUARD,
        Role.EXECUTOR,
        Role.LIFEBOAT,
        Role.RECONCILER,
    ),
}


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool | None
    """True 通过，False 失败，None 只是提示（不影响退出码）。"""
    detail: str

    def line(self) -> str:
        mark = {True: "✓", False: "✗", None: "·"}[self.ok]
        return f"{mark} {self.name}：{self.detail}"


def read_records(data_dir: Path) -> list[DecisionRecord]:
    out: list[DecisionRecord] = []
    for f in sorted((data_dir / "decision_log").glob("*.jsonl")):
        with f.open(encoding="utf-8") as fh:
            out += [DecisionRecord.model_validate_json(line) for line in fh if line.strip()]
    return sorted(out, key=lambda r: r.ts)


def read_statuses(data_dir: Path) -> dict[str, ModuleStatus]:
    path = data_dir / "module_status.json"
    if not path.is_file():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {k: ModuleStatus.model_validate(v) for k, v in raw.items()}


def _percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(q * len(s)))] if s else 0.0


def audit(
    records: list[DecisionRecord],
    statuses: dict[str, ModuleStatus],
    *,
    stage: str,
    hours: float,
    now: datetime,
    risk: RiskConfig | None = None,
    stale_s: float = 180.0,
    lifeboat_within_s: float = 30.0,
    max_jev_p95_ms: float = 1500.0,
) -> list[Check]:
    checks: list[Check] = []
    add = checks.append

    # 1 运行时长
    if records:
        span = records[-1].ts - records[0].ts
        ok = span >= timedelta(hours=hours) - timedelta(minutes=30)
        add(
            Check(
                "运行时长",
                ok,
                f"日志从 {records[0].ts:%m-%d %H:%M} 到 {records[-1].ts:%m-%d %H:%M} UTC，"
                f"共 {span.total_seconds() / 3600:.1f} 小时（要求 ≥ {hours:g}）",
            )
        )
    else:
        add(Check("运行时长", False, "决策日志是空的"))

    # 2 心跳
    for role in STAGE_ROLES[stage]:
        st = statuses.get(role.value)
        if st is None:
            add(Check(f"心跳 {role.value}", False, "没有上报过状态"))
            continue
        age = (now - st.ts).total_seconds()
        ok = age <= stale_s and st.health is not Health.DOWN
        add(
            Check(
                f"心跳 {role.value}",
                ok,
                f"{st.health.value}，{age:.0f} 秒前；{st.last_error or '无错误'}",
            )
        )

    # 3-5 铁律：放行在先、Jev 失败不开仓、开仓必有保命止损
    passes: dict[str, datetime] = {}
    failed_jev: set[str] = set()
    opens: list[DecisionRecord] = []
    closes: dict[str, DecisionRecord] = {}
    lifeboats: dict[str, datetime] = {}
    for r in records:
        if r.role is Role.RISK_GUARD and r.outcome is Outcome.PASS and r.signal_id:
            passes.setdefault(r.signal_id, r.ts)
        elif r.role is Role.GATE and r.outcome is Outcome.ERROR and r.signal_id:
            failed_jev.add(r.signal_id)
        elif r.role is Role.EXECUTOR and r.outcome is Outcome.ACTION:
            opens.append(r)
        elif r.role is Role.EXECUTOR and r.outcome is Outcome.CLOSE and r.position_id:
            closes[r.position_id] = r
        elif r.role is Role.LIFEBOAT and r.outcome is Outcome.ACTION and r.position_id:
            lifeboats.setdefault(r.position_id, r.ts)

    if stage == "m1":
        no_pass = [r for r in opens if r.signal_id not in passes or passes[r.signal_id] > r.ts]
        add(
            Check(
                "每笔开仓都有保镖放行在先",
                not no_pass,
                f"开仓 {len(opens)} 笔，缺放行记录 {len(no_pass)} 笔" + _ids(no_pass),
            )
        )
        bad = [r for r in opens if r.signal_id in failed_jev]
        add(
            Check(
                "Jev 失败的信号没有开仓",
                not bad,
                f"Jev 失败 {len(failed_jev)} 次，其中开了仓 {len(bad)} 笔" + _ids(bad),
            )
        )
    late = [
        r
        for r in opens
        if r.position_id not in lifeboats
        or (lifeboats[r.position_id] - r.ts).total_seconds() > lifeboat_within_s
    ]
    add(
        Check(
            f"每笔开仓 {lifeboat_within_s:g} 秒内挂上保命止损",
            not late,
            f"开仓 {len(opens)} 笔，没挂或挂晚了 {len(late)} 笔" + _ids(late),
        )
    )

    # 6 指令合规
    if risk is None:
        add(Check("放行指令符合硬上限", None, "没给 --risk，跳过"))
    else:
        bad_i: list[str] = []
        n = 0
        for r in records:
            if r.role is Role.RISK_GUARD and r.outcome is Outcome.PASS:
                n += 1
                bad_i += _intent_problems(r, risk)
        add(
            Check(
                "放行指令符合硬上限",
                not bad_i,
                f"检查 {n} 条放行" + ("" if not bad_i else "；问题：" + "；".join(bad_i[:5])),
            )
        )

    # 7 同时持仓不超上限
    if risk is not None:
        peak = _peak_open(records)
        add(
            Check(
                "同时持仓不超上限",
                peak <= risk.max_open_positions,
                f"峰值 {peak}，上限 {risk.max_open_positions}",
            )
        )

    # 8 对账员
    unresolved = [r for r in records if r.role is Role.RECONCILER and r.outcome is Outcome.ERROR]
    add(
        Check(
            "对账员没有遗留问题", not unresolved, f"未修复 {len(unresolved)} 条" + _ids(unresolved)
        )
    )

    # 9 没有悬空持仓
    dangling = [r for r in opens if r.position_id not in closes]
    if dangling:
        add(
            Check(
                "持仓都已收尾",
                None,
                f"仍有 {len(dangling)} 笔未平（验收时点还在持有属正常，请在面板确认）",
            )
        )
    else:
        add(Check("持仓都已收尾", True, "没有未平仓位"))

    # 10 时延
    lat = [r.latency_ms for r in records if r.role is Role.GATE and r.latency_ms > 0]
    if lat:
        p95 = _percentile(lat, 0.95)
        add(
            Check(
                "Jev 延迟",
                p95 <= max_jev_p95_ms,
                f"{len(lat)} 次，中位 {_percentile(lat, 0.5):.0f}ms，p95 {p95:.0f}ms"
                f"（要求 ≤ {max_jev_p95_ms:g}ms）",
            )
        )

    # 11 统计（提示）
    by_role: Counter[str] = Counter(r.role.value for r in records)
    add(Check("记录数", None, "，".join(f"{k} {v}" for k, v in by_role.most_common())))
    nets = [float(c.output.get("net", 0.0)) for c in closes.values()]
    if nets:
        wins = sum(1 for x in nets if x > 0)
        add(
            Check(
                "成交结果",
                None,
                f"{len(nets)} 笔已平，胜率 {wins / len(nets) * 100:.0f}%，"
                f"净盈亏 {sum(nets):+.2f} U",
            )
        )
    cost = sum(r.cost_usd for r in records)
    add(Check("模型花费", None, f"合计 {cost:.2f} 美元"))
    return checks


def _ids(rs: list[DecisionRecord]) -> str:
    return "" if not rs else "（如 " + "、".join(r.signal_id or r.record_id for r in rs[:3]) + "）"


def _intent_problems(r: DecisionRecord, risk: RiskConfig) -> list[str]:
    """只读检查日志里的放行指令（字段当字典读，不重新构造 OrderIntent）。"""
    o = r.output
    try:
        iid = str(o["intent_id"])
        lev = int(o["leverage"])
        notional = float(o["notional_usdt"])
        stop = float(o["stop_loss_pct"])
        tp = float(o["take_profit_pct"])
        lifeboat = float(o["lifeboat_stop_pct"])
        approved_by = o["approved_by"]
    except (KeyError, TypeError, ValueError):
        return [f"{r.record_id} 不是合法的放行指令"]
    probs = []
    if approved_by != "risk_guard":
        probs.append(f"{iid} 不是保镖签发")
    if lev > risk.max_leverage:
        probs.append(f"{iid} 杠杆 {lev} 超过 {risk.max_leverage}")
    if notional > risk.max_notional_per_trade_usdt + 1e-6:
        probs.append(f"{iid} 名义 {notional:.0f} 超过 {risk.max_notional_per_trade_usdt:.0f}")
    if lifeboat < stop * risk.lifeboat_stop_multiplier * (1 - 1e-6):
        probs.append(f"{iid} 保命止损不够宽")
    if tp * 100 < risk.min_tp_fee_multiple * 2 * risk.taker_fee_bps - 1e-6:
        probs.append(f"{iid} 止盈覆盖不了手续费")
    return probs


def _peak_open(records: list[DecisionRecord]) -> int:
    open_now: set[str] = set()
    peak = 0
    for r in records:
        if r.role is Role.EXECUTOR and r.position_id:
            if r.outcome is Outcome.ACTION:
                open_now.add(r.position_id)
            elif r.outcome is Outcome.CLOSE:
                open_now.discard(r.position_id)
        peak = max(peak, len(open_now))
    return peak


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m jiaoyi.acceptance", description="24 小时纸面验收")
    p.add_argument(
        "--data", default="data", help="data_dir，里面有 decision_log/ 和 module_status.json"
    )
    p.add_argument("--stage", choices=sorted(STAGE_ROLES), default="m1")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--risk", default="config/risk.toml")
    p.add_argument("--stale-s", type=float, default=180.0, help="心跳多久没更新算掉线")
    p.add_argument("--no-heartbeat", action="store_true", help="回放日志没有心跳，跳过心跳检查")
    args = p.parse_args(argv)

    data = Path(args.data)
    risk: RiskConfig | None = None
    try:
        risk = load_risk_config(Path(args.risk))
    except ConfigError as e:
        print(f"（硬上限没读到，相关检查跳过）{e}", file=sys.stderr)

    checks = audit(
        read_records(data),
        {} if args.no_heartbeat else read_statuses(data),
        stage=args.stage,
        hours=args.hours,
        now=datetime.now(UTC),
        risk=risk,
        stale_s=args.stale_s,
    )
    if args.no_heartbeat:
        checks = [c for c in checks if not c.name.startswith("心跳")]
    for c in checks:
        print(c.line())
    failed = [c for c in checks if c.ok is False]
    passed = sum(1 for c in checks if c.ok)
    verdict = "全部通过" if not failed else f"有 {len(failed)} 项失败"
    print(f"\n{verdict}（{passed} 项通过）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
