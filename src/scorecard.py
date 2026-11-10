"""成绩单 — 把"你错了"回流到下一轮 AI prompt (Phase 1).

AI 每轮只看到 metrics/估值叙事, 看不到自己预测的实际结果, 因此不会修正。
本模块从 prediction_log + prediction_outcomes + 估值历史 生成 ≤800 字符的
成绩单, 由 alpha.py 注入 market_state, analyzer 渲染进 prompt。

只读, 不写任何文件, 不影响任何执行链路。
"""
from datetime import datetime, timedelta, timezone

from src.knowledge import _prediction_key, normalize_point_verdict
from src.valuation import is_valid

_VERDICT_LABEL = {
    "correct": "方向对",
    "wrong_direction": "方向错",
    "flat": "震荡",
}


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _read_outcomes(knowledge) -> list[dict]:
    rows = knowledge._read_jsonl(knowledge.prediction_outcome_file)
    deduped = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        key = str(row.get("prediction_id") or f"__unknown__{index}")
        deduped[key] = row
    return list(deduped.values())


def _prediction_map(knowledge) -> dict:
    predictions = knowledge._read_jsonl(knowledge.prediction_log_file)
    return {_prediction_key(item): item for item in predictions
            if isinstance(item, dict)}


def _drawdown_90d(knowledge, current_price):
    history = knowledge._price_history()
    if not history:
        return None
    cutoff = _now() - timedelta(days=90)
    window = [price for ts, price in history if ts >= cutoff]
    if not window:
        window = [price for _, price in history[-90:]]
    if not window:
        return None
    price = current_price if current_price is not None else window[-1]
    peak = max(window)
    if not peak:
        return None
    return (float(price) - peak) / peak * 100.0


def _valuation_trajectory(memory, valuation_now, btc_price):
    """返回 (days, valuation_then, valuation_now, btc_change_pct) 或 None。"""
    if memory is None or not is_valid(valuation_now):
        return None
    history = memory.load_valuation_history()
    if not history:
        return None
    target = _now() - timedelta(days=30)
    candidate = None
    best_delta = None
    for item in history:
        if not isinstance(item, dict):
            continue
        try:
            ts = datetime.fromisoformat(str(item.get("date", "")).replace("Z", "+00:00"))
            ts = ts.replace(tzinfo=None)
            value = float(item.get("valuation"))
        except (TypeError, ValueError):
            continue
        if not is_valid(value):
            continue
        delta = abs((ts - target).total_seconds())
        if best_delta is None or delta < best_delta:
            best_delta = delta
            candidate = (ts, value, item.get("btc_price"))
    if candidate is None or best_delta is None or best_delta > 10 * 86400:
        return None
    ts, value_then, price_then = candidate
    change = None
    if price_then and btc_price:
        try:
            change = (float(btc_price) - float(price_then)) / float(price_then) * 100.0
        except (TypeError, ValueError, ZeroDivisionError):
            change = None
    return (max(1, (_now() - ts).days), value_then, float(valuation_now), change)


def build_scorecard(knowledge, memory=None, valuation_now=None, btc_price=None,
                    limit: int = 8, max_chars: int = 800) -> str:
    """构建成绩单文本; 无可用数据返回空串。"""
    if knowledge is None:
        return ""
    outcomes = _read_outcomes(knowledge)
    predictions = _prediction_map(knowledge)
    decided_rows = []
    counts = {"correct": 0, "wrong_direction": 0, "flat": 0}
    for outcome in outcomes:
        pid = str(outcome.get("prediction_id") or "")
        pred = predictions.get(pid, {})
        verdict = normalize_point_verdict(outcome.get("verdict"))
        if verdict not in counts:
            continue
        counts[verdict] += 1
        decided_rows.append({
            "ts": str(pred.get("ts") or outcome.get("ts") or ""),
            "expected": outcome.get("expected") or pred.get("expected"),
            "move_pct": outcome.get("move_pct"),
            "verdict": verdict,
        })
    decided_rows.sort(key=lambda row: row["ts"], reverse=True)
    recent = decided_rows[:limit]
    decided = counts["correct"] + counts["wrong_direction"]
    hit_rate = (counts["correct"] / decided * 100.0) if decided else None
    streak = 0
    for row in decided_rows:
        if row["verdict"] == "wrong_direction":
            streak += 1
        else:
            break

    lines = []
    if recent:
        lines.append(f"## 你的近期成绩 (最近 {len(recent)} 条已审计)")
        for row in recent:
            date_text = row["ts"][5:10] if len(row["ts"]) >= 10 else "?"
            direction = row["expected"] or "?"
            move = row["move_pct"]
            move_text = f"{float(move):+.1f}%" if isinstance(move, (int, float)) else "?"
            lines.append(f"- {date_text} 预期{direction} 实际{move_text} → "
                         f"{_VERDICT_LABEL.get(row['verdict'], row['verdict'])}")
        summary = []
        if hit_rate is not None:
            summary.append(f"命中率 {hit_rate:.1f}% ({counts['correct']}/{decided})")
        if streak >= 2:
            summary.append(f"连续方向错 {streak} 条")
        drawdown = _drawdown_90d(knowledge, btc_price)
        if drawdown is not None:
            summary.append(f"当前回撤 {drawdown:+.1f}%")
        if summary:
            lines.append(" | ".join(summary))
        trajectory = _valuation_trajectory(memory, valuation_now, btc_price)
        if trajectory:
            days, value_then, value_now, btc_change = trajectory
            text = f"{days}天前 估值={value_then:.2f} → 现在 {value_now:.2f}"
            if btc_change is not None:
                text += f" | 同期 BTC {btc_change:+.1f}%"
            lines.append(text)

    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n...(成绩单已截断)"
    return text + "\n"
