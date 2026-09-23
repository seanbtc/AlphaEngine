"""WP4 ①: state 损坏备份 + 从记忆重建 — 离线单元测试 (真实 data/ 样例只读)."""
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.alpha_engine import AlphaEngine  # noqa: E402
from src.memory import Memory  # noqa: E402
from src.state_manager import StateManager  # noqa: E402

_REAL_ALPHA_HISTORY = _ALPHA_ROOT / "data" / "alpha_history.json"
_REAL_MEMORY_MD = _ALPHA_ROOT / "data" / "memory.md"


def _seed_real_memory(data_dir: Path) -> int:
    """把真实 alpha_history.json/memory.md 复制到临时目录, 返回历史条数."""
    if not _REAL_ALPHA_HISTORY.exists():
        pytest.skip("真实 data/alpha_history.json 不存在")
    shutil.copy2(_REAL_ALPHA_HISTORY, data_dir / "alpha_history.json")
    if _REAL_MEMORY_MD.exists():
        shutil.copy2(_REAL_MEMORY_MD, data_dir / "memory.md")
    history = json.loads(_REAL_ALPHA_HISTORY.read_text(encoding="utf-8"))
    return len(history)


def _expected_last():
    history = json.loads(_REAL_ALPHA_HISTORY.read_text(encoding="utf-8"))
    return history[-1]["regime"], float(history[-1]["alpha"])


def _corrupt_backups(data_dir: Path):
    return list(data_dir.glob("state.json.corrupt.*"))


def _load_with_events(data_dir: Path):
    events = []
    sm = StateManager(str(data_dir), "state.json")
    state = sm.load(on_recovered=events.append)
    return sm, state, events


def test_corrupt_json_backup_and_rebuild(tmp_path):
    count = _seed_real_memory(tmp_path)
    (tmp_path / "state.json").write_text("{ this is not json", encoding="utf-8")
    regime, alpha = _expected_last()

    sm, state, events = _load_with_events(tmp_path)

    assert len(events) == 1
    info = events[0]
    assert info["rebuilt"] is True
    assert "解析失败" in info["reason"]
    backup = Path(info["backup_path"])
    assert backup.exists()
    assert backup.name.startswith("state.json.corrupt.")
    assert backup.read_text(encoding="utf-8") == "{ this is not json"
    assert state["regime"]["current"] == regime
    assert state["alpha"]["current"] == pytest.approx(alpha)
    assert state["runtime"]["analysis_count"] == count
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["regime"]["current"] == regime
    assert persisted["alpha"]["current"] == pytest.approx(alpha)
    # 二次 load 走缓存, 不再回调/不再备份
    assert sm.load(on_recovered=events.append) is state
    assert len(events) == 1
    assert len(_corrupt_backups(tmp_path)) == 1


def test_version_mismatch_rebuilds(tmp_path):
    _seed_real_memory(tmp_path)
    legacy = {"version": 2, "regime": {"current": "BULL"}, "alpha": {"current": 0.5}}
    (tmp_path / "state.json").write_text(json.dumps(legacy), encoding="utf-8")
    regime, alpha = _expected_last()

    _, state, events = _load_with_events(tmp_path)

    assert events[0]["rebuilt"] is True
    assert "version 不匹配" in events[0]["reason"]
    assert Path(events[0]["backup_path"]).read_text(encoding="utf-8") == json.dumps(legacy)
    assert state["regime"]["current"] == regime
    assert state["alpha"]["current"] == pytest.approx(alpha)


def test_invalid_regime_rebuilds(tmp_path):
    _seed_real_memory(tmp_path)
    broken = {"version": 1, "regime": {"current": "MOON"}, "alpha": {"current": 0.1}}
    (tmp_path / "state.json").write_text(json.dumps(broken), encoding="utf-8")

    _, state, events = _load_with_events(tmp_path)

    assert events[0]["rebuilt"] is True
    assert "regime.current 非法" in events[0]["reason"]
    assert state["regime"]["current"] in ("BEAR", "RECOVERY")


def test_alpha_out_of_range_rebuilds(tmp_path):
    _seed_real_memory(tmp_path)
    broken = {"version": 1, "regime": {"current": "BULL"}, "alpha": {"current": 1.5}}
    (tmp_path / "state.json").write_text(json.dumps(broken), encoding="utf-8")

    _, state, events = _load_with_events(tmp_path)

    assert events[0]["rebuilt"] is True
    assert "alpha.current 越界" in events[0]["reason"]
    assert -1.0 <= state["alpha"]["current"] <= 1.0


def test_missing_regime_structure_rebuilds(tmp_path):
    _seed_real_memory(tmp_path)
    (tmp_path / "state.json").write_text(
        json.dumps({"version": 1, "alpha": {"current": 0.0}}), encoding="utf-8")

    _, _, events = _load_with_events(tmp_path)

    assert events[0]["rebuilt"] is True
    assert "regime 结构非法" in events[0]["reason"]


def test_illegal_history_sanitized_on_rebuild(tmp_path):
    (tmp_path / "alpha_history.json").write_text(json.dumps([
        {"date": "2026-09-01T00:00:00Z", "regime": "INIT",
         "alpha": 2.5, "target_alpha": -9},
    ]), encoding="utf-8")
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")

    _, state, events = _load_with_events(tmp_path)

    assert events[0]["rebuilt"] is True
    assert state["regime"]["current"] == "BEAR"
    assert state["alpha"]["current"] == 1.0
    assert state["alpha"]["target"] == -1.0
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["alpha"]["current"] == 1.0


def test_no_memory_falls_back_to_default(tmp_path):
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")

    _, state, events = _load_with_events(tmp_path)

    info = events[0]
    assert info["rebuilt"] is False
    assert Path(info["backup_path"]).read_text(encoding="utf-8") == "broken"
    assert state["regime"]["current"] == "BEAR"
    assert state["alpha"]["current"] == 0.0
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["version"] == 1
    assert persisted["regime"]["current"] == "BEAR"


def test_empty_alpha_history_falls_back_to_default(tmp_path):
    (tmp_path / "alpha_history.json").write_text("[]", encoding="utf-8")
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")

    _, _, events = _load_with_events(tmp_path)

    assert events[0]["rebuilt"] is False


def test_lazy_memory_built_when_not_passed(tmp_path):
    _seed_real_memory(tmp_path)
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")
    events = []
    sm = StateManager(str(tmp_path), "state.json")

    state = sm.load(on_recovered=events.append)

    assert events[0]["rebuilt"] is True
    assert sm.last_recovery is not None
    assert state["regime"]["current"] == _expected_last()[0]


def test_callback_exception_does_not_break_load(tmp_path):
    _seed_real_memory(tmp_path)
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")
    sm = StateManager(str(tmp_path), "state.json")

    def _boom(info):
        raise RuntimeError("notify down")

    state = sm.load(on_recovered=_boom)

    assert state["regime"]["current"] == _expected_last()[0]
    assert sm.last_recovery["rebuilt"] is True


def test_normal_load_no_backup_no_rebuild(tmp_path):
    valid = {
        "version": 1,
        "regime": {"current": "DEEP_BULL", "started_at": "2026-01-01T00:00:00Z",
                   "entered_from": "BULL", "cooldown_remaining": 0,
                   "stability_counter": 0, "last_changed_at": "2026-01-01T00:00:00Z"},
        "alpha": {"current": 0.3, "target": 0.3, "regime_progress": 0.5,
                  "deferred_build": False, "transition_progress": 1.0,
                  "last_change_at": "2026-01-01T00:00:00Z", "locked": False,
                  "lock_reason": ""},
    }
    (tmp_path / "state.json").write_text(json.dumps(valid), encoding="utf-8")
    events = []

    sm = StateManager(str(tmp_path), "state.json")
    state = sm.load(on_recovered=events.append)

    assert events == []
    assert sm.last_recovery is None
    assert state["regime"]["current"] == "DEEP_BULL"
    assert state["alpha"]["current"] == 0.3
    assert _corrupt_backups(tmp_path) == []
    assert not (tmp_path / "state.json.tmp").exists()


def test_default_state_when_file_missing(tmp_path):
    events = []
    sm = StateManager(str(tmp_path), "state.json")

    state = sm.load(on_recovered=events.append)

    assert events == []
    assert state["version"] == 1
    assert state["regime"]["current"] == "BEAR"


def test_rebuild_uses_passed_memory_object(tmp_path):
    memory = Memory(str(tmp_path))
    memory.save_alpha_history([{"date": "2026-09-10T00:00:00Z", "regime": "BEAR_DEEP",
                                "alpha": -0.25, "target_alpha": -0.3}])
    (tmp_path / "state.json").write_text("broken", encoding="utf-8")
    events = []
    sm = StateManager(str(tmp_path), "state.json")

    state = sm.load(on_recovered=events.append, memory=memory)

    assert events[0]["rebuilt"] is True
    assert state["regime"]["current"] == "BEAR_DEEP"
    assert state["alpha"]["current"] == pytest.approx(-0.25)
    assert state["alpha"]["target"] == pytest.approx(-0.3)


# ---- WP8 8C 附带加固: alpha.regime_progress 非法/越界归一化 ----

@pytest.mark.parametrize("bad,expected", [
    ("bad", 0.5), (None, 0.5), (True, 0.5), (float("nan"), 0.5),
    (1.5, 1.0), (-0.2, 0.0), ("1.5", 1.0), ("0.25", 0.25),
])
def test_dirty_regime_progress_normalized(tmp_path, bad, expected, capsys):
    """非法/越界 progress → 归一化+告警 (不触发损坏恢复), tick_alpha 不抛错。"""
    state = {
        "version": 1,
        "regime": {"current": "BEAR"},
        "alpha": {"current": 0.0, "regime_progress": bad},
        "runtime": {"last_tick_at": "2026-09-22T00:00:00Z"},
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")

    sm = StateManager(str(tmp_path), "state.json")
    loaded = sm.load()

    assert loaded["alpha"]["regime_progress"] == pytest.approx(expected)
    assert sm.get("alpha.regime_progress") == pytest.approx(expected)
    out = capsys.readouterr().out
    assert "告警" in out and "regime_progress" in out
    assert sm.last_recovery is None
    assert _corrupt_backups(tmp_path) == []
    # 归一化值随下一次 save 落盘 (在 tick 改变 progress 之前校验)
    sm.save(force=True)
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["alpha"]["regime_progress"] == pytest.approx(expected)
    # 归一化后 tick_alpha 正常推进 (旧实现字符串会 TypeError)
    engine = AlphaEngine({}, sm)
    engine.tick_alpha(now=datetime(2026, 9, 23, 0, 0, 0))
    assert 0.0 <= float(sm.get("alpha.regime_progress")) <= 1.0


def test_valid_regime_progress_no_warning(tmp_path, capsys):
    state = {
        "version": 1,
        "regime": {"current": "BEAR"},
        "alpha": {"current": 0.0, "regime_progress": 0.5},
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    sm = StateManager(str(tmp_path), "state.json")
    assert sm.load()["alpha"]["regime_progress"] == 0.5
    assert capsys.readouterr().out == ""


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
                                     "rebuilt": True, "regime": "BEAR",
                                     "alpha": -0.5})
    assert recorder.calls[0][0] == "state 已从记忆重建"
    assert "BEAR" in recorder.calls[0][1]

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

    _seed_real_memory(tmp_path)
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
    assert components["state"].get_regime() == _expected_last()[0]
