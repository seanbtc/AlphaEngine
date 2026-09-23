"""WP7 ①: prompt/模型版本化 — hash 稳定性 + prediction_log 字段 + 旧记录兼容."""
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.alpha import run_cycle  # noqa: E402
from src.alpha_engine import AlphaEngine, EvidenceAccumulator  # noqa: E402
from src.analyzer import PROMPT_HASH, SYSTEM_PROMPT, Analyzer  # noqa: E402
from src.knowledge import Knowledge  # noqa: E402
from src.state_manager import StateManager  # noqa: E402


class _StubClient:
    def __init__(self, response):
        self.response = response

    def chat(self, **kwargs):
        return self.response


class _StubAnalyzer:
    enabled = True
    endpoint = "http://127.0.0.1:5010"
    timeout = 1

    def __init__(self, prompt_hash=None, model=None, temperature=None):
        self.client = _StubClient({})
        if prompt_hash is not None:
            self.prompt_hash = prompt_hash
        if model is not None:
            self.last_call_model = model
        if temperature is not None:
            self.last_call_temperature = temperature


def _read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


# ---- hash 稳定性 ----

def test_prompt_hash_is_sha256_prefix():
    assert PROMPT_HASH == hashlib.sha256(
        SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12]
    assert len(PROMPT_HASH) == 12
    assert all(c in "0123456789abcdef" for c in PROMPT_HASH)


def test_prompt_hash_stable_for_same_prompt():
    assert hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:12] == PROMPT_HASH


def test_prompt_hash_changes_when_prompt_changes():
    changed = SYSTEM_PROMPT + "\n- 新增一条规则"
    assert hashlib.sha256(changed.encode("utf-8")).hexdigest()[:12] != PROMPT_HASH


def test_analyzer_exposes_prompt_hash():
    analyzer = Analyzer({"enabled": True, "endpoint": "http://127.0.0.1:5010"})
    assert analyzer.prompt_hash == PROMPT_HASH


def test_call_api_records_model_and_temperature():
    analyzer = Analyzer({"enabled": True, "endpoint": "http://127.0.0.1:5010",
                         "temperature": 0.3})
    analyzer.client = _StubClient({"ok": True, "content": '{"x": 1}',
                                   "json": {"x": 1}, "model": "deepseek-v3"})

    result, retryable = analyzer._call_api("hello")

    assert result == {"x": 1}
    assert retryable is False
    assert analyzer.last_call_model == "deepseek-v3"
    assert analyzer.last_call_temperature == 0.3


def test_call_api_missing_model_stays_none():
    analyzer = Analyzer({"enabled": True, "endpoint": "http://127.0.0.1:5010"})
    analyzer.client = _StubClient({"ok": True, "content": '{"x": 1}', "json": {"x": 1}})

    analyzer._call_api("hello")

    assert analyzer.last_call_model is None


# ---- prediction_log 新字段 ----

def test_log_prediction_records_hash_model_temperature(tmp_path):
    analyzer = _StubAnalyzer(prompt_hash="deadbeefcafe", model="deepseek-v3",
                             temperature=0.3)
    knowledge = Knowledge({}, str(tmp_path), analyzer)

    knowledge.log_prediction("BULL", "high", 100000.0)

    entry = _read_jsonl(tmp_path / "prediction_log.jsonl")[0]
    assert entry["prompt_hash"] == "deadbeefcafe"
    assert entry["model"] == "deepseek-v3"
    assert entry["temperature"] == 0.3
    assert entry["cycle_position"] == "BULL"
    assert entry["btc_price"] == 100000.0


def test_log_prediction_explicit_args_override(tmp_path):
    analyzer = _StubAnalyzer(prompt_hash="deadbeefcafe", model="m1", temperature=0.3)
    knowledge = Knowledge({}, str(tmp_path), analyzer)

    knowledge.log_prediction("BEAR", "low", 90000.0,
                             prompt_hash="000000000000", model="m2", temperature=0.7)

    entry = _read_jsonl(tmp_path / "prediction_log.jsonl")[0]
    assert entry["prompt_hash"] == "000000000000"
    assert entry["model"] == "m2"
    assert entry["temperature"] == 0.7


def test_log_prediction_falls_back_to_module_hash_and_omits_meta(tmp_path):
    knowledge = Knowledge({}, str(tmp_path), _StubAnalyzer())

    knowledge.log_prediction("BEAR_DEEP", "medium", 90000.0)

    entry = _read_jsonl(tmp_path / "prediction_log.jsonl")[0]
    assert entry["prompt_hash"] == PROMPT_HASH
    assert "model" not in entry
    assert "temperature" not in entry


def test_legacy_prediction_without_hash_still_audited(tmp_path):
    knowledge = Knowledge({}, str(tmp_path), _StubAnalyzer())
    old_ts = (datetime.utcnow() - timedelta(days=40)).isoformat() + "Z"
    legacy = {
        "ts": old_ts,
        "prediction_id": f"{old_ts}|BULL",
        "cycle_position": "BULL",
        "confidence": "high",
        "btc_price": 100000.0,
        "verified": False,
    }
    (tmp_path / "prediction_log.jsonl").write_text(
        json.dumps(legacy, ensure_ascii=False) + "\n", encoding="utf-8")

    result = knowledge.audit_predictions(current_price=110000.0)

    assert result["new_audited"] == 1
    assert result["correct"] == 1
    outcomes = _read_jsonl(tmp_path / "prediction_outcomes.jsonl")
    assert outcomes[0]["prediction_id"] == legacy["prediction_id"]
    assert outcomes[0]["verdict"] == "correct"


# ---- state.runtime.last_prompt_hash 同步 ----

class _Memory:
    def get_context_for_ai(self):
        return ""

    def append_alpha(self, record):
        pass

    def append_entry(self, text):
        pass

    def add_metric(self, name, value):
        pass


class _KnowledgeStub:
    def __init__(self):
        self.predictions = []

    def load_knowledge_base(self):
        return ""

    def log_drift_meta(self, meta, cycle_position):
        pass

    def check_drift(self):
        return []

    def log_prediction(self, cycle_position, confidence, btc_price=None):
        self.predictions.append((cycle_position, confidence, btc_price))

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


class _Fetcher:
    def __init__(self, tweets):
        self.tweets = list(tweets)

    def fetch(self):
        return [dict(t) for t in self.tweets]

    def get_tweets_by_ids(self, ids):
        return []


class _RunAnalyzer:
    def __init__(self, result, prompt_hash=None):
        if prompt_hash is not None:
            self.prompt_hash = prompt_hash
        self.result = result
        self.last_sent_tweet_ids = []
        self.last_filtered_tweet_ids = []

    def analyze(self, tweets, memory_context, knowledge_base="", retries=1,
                market_state=None):
        self.last_sent_tweet_ids = [str(t.get("id", "")) for t in tweets]
        return dict(self.result)


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


def _components(tmp_path, fetcher, analyzer):
    data_dir = str(tmp_path)
    sm = StateManager(data_dir, "state.json")
    sm.load()
    engine = AlphaEngine({"smoothing": {},
                          "stability": {"required_confirmations": 1}}, sm)
    return {
        "cfg": {"schedule": {"min_analysis_interval_hours": 0}},
        "data_dir": data_dir,
        "memory": _Memory(),
        "state": sm,
        "fetcher": fetcher,
        "analyzer": analyzer,
        "engine": engine,
        "evidence": EvidenceAccumulator(sm, 0.02),
        "knowledge": _KnowledgeStub(),
        "tradesync": _TradeSync(),
        "datafeed": _DataFeed(),
        "dingtalk": _DingTalk(),
        "review": _Review(),
    }


_TWEET = {"id": "1001", "date": "2026-09-17T12:25:00",
          "url": "https://x.com/i/web/status/1001",
          "content": "BTC ETF flow update"}


def test_run_cycle_syncs_last_prompt_hash(tmp_path):
    comp = _components(tmp_path, _Fetcher([_TWEET]),
                       _RunAnalyzer(_analysis(), prompt_hash="abc123abc123"))

    run_cycle(comp)

    assert comp["state"].get("runtime.last_prompt_hash") == "abc123abc123"
    assert comp["knowledge"].predictions == [("BEAR", "high", None)]


def test_run_cycle_missing_hash_attribute_stays_empty(tmp_path):
    comp = _components(tmp_path, _Fetcher([_TWEET]), _RunAnalyzer(_analysis()))

    run_cycle(comp)

    assert comp["state"].get("runtime.last_prompt_hash") == ""
