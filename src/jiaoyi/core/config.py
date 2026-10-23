"""读硬上限和运行配置。缺文件、缺项、超范围一律抛 ``ConfigError``，程序拒绝启动。"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

from pydantic import ValidationError

from jiaoyi.contracts import AppConfig, RiskConfig
from jiaoyi.core import profiles

DEFAULT_RISK_PATH = Path("config/risk.toml")
DEFAULT_APP_PATH = Path("config/app.toml")

logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """配置不合格。消息是给人看的中文，直接打印即可。"""


def _read_toml(path: Path) -> dict[str, object]:
    if not path.is_file():
        raise ConfigError(
            f"找不到配置文件 {path}。先运行 make init-config，从示例复制一份再按需修改。"
        )
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} 不是合法的 TOML：{e}") from e


def _explain(path: Path, err: ValidationError) -> ConfigError:
    lines = [f"{path} 不合格，拒绝启动："]
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "(整体)"
        msg = "缺少这一项" if e["type"] == "missing" else e["msg"]
        lines.append(f"  - {loc}: {msg}")
    return ConfigError("\n".join(lines))


def load_risk_config(path: Path = DEFAULT_RISK_PATH) -> RiskConfig:
    data = _read_toml(path)
    try:
        return RiskConfig.model_validate(data)
    except ValidationError as e:
        raise _explain(path, e) from e


def with_profile(raw: dict[str, object]) -> dict[str, object]:
    """把档位默认垫到 app.toml 原始内容下面：写了的键优先，没写的用档位的。

    ``meeting_interval`` 决定开会节奏，``hold`` 决定持仓相关参数；旧 ``profile`` 键映射成
    ``meeting_interval`` 并告警。"""
    profile = profiles.profile_of(raw)
    hold = profiles.hold_of(raw)
    if notice := profiles.legacy_notice(raw):
        logger.warning(notice)
    top = {k: v for k, v in raw.items() if k not in ("fast_loop", "predictor", "profile")}
    top.setdefault("meeting_interval", profile)
    top.setdefault("hold", hold)
    fast = raw.get("fast_loop", {})
    data = profiles.fill(top, profile, "app", hold)
    data["fast_loop"] = profiles.fill(
        fast if isinstance(fast, dict) else {}, profile, "fast_loop", hold
    )
    pred = raw.get("predictor", {})
    data["predictor"] = profiles.fill(
        pred if isinstance(pred, dict) else {}, profile, "predictor", hold
    )
    return data


def load_app_config(path: Path = DEFAULT_APP_PATH) -> AppConfig:
    data = with_profile(_read_toml(path))
    try:
        return AppConfig.model_validate(data)
    except ValidationError as e:
        raise _explain(path, e) from e
