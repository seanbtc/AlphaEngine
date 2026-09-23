"""长周期纪律 (prompt 章节 / 运行时锚点 / 被拒提议 progress 语义) — 单元测试, 全程离线。"""
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.alpha import _select_progress, build_market_state, run_cycle  # noqa: E402
from src.alpha_engine import (AlphaEngine, EvidenceAccumulator,  # noqa: E402
                              FORWARD_NEXT_REGIME, REGIME_TRANSITIONS)
from src.analyzer import SYSTEM_PROMPT, Analyzer  # noqa: E402
from src.state_manager import StateManager  # noqa: E402


# ---- 状态机: 严格相邻 ±1 不变量 ----

_CYCLE = ["BEAR", "BEAR_DEEP", "BEAR_BOTTOM", "RECOVERY",
          "BULL", "DEEP_BULL", "BULL_COOLING"]


def test_forward_next_regime_matches_cycle():
    for i, regime in enumerate(_CYCLE):
        assert FORWARD_NEXT_REGIME[regime] == _CYCLE[(i + 1) % len(_CYCLE)]


def test_transition_table_covers_all_regimes():
    assert set(REGIME_TRANSITIONS) == {"INIT"} | set(_CYCLE)


@pytest.mark.parametrize("regime", ["INIT"] + _CYCLE)
def test_transition_table_is_strictly_adjacent(regime):
    allowed = set(REGIME_TRANSITIONS[regime])
    if regime == "INIT":
        assert allowed == {"BEAR"}
        return
    i = _CYCLE.index(regime)
    expected = {_CYCLE[i - 1], regime, _CYCLE[(i + 1) % len(_CYCLE)]}
    assert allowed == expected


def test_old_multi_step_exceptions_removed():
    assert "BULL_COOLING" not in REGIME_TRANSITIONS["BULL"]
    assert "BEAR_DEEP" not in REGIME_TRANSITIONS["RECOVERY"]


# ---- SYSTEM_PROMPT 长周期纪律章节 ----

def test_system_prompt_contains_long_cycle_discipline():
    assert "长周期纪律" in SYSTEM_PROMPT
    assert "周期级别" in SYSTEM_PROMPT
    assert "相邻" in SYSTEM_PROMPT
    assert "跨级" in SYSTEM_PROMPT
    assert "变更必须相邻" in SYSTEM_PROMPT
    assert "RECOVERY→BULL_COOLING" in SYSTEM_PROMPT
    assert "锚点中会列出本轮允许的变更" in SYSTEM_PROMPT
    assert "严格逐步推进" in SYSTEM_PROMPT


def test_long_cycle_section_before_position_discipline():
    assert SYSTEM_PROMPT.index("长周期纪律") < SYSTEM_PROMPT.index("## 仓位纪律")


# ---- 运行时锚点: allowed_transitions ----

def _make_analyzer():
    return Analyzer({"endpoint": "http://127.0.0.1:5010", "enabled": False})


_ANCHOR_LABELS = {
    "INIT": "- 允许的变更: 正向 BEAR",
    "BEAR": "- 允许的变更: 保持 BEAR / 正向 BEAR_DEEP / 回退 BULL_COOLING",
    "BEAR_DEEP": "- 允许的变更: 保持 BEAR_DEEP / 正向 BEAR_BOTTOM / 回退 BEAR",
    "BEAR_BOTTOM": "- 允许的变更: 保持 BEAR_BOTTOM / 正向 RECOVERY / 回退 BEAR_DEEP",
    "RECOVERY": "- 允许的变更: 保持 RECOVERY / 正向 BULL / 回退 BEAR_BOTTOM",
    "BULL": "- 允许的变更: 保持 BULL / 正向 DEEP_BULL / 回退 RECOVERY",
    "DEEP_BULL": "- 允许的变更: 保持 DEEP_BULL / 正向 BULL_COOLING / 回退 BULL",
    "BULL_COOLING": "- 允许的变更: 保持 BULL_COOLING / 正向 BEAR / 回退 DEEP_BULL",
}


@pytest.mark.parametrize("regime,expected", list(_ANCHOR_LABELS.items()))
def test_format_market_state_all_regimes_labels(regime, expected):
    text = _make_analyzer()._format_market_state({
        "regime": regime,
        "allowed_transitions": REGIME_TRANSITIONS[regime],
    })
    assert expected in text


def test_format_market_state_without_allowed_transitions():
    analyzer = _make_analyzer()
    assert "允许的变更" not in analyzer._format_market_state({"regime": "RECOVERY"})
    assert "允许的变更" not in analyzer._format_market_state(
        {"regime": "RECOVERY", "allowed_transitions": []})


def test_build_market_state_includes_allowed_transitions(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", "RECOVERY")
    engine = AlphaEngine({}, sm)

    state = build_market_state({"state": sm, "engine": engine})

    assert state["regime"] == "RECOVERY"
    assert state["allowed_transitions"] == ["BEAR_BOTTOM", "RECOVERY", "BULL"]
    text = _make_analyzer()._format_market_state(state)
    assert "- 允许的变更: 保持 RECOVERY / 正向 BULL / 回退 BEAR_BOTTOM" in text
    # 返回的是副本, 外部修改不影响状态机表
    state["allowed_transitions"].append("BEAR_DEEP")
    assert REGIME_TRANSITIONS["RECOVERY"] == ["BEAR_BOTTOM", "RECOVERY", "BULL"]


# ---- _select_progress 矩阵 ----

@pytest.mark.parametrize("rp,ok,cp,current,old,expected", [
    (None, False, "BULL_COOLING", "RECOVERY", 0.40, 0.40),   # 无提议 → 旧值
    (None, True, "BULL", "RECOVERY", 0.40, 0.40),            # 无进度 → 旧值
    (0.82, True, "BULL", "RECOVERY", 0.40, 0.82),            # 提议被接受 → 新值
    (0.65, False, "RECOVERY", "RECOVERY", 0.40, 0.65),       # cp==当前 → 新值
    (0.65, True, "RECOVERY", "RECOVERY", 0.40, 0.65),        # 保持 + 接受 → 新值
    (0.82, False, "BULL_COOLING", "RECOVERY", 0.40, 0.40),   # 被拒跨级 → 旧值
    ("bad", False, "BULL_COOLING", "RECOVERY", 0.40, 0.40),  # 非法值 → 旧值
    (1.20, True, "BULL", "RECOVERY", 0.40, 1.00),            # 越界 → clamp 1.0
    (-0.30, False, "RECOVERY", "RECOVERY", 0.40, 0.00),      # 越界 → clamp 0.0
])
def test_select_progress_matrix(rp, ok, cp, current, old, expected):
    assert _select_progress(rp, ok, cp, current, old) == expected


# ---- 离线仿真: 被拒跨级提议不得覆盖 alpha.regime_progress ----

class _FakeMemory:
    def get_context_for_ai(self):
        return ""

    def append_alpha(self, record):
        pass

    def append_entry(self, text):
        pass

    def add_metric(self, name, value):
        pass


class _FakeFetcher:
    def fetch(self):
        return [{"id": "1", "date": "2026-09-17T12:25:00",
                 "url": "https://x.com/i/web/status/1",
                 "content": "BTC ETF flow update"}]


class _FakeAnalyzer:
    def __init__(self, result):
        self.result = result

    def analyze(self, tweets, memory_context, knowledge_base="",
                retries=1, market_state=None):
        return self.result


class _FakeKnowledge:
    def load_knowledge_base(self):
        return ""

    def log_drift_meta(self, meta, cycle_position):
        pass

    def check_drift(self):
        return []

    def log_prediction(self, cycle_position, confidence, btc_price=None):
        pass

    def distill_due(self, state_manager, now=None):
        return False


class _FakeTradeSync:
    def send_order(self, alpha, regime, price=None):
        return None


class _FakeDataFeed:
    def get_price(self):
        return None


class _FakeDingTalk:
    def regime_change(self, *args, **kwargs):
        pass

    def alpha_change(self, *args, **kwargs):
        pass

    def analysis(self, *args, **kwargs):
        return ""

    def alert(self, *args, **kwargs):
        pass


class _FakeReview:
    def record_price(self, *args, **kwargs):
        pass

    def price_trend(self):
        return {}

    def should_review(self):
        return False


def _build_components(tmp_path, analysis, regime="RECOVERY", alpha=0.70,
                      progress=0.40, cooldown=0):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", regime)
    sm.set("regime.entered_from", "BEAR_BOTTOM")
    sm.set("regime.cooldown_remaining", cooldown)
    sm.set("alpha.current", alpha)
    sm.set("alpha.target", alpha)
    sm.set("alpha.regime_progress", progress)

    engine = AlphaEngine({"smoothing": {},
                          "stability": {"required_confirmations": 1}}, sm)
    return {
        "cfg": {"schedule": {"min_analysis_interval_hours": 0}},
        "memory": _FakeMemory(),
        "state": sm,
        "fetcher": _FakeFetcher(),
        "analyzer": _FakeAnalyzer(analysis),
        "engine": engine,
        "evidence": EvidenceAccumulator(sm, 0.02),
        "knowledge": _FakeKnowledge(),
        "tradesync": _FakeTradeSync(),
        "datafeed": _FakeDataFeed(),
        "dingtalk": _FakeDingTalk(),
        "review": _FakeReview(),
    }


def test_rejected_cross_level_proposal_keeps_progress(tmp_path, capsys):
    """RECOVERY 下 AI 跨级提议 BULL_COOLING (rp=0.82): 拒绝 + progress 保留 0.40。"""
    analysis = {
        "cycle_position": "BULL_COOLING",
        "cycle_confidence": "high",
        "regime_progress": 0.82,
        "regime_evidence": "ETF 流出转弱 (跨级提议)",
        "summary": "cross-level proposal",
        "evidence_scores": {"profitability": -0.4, "institutional": -0.5,
                            "onchain": -0.3, "derivatives": -0.2, "macro": -0.1},
        "signal_board": [],
        "meta": {"analysis_quality": 8},
    }
    components = _build_components(tmp_path, analysis)
    sm = components["state"]
    engine = components["engine"]

    run_cycle(components)
    out = capsys.readouterr().out

    assert engine.get_regime() == "RECOVERY"
    assert sm.get("alpha.regime_progress") == pytest.approx(0.40)
    assert "请求 BULL_COOLING 被拒绝" in out
    assert "保留旧值" in out
    # target 按旧 progress 计算: 0.70 + (1.00-0.70)*0.40 = 0.82
    # (若错位采用被拒提议的 0.82 → 0.946)
    assert sm.get("alpha.target") == pytest.approx(0.82)
    assert sm.get("alpha.current") == pytest.approx(0.72)


def test_same_regime_progress_still_applies(tmp_path):
    """cp==当前 regime 时 (未被拒), AI 的 progress 正常落盘并参与 target。"""
    analysis = {
        "cycle_position": "RECOVERY",
        "cycle_confidence": "high",
        "regime_progress": 0.80,
        "regime_evidence": "恢复确认推进",
        "summary": "in-regime progress update",
        "evidence_scores": {"profitability": 0.4, "institutional": 0.5,
                            "onchain": 0.3, "derivatives": 0.2, "macro": 0.1},
        "signal_board": [],
        "meta": {"analysis_quality": 8},
    }
    components = _build_components(tmp_path, analysis)
    sm = components["state"]

    run_cycle(components)

    assert components["engine"].get_regime() == "RECOVERY"
    assert sm.get("alpha.regime_progress") == pytest.approx(0.80)
    assert sm.get("alpha.target") == pytest.approx(0.94)


# ---- 离线仿真: 接受路径 (相邻前进一步) ----

def test_accepted_adjacent_proposal_uses_new_progress(tmp_path, capsys):
    """BEAR 下 AI 相邻提议 BEAR_DEEP (rp=0.80): 接受 + execute 用新 progress。

    若 execute 误用旧 progress 0.50, target 会是 -0.15 而非 -0.06。
    """
    analysis = {
        "cycle_position": "BEAR_DEEP",
        "cycle_confidence": "high",
        "regime_progress": 0.80,
        "regime_evidence": "投降特征出现但未达极值",
        "summary": "adjacent forward step",
        "evidence_scores": {"profitability": -0.6, "institutional": -0.5,
                            "onchain": -0.4, "derivatives": -0.3, "macro": -0.2},
        "signal_board": [],
        "meta": {"analysis_quality": 8},
    }
    components = _build_components(tmp_path, analysis, regime="BEAR",
                                   alpha=-1.00, progress=0.50)
    sm = components["state"]
    engine = components["engine"]

    run_cycle(components)
    out = capsys.readouterr().out

    assert "[REGIME] BEAR → BEAR_DEEP" in out
    assert engine.get_regime() == "BEAR_DEEP"
    assert sm.get("alpha.regime_progress") == pytest.approx(0.80)
    # BEAR_DEEP 基准 -0.30, 下一位置 BEAR_BOTTOM=0: -0.30 + 0.30*0.80 = -0.06
    assert sm.get("alpha.target") == pytest.approx(-0.06)
    assert sm.get("alpha.current") == pytest.approx(-0.06)


# ---- 离线仿真: 拒绝原因 (冷却期 / 低置信) ----

@pytest.mark.parametrize("cooldown,conf,expected_reason", [
    (3, "high", "冷却期剩余 3 轮"),
    (0, "low", "置信度为 low，拒绝 regime 变更"),
])
def test_rejection_reasons_keep_progress(tmp_path, capsys, cooldown, conf,
                                         expected_reason):
    """相邻提议 BULL 因冷却期/低置信被拒时, progress 与 regime 均不变。"""
    analysis = {
        "cycle_position": "BULL",
        "cycle_confidence": conf,
        "regime_progress": 0.82,
        "regime_evidence": "恢复确认充分",
        "summary": "rejected for gating reason",
        "evidence_scores": {"profitability": 0.5, "institutional": 0.5,
                            "onchain": 0.4, "derivatives": 0.3, "macro": 0.2},
        "signal_board": [],
        "meta": {"analysis_quality": 8},
    }
    components = _build_components(tmp_path, analysis, regime="RECOVERY",
                                   alpha=0.70, progress=0.40, cooldown=cooldown)
    sm = components["state"]

    run_cycle(components)
    out = capsys.readouterr().out

    assert expected_reason in out
    assert "保留旧值" in out
    assert components["engine"].get_regime() == "RECOVERY"
    assert sm.get("alpha.regime_progress") == pytest.approx(0.40)
