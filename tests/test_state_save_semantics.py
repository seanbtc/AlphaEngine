"""state 落盘语义: update_runtime 置脏 / 脏门控 / force 路径.

回归背景: update_runtime() 直接改内存后仅调 save(), 不置 _dirty; 复盘轮
set()+save() 已清脏时, update_runtime 及其后的裸 save() 均被静默跳过,
内存 analysis_count 已 +1 而磁盘停在复盘快照 (09-24 事故)。
"""
import json
import os
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.memory import Memory  # noqa: E402
from src.state_manager import StateManager  # noqa: E402


def _disk_runtime(data_dir: Path) -> dict:
    raw = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
    return raw["runtime"]


def _load_fresh(data_dir: Path) -> StateManager:
    sm = StateManager(str(data_dir), "state.json")
    sm.load()
    return sm


# ---- 事故复现: 复盘 set+save 清脏后 update_runtime 仍须落盘 ----

def test_update_runtime_persists_after_dirty_cleared(tmp_path):
    sm = _load_fresh(tmp_path)
    sm.set("runtime.post_count", 41)
    sm.set("runtime.last_deepseek_at", "2026-09-24T03:00:00Z")
    sm.set("runtime.last_analysis_at", "2026-09-22T03:00:00Z")
    sm.save()
    assert _disk_runtime(tmp_path)["analysis_count"] == 0

    sm.update_runtime()
    sm.save()

    disk = _disk_runtime(tmp_path)
    assert disk["analysis_count"] == 1
    assert disk["last_analysis_at"] != "2026-09-22T03:00:00Z"
    assert disk["last_analysis_at"].endswith("Z")
    # 从磁盘重新加载 (而非读内存) 验证真正落盘
    reloaded = _load_fresh(tmp_path)
    assert reloaded.get("runtime.analysis_count") == 1
    assert reloaded.get("runtime.last_analysis_at") == disk["last_analysis_at"]
    assert reloaded.get("runtime.post_count") == 41


def test_two_consecutive_update_runtime_both_persist(tmp_path):
    sm = _load_fresh(tmp_path)

    sm.update_runtime()
    sm.save()
    assert _disk_runtime(tmp_path)["analysis_count"] == 1

    # 第二轮: 上一轮 save 已清脏, update_runtime 必须重新置脏
    sm.update_runtime()
    sm.save()
    assert _disk_runtime(tmp_path)["analysis_count"] == 2

    assert _load_fresh(tmp_path).get("runtime.analysis_count") == 2


def test_update_runtime_alone_writes_disk(tmp_path):
    """无后续 save() 时, update_runtime 内部的 save() 也须真正写盘。"""
    sm = _load_fresh(tmp_path)

    sm.update_runtime()

    assert _disk_runtime(tmp_path)["analysis_count"] == 1


# ---- 脏门控回归: 无变化不写盘, set 后写盘 ----

def test_save_skips_write_when_clean_mtime_unchanged(tmp_path):
    sm = _load_fresh(tmp_path)
    sm.set("runtime.post_count", 7)
    sm.save()
    before = (tmp_path / "state.json").stat().st_mtime_ns

    sm.save()  # 干净状态: 不产生任何写盘

    assert (tmp_path / "state.json").stat().st_mtime_ns == before


def test_set_marks_dirty_and_save_writes(tmp_path):
    sm = _load_fresh(tmp_path)
    sm.save()  # 清脏
    state_file = tmp_path / "state.json"
    # Windows 文件时间戳分辨率低: 两次写可能落在同一 tick, 直接比较 mtime
    # 会偶发相等; 先把 mtime 拨回过去, 使"脏 save 确实写盘"可稳定判定
    old = state_file.stat().st_mtime_ns - 1_000_000_000
    os.utime(state_file, ns=(old, old))
    before = state_file.stat().st_mtime_ns
    assert before == old

    sm.set("runtime.post_count", 9)
    sm.save()

    assert state_file.stat().st_mtime_ns != before
    assert _disk_runtime(tmp_path)["post_count"] == 9


# ---- force 路径: regenerate_from_memory / 首次默认状态 ----

def test_regenerate_from_memory_forces_save(tmp_path):
    memory = Memory(str(tmp_path))
    memory.save_alpha_history([
        {"date": "2026-09-10T00:00:00Z", "regime": "BEAR_DEEP",
         "alpha": -0.25, "target_alpha": -0.3},
        {"date": "2026-09-20T00:00:00Z", "regime": "BEAR_DEEP",
         "alpha": -0.2, "target_alpha": -0.25},
    ])
    sm = StateManager(str(tmp_path), "state.json")

    assert sm.regenerate_from_memory(memory) is True

    disk = _disk_runtime(tmp_path)
    assert disk["analysis_count"] == 2
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["regime"]["current"] == "BEAR_DEEP"
    # force 落盘后恢复干净: 裸 save() 不再写盘
    before = (tmp_path / "state.json").stat().st_mtime_ns
    sm.save()
    assert (tmp_path / "state.json").stat().st_mtime_ns == before


def test_default_state_written_on_first_load(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")

    sm.load()

    assert (tmp_path / "state.json").exists()
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    assert persisted["runtime"]["analysis_count"] == 0


def test_duplicate_alpha_count_scenario_no_skips(tmp_path):
    """09-24 时序剧本: 复盘清脏 → 无校准 → update_runtime; 下一轮再 +1, 不跳号。"""
    sm = _load_fresh(tmp_path)
    sm.set("runtime.post_count", 40)
    sm.set("runtime.analysis_count", 41)
    sm.save()

    sm.update_runtime()      # 09-24 轮, 紧随复盘 save 清脏
    sm.save()
    assert _disk_runtime(tmp_path)["analysis_count"] == 42

    sm.update_runtime()      # 09-26 轮
    sm.save()
    disk = _disk_runtime(tmp_path)
    assert disk["analysis_count"] == 43
    assert _load_fresh(tmp_path).get("runtime.analysis_count") == 43
