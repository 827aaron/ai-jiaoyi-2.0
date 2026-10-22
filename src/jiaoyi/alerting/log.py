"""没配 Telegram 时的退路：告警打到日志里（stderr）。"""

from __future__ import annotations

import logging

from jiaoyi.contracts import Alert

log = logging.getLogger("jiaoyi.alert")


class LogAlerter:
    async def send(self, alert: Alert) -> None:
        log.warning("[%s] %s %s %s", alert.level.value, alert.title, alert.symbol or "", alert.body)
