"""AI 输出校验强化 — _validate 表驱动 + 首轮/回溯 cp 守卫 (离线)。"""
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.alpha import (  # noqa: E402
    _valid_cycle_position, run_backfill, run_first_analysis)
from src.alpha_engine import AlphaEngine, EvidenceAccumulator  # noqa: E402
from src.analyzer import Analyzer  # noqa: E402
from src.state_manager import StateManager  # noqa: E402

_VALID_SCORES = {"profitability": 0.1, "institutional": 0.1, "onchain": 0.1,
                 "derivatives": 0.1, "macro": 0.1}


def _result(**overrides):
    result = {
        "cycle_position": "RECOVERY", "cycle_confidence": "high",
        "regime_progress": 0.5, "regime_evidence": "x", "summary": "x",
        "evidence_scores": dict(_VALID_SCORES),
    }
    result.update(overrides)
    return result


# ---- _validate 表驱动 ----

@pytest.mark.parametrize("case,result,expected", [
    ("合法值", _result(), True),
    ("额外顶层键容忍", _result(tweet_draft="t", position_narrative="p",
                               risks=[], signal_board=[]), True),
    ("分数边界 ±1/0", _result(evidence_scores={
        "profitability": 1.0, "institutional": -1.0, "onchain": 0,
        "derivatives": 0.5, "macro": -0.5}), True),
    ("非法 cp 拼写", _result(cycle_position="BULLISH"), False),
    ("非法 cp 小写", _result(cycle_position="bull"), False),
    ("非法 cp INIT (AI 不应输出)", _result(cycle_position="INIT"), False),
    ("非法 cp 缺失", _result(cycle_position=None), False),
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
    ("progress 越界", _result(regime_progress=1.2), False),
    ("confidence 非法", _result(cycle_confidence="certain"), False),
])
def test_validate_table(case, result, expected):
    assert Analyzer._validate(result) is expected, case


def test_validate_valid_baseline_prints_no_failure(capsys):
    assert Analyzer._validate(_result()) is True
    assert "VALIDATION" not in capsys.readouterr().out


# ---- cp 合法性守卫 ----

@pytest.mark.parametrize("cp,expected", [
    ("BEAR", True), ("RECOVERY", True), ("BULL", True),
    ("INIT", False), ("bear", False), ("", False), (None, False),
])
def test_valid_cycle_position(cp, expected):
    assert _valid_cycle_position(cp) is expected


# ---- 首轮/回溯: 非法 cp 拒绝且不写 state ----

class _FakeMemory:
    def get_context_for_ai(self):
        return ""

    def append_alpha(self, record):
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
    def regime_change(self, *args, **kwargs):
        pass

    def alpha_change(self, *args, **kwargs):
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
        "engine": AlphaEngine({}, sm),
        "evidence": EvidenceAccumulator(sm, 0.02),
        "knowledge": _FakeKnowledge(),
        "dingtalk": _FakeDingTalk(),
    }


def test_run_first_analysis_rejects_invalid_cp(tmp_path, capsys):
    analysis = _result(cycle_position="MOON")
    components = _components(tmp_path, analysis)

    assert run_first_analysis(components) is False

    sm = components["state"]
    assert sm.get("regime.current") == "BEAR"
    assert sm.get("alpha.current") == pytest.approx(0.0)
    assert sm.get("runtime.analysis_count") == 0
    assert sm.get("runtime.last_deepseek_at", "") == ""
    out = capsys.readouterr().out
    assert "非法 cycle_position='MOON', 拒绝本批" in out


def test_run_backfill_rejects_invalid_cp(tmp_path, capsys):
    analysis = _result(cycle_position="INIT")
    components = _components(tmp_path, analysis)

    assert run_backfill(components, force=False) is False

    sm = components["state"]
    assert sm.get("regime.current") == "BEAR"
    assert sm.get("alpha.current") == pytest.approx(0.0)
    assert sm.get("runtime.last_deepseek_at", "") == ""
    out = capsys.readouterr().out
    assert "非法 cycle_position='INIT', 拒绝本批" in out


def test_run_first_analysis_accepts_valid_cp(tmp_path):
    analysis = _result(cycle_position="RECOVERY")
    components = _components(tmp_path, analysis)

    assert run_first_analysis(components) is True

    sm = components["state"]
    assert components["engine"].get_regime() == "RECOVERY"
    assert sm.get("runtime.analysis_count") == 1
