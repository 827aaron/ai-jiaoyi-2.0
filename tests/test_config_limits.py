"""硬上限的“拒绝启动”测试：每个字段缺项、越界、拼错都必须被拒，边界值必须被接受。

范围从模型的字段约束里读出来，不手抄一遍：以后有人放宽某个范围，这里的越界用例会跟着变，
硬上限只拦物理 / 交易所约束（Aaron 2026-10-23），最后几个测试钉死这些约束和保守值提示。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import annotated_types as at
import pytest
from pydantic import BaseModel
from pydantic.fields import FieldInfo

from jiaoyi.alerting.kinds import KINDS
from jiaoyi.contracts import AppConfig, RiskConfig
from jiaoyi.core import profiles
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


OPTIONAL_RISK_FIELDS = {
    "max_notional_per_trade_x",
    "max_notional_per_trade_usdt",
    "max_spread_bps",
    "max_slippage_bps",
}


@pytest.mark.parametrize(
    "field", [f for f in RiskConfig.model_fields if f not in OPTIONAL_RISK_FIELDS]
)
def test_every_risk_field_is_required(tmp_path: Path, field: str) -> None:
    data = {k: v for k, v in b.RISK_DEFAULTS.items() if k != field}
    with pytest.raises(ConfigError, match=f"{field}: 缺少这一项"):
        load_risk_config(_write(tmp_path, data))


# fast_loop、predictor 是有默认值的可选段；reconciler_close_unknown_positions 是默认关的可选开关、
# jev_max_concurrent 默认 4；
# profile 默认 2h，档位管的键不写就取档位默认（core/profiles.py）；其余字段必填
OPTIONAL_APP_FIELDS = {
    "fast_loop",
    "predictor",
    "reconciler_close_unknown_positions",
    "trade_notices",
    *(f"notify_{k}" for k in KINDS),
    "credit_low_floor_usd",
    "credit_resend_hours",
    "jev_max_concurrent",  # 默认 4，老配置文件没有这一行
    "protection_stale_s",  # 默认 120，老配置文件没有这一行
    "sim_environment",
    "meeting_interval",
    "hold",
    *profiles.defaults("1h", "app"),
}


@pytest.mark.parametrize(
    "field", [f for f in AppConfig.model_fields if f not in OPTIONAL_APP_FIELDS]
)
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
    # 示例里价差 / 滑点的固定 bp 注释掉了（只按止损比例），夹具保留固定值好测「取小」
    fixed = {"max_spread_bps": None, "max_slippage_bps": None}
    assert shipped == b.risk_config().model_copy(update=fixed)


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


# ---- Aaron 2026-10-23：硬上限数值由人定，只拦不合理的值；超出保守值只提示
def test_only_impossible_values_are_refused() -> None:
    """Aaron 2026-10-23：硬围栏都删掉，只给推荐；只拦逻辑上不成立的值。"""
    f = RiskConfig.model_fields
    assert _bounds(f["max_leverage"]) == {"ge": 1.0}, "杠杆能开多少由币安按合约决定"
    assert _bounds(f["max_total_exposure_x"]) == {"gt": 0.0}
    assert _bounds(f["risk_per_trade_pct"]) == {"gt": 0.0, "le": 100.0}
    assert _bounds(f["daily_loss_halt_pct"]) == {"gt": 0.0, "le": 100.0}
    assert _bounds(f["lifeboat_stop_multiplier"]) == {"gt": 1.0}, "保命止损必须比正常止损更宽"


def test_leverage_above_binance_max_refuses_with_reason(tmp_path: Path) -> None:
    ok = load_risk_config(_write(tmp_path, {**b.RISK_DEFAULTS, "max_leverage": 125}))
    assert ok.max_leverage == 125
    with pytest.raises(ConfigError, match="币安 U 本位合约最高 125 倍"):
        load_risk_config(_write(tmp_path, {**b.RISK_DEFAULTS, "max_leverage": 126}, "b.toml"))


def test_notional_cap_follows_equity() -> None:
    cfg = b.risk_config(max_notional_per_trade_x=2.0)
    assert cfg.notional_cap(1_000.0) == 2_000.0
    assert cfg.notional_cap(5_000.0) == 10_000.0
    assert cfg.notional_cap(None) == float("inf")  # 不知道权益：风控员按实时权益封顶
    topped = b.risk_config(max_notional_per_trade_x=2.0, max_notional_per_trade_usdt=3_000.0)
    assert topped.notional_cap(5_000.0) == 3_000.0
    assert topped.notional_cap(None) == 3_000.0


def test_aaron_paper_values_are_accepted_with_cautions(tmp_path: Path) -> None:
    data = {
        **b.RISK_DEFAULTS,
        "risk_per_trade_pct": 5.0,
        "daily_loss_halt_pct": 15.0,
        "max_leverage": 10,
    }
    risk = load_risk_config(_write(tmp_path, data))
    hits = risk.cautions()
    assert sorted(h.split(" ")[0] for h in hits) == [
        "daily_loss_halt_pct",
        "max_leverage",
        "risk_per_trade_pct",
    ]
    assert "推荐 1" in hits[0]


def test_conservative_defaults_have_no_cautions() -> None:
    assert b.risk_config().cautions() == []


def test_daily_halt_below_one_lifeboat_loss_is_only_a_caution(tmp_path: Path) -> None:
    data = {**b.RISK_DEFAULTS, "risk_per_trade_pct": 1.0, "daily_loss_halt_pct": 1.2}
    hits = load_risk_config(_write(tmp_path, data)).cautions()
    assert any("满仓位的单子会被风控员全部拒掉" in h for h in hits)


@pytest.mark.parametrize(
    ("equity", "risk", "lev", "positions"),
    [
        (None, 1.0, 5, 5),  # 不知道资金：按 1000U
        (300.0, 1.0, 10, 2),
        (50_000.0, 0.75, 5, 5),
        (200_000.0, 0.5, 5, 5),
    ],
)
def test_recommendations_follow_equity(
    equity: float | None, risk: float, lev: int, positions: int
) -> None:
    from jiaoyi.contracts.risk_config import recommended_caps

    rec = recommended_caps(equity)
    assert rec["risk_per_trade_pct"].value == risk
    assert rec["daily_loss_halt_pct"].value == risk * 3
    assert rec["max_leverage"].value == lev
    assert rec["max_open_positions"].value == positions
    assert rec["max_notional_per_trade_x"].value == 2.0
    assert set(rec) <= set(RiskConfig.model_fields)


def test_shipped_example_matches_the_1000u_recommendation() -> None:
    from jiaoyi.contracts.risk_config import recommended_caps
    from tests.conftest import ROOT

    shipped = load_risk_config(ROOT / "config" / "risk.example.toml")
    for name, rec in recommended_caps(None).items():
        assert getattr(shipped, name) == rec.value, name
