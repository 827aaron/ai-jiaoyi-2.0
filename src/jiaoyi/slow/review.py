"""作战卡复盘：每张卡落一份流水，事后对照真实走势打分。

只看、不改：这里**不会**自动调任何权重、阈值或提示词，只给 Aaron 一张“军师说的方向后来对不对”的表，
让人决定要不要动。

流程：
1. 出卡时 ``CardJournal.record(card, fresh)`` 往 ``data/card_review/cards.jsonl`` 追加每个标的一行
   （只记这次真的开了会的标的；沿用旧卡的不重复记，免得一个结论被算多次）。
2. 卡过期后跑 ``python -m jiaoyi.slow.review``：按卡的有效期取 5 分钟 K 线，算
   收盘涨跌、最大有利 / 不利波动，再按方向汇总。long/short 才算命中；both / no_trade 只报波动幅度。

这里的命中率是**原始口径**：起点用整轮开始（比卡真正发出早几分钟到二十几分钟）、只看原始涨跌、
没扣大盘，会跟着「大盘恰好顺着卡的多空倾斜走」大起大落，只作对照。看卡的方向到底准不准用
``slow/card_score.py``（``jiaoyi card-score``，面板「历史卡」顶上的方向记分）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from jiaoyi.contracts import BattleCard, Bias

logger = logging.getLogger(__name__)

_BASE = "https://fapi.binance.com"
JOURNAL_NAME = "cards.jsonl"


class JournalRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    card_id: str
    version: int
    created_at: datetime
    expires_at: datetime
    regime: str
    refresh_reason: str
    symbol: str
    category: str
    bias: str


class CardJournal:
    """追加式流水，写失败只记日志（复盘不能拖累出卡）。"""

    def __init__(self, data_dir: str | Path) -> None:
        self._path = Path(data_dir) / "card_review" / JOURNAL_NAME

    @property
    def path(self) -> Path:
        return self._path

    def record(self, card: BattleCard, fresh: Iterable[str]) -> None:
        fresh_set = set(fresh)
        rows = [
            JournalRow(
                card_id=card.card_id,
                version=card.version,
                created_at=s.issued_at or card.created_at,
                expires_at=s.expires_at or card.expires_at,
                regime=card.regime,
                refresh_reason=card.refresh_reason,
                symbol=s.symbol,
                category=s.category.value,
                bias=s.bias.value,
            )
            for s in card.symbols
            if s.symbol in fresh_set
        ]
        if not rows:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as f:
                for r in rows:
                    f.write(r.model_dump_json() + "\n")
        except OSError as e:
            logger.warning("作战卡复盘流水写不进去：%s", e)

    def read(self) -> list[JournalRow]:
        if not self._path.is_file():
            return []
        rows: list[JournalRow] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(JournalRow.model_validate_json(line))
            except ValidationError:
                logger.warning("跳过读不懂的复盘流水行：%.80s", line)
        return rows


@dataclass(frozen=True)
class Bar:
    open_time: datetime
    open: float
    high: float
    low: float
    close: float


# (symbol, start, end) → 这段时间的 5 分钟 K 线（按时间升序）
BarFetcher = Callable[[str, datetime, datetime], Awaitable[list[Bar]]]


@dataclass(frozen=True)
class Outcome:
    row: JournalRow
    entry: float
    ret_pct: float
    mfe_pct: float  # 区间内最大上冲
    mae_pct: float  # 区间内最大下探（负数）
    dir_ret_pct: float | None  # 按卡的方向算的收益，both / no_trade 为 None

    @property
    def hit(self) -> bool | None:
        return None if self.dir_ret_pct is None else self.dir_ret_pct > 0


def score_row(row: JournalRow, bars: Sequence[Bar]) -> Outcome | None:
    """把一行流水和它有效期内的 K 线对上。K 线为空（拉不到）返回 None。"""
    inside = [b for b in bars if row.created_at <= b.open_time < row.expires_at]
    if not inside or inside[0].open <= 0:
        return None
    entry = inside[0].open
    ret = (inside[-1].close / entry - 1) * 100
    up = (max(b.high for b in inside) / entry - 1) * 100
    down = (min(b.low for b in inside) / entry - 1) * 100
    sign = {Bias.LONG.value: 1.0, Bias.SHORT.value: -1.0}.get(row.bias)
    return Outcome(row, entry, ret, up, down, None if sign is None else ret * sign)


@dataclass(frozen=True)
class Summary:
    key: str
    n: int
    scored: int
    hit_rate: float | None
    mean_dir_ret_pct: float | None
    mean_abs_ret_pct: float
    mean_range_pct: float


def summarize(outcomes: Iterable[Outcome], by: Callable[[Outcome], str]) -> list[Summary]:
    groups: dict[str, list[Outcome]] = defaultdict(list)
    for o in outcomes:
        groups[by(o)].append(o)
    out: list[Summary] = []
    for key, g in sorted(groups.items()):
        dirs = [o.dir_ret_pct for o in g if o.dir_ret_pct is not None]
        hits = [o for o in g if o.hit]
        out.append(
            Summary(
                key=key,
                n=len(g),
                scored=len(dirs),
                hit_rate=len(hits) / len(dirs) if dirs else None,
                mean_dir_ret_pct=sum(dirs) / len(dirs) if dirs else None,
                mean_abs_ret_pct=sum(abs(o.ret_pct) for o in g) / len(g),
                mean_range_pct=sum(o.mfe_pct - o.mae_pct for o in g) / len(g),
            )
        )
    return out


async def binance_bars(
    client: httpx.AsyncClient, symbol: str, start: datetime, end: datetime
) -> list[Bar]:
    r = await client.get(
        "/fapi/v1/klines",
        params={
            "symbol": symbol,
            "interval": "5m",
            "startTime": int(start.timestamp() * 1000),
            "endTime": int(end.timestamp() * 1000),
            "limit": 1000,
        },
    )
    r.raise_for_status()
    return [
        Bar(
            datetime.fromtimestamp(int(k[0]) / 1000, UTC),
            float(k[1]),
            float(k[2]),
            float(k[3]),
            float(k[4]),
        )
        for k in r.json()
    ]


async def score_journal(
    rows: Sequence[JournalRow],
    fetch: BarFetcher,
    now: datetime,
    *,
    since: datetime | None = None,
    concurrency: int = 4,
) -> list[Outcome]:
    """只给已经过期的卡打分；拉不到 K 线的行跳过（并发拉取）。"""
    due = [r for r in rows if r.expires_at <= now and (since is None or r.created_at >= since)]
    sem = asyncio.Semaphore(concurrency)

    async def one(r: JournalRow) -> Outcome | None:
        async with sem:
            try:
                bars = await fetch(r.symbol, r.created_at, r.expires_at)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                logger.warning("%s 的 K 线拉不到：%s", r.symbol, e)
                return None
        return score_row(r, bars)

    res = await asyncio.gather(*(one(r) for r in due))
    return [o for o in res if o is not None]


def _pct(v: float | None) -> str:
    return "-" if v is None else f"{v:+.2f}%"


def render(outcomes: Sequence[Outcome]) -> str:
    if not outcomes:
        return "还没有已过期且拉得到走势的作战卡可复盘。"
    lines: list[str] = [
        "# 原始口径（起点 = 整轮开始，未扣大盘）：只作对照，主指标看 jiaoyi card-score",
        "",
    ]
    for title, by in (
        ("按方向", lambda o: o.row.bias),
        ("按类别", lambda o: o.row.category),
        ("按出卡原因", lambda o: o.row.refresh_reason),
    ):
        lines.append(f"## {title}")
        lines.append("分组 | 条数 | 命中率 | 平均方向收益 | 平均|涨跌| | 平均波动幅度")
        for s in summarize(outcomes, by):
            hr = "-" if s.hit_rate is None else f"{s.hit_rate:.0%}（{s.scored}条）"
            lines.append(
                f"{s.key} | {s.n} | {hr} | {_pct(s.mean_dir_ret_pct)} | "
                f"{s.mean_abs_ret_pct:.2f}% | {s.mean_range_pct:.2f}%"
            )
        lines.append("")
    lines.append(
        "说明：命中 = 按卡的方向算收益 > 0（原始涨跌，含大盘）；both / no_trade 不计命中率。"
        "大盘一边倒时这个数跟着卡的多空倾斜摆，扣掉大盘的超额命中看 jiaoyi card-score。"
    )
    return "\n".join(lines)


async def _run(args: argparse.Namespace) -> int:
    now = datetime.now(UTC)
    rows = CardJournal(args.data_dir).read()
    since = now - timedelta(days=args.days)
    async with httpx.AsyncClient(base_url=_BASE, timeout=10.0) as client:

        async def fetch(sym: str, a: datetime, b: datetime) -> list[Bar]:
            return await binance_bars(client, sym, a, b)

        outcomes = await score_journal(rows, fetch, now, since=since)
    if args.json:
        payload: list[dict[str, Any]] = [
            {**asdict(o), "row": json.loads(o.row.model_dump_json()), "hit": o.hit}
            for o in outcomes
        ]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(outcomes))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="作战卡复盘：对照真实走势给历史作战卡打分")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--days", type=float, default=7.0, help="只看最近几天的卡")
    p.add_argument("--json", action="store_true", help="输出逐条 JSON")
    return asyncio.run(_run(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
