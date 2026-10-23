"""选币页的固定 / 拉黑 / 手动加入：文件存储、面板指令、拉黑立即不开新仓。"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from pathlib import Path

import pytest

from jiaoyi.contracts import SymbolPool
from jiaoyi.core.commands import write_command
from jiaoyi.core.pool_overrides import (
    POOL_OVERRIDES_FILE,
    POOL_OVERRIDES_PAUSE,
    PoolOverrides,
    read_pool_overrides,
    write_pool_overrides,
)
from jiaoyi.panel import actions
from jiaoyi.panel.actions import ActionError
from jiaoyi.slow.screener import UserOverrides
from tests import fastloop_helpers as h
from tests.test_commands import _cmd, _restart_runner, _result


def test_apply_keeps_the_lists_exclusive() -> None:
    ov = PoolOverrides().apply("pin_symbol", "SOLUSDT").apply("add_symbol", "WIFUSDT")
    assert ov.pinned == {"SOLUSDT"} and ov.manual == {"WIFUSDT"}
    ov = ov.apply("blacklist_symbol", "SOLUSDT").apply("blacklist_symbol", "WIFUSDT")
    assert ov.blacklist == {"SOLUSDT", "WIFUSDT"} and not ov.pinned and not ov.manual
    ov = ov.apply("pin_symbol", "SOLUSDT")  # 固定会取消拉黑
    assert ov.pinned == {"SOLUSDT"} and ov.blacklist == {"WIFUSDT"}
    assert ov.apply("unblacklist_symbol", "WIFUSDT").blacklist == frozenset()
    with pytest.raises(ValueError, match="BTCUSDT"):
        ov.apply("pin_symbol", "btc")


def test_file_roundtrip_missing_and_corrupt(tmp_path: Path) -> None:
    assert read_pool_overrides(tmp_path) == PoolOverrides()  # 没有文件 = 没有覆盖项
    ov = PoolOverrides().apply("blacklist_symbol", "PEPEUSDT")
    write_pool_overrides(tmp_path, ov)
    assert read_pool_overrides(tmp_path) == ov
    (tmp_path / POOL_OVERRIDES_FILE).write_text('{"pinned": 1}', encoding="utf-8")
    assert read_pool_overrides(tmp_path) is None


def test_user_overrides_from_file() -> None:
    ov = PoolOverrides().apply("pin_symbol", "SOLUSDT").apply("blacklist_symbol", "PEPEUSDT")
    uo = UserOverrides.from_file(ov)
    assert uo.pinned == {"SOLUSDT"} and uo.blacklist == {"PEPEUSDT"}


class _Screener:
    """记下拿到的覆盖项；选币结果只放手动加入 / 固定的币。"""

    def __init__(self) -> None:
        self.ov = UserOverrides()

    def set_overrides(self, overrides: UserOverrides) -> None:
        self.ov = overrides

    async def screen(self, now: datetime) -> SymbolPool:
        return SymbolPool(pool_id="pool-x", ts=now, entries=[])


async def test_panel_commands_write_file_rescreen_and_block_entries(
    tmp_path: Path, now: datetime
) -> None:
    r, ex = _restart_runner(tmp_path, now, lambda: None)
    scr = _Screener()
    r.m = dataclasses.replace(r.m, screener=scr)  # type: ignore[arg-type]
    r.app.m = r.m

    write_command(tmp_path, _cmd(now, "blacklist_symbol", symbol="BTCUSDT"))
    await r.command_tick()
    res = _result(tmp_path, "cmd-blacklist_symbol-0001")
    assert res["status"] == "done" and "已拉黑" in res["detail"]
    assert read_pool_overrides(tmp_path) == PoolOverrides(blacklist=frozenset({"BTCUSDT"}))
    assert scr.ov.blacklist == {"BTCUSDT"}  # 选币器已拿到
    await r.handle_signal(h.signal(now))  # 旧作战卡里还有 BTC：拉黑后立即不开
    assert ex.opened == []

    write_command(tmp_path, _cmd(now, "unblacklist_symbol", 2, symbol="BTCUSDT"))
    await r.command_tick()
    assert _result(tmp_path, "cmd-unblacklist_symbol-0002")["status"] == "done"
    await r.handle_signal(h.signal(now))
    assert ex.opened == ["int-0001"]

    write_command(tmp_path, _cmd(now, "pin_symbol", 3, symbol="bad"))
    await r.command_tick()
    assert _result(tmp_path, "cmd-pin_symbol-0003")["status"] == "rejected"


async def test_hand_edited_file_is_picked_up_on_next_screen(tmp_path: Path, now: datetime) -> None:
    r, _ = _restart_runner(tmp_path, now, lambda: None)
    scr = _Screener()
    r.m = dataclasses.replace(r.m, screener=scr)  # type: ignore[arg-type]
    r.app.m = r.m
    write_pool_overrides(tmp_path, PoolOverrides(manual=frozenset({"WIFUSDT"})))
    await r._rescreen(now)
    assert scr.ov.manual == {"WIFUSDT"}
    (tmp_path / POOL_OVERRIDES_FILE).write_text("坏了", encoding="utf-8")
    await r._rescreen(now)
    assert scr.ov.manual == {"WIFUSDT"}  # 读不出沿用上一份，不清空


def test_panel_needs_password_only_for_riskier_pool_actions(tmp_path: Path, now: datetime) -> None:
    with pytest.raises(ActionError, match="BTCUSDT"):
        actions.send_command(tmp_path, "blacklist_symbol", {"symbol": ""}, now, None)
    # 拉黑只会更保守：不要密码
    actions.send_command(tmp_path, "blacklist_symbol", {"symbol": "PEPEUSDT"}, now, None)
    for kind in ("pin_symbol", "add_symbol", "unblacklist_symbol"):
        assert kind in actions.NEEDS_PASSWORD


def test_corrupt_file_at_startup_is_flagged(tmp_path: Path, now: datetime) -> None:
    from tests.test_commands import _app

    (tmp_path / POOL_OVERRIDES_FILE).write_text("坏了", encoding="utf-8")
    app = _app(tmp_path, now)
    assert app.pool_overrides_corrupt and app.pool_overrides == PoolOverrides()
    [backup] = list(tmp_path.glob(f"{POOL_OVERRIDES_FILE}.corrupt-*"))
    assert backup.read_text(encoding="utf-8") == "坏了"


async def test_corrupt_file_is_not_overwritten_by_a_panel_action(
    tmp_path: Path, now: datetime
) -> None:
    """文件坏了时不拿“空 + 这一次操作”覆盖，免得原来的拉黑清单丢掉。"""
    r, _ = _restart_runner(tmp_path, now, lambda: None)
    (tmp_path / POOL_OVERRIDES_FILE).write_text("坏了", encoding="utf-8")
    write_command(tmp_path, _cmd(now, "pin_symbol", symbol="SOLUSDT"))
    await r.command_tick()
    res = _result(tmp_path, "cmd-pin_symbol-0001")
    assert res["status"] == "rejected" and "读不出" in res["detail"]
    assert (tmp_path / POOL_OVERRIDES_FILE).read_text(encoding="utf-8") == "坏了"


async def test_corrupt_file_pauses_new_entries_until_fixed(tmp_path: Path, now: datetime) -> None:
    """拉黑清单没生效 → 失败即关闭：暂停开新仓、面板不能恢复；修好后重启自动恢复。"""
    from jiaoyi.core.commands import read_paused
    from tests.test_commands import _app

    (tmp_path / POOL_OVERRIDES_FILE).write_text("坏了", encoding="utf-8")
    app = _app(tmp_path, now)
    await app.start()
    paused = read_paused(tmp_path)
    assert paused is not None and paused["source"] == POOL_OVERRIDES_PAUSE
    assert "读不出" in paused["reason"]
    await app.stop()

    r, _ = _restart_runner(tmp_path, now, lambda: None)
    write_command(tmp_path, _cmd(now, "resume"))
    await r.command_tick()
    assert _result(tmp_path, "cmd-resume-0001")["status"] == "rejected"
    assert read_paused(tmp_path) is not None

    (tmp_path / POOL_OVERRIDES_FILE).unlink()  # 修好（删掉也算）
    app = _app(tmp_path, now)
    await app.start()
    assert app.paused is None and read_paused(tmp_path) is None
    await app.stop()


async def test_corrupt_file_keeps_a_manual_pause_as_is(tmp_path: Path, now: datetime) -> None:
    from jiaoyi.core.commands import read_paused
    from tests.test_commands import _app

    app = _app(tmp_path, now)
    app.set_paused(True, reason="我手动停的")
    (tmp_path / POOL_OVERRIDES_FILE).write_text("坏了", encoding="utf-8")
    app = _app(tmp_path, now)
    await app.start()
    assert app.paused is not None and app.paused["reason"] == "我手动停的"
    await app.stop()
    (tmp_path / POOL_OVERRIDES_FILE).unlink()
    app = _app(tmp_path, now)
    await app.start()  # 人工暂停不会因为文件修好就自动解除
    paused = read_paused(tmp_path)
    assert paused is not None and paused["reason"] == "我手动停的"
    await app.stop()
