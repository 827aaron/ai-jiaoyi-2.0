"""军师团 / 总盘会的提示词：币安永续写法，一张 7 档词表喂四处（tier-prompts-spec v2 §2–§4）。

7 行数字在 ``core/profiles.py``；这里是 3 组文字（short / mid / long），每组中英文放在一起写一次，
公共说明、总盘会挑币、大盘简报、转卡都只从这里取词，改一处四处一起变。
纯文本、不导入 tradingagents，便于快照测试。
"""

from __future__ import annotations

from dataclasses import dataclass

from jiaoyi.core import profiles

_MINUTES = {"30m": 30, "1h": 60, "2h": 120, "4h": 240, "8h": 480, "12h": 720, "24h": 1440}


@dataclass(frozen=True)
class PromptParams:
    """一套提示词要填的数：来自最长持仓档（hold）。自定义时长取不短于它的那档（2h 的写法写，
    {HM} 写实际分钟数，{H} / {FEE} 写那一档的），这是已知限制。"""

    hold: str = "2h"
    style: str = "short"
    hold_minutes: int = 120  # {HM}
    horizon: str = "2 hours"  # {H}
    horizon_zh: str = "2 小时"  # {H_zh}
    indicator: str = "15m"  # {IND}
    fee_pct: int = 35  # {FEE}
    zone_required: bool = True

    @classmethod
    def for_hold(cls, hold: str, hold_minutes: int | None = None) -> PromptParams:
        h = hold if hold in profiles.PROMPT_STYLE else profiles.DEFAULT_HOLD
        return cls(
            hold=h,
            style=profiles.prompt_style(h),
            hold_minutes=hold_minutes or _MINUTES[h],
            horizon=profiles.HORIZON_EN[h],
            horizon_zh=profiles.HORIZON_ZH[h],
            indicator=profiles.indicator_interval(h),
            fee_pct=profiles.fee_share_pct(h),
            zone_required=profiles.zone_required(h),
        )


@dataclass(frozen=True)
class Words:
    """一组文字（一种写法）。"""

    not_this: str
    not_this_zh: str
    window: str
    window_zh: str
    evidence: str
    background: str
    background_zh: str
    habit: str
    habit_zh: str
    change: str
    change_zh: str
    price_action: str  # 多空研究员「价格行为」那一行的周期和内容
    brief_main: tuple[str, ...]  # 简报主字段
    brief_bg: tuple[str, ...]  # 简报背景字段


WORDS: dict[str, Words] = {
    "short": Words(
        not_this=", not today or this week",
        not_this_zh="，不是今天、也不是这周",
        window="the last 15 minutes to 5 hours",
        window_zh="最近 15 分钟到 5 小时",
        evidence=(
            "the % change over the last 15m, 1h and 4h, where price sits in its 24h range, "
            "whether volume is rising or fading, taker buy/sell flow, funding, open-interest "
            "change and recent liquidations"
        ),
        background=(
            "Daily and multi-day readings (20-day MA, daily RSI, 50/200-period averages, 7-day "
            "change) are background only and must never decide the direction on their own."
        ),
        background_zh="20 日线、日线 RSI、7 天涨跌只作背景，不能单独决定方向",
        habit=(
            "after a sharp move over the last few hours, the next {H} more often gives back part "
            "of it than extends it, in both directions. The edge is small (about 53 right calls "
            "in 100), so treat it as a tilt, not a certainty. Do not chase: after a sharp drop do "
            "not open a short near the low, and after a sharp rally do not open a long near the "
            "high; sell into a bounce zone or buy into a dip zone instead. On {IND} bars, an "
            "extreme RSI or a Bollinger-band breach is more often followed by a pullback than by "
            "continuation at this horizon."
        ),
        habit_zh=(
            "最近几小时涨跌多了，接下来 {H_zh} 更容易回头一部分，涨跌两个方向都是；但优势很小，"
            "大约 100 次对 53 次。不追涨杀跌：急跌后不在低点追空，急涨后不在高点追多，"
            "改成反弹到区间再空、回调到区间再多"
        ),
        change=(
            "Weigh the last few hours over the earlier trend: if the last 1-4 hours contradict "
            "it (for example a rebound off the low on rising volume, or a break below a level "
            "that had held), say so and change the view."
        ),
        change_zh=(
            "最近 1–4 小时和之前的走势不一致（比如放量从低点反弹，或者跌破守了很久的位置），"
            "就直说并改判断"
        ),
        price_action=(
            "the % change over the last 15m / 1h / 4h, position in the 24h range, {price_tail}"
        ),
        brief_main=("chg_1h", "chg_4h", "chg_24h", "range_24h", "vol_1h"),
        brief_bg=("sma20", "chg_7d"),
    ),
    "mid": Words(
        not_this=", not this week",
        not_this_zh="，不是这周",
        window="the last 1 hour to 1 day",
        window_zh="最近 1 小时到 1 天",
        evidence=(
            "the % change over the last 1h, 4h and 24h, where price sits in its 24h and 3-day "
            "range, volume, taker flow, funding, open-interest change and liquidations"
        ),
        background="Multi-week readings (20-day MA, daily RSI) are background only.",
        background_zh="20 日线、日线 RSI 只作背景",
        habit=(
            "both effects are weak. A sharp move of the last 1-5 hours tends to give back a "
            "little, while the move of the last day tends to continue a little. Neither alone "
            "decides the direction: look for the two to agree, otherwise stay flat. Do not chase "
            "a move that is already stretched; prefer a stated entry zone."
        ),
        habit_zh=(
            "两种习性都很弱：最近 1–5 小时的急涨急跌容易回一点，最近一天的走势容易再走一点。"
            "两者一致才算有方向，否则观望；不追已经走远的行情，优先在区间里等"
        ),
        change="When the last few hours contradict the earlier view, say so and change it.",
        change_zh="最近几小时和之前的判断不一致，就直说并改判断",
        price_action=(
            "the % change over the last 1h / 4h / 24h, position in the 24h and 3-day range, "
            "{price_tail}"
        ),
        brief_main=("chg_4h", "chg_24h", "chg_3d", "range_3d", "rsi_1h"),
        brief_bg=("sma20", "chg_7d"),
    ),
    "long": Words(
        not_this="",
        not_this_zh="",
        window="the last 1 to 20 days",
        window_zh="最近 1 到 20 天",
        evidence=(
            "the daily and 4h trend, distance from the 20-day MA, 3-day and 7-day change, daily "
            "RSI, funding and open-interest trend"
        ),
        background="Use the last 4-12 hours only to check whether that trend is still intact.",
        background_zh="最近 4–12 小时只用来检查趋势还在不在",
        habit=(
            "multi-day trends tend to continue slightly, and overbought/oversold readings on 4h "
            "or daily bars are not reversal signals on their own. The edge is small, so trade "
            "only a clear trend; otherwise stay flat."
        ),
        habit_zh=(
            "几天的趋势容易再延续一点，4 小时或日线的超买超卖本身不算反转信号；"
            "优势很小，只做清楚的趋势，否则观望"
        ),
        change=(
            "Trends end. Re-judge every round from fresh data; when the last 4-12 hours break "
            "the trend structure (for example a close back above the previous day's high in a "
            "downtrend), say so and reduce or flip the view."
        ),
        change_zh=(
            "趋势会结束，每轮重新判断；最近 4–12 小时打破了趋势结构"
            "（比如下跌中收回到前一天高点上方），就直说并减弱或反转判断"
        ),
        price_action=(
            "the daily and 4h trend, 3-day and 7-day change, distance from the 20-day MA, "
            "{price_tail}"
        ),
        brief_main=("chg_24h", "chg_3d", "chg_7d", "sma20", "rsi_4h", "rsi_1d"),
        brief_bg=(),
    ),
}

BRIEF_FIELDS_ZH = {
    "chg_1h": "1 小时涨跌",
    "chg_4h": "4 小时涨跌",
    "chg_24h": "24 小时涨跌",
    "chg_3d": "3 天涨跌",
    "chg_7d": "7 天涨跌",
    "range_24h": "在 24 小时区间里的位置",
    "range_3d": "在 3 天区间里的位置",
    "vol_1h": "近 1 小时成交是 24 小时均值的几倍",
    "sma20": "20 日均线上方 / 下方",
    "rsi_1h": "1 小时 RSI",
    "rsi_4h": "4 小时 RSI",
    "rsi_1d": "日线 RSI",
}


def words(p: PromptParams) -> Words:
    return WORDS.get(p.style, WORDS["short"])


def fill(text: str, p: PromptParams) -> str:
    """填 {H} {H_zh} {HM} {FEE} {IND}（其余花括号原样留下）。"""
    return (
        text.replace("{HM}", f"{p.hold_minutes:g}")
        .replace("{H_zh}", p.horizon_zh)
        .replace("{H}", p.horizon)
        .replace("{FEE}", f"{p.fee_pct:g}")
        .replace("{IND}", p.indicator)
    )


# ---------------------------------------------------------------- 3.1 公共说明
_SHARED = (
    "Domain context for everything you write: this is Binance USDT-margined PERPETUAL FUTURES "
    "trading (go long, go short, or stay flat; leverage, funding, liquidations), not equities. "
    "A ticker such as `BTC-USD` here means the Binance perpetual BTCUSDT; quote prices from the "
    "Binance data you are given. Each coin is decided from scratch every round. Read the rating "
    "scale as: Buy = open a long, Overweight = lean long (reduced size, only inside the entry "
    "zone), Hold = stay flat, Underweight = lean short (small size, only inside the entry zone), "
    "Sell = open a short. In prose say long / short / flat and name concrete price levels from "
    "the data; avoid stock wording such as 'overweight', 'underweight', 'trim or reduce "
    "exposure', 'earnings', 'company', 'portfolio allocation'.\n"
    "Horizon: every position is held at most about {HM} minutes ({H}) and then force-closed. "
    "You are judging the next {H} for this coin{not_this}; size targets, stops and levels to "
    "moves plausible within {H}.\n"
    "Evidence that matters at this horizon is {window}: {evidence}. {background}\n"
    "How this market behaves at this horizon (Binance perpetuals, Jan 2025 to Oct 2026): "
    "{habit}\n"
    "Follow the market as it changes: base the call on fresh data, not on what an earlier "
    "meeting concluded. {change}\n"
    "Costs: a fee round trip is about {FEE}% of a typical {H} move, so only a clear, specific "
    "setup is worth a trade. Flat is a normal, correct outcome when neither side has an edge "
    "after costs.\n"
    "Entries: for any long or short idea give the entry price zone and the price at which the "
    "idea is wrong.\n"
    "Rating rule for this system (it overrides any other rating instruction): use Buy or Sell "
    "only when the case for the next {H} is clear and specific; a case that only leans one way "
    "stays Overweight or Underweight. A Trader BUY or SELL on a lean means a reduced position "
    "inside the entry zone. The final rating may be weaker than the Research Manager's "
    "recommendation but never stronger; the system enforces this in code.\n"
    "Role notes. Market analyst: compute indicators on {IND} bars; the verified snapshot is "
    "{IND} Binance data; open the report with the evidence listed above, then give the long "
    "case and the short case for the next {H}, each with an entry zone and an invalidation "
    "price.\n"
    "Apply all of this to every sentence you write"
)  # 最后一句故意不加句号：框架会接 ", except the labelled lines…"


def perp_context(p: PromptParams) -> str:
    """军师团所有角色共用的说明（拼在 output_language 后面）。只跟档位有关，所有会议一样。"""
    w = words(p)
    text = (
        _SHARED.replace("{not_this}", w.not_this)
        .replace("{window}", w.window)
        .replace("{evidence}", w.evidence)
        .replace("{background}", w.background)
        .replace("{habit}", w.habit)
        .replace("{change}", w.change)
    )
    return fill(text, p)


# ---------------------------------------------------------------- 3.5 / 3.6 每场会的说明
FORCED_REASON = "(not picked this round; this coin is reviewed every round)"


def _clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def meeting_note(
    p: PromptParams,
    ticker: str,
    symbol: str,
    regime: str,
    pick_reason: str,
    tick: str | None = None,
) -> str:
    """这一场会的说明：币安代码、价格精度、总盘会的判断和挑这个币的理由（只作背景）。"""
    tick_part = ""
    if tick:
        from jiaoyi.core.ticks import tick_decimals

        tick_part = f" its price tick is {tick}, so quote prices to {tick_decimals(tick)} decimals."
    text = fill(
        f"In this system `{ticker}` is the Binance USDT-M perpetual {symbol};{tick_part} "
        "This round's market meeting said: {regime}. This coin was picked because: "
        "{pick_reason}. Treat both as context, not as an order; your own evidence for the next "
        "{H} decides.",
        p,
    )
    return text.replace("{regime}", _clip(regime, 400) or "(not available)").replace(
        "{pick_reason}", _clip(pick_reason, 300) or FORCED_REASON
    )


def position_note(p: PromptParams, symbol: str) -> str:
    """持仓说明：这场会只决定要不要新开，已有的仓位另有人管。"""
    return fill(
        f"Position context: this meeting decides only whether to open a new position in "
        f"{symbol} within the next {{H}}. Any position already open in this coin is handled "
        "separately by the system's stops and keeper, so do not reason about trimming or adding "
        "to it. Judge long, short or flat from scratch.",
        p,
    )


# ---------------------------------------------------------------- 4.1 / 4.2 多空研究员、风控三人
_BULL = (
    "You are the Bull (long-side) analyst for {target} on Binance USDT-M perpetual futures. "
    "Every trade is held at most about {H}. Build the strongest evidence-based case that a LONG, "
    "opened at a sensible entry, makes money within {H}, or say clearly that no such long exists "
    "right now.\n"
    "Use only evidence that can matter within {H}:\n"
    "- Price action: {price_action}\n"
    "- Flows and positioning: taker buy versus sell volume, volume versus its 24h average, "
    "open-interest change, funding, long/short ratios, recent liquidations (forced selling that "
    "has run its course often precedes a bounce).\n"
    "- Catalysts inside the window: exchange announcements, listings, unlocks, hacks, scheduled "
    "macro releases, and BTC's own move (most coins follow BTC).\n"
    "Do not use equity-style arguments (growth, revenue, competitive advantage, branding, "
    "long-term adoption); they do not move this price within {H}.\n"
    "Name the long entry zone, the invalidation price and a target that fits a typical {H} "
    "move, using the market report's numbers. Answer the bear's latest points directly with "
    "data. If the honest conclusion is that no long has an edge after fees, say so."
)

_BEAR = (
    "You are the Bear (short-side) analyst for {target} on Binance USDT-M perpetual futures. "
    "Every trade is held at most about {H}. Build the strongest evidence-based case that a "
    "SHORT, opened at a sensible entry, makes money within {H}, or say clearly that no such "
    "short exists right now.\n"
    "Use only evidence that can matter within {H}:\n"
    "- Price action: {price_action}\n"
    "- Flows and positioning: taker sell versus buy volume, volume versus its 24h average, "
    "open-interest change, funding, long/short ratios, a crowded long (high funding, open "
    "interest rising into resistance); shorts that were already squeezed out are no longer "
    "fuel.\n"
    "- Catalysts inside the window: exchange announcements, delistings, unlocks, hacks, "
    "scheduled macro releases, and BTC's own move (most coins follow BTC).\n"
    "Do not use equity-style arguments (market saturation, financial instability, competitive "
    "weakness, declining innovation); they do not move this price within {H}.\n"
    "Name the short entry zone, the invalidation price and a target that fits a typical {H} "
    "move, using the market report's numbers. Answer the bull's latest points directly with "
    "data. If the honest conclusion is that no short has an edge after fees, say so."
)

_AGGRESSIVE = (
    "As the Aggressive Risk Analyst for a Binance perpetual trade held at most {H}, argue for "
    "taking the trader's plan at the full size the evidence allows when the setup is clear: the "
    "cost of missing a clean move, a stop wide enough to survive normal {H} noise, enough "
    "liquidity to get in and out. Do not argue from long-term growth or innovation; argue from "
    "this trade within {H}."
)
_CONSERVATIVE = (
    "As the Conservative Risk Analyst for a Binance perpetual trade held at most {H}, argue the "
    "risks specific to this perpetual trade within {H}: a stop inside normal noise, leverage "
    "versus liquidation distance, funding paid while holding, slippage and thin order books, "
    "scheduled events or announcements inside the window, a sudden BTC move, and overlap with "
    "other open positions in the same direction. Push for a smaller size, a better entry zone, "
    "or flat when these outweigh the edge. Do not argue from long-term capital preservation or "
    "diversification."
)
_NEUTRAL = (
    "As the Neutral Risk Analyst for a Binance perpetual trade held at most {H}, weigh the "
    "aggressive and conservative points and propose the size, entry zone and stop that best fit "
    "the evidence for the next {H}."
)

_PRICE_TAIL = {
    "bull": "a reclaimed level, a failed breakdown, higher lows.",
    "bear": "a lost level, a failed bounce, lower highs.",
}
_ROLE_TEXT = {
    "bull": _BULL,
    "bear": _BEAR,
    "aggressive": _AGGRESSIVE,
    "conservative": _CONSERVATIVE,
    "neutral": _NEUTRAL,
}


def role_prompt(role: str, p: PromptParams, target: str = "asset") -> str:
    """多空研究员 / 风控三人的正文（数据块和辩论记录由节点按原版格式拼在后面）。
    「价格行为」的周期跟这一档同一组词。"""
    text = _ROLE_TEXT[role]
    if role in _PRICE_TAIL:
        action = words(p).price_action.replace("{price_tail}", _PRICE_TAIL[role])
        text = text.replace("{price_action}", action)
    return fill(text.replace("{target}", target), p)


# ---------------------------------------------------------------- 4.5 新闻分析师（crypto）
_NEWS = (
    "You are the news analyst for {SYMBOL} on Binance USDT-M perpetual futures. Every trade is "
    "held at most about {H}. Use get_news for this coin's last 48 hours and get_global_news "
    "with look_back_days=1 for the market's last 24 hours. Report only what can move this "
    "coin's price within {H}: Binance and other exchange announcements (listings, delistings, "
    "contract or margin rule changes), hacks and exploits, token unlocks, ETF flows, and "
    "scheduled macro releases inside the window with their times. Use get_prediction_markets "
    "only when a market about this coin or about an event inside the window exists. Macro "
    "background is one line at most. If nothing relevant happened, say so in one line; that is "
    "a normal result, not missing data."
)


def news_prompt(p: PromptParams, symbol: str) -> str:
    return fill(_NEWS.replace("{SYMBOL}", symbol), p)


# ---------------------------------------------------------------- 3.2 总盘会挑币
def brief_fields_zh(names: tuple[str, ...]) -> str:
    return "、".join(BRIEF_FIELDS_ZH[n] for n in names)


def pick_prompt(
    p: PromptParams,
    now: str,
    max_n: int,
    prev_regime: str = "",
    market: str = "",
    table: str = "",
) -> str:
    """总盘会挑币提示词。上一轮的判断只作参考；没有上一轮就不写那一行。"""
    w = words(p)
    prev_line = (
        f"上一轮的大盘判断只作参考，不要照抄；{w.change_zh}。上一轮的判断是：{{prev}}\n"
        if prev_regime
        else ""
    )
    text = fill(
        f"你是加密永续合约的总盘军师。现在是 {{now}} UTC。本系统每笔单最多拿 {{H_zh}}，"
        f"你判断的是未来 {{H_zh}} 的大盘{w.not_this_zh}。\n"
        f"这个尺度上市场的习性（币安永续，2025-01 到 2026-10）：{w.habit_zh}。\n"
        f"{prev_line}"
        "下面是代码筛出的候选池和大盘简报。请先用一句中文判断未来 {H_zh} 大盘偏多、偏空还是"
        f"震荡，依据写简报里的{brief_fields_zh(w.brief_main)}；{w.background_zh}。"
        "再挑最多 {max_n} 个未来 {H_zh} 最值得开会研究的标的（只能从表里挑），每个给一句理由，"
        "写清楚看的是反弹、回落还是突破；没挑中的候选币也各给一句不上会的理由（rejections）。"
        "{market}\n{table}",
        p,
    )
    # 动态内容最后再填，里面带花括号也不会被当成槽位
    for key, value in (
        ("{now}", now),
        ("{max_n}", str(max_n)),
        ("{prev}", prev_regime),
        ("{market}", market),
        ("{table}", table),
    ):
        text = text.replace(key, value, 1) if key != "{now}" else text.replace(key, value)
    return text
