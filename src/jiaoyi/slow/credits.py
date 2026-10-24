"""模型额度（OpenRouter 余额）看护：定期只读查余额，快用完时提前告警；遇到 402 立即告警。

为什么要有：OpenRouter 按每次请求的 max_tokens 预留额度，余额低时并发开会会成批 402
（in_flight_budget_exhausted），会议全失败、没有新卡。这里不拦任何请求，只负责让人早点知道。

状态落盘在 ``data/llm_credits.json``，面板总览顶部据此显示横幅。只读 OpenRouter 的
``/api/v1/credits`` 和 ``/api/v1/key``，不会花钱。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CREDITS_FILE = "llm_credits.json"
BASE_URL = "https://openrouter.ai/api/v1"
CHECK_EVERY = timedelta(hours=1)
LOW_FLOOR_USD = 5.0
LOW_DAYS = 2.0
"""余额低于「日均花费 × 这么多天」或低于 ``LOW_FLOOR_USD`` 就算快用完。"""
RESEND_AFTER = timedelta(hours=6)
BANNER_EXHAUSTED_FOR = timedelta(minutes=30)
BANNER_LOW_FOR = timedelta(hours=3)


def is_credit_error(exc: BaseException) -> bool:
    """这个失败是不是模型额度不足（HTTP 402 / in_flight_budget_exhausted）。"""
    text = f"{exc} {getattr(exc, 'status_code', '')} {getattr(exc, 'code', '')}".lower()
    return (
        "in_flight_budget_exhausted" in text
        or "insufficient credit" in text
        or "payment required" in text
        or ("402" in text and ("credit" in text or "budget" in text or "afford" in text))
        or getattr(exc, "status_code", None) == 402
    )


@dataclass(frozen=True)
class Balance:
    balance_usd: float
    avg_daily_usd: float

    @property
    def low(self) -> bool:
        return self.balance_usd < max(LOW_FLOOR_USD, self.avg_daily_usd * LOW_DAYS)


def fetch_balance(key: str, transport: httpx.BaseTransport | None = None) -> Balance:
    """余额 = 买过的额度 − 已用（key 自己设了上限就取剩余额度和它里面小的）；日均花费看近 7 天。"""
    with httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {key}"},
        timeout=10.0,
        transport=transport,
    ) as c:
        credits = c.get("/credits")
        credits.raise_for_status()
        data = credits.json()["data"]
        balance = float(data["total_credits"]) - float(data["total_usage"])
        weekly = daily = 0.0
        try:
            info = c.get("/key")
            info.raise_for_status()
            k = info.json()["data"]
            weekly = float(k.get("usage_weekly") or 0.0)
            daily = float(k.get("usage_daily") or 0.0)
            if k.get("limit_remaining") is not None:
                balance = min(balance, float(k["limit_remaining"]))
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            logger.warning("读 OpenRouter key 用量失败，日均花费按 0 算", exc_info=True)
    return Balance(round(balance, 4), round(weekly / 7 if weekly > 0 else daily, 4))


Alert = tuple[str, str, str]
"""(级别 warn / critical, 标题, 正文)"""


class CreditMonitor:
    def __init__(
        self,
        data_dir: str | Path,
        get_key: Callable[[], str | None],
        fetch: Callable[[str], Balance] = fetch_balance,
    ) -> None:
        self._path = Path(data_dir) / CREDITS_FILE
        self._get_key = get_key
        self._fetch = fetch
        self._last_check: datetime | None = None
        self._exhausted_at: datetime | None = None
        self._exhausted_sent: datetime | None = None
        self._low_sent: datetime | None = None
        self._state: dict[str, Any] = {}

    def note_exhausted(self, now: datetime) -> None:
        """会议撞上 402（线程里调也行）。下一次 ``tick`` 发告警、写面板横幅。"""
        self._exhausted_at = now
        self._write(now)

    def tick(self, now: datetime) -> list[Alert]:
        """每隔一会儿调一次：到点就查余额，返回要发的告警（同一件事 6 小时内最多发一次）。"""
        alerts: list[Alert] = []
        if self._last_check is None or now - self._last_check >= CHECK_EVERY:
            self._check(now, alerts)
        if self._exhausted_at is not None and (
            self._exhausted_sent is None or now - self._exhausted_sent >= RESEND_AFTER
        ):
            self._exhausted_sent = now
            alerts.append(
                (
                    "critical",
                    "模型额度不足（OpenRouter 402）",
                    "军师团开会被 OpenRouter 拒了（额度不够）：新会议会失败、没有新作战卡。"
                    "请充值；持仓照常盯。",
                )
            )
        self._write(now)
        return alerts

    def _check(self, now: datetime, alerts: list[Alert]) -> None:
        key = self._get_key()
        if not key:
            return
        self._last_check = now
        try:
            b = self._fetch(key)
        except Exception as exc:
            logger.warning("查 OpenRouter 余额失败：%s", type(exc).__name__)
            return
        self._state = {"balance_usd": b.balance_usd, "avg_daily_usd": b.avg_daily_usd}
        if not b.low:
            self._exhausted_at = None  # 余额够了，之前的 402 不再显示
            self._low_sent = None
            return
        self._state["low"] = True
        if self._low_sent is None or now - self._low_sent >= RESEND_AFTER:
            self._low_sent = now
            alerts.append(
                (
                    "warn",
                    "模型额度快用完了",
                    f"OpenRouter 余额 ${b.balance_usd:.2f}，近 7 天日均花 ${b.avg_daily_usd:.2f}"
                    f"（低于 ${max(LOW_FLOOR_USD, b.avg_daily_usd * LOW_DAYS):.2f}）。请充值，"
                    "否则并发开会会成批被拒。",
                )
            )

    def _write(self, now: datetime) -> None:
        out = {
            **self._state,
            "checked_at": self._last_check.isoformat() if self._last_check else None,
            "exhausted_at": self._exhausted_at.isoformat() if self._exhausted_at else None,
        }
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            logger.exception("写 %s 失败（只影响面板横幅）", CREDITS_FILE)


def banner(raw: Any, now: datetime) -> str | None:
    """面板总览顶部要显示的话；没有事就是 None。``raw`` 是 llm_credits.json 的内容。"""
    if not isinstance(raw, dict):
        return None

    def when(key: str) -> datetime | None:
        try:
            v = raw.get(key)
            return datetime.fromisoformat(v) if v else None
        except (TypeError, ValueError):
            return None

    exhausted, checked = when("exhausted_at"), when("checked_at")
    if exhausted is not None and now - exhausted <= BANNER_EXHAUSTED_FOR:
        return "模型额度不足：OpenRouter 拒绝了开会请求（402），新会议会失败。请充值。"
    if raw.get("low") and checked is not None and now - checked <= BANNER_LOW_FOR:
        bal, avg = raw.get("balance_usd"), raw.get("avg_daily_usd")
        if isinstance(bal, int | float) and isinstance(avg, int | float):
            return f"模型额度快用完了：OpenRouter 余额 ${bal:.2f}，日均花 ${avg:.2f}。请充值。"
    return None
