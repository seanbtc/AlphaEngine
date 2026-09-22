"""移动均线上下文 (日线) — 单元测试, 全程离线 (fake DataFeed, 只读合成序列)。"""
import gzip
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src import ma_context  # noqa: E402
from src.alpha import _run_test_ai, build_market_state  # noqa: E402
from src.alpha_engine import AlphaEngine  # noqa: E402
from src.analyzer import SYSTEM_PROMPT, Analyzer  # noqa: E402
from src.state_manager import StateManager  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_ma_cache():
    ma_context.clear_cache()
    yield
    ma_context.clear_cache()


def _dates(count, start=date(2025, 1, 1)):
    return [(start + timedelta(days=i)).isoformat() for i in range(count)]


def _dated(prices, start=date(2025, 1, 1)):
    return list(zip(_dates(len(prices), start), prices))


def _ms(day, hour=0):
    return int(datetime(day.year, day.month, day.day, hour,
                        tzinfo=timezone.utc).timestamp() * 1000)


def _rising_prices(count=300, base=100.0, step=0.5):
    return [base + i * step for i in range(count)]


def _klines_result(prices, start=date(2025, 1, 1), stale=False, ok=True,
                   source="archive+binance-futures", error=None):
    bars = []
    for i, close in enumerate(prices):
        day = start + timedelta(days=i)
        bars.append({"open_time": _ms(day), "open": str(close), "high": str(close),
                     "low": str(close), "close": str(close), "volume": "1",
                     "close_time": _ms(day) + 86_399_999})
    return {"ok": ok, "stale": stale, "error": error, "source": source, "bars": bars}


class _FakeDataFeed:
    """最小 DataFeed 客户端 fake."""

    def __init__(self, result=None, exc=None, price=76000.0):
        self.result = result
        self.exc = exc
        self.price = price
        self.klines_calls = []
        self.endpoint = "http://fake:9550"
        self.symbol = "BTC/USDT"

    def get_klines(self, interval="1d", limit=None):
        self.klines_calls.append({"interval": interval, "limit": limit})
        if self.exc is not None:
            raise self.exc
        return self.result

    def get_price(self):
        return self.price


def _cfg(tmp_path, **overrides):
    cfg = {
        "history_file": str(tmp_path / "ma_history.jsonl"),
        "kline_limit": 400,
        "cache_ttl_seconds": 0,
    }
    cfg.update(overrides)
    return cfg


def _write_legacy_archive(path, series):
    """写旧版 4h 归档 (仅用于断言新代码不再读取它)。"""
    with gzip.open(path, "wt", encoding="utf-8", newline="") as fh:
        fh.write("timestamp,open,high,low,close,volume,hl2\n")
        for day, close in series:
            fh.write(f"{day} 00:00:00+00:00,"
                     f"{close},{close},{close},{close},1,{close}\n")


def _make_analyzer():
    return Analyzer({"endpoint": "http://127.0.0.1:5010", "enabled": False})


# ---- 均线数值 (与 pandas 对照) ----

def test_ema_and_sma_match_pandas():
    prices = [100 + math.sin(i / 7.0) * 5 + i * 0.1 for i in range(320)]
    snapshot = ma_context.compute_ma_snapshot(_dated(prices), {})
    frame = pd.Series(prices)

    for period in (5, 10, 20):
        expected = frame.ewm(span=period, adjust=False).mean().iloc[-1]
        assert snapshot["mas"][f"ema{period}"]["value"] == pytest.approx(expected, abs=0.005)
    for period in (50, 100, 200, 250):
        expected = frame.rolling(period).mean().iloc[-1]
        assert snapshot["mas"][f"sma{period}"]["value"] == pytest.approx(expected, abs=0.005)


def test_snapshot_fields_and_slope():
    prices = _rising_prices(300)
    snapshot = ma_context.compute_ma_snapshot(_dated(prices), {})

    assert snapshot["as_of"] == _dates(300)[-1]
    assert snapshot["price"] == pytest.approx(prices[-1], abs=0.01)
    assert snapshot["mas"]["ema5"]["pos"] == "above"
    assert snapshot["mas"]["ema5"]["slope"] == "up"
    assert snapshot["mas"]["sma250"]["slope"] == "up"
    assert snapshot["zone"] == "强势多头区"
    assert snapshot["long_ma"] == "sma200"
    assert snapshot["long_ma_label"] == "200SMA"
    assert snapshot["days_above_long_30d"] == 30
    assert snapshot["days_above_long_90d"] == 90


def test_snapshot_none_when_insufficient_data():
    assert ma_context.compute_ma_snapshot(_dated([100.0] * 249), {}) is None
    assert ma_context.compute_ma_snapshot([], {}) is None


# ---- 区间档位 ----

_MAS = {"ema5": 101, "ema10": 100, "ema20": 99, "sma50": 98, "sma100": 75,
        "sma200": 90, "sma250": 85}


@pytest.mark.parametrize("price,expected", [
    (105, "强势多头区"),          # 站上全部均线
    (92, "上升趋势回调区"),        # 200/250 上方, 50/100 下方
    (87, "趋势转变观察区"),        # 200 与 250 之间
    (80, "转弱/反抽区"),           # 200/250 下方, 100 上方
    (70, "空头区"),                # 低于所有均线
])
def test_classify_zone_tiers(price, expected):
    assert ma_context.classify_zone(price, _MAS, {}) == expected


def test_classify_zone_respects_threshold_config():
    cfg = {"zone_thresholds": {"long_trend": "sma50", "fair_value": "sma100",
                               "mid_term": "sma200"}}
    assert ma_context.classify_zone(99, _MAS, cfg) == "上升趋势回调区"


# ---- 事件检测 (穿越 / 金叉死叉 / 斜率翻转) ----

def test_event_detection_cross_golden_cross_slope_flip():
    prices = [100.0] * 220
    prices += [100 + i * 2.0 for i in range(1, 41)]     # 上升段
    prices += [180 - i * 2.0 for i in range(1, 81)]     # 回落段 (足够长以形成死叉)
    snapshot = ma_context.compute_ma_snapshot(
        _dated(prices), {"events_lookback": len(prices), "max_events": 50})
    events = snapshot["events"]
    texts = [e["text"] for e in events]

    assert any("50/200 金叉" in t for t in texts)
    assert any("50/200 死叉" in t for t in texts)
    assert any("斜率转下" in t for t in texts)
    assert any(t.startswith("下穿") for t in texts)
    assert all(e["date"] for e in events)
    # 新的在前 (日期降序)
    assert [e["date"] for e in events] == sorted((e["date"] for e in events), reverse=True)


def test_events_limited_by_window_and_max():
    prices = [100.0] * 220 + [100 + i * 2.0 for i in range(1, 41)]
    snapshot = ma_context.compute_ma_snapshot(
        _dated(prices), {"events_lookback": 2, "max_events": 3})
    assert len(snapshot["events"]) <= 3


# ---- DataFeed K 线 → 日线序列 ----

def test_load_daily_closes_from_datafeed_bars():
    day = date(2026, 9, 17)
    bars = [
        {"open_time": _ms(day, 0), "close": "100"},
        {"open_time": _ms(day, 12), "close": "102"},             # 同日最后一根
        {"open_time": _ms(day + timedelta(days=1), 0), "close": "103"},
        {"open_time": "bad", "close": "1"},                      # 坏行跳过
        {"open_time": _ms(day), "close": ""},                    # 缺 close 跳过
    ]
    fake = _FakeDataFeed(result={"ok": True, "stale": False, "error": None,
                                 "source": "archive", "bars": bars})

    series, info = ma_context.load_daily_closes(fake, interval="1d", limit=400)

    assert series == [("2026-09-17", 102.0), ("2026-09-18", 103.0)]
    assert info == {"ok": True, "stale": False, "error": None, "source": "archive"}
    assert fake.klines_calls == [{"interval": "1d", "limit": 400}]


def test_load_daily_closes_unavailable_variants():
    assert ma_context.load_daily_closes(None)[0] == []

    series, info = ma_context.load_daily_closes(_FakeDataFeed(result=None))
    assert series == [] and info["ok"] is False

    failed = {"ok": False, "stale": True, "error": "无归档", "source": "archive",
              "bars": []}
    series, info = ma_context.load_daily_closes(_FakeDataFeed(result=failed))
    assert series == [] and info["ok"] is False and info["error"] == "无归档"

    stale = _klines_result(_rising_prices(300), stale=True)
    series, info = ma_context.load_daily_closes(_FakeDataFeed(result=stale))
    assert series == [] and info["stale"] is True


# ---- build_ma_context: DataFeed 正常/降级 ----

def test_build_ma_context_from_datafeed(tmp_path):
    fake = _FakeDataFeed(result=_klines_result(_rising_prices(300)))
    cfg = _cfg(tmp_path)

    context = ma_context.build_ma_context(cfg, client=fake)

    assert context is not None
    assert context["source"] == "datafeed"
    assert context["as_of"] == _dates(300)[-1]
    assert context["snapshot"]["zone"] == "强势多头区"
    assert len(ma_context.load_history(cfg["history_file"])) == 1


@pytest.mark.parametrize("result,exc,needle", [
    (None, None, "不可用"),
    ({"ok": False, "stale": False, "error": "K 线暂不可用（无归档）", "source": "archive",
      "bars": []}, None, "不可用"),
    (_klines_result(_rising_prices(300), stale=True), None, "stale"),
    (_klines_result(_rising_prices(100)), None, "不足"),
])
def test_build_ma_context_unavailable_returns_none(tmp_path, capsys, result, exc, needle):
    fake = _FakeDataFeed(result=result, exc=exc)
    assert ma_context.build_ma_context(_cfg(tmp_path), client=fake) is None
    out = capsys.readouterr().out
    assert "[MA]" in out and needle in out


def test_build_ma_context_client_exception_returns_none(tmp_path, capsys):
    fake = _FakeDataFeed(exc=RuntimeError("boom"))
    assert ma_context.build_ma_context(_cfg(tmp_path), client=fake) is None
    assert "取数异常" in capsys.readouterr().out


def test_build_ma_context_without_client_returns_none(capsys):
    assert ma_context.build_ma_context({}) is None
    assert "未注入" in capsys.readouterr().out


def test_build_ma_context_does_not_fall_back_to_local_archive(tmp_path):
    archive = tmp_path / "legacy_archive.csv.gz"
    _write_legacy_archive(archive, _dated(_rising_prices(300)))
    cfg = _cfg(tmp_path, data_file=str(archive),
               data_file_fallbacks=[str(archive)])
    fake = _FakeDataFeed(result=None)  # DataFeed 不可用

    assert ma_context.build_ma_context(cfg, client=fake) is None


def test_build_ma_context_stale_does_not_write_history(tmp_path):
    cfg = _cfg(tmp_path)
    fake = _FakeDataFeed(result=_klines_result(_rising_prices(300), stale=True))

    assert ma_context.build_ma_context(cfg, client=fake) is None
    assert not Path(cfg["history_file"]).exists()


# ---- build_ma_context: 跨轮记忆 / as_of / 缓存 ----

def test_build_ma_context_memory_and_zone_change(tmp_path):
    cfg = _cfg(tmp_path)
    declining = [300 - i * 0.5 for i in range(320)]
    fake = _FakeDataFeed(result=_klines_result(declining))

    first = ma_context.build_ma_context(cfg, client=fake, as_of=_dates(320)[-1])
    assert first["snapshot"]["zone"] == "空头区"
    assert first["last_zone"] is None
    assert first["zone_changed"] is False

    rally = declining + [declining[-1] + 200]
    fake.result = _klines_result(rally)
    second = ma_context.build_ma_context(cfg, client=fake, as_of=_dates(321)[-1])

    assert second["snapshot"]["zone"] == "强势多头区"
    assert second["last_zone"] == "空头区"
    assert second["last_as_of"] == _dates(320)[-1]
    assert second["zone_changed"] is True
    assert second["zone_change_reason"]
    assert second["recent_history"][0]["as_of"] == _dates(320)[-1]
    assert second["stats"]["days_above_long_30d"] is not None
    assert second["stats"]["zone_changes_30d"] >= 1
    assert len(ma_context.load_history(cfg["history_file"])) == 2


def test_build_ma_context_as_of_excludes_future_bars(tmp_path):
    prices = [100.0] * 299 + [500.0]
    fake = _FakeDataFeed(result=_klines_result(prices))

    context = ma_context.build_ma_context(
        _cfg(tmp_path), client=fake, as_of=_dates(300)[-2])

    assert context["snapshot"]["as_of"] == _dates(300)[-2]
    assert context["snapshot"]["price"] == pytest.approx(100.0, abs=0.01)


def test_build_ma_context_kline_cache_respects_ttl(tmp_path):
    cfg = _cfg(tmp_path, cache_ttl_seconds=300)
    fake = _FakeDataFeed(result=_klines_result(_rising_prices(300, base=100.0)))

    first = ma_context.build_ma_context(cfg, client=fake)
    fake.result = _klines_result(_rising_prices(300, base=200.0))
    second = ma_context.build_ma_context(cfg, client=fake)
    assert second["snapshot"]["price"] == first["snapshot"]["price"]
    assert len(fake.klines_calls) == 1

    ma_context.clear_cache()
    third = ma_context.build_ma_context(cfg, client=fake)
    assert third["snapshot"]["price"] != first["snapshot"]["price"]
    assert len(fake.klines_calls) == 2


# ---- 历史记忆: 按日去重 + 保留上限 ----

def test_update_history_dedup_and_keep(tmp_path):
    history_file = str(tmp_path / "ma_history.jsonl")
    days = _dates(5)
    for i, day in enumerate(days):
        ma_context.update_history(
            history_file, {"as_of": day, "price": float(i), "zone": "z"}, keep=3)

    entries = ma_context.load_history(history_file)
    assert [e["as_of"] for e in entries] == days[2:]

    ma_context.update_history(
        history_file, {"as_of": days[-1], "price": 999.0, "zone": "z2"}, keep=3)
    entries = ma_context.load_history(history_file)
    assert [e["as_of"] for e in entries] == days[2:]
    assert entries[-1]["price"] == 999.0


def test_load_history_skips_corrupt_lines(tmp_path):
    path = tmp_path / "h.jsonl"
    path.write_text('{"as_of": "2026-01-01"}\nnot-json\n\n', encoding="utf-8")
    assert [e["as_of"] for e in ma_context.load_history(str(path))] == ["2026-01-01"]


# ---- persist 开关: --test-ai / 回溯只读, 正常轮次写 ----

def test_build_ma_context_persist_false_is_read_only(tmp_path):
    cfg = _cfg(tmp_path)
    ma_context.update_history(
        cfg["history_file"], {"as_of": "2026-01-01", "zone": "空头区"}, keep=10)
    before = Path(cfg["history_file"]).read_text(encoding="utf-8")
    fake = _FakeDataFeed(result=_klines_result(_rising_prices(300)))

    context = ma_context.build_ma_context(cfg, client=fake, persist=False)

    assert context is not None
    assert context["last_zone"] == "空头区"  # 只读仍可提供跨轮连续性
    assert Path(cfg["history_file"]).read_text(encoding="utf-8") == before


# ---- Analyzer 渲染 ----

def _sample_ma_context():
    return {
        "as_of": "2026-09-17",
        "source": "datafeed",
        "snapshot": {
            "price": 76401.8,
            "zone": "上升趋势回调区",
            "long_ma_label": "200SMA",
            "dist_long_pct": 12.4,
            "days_above_long_30d": 24,
            "mas": {
                "ema5": {"label": "5EMA", "value": 80200.0, "pos": "below",
                         "dist_pct": -4.7, "slope": "down"},
                "sma50": {"label": "50SMA", "value": 74000.0, "pos": "above",
                          "dist_pct": 3.2, "slope": "flat"},
                "sma200": {"label": "200SMA", "value": 68000.0, "pos": "above",
                           "dist_pct": 12.4, "slope": "up"},
            },
        },
        "events": [{"date": "2026-09-18", "text": "下穿 50SMA"},
                   {"date": "2026-09-10", "text": "50/200 金叉"}],
        "last_zone": "强势多头区",
        "last_as_of": "2026-09-18",
        "zone_changed": True,
        "zone_change_reason": "下穿 50SMA",
    }


def test_analyzer_renders_ma_section():
    analyzer = _make_analyzer()
    text = analyzer._format_market_state(
        {"regime": "BEAR", "ma_context": _sample_ma_context()})

    assert "## 移动均线结构 (日线, 辅助判断区间与趋势)" in text
    assert "- 数据源: datafeed | 价格: 76,402" in text
    assert "5EMA 80.2k(below,-4.7%,down)" in text
    assert "50SMA 74.0k(above,+3.2%,flat)" in text
    assert ("- 当前区间: 上升趋势回调区 (截至 2026-09-17)"
            " | 距200SMA +12.4% | 近30日站上200SMA 24 天") in text
    assert "- 近期事件: 09-18 下穿 50SMA；09-10 50/200 金叉" in text
    assert "- 上次评审区间: 强势多头区 (09-18) → 变更原因: 下穿 50SMA" in text


def test_analyzer_render_without_as_of():
    analyzer = _make_analyzer()
    context = _sample_ma_context()
    context.pop("as_of")
    context["snapshot"].pop("as_of", None)

    text = analyzer._format_market_state({"regime": "BEAR", "ma_context": context})

    assert "截至" not in text
    assert "- 当前区间: 上升趋势回调区 | 距200SMA +12.4%" in text


def test_analyzer_render_without_source():
    analyzer = _make_analyzer()
    context = _sample_ma_context()
    context.pop("source")

    text = analyzer._format_market_state({"regime": "BEAR", "ma_context": context})

    assert "数据源" not in text
    assert "- 价格: 76,402" in text


def test_analyzer_omits_ma_section_without_context():
    analyzer = _make_analyzer()
    assert "移动均线结构" not in analyzer._format_market_state({"regime": "BEAR"})
    assert "移动均线结构" not in analyzer._format_market_state(
        {"regime": "BEAR", "ma_context": None})


def test_system_prompt_has_ma_cheatsheet():
    for phrase in ("5 EMA ⚡动能", "10 EMA 🔍短期趋势", "20 EMA 🎯均值回归",
                   "50 SMA 🛡️强劲上升趋势支撑", "100 SMA 📉回调买入警报",
                   "200 SMA 🔄趋势转变", "250 SMA 💰公允价值",
                   "不构成短期交易信号", "价格结构与周期位置明显背离"):
        assert phrase in SYSTEM_PROMPT


# ---- build_market_state 接线 ----

def _market_state_components(tmp_path, ma_cfg, datafeed):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    engine = AlphaEngine({}, sm)
    return {"state": sm, "engine": engine,
            "cfg": {"ma_context": ma_cfg}, "datafeed": datafeed}


def test_build_market_state_attaches_ma_context(tmp_path):
    fake = _FakeDataFeed(result=_klines_result(_rising_prices(300)))
    ma_cfg = {"enabled": True, "history_file": str(tmp_path / "h.jsonl"),
              "kline_limit": 400, "cache_ttl_seconds": 0}

    state = build_market_state(_market_state_components(tmp_path, ma_cfg, fake))

    assert state["ma_context"]["source"] == "datafeed"
    assert state["ma_context"]["snapshot"]["zone"] == "强势多头区"


def test_build_market_state_ma_failure_does_not_block(tmp_path):
    fake = _FakeDataFeed(result=None)
    ma_cfg = {"enabled": True, "history_file": str(tmp_path / "h.jsonl"),
              "cache_ttl_seconds": 0}

    state = build_market_state(_market_state_components(tmp_path, ma_cfg, fake))

    assert "ma_context" not in state
    assert state["regime"] == "BEAR"


def test_build_market_state_persist_ma_false_is_read_only(tmp_path):
    fake = _FakeDataFeed(result=_klines_result(_rising_prices(300)))
    history_file = tmp_path / "ma_history.jsonl"
    ma_cfg = {"enabled": True, "history_file": str(history_file),
              "kline_limit": 400, "cache_ttl_seconds": 0}
    components = _market_state_components(tmp_path, ma_cfg, fake)

    state = build_market_state(components, persist_ma=False)

    assert "ma_context" in state
    assert not history_file.exists()

    state = build_market_state(components)

    assert "ma_context" in state
    assert history_file.exists()


def test_build_market_state_ma_disabled_by_default(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    engine = AlphaEngine({}, sm)
    state = build_market_state({"state": sm, "engine": engine, "cfg": {}})
    assert "ma_context" not in state


# ---- --test-ai 只读路径 ----

class _FakeMemory:
    def get_context_for_ai(self):
        return ""


class _FakeAnalyzer:
    def analyze(self, tweets, memory_context, knowledge_base="",
                retries=1, market_state=None):
        return None


class _FakeKnowledge:
    def load_knowledge_base(self):
        return ""


class _FakeFetcher:
    def preview_live(self):
        return [{"id": "1", "date": "2026-09-21T12:25:00",
                 "url": "https://x.com/i/web/status/1", "content": "BTC"}]


def _test_ai_components(tmp_path, datafeed, history_file):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    return {
        "state": sm,
        "engine": AlphaEngine({}, sm),
        "cfg": {"ma_context": {"enabled": True, "history_file": history_file,
                               "kline_limit": 400, "cache_ttl_seconds": 0}},
        "datafeed": datafeed,
        "memory": _FakeMemory(),
        "analyzer": _FakeAnalyzer(),
        "knowledge": _FakeKnowledge(),
        "fetcher": _FakeFetcher(),
    }


def test_test_ai_path_does_not_touch_ma_history(tmp_path):
    datafeed = _FakeDataFeed(result=_klines_result(_rising_prices(300)))
    history_file = tmp_path / "ma_history.jsonl"
    history_file.write_text(
        json.dumps({"as_of": "2026-01-01", "zone": "空头区"}) + "\n", encoding="utf-8")
    before = history_file.read_text(encoding="utf-8")

    _run_test_ai(_test_ai_components(tmp_path, datafeed, str(history_file)))

    assert history_file.read_text(encoding="utf-8") == before


def test_test_ai_path_does_not_create_ma_history(tmp_path):
    datafeed = _FakeDataFeed(result=_klines_result(_rising_prices(300)))
    history_file = tmp_path / "ma_history.jsonl"

    _run_test_ai(_test_ai_components(tmp_path, datafeed, str(history_file)))

    assert not history_file.exists()
