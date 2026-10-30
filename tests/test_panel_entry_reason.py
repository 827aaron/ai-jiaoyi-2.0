"""持仓 / 历史展开第一行的开仓理由：信号、开仓时卡上的方向和那场会、拍板概率、风控仓位档。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from jiaoyi import contracts as c
from jiaoyi.core.store import JsonlDecisionLog
from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.overview import build_overview
from jiaoyi.panel.pages import build_history
from tests.test_panel import HB, NOW, RISK, _card_cs, _cfg, _position, _rec

T = lambda h, m=0: datetime(2026, 10, 22, h, m, tzinfo=UTC)  # noqa: E731


def _seed(tmp_path: Path) -> None:
    held = _position(position_id="open", symbol="ETHUSDT", side="short", signal_id="sig-e",
                     signal_kind="volume_surge", opened_at="2026-10-22T05:10:00Z")  # fmt: skip
    shut = _position(position_id="shut", symbol="SOLUSDT", signal_id="sig-s",
                     opened_at="2026-10-22T03:00:00Z", status="closed", close_reason="stop_loss",
                     closed_at="2026-10-22T04:00:00Z", realized_pnl_usdt=-3.0,
                     exit_price=67900.0)  # fmt: skip
    (tmp_path / "positions.json").write_text(
        json.dumps({"positions": {"open": held, "shut": shut}})
    )
    log = JsonlDecisionLog(tmp_path)
    for r in [
        _rec(c.Role.COMMANDER, c.Outcome.ACTION, "ETH 开会", T(4), symbol="ETHUSDT",
             output=_card_cs("ETHUSDT", "short", "跌破支撑，量能放大")),
        # 会判观望、按设置不翻转：卡上仍是 04:00 那场的做空和理由
        _rec(c.Role.COMMANDER, c.Outcome.ACTION, "ETH 开会", T(4, 30), symbol="ETHUSDT",
             output=_card_cs("ETHUSDT", "no_trade", "看不清")),
        _rec(c.Role.COMMANDER, c.Outcome.INFO, "ETH 会判不做，按设置不翻转", T(4, 31),
             symbol="ETHUSDT", output={"card_flip": "ignored"}),
        _rec(c.Role.GATE, c.Outcome.INFO, "ETH 空：做 take 0.68", T(5, 9), symbol="ETHUSDT",
             signal_id="sig-e", output={"jev": {"answers": {"take": {"take": 0.68}}}}),
        _rec(c.Role.RISK_GUARD, c.Outcome.PASS, "ETH 空 放行", T(5, 9), symbol="ETHUSDT",
             signal_id="sig-e", output={"detail": {"size_tier": "M"}}),
        # 开仓之后的会不算开仓理由
        _rec(c.Role.COMMANDER, c.Outcome.ACTION, "ETH 开会", T(6), symbol="ETHUSDT",
             output=_card_cs("ETHUSDT", "long", "反弹")),
        _rec(c.Role.SENTINEL, c.Outcome.ACTION, "SOL 放量突破", T(2, 59), symbol="SOLUSDT",
             signal_id="sig-s", output={"kind": "volume_breakout"}),
        _rec(c.Role.RISK_GUARD, c.Outcome.REJECT, "SOL 拒绝", T(2, 58), symbol="SOLUSDT",
             signal_id="sig-s", output={"detail": {"size_tier": "L"}}),
    ]:  # fmt: skip
        log.write(r)


def test_position_entry_reason_uses_card_in_force_at_open(tmp_path: Path) -> None:
    _seed(tmp_path)
    ov = build_overview(PanelFiles(tmp_path, RISK.day_reset_tz), HB, RISK, _cfg(tmp_path), NOW)
    e = next(p for p in ov.positions if p.position_id == "open").entry
    assert e is not None
    assert e.model_dump() == {
        "signal_kind": "volume_surge", "side": "short", "card": "short", "card_at": T(4),
        "card_reason": "跌破支撑，量能放大", "jev_take": 0.68, "size_tier": "M",
    }  # fmt: skip


def test_history_entry_reason_without_card_or_pass(tmp_path: Path) -> None:
    """开仓前没有这个币的会、风控员只有拒绝记录：方向和仓位档都不写，信号类型从哨兵记录补。"""
    _seed(tmp_path)
    page = build_history(PanelFiles(tmp_path, RISK.day_reset_tz), RISK, NOW, "today",
                         include_tests=True)  # fmt: skip
    e = next(t for t in page.rows if t.position_id == "shut").entry
    assert e is not None
    assert e.model_dump() == {
        "signal_kind": "volume_breakout", "side": "long", "card": None, "card_at": None,
        "card_reason": "", "jev_take": None, "size_tier": None,
    }  # fmt: skip
