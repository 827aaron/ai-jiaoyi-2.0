"""hummingbot-api 的 REST 客户端（只含我们用到的几个接口）。

接口形状按钉住的上游提交（见 Makefile 的 HB_API_REF）读源码得来：
``POST /executors/``、``GET /executors/{id}``、``POST /executors/{id}/stop``、
``POST /executors/search``、``POST /portfolio/state``、``POST /trading/positions``。
鉴权是 HTTP Basic。每个调用都有超时；失败统一抛 ``HbApiError``。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import httpx


class HbApiError(Exception):
    """hummingbot-api 不可达、鉴权失败或返回错误。

    ``definite_rejection`` 为真表示对方明确拒绝（HTTP 4xx），请求肯定没有生效；
    超时、断连、5xx 则结果不明——对方可能已经执行了。
    ``transient`` 为真表示是网络层的一时失败（超时、连不上），只读查询可以稍后重试。
    """

    def __init__(
        self, message: str, *, definite_rejection: bool = False, transient: bool = False
    ) -> None:
        super().__init__(message)
        self.definite_rejection = definite_rejection
        self.transient = transient


@runtime_checkable
class HummingbotApi(Protocol):
    async def create_executor(
        self, executor_config: dict[str, Any], account_name: str, controller_id: str
    ) -> dict[str, Any]:
        """``controller_id`` 是我们的认领标记（``jy-<position_id>``），hummingbot 会原样带回。"""
        ...

    async def find_executors(self, account_name: str, controller_id: str) -> list[dict[str, Any]]:
        """按认领标记查执行器，运行中和已结束的都算。"""
        ...

    async def get_executor(self, executor_id: str) -> dict[str, Any] | None:
        """找不到返回 None。"""
        ...

    async def stop_executor(self, executor_id: str, *, keep_position: bool) -> None: ...

    async def running_executors(self, account_name: str) -> list[dict[str, Any]]: ...

    async def portfolio_state(self, account_name: str, connector_name: str) -> list[dict[str, Any]]:
        """该账户该连接器下的各币种余额条目（token、units、available_units ...）。"""
        ...

    async def positions(self, account_name: str, connector_name: str) -> list[dict[str, Any]]: ...

    async def close(self) -> None: ...


class HttpHummingbotApi:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout_s: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=httpx.BasicAuth(username, password),
            timeout=timeout_s,
            transport=transport,
        )

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise HbApiError(
                f"hummingbot-api 不可达（{method} {path}）：{e!r}", transient=True
            ) from e
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise HbApiError(
                f"hummingbot-api {method} {path} 返回 {resp.status_code}：{resp.text[:300]}"
            )
        try:
            return resp.json()
        except ValueError as e:
            raise HbApiError(f"hummingbot-api {path} 返回了非 JSON") from e

    async def create_executor(
        self, executor_config: dict[str, Any], account_name: str, controller_id: str
    ) -> dict[str, Any]:
        out = await self._call(
            "POST",
            "/executors/",
            json={
                "account_name": account_name,
                "executor_config": executor_config,
                "controller_id": controller_id,
            },
        )
        if not isinstance(out, dict) or not out.get("executor_id"):
            raise HbApiError(f"建执行器没有返回 executor_id：{out!r}")
        return out

    async def get_executor(self, executor_id: str) -> dict[str, Any] | None:
        out = await self._call("GET", f"/executors/{executor_id}")
        return out if isinstance(out, dict) else None

    async def stop_executor(self, executor_id: str, *, keep_position: bool) -> None:
        out = await self._call(
            "POST", f"/executors/{executor_id}/stop", json={"keep_position": keep_position}
        )
        if out is None:
            raise HbApiError(f"停执行器时找不到 {executor_id}")

    async def find_executors(self, account_name: str, controller_id: str) -> list[dict[str, Any]]:
        out = await self._call(
            "POST",
            "/executors/search",
            json={"account_names": [account_name], "controller_ids": [controller_id], "limit": 50},
        )
        return [d for d in (out or {}).get("data", []) if isinstance(d, dict)]

    async def running_executors(self, account_name: str) -> list[dict[str, Any]]:
        out = await self._call(
            "POST",
            "/executors/search",
            json={"account_names": [account_name], "status": "RUNNING", "limit": 200},
        )
        data = (out or {}).get("data", [])
        return [d for d in data if isinstance(d, dict)]

    async def portfolio_state(self, account_name: str, connector_name: str) -> list[dict[str, Any]]:
        out = await self._call(
            "POST",
            "/portfolio/state",
            json={
                "account_names": [account_name],
                "connector_names": [connector_name],
                "refresh": True,
                "skip_gateway": True,
            },
        )
        entries = ((out or {}).get(account_name) or {}).get(connector_name) or []
        return [e for e in entries if isinstance(e, dict)]

    async def positions(self, account_name: str, connector_name: str) -> list[dict[str, Any]]:
        out = await self._call(
            "POST",
            "/trading/positions",
            json={"account_names": [account_name], "connector_names": [connector_name]},
        )
        return [d for d in (out or {}).get("data", []) if isinstance(d, dict)]

    async def close(self) -> None:
        await self._http.aclose()
