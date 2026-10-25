"""Telegram 告警的类别：每一类对应 ``[app]`` 里一个 ``notify_<类别>`` 开关，默认都开。

发告警的地方给 ``Alert.kind`` 填类别（不靠标题文字猜）；Telegram 发送前看开关，关了就不推。
面板告警列表和日志不受影响。直接走 Telegram 的几条（面板切模式、测试消息、看门狗、命令行
go-live）不经过告警器，所以没有开关。
"""

from __future__ import annotations

KINDS: tuple[str, ...] = (
    "open_filled",
    "close_done",
    "lifeboat_hit",
    "lifeboat_missing",
    "order_failed",
    "reconcile",
    "news_block",
    "model_credit",
    "meeting_paused",
    "control",
    "live_started",
    "system",
)


def field_of(kind: str) -> str:
    return f"notify_{kind}"
