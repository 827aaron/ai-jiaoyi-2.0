"""选币页的覆盖项（固定 / 拉黑 / 手动加入）：存 ``data/pool_overrides.json``，AI 改不了。

- 只有主循环写这个文件：面板发 ``pin_symbol`` 等指令（core/commands.py），主循环执行时
  改文件、立即重新选币。手改文件也行，下一次选币（每小时，或下一条选币指令）读到。
- 优先级同选币（slow/screener.py）：拉黑 > 固定 / 手动加入 > 打分。拉黑的币立即不再开新仓
  （loop.handle_signal 拦），不用等军师团下一轮出卡。
- 文件读不出：启动时按“没有覆盖项”跑并告警；运行中沿用上一份（拉黑不会因为文件坏了悄悄失效）。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from jiaoyi.contracts.common import Category

log = logging.getLogger(__name__)

POOL_OVERRIDES_FILE = "pool_overrides.json"
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")

PoolAction = Literal[
    "pin_symbol",
    "unpin_symbol",
    "blacklist_symbol",
    "unblacklist_symbol",
    "add_symbol",
    "remove_symbol",
]
POOL_ACTIONS: frozenset[str] = frozenset(
    {
        "pin_symbol",
        "unpin_symbol",
        "blacklist_symbol",
        "unblacklist_symbol",
        "add_symbol",
        "remove_symbol",
    }
)


class PoolOverrides(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pinned: frozenset[str] = frozenset()
    """固定在池里（仍要过硬条件），不受类别上限限制。"""
    blacklist: frozenset[str] = frozenset()
    """永不进池，也不开新仓。"""
    manual: frozenset[str] = frozenset()
    """手动加入（仍要过硬条件）。"""
    category_caps: dict[Category, int] = Field(default_factory=dict)
    """每个类别最多进池几个；不写用选币默认。"""

    def apply(self, action: PoolAction, symbol: str) -> PoolOverrides:
        """执行一条选币页操作，返回新的覆盖项。互斥的一边自动去掉（拉黑会取消固定和手动加入）。"""
        if not SYMBOL_RE.match(symbol):
            raise ValueError(f"币种代码不对：{symbol!r}（要像 BTCUSDT）")
        pinned, black, manual = set(self.pinned), set(self.blacklist), set(self.manual)
        if action == "pin_symbol":
            pinned.add(symbol)
            black.discard(symbol)
        elif action == "unpin_symbol":
            pinned.discard(symbol)
        elif action == "blacklist_symbol":
            black.add(symbol)
            pinned.discard(symbol)
            manual.discard(symbol)
        elif action == "unblacklist_symbol":
            black.discard(symbol)
        elif action == "add_symbol":
            manual.add(symbol)
            black.discard(symbol)
        else:  # remove_symbol
            manual.discard(symbol)
        return self.model_copy(
            update={
                "pinned": frozenset(pinned),
                "blacklist": frozenset(black),
                "manual": frozenset(manual),
            }
        )


def read_pool_overrides(data_dir: str | Path) -> PoolOverrides | None:
    """读覆盖项。文件不存在 → 空覆盖项；读不出或格式不对 → None（调用方决定怎么兜底）。"""
    path = Path(data_dir) / POOL_OVERRIDES_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return PoolOverrides()
    except OSError:
        log.exception("读 %s 失败", path)
        return None
    try:
        return PoolOverrides.model_validate_json(raw)
    except ValidationError as e:
        log.critical("%s 格式不对：%s", path, str(e).splitlines()[0])
        return None


def write_pool_overrides(data_dir: str | Path, ov: PoolOverrides) -> None:
    path = Path(data_dir) / POOL_OVERRIDES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "pinned": sorted(ov.pinned),
        "blacklist": sorted(ov.blacklist),
        "manual": sorted(ov.manual),
        "category_caps": {c.value: n for c, n in ov.category_caps.items()},
    }
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
