"""校准参数落盘 — params.json overlay + calibration_log.jsonl 审计.

- apply_calibration 每次生效调整 → 合并写 data/params.json (键=参数路径, 值=新值;
  tmp+原子替换) 并 append data/calibration_log.jsonl (时间/参数/旧→新/来源)。
- 启动时 load_overlay + apply_overlay 恢复上次校准结果, 解决"只改内存,
  重启即失"的问题; 非法文件容错并告警。
- 只读入口 (--test-ai/--status 等) 同样只读加载, 不产生任何文件。
"""
import json
import math
import os
from datetime import datetime

from src.alpha_engine import REGIME_EXPECTED_DAYS
from src.atomic_io import atomic_write_json

PARAMS_FILE = "params.json"
LOG_FILE = "calibration_log.jsonl"

# 参数安全边界 (与月度复盘校准 prompt 中的范围一致)
PARAM_BOUNDS = {
    "smoothing.max_change_per_step": (0.01, 0.10),
    "smoothing.min_daily_step": (0.005, 0.05),
    "smoothing.cooldown_cycles_after_regime_change": (3, 30),
    "evidence.min_categories_for_regime_change": (1, 5),
    "evidence.min_total_score_for_regime_change": (0.5, 3.0),
    "evidence.high_conf_min_categories": (1, 3),
    "evidence.high_conf_min_total": (0.5, 2.5),
    "evidence.decay_per_cycle": (0.005, 0.05),
    "confidence_gate.low_confidence_max_alpha_abs": (0.1, 0.5),
}


def clamp_param(param: str, value):
    """按参数路径收敛到安全边界内 (未知参数原样返回)."""
    if param in PARAM_BOUNDS:
        lo, hi = PARAM_BOUNDS[param]
        return max(lo, min(hi, value))
    if param.startswith("regime_expected_days."):
        return max(20, min(700, int(value)))
    if param.startswith("regime_alpha_map."):
        return max(-1.0, min(1.0, float(value)))
    return value


def apply_param(engine, param: str, value, evidence=None, knowledge=None):
    """应用单个校准参数到引擎内存 (clamp 后). 返回 (ok, final_value, error).

    evidence/knowledge 为 None 时跳过证据衰减联动 (与月度复盘旧行为一致)。
    兼容无 expected_days 的旧引擎: regime_expected_days 回退改模块级锚表。
    非数值/非有限数值 (inf/NaN, 如 JSON 1e999) 直接跳过并告警, 不阻止启动。
    """
    if param == "confidence_gate.low_confidence_blocks_regime_change":
        final = bool(value)
        engine.conf_gate["low_confidence_blocks_regime_change"] = final
        return True, final, ""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False, None, f"非数值: {value!r}"
    if not math.isfinite(numeric):
        return False, None, f"非有限数值: {value!r}"
    if param == "smoothing.max_change_per_step":
        final = clamp_param(param, numeric)
        engine.smoothing["max_change_per_step"] = final
    elif param == "smoothing.min_daily_step":
        final = clamp_param(param, numeric)
        engine.smoothing["min_daily_step"] = final
    elif param == "smoothing.cooldown_cycles_after_regime_change":
        final = clamp_param(param, int(numeric))
        engine.smoothing["cooldown_cycles_after_regime_change"] = final
    elif param == "evidence.min_categories_for_regime_change":
        final = clamp_param(param, int(numeric))
        engine.evidence_cfg["min_categories_for_regime_change"] = final
    elif param == "evidence.min_total_score_for_regime_change":
        final = clamp_param(param, numeric)
        engine.evidence_cfg["min_total_score_for_regime_change"] = final
    elif param == "evidence.high_conf_min_categories":
        final = clamp_param(param, int(numeric))
        engine.evidence_cfg["high_conf_min_categories"] = final
    elif param == "evidence.high_conf_min_total":
        final = clamp_param(param, numeric)
        engine.evidence_cfg["high_conf_min_total"] = final
    elif param == "evidence.decay_per_cycle":
        final = clamp_param(param, numeric)
        if evidence is None:
            return False, None, "证据累加器未注入, 跳过"
        engine.evidence_cfg["decay_per_cycle"] = final
        if knowledge is not None:
            knowledge.drift_cfg["decay_per_cycle"] = final
        evidence.set_decay(final)
    elif param == "confidence_gate.low_confidence_max_alpha_abs":
        final = clamp_param(param, numeric)
        engine.conf_gate["low_confidence_max_alpha_abs"] = final
    elif param.startswith("regime_expected_days."):
        key = param.split(".", 1)[1]
        final = clamp_param(param, int(numeric))
        if hasattr(engine, "expected_days"):
            engine.expected_days[key] = final
        else:  # 兼容无 expected_days 的旧/测试引擎
            REGIME_EXPECTED_DAYS[key] = final
    elif param.startswith("regime_alpha_map."):
        key = param.split(".", 1)[1]
        final = clamp_param(param, numeric)
        engine.alpha_map[key] = final
    else:
        return False, None, f"未知参数: {param}"
    return True, final, ""


def load_overlay(data_dir: str) -> dict:
    """读取校准覆盖 params.json; 不存在返回 {}; 非法文件告警并忽略 (容错)."""
    path = os.path.join(data_dir, PARAMS_FILE)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        print(f"[Params] 校准覆盖文件损坏, 已忽略: {exc}")
        return {}
    if not isinstance(data, dict):
        print("[Params] 校准覆盖文件结构非法 (非 JSON 对象), 已忽略")
        return {}
    return data


def save_overlay(data_dir: str, params: dict) -> str:
    """把 {参数路径: 新值} 合并进 params.json (tmp+原子替换), 返回文件路径."""
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, PARAMS_FILE)
    merged = load_overlay(data_dir)
    merged.update(params or {})
    atomic_write_json(path, merged)
    return path


def append_calibration_log(data_dir: str, entries: list,
                           source: str = "monthly_review"):
    """append 校准审计 (时间/参数/旧→新/来源); 无条目返回 None."""
    entries = [e for e in (entries or []) if isinstance(e, dict)]
    if not entries:
        return None
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, LOG_FILE)
    ts = datetime.utcnow().isoformat() + "Z"
    with open(path, "a", encoding="utf-8") as f:
        for entry in entries:
            record = {"ts": ts, "source": source}
            record.update(entry)
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


def apply_overlay(engine, overlay: dict, evidence=None, knowledge=None) -> list:
    """把 params.json overlay 应用到引擎 (启动时恢复校准结果), 返回生效描述列表.

    单参数异常/未知参数只告警跳过, 不影响其它参数与主流程。
    """
    applied = []
    if not isinstance(overlay, dict):
        return applied
    for param, value in overlay.items():
        if not isinstance(param, str) or param == "none":
            continue
        try:
            ok, final, error = apply_param(engine, param, value, evidence, knowledge)
        except (TypeError, ValueError, ArithmeticError) as exc:
            print(f"[Params] 覆盖参数 {param} 应用失败 (已跳过): {exc}")
            continue
        if not ok:
            print(f"[Params] 覆盖参数 {param} 跳过: {error}")
            continue
        applied.append(f"{param}={final}")
    return applied
