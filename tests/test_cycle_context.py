"""周期定位与历史类比上下文 — 单元测试 (全程离线, fake DataFeed + 合成序列)。"""
import csv
import gzip
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src import cycle_context  # noqa: E402
from src.alpha import build_market_state  # noqa: E402
from src.alpha_engine import AlphaEngine  # noqa: E402
from src.analyzer import SYSTEM_PROMPT, Analyzer  # noqa: E402
from src.state_manager import StateManager  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cycle_cache():
    cycle_context.clear_cache()
    yield
    cycle_context.clear_cache()


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
                   source="archive+binance-futures", error=None, highs=None):
    bars = []
    for i, close in enumerate(prices):
        day = start + timedelta(days=i)
        high = highs[i] if highs is not None else close
        bars.append({"open_time": _ms(day), "open": str(close), "high": str(high),
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


def _cfg(**overrides):
    cfg = {"kline_limit": 3000, "min_bars": 300, "cache_ttl_seconds": 0}
    cfg.update(overrides)
    return cfg


def _make_analyzer():
    return Analyzer({"endpoint": "http://127.0.0.1:5010", "enabled": False})


def _segmented_prices():
    """321 根: 100→200 (idx100 ATH) → 50 (idx250 低点) → 120 (末尾)。"""
    prices = [100.0 + i for i in range(101)]
    prices += [200.0 - i for i in range(1, 151)]      # idx 101..250, 末值 50
    prices += [50.0 + i for i in range(1, 71)]        # idx 251..320, 末值 120
    return prices


# ---- 通用工具 ----

def test_run_starts_and_merge_signals():
    assert cycle_context._run_starts([False, True, True, False, True]) == [1, 4]
    assert cycle_context.merge_signals([0, 3, 5, 11, 20], gap=5) == [0, 11, 20]
    assert cycle_context.merge_signals([], gap=5) == []
    assert cycle_context.merge_signals([7]) == [7]


def test_pct_change_guards():
    assert cycle_context._pct_change(110, 100) == pytest.approx(10.0)
    assert cycle_context._pct_change(90, 100) == pytest.approx(-10.0)
    assert cycle_context._pct_change(None, 100) is None
    assert cycle_context._pct_change(100, None) is None
    assert cycle_context._pct_change(100, 0) is None
    assert cycle_context._slope_diff([1, 2, 3], 2, 5) is None
    assert cycle_context._slope_dir(None) is None
    assert cycle_context._slope_dir(0.0) == "down"


# ---- 周期指标 (合成序列已知值) ----

def test_cycle_metrics_known_values():
    prices = _segmented_prices()
    start = date(2024, 1, 1)
    dates = _dates(len(prices), start)
    context = cycle_context.compute_cycle_context(_dated(prices, start), _cfg())

    cycle = context["cycle"]
    assert context["as_of"] == dates[-1]
    assert context["bars"] == 321
    assert cycle["ath_close"] == pytest.approx(200.0)
    assert cycle["ath_close_date"] == dates[100]
    assert cycle["ath_high"] == pytest.approx(200.0)
    assert cycle["close"] == pytest.approx(120.0)
    assert cycle["drawdown_pct"] == pytest.approx(-40.0)
    assert cycle["low_close"] == pytest.approx(50.0)
    assert cycle["low_date"] == dates[250]
    assert cycle["recovery_pct"] == pytest.approx(140.0)
    assert cycle["days_since_low"] == 70
    assert cycle["days_since_ath"] == 220
    assert cycle["days_since_halving"] == 210  # 2024-01-01 + 320d = 2024-11-16


def test_cycle_metrics_high_caliber_and_tie_first():
    prices = [100.0, 130.0, 120.0, 125.0]
    highs = [110.0, 140.0, 120.0, 125.0]
    context = cycle_context.compute_cycle_context(
        list(zip(_dates(4), prices, highs)), _cfg())
    cycle = context["cycle"]
    assert cycle["ath_close"] == pytest.approx(130.0)
    assert cycle["ath_high"] == pytest.approx(140.0)
    assert cycle["ath_high_date"] == _dates(4)[1]
    assert cycle["high_drawdown_pct"] == pytest.approx(-10.71)


# ---- 阶段判定: ④③②① + 过渡期 ----

def test_phase_bear():
    phase = cycle_context.classify_phase(90, 100, 95, "down", "down")
    assert phase == {"label": "熊市", "code": 4,
                     "reasons": ["close<SMA200 (-10.0%) 且 200SMA斜率↓"]}


def test_phase_top_variants():
    by_risk = cycle_context.classify_phase(
        105, 100, 95, "up", "down",
        top_risk={"active": True,
                  "reasons": ["距90日最高收盘回撤 -12.0% (阈值 -10%)"]})
    assert by_risk["code"] == 3
    assert by_risk["reasons"][0].startswith("距90日")

    by_regime = cycle_context.classify_phase(105, 100, 95, "up", "down",
                                             regime="BULL_COOLING")
    assert by_regime["code"] == 3
    assert by_regime["reasons"] == ["regime=BULL_COOLING (牛顶确认)"]

    both = cycle_context.classify_phase(
        105, 100, 95, "up", "down", regime="BULL_COOLING",
        top_risk={"active": True, "reasons": ["距90日最高收盘回撤 -12.0%"]})
    assert both["code"] == 3 and len(both["reasons"]) == 2


def test_phase_distance_alone_no_longer_triggers_top():
    # 旧 top_deviation_pct 口径已移除: 距200SMA +40% 不再单独触发 ③
    phase = cycle_context.classify_phase(140, 100, 95, "up", "up")
    assert phase["code"] == 0
    assert "top_deviation_pct" not in cycle_context._DEFAULT_TOP_RISK


def test_phase_top_risk_inactive_does_not_trigger():
    phase = cycle_context.classify_phase(
        105, 100, 95, "up", "down", top_risk={"active": False, "reasons": []})
    assert phase["code"] == 0


def test_phase_alpha_decoupled_from_judgement():
    # alpha 由引擎按时间自推, 不再参与阶段判定 (防自反馈):
    # alpha 0.95 且结构非顶部 (距200SMA +5%, 250SMA↓, BULL) → 过渡期而非 ③
    phase = cycle_context.classify_phase(105, 100, 95, "up", "down",
                                         regime="BULL", alpha=0.95)
    assert phase["code"] != 3
    assert phase["code"] == 0
    assert all("alpha" not in reason for reason in phase["reasons"])
    # 相同结构传 alpha=None 结果一致 (alpha 完全不影响判定)
    assert cycle_context.classify_phase(105, 100, 95, "up", "down",
                                        regime="BULL") == phase
    assert "top_alpha" not in cycle_context._DEFAULT_TOP_RISK


def test_phase_zone_changes_does_not_trigger_top():
    # 区间变更次数已移除独立触发; 遗留配置键不应影响判定
    phase = cycle_context.classify_phase(
        112, 100, 95, "up", "down", regime="RECOVERY",
        cfg={"phase": {"top_zone_changes": 3}})
    assert phase["code"] == 1
    assert "top_zone_changes" not in cycle_context._DEFAULT_TOP_RISK


def test_phase_confirmed_and_early_recovery():
    confirmed = cycle_context.classify_phase(105, 100, 95, "up", "up",
                                             regime="RECOVERY")
    assert confirmed["code"] == 2 and "250SMA斜率↑" in confirmed["reasons"][0]

    early = cycle_context.classify_phase(105, 100, 95, "up", "down",
                                         regime="BEAR_BOTTOM")
    assert early["code"] == 1 and "regime=BEAR_BOTTOM" in early["reasons"][0]


def test_phase_transition_and_precedence():
    transition = cycle_context.classify_phase(105, 100, 95, "up", "down",
                                              regime="BULL")
    assert transition["code"] == 0 and transition["label"] == "过渡期"

    bear_wins = cycle_context.classify_phase(90, 100, 95, "down", "up",
                                             regime="BULL", alpha=0.95)
    assert bear_wins["code"] == 4


# ---- 顶部风险 (compute_top_risk: 滞回进入/退出 + since 锁存) ----

def test_top_risk_confirm_entry_boundary_and_since():
    # 100 → 90: 回撤恰好 -10.00% (阈值含等号), 第 2 日确认进入, since=确认段首日
    dates = _dates(4)
    risk = cycle_context.compute_top_risk(
        dates, [100.0, 100.0, 90.0, 90.0],
        [80.0] * 4, [95.0] * 4)

    assert risk["enabled"] is True
    assert risk["active"] is True
    assert risk["since"] == dates[2]
    assert "连续2日确认" in risk["reasons"][0]
    assert risk["drawdown_from_90d_high_pct"] == pytest.approx(-10.0)
    assert risk["distance_200sma_pct"] == pytest.approx(12.5)
    assert risk["drawdown_window_days"] == 90
    assert risk["structure_ma"] == "sma50"
    assert any("SMA50" in reason for reason in risk["reasons"])


def test_top_risk_immediate_entry_boundary():
    # 回撤恰好 -11.00%: 无需确认, 首日立即进入
    dates = _dates(2)
    risk = cycle_context.compute_top_risk(
        dates, [100.0, 89.0], [80.0] * 2, [95.0] * 2)
    assert risk["active"] is True
    assert risk["since"] == dates[1]
    assert "立即进入" in risk["reasons"][0]


def test_top_risk_exit_paths():
    dates = _dates(5)
    # 结构修复: 收盘 > SMA50 (回撤仍深, 未达 dd 退出线)
    structure_exit = cycle_context.compute_top_risk(
        dates, [200.0, 200.0, 150.0, 150.0, 160.0],
        [100.0] * 5, [155.0, 155.0, 155.0, 155.0, 158.0])
    assert structure_exit["active"] is False

    # 回撤收窄至恰好 -9.00% (≥ 退出线) 退出
    dd_exit = cycle_context.compute_top_risk(
        dates, [100.0, 100.0, 90.0, 90.0, 91.0],
        [80.0] * 5, [95.0] * 5)
    assert dd_exit["active"] is False


def test_top_risk_exit_below_200sma_optional():
    dates = _dates(4)
    closes = [200.0, 200.0, 150.0, 160.0]
    long_ma = [100.0, 100.0, 100.0, 165.0]     # 第 4 日收盘 160 < 200SMA 165
    structure = [155.0, 155.0, 155.0, 170.0]

    default = cycle_context.compute_top_risk(dates, closes, long_ma, structure)
    assert default["active"] is True           # 默认不因跌破 200SMA 退出
    assert any("已跌破长期趋势" in reason for reason in default["reasons"])

    option = cycle_context.compute_top_risk(
        dates, closes, long_ma, structure, cfg={"exit_below_200sma": True})
    assert option["active"] is False


def test_top_risk_since_latch_within_and_outside_window():
    # 进入 → 退出 → 再进入: 距簇首 ≤30 交易日沿用簇首 since; >30 重置
    dates = _dates(40)
    within = [100.0] * 40
    within[2] = within[3] = 90.0               # 首次进入 (连续2日), since=dates[2]
    within[4] = 100.0                          # 收盘 > SMA50 → 退出
    within[6] = within[7] = 90.0               # 再进入 (距簇首 4 交易日)
    within[8:] = [90.0] * 32                   # 保持激活
    latched = cycle_context.compute_top_risk(dates, within, [80.0] * 40, [95.0] * 40)
    assert latched["active"] is True
    assert latched["since"] == dates[2]

    outside = [100.0] * 40
    outside[2] = outside[3] = 90.0             # 首次进入
    outside[4] = 100.0                         # 退出
    outside[35] = outside[36] = 90.0           # 再进入 (距簇首 34 交易日 > 30)
    outside[37:] = [90.0] * 3
    reset = cycle_context.compute_top_risk(dates, outside, [80.0] * 40, [95.0] * 40)
    assert reset["active"] is True
    assert reset["since"] == dates[35]


def test_top_risk_threshold_config_override():
    # 回撤 -8% 低于默认 10% 阈值不激活; 阈值放宽到 8% (连续2日) 后激活
    dates = _dates(3)
    closes = [100.0, 92.0, 92.0]
    default = cycle_context.compute_top_risk(
        dates, closes, [80.0] * 3, [95.0] * 3)
    assert default["active"] is False

    loose = cycle_context.compute_top_risk(
        dates, closes, [80.0] * 3, [95.0] * 3,
        cfg={"enter_drawdown_pct": 0.08})
    assert loose["active"] is True


def test_top_risk_warmup_and_disabled():
    dates = _dates(3)
    # 结构均线/200SMA 预热期未就绪 → 不激活
    warmup = cycle_context.compute_top_risk(
        dates, [100.0, 90.0, 90.0], [None] * 3, [None] * 3)
    assert warmup["active"] is False
    assert warmup["reasons"] == []

    # enabled=false: 条件满足也不激活 (③ 仅剩 regime=BULL_COOLING)
    disabled = cycle_context.compute_top_risk(
        dates, [100.0, 90.0, 90.0], [80.0] * 3, [95.0] * 3,
        cfg={"enabled": False})
    assert disabled["enabled"] is False
    assert disabled["active"] is False
    assert disabled["reasons"] == []

    # 收盘跌破 200SMA → 不激活 (防熊市触发)
    below = cycle_context.compute_top_risk(
        dates, [100.0, 90.0, 90.0], [95.0] * 3, [98.0] * 3)
    assert below["active"] is False


def test_top_risk_insufficient_data():
    risk = cycle_context.compute_top_risk([], [], [], [])
    assert risk["active"] is False
    assert risk["since"] is None
    assert risk["drawdown_from_90d_high_pct"] is None
    assert risk["distance_200sma_pct"] is None

    one = cycle_context.compute_top_risk(["2026-01-01"], [100.0], [80.0], [95.0])
    assert one["active"] is False and one["since"] is None


def test_top_risk_ema50_structure_override():
    dates = _dates(3)
    risk = cycle_context.compute_top_risk(
        dates, [100.0, 90.0, 90.0], [80.0] * 3, [95.0] * 3,
        cfg={"structure_ma": "ema50"})
    assert risk["structure_ma"] == "ema50"
    assert risk["active"] is True
    assert any("EMA50" in reason for reason in risk["reasons"])


def test_cycle_context_computes_top_risk_and_phase3():
    # 300 根爬升后 12% 回撤: 收盘跌破 SMA50 但仍在 SMA200 上 → top_risk 激活 → ③
    prices = _rising_prices(300, base=100.0, step=1.0)   # 末值 399
    prices.append(350.0)                                  # 回撤 -12.3%
    context = cycle_context.compute_cycle_context(_dated(prices), _cfg(min_bars=1))

    top_risk = context["top_risk"]
    assert top_risk["active"] is True
    assert top_risk["since"] == _dates(len(prices))[-1]
    assert context["phase"]["code"] == 3
    assert any("回撤" in reason for reason in context["phase"]["reasons"])

    # 收复 SMA50 后回落为未激活 (且阶段不再为 ③)
    prices.append(400.0)
    recovered = cycle_context.compute_cycle_context(_dated(prices), _cfg(min_bars=1))
    assert recovered["top_risk"]["active"] is False
    assert recovered["phase"]["code"] == 0


# ---- 类比统计 (小序列手工可验) ----

def test_stats_manual_median_and_positive_rate():
    stats = cycle_context._stats([0.10, -0.20, 0.30, 0.50])
    assert stats == {"n": 4, "median": 20.0, "pct_positive": 75.0,
                     "min": -20.0, "max": 50.0}
    empty = cycle_context._stats([])
    assert empty == {"n": 0, "median": None, "pct_positive": None,
                     "min": None, "max": None}


def test_fwd_returns_truncation():
    closes = [100.0, 110.0, 121.0]
    assert cycle_context._fwd_returns(closes, [0], 2) == [pytest.approx(0.21)]
    assert cycle_context._fwd_returns(closes, [0], 3) == []


def test_fwd_max_dd_manual():
    closes = [100.0, 90.0, 95.0, 105.0]
    assert cycle_context._fwd_max_dd(closes, 0, 2) == pytest.approx(-0.10)
    assert cycle_context._fwd_max_dd(closes, 0, 4) is None


def test_build_analog_debounce_dates_and_stats():
    closes = [100.0, 110.0, 105.0, 90.0, 95.0]
    dates = _dates(5)
    item = cycle_context._build_analog(
        "A", [0, 2], [False, False, False, False, False], dates, closes,
        windows=[1, 3], merge_gap=5, dd_window=2, dd_enabled=True)

    assert item["n"] == 2 and item["n_merged"] == 1   # 去抖合并
    assert item["dates"] == [dates[0], dates[2]]
    assert item["dates_merged"] == [dates[0]]
    assert item["returns"][1]["n"] == 2
    assert item["returns"][1]["median"] == pytest.approx(-2.14, abs=0.01)
    assert item["returns"][1]["pct_positive"] == 50.0
    assert item["returns"][3]["n"] == 1               # 截断剔除
    assert item["returns"][3]["median"] == pytest.approx(-10.0)
    assert item["fwd_180d_max_dd"]["n"] == 2
    assert item["fwd_180d_max_dd"]["median"] == pytest.approx(-4.64, abs=0.01)
    assert item["fwd_180d_max_dd"]["worst"] == pytest.approx(-14.29, abs=0.01)


def test_select_dates_recent_five_plus_earliest():
    dates = [f"2026-01-{i:02d}" for i in range(1, 9)]
    picked = cycle_context._select_dates(dates, list(range(8)))
    assert picked == ["2026-01-01", "2026-01-04", "2026-01-05",
                      "2026-01-06", "2026-01-07", "2026-01-08"]


# ---- 类比信号条件 / SMA 预热 ----

def test_analog_masks_known_patterns():
    # 构造: 长期横盘后最后一根放量上穿 200SMA → A/D 当日命中
    prices = [100.0] * 299 + [300.0]
    context = cycle_context.compute_cycle_context(_dated(prices), _cfg(min_bars=1))
    analogs = context["analogs"]

    assert analogs["A"]["n"] == 1
    assert analogs["A"]["hit_now"] is True
    assert analogs["D"]["n"] == 1
    assert analogs["D"]["hit_now"] is True
    assert set(analogs["A"]["returns"].keys()) == {30, 90, 180, 365}
    assert analogs["A"]["fwd_180d_max_dd"] is None      # 仅 B/D 输出
    # D 的信号在末端, 无完整前瞻窗口 → 不输出最大回撤
    assert analogs["D"]["fwd_180d_max_dd"] is None


def test_analog_b_and_c_conditions():
    # C: SMA250 斜率由 ≤0 转 >0; B: >200SMA + 200↑ + 250↓ + 距200SMA 10-30% + 距ATH>30%
    prices = [200.0 - i * 0.4 for i in range(300)]      # 下行
    prices += [82.0 + i * 0.5 for i in range(120)]      # 上行修复
    context = cycle_context.compute_cycle_context(_dated(prices), _cfg(min_bars=1))
    analogs = context["analogs"]

    assert analogs["C"]["n"] >= 1
    assert analogs["B"]["hit_now"] is False             # 距ATH回撤不满足
    assert analogs["B"]["n"] >= 0
    if analogs["B"]["n"]:
        stats = analogs["B"]["returns"][90]
        assert stats["n"] <= analogs["B"]["n"]


def test_compute_with_insufficient_bars_no_sma():
    context = cycle_context.compute_cycle_context([100.0] * 10, _cfg())
    assert context is not None
    assert context["trend"]["sma200"] is None
    assert context["trend"]["slope200"] is None
    assert context["phase"]["code"] == 0
    assert context["cycle"]["days_since_low"] is None


# ---- build_cycle_context: 降级 / 缓存 / 复用 ma_context ----

def test_build_cycle_context_from_datafeed():
    fake = _FakeDataFeed(result=_klines_result(_rising_prices(300)))

    context = cycle_context.build_cycle_context(_cfg(), fake)

    assert context is not None
    assert context["source"] == "datafeed"
    assert context["cycle"]["close"] == pytest.approx(249.5)
    assert fake.klines_calls == [{"interval": "1d", "limit": 3000}]


def test_build_cycle_context_min_bars_returns_none(capsys):
    fake = _FakeDataFeed(result=_klines_result(_rising_prices(299)))
    assert cycle_context.build_cycle_context(_cfg(), fake) is None
    out = capsys.readouterr().out
    assert "[Cycle]" in out and "不足" in out


@pytest.mark.parametrize("result,exc,needle", [
    (None, None, "不可用"),
    ({"ok": False, "stale": False, "error": "K 线暂不可用（无归档）",
      "source": "archive", "bars": []}, None, "不可用"),
    (_klines_result(_rising_prices(300), stale=True), None, "stale"),
    (_klines_result(_rising_prices(300)), RuntimeError("boom"), "取数异常"),
])
def test_build_cycle_context_unavailable_returns_none(capsys, result, exc, needle):
    fake = _FakeDataFeed(result=result, exc=exc)
    assert cycle_context.build_cycle_context(_cfg(), fake) is None
    out = capsys.readouterr().out
    assert "[Cycle]" in out and needle in out


def test_build_cycle_context_without_client_returns_none(capsys):
    assert cycle_context.build_cycle_context({}) is None
    assert "未注入" in capsys.readouterr().out


def test_build_cycle_context_reuses_ma_context():
    fake = _FakeDataFeed(result=_klines_result(_rising_prices(300)))
    ma_context = {"snapshot": {"zone": "上升趋势回调区", "days_above_long_30d": 12,
                               "days_above_long_90d": 40, "zone_changes_30d": 3}}

    context = cycle_context.build_cycle_context(_cfg(), fake, ma_context=ma_context,
                                                regime="RECOVERY")

    trend = context["trend"]
    assert trend["zone"] == "上升趋势回调区"
    assert trend["zone_source"] == "ma_context"
    assert trend["days_above_200_30d"] == 12
    assert trend["days_above_200_90d"] == 40
    assert trend["zone_changes_30d"] == 3
    assert context["phase"]["code"] == 2    # 250SMA↑ + RECOVERY → ②, 不再看区间变更


def test_build_cycle_context_alpha_and_regime_inputs():
    # 长平台+深跌+修复: close 刚站上 200SMA (+2%), 250SMA 仍向下
    prices = [200.0] * 150 + [50.0] * 100
    prices += [50.0 + i * 45.0 / 49.0 for i in range(50)]
    fake = _FakeDataFeed(result=_klines_result(prices))

    # alpha 0.95 不再触发 ③ (解耦): 结构为复苏早期 → ①
    high_alpha = cycle_context.build_cycle_context(
        _cfg(), fake, regime={"name": "RECOVERY", "alpha": 0.95})
    assert high_alpha["phase"]["code"] == 1

    early = cycle_context.build_cycle_context(
        _cfg(), fake, regime={"name": "RECOVERY", "alpha": 0.70})
    assert early["phase"]["code"] == 1
    assert early["phase"]["reasons"] == [
        "regime=RECOVERY 且 close>SMA200 且 250SMA未转正(斜率↓)"]


def test_build_cycle_context_cache_respects_ttl():
    cfg = _cfg(cache_ttl_seconds=300)
    fake = _FakeDataFeed(result=_klines_result(_rising_prices(300, base=100.0)))

    first = cycle_context.build_cycle_context(cfg, fake)
    fake.result = _klines_result(_rising_prices(300, base=200.0))
    second = cycle_context.build_cycle_context(cfg, fake)
    assert second["cycle"]["close"] == first["cycle"]["close"]
    assert len(fake.klines_calls) == 1

    cycle_context.clear_cache()
    third = cycle_context.build_cycle_context(cfg, fake)
    assert third["cycle"]["close"] != first["cycle"]["close"]
    assert len(fake.klines_calls) == 2


def test_load_daily_bars_keeps_high_and_last_bar_per_day():
    day = date(2026, 9, 17)
    bars = [
        {"open_time": _ms(day, 0), "close": "100", "high": "105"},
        {"open_time": _ms(day, 12), "close": "102", "high": "108"},
        {"open_time": _ms(day + timedelta(days=1), 0), "close": "103"},
        {"open_time": "bad", "close": "1", "high": "1"},
        {"open_time": _ms(day), "close": "", "high": "9"},
    ]
    fake = _FakeDataFeed(result={"ok": True, "stale": False, "error": None,
                                 "source": "archive", "bars": bars})

    series, info = cycle_context.load_daily_bars(fake, interval="1d", limit=3000)

    assert series == [("2026-09-17", 102.0, 108.0), ("2026-09-18", 103.0, 103.0)]
    assert info["ok"] is True and info["source"] == "archive"


# ---- 渲染 ----

def _sample_context():
    return {
        "as_of": "2026-09-23",
        "low_lookback_days": 365,
        "cycle": {"ath_close": 124628.5, "ath_close_date": "2025-10-06",
                  "ath_high": 126208.5, "close": 86881.0,
                  "drawdown_pct": -30.29, "low_close": 58605.4,
                  "low_date": "2026-06-30", "recovery_pct": 48.25,
                  "days_since_low": 85, "days_since_ath": 352,
                  "days_since_halving": 886},
        "phase": {"label": "结构确认", "code": 2,
                  "reasons": ["250SMA斜率↑ 且 regime=RECOVERY"]},
        "top_risk": {"enabled": True, "active": False, "since": None,
                     "reasons": [], "drawdown_from_90d_high_pct": 0.0,
                     "distance_200sma_pct": 22.76, "drawdown_window_days": 90,
                     "structure_ma": "sma50"},
        "trend": {"sma200": 70773.8, "sma250": 71567.48, "slope200": "up",
                  "slope250": "down", "dist_200_pct": 22.76,
                  "days_above_200_30d": 30, "days_above_200_90d": 36,
                  "zone": "强势多头区", "zone_source": "computed",
                  "zone_changes_30d": 4},
        "analogs": {
            "A": {"label": "首次上穿200SMA", "n": 26, "n_merged": 20,
                  "dates": [], "dates_merged": [], "hit_now": False,
                  "fwd_180d_max_dd": None,
                  "returns": {30: {"n": 26, "median": -0.01, "pct_positive": 50.0,
                                   "min": -23.03, "max": 36.86},
                              90: {"n": 25, "median": 25.61, "pct_positive": 72.0,
                                   "min": -22.78, "max": 53.65},
                              180: {"n": 25, "median": 29.02, "pct_positive": 56.0,
                                    "min": -57.93, "max": 124.46},
                              365: {"n": 22, "median": -16.99, "pct_positive": 36.4,
                                    "min": -65.59, "max": 135.36}}},
            "B": {"label": "当前状态类比", "n": 7, "n_merged": 6,
                  "dates": [], "dates_merged": [], "hit_now": True,
                  "fwd_180d_max_dd": {"window": 180, "n": 3,
                                      "median": -11.12, "worst": -18.25},
                  "returns": {30: {"n": 4, "median": 10.21, "pct_positive": 100.0,
                                   "min": 7.08, "max": 12.45},
                              90: {"n": 3, "median": 22.58, "pct_positive": 100.0,
                                   "min": 9.06, "max": 24.57},
                              180: {"n": 3, "median": 31.92, "pct_positive": 100.0,
                                    "min": 8.01, "max": 34.75},
                              365: {"n": 3, "median": 111.78, "pct_positive": 100.0,
                                    "min": 83.98, "max": 121.06}}},
        },
        "caveats": ["收盘口径的日线历史统计; 前瞻窗口不足的信号已剔除",
                    "去抖: 相邻≤5 交易日信号已并为同簇 (去抖后 A/B/C/D = 20/6/5/24)",
                    "B(当前状态)有效窗口仅 3 例, 且当前命中信号本身无前瞻数据"],
    }


def test_format_cycle_context_key_lines_and_limit():
    text = cycle_context.format_cycle_context(_sample_context())

    assert "## 周期定位与历史类比 (日线, 参考)" in text
    assert "ATH收盘 124,628.50 (2025-10-06)" in text
    assert "距ATH -30.3%" in text
    assert "低点恢复 +48.2%" in text
    assert "距减半 886 天" in text
    assert "- 阶段: 结构确认 (code=2)" in text
    assert "- 顶部风险: 无" in text
    assert "250SMA 71,567.48(down)" in text
    assert "- 当前区间: 强势多头区 | 近30日区间变更 4 次" in text
    assert "A 首次上穿200SMA (n=26, 去抖20)" in text
    assert "90d +25.6%(72%正)" in text
    assert "B 当前状态类比 (n=7, 去抖6, 当前命中)" in text
    assert "未来180d最大回撤 中位-11.1% 最差-18.2%" in text
    assert "- 风险提示: B(当前状态)有效窗口仅 3 例" in text
    assert len(text.splitlines()) <= 45


def test_format_cycle_context_empty_variants():
    assert cycle_context.format_cycle_context(None) == ""
    assert cycle_context.format_cycle_context({}) == ""
    assert cycle_context.format_cycle_context("bad") == ""


def test_format_cycle_context_top_risk_active_and_disabled():
    active = _sample_context()
    active["top_risk"] = {
        "enabled": True, "active": True, "since": "2025-10-11",
        "reasons": ["距90日最高收盘回撤 -11.3% (阈值 -10%)"],
        "drawdown_from_90d_high_pct": -11.27, "distance_200sma_pct": 3.69,
        "drawdown_window_days": 90, "structure_ma": "sma50"}
    text = cycle_context.format_cycle_context(active)
    assert ("- 顶部风险: 激活(自 2025-10-11, 距90日高点 -11.3%, "
            "距200SMA +3.7%, 跌破SMA50)") in text

    disabled = _sample_context()
    disabled["top_risk"] = {
        "enabled": False, "active": False, "since": None, "reasons": [],
        "drawdown_from_90d_high_pct": None, "distance_200sma_pct": None,
        "drawdown_window_days": 90, "structure_ma": "sma50"}
    assert "- 顶部风险: 已关闭 (config)" in cycle_context.format_cycle_context(disabled)

    # 旧版上下文 (无 top_risk 字段) 不渲染该行 (向后兼容)
    legacy = _sample_context()
    legacy.pop("top_risk")
    assert "顶部风险" not in cycle_context.format_cycle_context(legacy)


# ---- Analyzer 渲染 / prompt / 输出字段 ----

def test_format_market_state_includes_cycle_section():
    analyzer = _make_analyzer()
    text = analyzer._format_market_state(
        {"regime": "BEAR", "cycle_context": _sample_context()})

    assert "## 周期定位与历史类比 (日线, 参考)" in text
    assert "历史类比 (收盘口径" in text


def test_format_market_state_omits_cycle_section_without_context():
    analyzer = _make_analyzer()
    assert "周期定位与历史类比" not in analyzer._format_market_state(
        {"regime": "BEAR"})
    assert "周期定位与历史类比" not in analyzer._format_market_state(
        {"regime": "BEAR", "cycle_context": None})


def test_system_prompt_mentions_cycle_context():
    for phrase in ("周期定位与历史类比用于辅助判断 cycle_position/confidence",
                   "样本量小仅作参考",
                   "顶部风险为**价格结构警示**",
                   "须与链上/宏观/新闻证据 (Glassnode 内容) 结合判断",
                   "不单独构成结论"):
        assert phrase in SYSTEM_PROMPT


def test_ai_output_schema_unchanged():
    result = {
        "cycle_position": "RECOVERY", "cycle_confidence": "high",
        "regime_progress": 0.5, "regime_evidence": "x", "summary": "x",
        "evidence_scores": {"profitability": 0.1, "institutional": 0.1,
                            "onchain": 0.1, "derivatives": 0.1, "macro": 0.1},
        "signal_board": [], "meta": {"analysis_quality": 8},
    }
    assert Analyzer._validate(result) is True
    for field in ("cycle_position", "cycle_confidence", "regime_progress",
                  "evidence_scores", "summary", "regime_evidence",
                  "signal_board", "tweet_draft", "position_narrative",
                  "risks", "meta"):
        assert f'"{field}"' in SYSTEM_PROMPT
    assert "## 输出格式 (严格 JSON)" in SYSTEM_PROMPT


# ---- build_market_state 接线 ----

def _market_state_components(tmp_path, cycle_cfg, datafeed):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    engine = AlphaEngine({}, sm)
    return {"state": sm, "engine": engine,
            "cfg": {"cycle_context": cycle_cfg}, "datafeed": datafeed}


def test_build_market_state_attaches_cycle_context(tmp_path):
    fake = _FakeDataFeed(result=_klines_result(_rising_prices(300)))
    cfg = _cfg(enabled=True)

    state = build_market_state(_market_state_components(tmp_path, cfg, fake))

    assert state["cycle_context"]["source"] == "datafeed"
    assert state["cycle_context"]["cycle"]["close"] == pytest.approx(249.5)
    assert state["cycle_context"]["top_risk"]["enabled"] is True
    assert state["cycle_context"]["top_risk"]["active"] is False
    assert state["regime"] == "BEAR"


def test_build_market_state_cycle_failure_does_not_block(tmp_path):
    fake = _FakeDataFeed(result=None)
    cfg = _cfg(enabled=True)

    state = build_market_state(_market_state_components(tmp_path, cfg, fake))

    assert "cycle_context" not in state
    assert state["regime"] == "BEAR"


def test_build_market_state_cycle_disabled_by_default(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    engine = AlphaEngine({}, sm)
    state = build_market_state({"state": sm, "engine": engine, "cfg": {}})
    assert "cycle_context" not in state


# ---- 真实 payload 冒烟 (本地归档, 不联网; 文件缺失时跳过) ----

_CSV_PATH = (Path(__file__).resolve().parents[2] / "KlinesData"
             / "BTCUSDT_futures_daily_1d"
             / "backtest_data_BTCUSDT_1d_daily_20191231-20260921.csv.gz")


def _load_real_bars():
    bars = []
    with gzip.open(_CSV_PATH, "rt", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            bars.append({
                "open_time": int(row["open_time"]), "open": row["open"],
                "high": row["high"], "low": row["low"], "close": row["close"],
            })
    for day, close in (("2026-09-22", 86158.6), ("2026-09-23", 86881.0)):
        stamp = int(datetime.strptime(day, "%Y-%m-%d")
                    .replace(tzinfo=timezone.utc).timestamp() * 1000)
        bars.append({"open_time": stamp, "open": str(close), "high": str(close),
                     "low": str(close), "close": str(close)})
    return bars


@pytest.mark.skipif(not _CSV_PATH.exists(), reason="本地归档不存在")
def test_real_payload_smoke():
    fake = _FakeDataFeed(result={
        "ok": True, "stale": False, "error": None, "source": "local-archive",
        "bars": _load_real_bars(),
    })
    context = cycle_context.build_cycle_context(
        _cfg(), fake, regime={"name": "RECOVERY", "alpha": 0.70})

    assert context is not None
    assert context["bars"] == 2459
    assert context["cycle"]["ath_close"] == 124628.50
    assert context["cycle"]["ath_close_date"] == "2025-10-06"
    assert context["cycle"]["ath_high"] == 126208.50
    assert context["cycle"]["drawdown_pct"] == pytest.approx(-30.3, abs=0.05)
    assert context["cycle"]["low_close"] == 58605.40
    assert context["cycle"]["low_date"] == "2026-06-30"
    assert context["cycle"]["recovery_pct"] == pytest.approx(48.25, abs=0.01)

    analogs = context["analogs"]
    assert analogs["A"]["n"] == 26
    assert analogs["B"]["n"] == 7
    assert analogs["C"]["n"] == 6
    assert analogs["D"]["n"] == 31
    assert analogs["A"]["returns"][90]["median"] == pytest.approx(25.61, abs=0.01)
    assert analogs["A"]["returns"][90]["pct_positive"] == 72.0
    assert analogs["B"]["returns"][365]["median"] == pytest.approx(111.78, abs=0.01)
    assert analogs["B"]["hit_now"] is True
    assert analogs["D"]["fwd_180d_max_dd"]["median"] == pytest.approx(-15.65, abs=0.01)
    assert analogs["D"]["fwd_180d_max_dd"]["worst"] == pytest.approx(-50.06, abs=0.01)

    # 当前 RECOVERY + 价>200SMA + 250SMA↓ → ① 复苏早期 (顶部风险未激活, 不再误判为③)
    assert context["phase"]["code"] == 1
    assert context["phase"]["label"] == "复苏早期"
    assert "250SMA未转正" in context["phase"]["reasons"][0]
    assert context["trend"]["zone_changes_30d"] is not None   # 仅作趋势参考

    top_risk = context["top_risk"]
    assert top_risk["enabled"] is True
    assert top_risk["active"] is False          # 现价即 90 日新高, 回撤 0
    assert top_risk["drawdown_from_90d_high_pct"] == pytest.approx(0.0)
    assert top_risk["distance_200sma_pct"] == pytest.approx(22.76, abs=0.05)
    assert top_risk["since"] is None
    assert top_risk["structure_ma"] == "sma50"

    text = cycle_context.format_cycle_context(context)
    assert "- 阶段: 复苏早期 (code=1)" in text
    assert "- 顶部风险: 无" in text
    assert "250SMA未转正" in text
    assert "ATH收盘 124,628.50 (2025-10-06)" in text
    assert "距ATH -30.3%" in text
    assert "A 首次上穿200SMA (n=26" in text
    assert "B 当前状态类比 (n=7" in text
    assert "C 250SMA斜率转正 (n=6" in text
    assert "D 距200SMA>20%首入 (n=31" in text
    assert len(text.splitlines()) <= 45
