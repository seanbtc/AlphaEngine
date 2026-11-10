"""分析帖子文本: 估值头部 + 中文类目 + 评估总结/预测 + 不展示置信标签."""
import sys
from pathlib import Path

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.notify import DingTalk, _format_forecast  # noqa: E402


def _dt():
    return DingTalk({"enabled": False})


SIGNALS = [
    {"category": "institutional", "name": "美国现货ETF资金流",
     "detail": "周度净流出$1.60B"},
    {"category": "derivatives", "name": "永续巨鲸持仓", "detail": "净空头最大"},
    {"category": "profitability", "name": "短期持有者盈亏", "detail": "接近盈亏平衡"},
    {"category": "onchain", "name": "山寨投机退潮", "detail": "成交量回归正常"},
    {"category": "macro", "name": "跨资产资金轮动", "detail": "资金转向股市"},
]

FORECAST = {"horizon_days": 30, "direction": "flat", "price_low": 70000,
            "price_high": 80000, "confidence": 0.55, "hit_probability": 0.65}


def test_analysis_text_header_shows_valuation_band():
    text = _dt().analysis_text("摘要", 0.62, confidence="medium",
                               signal_board=SIGNALS, post_no=39)

    assert text.startswith("No.39 | 估值 0.62 · 轻度高估")
    assert "周期" not in text
    assert "置信: medium" not in text
    for label in ("[机构]", "[衍生品]", "[盈利]", "[链上]", "[宏观]"):
        assert label in text
    assert "[institutional]" not in text


def test_analysis_text_unassessed_when_valuation_missing():
    text = _dt().analysis_text("摘要", None)

    assert text.startswith("估值 未评估")
    assert "No." not in text


def test_analysis_text_appends_assessment_and_forecast():
    text = _dt().analysis_text(
        "摘要", 0.62, confidence="medium", signal_board=SIGNALS, post_no=39,
        valuation_evidence="MVRV 2.1 偏高, ETF 流入放缓",
        forecast=FORECAST)

    assert "评估总结: MVRV 2.1 偏高, ETF 流入放缓" in text
    assert "预测: 30天震荡 | 区间 $70,000-$80,000 | 置信 55%" in text
    assert text.index("评估总结") < text.index("预测:")
    assert text.rstrip().endswith("置信 55%")


def test_analysis_text_without_forecast_omits_prediction():
    text = _dt().analysis_text("摘要", 0.18, signal_board=SIGNALS)

    assert "评估总结" not in text
    assert "预测:" not in text
    assert text.startswith("估值 0.18 · 极度低估")


def test_format_forecast_handles_invalid_payload():
    assert _format_forecast(None) == ""
    assert _format_forecast({}) == ""
    assert _format_forecast({"price_low": 1, "price_high": 0}) == ""
    line = _format_forecast({"horizon_days": 90, "direction": "up",
                             "price_low": 70000, "price_high": 90000})
    assert line == "预测: 90天看涨 | 区间 $70,000-$90,000"


def test_format_forecast_without_direction_keeps_range():
    line = _format_forecast({"horizon_days": 30, "direction": None,
                             "price_low": 70000, "price_high": 80000})
    assert line == "预测: 区间 $70,000-$80,000"
