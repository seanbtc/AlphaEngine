"""持久状态管理器 — state.json 读写 + 重启恢复 + v1→v2 迁移.

state v2 schema (2026-11 估值重构):
    version: 2
    valuation: {current(0~1 或 null), previous, label, confidence, evidence,
                updated_at, changes}
    runtime:   {analysis_count, post_count, last_*_at, outage, ...}
    ma / thesis: 由各模块写入

v1 (regime/alpha/evidence 三件套) 启动时一次性迁移: 保留 runtime 计数与
ma/thesis, 丢弃 regime/alpha/evidence, 估值置 null (等待下一轮 AI 重新评估)。
"""
import json
import os
from datetime import datetime, timezone
from typing import Optional

from src.atomic_io import atomic_copy, atomic_write_text
from src.valuation import UNASSESSED_LABEL, band_of, is_valid

STATE_VERSION = 2


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


class StateManager:
    def __init__(self, data_dir: str, state_file: str = "state.json"):
        self.state_file = os.path.join(data_dir, state_file) if not os.path.isabs(state_file) else state_file
        self.data_dir = data_dir
        self._state = None
        self._dirty = False
        self.last_recovery: Optional[dict] = None

    @staticmethod
    def _default_state() -> dict:
        now = _utc_iso()
        return {
            "version": STATE_VERSION,
            "created_at": now,
            "valuation": {
                "current": None,
                "previous": None,
                "label": UNASSESSED_LABEL,
                "confidence": "low",
                "evidence": "",
                "updated_at": "",
                "changes": 0,
            },
            "runtime": {
                "analysis_count": 0,
                "post_count": 0,
                "last_analysis_at": "",
                "last_deepseek_at": "",
                "last_distill_at": "",
                "last_calibration_at": "",
                "last_tick_at": "",
                "last_prompt_hash": "",
                "uptime_started_at": now,
                "outage": {},
            },
        }

    def load(self, on_recovered=None, memory=None) -> dict:
        """加载 state.json; 损坏 (JSON 非法/版本不符/结构非法) 时备份并从记忆重建.

        on_recovered(info) 在发生损坏恢复后回调 (info 含 reason/backup_path/rebuilt/
        valuation); 回调异常不影响启动。memory 缺省时按 data_dir 惰性构建。

        v1 → v2: 就地迁移 (保留 runtime/ma/thesis, regime/alpha/evidence 丢弃),
        不触发损坏恢复, 迁移结果随下一次 save 落盘。
        """
        if self._state is not None:
            return self._state
        os.makedirs(self.data_dir, exist_ok=True)
        if os.path.exists(self.state_file):
            problem = None
            loaded = None
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                problem = f"JSON 解析失败: {exc}"
            except IOError as exc:
                problem = f"读取失败: {exc}"
            if problem is None and isinstance(loaded, dict) and loaded.get("version") == 1:
                migrated = self._migrate_v1(loaded)
                self._state = migrated
                self._dirty = True
                self.save()
                print("[StateManager] state v1 → v2 迁移完成: "
                      "regime/alpha/evidence 已移除, 估值待下一轮 AI 重新评估 "
                      f"(runtime.analysis_count={migrated['runtime'].get('analysis_count', 0)} 保留)")
                return self._state
            if problem is None:
                problem = self._validate_state(loaded)
            if problem is None:
                self._state = loaded
                return self._state
            self._recover_from_corruption(problem, on_recovered, memory)
            return self._state
        self._state = self._default_state()
        # 新建默认状态必须落盘: 此刻 _dirty 为 False, 非 force 会被脏门控跳过
        self.save(force=True)
        return self._state

    @staticmethod
    def _migrate_v1(loaded: dict) -> dict:
        """v1 (regime/alpha) → v2 (valuation): 保留非仓位字段, 估值重置为空."""
        state = {
            "version": STATE_VERSION,
            "created_at": loaded.get("created_at") or _utc_iso(),
            "valuation": {
                "current": None,
                "previous": None,
                "label": UNASSESSED_LABEL,
                "confidence": "low",
                "evidence": "",
                "updated_at": "",
                "changes": 0,
            },
            "runtime": loaded.get("runtime") if isinstance(loaded.get("runtime"), dict) else {},
        }
        defaults = StateManager._default_state()["runtime"]
        for key, value in defaults.items():
            state["runtime"].setdefault(key, value)
        # old alpha 为仓位语义 (-1~+1), 不能当作估值沿用; 仅清空 last_tick 锚点
        state["runtime"].pop("last_tick_at", None)
        for key in ("ma", "thesis"):
            if key in loaded:
                state[key] = loaded[key]
        return state

    @staticmethod
    def _validate_state(loaded) -> Optional[str]:
        """校验 state 结构; 合法返回 None, 否则返回原因字符串."""
        if not isinstance(loaded, dict):
            return "顶层结构非法 (非 JSON 对象)"
        if loaded.get("version") != STATE_VERSION:
            return f"version 不匹配: {loaded.get('version')!r} != {STATE_VERSION}"
        valuation = loaded.get("valuation")
        if not isinstance(valuation, dict):
            return "valuation 结构非法"
        current = valuation.get("current")
        if current is not None and not is_valid(current):
            return f"valuation.current 非法: {current!r}"
        runtime = loaded.get("runtime")
        if not isinstance(runtime, dict):
            return "runtime 结构非法"
        if "analysis_count" not in runtime or "post_count" not in runtime:
            return "runtime 计数缺失"
        return None

    def _recover_from_corruption(self, problem: str, on_recovered, memory) -> dict:
        """备份损坏文件 → 尝试从记忆重建 → 无记忆时降级默认, 最后回调通知."""
        backup_path = self._backup_corrupt_state()
        print(f"[StateManager] state 损坏 ({problem}); 备份: {backup_path or '备份失败'}")
        info = {"reason": problem, "backup_path": backup_path, "rebuilt": False,
                "valuation": None}
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
        info["valuation"] = self.get_valuation()
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
        ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%dT%H%M%SZ")
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
        state["runtime"]["last_analysis_at"] = _utc_iso()
        self._state = state
        # 本方法直接改内存而未走 set(): 必须置脏, 否则 save() 会被脏门控
        # 静默跳过 (复盘轮 set+save 清脏后, 内存已 +1 但磁盘停在旧快照)。
        self._dirty = True
        self.save()

    def set_valuation(self, value, *, confidence: str = "low", evidence: str = "",
                      updated_at: str = "") -> dict:
        """写入新估值 (0~1): 记录 previous/label/changes 并置脏 (不落盘).

        非法估值直接抛 ValueError (调用方必须先校验) —— 不静默写入脏值。
        """
        if not is_valid(value):
            raise ValueError(f"非法估值: {value!r} (需要 0~1 数字)")
        conf = str(confidence or "low").strip().lower()
        if conf not in ("high", "medium", "low"):
            conf = "low"
        state = self.load()
        block = state.setdefault("valuation", {})
        previous = block.get("current")
        block["previous"] = previous if is_valid(previous) else None
        block["current"] = round(float(value), 4)
        block["label"] = band_of(value)
        block["confidence"] = conf
        block["evidence"] = str(evidence or "")[:300]
        block["updated_at"] = updated_at or _utc_iso()
        if is_valid(previous) and float(previous) != float(value):
            block["changes"] = int(block.get("changes", 0) or 0) + 1
        self._state = state
        self._dirty = True
        return block

    def regenerate_from_memory(self, memory) -> bool:
        """从 valuation_history.json 重建状态 (状态文件损坏/丢失时恢复).

        重建后校验估值落在 [0,1]; 非法值回退 null (未评估) 并打印告警。
        无估值历史返回 False。
        """
        history = memory.load_valuation_history()
        if not history:
            print("[StateManager] 无估值历史可重建, 使用默认状态")
            return False

        last = history[-1] if isinstance(history[-1], dict) else {}
        raw = last.get("valuation")
        state = self._default_state()
        if is_valid(raw):
            state["valuation"].update({
                "current": round(float(raw), 4),
                "label": band_of(raw),
                "confidence": str(last.get("confidence") or "low"),
                "evidence": str(last.get("note") or "")[:300],
                "updated_at": str(last.get("date") or ""),
            })
        elif raw is not None:
            print(f"[StateManager] 重建估值非法 {raw!r}, 回退未评估")
        state["runtime"]["analysis_count"] = len(history)

        problem = self._validate_state(state)
        if problem:
            print(f"[StateManager] 重建状态校验失败: {problem}")
            return False

        self._state = state
        self.save(force=True)
        print(f"[StateManager] 已从估值历史重建 (估值={state['valuation']['current']}, "
              f"records={len(history)})")
        return True

    def get_valuation(self) -> Optional[float]:
        """当前估值 (0~1); 尚未评估返回 None."""
        value = self.get("valuation.current", None)
        return float(value) if is_valid(value) else None
