"""记分共用的两样：t 时刻生效的币池、按时段聚类的 bootstrap 区间。

作战卡方向记分（``slow/card_score.py``）在用；之后的总盘记分也用这一份，口径不分叉。
"""

from __future__ import annotations

import json
import random
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

BOOT_B = 2000
BOOT_SEED = 7


def parse_ts(v: object) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    return datetime.fromisoformat(v).astimezone(UTC)


@dataclass(frozen=True)
class Pool:
    pool_id: str | None
    symbols: tuple[str, ...]


class PoolTimeline:
    """t 时刻生效的币池：``market_meeting`` 每轮记下的池，取 t 之前最近一轮。
    最早一轮之前没有记录，返回 None（不拿后来的池顶替）。"""

    def __init__(self, entries: Sequence[tuple[datetime, Pool]]) -> None:
        self._entries = sorted(entries, key=lambda e: e[0])
        self._keys = [t for t, _ in self._entries]

    @classmethod
    def load(cls, data_dir: str | Path) -> PoolTimeline:
        out: list[tuple[datetime, Pool]] = []
        for path in sorted((Path(data_dir) / "market_meeting").glob("????-??-??.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in lines:
                try:
                    r = json.loads(line)
                    t = parse_ts(r.get("ts"))
                    syms = tuple(p["symbol"] for p in r.get("pool") or [])
                except (ValueError, KeyError, TypeError):
                    continue
                if t is not None and syms:
                    out.append((t, Pool(r.get("pool_id"), syms)))
        return cls(out)

    @property
    def first(self) -> datetime | None:
        return self._keys[0] if self._keys else None

    def at(self, t: datetime) -> Pool | None:
        i = bisect_right(self._keys, t) - 1
        return self._entries[i][1] if i >= 0 else None


@lru_cache(maxsize=2)
def _boot_draws(k: int, b: int, seed: int) -> tuple[tuple[int, ...], ...]:
    """同样的时段数、次数、种子，抽到的时段下标完全一样（命中和均值共用一套抽样）。
    只缓存最近两种时段数：一次汇总里总体那几项共用，分组的各算各的。"""
    rng = random.Random(seed)
    return tuple(tuple(rng.randrange(k) for _ in range(k)) for _ in range(b))


def cluster_ci(
    values: Sequence[float], blocks: Sequence[str], *, b: int = BOOT_B, seed: int = BOOT_SEED
) -> tuple[float | None, float | None, int]:
    """按时段整块有放回重抽 ``b`` 次，取均值的 2.5% / 97.5% 分位。时段少于 3 个不给区间。
    返回 (下限, 上限, 时段数)。"""
    sums: dict[str, float] = {}
    cnts: dict[str, int] = {}
    for v, k in zip(values, blocks, strict=True):
        sums[k] = sums.get(k, 0.0) + v
        cnts[k] = cnts.get(k, 0) + 1
    keys = list(sums)
    if len(keys) < 3:
        return None, None, len(keys)
    s = [sums[k] for k in keys]
    c = [cnts[k] for k in keys]
    sims = sorted(
        sum(s[i] for i in draw) / sum(c[i] for i in draw)
        for draw in _boot_draws(len(keys), b, seed)
    )
    return sims[int(0.025 * b)], sims[int(0.975 * b)], len(keys)
