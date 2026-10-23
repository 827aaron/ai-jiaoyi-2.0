"""命令行入口：``jiaoyi <命令>``。"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from jiaoyi.alerting.telegram import TelegramAlerter
from jiaoyi.contracts import Alert, AppConfig, RiskConfig, Role
from jiaoyi.contracts.module_status import AlertLevel
from jiaoyi.core import profiles
from jiaoyi.core.commands import config_fingerprint
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
    KeychainWriter,
    mask,
)
from jiaoyi.execution.factory import MissingSecret
from jiaoyi.execution.live_gate import CONFIRM_WORD, LiveGate, LiveGateError
from jiaoyi.slow.settings import (
    DEFAULT_SLOW_PATH,
    SlowConfig,
    load_slow_config,
    missing_slow_stack,
)


def _load(args: argparse.Namespace) -> tuple[RiskConfig, AppConfig, SlowConfig]:
    """读三份配置；slow.toml 里没写的节奏参数取 app.toml 所选档位的默认。"""
    app = load_app_config(Path(args.app))
    return load_risk_config(Path(args.risk)), app, load_slow_config(Path(args.slow), app.profile)


def _profile_line(args: argparse.Namespace, app: AppConfig) -> str:
    """当前档位，以及配置文件里哪些键覆盖了档位默认（覆盖优先）。"""
    raw_app = tomllib.loads(Path(args.app).read_text(encoding="utf-8"))
    slow_path = Path(args.slow)
    raw_slow = tomllib.loads(slow_path.read_text(encoding="utf-8")) if slow_path.is_file() else {}
    sections: list[tuple[str, dict[str, object], profiles.Section]] = [
        ("", raw_app, "app"),
        ("fast_loop.", raw_app.get("fast_loop", {}), "fast_loop"),
        ("schedule.", raw_slow.get("schedule", {}), "slow.schedule"),
        ("budget.", raw_slow.get("budget", {}), "slow.budget"),
    ]
    keys = [
        prefix + k
        for prefix, raw, sec in sections
        for k in profiles.overrides(raw, app.profile, sec)
    ]
    tag = "（仅模拟盘）" if app.profile in profiles.PAPER_ONLY else ""
    tail = f"；单项覆盖档位默认：{', '.join(keys)}" if keys else ""
    return f"持仓档位 {app.profile}{tag}{tail}"


def _caution_line(risk: RiskConfig) -> str | None:
    """硬上限比推荐值冒险时的提示（不拒绝启动）。终端拿不到账户资金，推荐按 1000U 算。"""
    hit = risk.cautions()
    return (
        f"注意：硬上限比推荐值冒险（推荐按 1000U 算，只提示不拦）：{'；'.join(hit)}"
        if hit
        else None
    )


def _check_config(args: argparse.Namespace) -> int:
    try:
        risk, app, slow = _load(args)
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    if problem := missing_slow_stack(slow, ChainedSecretStore()):
        print(problem, file=sys.stderr)
        return 2
    print(
        f"配置合格。模式 {app.mode}，每笔风险 {risk.risk_per_trade_pct}%，"
        f"最大杠杆 {risk.max_leverage}x，日亏熔断 {risk.daily_loss_halt_pct}%。"
    )
    print(_profile_line(args, app))
    if caution := _caution_line(risk):
        print(caution)
    if gap := slow.schedule.caution():
        print(f"注意：{gap}")
    for note in slow.screener.cautions(risk.max_spread_bps):
        print(f"注意：选币门槛——{note}")
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
    try:
        # 解锁绑定此刻的 risk.toml：之后改了硬上限，实盘启动会被拒，要重新 go-live
        fingerprint = config_fingerprint(load_risk_config(Path(args.risk)))
    except Exception as e:
        print(f"读不了 risk 配置：{e}", file=sys.stderr)
        return 1
    word = input(f"输入确认词“{CONFIRM_WORD}”：")
    password = getpass.getpass("面板密码：")
    try:
        armed_at = gate.unlock(word, password, fingerprint)
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
        risk, app_cfg, slow = _load(args)
        app = App(risk, app_cfg, slow_cfg=slow)
    except (ConfigError, LiveGateError, MissingSecret) as e:
        print(e, file=sys.stderr)
        return 2
    print(_profile_line(args, app.app_cfg))
    if caution := _caution_line(risk):
        print(caution)
    if gap := slow.schedule.caution():
        print(f"注意：{gap}")
    for role, st in app.status().items():
        print(f"{role.value:<12} {st.health.value:<9} {st.headline}")
    from jiaoyi.app import read_blocked

    for sym, reason in read_blocked(app.app_cfg.data_dir).items():
        print(f"禁开新仓     {sym:<12} {reason}")
    if app.paused is not None:
        since, reason = app.paused.get("since", ""), app.paused.get("reason", "")
        print(f"人工暂停     自 {since} 起不开新仓 {reason}")
    asyncio.run(app.stop())
    return 0


def _run(args: argparse.Namespace) -> int:
    from jiaoyi.app import App
    from jiaoyi.loop import Runner

    if problem := _startup_problem(args):
        print(problem, file=sys.stderr)
        return 2
    try:
        risk, app_cfg, slow = _load(args)
        app = App(risk, app_cfg, slow_cfg=slow)
    except (ConfigError, LiveGateError, MissingSecret) as e:
        print(e, file=sys.stderr)
        return 2
    print(f"启动：模式 {app.app_cfg.mode.value}，拍板 {app.gate_label()}。Ctrl+C 停止。")
    print(_profile_line(args, app.app_cfg))
    if caution := _caution_line(risk):
        print(caution)
    if gap := slow.schedule.caution():
        print(f"注意：{gap}")
    runner = Runner(app, restart_check=lambda: _restart_blocker(args), restart_exit=_restart_exit)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(runner.run())
    if runner.restart_requested:
        print("面板要求重启：已干净退出，launchd 会重新拉起")
    return 0


LAUNCHD_RUN_LABEL = "com.jiaoyi.run"


def _restart_exit() -> None:
    """面板重启：直接结束进程（不等工作线程里没开完的会），launchd KeepAlive 会拉起。"""
    with contextlib.suppress(Exception):
        print("面板要求重启：退出，launchd 会重新拉起", flush=True)
        sys.stderr.flush()
    os._exit(0)


def _restart_blocker(args: argparse.Namespace) -> str | None:
    """面板“重启生效”前的检查：退出后要有人拉起，新配置要能启动，否则不退。"""
    if os.environ.get("XPC_SERVICE_NAME") != LAUNCHD_RUN_LABEL:
        return "不是 launchd 托管（com.jiaoyi.run），退出后不会自动拉起；请在终端手动重启"
    if problem := _startup_problem(args):
        return f"按现在的配置主程序起不来，没有重启：{problem}"
    return None


def _startup_problem(args: argparse.Namespace) -> str | None:
    """``jiaoyi run`` 启动前就能查出的拒绝启动原因（启动路径和面板重启前共用，两边口径一致）。

    配置校验；军师团有 key 却没装依赖；实盘没解锁、或解锁后 risk.toml 改过（指纹对不上）。
    """
    try:
        risk, app_cfg, slow = _load(args)
    except ConfigError as e:
        return str(e)
    if problem := missing_slow_stack(slow, ChainedSecretStore()):
        return problem
    if app_cfg.mode == "live":
        try:
            gate = LiveGate(Path(app_cfg.data_dir) / "live_gate.json")
            gate.require_armed(config_fingerprint(risk))
        except LiveGateError as e:
            return f"{e}（硬上限改过的话，先在终端运行 jiaoyi go-live）"
    return None


def _watchdog(args: argparse.Namespace) -> int:
    """主进程外的心跳检查，由 launchd 每分钟跑一次（com.jiaoyi.watchdog）。"""
    from jiaoyi.watchdog import run_once

    try:
        data_dir = load_app_config(Path(args.app)).data_dir
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    store = ChainedSecretStore()
    return run_once(
        data_dir,
        store.get("TELEGRAM_BOT_TOKEN"),
        store.get("TELEGRAM_CHAT_ID"),
        datetime.now(UTC),
    )


def _panel(args: argparse.Namespace) -> int:
    """面板：独立进程，读 data 目录和 hummingbot-api；写操作只写指令文件和配置文件。

    主循环停着也能开。
    """
    import uvicorn

    from jiaoyi.execution.factory import CONNECTOR_BY_MODE
    from jiaoyi.panel.actions import ConfigPaths
    from jiaoyi.panel.hb import HbReader
    from jiaoyi.panel.server import create_app

    # 新机器第一次开面板：没有配置就从 *.example.toml 复制一份（默认保守值、paper 模式），
    # 剩下的在首次设置向导里配
    for target in (Path(args.risk), Path(args.app)):
        example = target.with_name(target.name.replace(".toml", ".example.toml"))
        if not target.exists() and example.is_file():
            shutil.copy2(example, target)
            print(f"没有 {target}，已从 {example.name} 复制一份")
    try:
        risk = load_risk_config(Path(args.risk))
        app_cfg = load_app_config(Path(args.app))
    except ConfigError as e:
        print(e, file=sys.stderr)
        return 2
    hb = HbReader(
        args.hb_url,
        "admin",
        ChainedSecretStore().get("HUMMINGBOT_API_PASSWORD"),
        account_name="master_account",
        connector_name=CONNECTOR_BY_MODE[app_cfg.mode],
    )
    # 设置页只显示每个密钥配没配：在这里查好传进去，密钥的值不进面板的数据和接口
    store = ChainedSecretStore()
    keys = {name: bool(store.get(name)) for name in KNOWN_KEYS}
    writer = KeychainWriter()
    print(f"面板：http://{args.host}:{args.port}/ （只接受局域网访问，Ctrl+C 停止）")
    if not all(keys.get(k) for k in ("OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN")):
        print(f"还没配齐：在这台 Mac 上打开 http://127.0.0.1:{args.port}/setup")
    paths = ConfigPaths(risk=Path(args.risk), app=Path(args.app), slow=Path(args.slow))
    app = create_app(
        risk,
        app_cfg,
        hb,
        key_status=keys,
        config_paths=paths,
        allow_hosts=frozenset(h.lower() for h in args.allow_host),
        secret_writer=writer.set if writer.available() else None,
        secret_reader=store.get,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jiaoyi")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("check-config", help="检查硬上限和运行配置，不合格返回非 0")
    p.add_argument("--risk", default=str(DEFAULT_RISK_PATH))
    p.add_argument("--app", default=str(DEFAULT_APP_PATH))
    p.add_argument("--slow", default=str(DEFAULT_SLOW_PATH), help="可选；没有就用默认值")
    p.set_defaults(func=_check_config)

    p = sub.add_parser("status", help="团队页的文字版：每个角色的状态")
    p.add_argument("--risk", default=str(DEFAULT_RISK_PATH))
    p.add_argument("--app", default=str(DEFAULT_APP_PATH))
    p.add_argument("--slow", default=str(DEFAULT_SLOW_PATH), help="可选；没有就用默认值")
    p.set_defaults(func=_status)

    p = sub.add_parser("run", help="启动快环主循环（行情、哨兵、拍板、保镖、盯仓、定时开会）")
    p.add_argument("--risk", default=str(DEFAULT_RISK_PATH))
    p.add_argument("--app", default=str(DEFAULT_APP_PATH))
    p.add_argument("--slow", default=str(DEFAULT_SLOW_PATH), help="可选；没有就用默认值")
    p.set_defaults(func=_run)

    p = sub.add_parser("watchdog", help="看一次主循环心跳，停了就发 Telegram（launchd 每分钟跑）")
    p.add_argument("--app", default=str(DEFAULT_APP_PATH))
    p.set_defaults(func=_watchdog)

    p = sub.add_parser("panel", help="启动面板网页（局域网）")
    p.add_argument("--risk", default=str(DEFAULT_RISK_PATH))
    p.add_argument("--app", default=str(DEFAULT_APP_PATH))
    p.add_argument("--slow", default=str(DEFAULT_SLOW_PATH), help="可选；没有就用默认值")
    p.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="额外允许的主机名（浏览器地址栏里用的名字）；局域网 IP、localhost、*.local 默认允许",
    )
    p.add_argument("--host", default="0.0.0.0", help="0.0.0.0 = 局域网内其他设备也能打开")
    p.add_argument("--port", type=int, default=8090)
    p.add_argument("--hb-url", default="http://127.0.0.1:8000")
    p.set_defaults(func=_panel)

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
        p.add_argument("--risk", default=str(DEFAULT_RISK_PATH), help="go-live 用：绑定这份硬上限")
        p.set_defaults(func=func)

    args = parser.parse_args(argv)
    code: int = args.func(args)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
