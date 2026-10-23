"""面板的 HTTP 服务（FastAPI）。``jiaoyi panel`` 启动，Mac mini 上用局域网地址打开。

- 只接受本机和局域网地址（私有网段、回环、链路本地）来的请求，其余一律 403。
- Host 头也要是局域网 IP、localhost、``*.local`` 或 ``--allow-host`` 给的名字，挡 DNS 重绑定。
- 写操作（POST）另要 ``Origin`` 和 Host 一致、带 ``X-Jiaoyi-Panel: 1`` 头（别的网站发不出来），
  具体动作和密码规则见 ``jiaoyi.panel.actions``。面板只写指令文件和配置文件，自己不下单。
- 页面是 ``static/`` 下的静态文件，数据走 ``/api/*``；换皮只动 ``static/theme.css``。
"""

from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from jiaoyi.contracts import AppConfig, RiskConfig, Role
from jiaoyi.core.commands import CommandKind
from jiaoyi.core.config import ConfigError
from jiaoyi.panel import actions
from jiaoyi.panel.actions import ActionError, ConfigPaths, Section
from jiaoyi.panel.files import PanelFiles
from jiaoyi.panel.hb import HbReader
from jiaoyi.panel.market import Interval, KlineError, KlineSource
from jiaoyi.panel.overview import Overview, build_overview
from jiaoyi.panel.pages import (
    FundsPage,
    HistoryPage,
    HistoryRange,
    PoolPage,
    PositionsPage,
    SettingsPage,
    TeamPage,
    TrailItem,
    build_funds,
    build_history,
    build_pool,
    build_positions,
    build_settings,
    build_signal,
    build_team,
)

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


def host_ok(host_header: str | None, allow: frozenset[str] = frozenset()) -> bool:
    """浏览器地址栏里的主机名是不是我们自己的（局域网 IP / localhost / *.local / 白名单）。"""
    if not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8090
        host = host[1 : host.find("]")] if "]" in host else ""
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    if host in allow or host == "localhost" or host.endswith(".local"):
        return True
    return is_lan(host)


class CommandBody(BaseModel):
    kind: CommandKind
    params: dict[str, str] = Field(default_factory=dict)
    password: str | None = Field(default=None, max_length=256)


class SettingsBody(BaseModel):
    section: Section
    changes: dict[str, Any]
    password: str = Field(max_length=256)


class CommandState(BaseModel):
    id: str
    status: Literal["pending", "done", "rejected", "expired", "failed"]
    detail: str = ""


def create_app(
    risk: RiskConfig,
    app_cfg: AppConfig,
    hb: HbReader,
    *,
    clock: Clock = lambda: datetime.now(UTC),
    lan_only: bool = True,
    klines: KlineSource | None = None,
    key_status: dict[str, bool] | None = None,
    config_paths: ConfigPaths | None = None,
    allow_hosts: frozenset[str] = frozenset(),
) -> FastAPI:
    """``key_status``：密钥名 → 配没配，由命令行查好传进来（密钥的值不进面板的数据和接口）。

    ``config_paths``：给了才能在网页上改配置；设置页也按文件里的现值显示（主循环重启后才用上）。
    """
    data_dir = app_cfg.data_dir
    files = PanelFiles(data_dir, risk.day_reset_tz)
    market = klines or KlineSource()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await hb.close()
        await market.close()

    api = FastAPI(title="jiaoyi 面板", docs_url=None, redoc_url=None, lifespan=lifespan)

    @api.middleware("http")
    async def lan_guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]):  # type: ignore[no-untyped-def]
        host = request.client.host if request.client else None
        if lan_only and not is_lan(host):
            return JSONResponse({"detail": "面板只对局域网开放"}, status_code=403)
        if lan_only and not host_ok(request.headers.get("host"), allow_hosts):
            return JSONResponse({"detail": "地址不在面板白名单里"}, status_code=403)
        if request.method not in ("GET", "HEAD") and not _same_origin(request):
            return JSONResponse({"detail": "写操作只能从面板页面发起"}, status_code=403)
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    @api.get("/api/overview")
    async def overview() -> Overview:
        ov = build_overview(files, await hb.snapshot(), risk, app_cfg, clock())
        return ov.model_copy(update={"control": actions.control_state(data_dir, config_paths)})

    @api.get("/api/positions")
    async def positions() -> PositionsPage:
        return build_positions(files, await hb.snapshot(), risk, clock())

    @api.get("/api/team")
    async def team(role: Role | None = None) -> TeamPage:
        return build_team(files, clock(), role)

    @api.get("/api/history")
    async def history(range: HistoryRange = "today") -> HistoryPage:
        return build_history(files, risk, clock(), range)

    @api.get("/api/funds")
    async def funds() -> FundsPage:
        return build_funds(files, await hb.snapshot(), risk, clock())

    @api.get("/api/pool")
    async def pool() -> PoolPage:
        return build_pool(files)

    @api.get("/api/settings")
    async def settings() -> SettingsPage:
        r, a, error = risk, app_cfg, None
        if config_paths:
            try:
                r, a, _ = actions.load_configs(config_paths)
            except ConfigError as e:
                error = str(e)
        page = build_settings(files, r, a, key_status or {})
        return page.model_copy(
            update={
                "writable": config_paths is not None and error is None,
                "file_error": error,
                "changes": actions.recent_changes(data_dir),
                "app_editable": sorted(actions.APP_EDITABLE),
            }
        )

    @api.post("/api/settings")
    async def edit_settings(body: SettingsBody) -> dict[str, Any]:
        if config_paths is None:
            raise HTTPException(status_code=400, detail="面板启动时没给配置文件路径，只能看不能改")
        try:
            changed = actions.update_config(
                config_paths, data_dir, body.section, body.changes, body.password, clock()
            )
        except ActionError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"changed": changed, "pending": actions.config_pending(config_paths, data_dir)}

    @api.post("/api/command")
    async def command(body: CommandBody) -> CommandState:
        try:
            cid = actions.send_command(data_dir, body.kind, body.params, clock(), body.password)
        except (ActionError, ValueError) as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return CommandState(id=cid, status="pending")

    @api.get("/api/command/{command_id}")
    async def command_state(command_id: str) -> CommandState:
        try:
            res = actions.command_status(data_dir, command_id)
        except ActionError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if res is None:
            return CommandState(id=command_id, status="pending")
        return CommandState(id=command_id, status=res.status, detail=res.detail)

    @api.get("/api/signal/{signal_id}")
    async def signal(signal_id: str) -> list[TrailItem]:
        return build_signal(files, clock(), signal_id)

    @api.get("/api/klines")
    async def klines_(symbol: str, interval: Interval = "1m") -> list[dict[str, float]]:
        try:
            return await market.klines(symbol, interval)
        except KlineError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    @api.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    api.mount("/static", StaticFiles(directory=STATIC), name="static")
    return api


def _same_origin(request: Request) -> bool:
    """跨站页面能让浏览器发 POST，但带不了自定义头，也改不了 Origin。两样都查。"""
    if request.headers.get("x-jiaoyi-panel") != "1":
        return False
    origin = request.headers.get("origin")
    host = request.headers.get("host")
    if not origin or not host:
        return False
    return origin in (f"http://{host}", f"https://{host}")
