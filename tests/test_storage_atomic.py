"""WP4 ③: 长期存储原子写 + memory/knowledge 备份."""
import json
import os
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src import atomic_io  # noqa: E402
from src.knowledge import Knowledge  # noqa: E402
from src.memory import Memory  # noqa: E402


class _StubClient:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        return self.response


class _StubAnalyzer:
    enabled = True
    endpoint = "http://127.0.0.1:5010"
    timeout = 1

    def __init__(self, response):
        self.client = _StubClient(response)


class _StubState:
    def __init__(self):
        self.values = {}

    def get(self, path, default=None):
        return self.values.get(path, default)

    def set(self, path, value):
        self.values[path] = value


# ---- 原子写 ----

def test_atomic_write_replaces_and_leaves_no_tmp(tmp_path):
    target = tmp_path / "x.txt"

    atomic_io.atomic_write_text(str(target), "v1")
    atomic_io.atomic_write_text(str(target), "v2")

    assert target.read_text(encoding="utf-8") == "v2"
    assert not (tmp_path / "x.txt.tmp").exists()


def test_atomic_write_failure_keeps_original_and_cleans_tmp(tmp_path, monkeypatch):
    target = tmp_path / "metrics.json"
    target.write_text('{"old": 1}', encoding="utf-8")

    def _boom(src, dst):
        raise OSError("simulated interruption")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        atomic_io.atomic_write_text(str(target), '{"new": 2}')

    assert target.read_text(encoding="utf-8") == '{"old": 1}'
    assert not (tmp_path / "metrics.json.tmp").exists()


def test_atomic_copy_failure_keeps_target(tmp_path, monkeypatch):
    src = tmp_path / "src.txt"
    dst = tmp_path / "dst.bak"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")

    def _boom(src_path, dst_path):
        raise OSError("simulated interruption")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        atomic_io.atomic_copy(str(src), str(dst))

    assert dst.read_text(encoding="utf-8") == "old"
    assert not (tmp_path / "dst.bak.tmp").exists()


# ---- Memory 原子写 ----

def test_memory_json_write_is_atomic(tmp_path):
    memory = Memory(str(tmp_path))

    memory.save_metrics({"m": [{"value": 1}]})
    memory.save_alpha_history([{"alpha": 0.1}])

    assert json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8"))["m"][0]["value"] == 1
    assert json.loads((tmp_path / "alpha_history.json").read_text(encoding="utf-8"))[0]["alpha"] == 0.1
    assert not (tmp_path / "metrics.json.tmp").exists()
    assert not (tmp_path / "alpha_history.json.tmp").exists()


def test_memory_write_failure_keeps_previous_file(tmp_path, monkeypatch):
    memory = Memory(str(tmp_path))
    memory.save_metrics({"old": 1})
    original = (tmp_path / "metrics.json").read_text(encoding="utf-8")

    monkeypatch.setattr(os, "replace",
                        lambda src, dst: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        memory.save_metrics({"new": 2})

    assert (tmp_path / "metrics.json").read_text(encoding="utf-8") == original
    assert not (tmp_path / "metrics.json.tmp").exists()


def test_memory_md_write_atomic(tmp_path):
    memory = Memory(str(tmp_path))

    memory.save_memory_md("hello")

    assert memory.load_memory_md() == "hello"
    assert not (tmp_path / "memory.md.tmp").exists()


# ---- memory.md 压缩备份 ----

def _knowledge(tmp_path, analyzer, threshold=50):
    return Knowledge({"distill": {"memory_compress_threshold_chars": threshold}},
                     str(tmp_path), analyzer)


def test_compress_memory_backs_up_original(tmp_path):
    memory = Memory(str(tmp_path))
    original = "A" * 200
    memory.save_memory_md(original)
    analyzer = _StubAnalyzer({"ok": True, "content": "compressed summary"})

    assert _knowledge(tmp_path, analyzer).compress_memory(memory) is True

    assert (tmp_path / "memory.md.bak").read_text(encoding="utf-8") == original
    content = memory.load_memory_md()
    assert content.startswith("# Glassnode Alpha Engine Memory")
    assert "compressed summary" in content
    assert not (tmp_path / "memory.md.tmp").exists()
    assert not (tmp_path / "memory.md.bak.tmp").exists()


def test_compress_memory_below_threshold_no_backup(tmp_path):
    memory = Memory(str(tmp_path))
    memory.save_memory_md("short")
    analyzer = _StubAnalyzer({"ok": True, "content": "compressed"})

    assert _knowledge(tmp_path, analyzer).compress_memory(memory) is False
    assert not (tmp_path / "memory.md.bak").exists()


def test_compress_memory_failure_keeps_original(tmp_path):
    memory = Memory(str(tmp_path))
    original = "B" * 200
    memory.save_memory_md(original)
    analyzer = _StubAnalyzer({"ok": False, "error": "api down"})

    assert _knowledge(tmp_path, analyzer).compress_memory(memory) is False

    assert memory.load_memory_md() == original
    assert not (tmp_path / "memory.md.bak").exists()


def test_compress_memory_backup_overwrites_previous(tmp_path):
    memory = Memory(str(tmp_path))
    (tmp_path / "memory.md.bak").write_text("stale backup", encoding="utf-8")
    memory.save_memory_md("C" * 200)
    analyzer = _StubAnalyzer({"ok": True, "content": "compressed"})

    assert _knowledge(tmp_path, analyzer).compress_memory(memory) is True

    assert (tmp_path / "memory.md.bak").read_text(encoding="utf-8") == "C" * 200


# ---- knowledge_base.md 蒸馏备份 ----

def test_distill_backs_up_knowledge_base(tmp_path):
    memory = Memory(str(tmp_path))
    memory.save_memory_md("narrative")
    memory.save_alpha_history([{"date": "2026-01-01T00:00:00Z",
                                "alpha": 0.1, "regime": "BULL"}])
    kb_file = tmp_path / "knowledge_base.md"
    kb_file.write_text("OLD KNOWLEDGE", encoding="utf-8")
    analyzer = _StubAnalyzer({"ok": True, "content": "NEW KNOWLEDGE"})
    knowledge = Knowledge({}, str(tmp_path), analyzer)

    result = knowledge.distill(memory, _StubState())

    assert result == "NEW KNOWLEDGE"
    assert kb_file.read_text(encoding="utf-8") == "NEW KNOWLEDGE"
    assert (tmp_path / "knowledge_base.md.bak").read_text(encoding="utf-8") == "OLD KNOWLEDGE"
    assert not (tmp_path / "knowledge_base.md.tmp").exists()


def test_distill_failure_keeps_knowledge_base(tmp_path):
    memory = Memory(str(tmp_path))
    memory.save_memory_md("narrative")
    kb_file = tmp_path / "knowledge_base.md"
    kb_file.write_text("OLD KNOWLEDGE", encoding="utf-8")
    analyzer = _StubAnalyzer({"ok": False, "error": "api down"})
    knowledge = Knowledge({}, str(tmp_path), analyzer)

    assert knowledge.distill(memory, _StubState()) is None

    assert kb_file.read_text(encoding="utf-8") == "OLD KNOWLEDGE"
    assert not (tmp_path / "knowledge_base.md.bak").exists()


def test_save_knowledge_base_without_existing_no_backup(tmp_path):
    analyzer = _StubAnalyzer({"ok": True, "content": "x"})
    knowledge = Knowledge({}, str(tmp_path), analyzer)

    knowledge.save_knowledge_base("FIRST")

    assert (tmp_path / "knowledge_base.md").read_text(encoding="utf-8") == "FIRST"
    assert not (tmp_path / "knowledge_base.md.bak").exists()
