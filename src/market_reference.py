"""外部估值参考：恐惧贪婪指数 + AHR999（只读，失败不影响主流程）.

- F&G: Alternative.me，取最近 30 天，缓存 1 小时
- AHR999: commons.ahr999（Binance/DataFeed 日线），缓存 1 小时
- 200MA 由 ma_context 提供，不在本模块重复抓取
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from commons.ahr999 import fetch_ahr999  # noqa: E402

FNG_URL = 'https://api.alternative.me/fng/'
HTTP_TIMEOUT_SECONDS = 8.0
CACHE_TTL_SECONDS = 3600.0

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _now_ts() -> float:
    return time.monotonic()


def _cached(key: str) -> dict[str, Any] | None:
    item = _CACHE.get(key)
    if not item:
        return None
    ts, value = item
    if _now_ts() - ts > CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return None
    return value


def _store(key: str, value: dict[str, Any]) -> dict[str, Any]:
    _CACHE[key] = (_now_ts(), value)
    return value


def clear_cache() -> None:
    _CACHE.clear()


def _fng_label(value: int) -> str:
    if value <= 24:
        return '极度恐惧'
    if value <= 49:
        return '恐惧'
    if value <= 74:
        return '贪婪'
    return '极度贪婪'


def fetch_fear_greed(*, limit: int = 30, timeout: float = HTTP_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Alternative.me F&G；返回当前值/分类/7d/30d 均值；失败 available=False."""
    cached = _cached('fng')
    if cached is not None:
        return cached
    try:
        response = requests.get(
            FNG_URL, params={'limit': limit, 'format': 'json'},
            timeout=max(2.0, float(timeout)),
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get('data') if isinstance(payload, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ValueError('F&G 返回为空')
        parsed = []
        for row in rows:
            try:
                value = int(float(row.get('value')))
            except (TypeError, ValueError):
                continue
            parsed.append({
                'value': value,
                'classification': _fng_label(value),
                'raw_classification': str(row.get('value_classification') or ''),
                'timestamp': row.get('timestamp'),
            })
        if not parsed:
            raise ValueError('F&G 无有效数值')
        current = parsed[0]
        avg7 = sum(item['value'] for item in parsed[:7]) / min(7, len(parsed))
        avg30 = sum(item['value'] for item in parsed[:30]) / min(30, len(parsed))
        result = {
            'available': True,
            'value': current['value'],
            'classification': current['classification'],
            'raw_classification': current['raw_classification'],
            'as_of': datetime.now(timezone.utc).date().isoformat(),
            'avg_7d': round(avg7, 1),
            'avg_30d': round(avg30, 1),
            'samples': len(parsed),
        }
        return _store('fng', result)
    except Exception as exc:  # noqa: BLE001 - 外部指标失败不阻塞估值
        return {
            'available': False,
            'error': f'{type(exc).__name__}: {str(exc)[:200]}',
            'as_of': datetime.now(timezone.utc).date().isoformat(),
        }


def fetch_ahr999_metric(*, now: datetime | None = None) -> dict[str, Any]:
    """AHR999（commons 实现）；返回 value/zone/as_of/price/200SMA；失败 available=False."""
    cached = _cached('ahr999')
    if cached is not None:
        return cached
    try:
        row = fetch_ahr999(now=now)
        if not isinstance(row, dict):
            raise ValueError('AHR999 返回非字典')
        if not row.get('available'):
            return {
                'available': False,
                'error': str(row.get('error') or 'AHR999 不可用'),
                'source': row.get('source'),
                'as_of': row.get('as_of'),
            }
        result = {
            'available': True,
            'value': row.get('value'),
            'zone': row.get('zone'),
            'zone_key': row.get('zone_key'),
            'as_of': row.get('as_of'),
            'price': row.get('price'),
            'cost_200_sma': row.get('cost_200_sma'),
            'stale': bool(row.get('stale')),
            'source': row.get('source'),
        }
        return _store('ahr999', result)
    except Exception as exc:  # noqa: BLE001 - 外部指标失败不阻塞估值
        return {
            'available': False,
            'error': f'{type(exc).__name__}: {str(exc)[:200]}',
            'as_of': None,
        }


def collect_market_reference(*, now: datetime | None = None,
                             price: float | None = None) -> dict[str, Any]:
    """汇总外部参考；各子项独立失败，不抛异常."""
    fear_greed = fetch_fear_greed()
    ahr999 = fetch_ahr999_metric(now=now)
    if price is not None and ahr999.get('available'):
        ahr999['price'] = float(price)
    return {
        'as_of': datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + 'Z',
        'fear_greed': fear_greed,
        'ahr999': ahr999,
    }
