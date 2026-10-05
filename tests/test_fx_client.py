"""fxtwitter 客户端 (FxClient) — 字段映射/异常隔离/UA/批内去重 (离线单测)."""
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.fx_client import (  # noqa: E402
    DEFAULT_USER_AGENT,
    FxClient,
    _clamp_count,
    _coerce_timeout,
    build_search_request,
    build_user_timeline_request,
    map_results,
    map_status,
)

_MEDIA_URL = "https://pbs.twimg.com/media/HTzSeLmawAAWqVC.png?name=orig"
_LINK = "https://example.com/report"


def _status(**over):
    item = {
        "type": "status",
        "id": "2106790615455510906",
        "url": "https://x.com/glassnode/status/2106790615455510906",
        "text": "bitcoin update",
        "created_at": "Sun Oct 04 16:56:39 +0000 2026",
        "created_timestamp": 1791132999,
        "author": {"type": "profile", "screen_name": "glassnode",
                   "avatar_url": "https://pbs.twimg.com/profile_images/1/a.jpg"},
        "reposted_by": None,
        "media": {"all": [
            {"type": "photo", "url": _MEDIA_URL, "altText": "Liquidation cluster chart"},
        ]},
        "raw_text": {
            "text": "bitcoin update",
            "facets": [{"type": "url", "original": "https://t.co/x",
                        "replacement": _LINK, "display": "example.com"}],
        },
    }
    item.update(over)
    return item


class _Resp:
    def __init__(self, status_code=200, payload=None, json_exc=None):
        self.status_code = status_code
        self._payload = payload
        self._json_exc = json_exc

    def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._payload


# ---- 字段映射 ----

def test_map_status_fields_and_media_extra():
    tweet = map_status(_status(), "fxtwitter/glassnode")

    assert set(tweet.keys()) == {"id", "content", "date", "url", "author",
                                "source", "fetched_at", "images"}
    assert tweet["id"] == "2106790615455510906"
    assert tweet["author"] == "glassnode"
    assert tweet["source"] == "fxtwitter/glassnode"
    assert tweet["url"] == "https://x.com/glassnode/status/2106790615455510906"
    assert tweet["date"] == "2026-10-04T16:56:39+00:00"
    assert tweet["fetched_at"]
    assert tweet["images"] == [_MEDIA_URL]
    # 全文 + 媒体 alt/外链附加 (与 Fetcher._format_media_info 同风格)
    assert tweet["content"].startswith("bitcoin update\n")
    assert "Liquidation cluster chart" in tweet["content"]
    assert f"[链接] {_LINK}" in tweet["content"]


def test_external_link_already_in_text_not_duplicated():
    item = _status(text=f"see {_LINK} for details")
    tweet = map_status(item, "fxtwitter/glassnode")

    assert tweet["content"].count(_LINK) == 1
    assert "[链接]" not in tweet["content"]


def test_images_only_content_media_urls():
    item = _status(media={"all": [
        {"type": "photo", "url": "https://pbs.twimg.com/profile_images/1/a.jpg"},
        {"type": "video", "url": "https://video.twimg.com/ext_tw_video/1/vid.mp4"},
        {"type": "video",
         "thumbnail_url": "https://pbs.twimg.com/media/thumb.jpg?name=orig"},
        {"type": "photo", "url": _MEDIA_URL},
    ]})
    tweet = map_status(item, "fxtwitter/glassnode")

    assert tweet["images"] == [
        "https://pbs.twimg.com/media/thumb.jpg?name=orig", _MEDIA_URL]


def test_retweet_filtered_unless_original_author_whitelisted():
    rt = _status(id="999", reposted_by={"screen_name": "glassnode"},
                 author={"screen_name": "N3oCortex"})

    assert map_status(rt, "fxtwitter/glassnode") is None
    assert map_status(rt, "fxtwitter/glassnode", retweet_whitelist=["other"]) is None
    kept = map_status(rt, "fxtwitter/glassnode", retweet_whitelist=["n3ocortex"])
    assert kept is not None
    assert kept["author"] == "n3ocortex"


def test_date_fallback_chain():
    # created_timestamp 缺失 -> 解析 created_at
    item = _status()
    item.pop("created_timestamp")
    assert map_status(item, "s")["date"] == "2026-10-04T16:56:39+00:00"
    # 两者都非法 -> 空串 (不抛)
    bad = _status(created_timestamp=None, created_at="not-a-date")
    assert map_status(bad, "s")["date"] == ""


def test_map_results_batch_dedup_and_shared_fetched_at():
    results = [_status(), _status(text="changed"), _status(id="2"),
               _status(id="")]
    tweets = map_results(results, "fxtwitter/search:bitcoin")

    assert [t["id"] for t in tweets] == ["2106790615455510906", "2"]
    assert all(t["source"] == "fxtwitter/search:bitcoin" for t in tweets)
    assert len({t["fetched_at"] for t in tweets}) == 1


@pytest.mark.parametrize("bad", [
    None, "string", [], {}, {"id": "", "text": "x"}, {"id": "1", "text": ""},
    {"id": "1", "text": "   "},
    {"id": "1", "text": "x", "author": "bad", "media": "bad", "raw_text": "bad"},
    {"type": "tombstone", "id": "1", "text": "x"},
    {"id": "1", "text": "x", "media": {"all": [None, 3]}},
])
def test_map_status_defensive_never_raises(bad):
    out = map_status(bad, "s")
    assert out is None or isinstance(out, dict)


# ---- 请求构造 ----

def test_build_user_timeline_request_clamp_and_params():
    url, params = build_user_timeline_request(
        "glassnode", count=500, since=1791132999, with_replies=True, cursor="abc")
    assert url == "https://api.fxtwitter.com/2/profile/glassnode/statuses"
    assert params == {"count": 100, "since": 1791132999,
                      "with_replies": "1", "cursor": "abc"}

    _, params = build_user_timeline_request("glassnode", count="bad")
    assert params["count"] == 20
    _, params = build_user_timeline_request("glassnode", count=0)
    assert params["count"] == 1


def test_build_search_request_feed_fallback():
    url, params = build_search_request("bitcoin min_faves:1000", count=5,
                                       feed="latest")
    assert url == "https://api.fxtwitter.com/2/search"
    assert params["q"] == "bitcoin min_faves:1000"
    assert params["feed"] == "latest"
    assert params["count"] == 5

    _, params = build_search_request("x", feed="bogus")
    assert params["feed"] == "latest"


# ---- 客户端: UA/异常隔离 ----

def test_client_sends_user_agent_and_params(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params, headers=headers, timeout=timeout)
        return _Resp(200, {"code": 200, "results": []})

    monkeypatch.setattr("src.fx_client.requests.get", fake_get)
    got = FxClient({"timeout_seconds": 25}).fetch_user_timeline(
        "glassnode", count=5, with_replies=True)

    assert got == []
    assert captured["headers"]["User-Agent"] == DEFAULT_USER_AGENT
    assert captured["headers"]["User-Agent"]
    assert captured["url"] == "https://api.fxtwitter.com/2/profile/glassnode/statuses"
    assert captured["params"]["count"] == 5
    assert captured["params"]["with_replies"] == "1"
    assert captured["timeout"] == 25.0


def test_search_source_and_params(monkeypatch):
    captured = {}

    def fake_get(url, params=None, headers=None, timeout=None):
        captured.update(url=url, params=params)
        return _Resp(200, {"code": 200, "results": [_status()]})

    monkeypatch.setattr("src.fx_client.requests.get", fake_get)
    tweets = FxClient({}).search("bitcoin min_faves:1000", count=5)

    assert captured["url"] == "https://api.fxtwitter.com/2/search"
    assert captured["params"]["q"] == "bitcoin min_faves:1000"
    assert captured["params"]["feed"] == "latest"
    assert captured["params"]["count"] == 5
    assert tweets[0]["source"] == "fxtwitter/search:bitcoin min_faves:1000"


def test_empty_search_query_skips_request(monkeypatch):
    calls = []
    monkeypatch.setattr("src.fx_client.requests.get",
                        lambda *a, **k: calls.append(1))
    assert FxClient({}).search("   ") == []
    assert calls == []


@pytest.mark.parametrize("resp_or_exc,expect_error", [
    (TimeoutError("timed out"), True),
    (_Resp(500), True),
    (_Resp(200, json_exc=ValueError("bad json")), True),
    (_Resp(200, ["not", "a", "dict"]), True),
    (_Resp(200, {"code": 500, "results": []}), True),
    (_Resp(200, {"code": 200, "results": "bad"}), True),
])
def test_client_exception_isolation_returns_empty(monkeypatch, capsys,
                                                  resp_or_exc, expect_error):
    import src.fx_client as fx

    def fake_get(*args, **kwargs):
        if isinstance(resp_or_exc, Exception):
            raise resp_or_exc
        return resp_or_exc

    monkeypatch.setattr(fx.requests, "get", fake_get)
    assert FxClient({}).fetch_user_timeline("glassnode") == []
    out = capsys.readouterr().out
    if expect_error:
        assert "[FxClient]" in out


def test_client_204_returns_empty_without_error(monkeypatch, capsys):
    monkeypatch.setattr("src.fx_client.requests.get",
                        lambda *a, **k: _Resp(204))
    assert FxClient({}).search("bitcoin") == []
    assert "[FxClient] HTTP" not in capsys.readouterr().out


def test_client_maps_results_from_payload(monkeypatch):
    monkeypatch.setattr(
        "src.fx_client.requests.get",
        lambda *a, **k: _Resp(200, {"code": 200, "results": [_status(), _status()]}))
    tweets = FxClient({}).fetch_user_timeline("glassnode", count=5)
    assert len(tweets) == 1
    assert tweets[0]["id"] == "2106790615455510906"
    assert tweets[0]["source"] == "fxtwitter/glassnode"


def test_client_payload_without_code_maps_normally(monkeypatch):
    """payload 缺 code 字段 (结构变化/旧版) -> 跳过 code 校验, 正常映射不崩."""
    monkeypatch.setattr(
        "src.fx_client.requests.get",
        lambda *a, **k: _Resp(200, {"results": [_status(), _status(id="2")]}))
    tweets = FxClient({}).search("bitcoin", count=5)

    assert [t["id"] for t in tweets] == ["2106790615455510906", "2"]
    assert tweets[0]["source"] == "fxtwitter/search:bitcoin"


# ---- 数值归一化 (F2 溢出防御) ----

def test_numeric_normalization_overflow_safe():
    """inf -> int 抛 OverflowError; 巨整数 -> float 抛 OverflowError: 均回退默认."""
    assert _clamp_count(float("inf")) == 20
    assert _coerce_timeout(10 ** 400) == 20.0
    # 非溢出病态值保持既有行为 (对照, 防回归)
    assert _clamp_count("abc") == 20
    assert _clamp_count(0) == 1
    assert _coerce_timeout(float("nan")) == 20.0
    assert _coerce_timeout(0) == 1.0


def test_client_caps_mapped_results_to_count(monkeypatch):
    """时间线端点实测可能忽略 count 返回整页, 客户端按 count 截断."""
    results = [_status(id=str(100 + i)) for i in range(20)]
    monkeypatch.setattr(
        "src.fx_client.requests.get",
        lambda *a, **k: _Resp(200, {"code": 200, "results": results}))
    tweets = FxClient({}).fetch_user_timeline("glassnode", count=3)
    assert [t["id"] for t in tweets] == ["100", "101", "102"]
