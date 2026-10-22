"""桩：告警只存在内存里，测试可以检查发了什么。"""

from __future__ import annotations

from jiaoyi.contracts import Alert


class StubAlerter:
    def __init__(self) -> None:
        self.sent: list[Alert] = []

    async def send(self, alert: Alert) -> None:
        self.sent.append(alert)
