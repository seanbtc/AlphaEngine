"""方案A: Promo 载荷品牌词避让 + 术语映射.

起因: 09-24 No.18 因内容含 "Coinbase premium" 被 Promo 屏蔽词拦下。
约束: term_replacements 仅作用于发往 Promo 的载荷文本; 钉钉消息/记忆/知识库/
AI 原始输出保持原文; 配置缺失/非法/替换后为空 → 原文透传 + 日志。
"""
import json
import sys
from pathlib import Path

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

import src.alpha as alpha_module  # noqa: E402
from src.alpha import (  # noqa: E402
    _apply_promo_term_replacements, _write_promo_post, run_cycle)
from src.alpha_engine import AlphaEngine, EvidenceAccumulator  # noqa: E402
from src.analyzer import SYSTEM_PROMPT  # noqa: E402
from src.notify import DingTalk  # noqa: E402
from src.state_manager import StateManager  # noqa: E402

_DEFAULT_RULES = [
    {"pattern": "Coinbase Premium", "replacement": "美资现货溢价"},
    {"pattern": "coinbase", "replacement": "美资平台"},
]


def _capture_promo(monkeypatch):
    """捕获发往 Promo 的事件记录 (不实际发 HTTP)。"""
    records = []

    def _fake_push(record):
        records.append(record)
        return True

    monkeypatch.setattr(alpha_module, "_push_promo_event", _fake_push)
    return records


# ---- 配置默认映射 ----

def test_config_default_mappings_cover_coinbase():
    cfg = json.loads((_ALPHA_ROOT / "config.json").read_text(encoding="utf-8"))
    rules = cfg["promo"]["term_replacements"]
    assert isinstance(rules, list) and rules
    assert any("coinbase" in str(r["pattern"]).lower() for r in rules)
    for rule in rules:
        assert isinstance(rule["pattern"], str) and rule["pattern"]
        assert isinstance(rule["replacement"], str)


# ---- 替换应用范围: Promo 载荷替换 ----

def test_promo_payload_replaced_case_insensitive(monkeypatch, capsys):
    records = _capture_promo(monkeypatch)
    cfg = {"promo": {"enabled": True, "term_replacements": _DEFAULT_RULES}}
    text = ("No.18: Coinbase Premium 转负, COINBASE 流出放缓, "
            "coinbase premium 收窄")

    _write_promo_post(cfg, text, 18, "BEAR", -1.0)

    content = records[0]["content"]
    assert "美资现货溢价" in content
    assert "美资平台" in content
    assert "coinbase" not in content.lower()
    assert "premium" not in content.lower()
    assert "术语替换已应用" in capsys.readouterr().out


def test_promo_event_structure_unchanged(monkeypatch):
    records = _capture_promo(monkeypatch)
    cfg = {"promo": {"enabled": True, "term_replacements": _DEFAULT_RULES}}

    _write_promo_post(cfg, "Coinbase Premium 转负", 18, "BEAR", -0.42)

    record = records[0]
    assert record["event_type"] == "alpha_post"
    assert record["source"] == "AlphaEngine"
    assert record["post_no"] == 18
    assert record["cycle"] == "BEAR"
    assert record["alpha"] == -0.42
    assert isinstance(record["content"], str)
    assert record["ts"]


def test_more_specific_rule_applied_first():
    text = "Coinbase Premium 与 Coinbase 现货"
    result = _apply_promo_term_replacements(
        text, {"term_replacements": _DEFAULT_RULES})
    assert result == "美资现货溢价 与 美资平台 现货"


def test_pattern_matched_literally_not_as_regex():
    rules = [{"pattern": "GATE.IO", "replacement": "某所"}]
    result = _apply_promo_term_replacements(
        "gate.io 与 gateXio", {"term_replacements": rules})
    assert result == "某所 与 gateXio"


# ---- 配置缺失/非法回退 ----

def test_missing_config_passes_through_with_log(capsys):
    text = "Coinbase Premium 转负"
    assert _apply_promo_term_replacements(text, {}) == text
    assert "term_replacements" in capsys.readouterr().out


def test_invalid_config_type_passes_through_with_log(capsys):
    text = "Coinbase Premium 转负"
    assert _apply_promo_term_replacements(
        text, {"term_replacements": "Coinbase"}) == text
    assert "原文透传" in capsys.readouterr().out


def test_invalid_rule_entries_skipped_valid_applied(capsys):
    rules = [{"pattern": "", "replacement": "x"},
             {"pattern": "Coinbase"},
             "junk",
             {"pattern": "Coinbase", "replacement": "美资平台"}]
    result = _apply_promo_term_replacements(
        "Coinbase Premium", {"term_replacements": rules})
    assert result == "美资平台 Premium"
    assert "忽略非法术语规则" in capsys.readouterr().out


def test_empty_after_replacement_falls_back(capsys):
    rules = [{"pattern": "Coinbase", "replacement": "  "}]
    text = "Coinbase"
    assert _apply_promo_term_replacements(
        text, {"term_replacements": rules}) == text
    assert "为空" in capsys.readouterr().out


# ---- 应用范围: 钉钉/记忆保持原文 (run_cycle 接线) ----

class _Memory:
    def __init__(self):
        self.entries = []

    def get_context_for_ai(self):
        return ""

    def append_alpha(self, record):
        pass

    def append_entry(self, text):
        self.entries.append(text)

    def add_metric(self, name, value):
        pass


class _KnowledgeStub:
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


class _RecordingDingTalk:
    def __init__(self):
        self.analysis_texts = []
        self._inner = DingTalk({"enabled": False})

    def analysis(self, summary, cycle, conf, alpha, signals,
                 post_no=None, engine_regime=None):
        text = self._inner.analysis_text(
            summary, cycle, conf, alpha, signals, post_no, engine_regime)
        self.analysis_texts.append(text)
        return text

    def regime_change(self, *args, **kwargs):
        pass

    def alpha_change(self, *args, **kwargs):
        pass

    def alert(self, *args, **kwargs):
        return True


class _Fetcher:
    def fetch(self):
        return [{"id": "1001", "date": "2026-09-24T12:25:00",
                 "url": "https://x.com/i/web/status/1001",
                 "content": "Coinbase Premium update"}]

    def get_tweets_by_ids(self, ids):
        return []


class _RunAnalyzer:
    def __init__(self, result):
        self.result = result
        self.prompt_hash = "test123test"
        self.last_sent_tweet_ids = []
        self.last_filtered_tweet_ids = []

    def analyze(self, tweets, memory_context, knowledge_base="",
                market_state=None):
        self.last_sent_tweet_ids = [str(t.get("id", "")) for t in tweets]
        return dict(self.result)


def test_promo_replaced_but_dingtalk_and_memory_keep_original(
        tmp_path, monkeypatch):
    records = _capture_promo(monkeypatch)
    summary = "Coinbase Premium 转负 $1.2B, 美资需求走弱"
    memory = _Memory()
    dingtalk = _RecordingDingTalk()
    analyzer = _RunAnalyzer({
        "cycle_position": "BEAR",
        "cycle_confidence": "high",
        "regime_progress": 0.5,
        "regime_evidence": "evidence",
        "summary": summary,
        "evidence_scores": {"profitability": 0.1, "institutional": 0.1,
                            "onchain": 0.1, "derivatives": 0.1, "macro": 0.1},
        "signal_board": [],
        "meta": {"analysis_quality": 8},
    })
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    engine = AlphaEngine({"smoothing": {},
                          "stability": {"required_confirmations": 1}}, sm)
    components = {
        "cfg": {"schedule": {"min_analysis_interval_hours": 0},
                "promo": {"enabled": True,
                          "term_replacements": _DEFAULT_RULES}},
        "data_dir": str(tmp_path), "memory": memory, "state": sm,
        "fetcher": _Fetcher(), "analyzer": analyzer, "engine": engine,
        "evidence": EvidenceAccumulator(sm, 0.02),
        "knowledge": _KnowledgeStub(), "tradesync": _TradeSync(),
        "datafeed": _DataFeed(), "dingtalk": dingtalk, "review": _Review(),
    }

    run_cycle(components)

    assert records, "Promo 载荷未推送"
    promo_content = records[0]["content"]
    assert "Coinbase Premium" not in promo_content
    assert "美资现货溢价" in promo_content
    # 钉钉原文
    assert dingtalk.analysis_texts
    assert "Coinbase Premium" in dingtalk.analysis_texts[0]
    # 记忆原文
    assert any("Coinbase Premium" in e for e in memory.entries)


# ---- prompt 指引 + schema 回归 ----

def test_system_prompt_has_brand_avoidance_guidance():
    for phrase in ("## 发帖/摘要措辞 (品牌词避让)",
                   "交易平台品牌名",
                   "Coinbase、Binance、OKX",
                   "美资现货溢价",
                   "美资溢价指数",
                   "亚洲溢价",
                   "不改变输出 JSON 字段"):
        assert phrase in SYSTEM_PROMPT


def test_system_prompt_schema_unchanged_by_guidance():
    for field in ("cycle_position", "cycle_confidence", "regime_progress",
                  "evidence_scores", "summary", "regime_evidence",
                  "signal_board", "tweet_draft", "position_narrative",
                  "risks", "meta"):
        assert f'"{field}"' in SYSTEM_PROMPT
    assert "## 输出格式 (严格 JSON)" in SYSTEM_PROMPT
