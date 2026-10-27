"""构建时附加只读观测安装，不复制或重写上游交易判断方法体。"""

import ast
import importlib.util
import pathlib
import sys

MODULE = "hummingbot.strategy_v2.executors.position_executor.position_executor"
MARK = "# jiaoyi: bounded read-only stop diagnostics v1"
SUFFIX = (
    f"\n\n{MARK}\n"
    "try:\n"
    "    from .jiaoyi_stop_diagnostics import install as _jiaoyi_install_stop_diagnostics\n"
    "    _jiaoyi_install_stop_diagnostics(PositionExecutor)\n"
    "except Exception:\n"
    "    pass  # 诊断不可用不能阻止原执行器加载；API缺能力即unknown。\n"
)


def patch(text: str) -> str:
    if MARK in text:
        return text
    tree = ast.parse(text)
    cls = next(
        (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "PositionExecutor"), None
    )
    required = {
        "control_task",
        "control_stop_loss",
        "control_trailing_stop",
        "place_close_order_and_cancel_open_orders",
        "get_custom_info",
    }
    methods = (
        {n.name for n in cls.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
        if cls
        else set()
    )
    if not required <= methods:
        raise ValueError("上游止损接口已变化，观测补丁需重审")
    result = text + SUFFIX
    compile(result, MODULE, "exec")
    return result


def main() -> None:
    spec = importlib.util.find_spec(MODULE)
    if spec is None or spec.origin is None:
        sys.exit("找不到目标执行器模块")
    path = pathlib.Path(spec.origin)
    original = path.read_text()
    modified = patch(original)
    runtime = pathlib.Path(__file__).with_name("stop_diagnostics.py").read_text()
    compile(runtime, "jiaoyi_stop_diagnostics.py", "exec")
    path.with_name("jiaoyi_stop_diagnostics.py").write_text(runtime)
    if modified != original:
        path.write_text(modified)
    print("patched stop diagnostics (read-only observer)")


if __name__ == "__main__":
    main()
