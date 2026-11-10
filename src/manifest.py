"""Run Manifest — 每轮"为什么 α 动了"的唯一真相 (Phase 1)。

append-only JSONL (data/run_manifest.jsonl), 记录输入/输出/门控/命题状态。
失败不影响主流程 (调用方 try/except)。
"""
import json
import os
from datetime import datetime, timezone

MANIFEST_FILE = "run_manifest.jsonl"


def _iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def build_manifest(*, mode: str, cycle_id: str = None, **fields) -> dict:
    entry = {
        "cycle_id": cycle_id or _iso(),
        "ts": _iso(),
        "mode": mode,
    }
    for key, value in fields.items():
        if value is not None:
            entry[key] = value
    return entry


def append_manifest(data_dir: str, entry: dict) -> None:
    if not isinstance(entry, dict):
        return
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, MANIFEST_FILE), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
