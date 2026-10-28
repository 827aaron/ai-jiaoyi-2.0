"""大盘台的「4 小时之内」（``[market_desk] publish_intra``）：大盘台最近一轮的 1 小时概率和代码写的
短线现状，接在 4 小时观点后面给各币会议、管家和面板。

放在单独的轻模块里：面板也要用同一段文字，不能为此把整个大盘台（模型调用、开会引擎）拉进来。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

Order = Literal["up_first", "down_first"]

INTRA_FIELDS: tuple[str, ...] = (
    "pool_ret_15m_pct",
    "pool_up_count_15m",
    "pool_down_count_15m",
    "pool_ret_1h_pct",
    "pool_up_count_1h",
    "pool_down_count_1h",
    "btc_ret_15m_pct",
    "btc_ret_1h_pct",
    "pool_taker_buy_share_1h",
    "pool_taker_sell_share_1h",
)
PROBS_1H_HORIZON = timedelta(hours=1)
"""「未来 1 小时概率」那一轮开跑超过 1 小时就不给：预测的那一小时已经过去了。"""


def _pair(order: Order, up: str, down: str) -> tuple[str, str]:
    return (up, down) if order == "up_first" else (down, up)


def compose_intra(block: Mapping[str, Any], now: datetime, max_age_min: float) -> str:
    """「HH:MM UTC 起 1 小时概率」（那一轮开跑时刻起）+「短线现状（截至 HH:MM UTC）」，全是代码
    按数写的、成对的话；涨 / 跌（买入 / 卖出）谁先按那一轮的顺序交替。

    短线现状优先用两轮之间刷新的 ``status``（比这一轮数据包新时），主动买卖占比只有每轮的那份，
    和现状不是同一时刻就标上自己的时间。数据旧过 ``max_age_min``（0 = 不查）写缺失。"""
    order: Order = "down_first" if block.get("order") == "down_first" else "up_first"
    t0 = datetime.fromisoformat(str(block["t0"]))
    run_as_of = datetime.fromisoformat(str(block["as_of"]))
    p = block.get("probs_1h") or {}
    run = f"{t0.astimezone(UTC):%H:%M} UTC"
    if now - t0 > PROBS_1H_HORIZON:
        head = f"{run} 起 1 小时概率：缺失（已过 1 小时）。"
    else:
        a, b = _pair(order, f"涨 {float(p['up']):.0%}", f"跌 {float(p['down']):.0%}")
        head = f"{run} 起 1 小时概率：{a} / 平 {float(p['flat']):.0%} / {b}。"
    run_values: Mapping[str, float] = block.get("values") or {}
    status = block.get("status")
    as_of, v = run_as_of, run_values
    if isinstance(status, dict):
        at = datetime.fromisoformat(str(status["as_of"]))
        if at > run_as_of:
            as_of, v = at, status.get("values") or {}

    def fresh(at: datetime) -> bool:
        return max_age_min <= 0 or (now - at).total_seconds() / 60 <= max_age_min

    stamp = f"{as_of.astimezone(UTC):%H:%M} UTC"
    if not fresh(as_of):
        age = (now - as_of).total_seconds() / 60
        why = f"截至 {stamp} 的数据已过 {age:.0f} 分钟，上限 {max_age_min:g} 分钟"
        return head + f"短线现状：缺失（{why}）。"

    def pct(name: str) -> str:
        return f"{v[name]:+.2f}%" if name in v else "缺失"

    pool_bits = []
    for tag, label in (("15m", "15 分钟"), ("1h", "1 小时")):
        up, down = v.get(f"pool_up_count_{tag}"), v.get(f"pool_down_count_{tag}")
        counts = ""
        if up is not None and down is not None:
            x, y = _pair(order, f"涨 {up:.0f}", f"跌 {down:.0f}")
            counts = f"（{x} / {y} 个）"
        pool_bits.append(f"{label} {pct(f'pool_ret_{tag}_pct')}{counts}")
    parts = [
        "等权池 " + "，".join(pool_bits),
        f"BTC 15 分钟 {pct('btc_ret_15m_pct')}，1 小时 {pct('btc_ret_1h_pct')}",
    ]
    buy = run_values.get("pool_taker_buy_share_1h")
    sell = run_values.get("pool_taker_sell_share_1h")
    if buy is not None and sell is not None:
        x, y = _pair(order, f"主动买入 {buy:.1f}%", f"主动卖出 {sell:.1f}%")
        own = f"{run_as_of.astimezone(UTC):%H:%M} UTC"
        if not fresh(run_as_of):
            parts.append(f"1 小时主动买卖占比缺失（只在大盘台每轮取，上次截至 {own}）")
        elif as_of != run_as_of:
            parts.append(f"1 小时成交里{x} / {y}（截至 {own}）")
        else:
            parts.append(f"1 小时成交里{x} / {y}")
    return head + f"短线现状（截至 {stamp}）：" + "；".join(parts) + "。"
