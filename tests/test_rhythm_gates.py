"""WP8 节奏门控 — 默认关闭回归 + 三门控单元 + fail-open + 组合采样 (离线, 冻结时钟)。

覆盖:
- 默认关闭: 三门控 enabled=false 时 apply_rhythm_gates 恒等; 冻结时钟多轮
  alpha/progress 轨迹与"未引入前"(直接 tick/step) 逐点一致;
- G1 熊侧门控: 未确认收复 → progress 上限; 连续 N 日确认后放行; N-1/N 边界;
  非熊 regime 不受影响; 自定义 cap/确认天数;
- G2 复苏完成门控: RECOVERY + 250SMA 斜率↑ → 1.0; 仍↓/flat → 不变; cycle 回退源;
- G3 牛市门控: progress>阈值 无释放条件 → 压回; top_risk.active / 250SMA↓ → 放行;
  ≤阈值 不受影响; 释放条件数据不完整 → fail-open;
- fail-open: ma_ctx 缺失/异常/形状非法 → 不施加门控 + 日志;
- 组合: 三门控同时开, 模拟 2022 熊市 / 2023 复苏 / 2024-2025 牛市片段,
  输出 progress/alpha 采样表 (真实合成日线 → ma_context/cycle_context 快照);
- 接线: run_cycle 空闲路径 (tick) 与分析路径 (step) 均生效, 默认关闭时零变化。
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src import ma_context  # noqa: E402
from src.alpha import (_apply_rhythm_gates_to_progress,  # noqa: E402
                       run_cycle)
from src.alpha_engine import (AlphaEngine, EvidenceAccumulator,  # noqa: E402
                              REGIME_EXPECTED_DAYS)
from src.cycle_context import compute_top_risk  # noqa: E402
from src.ma_context import compute_ma_snapshot, sma_series  # noqa: E402
from src.rhythm_gates import apply_rhythm_gates  # noqa: E402
from src.state_manager import StateManager  # noqa: E402

_T0 = datetime(2026, 9, 23, 12, 25, 0)

_ALL_ON = {
    "bear_reclaim_gate": {"enabled": True, "progress_cap": 0.5,
                          "reclaim_confirm_days": 10},
    "recovery_completion_gate": {"enabled": True},
    "bull_top_gate": {"enabled": True, "progress_threshold": 0.7},
}
_ALL_OFF = {
    "bear_reclaim_gate": {"enabled": False, "progress_cap": 0.5,
                          "reclaim_confirm_days": 10},
    "recovery_completion_gate": {"enabled": False},
    "bull_top_gate": {"enabled": False, "progress_threshold": 0.7},
}


@pytest.fixture(autouse=True)
def _clear_ma_cache():
    ma_context.clear_cache()
    yield
    ma_context.clear_cache()


def _ma_ctx(streak=None, slope250=None, slope200=None, top_active=None):
    """构造市场上下文: ma_context 快照 + 可选 cycle_context.top_risk。"""
    mas = {}
    if slope200 is not None:
        mas["sma200"] = {"slope": slope200}
    if slope250 is not None:
        mas["sma250"] = {"slope": slope250}
    snapshot = {"mas": mas}
    if streak is not None:
        snapshot["days_above_long_streak"] = streak
    ctx = {"ma_context": {"snapshot": snapshot}}
    if top_active is not None:
        ctx["cycle_context"] = {"top_risk": {"active": top_active, "enabled": True}}
    return ctx


def _engine(tmp_path, cfg=None, regime="RECOVERY", alpha=0.70, progress=0.40,
            last_tick=None):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", regime)
    sm.set("alpha.current", alpha)
    sm.set("alpha.target", alpha)
    sm.set("alpha.regime_progress", progress)
    if last_tick is not None:
        sm.set("runtime.last_tick_at", last_tick.isoformat() + "Z")
    return AlphaEngine(cfg or {}, sm), sm


# ---- 默认关闭: 恒等 + 轨迹逐点一致 (冻结时钟) ----

@pytest.mark.parametrize("regime,progress", [
    ("BEAR", 0.9), ("BEAR_DEEP", 0.9), ("RECOVERY", 0.4), ("BULL", 0.9),
    ("DEEP_BULL", 0.5), ("BULL_COOLING", 0.5), ("BEAR_BOTTOM", 0.5),
    ("INIT", 0.5),
])
def test_disabled_gates_identity(regime, progress):
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)
    gated, reasons = apply_rhythm_gates(regime, progress, ctx, _ALL_OFF)
    assert gated == progress
    assert reasons == []


def test_missing_or_empty_cfg_identity():
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)
    assert apply_rhythm_gates("BEAR", 0.9, ctx, None) == (0.9, [])
    assert apply_rhythm_gates("BULL", 0.9, ctx, {}) == (0.9, [])


@pytest.mark.parametrize("regime,alpha,progress", [
    ("BEAR", -1.0, 0.30), ("BEAR_DEEP", -0.30, 0.40),
    ("RECOVERY", 0.70, 0.30), ("BULL", 1.00, 0.50)])
def test_disabled_gates_tick_trajectory_matches_baseline(tmp_path, regime,
                                                         alpha, progress):
    """默认关闭: 空闲 tick 轨迹与"未引入前"逐点一致 (冻结时钟 12 轮)。"""
    cfg = {"smoothing": {"min_daily_step": 0.015, "max_change_per_step": 0.05}}
    gated_engine, gated_sm = _engine(tmp_path / "g", cfg, regime, alpha,
                                     progress, _T0)
    base_engine, base_sm = _engine(tmp_path / "b", cfg, regime, alpha,
                                   progress, _T0)
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)

    for i in range(1, 13):
        now = _T0 + timedelta(days=i * 7 / 3)
        current = gated_sm.get("alpha.regime_progress")
        gated, reasons = apply_rhythm_gates(regime, current, ctx, _ALL_OFF)
        if gated != current:
            gated_sm.set("alpha.regime_progress", gated)
        gated_engine.tick_alpha(now=now)
        base_engine.tick_alpha(now=now)

        assert reasons == []
        assert gated_sm.get("alpha.regime_progress") == \
            base_sm.get("alpha.regime_progress")
        assert gated_engine.get_alpha() == base_engine.get_alpha()


def test_disabled_gates_step_trajectory_matches_baseline(tmp_path):
    """默认关闭: 分析 step 轨迹与"未引入前"逐点一致 (冻结时钟 8 轮)。"""
    cfg = {"smoothing": {"min_daily_step": 0.015, "max_change_per_step": 0.05}}
    gated_engine, gated_sm = _engine(tmp_path / "g", cfg, "RECOVERY",
                                     0.70, 0.30, _T0)
    base_engine, base_sm = _engine(tmp_path / "b", cfg, "RECOVERY",
                                   0.70, 0.30, _T0)
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)

    for i in range(1, 9):
        now = _T0 + timedelta(days=i * 2.5)
        ai_progress = min(0.95, 0.30 + i * 0.05)
        gated_sm.set("alpha.regime_progress", ai_progress)
        base_sm.set("alpha.regime_progress", ai_progress)
        gated, _ = apply_rhythm_gates("RECOVERY", ai_progress, ctx, _ALL_OFF)
        if gated != ai_progress:
            gated_sm.set("alpha.regime_progress", gated)
        gated_engine.step_alpha(now=now)
        base_engine.step_alpha(now=now)

        assert gated_sm.get("alpha.regime_progress") == \
            base_sm.get("alpha.regime_progress")
        assert gated_engine.get_alpha() == base_engine.get_alpha()


def test_config_rhythm_section_defaults_off():
    """8A: config.json alpha.rhythm 三门控默认关闭 (行为默认零变化)。"""
    config = json.loads((_ALPHA_ROOT / "config.json").read_text(encoding="utf-8"))
    rhythm = config["alpha"]["rhythm"]
    assert rhythm["bear_reclaim_gate"] == {
        "enabled": False, "progress_cap": 0.5, "reclaim_confirm_days": 10}
    assert rhythm["recovery_completion_gate"] == {"enabled": False}
    assert rhythm["bull_top_gate"] == {
        "enabled": False, "progress_threshold": 0.7}
    assert "_note" in rhythm and "防过拟合" in rhythm["_note"]


# ---- G1 熊侧门控 ----

def test_g1_caps_unconfirmed_bear(capsys):
    ctx = _ma_ctx(streak=3, slope250="down", top_active=False)
    gated, reasons = apply_rhythm_gates("BEAR", 0.9, ctx, _ALL_ON)
    assert gated == 0.5
    assert "G1" in reasons[0] and "0.90 → 0.50" in reasons[0]
    assert capsys.readouterr().out == ""  # 纯函数不打印


def test_g1_caps_bear_deep_too():
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)
    gated, _ = apply_rhythm_gates("BEAR_DEEP", 0.8, ctx, _ALL_ON)
    assert gated == 0.5


def test_g1_passes_after_confirmed_reclaim():
    ctx = _ma_ctx(streak=18, slope250="down", top_active=False)
    gated, reasons = apply_rhythm_gates("BEAR", 0.9, ctx, _ALL_ON)
    assert gated == 0.9
    assert "放行" in reasons[0] and "18/10" in reasons[0]


@pytest.mark.parametrize("streak,expected", [(9, 0.5), (10, 0.9), (11, 0.9)])
def test_g1_confirmation_day_boundary(streak, expected):
    ctx = _ma_ctx(streak=streak, slope250="down", top_active=False)
    gated, _ = apply_rhythm_gates("BEAR", 0.9, ctx, _ALL_ON)
    assert gated == expected


def test_g1_within_cap_is_noop():
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)
    gated, reasons = apply_rhythm_gates("BEAR", 0.4, ctx, _ALL_ON)
    assert gated == 0.4
    assert reasons == []


def test_g1_non_bear_regimes_unaffected():
    """G1 只作用于 BEAR/BEAR_DEEP; 其它 regime 不被 G1 压回。"""
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)
    for regime in ("RECOVERY", "DEEP_BULL", "BULL_COOLING", "BEAR_BOTTOM"):
        gated, _ = apply_rhythm_gates(regime, 0.9, ctx, _ALL_ON)
        assert gated == 0.9, regime


def test_g1_custom_cap_and_confirm_days():
    cfg = {"bear_reclaim_gate": {"enabled": True, "progress_cap": 0.25,
                                 "reclaim_confirm_days": 3}}
    assert apply_rhythm_gates("BEAR", 0.8,
                              _ma_ctx(streak=2, slope250="down"), cfg)[0] == 0.25
    assert apply_rhythm_gates("BEAR", 0.8,
                              _ma_ctx(streak=3, slope250="down"), cfg)[0] == 0.8


def test_g1_bad_cap_clamped():
    cfg = {"bear_reclaim_gate": {"enabled": True, "progress_cap": 5,
                                 "reclaim_confirm_days": "bad"}}
    gated, _ = apply_rhythm_gates("BEAR", 0.9, _ma_ctx(streak=0), cfg)
    assert gated == 0.9  # cap clamp 到 1.0, confirm_days 回退 10


# ---- G2 复苏完成门控 ----

def test_g2_recovery_completion_on_slope_up():
    ctx = _ma_ctx(slope250="up", top_active=False)
    gated, reasons = apply_rhythm_gates("RECOVERY", 0.4, ctx, _ALL_ON)
    assert gated == 1.0
    assert "G2" in reasons[0] and "1.00" in reasons[0]


@pytest.mark.parametrize("slope", ["down", "flat"])
def test_g2_recovery_unchanged_when_slope_not_up(slope):
    ctx = _ma_ctx(slope250=slope, top_active=False)
    gated, reasons = apply_rhythm_gates("RECOVERY", 0.4, ctx, _ALL_ON)
    assert gated == 0.4
    assert reasons == []


def test_g2_already_complete_is_noop():
    ctx = _ma_ctx(slope250="up", top_active=False)
    gated, reasons = apply_rhythm_gates("RECOVERY", 1.0, ctx, _ALL_ON)
    assert gated == 1.0
    assert reasons == []


def test_g2_fallback_to_cycle_context_slope():
    """ma_context 缺失斜率时回退 cycle_context.trend.slope250。"""
    ctx = {"cycle_context": {"trend": {"slope250": "up"},
                             "top_risk": {"active": False}}}
    gated, _ = apply_rhythm_gates("RECOVERY", 0.3, ctx, _ALL_ON)
    assert gated == 1.0


def test_g2_non_recovery_unaffected():
    ctx = _ma_ctx(slope250="up", top_active=False)
    for regime in ("BULL", "BEAR", "BEAR_BOTTOM"):
        gated, _ = apply_rhythm_gates(regime, 0.4, ctx, _ALL_ON)
        assert gated == 0.4, regime


# ---- G3 牛市门控 ----

def test_g3_caps_above_threshold_without_release():
    ctx = _ma_ctx(slope250="up", top_active=False)
    gated, reasons = apply_rhythm_gates("BULL", 0.9, ctx, _ALL_ON)
    assert gated == 0.7
    assert "G3" in reasons[0] and "压回 0.70" in reasons[0]


def test_g3_release_by_top_risk():
    ctx = _ma_ctx(slope250="up", top_active=True)
    gated, reasons = apply_rhythm_gates("BULL", 0.9, ctx, _ALL_ON)
    assert gated == 0.9
    assert "放行" in reasons[0] and "top_risk" in reasons[0]


def test_g3_release_by_slope_down():
    ctx = _ma_ctx(slope250="down", top_active=False)
    gated, reasons = apply_rhythm_gates("BULL", 0.9, ctx, _ALL_ON)
    assert gated == 0.9
    assert "放行" in reasons[0] and "250SMA" in reasons[0]


@pytest.mark.parametrize("progress,expected", [
    (0.7, 0.7), (0.7001, 0.7), (0.6, 0.6), (0.0, 0.0)])
def test_g3_threshold_boundary(progress, expected):
    ctx = _ma_ctx(slope250="up", top_active=False)
    gated, _ = apply_rhythm_gates("BULL", progress, ctx, _ALL_ON)
    assert gated == expected


def test_g3_custom_threshold():
    cfg = {"bull_top_gate": {"enabled": True, "progress_threshold": 0.9}}
    ctx = _ma_ctx(slope250="up", top_active=False)
    assert apply_rhythm_gates("BULL", 0.8, ctx, cfg)[0] == 0.8
    assert apply_rhythm_gates("BULL", 0.95, ctx, cfg)[0] == 0.9


def test_g3_non_bull_unaffected():
    ctx = _ma_ctx(slope250="flat", top_active=False)
    for regime in ("DEEP_BULL", "RECOVERY", "BEAR"):
        gated, _ = apply_rhythm_gates(regime, 0.9, ctx, _ALL_ON)
        assert gated == 0.9, regime


# ---- fail-open (数据缺失/异常 → 不施加门控 + 日志) ----

def test_fail_open_missing_ma_ctx_for_g1():
    gated, reasons = apply_rhythm_gates("BEAR", 0.9, None, _ALL_ON)
    assert gated == 0.9
    assert "fail-open" in reasons[0]


def test_fail_open_missing_streak_for_g1():
    ctx = {"ma_context": {"snapshot": {"mas": {"sma250": {"slope": "down"}}}}}
    gated, reasons = apply_rhythm_gates("BEAR", 0.9, ctx, _ALL_ON)
    assert gated == 0.9
    assert "fail-open" in reasons[0]


def test_fail_open_missing_slope_for_g2():
    ctx = {"ma_context": {"snapshot": {"mas": {}}}}
    gated, reasons = apply_rhythm_gates("RECOVERY", 0.4, ctx, _ALL_ON)
    assert gated == 0.4
    assert "fail-open" in reasons[0]


def test_fail_open_incomplete_release_data_for_g3():
    ctx = {"cycle_context": {"top_risk": {"active": False}}}  # 斜率缺失
    gated, reasons = apply_rhythm_gates("BULL", 0.9, ctx, _ALL_ON)
    assert gated == 0.9
    assert "fail-open" in reasons[0]


@pytest.mark.parametrize("bad_ctx", [
    "bad", 3, [], {"ma_context": "bad", "cycle_context": [1]},
    {"ma_context": {"snapshot": "bad"}}, {"ma_context": {"snapshot": [1, 2]}},
])
def test_fail_open_bad_shapes_do_not_raise(bad_ctx):
    for regime in ("BEAR", "RECOVERY", "BULL"):
        gated, reasons = apply_rhythm_gates(regime, 0.9, bad_ctx, _ALL_ON)
        assert gated == 0.9
        assert reasons  # fail-open reason 而非静默


def test_fail_open_bad_progress():
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)
    gated, reasons = apply_rhythm_gates("BEAR", None, ctx, _ALL_ON)
    assert gated is None
    assert "fail-open" in reasons[0]


# ---- 接线层: 写回 state / fail-open 日志 / 空配置零开销 ----

def test_helper_applies_gate_and_writes_state(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("alpha.regime_progress", 0.9)
    components = {"cfg": {"alpha": {"rhythm": _ALL_ON}}, "state": sm}

    gated = _apply_rhythm_gates_to_progress(
        components, "BEAR", 0.9, _ma_ctx(streak=0, slope250="down"))

    assert gated == 0.5
    assert sm.get("alpha.regime_progress") == 0.5


def test_helper_noop_when_rhythm_absent(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("alpha.regime_progress", 0.9)
    for cfg in ({}, {"alpha": {}}, {"alpha": {"rhythm": {}}}, {"alpha": None}):
        components = {"cfg": cfg, "state": sm}
        assert _apply_rhythm_gates_to_progress(
            components, "BEAR", 0.9, _ma_ctx(streak=0)) == 0.9
    assert sm.get("alpha.regime_progress") == 0.9


def test_helper_fail_open_logs_and_keeps_progress(tmp_path, capsys):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("alpha.regime_progress", 0.9)
    components = {"cfg": {"alpha": {"rhythm": _ALL_ON}}, "state": sm}

    gated = _apply_rhythm_gates_to_progress(components, "BEAR", 0.9, None)

    assert gated == 0.9
    assert sm.get("alpha.regime_progress") == 0.9
    out = capsys.readouterr().out
    assert "[Rhythm]" in out and "fail-open" in out


# ---- ma_context 最小扩展: 连续站上天数 (既有键不变) ----

def _dates(count):
    return [(date(2022, 1, 1) + timedelta(days=i)).isoformat()
            for i in range(count)]


def _snapshot(prices):
    return compute_ma_snapshot(list(zip(_dates(len(prices)), prices)))


def _bear_prices():
    """2022 类比: 260 日下跌 + 40 日弱反弹 (未收复 200SMA)。"""
    return [100 - 0.15 * i for i in range(260)] + [61 + 0.05 * i for i in range(40)]


def _reclaim_prices():
    """熊市后强反弹: 连续站上 200SMA ≥ 10 日。"""
    return [100 - 0.15 * i for i in range(260)] + [61 + 0.6 * i for i in range(40)]


def _recovery_prices():
    """2023 类比: 200 日下跌后 100 日回升 (250SMA 斜率↑, 价格站上 200SMA)。"""
    return [120 - 0.2 * i for i in range(200)] + [80 + 0.8 * i for i in range(100)]


def _bull_steady_prices():
    """2024-2025 类比: 单边上升 (250SMA↑, 无顶部风险)。"""
    return [100 + 0.7 * i for i in range(300)]


def _bull_top_prices():
    """牛市见顶: 上升 + 高位平台 + 3 日破位 (距 90 日高点回撤 14%, 跌破 SMA50)。"""
    return ([100 + 0.8 * i for i in range(260)] + [307.2] * 36
            + [291.8, 276.5, 270.3, 264.2])


def test_snapshot_streak_field_added_without_removing_keys():
    bear = _snapshot(_bear_prices())
    assert bear["days_above_long_streak"] == 0
    assert bear["days_above_long_30d"] is not None
    assert bear["days_above_long_90d"] is not None

    reclaim = _snapshot(_reclaim_prices())
    assert reclaim["days_above_long_streak"] >= 10


def test_snapshot_streak_feeds_g1():
    """真实合成日线 → snapshot → G1 压回/放行 (数据链路闭环)。"""
    bear_ctx = {"ma_context": {"snapshot": _snapshot(_bear_prices())}}
    reclaim_ctx = {"ma_context": {"snapshot": _snapshot(_reclaim_prices())}}

    assert apply_rhythm_gates("BEAR", 0.9, bear_ctx, _ALL_ON)[0] == 0.5
    assert apply_rhythm_gates("BEAR", 0.9, reclaim_ctx, _ALL_ON)[0] == 0.9


def test_snapshot_streak_feeds_g2_g3():
    recovery_ctx = {"ma_context": {"snapshot": _snapshot(_recovery_prices())}}
    bull_ctx = {"ma_context": {"snapshot": _snapshot(_bull_steady_prices())},
                "cycle_context": {"top_risk": {"active": False}}}

    assert apply_rhythm_gates("RECOVERY", 0.4, recovery_ctx, _ALL_ON)[0] == 1.0
    assert apply_rhythm_gates("BULL", 0.9, bull_ctx, _ALL_ON)[0] == 0.7


# ---- 组合: 三门控同时开 (模拟 2022 熊市 / 2023 复苏 / 2024-2025 牛市) ----

def _top_risk_context(prices):
    closes = list(prices)
    sma200 = sma_series(closes, 200)
    sma50 = sma_series(closes, 50)
    top = compute_top_risk(_dates(len(closes)), closes, sma200, sma50,
                           cfg={"enabled": True})
    return {"ma_context": {"snapshot": _snapshot(prices)},
            "cycle_context": {"top_risk": top}}


def _tick_segment(tmp_path, name, regime, alpha, progress, market_state,
                  rounds=8, step_days=7.0):
    """冻结时钟空闲 tick 轨迹: (round, 门控后 progress, state progress, alpha)。"""
    engine, sm = _engine(tmp_path / name, regime=regime, alpha=alpha,
                         progress=progress, last_tick=_T0)
    delta = step_days / REGIME_EXPECTED_DAYS[regime]
    samples = []
    for i in range(1, rounds + 1):
        now = _T0 + timedelta(days=i * step_days)
        current = float(sm.get("alpha.regime_progress", 0.5))
        gated, _ = apply_rhythm_gates(regime, current, market_state, _ALL_ON)
        if gated != current:
            sm.set("alpha.regime_progress", gated)
        engine.tick_alpha(now=now)
        samples.append((i, float(gated),
                        float(sm.get("alpha.regime_progress")),
                        float(engine.get_alpha())))
    return samples, delta


def test_combined_gates_sampling_table(tmp_path, capsys):
    """三门控同时开: 各片段 progress/alpha 采样表 + 关键性质断言。"""
    bear_ctx = _top_risk_context(_bear_prices())
    reclaim_ctx = _top_risk_context(_reclaim_prices())
    recovery_ctx = _top_risk_context(_recovery_prices())
    bull_ctx = _top_risk_context(_bull_steady_prices())
    top_ctx = _top_risk_context(_bull_top_prices())

    results = {}
    results["2022熊市(未收复)"] = _tick_segment(
        tmp_path, "bear", "BEAR", -1.0, 0.40, bear_ctx)
    results["熊市(收复确认)"] = _tick_segment(
        tmp_path, "reclaim", "BEAR", -1.0, 0.40, reclaim_ctx)
    results["2023复苏(250↑)"] = _tick_segment(
        tmp_path, "recovery", "RECOVERY", 0.70, 0.30, recovery_ctx)
    results["2024-25牛市(无释放)"] = _tick_segment(
        tmp_path, "bull", "BULL", 1.00, 0.60, bull_ctx)
    results["牛市(顶部风险释放)"] = _tick_segment(
        tmp_path, "bull_top", "BULL", 1.00, 0.60, top_ctx)
    results["牛市(250↓释放)"] = _tick_segment(
        tmp_path, "bull_slope", "BULL", 1.00, 0.60,
        _ma_ctx(streak=0, slope250="down", top_active=False))

    lines = ["[Rhythm组合] 三门控同时开 — progress/alpha 采样表 "
             "(冻结时钟 7 天/轮; gate=门控后 progress, state=轮末落盘 progress)"]
    for name, (samples, _delta) in results.items():
        lines.append(f"  {name}: " + " | ".join(
            f"R{r} gate={g:.2f} state={s:.4f} alpha={a:+.3f}"
            for r, g, s, a in samples))
    print("\n".join(lines))

    bear_samples, bear_delta = results["2022熊市(未收复)"]
    assert max(g for _, g, _, _ in bear_samples) == 0.5          # G1 压回
    assert max(s for _, _, s, _ in bear_samples) <= 0.5 + bear_delta + 1e-9
    assert bear_samples[-1][2] < 0.6                             # 未累计推进

    reclaim_samples, _ = results["熊市(收复确认)"]
    assert reclaim_samples[-1][1] > 0.5                          # G1 放行

    recovery_samples, _ = results["2023复苏(250↑)"]
    assert recovery_samples[0][1] == 1.0                         # G2 完成
    assert recovery_samples[-1][3] == pytest.approx(1.0, abs=1e-3)

    bull_samples, bull_delta = results["2024-25牛市(无释放)"]
    assert max(g for _, g, _, _ in bull_samples) == 0.7          # G3 压回
    assert max(s for _, _, s, _ in bull_samples) <= 0.7 + bull_delta + 1e-9

    top_samples, _ = results["牛市(顶部风险释放)"]
    assert top_samples[-1][1] > 0.7                              # G3 放行 (top_risk)

    slope_samples, _ = results["牛市(250↓释放)"]
    assert slope_samples[-1][1] > 0.7                            # G3 放行 (斜率↓)

    assert "[Rhythm组合]" in capsys.readouterr().out


def test_combined_disabled_trajectory_reaches_full_progress(tmp_path):
    """组合对照: 同一牛市片段三门控全关 → progress 推进到 1.0 (全开时被压回 0.7)。"""
    engine, sm = _engine(tmp_path, regime="BULL", alpha=1.00, progress=0.60,
                         last_tick=_T0)
    ctx = _ma_ctx(streak=0, slope250="up", top_active=False)
    for i in range(1, 25):
        current = float(sm.get("alpha.regime_progress", 0.5))
        gated, reasons = apply_rhythm_gates("BULL", current, ctx, _ALL_OFF)
        assert gated == current and reasons == []
        engine.tick_alpha(now=_T0 + timedelta(days=i * 7))
    assert float(sm.get("alpha.regime_progress")) == 1.0


# ---- 接线: run_cycle 空闲 (tick) / 分析 (step) 路径 ----

class _Memory:
    def get_context_for_ai(self):
        return ""

    def append_alpha(self, record):
        pass

    def append_entry(self, text):
        pass

    def add_metric(self, name, value):
        pass


class _Fetcher:
    def __init__(self, tweets=None):
        self.tweets = tweets or []

    def fetch(self):
        return self.tweets


class _Analyzer:
    def __init__(self, result=None):
        self.result = result

    def analyze(self, tweets, memory_context, knowledge_base="",
                retries=1, market_state=None):
        return self.result


class _Knowledge:
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


class _TradeSync:
    def send_order(self, alpha, regime, price=None):
        return None


class _DingTalk:
    def regime_change(self, *args, **kwargs):
        pass

    def alpha_change(self, *args, **kwargs):
        pass

    def analysis(self, *args, **kwargs):
        return ""

    def alert(self, *args, **kwargs):
        pass


class _Review:
    def record_price(self, *args, **kwargs):
        pass

    def price_trend(self):
        return {}

    def should_review(self):
        return False


class _FakeDataFeed:
    def __init__(self, prices):
        self.prices = prices
        self.endpoint = "http://fake:9550"
        self.symbol = "BTC/USDT"

    def get_price(self):
        return None

    def get_klines(self, interval="1d", limit=None):
        bars = []
        for i, close in enumerate(self.prices):
            day = date(2022, 1, 1) + timedelta(days=i)
            ms = int(datetime(day.year, day.month, day.day,
                              tzinfo=timezone.utc).timestamp() * 1000)
            bars.append({"open_time": ms, "open": str(close), "high": str(close),
                         "low": str(close), "close": str(close), "volume": "1"})
        return {"ok": True, "stale": False, "error": None,
                "source": "fake", "bars": bars}


def _cycle_components(tmp_path, prices, rhythm, regime, alpha, progress,
                      tweets=None, analysis=None, extra_alpha=None):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", regime)
    sm.set("alpha.current", alpha)
    sm.set("alpha.target", alpha)
    sm.set("alpha.regime_progress", progress)
    sm.set("runtime.last_tick_at",
           (datetime.utcnow() - timedelta(days=1)).isoformat() + "Z")
    alpha_cfg = {"rhythm": rhythm}
    alpha_cfg.update(extra_alpha or {})
    cfg = {"schedule": {"min_analysis_interval_hours": 0},
           "ma_context": {"enabled": True,
                          "history_file": str(tmp_path / "h.jsonl"),
                          "kline_limit": 400, "cache_ttl_seconds": 0},
           "alpha": alpha_cfg}
    return {
        "cfg": cfg, "memory": _Memory(), "state": sm,
        "fetcher": _Fetcher(tweets),
        "analyzer": _Analyzer(analysis),
        "engine": AlphaEngine(cfg["alpha"], sm),
        "evidence": EvidenceAccumulator(sm, 0.02),
        "knowledge": _Knowledge(), "tradesync": _TradeSync(),
        "datafeed": _FakeDataFeed(prices), "dingtalk": _DingTalk(),
        "review": _Review(),
    }


_STRONG_SCORES = {"profitability": 0.6, "institutional": 0.6, "onchain": 0.5,
                  "derivatives": 0.4, "macro": 0.3}

_ONE_TWEET = [{"id": "1", "date": "2026-09-21T12:25:00",
               "url": "https://x.com/i/web/status/1", "content": "BTC ETF flow"}]


def _analysis_result(cp, conf="high", progress=0.9, quality=8):
    return {
        "cycle_position": cp, "cycle_confidence": conf,
        "regime_progress": progress, "regime_evidence": "evidence",
        "summary": "s", "evidence_scores": dict(_STRONG_SCORES),
        "signal_board": [], "meta": {"analysis_quality": quality},
    }


# ---- #2 复审修复: 变更日/低置信分支不旁路门控 ----

def test_change_day_g1_not_bypassed(tmp_path, capsys):
    """G1 开, BULL_COOLING→BEAR, AI progress 0.9 → 新 regime 语境压回 0.5 → alpha -0.65。"""
    components = _cycle_components(
        tmp_path, _bear_prices(), _ALL_ON, "BULL_COOLING", 0.0, 0.40,
        tweets=_ONE_TWEET, analysis=_analysis_result("BEAR", progress=0.9),
        extra_alpha={"stability": {"required_confirmations": 1}})

    run_cycle(components)

    out = capsys.readouterr().out
    assert "[REGIME] BULL_COOLING → BEAR" in out
    assert "G1 熊侧门控" in out and "0.90 → 0.50" in out
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["alpha"]["regime_progress"] == 0.5
    assert persisted["alpha"]["current"] == pytest.approx(-0.65)
    assert persisted["alpha"]["target"] == pytest.approx(-0.65)


def test_change_day_g1_disabled_uses_ai_progress(tmp_path, capsys):
    """对照: G1 关时变更日沿用 AI progress 0.9 → alpha -0.37 (基线行为)。"""
    components = _cycle_components(
        tmp_path, _bear_prices(), _ALL_OFF, "BULL_COOLING", 0.0, 0.40,
        tweets=_ONE_TWEET, analysis=_analysis_result("BEAR", progress=0.9),
        extra_alpha={"stability": {"required_confirmations": 1}})

    run_cycle(components)

    out = capsys.readouterr().out
    assert "[Rhythm]" not in out
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["alpha"]["regime_progress"] == 0.9
    assert persisted["alpha"]["current"] == pytest.approx(-0.37)


def test_change_day_g2_recovery_completion(tmp_path, capsys):
    """G2 开, BEAR_BOTTOM→RECOVERY + 250↑ → progress=1.0, alpha 定位 +1.0。"""
    components = _cycle_components(
        tmp_path, _recovery_prices(), _ALL_ON, "BEAR_BOTTOM", 0.0, 0.40,
        tweets=_ONE_TWEET, analysis=_analysis_result("RECOVERY", progress=0.4),
        extra_alpha={"stability": {"required_confirmations": 1}})

    run_cycle(components)

    out = capsys.readouterr().out
    assert "[REGIME] BEAR_BOTTOM → RECOVERY" in out
    assert "G2 复苏完成门控" in out
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["alpha"]["regime_progress"] == 1.0
    assert persisted["alpha"]["current"] == pytest.approx(1.0)


def test_low_confidence_branch_gates_progress(tmp_path, capsys):
    """低置信分支: cp==当前 BEAR + G1 开, AI progress 0.9 → 压回 0.5, alpha 锁定不变。"""
    components = _cycle_components(
        tmp_path, _bear_prices(), _ALL_ON, "BEAR", -1.0, 0.40,
        tweets=_ONE_TWEET,
        analysis=_analysis_result("BEAR", conf="low", progress=0.9))

    run_cycle(components)

    out = capsys.readouterr().out
    assert "置信度 low" in out
    assert "G1 熊侧门控" in out
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["alpha"]["regime_progress"] == 0.5
    assert persisted["alpha"]["current"] == pytest.approx(-1.0)


def test_low_confidence_branch_disabled_is_unchanged(tmp_path, capsys):
    """对照: 门控关时低置信分支保留 AI progress 0.9 (基线行为)。"""
    components = _cycle_components(
        tmp_path, _bear_prices(), _ALL_OFF, "BEAR", -1.0, 0.40,
        tweets=_ONE_TWEET,
        analysis=_analysis_result("BEAR", conf="low", progress=0.9))

    run_cycle(components)

    out = capsys.readouterr().out
    assert "[Rhythm]" not in out
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["alpha"]["regime_progress"] == 0.9


def test_helper_bad_progress_fail_open_no_raise(tmp_path, capsys):
    """#6 守卫: 脏 progress (非数值) 传入不抛错, fail-open 返回原值 + 日志。"""
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("alpha.regime_progress", "bad")
    components = {"cfg": {"alpha": {"rhythm": _ALL_ON}}, "state": sm}

    result = _apply_rhythm_gates_to_progress(
        components, "BEAR", sm.get("alpha.regime_progress"), _ma_ctx(streak=0))

    assert result == "bad"
    assert sm.get("alpha.regime_progress") == "bad"
    out = capsys.readouterr().out
    assert "fail-open" in out and "非数值" in out


def test_run_cycle_idle_g2_wiring(tmp_path, capsys):
    """空闲 tick 路径接线: RECOVERY + 250↑ → G2 progress=1.0。"""
    components = _cycle_components(tmp_path, _recovery_prices(), _ALL_ON,
                                   "RECOVERY", 0.70, 0.30)

    run_cycle(components)

    out = capsys.readouterr().out
    assert "G2 复苏完成门控" in out
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["alpha"]["regime_progress"] == 1.0


def test_run_cycle_idle_disabled_is_unchanged(tmp_path, capsys):
    """空闲 tick 路径默认关闭: 无 [Rhythm] 日志, progress 仅按自然日推进。"""
    components = _cycle_components(tmp_path, _recovery_prices(), _ALL_OFF,
                                   "RECOVERY", 0.70, 0.30)

    run_cycle(components)

    out = capsys.readouterr().out
    assert "[Rhythm]" not in out
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    progress = persisted["alpha"]["regime_progress"]
    assert progress == pytest.approx(0.30 + 1 / REGIME_EXPECTED_DAYS["RECOVERY"],
                                     abs=1e-3)
    assert progress < 1.0


def test_run_cycle_analysis_g1_wiring(tmp_path, capsys):
    """分析 step 路径接线: BEAR 未收复 → AI 进度 0.9 被压回 0.5。"""
    tweets = [{"id": "1", "date": "2026-09-21T12:25:00",
               "url": "https://x.com/i/web/status/1", "content": "BTC ETF flow"}]
    analysis = {
        "cycle_position": "BEAR", "cycle_confidence": "high",
        "regime_progress": 0.9, "regime_evidence": "evidence",
        "summary": "s", "evidence_scores": {},
        "signal_board": [], "meta": {"analysis_quality": 8},
    }
    components = _cycle_components(tmp_path, _bear_prices(), _ALL_ON,
                                   "BEAR", -1.0, 0.40,
                                   tweets=tweets, analysis=analysis)

    run_cycle(components)

    out = capsys.readouterr().out
    assert "G1 熊侧门控" in out
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["alpha"]["regime_progress"] == 0.5


def test_run_cycle_analysis_disabled_uses_ai_progress(tmp_path, capsys):
    """分析 step 路径默认关闭: AI 进度 0.9 原样采用 (行为零变化)。"""
    tweets = [{"id": "1", "date": "2026-09-21T12:25:00",
               "url": "https://x.com/i/web/status/1", "content": "BTC ETF flow"}]
    analysis = {
        "cycle_position": "BEAR", "cycle_confidence": "high",
        "regime_progress": 0.9, "regime_evidence": "evidence",
        "summary": "s", "evidence_scores": {},
        "signal_board": [], "meta": {"analysis_quality": 8},
    }
    components = _cycle_components(tmp_path, _bear_prices(), _ALL_OFF,
                                   "BEAR", -1.0, 0.40,
                                   tweets=tweets, analysis=analysis)

    run_cycle(components)

    out = capsys.readouterr().out
    assert "[Rhythm]" not in out
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["alpha"]["regime_progress"] == 0.9
