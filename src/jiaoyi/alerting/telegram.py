"""Telegram 告警。发送失败只记日志，绝不抛出、绝不拖住交易回路。

同一 ``dedup_key`` 在冷却时间内只发一次，免得对账员每 30 秒刷屏。
bot token 在请求 URL 里，所以任何日志都不打印异常原文，只打印异常类型。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

import httpx

from jiaoyi.contracts import Alert
from jiaoyi.contracts.module_status import AlertLevel

log = logging.getLogger(__name__)

_ICON = {AlertLevel.INFO: "ℹ️", AlertLevel.WARN: "⚠️", AlertLevel.CRITICAL: "🚨"}


class TelegramAlerter:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        cooldown_s: float = 300.0,
        muted_kinds: frozenset[str] = frozenset(),
        timeout_s: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._chat_id = chat_id
        self._cooldown_s = cooldown_s
        self._muted = muted_kinds
        self._monotonic = monotonic
        self._last_sent: dict[str, float] = {}
        self.last_error: str | None = None
        """上一条发送失败的原因（只写类型 / HTTP 码，不带 token）；成功是 None。"""
        self.last_skipped = False
        """上一条是不是因为冷却期内同一件事被跳过了。"""
        self._http = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{bot_token}",
            timeout=timeout_s,
            transport=transport,
        )

    @staticmethod
    def format(alert: Alert) -> str:
        head = f"{_ICON[alert.level]} {alert.title}"
        symbol = f" [{alert.symbol}]" if alert.symbol else ""
        return f"{head}{symbol}\n{alert.body}".strip()

    async def send(self, alert: Alert) -> None:
        self.last_skipped = False
        if alert.kind in self._muted:
            return  # 设置里关了这一类：不发 Telegram（面板告警列表照记，也不占冷却）
        if alert.dedup_key is not None:
            now = self._monotonic()
            last = self._last_sent.get(alert.dedup_key)
            if last is not None and now - last < self._cooldown_s:
                self.last_skipped = True
                return
            self._last_sent[alert.dedup_key] = now
        try:
            resp = await self._http.post(
                "/sendMessage", json={"chat_id": self._chat_id, "text": self.format(alert)}
            )
            if resp.status_code >= 400:
                self.last_error = f"HTTP {resp.status_code}"
                log.error("Telegram 告警被拒：HTTP %d，标题 %s", resp.status_code, alert.title)
            else:
                self.last_error = None
        except Exception as e:
            self.last_error = type(e).__name__
            log.error("Telegram 告警发送失败（%s），标题 %s", type(e).__name__, alert.title)

    async def close(self) -> None:
        await self._http.aclose()
