"""WP5 故障语义 — 数据源故障不盲推进 + 失败推文重放 (离线单元测试)。"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.alpha import print_status, run_cycle  # noqa: E402
from src.alpha_engine import AlphaEngine, EvidenceAccumulator  # noqa: E402
from src.analyzer import Analyzer  # noqa: E402
from src.fetcher import Fetcher  # noqa: E402
from src.pending_analysis import PendingAnalysis  # noqa: E402
from src.state_manager import StateManager  # noqa: E402

_TWEET = {"id": "1001", "date": "2026-09-17T12:25:00",
          "url": "https://x.com/i/web/status/1001",
          "content": "BTC ETF flow update"}


class _FakeMemory:
    def get_context_for_ai(self):
        return ""

    def append_alpha(self, record):
        pass

    def append_entry(self, text):
        pass

    def add_metric(self, name, value):
        pass


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


class _FakeReview:
    def record_price(self, *args, **kwargs):
        pass

    def price_trend(self):
        return {}

    def should_review(self):
        return False


class _AlertDingTalk:
    def __init__(self):
        self.alerts = []

    def regime_change(self, *args, **kwargs):
        pass

    def alpha_change(self, *args, **kwargs):
        pass

    def analysis(self, *args, **kwargs):
        return ""

    def alert(self, title, body=""):
        self.alerts.append((title, body))
        return True


class _Fetcher:
    def __init__(self, tweets=None, exc=None, store=None):
        self.tweets = list(tweets or [])
        self.exc = exc
        self.store = dict(store or {})
        self.fetch_calls = 0

    def fetch(self):
        self.fetch_calls += 1
        if self.exc:
            raise self.exc
        return [dict(t) for t in self.tweets]

    def get_tweets_by_ids(self, ids):
        return [self.store[i] for i in ids if i in self.store]


class _Analyzer:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def analyze(self, tweets, memory_context, knowledge_base="",
                retries=1, market_state=None):
        self.calls.append(list(tweets))
        if self.exc:
            raise self.exc
        return self.result


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


def _components(tmp_path, fetcher, analyzer, regime="BEAR", alpha=-1.00,
                progress=0.5, cooldown=3, pending=None):
    data_dir = str(tmp_path)
    sm = StateManager(data_dir, "state.json")
    sm.load()
    sm.set("regime.current", regime)
    sm.set("regime.entered_from", "INIT")
    sm.set("regime.cooldown_remaining", cooldown)
    sm.set("alpha.current", alpha)
    sm.set("alpha.target", alpha)
    sm.set("alpha.regime_progress", progress)
    # WP6 时间语义: 预置 1 天前的自然日锚点 (旧 state 首轮仅初始化不推进)
    sm.set("runtime.last_tick_at",
           (datetime.utcnow() - timedelta(days=1)).isoformat() + "Z")
    engine = AlphaEngine({"smoothing": {},
                          "stability": {"required_confirmations": 1}}, sm)
    return {
        "cfg": {"schedule": {"min_analysis_interval_hours": 0}},
        "data_dir": data_dir,
        "memory": _FakeMemory(),
        "state": sm,
        "fetcher": fetcher,
        "analyzer": analyzer,
        "engine": engine,
        "evidence": EvidenceAccumulator(sm, 0.02),
        "knowledge": _FakeKnowledge(),
        "tradesync": _FakeTradeSync(),
        "datafeed": _FakeDataFeed(),
        "dingtalk": _AlertDingTalk(),
        "review": _FakeReview(),
        "pending": pending if pending is not None else PendingAnalysis(data_dir),
    }


# ---- 1. 故障期不盲推进 ----

def test_fetch_error_freezes_ticks_and_sets_outage(tmp_path, capsys):
    comp = _components(tmp_path, _Fetcher(exc=RuntimeError("boom")),
                       _Analyzer(None))
    comp["state"].set("evidence.accumulators", {"BEAR": 1.0})

    run_cycle(comp)
    out = capsys.readouterr().out

    sm = comp["state"]
    assert sm.get("alpha.current") == -1.00
    assert sm.get("alpha.regime_progress") == 0.5
    assert sm.get("regime.cooldown_remaining") == 3
    assert sm.get("regime.stability_counter") == 0
    assert sm.get("evidence.accumulators") == {"BEAR": 1.0}   # 未 decay
    assert sm.get("runtime.analysis_count") == 0              # 故障轮不计分析
    outage = sm.get("runtime.outage")
    assert outage["reason"] == "fetch_error"
    assert "boom" in outage["detail"]
    assert outage["since"].endswith("Z")
    assert "Idle (fetch_error)" in out
    assert "不推进" in out

    assert [a[0] for a in comp["dingtalk"].alerts] == ["数据源故障"]
    assert "fetch_error" in comp["dingtalk"].alerts[0][1]

    persisted = StateManager(str(tmp_path), "state.json")
    persisted.load()
    assert persisted.get("runtime.outage")["reason"] == "fetch_error"


def test_consecutive_fetch_errors_alert_once(tmp_path, capsys):
    comp = _components(tmp_path, _Fetcher(exc=RuntimeError("boom")),
                       _Analyzer(None))

    run_cycle(comp)
    since = comp["state"].get("runtime.outage")["since"]
    run_cycle(comp)
    capsys.readouterr()

    assert [a[0] for a in comp["dingtalk"].alerts] == ["数据源故障"]
    assert comp["state"].get("runtime.outage")["since"] == since
    assert comp["state"].get("alpha.current") == -1.00


def test_recovery_cycle_clears_outage_and_resumes_ticks(tmp_path, capsys):
    fetcher = _Fetcher(exc=RuntimeError("boom"))
    comp = _components(tmp_path, fetcher, _Analyzer(None))
    run_cycle(comp)
    capsys.readouterr()

    fetcher.exc = None
    run_cycle(comp)
    out = capsys.readouterr().out

    sm = comp["state"]
    assert sm.get("runtime.outage") == {}
    assert [a[0] for a in comp["dingtalk"].alerts] == ["数据源故障", "数据源恢复"]
    assert "Idle (no new tweets)" in out
    # 故障恢复不补记: 恢复轮锚点已重置为 now → progress 增量≈0
    assert sm.get("alpha.regime_progress") == pytest.approx(0.5, abs=1e-6)
    assert sm.get("regime.stability_counter") == 1
    assert sm.get("runtime.analysis_count") == 1
    # 恢复后从下一轮起按自然日正常推进
    anchor = datetime.fromisoformat(
        sm.get("runtime.last_tick_at").replace("Z", "+00:00")).replace(tzinfo=None)
    comp["engine"].tick_alpha(now=anchor + timedelta(days=1))
    assert sm.get("alpha.regime_progress") > 0.5


def test_recovery_does_not_backfill_three_day_outage(tmp_path, capsys):
    """WP6 建议1: 3 天故障 → 恢复轮 progress 增量≈0 (而非 3/expected)。"""
    fetcher = _Fetcher(exc=RuntimeError("boom"))
    comp = _components(tmp_path, fetcher, _Analyzer(None))
    sm = comp["state"]
    # 故障开始前锚点为 3 天前 (若恢复补记, 增量会是 3/420)
    sm.set("runtime.last_tick_at",
           (datetime.utcnow() - timedelta(days=3)).isoformat() + "Z")
    run_cycle(comp)   # fetch_error: 故障轮不推进
    assert sm.get("alpha.regime_progress") == pytest.approx(0.5)
    capsys.readouterr()

    fetcher.exc = None
    run_cycle(comp)   # 恢复轮: _clear_outage 重置锚点 → 不补记 3 天
    out = capsys.readouterr().out

    assert sm.get("runtime.outage") == {}
    assert "Idle (no new tweets)" in out
    assert sm.get("alpha.regime_progress") == pytest.approx(0.5, abs=1e-6)
    # alpha 仅按单轮下限走一步 (0.02), 与故障天数无关 (非 3 天折算)
    assert sm.get("alpha.current") == pytest.approx(-0.98)
    three_days = 3 / 420
    assert abs(sm.get("alpha.regime_progress") - 0.5) < three_days / 100


def test_normal_idle_unchanged_without_outage(tmp_path, capsys):
    comp = _components(tmp_path, _Fetcher(tweets=[]), _Analyzer(None))

    run_cycle(comp)
    out = capsys.readouterr().out

    sm = comp["state"]
    assert not sm.get("runtime.outage")
    assert comp["dingtalk"].alerts == []
    assert sm.get("alpha.current") == pytest.approx(-0.98)
    assert sm.get("alpha.regime_progress") > 0.5
    assert sm.get("regime.cooldown_remaining") == 2
    assert sm.get("regime.stability_counter") == 1
    assert "Idle (no new tweets)" in out


# ---- 2. 失败推文重放 ----

def test_analysis_failure_enqueues_pending_and_sets_outage(tmp_path, capsys):
    tweet = dict(_TWEET)
    comp = _components(tmp_path, _Fetcher(tweets=[tweet]), _Analyzer(None))

    run_cycle(comp)
    out = capsys.readouterr().out

    assert "Analysis failed" in out
    assert "[Pending] 入队 1 条 (原因: analysis_failed" in out
    items = comp["pending"].load()
    assert [i["id"] for i in items] == ["1001"]
    assert items[0]["reason"] == "analysis_failed"
    assert items[0]["added_at"].endswith("Z")
    outage = comp["state"].get("runtime.outage")
    assert outage["reason"] == "analysis_failed"
    assert "1 条入重放队列" in outage["detail"]
    assert [a[0] for a in comp["dingtalk"].alerts] == ["数据源故障"]


def test_replay_next_cycle_success_clears_pending_and_outage(tmp_path, capsys):
    tweet = dict(_TWEET)
    fetcher = _Fetcher(tweets=[tweet], store={"1001": tweet})
    analyzer = _Analyzer(None)
    comp = _components(tmp_path, fetcher, analyzer)
    run_cycle(comp)
    assert comp["pending"].count() == 1

    fetcher.tweets = []
    analyzer.result = _analysis(cp="BEAR")
    run_cycle(comp)
    out = capsys.readouterr().out

    assert "[Pending] 重放 1 条失败推文" in out
    assert "[Pending] 分析成功, 移出队列 1 条" in out
    assert comp["pending"].count() == 0
    assert analyzer.calls[-1][0]["id"] == "1001"
    assert comp["state"].get("runtime.outage") == {}
    # 重放轮与正常轮语义一致: 计数/发帖/预测照常
    assert comp["state"].get("runtime.analysis_count") == 2
    assert comp["state"].get("runtime.post_count") == 1
    assert "cycle_position: BEAR" in out


def test_replay_failure_requeues_without_duplicate_or_alert(tmp_path, capsys):
    tweet = dict(_TWEET)
    fetcher = _Fetcher(tweets=[tweet], store={"1001": tweet})
    comp = _components(tmp_path, fetcher, _Analyzer(None))
    run_cycle(comp)
    first_added_at = comp["pending"].load()[0]["added_at"]
    capsys.readouterr()

    fetcher.tweets = []
    run_cycle(comp)
    capsys.readouterr()

    items = comp["pending"].load()
    assert len(items) == 1
    assert items[0]["added_at"] == first_added_at
    assert [a[0] for a in comp["dingtalk"].alerts] == ["数据源故障"]


def test_replay_missing_tweet_removed_from_queue(tmp_path, capsys):
    tweet = dict(_TWEET)
    fetcher = _Fetcher(tweets=[tweet], store={})
    comp = _components(tmp_path, fetcher, _Analyzer(None))
    run_cycle(comp)
    capsys.readouterr()

    fetcher.tweets = []
    run_cycle(comp)
    out = capsys.readouterr().out

    assert "1 条推文不在 tweets.jsonl, 已移出队列" in out
    assert comp["pending"].count() == 0
    assert comp["state"].get("runtime.outage") == {}   # fetch 成功 → 恢复


def test_pending_id_also_in_new_tweets_gets_removed(tmp_path):
    tweet = dict(_TWEET)
    fetcher = _Fetcher(tweets=[tweet], store={"1001": tweet})
    comp = _components(tmp_path, fetcher, _Analyzer(_analysis(cp="BEAR")))
    comp["pending"].add(["1001"])

    run_cycle(comp)

    assert comp["pending"].count() == 0


def test_analysis_exception_treated_as_failure(tmp_path, capsys):
    tweet = dict(_TWEET)
    comp = _components(tmp_path, _Fetcher(tweets=[tweet]),
                       _Analyzer(exc=RuntimeError("api down")))

    run_cycle(comp)
    out = capsys.readouterr().out

    assert "Analysis error: RuntimeError: api down" in out
    assert comp["pending"].count() == 1
    assert comp["state"].get("runtime.outage")["reason"] == "analysis_failed"


def test_fetch_error_with_replay_still_analyzes_but_keeps_outage(tmp_path, capsys):
    tweet = dict(_TWEET)
    fetcher = _Fetcher(tweets=[tweet], store={"1001": tweet})
    comp = _components(tmp_path, fetcher, _Analyzer(None))
    run_cycle(comp)
    capsys.readouterr()

    fetcher.exc = RuntimeError("boom")
    comp["analyzer"].result = _analysis(cp="BEAR")
    run_cycle(comp)
    out = capsys.readouterr().out

    assert "[Pending] 重放 1 条失败推文" in out
    assert comp["pending"].count() == 0
    # 数据源仍故障: outage 持续 (reason 更新, 不重复告警)
    assert comp["state"].get("runtime.outage")["reason"] == "fetch_error"
    assert [a[0] for a in comp["dingtalk"].alerts] == ["数据源故障"]


def test_locked_cycle_does_not_replay_or_clear_outage(tmp_path, capsys):
    tweet = dict(_TWEET)
    fetcher = _Fetcher(tweets=[tweet], store={"1001": tweet})
    comp = _components(tmp_path, fetcher, _Analyzer(None))
    comp["cfg"]["schedule"]["min_analysis_interval_hours"] = 24
    sm = comp["state"]
    sm.set("runtime.last_deepseek_at",
           (datetime.utcnow() - timedelta(hours=1)).isoformat() + "Z")
    comp["pending"].add(["1001"])

    run_cycle(comp)
    out = capsys.readouterr().out

    assert "[Fetch] Skipped (analysis lock)" in out
    assert comp["pending"].count() == 1       # 锁定轮不重放
    assert comp["analyzer"].calls == []       # 不调用 AI
    assert fetcher.fetch_calls == 0


# ---- B1: 分析窗口 (20 条) 与重放精确移除 (真实 Analyzer) ----

def _btc_tweet(tid, content=None):
    return {"id": tid, "date": "2026-09-23T00:00:00",
            "url": f"https://x.com/i/web/status/{tid}",
            "content": content or f"BTC ETF flow update {tid}"}


def _real_analyzer(result, prompts=None):
    analyzer = Analyzer({"enabled": True, "endpoint": "http://127.0.0.1:59999",
                         "cross_check": {"samples": 1,
                                         "structure_check": {"enabled": False}}})

    def fake_call_api(user_msg, images=None):
        if prompts is not None:
            prompts.append(user_msg)
        return dict(result), False

    analyzer._call_api = fake_call_api
    return analyzer


def test_replay_window_sends_only_20_and_keeps_unsent(tmp_path, capsys):
    tweets = [_btc_tweet(f"{i:04d}") for i in range(1, 26)]
    fetcher = _Fetcher(tweets=[], store={t["id"]: t for t in tweets})
    prompts = []
    analyzer = _real_analyzer(_analysis(cp="BEAR"), prompts)
    pending = PendingAnalysis(str(tmp_path))
    pending.add([t["id"] for t in tweets])
    comp = _components(tmp_path, fetcher, analyzer, pending=pending)

    run_cycle(comp)
    out = capsys.readouterr().out

    # 25 条队列 → 仅最新 20 条进 prompt (合并后取 tweets[-20:])
    assert analyzer.last_sent_tweet_ids == [f"{i:04d}" for i in range(6, 26)]
    assert len(prompts) == 1
    assert "0006" in prompts[0] and "0025" in prompts[0]
    assert "0005" not in prompts[0]
    # 未进窗口的 5 条仍在队列 (不得被静默清空), 下轮全部重放
    assert [it["id"] for it in pending.load()] == [f"{i:04d}" for i in range(1, 6)]
    assert "5 条未进分析窗口, 留队列下轮重试" in out

    run_cycle(comp)
    assert pending.count() == 0
    assert analyzer.last_sent_tweet_ids == [f"{i:04d}" for i in range(1, 6)]


def test_success_removes_sent_and_filtered_only(tmp_path):
    tweets = [_btc_tweet("0001"), _btc_tweet("alt01", "Solana validators upgrade tonight"),
              _btc_tweet("0003")]
    fetcher = _Fetcher(tweets=[], store={t["id"]: t for t in tweets})
    analyzer = _real_analyzer(_analysis(cp="BEAR"))
    pending = PendingAnalysis(str(tmp_path))
    pending.add(["0001", "alt01", "0003"])
    comp = _components(tmp_path, fetcher, analyzer, pending=pending)

    run_cycle(comp)

    assert analyzer.last_filtered_tweet_ids == ["alt01"]
    assert analyzer.last_sent_tweet_ids == ["0001", "0003"]
    assert pending.count() == 0   # sent ∪ filtered = 全部, 精确清空


def test_fresh_tweets_pushed_out_of_window_are_requeued(tmp_path, capsys):
    pending_tweets = [_btc_tweet(f"p{i:03d}") for i in range(1, 21)]
    new_tweets = [_btc_tweet(f"n{i:03d}") for i in range(1, 4)]
    fetcher = _Fetcher(tweets=new_tweets,
                       store={t["id"]: t for t in pending_tweets})
    analyzer = _real_analyzer(_analysis(cp="BEAR"))
    pending = PendingAnalysis(str(tmp_path))
    pending.add([t["id"] for t in pending_tweets])
    comp = _components(tmp_path, fetcher, analyzer, pending=pending)

    run_cycle(comp)
    out = capsys.readouterr().out

    assert analyzer.last_sent_tweet_ids == [t["id"] for t in pending_tweets]
    assert "3 条未进分析窗口, 留队列下轮重试" in out
    items = pending.load()
    assert {it["id"] for it in items} == {"n001", "n002", "n003"}
    assert all(it["reason"] == "window_overflow" for it in items)


def test_all_filtered_treated_as_skip_not_failure(tmp_path, capsys):
    tweets = [_btc_tweet("alt01", "Solana validators upgrade tonight"),
              _btc_tweet("alt02", "Ethereum layer2 fees drop")]
    fetcher = _Fetcher(tweets=tweets, store={t["id"]: t for t in tweets})
    analyzer = _real_analyzer(_analysis(cp="BEAR"))
    comp = _components(tmp_path, fetcher, analyzer)

    run_cycle(comp)
    out = capsys.readouterr().out

    assert "本批全部被 BTC 过滤跳过" in out
    assert analyzer.last_sent_tweet_ids == []
    assert comp["pending"].count() == 0
    assert not comp["state"].get("runtime.outage")
    # 视为无有效新闻: 走 idle 正常推进, 不中断主流程
    assert comp["state"].get("alpha.regime_progress") > 0.5
    assert comp["state"].get("runtime.analysis_count") == 1


def test_all_filtered_removes_altcoin_entries_from_queue(tmp_path, capsys):
    tweets = [_btc_tweet("alt01", "Solana validators upgrade tonight"),
              _btc_tweet("alt02", "Ethereum layer2 fees drop")]
    fetcher = _Fetcher(tweets=[], store={t["id"]: t for t in tweets})
    analyzer = _real_analyzer(_analysis(cp="BEAR"))
    pending = PendingAnalysis(str(tmp_path))
    pending.add(["alt01", "alt02"])
    comp = _components(tmp_path, fetcher, analyzer, pending=pending)

    run_cycle(comp)
    out = capsys.readouterr().out

    assert "本批全部被 BTC 过滤跳过" in out
    assert "[Pending] 过滤跳过, 移出队列 2 条" in out
    assert pending.count() == 0
    assert not comp["state"].get("runtime.outage")

    print_status(comp)
    assert "PendingTweets" not in capsys.readouterr().out


def test_pending_corrupt_bytes_do_not_break_cycle(tmp_path, capsys):
    path = tmp_path / "pending_analysis.jsonl"
    path.write_bytes(b'{"id":"1","added_at":"2026-09-01T00:00:00Z","reason":"x"}\n'
                     b"\xff\xfe\x00bad\n")
    fetcher = _Fetcher(tweets=[])
    comp = _components(tmp_path, fetcher, _Analyzer(None))

    run_cycle(comp)
    out = capsys.readouterr().out

    assert "无法解码" in out
    assert "推文重放队列损坏" in [t for t, _ in comp["dingtalk"].alerts]
    assert comp["state"].get("alpha.regime_progress") > 0.5   # idle 正常推进
    assert comp["state"].get("runtime.analysis_count") == 1


# ---- print_status 展示 ----

def test_print_status_shows_outage_and_pending(tmp_path, capsys):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    sm.set("runtime.outage", {"since": "2026-09-23T00:00:00Z",
                              "reason": "fetch_error", "detail": "boom"})
    pending = PendingAnalysis(str(tmp_path))
    pending.add(["1", "2"], reason="analysis_failed")

    print_status({"state": sm, "pending": pending})
    out = capsys.readouterr().out

    assert "Outage=fetch_error(since 2026-09-23T00:00:00Z)" in out
    assert "PendingTweets=2" in out


def test_print_status_silent_without_outage_or_pending(tmp_path, capsys):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()

    print_status({"state": sm, "pending": PendingAnalysis(str(tmp_path))})
    out = capsys.readouterr().out

    assert "Outage=" not in out
    assert "PendingTweets" not in out


# ---- 3. x.com 可达性 TTL ----

def _ttl_fetcher(tmp_path, ttl=900):
    return Fetcher({"usernames": ["glassnode"],
                    "reachability_ttl_seconds": ttl}, str(tmp_path))


def test_reachability_failure_reprobed_after_ttl(tmp_path):
    fetcher = _ttl_fetcher(tmp_path)
    calls = []
    fetcher._check_x_com = lambda: (calls.append(1), False)[1]

    assert fetcher._fetch_live_tweets() == []
    assert len(calls) == 1
    assert fetcher._fetch_live_tweets() == []   # TTL 内不复探
    assert len(calls) == 1

    fetcher._x_com_checked_at -= 901            # 模拟 TTL 到期
    assert fetcher._fetch_live_tweets() == []
    assert len(calls) == 2


def test_reachability_success_stays_cached(tmp_path, capsys):
    fetcher = _ttl_fetcher(tmp_path)
    calls = []
    fetcher._check_x_com = lambda: (calls.append(1), True)[1]
    fetcher._fetch_x_web = lambda user: ""

    assert fetcher._fetch_live_tweets() == []
    assert len(calls) == 1

    fetcher._x_com_checked_at -= 10 ** 6
    assert fetcher._fetch_live_tweets() == []
    assert len(calls) == 1   # 成功结果保持进程内缓存


def test_reachability_ttl_zero_reprobes_each_call(tmp_path):
    fetcher = _ttl_fetcher(tmp_path, ttl=0)
    calls = []
    fetcher._check_x_com = lambda: (calls.append(1), False)[1]

    fetcher._fetch_live_tweets()
    fetcher._fetch_live_tweets()

    assert len(calls) == 2


def test_reachability_ttl_config_parse(tmp_path):
    assert Fetcher({}, str(tmp_path)).reachability_ttl == 900.0
    assert _ttl_fetcher(tmp_path, ttl=60).reachability_ttl == 60.0
    assert Fetcher({"reachability_ttl_seconds": "bad"},
                   str(tmp_path)).reachability_ttl == 900.0
    assert Fetcher({"reachability_ttl_seconds": -5},
                   str(tmp_path)).reachability_ttl == 0.0


def test_get_tweets_by_ids_order_and_missing(tmp_path):
    fetcher = Fetcher({}, str(tmp_path))
    with open(fetcher.tweets_file, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "1", "content": "a"}) + "\n")
        fh.write("not json\n")
        fh.write(json.dumps({"id": "2", "content": "b"}) + "\n")
        fh.write(json.dumps({"id": "3", "content": "c"}) + "\n")

    found = fetcher.get_tweets_by_ids(["2", "9", "1"])

    assert [t["id"] for t in found] == ["2", "1"]
    assert fetcher.get_tweets_by_ids([]) == []
