"""面板用的 hummingbot-api 只读客户端：只查余额和持仓，没有下单 / 停执行器的方法。

接口形状同 ``jiaoyi.execution.hb_client``（那边是执行层的读写客户端，面板不导入执行层）。
结果缓存几秒，面板几个人同时开、几秒刷新一次也不会打爆 hummingbot-api。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx


@dataclass
class HbSnapshot:
    ok: bool
    error: str | None = None
    wallet_usdt: float | None = None
    """USDT 钱包余额（不含浮盈浮亏）。"""
    available_usdt: float | None = None
    """可用保证金（还能开新仓的 USDT）。"""
    positions: list[dict[str, Any]] = field(default_factory=list)
    """hummingbot-api 原样返回的持仓条目（trading_pair、unrealized_pnl、amount ...）。"""
    fetched_at: datetime | None = None
    """面板向 hummingbot-api 拉到这份数的时间（要求它先从币安刷新余额）。"""

    @property
    def unrealized_usdt(self) -> float:
        return sum(_num(p.get("unrealized_pnl")) for p in self.positions)

    @property
    def equity_usdt(self) -> float | None:
        """口径同执行层：钱包余额 + 未实现盈亏。"""
        return None if self.wallet_usdt is None else self.wallet_usdt + self.unrealized_usdt

    def unrealized_of(self, symbol: str) -> float | None:
        """``BTCUSDT`` → 该币的未实现盈亏；hummingbot 用 ``BTC-USDT``。"""
        pair = symbol.removesuffix("USDT") + "-USDT"
        hits = [p for p in self.positions if str(p.get("trading_pair")) in (pair, symbol)]
        return sum(_num(p.get("unrealized_pnl")) for p in hits) if hits else None


class HbReader:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str | None,
        *,
        account_name: str,
        connector_name: str,
        timeout_s: float = 4.0,
        cache_s: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._password = password
        self._username = username
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=httpx.BasicAuth(username, password or ""),
            timeout=timeout_s,
            transport=transport,
        )
        self._account = account_name
        self._connector = connector_name
        self._cache_s = cache_s
        self._cached: tuple[float, HbSnapshot] | None = None
        self._stop_cached: dict[str, tuple[float, object]] = {}

    def update_password(self, password: str) -> None:
        """首次设置向导刚写进钥匙串的 hummingbot-api 密码，不用重启面板就生效。"""
        self._password = password
        self._http.auth = httpx.BasicAuth(self._username, password)
        self._cached = None
        self._stop_cached.clear()

    async def snapshot(self) -> HbSnapshot:
        if self._cached and time.monotonic() - self._cached[0] < self._cache_s:
            return self._cached[1]
        snap = await self._fetch()
        self._cached = (time.monotonic(), snap)
        return snap

    async def _fetch(self) -> HbSnapshot:
        if not self._password:
            return HbSnapshot(ok=False, error="没配 HUMMINGBOT_API_PASSWORD")
        names = {"account_names": [self._account], "connector_names": [self._connector]}
        try:
            # refresh：先让 hummingbot-api 从币安重拉余额，否则是它后台几分钟一次的旧数（同执行层）
            state = await self._post(
                "/portfolio/state", {**names, "skip_gateway": True, "refresh": True}
            )
            pos = await self._post("/trading/positions", names)
        except _HbError as e:
            return HbSnapshot(ok=False, error=str(e))
        entries = ((state or {}).get(self._account) or {}).get(self._connector) or []
        usdt = next((e for e in entries if isinstance(e, dict) and e.get("token") == "USDT"), None)
        return HbSnapshot(
            ok=True,
            wallet_usdt=_num(usdt.get("units")) if usdt else None,
            available_usdt=_num(usdt.get("available_units")) if usdt else None,
            positions=[d for d in (pos or {}).get("data", []) if isinstance(d, dict)],
            fetched_at=datetime.now(UTC),
        )

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        try:
            resp = await self._http.post(path, json=body)
        except httpx.HTTPError as e:
            raise _HbError(f"hummingbot-api 连不上：{type(e).__name__}") from e
        if resp.status_code >= 400:
            raise _HbError(f"hummingbot-api {path} 返回 {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise _HbError(f"hummingbot-api {path} 返回了非 JSON") from e

    async def close(self) -> None:
        await self._http.aclose()

    async def stop_diagnostics(self, executor_id: str | None) -> object:
        """执行器详情的只读诊断投影；不回传其它账户 / 订单字段。"""
        if (
            not self._password
            or not executor_id
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", executor_id)
        ):
            return None
        hit = self._stop_cached.get(executor_id)
        if hit and time.monotonic() - hit[0] < self._cache_s:
            return hit[1]
        result: object = None
        try:
            async with self._http.stream("GET", f"/executors/{executor_id}") as resp:
                if resp.status_code == 200:
                    body = bytearray()
                    async for chunk in resp.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 256 * 1024:
                            break
                    else:
                        raw = json.loads(body)
                        if (
                            isinstance(raw, dict)
                            and raw.get("id", raw.get("executor_id")) == executor_id
                            and raw.get("executor_id", executor_id) == executor_id
                        ):
                            custom = raw.get("custom_info")
                            if isinstance(custom, dict):
                                result = custom.get("stop_diagnostics")
        except (httpx.HTTPError, ValueError):
            pass
        if len(self._stop_cached) >= 100:
            self._stop_cached.clear()
        self._stop_cached[executor_id] = (time.monotonic(), result)
        return result


class _HbError(Exception):
    pass


def _num(v: object) -> float:
    if isinstance(v, int | float | str) and not isinstance(v, bool):
        try:
            return float(v)
        except ValueError:
            return 0.0
    return 0.0
