"""WP7 ⑥: 图片缓存 LRU 上限 — 条目/字节淘汰 + Analyzer 接线."""
import sys
from pathlib import Path

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.analyzer import Analyzer, _ImageCache  # noqa: E402


# ---- _ImageCache 淘汰行为 ----

def test_entry_limit_evicts_oldest():
    cache = _ImageCache(max_entries=2, max_bytes=0)

    cache["a"] = "1"
    cache["b"] = "2"
    cache["c"] = "3"

    assert len(cache) == 2
    assert "a" not in cache
    assert cache["b"] == "2" and cache["c"] == "3"


def test_get_refreshes_lru_order():
    cache = _ImageCache(max_entries=2, max_bytes=0)
    cache["a"] = "1"
    cache["b"] = "2"

    assert cache["a"] == "1"   # a 变为最近使用
    cache["c"] = "3"

    assert "b" not in cache
    assert "a" in cache and "c" in cache


def test_overwrite_does_not_duplicate_or_leak_bytes():
    cache = _ImageCache(max_entries=10, max_bytes=100)
    cache["a"] = "12345"

    cache["a"] = "123"

    assert len(cache) == 1
    assert cache["a"] == "123"
    assert cache._bytes == 3


def test_byte_limit_evicts_oldest():
    cache = _ImageCache(max_entries=10, max_bytes=10)
    cache["a"] = "12345"   # 5 bytes
    cache["b"] = "12345"   # 10 bytes

    cache["c"] = "12345"   # 15 bytes → 淘汰 a

    assert "a" not in cache
    assert cache["b"] == "12345" and cache["c"] == "12345"
    assert cache._bytes == 10


def test_oversized_entry_kept_when_alone():
    cache = _ImageCache(max_entries=10, max_bytes=5)

    cache["big"] = "x" * 100

    assert cache["big"] == "x" * 100


def test_failed_download_cached_as_none():
    cache = _ImageCache(max_entries=2, max_bytes=0)

    cache["u"] = None

    assert "u" in cache
    assert cache["u"] is None
    assert cache.get("missing") is None


def test_missing_key_raises_keyerror():
    cache = _ImageCache()

    try:
        cache["nope"]
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


# ---- Analyzer 接线 ----

def test_analyzer_image_cache_config():
    analyzer = Analyzer({"enabled": True, "endpoint": "http://127.0.0.1:5010",
                         "image_cache_max_entries": 3,
                         "image_cache_max_bytes": 1024})

    assert analyzer.image_cache_max_entries == 3
    assert analyzer._image_cache.max_entries == 3
    assert analyzer._image_cache.max_bytes == 1024


def test_analyzer_image_cache_defaults():
    analyzer = Analyzer({"enabled": True, "endpoint": "http://127.0.0.1:5010"})

    assert analyzer.image_cache_max_entries == 32
    assert analyzer._image_cache.max_bytes == 64 * 1024 * 1024


def test_analyzer_image_cache_bad_values_fall_back():
    analyzer = Analyzer({"enabled": True, "endpoint": "http://127.0.0.1:5010",
                         "image_cache_max_entries": "abc",
                         "image_cache_max_bytes": None})

    assert analyzer.image_cache_max_entries == 32
    assert analyzer._image_cache.max_bytes == 64 * 1024 * 1024


def test_download_image_data_url_evicts(tmp_path, monkeypatch):
    class _Resp:
        status_code = 200
        headers = {"content-type": "image/png"}
        content = b"abc"

    monkeypatch.setattr("src.analyzer.requests.get", lambda *a, **k: _Resp())
    analyzer = Analyzer({"enabled": True, "endpoint": "http://127.0.0.1:5010",
                         "image_cache_max_entries": 2})

    for i in range(3):
        analyzer._download_image_data_url(f"https://img.example/{i}.png")

    assert len(analyzer._image_cache) == 2
    assert "https://img.example/0.png" not in analyzer._image_cache
    assert analyzer._download_image_data_url("https://img.example/2.png").startswith(
        "data:image/png;base64,")
