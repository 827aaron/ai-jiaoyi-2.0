"""回放汇总：触发数、成交数、胜率、盈亏、拒单原因分布。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from pydantic import BaseModel, Field

from jiaoyi.backtest.sim import ClosedTrade


class ReplayReport(BaseModel):
    initial_equity: float
    final_equity: float
    triggers: int = 0
    jev_calls: int = 0
    jev_failed: int = 0
    jev_take: int = Field(0, description="决策模型回答“做”的次数")
    risk_approved: int = 0
    risk_rejected: int = 0
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    filled: int = 0
    unfilled: int = 0
    closed: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float | None = None
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    net_pnl_per_10k: float = 0.0
    max_drawdown: float = 0.0
    exits_by_reason: dict[str, int] = Field(default_factory=dict)
    by_signal_kind: dict[str, dict[str, float]] = Field(default_factory=dict)
    day_resets: int = 0
    avg_jev_latency_ms: float = 0.0

    def render_text(self) -> str:
        wr = "—" if self.win_rate is None else f"{self.win_rate * 100:.0f}%"
        lines = [
            "回放汇总",
            f"  触发 {self.triggers}，问决策模型 {self.jev_calls} 次（失败 {self.jev_failed}，"
            f"回答“做”{self.jev_take}）",
            f"  风控员放行 {self.risk_approved}，拒绝 {self.risk_rejected}",
            f"  成交 {self.filled}，已平 {self.closed}，未成交 {self.unfilled}",
            f"  胜率 {wr}（{self.wins} 胜 {self.losses} 负）",
            f"  毛盈亏 {self.gross_pnl:+.2f} U，手续费 {self.fees:.2f} U，"
            f"净盈亏 {self.net_pnl:+.2f} U"
            f"（折合每 1 万 U {self.net_pnl_per_10k:+.2f} U）",
            f"  最大回撤 {self.max_drawdown:.2f} U，期末权益 {self.final_equity:.2f} U",
        ]
        if self.exits_by_reason:
            lines.append(
                "  退出原因：" + "，".join(f"{k} {v}" for k, v in self.exits_by_reason.items())
            )
        if self.rejection_reasons:
            lines.append("  拒单原因（多到少）：")
            lines += [f"    {n:>4}  {r}" for r, n in self.rejection_reasons.items()]
        if self.by_signal_kind:
            lines.append("  按信号类型：")
            for k, v in self.by_signal_kind.items():
                lines.append(f"    {k}: 成交 {int(v['trades'])}，净盈亏 {v['net']:+.2f} U")
        return "\n".join(lines)


def summarize(
    trades: Sequence[ClosedTrade],
    kinds: dict[str, str],
    reasons: Counter[str],
    initial_equity: float,
    final_equity: float,
) -> dict[str, object]:
    """从已平仓列表算出成交相关的汇总字段。"""
    wins = sum(1 for t in trades if t.net_pnl > 0)
    losses = sum(1 for t in trades if t.net_pnl <= 0)
    gross = sum(t.gross_pnl for t in trades)
    fees = sum(t.fees for t in trades)
    net = gross - fees
    peak = dd = cum = 0.0
    for t in sorted(trades, key=lambda x: x.exit_ts):
        cum += t.net_pnl
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    by_kind: dict[str, dict[str, float]] = {}
    for t in trades:
        k = kinds.get(t.signal_id, "unknown")
        d = by_kind.setdefault(k, {"trades": 0.0, "net": 0.0})
        d["trades"] += 1
        d["net"] += t.net_pnl
    return {
        "closed": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(trades) if trades else None,
        "gross_pnl": gross,
        "fees": fees,
        "net_pnl": net,
        "net_pnl_per_10k": net * 10_000.0 / initial_equity,
        "max_drawdown": dd,
        "exits_by_reason": dict(Counter(t.reason for t in trades).most_common()),
        "rejection_reasons": dict(reasons.most_common()),
        "by_signal_kind": by_kind,
        "final_equity": final_equity,
    }
