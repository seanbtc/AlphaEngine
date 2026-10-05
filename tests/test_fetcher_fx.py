"""fetcher 的 fxtwitter 加装接线 — disabled 等价现状/fallback/keywords/截断/预览."""
import json
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.fetcher import Fetcher  # noqa: E402


def _tweet(tid, author="glassnode", source="x.com/glassnode", content=None):
    return {"id": str(tid), "date": "2026-10-01T00:00:00Z",
            "content": content or f"tweet {tid}",
            "url": f"https://x.com/{author}/status/{tid}",
            "author": author, "source": source,
            "fetched_at": "2026-10-01T00:00:00Z", "images": []}


class _RecordingFx:
    """记录调用的假 FxClient; 可配置返回/抛错."""

    def __init__(self, timeline=None, search=None,
                 raise_timeline=False, raise_search=False):
        self.timeline = list(timeline or [])
        self.search_results = list(search or [])
        self.raise_timeline = raise_timeline
        self.raise_search = raise_search
        self.timeline_calls = []
        self.search_calls = []

    def fetch_user_timeline(self, handle, count=20, since=None,
                            with_replies=False, cursor=None):
        self.timeline_calls.append({"handle": handle, "count": count,
                                    "with_replies": with_replies})
        if self.raise_timeline:
            raise RuntimeError("fx timeline boom")
        return [dict(t) for t in self.timeline]

    def search(self, query, count=30, feed="latest", cursor=None):
        self.search_calls.append({"query": query, "count": count})
        if self.raise_search:
            raise RuntimeError("fx search boom")
        return [dict(t) for t in self.search_results]


class _RawFx:
    """原样返回任意对象 (不拷贝/不校验) 的假 client — F1 防御回归用."""

    def __init__(self, timeline=None, search=None):
        self.timeline = timeline
        self.search_results = search

    def fetch_user_timeline(self, *args, **kwargs):
        return self.timeline

    def search(self, *args, **kwargs):
        return self.search_results


class _Resp:
    """鸭子类型 requests.Response (仅 json())."""

    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _make(tmp_path, fx=None, keywords=None, max_tweets=20,
          usernames=("glassnode",), retweet_whitelist=None):
    cfg = {"username": "glassnode", "usernames": list(usernames),
           "web_fallback": False, "max_tweets_per_fetch": max_tweets}
    if fx is not None:
        cfg["fxtwitter"] = fx
    if keywords is not None:
        cfg["keywords"] = keywords
    if retweet_whitelist is not None:
        cfg["retweet_whitelist"] = list(retweet_whitelist)
    return Fetcher(cfg, str(tmp_path))


def _install(fetcher, recorder, monkeypatch):
    monkeypatch.setattr(fetcher, "fx_client", recorder)


# ---- disabled: 等价现状 ----

def test_disabled_no_fx_calls_and_output_unchanged(tmp_path, monkeypatch):
    f = _make(tmp_path / "a")  # 无 fxtwitter 键 (旧配置)
    assert f.fx_enabled is False
    assert f.fx_max_tweets == 20
    assert f.keywords == []

    rec = _RecordingFx(timeline=[_tweet("10")], search=[_tweet("11")])
    _install(f, rec, monkeypatch)
    live = [_tweet("1"), _tweet("2")]
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [dict(t) for t in live])

    out = f.fetch()

    assert [t["id"] for t in out] == ["1", "2"]
    assert rec.timeline_calls == [] and rec.search_calls == []
    lst = [_tweet("1")]
    assert f._merge_fx(lst) is lst  # disabled 原样返回

    # 显式 enabled=false + keywords 配置同样不触网
    f2 = _make(tmp_path / "b", fx={"enabled": False}, keywords=["q"])
    rec2 = _RecordingFx(search=[_tweet("12")])
    _install(f2, rec2, monkeypatch)
    monkeypatch.setattr(f2, "_fetch_live_tweets", lambda: [_tweet("1")])
    assert [t["id"] for t in f2.fetch()] == ["1"]
    assert rec2.search_calls == []


# ---- fallback: 现有为空时补入 ----

def test_fallback_timeline_fills_when_existing_empty(tmp_path, monkeypatch):
    f = _make(tmp_path, fx={"enabled": True, "max_tweets": 5}, keywords=[])
    rec = _RecordingFx(timeline=[_tweet("10", source="fxtwitter/glassnode",
                                        content="fx only")])
    _install(f, rec, monkeypatch)
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [])

    out = f.fetch()

    assert [t["id"] for t in out] == ["10"]
    assert out[0]["source"] == "fxtwitter/glassnode"
    assert rec.timeline_calls == [{"handle": "glassnode", "count": 5,
                                   "with_replies": False}]
    assert rec.search_calls == []
    lines = (tmp_path / "tweets.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_fallback_skipped_when_existing_present(tmp_path, monkeypatch):
    f = _make(tmp_path, fx={"enabled": True}, keywords=[])
    rec = _RecordingFx(timeline=[_tweet("10")])
    _install(f, rec, monkeypatch)
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [_tweet("1")])

    out = f.fetch()

    assert [t["id"] for t in out] == ["1"]
    assert rec.timeline_calls == []


def test_unknown_mode_behaves_like_fallback(tmp_path, monkeypatch):
    f = _make(tmp_path, fx={"enabled": True, "mode": "weird"}, keywords=[])
    rec = _RecordingFx(timeline=[_tweet("9")])
    _install(f, rec, monkeypatch)
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [_tweet("1")])

    assert [t["id"] for t in f.fetch()] == ["1"]
    assert rec.timeline_calls == []


def test_mode_always_fetches_timeline_with_existing(tmp_path, monkeypatch):
    f = _make(tmp_path, fx={"enabled": True, "mode": "always"}, keywords=[])
    rec = _RecordingFx(timeline=[_tweet("9")])
    _install(f, rec, monkeypatch)
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [_tweet("1")])

    out = f.fetch()

    assert [t["id"] for t in out] == ["1", "9"]
    assert len(rec.timeline_calls) == 1


# ---- keywords: 独立路径 ----

def test_keywords_append_and_author_not_tracked_filtered(tmp_path, monkeypatch):
    f = _make(tmp_path, fx={"enabled": True, "max_tweets": 5},
              keywords=["bitcoin min_faves:1000"])
    rec = _RecordingFx(search=[_tweet(
        "2", author="randomguy",
        source="fxtwitter/search:bitcoin min_faves:1000")])
    _install(f, rec, monkeypatch)
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [_tweet("1")])

    out = f.fetch()

    assert [t["id"] for t in out] == ["1", "2"]
    assert out[1]["author"] == "randomguy"  # 关键词作者不做 tracked 过滤
    assert rec.search_calls == [{"query": "bitcoin min_faves:1000", "count": 5}]
    assert rec.timeline_calls == []  # 现有非空, fallback 不触发


_RT_PAYLOAD = {
    "code": 200,
    "results": [{
        "type": "status",
        "id": "555",
        "url": "https://x.com/N3oCortex/status/555",
        "text": "rt body",
        "created_timestamp": 1791132999,
        "author": {"screen_name": "N3oCortex"},
        "reposted_by": {"screen_name": "glassnode"},
    }],
}


def test_keyword_retweet_non_whitelisted_author_filtered(tmp_path, monkeypatch):
    """fetcher 级集成: keywords 命中转推, 原作者不在白名单 -> 整条被过滤."""
    import src.fx_client as fx

    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params)
        return _Resp(200, _RT_PAYLOAD)

    monkeypatch.setattr(fx.requests, "get", fake_get)
    f = _make(tmp_path, fx={"enabled": True}, keywords=["bitcoin"])
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [])

    assert f.fetch() == []
    assert captured["url"] == "https://api.fxtwitter.com/2/search"

    # 对照: 同一链路下原作者在白名单 -> 保留 (过滤不是误伤)
    f2 = _make(tmp_path / "b", fx={"enabled": True}, keywords=["bitcoin"],
               retweet_whitelist=["n3ocortex"])
    monkeypatch.setattr(f2, "_fetch_live_tweets", lambda: [])
    out = f2.fetch()
    assert [t["id"] for t in out] == ["555"]
    assert out[0]["author"] == "n3ocortex"


# ---- 异常隔离 ----

def test_fx_errors_are_swallowed(tmp_path, monkeypatch, capsys):
    f = _make(tmp_path, fx={"enabled": True}, keywords=["q"])
    rec = _RecordingFx(timeline=[_tweet("10")],
                       raise_timeline=True, raise_search=True)
    _install(f, rec, monkeypatch)
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [_tweet("1")])

    out = f.fetch()

    assert [t["id"] for t in out] == ["1"]
    assert "已忽略" in capsys.readouterr().out
    lines = (tmp_path / "tweets.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 1


def test_non_dict_fx_batches_do_not_raise(tmp_path, monkeypatch):
    """F1: client 返回非 dict 列表/整批非列表时 _fetch_fx 与 fetch() 均不抛."""
    f = _make(tmp_path, fx={"enabled": True, "mode": "always"}, keywords=["q"])
    monkeypatch.setattr(f, "fx_client", _RawFx(
        timeline=["junk", 123, None, _tweet("1")],
        search=[_tweet("2"), "more junk"]))
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [])

    assert [t["id"] for t in f._fetch_fx([])] == ["1", "2"]  # 非 dict 元素被跳过
    assert [t["id"] for t in f.fetch()] == ["1", "2"]

    # 整批非列表 (无 len) 同样被 try 兜住
    f2 = _make(tmp_path / "b", fx={"enabled": True, "mode": "always"})
    monkeypatch.setattr(f2, "fx_client", _RawFx(timeline=object()))
    monkeypatch.setattr(f2, "_fetch_live_tweets", lambda: [])
    assert f2.fetch() == []


# ---- 合并去重与截断 ----

def test_merge_dedup_and_global_truncation(tmp_path, monkeypatch):
    f = _make(tmp_path, fx={"enabled": True}, keywords=["q"], max_tweets=3)
    rec = _RecordingFx(search=[_tweet("3"), _tweet("4"), _tweet("5")])
    _install(f, rec, monkeypatch)
    monkeypatch.setattr(f, "_fetch_live_tweets",
                        lambda: [_tweet("1"), _tweet("2"), _tweet("3")])

    out = f.fetch()

    assert [t["id"] for t in out] == ["3", "4", "5"]  # 合并后走全局截断
    assert [t["id"] for t in out].count("3") == 1  # id 去重


def test_fx_duplicate_of_persisted_id_not_written_again(tmp_path, monkeypatch):
    """fx 结果与已落盘 existing_ids 重复 -> 跨轮不二次写盘 (幂等)."""
    (tmp_path / "tweets.jsonl").write_text(
        json.dumps(_tweet("1"), ensure_ascii=False) + "\n", encoding="utf-8")
    f = _make(tmp_path, fx={"enabled": True}, keywords=[])
    rec = _RecordingFx(timeline=[_tweet("1", source="fxtwitter/glassnode")])
    _install(f, rec, monkeypatch)
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [])

    assert f.fetch() == []
    assert f.fetch() == []  # 第二轮同样不写
    lines = (tmp_path / "tweets.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["id"] == "1"


# ---- silent-empty = idle 语义 ----

def test_enabled_silent_empty_returns_empty_idle(tmp_path, monkeypatch):
    """enabled + 现有为空 + fx 返回空 -> [] 且不抛/不落盘."""
    f = _make(tmp_path, fx={"enabled": True, "mode": "always"}, keywords=["q"])
    _install(f, _RecordingFx(), monkeypatch)
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [])

    assert f.fetch() == []
    assert not (tmp_path / "tweets.jsonl").exists()


def test_empty_fx_output_matches_disabled_pointwise(tmp_path, monkeypatch):
    """现有非空 + fx enabled 但返回空 -> 输出与 disabled 逐点一致."""
    live = [_tweet("3"), _tweet("1", content="keep me"), _tweet("2")]

    f_off = _make(tmp_path / "off", fx={"enabled": False}, keywords=["q"])
    monkeypatch.setattr(f_off, "_fetch_live_tweets",
                        lambda: [dict(t) for t in live])
    out_off = f_off.fetch()

    f_on = _make(tmp_path / "on", fx={"enabled": True, "mode": "always"},
                 keywords=["q"])
    _install(f_on, _RecordingFx(), monkeypatch)
    monkeypatch.setattr(f_on, "_fetch_live_tweets",
                        lambda: [dict(t) for t in live])
    out_on = f_on.fetch()

    assert out_on == out_off


# ---- preview_live: 支持 fx 且不落盘 ----

def test_preview_live_supports_fx_without_disk_write(tmp_path, monkeypatch):
    f = _make(tmp_path, fx={"enabled": True}, keywords=["q"])
    rec = _RecordingFx(timeline=[_tweet("7", source="fxtwitter/glassnode")],
                       search=[_tweet("8", source="fxtwitter/search:q")])
    _install(f, rec, monkeypatch)
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [])

    out = f.preview_live()

    assert [t["id"] for t in out] == ["7", "8"]
    assert not (tmp_path / "tweets.jsonl").exists()
    assert rec.timeline_calls and rec.search_calls


# ---- 配置归一化 ----

def test_config_normalization(tmp_path):
    f = _make(tmp_path, fx={"enabled": "yes", "max_tweets": 500,
                            "with_replies": 1},
              keywords=["a", "  ", 3])
    assert f.fx_enabled is True
    assert f.fx_max_tweets == 100
    assert f.fx_with_replies is True
    assert f.keywords == ["a", "3"]

    f2 = _make(tmp_path / "b", fx={"max_tweets": "abc"}, keywords="not-a-list")
    assert f2.fx_max_tweets == 20
    assert f2.keywords == []


@pytest.mark.parametrize("value", ["false", "FALSE", " 0 ", "no", "Off"])
def test_enabled_string_falsy_whitelist_behaves_disabled(tmp_path, monkeypatch,
                                                         value):
    """F3: enabled 字符串白名单 -> False, 行为与 disabled 一致 (零 fx 调用)."""
    f = _make(tmp_path, fx={"enabled": value}, keywords=["q"])
    assert f.fx_enabled is False
    rec = _RecordingFx(search=[_tweet("12")])
    _install(f, rec, monkeypatch)
    monkeypatch.setattr(f, "_fetch_live_tweets", lambda: [_tweet("1")])

    assert [t["id"] for t in f.fetch()] == ["1"]
    assert rec.timeline_calls == [] and rec.search_calls == []


def test_enabled_string_truthy_and_non_strings_keep_bool(tmp_path):
    """F3: 非白名单字符串保持 True; 非字符串保持 bool() 现行为."""
    assert _make(tmp_path / "yes", fx={"enabled": "yes"}).fx_enabled is True
    for i, value in enumerate(["true", "on", "1", "anything", " FALSE!"]):
        assert _make(tmp_path / f"t{i}", fx={"enabled": value}).fx_enabled is True
    assert _make(tmp_path / "num", fx={"enabled": 1}).fx_enabled is True
    assert _make(tmp_path / "zero", fx={"enabled": 0}).fx_enabled is False


@pytest.mark.parametrize("bad", [1e400, "abc", 0, -1, float("nan")])
def test_pathological_numeric_config_never_crashes_init(tmp_path, bad):
    """F2: 1e400 -> inf 等病态值即使 enabled=false 也不得崩构造."""
    f = _make(tmp_path, fx={"enabled": False, "max_tweets": bad,
                            "timeout_seconds": bad})
    assert 1 <= f.fx_max_tweets <= 100
    assert 1.0 <= f.fx_client.timeout <= 120.0
