"""WP3 发单一致性钩子 — 离线单元测试 + 真实 TradeSync file 模式冒烟。

覆盖: 四路径各发一次 (regime 变更跨零 / SIDE-FIX / step / idle tick)、
不发 (无变化)、不重复 (每轮一次只发最终值)、失败容错 (异常/None)、
豁免 (first/backfill 不发; enabled=false 不触网/不写盘)。
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.alpha import (run_backfill, run_cycle,  # noqa: E402
                       run_first_analysis, _send_alpha_order_if_changed)
from src.alpha_engine import AlphaEngine, EvidenceAccumulator  # noqa: E402
from src.state_manager import StateManager  # noqa: E402
from src.tradesync import TradeSync  # noqa: E402

_PRICE = 85000.0

_TWEETS = [{"id": "1", "date": "2026-09-17T12:25:00",
            "url": "https://x.com/i/web/status/1",
            "content": "BTC ETF flow update"}]

_STRONG_SCORES = {"profitability": 0.6, "institutional": 0.6, "onchain": 0.5,
                  "derivatives": 0.4, "macro": 0.3}


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
    def __init__(self, tweets=None):
        self.tweets = _TWEETS if tweets is None else tweets

    def count_tweets(self):
        return len(self.tweets)

    def fetch_bulk(self, limit=0):
        return 0

    def fetch(self):
        return self.tweets

    def load_all_tweets(self):
        return self.tweets


class _IdleFetcher:
    def fetch(self):
        return []


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

    def audit_predictions(self, btc_price):
        return {}


class _RecordingTradeSync:
    """记录 send_order 调用; result=None 模拟"被跳过", exc 模拟发送异常。"""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def send_order(self, alpha, regime, price=None):
        self.calls.append({"alpha": alpha, "regime": regime, "price": price})
        if self.exc:
            raise self.exc
        return self.result


class _FakeDataFeed:
    def __init__(self, price=_PRICE):
        self.price = price

    def get_price(self):
        return self.price


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


def _analysis(cp="RECOVERY", conf="high", quality=8, progress=0.40,
              scores=None):
    return {
        "cycle_position": cp,
        "cycle_confidence": conf,
        "regime_progress": progress,
        "regime_evidence": "evidence",
        "summary": "summary",
        "evidence_scores": dict(scores or _STRONG_SCORES),
        "signal_board": [],
        "meta": {"analysis_quality": quality},
    }


def _components(tmp_path, analysis, regime="RECOVERY", alpha=0.70,
                progress=0.40, cooldown=0, alpha_cfg=None, tradesync=None,
                fetcher=None, price=_PRICE):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", regime)
    sm.set("regime.entered_from", "BEAR_BOTTOM")
    sm.set("regime.cooldown_remaining", cooldown)
    sm.set("alpha.current", alpha)
    sm.set("alpha.target", alpha)
    sm.set("alpha.regime_progress", progress)
    # WP6 时间语义: 预置 1 天前的自然日锚点 (旧 state 首轮仅初始化不推进)
    sm.set("runtime.last_tick_at",
           (datetime.utcnow() - timedelta(days=1)).isoformat() + "Z")
    engine = AlphaEngine(alpha_cfg or {"smoothing": {},
                                       "stability": {"required_confirmations": 1}},
                         sm)
    return {
        "cfg": {"schedule": {"min_analysis_interval_hours": 0}},
        "memory": _FakeMemory(),
        "state": sm,
        "fetcher": fetcher if fetcher is not None else _FakeFetcher(),
        "analyzer": _FakeAnalyzer(analysis),
        "engine": engine,
        "evidence": EvidenceAccumulator(sm, 0.02),
        "knowledge": _FakeKnowledge(),
        "tradesync": tradesync if tradesync is not None else _RecordingTradeSync(),
        "datafeed": _FakeDataFeed(price),
        "dingtalk": _FakeDingTalk(),
        "review": _FakeReview(),
    }


# ---- 四路径: 各发一次 ----

def test_regime_change_cross_zero_sends_close_all_once(tmp_path, capsys):
    """① regime 变更日跨零 (BEAR_BOTTOM(-0.3) → RECOVERY): alpha→0, close_all, 恰一次。"""
    components = _components(tmp_path, _analysis(cp="RECOVERY", progress=0.40),
                             regime="BEAR_BOTTOM", alpha=-0.30, progress=0.40)
    ts = components["tradesync"]

    assert run_cycle(components) is True

    assert len(ts.calls) == 1
    assert ts.calls[0]["alpha"] == pytest.approx(0.0)
    assert ts.calls[0]["regime"] == "RECOVERY"
    assert ts.calls[0]["price"] == _PRICE
    assert components["engine"].get_alpha() == pytest.approx(0.0)
    out = capsys.readouterr().out
    assert "reason=deferred_build" in out
    assert "alpha -0.3000 → +0.0000" in out


def test_regime_change_same_side_sends_target_once(tmp_path, capsys):
    """regime 变更同侧 (RECOVERY→BULL, progress 0.80): alpha 直接定位 0.44, 恰一次。"""
    components = _components(tmp_path, _analysis(cp="BULL", progress=0.80),
                             regime="RECOVERY", alpha=0.70, progress=0.40)
    ts = components["tradesync"]

    assert run_cycle(components) is True

    assert len(ts.calls) == 1
    assert ts.calls[0]["alpha"] == pytest.approx(0.44)
    assert ts.calls[0]["regime"] == "BULL"
    assert components["engine"].get_regime() == "BULL"
    assert components["engine"].get_alpha() == pytest.approx(0.44)
    out = capsys.readouterr().out
    assert "reason=regime_change" in out


def test_side_fix_sends_final_alpha_once(tmp_path, capsys):
    """② SIDE-FIX (BEAR_DEEP 持多 +0.2, progress=1.0 → target 0): alpha→0, close_all, 恰一次。"""
    components = _components(tmp_path, _analysis(cp="BEAR_DEEP", progress=1.0),
                             regime="BEAR_DEEP", alpha=0.20, progress=1.0)
    ts = components["tradesync"]

    assert run_cycle(components) is True

    assert len(ts.calls) == 1
    assert ts.calls[0]["alpha"] == pytest.approx(0.0)
    assert ts.calls[0]["regime"] == "BEAR_DEEP"
    assert components["engine"].get_alpha() == pytest.approx(0.0)
    out = capsys.readouterr().out
    assert "reason=side_fix" in out
    assert "[SIDE-FIX]" in out


def test_step_sends_new_alpha_once(tmp_path, capsys):
    """③ 正常 step (RECOVERY 0.70, progress 0.40→0.80): 步进到 0.72, 恰一次, reason=step。"""
    components = _components(tmp_path, _analysis(cp="RECOVERY", progress=0.80),
                             regime="RECOVERY", alpha=0.70, progress=0.40)
    ts = components["tradesync"]

    assert run_cycle(components) is True

    assert len(ts.calls) == 1
    assert ts.calls[0]["alpha"] == pytest.approx(0.72)
    assert ts.calls[0]["regime"] == "RECOVERY"
    assert components["engine"].get_alpha() == pytest.approx(0.72)
    out = capsys.readouterr().out
    assert "reason=step" in out


def test_idle_tick_sends_new_alpha_once(tmp_path, capsys):
    """④ idle tick (无新推文, 时间推进 0.70→0.72): 恰一次, reason=idle_tick。"""
    components = _components(tmp_path, _analysis(), regime="RECOVERY",
                             alpha=0.70, progress=0.40, fetcher=_IdleFetcher())
    ts = components["tradesync"]

    assert run_cycle(components) is False

    assert len(ts.calls) == 1
    assert ts.calls[0]["alpha"] == pytest.approx(0.72)
    assert ts.calls[0]["regime"] == "RECOVERY"
    assert components["engine"].get_alpha() == pytest.approx(0.72)
    out = capsys.readouterr().out
    assert "reason=idle_tick" in out


# ---- 不发: alpha 无变化 ----

def test_low_confidence_lock_no_send(tmp_path, capsys):
    """低置信锁定: progress 落盘但本轮步进被锁, alpha 不变 → 不发单。"""
    components = _components(tmp_path, _analysis(cp="RECOVERY", conf="low",
                                                 progress=0.80),
                             regime="RECOVERY", alpha=0.70, progress=0.40)
    ts = components["tradesync"]

    run_cycle(components)

    assert ts.calls == []
    assert components["state"].get("alpha.regime_progress") == pytest.approx(0.80)
    assert components["engine"].get_alpha() == pytest.approx(0.70)
    out = capsys.readouterr().out
    assert "置信度 low" in out
    assert "[TradeSync]" not in out


def test_already_at_target_no_send(tmp_path, capsys):
    """已达标: alpha == target, step_alpha 无变化 → 不发单。"""
    components = _components(tmp_path, _analysis(cp="RECOVERY", progress=0.40),
                             regime="RECOVERY", alpha=0.82, progress=0.40)
    ts = components["tradesync"]

    run_cycle(components)

    assert ts.calls == []
    assert components["engine"].get_alpha() == pytest.approx(0.82)
    assert "[TradeSync]" not in capsys.readouterr().out


def test_idle_neutral_no_change_no_send(tmp_path, capsys):
    """idle 且中性确认位: tick_alpha 冻结不动 → 不发单。"""
    components = _components(tmp_path, _analysis(), regime="BEAR_BOTTOM",
                             alpha=-0.30, progress=0.40, fetcher=_IdleFetcher())
    ts = components["tradesync"]

    assert run_cycle(components) is False

    assert ts.calls == []
    assert components["engine"].get_alpha() == pytest.approx(-0.30)
    assert "[TradeSync]" not in capsys.readouterr().out


# ---- 失败容错 ----

def test_send_exception_does_not_break_cycle(tmp_path, capsys):
    """send_order 抛异常 → 主流程继续, alpha 已推进, 有日志。"""
    ts = _RecordingTradeSync(exc=RuntimeError("boom"))
    components = _components(tmp_path, _analysis(cp="RECOVERY", progress=0.80),
                             regime="RECOVERY", alpha=0.70, progress=0.40,
                             tradesync=ts)

    assert run_cycle(components) is True

    assert len(ts.calls) == 1
    assert components["engine"].get_alpha() == pytest.approx(0.72)
    assert components["state"].get("alpha.current") == pytest.approx(0.72)
    out = capsys.readouterr().out
    assert "发送异常" in out
    assert "boom" in out
    assert "reason=step" in out


def test_send_none_does_not_break_cycle(tmp_path, capsys):
    """send_order 返回 None (未启用/去重) → 主流程继续, 有日志。"""
    ts = _RecordingTradeSync(result=None)
    components = _components(tmp_path, _analysis(cp="RECOVERY", progress=0.80),
                             regime="RECOVERY", alpha=0.70, progress=0.40,
                             tradesync=ts)

    assert run_cycle(components) is True

    assert len(ts.calls) == 1
    assert components["engine"].get_alpha() == pytest.approx(0.72)
    out = capsys.readouterr().out
    assert "未生效" in out
    assert "reason=step" in out


# ---- 钩子单元行为 (epsilon) ----

def _memory_engine(tmp_path, alpha=0.5, regime="BULL"):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("alpha.current", alpha)
    sm.set("regime.current", regime)
    return AlphaEngine({}, sm)


def test_helper_skips_below_epsilon(tmp_path):
    engine = _memory_engine(tmp_path, alpha=0.5)
    ts = _RecordingTradeSync()

    assert _send_alpha_order_if_changed(engine, ts, 0.5, _PRICE,
                                        reason="step") is None
    assert _send_alpha_order_if_changed(engine, ts, 0.5 + 1e-12, _PRICE,
                                        reason="step") is None
    assert ts.calls == []


def test_helper_sends_above_epsilon(tmp_path):
    engine = _memory_engine(tmp_path, alpha=0.5, regime="BULL")
    ts = _RecordingTradeSync(result={"ok": True})

    order = _send_alpha_order_if_changed(engine, ts, 0.5 - 1e-8, _PRICE,
                                         reason="step")

    assert order == {"ok": True}
    assert len(ts.calls) == 1
    assert ts.calls[0]["alpha"] == pytest.approx(0.5)
    assert ts.calls[0]["regime"] == "BULL"
    assert ts.calls[0]["price"] == _PRICE


# ---- 豁免: first / backfill 不发单 ----

def test_first_analysis_exempt_from_order(tmp_path):
    ts = _RecordingTradeSync()
    components = _components(tmp_path, _analysis(cp="RECOVERY"),
                             tradesync=ts, alpha=0.70)
    components["state"].set("runtime.analysis_count", 0)

    assert run_first_analysis(components) is True

    assert ts.calls == []
    assert components["engine"].get_regime() == "RECOVERY"


def test_backfill_exempt_from_order(tmp_path):
    ts = _RecordingTradeSync()
    components = _components(tmp_path, _analysis(cp="RECOVERY"),
                             tradesync=ts, alpha=0.70)
    components["state"].set("runtime.analysis_count", 0)

    assert run_backfill(components, force=False) is True

    assert ts.calls == []


# ---- 豁免: enabled=false 不触网/不写盘 ----

def test_disabled_tradesync_no_file_no_network(tmp_path, monkeypatch, capsys):
    def _boom(*args, **kwargs):
        raise AssertionError("network should not be called when disabled")

    monkeypatch.setattr(requests, "post", _boom)
    ts = TradeSync({"enabled": False, "mode": "http",
                    "http_endpoint": "http://127.0.0.1:1/orders",
                    "output_dir": "orders"}, str(tmp_path))
    components = _components(tmp_path, _analysis(cp="RECOVERY", progress=0.80),
                             regime="RECOVERY", alpha=0.70, progress=0.40,
                             tradesync=ts)

    assert run_cycle(components) is True

    assert not (tmp_path / "orders").exists()
    assert ts._last_order is None
    out = capsys.readouterr().out
    assert "未生效" in out


# ---- 真实链路冒烟: 真实 TradeSync (file 模式) 四路径 ----

_SMOKE_SCENARIOS = {
    "regime_change_cross_zero": {
        "regime": "BEAR_BOTTOM", "alpha": -0.30, "progress": 0.40,
        "analysis": _analysis(cp="RECOVERY", progress=0.40),
        "idle": False,
        "expected": {"alpha": 0.0, "direction": "cash", "size_pct": 0.0,
                     "action": "close_all", "regime": "RECOVERY"},
    },
    "side_fix": {
        "regime": "BEAR_DEEP", "alpha": 0.20, "progress": 1.0,
        "analysis": _analysis(cp="BEAR_DEEP", progress=1.0),
        "idle": False,
        "expected": {"alpha": 0.0, "direction": "cash", "size_pct": 0.0,
                     "action": "close_all", "regime": "BEAR_DEEP"},
    },
    "step": {
        "regime": "RECOVERY", "alpha": 0.70, "progress": 0.40,
        "analysis": _analysis(cp="RECOVERY", progress=0.80),
        "idle": False,
        "expected": {"alpha": 0.72, "direction": "long", "size_pct": 72.0,
                     "action": "adjust", "regime": "RECOVERY"},
    },
    "idle_tick": {
        "regime": "RECOVERY", "alpha": 0.70, "progress": 0.40,
        "analysis": None, "idle": True,
        "expected": {"alpha": 0.72, "direction": "long", "size_pct": 72.0,
                     "action": "adjust", "regime": "RECOVERY"},
    },
}


@pytest.mark.parametrize("name", list(_SMOKE_SCENARIOS))
def test_real_tradesync_file_mode_writes_single_order(tmp_path, name):
    scenario = _SMOKE_SCENARIOS[name]
    tradesync = TradeSync({"enabled": True, "mode": "file",
                           "output_dir": "orders"}, str(tmp_path))
    components = _components(
        tmp_path, scenario["analysis"], regime=scenario["regime"],
        alpha=scenario["alpha"], progress=scenario["progress"],
        tradesync=tradesync,
        fetcher=_IdleFetcher() if scenario["idle"] else None)

    run_cycle(components)

    files = list((tmp_path / "orders").glob("*.jsonl"))
    assert len(files) == 1
    lines = [line for line in files[0].read_text(encoding="utf-8").splitlines()
             if line.strip()]
    assert len(lines) == 1
    order = json.loads(lines[0])
    expected = scenario["expected"]
    assert order["alpha"] == pytest.approx(expected["alpha"])
    assert order["regime"] == expected["regime"]
    assert order["direction"] == expected["direction"]
    assert order["size_pct"] == expected["size_pct"]
    assert order["action"] == expected["action"]
    assert order["btc_price"] == _PRICE
    assert order["timestamp"].endswith("Z")
    datetime.fromisoformat(order["timestamp"].replace("Z", "+00:00"))
