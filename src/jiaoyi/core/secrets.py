"""读密钥。key 不进仓库、不进网页：正式环境存 macOS 钥匙串，开发时用环境变量。

钥匙串条目约定：service = ``jiaoyi``，account = 密钥名（例如 ``BINANCE_API_KEY``）。
写入由 ``setup-keys`` 命令负责（执行工程师实现），这里只读。
"""

from __future__ import annotations

import os
import shutil
import subprocess

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
