"""``make kronos-fetch`` 用：按 ``MODEL_SPECS`` 钉的提交号预先拉 Kronos 权重到 HuggingFace 缓存。

默认拉最大（base）和加载失败时退回的标准（small），以及它们的分词器；第一次推理就不用现下载。
用法：``python -m jiaoyi.predictor.fetch_weights [mini small base ...]``
"""

from __future__ import annotations

import importlib
import sys

from jiaoyi.predictor.kronos import FALLBACK_SIZE, MODEL_SPECS


def main(argv: list[str]) -> int:
    sizes = argv or ["base", FALLBACK_SIZE]
    bad = [s for s in sizes if s not in MODEL_SPECS]
    if bad:
        print(f"不认识的模型尺寸：{' '.join(bad)}（可选 {' / '.join(MODEL_SPECS)}）")
        return 2
    hub = importlib.import_module("huggingface_hub")
    repos: dict[tuple[str, str], str] = {}
    for s in sizes:
        sp = MODEL_SPECS[s]
        repos[(sp.model_id, sp.model_revision)] = sp.name
        repos[(sp.tokenizer_id, sp.tokenizer_revision)] = f"{sp.name} 的分词器"
    for (repo, rev), what in repos.items():
        hub.snapshot_download(repo_id=repo, revision=rev)
        print(f"已拉取 {what}：{repo} @ {rev[:7]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
