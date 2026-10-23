"""loop.py / app.py 里 runtime_checkable 的“可选能力”Protocol × 真实实现类，逐个核对签名。

``isinstance(x, SomeProtocol)`` 只查成员名，不查参数、不查是不是 async。签名对不上要到运行时才炸
（#57：heartbeat 少传 now，启动即崩），或者更糟：同步调用了 async 方法，协程没人 await，
什么都没发生也不报错。这里把 jiaoyi 包里所有实现了对应接口的类都找出来，按调用方的方式 bind。
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Any, get_type_hints

import pytest

import jiaoyi
from jiaoyi import app as app_mod
from jiaoyi import interfaces
from jiaoyi import loop as loop_mod
from jiaoyi.risk.guard import LotRulesProvider


def _jiaoyi_classes() -> list[type]:
    out: dict[str, type] = {}
    for info in pkgutil.walk_packages(jiaoyi.__path__, "jiaoyi."):
        try:
            module = importlib.import_module(info.name)
        except ImportError:  # 可选依赖没装（例如 tradingagents），这些类在 CI 里本来也用不到
            continue
        for obj in vars(module).values():
            if (
                inspect.isclass(obj)
                and obj.__module__ == module.__name__
                and not getattr(obj, "_is_protocol", False)
            ):
                out[f"{obj.__module__}.{obj.__qualname__}"] = obj
    return list(out.values())


CLASSES = _jiaoyi_classes()


def _members(proto: type) -> set[str]:
    """Protocol 自己声明的成员（Python 3.11 没有 ``__protocol_attrs__``，自己从类体里取）。"""
    names = set(getattr(proto, "__annotations__", {}))
    names |= {n for n in vars(proto) if not n.startswith("_")}
    return names


def _params(fn: Any) -> list[str]:
    return list(inspect.signature(fn).parameters)[1:]


def _implements(cls: type, iface: type) -> bool:
    """类上有接口的全部方法，且参数名一致（光同名不算，例如 CommandInbox.check 不是巡逻兵）。"""
    for m in _members(iface) - _instance_attrs(iface):
        want, got = inspect.getattr_static(iface, m, None), inspect.getattr_static(cls, m, None)
        if isinstance(want, property):
            if not isinstance(got, property):
                return False
            continue
        if not inspect.isfunction(got):
            return False
        if inspect.isfunction(want) and _params(got)[: len(_params(want))] != _params(want):
            return False
    return True


def _sets_attr(cls: type, name: str) -> bool:
    try:
        return f"self.{name}" in inspect.getsource(cls)
    except (OSError, TypeError):
        return False


def _instance_attrs(proto: type) -> set[str]:
    """Protocol 里只是注解的成员（实例属性，类上查不到）。"""
    return set(getattr(proto, "__annotations__", {}))


def impls(*ifaces: type) -> list[type]:
    return sorted(
        {c for c in CLASSES for i in ifaces if _implements(c, i)}, key=lambda c: c.__qualname__
    )


# 可选能力 → 调用方会对哪几个槽位做 isinstance（见 loop.py / app.py）
SLOTS: dict[type, tuple[type, ...]] = {
    loop_mod._Forgets: (interfaces.Keeper,),
    loop_mod._HasUrgent: (interfaces.Patrol,),
    loop_mod._HoldsAware: (interfaces.BattleCardProvider,),
    loop_mod._NewsAware: (interfaces.BattleCardProvider,),
    loop_mod._RefreshGate: (interfaces.BattleCardProvider,),
    loop_mod._RefreshReason: (interfaces.BattleCardProvider,),
    app_mod._Blocks: (interfaces.BattleCardProvider,),
    app_mod._SupportsWarmup: (interfaces.DecisionModel,),
    app_mod._SupportsClose: (
        interfaces.Predictor,
        interfaces.Keeper,
        interfaces.Patrol,
        interfaces.EntryGate,
        interfaces.DecisionModel,
        interfaces.BattleCardProvider,
    ),
    app_mod._Refreshable: (LotRulesProvider,),
    # heartbeat_all 遍历 Modules 的每个字段
    app_mod._SupportsHeartbeat: tuple(get_type_hints(app_mod.Modules).values()),
}


def _cases() -> list[Any]:
    cases = []
    for proto, ifaces in SLOTS.items():
        for cls in impls(*ifaces):
            for name in _members(proto):
                if hasattr(cls, name) or (name in _instance_attrs(proto) and _sets_attr(cls, name)):
                    cases.append(
                        pytest.param(
                            proto, cls, name, id=f"{proto.__name__}-{cls.__qualname__}-{name}"
                        )
                    )
    return cases


def test_every_capability_has_a_real_implementation() -> None:
    covered = {p for p, *_ in (c.values for c in _cases())}
    assert covered == set(SLOTS), f"没有真实实现的能力：{set(SLOTS) - covered}"


@pytest.mark.parametrize(("proto", "cls", "name"), _cases())
def test_signature_matches(proto: type, cls: type, name: str) -> None:
    if name in _instance_attrs(proto):
        # 实例属性：类上要么没有（__init__ 里设），要么不能是方法
        assert not inspect.isfunction(inspect.getattr_static(cls, name, None))
        return
    want = inspect.getattr_static(proto, name)
    got = inspect.getattr_static(cls, name)
    if isinstance(want, property):
        assert isinstance(got, property), f"{cls.__qualname__}.{name} 应该是属性，调用方不加括号"
        return
    assert inspect.isfunction(got), f"{cls.__qualname__}.{name} 应该是方法"
    assert inspect.iscoroutinefunction(got) == inspect.iscoroutinefunction(want), (
        f"{cls.__qualname__}.{name} 同步 / async 和调用方不一致"
    )
    # 按调用方的方式传参：位置参数传位置，仅关键字的传关键字
    args: list[object] = [object()]
    kwargs: dict[str, object] = {}
    for p in list(inspect.signature(want).parameters.values())[1:]:
        if p.kind is p.KEYWORD_ONLY:
            kwargs[p.name] = object()
        else:
            args.append(object())
    inspect.signature(got).bind(*args, **kwargs)
