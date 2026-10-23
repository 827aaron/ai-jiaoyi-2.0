"""面板的 HTTP 服务（FastAPI）。``jiaoyi panel`` 启动，Mac mini 上用局域网地址打开。

- 只接受本机和局域网地址（私有网段、回环、链路本地）来的请求，其余一律 403。
- 这一版全部只读：暂停 / 全部平仓 / 改设置要主循环配合，后续单独接（页面上按钮先灰掉）。
- 页面是 ``static/`` 下的静态文件，数据走 ``/api/*``；换皮只动 ``static/theme.css``。
"""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from jiaoyi.contracts import AppConfig, RiskConfig
from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.hb import HbReader
from jiaoyi.panel.overview import Overview, build_overview

STATIC = Path(__file__).parent / "static"

Clock = Callable[[], datetime]


def is_lan(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


def create_app(
    risk: RiskConfig,
    app_cfg: AppConfig,
    hb: HbReader,
    *,
    clock: Clock = lambda: datetime.now(UTC),
    lan_only: bool = True,
) -> FastAPI:
    files = PanelFiles(app_cfg.data_dir, risk.day_reset_tz)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await hb.close()

    api = FastAPI(title="jiaoyi 面板", docs_url=None, redoc_url=None, lifespan=lifespan)

    @api.middleware("http")
    async def lan_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]):  # type: ignore[no-untyped-def]
        host = request.client.host if request.client else None
        if lan_only and not is_lan(host):
            return JSONResponse({"detail": "面板只对局域网开放"}, status_code=403)
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    @api.get("/api/overview")
    async def overview() -> Overview:
        return build_overview(files, await hb.snapshot(), risk, app_cfg, clock())

    @api.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    api.mount("/static", StaticFiles(directory=STATIC), name="static")
    return api
