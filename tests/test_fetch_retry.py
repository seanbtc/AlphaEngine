"""WP7 ③: fetch 重试 (实现既有死参数) — 首败次成/两败/关闭/延迟 clamp."""
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.alpha import _fetch_retry_delay, run_cycle  # noqa: E402
from src.alpha_engine import AlphaEngine, EvidenceAccumulator  # noqa: E402
from src.state_manager import StateManager  # noqa: E402

_TWEET = {"id": "1001", "date": "2026-09-17T12:25:00",
          "url": "https://x.com/i/web/status/1001",
          "content": "BTC ETF flow update"}


class _Memory:
    def get_context_for_ai(self):
        return ""

    def append_alpha(self, record):
        pass

    def append_entry(self, text):
        pass

    def add_metric(self, name, value):
        pass


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


class _DataFeed:
    def get_price(self):
        return None


class _Review:
    def record_price(self, *args, **kwargs):
        pass

    def price_trend(self):
        return {}

    def should_review(self):
        return False


class _DingTalk:
    def regime_change(self, *args, **kwargs):
        pass

    def alpha_change(self, *args, **kwargs):
        pass

    def analysis(self, *args, **kwargs):
        return ""

    def alert(self, *args, **kwargs):
        return True


class _FlakyFetcher:
    def __init__(self, failures_before_success, tweets=None):
        self.remaining_failures = failures_before_success
        self.tweets = list(tweets or [])
        self.calls = 0

    def fetch(self):
        self.calls += 1
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            raise RuntimeError("boom")
        return [dict(t) for t in self.tweets]

    def get_tweets_by_ids(self, ids):
        return []


class _Analyzer:
    def __init__(self, result=None):
        self.result = result
        self.last_sent_tweet_ids = []
        self.last_filtered_tweet_ids = []

    def analyze(self, tweets, memory_context, knowledge_base="", retries=1,
                market_state=None):
        self.last_sent_tweet_ids = [str(t.get("id", "")) for t in tweets]
        return dict(self.result) if self.result else None


def _analysis(cp="BEAR"):
    return {
        "cycle_position": cp,
        "cycle_confidence": "high",
        "regime_progress": 0.5,
        "regime_evidence": "evidence",
        "summary": "summary",
        "evidence_scores": {"profitability": 0.4, "institutional": 0.4,
                            "onchain": 0.3, "derivatives": 0.2, "macro": 0.1},
        "signal_board": [],
        "meta": {"analysis_quality": 8},
    }


def _components(tmp_path, fetcher, analyzer, schedule):
    data_dir = str(tmp_path)
    sm = StateManager(data_dir, "state.json")
    sm.load()
    engine = AlphaEngine({"smoothing": {},
                          "stability": {"required_confirmations": 1}}, sm)
    return {
        "cfg": {"schedule": schedule},
        "data_dir": data_dir,
        "memory": _Memory(),
        "state": sm,
        "fetcher": fetcher,
        "analyzer": analyzer,
        "engine": engine,
        "evidence": EvidenceAccumulator(sm, 0.02),
        "knowledge": _Knowledge(),
        "tradesync": _TradeSync(),
        "datafeed": _DataFeed(),
        "dingtalk": _DingTalk(),
        "review": _Review(),
    }


@pytest.fixture
def slept(monkeypatch):
    calls = []
    monkeypatch.setattr("src.alpha.time.sleep", lambda seconds: calls.append(seconds))
    return calls


# ---- 延迟 clamp ----

@pytest.mark.parametrize("raw,expected", [
    (120, 30.0), (30, 30.0), (5, 5.0), (0, 0.0), (-5, 0.0),
    (None, 0.0), ("abc", 0.0), (float("nan"), 0.0), ("12.5", 12.5),
])
def test_fetch_retry_delay_clamp(raw, expected):
    assert _fetch_retry_delay(raw) == expected


# ---- 首败次成 ----

def test_first_failure_then_success_no_outage(tmp_path, capsys, slept):
    fetcher = _FlakyFetcher(failures_before_success=1, tweets=[_TWEET])
    comp = _components(tmp_path, fetcher, _Analyzer(_analysis()),
                       {"min_analysis_interval_hours": 0,
                        "retry_on_fetch_failure": True, "retry_delay_seconds": 120})

    run_cycle(comp)
    out = capsys.readouterr().out

    assert fetcher.calls == 2
    assert slept == [30.0]
    assert "[Fetch] 重试 1/1 (等待 30s) ..." in out
    assert "[Fetch] 重试成功" in out
    assert comp["state"].get("runtime.outage") == {}
    assert comp["state"].get("runtime.analysis_count") == 1


def test_zero_delay_retry_does_not_sleep(tmp_path, capsys, slept):
    fetcher = _FlakyFetcher(failures_before_success=1, tweets=[_TWEET])
    comp = _components(tmp_path, fetcher, _Analyzer(_analysis()),
                       {"min_analysis_interval_hours": 0,
                        "retry_on_fetch_failure": True, "retry_delay_seconds": 0})

    run_cycle(comp)

    assert fetcher.calls == 2
    assert slept == []
    assert comp["state"].get("runtime.outage") == {}


# ---- 两次都失败 ----

def test_double_failure_sets_outage(tmp_path, capsys, slept):
    fetcher = _FlakyFetcher(failures_before_success=2)
    comp = _components(tmp_path, fetcher, _Analyzer(None),
                       {"min_analysis_interval_hours": 0,
                        "retry_on_fetch_failure": True, "retry_delay_seconds": 30})

    run_cycle(comp)
    out = capsys.readouterr().out

    assert fetcher.calls == 2
    assert slept == [30.0]
    assert "[Fetch] 重试失败: boom" in out
    outage = comp["state"].get("runtime.outage")
    assert outage["reason"] == "fetch_error"
    assert "boom" in outage["detail"]
    # 故障轮冻结推进 (WP5 语义不变)
    assert comp["state"].get("runtime.analysis_count") == 0


# ---- 关闭重试 (默认缺省也视为关闭) ----

def test_retry_disabled_single_call(tmp_path, capsys, slept):
    fetcher = _FlakyFetcher(failures_before_success=2)
    comp = _components(tmp_path, fetcher, _Analyzer(None),
                       {"min_analysis_interval_hours": 0,
                        "retry_on_fetch_failure": False})

    run_cycle(comp)
    out = capsys.readouterr().out

    assert fetcher.calls == 1
    assert slept == []
    assert "重试" not in out
    assert comp["state"].get("runtime.outage")["reason"] == "fetch_error"


def test_retry_key_missing_defaults_disabled(tmp_path, capsys, slept):
    fetcher = _FlakyFetcher(failures_before_success=2)
    comp = _components(tmp_path, fetcher, _Analyzer(None),
                       {"min_analysis_interval_hours": 0})

    run_cycle(comp)

    assert fetcher.calls == 1
    assert slept == []
