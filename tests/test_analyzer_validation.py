"""AI 输出校验强化 — _validate 表驱动 + 首轮/回溯估值守卫 (离线)。"""
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.alpha import run_backfill, run_first_analysis  # noqa: E402
from src.analyzer import Analyzer  # noqa: E402
from src.state_manager import StateManager  # noqa: E402
from src.valuation import band_of, is_valid  # noqa: E402

_VALID_SCORES = {"profitability": 0.1, "institutional": 0.1, "onchain": 0.1,
                 "derivatives": 0.1, "macro": 0.1}


def _result(**overrides):
    result = {
        "valuation": 0.62, "valuation_confidence": "high",
        "valuation_evidence": "MVRV 2.1 中性偏高, ETF 流入放缓", "summary": "x",
        "evidence_scores": dict(_VALID_SCORES),
    }
    result.update(overrides)
    return result


# ---- _validate 表驱动 ----

@pytest.mark.parametrize("case,result,expected", [
    ("合法值", _result(), True),
    ("额外顶层键容忍", _result(tweet_draft="t", risks=[], signal_board=[]), True),
    ("分数边界 ±1/0", _result(evidence_scores={
        "profitability": 1.0, "institutional": -1.0, "onchain": 0,
        "derivatives": 0.5, "macro": -0.5}), True),
    ("估值下边界 0", _result(valuation=0.0), True),
    ("估值上边界 1", _result(valuation=1), True),
    ("估值越界 >1", _result(valuation=1.01), False),
    ("估值越界 <0", _result(valuation=-0.01), False),
    ("估值字符串", _result(valuation="0.62"), False),
    ("估值百分比形式", _result(valuation="62%"), False),
    ("估值布尔值", _result(valuation=True), False),
    ("估值缺失", _result(valuation=None), False),
    ("估值依据为空", _result(valuation_evidence="   "), False),
    ("置信度非法", _result(valuation_confidence="certain"), False),
    ("分数越界 >1", _result(evidence_scores={**_VALID_SCORES,
                                             "macro": 1.01}), False),
    ("分数越界 <-1", _result(evidence_scores={**_VALID_SCORES,
                                              "macro": -1.5}), False),
    ("分数非数值", _result(evidence_scores={**_VALID_SCORES,
                                            "macro": "high"}), False),
    ("缺类别 (4/5)", _result(evidence_scores={
        "profitability": 0.1, "institutional": 0.1, "onchain": 0.1,
        "derivatives": 0.1}), False),
    ("多类别 (未知维度)", _result(evidence_scores={**_VALID_SCORES,
                                                   "sentiment": -0.2}), False),
    ("evidence_scores 非 dict", _result(evidence_scores=[0.1] * 5), False),
])
def test_validate_table(case, result, expected):
    assert Analyzer._validate(result) is expected, case


def test_validate_valid_baseline_prints_no_failure(capsys):
    assert Analyzer._validate(_result()) is True
    assert "VALIDATION" not in capsys.readouterr().out


# ---- 估值档位口径 ----

@pytest.mark.parametrize("value,expected", [
    (0.0, "极度低估"), (0.19, "极度低估"), (0.20, "轻度低估"),
    (0.49, "轻度低估"), (0.50, "轻度高估"), (0.79, "轻度高估"),
    (0.80, "极度高估"), (1.0, "极度高估"),
])
def test_band_boundaries(value, expected):
    assert band_of(value) == expected


@pytest.mark.parametrize("value,expected", [
    (0.0, True), (1.0, True), (0.5, True),
    (-0.1, False), (1.1, False), (None, False), ("0.5", False), (True, False),
])
def test_is_valid(value, expected):
    assert is_valid(value) is expected


# ---- 首轮/回溯: 非法估值拒绝且不写 state ----

class _FakeMemory:
    def get_context_for_ai(self):
        return ""

    def append_valuation(self, record):
        pass

    def append_entry(self, text):
        pass


class _FakeFetcher:
    def count_tweets(self):
        return 1

    def fetch_bulk(self, limit=0):
        return 0

    def fetch(self):
        return []

    def load_all_tweets(self):
        return [{"id": "1", "date": "2026-09-17T12:25:00",
                 "url": "https://x.com/i/web/status/1",
                 "content": "BTC ETF flow update"}]


class _FakeAnalyzer:
    def __init__(self, result):
        self.result = result

    def analyze(self, *args, **kwargs):
        return self.result


class _FakeKnowledge:
    def load_knowledge_base(self):
        return ""


class _FakeDingTalk:
    def valuation_change(self, *args, **kwargs):
        pass

    def analysis(self, *args, **kwargs):
        return ""

    def alert(self, *args, **kwargs):
        pass


def _components(tmp_path, analysis):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    return {
        "cfg": {"schedule": {"min_analysis_interval_hours": 0}},
        "memory": _FakeMemory(),
        "state": sm,
        "fetcher": _FakeFetcher(),
        "analyzer": _FakeAnalyzer(analysis),
        "knowledge": _FakeKnowledge(),
        "dingtalk": _FakeDingTalk(),
    }


def test_run_first_analysis_rejects_invalid_valuation(tmp_path, capsys):
    analysis = _result(valuation=1.7)
    components = _components(tmp_path, analysis)

    assert run_first_analysis(components) is False

    sm = components["state"]
    assert sm.get_valuation() is None
    assert sm.get("runtime.analysis_count") == 0
    assert sm.get("runtime.last_deepseek_at", "") == ""
    out = capsys.readouterr().out
    assert "非法 valuation=1.7, 拒绝本批" in out


def test_run_backfill_rejects_invalid_valuation(tmp_path, capsys):
    analysis = _result(valuation=None)
    components = _components(tmp_path, analysis)

    assert run_backfill(components, force=False) is False

    sm = components["state"]
    assert sm.get_valuation() is None
    assert sm.get("runtime.last_deepseek_at", "") == ""
    out = capsys.readouterr().out
    assert "非法 valuation=None, 拒绝本批" in out


def test_run_first_analysis_accepts_valid_valuation(tmp_path):
    analysis = _result(valuation=0.62)
    components = _components(tmp_path, analysis)

    assert run_first_analysis(components) is True

    sm = components["state"]
    assert sm.get_valuation() == pytest.approx(0.62)
    assert sm.get("valuation.label") == "轻度高估"
    assert sm.get("runtime.analysis_count") == 1
