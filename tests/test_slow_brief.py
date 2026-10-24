from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from jiaoyi.slow.brief import MarketBriefService, describe_trend
from tests.test_slow_commander import FakeEngine, entry, make, pool

NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)


def bars(n: int, last: float = 129.0, step: float = 1.0, vol: float = 10.0) -> list[list[object]]:
    """n 根线，收盘价每根 +step，最后一根收在 last；高 = 收 + 1，低 = 收 − 1。"""
    out: list[list[object]] = []
    for i in range(n):
        c = last - (n - 1 - i) * step
        out.append([i, str(c), str(c + 1), str(c - 1), str(c), str(vol)])
    return out


def test_describe_trend_short_uses_five_minute_windows_and_background() -> None:
    from jiaoyi.slow.ta_prompts import WORDS

    w = WORDS["short"]
    text = describe_trend(
        "BTC",
        w.brief_main,
        w.brief_bg,
        bars(289, 200.0, 0.1),
        [],
        [],
        bars(21),
        lambda x: f"{x:.1f}",
    )
    assert text.startswith("- BTC price 200.0:")
    assert "1h % change +0.6%" in text  # 12 根 × 0.1 = 1.2
    assert "4h % change +2.5%" in text and "24h % change +16.8%" in text
    assert "position in the 24h range 97% (low 170.3 – high 201.0)" in text
    assert "last-hour volume versus the 24h average 1.0x" in text
    assert (
        "; background: distance from the 20-day MA above (" in text
        and "7-day % change +" in text
        and "RSI" not in text
    )


def test_describe_trend_mid_and_long_show_only_their_fields() -> None:
    from jiaoyi.slow.ta_prompts import WORDS

    mid, long_ = WORDS["mid"], WORDS["long"]
    m = describe_trend(
        "ETH",
        mid.brief_main,
        mid.brief_bg,
        bars(289, 100.0),
        bars(73, 100.0),
        [],
        bars(21),
        lambda x: f"{x:.1f}",
    )
    assert "3-day % change" in m and "position in the 3-day range" in m and "1h RSI 100" in m
    assert "1h % change" not in m and "background: distance from the 20-day MA above" in m
    lg = describe_trend(
        "ETH", long_.brief_main, long_.brief_bg, bars(289), bars(73), bars(30), bars(21)
    )
    assert "4h RSI 100" in lg and "daily RSI 100" in lg and "background" not in lg
    assert "1h % change" not in lg and "7-day % change" in lg


def transport(fail_fng: bool = False, fail_klines: bool = False) -> httpx.MockTransport:
    calls: list[str] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/fapi/v1/klines":
            if fail_klines:
                return httpx.Response(500)
            n = int(req.url.params["limit"])
            return httpx.Response(200, json=bars(n))
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
    svc = MarketBriefService(client=httpx.AsyncClient(transport=t), clock=lambda: NOW, style="mid")
    text = await svc.refresh()
    assert (
        "BTC" in text
        and "ETH" in text
        and "Fear & Greed index: 50 (Neutral) (previous day 68)" in text
    )
    n = len(t.calls)  # type: ignore[attr-defined]
    assert await svc.refresh() == text and len(t.calls) == n  # type: ignore[attr-defined]


async def test_brief_part_failure_is_soft_and_all_failed_keeps_old() -> None:
    clock = [NOW]
    svc = MarketBriefService(
        client=httpx.AsyncClient(transport=transport(fail_fng=True)), clock=lambda: clock[0]
    )
    text = await svc.refresh()
    assert "BTC" in text and "unavailable" in text and svc.failed_parts == 1

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
