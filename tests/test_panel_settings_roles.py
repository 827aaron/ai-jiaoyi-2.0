"""设置页按角色分块：接口给的每个设置项都点名分到了某个角色，不掉进「其他」。

前端没有 JS 测试，这里直接读 app.js 里的 ROLES 表（「段.键」字符串和「段.前缀*」通配）。
新加的配置键没分进角色时这个测试会失败，提醒把它放进对应角色那一块。
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.conftest import ROOT
from tests.test_panel_actions import _client, _setup

APP_JS = ROOT / "src" / "jiaoyi" / "panel" / "static" / "app.js"
SECTIONS = ["risk", "app", "fast_loop", "predictor", "schedule", "budget", "screener", "tiers"]


def _roles_block() -> str:
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("const ROLES = [")
    return js[start : js.index("\n];", start)]


def _shown_elsewhere() -> set[str]:
    js = APP_JS.read_text(encoding="utf-8")
    line = next(x for x in js.splitlines() if x.startswith("const SHOWN_ELSEWHERE"))
    return set(re.findall(r'"([a-z_]+\.[a-z_0-9]+)"', line))


def test_every_setting_belongs_to_a_role(tmp_path: Path) -> None:
    block = _roles_block()
    named = set(re.findall(r'"([a-z_]+\.[a-z_0-9]+)"', block))
    globs = re.findall(r'"([a-z_]+\.[a-z_0-9]*)\*"', block)
    core = re.search(r"const CORE = \[(.*?)\];", APP_JS.read_text(encoding="utf-8"))
    assert core and "keys: CORE" in block
    named |= set(re.findall(r'"([a-z_]+\.[a-z_0-9]+)"', core.group(1)))

    paths, _ = _setup(tmp_path)
    with _client(paths) as client:
        d = client.get("/api/settings").json()
    keys = {f"{sec}.{i['key']}" for sec in SECTIONS for i in d.get(sec) or []}
    assert len(keys) > 100

    skip = _shown_elsewhere()
    loose = sorted(
        k for k in keys - skip if k not in named and not any(k.startswith(g) for g in globs)
    )
    assert loose == [], f"这些设置没分到角色（会掉进「其他」）：{loose}"
    unknown = sorted(named - keys - skip)
    assert unknown == [], f"ROLES 里点名了接口没有的键：{unknown}"


def test_role_ids_match_team_page() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    ids = re.findall(r'\{ id: "([a-z_]+)", t: "', _roles_block())
    team = set(re.findall(r"\b([a-z_]+): \[\d+, \d+\]", js[js.index("const TF_POS") :]))
    # 团队页的角色点「它的设置」跳 #settings/<同一个 id>
    assert {"screener", "market", "commander", "predictor", "sentinel", "gate"} <= set(ids)
    assert {"risk_guard", "executor", "keeper", "patrol", "reconciler", "lifeboat"} <= set(ids)
    assert set(ids) - {"ai_cost", "keys", "show"} <= team
