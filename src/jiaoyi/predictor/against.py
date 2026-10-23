"""预测员看反作战卡：同一个币连续 ``streak`` 次新预测的方向都和卡上方向相反，就发一次事件。

慢环拿它触发单币临时会（研究 v2「预测员反向」）：

- 订阅：``watch.subscribe(callback)``，达到次数时回调一次 ``AgainstCard``（在事件循环线程里）。
- 查询：``watch.current(now)``，现在还处在“看反”状态的币。

方向：卡做多而 ``p_up < 0.5``、卡做空而 ``p_up > 0.5`` 算看反；``p_up == 0.5`` 不算任何一边，
中断连续计数。卡是双向 / 只看不做 / 没有这个币 / 卡过期都不算。这个币卡上的方向变了就从头数
（同一轮会里卡版本会随每个币开完往上加，只看方向，不看版本）。同一条预测只算一次
（按预测时间去重）。只报信号，不平仓、不改卡。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from jiaoyi.contracts import BattleCard, Bias, Forecast

log = logging.getLogger(__name__)

DEFAULT_STREAK = 2
"""预测员每 5 分钟一次，连续 2 次 = 10 分钟一致看反。"""


@dataclass(frozen=True)
class AgainstCard:
    symbol: str
    card_bias: Bias
    p_up: float
    """最近一次预测的上涨概率。"""
    streak: int
    since: datetime
    """连续看反的第一条预测时间。"""
    ts: datetime
    """最近一条预测时间。"""
    card_id: str
    card_version: int


class AgainstCardWatch:
    def __init__(
        self, card: Callable[[datetime], BattleCard | None], *, streak: int = DEFAULT_STREAK
    ) -> None:
        if streak < 1:
            raise ValueError("streak 至少为 1")
        self._card = card
        self._need = streak
        self._state: dict[str, AgainstCard] = {}
        self._last_ts: dict[str, datetime] = {}
        self._listeners: list[Callable[[AgainstCard], None]] = []

    def subscribe(self, callback: Callable[[AgainstCard], None]) -> None:
        self._listeners.append(callback)

    def current(self, now: datetime) -> dict[str, AgainstCard]:
        """达到次数、而且卡还是那张有效卡的币。"""
        card = self._card(now)
        if card is None or not card.is_valid(now):
            return {}
        out: dict[str, AgainstCard] = {}
        for s, a in self._state.items():
            cs = card.for_symbol(s)
            if a.streak >= self._need and cs is not None and cs.bias == a.card_bias:
                out[s] = a
        return out

    def observe(self, fc: Forecast) -> None:
        """预测员每出一条新预测调一次（``KronosForecaster.add_listener``）。"""
        sym = fc.symbol
        if self._last_ts.get(sym) == fc.ts:
            return
        self._last_ts[sym] = fc.ts
        card = self._card(fc.ts)
        cs = card.for_symbol(sym) if card is not None and card.is_valid(fc.ts) else None
        if card is None or cs is None or not _against(cs.bias, fc.p_up):
            self._state.pop(sym, None)
            return
        prev = self._state.get(sym)
        same = prev is not None and prev.card_bias == cs.bias
        n = prev.streak + 1 if prev is not None and same else 1
        cur = AgainstCard(
            symbol=sym,
            card_bias=cs.bias,
            p_up=fc.p_up,
            streak=n,
            since=prev.since if prev is not None and same else fc.ts,
            ts=fc.ts,
            card_id=card.card_id,
            card_version=card.version,
        )
        self._state[sym] = cur
        if n == self._need:
            for cb in list(self._listeners):
                try:
                    cb(cur)
                except Exception:
                    log.exception("预测员看反事件的订阅方出错")


def _against(bias: Bias, p_up: float) -> bool:
    if bias == Bias.LONG:
        return p_up < 0.5
    if bias == Bias.SHORT:
        return p_up > 0.5
    return False
