"""WP8 8C 影子账本 + 报告工具 — 四轨迹分叉/成本/去重/隔离/报告 (离线, 冻结时钟).

覆盖:
- 初始化: 四轨迹同起点 (regime/progress/alpha), init 事件 + 净值快照;
- G1 熊市分叉 (progress 封顶 0.5, 确认收复后放行);
- G2 复苏分叉 (250SMA 斜率↑ → progress=1.0);
- G3 牛市分叉 (progress>阈值 无释放条件 → 压回);
- 引擎换挡同步: progress 取引擎落盘值 (同起点) + 跨零先平仓/deferred 次日定位;
- 成本/去重/阈值边界 (TradeSync 口径) + 净值 mark-to-market;
- 隔离: 写盘失败 (mock OSError) 不外抛; 接线层异常吞掉; 状态损坏重初始化;
- outage 轮不推进 (仅刷新锚点, 故障时长不补记);
- run_cycle 接线 (空闲/分析/失败三条路径);
- 报告工具: shadow_report 四轨迹段落/阶段归因; rhythm_report 敏感性矩阵 27 行。
"""
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.alpha import _record_shadow_cycle, run_cycle  # noqa: E402
from src.alpha_engine import AlphaEngine, EvidenceAccumulator  # noqa: E402
from src.shadow_ledger import TRACKS, ShadowLedger  # noqa: E402
from src.state_manager import StateManager  # noqa: E402
from tools.rhythm_report import main as rhythm_main  # noqa: E402
from tools.shadow_report import build_report, main as shadow_main  # noqa: E402

_T0 = datetime(2026, 9, 23, 12, 25, 0)

_SMOOTHING = {"min_daily_step": 0.015, "max_change_per_step": 0.05}
_RHYTHM = {
    "bear_reclaim_gate": {"enabled": False, "progress_cap": 0.5,
                          "reclaim_confirm_days": 10},
    "recovery_completion_gate": {"enabled": False},
    "bull_top_gate": {"enabled": False, "progress_threshold": 0.7},
}
_SHADOW = {"enabled": True, "notional": 10000, "fee_pct": 0.05,
           "slip_pct": 0.02, "rebalance_threshold_pp": 1.0}


def _make_ledger(tmp_path, regime="RECOVERY", alpha=0.70, progress=0.40,
                 shadow=None, rhythm=None):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", regime)
    sm.set("alpha.current", alpha)
    sm.set("alpha.target", alpha)
    sm.set("alpha.regime_progress", progress)
    alpha_cfg = {
        "smoothing": dict(_SMOOTHING),
        "rhythm": dict(rhythm or _RHYTHM),
        "shadow": dict(shadow if shadow is not None else _SHADOW),
    }
    engine = AlphaEngine(alpha_cfg, sm)
    ledger = ShadowLedger(alpha_cfg, str(tmp_path), engine, sm)
    return ledger, engine, sm


def _ma_ctx(streak=None, slope250=None, top_active=None, phase_code=None):
    mas = {}
    if slope250 is not None:
        mas["sma250"] = {"slope": slope250}
    snapshot = {"mas": mas}
    if streak is not None:
        snapshot["days_above_long_streak"] = streak
    context = {"top_risk": {"active": bool(top_active)}}
    if phase_code is not None:
        context["phase"] = {"code": phase_code, "label": "x"}
    return {"ma_context": {"snapshot": snapshot}, "cycle_context": context}


def _iso_utc(dt):
    return dt.isoformat() + "Z"


def _read_shadow_state(tmp_path):
    path = tmp_path / "shadow" / "state.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---- 初始化 / 停用 ----

def test_init_creates_four_tracks_same_start(tmp_path):
    ledger, _engine, _sm = _make_ledger(tmp_path, regime="RECOVERY",
                                        alpha=0.70, progress=0.40)
    assert ledger.on_cycle(now=_T0, price=86000.0,
                           market_state=_ma_ctx(streak=0, slope250="up",
                                                top_active=False)) is True
    state = _read_shadow_state(tmp_path)
    assert set(state["tracks"]) == set(TRACKS)
    for name in TRACKS:
        track = state["tracks"][name]
        assert track["regime"] == "RECOVERY"
        assert track["progress"] == 0.40
        assert track["alpha"] == 0.70
        assert track["equity"] == 10000.0
        assert track["position"] == pytest.approx(7000.0)
        assert track["direction"] == "long"
    events = _read_jsonl(tmp_path / "shadow" / "ledger.jsonl")
    assert len(events) == 4
    assert all(event["event"] == "init" for event in events)
    assert all(event["cost"] == 0.0 for event in events)
    snapshots = _read_jsonl(tmp_path / "shadow" / "equity.jsonl")
    assert len(snapshots) == 4
    assert {snap["track"] for snap in snapshots} == set(TRACKS)
    assert all(snap["equity"] == 10000.0 for snap in snapshots)


def test_disabled_writes_nothing(tmp_path):
    ledger, _engine, _sm = _make_ledger(
        tmp_path, shadow={"enabled": False})
    assert ledger.on_cycle(now=_T0, price=100.0,
                           market_state=_ma_ctx(streak=0)) is False
    assert not (tmp_path / "shadow").exists()


# ---- 三门控分叉 ----

def test_g1_diverges_in_bear(tmp_path):
    """熊市未收复: G1 轨迹 progress 封顶 0.5, baseline 先向 0 靠拢 (第 7 轮分叉)。"""
    ledger, _engine, _sm = _make_ledger(tmp_path, regime="BEAR", alpha=-1.0,
                                        progress=0.40)
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)
    ledger.on_cycle(now=_T0, price=100.0, market_state=ctx)
    series = {name: [] for name in TRACKS}
    for i in range(1, 31):
        ledger.on_cycle(now=_T0 + timedelta(days=7 * i), price=100.0,
                        market_state=ctx)
        state = _read_shadow_state(tmp_path)
        for name in TRACKS:
            series[name].append(state["tracks"][name]["alpha"])
    assert series["G1"] == series["G1_G2"] == series["G1_G2_G3"]
    first = next(index for index in range(len(series["baseline"]))
                 if abs(series["baseline"][index] - series["G1"][index]) > 1e-9)
    # 门控先于 +days: G1 稳态 progress=0.5+1步长 → 目标 -0.6383,
    # 第 22 轮到达该目标, baseline 继续向 0 → 分叉
    assert first == 21
    assert series["baseline"][-1] > series["G1"][-1]
    state = _read_shadow_state(tmp_path)
    # 空闲轮门控先于 +days: 压回 0.5 后再推进一个自然日步长 (落盘可高于 cap)
    assert state["tracks"]["G1"]["progress"] == \
        pytest.approx(0.5 + 7 / 420)
    assert state["tracks"]["baseline"]["progress"] > 0.5


def test_g1_release_after_confirmed_reclaim(tmp_path):
    """连续站上长均线 >= 10 日: G1 放行, 与 baseline 同轨迹。"""
    ledger, _engine, _sm = _make_ledger(tmp_path, regime="BEAR", alpha=-1.0,
                                        progress=0.40)
    ctx = _ma_ctx(streak=12, slope250="down", top_active=False)
    ledger.on_cycle(now=_T0, price=100.0, market_state=ctx)
    for i in range(1, 9):
        ledger.on_cycle(now=_T0 + timedelta(days=7 * i), price=100.0,
                        market_state=ctx)
    state = _read_shadow_state(tmp_path)
    assert state["tracks"]["G1"]["progress"] > 0.5
    assert state["tracks"]["G1"]["alpha"] == \
        state["tracks"]["baseline"]["alpha"]


def test_g2_diverges_in_recovery(tmp_path):
    """复苏 + 250SMA 斜率↑: G2 轨迹 progress=1.0, 第 5 轮起 alpha 领先。"""
    ledger, _engine, _sm = _make_ledger(tmp_path, regime="RECOVERY",
                                        alpha=0.70, progress=0.40)
    ctx = _ma_ctx(streak=0, slope250="up", top_active=False)
    ledger.on_cycle(now=_T0, price=100.0, market_state=ctx)
    series = {name: [] for name in TRACKS}
    for i in range(1, 9):
        ledger.on_cycle(now=_T0 + timedelta(days=7 * i), price=100.0,
                        market_state=ctx)
        state = _read_shadow_state(tmp_path)
        for name in TRACKS:
            series[name].append(state["tracks"][name]["alpha"])
    assert series["baseline"] == series["G1"]
    assert series["G1_G2"] == series["G1_G2_G3"]
    first = next(index for index in range(len(series["baseline"]))
                 if abs(series["baseline"][index]
                        - series["G1_G2"][index]) > 1e-9)
    assert first == 4  # 第 5 轮
    state = _read_shadow_state(tmp_path)
    assert state["tracks"]["G1_G2"]["progress"] == 1.0
    assert series["G1_G2"][-1] == pytest.approx(1.0)
    assert series["G1_G2"][-1] > series["baseline"][-1]


def test_g3_keeps_higher_exposure_in_bull(tmp_path):
    """牛市无释放条件: G3 轨迹 progress 压回 0.7, alpha 高于其它轨迹。"""
    ledger, _engine, _sm = _make_ledger(tmp_path, regime="BULL", alpha=1.0,
                                        progress=0.60)
    ctx = _ma_ctx(streak=0, slope250="up", top_active=False)
    ledger.on_cycle(now=_T0, price=100.0, market_state=ctx)
    for i in range(1, 41):
        ledger.on_cycle(now=_T0 + timedelta(days=7 * i), price=100.0,
                        market_state=ctx)
    state = _read_shadow_state(tmp_path)
    # 空闲轮: 压回阈值 0.7 后再 +1 个自然日步长 (轮末可略高于阈值)
    assert state["tracks"]["G1_G2_G3"]["progress"] == \
        pytest.approx(0.7 + 7 / 420)
    assert state["tracks"]["G1_G2_G3"]["alpha"] > \
        state["tracks"]["baseline"]["alpha"]
    assert state["tracks"]["baseline"]["alpha"] == \
        state["tracks"]["G1"]["alpha"] == state["tracks"]["G1_G2"]["alpha"]


# ---- 引擎换挡同步 ----

def test_engine_regime_change_syncs_same_start(tmp_path):
    """引擎换挡: 四轨迹 progress 同起点 (引擎落盘值) + 跨零先平仓/deferred。"""
    ledger, engine, _sm = _make_ledger(tmp_path, regime="BEAR", alpha=-0.15,
                                       progress=0.90)
    ledger.on_cycle(now=_T0, price=100.0,
                    market_state=_ma_ctx(streak=0, slope250="down",
                                         top_active=False))
    engine.execute_regime_change("RECOVERY", 0.72)

    ledger.on_cycle(now=_T0 + timedelta(days=3), price=100.0,
                    market_state=_ma_ctx(streak=0, slope250="up",
                                         top_active=False),
                    mode="analysis")
    state = _read_shadow_state(tmp_path)
    for name in TRACKS:
        track = state["tracks"][name]
        assert track["regime"] == "RECOVERY"
        assert track["progress"] == 0.72
        assert track["alpha"] == 0.0
        assert track["deferred_build"] is True
    events = _read_jsonl(tmp_path / "shadow" / "ledger.jsonl")
    exits = [event for event in events if event["event"] == "exit"]
    assert len(exits) == 4
    assert all("引擎换挡" in event["reason"] for event in exits)

    # 下一分析轮: progress 取引擎落盘值 (0.72, 不叠加 days) → deferred 定位
    # (G2 轨迹门控后 progress=1.0 → alpha 1.0; 其余 target = 0.7+0.3*0.72)
    ledger.on_cycle(now=_T0 + timedelta(days=6), price=100.0,
                    market_state=_ma_ctx(streak=0, slope250="up",
                                         top_active=False),
                    mode="analysis")
    state = _read_shadow_state(tmp_path)
    expected = round(0.7 + 0.3 * 0.72, 4)
    for name in ("baseline", "G1"):
        assert state["tracks"][name]["progress"] == 0.72
        assert state["tracks"][name]["alpha"] == pytest.approx(expected)
    for name in ("G1_G2", "G1_G2_G3"):
        assert state["tracks"][name]["progress"] == 1.0
        assert state["tracks"][name]["alpha"] == pytest.approx(1.0)


def test_neutral_regime_freezes_alpha(tmp_path):
    """换挡到中性位 (BEAR_BOTTOM): 保留仓位, progress 不推进。"""
    ledger, engine, _sm = _make_ledger(tmp_path, regime="BEAR_DEEP",
                                       alpha=-0.30, progress=0.80)
    ledger.on_cycle(now=_T0, price=100.0,
                    market_state=_ma_ctx(streak=0, slope250="down",
                                         top_active=False))
    engine.execute_regime_change("BEAR_BOTTOM", 0.0)
    for i in range(1, 4):
        ledger.on_cycle(now=_T0 + timedelta(days=7 * i), price=100.0,
                        market_state=_ma_ctx(streak=0, slope250="down",
                                             top_active=False))
    state = _read_shadow_state(tmp_path)
    for name in TRACKS:
        track = state["tracks"][name]
        assert track["regime"] == "BEAR_BOTTOM"
        assert track["alpha"] == pytest.approx(-0.30)
        assert track["progress"] == 0.0


# ---- 成本 / 去重 / 阈值 / 净值 ----

def test_dedup_threshold_and_cost(tmp_path):
    ledger, _engine, _sm = _make_ledger(tmp_path)
    track = {"alpha": 0.505, "position": 5000.0, "equity": 10000.0,
             "direction": "long", "size_pct": 50.0,
             "last_order": {"direction": "long", "size_pct": 50.0}}
    # 同向 +0.5pp < 1.0pp → 不记事件, 仓位不变
    assert ledger._maybe_emit("G1", track, 100.0, "4", "ts1", "x") is None
    assert track["position"] == 5000.0
    # 同向 +1.0pp (边界, >=) → 记事件
    track["alpha"] = 0.51
    event = ledger._maybe_emit("G1", track, 100.0, "4", "ts2", "x")
    assert event["event"] == "adjust"
    assert event["size_pct"] == 51.0
    assert event["notional"] == pytest.approx(100.0)
    assert event["cost"] == pytest.approx(100.0 * 0.0007)
    assert track["position"] == pytest.approx(5100.0)
    assert track["equity"] == pytest.approx(10000.0 - 0.07)
    # 清仓 → exit (cash), 按成交名义计成本
    track["alpha"] = 0.03
    event = ledger._maybe_emit("G1", track, 100.0, "4", "ts3", "x")
    assert event["event"] == "exit"
    assert event["direction"] == "cash"
    assert track["position"] == 0.0
    assert track["equity"] == pytest.approx(10000.0 - 0.07 - 5100 * 0.0007)


def test_direction_threshold_boundary(tmp_path):
    ledger, _engine, _sm = _make_ledger(tmp_path)
    track = {"alpha": 0.04, "position": 0.0, "equity": 10000.0,
             "direction": "cash", "size_pct": 0.0,
             "last_order": {"direction": "cash", "size_pct": 0.0}}
    # |alpha| <= 0.05 → cash; 方向未变且仓位差 4.0pp >= 1.0 → adjust
    event = ledger._maybe_emit("G1", track, 100.0, "", "ts1", "x")
    assert event["direction"] == "cash"
    assert event["event"] == "adjust"
    assert event["size_pct"] == 4.0
    assert track["position"] == 0.0


def test_mark_to_market_equity(tmp_path):
    ledger, _engine, _sm = _make_ledger(tmp_path)
    track = {"equity": 10000.0, "position": 10000.0, "last_price": 100.0}
    ledger._mark_to_market(track, 110.0)
    assert track["equity"] == pytest.approx(11000.0)
    ledger._mark_to_market(track, 99.0)
    assert track["equity"] == pytest.approx(11000.0 + 10000.0 * (99 / 110 - 1))
    short = {"equity": 10000.0, "position": -5000.0, "last_price": 100.0}
    ledger._mark_to_market(short, 90.0)
    assert short["equity"] == pytest.approx(10500.0)


def test_phase_recorded_in_events(tmp_path):
    ledger, _engine, _sm = _make_ledger(tmp_path, regime="BEAR", alpha=-1.0,
                                        progress=0.40)
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False, phase_code=4)
    ledger.on_cycle(now=_T0, price=100.0, market_state=ctx)
    for i in range(1, 4):
        ledger.on_cycle(now=_T0 + timedelta(days=7 * i), price=100.0,
                        market_state=ctx)
    events = _read_jsonl(tmp_path / "shadow" / "ledger.jsonl")
    adjust = [event for event in events if event["event"] == "adjust"]
    assert adjust and all(event["phase"] == "4" for event in adjust)
    snapshots = _read_jsonl(tmp_path / "shadow" / "equity.jsonl")
    assert all(snap["phase"] == "4" for snap in snapshots[-4:])


# ---- 隔离 ----

def test_write_failure_never_raises(tmp_path, monkeypatch, capsys):
    ledger, _engine, _sm = _make_ledger(tmp_path)

    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("src.shadow_ledger.atomic_write_text", boom)
    result = ledger.on_cycle(now=_T0, price=100.0,
                             market_state=_ma_ctx(streak=0))
    assert result is False
    out = capsys.readouterr().out
    assert "[Shadow]" in out and "OSError" in out


def test_corrupt_state_reinitializes(tmp_path, capsys):
    shadow_dir = tmp_path / "shadow"
    shadow_dir.mkdir()
    (shadow_dir / "state.json").write_text("{bad json", encoding="utf-8")
    ledger, _engine, _sm = _make_ledger(tmp_path)
    assert ledger.on_cycle(now=_T0, price=100.0,
                           market_state=_ma_ctx(streak=0)) is True
    state = _read_shadow_state(tmp_path)
    assert set(state["tracks"]) == set(TRACKS)
    assert "[Shadow]" in capsys.readouterr().out


def test_wiring_helper_swallows_errors(capsys):
    class _Boom:
        def on_cycle(self, **kwargs):
            raise RuntimeError("boom")

    _record_shadow_cycle({"shadow": _Boom()}, 100.0, None,
                         advance=True, mode="idle")
    out = capsys.readouterr().out
    assert "[Shadow]" in out and "boom" in out


def test_wiring_helper_without_shadow_is_noop(capsys):
    _record_shadow_cycle({}, 100.0, None, advance=True, mode="idle")
    assert capsys.readouterr().out == ""


def test_outage_round_freezes_but_resets_clock(tmp_path):
    ledger, _engine, _sm = _make_ledger(tmp_path, regime="BEAR", alpha=-1.0,
                                        progress=0.40)
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)
    ledger.on_cycle(now=_T0, price=100.0, market_state=ctx)
    # outage 轮: 不推进且不刷新锚点 (对齐引擎故障轮不 tick)
    ledger.on_cycle(now=_T0 + timedelta(days=7), price=100.0, market_state=ctx,
                    advance=False, mode="outage")
    state = _read_shadow_state(tmp_path)
    assert state["tracks"]["baseline"]["progress"] == 0.40
    assert state["tracks"]["baseline"]["alpha"] == -1.0
    assert state["tracks"]["baseline"]["last_tick_at"] == _iso_utc(_T0)
    assert state["tracks"]["baseline"]["outage_pending"] is True
    # 恢复轮: 一次性重置锚点且 days=0 (对齐 _clear_outage, 故障时长不补记)
    ledger.on_cycle(now=_T0 + timedelta(days=14), price=100.0,
                    market_state=ctx)
    state = _read_shadow_state(tmp_path)
    assert state["tracks"]["baseline"]["progress"] == 0.40
    assert state["tracks"]["baseline"]["last_tick_at"] == \
        _iso_utc(_T0 + timedelta(days=14))
    # 下一推进轮: 从恢复时刻起算 7 天
    ledger.on_cycle(now=_T0 + timedelta(days=21), price=100.0,
                    market_state=ctx)
    state = _read_shadow_state(tmp_path)
    assert state["tracks"]["baseline"]["progress"] == \
        pytest.approx(0.40 + 7 / 420)


# ---- B1 保真度: baseline 与引擎逐点一致 ----

def _assert_baseline_matches(tmp_path, sm, engine):
    """baseline 轨迹 progress/alpha 与引擎逐点一致 (浮点严格)。"""
    baseline = _read_shadow_state(tmp_path)["tracks"]["baseline"]
    assert baseline["progress"] == pytest.approx(
        float(sm.get("alpha.regime_progress")), abs=1e-12)
    assert baseline["alpha"] == pytest.approx(engine.get_alpha(), abs=1e-12)


def test_baseline_matches_engine_idle(tmp_path):
    """场景1 空闲多轮: 先门控 → 再 +days/expected, 逐点一致。"""
    ledger, engine, sm = _make_ledger(tmp_path, regime="RECOVERY",
                                      alpha=0.70, progress=0.40)
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)
    ledger.on_cycle(now=_T0, price=100.0, market_state=ctx, mode="idle")
    sm.set("runtime.last_tick_at", _iso_utc(_T0))
    _assert_baseline_matches(tmp_path, sm, engine)
    for i in range(1, 9):
        now = _T0 + timedelta(days=3 * i)
        engine.tick_alpha(now=now)
        ledger.on_cycle(now=now, price=100.0, market_state=ctx, mode="idle")
        _assert_baseline_matches(tmp_path, sm, engine)


def test_baseline_matches_engine_analysis_jumps(tmp_path):
    """场景2 分析轮: AI progress 跳变, 取引擎落盘值不叠加 days, 逐点一致。"""
    ledger, engine, sm = _make_ledger(tmp_path, regime="RECOVERY",
                                      alpha=0.70, progress=0.40)
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)
    ledger.on_cycle(now=_T0, price=100.0, market_state=ctx, mode="idle")
    sm.set("runtime.last_tick_at", _iso_utc(_T0))
    for i, ai_progress in enumerate((0.55, 0.90, 0.30, 0.80, 0.60), 1):
        now = _T0 + timedelta(days=7 * i)
        sm.set("alpha.regime_progress", ai_progress)
        engine.step_alpha(now=now)
        ledger.on_cycle(now=now, price=100.0, market_state=ctx,
                        mode="analysis")
        _assert_baseline_matches(tmp_path, sm, engine)


def test_baseline_matches_engine_regime_change_deferred(tmp_path):
    """场景3 换挡+deferred: 跨零平仓 → 次日定位, 逐点一致。"""
    ledger, engine, sm = _make_ledger(tmp_path, regime="BEAR", alpha=-1.0,
                                      progress=0.40)
    ctx_down = _ma_ctx(streak=0, slope250="down", top_active=False)
    ctx_up = _ma_ctx(streak=0, slope250="up", top_active=False)
    ledger.on_cycle(now=_T0, price=100.0, market_state=ctx_down, mode="idle")
    sm.set("runtime.last_tick_at", _iso_utc(_T0))
    t1 = _T0 + timedelta(days=3)
    engine.execute_regime_change("RECOVERY", 0.5)
    sm.set("runtime.last_tick_at", _iso_utc(t1))  # 对齐换挡锚点重置
    ledger.on_cycle(now=t1, price=100.0, market_state=ctx_up, mode="analysis")
    _assert_baseline_matches(tmp_path, sm, engine)
    t2 = _T0 + timedelta(days=7)
    engine.step_alpha(now=t2)  # deferred → 直接定位
    ledger.on_cycle(now=t2, price=100.0, market_state=ctx_up, mode="analysis")
    _assert_baseline_matches(tmp_path, sm, engine)


def test_baseline_matches_engine_outage_recovery(tmp_path):
    """场景4 outage→恢复: 故障轮不刷新锚点, 恢复轮重置+days=0, 逐点一致。"""
    ledger, engine, sm = _make_ledger(tmp_path, regime="RECOVERY",
                                      alpha=0.70, progress=0.40)
    ctx = _ma_ctx(streak=0, slope250="down", top_active=False)
    ledger.on_cycle(now=_T0, price=100.0, market_state=ctx, mode="idle")
    sm.set("runtime.last_tick_at", _iso_utc(_T0))
    ledger.on_cycle(now=_T0 + timedelta(days=7), price=100.0,
                    market_state=ctx, advance=False, mode="outage")
    assert _read_shadow_state(tmp_path)["tracks"]["baseline"][
        "last_tick_at"] == _iso_utc(_T0)
    _assert_baseline_matches(tmp_path, sm, engine)
    # 恢复轮: _clear_outage 重置锚点 → 双方 days=0 (不推进)
    t2 = _T0 + timedelta(days=10)
    sm.set("runtime.last_tick_at", _iso_utc(t2))
    engine.tick_alpha(now=t2)
    ledger.on_cycle(now=t2, price=100.0, market_state=ctx, mode="idle")
    _assert_baseline_matches(tmp_path, sm, engine)
    # 下一轮: 双方都从恢复时刻起算 7 天
    t3 = _T0 + timedelta(days=17)
    engine.tick_alpha(now=t3)
    ledger.on_cycle(now=t3, price=100.0, market_state=ctx, mode="idle")
    _assert_baseline_matches(tmp_path, sm, engine)


# ---- run_cycle 接线 ----

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

    def audit_predictions(self, btc_price):
        return {}

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
    def get_price(self):
        return 86000.0

    def get_klines(self, interval="1d", limit=None):
        return None


_STRONG_SCORES = {"profitability": 0.6, "institutional": 0.6, "onchain": 0.5,
                  "derivatives": 0.4, "macro": 0.3}
_ONE_TWEET = [{"id": "1", "date": "2026-09-21T12:25:00",
               "url": "https://x.com/i/web/status/1", "content": "BTC ETF flow"}]


def _analysis_result(cp, conf="high", progress=0.72):
    return {
        "cycle_position": cp, "cycle_confidence": conf,
        "regime_progress": progress, "regime_evidence": "evidence",
        "summary": "s", "evidence_scores": dict(_STRONG_SCORES),
        "signal_board": [], "meta": {"analysis_quality": 8},
    }


def _components(tmp_path, *, tweets=None, analysis=None, regime="RECOVERY",
                alpha=0.70, progress=0.40, shadow=None):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", regime)
    sm.set("alpha.current", alpha)
    sm.set("alpha.target", alpha)
    sm.set("alpha.regime_progress", progress)
    sm.set("runtime.last_tick_at",
           (datetime.utcnow() - timedelta(days=1)).isoformat() + "Z")
    alpha_cfg = {
        "rhythm": dict(_RHYTHM),
        "smoothing": dict(_SMOOTHING),
        "stability": {"required_confirmations": 1},
        "shadow": dict(shadow if shadow is not None else _SHADOW),
    }
    cfg = {"schedule": {"min_analysis_interval_hours": 0},
           "alpha": alpha_cfg}
    engine = AlphaEngine(alpha_cfg, sm)
    return {
        "cfg": cfg, "memory": _Memory(), "state": sm,
        "fetcher": _Fetcher(tweets), "analyzer": _Analyzer(analysis),
        "engine": engine, "evidence": EvidenceAccumulator(sm, 0.02),
        "knowledge": _Knowledge(), "tradesync": _TradeSync(),
        "datafeed": _FakeDataFeed(), "dingtalk": _DingTalk(),
        "review": _Review(),
        "shadow": ShadowLedger(alpha_cfg, str(tmp_path), engine, sm),
    }


def test_run_cycle_idle_writes_shadow_ledger(tmp_path):
    components = _components(tmp_path)
    run_cycle(components)
    state = _read_shadow_state(tmp_path)
    assert set(state["tracks"]) == set(TRACKS)
    events = _read_jsonl(tmp_path / "shadow" / "ledger.jsonl")
    assert [event["event"] for event in events] == ["init"] * 4
    snapshots = _read_jsonl(tmp_path / "shadow" / "equity.jsonl")
    assert len(snapshots) == 4
    assert all(snap["regime"] == "RECOVERY" for snap in snapshots)
    assert all(snap["price"] == 86000.0 for snap in snapshots)


def test_run_cycle_analysis_round_writes_shadow(tmp_path):
    """分析轮 (引擎换挡 RECOVERY→BULL) 轮末落盘: 首轮从引擎换挡后状态初始化。"""
    components = _components(tmp_path, tweets=_ONE_TWEET,
                             analysis=_analysis_result("BULL"),
                             regime="RECOVERY", alpha=0.70, progress=0.40)
    run_cycle(components)
    state = _read_shadow_state(tmp_path)
    expected = round(1.0 - 0.7 * 0.72, 4)  # BULL 基准 1.0, 下一位置 DEEP_BULL 0.3
    for name in TRACKS:
        track = state["tracks"][name]
        assert track["regime"] == "BULL"
        assert track["alpha"] == pytest.approx(expected)
    events = _read_jsonl(tmp_path / "shadow" / "ledger.jsonl")
    assert [event["event"] for event in events] == ["init"] * 4
    snapshots = _read_jsonl(tmp_path / "shadow" / "equity.jsonl")
    assert len(snapshots) == 4
    assert all(snap["regime"] == "BULL" for snap in snapshots)


def test_run_cycle_analysis_failed_outage_does_not_advance(tmp_path):
    components = _components(tmp_path, tweets=_ONE_TWEET, analysis=None,
                             regime="BEAR", alpha=-1.0, progress=0.40)
    run_cycle(components)
    state = _read_shadow_state(tmp_path)
    assert state["tracks"]["baseline"]["progress"] == 0.40
    assert state["tracks"]["baseline"]["alpha"] == -1.0
    events = _read_jsonl(tmp_path / "shadow" / "ledger.jsonl")
    assert [event["event"] for event in events] == ["init"] * 4


# ---- 报告工具: shadow_report ----

def _synthetic_shadow(tmp_path):
    shadow_dir = tmp_path / "shadow"
    shadow_dir.mkdir(exist_ok=True)

    def event(ts, track, kind, alpha, direction, size, cost=0.0, phase="",
              reason=""):
        return {"ts": ts, "track": track, "event": kind, "price": 100.0,
                "alpha": alpha, "size_pct": size, "direction": direction,
                "reason": reason, "phase": phase, "fee_pct": 0.05,
                "slip_pct": 0.02, "notional": abs(alpha) * 10000,
                "cost": cost}

    ledger = [event("2026-09-01T00:00:00Z", track, "init", -1.0, "short",
                    100.0, reason="初始化(同步引擎)") for track in TRACKS]
    ledger.append(event("2026-09-08T00:00:00Z", "baseline", "adjust", -0.95,
                        "short", 95.0, cost=0.35, phase="4",
                        reason="自然日推进"))
    ledger.append(event("2026-09-08T00:00:00Z", "G1", "adjust", -1.0, "short",
                        100.0, cost=0.0, phase="4",
                        reason="G1 熊侧门控: 连续站上长均线 0/10 日未确认"))
    equity = []
    for track in TRACKS:
        equity.append({"ts": "2026-09-01T00:00:00Z", "track": track,
                       "price": 100.0, "equity": 10000.0,
                       "position": -10000.0, "alpha": -1.0,
                       "regime": "BEAR", "phase": "4"})
        value = 11100.0 if track == "baseline" else 10800.0
        equity.append({"ts": "2026-09-08T00:00:00Z", "track": track,
                       "price": 90.0, "equity": value, "position": -9500.0,
                       "alpha": -0.95, "regime": "BEAR", "phase": "4"})
    with open(shadow_dir / "ledger.jsonl", "w", encoding="utf-8") as handle:
        for record in ledger:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    with open(shadow_dir / "equity.jsonl", "w", encoding="utf-8") as handle:
        for record in equity:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return ledger, equity


def test_shadow_report_sections(tmp_path):
    ledger, equity = _synthetic_shadow(tmp_path)
    report = build_report(ledger, equity, notional=10000.0)
    assert "## 1. 四轨迹当前状态" in report
    for track in TRACKS:
        assert track in report
    assert "## 2. 观察期净值曲线" in report
    assert "## 3. 按结构阶段盈亏归因" in report
    assert "④熊市" in report
    assert "## 4. 首个分叉点" in report
    assert "G1 熊侧门控" in report
    assert "## 5. 最近事件" in report
    # 阶段归因: baseline 熊市 +1,100 (11000 - 10000 - 0.35 成本... 快照差分口径)
    assert "+1,100.00" in report


def test_shadow_report_main_writes_file(tmp_path, capsys):
    _synthetic_shadow(tmp_path)
    out_file = tmp_path / "report.md"
    assert shadow_main(["--data-dir", str(tmp_path),
                        "--out", str(out_file)]) == 0
    text = out_file.read_text(encoding="utf-8")
    assert "AlphaEngine 影子账本报告" in text
    assert "baseline (无门控)" in text
    assert "已写入" in capsys.readouterr().out


def test_shadow_report_empty_data(tmp_path):
    report = build_report([], [], notional=10000.0)
    assert "暂无数据" in report


# ---- 报告工具: rhythm_report ----

def _write_synthetic_csv(path, n=1000):
    lines = ["timestamp,open,high,low,close,volume"]
    for i in range(n):
        if i < 250:
            price = 100 - 0.08 * i
        elif i < 450:
            price = 80 + 0.6 * (i - 250)
        elif i < 700:
            price = 200 - 0.4 * (i - 450)
        else:
            price = 100 + 0.5 * (i - 700)
        day = date(2020, 1, 1) + timedelta(days=i)
        lines.append(f"{day.isoformat()},{price:.2f},{price:.2f},"
                     f"{price:.2f},{price:.2f},1")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_rhythm_report_sensitivity_matrix_shape(tmp_path):
    csv_path = _write_synthetic_csv(tmp_path / "sample.csv")
    out_file = tmp_path / "rhythm.md"
    assert rhythm_main(["--csv", str(csv_path),
                        "--out", str(out_file)]) == 0
    text = out_file.read_text(encoding="utf-8")
    assert "## 1. 结构段长统计" in text
    assert "## 2. 关键里程碑" in text
    assert "## 3. 现表对照" in text
    assert "## 4. 门控参数敏感性扫描" in text
    rows = [line for line in text.splitlines()
            if re.match(r"^\| 0\.[357] \| (5|10|20) \| 0\.[678] \|", line)]
    assert len(rows) == 27
    assert "基线" in text and "参数轴稳健性" in text


def test_rhythm_report_missing_data_returns_error(tmp_path, capsys):
    assert rhythm_main(["--csv", str(tmp_path / "missing.csv")]) == 1
    assert "无可用 K 线数据" in capsys.readouterr().out
