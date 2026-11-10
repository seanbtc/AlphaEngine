"""可证伪命题 (Thesis) — AI 判断的后果载体 (Phase 1, 只记录/评估)。

设计约束:
- 2–5 条条件, 至少 1 条价格/均线类 (每日可验证);
- 指标只开放 Phase 1 可稳定获取的价格/均线/周期结构字段;
- 失效阈值: unmet >= max(2, ceil(n/2)); 数据缺失 (unknown) 不计入 unmet,
  避免数据源故障把命题误判为失效;
- 生命周期: active → weakened → invalidated / expired / superseded.
- label 为提出命题时所处的估值档位 (极度低估/轻度低估/轻度高估/极度高估),
  仅作追溯, 不参与任何执行逻辑。
"""
import json
import math
import os
from datetime import datetime, timedelta, timezone

from src.valuation import BAND_LABELS

THESIS_FILE = "theses.jsonl"

REGIMES = set(BAND_LABELS)

NUMERIC_METRICS = {
    "close", "sma50", "sma100", "sma200", "sma250",
    "dist_sma200_pct", "drawdown_90d_pct",
    "price_change_7d", "price_change_30d",
}
STRING_METRICS = {"sma200_slope", "sma250_slope"}
ALLOWED_METRICS = NUMERIC_METRICS | STRING_METRICS
# Phase 1 至少一条"每日可验证"的价格/均线类条件
PRICE_METRICS = {"close", "sma50", "sma100", "sma200", "sma250",
                 "dist_sma200_pct", "drawdown_90d_pct"}
NUMERIC_OPS = {">=", "<=", ">", "<"}
STRING_OPS = {"==", "!="}
SLOPE_VALUES = {"up", "down", "flat", "na"}
MAX_CONDITIONS = 5
MIN_CONDITIONS = 2
MAX_HORIZON_DAYS = 90
DEFAULT_HORIZON_DAYS = 30
TERMINAL_STATUSES = {"invalidated", "expired", "superseded"}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt=None):
    return (dt or _now()).isoformat() + "Z"


def _parse_deadline(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None).date()


def extract_metrics(market_state: dict) -> dict:
    """从 market_state 提取命题可引用的指标 (缺失即不放入, 评估时记 unknown)。"""
    state = market_state if isinstance(market_state, dict) else {}
    snapshot = (state.get("ma_context") or {}).get("snapshot") or {}
    mas = snapshot.get("mas") if isinstance(snapshot.get("mas"), dict) else {}
    metrics = {}
    price = state.get("price")
    if price is None:
        price = snapshot.get("price")
    try:
        if price is not None:
            metrics["close"] = float(price)
    except (TypeError, ValueError):
        pass
    for key in ("sma50", "sma100", "sma200", "sma250"):
        item = mas.get(key) if isinstance(mas.get(key), dict) else {}
        try:
            if item.get("value") is not None:
                metrics[key] = float(item["value"])
            if key in ("sma200", "sma250") and item.get("slope") in SLOPE_VALUES:
                metrics[f"{key}_slope"] = item["slope"]
        except (TypeError, ValueError):
            continue
    item200 = mas.get("sma200") if isinstance(mas.get("sma200"), dict) else {}
    if item200.get("dist_pct") is not None:
        try:
            metrics["dist_sma200_pct"] = float(item200["dist_pct"])
        except (TypeError, ValueError):
            pass
    top_risk = (state.get("cycle_context") or {}).get("top_risk") or {}
    if top_risk.get("drawdown_from_90d_high_pct") is not None:
        try:
            metrics["drawdown_90d_pct"] = float(top_risk["drawdown_from_90d_high_pct"])
        except (TypeError, ValueError):
            pass
    for key in ("price_change_7d", "price_change_30d"):
        if state.get(key) is not None:
            try:
                metrics[key] = float(state[key])
            except (TypeError, ValueError):
                pass
    return metrics


def _normalize_condition(item: dict, index: int, now: datetime):
    if not isinstance(item, dict):
        return None
    metric = str(item.get("metric") or "").strip().lower()
    if metric not in ALLOWED_METRICS:
        return None
    op = str(item.get("op") or "").strip()
    value = item.get("value")
    if metric in NUMERIC_METRICS:
        if op not in NUMERIC_OPS:
            return None
        try:
            value = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(value):
            return None
    else:
        if op not in STRING_OPS:
            return None
        value = str(value or "").strip().lower()
        if value not in SLOPE_VALUES:
            return None
    deadline = _parse_deadline(item.get("deadline"))
    if deadline is None:
        deadline = (now + timedelta(days=DEFAULT_HORIZON_DAYS)).date()
    max_deadline = (now + timedelta(days=MAX_HORIZON_DAYS)).date()
    if deadline > max_deadline:
        deadline = max_deadline
    cond = {
        "id": str(item.get("id") or f"c{index + 1}").strip()[:16] or f"c{index + 1}",
        "metric": metric,
        "op": op,
        "value": value,
        "deadline": deadline.isoformat(),
    }
    source = str(item.get("source") or "").strip()[:40]
    if source:
        cond["source"] = source
    return cond


def thesis_signature(thesis: dict):
    """条件指纹 (不含 deadline/claim): 相同指纹视为同一命题的延续。"""
    if not isinstance(thesis, dict):
        return None
    conditions = thesis.get("conditions") or []
    if not isinstance(conditions, list):
        return None
    return tuple(sorted(
        (str(c.get("metric")), str(c.get("op")), str(c.get("value")))
        for c in conditions if isinstance(c, dict)
    ))


def _thesis_id(now: datetime, conditions: list) -> str:
    import hashlib
    raw = json.dumps(conditions, sort_keys=True, ensure_ascii=False)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:4]
    return f"T-{now.strftime('%Y%m%d-%H%M%S')}-{digest}"


def normalize_thesis(raw, now: datetime = None) -> dict | None:
    """校验/归一 AI 输出的 thesis; 非法返回 None (不阻断主分析)."""
    now = now or _now()
    if not isinstance(raw, dict):
        return None
    claim = str(raw.get("claim") or "").strip()[:300]
    if not claim:
        return None
    items = raw.get("conditions")
    if not isinstance(items, list):
        return None
    conditions = []
    seen_ids = set()
    for index, item in enumerate(items[:MAX_CONDITIONS]):
        cond = _normalize_condition(item, index, now)
        if cond is None:
            continue
        base = cond["id"]
        suffix = 1
        while cond["id"] in seen_ids:
            cond["id"] = f"{base[:12]}_{suffix}"
            suffix += 1
        seen_ids.add(cond["id"])
        conditions.append(cond)
    if not (MIN_CONDITIONS <= len(conditions) <= MAX_CONDITIONS):
        return None
    if not any(cond["metric"] in PRICE_METRICS for cond in conditions):
        return None
    label = str(raw.get("label") or "").strip()
    if label not in BAND_LABELS:
        label = ""
    confidence = raw.get("confidence")
    try:
        confidence = float(confidence)
        confidence = max(0.0, min(1.0, confidence))
    except (TypeError, ValueError):
        confidence = None
    thesis = {
        "thesis_id": _thesis_id(now, conditions),
        "created_at": _iso(now),
        "updated_at": _iso(now),
        "label": label,
        "claim": claim,
        "conditions": conditions,
        "status": "active",
        "conditions_met": 0,
        "unmet_count": 0,
        "unknown_count": 0,
    }
    if confidence is not None:
        thesis["confidence"] = round(confidence, 3)
    supersedes = str(raw.get("supersedes") or "").strip()
    if supersedes:
        thesis["supersedes"] = supersedes
    return thesis


def adopt_thesis(raw, current: dict = None, now: datetime = None):
    """把 AI 输出并入当前命题。

    返回 (thesis, event, previous):
    - event: created / updated / rejected
    - previous: 被取代的旧命题 (需由调用方落 superseded 事件), 否则 None
    """
    now = now or _now()
    thesis = normalize_thesis(raw, now=now)
    if thesis is None:
        return current, "rejected", None
    current = current if isinstance(current, dict) else None
    if current and current.get("status") not in TERMINAL_STATUSES:
        if thesis_signature(current) == thesis_signature(thesis):
            merged = dict(current)
            merged["conditions"] = thesis["conditions"]
            merged["claim"] = thesis["claim"] or current.get("claim", "")
            merged["label"] = thesis["label"] or current.get("label", "")
            if thesis.get("confidence") is not None:
                merged["confidence"] = thesis["confidence"]
            merged["updated_at"] = _iso(now)
            merged["last_seen_at"] = _iso(now)
            return merged, "updated", None
        previous = dict(current)
        previous["status"] = "superseded"
        previous["superseded_at"] = _iso(now)
        thesis["supersedes"] = current.get("thesis_id", "")
        return thesis, "created", previous
    return thesis, "created", None


def _compare(actual, op: str, expected):
    if op == ">=":
        return actual >= expected
    if op == "<=":
        return actual <= expected
    if op == ">":
        return actual > expected
    if op == "<":
        return actual < expected
    if op == "==":
        return actual == expected
    if op == "!=":
        return actual != expected
    return None


def _invalidation_threshold(total: int) -> int:
    return max(2, int(math.ceil(total / 2.0)))


def evaluate_thesis(thesis: dict, market_state: dict, now: datetime = None) -> dict:
    """按当前市场状态评估命题条件; 返回更新后的副本 (不写盘)。"""
    now = now or _now()
    if not isinstance(thesis, dict):
        return thesis
    out = dict(thesis)
    if out.get("status") in TERMINAL_STATUSES:
        return out
    conditions = out.get("conditions")
    conditions = conditions if isinstance(conditions, list) else []
    metrics = extract_metrics(market_state)
    evaluations = []
    met = unmet = unknown = 0
    deadlines = []
    for cond in conditions:
        if not isinstance(cond, dict):
            continue
        metric = cond.get("metric")
        actual = metrics.get(metric)
        deadline = _parse_deadline(cond.get("deadline"))
        if deadline:
            deadlines.append(deadline)
        if actual is None:
            status = "unknown"
            reason = f"{metric} 数据缺失"
            unknown += 1
        else:
            passed = _compare(actual, cond.get("op"), cond.get("value"))
            if passed is None:
                status = "unknown"
                reason = f"无法比较 {metric}"
                unknown += 1
            elif passed:
                status = "met"
                reason = ""
                met += 1
            else:
                status = "unmet"
                actual_text = (f"{actual:.4g}" if isinstance(actual, (int, float))
                               else str(actual))
                reason = (f"{metric}={actual_text} {cond.get('op')} "
                          f"{cond.get('value')}")
                if deadline and now.date() > deadline:
                    reason += " (已过 deadline)"
                unmet += 1
        evaluations.append({
            "id": cond.get("id"),
            "metric": metric,
            "op": cond.get("op"),
            "value": cond.get("value"),
            "actual": actual,
            "status": status,
            "reason": reason,
            "deadline": cond.get("deadline"),
        })
    total = len(conditions)
    out["conditions_met"] = met
    out["unmet_count"] = unmet
    out["unknown_count"] = unknown
    out["evaluations"] = evaluations
    out["last_evaluated_at"] = _iso(now)
    if total:
        threshold = _invalidation_threshold(total)
        if unmet >= threshold:
            out["status"] = "invalidated"
            out["invalidated_at"] = _iso(now)
            out["invalidated_reason"] = f"{unmet}/{total} 条条件未满足"
        elif deadlines and now.date() > max(deadlines):
            out["status"] = "expired"
            out["expired_at"] = _iso(now)
        elif unmet > 0:
            out["status"] = "weakened"
        elif unknown > 0 and out.get("status") == "weakened":
            # 数据缺失不能把"已转弱"洗白成 active (Phase 1 观测信号必须诚实)
            out["status"] = "weakened"
        else:
            out["status"] = "active"
    return out


def format_thesis_for_prompt(thesis: dict, max_chars: int = 700) -> str:
    """渲染给 AI 的当前命题段落 (≤max_chars)。"""
    if not isinstance(thesis, dict) or not thesis.get("thesis_id"):
        return ""
    lines = [f"## 当前命题 ({thesis.get('thesis_id')})",
             f"- 状态: {thesis.get('status', '?')} | 宣称: {thesis.get('claim', '')}"]
    evaluations = thesis.get("evaluations")
    evaluations = evaluations if isinstance(evaluations, list) else []
    conditions = thesis.get("conditions")
    conditions = conditions if isinstance(conditions, list) else []
    if evaluations:
        for item in evaluations[:5]:
            if not isinstance(item, dict):
                continue
            actual = item.get("actual")
            if isinstance(actual, (int, float)):
                actual_text = f"{actual:.4g}"
            elif actual is None:
                actual_text = "缺失"
            else:
                actual_text = str(actual)
            lines.append(
                f"- {item.get('id')} {item.get('metric')} {item.get('op')} "
                f"{item.get('value')} (实际 {actual_text}) → {item.get('status')}"
                + (f" | {item.get('reason')}" if item.get("status") != "met" else "")
            )
    elif conditions:
        for cond in conditions[:5]:
            if not isinstance(cond, dict):
                continue
            lines.append(f"- {cond.get('id')} {cond.get('metric')} {cond.get('op')} "
                         f"{cond.get('value')} (deadline {cond.get('deadline')})")
    text = "\n".join(lines) + "\n"
    return text[:max_chars]


def load_current(state_manager) -> dict | None:
    try:
        current = state_manager.get("thesis.current")
    except Exception:
        return None
    return current if isinstance(current, dict) and current.get("thesis_id") else None


def save_current(state_manager, thesis: dict) -> None:
    state_manager.set("thesis.current", thesis)


def append_event(data_dir: str, event: str, thesis: dict) -> None:
    """追加命题生命周期事件 (仅 append, 失败不影响主流程)。"""
    if not isinstance(thesis, dict) or not thesis.get("thesis_id"):
        return
    os.makedirs(data_dir, exist_ok=True)
    row = {
        "ts": _iso(),
        "event": event,
        "thesis_id": thesis.get("thesis_id"),
        "status": thesis.get("status"),
        "label": thesis.get("label"),
        "claim": thesis.get("claim"),
        "conditions_met": thesis.get("conditions_met"),
        "unmet_count": thesis.get("unmet_count"),
        "unknown_count": thesis.get("unknown_count"),
        "thesis": thesis,
    }
    with open(os.path.join(data_dir, THESIS_FILE), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
