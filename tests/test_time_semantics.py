"""WP6 时间语义 — 自然日推进 / 步长折算 / 锚表 1461 / 校准落盘 (离线, 冻结时钟)。

覆盖:
- progress/alpha 按自然日推进 (跨 3/7/30 天 = days/expected; 同日重复不重复推进;
  catchup 上限; 旧 state 首轮初始化不推进; 时间倒流不回退)
- 动态步长按自然日折算 (2.33 天 > 下限; 0.5 天取下限; 上限仍生效)
- 锚表合计=1461 且保持旧相对比例; calculate_target_alpha 各 regime 边界插值
- 校准落盘 params.json + calibration_log.jsonl → 重启加载生效; 非法文件容错;
  init_components 只读加载不写盘 (--test-ai 路径)
- 冒烟: 冻结时钟 3 次/周 × 4 周 → 与日历一致, 旧逻辑慢 7/3≈2.33×
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.alpha import init_components, print_status  # noqa: E402
from src.alpha_engine import (AlphaEngine, EvidenceAccumulator,  # noqa: E402
                              EXPECTED_DAYS_TOTAL, FORWARD_NEXT_REGIME,
                              NEUTRAL_REGIMES, REGIME_ALPHA_MAP,
                              REGIME_EXPECTED_DAYS)
from src.analyzer import SYSTEM_PROMPT  # noqa: E402
from src.params_store import (apply_overlay, clamp_param,  # noqa: E402
                              load_overlay, save_overlay)
from src.review_engine import ReviewEngine  # noqa: E402
from src.state_manager import StateManager  # noqa: E402

_T0 = datetime(2026, 9, 23, 12, 25, 0)
_OLD_DAYS = {"BEAR_BOTTOM": 180, "RECOVERY": 90, "BULL": 365,
             "DEEP_BULL": 90, "BULL_COOLING": 90, "BEAR": 365, "BEAR_DEEP": 90}


def _engine(tmp_path, cfg=None, regime="RECOVERY", alpha=0.70,
            progress=0.40, last_tick=None):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", regime)
    sm.set("alpha.current", alpha)
    sm.set("alpha.target", alpha)
    sm.set("alpha.regime_progress", progress)
    if last_tick is not None:
        sm.set("runtime.last_tick_at", last_tick.isoformat() + "Z")
    return AlphaEngine(cfg or {}, sm), sm


# ---- 锚表对齐 1461 天 ----

def test_regime_expected_days_sum_is_1461():
    assert sum(REGIME_EXPECTED_DAYS.values()) == 1461  # 4 年周期
    assert set(REGIME_EXPECTED_DAYS) == set(_OLD_DAYS)


@pytest.mark.parametrize("regime,old", list(_OLD_DAYS.items()))
def test_regime_expected_days_keeps_relative_ratio(regime, old):
    expected = old * 1461 / 1270  # 旧锚表 1270 → 1461 等比放大
    assert abs(REGIME_EXPECTED_DAYS[regime] - expected) <= 1.0
    assert 30 <= REGIME_EXPECTED_DAYS[regime] <= 500  # 校准边界


def test_config_override_expected_days(tmp_path):
    engine, _ = _engine(tmp_path, cfg={"regime_expected_days": {
        "BULL": 500, "BAD": 1, "BEAR": "bad"}})
    assert engine.expected_days["BULL"] == 500
    assert engine.expected_days["BEAR"] == REGIME_EXPECTED_DAYS["BEAR"]
    assert "BAD" not in engine.expected_days


def test_config_expected_days_clamped(tmp_path):
    """WP8 8A: config 覆盖值 clamp [20, 700] (0/负值/超大不越界, 整数与非法值处理不变)。"""
    engine, _ = _engine(tmp_path, cfg={"regime_expected_days": {
        "BULL": 0, "BEAR": -5, "DEEP_BULL": 99999, "RECOVERY": 103,
        "BULL_COOLING": "bad"}})
    assert engine.expected_days["BULL"] == 20
    assert engine.expected_days["BEAR"] == 20
    assert engine.expected_days["DEEP_BULL"] == 700
    assert engine.expected_days["RECOVERY"] == 103
    assert engine.expected_days["BULL_COOLING"] == REGIME_EXPECTED_DAYS["BULL_COOLING"]


def test_expected_days_sum_governance(tmp_path, capsys):
    """WP8 8A/#7: 构造期不告警; 显式校验 (启动路径 overlay 后) 告警一次, 不阻断。"""
    engine, _ = _engine(tmp_path)
    assert engine.expected_days_sum() == EXPECTED_DAYS_TOTAL == 1461
    assert "告警" not in capsys.readouterr().out
    assert engine.warn_if_expected_days_mismatch() == EXPECTED_DAYS_TOTAL
    assert "告警" not in capsys.readouterr().out

    engine, _ = _engine(tmp_path / "mismatch",
                        cfg={"regime_expected_days": {"BULL": 700}})
    assert engine.expected_days_sum() == EXPECTED_DAYS_TOTAL - 420 + 700
    assert "告警" not in capsys.readouterr().out  # 构造期无重复告警
    assert engine.warn_if_expected_days_mismatch() == 1741
    out = capsys.readouterr().out
    assert "告警" in out and "不阻断" in out
    assert engine.expected_days["BULL"] == 700  # 不阻断: 覆盖值仍生效


def test_print_status_regime_days_annotation(tmp_path, capsys):
    """WP8 8A/#7: --status 展示合计; 不匹配时标注 (!目标1461)。"""
    engine, sm = _engine(tmp_path)
    print_status({"state": sm, "engine": engine})
    assert f"RegimeDays={EXPECTED_DAYS_TOTAL}" in capsys.readouterr().out

    engine, sm = _engine(tmp_path / "mismatch",
                         cfg={"regime_expected_days": {"BULL": 700}})
    print_status({"state": sm, "engine": engine})
    out = capsys.readouterr().out
    assert f"RegimeDays=1741(!目标{EXPECTED_DAYS_TOTAL})" in out


def test_clamp_param_expected_days_aligned_bounds():
    """WP8 #5: 校准路径与 config 覆盖路径同边界 [20, 700]。"""
    assert clamp_param("regime_expected_days.BULL", 0) == 20
    assert clamp_param("regime_expected_days.BULL", -5) == 20
    assert clamp_param("regime_expected_days.BULL", 99999) == 700
    assert clamp_param("regime_expected_days.BULL", 420) == 420


def test_calibration_expected_days_wide_bounds_persist(tmp_path):
    """WP8 #5: 校准建议 600 生效 (旧 [30,500] 会压到 500) 并落盘。"""
    engine, sm = _engine(tmp_path)
    review = ReviewEngine({}, str(tmp_path), sm, engine, _FakeKnowledge())

    applied = review.apply_calibration({"calibration": {"adjustments": [
        {"param": "regime_expected_days.BULL", "old": 420, "new": 600,
         "reason": "test"}]}})

    assert applied == ["regime_expected_days.BULL: 420 → 600"]
    assert engine.expected_days["BULL"] == 600
    params = json.loads((tmp_path / "params.json").read_text(encoding="utf-8"))
    assert params["regime_expected_days.BULL"] == 600


# ---- 自然日推进 ----

@pytest.mark.parametrize("days,effective", [(3, 3.0), (7, 7.0), (30, 7.0)])
def test_progress_advances_by_natural_days(tmp_path, days, effective):
    """跨 3/7/30 天推进量 = min(days, catchup=7) / expected_days。"""
    engine, sm = _engine(tmp_path, last_tick=_T0 - timedelta(days=days),
                         progress=0.20)
    engine.tick_alpha(now=_T0)
    assert sm.get("alpha.regime_progress") == pytest.approx(
        0.20 + effective / REGIME_EXPECTED_DAYS["RECOVERY"])


def test_first_run_without_anchor_initializes_without_advancing(tmp_path):
    """旧 state 无 runtime.last_tick_at: 首轮仅初始化锚点, 不推进。"""
    engine, sm = _engine(tmp_path, progress=0.30, alpha=0.70)
    assert sm.get("runtime.last_tick_at", "") == ""

    new_alpha, changed = engine.tick_alpha(now=_T0)

    assert changed is False
    assert new_alpha == pytest.approx(0.70)
    assert sm.get("alpha.regime_progress") == pytest.approx(0.30)
    assert sm.get("runtime.last_tick_at") == _T0.isoformat() + "Z"

    # 之后正常按自然日推进
    engine.tick_alpha(now=_T0 + timedelta(days=3))
    assert sm.get("alpha.regime_progress") == pytest.approx(
        0.30 + 3 / REGIME_EXPECTED_DAYS["RECOVERY"])


def test_same_moment_rerun_is_idempotent(tmp_path):
    """同秒重复运行 elapsed=0 → progress/alpha 均不重复推进。"""
    engine, sm = _engine(tmp_path, last_tick=_T0 - timedelta(days=2),
                         progress=0.20, alpha=0.70)
    engine.tick_alpha(now=_T0)
    progress_1 = sm.get("alpha.regime_progress")
    alpha_1 = engine.get_alpha()

    engine.tick_alpha(now=_T0)

    assert sm.get("alpha.regime_progress") == progress_1
    assert engine.get_alpha() == alpha_1
    assert sm.get("runtime.last_tick_at") == _T0.isoformat() + "Z"


def test_same_day_rerun_does_not_double_advance(tmp_path):
    """同日稍后重跑只按实际经过时间折算 (远小于一个自然日推进量)。"""
    engine, sm = _engine(tmp_path, last_tick=_T0 - timedelta(days=2),
                         progress=0.20)
    engine.tick_alpha(now=_T0)
    progress_1 = sm.get("alpha.regime_progress")

    engine.tick_alpha(now=_T0 + timedelta(minutes=5))
    progress_2 = sm.get("alpha.regime_progress")

    assert progress_2 - progress_1 == pytest.approx(
        (5 / 1440) / REGIME_EXPECTED_DAYS["RECOVERY"])
    assert progress_2 - progress_1 < (1 / REGIME_EXPECTED_DAYS["RECOVERY"]) / 100


def test_catchup_cap_configurable(tmp_path):
    engine, sm = _engine(
        tmp_path, cfg={"time_semantics": {"max_catchup_days": 3}},
        last_tick=_T0 - timedelta(days=30), progress=0.20)
    assert engine.max_catchup_days() == 3.0

    engine.tick_alpha(now=_T0)

    assert sm.get("alpha.regime_progress") == pytest.approx(
        0.20 + 3 / REGIME_EXPECTED_DAYS["RECOVERY"])


def test_catchup_default_and_bad_values(tmp_path):
    engine, _ = _engine(tmp_path)
    assert engine.max_catchup_days() == 7.0
    bad, _ = _engine(tmp_path / "bad",
                     cfg={"time_semantics": {"max_catchup_days": "bad"}})
    assert bad.max_catchup_days() == 7.0
    zero, _ = _engine(tmp_path / "zero",
                      cfg={"time_semantics": {"max_catchup_days": -1}})
    assert zero.max_catchup_days() == 0.0


def test_clock_skew_does_not_advance_or_rewind(tmp_path):
    """时间倒流: 不推进, 锚点也不回退。"""
    engine, sm = _engine(tmp_path, last_tick=_T0, progress=0.40)

    days = engine.consume_elapsed_days(now=_T0 - timedelta(hours=1))

    assert days == 0.0
    assert sm.get("runtime.last_tick_at") == _T0.isoformat() + "Z"


def test_large_clock_rollback_resets_anchor(tmp_path):
    """建议4: 回拨超过 max_catchup_days → 重置锚点为 now, 后续正常推进。"""
    engine, sm = _engine(tmp_path, last_tick=_T0 + timedelta(days=30),
                         progress=0.20)

    assert engine.consume_elapsed_days(now=_T0) == 0.0
    assert sm.get("runtime.last_tick_at") == _T0.isoformat() + "Z"

    engine.tick_alpha(now=_T0 + timedelta(days=2))
    assert sm.get("alpha.regime_progress") == pytest.approx(
        0.20 + 2 / REGIME_EXPECTED_DAYS["RECOVERY"])


def test_small_clock_rollback_keeps_anchor(tmp_path):
    """建议4 对照: 小偏差不回退锚点 (仅不推进)。"""
    future = _T0 + timedelta(hours=1)
    engine, sm = _engine(tmp_path, last_tick=future, progress=0.20)

    assert engine.consume_elapsed_days(now=_T0) == 0.0
    assert sm.get("runtime.last_tick_at") == future.isoformat() + "Z"


def test_neutral_regime_freezes_progress_but_advances_clock(tmp_path):
    """中性确认位: progress/alpha 冻结, 但时间锚点照常前移。"""
    engine, sm = _engine(tmp_path, regime="BEAR_BOTTOM", alpha=0.0,
                         progress=0.40, last_tick=_T0 - timedelta(days=2))

    new_alpha, changed = engine.tick_alpha(now=_T0)

    assert changed is False
    assert new_alpha == pytest.approx(0.0)
    assert sm.get("alpha.regime_progress") == pytest.approx(0.40)
    assert sm.get("runtime.last_tick_at") == _T0.isoformat() + "Z"


def test_cooldown_and_stability_remain_per_round(tmp_path):
    """tick_cooldown/tick_stability 仍按轮次 (决策机会数), 不按自然日。"""
    engine, sm = _engine(tmp_path, regime="RECOVERY",
                         last_tick=_T0 - timedelta(days=7))
    sm.set("regime.cooldown_remaining", 5)

    engine.tick_alpha(now=_T0)   # 7 个自然日
    engine.tick_cooldown()
    engine.tick_stability()

    assert sm.get("regime.cooldown_remaining") == 4
    assert sm.get("regime.stability_counter") == 1


def test_regime_change_cycle_consumes_anchor(tmp_path):
    """建议5: execute_regime_change 消费锚点, 旧 regime 时间不计入新 regime。"""
    engine, sm = _engine(tmp_path, regime="BEAR", alpha=-1.0, progress=0.5,
                         last_tick=_T0 - timedelta(days=10))

    engine.execute_regime_change("BEAR_DEEP", 0.5)

    anchor = engine._parse_utc(sm.get("runtime.last_tick_at"))
    assert anchor is not None
    assert abs((datetime.utcnow() - anchor).total_seconds()) < 5

    engine.tick_alpha(now=anchor + timedelta(days=2))

    # 只记变更后 2 天 (而非旧 regime 的 10 天/catchup 7 天)
    assert sm.get("alpha.regime_progress") == pytest.approx(
        0.5 + 2 / REGIME_EXPECTED_DAYS["BEAR_DEEP"])


# ---- 动态步长按自然日折算 ----

@pytest.mark.parametrize("days,expected_step", [
    (2.33, 2.33 / REGIME_EXPECTED_DAYS["RECOVERY"]),  # 动态 > 下限 0.015 → 动态生效
    (0.5, 0.015),         # 低于下限 → 取下限
    (10.0, 0.05),         # 7 天 catchup → 7/expected≈0.068 → 上限 0.05
])
def test_dynamic_step_scales_with_natural_days(tmp_path, days, expected_step):
    cfg = {"smoothing": {"min_daily_step": 0.015, "max_change_per_step": 0.05}}
    engine, sm = _engine(tmp_path, cfg=cfg, last_tick=_T0 - timedelta(days=days),
                         alpha=0.70, progress=0.40)

    new_alpha, changed = engine.step_alpha(now=_T0)

    assert changed is True
    assert new_alpha == pytest.approx(round(0.70 + expected_step, 4), abs=1e-9)
    assert sm.get("alpha.current") == pytest.approx(new_alpha)


def test_dynamic_step_audit_number_with_90_day_override(tmp_path):
    """审计口径: 1/90×2.33≈0.0259 > 下限 0.015 (config 覆盖 90 天时)。"""
    cfg = {"smoothing": {"min_daily_step": 0.015, "max_change_per_step": 0.05},
           "regime_expected_days": {"RECOVERY": 90}}
    engine, _ = _engine(tmp_path, cfg=cfg, last_tick=_T0 - timedelta(days=2.33),
                        alpha=0.70, progress=0.40)

    new_alpha, _ = engine.step_alpha(now=_T0)

    step = new_alpha - 0.70
    assert step == pytest.approx(2.33 / 90, abs=1e-4)
    assert step > 0.015


def test_tick_alpha_uses_min_step_below_floor(tmp_path):
    cfg = {"smoothing": {"min_daily_step": 0.015, "max_change_per_step": 0.05}}
    engine, sm = _engine(tmp_path, cfg=cfg,
                         last_tick=_T0 - timedelta(days=0.5),
                         alpha=0.70, progress=0.40)

    new_alpha, changed = engine.tick_alpha(now=_T0)

    assert changed is True
    assert sm.get("alpha.regime_progress") == pytest.approx(
        0.40 + 0.5 / REGIME_EXPECTED_DAYS["RECOVERY"])
    assert new_alpha == pytest.approx(0.715)  # 下限步长


def test_step_alpha_first_run_uses_min_step(tmp_path):
    """旧 state 首轮 (无锚点, days=0): 步长取下限, 不额外推进 progress。"""
    cfg = {"smoothing": {"min_daily_step": 0.015, "max_change_per_step": 0.05}}
    engine, sm = _engine(tmp_path, cfg=cfg, alpha=0.70, progress=0.40)

    new_alpha, changed = engine.step_alpha(now=_T0)

    assert changed is True
    assert new_alpha == pytest.approx(0.715)
    assert sm.get("alpha.regime_progress") == pytest.approx(0.40)


# ---- calculate_target_alpha 插值回归 ----

@pytest.mark.parametrize("regime", list(REGIME_ALPHA_MAP))
def test_calculate_target_alpha_boundaries(tmp_path, regime):
    engine, _ = _engine(tmp_path, regime=regime)
    base = REGIME_ALPHA_MAP[regime]

    assert engine.calculate_target_alpha(regime, 0.0) == pytest.approx(base)
    if regime in NEUTRAL_REGIMES:
        assert engine.calculate_target_alpha(regime, 1.0) == pytest.approx(base)
    else:
        nxt = FORWARD_NEXT_REGIME[regime]
        assert engine.calculate_target_alpha(regime, 1.0) == pytest.approx(
            REGIME_ALPHA_MAP[nxt])
    # 越界 clamp
    assert engine.calculate_target_alpha(regime, 2.0) == \
        engine.calculate_target_alpha(regime, 1.0)
    assert engine.calculate_target_alpha(regime, -1.0) == \
        engine.calculate_target_alpha(regime, 0.0)


def test_calculate_target_alpha_bear_deep_progress(tmp_path):
    engine, _ = _engine(tmp_path, regime="BEAR_DEEP")
    # -0.30 + (0 - (-0.30)) × 0.8 = -0.06
    assert engine.calculate_target_alpha("BEAR_DEEP", 0.8) == pytest.approx(-0.06)


# ---- 校准落盘 / 重启恢复 / 容错 ----

class _FakeKnowledge:
    def __init__(self):
        self.drift_cfg = {}


def test_apply_calibration_persists_and_restart_loads(tmp_path):
    engine, sm = _engine(tmp_path, cfg={"smoothing": {"min_daily_step": 0.015}})
    evidence = EvidenceAccumulator(sm, 0.015)
    review = ReviewEngine({}, str(tmp_path), sm, engine, _FakeKnowledge(),
                          evidence=evidence)

    applied = review.apply_calibration({"calibration": {"adjustments": [
        {"param": "smoothing.min_daily_step", "old": 0.015, "new": 0.03,
         "reason": "test"},
        {"param": "regime_expected_days.BULL", "old": 420, "new": 400,
         "reason": "test"},
    ]}})

    assert applied == ["smoothing.min_daily_step: 0.015 → 0.03",
                       "regime_expected_days.BULL: 420 → 400"]
    # 内存立即生效
    assert engine.smoothing["min_daily_step"] == pytest.approx(0.03)
    assert engine.expected_days["BULL"] == 400
    # params.json overlay
    params = json.loads((tmp_path / "params.json").read_text(encoding="utf-8"))
    assert params["smoothing.min_daily_step"] == pytest.approx(0.03)
    assert params["regime_expected_days.BULL"] == 400
    # calibration_log.jsonl 审计
    lines = [line for line in (tmp_path / "calibration_log.jsonl")
             .read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    entry = json.loads(lines[0])
    assert entry["source"] == "monthly_review"
    assert entry["param"] == "smoothing.min_daily_step"
    assert entry["old"] == pytest.approx(0.015)
    assert entry["new"] == pytest.approx(0.03)
    assert entry["ts"].endswith("Z")
    assert sm.get("runtime.last_calibration_at", "").endswith("Z")

    # 模拟重启: 新 state/引擎 → 从 params.json 恢复生效
    sm2 = StateManager(str(tmp_path), "state.json")
    sm2.load()
    engine2 = AlphaEngine({}, sm2)
    assert engine2.expected_days["BULL"] == REGIME_EXPECTED_DAYS["BULL"]

    overlay = load_overlay(str(tmp_path))
    applied_overlay = apply_overlay(
        engine2, overlay, evidence=EvidenceAccumulator(sm2, 0.015))

    assert len(applied_overlay) == 2
    assert engine2.smoothing["min_daily_step"] == pytest.approx(0.03)
    assert engine2.expected_days["BULL"] == 400


def test_calibration_output_is_ascii_safe(tmp_path, capsys):
    """建议7: 校准打印 ASCII 化 (GBK 控制台不因 ✓/✗/⚠ 抛 UnicodeEncodeError)。"""
    engine, sm = _engine(tmp_path, cfg={"smoothing": {"min_daily_step": 0.015}})
    review = ReviewEngine({}, str(tmp_path), sm, engine, _FakeKnowledge())

    review.apply_calibration({"calibration": {"adjustments": [
        {"param": "smoothing.min_daily_step", "old": 0.015, "new": 0.02,
         "reason": "test"},
        {"param": "unknown.param", "new": 1, "reason": "test"},
    ]}})

    out = capsys.readouterr().out
    assert "✓" not in out and "✗" not in out and "⚠" not in out
    assert "[OK] smoothing.min_daily_step" in out
    assert "[!!]" in out
    # 输出不抛异常 → state 保存不被跳过
    assert (tmp_path / "params.json").exists()


def test_corrupt_params_json_tolerated(tmp_path, capsys):
    (tmp_path / "params.json").write_text("{not json", encoding="utf-8")
    assert load_overlay(str(tmp_path)) == {}
    assert "损坏" in capsys.readouterr().out

    (tmp_path / "params.json").write_text("[1, 2]", encoding="utf-8")
    assert load_overlay(str(tmp_path)) == {}
    assert "结构非法" in capsys.readouterr().out

    # 损坏文件不阻塞后续落盘 (覆盖为合法 JSON)
    save_overlay(str(tmp_path), {"smoothing.min_daily_step": 0.02})
    assert load_overlay(str(tmp_path)) == {"smoothing.min_daily_step": 0.02}


def test_apply_overlay_skips_unknown_and_bad_params(tmp_path, capsys):
    engine, _ = _engine(tmp_path)
    overlay = {"unknown.param": 1,
               "smoothing.min_daily_step": 0.02,
               "evidence.decay_per_cycle": 0.03}

    applied = apply_overlay(engine, overlay)

    assert applied == ["smoothing.min_daily_step=0.02"]
    assert engine.smoothing["min_daily_step"] == pytest.approx(0.02)
    out = capsys.readouterr().out
    assert "未知参数" in out
    assert "证据累加器未注入" in out


def test_apply_overlay_rejects_non_finite_values(tmp_path, capsys):
    """建议2: inf/NaN (如 JSON 1e999) → 跳过该参数 + 告警, 不阻止启动。"""
    engine, _ = _engine(tmp_path, cfg={"smoothing": {"min_daily_step": 0.015}})
    overlay = {"smoothing.min_daily_step": float("inf"),
               "regime_expected_days.BULL": 1e999,
               "smoothing.max_change_per_step": 0.05}

    applied = apply_overlay(engine, overlay)

    assert applied == ["smoothing.max_change_per_step=0.05"]
    assert engine.smoothing["min_daily_step"] == pytest.approx(0.015)
    assert engine.expected_days["BULL"] == REGIME_EXPECTED_DAYS["BULL"]
    out = capsys.readouterr().out
    assert "非有限数值" in out


def test_params_file_with_infinity_does_not_block_startup(tmp_path, capsys):
    """建议2: params.json 内 1e999 → 只读加载容错, 参数跳过且引擎值不变。"""
    (tmp_path / "params.json").write_text(
        '{"smoothing.min_daily_step": 1e999,'
        ' "regime_expected_days.BULL": 1e999}',
        encoding="utf-8")
    overlay = load_overlay(str(tmp_path))
    engine, _ = _engine(tmp_path, cfg={"smoothing": {"min_daily_step": 0.015}})

    applied = apply_overlay(engine, overlay)

    assert applied == []
    assert engine.smoothing["min_daily_step"] == pytest.approx(0.015)
    assert engine.expected_days["BULL"] == REGIME_EXPECTED_DAYS["BULL"]
    assert "非有限数值" in capsys.readouterr().out


def test_init_components_loads_overlay_read_only(tmp_path):
    """--test-ai 等入口: 只读加载 params.json 生效, 不写盘/不建审计文件。"""
    save_overlay(str(tmp_path), {"smoothing.min_daily_step": 0.03})
    before = (tmp_path / "params.json").read_text(encoding="utf-8")
    cfg = {
        "paths": {"data_dir": str(tmp_path), "state_file": "state.json"},
        "ai_service": {"enabled": False, "endpoint": "http://127.0.0.1:1"},
        "fetcher": {}, "alpha": {}, "knowledge": {}, "tradesync": {},
        "datafeed": {}, "dingtalk": {"enabled": False},
        "promo": {"enabled": False}, "review": {},
    }

    components = init_components(cfg)

    assert components["engine"].smoothing["min_daily_step"] == pytest.approx(0.03)
    assert (tmp_path / "params.json").read_text(encoding="utf-8") == before
    assert not (tmp_path / "calibration_log.jsonl").exists()


# ---- prompt 同步 (输入文案, schema 不变) ----

def test_system_prompt_matches_natural_day_semantics():
    assert "每日最多向目标移动 0.02" not in SYSTEM_PROMPT
    assert "按**自然日**推进" in SYSTEM_PROMPT
    assert "单轮上限 0.05" in SYSTEM_PROMPT
    assert "下限 0.015" in SYSTEM_PROMPT


# ---- 冒烟: 冻结时钟 3 次/周 × 4 周 → 与日历一致 (旧逻辑慢 2.33×) ----

def test_frozen_clock_three_runs_per_week_matches_calendar(tmp_path):
    cfg = {"smoothing": {"min_daily_step": 0.015, "max_change_per_step": 0.05}}
    engine, sm = _engine(tmp_path, cfg=cfg, regime="RECOVERY",
                         alpha=0.70, progress=0.0)

    engine.tick_alpha(now=_T0)  # 首轮: 初始化锚点, 不推进
    assert sm.get("alpha.regime_progress") == pytest.approx(0.0)

    interval_days = 7 / 3  # 3 次/周 ≈ 2.333 天
    for i in range(1, 13):  # 12 个间隔 = 4 周 = 28 天
        engine.tick_alpha(now=_T0 + timedelta(days=i * interval_days))

    expected_days = REGIME_EXPECTED_DAYS["RECOVERY"]
    progress = sm.get("alpha.regime_progress")
    assert progress == pytest.approx(28 / expected_days)   # = 日历天数/expected
    # alpha 轨迹与 target 一致 (步长按自然日折算)
    assert engine.get_alpha() == pytest.approx(
        engine.calculate_target_alpha("RECOVERY", progress))
    # 对照旧逻辑 (每轮 +1/expected_days): 12 轮只推进 12/expected_days
    old_progress = 12 / expected_days
    assert progress / old_progress == pytest.approx(7 / 3, rel=1e-9)
    assert old_progress < progress   # 旧逻辑慢 2.33×
