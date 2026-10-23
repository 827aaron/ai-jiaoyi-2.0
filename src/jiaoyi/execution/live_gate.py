"""实盘上线门槛：配置里写 ``mode = "live"`` 不够，还要走一遍“确认词 + 面板密码”。

规则（设计文档 §十）：
  - 默认 paper；切 live 必须输入确认词和面板密码
  - 密码连错 5 次，锁 15 分钟（重启也不重置，状态落盘）
  - 切换成功后发 Telegram / 日志告警
  - ``build_execution(mode=live)`` 会调 ``require_armed()``，没解锁就拒绝建立实盘连接

面板密码只存加盐的 scrypt 哈希，不存明文。
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from jiaoyi.execution.clock import Clock, utc_now

CONFIRM_WORD = "确认切换实盘"
MAX_FAILURES = 5
LOCK = timedelta(minutes=15)
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class LiveGateError(Exception):
    """实盘门槛没过。消息是给人看的中文。"""


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    salt_hex: str | None = None
    hash_hex: str | None = None
    failures: int = 0
    locked_until: datetime | None = None
    armed_at: datetime | None = None
    risk_fingerprint: str | None = None
    """解锁时 risk.toml 的指纹。硬上限改过就要重新解锁（设计：改硬上限需面板密码）。"""


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)


class LiveGate:
    def __init__(self, path: str | Path, clock: Clock = utc_now) -> None:
        self._path = Path(path)
        self._clock = clock
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._state = (
            _State.model_validate_json(self._path.read_text(encoding="utf-8"))
            if self._path.is_file()
            else _State()
        )

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(self._state.model_dump_json(indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    @property
    def password_set(self) -> bool:
        return self._state.hash_hex is not None

    def set_password(self, new_password: str, old_password: str | None = None) -> None:
        """首次设置不需要旧密码；改密码必须给对旧密码（同样计入错误次数和锁定）。"""
        if len(new_password) < 8:
            raise LiveGateError("面板密码至少 8 位")
        if self.password_set:
            self._verify(old_password or "")
        salt = secrets.token_bytes(16)
        self._state = self._state.model_copy(
            update={
                "salt_hex": salt.hex(),
                "hash_hex": _hash(new_password, salt).hex(),
                "failures": 0,
                "locked_until": None,
                "armed_at": None,  # 改密码后要重新解锁
            }
        )
        self._save()

    def _verify(self, password: str) -> None:
        now = self._clock()
        if self._state.locked_until is not None and now < self._state.locked_until:
            minutes = int((self._state.locked_until - now).total_seconds() // 60) + 1
            raise LiveGateError(f"密码错误次数过多，已锁定，约 {minutes} 分钟后再试")
        if self._state.salt_hex is None or self._state.hash_hex is None:
            raise LiveGateError("还没有设置面板密码：先运行 jiaoyi set-panel-password")
        ok = hmac.compare_digest(
            _hash(password, bytes.fromhex(self._state.salt_hex)).hex(), self._state.hash_hex
        )
        if ok:
            self._state = self._state.model_copy(update={"failures": 0, "locked_until": None})
            self._save()
            return
        failures = self._state.failures + 1
        locked = now + LOCK if failures >= MAX_FAILURES else None
        self._state = self._state.model_copy(
            update={"failures": 0 if locked else failures, "locked_until": locked}
        )
        self._save()
        if locked:
            raise LiveGateError(f"连错 {MAX_FAILURES} 次，锁定 15 分钟")
        raise LiveGateError(f"密码不对（还能试 {MAX_FAILURES - failures} 次）")

    def unlock(self, confirm_word: str, password: str, risk_fingerprint: str) -> datetime:
        """两样都对才解锁实盘，返回解锁时间。调用方负责随后发通知。

        ``risk_fingerprint`` 是此刻 risk.toml 的指纹，之后改了硬上限，实盘启动会被拒。
        """
        if confirm_word.strip() != CONFIRM_WORD:
            raise LiveGateError(f"确认词不对，需要原样输入：{CONFIRM_WORD}")
        self._verify(password)
        armed_at = self._clock()
        self._state = self._state.model_copy(
            update={"armed_at": armed_at, "risk_fingerprint": risk_fingerprint}
        )
        self._save()
        return armed_at

    def disarm(self) -> None:
        self._state = self._state.model_copy(update={"armed_at": None})
        self._save()

    @property
    def armed(self) -> bool:
        return self._state.armed_at is not None

    def require_armed(self, risk_fingerprint: str) -> None:
        if not self.armed:
            raise LiveGateError(
                "实盘未解锁：配置里写 live 不够，先运行 jiaoyi go-live 输入确认词和面板密码"
            )
        if self._state.risk_fingerprint != risk_fingerprint:
            raise LiveGateError(
                "risk.toml 在解锁之后改过（或解锁记录没有指纹）：改硬上限要重新确认，"
                "先运行 jiaoyi go-live"
            )
