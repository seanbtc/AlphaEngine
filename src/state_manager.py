"""持久状态管理器 — state.json 读写 + 重启恢复."""
import json
import os
from datetime import datetime
from typing import Optional

from src.atomic_io import atomic_copy, atomic_write_text

try:
    from src.alpha_engine import REGIME_ALPHA_MAP
    _LEGAL_REGIMES = tuple(REGIME_ALPHA_MAP.keys())
except Exception:  # pragma: no cover - 兜底, 防止 import 路径异常时无法校验
    _LEGAL_REGIMES = ("BEAR_BOTTOM", "RECOVERY", "BULL", "DEEP_BULL",
                      "BULL_COOLING", "BEAR", "BEAR_DEEP")


class StateManager:
    def __init__(self, data_dir: str, state_file: str = "state.json"):
        self.state_file = os.path.join(data_dir, state_file) if not os.path.isabs(state_file) else state_file
        self.data_dir = data_dir
        self._state = None
        self._dirty = False
        self.last_recovery: Optional[dict] = None

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
                "regime_progress": 0.5,
                "deferred_build": False,
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
                "post_count": 0,
                "last_analysis_at": "",
                "last_deepseek_at": "",
                "last_distill_at": "",
                "last_calibration_at": "",
                "last_review_at": "",
                "last_tick_at": "",
                "last_prompt_hash": "",
                "uptime_started_at": datetime.utcnow().isoformat() + "Z",
                "outage": {},
            },
        }

    def load(self, on_recovered=None, memory=None) -> dict:
        """加载 state.json; 损坏 (JSON 非法/版本不符/结构非法) 时备份并从记忆重建.

        on_recovered(info) 在发生损坏恢复后回调 (info 含 reason/backup_path/rebuilt/
        regime/alpha); 回调异常不影响启动。memory 缺省时按 data_dir 惰性构建。
        """
        if self._state is not None:
            return self._state
        os.makedirs(self.data_dir, exist_ok=True)
        if os.path.exists(self.state_file):
            problem = None
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                problem = self._validate_state(loaded)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                problem = f"JSON 解析失败: {exc}"
            except IOError as exc:
                problem = f"读取失败: {exc}"
            if problem is None:
                self._state = loaded
                warning = self._normalize_regime_progress(loaded)
                if warning:
                    self._dirty = True
                    print(f"[StateManager] 告警: {warning} (已归一化为 "
                          f"{loaded['alpha']['regime_progress']})")
                return self._state
            self._recover_from_corruption(problem, on_recovered, memory)
            return self._state
        self._state = self._default_state()
        # 新建默认状态必须落盘: 此刻 _dirty 为 False, 非 force 会被脏门控跳过
        self.save(force=True)
        return self._state

    @staticmethod
    def _validate_state(loaded) -> Optional[str]:
        """校验 state 结构; 合法返回 None, 否则返回原因字符串.

        alpha.regime_progress 非法/越界不在此判定 (不触发损坏恢复): 由
        _normalize_regime_progress 就地归一化并告警 (脏 progress 会让
        tick_alpha/step_alpha 抛错)。
        """
        if not isinstance(loaded, dict):
            return "顶层结构非法 (非 JSON 对象)"
        if loaded.get("version") != 1:
            return f"version 不匹配: {loaded.get('version')!r} != 1"
        regime = loaded.get("regime")
        if not isinstance(regime, dict):
            return "regime 结构非法"
        if regime.get("current") not in _LEGAL_REGIMES:
            return f"regime.current 非法: {regime.get('current')!r}"
        alpha = loaded.get("alpha")
        if not isinstance(alpha, dict):
            return "alpha 结构非法"
        value = alpha.get("current")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"alpha.current 非数值: {value!r}"
        if not (-1.0 <= float(value) <= 1.0):
            return f"alpha.current 越界: {value!r}"
        return None

    @staticmethod
    def _normalize_regime_progress(loaded) -> Optional[str]:
        """alpha.regime_progress 非法/越界 → 就地归一化, 返回告警原因 (None=正常).

        仅归一化, 不触发损坏恢复 (区别于 _validate_state 的返回原因路径):
        脏 progress (非数值/NaN/越界) 会让 tick_alpha/step_alpha 抛错, 这里统一
        收敛为 [0,1] 浮点并告警; 归一化值随下一次 save 落盘 (标记 dirty)。
        """
        alpha = loaded.get("alpha") if isinstance(loaded, dict) else None
        if not isinstance(alpha, dict) or "regime_progress" not in alpha:
            return None
        value = alpha.get("regime_progress")
        if isinstance(value, bool):
            alpha["regime_progress"] = 0.5
            return f"alpha.regime_progress 非数值: {value!r}"
        try:
            number = float(value)
        except (TypeError, ValueError):
            alpha["regime_progress"] = 0.5
            return f"alpha.regime_progress 非数值: {value!r}"
        if number != number:  # NaN
            alpha["regime_progress"] = 0.5
            return f"alpha.regime_progress 非数值: {value!r}"
        if not isinstance(value, (int, float)):
            alpha["regime_progress"] = max(0.0, min(1.0, number))
            return f"alpha.regime_progress 非数值: {value!r}"
        if not (0.0 <= number <= 1.0):
            alpha["regime_progress"] = max(0.0, min(1.0, number))
            return f"alpha.regime_progress 越界: {value!r}"
        return None

    def _recover_from_corruption(self, problem: str, on_recovered, memory) -> dict:
        """备份损坏文件 → 尝试从记忆重建 → 无记忆时降级默认, 最后回调通知."""
        backup_path = self._backup_corrupt_state()
        print(f"[StateManager] state 损坏 ({problem}); 备份: {backup_path or '备份失败'}")
        info = {"reason": problem, "backup_path": backup_path, "rebuilt": False,
                "regime": None, "alpha": None}
        if memory is None:
            memory = self._build_memory()
        rebuilt = False
        if memory is not None:
            try:
                rebuilt = self.regenerate_from_memory(memory)
            except Exception as exc:
                print(f"[StateManager] 从记忆重建异常: {exc}")
                rebuilt = False
        if not rebuilt:
            self._state = self._default_state()
            self.save(force=True)
            print("[StateManager] 无记忆可重建, 已使用默认状态")
        info["rebuilt"] = rebuilt
        info["regime"] = self.get_regime()
        info["alpha"] = self.get_alpha()
        self.last_recovery = info
        if on_recovered is not None:
            try:
                on_recovered(info)
            except Exception as exc:
                print(f"[StateManager] 恢复回调异常 (不影响启动): {exc}")
        return info

    def _build_memory(self):
        try:
            from src.memory import Memory
            return Memory(self.data_dir)
        except Exception as exc:
            print(f"[StateManager] 无法构建 Memory 用于重建: {exc}")
            return None

    def _backup_corrupt_state(self) -> Optional[str]:
        """把损坏的 state.json 备份为 state.json.corrupt.<UTC时间戳> (保留全部)."""
        if not os.path.exists(self.state_file):
            return None
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        base = f"{self.state_file}.corrupt.{ts}"
        backup = base
        index = 1
        while os.path.exists(backup):
            backup = f"{base}.{index}"
            index += 1
        try:
            atomic_copy(self.state_file, backup)
            return backup
        except OSError as exc:
            print(f"[StateManager] 损坏文件备份失败: {exc}")
            return None

    def save(self, force: bool = False) -> None:
        if self._state is None:
            return
        if not force and not self._dirty:
            return
        os.makedirs(self.data_dir, exist_ok=True)
        atomic_write_text(self.state_file,
                          json.dumps(self._state, ensure_ascii=False, indent=2))
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
        # 本方法直接改内存而未走 set(): 必须置脏, 否则 save() 会被脏门控
        # 静默跳过 (复盘轮 set+save 清脏后, 内存已 +1 但磁盘停在旧快照)。
        self._dirty = True
        self.save()

    @staticmethod
    def _clamp_alpha(value) -> float:
        """把任意输入收敛为 [-1, 1] 内的浮点数 (非数值/NaN → 0.0)."""
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 0.0
        if value != value:  # NaN
            return 0.0
        return max(-1.0, min(1.0, value))

    def regenerate_from_memory(self, memory) -> bool:
        """从 alpha_history.json 重建状态（用于状态文件损坏/丢失时恢复）.

        重建后校验 regime 为 7 个合法枚举之一、alpha/target 收敛到 [-1, 1];
        非法值回退 (regime→BEAR, alpha→0.0) 并打印告警。无 alpha 历史返回 False。
        """
        alpha_hist = memory.load_alpha_history()
        if not alpha_hist:
            print("[StateManager] No alpha history to rebuild from, using default state")
            return False

        last = alpha_hist[-1] if isinstance(alpha_hist[-1], dict) else {}
        regime = last.get("regime", "BEAR")
        if regime not in _LEGAL_REGIMES:
            print(f"[StateManager] 重建 regime 非法 {regime!r}, 回退 BEAR")
            regime = "BEAR"
        alpha = self._clamp_alpha(last.get("alpha", 0.0))
        target = self._clamp_alpha(last.get("target_alpha", alpha))

        state = self._default_state()
        state["regime"]["current"] = regime
        state["regime"]["started_at"] = last.get("date", state["regime"]["started_at"])
        state["regime"]["last_changed_at"] = last.get("date", state["regime"]["last_changed_at"])
        state["alpha"]["current"] = alpha
        state["alpha"]["target"] = target
        state["alpha"]["last_change_at"] = last.get("date", state["alpha"]["last_change_at"])
        state["runtime"]["analysis_count"] = len(alpha_hist)

        problem = self._validate_state(state)
        if problem:
            print(f"[StateManager] 重建状态校验失败: {problem}")
            return False

        self._state = state
        self.save(force=True)
        print(f"[StateManager] Rebuilt state from alpha history "
              f"(regime={state['regime']['current']}, alpha={state['alpha']['current']})")
        return True

    def get_regime(self) -> str:
        return self.get("regime.current", "BEAR")

    def get_alpha(self) -> float:
        return float(self.get("alpha.current", 0.0))
