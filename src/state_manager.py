"""持久状态管理器 — state.json 读写 + 重启恢复."""
import json
import os
from datetime import datetime
from typing import Optional


class StateManager:
    def __init__(self, data_dir: str, state_file: str = "state.json"):
        self.state_file = os.path.join(data_dir, state_file) if not os.path.isabs(state_file) else state_file
        self.data_dir = data_dir
        self._state = None
        self._dirty = False

    def _default_state(self) -> dict:
        return {
            "version": 1,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "regime": {
                "current": "BEAR",
                "started_at": datetime.utcnow().isoformat() + "Z",
                "entered_from": "INIT",
                "cooldown_remaining": 0,
                "stability_counter": 0,
                "last_changed_at": datetime.utcnow().isoformat() + "Z",
            },
            "alpha": {
                "current": 0.0,
                "target": 0.0,
                "transition_progress": 0.0,
                "last_change_at": datetime.utcnow().isoformat() + "Z",
                "locked": False,
                "lock_reason": "",
            },
            "evidence": {
                "accumulators": {},
                "by_category": {},
            },
            "runtime": {
                "analysis_count": 0,
                "last_analysis_at": "",
                "last_distill_at": "",
                "last_calibration_at": "",
                "uptime_started_at": datetime.utcnow().isoformat() + "Z",
            },
        }

    def load(self) -> dict:
        if self._state is not None:
            return self._state
        os.makedirs(self.data_dir, exist_ok=True)
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if loaded.get("version") == 1:
                    self._state = loaded
                    return self._state
            except (json.JSONDecodeError, IOError):
                pass
        self._state = self._default_state()
        self.save()
        return self._state

    def save(self, force: bool = False) -> None:
        if self._state is None:
            return
        if not force and not self._dirty:
            return
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_file)
        self._dirty = False

    def get(self, path: str, default=None):
        state = self.load()
        keys = path.split(".")
        current = state
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key, default)
            else:
                return default
        return current

    def set(self, path: str, value):
        state = self.load()
        keys = path.split(".")
        current = state
        for key in keys[:-1]:
            current = current.setdefault(key, {})
        current[keys[-1]] = value
        self._state = state
        self._dirty = True

    def update_runtime(self):
        state = self.load()
        state["runtime"]["analysis_count"] += 1
        state["runtime"]["last_analysis_at"] = datetime.utcnow().isoformat() + "Z"
        self._state = state
        self.save()

    def regenerate_from_memory(self, memory) -> bool:
        """从 memory.md 和 alpha_history.json 重建状态（用于状态文件损坏/丢失时恢复）."""
        alpha_hist = memory.load_alpha_history()
        if not alpha_hist:
            print("[StateManager] No alpha history to rebuild from, using default state")
            return False

        last = alpha_hist[-1]
        state = self._default_state()
        state["regime"]["current"] = last.get("regime", "BEAR")
        state["regime"]["started_at"] = last.get("date", state["regime"]["started_at"])
        state["regime"]["last_changed_at"] = last.get("date", state["regime"]["last_changed_at"])
        state["alpha"]["current"] = last.get("alpha", 0.0)
        state["alpha"]["target"] = last.get("target_alpha", last.get("alpha", 0.0))
        state["alpha"]["last_change_at"] = last.get("date", state["alpha"]["last_change_at"])
        state["runtime"]["analysis_count"] = len(alpha_hist)

        self._state = state
        self.save()
        print(f"[StateManager] Rebuilt state from alpha history "
              f"(regime={state['regime']['current']}, alpha={state['alpha']['current']})")
        return True

    def get_regime(self) -> str:
        return self.get("regime.current", "BEAR")

    def get_alpha(self) -> float:
        return float(self.get("alpha.current", 0.0))
