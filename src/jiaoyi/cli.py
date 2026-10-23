"""命令行入口：``jiaoyi <命令>``。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import shutil
import subprocess
import sys
from pathlib import Path

from jiaoyi.alerting.telegram import TelegramAlerter
from jiaoyi.contracts import Alert, Role
from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.core.config import (
    DEFAULT_APP_PATH,
    DEFAULT_RISK_PATH,
    ConfigError,
    load_app_config,
    load_risk_config,
)
from jiaoyi.core.secrets import (
    KEYCHAIN_SERVICE,
    KNOWN_KEYS,
    ChainedSecretStore,
    KeychainSecretStore,
    mask,
)
from jiaoyi.execution.factory import MissingSecret
from jiaoyi.execution.live_gate import CONFIRM_WORD, LiveGate, LiveGateError


def _check_config(args: argparse.Namespace) -> int:
    try:
        risk = load_risk_config(Path(args.risk))
        app = load_app_config(Path(args.app))
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    print(
        f"配置合格。模式 {app.mode}，每笔风险 {risk.risk_per_trade_pct}%，"
        f"最大杠杆 {risk.max_leverage}x，日亏熔断 {risk.daily_loss_halt_pct}%。"
    )
    return 0


def _key_status(_: argparse.Namespace) -> int:
    store = ChainedSecretStore()
    for name in KNOWN_KEYS:
        print(f"{name:<26} {mask(store.get(name))}")
    return 0


def _setup_keys(args: argparse.Namespace) -> int:
    """逐个把密钥写进 macOS 钥匙串。值由 ``security`` 自己在终端里提示输入，
    不经过本程序的参数、环境变量、日志，也不会出现在聊天里。"""
    if shutil.which("security") is None:
        print("setup-keys 只支持 macOS 钥匙串。开发环境请用 .env / 环境变量。", file=sys.stderr)
        return 2
    names = args.names or list(KNOWN_KEYS)
    unknown = [n for n in names if n not in KNOWN_KEYS]
    if unknown:
        print(
            f"不认识的密钥名：{', '.join(unknown)}。可选：{', '.join(KNOWN_KEYS)}", file=sys.stderr
        )
        return 2
    store = KeychainSecretStore()
    for name in names:
        current = mask(store.get(name))
        answer = input(f"{name}（当前 {current}）要写入 / 覆盖吗？[y/N] ").strip().lower()
        if answer != "y":
            continue
        result = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            check=False,
        )
        if result.returncode != 0:
            print(f"{name} 写入失败（security 退出码 {result.returncode}）", file=sys.stderr)
            return 1
        print(f"{name} 已写入，当前 {mask(store.get(name))}")
    return 0


def _gate(args: argparse.Namespace) -> LiveGate:
    return LiveGate(Path(args.data_dir) / "live_gate.json")


def _set_panel_password(args: argparse.Namespace) -> int:
    gate = _gate(args)
    old = getpass.getpass("旧面板密码：") if gate.password_set else None
    new = getpass.getpass("新面板密码（至少 8 位）：")
    if new != getpass.getpass("再输一次："):
        print("两次不一致", file=sys.stderr)
        return 1
    try:
        gate.set_password(new, old)
    except LiveGateError as e:
        print(e, file=sys.stderr)
        return 1
    print("面板密码已设置。")
    return 0


def _go_live(args: argparse.Namespace) -> int:
    """切实盘：确认词 + 面板密码。成功后发通知（有 Telegram 密钥就发 Telegram，否则打印）。"""
    gate = _gate(args)
    word = input(f"输入确认词“{CONFIRM_WORD}”：")
    password = getpass.getpass("面板密码：")
    try:
        armed_at = gate.unlock(word, password)
    except LiveGateError as e:
        print(e, file=sys.stderr)
        return 1
    message = f"实盘已解锁（{armed_at.isoformat()}）。下次启动配置为 mode=live 时才会真连币安主网。"
    store = ChainedSecretStore()
    token, chat = store.get("TELEGRAM_BOT_TOKEN"), store.get("TELEGRAM_CHAT_ID")
    if token and chat:

        async def notify() -> None:
            alerter = TelegramAlerter(token, chat)
            await alerter.send(
                Alert(
                    ts=armed_at,
                    level=AlertLevel.CRITICAL,
                    role=Role.EXECUTOR,
                    title="实盘已解锁",
                    body=message,
                )
            )
            await alerter.close()

        asyncio.run(notify())
    print(message)
    return 0


def _go_paper(args: argparse.Namespace) -> int:
    _gate(args).disarm()
    print("实盘已上锁。")
    return 0


def _status(args: argparse.Namespace) -> int:
    from jiaoyi.app import App

    try:
        app = App(load_risk_config(Path(args.risk)), load_app_config(Path(args.app)))
    except (ConfigError, LiveGateError, MissingSecret) as e:
        print(e, file=sys.stderr)
        return 2
    for role, st in app.status().items():
        print(f"{role.value:<12} {st.health.value:<9} {st.headline}")
    from jiaoyi.app import read_blocked

    for sym, reason in read_blocked(app.app_cfg.data_dir).items():
        print(f"禁开新仓     {sym:<12} {reason}")
    asyncio.run(app.stop())
    return 0


def _run(args: argparse.Namespace) -> int:
    from jiaoyi.app import App
    from jiaoyi.loop import Runner

    try:
        app = App(load_risk_config(Path(args.risk)), load_app_config(Path(args.app)))
    except (ConfigError, LiveGateError, MissingSecret) as e:
        print(e, file=sys.stderr)
        return 2
    print(f"启动：模式 {app.app_cfg.mode.value}，拍板 {app.gate_label()}。Ctrl+C 停止。")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(Runner(app).run())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jiaoyi")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check-config", help="检查硬上限和运行配置，不合格返回非 0")
    p.add_argument("--risk", default=str(DEFAULT_RISK_PATH))
    p.add_argument("--app", default=str(DEFAULT_APP_PATH))
    p.set_defaults(func=_check_config)

    p = sub.add_parser("status", help="团队页的文字版：每个角色的状态")
    p.add_argument("--risk", default=str(DEFAULT_RISK_PATH))
    p.add_argument("--app", default=str(DEFAULT_APP_PATH))
    p.set_defaults(func=_status)

    p = sub.add_parser("run", help="启动快环主循环（行情、哨兵、拍板、保镖、盯仓、定时开会）")
    p.add_argument("--risk", default=str(DEFAULT_RISK_PATH))
    p.add_argument("--app", default=str(DEFAULT_APP_PATH))
    p.set_defaults(func=_run)

    p = sub.add_parser("key-status", help="显示每个密钥配没配（只显示末四位）")
    p.set_defaults(func=_key_status)

    p = sub.add_parser("setup-keys", help="把密钥写进 macOS 钥匙串（值只在终端输入）")
    p.add_argument("names", nargs="*", help="只设这几个；不写就逐个问")
    p.set_defaults(func=_setup_keys)

    for name, func, text in (
        ("set-panel-password", _set_panel_password, "设置 / 修改面板密码"),
        ("go-live", _go_live, "解锁实盘：确认词 + 面板密码，5 次错误锁 15 分钟"),
        ("go-paper", _go_paper, "重新锁上实盘"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--data-dir", default="data")
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
