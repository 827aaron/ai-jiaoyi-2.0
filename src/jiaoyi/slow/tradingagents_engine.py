"""TradingAgents（Apache-2.0，作为依赖，不复制代码）+ DeepSeek 的会议引擎。

只在这个文件里导入 ``tradingagents`` / ``langchain_core``，且都是用到时才导入，
所以没装 ``jiaoyi[slow]`` 或没有 key 时其余代码和测试照常跑。

我们对 TradingAgents 做的三处接入，不改它的源码：
1. 配置：deepseek provider、中文、1 轮辩论 + 1 轮风控、最大输出 8192、6 次重试
2. 数据：往它的 ``VENDOR_METHODS`` 里登记一个 ``binance`` 数据源（K 线、指标），
   美股永续的财报 / 新闻仍走它自带的工具
3. 输出：它的结论是一段文字，再用一次便宜的结构化调用转成 ``CardSymbol``，校验失败重试一次

⚠ 本文件对着 TradingAgents 0.7.0 的公开接口写成，还没用真 key 跑过；M2 拿到 DeepSeek key 后
要做一次真机验证（见 PR 说明）。
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field, ValidationError

from jiaoyi.contracts import (
    BattleCard,
    Bias,
    CardSymbol,
    Category,
    KeyLevel,
    LevelKind,
    Range,
    RiskConfig,
    SignalKind,
    SizeTier,
    SymbolPoolEntry,
)
from jiaoyi.interfaces import SecretStore
from jiaoyi.slow.commander import Analysis, Pick, PickResult

_STOCK_LIKE = frozenset({Category.US_TECH, Category.CRYPTO_STOCK})


@dataclass(frozen=True)
class TradingAgentsSettings:
    provider: str = "deepseek"
    quick_model: str = "deepseek-flash"
    deep_model: str = "deepseek-v4-pro"
    output_language: str = "Chinese"
    debate_rounds: int = 1
    risk_rounds: int = 1
    max_tokens: int = 8192
    max_retries: int = 6
    # 每百万 token 的美元价（输入, 输出）。⚠ 占位值，上线前按 DeepSeek 官网核对。
    price_per_mtok: Mapping[str, tuple[float, float]] = field(
        default_factory=lambda: {"flash": (0.15, 0.60), "pro": (0.50, 2.00)}
    )
    results_dir: str = "data/tradingagents"


def build_config(base: Mapping[str, Any], s: TradingAgentsSettings) -> dict[str, Any]:
    """在 TradingAgents 的 ``DEFAULT_CONFIG`` 上叠加我们的设置，返回新字典。"""
    cfg = dict(base)
    cfg.update(
        llm_provider=s.provider,
        quick_think_llm=s.quick_model,
        deep_think_llm=s.deep_model,
        output_language=s.output_language,
        max_debate_rounds=s.debate_rounds,
        max_risk_discuss_rounds=s.risk_rounds,
        max_tokens=s.max_tokens,
        llm_max_retries=s.max_retries,
        results_dir=os.path.join(s.results_dir, "logs"),
        data_cache_dir=os.path.join(s.results_dir, "cache"),
        memory_log_path=os.path.join(s.results_dir, "memory.md"),
    )
    vendors = dict(cfg.get("data_vendors", {}))
    vendors.update(core_stock_apis="binance", technical_indicators="binance")
    cfg["data_vendors"] = vendors
    return cfg


def ta_ticker(entry: SymbolPoolEntry) -> tuple[str, str]:
    """币安代码 → (TradingAgents 的 ticker, asset_type)。美股永续用股票代码走财报 / 新闻。"""
    base = entry.symbol.removesuffix("USDT")
    if entry.category in _STOCK_LIKE:
        return base, "stock"
    return f"{base}-USD", "crypto"


def binance_symbol(ticker: str) -> str:
    """TradingAgents 传来的 ticker（BTC / BTC-USD / BTCUSDT）→ 币安永续代码。"""
    t = ticker.upper().removesuffix("-USD").removesuffix("USDT")
    return f"{t}USDT"


# ---------------------------------------------------------------- 币安数据源
_KL = "https://fapi.binance.com/fapi/v1/klines"


def _klines(symbol: str, interval: str, limit: int, end: datetime | None = None) -> list[Any]:
    params: dict[str, Any] = {
        "symbol": binance_symbol(symbol),
        "interval": interval,
        "limit": limit,
    }
    if end is not None:
        params["endTime"] = int(end.timestamp() * 1000)
    r = httpx.get(_KL, params=params, timeout=10.0)
    r.raise_for_status()
    data: list[Any] = r.json()
    return data


def get_binance_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    """登记为 TradingAgents 的 ``get_stock_data``：小时线 CSV（最近 ~200 根）。"""
    end = datetime.fromisoformat(end_date).replace(tzinfo=UTC)
    rows = _klines(symbol, "1h", 200, end)
    lines = ["Date,Open,High,Low,Close,Volume,QuoteVolume"]
    for k in rows:
        ts = datetime.fromtimestamp(k[0] / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{ts},{k[1]},{k[2]},{k[3]},{k[4]},{k[5]},{k[7]}")
    return (
        f"# {binance_symbol(symbol)} 币安永续 1h K 线（{start_date} → {end_date}）\n"
        + "\n".join(lines)
    )


def _ema(xs: Sequence[float], n: int) -> list[float]:
    k = 2 / (n + 1)
    out: list[float] = []
    for x in xs:
        out.append(x if not out else x * k + out[-1] * (1 - k))
    return out


def _indicator(
    name: str, o: list[float], h: list[float], low: list[float], c: list[float]
) -> list[float]:
    if name.endswith("_sma"):
        n = int(name.split("_")[1])
        return [sum(c[max(0, i - n + 1) : i + 1]) / min(i + 1, n) for i in range(len(c))]
    if name.endswith("_ema"):
        return _ema(c, int(name.split("_")[1]))
    if name == "rsi":
        gains = [max(c[i] - c[i - 1], 0) for i in range(1, len(c))]
        losses = [max(c[i - 1] - c[i], 0) for i in range(1, len(c))]
        ag, al = _ema(gains, 14), _ema(losses, 14)
        return [50.0] + [
            100.0 if x == 0 else 100 - 100 / (1 + g / x) for g, x in zip(ag, al, strict=True)
        ]
    if name == "atr":
        tr = [h[0] - low[0]] + [
            max(h[i] - low[i], abs(h[i] - c[i - 1]), abs(low[i] - c[i - 1]))
            for i in range(1, len(c))
        ]
        return _ema(tr, 14)
    if name in ("macd", "macds", "macdh"):
        macd = [a - b for a, b in zip(_ema(c, 12), _ema(c, 26), strict=True)]
        sig = _ema(macd, 9)
        return {
            "macd": macd,
            "macds": sig,
            "macdh": [m - s for m, s in zip(macd, sig, strict=True)],
        }[name]
    raise ValueError(f"币安数据源暂不支持指标 {name}")


def get_binance_indicators(
    symbol: str, indicator: str, as_of_date: str, look_back_days: int
) -> str:
    """登记为 TradingAgents 的 ``get_indicators``：对小时线算 TradingAgents 常用的几个指标。"""
    end = datetime.fromisoformat(as_of_date).replace(tzinfo=UTC)
    rows = _klines(symbol, "1h", 500, end)
    o, h, lo, c = ([float(k[i]) for k in rows] for i in (1, 2, 3, 4))
    vals = _indicator(indicator, o, h, lo, c)
    n = min(look_back_days * 24, len(vals))
    out = [
        f"{datetime.fromtimestamp(k[0] / 1000, tz=UTC):%Y-%m-%d %H:%M}: {v:.6g}"
        for k, v in zip(rows[-n:], vals[-n:], strict=True)
    ]
    return f"## {indicator}（{binance_symbol(symbol)}，1h）\n" + "\n".join(reversed(out))


def register_binance_vendor() -> None:
    """把 ``binance`` 加进 TradingAgents 的数据路由表。重复调用无害。"""
    from tradingagents.dataflows import router

    router.VENDOR_METHODS["get_stock_data"]["binance"] = get_binance_stock_data
    router.VENDOR_METHODS["get_indicators"]["binance"] = get_binance_indicators


# ---------------------------------------------------------------- LLM 的结构化输出
class _LevelDraft(BaseModel):
    price: float = Field(gt=0)
    kind: LevelKind
    note: str = ""


class _CardSymbolDraft(BaseModel):
    bias: Bias
    key_levels: list[_LevelDraft] = Field(default_factory=list)
    allowed_signals: list[SignalKind] = Field(default_factory=list)
    size_tier_max: SizeTier
    notional_min: float = Field(ge=0)
    notional_max: float = Field(ge=0)
    leverage_min: float = Field(ge=0)
    leverage_max: float = Field(ge=0)
    stop_atr_min: float = Field(ge=0)
    stop_atr_max: float = Field(ge=0)
    take_profit_r_min: float = Field(ge=0)
    take_profit_r_max: float = Field(ge=0)
    reason: str = Field(min_length=1, description="中文，一两句话")


class _PickDraft(BaseModel):
    class _P(BaseModel):
        symbol: str
        reason: str

    regime: str = Field(min_length=1, description="对大盘的一句话判断，中文")
    picks: list[_P]


def draft_to_card_symbol(d: _CardSymbolDraft, entry: SymbolPoolEntry) -> CardSymbol:
    def rng(lo: float, hi: float) -> Range:
        return Range(min=min(lo, hi), max=max(lo, hi))

    return CardSymbol(
        symbol=entry.symbol,
        category=entry.category,
        bias=d.bias,
        key_levels=[KeyLevel(price=x.price, kind=x.kind, note=x.note) for x in d.key_levels],
        allowed_signals=d.allowed_signals if d.bias is not Bias.NO_TRADE else [],
        size_tier_max=d.size_tier_max,
        notional_usdt=rng(d.notional_min, d.notional_max),
        leverage=rng(d.leverage_min, d.leverage_max),
        stop_atr=rng(d.stop_atr_min, d.stop_atr_max),
        take_profit_r=rng(d.take_profit_r_min, d.take_profit_r_max),
        reason=d.reason,
    )


# ---------------------------------------------------------------- 引擎
class TradingAgentsEngine:
    """满足 ``commander.MeetingEngine``。每次开会新建一个 TradingAgents 图（它不是并发安全的）。"""

    def __init__(
        self,
        risk: RiskConfig,
        secrets: SecretStore,
        settings: TradingAgentsSettings | None = None,
        today: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        key = secrets.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("缺少 DEEPSEEK_API_KEY：先跑一次 setup-keys")
        os.environ.setdefault("DEEPSEEK_API_KEY", key)  # TradingAgents 从环境变量读
        self._risk = risk
        self._s = settings or TradingAgentsSettings()
        self._today = today
        register_binance_vendor()

    def _cost(self, usage: Mapping[str, Mapping[str, Any]]) -> float:
        total = 0.0
        for model, u in usage.items():
            tier = "pro" if "pro" in model else "flash"
            pin, pout = self._s.price_per_mtok[tier]
            total += (u.get("input_tokens", 0) * pin + u.get("output_tokens", 0) * pout) / 1e6
        return round(total, 6)

    def _graph(self, analysts: tuple[str, ...], handler: Any) -> Any:
        from tradingagents.default_config import build_default_config
        from tradingagents.graph.trading_graph import (
            TradingAgentsGraph,
        )

        cfg = build_config(build_default_config(), self._s)
        return TradingAgentsGraph(selected_analysts=analysts, config=cfg, callbacks=[handler])

    async def pick(
        self, candidates: Sequence[SymbolPoolEntry], max_n: int, previous: BattleCard | None
    ) -> PickResult:
        if max_n <= 0 or not candidates:
            return PickResult(regime=previous.regime if previous else "无新增标的", picks=[])
        return await asyncio.to_thread(self._pick_sync, list(candidates), max_n, previous)

    def _pick_sync(
        self, candidates: list[SymbolPoolEntry], max_n: int, previous: BattleCard | None
    ) -> PickResult:
        from langchain_core.callbacks import UsageMetadataCallbackHandler

        handler = UsageMetadataCallbackHandler()
        llm = self._graph(("market",), handler).quick_thinking_llm.with_structured_output(
            _PickDraft
        )
        table = "\n".join(
            f"{e.symbol} 类别={e.category.value} 分数={e.score:.2f} 理由={'、'.join(e.reasons)}"
            for e in candidates
        )
        prev = f"上一张卡的大盘判断：{previous.regime}\n" if previous else ""
        draft: _PickDraft = llm.invoke(
            f"你是加密永续合约的总盘军师。现在是 {self._today():%Y-%m-%d %H:%M} UTC。\n{prev}"
            f"下面是代码筛出的候选池。请先用一句中文判断大盘，再挑最多 {max_n} 个今天最值得"
            f"开会研究的标的（只能从表里挑），每个给一句理由。\n{table}",
            config={"callbacks": [handler]},
        )
        valid = {e.symbol for e in candidates}
        picks = [Pick(p.symbol, p.reason) for p in draft.picks if p.symbol in valid][:max_n]
        return PickResult(draft.regime, picks, self._cost(handler.usage_metadata))

    async def analyze(self, entry: SymbolPoolEntry, regime: str) -> Analysis:
        return await asyncio.to_thread(self._analyze_sync, entry, regime)

    def _analyze_sync(self, entry: SymbolPoolEntry, regime: str) -> Analysis:
        from langchain_core.callbacks import UsageMetadataCallbackHandler

        handler = UsageMetadataCallbackHandler()
        ticker, asset_type = ta_ticker(entry)
        analysts = (
            ("market", "social", "news", "fundamentals")
            if asset_type == "stock"
            else ("market", "social", "news")
        )
        graph = self._graph(analysts, handler)
        state, _rating = graph.propagate(ticker, f"{self._today():%Y-%m-%d}", asset_type=asset_type)
        decision = str(state.get("final_trade_decision", ""))
        if not decision:
            raise RuntimeError(f"{entry.symbol}：TradingAgents 没有给出结论")

        r = self._risk
        prompt = (
            "把下面的交易结论整理成作战指令。所有数值必须落在硬上限内：单笔名义价值 ≤ "
            f"{r.max_notional_per_trade_usdt:g} USDT，杠杆 ≤ {r.max_leverage} 倍。结论偏弱、"
            "矛盾或财报前夜之类就选 no_trade。止损用 ATR 倍数区间（如 1–2），止盈用止损距离的"
            "倍数区间（如 1.5–3）。key_levels 只填结论里明确提到的价位。reason 用中文。\n"
            f"标的：{entry.symbol}（{entry.category.value}）\n大盘：{regime}\n结论：\n{decision}"
        )
        llm = graph.quick_thinking_llm.with_structured_output(_CardSymbolDraft)
        last: Exception | None = None
        for _ in range(2):
            try:
                draft = llm.invoke(prompt, config={"callbacks": [handler]})
                cs = draft_to_card_symbol(draft, entry)
                return Analysis(cs, self._cost(handler.usage_metadata))
            except (ValidationError, ValueError) as exc:
                last = exc
        raise RuntimeError(f"{entry.symbol}：作战指令格式不合格：{last}")
