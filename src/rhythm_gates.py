"""WP8 节奏门控 (rhythm gates) — 结构定性门控, 默认全关.

防过拟合原则:
- 不改节奏表数值 (REGIME_EXPECTED_DAYS / smoothing 步长边界), 只修定性错误;
- 三门控全部默认关闭 (config.alpha.rhythm.*.enabled=false), 可开关、可回滚;
- 纯函数、无 IO (取数由调用方经 market_state 注入); 数据缺失/异常 →
  fail-open (不施加门控 = baseline 行为) 并返回 fail-open reason 供调用方记日志;
- 失败隔离: 任何异常不向外抛, 调用方按恒等 (原 progress) 处理。

三门控 (regime 互斥, 同一轮至多一门生效):
- G1 熊侧门控 (bear_reclaim_gate): regime ∈ {BEAR, BEAR_DEEP} 时, 若"连续
  reclaim_confirm_days 日收盘 > 长均线 (默认 SMA200)"(结构修复)未确认 →
  progress 上限 progress_cap; 确认后放行 (允许推进至熊底)。
  数据: ma_context.snapshot.days_above_long_streak。
- G2 复苏完成门控 (recovery_completion_gate): regime == RECOVERY 且 SMA250
  斜率↑ → progress=1.0 (结构完成即完成)。
- G3 牛市门控 (bull_top_gate): regime == BULL 且 progress > progress_threshold
  时, 需释放条件 (top_risk.active 或 SMA250 斜率↓) 才允许继续推进, 否则压回
  threshold; 释放条件数据不完整 → fail-open。

输入 ma_ctx: market_state (含 ma_context/cycle_context) 或裸 ma_context;
斜率口径与 ma_context 一致 (slope: up/flat/down, 与 slope_lookback 日前比较),
"转↑/转↓"按当前斜率方向判定 (非方向翻转事件)。
"""

_DEFAULT_G1 = {"enabled": False, "progress_cap": 0.5, "reclaim_confirm_days": 10}
_DEFAULT_G2 = {"enabled": False}
_DEFAULT_G3 = {"enabled": False, "progress_threshold": 0.7}

_VALID_SLOPES = ("up", "down", "flat")
_BEAR_REGIMES = ("BEAR", "BEAR_DEEP")


def _as_float(value, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _enabled(gate_cfg) -> bool:
    """门控开关 (缺失/非法/非 dict → 关闭)."""
    return isinstance(gate_cfg, dict) and bool(gate_cfg.get("enabled", False))


def _gate_cfg(cfg, key, defaults):
    merged = dict(defaults)
    gate = cfg.get(key) if isinstance(cfg, dict) else None
    if isinstance(gate, dict):
        merged.update(gate)
    return merged


def _sources(ma_ctx):
    """归一化输入 → (ma_context, cycle_context); 裸 ma_context 亦可."""
    if not isinstance(ma_ctx, dict):
        return {}, {}
    ma = ma_ctx.get("ma_context")
    ma = ma if isinstance(ma, dict) else ma_ctx
    cycle = ma_ctx.get("cycle_context")
    cycle = cycle if isinstance(cycle, dict) else {}
    return ma, cycle


def _snapshot(ma):
    snapshot = ma.get("snapshot") if isinstance(ma, dict) else None
    return snapshot if isinstance(snapshot, dict) else {}


def _reclaim_streak(ma):
    """连续站上长均线天数 (snapshot.days_above_long_streak); 缺失/非法 → None."""
    value = _snapshot(ma).get("days_above_long_streak")
    if value is None:
        return None
    try:
        streak = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, streak)


def _slope250(ma, cycle):
    """SMA250 斜率方向: 优先 ma_context.snapshot.mas, 回退 cycle_context.trend.

    口径差异 (回退时注意): ma_context 的 slope 由 `_slope_label` 计算, 带
    ±slope_flat_pct 走平带 (up/flat/down 三态); cycle_context.trend.slope250 由
    `_slope_dir` 计算, 无 flat 档 (MA[t] > MA[t-lookback] 记 up, 否则 down)。
    因此回退源的 flat 实际按 down 表达 —— G2 仅 "up" 视为结构完成、G3 仅 "down"
    视为释放, 该差异使回退口径偏保守 (走平按 down 处理), 不会把 flat 误判为 up。
    """
    mas = _snapshot(ma).get("mas")
    if isinstance(mas, dict):
        item = mas.get("sma250")
        if isinstance(item, dict) and item.get("slope") in _VALID_SLOPES:
            return item.get("slope")
    trend = cycle.get("trend") if isinstance(cycle, dict) else None
    if isinstance(trend, dict) and trend.get("slope250") in _VALID_SLOPES:
        return trend.get("slope250")
    return None


def _top_risk(cycle):
    """→ (available, active); cycle_context.top_risk 缺失/非法 → (False, False)."""
    top = cycle.get("top_risk") if isinstance(cycle, dict) else None
    if not isinstance(top, dict) or "active" not in top:
        return False, False
    return True, bool(top.get("active"))


def _parse_progress(progress):
    """progress → float; 非法/NaN → None."""
    try:
        value = float(progress)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN
        return None
    return value


def _bear_gate(regime, progress, ma, cfg):
    """G1 熊侧门控: 收复未确认 → progress 上限; 确认 → 放行."""
    gate = _gate_cfg(cfg, "bear_reclaim_gate", _DEFAULT_G1)
    cap = max(0.0, min(1.0, _as_float(gate.get("progress_cap"), 0.5)))
    confirm_days = max(1, _as_int(gate.get("reclaim_confirm_days"), 10))
    value = _parse_progress(progress)
    if value is None:
        return progress, ["G1 fail-open: progress 非数值, 不施加门控"]
    streak = _reclaim_streak(ma)
    if streak is None:
        return progress, ["G1 fail-open: 连续站上长均线天数缺失, 不施加门控"]
    if streak >= confirm_days:
        return value, [f"G1 放行: 连续站上长均线 {streak}/{confirm_days} 日 "
                       f"(结构修复确认, regime={regime})"]
    capped = min(value, cap)
    if capped < value:
        return capped, [f"G1 熊侧门控: 连续站上长均线 {streak}/{confirm_days} 日未确认, "
                        f"progress {value:.2f} → {capped:.2f} (上限 {cap:.2f})"]
    return value, []


def _recovery_gate(regime, progress, ma, cycle, cfg):
    """G2 复苏完成门控: RECOVERY + 250SMA 斜率↑ → progress=1.0."""
    value = _parse_progress(progress)
    if value is None:
        return progress, ["G2 fail-open: progress 非数值, 不施加门控"]
    slope = _slope250(ma, cycle)
    if slope is None:
        return progress, ["G2 fail-open: 250SMA 斜率缺失, 不施加门控"]
    if slope == "up":
        if value >= 1.0:
            return value, []
        return 1.0, [f"G2 复苏完成门控: 250SMA 斜率↑ (结构完成), "
                     f"progress {value:.2f} → 1.00"]
    return value, []


def _bull_gate(regime, progress, ma, cycle, cfg):
    """G3 牛市门控: progress > 阈值 且无释放条件 → 压回阈值."""
    gate = _gate_cfg(cfg, "bull_top_gate", _DEFAULT_G3)
    threshold = max(0.0, min(1.0, _as_float(gate.get("progress_threshold"), 0.7)))
    value = _parse_progress(progress)
    if value is None:
        return progress, ["G3 fail-open: progress 非数值, 不施加门控"]
    if value <= threshold:
        return value, []
    top_available, top_active = _top_risk(cycle)
    slope = _slope250(ma, cycle)
    if top_active:
        return value, [f"G3 放行: top_risk 激活, progress {value:.2f} 允许继续推进"]
    if slope == "down":
        return value, [f"G3 放行: 250SMA 斜率↓, progress {value:.2f} 允许继续推进"]
    if not top_available or slope is None:
        return progress, ["G3 fail-open: 释放条件数据不完整 (top_risk/250SMA 斜率), "
                          "不施加门控"]
    return threshold, [f"G3 牛市门控: progress {value:.2f} > {threshold:.2f} 且无释放条件 "
                       f"(top_risk 未激活/250SMA 未转↓), 压回 {threshold:.2f}"]


def apply_rhythm_gates(regime, progress, ma_ctx, cfg=None):
    """对 progress 应用节奏门控; 返回 (progress_capped, reasons).

    纯函数、无 IO:
    - 门控关闭/regime 不适用 → 恒等返回 (progress 原值, reasons=[]);
    - 数据缺失/异常 → fail-open (不施加门控), reasons 含 fail-open 说明;
    - reasons 为人类可读的门控说明 (压回/放行/完成/fail-open), 供调用方记日志。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    try:
        ma, cycle = _sources(ma_ctx)
        if regime in _BEAR_REGIMES:
            if _enabled(cfg.get("bear_reclaim_gate")):
                return _bear_gate(regime, progress, ma, cfg)
        elif regime == "RECOVERY":
            if _enabled(cfg.get("recovery_completion_gate")):
                return _recovery_gate(regime, progress, ma, cycle, cfg)
        elif regime == "BULL":
            if _enabled(cfg.get("bull_top_gate")):
                return _bull_gate(regime, progress, ma, cycle, cfg)
    except Exception as exc:  # 失败隔离: 任何异常 → fail-open
        return progress, [f"fail-open: 门控异常 ({exc})"]
    return progress, []
