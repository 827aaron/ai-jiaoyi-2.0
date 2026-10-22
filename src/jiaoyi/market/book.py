"""盘口员公式：中间价、微价格、价差、深度、失衡，加上 OFI（订单流失衡）。

中间价 / 微价格 / 价差 / 深度 / 失衡的写法来自 buberlo/jev-trader
（https://github.com/buberlo/jev-trader ，提交 15d089e，src/jev_trader/state/book.py，
MIT License，Copyright (c) 2026 buberlo），改成直接读 ``BookTop`` 契约。
OFI 按 Cont、Kukanov、Stoikov (2014) 的一档公式自己写。

第一版不训练，只作拍板人的辅助输入，不能单独触发开仓（设计文档 §八）。
"""

from __future__ import annotations

from jiaoyi.contracts import BookTop


def mid(book: BookTop) -> float:
    return (book.bids[0][0] + book.asks[0][0]) / 2.0


def spread_bps(book: BookTop) -> float:
    m = mid(book)
    if m <= 0:
        return 0.0
    return max(0.0, 10_000.0 * (book.asks[0][0] - book.bids[0][0]) / m)


def microprice(book: BookTop) -> float:
    (bp, bq), (ap, aq) = book.bids[0], book.asks[0]
    total = bq + aq
    if total <= 0:
        return mid(book)
    return (bp * aq + ap * bq) / total


def depth_usdt(book: BookTop, levels: int = 5) -> tuple[float, float]:
    """买、卖各前 ``levels`` 档的名义价值（USDT）。"""
    bid = sum(p * q for p, q in book.bids[:levels])
    ask = sum(p * q for p, q in book.asks[:levels])
    return bid, ask


def imbalance(book: BookTop, levels: int = 5) -> float:
    """(买量-卖量)/(买量+卖量)，按名义价值，范围 [-1, 1]。"""
    bid, ask = depth_usdt(book, levels)
    total = bid + ask
    if total <= 0:
        return 0.0
    return (bid - ask) / total


def ofi_event(prev: BookTop, cur: BookTop) -> float:
    """两次一档盘口之间的订单流失衡（合约数量），正数偏买。

    买一价上移或持平时新挂量算买压，买一价下移或持平时旧量撤走算卖压；卖一对称。
    """
    (pb, pbq), (pa, paq) = prev.bids[0], prev.asks[0]
    (cb, cbq), (ca, caq) = cur.bids[0], cur.asks[0]
    e = 0.0
    if cb >= pb:
        e += cbq
    if cb <= pb:
        e -= pbq
    if ca <= pa:
        e -= caq
    if ca >= pa:
        e += paq
    return e
