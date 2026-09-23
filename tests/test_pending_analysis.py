"""失败推文待分析队列 (PendingAnalysis) — 单元测试 (离线, tmp_path)。"""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.pending_analysis import PendingAnalysis  # noqa: E402


def test_add_dedup_and_fields(tmp_path):
    p = PendingAnalysis(str(tmp_path))
    now = datetime(2026, 9, 23, 0, 0, 0)

    stats = p.add(["a", "b", "a"], reason="analysis_failed", now=now)

    assert stats["added"] == 2
    assert stats["total"] == 2
    items = p.load()
    assert [i["id"] for i in items] == ["a", "b"]
    assert items[0]["added_at"] == "2026-09-23T00:00:00Z"
    assert items[0]["reason"] == "analysis_failed"

    stats = p.add(["a"], reason="analysis_failed", now=now + timedelta(hours=1))
    assert stats["added"] == 0
    assert stats["total"] == 2
    assert p.load()[0]["added_at"] == "2026-09-23T00:00:00Z"  # 保留首入队时间


def test_expired_dropped_and_reported(tmp_path):
    p = PendingAnalysis(str(tmp_path), max_age_days=7)
    old = datetime.utcnow() - timedelta(days=8)
    p.add(["old"], now=old)

    stats = p.add(["new"], now=datetime.utcnow())

    assert stats["expired"] == 1
    assert stats["expired_ids"] == ["old"]
    assert stats["total"] == 1
    assert [i["id"] for i in p.load()] == ["new"]


def test_cap_keeps_newest_and_reports_overflow(tmp_path):
    p = PendingAnalysis(str(tmp_path), max_items=3)
    base = datetime(2026, 9, 1, 0, 0, 0)
    stats = {}
    for i in range(5):
        stats = p.add([f"id{i}"], now=base + timedelta(minutes=i))

    assert stats["overflow"] == 1
    assert stats["overflow_ids"] == ["id1"]
    assert stats["total"] == 3
    assert [i["id"] for i in p.load()] == ["id2", "id3", "id4"]


def test_remove_persists_and_counts(tmp_path):
    p = PendingAnalysis(str(tmp_path))
    p.add(["a", "b", "c"])

    assert p.remove(["b", "missing"]) == 1
    assert [i["id"] for i in p.load()] == ["a", "c"]
    assert p.remove([]) == 0
    assert p.remove(["none"]) == 0


def test_corrupt_lines_and_duplicates_skipped(tmp_path):
    path = tmp_path / "pending_analysis.jsonl"
    path.write_text(
        '{"id":"1","added_at":"2026-09-01T00:00:00Z","reason":"x"}\n'
        "not json\n"
        "[]\n"
        '{"id":"1","added_at":"2026-09-02T00:00:00Z","reason":"y"}\n'
        '{"id":"2","added_at":"2026-09-01T00:00:00Z","reason":"x"}\n',
        encoding="utf-8")

    items = PendingAnalysis(str(tmp_path)).load()

    assert [i["id"] for i in items] == ["1", "2"]
    assert items[0]["added_at"] == "2026-09-01T00:00:00Z"


def test_load_skips_non_utf8_and_reports_once(tmp_path):
    path = tmp_path / "pending_analysis.jsonl"
    path.write_bytes(b'{"id":"1","added_at":"2026-09-01T00:00:00Z","reason":"x"}\n'
                     b"\xff\xfe\x00bad\n")
    p = PendingAnalysis(str(tmp_path))
    calls = []

    items = p.load(on_error=calls.append)

    assert [i["id"] for i in items] == ["1"]
    assert calls and "无法解码" in calls[0]
    p.load(on_error=calls.append)   # 同一次损坏不重复回调
    assert len(calls) == 1


def test_empty_queue_no_file_side_effects(tmp_path):
    p = PendingAnalysis(str(tmp_path))

    assert p.count() == 0
    assert p.remove(["x"]) == 0
    assert not os.path.exists(p.path)


def test_add_empty_ids_without_existing_does_not_create_file(tmp_path):
    p = PendingAnalysis(str(tmp_path))

    stats = p.add([])

    assert stats["added"] == 0 and stats["total"] == 0
    assert not os.path.exists(p.path)


def test_invalid_max_age_keeps_unknown_timestamp(tmp_path):
    p = PendingAnalysis(str(tmp_path), max_age_days=1)
    p.add(["a"], now=datetime.utcnow() - timedelta(days=30))
    # 时间戳非法时不视为超龄 (避免误删)
    path = tmp_path / "pending_analysis.jsonl"
    path.write_text(json.dumps({"id": "a", "added_at": "bad", "reason": "x"}) + "\n",
                    encoding="utf-8")
    stats = p.add(["b"])

    assert stats["total"] == 2
