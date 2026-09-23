"""WP2 ④: 质量门槛 + 连续同向确认 — 离线单元测试。"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src import ma_context  # noqa: E402
from src.alpha import (print_status, run_backfill, run_cycle,  # noqa: E402
                       run_first_analysis)
from src.alpha_engine import AlphaEngine, EvidenceAccumulator  # noqa: E402
from src.analyzer import Analyzer  # noqa: E402
from src.state_manager import StateManager  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_ma_cache():
    ma_context.clear_cache()
    yield
    ma_context.clear_cache()

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


def _analysis(cp="BULL", conf="high", quality=8, progress=0.82,
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
                progress=0.40, cooldown=0, alpha_cfg=None):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", regime)
    sm.set("regime.entered_from", "BEAR_BOTTOM")
    sm.set("regime.cooldown_remaining", cooldown)
    sm.set("alpha.current", alpha)
    sm.set("alpha.target", alpha)
    sm.set("alpha.regime_progress", progress)
    engine = AlphaEngine(alpha_cfg or {}, sm)
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


def _pending(sm):
    return sm.get("regime.pending_proposal", {})


# ---- 连续同向确认 (默认 2) ----

def test_same_direction_needs_two_confirmations(tmp_path, capsys):
    components = _components(tmp_path, _analysis(cp="BULL"))
    sm, engine = components["state"], components["engine"]

    run_cycle(components)
    out = capsys.readouterr().out
    assert engine.get_regime() == "RECOVERY"
    assert "待确认 1/2" in out
    assert _pending(sm)["cp"] == "BULL"
    assert _pending(sm)["count"] == 1
    assert _pending(sm)["first_at"]
    assert _pending(sm)["last_at"]

    run_cycle(components)
    out = capsys.readouterr().out
    assert "[REGIME] RECOVERY → BULL" in out
    assert engine.get_regime() == "BULL"
    assert _pending(sm) == {}


def test_direction_change_resets_pending(tmp_path, capsys):
    components = _components(tmp_path, _analysis(cp="BULL"))
    sm = components["state"]

    run_cycle(components)
    assert _pending(sm)["count"] == 1

    components["analyzer"].result = _analysis(cp="BEAR_BOTTOM")
    run_cycle(components)
    out = capsys.readouterr().out
    assert _pending(sm)["cp"] == "BEAR_BOTTOM"
    assert _pending(sm)["count"] == 1
    assert components["engine"].get_regime() == "RECOVERY"
    assert "待确认 1/2" in out


def test_idle_does_not_touch_pending(tmp_path):
    components = _components(tmp_path, _analysis(cp="BULL"))
    sm = components["state"]

    run_cycle(components)
    before = dict(_pending(sm))
    assert before["count"] == 1

    components["fetcher"] = _IdleFetcher()
    run_cycle(components)

    assert _pending(sm) == before
    assert components["engine"].get_regime() == "RECOVERY"


def test_rejected_gate_keeps_pending_count(tmp_path, capsys):
    alpha_cfg = {"smoothing": {},
                 "stability": {"required_confirmations": 1}}
    components = _components(tmp_path, _analysis(cp="BULL"),
                             cooldown=3, alpha_cfg=alpha_cfg)
    sm, engine = components["state"], components["engine"]

    run_cycle(components)
    out = capsys.readouterr().out
    assert "请求 BULL 被拒绝" in out
    assert "冷却期剩余 3 轮" in out
    assert _pending(sm)["count"] == 1
    assert engine.get_regime() == "RECOVERY"

    run_cycle(components)
    out = capsys.readouterr().out
    assert "冷却期剩余 2 轮" in out
    assert _pending(sm)["count"] == 2
    assert engine.get_regime() == "RECOVERY"


def test_proposal_back_to_current_clears_pending(tmp_path):
    components = _components(tmp_path, _analysis(cp="BULL"))
    sm = components["state"]

    run_cycle(components)
    assert _pending(sm)["count"] == 1

    components["analyzer"].result = _analysis(cp="RECOVERY")
    run_cycle(components)

    assert _pending(sm) == {}


def test_required_one_matches_legacy_behavior(tmp_path, capsys):
    alpha_cfg = {"smoothing": {},
                 "stability": {"required_confirmations": 1}}
    components = _components(tmp_path, _analysis(cp="BULL"), alpha_cfg=alpha_cfg)

    run_cycle(components)
    out = capsys.readouterr().out

    assert "[REGIME] RECOVERY → BULL" in out
    assert components["engine"].get_regime() == "BULL"
    assert _pending(components["state"]) == {}


# ---- 首轮/回溯豁免确认 ----

def _first_components(tmp_path, analysis, alpha_cfg):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    return {
        "cfg": {"schedule": {"min_analysis_interval_hours": 0}},
        "memory": _FakeMemory(),
        "state": sm,
        "fetcher": _FakeFetcher(),
        "analyzer": _FakeAnalyzer(analysis),
        "engine": AlphaEngine(alpha_cfg, sm),
        "evidence": EvidenceAccumulator(sm, 0.02),
        "knowledge": _FakeKnowledge(),
        "dingtalk": _FakeDingTalk(),
    }


def test_first_analysis_exempt_from_confirmation(tmp_path):
    components = _first_components(
        tmp_path, _analysis(cp="RECOVERY"),
        {"stability": {"required_confirmations": 2}})

    assert run_first_analysis(components) is True

    assert components["engine"].get_regime() == "RECOVERY"
    assert _pending(components["state"]) == {}


def test_backfill_exempt_from_confirmation(tmp_path):
    components = _first_components(
        tmp_path, _analysis(cp="RECOVERY"),
        {"stability": {"required_confirmations": 2}})

    assert run_backfill(components, force=False) is True

    assert components["engine"].get_regime() == "RECOVERY"
    assert _pending(components["state"]) == {}


# ---- 质量门 ----

def test_quality_gate_rejects_with_value(tmp_path, capsys):
    alpha_cfg = {"smoothing": {},
                 "stability": {"required_confirmations": 1},
                 "evidence": {"require_quality": True,
                              "min_quality_for_regime_change": 5}}
    components = _components(tmp_path, _analysis(cp="BULL", quality=3),
                             alpha_cfg=alpha_cfg)

    run_cycle(components)
    out = capsys.readouterr().out

    assert components["engine"].get_regime() == "RECOVERY"
    assert "请求 BULL 被拒绝" in out
    assert "分析质量太低" in out
    assert "quality=3" in out
    assert "min_quality_for_regime_change=5" in out


def test_quality_gate_disabled_allows_change(tmp_path, capsys):
    alpha_cfg = {"smoothing": {},
                 "stability": {"required_confirmations": 1},
                 "evidence": {"require_quality": False,
                              "min_quality_for_regime_change": 5}}
    components = _components(tmp_path, _analysis(cp="BULL", quality=1),
                             alpha_cfg=alpha_cfg)

    run_cycle(components)
    out = capsys.readouterr().out

    assert "[REGIME] RECOVERY → BULL" in out
    assert components["engine"].get_regime() == "BULL"


@pytest.mark.parametrize("quality,min_quality,changed", [
    (4, 5, False),
    (5, 5, True),
    (6, 7, False),
    (7, 7, True),
])
def test_quality_threshold_boundary(tmp_path, quality, min_quality, changed):
    alpha_cfg = {"smoothing": {},
                 "stability": {"required_confirmations": 1},
                 "evidence": {"require_quality": True,
                              "min_quality_for_regime_change": min_quality}}
    components = _components(tmp_path, _analysis(cp="BULL", quality=quality),
                             alpha_cfg=alpha_cfg)

    run_cycle(components)

    assert (components["engine"].get_regime() == "BULL") is changed


def test_min_quality_default_and_bad_value():
    sm_state = {}
    assert AlphaEngine({"evidence": {"require_quality": True}},
                       _MemoryState(sm_state)).min_quality_for_regime_change() == 5
    assert AlphaEngine(
        {"evidence": {"min_quality_for_regime_change": "bad"}},
        _MemoryState(sm_state)).min_quality_for_regime_change() == 5
    assert AlphaEngine(
        {"evidence": {"min_quality_for_regime_change": 7}},
        _MemoryState(sm_state)).min_quality_for_regime_change() == 7


class _MemoryState:
    def __init__(self, state):
        self.state = state

    def get(self, path, default=None):
        current = self.state
        for key in path.split("."):
            if not isinstance(current, dict):
                return default
            current = current.get(key, default)
        return current

    def set(self, path, value):
        keys = path.split(".")
        current = self.state
        for key in keys[:-1]:
            current = current.setdefault(key, {})
        current[keys[-1]] = value


# ---- 确认 API (引擎级) ----

def test_required_confirmations_default_and_config(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    assert AlphaEngine({}, sm).required_confirmations() == 2
    assert AlphaEngine(
        {"stability": {"required_confirmations": 1}},
        sm).required_confirmations() == 1
    assert AlphaEngine(
        {"stability": {"required_confirmations": 0}},
        sm).required_confirmations() == 1
    assert AlphaEngine(
        {"stability": {"required_confirmations": "bad"}},
        sm).required_confirmations() == 2


def test_execute_regime_change_clears_stale_pending(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", "RECOVERY")
    engine = AlphaEngine({}, sm)
    engine.note_regime_proposal("BULL")
    assert engine.get_pending_proposal()["count"] == 1

    engine.execute_regime_change("BULL", 0.5)

    assert engine.get_pending_proposal() == {}


def test_pending_proposal_api(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", "RECOVERY")
    engine = AlphaEngine({"stability": {"required_confirmations": 2}}, sm)

    pending = engine.note_regime_proposal("BULL")
    assert pending["cp"] == "BULL" and pending["count"] == 1
    assert not engine.proposal_ready()

    pending = engine.note_regime_proposal("BULL")
    assert pending["count"] == 2
    assert engine.proposal_ready()

    engine.clear_pending_proposal()
    assert engine.get_pending_proposal() == {}

    engine.note_regime_proposal("BULL")
    engine.note_regime_proposal("RECOVERY")
    assert engine.get_pending_proposal() == {}


# ---- N2: pending 脏值容错 ----

def test_pending_dirty_count_does_not_break(tmp_path, capsys):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", "RECOVERY")
    engine = AlphaEngine({}, sm)
    sm.set("regime.pending_proposal", {"cp": "BULL", "count": "bad"})

    assert engine.pending_count(engine.get_pending_proposal()) == 0
    assert not engine.proposal_ready()

    pending = engine.note_regime_proposal("BULL")
    assert pending["count"] == 1

    components = _components(tmp_path, _analysis(cp="BULL"))
    components["state"].set("regime.pending_proposal",
                            {"cp": "BULL", "count": {"oops": 1}})
    run_cycle(components)
    out = capsys.readouterr().out
    assert "待确认 1/2" in out
    assert components["state"].get("regime.pending_proposal")["count"] == 1


# ---- N4: --backfill 结束时清 pending (含 final_regime == current) ----

def test_backfill_clears_stale_pending_on_current_regime(tmp_path):
    components = _first_components(
        tmp_path, _analysis(cp="BEAR"),
        {"stability": {"required_confirmations": 2}})
    sm = components["state"]
    sm.set("regime.pending_proposal", {"cp": "RECOVERY", "count": 1})

    assert run_backfill(components, force=False) is True

    assert components["engine"].get_regime() == "BEAR"
    assert _pending(sm) == {}


# ---- N5: print_status 展示 pending ----

def test_print_status_shows_pending(tmp_path, capsys):
    components = _components(tmp_path, _analysis(cp="BULL"))

    print_status(components)
    out = capsys.readouterr().out
    assert "Pending" not in out

    components["engine"].note_regime_proposal("BULL")
    print_status(components)
    out = capsys.readouterr().out
    assert "Pending=BULL(1/2)" in out


# ---- B1: 结构一致性方向感知 (真实 Analyzer + run_cycle 集成) ----

def _falling_prices(count=300, base=250.0, step=0.5):
    return [base - i * step for i in range(count)]


def _klines(prices, start=date(2025, 1, 1)):
    bars = []
    for i, close in enumerate(prices):
        day = start + timedelta(days=i)
        ts = int(datetime(day.year, day.month, day.day,
                          tzinfo=timezone.utc).timestamp() * 1000)
        bars.append({"open_time": ts, "open": str(close), "high": str(close),
                     "low": str(close), "close": str(close), "volume": "1",
                     "close_time": ts + 86_399_999})
    return {"ok": True, "stale": False, "error": None, "source": "test",
            "bars": bars}


class _StructureDataFeed:
    def __init__(self, result):
        self.result = result
        self.endpoint = "http://fake:9550"
        self.symbol = "BTC/USDT"

    def get_klines(self, interval="1d", limit=None):
        return self.result

    def get_price(self):
        return None


class _StubApiAnalyzer(Analyzer):
    """真实 Analyzer (samples=1) + stub _call_api: 走交叉验证/结构检查完整路径。"""

    def __init__(self, result):
        super().__init__({"enabled": True,
                          "endpoint": "http://127.0.0.1:5010",
                          "cross_check": {"samples": 1,
                                          "structure_check": {"enabled": True}}})
        self.result = result

    def _call_api(self, user_msg, images=None):
        return dict(self.result), False


def _structure_components(tmp_path, regime, alpha, analysis):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("regime.current", regime)
    sm.set("regime.entered_from", "BEAR_BOTTOM")
    sm.set("alpha.current", alpha)
    sm.set("alpha.target", alpha)
    sm.set("alpha.regime_progress", 0.5)
    engine = AlphaEngine({
        "smoothing": {"cooldown_cycles_after_regime_change": 0},
        "stability": {"required_confirmations": 2},
    }, sm)
    return {
        "cfg": {"schedule": {"min_analysis_interval_hours": 0},
                "ma_context": {"enabled": True,
                               "history_file": str(tmp_path / "ma_history.jsonl"),
                               "kline_limit": 400, "cache_ttl_seconds": 0}},
        "memory": _FakeMemory(),
        "state": sm,
        "fetcher": _FakeFetcher(),
        "analyzer": _StubApiAnalyzer(analysis),
        "engine": engine,
        "evidence": EvidenceAccumulator(sm, 0.02),
        "knowledge": _FakeKnowledge(),
        "tradesync": _FakeTradeSync(),
        "datafeed": _StructureDataFeed(_klines(_falling_prices())),
        "dingtalk": _FakeDingTalk(),
        "review": _FakeReview(),
    }


def test_deep_bull_to_bull_cooling_executes_with_weak_structure(tmp_path, capsys):
    """B1 回归: close<SMA200 时 DEEP_BULL→BULL_COOLING (减仓/清仓) 不被降级, 2 轮确认后执行。"""
    components = _structure_components(
        tmp_path, regime="DEEP_BULL", alpha=0.30,
        analysis=_analysis(cp="BULL_COOLING"))

    run_cycle(components)
    assert "结构一致性冲突" not in capsys.readouterr().out
    assert components["engine"].get_regime() == "DEEP_BULL"
    assert _pending(components["state"])["count"] == 1

    run_cycle(components)
    out = capsys.readouterr().out
    assert "结构一致性冲突" not in out
    assert "[REGIME] DEEP_BULL → BULL_COOLING" in out
    assert components["engine"].get_regime() == "BULL_COOLING"
    assert _pending(components["state"]) == {}


def test_recovery_to_bull_blocked_by_weak_structure(tmp_path, capsys):
    """对照: close<SMA200 时 RECOVERY→BULL (增加多头暴露) 被降级为 low, 2 轮均被拦截。"""
    components = _structure_components(
        tmp_path, regime="RECOVERY", alpha=0.70,
        analysis=_analysis(cp="BULL"))

    run_cycle(components)
    run_cycle(components)
    out = capsys.readouterr().out

    assert "结构一致性冲突" in out
    assert "置信度为 low" in out
    assert components["engine"].get_regime() == "RECOVERY"
    assert _pending(components["state"])["count"] == 2


def test_bear_to_bear_deep_executes_with_weak_structure(tmp_path, capsys):
    """熊侧空头减仓回归: close<SMA200 时 BEAR→BEAR_DEEP (目标 -0.3) 不被降级, 2 轮后执行。"""
    components = _structure_components(
        tmp_path, regime="BEAR", alpha=-1.00,
        analysis=_analysis(cp="BEAR_DEEP"))

    run_cycle(components)
    assert "结构一致性冲突" not in capsys.readouterr().out
    assert components["engine"].get_regime() == "BEAR"
    assert _pending(components["state"])["count"] == 1

    run_cycle(components)
    out = capsys.readouterr().out
    assert "结构一致性冲突" not in out
    assert "[REGIME] BEAR → BEAR_DEEP" in out
    assert components["engine"].get_regime() == "BEAR_DEEP"
    assert _pending(components["state"]) == {}
