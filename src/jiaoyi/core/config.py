"""读硬上限和运行配置。缺文件、缺项、超范围一律抛 ``ConfigError``，程序拒绝启动。"""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import ValidationError

from jiaoyi.contracts import AppConfig, RiskConfig

DEFAULT_RISK_PATH = Path("config/risk.toml")
DEFAULT_APP_PATH = Path("config/app.toml")


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


def load_app_config(path: Path = DEFAULT_APP_PATH) -> AppConfig:
    data = _read_toml(path)
    try:
        return AppConfig.model_validate(data)
    except ValidationError as e:
        raise _explain(path, e) from e
