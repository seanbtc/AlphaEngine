"""Risk Budget v2 单测 (Phase 2, dry_run): 姿态/上限/棘轮/释放/缺数据 fail-open.

Phase 2 约束: 只计算并记录, 不改变 alpha。任何数据缺失不得误降风险 (fail-open)。
"""
import sys
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
    previous = {"posture": "offensive", "cap": 0.8, "release_counter": 0}
    state = evaluate_risk({}, previous=previous)

    assert state["posture"] == "neutral"
    assert state["cap"] == 0.8  # fail-open: 无证据不改变 cap
    assert state["data_ok"] is False
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
