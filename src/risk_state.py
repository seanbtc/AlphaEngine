"""Risk Budget v2 — 独立于 AI 的风险姿态/仓位上限 (Phase 2, 默认 dry_run)。

设计:
- posture ∈ offensive / neutral / defensive / flat;
- 硬风控只降 cap: 回撤/破位/top_risk 触发后, 同一风险事件内 cap 不回升;
- 解除需要连续 release_confirm_evals 轮结构修复 (close>SMA50 且回撤<release_drawdown_pct);
- Phase 2 只观察 (mode=dry_run): 计算并写入 state.risk_v2 + manifest,
  不改变 alpha; enforce 待 dry-run 回放验证后另行开启。
"""
from datetime import datetime, timezone

DEFAULT_EXPOSURE_CAP = {
    "offensive": 0.80,
    "neutral": 0.50,
    "defensive": 0.25,
    "flat": 0.00,
}
DEFAULT_FAST_CHANNEL = {
    "drawdown_defensive_pct": 12.0,
    "drawdown_flat_pct": 20.0,
}
DEFAULT_RELEASE = {
    "confirm_evals": 3,
    "release_drawdown_pct": 8.0,
    "require_above_sma50": True,
}


def _now_iso():
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def extract_inputs(market_state: dict, price_history=None) -> dict:
    """提取风险判定输入; 缺失字段为 None (fail-open, 不误降风险)。"""
    state = market_state if isinstance(market_state, dict) else {}
    snapshot = (state.get("ma_context") or {}).get("snapshot") or {}
    mas = snapshot.get("mas") if isinstance(snapshot.get("mas"), dict) else {}
    close = _as_float(state.get("price"))
    if close is None:
        close = _as_float(snapshot.get("price"))
    sma50 = _as_float((mas.get("sma50") or {}).get("value"))
    sma200 = _as_float((mas.get("sma200") or {}).get("value"))
    slope200 = (mas.get("sma200") or {}).get("slope")
    top_risk = (state.get("cycle_context") or {}).get("top_risk")
    top_risk = top_risk if isinstance(top_risk, dict) else {}
    drawdown = _as_float(top_risk.get("drawdown_from_90d_high_pct"))
    if drawdown is None and close is not None and price_history:
        window = [price for _, price in price_history[-90:]]
        if window:
            peak = max(window)
            if peak:
                drawdown = (close - peak) / peak * 100.0
    return {
        "close": close,
        "sma50": sma50,
        "sma200": sma200,
        "sma200_slope": slope200,
        "drawdown_pct": drawdown,
        "top_risk_active": bool(top_risk.get("active")),
        "top_risk_since": top_risk.get("since"),
    }


def _has_evidence(inputs: dict) -> bool:
    """是否存在可用于风险判定的实际证据 (缺证据时 fail-open 保持旧 cap)。"""
    return any((
        inputs.get("drawdown_pct") is not None,
        inputs.get("top_risk_active"),
        inputs.get("close") is not None and inputs.get("sma200") is not None,
    ))


def classify_posture(inputs: dict, fast_cfg: dict = None) -> tuple[str, list]:
    fast_cfg = {**DEFAULT_FAST_CHANNEL, **(fast_cfg or {})}
    close = inputs.get("close")
    sma200 = inputs.get("sma200")
    drawdown = inputs.get("drawdown_pct")
    reasons = []
    if inputs.get("top_risk_active"):
        reasons.append("top_risk.active")
    if drawdown is not None:
        if drawdown <= -abs(fast_cfg["drawdown_flat_pct"]):
            reasons.append(f"回撤 {drawdown:.1f}% <= -{fast_cfg['drawdown_flat_pct']:.0f}%")
            return "flat", reasons
        if drawdown <= -abs(fast_cfg["drawdown_defensive_pct"]):
            reasons.append(f"回撤 {drawdown:.1f}% <= -{fast_cfg['drawdown_defensive_pct']:.0f}%")
    if close is not None and sma200 is not None and close < sma200:
        reasons.append(f"close {close:.0f} < SMA200 {sma200:.0f}")
    if reasons:
        return "defensive", reasons
    if (close is not None and sma200 is not None and close > sma200
            and inputs.get("sma200_slope") == "up"):
        reasons.append("close>SMA200 且 200SMA 斜率↑")
        return "offensive", reasons
    reasons.append("结构中性/数据不足")
    return "neutral", reasons


def evaluate_risk(market_state: dict, previous: dict = None, cfg: dict = None,
                  price_history=None) -> dict:
    """计算本轮 risk state (纯函数, 不写盘)。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    previous = previous if isinstance(previous, dict) else {}
    mode = str(cfg.get("mode") or "dry_run").strip().lower()
    inputs = extract_inputs(market_state, price_history)
    posture, reasons = classify_posture(inputs, cfg.get("fast_channel"))
    cap_map = {**DEFAULT_EXPOSURE_CAP, **(cfg.get("exposure_cap") or {})}
    mapped_cap = _as_float(cap_map.get(posture))
    release_cfg = {**DEFAULT_RELEASE, **(cfg.get("release") or {})}

    data_ok = inputs.get("close") is not None and inputs.get("sma200") is not None
    prev_cap = _as_float(previous.get("cap"))
    release_counter = int(previous.get("release_counter") or 0)
    ratchet = False
    cap = mapped_cap
    release_ok = False
    if not _has_evidence(inputs):
        # 数据缺失 fail-open: 不因"没数据"改变仓位上限 (保护不被静默撤销)
        if prev_cap is not None:
            cap = prev_cap
            ratchet = True
            reasons.append(f"数据不足, fail-open 保持旧 cap {prev_cap:.2f}")
        else:
            cap = None
            reasons.append("数据不足, 无旧 cap 可用 (不施加限制)")
    elif mapped_cap is not None and prev_cap is not None:
        if mapped_cap >= prev_cap:
            # 结构改善: 默认保持旧 cap (棘轮), 满足释放条件才允许上调
            release_ok = _release_condition(inputs, release_cfg)
            release_counter = release_counter + 1 if release_ok else 0
            if release_ok and release_counter >= int(release_cfg["confirm_evals"]):
                cap = mapped_cap
                reasons.append(f"风险释放确认 {release_counter}/{release_cfg['confirm_evals']}")
            else:
                cap = prev_cap
                ratchet = True
                reasons.append(f"棘轮保持 cap {prev_cap:.2f}")
        else:
            cap = mapped_cap
            release_counter = 0
            reasons.append(f"cap 下调 {prev_cap:.2f} → {mapped_cap:.2f}")
    elif mapped_cap is not None:
        release_counter = 0

    since = previous.get("since")
    if not since or previous.get("posture") != posture:
        since = _now_iso()
    state = {
        "mode": mode,
        "evaluated_at": _now_iso(),
        "inputs": inputs,
        "posture": posture,
        "cap": cap,
        "cap_mapped": mapped_cap,
        "ratchet": ratchet,
        "release_ok": release_ok,
        "release_counter": release_counter,
        "posture_since": since,
        "reasons": reasons,
        "data_ok": data_ok,
        "applied": False,
    }
    return state


def _release_condition(inputs: dict, cfg: dict) -> bool:
    close = inputs.get("close")
    sma50 = inputs.get("sma50")
    drawdown = inputs.get("drawdown_pct")
    if close is None:
        return False
    if cfg.get("require_above_sma50", True):
        if sma50 is None or close <= sma50:
            return False
    if inputs.get("top_risk_active"):
        return False
    if drawdown is None:
        return False
    return drawdown > -abs(cfg["release_drawdown_pct"])


def format_risk(risk_state: dict) -> str:
    if not isinstance(risk_state, dict):
        return ""
    inputs = risk_state.get("inputs") or {}
    drawdown = inputs.get("drawdown_pct")
    drawdown_text = f"{drawdown:+.1f}%" if isinstance(drawdown, (int, float)) else "?"
    cap = risk_state.get("cap")
    cap_text = f"{cap:.2f}" if isinstance(cap, (int, float)) else "?"
    return (f"posture={risk_state.get('posture')} cap={cap_text} "
            f"dd={drawdown_text} mode={risk_state.get('mode')} "
            f"| {'; '.join(risk_state.get('reasons') or [])}")
