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

    @model_validator(mode="after")
    def _check(self) -> AppConfig:
        if self.card_pick_max > self.pool_size:
            raise ValueError("card_pick_max 不能大于 pool_size")
        return self
