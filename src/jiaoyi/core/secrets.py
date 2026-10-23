"""读密钥。key 不进仓库、不进网页：正式环境存 macOS 钥匙串，开发时用环境变量。

钥匙串条目约定：service = ``jiaoyi``，account = 密钥名（例如 ``BINANCE_API_KEY``）。
写入：终端用 ``setup-keys``；面板的首次设置向导（只对本机开放）用 ``KeychainWriter``。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Literal

KEYCHAIN_SERVICE = "jiaoyi"

KNOWN_KEYS = (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "JEV_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "FRED_API_KEY",
    "HUMMINGBOT_API_PASSWORD",
)


class EnvSecretStore:
    def get(self, name: str) -> str | None:
        value = os.environ.get(name, "").strip()
        return value or None


class KeychainSecretStore:
    """用 macOS 自带的 ``security`` 命令读钥匙串。非 macOS 上永远返回 None。"""

    def get(self, name: str) -> str | None:
        if shutil.which("security") is None:
            return None
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None


# 首次设置向导：跑 paper（Demo Trading）+ 一把 OpenRouter key 管拍板和军师团时必须有的
REQUIRED_KEYS = (
    "OPENROUTER_API_KEY",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "HUMMINGBOT_API_PASSWORD",
)
# 现在可以不填：主网 key 到 M4 小额实盘再填；直连 Jev / DeepSeek 只在不走 OpenRouter 时要
OPTIONAL_KEYS = (
    "BINANCE_API_KEY",
    "BINANCE_API_SECRET",
    "JEV_API_KEY",
    "DEEPSEEK_API_KEY",
    "FRED_API_KEY",
)

KeyNeed = Literal["required", "optional", "unused"]


def key_needs(mode: str, jev_provider: str, llm_provider: str) -> dict[str, KeyNeed]:
    """按当前配置，每个密钥是必须、可选还是用不到（设置页据此显示，不让人对着“未配置”发慌）。"""
    live = mode == "live"
    openrouter = "openrouter" in (jev_provider, llm_provider)
    need: dict[str, bool | None] = {
        "BINANCE_API_KEY": live,
        "BINANCE_API_SECRET": live,
        "BINANCE_TESTNET_API_KEY": not live,
        "BINANCE_TESTNET_API_SECRET": not live,
        "JEV_API_KEY": jev_provider == "typesafe",
        "OPENROUTER_API_KEY": openrouter,
        "DEEPSEEK_API_KEY": llm_provider == "deepseek",
        "TELEGRAM_BOT_TOKEN": True,
        "TELEGRAM_CHAT_ID": True,
        "HUMMINGBOT_API_PASSWORD": True,
        "FRED_API_KEY": None,
    }
    return {k: "optional" if v is None else "required" if v else "unused" for k, v in need.items()}


# 密钥只允许常见 token 字符：不含空白、引号、反斜杠，避免拼进 ``security -i`` 命令时出岔子
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_\-.:+/=@~!#$%^&*()\[\]{},;?|<>]{1,512}$")


class SecretError(Exception):
    """写密钥失败。消息是给人看的中文，不含密钥值。"""


def check_secret_value(name: str, value: str) -> str:
    """去掉首尾空白后检查格式，返回要写入的值。"""
    if name not in KNOWN_KEYS:
        raise SecretError(f"不认识的密钥名：{name}")
    v = value.strip()
    if not v:
        raise SecretError(f"{name} 是空的")
    if name == "TELEGRAM_CHAT_ID":
        if not re.fullmatch(r"-?\d{1,20}", v):
            raise SecretError("TELEGRAM_CHAT_ID 应该是一串数字（群组以 - 开头）")
    elif not _SAFE_VALUE.match(v):
        raise SecretError(f"{name} 里有空格、引号或其他不该出现的字符，检查是不是粘贴多了")
    return v


class KeychainWriter:
    """写 macOS 钥匙串。值经 ``security -i`` 的标准输入传进去，不出现在进程参数里（ps 看不到）。"""

    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        read: Callable[[str], str | None] | None = None,
    ) -> None:
        self._run = run
        self._read = read or KeychainSecretStore().get

    def available(self) -> bool:
        return shutil.which("security") is not None

    def set(self, name: str, value: str) -> None:
        v = check_secret_value(name, value)
        if not self.available():
            raise SecretError("这台机器没有 macOS 钥匙串（security 命令），只能用 .env / 环境变量")
        cmd = f'add-generic-password -U -s {KEYCHAIN_SERVICE} -a {name} -w "{v}"\n'
        result = self._run(
            ["security", "-i"], input=cmd, capture_output=True, text=True, check=False
        )
        # -i 模式下单条命令失败时退出码也可能是 0：读回来核对一遍
        if result.returncode != 0 or self._read(name) != v:
            raise SecretError(f"{name} 写入钥匙串失败（security 退出码 {result.returncode}）")


class ChainedSecretStore:
    """先钥匙串，后环境变量。"""

    def __init__(self) -> None:
        self._stores = (KeychainSecretStore(), EnvSecretStore())

    def get(self, name: str) -> str | None:
        for store in self._stores:
            value = store.get(name)
            if value:
                return value
        return None


def mask(value: str | None) -> str:
    """面板和日志里只显示末四位；太短的值连末四位也不显示。"""
    if not value:
        return "未配置"
    if len(value) < 12:
        return "已配置"
    return "****" + value[-4:]
