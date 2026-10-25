"""验收脚本的测试：好日志全部通过；每一种“违规”日志都要被对应的检查抓住。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jiaoyi.acceptance import STAGE_ROLES, Check, audit, main, read_records
from jiaoyi.backtest import LatencyModel, ReplayEngine, static_card
from jiaoyi.backtest.synthetic import synthetic_trades
from jiaoyi.contracts import DecisionRecord, ModuleStatus, Role
from jiaoyi.contracts.decision_log import Outcome
from jiaoyi.contracts.module_status import Health
from jiaoyi.core.store import JsonlDecisionLog
from tests.support import builders as b
from tests.support.fakes import FakeEntryGate, InMemoryDecisionLog, RuleDecisionModel
from tests.support.reference_risk import ReferenceRiskGuard
from tests.support.simple_pipeline import SimpleFeatureEngine, SimpleTrigger

START = datetime(2026, 10, 20, tzinfo=UTC)
NOW = START + timedelta(hours=5)
RISK = b.risk_config()


async def good_records(hours: int = 4, model: object | None = None) -> list[DecisionRecord]:
    log = InMemoryDecisionLog()
    card = b.card([b.card_symbol()], now=START, ttl=timedelta(days=2))
    eng = ReplayEngine(
        features=SimpleFeatureEngine(),
        trigger=SimpleTrigger(),
        gate=FakeEntryGate(model or RuleDecisionModel(take_prob=1.0)),
        guard=ReferenceRiskGuard(RISK),
        cards=static_card(card),
        risk=RISK,
        log=log,
        latency=LatencyModel(jitter=0.0),
    )
    await eng.run(synthetic_trades("BTCUSDT", START, hours * 3600))
    assert any(r.role is Role.EXECUTOR for r in log.records)
    return sorted(log.records, key=lambda r: r.ts)


def healthy(roles: tuple[Role, ...], age_s: float = 10.0) -> dict[str, ModuleStatus]:
    return {
        r.value: ModuleStatus(role=r, health=Health.OK, ts=NOW - timedelta(seconds=age_s))
        for r in roles
    }


def run(
    records: list[DecisionRecord], statuses: dict[str, ModuleStatus] | None = None, **kw: object
) -> dict[str, Check]:
    checks = audit(
        records,
        statuses if statuses is not None else healthy(STAGE_ROLES["m1"]),
        stage=str(kw.pop("stage", "m1")),
        hours=float(kw.pop("hours", 3)),  # type: ignore[arg-type]
        now=NOW,
        risk=kw.pop("risk", RISK),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )
    return {c.name: c for c in checks}


def failed(checks: dict[str, Check]) -> list[str]:
    return [n for n, c in checks.items() if c.ok is False]


async def test_replay_output_passes_the_audit() -> None:
    assert failed(run(await good_records())) == []


async def test_too_short_run_fails() -> None:
    recs = await good_records()
    assert "运行时长" in failed(run(recs, hours=24))


async def test_empty_log_fails() -> None:
    assert "运行时长" in failed(run([]))


async def test_stale_or_missing_heartbeat_fails() -> None:
    recs = await good_records()
    st = healthy(STAGE_ROLES["m1"], age_s=3600)
    assert "心跳 gate" in failed(run(recs, st))
    del st["risk_guard"]
    assert "心跳 risk_guard" in failed(run(recs, st))


async def test_down_module_fails() -> None:
    recs = await good_records()
    st = healthy(STAGE_ROLES["m1"])
    st["executor"] = ModuleStatus(role=Role.EXECUTOR, health=Health.DOWN, ts=NOW)
    assert "心跳 executor" in failed(run(recs, st))


async def test_open_without_risk_pass_fails() -> None:
    recs = await good_records()
    victim = next(r for r in recs if r.role is Role.EXECUTOR and r.outcome is Outcome.ACTION)
    kept = [
        r
        for r in recs
        if not (
            r.role is Role.RISK_GUARD
            and r.outcome is Outcome.PASS
            and r.signal_id == victim.signal_id
        )
    ]
    assert "每笔开仓都有风控员放行在先" in failed(run(kept))


async def test_open_after_jev_failure_fails() -> None:
    recs = await good_records()
    victim = next(r for r in recs if r.role is Role.EXECUTOR and r.outcome is Outcome.ACTION)
    forged = DecisionRecord(
        record_id="forged-1",
        ts=victim.ts - timedelta(seconds=1),
        role=Role.GATE,
        symbol=victim.symbol,
        signal_id=victim.signal_id,
        outcome=Outcome.ERROR,
        summary="决策模型失败：超时",
    )
    assert "Jev 失败的信号没有开仓" in failed(run([*recs, forged]))


async def test_missing_lifeboat_fails() -> None:
    recs = await good_records()
    victim = next(r for r in recs if r.role is Role.EXECUTOR and r.outcome is Outcome.ACTION)
    kept = [
        r
        for r in recs
        if not (
            r.role is Role.LIFEBOAT
            and r.outcome is Outcome.ACTION
            and r.position_id == victim.position_id
        )
    ]
    assert "每笔开仓 30 秒内挂上保命止损" in failed(run(kept))


async def test_late_lifeboat_fails() -> None:
    recs = await good_records()
    victim = next(r for r in recs if r.role is Role.LIFEBOAT and r.outcome is Outcome.ACTION)
    late = victim.model_copy(update={"ts": victim.ts + timedelta(seconds=120)})
    swapped = [late if r is victim else r for r in recs]
    assert "每笔开仓 30 秒内挂上保命止损" in failed(run(sorted(swapped, key=lambda r: r.ts)))


async def test_intent_over_limit_fails() -> None:
    recs = await good_records()
    victim = next(r for r in recs if r.role is Role.RISK_GUARD and r.outcome is Outcome.PASS)
    bad = victim.model_copy(update={"output": {**victim.output, "leverage": 20}})
    swapped = [bad if r is victim else r for r in recs]
    checks = run(swapped)
    assert "放行指令符合硬上限" in failed(checks)
    assert "杠杆 20" in checks["放行指令符合硬上限"].detail


async def test_garbage_intent_fails() -> None:
    recs = await good_records()
    victim = next(r for r in recs if r.role is Role.RISK_GUARD and r.outcome is Outcome.PASS)
    bad = victim.model_copy(update={"output": {"nonsense": 1}})
    assert "放行指令符合硬上限" in failed(run([bad if r is victim else r for r in recs]))


async def test_too_many_concurrent_positions_fails() -> None:
    recs = await good_records()
    tight = b.risk_config(max_open_positions=1)
    extra = [
        DecisionRecord(
            record_id=f"x{i}",
            ts=START + timedelta(seconds=i),
            role=Role.EXECUTOR,
            position_id=f"p{i}",
            symbol="BTCUSDT",
            outcome=Outcome.ACTION,
            summary="开仓",
        )
        for i in range(3)
    ]
    assert "同时持仓不超上限" in failed(
        run(sorted([*recs, *extra], key=lambda r: r.ts), risk=tight)
    )


async def test_unresolved_reconcile_issue_fails() -> None:
    recs = await good_records()
    issue = DecisionRecord(
        record_id="rc-1",
        ts=START + timedelta(hours=1),
        role=Role.RECONCILER,
        symbol="BTCUSDT",
        outcome=Outcome.ERROR,
        summary="币安上有仓位但没有保命止损",
    )
    assert "对账员没有遗留问题" in failed(run(sorted([*recs, issue], key=lambda r: r.ts)))


async def test_slow_jev_fails() -> None:
    recs = await good_records(model=RuleDecisionModel(take_prob=1.0, latency_ms=4000.0))
    assert "Jev 延迟" in failed(run(recs))


async def test_m0_stage_does_not_require_trading_loop_roles() -> None:
    recs = [r for r in await good_records() if r.role in (Role.EXECUTOR, Role.LIFEBOAT)]
    st = healthy(STAGE_ROLES["m0"])
    assert failed(run(recs, st, stage="m0")) == []


async def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    recs = await good_records()
    log = JsonlDecisionLog(tmp_path)
    for r in recs:
        log.write(r)
    risk = tmp_path / "risk.toml"
    risk.write_text(
        "\n".join(
            f"{k} = {v!r}".replace("'", '"').replace("True", "true")
            for k, v in b.RISK_DEFAULTS.items()
            if not isinstance(v, dict)
        )
        + "\n[category_exposure_x]\n"
        + "\n".join(f"{k} = {v}" for k, v in b.RISK_DEFAULTS["category_exposure_x"].items())
        + "\n",
        encoding="utf-8",
    )
    argv = ["--data", str(tmp_path), "--risk", str(risk), "--no-heartbeat", "--hours", "3"]
    assert main(argv) == 0
    assert "全部通过" in capsys.readouterr().out
    assert main([*argv[:-1], "48"]) == 1
    assert "项失败" in capsys.readouterr().out
    assert len(read_records(tmp_path)) == len(recs)
