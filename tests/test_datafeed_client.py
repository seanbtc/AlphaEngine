"""DataFeed 服务客户端 (价格/K 线) — 单元测试, 不联网 (monkeypatch requests)。"""
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src import datafeed as datafeed_module  # noqa: E402
from src.datafeed import DataFeed  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeHTTP:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def fake_http(monkeypatch):
    def _install(response):
        fake = _FakeHTTP(response)
        monkeypatch.setattr(datafeed_module.requests, "get", fake.get)
        return fake
    return _install


def _client(**overrides):
    cfg = {"enabled": True, "endpoint": "http://127.0.0.1:9550/",
           "symbol": "BTC/USDT", "timeout_seconds": 7}
    cfg.update(overrides)
    return DataFeed(cfg)


# ---- get_price ----

def test_get_price_sends_symbol_and_timeout(fake_http):
    http = fake_http(_FakeResponse(200, {
        "ok": True, "price": "76000.5", "ts": 1, "stale": False, "source": "poller"}))

    assert _client().get_price() == 76000.5

    url, kwargs = http.calls[0]
    assert url == "http://127.0.0.1:9550/price"
    assert kwargs["params"] == {"symbol": "BTCUSDT"}
    assert kwargs["timeout"] == 7


def test_get_price_ok_false_returns_none(fake_http, capsys):
    fake_http(_FakeResponse(200, {"ok": False, "error": "行情源不可达"}))
    assert _client().get_price() is None
    assert "ok=false" in capsys.readouterr().out


def test_get_price_non_200_and_exception_return_none(fake_http, capsys):
    fake_http(_FakeResponse(502, None))
    assert _client().get_price() is None
    assert "HTTP 502" in capsys.readouterr().out

    fake_http(RuntimeError("connection refused"))
    assert _client().get_price() is None
    assert "请求失败" in capsys.readouterr().out


def test_get_price_stale_returns_price_with_warning(fake_http, capsys):
    fake_http(_FakeResponse(200, {"ok": True, "price": "100", "ts": 1,
                                  "stale": True, "source": "poller"}))
    assert _client().get_price() == 100.0
    assert "stale" in capsys.readouterr().out


def test_get_price_strict_stale_returns_none(fake_http):
    fake_http(_FakeResponse(200, {"ok": True, "price": "100", "stale": True,
                                  "source": "poller"}))
    assert _client(strict_stale_price=True).get_price() is None


def test_get_price_disabled_makes_no_request(fake_http):
    http = fake_http(_FakeResponse(200, {"ok": True, "price": "100"}))
    assert _client(enabled=False).get_price() is None
    assert http.calls == []


# ---- get_klines ----

def test_get_klines_success_params(fake_http):
    payload = {"ok": True, "stale": False, "error": None, "source": "archive",
               "bars": [{"open_time": 1, "close": "2"}]}
    http = fake_http(_FakeResponse(200, payload))

    result = _client().get_klines(interval="1d", limit=400)

    assert result == {"ok": True, "stale": False, "error": None,
                      "source": "archive", "bars": [{"open_time": 1, "close": "2"}]}
    url, kwargs = http.calls[0]
    assert url == "http://127.0.0.1:9550/klines"
    assert kwargs["params"] == {"symbol": "BTCUSDT", "interval": "1d", "limit": 400}
    assert kwargs["timeout"] == 7


def test_get_klines_without_limit_omits_param(fake_http):
    http = fake_http(_FakeResponse(200, {"ok": True, "bars": []}))
    _client().get_klines()
    assert http.calls[0][1]["params"] == {"symbol": "BTCUSDT", "interval": "1d"}


def test_get_klines_non_200_returns_none_with_code(fake_http, capsys):
    fake_http(_FakeResponse(502, None))
    assert _client().get_klines() is None
    assert "HTTP 502" in capsys.readouterr().out


def test_get_klines_exception_returns_none(fake_http, capsys):
    fake_http(RuntimeError("boom"))
    assert _client().get_klines() is None
    assert "请求失败" in capsys.readouterr().out


def test_get_klines_ok_false_still_returns_dict(fake_http):
    fake_http(_FakeResponse(200, {"ok": False, "stale": True, "error": "无归档",
                                  "source": "archive", "bars": []}))
    result = _client().get_klines()
    assert result["ok"] is False
    assert result["error"] == "无归档"
    assert result["bars"] == []


def test_get_klines_disabled_makes_no_request(fake_http):
    http = fake_http(_FakeResponse(200, {"ok": True}))
    assert _client(enabled=False).get_klines() is None
    assert http.calls == []
