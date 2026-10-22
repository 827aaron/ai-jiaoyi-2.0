"""硬上限的“拒绝启动”测试：每个字段缺项、越界、拼错都必须被拒，边界值必须被接受。

范围从模型的字段约束里读出来，不手抄一遍：以后有人放宽某个范围，这里的越界用例会跟着变，
但“放宽到比设计文档更松”要靠评审发现——所以另外钉死了几个最关键的上限（见最后几个测试）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import annotated_types as at
import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from jiaoyi.contracts import AppConfig, RiskConfig
from jiaoyi.core.config import ConfigError, load_app_config, load_risk_config
from tests.support import builders as b


def _toml(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return f'"{v}"'
    return repr(v)


def _write(tmp_path: Path, data: dict[str, Any], name: str = "cfg.toml") -> Path:
    scalars = [f"{k} = {_toml(v)}" for k, v in data.items() if not isinstance(v, dict)]
    tables = []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append(f"[{k}]")
            tables += [f"{kk} = {_toml(vv)}" for kk, vv in v.items()]
    path = tmp_path / name
    path.write_text("\n".join([*scalars, *tables]) + "\n", encoding="utf-8")
    return path


def _bounds(field: FieldInfo) -> dict[str, float]:
    out: dict[str, float] = {}
    for m in field.metadata:
        if isinstance(m, at.Ge):
            out["ge"] = float(m.ge)  # type: ignore[arg-type]
        elif isinstance(m, at.Le):
            out["le"] = float(m.le)  # type: ignore[arg-type]
        elif isinstance(m, at.Gt):
            out["gt"] = float(m.gt)  # type: ignore[arg-type]
        elif isinstance(m, at.Lt):
            out["lt"] = float(m.lt)  # type: ignore[arg-type]
    return out


def _numeric_cases(model: type[BaseModel]) -> list[tuple[str, str, float, bool]]:
    """(字段, 描述, 取值, 是否应被接受)。"""
    cases: list[tuple[str, str, float, bool]] = []
    for name, field in model.model_fields.items():
        bnd = _bounds(field)
        if not bnd:
            continue
        is_int = field.annotation is int
        step = 1.0 if is_int else 1e-3
        if "ge" in bnd:
            cases += [(name, "下限", bnd["ge"], True), (name, "低于下限", bnd["ge"] - step, False)]
        if "gt" in bnd:
            cases += [(name, "等于开区间下限", bnd["gt"], False)]
        if "le" in bnd:
            cases += [(name, "上限", bnd["le"], True), (name, "高于上限", bnd["le"] + step, False)]
    return cases


RISK_CASES = _numeric_cases(RiskConfig)
APP_CASES = _numeric_cases(AppConfig)


def _typed(model: type[BaseModel], field: str, value: float) -> Any:
    return int(value) if model.model_fields[field].annotation is int else value


@pytest.mark.parametrize(("field", "what", "value", "ok"), RISK_CASES)
def test_risk_numeric_bounds(tmp_path: Path, field: str, what: str, value: float, ok: bool) -> None:
    data = {**b.RISK_DEFAULTS, field: _typed(RiskConfig, field, value)}
    # 总敞口上限被拉低时，分组上限要跟着不超过它，否则会因为别的规则被拒
    if field == "max_total_exposure_x":
        data["category_exposure_x"] = dict.fromkeys(b.RISK_DEFAULTS["category_exposure_x"], 0.05)
    path = _write(tmp_path, data)
    if ok:
        assert getattr(load_risk_config(path), field) == data[field]
    else:
        with pytest.raises(ConfigError, match=field):
            load_risk_config(path)


@pytest.mark.parametrize("field", list(RiskConfig.model_fields))
def test_every_risk_field_is_required(tmp_path: Path, field: str) -> None:
    data = {k: v for k, v in b.RISK_DEFAULTS.items() if k != field}
    with pytest.raises(ConfigError, match=f"{field}: 缺少这一项"):
        load_risk_config(_write(tmp_path, data))


@pytest.mark.parametrize("field", list(AppConfig.model_fields))
def test_every_app_field_is_required(tmp_path: Path, field: str) -> None:
    data = {k: v for k, v in b.APP_DEFAULTS.items() if k != field}
    with pytest.raises(ConfigError, match=f"{field}: 缺少这一项"):
        load_app_config(_write(tmp_path, data))


@pytest.mark.parametrize(("field", "what", "value", "ok"), APP_CASES)
def test_app_numeric_bounds(tmp_path: Path, field: str, what: str, value: float, ok: bool) -> None:
    data = {**b.APP_DEFAULTS, field: _typed(AppConfig, field, value)}
    # card_pick_max 不能超过 pool_size，边界测试时把另一个放宽
    if field == "pool_size" and ok:
        data["card_pick_max"] = min(int(value), int(b.APP_DEFAULTS["card_pick_max"]))
    if field == "card_pick_max":
        data["pool_size"] = 100
    path = _write(tmp_path, data)
    if ok:
        assert getattr(load_app_config(path), field) == data[field]
    else:
        with pytest.raises(ConfigError, match=field):
            load_app_config(path)


def test_defaults_in_builders_match_the_shipped_example() -> None:
    """测试夹具的默认值和仓库里的示例文件一致，示例改了这里会提醒。"""
    from tests.conftest import ROOT

    shipped = load_risk_config(ROOT / "config" / "risk.example.toml")
    assert shipped == b.risk_config(max_notional_per_trade_usdt=2000)


def test_unknown_risk_field_refuses(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="max_levarage"):
        load_risk_config(_write(tmp_path, {**b.RISK_DEFAULTS, "max_levarage": 5}))


def test_wrong_type_refuses(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="max_leverage"):
        load_risk_config(_write(tmp_path, {**b.RISK_DEFAULTS, "max_leverage": "five"}))


def test_empty_file_refuses(tmp_path: Path) -> None:
    (tmp_path / "r.toml").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="缺少这一项"):
        load_risk_config(tmp_path / "r.toml")


def test_garbage_file_refuses(tmp_path: Path) -> None:
    (tmp_path / "r.toml").write_text("这不是 toml ===", encoding="utf-8")
    with pytest.raises(ConfigError, match="不是合法的 TOML"):
        load_risk_config(tmp_path / "r.toml")


def test_category_cap_may_equal_but_not_exceed_total(tmp_path: Path) -> None:
    ok = {
        **b.RISK_DEFAULTS,
        "category_exposure_x": dict.fromkeys(b.RISK_DEFAULTS["category_exposure_x"], 3.0),
    }
    assert load_risk_config(_write(tmp_path, ok)).category_exposure_x["alt"] == 3.0
    bad = {**ok, "category_exposure_x": {**ok["category_exposure_x"], "alt": 3.01}}
    with pytest.raises(ConfigError, match="alt"):
        load_risk_config(_write(tmp_path, bad, "bad.toml"))


# ---- 钉死最关键的几个“绝对上限”：设计文档说 AI 和代码都不能比这更松
def test_absolute_ceilings_match_the_design() -> None:
    f = RiskConfig.model_fields
    assert _bounds(f["max_leverage"])["le"] <= 10
    assert _bounds(f["risk_per_trade_pct"])["le"] <= 2.0
    assert _bounds(f["max_total_exposure_x"])["le"] <= 5.0
    assert _bounds(f["daily_loss_halt_pct"])["le"] <= 10.0
    assert _bounds(f["lifeboat_stop_multiplier"])["ge"] > 1.0, "保命止损必须比正常止损更宽"
