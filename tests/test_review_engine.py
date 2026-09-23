"""月度复盘引擎 — should_review 补偿式触发 + apply_calibration 修复 (离线)。"""
import sys
from datetime import datetime
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.alpha_engine import (EvidenceAccumulator,  # noqa: E402
                              REGIME_ALPHA_MAP, REGIME_EXPECTED_DAYS)
from src.review_engine import ReviewEngine  # noqa: E402
from src.state_manager import StateManager  # noqa: E402


def _make_review(tmp_path, cfg=None, with_evidence=False, count=0, last=""):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("runtime.analysis_count", count)
    sm.set("runtime.last_review_at", last)
    evidence = EvidenceAccumulator(sm, 0.01) if with_evidence else None
    return ReviewEngine(cfg or {}, str(tmp_path), sm, None, None,
                        evidence=evidence), sm


# ---- should_review: 补偿式计划时刻比较 ----

def test_review_boundary_is_last_month_end_before_now():
    b = ReviewEngine._review_boundary
    assert b(datetime(2026, 10, 3, 4, 25)) == datetime(2026, 9, 30, 20, 0)
    assert b(datetime(2026, 9, 30, 19, 59)) == datetime(2026, 8, 31, 20, 0)
    assert b(datetime(2026, 9, 30, 20, 0)) == datetime(2026, 9, 30, 20, 0)
    # 跨年回退到上一年 12 月末
    assert b(datetime(2026, 1, 1, 0, 0)) == datetime(2025, 12, 31, 20, 0)


@pytest.mark.parametrize("now,count,last,expected", [
    # 月末非运行日 (2026-09-30 周三): 周六运行补偿触发
    (datetime(2026, 10, 3, 4, 25), 25, "2026-08-31T21:00:00Z", True),
    # 跨月补偿: 10 月末 20:00 后首个运行日 (11-01)
    (datetime(2026, 11, 1, 4, 25), 25, "2026-10-01T00:00:00Z", True),
    # 当月已复盘 (本周期边界后已执行) → 不重复
    (datetime(2026, 10, 15, 4, 25), 25, "2026-10-03T04:26:00Z", False),
    (datetime(2026, 11, 15, 4, 25), 25, "2026-11-01T04:26:00Z", False),
    # min_cycles 门槛
    (datetime(2026, 10, 3, 4, 25), 19, "2026-08-31T21:00:00Z", False),
    (datetime(2026, 10, 3, 4, 25), 20, "2026-08-31T21:00:00Z", True),
    # 恰好边界 (月末 20:00) → 触发; 差 1 分钟 → 未到
    (datetime(2026, 9, 30, 20, 0), 25, "2026-08-31T21:00:00Z", True),
    (datetime(2026, 9, 30, 19, 59), 25, "2026-08-31T21:00:00Z", False),
    # 从未复盘 → 到期即触发; 脏值按未复盘处理
    (datetime(2026, 10, 3, 4, 25), 25, "", True),
    (datetime(2026, 10, 3, 4, 25), 25, "not-a-date", True),
])
def test_should_review_compensation_matrix(tmp_path, now, count, last, expected):
    review, _ = _make_review(tmp_path, count=count, last=last)
    assert review.should_review(now) is expected


def test_should_review_idempotent_same_cycle(tmp_path):
    review, sm = _make_review(tmp_path, count=25, last="2026-08-31T21:00:00Z")
    now = datetime(2026, 10, 3, 4, 25)
    assert review.should_review(now) is True
    # 模拟 run_review 落盘 last_review_at 后, 同周期不再触发
    sm.set("runtime.last_review_at", "2026-10-03T04:30:00Z")
    assert review.should_review(datetime(2026, 10, 20, 4, 25)) is False
    # 到下一月末边界后再次到期
    assert review.should_review(datetime(2026, 11, 1, 4, 25)) is True


def test_should_review_uses_config_min_cycles(tmp_path):
    review, _ = _make_review(tmp_path, cfg={"min_cycles_before_review": 3},
                             count=2, last="")
    assert review.should_review(datetime(2026, 10, 3, 4, 25)) is False
    review.sm.set("runtime.analysis_count", 3)
    assert review.should_review(datetime(2026, 10, 3, 4, 25)) is True


# ---- apply_calibration: evidence.decay_per_cycle 修复 ----

class _FakeEngine:
    def __init__(self):
        self.smoothing = {
            "max_change_per_step": 0.05, "min_daily_step": 0.015,
            "cooldown_cycles_after_regime_change": 10,
        }
        self.evidence_cfg = {
            "min_categories_for_regime_change": 3,
            "min_total_score_for_regime_change": 2.0,
            "high_conf_min_categories": 2, "high_conf_min_total": 1.5,
            "decay_per_cycle": 0.01,
        }
        self.conf_gate = {
            "low_confidence_blocks_regime_change": True,
            "low_confidence_max_alpha_abs": 0.3,
        }
        self.alpha_map = dict(REGIME_ALPHA_MAP)


class _FakeKnowledge:
    def __init__(self):
        self.drift_cfg = {}


def _calibration(*adjustments):
    return {"calibration": {"adjustments": list(adjustments)}}


def _decay_adj(new=0.03):
    return {"param": "evidence.decay_per_cycle", "old": 0.01, "new": new,
            "reason": "test"}


def test_apply_calibration_decay_updates_accumulator(tmp_path, capsys):
    review, sm = _make_review(tmp_path, with_evidence=True)
    review.engine = _FakeEngine()
    review.knowledge = _FakeKnowledge()

    applied = review.apply_calibration(_calibration(_decay_adj(0.03)))

    assert review.evidence.decay == pytest.approx(0.03)
    assert review.engine.evidence_cfg["decay_per_cycle"] == pytest.approx(0.03)
    assert review.knowledge.drift_cfg["decay_per_cycle"] == pytest.approx(0.03)
    assert applied == ["evidence.decay_per_cycle: 0.01 → 0.03"]
    out = capsys.readouterr().out
    assert "✗" not in out


def test_apply_calibration_decay_clamped_and_effective(tmp_path):
    review, _ = _make_review(tmp_path, with_evidence=True)
    review.engine = _FakeEngine()
    review.knowledge = _FakeKnowledge()

    review.apply_calibration(_calibration(_decay_adj(0.5)))

    assert review.evidence.decay == pytest.approx(0.05)   # clamp 上界
    assert review.engine.evidence_cfg["decay_per_cycle"] == pytest.approx(0.05)


def test_apply_calibration_decay_without_evidence_skips_safely(tmp_path, capsys):
    review, _ = _make_review(tmp_path, with_evidence=False)
    review.engine = _FakeEngine()
    review.knowledge = _FakeKnowledge()

    applied = review.apply_calibration(_calibration(_decay_adj(0.03)))

    assert applied == []
    assert review.engine.evidence_cfg["decay_per_cycle"] == pytest.approx(0.01)
    assert review.knowledge.drift_cfg == {}
    out = capsys.readouterr().out
    assert "证据累加器未注入, 跳过" in out


def test_apply_calibration_other_branches_regression(tmp_path, capsys):
    review, _ = _make_review(tmp_path, with_evidence=True)
    engine = _FakeEngine()
    review.engine = engine
    review.knowledge = _FakeKnowledge()
    original_bull_days = REGIME_EXPECTED_DAYS["BULL"]
    try:
        applied = review.apply_calibration(_calibration(
            {"param": "smoothing.max_change_per_step", "old": 0.05, "new": 0.08,
             "reason": "r"},
            {"param": "evidence.min_total_score_for_regime_change", "old": 2.0,
             "new": 1.6, "reason": "r"},
            {"param": "confidence_gate.low_confidence_blocks_regime_change",
             "old": True, "new": False, "reason": "r"},
            {"param": "regime_expected_days.BULL", "old": original_bull_days,
             "new": 400, "reason": "r"},
            {"param": "regime_alpha_map.DEEP_BULL", "old": 0.3, "new": 0.25,
             "reason": "r"},
            {"param": "none", "reason": "r"},
            {"param": "unknown.param", "new": 1, "reason": "r"},
        ))
        bull_days_after = REGIME_EXPECTED_DAYS["BULL"]
    finally:
        REGIME_EXPECTED_DAYS["BULL"] = original_bull_days

    assert engine.smoothing["max_change_per_step"] == pytest.approx(0.08)
    assert engine.evidence_cfg["min_total_score_for_regime_change"] == pytest.approx(1.6)
    assert engine.conf_gate["low_confidence_blocks_regime_change"] is False
    assert bull_days_after == 400
    assert engine.alpha_map["DEEP_BULL"] == pytest.approx(0.25)
    assert len(applied) == 5
    out = capsys.readouterr().out
    assert "未知参数: unknown.param" in out
