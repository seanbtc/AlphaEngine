"""外部估值参考（F&G / AHR999 / 200MA）测试。"""
import sys
from pathlib import Path

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

import pytest

from src import market_reference
from src.analyzer import Analyzer


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f'HTTP {self.status_code}')

    def json(self):
        return self._payload


def test_fear_greed_parse_and_average(monkeypatch):
    market_reference.clear_cache()
    payload = {'data': [
        {'value': '10', 'value_classification': 'Extreme Fear', 'timestamp': '1795219200'},
        {'value': '12', 'value_classification': 'Extreme Fear', 'timestamp': '1795132800'},
        {'value': '20', 'value_classification': 'Extreme Fear', 'timestamp': '1795046400'},
    ]}
    monkeypatch.setattr(market_reference.requests, 'get',
                        lambda *args, **kwargs: _FakeResponse(payload))
    result = market_reference.fetch_fear_greed()
    assert result['available'] is True
    assert result['value'] == 10
    assert result['classification'] == '极度恐惧'
    assert result['avg_7d'] == pytest.approx(14.0)
    assert result['avg_30d'] == pytest.approx(14.0)
    assert result['as_of'] == '2026-11-21'
    assert result['stale'] is False


def test_fear_greed_failure_safe(monkeypatch):
    market_reference.clear_cache()
    monkeypatch.setattr(market_reference.requests, 'get',
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('boom')))
    result = market_reference.fetch_fear_greed()
    assert result['available'] is False
    assert 'boom' in result['error']


def test_ahr999_metric_via_commons(monkeypatch):
    market_reference.clear_cache()
    monkeypatch.setattr(market_reference, 'fetch_ahr999', lambda **kwargs: {
        'available': True, 'value': 0.31, 'zone': '抄底区', 'zone_key': 'bottom',
        'as_of': '2026-11-18', 'price': 63500.0, 'cost_200_sma': 72000.0,
        'stale': False, 'source': 'test',
    })
    result = market_reference.fetch_ahr999_metric()
    assert result['available'] is True
    assert result['value'] == pytest.approx(0.31)
    assert result['cost_200_sma'] == pytest.approx(72000.0)


def test_collect_market_reference_merges(monkeypatch):
    market_reference.clear_cache()
    monkeypatch.setattr(market_reference, 'fetch_fear_greed', lambda: {
        'available': True, 'value': 10, 'classification': '极度恐惧',
        'avg_7d': 12.0, 'avg_30d': 15.0,
    })
    monkeypatch.setattr(market_reference, 'fetch_ahr999_metric', lambda **kwargs: {
        'available': True, 'value': 0.31, 'zone': '抄底区', 'price': 63000.0,
    })
    result = market_reference.collect_market_reference(price=63500.0)
    assert result['fear_greed']['value'] == 10
    assert result['ahr999']['price'] == 63500.0


def test_format_market_reference_section():
    analyzer = Analyzer({'enabled': True, 'endpoint': 'http://127.0.0.1:5010'})
    lines = analyzer._format_market_reference({
        'market_reference': {
            'fear_greed': {'available': True, 'value': 10, 'classification': '极度恐惧',
                           'as_of': '2026-11-21', 'avg_7d': 12.0, 'avg_30d': 15.0, 'stale': False},
            'ahr999': {'available': True, 'value': 0.31, 'zone': '抄底区',
                        'as_of': '2026-11-18', 'price': 63500.0, 'stale': False},
        },
        'ma200': {'value': 72800.0, 'dist_pct': -12.6, 'slope': 'down'},
    })
    text = '\n'.join(lines)
    assert '恐惧贪婪: 10' in text
    assert 'as_of 2026-11-21' in text
    assert 'AHR999: 0.31' in text
    assert '200MA: 72,800' in text
    assert '斜率 down' in text
