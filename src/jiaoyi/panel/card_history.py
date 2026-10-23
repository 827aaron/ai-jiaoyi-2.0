"""历史卡：过去每条作战卡指令（一个币一场会的结论）后来怎么样了。

数据来自 ``data/card_review/cards.jsonl``（出卡时每个真开过会的币追加一行，见 ``slow/review.py``），
参数（止损 / 止盈 / 杠杆 / 仓位 / 理由）从当时军师团的决策记录里找，开没开过仓从账本里找。
只读，不改任何东西。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from itertools import pairwise
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ValidationError

from jiaoyi.contracts import DecisionRecord, Outcome, Role
from jiaoyi.contracts.battle_card import CardSymbol
from jiaoyi.panel.files import PanelFiles
from jiaoyi.slow.review import CardJournal, JournalRow

HistoryEnd = Literal["active", "expired", "replaced"]
"""还在用 / 到点过期 / 到期前被同一个币的新结论替换。"""

MATCH_WINDOW = timedelta(minutes=30)
"""出卡时间前后多久内的军师团记录算这条指令的会议结论。"""


class CardHistoryRow(BaseModel):
    symbol: str
    category: str
    bias: str
    regime: str
    issued_at: datetime
    expires_at: datetime
    ended_at: datetime
    """实际失效时间：被替换就是新结论出来的时间，否则是到期时间。"""
    end: HistoryEnd
    reason: str = ""
    stop_atr: tuple[float, float] | None = None
    take_profit_r: tuple[float, float] | None = None
    leverage: tuple[float, float] | None = None
    notional_usdt: tuple[float, float] | None = None
    trades: int = 0
    """有效期内这个币开过几笔（含还没平的）。"""
    pnl_usdt: float | None = None
    """这几笔里已平仓的盈亏合计；一笔都没平就是 None。"""


class CardHistoryPage(BaseModel):
    day: str
    """看的是哪一天（本地时区），YYYY-MM-DD。"""
    days: list[str]
    """流水里有记录的日子，新的在前。"""
    symbols: list[str]
    """这一天出现过的币（筛选用）。"""
    rows: list[CardHistoryRow]
    """新的在前。"""


def build_card_history(
    files: PanelFiles, now: datetime, day: date | None = None, symbol: str | None = None
) -> CardHistoryPage:
    tz = ZoneInfo(files.tz)
    journal = CardJournal(files.root).read()

    def local(t: datetime) -> date:
        return t.astimezone(tz).date()

    days = sorted({local(r.created_at) for r in journal}, reverse=True)
    want = day or (days[0] if days else now.astimezone(tz).date())
    # 被替换：同一个币下一条指令在本条到期前出来
    by_sym: dict[str, list[JournalRow]] = {}
    for r in sorted(journal, key=lambda r: r.created_at):
        by_sym.setdefault(r.symbol, []).append(r)
    nxt: dict[int, datetime] = {}
    for rows in by_sym.values():
        for a, b in pairwise(rows):
            if b.created_at < a.expires_at:
                nxt[id(a)] = b.created_at
    picked = [r for r in journal if local(r.created_at) == want]
    symbols = sorted({r.symbol for r in picked})
    if symbol:
        picked = [r for r in picked if r.symbol == symbol]
    start = datetime.combine(want, datetime.min.time(), tz)
    recs = (
        files.decisions_since(start - MATCH_WINDOW, now, frozenset({Role.COMMANDER}))
        if picked
        else []
    )
    positions = files.ledger().positions
    out: list[CardHistoryRow] = []
    for r in sorted(picked, key=lambda r: r.created_at, reverse=True):
        replaced = nxt.get(id(r))
        ended = replaced or r.expires_at
        end: HistoryEnd = "replaced" if replaced else "expired" if now >= r.expires_at else "active"
        cs = _conclusion(recs, r)
        mine = [
            p for p in positions if p.symbol == r.symbol and r.created_at <= p.opened_at < ended
        ]
        closed = [p.realized_pnl_usdt for p in mine if p.realized_pnl_usdt is not None]
        out.append(
            CardHistoryRow(
                symbol=r.symbol,
                category=r.category,
                bias=r.bias,
                regime=r.regime,
                issued_at=r.created_at,
                expires_at=r.expires_at,
                ended_at=ended,
                end=end,
                reason=cs.reason if cs else "",
                stop_atr=(cs.stop_atr.min, cs.stop_atr.max) if cs else None,
                take_profit_r=(cs.take_profit_r.min, cs.take_profit_r.max) if cs else None,
                leverage=(cs.leverage.min, cs.leverage.max) if cs else None,
                notional_usdt=(cs.notional_usdt.min, cs.notional_usdt.max) if cs else None,
                trades=len(mine),
                pnl_usdt=sum(closed) if closed else None,
            )
        )
    return CardHistoryPage(
        day=want.isoformat(), days=[d.isoformat() for d in days], symbols=symbols, rows=out
    )


def _conclusion(recs: list[DecisionRecord], row: JournalRow) -> CardSymbol | None:
    """出卡时间前后最近的一条这个币的军师团结论。"""
    best: tuple[timedelta, CardSymbol] | None = None
    for rec in recs:
        if rec.symbol != row.symbol or rec.outcome is not Outcome.ACTION or not rec.output:
            continue
        gap = abs(rec.ts - row.created_at)
        if gap > MATCH_WINDOW or (best and gap >= best[0]):
            continue
        try:
            # 记录里还夹着 usage（token 用量）等不属于指令的键，契约禁止多余字段，先去掉
            cs = CardSymbol.model_validate(
                {k: v for k, v in rec.output.items() if k in CardSymbol.model_fields}
            )
        except ValidationError:
            continue
        if cs.bias.value != row.bias:  # 方向对不上就不是这场会
            continue
        best = (gap, cs)
    return best[1] if best else None
