"""模型额度看护：402 识别、余额查询、告警节奏、面板横幅。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from jiaoyi.slow import credits
from jiaoyi.slow.credits import Balance, CreditMonitor, banner, fetch_balance, is_credit_error

T0 = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)


def test_is_credit_error() -> None:
    assert is_credit_error(RuntimeError("Error code: 402 - in_flight_budget_exhausted"))
    assert is_credit_error(RuntimeError("Payment Required"))
    assert is_credit_error(RuntimeError("402 insufficient credits"))
    assert not is_credit_error(RuntimeError("Error code: 429 - rate limit"))
    assert not is_credit_error(RuntimeError("超过 900 秒未完成"))
    assert not is_credit_error(RuntimeError("took 4020 ms"))


def _transport(total: float, used: float, weekly: float = 14.0, remaining: float | None = None):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == "Bearer k"
        if req.url.path.endswith("/credits"):
            return httpx.Response(200, json={"data": {"total_credits": total, "total_usage": used}})
        return httpx.Response(
            200,
            json={
                "data": {"usage_weekly": weekly, "usage_daily": 3.0, "limit_remaining": remaining}
            },
        )

    return httpx.MockTransport(handler)


def test_fetch_balance_reads_credits_and_weekly_average() -> None:
    b = fetch_balance("k", _transport(100.0, 60.0, weekly=14.0))
    assert (b.balance_usd, b.avg_daily_usd) == (40.0, 2.0)
    capped = fetch_balance("k", _transport(100.0, 60.0, remaining=7.5))
    assert capped.balance_usd == 7.5  # key 自己设了上限


def test_low_balance_rule() -> None:
    assert Balance(4.0, 0.0).low  # 低于 5 美元
    assert Balance(30.0, 20.0).low  # 低于日均 × 2
    assert not Balance(30.0, 10.0).low


def _monitor(tmp_path: Path, balances: list[Balance]) -> CreditMonitor:
    it = iter(balances)
    return CreditMonitor(tmp_path, lambda: "k", lambda key: next(it))


def test_low_balance_alerts_once_then_again_after_six_hours(tmp_path: Path) -> None:
    m = _monitor(tmp_path, [Balance(0.055, 17.0)] * 10)
    first = m.tick(T0)
    assert [a[0] for a in first] == ["warn"] and "$0.06" in first[0][2]
    assert m.tick(T0 + timedelta(hours=1)) == []  # 6 小时内不重复
    assert len(m.tick(T0 + timedelta(hours=7))) == 1
    saved = json.loads((tmp_path / credits.CREDITS_FILE).read_text(encoding="utf-8"))
    assert saved["low"] is True and saved["balance_usd"] == 0.055


def test_exhausted_alerts_once_and_clears_when_balance_recovers(tmp_path: Path) -> None:
    m = _monitor(tmp_path, [Balance(50.0, 5.0)] * 5)
    assert m.tick(T0) == []
    m.note_exhausted(T0 + timedelta(minutes=5))
    sent = m.tick(T0 + timedelta(minutes=6))
    assert [a[0] for a in sent] == ["critical"]
    m.note_exhausted(T0 + timedelta(minutes=10))
    assert m.tick(T0 + timedelta(minutes=11)) == []  # 同一件事不刷屏
    raw = json.loads((tmp_path / credits.CREDITS_FILE).read_text(encoding="utf-8"))
    assert banner(raw, T0 + timedelta(minutes=12)) is not None
    m.tick(T0 + timedelta(hours=1, minutes=1))  # 余额够 → 清掉 402 标记
    raw = json.loads((tmp_path / credits.CREDITS_FILE).read_text(encoding="utf-8"))
    assert banner(raw, T0 + timedelta(hours=1, minutes=2)) is None


def test_no_key_or_failed_fetch_is_quiet(tmp_path: Path) -> None:
    assert CreditMonitor(tmp_path, lambda: None).tick(T0) == []

    def boom(key: str) -> Balance:
        raise httpx.ConnectError("x")

    assert CreditMonitor(tmp_path, lambda: "k", boom).tick(T0) == []


@pytest.mark.parametrize(
    ("raw", "text"),
    [
        (None, None),
        ({"exhausted_at": T0.isoformat()}, "402"),
        (
            {"low": True, "balance_usd": 1.5, "avg_daily_usd": 17.0, "checked_at": T0.isoformat()},
            "$1.50",
        ),
        ({"low": True, "balance_usd": 1.5, "avg_daily_usd": 17.0, "checked_at": "bad"}, None),
    ],
)
def test_banner(raw: object, text: str | None) -> None:
    out = banner(raw, T0 + timedelta(minutes=1))
    assert (out is None) if text is None else (out is not None and text in out)
    if isinstance(raw, dict) and raw.get("exhausted_at"):  # 超过 30 分钟不再显示
        assert banner(raw, T0 + timedelta(hours=1)) is None


def test_default_max_tokens_is_capped() -> None:
    from jiaoyi.slow.tradingagents_engine import DEFAULT_MAX_TOKENS, default_max_tokens

    big = [{"id": "a/x", "top_provider": {"max_completion_tokens": 384_000}}]
    small = [{"id": "a/x", "top_provider": {"max_completion_tokens": 8_000}}]
    assert default_max_tokens("openrouter", ["a/x"], lambda: big) == DEFAULT_MAX_TOKENS == 16384
    assert default_max_tokens("openrouter", ["a/x"], lambda: small) == 8_000  # 模型自己更小就用小的
    assert default_max_tokens("deepseek", ["d"], lambda: big) == 8192


def test_custom_floor_and_resend_interval(tmp_path: Path) -> None:
    it = iter([Balance(12.0, 1.0)] * 10)
    m = CreditMonitor(
        tmp_path,
        lambda: "k",
        lambda key: next(it),
        low_floor_usd=20.0,
        resend_after=timedelta(hours=2),
    )
    first = m.tick(T0)
    assert len(first) == 1 and "$20.00" in first[0][2]  # 提醒线 20，余额 12 就推
    assert m.tick(T0 + timedelta(hours=1, minutes=1)) == []
    assert len(m.tick(T0 + timedelta(hours=2, minutes=1))) == 1  # 2 小时后重复
    assert Balance(12.0, 1.0).low is False  # 默认提醒线 5，不触发
