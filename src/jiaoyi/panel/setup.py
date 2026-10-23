"""首次设置向导（``/setup``）：换一台 Mac 时不用敲命令，在面板上把密钥、面板密码、基本配置配齐。

安全规则（比面板其余部分更严）：
- 整个向导的写操作只接受**本机**请求：TCP 来源是回环地址，且浏览器地址栏是 localhost / 127.0.0.1。
  用局域网 IP 打开只能看到“请在 Mac 本机打开”，看不到哪些密钥配了以外的任何东西。
- 密钥只写不读：接口只回“配没配”，从不回显值；写进 macOS 钥匙串（值走标准输入，不进进程参数）。
- 第一步设面板密码（只能在还没设过时用；改密码走命令行）。之后写密钥、改配置都要这个密码，
  错 5 次锁 15 分钟，和命令行共用计数。
- POST 照样过面板的 Host 白名单和 Origin / ``X-Jiaoyi-Panel`` 校验。
- 面板进程会在自检时读 Telegram token 发一条测试消息；值只用于这次请求，不进返回内容和日志。
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from jiaoyi.contracts import Alert, AlertLevel, Role
from jiaoyi.core.config import ConfigError
from jiaoyi.core.secrets import OPTIONAL_KEYS, REQUIRED_KEYS, SecretError, check_secret_value
from jiaoyi.execution.live_gate import LiveGate, LiveGateError  # 只用密码；不碰交易所
from jiaoyi.panel import actions
from jiaoyi.panel.actions import ActionError, ConfigPaths, FieldSpec
from jiaoyi.panel.files import read_live_gate
from jiaoyi.panel.hb import HbReader
from jiaoyi.watchdog import send as send_telegram

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

SecretWriter = Callable[[str, str], None]
SecretReader = Callable[[str], str | None]

# 第三步“基本配置”能改的项：(面板段, 键, 可选值)
_BASIC: dict[str, tuple[actions.Section, FieldSpec]] = {
    "profile": ("app", FieldSpec(str, choices=("24h", "12h", "6h", "2h", "60m", "30m", "15m"))),
    "jev_provider": ("fast_loop", FieldSpec(str, choices=("typesafe", "openrouter"))),
    "llm_provider": ("llm", FieldSpec(str, choices=("deepseek", "openrouter"))),
    "daily_cost_limit_usd": ("budget", FieldSpec(float)),
}
_BASIC_KEY = {"llm_provider": "provider"}  # 向导里的名字 → 配置文件里的键


class SetupError(Exception):
    """给人看的中文；HTTP 400 原样返回。"""


def is_local(client_host: str | None, host_header: str | None) -> bool:
    """TCP 来源是回环，而且地址栏写的是 localhost / 127.0.0.1（不是局域网 IP）。"""
    try:
        ip = ipaddress.ip_address(client_host or "")
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if not ip.is_loopback or not host_header:
        return False
    host = host_header.strip().lower()
    if host.startswith("["):
        host = host[1 : host.find("]")] if "]" in host else ""
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host in LOCAL_HOSTS


class SetupState(BaseModel):
    local: bool
    """这次请求来自本机；不是本机时下面只给“配没配”，页面只显示“请在 Mac 本机打开”。"""
    password_set: bool
    keys: dict[str, bool]
    required: list[str]
    optional: list[str]
    keychain: bool
    """这台机器能写 macOS 钥匙串。"""
    done: bool
    """面板密码和必填密钥都齐了。"""
    basic: dict[str, Any] = {}
    """第三步的现值（本机才给）。"""
    basic_choices: dict[str, list[object]] = {}
    mode: str | None = None


class CheckItem(BaseModel):
    name: str
    ok: bool
    detail: str


def setup_state(
    data_dir: str | Path,
    keys: dict[str, bool],
    paths: ConfigPaths | None,
    *,
    local: bool,
    keychain: bool,
) -> SetupState:
    password_set, _ = read_live_gate(Path(data_dir))
    shown = {k: bool(keys.get(k)) for k in (*REQUIRED_KEYS, *OPTIONAL_KEYS)}
    done = password_set and all(shown[k] for k in REQUIRED_KEYS)
    state = SetupState(
        local=local,
        password_set=password_set,
        keys=shown,
        required=list(REQUIRED_KEYS),
        optional=list(OPTIONAL_KEYS),
        keychain=keychain,
        done=done,
    )
    if local and paths is not None:
        try:
            risk, app, slow = actions.load_configs(paths)
        except ConfigError:
            return state
        del risk
        state.basic = {
            "profile": app.profile,
            "jev_provider": app.fast_loop.jev_provider,
            "llm_provider": slow.llm.provider,
            "daily_cost_limit_usd": slow.budget.daily_cost_limit_usd,
        }
        state.basic_choices = {k: list(spec.choices) for k, (_, spec) in _BASIC.items()}
        state.mode = app.mode.value
    return state


def set_password(data_dir: str | Path, password: str, confirm: str) -> None:
    """只在还没设过面板密码时可用；改密码请用 ``jiaoyi set-panel-password``（要旧密码）。"""
    gate = LiveGate(Path(data_dir) / "live_gate.json")
    if gate.password_set:
        raise SetupError("面板密码已经设过了；要改请在终端运行 jiaoyi set-panel-password")
    if password != confirm:
        raise SetupError("两次输入的密码不一样")
    try:
        gate.set_password(password)
    except LiveGateError as e:
        raise SetupError(str(e)) from e


def write_keys(
    data_dir: str | Path,
    values: dict[str, str],
    password: str,
    writer: SecretWriter,
    keys: dict[str, bool],
) -> list[str]:
    """先验面板密码、再逐个检查格式，全部合格才开始写。返回写了哪些（只有名字）。"""
    try:
        actions.check_password(data_dir, password)
    except ActionError as e:
        raise SetupError(str(e)) from e
    allowed = (*REQUIRED_KEYS, *OPTIONAL_KEYS)
    todo: dict[str, str] = {}
    for name, value in values.items():
        if name not in allowed:
            raise SetupError(f"{name} 不能在这里设置")
        if not value.strip():
            continue  # 空着 = 这项不改
        try:
            todo[name] = check_secret_value(name, value)
        except SecretError as e:
            raise SetupError(str(e)) from e
    if not todo:
        raise SetupError("没有填任何密钥")
    written: list[str] = []
    for name, value in todo.items():
        try:
            writer(name, value)
        except SecretError as e:
            raise SetupError(f"{e}（之前的 {', '.join(written) or '无'} 已写入）") from e
        keys[name] = True
        written.append(name)
    return written


def write_basic(
    paths: ConfigPaths,
    data_dir: str | Path,
    values: dict[str, Any],
    password: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """第三步：档位、拍板人通道、军师团通道、日花费上限。按配置文件分段写，每段整份校验。"""
    unknown = set(values) - set(_BASIC)
    if unknown:
        raise SetupError(f"不能在这里设置：{', '.join(sorted(unknown))}")
    by_section: dict[actions.Section, dict[str, Any]] = {}
    fields: dict[actions.Section, dict[str, FieldSpec]] = {}
    for name, value in values.items():
        section, spec = _BASIC[name]
        key = _BASIC_KEY.get(name, name)
        by_section.setdefault(section, {})[key] = value
        fields.setdefault(section, {})[key] = spec
    changed: list[dict[str, Any]] = []
    try:
        for section, changes in by_section.items():
            changed += actions.update_config(
                paths, data_dir, section, changes, password, now, fields=fields[section]
            )
    except ActionError as e:
        raise SetupError(str(e)) from e
    return changed


async def self_check(
    paths: ConfigPaths | None,
    hb: HbReader,
    read_secret: SecretReader,
    now: datetime,
    transport: httpx.BaseTransport | None = None,
) -> list[CheckItem]:
    out: list[CheckItem] = []
    if paths is None:
        out.append(CheckItem(name="配置文件", ok=False, detail="面板启动时没给配置文件路径"))
    else:
        try:
            _, app, _ = actions.load_configs(paths)
            out.append(
                CheckItem(
                    name="配置文件",
                    ok=True,
                    detail=f"合格：模式 {app.mode.value}，档位 {app.profile}",
                )
            )
        except ConfigError as e:
            out.append(CheckItem(name="配置文件", ok=False, detail=str(e)))
    snap = await hb.snapshot()
    out.append(
        CheckItem(
            name="hummingbot-api",
            ok=snap.ok,
            detail="连上了" + (f"，钱包 {snap.wallet_usdt:,.2f} USDT" if snap.wallet_usdt else "")
            if snap.ok
            else snap.error or "连不上",
        )
    )
    token, chat = read_secret("TELEGRAM_BOT_TOKEN"), read_secret("TELEGRAM_CHAT_ID")
    if not (token and chat):
        out.append(CheckItem(name="Telegram", ok=False, detail="还没配 token 或 chat id"))
    else:
        alert = Alert(
            ts=now,
            level=AlertLevel.INFO,
            role=Role.ALERTING,
            title="面板首次设置：测试消息",
            body="收到这条说明告警能发到你的 Telegram。",
        )
        # 同步发送最长 10 秒：放到线程里，别卡住面板的事件循环
        ok = await asyncio.to_thread(send_telegram, token, chat, alert, transport)
        out.append(
            CheckItem(
                name="Telegram",
                ok=ok,
                detail="测试消息已发出，去 Telegram 看看"
                if ok
                else "发送失败：检查 token 和 chat id",
            )
        )
    return out
