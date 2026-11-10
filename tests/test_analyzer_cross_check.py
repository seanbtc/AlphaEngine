"""单次判断交叉验证 (按估值档位一致性) — 离线单元测试 (stub _call_api)。"""
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.analyzer import Analyzer  # noqa: E402

_TWEETS = [{"id": "1", "date": "2026-09-17T12:25:00",
            "url": "https://x.com/i/web/status/1",
            "content": "BTC ETF flow update"}]

_VALID_SCORES = {"profitability": 0.1, "institutional": 0.1, "onchain": 0.1,
                 "derivatives": 0.1, "macro": 0.1}


def _result(valuation=0.62, conf="high", forecast=None, **overrides):
    result = {
        "valuation": valuation,
        "valuation_confidence": conf,
        "valuation_evidence": "MVRV 2.1 中性偏高",
        "summary": "x",
        "evidence_scores": dict(_VALID_SCORES),
    }
    if forecast is not None:
        result["forecast"] = forecast
    result.update(overrides)
    return result


class _StubAnalyzer(Analyzer):
    def __init__(self, responses, cross_check=None, vision=False):
        cfg = {
            "enabled": True,
            "endpoint": "http://127.0.0.1:5010",
            "vision_enabled": vision,
            "cross_check": cross_check if cross_check is not None
            else {"samples": 2},
        }
        super().__init__(cfg)
        self._responses = list(responses)
        self.calls = 0

    def _call_api(self, user_msg, images=None):
        self.calls += 1
        if not self._responses:
            return None, False
        return self._responses.pop(0)


def test_init_components_injects_cross_check(tmp_path):
    from src.alpha import init_components

    cfg = {
        "paths": {"data_dir": str(tmp_path)},
        "ai_service": {"endpoint": "http://127.0.0.1:5010"},
        "valuation": {"cross_check": {"samples": 3}},
    }
    components = init_components(cfg)

    assert components["analyzer"].cross_check_samples == 3


# ---- 交叉验证: 档位一致 / 跨档 ----

def test_samples_two_same_band_consistent(capsys):
    analyzer = _StubAnalyzer([(_result(0.62), False), (_result(0.68), False)])

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result is not None
    assert result["valuation_confidence"] == "high"
    assert analyzer.calls == 2
    assert "交叉验证一致 (2 样本): 估值=0.62 (轻度高估)" in out
    assert result["cross_check"]["band_agreement"] == 1.0
    assert result["cross_check"]["dispersed"] is False


def test_samples_two_cross_band_degrades(capsys):
    analyzer = _StubAnalyzer([(_result(0.42), False), (_result(0.62), False)])

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result["valuation_confidence"] == "low"
    assert "交叉验证跨档不一致" in out
    assert "轻度低估" in out and "轻度高估" in out
    assert analyzer.calls == 2
    # 归一后保留原字段，并新增审计字段
    assert set(_result()) <= set(result)
    assert result["cross_check"]["dispersed"] is True
    assert result["cross_check"]["samples"] == 2
    assert result["cross_check"]["valuation_spread"] == pytest.approx(0.20)


def test_samples_three_cross_band_degrades(capsys):
    analyzer = _StubAnalyzer([(_result(0.62), False),
                              (_result(0.65), False),
                              (_result(0.22), False)],
                             cross_check={"samples": 3})

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result["valuation_confidence"] == "low"
    assert analyzer.calls == 3
    assert "交叉验证跨档不一致" in out
    assert result["cross_check"]["band_agreement"] == pytest.approx(2 / 3, abs=1e-4)


def test_same_band_wide_spread_only_logged(capsys):
    """档内数值离散 (spread > 0.15) 只留痕, 不改 live 置信。"""
    analyzer = _StubAnalyzer([(_result(0.52), False), (_result(0.78), False)])

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result["valuation_confidence"] == "high"
    assert "交叉验证档内离散" in out
    assert result["cross_check"]["dispersed"] is True
    assert result["cross_check"]["band_agreement"] == 1.0


def test_same_band_direction_disagreement_only_logged(capsys):
    up = {"horizon_days": 30, "direction": "up", "price_low": 1, "price_high": 2,
          "confidence": 0.6, "hit_probability": 0.6, "basis": ["inferred: x"]}
    down = dict(up, direction="down")
    analyzer = _StubAnalyzer([(_result(0.62, forecast=up), False),
                              (_result(0.64, forecast=down), False)])

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result["valuation_confidence"] == "high"
    assert "交叉验证档内离散" in out
    assert result["cross_check"]["forecast_direction_agreement"] == 0.5


def test_extra_sample_call_failure_logged(capsys):
    analyzer = _StubAnalyzer([(_result(), False), (None, True)])

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result["valuation_confidence"] == "high"
    assert analyzer.calls == 2
    assert "交叉验证样本 2/2 无效 (调用失败, retryable=True), 忽略" in out


def test_extra_sample_validation_failure_logged(capsys):
    analyzer = _StubAnalyzer([(_result(), False), (_result(valuation=1.7), False)])

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result["valuation_confidence"] == "high"
    assert analyzer.calls == 2
    assert "交叉验证样本 2/2 无效 (输出校验不通过), 忽略" in out


# ---- 首样本无效: 走既有失败流程, 不触发交叉验证 ----

def test_first_sample_invalid_no_cross_check_calls(capsys):
    analyzer = _StubAnalyzer([(None, False), (_result(), False)])

    result = analyzer.analyze(list(_TWEETS), "", retries=0)
    out = capsys.readouterr().out

    assert result is None
    assert analyzer.calls == 1
    assert "交叉验证" not in out


def test_first_sample_invalid_schema_no_cross_check_calls(capsys):
    analyzer = _StubAnalyzer([(_result(valuation=None), False), (_result(), False)])

    result = analyzer.analyze(list(_TWEETS), "", retries=0)
    out = capsys.readouterr().out

    assert result is None
    assert analyzer.calls == 1
    assert "VALIDATION failed" in out
    assert "交叉验证" not in out


# ---- samples=1 完全回退旧行为 ----

def test_samples_one_single_call(capsys):
    analyzer = _StubAnalyzer([(_result(), False)],
                             cross_check={"samples": 1})

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result["valuation_confidence"] == "high"
    assert analyzer.calls == 1
    assert "交叉验证" not in out
    assert "cross_check" not in result


def test_samples_default_is_two(capsys):
    analyzer = _StubAnalyzer([(_result(), False), (_result(), False)],
                             cross_check={})

    result = analyzer.analyze(list(_TWEETS), "")

    assert result["valuation_confidence"] == "high"
    assert analyzer.calls == 2


# ---- vision/图片路径同策略 ----

def test_vision_path_uses_same_cross_check(capsys):
    tweets = [dict(_TWEETS[0],
                   images=["https://pbs.twimg.com/media/example.jpg"])]
    analyzer = _StubAnalyzer([(_result(0.62), False),
                              (_result(0.18), False)], vision=True)

    result = analyzer.analyze(tweets, "")
    out = capsys.readouterr().out

    assert analyzer.calls == 2
    assert result["valuation_confidence"] == "low"
    assert "使用视觉(多模态)评估" in out
    assert "交叉验证跨档不一致" in out
