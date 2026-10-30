"""面板的 HTTP 服务（FastAPI）。``jiaoyi panel`` 启动，Mac mini 上用局域网地址打开。

- 只接受本机和局域网地址（私有网段、回环、链路本地）来的请求，其余一律 403。
- Host 头也要是局域网 IP、localhost、``*.local`` 或 ``--allow-host`` 给的名字，挡 DNS 重绑定。
- 写操作（POST）另要 ``Origin`` 和 Host 一致、带 ``X-Jiaoyi-Panel: 1`` 头（别的网站发不出来），
  具体动作和密码规则见 ``jiaoyi.panel.actions``。面板只写指令文件和配置文件，自己不下单。
- ``/setup`` 首次设置向导（密钥、面板密码、基本配置、自检）只对本机开放，
  规则见 ``jiaoyi.panel.setup``。
- 页面是 ``static/`` 下的静态文件，数据走 ``/api/*``；换皮只动 ``static/theme.css``。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import threading
import time as time_mod
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime, time, timedelta
from functools import partial
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from jiaoyi.contracts import Alert, AlertLevel, AppConfig, RiskConfig, Role
from jiaoyi.core.commands import CommandKind
from jiaoyi.core.config import ConfigError
from jiaoyi.core.recorder import local_day
from jiaoyi.execution.live_gate import CONFIRM_WORD
from jiaoyi.gate.bars import OutcomeBars
from jiaoyi.market.rest import RateLimited, WeightBudget
from jiaoyi.panel import actions, pending_config, pnl_curve, setup
from jiaoyi.panel.actions import ActionError, ConfigPaths, Section
from jiaoyi.panel.card_history import CardHistoryPage, build_card_history
from jiaoyi.panel.files import PanelFiles, read_live_gate
from jiaoyi.panel.hb import HbReader
from jiaoyi.panel.live import LiveFeed, LiveSource
from jiaoyi.panel.live_price import DEMO_WS_BASE, WS_BASE, LivePrices, merge_live
from jiaoyi.panel.market import BASE, DEMO_BASE, Interval, KlineError, KlineSource
from jiaoyi.panel.meetings import Meeting, read_meeting
from jiaoyi.panel.overview import (
    Overview,
    PositionView,
    PriceSrc,
    build_overview,
    compute_kpis,
)
from jiaoyi.panel.pages import (
    AiRange,
    FundsPage,
    HistoryPage,
    HistoryRange,
    PoolPage,
    PositionsPage,
    SettingsPage,
    TeamPage,
    TradeRow,
    TrailItem,
    build_funds,
    build_history,
    build_pool,
    build_positions,
    build_settings,
    build_signal,
    build_team,
    prompts_info,
    recent_trades,
    switched_off_roles,
)
from jiaoyi.panel.stop_diagnostics import parse_diagnostics
from jiaoyi.panel.updater import UpdateInfo, Updater
from jiaoyi.risk.guard import blend_atr, session_factor
from jiaoyi.slow import binance_gate, card_score, market_score
from jiaoyi.slow.credits import banner as credit_banner
from jiaoyi.slow.settings import slow_defaults
from jiaoyi.watchdog import send as send_telegram

log = logging.getLogger(__name__)

STATIC = Path(__file__).parent / "static"
RECENT_TTL_S = 10.0
BOOK_QUOTE_WAIT_S = 1.0
PACKAGE = Path(__file__).parent.parent
CARD_SCORE_FIRST_S = 120.0
"""面板启动后多久第一次算方向记分（别和启动时的其他读取挤在一起）。"""
CARD_SCORE_IDLE_MIN = 10
"""设置成 0（不算）时，隔多久再看一眼设置。"""
CARD_SCORE_LOOKBACK = timedelta(days=7)
"""面板只补算最近几天的卡；更早的用 ``jiaoyi card-score --days`` 补。"""
CARD_SCORE_WEIGHT_PER_MIN = 120
"""方向记分打币安 API 的权重预算（每分钟，币安按 IP 给 2400；大头留给交易程序）。"""
MARKET_SCORE_FIRST_S = CARD_SCORE_FIRST_S
MARKET_SCORE_EVERY_MIN = 30
"""大盘台记分（``data/market_desk/score.json``）多久算一次；只在大盘台有日志时算。"""
MARKET_SCORE_GAP_S = 2.5
"""大盘台记分两次币安请求之间至少隔几秒（每次最多 5 权重，约 120 权重 / 分钟）。"""


def in_daemon_thread(fn: Callable[[], Any]) -> asyncio.Future[Any]:
    """在守护线程里跑同步的 ``fn``，结果交回事件循环。不用默认线程池：面板关闭时
    ``asyncio.run`` 会等池里的线程跑完，一轮记分卡在请求上就会拖住重启。"""
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[Any] = loop.create_future()

    def settle(value: Any, exc: BaseException | None) -> None:
        if fut.done():
            return
        if exc is not None:
            fut.set_exception(exc)
        else:
            fut.set_result(value)

    def work() -> None:
        try:
            value, exc = fn(), None
        except BaseException as e:  # 交回事件循环那边处理
            value, exc = None, e
        with suppress(RuntimeError):  # 事件循环已经关了：没人等结果
            loop.call_soon_threadsafe(settle, value, exc)

    threading.Thread(target=work, name="market-score", daemon=True).start()
    return fut


def code_fingerprint(root: Path = PACKAGE) -> str:
    """jiaoyi 包下源码和静态文件的 (路径, 修改时间, 大小) 指纹；只 stat 不读内容，几毫秒。"""
    h = hashlib.sha256()
    for f in sorted(root.rglob("*")):
        if f.suffix in (".py", ".js", ".css", ".html") and "__pycache__" not in f.parts:
            st = f.stat()
            h.update(f"{f.relative_to(root)}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    return h.hexdigest()[:16]


def asset_version(name: str) -> str:
    st = (STATIC / name).stat()
    return f"{st.st_mtime_ns:x}{st.st_size:x}"


def index_page() -> HTMLResponse:
    """首页：app.js / app.css 带上文件指纹当版本号，改了文件浏览器就不会继续用缓存里的旧版。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "app.css"):
        html = html.replace(f'/static/{name}"', f'/static/{name}?v={asset_version(name)}"')
    return HTMLResponse(html)


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


class ModeBody(BaseModel):
    mode: Literal["paper", "testnet", "live"]
    password: str | None = Field(default=None, max_length=256)
    confirm_word: str | None = Field(default=None, max_length=64)
    acknowledged: bool = False
    """切实盘：提示已逐条勾过。"""


class UpdateBody(BaseModel):
    password: str | None = Field(default=None, max_length=256)


class CardAtr(BaseModel):
    symbol: str
    price: float
    """现价（1 分钟线最后一根收盘）。"""
    atr: float
    """风控员止损用的 ATR（价格单位）：止损距离 = 作战卡倍数 × 它。"""
    atr_short: float
    atr_long: float | None
    period_min: int
    long_period_min: int
    session_factor: float
    stop_atr_clamp: tuple[float, float] | None = None
    """风控员把作战卡止损倍数夹到这个范围（``card_range_clamp`` 开着时）；None = 不夹。"""
    tp_r_clamp: tuple[float, float] | None = None
    tp_r_cap: float | None = None
    """``fast_loop.tp_r_cap`` when set: the guard orders at most this R (fee floor not modelled)."""


class ModeInfo(BaseModel):
    mode: str
    """app.toml 里写的模式（主程序重启后才用上）。"""
    armed: bool
    local: bool
    """这次请求来自 Mac 本机；切实盘只在本机开放。"""
    confirm_word: str
    cautions: dict[str, list[str]]
    """目标模式 → 切过去前要逐条看过的提示。"""


class PasswordBody(BaseModel):
    password: str = Field(max_length=256)


class SetupPasswordBody(BaseModel):
    password: str = Field(max_length=256)
    confirm: str = Field(max_length=256)


class SetupKeysBody(BaseModel):
    values: dict[str, str] = Field(max_length=32)
    password: str = Field(max_length=256)


class SetupBasicBody(BaseModel):
    values: dict[str, Any]
    password: str = Field(max_length=256)


class SetupCheckBody(BaseModel):
    password: str = Field(max_length=256)


class CommandState(BaseModel):
    id: str
    status: Literal["pending", "done", "queued", "rejected", "expired", "failed"]
    detail: str = ""


def create_app(
    risk: RiskConfig,
    app_cfg: AppConfig,
    hb: HbReader,
    *,
    clock: Clock = lambda: datetime.now(UTC),
    lan_only: bool = True,
    klines: KlineSource | None = None,
    live_prices: LivePrices | None = None,
    key_status: dict[str, bool] | None = None,
    config_paths: ConfigPaths | None = None,
    allow_hosts: frozenset[str] = frozenset(),
    secret_writer: setup.SecretWriter | None = None,
    secret_reader: setup.SecretReader | None = None,
    pnl_sample_s: float | None = None,
    update_repo: Path | None = None,
) -> FastAPI:
    """``key_status``：密钥名 → 配没配，由命令行查好传进来（密钥的值不进面板的数据和接口）。

    ``config_paths``：给了才能在网页上改配置；设置页也按文件里的现值显示（主循环重启后才用上）。

    ``secret_writer`` / ``secret_reader``：首次设置向导写钥匙串、自检时读 Telegram 密钥用；
    不给就只能看不能写（例如不是 Mac）。

    ``pnl_sample_s``：今日盈亏曲线多久记一个点（None = 每分钟，0 = 不记）。

    ``update_repo``：源码目录；给了设置页才有「更新到最新版」（在这个目录跑 ``make update``）。
    """
    data_dir = app_cfg.data_dir
    files = PanelFiles(data_dir, risk.day_reset_tz)
    # 历史页要读取管家采样；独立缓存及线程锁避免大量 JSON 解析阻塞主事件循环。
    history_files = PanelFiles(data_dir, risk.day_reset_tz)
    history_lock = threading.Lock()
    live = actions.LiveConfigs(risk, app_cfg, config_paths)
    for f in (files, history_files):
        f.paper_book_pnl = lambda: live.get()[1].fast_loop.paper_stop_book_pnl

    def cfg() -> tuple[RiskConfig, AppConfig]:
        """配置文件改了就用新的（data_dir 例外：换目录要重启面板）。"""
        r, a = live.get()
        files.tz = r.day_reset_tz
        return r, a

    keys = key_status if key_status is not None else {}
    boot_code = code_fingerprint()
    market = klines or KlineSource()

    QUOTE_BASES = {"live": (BASE, WS_BASE), "demo": (DEMO_BASE, DEMO_WS_BASE)}
    quote_srcs: dict[str, tuple[KlineSource, LivePrices]] = {}

    def quote_env() -> Literal["live", "demo"] | None:
        """持仓在哪个盘子撮合：live 主网；paper / testnet 按 sim_environment（旧测试网不接）。"""
        a = cfg()[1]
        if a.mode.value == "live":
            return "live"
        return "demo" if a.sim_environment == "demo" else None

    def quotes() -> tuple[KlineSource, LivePrices] | None:
        """当前模式的行情源（持仓现价、持仓页 K 线）；测试注入的源不分模式。"""
        env = quote_env()
        if env is None:
            return None
        if klines is not None or live_prices is not None:
            if "test" not in quote_srcs:  # 只缓存一份，别每次都起新的重连任务
                quote_srcs["test"] = (market, live_prices or LivePrices("ws://127.0.0.1:9"))
            return quote_srcs["test"]
        if env not in quote_srcs:
            rest, ws = QUOTE_BASES[env]
            quote_srcs[env] = (market if rest == BASE else KlineSource(rest), LivePrices(ws))
        for other, (_, lp) in quote_srcs.items():
            if other != env:
                lp.watch(())  # 换了模式：另一边的推送不再订
        return quote_srcs[env]

    async def last_prices() -> dict[str, tuple[float, PriceSrc]]:
        """持仓币的现价，取持仓实际撮合的盘子（live 主网 / paper、testnet 币安 Demo）：推送优先；
        推送断了的币退回一次全量 ticker（2 秒缓存，限流时不发）；都拿不到的不放进来，
        持仓那边退回 hummingbot 的浮盈（标记价），不拿别的盘子的价顶。"""
        held = {p.symbol for p in files.ledger().live()}
        q = quotes()
        if q is None:
            return {}
        rest_src, ticks = q
        ticks.watch(held)
        out: dict[str, tuple[float, PriceSrc]] = {}
        for s in held:
            px = ticks.price(s)
            if px:
                out[s] = (px, "push")
        if held - out.keys():
            try:
                rest = await asyncio.wait_for(rest_src.last_prices(), 3.0)
            except (KlineError, TimeoutError):
                rest = {}
            for s in held - out.keys():
                if rest.get(s):
                    out[s] = (rest[s], "rest")
        return out

    async def sample_pnl() -> None:
        """今日盈亏曲线：每分钟记一个点（只记 hummingbot-api 拿得到总资产的时候）。"""
        while True:
            await asyncio.sleep(pnl_sample_s or pnl_curve.SAMPLE_EVERY_S)
            try:
                r = cfg()[0]
                now = clock()
                k = compute_kpis(files, files.ledger(), await hb.snapshot(), r, now)
                if k.equity_source == "hummingbot" and k.equity_usdt and k.day_start_equity_usdt:
                    day = local_day(now, r.day_reset_tz).isoformat()
                    pnl_curve.record(data_dir, day, now, k.equity_usdt, k.day_start_equity_usdt)
            except Exception:
                log.warning("记今日盈亏曲线失败", exc_info=True)

    packs = Path(data_dir) / "history"
    # 之后的总盘记分也在面板里跑时共用这一个（同一份日包缓存、同一个权重预算）
    score_fetch = OutcomeBars(cache=packs, budget=WeightBudget(CARD_SCORE_WEIGHT_PER_MIN))

    async def score_cards() -> None:
        """作战卡方向记分：每 ``card_score_every_min`` 分钟补算窗口走完的卡（只读主网公开 K 线，
        过去的整天走日包缓存），重写 data/card_score/summary.json。0 = 不算。"""
        await asyncio.sleep(CARD_SCORE_FIRST_S)
        while True:
            a = cfg()[1]
            every = a.card_score_every_min
            if every:
                now = clock()
                window = a.card_score_window_days
                try:
                    res = await card_score.update(
                        data_dir,
                        now,
                        score_fetch,
                        since=now - CARD_SCORE_LOOKBACK,
                        summary_since=now - timedelta(days=window) if window else None,
                        packs=packs,
                    )
                    log.info("%s（这次新记 %d 张）", card_score.headline(res.report), res.new)
                except RateLimited as e:
                    log.warning("作战卡方向记分：%s，这一轮作罢", e)
                except Exception:
                    log.warning("作战卡方向记分失败", exc_info=True)
            await asyncio.sleep((every or CARD_SCORE_IDLE_MIN) * 60)

    desk_stop = threading.Event()
    desk_get = market_score.paced(
        market_score.stoppable(binance_gate.get, desk_stop), MARKET_SCORE_GAP_S, desk_stop.wait
    )

    async def score_desk() -> None:
        """大盘台记分：每 ``MARKET_SCORE_EVERY_MIN`` 分钟重算最近 7 天（K 线有缓存，只补新的）。
        面板关闭时设 ``desk_stop``：正在跑的一轮在下一次请求前停下，线程是守护线程，不拖住退出。"""
        await asyncio.sleep(MARKET_SCORE_FIRST_S)
        while not desk_stop.is_set():
            if (Path(data_dir) / "market_desk").is_dir():
                job = partial(market_score.update, data_dir, clock(), get=desk_get, stop=desk_stop)
                try:
                    rep = await in_daemon_thread(job)
                    log.info("大盘台记分：%s", rep["panel"]["text"])
                except market_score.Stopped:
                    return
                except RateLimited as e:
                    log.warning("大盘台记分：%s，这一轮作罢", e)
                except Exception:
                    log.warning("大盘台记分失败", exc_info=True)
            await asyncio.sleep(MARKET_SCORE_EVERY_MIN * 60)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(sample_pnl()) if pnl_sample_s != 0 else None
        scorer = asyncio.create_task(score_cards())
        desk_scorer = asyncio.create_task(score_desk())
        yield
        scorer.cancel()
        desk_stop.set()
        desk_scorer.cancel()
        if task is not None:
            task.cancel()
        await hb.close()
        await market.close()
        for rest_src, lp in quote_srcs.values():
            await lp.close()
            if rest_src is not market:
                await rest_src.close()

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
        r, a = cfg()
        ov = build_overview(files, await hb.snapshot(), r, a, clock(), await last_prices())
        ctl = actions.control_state(data_dir, config_paths)
        ctl.panel_stale = code_fingerprint() != boot_code
        ctl.ui_version = asset_version("app.js")
        ctl.llm_credit = credit_banner(files.llm_credits(), clock())
        return ov.model_copy(update={"control": ctl})

    # 持仓页 2 秒拉一次，最近交易要按历史页口径读管家采样，每轮都读太重：有仓位平掉（持仓集合变了）
    # 或换了日子马上重算，否则最多旧 RECENT_TTL_S 秒（平仓价、手续费回填会晚一点到）
    recent_cache: list[tuple[tuple[frozenset[str], date], float, tuple[list[TradeRow], int]]] = []

    async def recent_for(live_ids: frozenset[str], now: datetime) -> tuple[list[TradeRow], int]:
        r = cfg()[0]
        key = (live_ids, local_day(now, r.day_reset_tz))
        at = time_mod.monotonic()
        if recent_cache and recent_cache[0][0] == key and at - recent_cache[0][1] < RECENT_TTL_S:
            return recent_cache[0][2]

        def read() -> tuple[list[TradeRow], int]:
            with history_lock:
                return recent_trades(history_files, r, now)

        value = await asyncio.to_thread(read)
        recent_cache[:] = [(key, at, value)]
        return value

    @api.get("/api/positions")
    async def positions() -> PositionsPage:
        now = clock()
        page = build_positions(
            files,
            await hb.snapshot(),
            cfg()[0],
            now,
            await last_prices(),
            cfg()[1].protection_stale_s,
            with_recent=False,
        )
        recent, recent_total = await recent_for(
            frozenset(v.position_id for v in page.positions), now
        )
        page = page.model_copy(update={"recent": recent, "recent_total": recent_total})
        quote_source = quotes()

        async def with_trail(v: PositionView) -> PositionView:
            # 交易所托管保护的新仓不依赖 hummingbot 的止损诊断（可能根本没部署）
            diag = None
            if v.protection is None:
                raw = await hb.stop_diagnostics(v.executor_id)
                diag = parse_diagnostics(raw, v.executor_id, clock(), v.opened_at)
            book = None
            if quote_source:
                with suppress(KlineError, TimeoutError, ValueError):
                    # 参考盘口慢了就这一轮不带，别让整页（平仓、现价）跟着等 REST 超时
                    book = await asyncio.wait_for(
                        quote_source[0].book_quote(v.symbol), BOOK_QUOTE_WAIT_S
                    )
            return v.model_copy(
                update={"trail": None, "stop_diagnostics": diag, "book_quote": book}
            )

        views = list(await asyncio.gather(*(with_trail(v) for v in page.positions)))
        return page.model_copy(update={"price_env": quote_env(), "positions": views})

    @api.get("/api/team")
    async def team(role: str | None = None) -> TeamPage:
        ac = cfg()[1]
        fl = ac.fast_loop
        th = {
            "min_p_genuine": fl.min_p_genuine,
            "min_p_take": fl.min_p_take,
            "min_p_direction": fl.min_p_direction,
            "max_p_toxic": fl.max_p_toxic,
        }
        schedule = None
        with suppress(Exception):
            assert config_paths is not None
            desk = actions.load_configs(config_paths)[2].market_desk
            schedule = (desk.mode, desk.cadence_min, desk.offset_min)
        desk_selected = role == "desk"
        try:
            picked = None if desk_selected or role is None else Role(role)
        except ValueError:
            raise HTTPException(status_code=422, detail="未知角色") from None
        page = build_team(
            files, clock(), picked, ac.predictor.mode, th,
            desk_schedule=schedule, desk_selected=desk_selected,
            gate_take_by_kind={k.value: v for k, v in fl.min_p_take_by_kind.items()},
            switched_off=switched_off_roles(ac),
        )  # fmt: skip
        return page.model_copy(update={"prompts": prompts_info(files)})

    live_src = LiveSource(files)

    @api.get("/api/team/live")
    async def team_live(cursor: str | None = None) -> LiveFeed:
        """团队页每 1–2 秒问一次：游标之后新写的决策记录（翻成动作）+ 在管的持仓。"""
        return await asyncio.to_thread(live_src.feed, clock(), cursor)

    @api.get("/api/prompts")
    async def prompts() -> dict[str, Any]:
        """军师团现在生效的提示词原文（data/prompts_active.json），只读。"""
        got = files.prompts_active()
        if got is None:
            raise HTTPException(404, "还没有提示词记录：军师团引擎启动后才写")
        return got[0]

    @api.get("/api/meeting/{symbol}")
    async def meeting(symbol: str) -> Meeting:
        m = read_meeting(Path(data_dir), symbol, clock())
        if m is None:
            raise HTTPException(404, "这个币还没有开会记录")
        return m

    @api.get("/api/card-history")
    async def card_history(day: date | None = None, symbol: str | None = None) -> CardHistoryPage:
        return build_card_history(files, clock(), day, symbol)

    @api.get("/api/card-score")
    async def card_score_summary() -> card_score.Report:
        """作战卡方向记分的汇总（data/card_score/summary.json），只读。"""
        got = await asyncio.to_thread(card_score.ScoreStore(data_dir).read_summary)
        if got is None:
            raise HTTPException(404, "方向记分还没算过：面板启动几分钟后自动算（设置 0 = 不算）")
        return got

    @api.get("/api/history")
    async def history(range: HistoryRange = "today", tests: bool = False) -> HistoryPage:
        current_risk, now = cfg()[0], clock()

        def read_history() -> HistoryPage:
            with history_lock:
                return build_history(history_files, current_risk, now, range, include_tests=tests)

        return await asyncio.to_thread(read_history)

    @api.get("/api/pnl-curve")
    async def pnl_curve_today() -> pnl_curve.PnlCurve:
        r = cfg()[0]
        now = clock()
        k = compute_kpis(files, files.ledger(), await hb.snapshot(), r, now)
        cur = (
            pnl_curve.PnlPoint(ts=now, pnl_usdt=k.day_pnl_usdt)
            if k.day_pnl_usdt is not None
            else None
        )
        day = local_day(now, r.day_reset_tz).isoformat()
        return pnl_curve.read(
            data_dir, day, k.day_start_equity_usdt, cur, await pnl_est(day, r.day_reset_tz)
        )

    async def pnl_est(day: str, tz: str) -> list[pnl_curve.PnlPoint] | None:
        """面板开始记之前那段（0 点 → 第一个记录点）的估算，算一次存下来；K 线拿不到就先不画。"""
        est = pnl_curve.load_est(data_dir, day)
        first = pnl_curve.first_recorded(data_dir, day)
        if est is not None or first is None:
            return est
        since = datetime.combine(date.fromisoformat(day), time(), ZoneInfo(tz))
        if first - since <= pnl_curve.EST_STEP * 2:
            return None
        try:
            ledger = files.ledger()  # Demo's fills: the recorded points are Demo equity
            est = await pnl_curve.backfill(
                [ledger.demo(p) for p in ledger.positions],
                since,
                first,
                lambda s: market.klines(s, "5m", 300),
            )
        except KlineError as e:
            log.info("今日盈亏曲线回填拿不到 K 线：%s", e)
            return None
        pnl_curve.save_est(data_dir, day, est)
        return est

    @api.get("/api/funds")
    async def funds(ai: AiRange = "today") -> FundsPage:
        return build_funds(files, await hb.snapshot(), cfg()[0], clock(), ai)

    @api.get("/api/pool")
    async def pool() -> PoolPage:
        if not config_paths:
            return build_pool(files, slow_defaults(cfg()[1].profile).screener)
        try:
            return build_pool(files, actions.load_configs(config_paths)[2].screener)
        except ConfigError as e:
            return build_pool(files, None, str(e))

    @api.get("/api/settings")
    async def settings() -> SettingsPage:
        r, a = cfg()
        sl, error, prof = None, None, None
        if config_paths:
            try:
                r, a, sl = actions.load_configs(config_paths)
            except ConfigError as e:
                error = str(e)
            prof = actions.profile_info(config_paths)
        page = build_settings(files, r, a, keys, sl, prof, (await hb.snapshot()).equity_usdt)
        return page.model_copy(
            update={
                "writable": config_paths is not None and error is None,
                "file_error": error,
                "changes": actions.recent_changes(data_dir),
                "prompts": prompts_info(files),
                "pending_config": pending_config.notice(config_paths, data_dir)
                if config_paths
                else None,
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

    @api.post("/api/settings/clear-profile-pins")
    async def clear_profile_pins(body: PasswordBody) -> dict[str, Any]:
        if config_paths is None:
            raise HTTPException(status_code=400, detail="面板启动时没给配置文件路径，只能看不能改")
        try:
            changed = actions.clear_profile_pins(config_paths, data_dir, body.password, clock())
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

    @api.get("/api/price-decimals")
    async def price_decimals() -> dict[str, int]:
        try:
            return await market.price_decimals()
        except KlineError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e

    @api.get("/api/klines")
    async def klines_(symbol: str, interval: Interval = "1m") -> list[dict[str, float]]:
        try:
            q = quotes()
            bars = await (q[0] if q else market).klines(symbol, interval)
        except KlineError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        # 最后一根接上推送，和持仓表现价同一个数
        return merge_live(bars, q[1].bar(symbol), interval) if q else bars

    @api.get("/api/card-atr")
    async def card_atr(symbol: str) -> CardAtr:
        """作战卡按风控员口径估止损：√(短 ATR × 长 ATR) × 曼谷时段系数（不含 7 天兜底）。"""
        fl = cfg()[1].fast_loop
        try:
            short = await market.atr(symbol, fl.atr_period_min)
            long_ = (
                await market.atr(symbol, fl.atr_long_period_min) if fl.atr_long_period_min else None
            )
            bars = await market.klines(symbol, "1m")
        except KlineError as e:
            raise HTTPException(status_code=502, detail=str(e)) from e
        if not short or not bars:
            raise HTTPException(status_code=502, detail="K 线不够，算不出 ATR")
        k = session_factor(fl, clock())
        return CardAtr(
            symbol=symbol,
            price=bars[-1]["close"],
            atr=blend_atr(short, long_, fl) * k,
            atr_short=short,
            atr_long=long_,
            period_min=fl.atr_period_min,
            long_period_min=fl.atr_long_period_min,
            session_factor=k,
            stop_atr_clamp=(fl.min_stop_atr, fl.max_stop_atr) if fl.card_range_clamp else None,
            tp_r_clamp=(fl.min_tp_r, fl.max_tp_r) if fl.card_range_clamp else None,
            tp_r_cap=fl.tp_r_cap or None,
        )

    def local(request: Request) -> bool:
        return setup.is_local(
            request.client.host if request.client else None, request.headers.get("host")
        )

    def local_only(request: Request) -> None:
        if not local(request):
            raise HTTPException(
                status_code=403, detail="首次设置只能在 Mac 本机打开 http://127.0.0.1:8090/setup"
            )

    def mode_paths() -> ConfigPaths:
        if config_paths is None:
            raise HTTPException(status_code=400, detail="面板启动时没给配置文件路径，只能看不能改")
        return config_paths

    updater = Updater(update_repo, data_dir)

    @api.get("/api/update")
    async def update_get() -> UpdateInfo:
        return await asyncio.to_thread(updater.info)

    @api.post("/api/update")
    async def update_post(body: UpdateBody) -> UpdateInfo:
        try:
            actions.check_password(data_dir, body.password or "")
        except ActionError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        try:
            await asyncio.to_thread(updater.start, clock())
        except RuntimeError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return await asyncio.to_thread(updater.info, False)

    @api.get("/api/mode")
    async def mode_get(request: Request) -> ModeInfo:
        paths = mode_paths()
        try:
            r, a, sl = actions.load_configs(paths)
        except ConfigError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        eq = (await hb.snapshot()).equity_usdt
        jev, llm = a.fast_loop.jev_provider, sl.llm.provider
        return ModeInfo(
            mode=actions.current_mode(paths),
            armed=read_live_gate(files.root)[1],
            local=local(request),
            confirm_word=CONFIRM_WORD,
            cautions={m: actions.mode_cautions(m, r, keys, jev, llm, eq) for m in actions.MODES},
        )

    @api.post("/api/mode")
    async def mode_post(request: Request, body: ModeBody) -> actions.ModeSwitch:
        paths = mode_paths()
        if body.mode == "live" and not local(request):
            raise HTTPException(
                status_code=403, detail="切实盘只能在 Mac 本机打开 http://127.0.0.1:8090 操作"
            )
        if body.mode == "live" and not body.acknowledged:
            raise HTTPException(status_code=400, detail="先把提示逐条勾上")
        now = clock()
        try:
            res = actions.switch_mode(
                paths, data_dir, body.mode, body.password, body.confirm_word, now
            )
        except ActionError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        read = secret_reader or (lambda _: None)
        token, chat = read("TELEGRAM_BOT_TOKEN"), read("TELEGRAM_CHAT_ID")
        if token and chat:
            name = actions.MODE_NAME
            if res.new == "live":
                title, body_text = "实盘已解锁", "面板上切到实盘，主程序重启后真连币安主网下单。"
            else:
                title = f"交易模式切到{name[res.new]}"
                body_text = f"面板上从{name.get(res.old, res.old)}切到{name[res.new]}，实盘已上锁。"
            alert = Alert(
                ts=now, level=AlertLevel.CRITICAL, role=Role.EXECUTOR, title=title, body=body_text
            )
            sent = await asyncio.to_thread(send_telegram, token, chat, alert, None)
            res = res.model_copy(update={"notified": sent})
        return res

    def setup_state(request: Request) -> setup.SetupState:
        return setup.setup_state(
            data_dir, keys, config_paths, local=local(request), keychain=secret_writer is not None
        )

    @api.get("/api/setup")
    async def setup_get(request: Request) -> setup.SetupState:
        return setup_state(request)

    @api.post("/api/setup/password")
    async def setup_password(request: Request, body: SetupPasswordBody) -> setup.SetupState:
        local_only(request)
        try:
            setup.set_password(data_dir, body.password, body.confirm)
        except setup.SetupError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return setup_state(request)

    @api.post("/api/setup/keys")
    async def setup_keys(request: Request, body: SetupKeysBody) -> dict[str, Any]:
        local_only(request)
        if secret_writer is None:
            raise HTTPException(
                status_code=400, detail="这台机器写不了钥匙串；请在终端运行 jiaoyi setup-keys"
            )
        try:
            written = setup.write_keys(data_dir, body.values, body.password, secret_writer, keys)
        except setup.SetupError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        if "HUMMINGBOT_API_PASSWORD" in written:
            hb.update_password(body.values["HUMMINGBOT_API_PASSWORD"].strip())
        return {"written": written, "state": setup_state(request)}

    @api.post("/api/setup/basic")
    async def setup_basic(request: Request, body: SetupBasicBody) -> dict[str, Any]:
        local_only(request)
        if config_paths is None:
            raise HTTPException(status_code=400, detail="面板启动时没给配置文件路径")
        try:
            changed = setup.write_basic(config_paths, data_dir, body.values, body.password, clock())
        except setup.SetupError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return {"changed": changed, "state": setup_state(request)}

    @api.post("/api/setup/check")
    async def setup_check(request: Request, body: SetupCheckBody) -> list[setup.CheckItem]:
        local_only(request)
        try:  # 会发一条 Telegram：也要密码，免得被刷
            actions.check_password(data_dir, body.password)
        except ActionError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        return await setup.self_check(config_paths, hb, secret_reader or (lambda _: None), clock())

    @api.get("/", response_model=None)
    async def index(request: Request) -> HTMLResponse | RedirectResponse:
        # 本机打开、还没配齐：直接进首次设置。局域网打开照常进总览（设置页会提示）
        if local(request) and not setup_state(request).done:
            return RedirectResponse("/setup", status_code=303)
        return index_page()

    @api.get("/setup")
    async def setup_page() -> HTMLResponse:
        return index_page()

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
