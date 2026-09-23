"""WP2 ⑤: 单次判断交叉验证 + 结构一致性检查 — 离线单元测试 (stub _call_api)。"""
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


def _result(cp="RECOVERY", conf="high", **overrides):
    result = {
        "cycle_position": cp,
        "cycle_confidence": conf,
        "regime_progress": 0.5,
        "regime_evidence": "x",
        "summary": "x",
        "evidence_scores": dict(_VALID_SCORES),
    }
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


def _ma_state(price, sma200, alpha=None):
    state = {"ma_context": {"snapshot": {"price": price,
                                         "mas": {"sma200": {"value": sma200}}}}}
    if alpha is not None:
        state["alpha"] = alpha
    return state


def _structure_analyzer(cp):
    return _StubAnalyzer([(_result(cp=cp), False)],
                         cross_check={"samples": 1,
                                      "structure_check": {"enabled": True}})


def test_init_components_injects_cross_check(tmp_path):
    from src.alpha import init_components

    cfg = {
        "paths": {"data_dir": str(tmp_path)},
        "ai_service": {"endpoint": "http://127.0.0.1:5010"},
        "alpha": {"cross_check": {"samples": 3,
                                  "structure_check": {"enabled": False}}},
    }
    components = init_components(cfg)

    assert components["analyzer"].cross_check_samples == 3
    assert components["analyzer"].structure_check_enabled is False


# ---- 交叉验证: 一致 / 不一致 ----

def test_samples_two_consistent(capsys):
    analyzer = _StubAnalyzer([(_result(), False), (_result(), False)])

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result is not None
    assert result["cycle_confidence"] == "high"
    assert analyzer.calls == 2
    assert "交叉验证一致 (2 样本): cp=RECOVERY" in out


def test_samples_two_inconsistent_degrades(capsys):
    analyzer = _StubAnalyzer([(_result(cp="RECOVERY"), False),
                              (_result(cp="BULL"), False)])

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result["cycle_confidence"] == "low"
    assert "交叉验证不一致" in out
    assert "RECOVERY" in out and "BULL" in out
    assert analyzer.calls == 2
    # 不新增/不改动其它输出字段
    assert set(result) == set(_result())


def test_samples_three_inconsistent_degrades(capsys):
    analyzer = _StubAnalyzer([(_result(cp="RECOVERY"), False),
                              (_result(cp="RECOVERY"), False),
                              (_result(cp="BULL"), False)],
                             cross_check={"samples": 3})

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result["cycle_confidence"] == "low"
    assert analyzer.calls == 3
    assert "交叉验证不一致" in out


def test_extra_sample_call_failure_logged(capsys):
    analyzer = _StubAnalyzer([(_result(), False), (None, True)])

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result["cycle_confidence"] == "high"
    assert analyzer.calls == 2
    assert "交叉验证样本 2/2 无效 (调用失败, retryable=True), 忽略" in out


def test_extra_sample_validation_failure_logged(capsys):
    analyzer = _StubAnalyzer([(_result(), False), (_result(cp="MOON"), False)])

    result = analyzer.analyze(list(_TWEETS), "")
    out = capsys.readouterr().out

    assert result["cycle_confidence"] == "high"
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
    analyzer = _StubAnalyzer([(_result(cp="MOON"), False), (_result(), False)])

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

    assert result["cycle_confidence"] == "high"
    assert analyzer.calls == 1
    assert "交叉验证" not in out


def test_samples_default_is_two(capsys):
    analyzer = _StubAnalyzer([(_result(), False), (_result(), False)],
                             cross_check={})

    result = analyzer.analyze(list(_TWEETS), "")

    assert result["cycle_confidence"] == "high"
    assert analyzer.calls == 2


# ---- vision/图片路径同策略 ----

def test_vision_path_uses_same_cross_check(capsys):
    tweets = [dict(_TWEETS[0],
                   images=["https://pbs.twimg.com/media/example.jpg"])]
    analyzer = _StubAnalyzer([(_result(), False),
                              (_result(cp="BULL"), False)], vision=True)

    result = analyzer.analyze(tweets, "")
    out = capsys.readouterr().out

    assert analyzer.calls == 2
    assert result["cycle_confidence"] == "low"
    assert "使用视觉(多模态)评估" in out
    assert "交叉验证不一致" in out


# ---- 结构一致性检查 (方向感知: 仅拦截增加多头暴露的提议) ----

def test_structure_bull_increasing_exposure_degrades(capsys):
    analyzer = _structure_analyzer("BULL")

    result = analyzer.analyze(list(_TWEETS), "",
                              market_state=_ma_state(80000, 90000, alpha=0.70))
    out = capsys.readouterr().out

    assert result["cycle_confidence"] == "low"
    assert "结构一致性冲突" in out
    assert "SMA200" in out
    assert set(result) == set(_result())


def test_structure_bull_already_full_not_degraded(capsys):
    analyzer = _structure_analyzer("BULL")

    result = analyzer.analyze(list(_TWEETS), "",
                              market_state=_ma_state(80000, 90000, alpha=1.00))
    out = capsys.readouterr().out

    assert result["cycle_confidence"] == "high"
    assert "结构一致性冲突" not in out


def test_structure_missing_alpha_fallback_bull_only(capsys):
    assert _structure_analyzer("BULL").analyze(
        list(_TWEETS), "",
        market_state=_ma_state(80000, 90000))["cycle_confidence"] == "low"
    assert _structure_analyzer("DEEP_BULL").analyze(
        list(_TWEETS), "",
        market_state=_ma_state(80000, 90000))["cycle_confidence"] == "high"
    assert _structure_analyzer("BULL_COOLING").analyze(
        list(_TWEETS), "",
        market_state=_ma_state(80000, 90000))["cycle_confidence"] == "high"


@pytest.mark.parametrize("cp,alpha", [
    ("DEEP_BULL", 1.00),     # BULL → DEEP_BULL 1.0 → 0.3 减仓
    ("DEEP_BULL", 0.70),     # 0.7 → 0.3 减仓
    ("BULL_COOLING", 0.30),  # DEEP_BULL → BULL_COOLING 0.3 → 0.0 清仓
    ("BULL_COOLING", 1.00),  # 1.0 → 0.0 清仓
    ("BEAR_DEEP", -1.00),    # BEAR → BEAR_DEEP 空头减仓 (目标 -0.3 <= 0)
    ("BEAR_BOTTOM", -0.30),  # BEAR_DEEP → BEAR_BOTTOM 见底准备 (目标 0.0 <= 0)
])
def test_structure_risk_reducing_positions_not_degraded(cp, alpha, capsys):
    analyzer = _structure_analyzer(cp)

    result = analyzer.analyze(list(_TWEETS), "",
                              market_state=_ma_state(80000, 90000, alpha=alpha))
    out = capsys.readouterr().out

    assert result["cycle_confidence"] == "high"
    assert "结构一致性冲突" not in out


@pytest.mark.parametrize("cp,alpha", [
    ("RECOVERY", 0.00),   # BEAR_BOTTOM → RECOVERY 翻多 (目标 +0.7)
    ("DEEP_BULL", 0.00),  # BULL_COOLING → DEEP_BULL 回退加多 (目标 +0.3)
])
def test_structure_bear_side_to_long_increases_degraded(cp, alpha, capsys):
    analyzer = _structure_analyzer(cp)

    result = analyzer.analyze(list(_TWEETS), "",
                              market_state=_ma_state(80000, 90000, alpha=alpha))
    out = capsys.readouterr().out

    assert result["cycle_confidence"] == "low"
    assert "结构一致性冲突" in out


@pytest.mark.parametrize("cp", ["BEAR", "BEAR_DEEP", "BEAR_BOTTOM", "RECOVERY"])
def test_structure_other_positions_not_intervening(cp):
    analyzer = _structure_analyzer(cp)

    result = analyzer.analyze(list(_TWEETS), "",
                              market_state=_ma_state(80000, 90000))

    assert result["cycle_confidence"] == "high"


def test_structure_above_sma200_not_intervening(capsys):
    analyzer = _structure_analyzer("BULL")

    result = analyzer.analyze(list(_TWEETS), "",
                              market_state=_ma_state(95000, 90000, alpha=0.70))
    out = capsys.readouterr().out

    assert result["cycle_confidence"] == "high"
    assert "结构一致性冲突" not in out


@pytest.mark.parametrize("market_state", [
    None,
    {},
    {"ma_context": {}},
    {"ma_context": {"snapshot": {}}},
    {"ma_context": {"snapshot": {"price": 80000}}},
    {"ma_context": {"snapshot": {"price": 80000, "mas": {}}}},
    {"ma_context": {"snapshot": {"price": 80000,
                                 "mas": {"sma200": {"value": None}}}}},
])
def test_structure_missing_data_not_intervening(market_state):
    analyzer = _StubAnalyzer([(_result(cp="BULL"), False)],
                             cross_check={"samples": 1,
                                          "structure_check": {"enabled": True}})

    result = analyzer.analyze(list(_TWEETS), "", market_state=market_state)

    assert result["cycle_confidence"] == "high"


def test_structure_check_disabled(capsys):
    analyzer = _StubAnalyzer([(_result(cp="BULL"), False)],
                             cross_check={"samples": 1,
                                          "structure_check": {"enabled": False}})

    result = analyzer.analyze(list(_TWEETS), "",
                              market_state=_ma_state(80000, 90000))
    out = capsys.readouterr().out

    assert result["cycle_confidence"] == "high"
    assert "结构一致性冲突" not in out


def test_cross_check_and_structure_both_degrade(capsys):
    analyzer = _StubAnalyzer([(_result(cp="BULL"), False),
                              (_result(cp="BULL_COOLING"), False)],
                             cross_check={"samples": 2,
                                          "structure_check": {"enabled": True}})

    result = analyzer.analyze(list(_TWEETS), "",
                              market_state=_ma_state(80000, 90000, alpha=0.70))
    out = capsys.readouterr().out

    assert result["cycle_confidence"] == "low"
    assert "交叉验证不一致" in out
    assert "结构一致性冲突" in out
