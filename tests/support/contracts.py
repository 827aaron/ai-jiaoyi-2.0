"""可复用的契约测试套件：给每个接口的实现（桩、假实现、真实现）套同一把尺子。

用法：在自己的测试文件里继承并实现 ``make_*`` 工厂方法，pytest 会自动收集::

    from tests.support.contracts import RiskGuardApprovalContract

    class TestMyRiskGuard(RiskGuardApprovalContract):
        def make_guard(self, cfg):
            return MyRiskGuard(cfg)

套件分两层：

- ``*SafetyContract``：只检查“该拒的必须拒 / 故障必须关闭”。桩也必须通过。
- ``*BehaviorContract`` / ``RiskGuardApprovalContract``：还检查正常路径的行为。真实现必须通过。

风控红线，任何实现都不得违反：Jev 失败不开仓；没有有效作战卡不开仓；放行的指令不得超过
硬上限；日亏熔断后不开仓，新的一天清零后恢复。
"""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

import pytest

from jiaoyi.contracts import (
    AccountState,
    BattleCard,
    Bias,
    CandidateSignal,
    JevResponse,
    OrderIntent,
    PositionRecord,
    RiskConfig,
    RiskVerdict,
    Side,
    SignalKind,
    Trade,
)
from jiaoyi.contracts.position import CloseReason
from jiaoyi.interfaces import (
    DecisionModel,
    EntryGate,
    ExecutorAdapter,
    Keeper,
    LifeboatStop,
    Patrol,
    RiskGuard,
    Trigger,
)
from tests.support import builders as b
from tests.support.fakes import FailingDecisionModel, ScriptedDecisionModel
from tests.support.reference_risk import ReferenceRiskGuard

EPS = 1e-6


# ---------------------------------------------------------------- 风控不变式（可单独复用）
def assert_intent_within_limits(
    intent: OrderIntent,
    cfg: RiskConfig,
    account: AccountState,
    card: BattleCard,
) -> None:
    """放行的指令必须同时满足全部硬上限。回放和属性测试里每个放行都过一遍。"""
    cs = card.for_symbol(intent.symbol)
    assert cs is not None, "放行了作战卡里没有的标的"
    assert intent.approved_by == "risk_guard"
    assert intent.leverage <= cfg.max_leverage, "杠杆超过硬上限"
    assert intent.leverage <= cs.leverage.max + EPS, "杠杆超过作战卡范围"
    cap = cfg.notional_cap(account.equity_usdt)
    assert intent.notional_usdt <= cap + EPS, "单笔名义超过硬上限（权益 × 倍数 / 绝对硬顶）"
    assert intent.notional_usdt <= cs.notional_usdt.max + EPS, "单笔名义超过作战卡范围"
    max_risk = account.equity_usdt * cfg.risk_per_trade_pct / 100.0
    assert intent.risk_usdt <= max_risk * (1 + 1e-6), "单笔风险超过硬上限"
    total_after = account.open_notional_usdt + intent.notional_usdt
    assert total_after <= cfg.max_total_exposure_x * account.equity_usdt + EPS, "总敞口超过硬上限"
    cat_after = account.open_notional_by_category.get(intent.category, 0.0) + intent.notional_usdt
    cat_cap = cfg.category_exposure_x[intent.category] * account.equity_usdt
    assert cat_after <= cat_cap + EPS, "相关组敞口超过硬上限"
    assert account.open_positions < cfg.max_open_positions, "超过持仓笔数上限"
    assert intent.lifeboat_stop_pct > intent.stop_loss_pct, "保命止损没有比正常止损更宽"
    min_lifeboat = intent.stop_loss_pct * cfg.lifeboat_stop_multiplier
    assert intent.lifeboat_stop_pct >= min_lifeboat * (1 - 1e-6), "保命止损宽度不够"
    round_trip_bps = 2 * cfg.taker_fee_bps
    assert intent.take_profit_pct * 100.0 >= cfg.min_tp_fee_multiple * round_trip_bps - EPS, (
        "止盈覆盖不了手续费"
    )


# ---------------------------------------------------------------- 风控员
class RiskGuardSafetyContract:
    """风控员红线：该拒的必须拒。"""

    def make_guard(self, cfg: RiskConfig) -> RiskGuard:
        raise NotImplementedError

    @pytest.fixture
    def cfg(self) -> RiskConfig:
        return b.risk_config()

    def review(
        self,
        cfg: RiskConfig,
        *,
        jev: JevResponse | None = None,
        card: BattleCard | str | None = "default",
        account: AccountState | None = None,
        signal: CandidateSignal | None = None,
    ) -> RiskVerdict:
        the_card = b.card() if isinstance(card, str) else card
        sig = signal or b.signal()
        return self.make_guard(cfg).review(
            sig, jev or b.jev_ok(), the_card, account or b.account(), b.T0, market=sig.features
        )

    @staticmethod
    def assert_rejected(v: RiskVerdict) -> None:
        assert not v.approved
        assert v.intent is None
        assert v.reasons, "拒绝必须写原因"

    def test_jev_failure_never_opens(self, cfg: RiskConfig) -> None:
        for err in ("超时", "解析失败", "429"):
            self.assert_rejected(self.review(cfg, jev=b.jev_failed(err)))

    def test_no_card_never_opens(self, cfg: RiskConfig) -> None:
        self.assert_rejected(self.review(cfg, card=None))

    def test_expired_card_never_opens(self, cfg: RiskConfig) -> None:
        expired = b.card(now=b.T0 - timedelta(hours=3), ttl=timedelta(hours=1))
        assert not expired.is_valid(b.T0)
        self.assert_rejected(self.review(cfg, card=expired))

    def test_symbol_not_in_card_never_opens(self, cfg: RiskConfig) -> None:
        card = b.card([b.card_symbol("ETHUSDT")])
        self.assert_rejected(self.review(cfg, card=card))

    def test_no_trade_bias_never_opens(self, cfg: RiskConfig) -> None:
        card = b.card([b.card_symbol(bias=Bias.NO_TRADE)])
        self.assert_rejected(self.review(cfg, card=card))

    def test_signal_against_card_bias_never_opens(self, cfg: RiskConfig) -> None:
        card = b.card([b.card_symbol(bias=Bias.SHORT)])
        self.assert_rejected(self.review(cfg, card=card, signal=b.signal(side=Side.LONG)))

    def test_signal_kind_not_allowed_never_opens(self, cfg: RiskConfig) -> None:
        card = b.card([b.card_symbol(allowed_signals=[SignalKind.LIQUIDATION_REBOUND])])
        self.assert_rejected(self.review(cfg, card=card))

    def test_paused_never_opens(self, cfg: RiskConfig) -> None:
        self.assert_rejected(self.review(cfg, account=b.account(paused=True)))

    def test_daily_loss_halt_blocks_opening(self, cfg: RiskConfig) -> None:
        at_limit = b.account(equity=9_700.0, day_start=10_000.0)  # 正好 -3%
        assert at_limit.day_pnl_pct <= -cfg.daily_loss_halt_pct
        self.assert_rejected(self.review(cfg, account=at_limit))
        self.assert_rejected(
            self.review(cfg, account=b.account(equity=9_000.0, day_start=10_000.0))
        )

    def test_wide_spread_never_opens(self, cfg: RiskConfig) -> None:
        sig = b.signal(spread_bps=cfg.max_spread_bps + 1.0)
        self.assert_rejected(self.review(cfg, signal=sig))

    def test_full_positions_never_opens(self, cfg: RiskConfig) -> None:
        acct = b.account(positions=cfg.max_open_positions)
        self.assert_rejected(self.review(cfg, account=acct))

    def test_total_exposure_full_never_opens(self, cfg: RiskConfig) -> None:
        acct = b.account(open_notional=cfg.max_total_exposure_x * 10_000.0)
        self.assert_rejected(self.review(cfg, account=acct))

    def test_verdict_reports_the_signal(self, cfg: RiskConfig) -> None:
        assert self.review(cfg).signal_id == "sig-1"


class RiskGuardApprovalContract(RiskGuardSafetyContract):
    """风控员正常路径：放行时的指令必须合规，日亏熔断次日恢复。"""

    def approved(self, cfg: RiskConfig, **kw: Any) -> RiskVerdict:
        v = self.review(cfg, **kw)
        assert v.approved, f"本应放行：{v.reasons}"
        assert v.intent is not None
        return v

    def test_happy_path_is_approved_within_limits(self, cfg: RiskConfig) -> None:
        acct = b.account()
        v = self.approved(cfg, account=acct)
        assert v.intent is not None
        assert_intent_within_limits(v.intent, cfg, acct, b.card())
        assert v.intent.side is Side.LONG
        assert v.intent.signal_id == "sig-1"

    def test_short_signal_gives_short_intent(self, cfg: RiskConfig) -> None:
        v = self.approved(
            cfg, signal=b.signal(side=Side.SHORT), jev=b.jev_ok({"direction": "down"})
        )
        assert v.intent is not None and v.intent.side is Side.SHORT

    def test_risk_never_exceeds_cap_at_any_equity(self, cfg: RiskConfig) -> None:
        wide = b.card([b.card_symbol(notional_usdt={"min": 10.0, "max": 100_000.0})])
        for eq in (1_000.0, 10_000.0, 50_000.0):
            acct = b.account(equity=eq)
            v = self.review(cfg, account=acct, card=wide)
            if v.approved:
                assert v.intent is not None
                assert_intent_within_limits(v.intent, cfg, acct, wide)

    def test_tighter_risk_config_gives_smaller_risk(self) -> None:
        # 名义上限放到很大，让“按风险金额定数量”这条约束成为实际生效的那条
        wide = b.card([b.card_symbol(notional_usdt={"min": 10.0, "max": 100_000.0})])
        big = {"max_notional_per_trade_usdt": 100_000.0, "max_total_exposure_x": 5.0}
        loose = self.approved(b.risk_config(risk_per_trade_pct=1.0, **big), card=wide)
        tight = self.approved(b.risk_config(risk_per_trade_pct=0.25, **big), card=wide)
        assert loose.intent is not None and tight.intent is not None
        assert tight.intent.risk_usdt < loose.intent.risk_usdt

    def test_leverage_never_above_config(self) -> None:
        cfg = b.risk_config(max_leverage=2)
        card = b.card([b.card_symbol(leverage={"min": 1.0, "max": 10.0})])
        v = self.approved(cfg, card=card)
        assert v.intent is not None and v.intent.leverage <= 2

    def test_exposure_cap_shrinks_or_rejects(self, cfg: RiskConfig) -> None:
        near = cfg.max_total_exposure_x * 10_000.0 - 300.0
        card = b.card([b.card_symbol(notional_usdt={"min": 10.0, "max": 2000.0})])
        v = self.review(cfg, account=b.account(open_notional=near), card=card)
        if v.approved:
            assert v.intent is not None
            assert near + v.intent.notional_usdt <= cfg.max_total_exposure_x * 10_000.0 + EPS

    def test_take_profit_too_small_for_fees_is_rejected(self, cfg: RiskConfig) -> None:
        self.assert_rejected(self.review(cfg, signal=b.signal(atr=1.0)))

    def test_daily_loss_resets_on_new_day(self, cfg: RiskConfig) -> None:
        self.assert_rejected(
            self.review(cfg, account=b.account(equity=9_600.0, day_start=10_000.0))
        )
        # 新的一天：起点权益重置为当前权益，亏损从零算起
        self.approved(cfg, account=b.account(equity=9_600.0, day_start=9_600.0))

    def test_loss_just_inside_limit_still_trades(self, cfg: RiskConfig) -> None:
        self.approved(cfg, account=b.account(equity=9_710.0, day_start=10_000.0))

    def test_property_sweep_every_approval_is_within_limits(self) -> None:
        """随机组合配置、账户、信号、回答，所有放行都必须满足硬上限。"""
        rng = random.Random(20261022)
        approvals = 0
        for _ in range(400):
            cfg = b.risk_config(
                risk_per_trade_pct=rng.choice([0.1, 0.5, 1.0, 2.0]),
                max_leverage=rng.choice([1, 2, 5, 10]),
                max_total_exposure_x=rng.choice([0.5, 1.0, 3.0, 5.0]),
                max_notional_per_trade_x=rng.choice([0.05, 0.5, 2.0, 10.0]),
                max_notional_per_trade_usdt=rng.choice([None, 50.0, 500.0, 5_000.0]),
                max_open_positions=rng.choice([1, 3, 5]),
                lifeboat_stop_multiplier=rng.choice([1.1, 1.5, 3.0]),
                category_exposure_x=dict.fromkeys(b.RISK_DEFAULTS["category_exposure_x"], 0.5),
            )
            eq = rng.choice([500.0, 5_000.0, 50_000.0])
            acct = b.account(
                equity=eq,
                day_start=eq * rng.choice([1.0, 1.01, 1.04]),
                open_notional=rng.uniform(0, 3.0) * eq,
                positions=rng.randint(0, 5),
            )
            price = rng.choice([68_000.0, 3_500.0, 0.5])
            sig = b.signal(
                side=rng.choice([Side.LONG, Side.SHORT]),
                price=price,
                atr=price * rng.choice([0.0005, 0.002, 0.01]),
            )
            answers = {
                "direction": "up" if sig.side_hint is Side.LONG else "down",
                "stop_width": rng.choice(["narrow", "mid", "wide"]),
                "size": rng.choice(["S", "M", "L"]),
            }
            card = b.card(
                [
                    b.card_symbol(
                        notional_usdt={"min": 10.0, "max": rng.choice([100.0, 1_000.0, 10_000.0])},
                        leverage={"min": 1.0, "max": rng.choice([1.0, 3.0, 20.0])},
                    )
                ]
            )
            v = self.make_guard(cfg).review(
                sig, b.jev_ok(answers), card, acct, b.T0, market=sig.features
            )
            if v.approved:
                approvals += 1
                assert v.intent is not None
                assert_intent_within_limits(v.intent, cfg, acct, card)
        assert approvals > 0, "随机样本里一次都没放行，套件没有测到放行路径"


# ---------------------------------------------------------------- 拍板人
class EntryGateSafetyContract:
    """拍板人：决策模型出任何问题，都必须返回 ok=False，不能抛异常，不能放行。"""

    def make_gate(self, model: Any) -> EntryGate:
        raise NotImplementedError

    async def test_failing_model_gives_not_ok(self) -> None:
        resp = await self.make_gate(FailingDecisionModel()).evaluate(b.signal(), b.card(), b.T0)
        assert not resp.ok and resp.error and not resp.answers

    async def test_raising_model_is_contained(self) -> None:
        class Boom:
            async def ask(self, request: Any) -> JevResponse:
                raise TimeoutError("模拟超时")

        resp = await self.make_gate(Boom()).evaluate(b.signal(), b.card(), b.T0)
        assert not resp.ok and resp.error


class EntryGateBehaviorContract(EntryGateSafetyContract):
    async def test_asks_the_six_entry_questions(self) -> None:
        model = ScriptedDecisionModel()
        resp = await self.make_gate(model).evaluate(b.signal(), b.card(), b.T0)
        assert resp.ok
        (req,) = model.requests
        assert req.kind == "entry" and req.signal_id == "sig-1" and req.symbol == "BTCUSDT"
        assert {q.id for q in req.questions} == {
            "genuine",
            "direction",
            "toxicity",
            "take",
            "stop_width",
            "size",
        }


# ---------------------------------------------------------------- 决策模型
class DecisionModelContract:
    def make_model(self) -> DecisionModel:
        raise NotImplementedError

    async def test_reply_matches_request_and_obeys_contract(self) -> None:
        req = b.entry_request(b.signal())
        resp = await self.make_model().ask(req)
        assert resp.request_id == req.request_id
        assert resp.ok or (resp.error and not resp.answers)
        assert resp.latency_ms >= 0
        assert resp.model_version


# ---------------------------------------------------------------- 哨兵
class TriggerSafetyContract:
    """哨兵：没有有效作战卡就不出信号；出的信号要带卡号。"""

    def make_trigger(self) -> Trigger:
        raise NotImplementedError

    def test_no_card_no_signals(self) -> None:
        assert self.make_trigger().check(b.features(), None, b.T0) == []

    def test_expired_card_no_signals(self) -> None:
        old = b.card(now=b.T0 - timedelta(hours=5), ttl=timedelta(hours=1))
        assert self.make_trigger().check(b.features(), old, b.T0) == []

    def test_symbol_not_on_card_no_signals(self) -> None:
        card = b.card([b.card_symbol("ETHUSDT")])
        assert self.make_trigger().check(b.features("BTCUSDT"), card, b.T0) == []

    def test_no_trade_symbol_no_signals(self) -> None:
        card = b.card([b.card_symbol(bias=Bias.NO_TRADE)])
        assert self.make_trigger().check(b.features(), card, b.T0) == []

    def test_signals_carry_card_and_respect_bias(self) -> None:
        card = b.card([b.card_symbol(bias=Bias.LONG)])
        for sig in self.make_trigger().check(b.features(), card, b.T0):
            assert sig.card_id == card.card_id
            assert sig.side_hint is Side.LONG
            assert sig.kind in card.symbols[0].allowed_signals


# ---------------------------------------------------------------- 持仓管家 / 巡逻兵
class KeeperSafetyContract:
    """管家：Jev 失败 → 维持现状（hold），绝不因为故障去平仓。"""

    def make_keeper(self, model: Any) -> Keeper:
        raise NotImplementedError

    async def test_jev_failure_means_hold(self) -> None:
        v = await self.make_keeper(FailingDecisionModel()).review(b.position(), b.T0)
        assert v.action == "hold"
        assert v.position_id == "pos-1"


class PatrolSafetyContract:
    """巡逻兵：只会要求平仓，而且只针对手上有的持仓。"""

    def make_patrol(self) -> Patrol:
        raise NotImplementedError

    def test_actions_are_close_only_and_on_held_symbols(self) -> None:
        held: list[PositionRecord] = [b.position("BTCUSDT")]
        ev = Trade(symbol="BTCUSDT", ts=b.T0, price=68_000.0, qty=1.0, taker_side=Side.SHORT)
        act = self.make_patrol().check(ev, held)
        if act is not None:
            assert act.action == "close"
            assert set(act.symbols) <= {"BTCUSDT"}

    def test_no_positions_no_action(self) -> None:
        ev = Trade(symbol="BTCUSDT", ts=b.T0, price=1.0, qty=1.0, taker_side=Side.SHORT)
        assert self.make_patrol().check(ev, []) is None


# ---------------------------------------------------------------- 执行
def an_intent() -> OrderIntent:
    """用参考风控员签发一张合规指令，给执行器套件用。"""
    v = ReferenceRiskGuard(b.risk_config()).review(
        b.signal(), b.jev_ok(), b.card(), b.account(), b.T0
    )
    assert v.intent is not None
    return v.intent


class ExecutorBehaviorContract:
    """执行器：开仓后查得到，平仓后查不到；不接受随便拼的字典。"""

    def make_executor(self) -> ExecutorAdapter:
        raise NotImplementedError

    async def test_open_then_close(self) -> None:
        ex = self.make_executor()
        pos = await ex.open(an_intent())
        assert pos.symbol == "BTCUSDT" and pos.signal_id == "sig-1"
        assert [p.position_id for p in await ex.positions()] == [pos.position_id]
        await ex.close(pos.position_id, CloseReason.MANUAL)
        assert await ex.positions() == []

    async def test_rejects_non_intent(self) -> None:
        ex = self.make_executor()
        with pytest.raises((TypeError, AttributeError, ValueError)):
            await ex.open({"symbol": "BTCUSDT"})  # type: ignore[arg-type]


class LifeboatBehaviorContract:
    def make_lifeboat(self) -> LifeboatStop:
        raise NotImplementedError

    async def test_place_fetch_cancel(self) -> None:
        lb = self.make_lifeboat()
        pos = b.position()
        assert await lb.fetch(pos) is None
        order = await lb.place(pos, 67_000.0)
        assert order.stop_price == 67_000.0
        fetched = await lb.fetch(pos)
        assert fetched is not None and fetched.stop_price == 67_000.0
        await lb.cancel(pos)
        assert await lb.fetch(pos) is None
