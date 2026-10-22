"""硬上限（你定，AI 改不了）和运行配置。

规则（设计文档 §四、§十一）：
- 每个字段都是**必填**，模型里不给默认值。配置缺项、多出未知字段、超出合理范围，
  程序拒绝启动。保守默认值写在 ``config/risk.example.toml``，第一次用时复制成
  ``config/risk.toml`` 再按需改。
- 下层只能更紧：作战卡的范围、拍板人的档位都不能超过这里的数。

读文件和报错见 ``jiaoyi.core.config``。
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from jiaoyi.contracts.common import Category, Contract, RunMode


class RiskConfig(Contract):
    """硬上限。范围是“合理范围”，不是建议值；建议值见示例文件。"""

    risk_per_trade_pct: float = Field(
        ge=0.05, le=2.0, description="每笔最多亏总资金的百分比。保守值 1.0。"
    )
    max_leverage: int = Field(ge=1, le=10, description="最大杠杆。保守值 5。")
    max_total_exposure_x: float = Field(
        gt=0.0, le=5.0, description="所有持仓名义价值合计 / 总资产的上限。保守值 3。"
    )
    category_exposure_x: dict[Category, float] = Field(
        description="每个相关组的敞口上限（总资产的倍数），不能超过总敞口上限。"
    )
    daily_loss_halt_pct: float = Field(
        ge=0.5, le=10.0, description="当日亏损达到这个百分比后只允许平仓。保守值 3。"
    )
    max_notional_per_trade_usdt: float = Field(
        ge=10.0, le=100_000.0, description="单笔名义价值上限，USDT。"
    )
    max_open_positions: int = Field(ge=1, le=50)
    lifeboat_stop_multiplier: float = Field(
        ge=1.1, le=3.0, description="保命止损距离 = 正常止损距离 × 这个倍数。保守值 1.5。"
    )
    max_spread_bps: float = Field(ge=0.5, le=50.0, description="价差超过就不开仓。")
    max_slippage_bps: float = Field(ge=1.0, le=100.0)
    taker_fee_bps: float = Field(ge=0.0, le=20.0, description="单边吃单费率，用于手续费门槛。")
    min_tp_fee_multiple: float = Field(
        ge=1.0, le=10.0, description="止盈距离至少是来回手续费的几倍，不够就拒单。"
    )
    day_reset_tz: Literal["Asia/Bangkok", "UTC"] = Field(
        description="“今日盈亏”和日亏熔断按哪个时区 0 点清零。"
    )

    @model_validator(mode="after")
    def _check(self) -> RiskConfig:
        missing = set(Category) - set(self.category_exposure_x)
        if missing:
            raise ValueError(f"category_exposure_x 缺少分组：{sorted(missing)}")
        for cat, cap in self.category_exposure_x.items():
            if not 0.0 <= cap <= self.max_total_exposure_x:
                total = self.max_total_exposure_x
                raise ValueError(f"分组 {cat} 的上限 {cap} 必须在 0 到总敞口上限 {total} 之间")
        return self


class FastLoopConfig(Contract):
    """快环的门槛和节奏（哨兵、拍板人、保镖的档位换算、持仓管家、巡逻兵）。

    这些是策略参数，不是硬上限：只能在硬上限以内起作用，保镖仍按 ``RiskConfig`` 封顶。
    每项都有默认值（契约规则：新加字段必须给默认值，旧的 app.toml 照样能读），
    但同样有合理范围，超范围拒绝启动。键名在 ``config/app.example.toml`` 的 ``[fast_loop]``。
    """

    # ---- 拍板人 / 决策模型
    jev_model: str = Field(
        default="jev-latest", min_length=1, description="锁定的 Jev 模型版本，写进日志用于校准。"
    )
    jev_usd_per_mtok: float = Field(
        default=0.042, ge=0.0, le=10.0, description="每百万输入 token 美元。"
    )
    jev_max_consecutive_errors: int = Field(
        default=5, ge=1, le=100, description="连续出错这么多次后暂停调用一段时间；成功一次就清零。"
    )
    jev_error_cooldown_s: int = Field(default=60, ge=5, le=3600)
    min_p_genuine: float = Field(default=0.6, ge=0.5, le=0.99, description="“真信号”概率门槛。")
    min_p_take: float = Field(default=0.6, ge=0.5, le=0.99, description="“做”概率门槛。")
    min_p_direction: float = Field(
        default=0.45, ge=0.34, le=0.99, description="信号方向（up/down）的概率门槛。"
    )
    max_p_toxic: float = Field(default=0.5, ge=0.05, le=0.95, description="订单流有毒概率上限。")
    # ---- 保镖的档位换算
    signal_max_age_s: float = Field(
        default=5.0, ge=0.5, le=60.0, description="信号从触发到保镖审核超过这么久就作废。"
    )
    size_fraction_s: float = Field(
        default=0.5, gt=0.0, le=1.0, description="S 档用每笔风险的几成。"
    )
    size_fraction_m: float = Field(default=0.75, gt=0.0, le=1.0, description="M 档；L 档固定 1.0。")
    stop_level_buffer_atr: float = Field(
        default=0.1, ge=0.0, le=1.0, description="止损放在关键位外侧多少个 ATR。"
    )
    max_stop_atr: float = Field(
        default=4.0,
        ge=0.5,
        le=10.0,
        description="作战卡止损 ATR 倍数的上限，超过整张卡这个标的不做。",
    )
    min_tp_r: float = Field(default=1.0, ge=0.5, le=5.0, description="作战卡止盈倍数下限。")
    max_tp_r: float = Field(default=6.0, ge=1.0, le=20.0, description="作战卡止盈倍数上限。")
    time_limit_s: int = Field(default=7200, ge=60, le=86400, description="单笔最长持仓时间。")
    # ---- 持仓管家
    keeper_close_p: float = Field(default=0.6, ge=0.5, le=0.99, description="平仓概率过线。")
    keeper_close_p_strong: float = Field(
        default=0.85, ge=0.5, le=1.0, description="概率明显过线时不用等第二次确认。"
    )
    keeper_confirmations: int = Field(default=2, ge=1, le=5, description="连续几次一致才平仓。")
    # ---- 巡逻兵
    patrol_shock_pct_1m: float = Field(
        default=2.0, ge=0.2, le=20.0, description="1 分钟内对持仓不利的波动达到这个百分比就全平。"
    )
    patrol_alert_pct_1m: float = Field(
        default=1.0, ge=0.1, le=20.0, description="达到这个百分比就让管家立刻复查。"
    )
    # ---- 哨兵
    breakout_vol_ratio: float = Field(default=3.0, ge=1.0, le=50.0)
    surge_vol_ratio: float = Field(default=4.0, ge=1.0, le=50.0)
    surge_taker_ratio: float = Field(default=0.7, ge=0.5, le=1.0)
    liq_min_usdt_1m: float = Field(default=100_000.0, ge=0.0, description="1 分钟爆仓额下限。")
    liq_ratio: float = Field(
        default=5.0, ge=1.0, le=100.0, description="1 分钟爆仓额 / 30 分钟均值。"
    )
    liq_drop_pct_5m: float = Field(
        default=1.0, ge=0.1, le=20.0, description="爆仓砸出的 5 分钟跌幅。"
    )
    crowded_funding_rate: float = Field(
        default=0.0005,
        ge=0.0001,
        le=0.01,
        description="资金费率绝对值达到这个算拥挤（0.0005=0.05%）。",
    )
    session_window_min: int = Field(
        default=5, ge=1, le=60, description="开盘 / 资金费结算前后几分钟。"
    )
    session_vol_ratio: float = Field(default=2.0, ge=1.0, le=50.0)

    @model_validator(mode="after")
    def _check(self) -> FastLoopConfig:
        if self.size_fraction_s > self.size_fraction_m:
            raise ValueError("size_fraction_s 不能大于 size_fraction_m")
        if self.keeper_close_p_strong < self.keeper_close_p:
            raise ValueError("keeper_close_p_strong 不能小于 keeper_close_p")
        if self.min_tp_r > self.max_tp_r:
            raise ValueError("min_tp_r 不能大于 max_tp_r")
        if self.patrol_alert_pct_1m > self.patrol_shock_pct_1m:
            raise ValueError("patrol_alert_pct_1m 不能大于 patrol_shock_pct_1m")
        return self


class AppConfig(Contract):
    """运行配置：模式、开关、节奏。同样全部必填，超范围拒绝启动。"""

    mode: RunMode
    predictor_enabled: bool = Field(description="预测员（Kronos）开关。")
    signal_dedup_s: int = Field(ge=5, le=600, description="同一去重键的冷却时间。")
    jev_timeout_ms: int = Field(ge=100, le=5000, description="超时即放弃本次开仓。")
    keeper_interval_s: int = Field(ge=5, le=120)
    reconcile_interval_s: int = Field(ge=10, le=300)
    slow_loop_interval_min: int = Field(ge=15, le=240)
    pool_size: int = Field(ge=5, le=100, description="候选池大小，设计值 30–50。")
    card_pick_max: int = Field(ge=1, le=50, description="军师每次最多挑几个，设计值 10–20。")
    data_dir: str = Field(min_length=1, description="决策日志、模块状态等写在这里。")
    fast_loop: FastLoopConfig = Field(
        default_factory=FastLoopConfig, description="快环门槛；不写就用默认值（见示例）。"
    )

    @model_validator(mode="after")
    def _check(self) -> AppConfig:
        if self.card_pick_max > self.pool_size:
            raise ValueError("card_pick_max 不能大于 pool_size")
        return self
