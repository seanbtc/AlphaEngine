"""Risk Budget v2 单测 (Phase 2, dry_run): 姿态/上限/棘轮/释放/缺数据 fail-open.

Phase 2 约束: 只计算并记录, 不改变 alpha。任何数据缺失不得误降风险 (fail-open)。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.risk_state import (classify_posture, evaluate_risk, extract_inputs,
                            format_risk)


def _market(close=80000, sma50=78000, sma200=75000, slope="up",
            drawdown=None, top_risk=False):
    state = {
        "price": close,
        "ma_context": {"snapshot": {
            "price": close,
            "mas": {
                "sma50": {"value": sma50},
                "sma200": {"value": sma200, "slope": slope},
            },
        }},
        "cycle_context": {"top_risk": {"active": top_risk}},
    }
    if drawdown is not None:
        state["cycle_context"]["top_risk"]["drawdown_from_90d_high_pct"] = drawdown
    return state


def test_extract_inputs_reads_price_ma_and_drawdown():
    inputs = extract_inputs(_market(close=81000, drawdown=-9.5))

    assert inputs["close"] == 81000
    assert inputs["sma50"] == 78000
    assert inputs["sma200"] == 75000
    assert inputs["sma200_slope"] == "up"
    assert inputs["drawdown_pct"] == -9.5
    assert inputs["top_risk_active"] is False


def test_extract_inputs_falls_back_to_price_history_for_drawdown():
    history = [(None, 100000.0), (None, 90000.0)]
    inputs = extract_inputs(_market(close=80000, drawdown=None), history)

    assert inputs["drawdown_pct"] == -20.0


def test_drawdown_fallback_uses_90_days_not_last_90_records():
    """3 次/周时最近 90 条≈210 天; 峰值在 90 天外不得算入回撤。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    history = []
    for index in range(200):
        ts = now - timedelta(days=(199 - index) * 2)  # 每 2 天一条 = 400 天
        price = 100000.0 if index <= 150 else 50000.0  # 峰值在 ~98 天前
        history.append((ts, price))

    inputs = extract_inputs(_market(close=50000, drawdown=None), history)

    # 90 天窗口内全为 50k → 回撤 0; 若误用最近 90 条会得到 -50%
    assert inputs["drawdown_pct"] == 0.0


def test_classify_posture_offensive_when_above_rising_sma200():
    posture, reasons = classify_posture(extract_inputs(_market()))

    assert posture == "offensive"
    assert any("SMA200" in reason for reason in reasons)


def test_classify_posture_defensive_on_deep_drawdown():
    posture, _ = classify_posture(extract_inputs(_market(drawdown=-15.0)))

    assert posture == "defensive"


def test_classify_posture_flat_on_20pct_drawdown():
    posture, _ = classify_posture(extract_inputs(_market(drawdown=-20.0)))

    assert posture == "flat"


def test_classify_posture_defensive_below_sma200():
    posture, reasons = classify_posture(
        extract_inputs(_market(close=74000, sma200=75000, slope="down")))

    assert posture == "defensive"
    assert any("SMA200" in reason for reason in reasons)


def test_classify_posture_neutral_when_structure_unclear():
    posture, _ = classify_posture(
        extract_inputs(_market(close=76000, sma200=75000, slope="flat")))

    assert posture == "neutral"


def test_classify_posture_top_risk_forces_defensive():
    posture, reasons = classify_posture(
        extract_inputs(_market(top_risk=True)))

    assert posture == "defensive"
    assert "top_risk.active" in reasons


def test_first_evaluation_maps_cap_without_ratchet():
    state = evaluate_risk(_market(), previous={})

    assert state["posture"] == "offensive"
    assert state["cap"] == 0.8
    assert state["ratchet"] is False
    assert state["applied"] is False


def test_cap_map_override_from_config():
    state = evaluate_risk(_market(), previous={},
                          cfg={"exposure_cap": {"offensive": 0.6}})

    assert state["cap"] == 0.6


def test_downgrade_lowers_cap_and_resets_release_counter():
    previous = {"posture": "offensive", "cap": 0.8, "release_counter": 2}
    state = evaluate_risk(_market(drawdown=-15.0), previous=previous)

    assert state["posture"] == "defensive"
    assert state["cap"] == 0.25
    assert state["release_counter"] == 0
    assert any("cap 下调" in reason for reason in state["reasons"])


def test_ratchet_holds_cap_until_release_confirmed():
    previous = {"posture": "defensive", "cap": 0.25, "release_counter": 0}
    # 结构已回到 offensive, 但 close 未站上 SMA50 → 释放条件不满足
    state = evaluate_risk(_market(close=77000, sma50=78000), previous=previous)

    assert state["posture"] == "offensive"
    assert state["cap"] == 0.25
    assert state["ratchet"] is True
    assert state["release_ok"] is False
    assert state["release_counter"] == 0


def test_release_requires_consecutive_confirmations():
    previous = {"posture": "defensive", "cap": 0.25, "release_counter": 0}

    first = evaluate_risk(_market(drawdown=-5.0), previous=previous)
    second = evaluate_risk(_market(drawdown=-5.0), previous=first)
    third = evaluate_risk(_market(drawdown=-5.0), previous=second)

    assert first["cap"] == 0.25 and first["release_counter"] == 1
    assert second["cap"] == 0.25 and second["release_counter"] == 2
    assert third["cap"] == 0.8 and third["release_counter"] == 3
    assert third["ratchet"] is False


def test_release_counter_resets_when_condition_fails_midway():
    previous = {"posture": "defensive", "cap": 0.25, "release_counter": 2}
    state = evaluate_risk(_market(top_risk=True), previous=previous)

    assert state["release_counter"] == 0
    assert state["cap"] == 0.25


def test_missing_data_fail_open_does_not_downgrade():
    previous = {"posture": "offensive", "cap": 0.8, "release_counter": 2}
    state = evaluate_risk({}, previous=previous)

    assert state["posture"] == "neutral"
    assert state["cap"] == 0.8  # fail-open: 无证据不改变 cap
    assert state["data_ok"] is False
    assert state["release_counter"] == 0  # 释放确认必须连续
    assert any("fail-open" in reason for reason in state["reasons"])


def test_missing_data_first_eval_has_no_cap_claim():
    state = evaluate_risk({}, previous={})

    assert state["cap"] is None
    assert state["data_ok"] is False


def test_enforce_mode_is_only_recorded_not_applied():
    state = evaluate_risk(_market(), previous={}, cfg={"mode": "enforce"})

    assert state["mode"] == "enforce"
    assert state["applied"] is False


def test_format_risk_contains_key_fields():
    text = format_risk(evaluate_risk(_market(drawdown=-13.0), previous={}))

    assert "posture=defensive" in text
    assert "cap=0.25" in text
    assert "dd=-13.0%" in text
    assert "mode=dry_run" in text


def test_missing_cap_mapping_keeps_previous_cap():
    previous = {"posture": "offensive", "cap": 0.8, "release_counter": 1}
    state = evaluate_risk(
        _market(), previous=previous,
        cfg={"exposure_cap": {"offensive": None, "neutral": None,
                              "defensive": None, "flat": None}})

    assert state["cap"] == 0.8
    assert state["ratchet"] is True
    assert any("无姿态映射" in reason for reason in state["reasons"])


# ---- S1 回归: 价格历史读取失败不得丢弃 risk_v2 配置 ----

class _RiskStateStub:
    def __init__(self):
        self.values = {}

    def get(self, path, default=None):
        return self.values.get(path, default)

    def set(self, path, value):
        self.values[path] = value


class _BrokenKnowledge:
    def _price_history(self):
        raise OSError("price history unavailable")


def test_price_history_failure_keeps_risk_config():
    from src.alpha import _update_risk_v2

    components = {
        "cfg": {
            "features": {"risk_v2": True},
            "alpha": {"risk_v2": {
                "mode": "enforce",
                "exposure_cap": {"offensive": 0.99},
            }},
        },
        "state": _RiskStateStub(),
        "knowledge": _BrokenKnowledge(),
    }

    state = _update_risk_v2(components, _market())

    # 历史读取失败仅影响回撤兜底, 配置仍按 enforce/0.99 生效 (dry-run 证据链)
    assert state["mode"] == "enforce"
    assert state["posture"] == "offensive"
    assert state["cap"] == 0.99
    assert components["state"].get("risk_v2.state") == state
