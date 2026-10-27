"""hummingbot-api 的 REST 客户端（只含我们用到的几个接口）。

接口形状按钉住的上游提交（见 Makefile 的 HB_API_REF）读源码得来：
``POST /executors/``、``GET /executors/{id}``、``POST /executors/{id}/stop``、
``POST /executors/search``、``POST /portfolio/state``、``POST /trading/positions``。
鉴权是 HTTP Basic。每个调用都有超时；失败统一抛 ``HbApiError``。
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

import httpx

_NOT_FOUND = object()


def executor_query_state(
    detail: dict[str, Any] | None, executor_id: str
) -> Literal["active", "terminal", "unknown"]:
    """逐项查询的证据，不把列表缺席、404 或矛盾字段当作已终止。

    active 只是接口报告的状态（上游可能读数据库），不保证执行器实时健康。
    """
    if not isinstance(detail, dict) or detail.get("executor_id") != executor_id:
        return "unknown"
    status = detail.get("status")
    active = detail.get("is_active")
    if status == "TERMINATED" and active is False:
        return "terminal"
    if status in ("NOT_STARTED", "RUNNING", "SHUTTING_DOWN") and active is True:
        return "active"
    return "unknown"


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
        """404 返回 None；仅代表查询未找到，不能证明运行中的执行器确实消失。"""
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

    async def _call(
        self, method: str, path: str, *, not_found_value: Any = None, **kwargs: Any
    ) -> Any:
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise HbApiError(
                f"hummingbot-api 不可达（{method} {path}）：{e!r}", transient=True
            ) from e
        if resp.status_code == 404:
            return not_found_value
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
        out = await self._call("GET", f"/executors/{executor_id}", not_found_value=_NOT_FOUND)
        if out is _NOT_FOUND:
            return None
        if not isinstance(out, dict) or executor_query_state(out, executor_id) == "unknown":
            # 不输出响应正文：状态校验不需要账户、订单或其它私有字段。
            raise HbApiError("hummingbot-api 执行器详情缺失、标识不匹配或状态不一致")
        return out

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
        return self._executor_page(out)

    async def running_executors(self, account_name: str) -> list[dict[str, Any]]:
        out = await self._call(
            "POST",
            "/executors/search",
            json={"account_names": [account_name], "status": "RUNNING", "limit": 200},
        )
        return self._executor_page(out)

    @staticmethod
    def _executor_page(out: Any) -> list[dict[str, Any]]:
        """查询失败、错误结构和未读完的分页都不是「没有执行器」。

        兼容未提供分页字段的旧成功响应；列表缺席仍需逐项终态证据，不能据此平仓。
        """
        if not isinstance(out, dict) or not isinstance(out.get("data"), list):
            raise HbApiError("hummingbot-api 执行器列表缺失或格式错误")
        data = out["data"]
        for item in data:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("executor_id"), str)
                or not item["executor_id"].strip()
            ):
                raise HbApiError("hummingbot-api 执行器列表含无效条目")
        if "pagination" in out:
            page = out["pagination"]
            if not isinstance(page, dict) or not isinstance(page.get("has_more"), bool):
                raise HbApiError("hummingbot-api 执行器列表分页信息不明确")
            if page["has_more"] or page.get("next_cursor") is not None:
                raise HbApiError("hummingbot-api 执行器列表分页未读完，不能判定执行器缺席")
            total = page.get("total_count")
            if total is not None and (type(total) is not int or total != len(data)):
                raise HbApiError("hummingbot-api 执行器列表总数与返回条目不一致")
        return list(data)

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
