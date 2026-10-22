"""合成行情：没有网络或只想跑通流程时用。固定随机种子，结果可复现。

价格是带偶发放量突破的随机游走；``reversal>0`` 时突破之后反向回走（突破失败）。
它**不代表真实市场**，只用来验证回放流程和单元测试。
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from datetime import datetime, timedelta

from jiaoyi.contracts import MarketEvent, Side, Trade


def synthetic_trades(
    symbol: str,
    start: datetime,
    seconds: int,
    *,
    start_price: float = 68_000.0,
    seed: int = 1,
    trades_per_s: int = 4,
    burst_every_s: int = 600,
    burst_move_pct: float = 0.3,
    reversal: float = 0.0,
) -> Iterator[MarketEvent]:
    rng = random.Random(seed)
    price = start_price
    drift = 0.0
    for sec in range(seconds):
        burst = sec > 0 and sec % burst_every_s < 20
        if burst and sec % burst_every_s == 0:
            drift = (
                rng.choice([-1.0, 1.0])
                * start_price
                * burst_move_pct
                / 100.0
                / (20 * trades_per_s * 8)
            )
        elif not burst:
            if sec % burst_every_s == 20 and reversal:
                drift = -drift * reversal  # 突破失败：放量后反向走，顺势单会被打止损
            else:
                drift *= 0.97 if reversal else 0.5
        n = trades_per_s * (8 if burst else 1)
        for i in range(n):
            price = max(1.0, price + drift + rng.gauss(0.0, start_price * 0.00002))
            yield Trade(
                symbol=symbol,
                ts=start + timedelta(seconds=sec, milliseconds=int(1000 * i / n)),
                price=round(price, 2),
                qty=round(rng.uniform(0.001, 0.05) * (6 if burst else 1), 3),
                taker_side=Side.LONG
                if (drift > 0 if burst else rng.random() < 0.5)
                else Side.SHORT,
            )
