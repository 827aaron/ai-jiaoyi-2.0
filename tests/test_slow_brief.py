from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from jiaoyi.slow.brief import MarketBriefService, describe_trend
from tests.test_slow_commander import FakeEngine, entry, make, pool

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


def test_describe_trend() -> None:
    daily = [100.0 + i for i in range(30)]
    h4 = [100.0 + i for i in range(30)]
    text = describe_trend("BTC", daily, h4)
    assert "129.0" in text and "+0.8%" in text and "20 日均线上方" in text and "RSI 100" in text


def transport(fail_fng: bool = False, fail_klines: bool = False) -> httpx.MockTransport:
    calls: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/fapi/v1/klines":
            if fail_klines:
                return httpx.Response(500)
            return httpx.Response(200, json=[[0, "0", "0", "0", str(100 + i)] for i in range(30)])
        if fail_fng:
            return httpx.Response(500)
        return httpx.Response(
            200,
            json={"data": [{"value": "50", "value_classification": "Neutral"}, {"value": "68"}]},
        )

    t = httpx.MockTransport(h)
    t.calls = calls  # type: ignore[attr-defined]
    return t


async def test_brief_builds_once_per_round_and_is_cached() -> None:
    t = transport()
    svc = MarketBriefService(client=httpx.AsyncClient(transport=t), clock=lambda: NOW)
    text = await svc.refresh()
    assert "BTC" in text and "ETH" in text and "恐慌贪婪指数：50（Neutral）（前一日 68）" in text
    n = len(t.calls)  # type: ignore[attr-defined]
    assert await svc.refresh() == text and len(t.calls) == n  # type: ignore[attr-defined]


async def test_brief_part_failure_is_soft_and_all_failed_keeps_old() -> None:
    clock = [NOW]
    svc = MarketBriefService(
        client=httpx.AsyncClient(transport=transport(fail_fng=True)), clock=lambda: clock[0]
    )
    text = await svc.refresh()
    assert "BTC" in text and "暂不可用" in text and svc.failed_parts == 1

    clock[0] = NOW + timedelta(hours=1)
    svc._c = httpx.AsyncClient(transport=transport(fail_fng=True, fail_klines=True))
    assert await svc.refresh() == text  # 全失败：沿用上一份
    assert svc.failed_parts == 3


async def test_commander_refreshes_brief_once_per_round_and_survives_failure(
    tmp_path: Path, now: datetime
) -> None:
    class Brief:
        def __init__(self, boom: bool) -> None:
            self.calls, self.boom = 0, boom
            self.text = ""

        async def refresh(self) -> str:
            self.calls += 1
            if self.boom:
                raise RuntimeError("down")
            return ""

    ok = Brief(False)
    c, _, _ = make(tmp_path, FakeEngine(["AAUSDT", "BBUSDT"]), now, brief=ok)
    await c.refresh(pool(now, entry("AAUSDT"), entry("BBUSDT")), "scheduled")
    assert ok.calls == 1  # 两个标的的会议共用一次简报

    bad = Brief(True)
    c2, log, _ = make(tmp_path / "b", FakeEngine(["AAUSDT"]), now, brief=bad)
    card = await c2.refresh(pool(now, entry("AAUSDT")), "scheduled")
    assert [s.symbol for s in card.symbols] == ["AAUSDT"]  # 简报挂了会议照开
    assert any("市场简报失败" in r.summary for r in log.read_day(now.date().isoformat()))
