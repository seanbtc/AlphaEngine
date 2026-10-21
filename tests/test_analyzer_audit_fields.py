# -*- coding: utf-8 -*-
"""K-03 分析器审计字段：source_type / forecast 校验 + 重跑离散。"""
import sys
from pathlib import Path

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.analyzer import Analyzer  # noqa: E402

_TWEETS = [{"id": "1", "date": "2026-10-20T12:25:00",
            "url": "https://x.com/i/web/status/1",
            "content": "BTC ETF flow update"}]

_SCORES = {"profitability": 0.1, "institutional": 0.1, "onchain": 0.1,
           "derivatives": 0.1, "macro": 0.1}


def _result(cp="RECOVERY", confidence="high", progress=0.5, **overrides):
    result = {
        "cycle_position": cp,
        "cycle_confidence": confidence,
        "regime_progress": progress,
        "regime_evidence": "x",
        "summary": "x",
        "evidence_scores": dict(_SCORES),
    }
    result.update(overrides)
    return result


class _StubAnalyzer(Analyzer):
    def __init__(self, responses, samples=2):
        super().__init__({
            "enabled": True,
            "endpoint": "http://127.0.0.1:5010",
            "cross_check": {"samples": samples, "structure_check": {"enabled": False}},
        })
        self._responses = list(responses)
        self.calls = 0

    def _call_api(self, user_msg, images=None):
        self.calls += 1
        if not self._responses:
            return None, False
        return self._responses.pop(0)


def test_normalize_audit_fields_valid():
    forecast = {
        "horizon_days": 30, "direction": "up", "price_low": 78000,
        "price_high": 92000, "confidence": 0.62, "hit_probability": 0.7,
        "basis": ["retrieved: tweet 1", "inferred: MA"],
    }
    board = [
        {"category": "onchain", "name": "LTH", "signal": 0.3,
         "detail": "x", "source": "1", "source_type": "retrieved"},
        {"category": "macro", "name": "ETF", "signal": 0.2,
         "detail": "y", "source_type": "remembered"},
        {"category": "derivatives", "name": "F", "signal": 0.1,
         "detail": "z", "source_type": "inferred"},
    ]
    out = Analyzer._normalize_audit_fields(_result(signal_board=board, forecast=forecast))
    assert out["source_type_counts"] == {
        "retrieved": 1, "remembered": 1, "inferred": 1,
        "unknown": 0, "retrieved_no_source": 0}
    assert out["forecast"]["horizon_days"] == 30
    assert out["forecast"]["price_low"] == 78000.0
    assert out["forecast"]["hit_probability"] == 0.7


def test_normalize_audit_fields_unknown_source_and_invalid_forecast():
    board = [{"category": "onchain", "name": "x", "signal": 0.1,
              "detail": "x", "source_type": "hallucinated"}]
    bad_forecast = {
        "horizon_days": 45, "direction": "moon", "price_low": 100,
        "price_high": 50, "hit_probability": 2,
    }
    out = Analyzer._normalize_audit_fields(_result(signal_board=board, forecast=bad_forecast))
    assert out["source_type_counts"]["unknown"] == 1
    assert out["signal_board"][0]["source_type"] == "unknown"
    assert out["forecast"] is None


def test_retrieved_without_source_becomes_unknown():
    board = [{"category": "onchain", "name": "x", "signal": 0.1,
              "detail": "x", "source_type": "retrieved"}]
    out = Analyzer._normalize_audit_fields(_result(signal_board=board))
    assert out["signal_board"][0]["source_type"] == "unknown"
    assert out["source_type_counts"]["retrieved"] == 0
    assert out["source_type_counts"]["retrieved_no_source"] == 1


def test_normalize_forecast_without_direction_keeps_interval():
    out = Analyzer._normalize_audit_fields(_result(forecast={
        "horizon_days": 90, "direction": "moon", "price_low": 70000,
        "price_high": 120000, "hit_probability": 0.8,
    }))
    assert out["forecast"]["horizon_days"] == 90
    assert out["forecast"]["direction"] is None
    assert out["forecast"]["price_low"] == 70000.0


def test_validate_ignores_optional_audit_fields():
    bad = _result(forecast={"horizon_days": 45}, signal_board=[{"source_type": "x"}])
    assert Analyzer._validate(bad) is True


def test_cross_check_records_consistent_dispersion():
    analyzer = _StubAnalyzer([
        (_result(progress=0.5, forecast={"horizon_days": 30, "direction": "up",
                                         "price_low": 1, "price_high": 2}), False),
        (_result(progress=0.5, forecast={"horizon_days": 30, "direction": "up",
                                         "price_low": 1, "price_high": 2}), False),
    ])
    result = analyzer.analyze(list(_TWEETS), "")
    assert result["cycle_confidence"] == "high"
    block = result["cross_check"]
    assert block["samples"] == 2
    assert block["position_agreement"] == 1.0
    assert block["progress_std"] == 0.0
    assert block["forecast_direction_agreement"] == 1.0
    assert block["dispersed"] is False


def test_cross_check_progress_dispersion_records():
    analyzer = _StubAnalyzer([
        (_result(progress=0.2), False),
        (_result(progress=0.8), False),
    ])
    result = analyzer.analyze(list(_TWEETS), "")
    # K-03: progress 离散只留痕，不改变 live 置信（只有 cycle_position 不一致才降级）
    assert result["cycle_confidence"] == "high"
    block = result["cross_check"]
    assert block["positions"] == ["RECOVERY", "RECOVERY"]
    assert block["position_agreement"] == 1.0
    assert block["progress_std"] == 0.3
    assert block["dispersed"] is True


def test_cross_check_direction_dispersion_records():
    analyzer = _StubAnalyzer([
        (_result(forecast={"horizon_days": 30, "direction": "up",
                           "price_low": 1, "price_high": 2}), False),
        (_result(forecast={"horizon_days": 30, "direction": "down",
                           "price_low": 3, "price_high": 4}), False),
    ])
    result = analyzer.analyze(list(_TWEETS), "")
    # K-03: 方向离散只留痕，不改变 live 置信
    assert result["cycle_confidence"] == "high"
    block = result["cross_check"]
    assert block["forecast_directions"] == ["up", "down"]
    assert block["forecast_direction_agreement"] == 0.5
    assert block["dispersed"] is True


def test_forecast_rejects_non_finite_price():
    out = Analyzer._normalize_audit_fields(_result(forecast={
        "horizon_days": 30, "direction": "up", "price_low": 1,
        "price_high": float("inf"), "hit_probability": 0.7,
    }))
    assert out["forecast"] is None


def test_forecast_non_finite_probability_becomes_none():
    out = Analyzer._normalize_audit_fields(_result(forecast={
        "horizon_days": 30, "direction": "up", "price_low": 1,
        "price_high": 2, "hit_probability": float("nan"),
    }))
    assert out["forecast"]["hit_probability"] is None


def test_forecast_overflow_values_are_tolerated():
    huge = 10 ** 1000
    out = Analyzer._normalize_audit_fields(_result(forecast={
        "horizon_days": float("inf"), "direction": "up",
        "price_low": 1, "price_high": 2,
    }))
    assert out["forecast"] is None

    out = Analyzer._normalize_audit_fields(_result(forecast={
        "horizon_days": 30, "direction": "up",
        "price_low": huge, "price_high": huge + 1,
    }))
    assert out["forecast"] is None

    out = Analyzer._normalize_audit_fields(_result(forecast={
        "horizon_days": 30, "direction": "up",
        "price_low": 1, "price_high": 2, "hit_probability": huge,
    }))
    assert out["forecast"]["hit_probability"] is None


def test_signal_board_capped_at_five_before_counting():
    board = [{"category": "onchain", "name": f"x{i}", "signal": 0.1,
              "detail": "x", "source": "1", "source_type": "retrieved"}
             for i in range(6)]
    out = Analyzer._normalize_audit_fields(_result(signal_board=board))
    assert len(out["signal_board"]) == 5
    assert out["source_type_counts"]["retrieved"] == 5
