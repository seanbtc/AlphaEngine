"""state 损坏备份 + 从估值历史重建 + v1→v2 迁移 — 离线单元测试."""
import json
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.memory import Memory  # noqa: E402
from src.state_manager import STATE_VERSION, StateManager  # noqa: E402

_HISTORY = [
    {"date": "2026-09-01T00:00:00Z", "valuation": 0.35, "label": "轻度低估",
     "confidence": "medium", "note": "ETF 流出放缓"},
    {"date": "2026-09-15T00:00:00Z", "valuation": 0.48, "label": "轻度低估",
     "confidence": "low", "note": "分歧"},
    {"date": "2026-10-01T00:00:00Z", "valuation": 0.62, "label": "轻度高估",
     "confidence": "high", "note": "MVRV 偏高"},
]


def _seed_memory(data_dir: Path) -> int:
    (data_dir / "valuation_history.json").write_text(
        json.dumps(_HISTORY, ensure_ascii=False), encoding="utf-8")
    return len(_HISTORY)


def _corrupt_backups(data_dir: Path):
    return list(data_dir.glob("state.json.corrupt.*"))


def _load_with_events(data_dir: Path):
    events = []
    sm = StateManager(str(data_dir), "state.json")
    state = sm.load(on_recovered=events.append)
    return sm, state, events


def test_corrupt_json_backup_and_rebuild(tmp_path):
    count = _seed_memory(tmp_path)
    (tmp_path / "state.json").write_text("{ this is not json", encoding="utf-8")

    sm, state, events = _load_with_events(tmp_path)

    assert len(events) == 1
    info = events[0]
    assert info["rebuilt"] is True
    assert "解析失败" in info["reason"]
    backup = Path(info["backup_path"])
    assert backup.exists()
    assert backup.name.startswith("state.json.corrupt.")
    assert backup.read_text(encoding="utf-8") == "{ this is not json"
    assert state["version"] == STATE_VERSION
    assert state["valuation"]["current"] == pytest.approx(0.62)
    assert state["valuation"]["label"] == "轻度高估"
    assert state["runtime"]["analysis_count"] == count
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["valuation"]["current"] == pytest.approx(0.62)
    # 二次 load 走缓存, 不再回调/不再备份
    assert sm.load(on_recovered=events.append) is state
    assert len(events) == 1
    assert len(_corrupt_backups(tmp_path)) == 1


def test_unknown_version_rebuilds(tmp_path):
    _seed_memory(tmp_path)
    legacy = {"version": 99, "valuation": {"current": 0.5}}
    (tmp_path / "state.json").write_text(json.dumps(legacy), encoding="utf-8")

    _, state, events = _load_with_events(tmp_path)

    assert events[0]["rebuilt"] is True
    assert "version 不匹配" in events[0]["reason"]
    assert Path(events[0]["backup_path"]).read_text(encoding="utf-8") == json.dumps(legacy)
    assert state["valuation"]["current"] == pytest.approx(0.62)


def test_v1_state_migrates_without_recovery(tmp_path):
    """v1 (regime/alpha) 启动时迁移: 保留 runtime/ma/thesis, 估值置空, 不触发恢复回调."""
    v1 = {
        "version": 1,
        "created_at": "2026-08-01T00:00:00Z",
        "regime": {"current": "RECOVERY", "started_at": "2026-08-01T00:00:00Z"},
        "alpha": {"current": 0.7, "target": 0.7, "regime_progress": 0.5},
        "runtime": {"analysis_count": 62, "post_count": 38,
                    "last_analysis_at": "2026-11-10T04:25:00Z",
                    "last_tick_at": "2026-11-10T04:25:00Z",
                    "outage": {"reason": "fetch_error", "since": "2026-11-09T00:00:00Z"}},
        "ma": {"available": True, "zone": "above"},
        "thesis": {"thesis_id": "t-1", "status": "active"},
    }
    (tmp_path / "state.json").write_text(json.dumps(v1), encoding="utf-8")

    sm, state, events = _load_with_events(tmp_path)

    assert events == [] and sm.last_recovery is None
    assert state["version"] == STATE_VERSION
    assert state["valuation"]["current"] is None
    assert state["valuation"]["label"] == "未评估"
    assert state["runtime"]["analysis_count"] == 62
    assert state["runtime"]["post_count"] == 38
    assert "last_tick_at" not in state["runtime"]
    assert state["ma"]["zone"] == "above"
    assert state["thesis"]["thesis_id"] == "t-1"
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["version"] == STATE_VERSION
    assert "regime" not in persisted and "alpha" not in persisted


def test_invalid_valuation_rebuilds(tmp_path):
    _seed_memory(tmp_path)
    broken = {"version": STATE_VERSION,
              "valuation": {"current": 1.5, "label": "极度高估"},
              "runtime": {"analysis_count": 1, "post_count": 0}}
    (tmp_path / "state.json").write_text(json.dumps(broken), encoding="utf-8")

    _, state, events = _load_with_events(tmp_path)

    assert events[0]["rebuilt"] is True
    assert "valuation.current 非法" in events[0]["reason"]
    assert state["valuation"]["current"] == pytest.approx(0.62)


def test_missing_valuation_structure_rebuilds(tmp_path):
    _seed_memory(tmp_path)
    (tmp_path / "state.json").write_text(
        json.dumps({"version": STATE_VERSION,
                    "runtime": {"analysis_count": 1, "post_count": 0}}),
        encoding="utf-8")

    _, _, events = _load_with_events(tmp_path)

    assert events[0]["rebuilt"] is True
    assert "valuation 结构非法" in events[0]["reason"]


def test_missing_runtime_structure_rebuilds(tmp_path):
    _seed_memory(tmp_path)
    (tmp_path / "state.json").write_text(
        json.dumps({"version": STATE_VERSION,
                    "valuation": {"current": 0.5}}), encoding="utf-8")

    _, _, events = _load_with_events(tmp_path)

    assert events[0]["rebuilt"] is True
    assert "runtime 结构非法" in events[0]["reason"]


def test_illegal_history_not_reused(tmp_path):
    (tmp_path / "valuation_history.json").write_text(json.dumps([
        {"date": "2026-09-01T00:00:00Z", "valuation": 2.5, "label": "?"},
    ]), encoding="utf-8")
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")

    _, state, events = _load_with_events(tmp_path)

    assert events[0]["rebuilt"] is True
    assert state["valuation"]["current"] is None
    assert state["valuation"]["label"] == "未评估"
    assert state["runtime"]["analysis_count"] == 1
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["valuation"]["current"] is None


def test_no_memory_falls_back_to_default(tmp_path):
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")

    _, state, events = _load_with_events(tmp_path)

    info = events[0]
    assert info["rebuilt"] is False
    assert Path(info["backup_path"]).read_text(encoding="utf-8") == "broken"
    assert state["valuation"]["current"] is None
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["version"] == STATE_VERSION
    assert persisted["valuation"]["current"] is None


def test_empty_valuation_history_falls_back_to_default(tmp_path):
    (tmp_path / "valuation_history.json").write_text("[]", encoding="utf-8")
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")

    _, _, events = _load_with_events(tmp_path)

    assert events[0]["rebuilt"] is False


def test_lazy_memory_built_when_not_passed(tmp_path):
    _seed_memory(tmp_path)
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")
    events = []
    sm = StateManager(str(tmp_path), "state.json")

    state = sm.load(on_recovered=events.append)

    assert events[0]["rebuilt"] is True
    assert sm.last_recovery is not None
    assert state["valuation"]["current"] == pytest.approx(0.62)


def test_callback_exception_does_not_break_load(tmp_path):
    _seed_memory(tmp_path)
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")
    sm = StateManager(str(tmp_path), "state.json")

    def _boom(info):
        raise RuntimeError("notify down")

    state = sm.load(on_recovered=_boom)

    assert state["valuation"]["current"] == pytest.approx(0.62)
    assert sm.last_recovery["rebuilt"] is True


def test_normal_load_no_backup_no_rebuild(tmp_path):
    valid = {
        "version": STATE_VERSION,
        "created_at": "2026-11-01T00:00:00Z",
        "valuation": {
            "current": 0.55, "previous": 0.5, "label": "轻度高估",
            "confidence": "medium", "evidence": "x",
            "updated_at": "2026-11-01T00:00:00Z", "changes": 1,
        },
        "runtime": {"analysis_count": 7, "post_count": 3},
    }
    (tmp_path / "state.json").write_text(json.dumps(valid), encoding="utf-8")
    events = []

    sm = StateManager(str(tmp_path), "state.json")
    state = sm.load(on_recovered=events.append)

    assert events == []
    assert sm.last_recovery is None
    assert state["valuation"]["current"] == pytest.approx(0.55)
    assert _corrupt_backups(tmp_path) == []
    assert not (tmp_path / "state.json.tmp").exists()


def test_default_state_when_file_missing(tmp_path):
    events = []
    sm = StateManager(str(tmp_path), "state.json")

    state = sm.load(on_recovered=events.append)

    assert events == []
    assert state["version"] == STATE_VERSION
    assert state["valuation"]["current"] is None
    assert state["runtime"]["analysis_count"] == 0


def test_rebuild_uses_passed_memory_object(tmp_path):
    memory = Memory(str(tmp_path))
    memory.save_valuation_history([{"date": "2026-09-10T00:00:00Z",
                                    "valuation": 0.18, "label": "极度低估",
                                    "confidence": "high", "note": "投降"}])
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")
    events = []
    sm = StateManager(str(tmp_path), "state.json")

    state = sm.load(on_recovered=events.append, memory=memory)

    assert events[0]["rebuilt"] is True
    assert state["valuation"]["current"] == pytest.approx(0.18)
    assert state["valuation"]["label"] == "极度低估"
    assert state["valuation"]["confidence"] == "high"
    assert state["valuation"]["evidence"] == "投降"


# ---- set_valuation 语义 ----

def test_set_valuation_tracks_previous_and_changes(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()

    sm.set_valuation(0.42, confidence="medium", evidence="a")
    block = sm.set_valuation(0.62, confidence="high", evidence="b")

    assert block["previous"] == pytest.approx(0.42)
    assert block["current"] == pytest.approx(0.62)
    assert block["label"] == "轻度高估"
    # 首次评估 (None → 0.42) 不算变更, 0.42 → 0.62 记 1 次
    assert block["changes"] == 1
    assert sm.get_valuation() == pytest.approx(0.62)


def test_set_valuation_same_value_not_counted_as_change(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()

    sm.set_valuation(0.62)
    block = sm.set_valuation(0.62)

    assert block["changes"] == 0


def test_get_valuation_invalid_falls_back_to_none(tmp_path):
    state = {
        "version": STATE_VERSION,
        "valuation": {"current": None, "label": "未评估"},
        "runtime": {"analysis_count": 0, "post_count": 0},
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    sm = StateManager(str(tmp_path), "state.json")

    assert sm.get_valuation() is None


# ---- 主入口告警接线 ----

def test_alert_state_recovery_sends_and_swallows_failure(capsys):
    from src.alpha import _alert_state_recovery

    class _Recorder:
        def __init__(self):
            self.calls = []

        def alert(self, title, body=""):
            self.calls.append((title, body))

    recorder = _Recorder()
    _alert_state_recovery(recorder, {"reason": "r", "backup_path": "b",
                                     "rebuilt": True, "valuation": 0.62})
    assert recorder.calls[0][0] == "state 已从记忆重建"
    assert "0.62" in recorder.calls[0][1]

    class _Boom:
        def alert(self, title, body=""):
            raise RuntimeError("notify down")

    _alert_state_recovery(_Boom(), {"reason": "r", "backup_path": None,
                                    "rebuilt": False})
    out = capsys.readouterr().out
    assert "notify down" in out
    assert "无记忆可重建" in out


def test_init_components_alerts_on_recovery(tmp_path, monkeypatch):
    from src import alpha as alpha_mod

    _seed_memory(tmp_path)
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")
    alerts = []

    class _RecordingDingTalk:
        def __init__(self, cfg):
            pass

        def alert(self, title, body=""):
            alerts.append((title, body))
            return True

    monkeypatch.setattr(alpha_mod, "DingTalk", _RecordingDingTalk)
    cfg = {"paths": {"data_dir": str(tmp_path)},
           "ai_service": {"endpoint": "http://127.0.0.1:5010"}}

    components = alpha_mod.init_components(cfg)

    assert alerts and alerts[0][0] == "state 已从记忆重建"
    assert components["state"].last_recovery["rebuilt"] is True
    assert components["state"].get_valuation() == pytest.approx(0.62)
