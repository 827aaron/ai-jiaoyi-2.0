"""Reddit 闸：缓存、最小间隔、失败冷却、关闭。"""

from __future__ import annotations

from jiaoyi.slow.social_guard import RedditGuard

BAD = "<Reddit unavailable: fetch failed (429); this is not an absence of discussion>"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _mk(results: list[str], **kw: float | str) -> tuple[RedditGuard, list[str], _Clock]:
    calls: list[str] = []
    it = iter(results)
    clock = _Clock()

    def fetch(ticker: str, subs: object = None, **_: object) -> str:
        calls.append(ticker)
        return next(it)

    return RedditGuard(fetch, clock=clock, **kw), calls, clock  # type: ignore[arg-type]


def test_cache_hit_skips_request() -> None:
    g, calls, clock = _mk(["post"])
    assert g("SOL") == "post"
    clock.t += 100
    assert g("SOL") == "post"
    assert calls == ["SOL"]


def test_min_gap_returns_placeholder_without_waiting() -> None:
    g, calls, clock = _mk(["a", "b"])
    g("SOL")
    clock.t += 10
    assert g("ETH").startswith("<Reddit unavailable")
    clock.t += 60
    assert g("ETH") == "b"
    assert calls == ["SOL", "ETH"]


def test_failure_triggers_cooldown() -> None:
    g, calls, clock = _mk([BAD, "ok"], cooldown_s=600)
    assert g("SOL") == BAD
    clock.t += 100
    assert "rate-limited" in g("SOL")
    clock.t += 600
    assert g("SOL") == "ok"
    assert calls == ["SOL", "SOL"]


def test_off_never_fetches() -> None:
    g, calls, _ = _mk([], mode="off")
    assert g("SOL").startswith("<Reddit unavailable")
    assert calls == []
