"""0–1 市场估值 — AI 直接输出的市场贵贱判断 (替代原 regime 状态机/仓位引擎).

估值口径 (用户定义, 唯一权威):
    0.00–0.20  极度低估
    0.20–0.50  轻度低估
    0.50–0.80  轻度高估
    0.80–1.00  极度高估

区间为左闭右开 (边界值归上一档): 0.20 → 轻度低估, 0.50 → 轻度高估, 0.80 → 极度高估.

语义边界:
- 这是"当前市场状态相对其内在价值的贵贱判断", 不是仓位、不是下单方向、不是周期位置;
- 每轮由 AI 在分析 JSON 里直接给出 `valuation`, 本地只做范围校验 + 档位映射 +
  变更留痕, **不做平滑、不做冷却、不做连续确认、不做状态机推进**;
- 估值分本身不做时间外推: 无新信息(无推文)时保持上一次 AI 判断, 不按自然日漂移。
"""

VALUATION_MIN = 0.0
VALUATION_MAX = 1.0

# (档位名, 下界含, 上界不含) — 顺序即从严到宽
BAND_TABLE = (
    ("极度低估", 0.0, 0.2),
    ("轻度低估", 0.2, 0.5),
    ("轻度高估", 0.5, 0.8),
    ("极度高估", 0.8, 1.0),
)
BAND_LABELS = tuple(item[0] for item in BAND_TABLE)
UNASSESSED_LABEL = "未评估"


def clampi(value):
    """把任意输入收敛为 [0, 1] 浮点; 非数值/NaN/inf → None (不做静默兜底)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return max(VALUATION_MIN, min(VALUATION_MAX, number))


def is_valid(value) -> bool:
    """是否为合法估值 (数值且在 [0, 1] 内, 布尔不算)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return number == number and VALUATION_MIN <= number <= VALUATION_MAX


def band_of(value) -> str:
    """估值 → 档位名; 非法值返回 UNASSESSED_LABEL (不猜)."""
    if not is_valid(value):
        return UNASSESSED_LABEL
    number = float(value)
    for label, _, upper in BAND_TABLE[:-1]:
        if number < upper:
            return label
    return BAND_TABLE[-1][0]


def describe(value, digits: int = 2) -> str:
    """渲染「0.62 · 轻度高估」; 非法值 → 「未评估」."""
    if not is_valid(value):
        return UNASSESSED_LABEL
    return f"{float(value):.{digits}f} · {band_of(value)}"


def parse(raw_value, confidence=None, evidence=None) -> dict | None:
    """归一 AI 输出的估值三件套; 估值非法返回 None (调用方按分析失败处理)."""
    if not is_valid(raw_value):
        return None
    value = round(float(raw_value), 4)
    conf = str(confidence or "").strip().lower()
    if conf not in ("high", "medium", "low"):
        conf = "low"
    text = str(evidence or "").strip()[:300]
    return {
        "value": value,
        "label": band_of(value),
        "confidence": conf,
        "evidence": text,
    }


def change_text(previous, current) -> str:
    """估值变更摘要 (供日志/通知); 无变化或非法返回空串."""
    if not (is_valid(previous) and is_valid(current)):
        return ""
    if float(previous) == float(current):
        return ""
    return f"{describe(previous)} → {describe(current)}"


def is_cross_band(previous, current) -> bool:
    """是否跨档 (换档才需要对外提示, 档内微调不打扰)."""
    if not (is_valid(previous) and is_valid(current)):
        return False
    return band_of(previous) != band_of(current)
