"""周期定位与历史类比上下文 (日线) — 辅助 AI 结合判断周期位置/阶段.

数据流:
    DataFeed 服务 /klines (1d, Binance U 本位) → 日线 OHLC → ATH / 周期低点 / 200·250 SMA
    → 固定规则阶段判定 + 四组历史类比统计 (A/B/C/D, 收盘口径) → 结构化 dict → prompt 小节

四组类比定义 (收盘口径, 连续日去重取区间首日):
    A 首次上穿 200SMA     (前一日 close≤SMA200 且当日 close>SMA200)
    B 当前状态类比        (>200SMA, 200SMA斜率↑, 250SMA斜率↓, 距200SMA 10-30%, 距ATH>30%)
    C 250SMA 斜率转正     (slope250 由 ≤0 转 >0)
    D 距200SMA>20% 首入   (close/SMA200-1 首次 >20%)

均线口径与 MA 模块一致: EMA=ewm(span=N, adjust=False), SMA=rolling(N),
斜率=MA[t]-MA[t-lookback] (>0 记 ↑, 否则 ↓); SMA 在预热期无值。

取数失败 / stale / 数据不足 → 返回 None 并打印 [Cycle] 日志, 不抛异常阻塞主流程。
历史统计样本量有限 (尤其 B), 仅作参考, 不构成短期交易信号。
"""
import statistics
import time
from datetime import date, datetime, timedelta, timezone

from src.ma_context import classify_zone, ema_series, sma_series

_DEFAULT_WINDOWS = [30, 90, 180, 365]
_DEFAULT_MIN_BARS = 300
_DEFAULT_LOW_LOOKBACK_DAYS = 365
_DEFAULT_MERGE_GAP = 5
_DEFAULT_DD_WINDOW = 180
_DEFAULT_DD_GROUPS = ("B", "D")
_DEFAULT_HALVING_DATE = "2024-04-20"

_DEFAULT_PHASE = {"top_deviation_pct": 0.30}
_DEFAULT_ANALOG = {"b_min_ratio": 0.10, "b_max_ratio": 0.30,
                   "b_min_drawdown": 0.30, "d_ratio": 0.20}

_PHASE_LABELS = {4: "熊市", 3: "中后期/顶部", 2: "结构确认", 1: "复苏早期", 0: "过渡期"}
_ANALOG_LABELS = {"A": "首次上穿200SMA", "B": "当前状态类比",
                  "C": "250SMA斜率转正", "D": "距200SMA>20%首入"}

_KLINE_CACHE = {}
_CONTEXT_CACHE = {"key": None, "value": None}


def clear_cache():
    """清空 K 线/上下文缓存 (测试与手工刷新用)."""
    _KLINE_CACHE.clear()
    _CONTEXT_CACHE["key"] = None
    _CONTEXT_CACHE["value"] = None


# ---- 通用计算 ----

def _split_bars(bars):
    """归一化 [(date, close, high), (date, close) 或 close, ...] → (dates, closes, highs)."""
    dates, closes, highs = [], [], []
    for item in bars or []:
        if isinstance(item, (list, tuple)) and len(item) >= 3:
            dates.append(str(item[0]))
            closes.append(float(item[1]))
            highs.append(float(item[2]))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            dates.append(str(item[0]))
            closes.append(float(item[1]))
            highs.append(float(item[1]))
        else:
            dates.append("")
            closes.append(float(item))
            highs.append(float(item))
    return dates, closes, highs


def _to_date(value):
    """str/date/datetime → date; 失败返回 None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _slope_diff(series, index, lookback):
    """MA[t] - MA[t-lookback]; 数据不足/预热期为 None."""
    previous = index - lookback
    if previous < 0 or index >= len(series):
        return None
    current, prev_value = series[index], series[previous]
    if current is None or prev_value is None:
        return None
    return current - prev_value


def _slope_dir(diff):
    """斜率方向: up / down / None (MA[t]>MA[t-lookback] 记 ↑, 含平记 ↓)."""
    if diff is None:
        return None
    return "up" if diff > 0 else "down"


def _pct_change(new, old):
    if new is None or old in (None, 0):
        return None
    return (new / old - 1.0) * 100.0


def _run_starts(mask):
    """连续 True 区间取首日 (连续日去重)."""
    starts, previous = [], False
    for index, flag in enumerate(mask or []):
        if flag and not previous:
            starts.append(index)
        previous = bool(flag)
    return starts


def merge_signals(indices, gap=5):
    """相邻信号间隔≤gap 个交易日视为同一簇, 仅保留簇首 (去抖)."""
    merged = []
    for index in indices or []:
        if merged and index - merged[-1] <= gap:
            continue
        merged.append(index)
    return merged


def _count_days_above(closes, ma_values, window):
    if not ma_values:
        return None
    start = max(0, len(closes) - window)
    count = 0
    for index in range(start, len(closes)):
        if ma_values[index] is not None and closes[index] > ma_values[index]:
            count += 1
    return count


def _count_zone_changes(zones, window):
    tail = [zone for zone in zones if zone][-(window + 1):]
    return sum(1 for a, b in zip(tail, tail[1:]) if a != b)


def _zone_from_closes(closes, cfg=None):
    """本地计算当前区间 + 近30日区间变更次数 (未复用 ma_context 时的回退)."""
    series_by_key = {}
    for period in (5, 10, 20):
        series_by_key[f"ema{period}"] = ema_series(closes, period)
    for period in (50, 100, 200, 250):
        series_by_key[f"sma{period}"] = sma_series(closes, period)
    zones = []
    for index in range(len(closes)):
        values = {key: values[index] for key, values in series_by_key.items()}
        if any(value is None for value in values.values()):
            zones.append("")
            continue
        zones.append(classify_zone(closes[index], values, cfg))
    return (zones[-1] if zones else ""), _count_zone_changes(zones, 30)


def _extract_regime(regime, alpha=None):
    """归一化 regime 参数 (str / dict / 带 get_regime 的引擎对象) → (name, alpha)."""
    name = None
    if isinstance(regime, dict):
        name = regime.get("name") or regime.get("regime") or regime.get("current")
        if alpha is None:
            alpha = regime.get("alpha")
    elif isinstance(regime, str):
        name = regime or None
    elif regime is not None:
        getter = getattr(regime, "get_regime", None)
        if callable(getter):
            try:
                name = getter()
            except Exception:
                name = None
        if alpha is None:
            alpha_getter = getattr(regime, "get_alpha", None)
            if callable(alpha_getter):
                try:
                    alpha = alpha_getter()
                except Exception:
                    alpha = None
    try:
        alpha = float(alpha) if alpha is not None else None
    except (TypeError, ValueError):
        alpha = None
    return (str(name) if name else None), alpha


# ---- 阶段判定 ----

def classify_phase(close, sma200, sma250, slope200, slope250, *,
                   regime=None, alpha=None, cfg=None):
    """固定规则阶段判定 (按 ④→③→②→① 顺序, 未命中为过渡期).

    close/sma200/sma250 为最新值; slope200/slope250 为斜率方向 (up/down/None);
    ③ 触发: 距200SMA偏离 > top_deviation_pct 或 regime=BULL_COOLING;
    阈值可在 cfg.phase 覆盖 (top_deviation_pct)。
    alpha 参数仅保留签名兼容 (仓位由引擎按时间自推, 参与判定会形成
    "时间推进→alpha 升→判顶部" 自反馈回路), 不参与阶段判定。
    区间变更次数 (zone_changes_30d) 仅作 trend 参考, 不参与阶段判定。
    """
    thresholds = dict(_DEFAULT_PHASE)
    thresholds.update((cfg or {}).get("phase") or {})
    ratio = _pct_change(close, sma200)
    if ratio is not None and ratio < 0 and slope200 == "down":
        return {"label": _PHASE_LABELS[4], "code": 4,
                "reasons": [f"close<SMA200 ({ratio:+.1f}%) 且 200SMA斜率↓"]}
    top_reasons = []
    if ratio is not None and ratio > thresholds["top_deviation_pct"] * 100.0:
        top_reasons.append(
            f"距200SMA {ratio:+.1f}% > {thresholds['top_deviation_pct'] * 100.0:.0f}%")
    if regime == "BULL_COOLING":
        top_reasons.append("regime=BULL_COOLING (牛顶确认)")
    if top_reasons:
        return {"label": _PHASE_LABELS[3], "code": 3, "reasons": top_reasons}
    if slope250 == "up" and regime in ("RECOVERY", "BULL"):
        return {"label": _PHASE_LABELS[2], "code": 2,
                "reasons": [f"250SMA斜率↑ 且 regime={regime}"]}
    if regime in ("BEAR_BOTTOM", "RECOVERY") and ratio is not None and ratio > 0 \
            and slope250 == "down":
        return {"label": _PHASE_LABELS[1], "code": 1,
                "reasons": [f"regime={regime} 且 close>SMA200 且 250SMA未转正(斜率↓)"]}
    return {"label": _PHASE_LABELS[0], "code": 0,
            "reasons": ["未满足阶段④③②①任一条件"]}


# ---- 历史类比 ----

def _analog_masks(closes, sma200, sma250, lookback, analog_cfg):
    """四组信号布尔序列 + 200SMA 偏离%/距滚动ATH回撤% 序列."""
    total = len(closes)
    running_high = 0.0
    ratio200, drawdown = [], []
    for index in range(total):
        running_high = max(running_high, closes[index])
        value = sma200[index] if index < len(sma200) else None
        ratio200.append(_pct_change(closes[index], value))
        drawdown.append(_pct_change(closes[index], running_high))
    slope200 = [_slope_diff(sma200, i, lookback) for i in range(total)]
    slope250 = [_slope_diff(sma250, i, lookback) for i in range(total)]

    min_ratio = analog_cfg["b_min_ratio"] * 100.0
    max_ratio = analog_cfg["b_max_ratio"] * 100.0
    min_dd = -analog_cfg["b_min_drawdown"] * 100.0
    d_ratio = analog_cfg["d_ratio"] * 100.0

    mask_a, mask_b, mask_c, mask_d = [], [], [], []
    for index in range(total):
        prev_ratio = ratio200[index - 1] if index > 0 else None
        prev_close = closes[index - 1] if index > 0 else None
        prev_ma200 = sma200[index - 1] if index > 0 else None
        current_ma200 = sma200[index] if index < len(sma200) else None
        mask_a.append(bool(prev_close is not None and prev_ma200 is not None
                           and current_ma200 is not None
                           and prev_close <= prev_ma200 and closes[index] > current_ma200))
        mask_b.append(bool(ratio200[index] is not None
                           and closes[index] > current_ma200
                           and slope200[index] is not None and slope200[index] > 0
                           and slope250[index] is not None and slope250[index] < 0
                           and min_ratio <= ratio200[index] <= max_ratio
                           and drawdown[index] < min_dd))
        prev_slope250 = slope250[index - 1] if index > 0 else None
        mask_c.append(bool(prev_slope250 is not None and prev_slope250 <= 0
                           and slope250[index] is not None and slope250[index] > 0))
        mask_d.append(bool(prev_ratio is not None and current_ma200 is not None
                           and prev_ratio <= d_ratio and ratio200[index] > d_ratio))
    return {"A": mask_a, "B": mask_b, "C": mask_c, "D": mask_d}


def _fwd_returns(closes, indices, window):
    last = len(closes) - 1
    values = []
    for index in indices:
        target = index + window
        if target <= last and closes[index]:
            values.append(closes[target] / closes[index] - 1.0)
    return values


def _fwd_max_dd(closes, index, window):
    target = index + window
    if target > len(closes) - 1 or not closes[index]:
        return None
    lowest = min(closes[index + 1:target + 1])
    return lowest / closes[index] - 1.0


def _stats(values):
    if not values:
        return {"n": 0, "median": None, "pct_positive": None, "min": None, "max": None}
    positive = sum(1 for value in values if value > 0)
    return {
        "n": len(values),
        "median": round(statistics.median(values) * 100.0, 2),
        "pct_positive": round(positive / len(values) * 100.0, 1),
        "min": round(min(values) * 100.0, 2),
        "max": round(max(values) * 100.0, 2),
    }


def _select_dates(dates, indices):
    """最近 5 个 + 最早 1 个 (不足 6 个则全列), 按时间升序."""
    if not indices:
        return []
    if len(indices) <= 6:
        picked = indices
    else:
        picked = [indices[0]] + indices[-5:]
    return [dates[index] if index < len(dates) else "" for index in picked]


def _build_analog(key, indices, mask, dates, closes, *, windows, merge_gap,
                  dd_window, dd_enabled):
    merged = merge_signals(indices, gap=merge_gap)
    returns = {window: _stats(_fwd_returns(closes, indices, window))
               for window in windows}
    item = {
        "label": _ANALOG_LABELS.get(key, key),
        "n": len(indices),
        "n_merged": len(merged),
        "dates": _select_dates(dates, indices),
        "dates_merged": _select_dates(dates, merged),
        "returns": returns,
        "fwd_180d_max_dd": None,
        "hit_now": bool(mask[-1]) if mask else False,
    }
    if dd_enabled:
        values = [value for value in
                  (_fwd_max_dd(closes, index, dd_window) for index in indices)
                  if value is not None]
        if values:
            item["fwd_180d_max_dd"] = {
                "window": dd_window,
                "n": len(values),
                "median": round(statistics.median(values) * 100.0, 2),
                "worst": round(min(values) * 100.0, 2),
            }
    return item


# ---- 主计算 ----

def compute_cycle_context(bars, cfg=None, *, ma_context=None, regime=None,
                          alpha=None, now=None):
    """从日线序列计算周期定位 + 阶段 + 趋势 + 历史类比 (纯计算, 无 IO).

    bars: [(YYYY-MM-DD, close, high), (YYYY-MM-DD, close) 或 close, ...] (升序);
    now:  参考日期 (str/date/datetime), 缺省取最后一根 K 线日期 (便于测试复现);
    ma_context: 传入时复用其 zone / days_above / zone_changes_30d。
    """
    cfg = cfg or {}
    dates, closes, highs = _split_bars(bars)
    if len(closes) < 2:
        return None
    stats_cfg = cfg.get("stats") or {}
    windows = [int(window) for window in (stats_cfg.get("windows") or _DEFAULT_WINDOWS)]
    low_lookback = int(stats_cfg.get("lookback_days", _DEFAULT_LOW_LOOKBACK_DAYS)
                       or _DEFAULT_LOW_LOOKBACK_DAYS)
    merge_gap = int(stats_cfg.get("merge_gap_days", _DEFAULT_MERGE_GAP)
                    or _DEFAULT_MERGE_GAP)
    dd_window = int(stats_cfg.get("dd_window", _DEFAULT_DD_WINDOW)
                    or _DEFAULT_DD_WINDOW)
    dd_groups = tuple(stats_cfg.get("dd_groups") or _DEFAULT_DD_GROUPS)
    analog_cfg = dict(_DEFAULT_ANALOG)
    analog_cfg.update(cfg.get("analog") or {})

    reference = _to_date(now) or _to_date(dates[-1]) or date.today()
    close = closes[-1]

    ath_index = max(range(len(closes)), key=lambda i: closes[i])
    high_index = max(range(len(highs)), key=lambda i: highs[i])
    ath_close = closes[ath_index]
    ath_high = highs[high_index]

    parsed_dates = [_to_date(value) for value in dates]
    low_candidates = [index for index in range(len(closes))
                      if parsed_dates[index] is not None
                      and (reference - parsed_dates[index]).days <= low_lookback]
    if not low_candidates:
        low_candidates = list(range(len(closes)))
    low_index = min(low_candidates, key=lambda i: closes[i])

    sma200 = sma_series(closes, 200)
    sma250 = sma_series(closes, 250)
    last = len(closes) - 1
    slope_lookback = int(cfg.get("slope_lookback", 5) or 5)
    slope200_diff = _slope_diff(sma200, last, slope_lookback)
    slope250_diff = _slope_diff(sma250, last, slope_lookback)
    slope200, slope250 = _slope_dir(slope200_diff), _slope_dir(slope250_diff)
    ratio200 = _pct_change(close, sma200[last] if last < len(sma200) else None)

    snapshot = ((ma_context or {}).get("snapshot") or {}) \
        if isinstance(ma_context, dict) else {}
    days30 = snapshot.get("days_above_long_30d")
    days90 = snapshot.get("days_above_long_90d")
    zone = snapshot.get("zone")
    zone_changes = snapshot.get("zone_changes_30d")
    zone_source = "ma_context" if zone else ""
    if days30 is None:
        days30 = _count_days_above(closes, sma200, 30)
    if days90 is None:
        days90 = _count_days_above(closes, sma200, 90)
    if not zone:
        zone, computed_changes = _zone_from_closes(closes, cfg)
        zone_source = "computed"
        if zone_changes is None:
            zone_changes = computed_changes

    regime_name, alpha = _extract_regime(regime, alpha)
    phase = classify_phase(close, sma200[last], sma250[last], slope200, slope250,
                           regime=regime_name, alpha=alpha, cfg=cfg)

    masks = _analog_masks(closes, sma200, sma250, slope_lookback, analog_cfg)
    analogs = {}
    for key in ("A", "B", "C", "D"):
        indices = _run_starts(masks[key])
        analogs[key] = _build_analog(
            key, indices, masks[key], dates, closes,
            windows=windows, merge_gap=merge_gap, dd_window=dd_window,
            dd_enabled=key in dd_groups)

    halving = _to_date(cfg.get("halving_date") or _DEFAULT_HALVING_DATE)
    low_date = parsed_dates[low_index]
    ath_date = parsed_dates[ath_index]
    caveats = [
        "收盘口径的日线历史统计; 前瞻窗口不足的信号已剔除 (returns.n 为有效样本数)",
        "去抖: 相邻≤{} 交易日信号已并为同簇 (去抖后 A/B/C/D = {}/{}/{}/{})".format(
            merge_gap, *(analogs[key]["n_merged"] for key in ("A", "B", "C", "D"))),
        "B(当前状态)有效窗口仅 {} 例, 且当前命中信号本身无前瞻数据".format(
            analogs["B"]["returns"].get(dd_window, {}).get("n", 0)),
        "200SMA 自 2020-07 起、250SMA 自 2020-09 起才有值, 预热期情形不可观测",
        "近30日区间变更次数仅作波动参考, 不参与阶段判定",
    ]

    return {
        "as_of": dates[last],
        "bars": len(closes),
        "low_lookback_days": low_lookback,
        "cycle": {
            "ath_close": round(ath_close, 2),
            "ath_close_date": dates[ath_index],
            "ath_high": round(ath_high, 2),
            "ath_high_date": dates[high_index],
            "close": round(close, 2),
            "drawdown_pct": round(_pct_change(close, ath_close), 2)
            if close is not None else None,
            "high_drawdown_pct": round(_pct_change(close, ath_high), 2)
            if close is not None else None,
            "low_close": round(closes[low_index], 2),
            "low_date": dates[low_index],
            "recovery_pct": round(_pct_change(close, closes[low_index]), 2),
            "days_since_low": (reference - low_date).days if low_date else None,
            "days_since_ath": (reference - ath_date).days if ath_date else None,
            "days_since_halving": (reference - halving).days if halving else None,
        },
        "phase": phase,
        "trend": {
            "sma200": round(sma200[last], 2) if sma200[last] is not None else None,
            "sma250": round(sma250[last], 2) if sma250[last] is not None else None,
            "slope200": slope200,
            "slope250": slope250,
            "dist_200_pct": round(ratio200, 2) if ratio200 is not None else None,
            "days_above_200_30d": days30,
            "days_above_200_90d": days90,
            "zone": zone or "",
            "zone_source": zone_source,
            "zone_changes_30d": zone_changes,
        },
        "analogs": analogs,
        "caveats": caveats,
    }


# ---- DataFeed 取数 ----

def load_daily_bars(client, interval="1d", limit=None):
    """经 DataFeed 客户端取日线 K 线 → ([(date, close, high), ...], info).

    同一 UTC 日期多根时取时间戳最后一根; 输出按日期升序; 缺 high 时以 close 代替。
    """
    result = client.get_klines(interval=interval, limit=limit) if client else None
    if not result:
        return [], {"ok": False, "stale": False, "error": "无响应", "source": ""}
    info = {
        "ok": bool(result.get("ok")),
        "stale": bool(result.get("stale")),
        "error": result.get("error"),
        "source": result.get("source") or "",
    }
    if not info["ok"] or info["stale"]:
        return [], info

    daily = {}
    for bar in result.get("bars") or []:
        open_time = bar.get("open_time")
        close_raw = bar.get("close")
        if open_time in (None, "") or close_raw in (None, ""):
            continue
        try:
            timestamp_ms = int(open_time)
            close = float(close_raw)
        except (TypeError, ValueError):
            continue
        high_raw = bar.get("high")
        try:
            high = float(high_raw) if high_raw not in (None, "") else close
        except (TypeError, ValueError):
            high = close
        date_str = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc) \
            .strftime("%Y-%m-%d")
        previous = daily.get(date_str)
        if previous is None or timestamp_ms >= previous[0]:
            daily[date_str] = (timestamp_ms, close, high)
    return [(date_str, daily[date_str][1], daily[date_str][2])
            for date_str in sorted(daily)], info


def _get_daily_bars(client, cfg):
    """取日线序列 (TTL 缓存, 避免一轮内重复请求); 返回 (bars, info)."""
    interval = str(cfg.get("interval") or "1d")
    limit = int(cfg.get("kline_limit", 3000) or 3000)
    ttl = float(cfg.get("cache_ttl_seconds", 300) or 0)
    key = (getattr(client, "endpoint", ""), getattr(client, "symbol", ""),
           interval, limit)
    now = time.monotonic()
    cached = _KLINE_CACHE.get(key)
    if cached and ttl > 0 and (now - cached[0]) <= ttl:
        return cached[1], cached[2]
    bars, info = load_daily_bars(client, interval=interval, limit=limit)
    if ttl > 0:
        _KLINE_CACHE[key] = (now, bars, info)
        if len(_KLINE_CACHE) > 8:
            _KLINE_CACHE.pop(next(iter(_KLINE_CACHE)))
    return bars, info


def build_cycle_context(cfg=None, client=None, *, ma_context=None, regime=None,
                        now=None):
    """组装周期上下文: 取数 → 计算 → 缓存.

    参数:
        cfg:        cycle_context 配置段 (缺省用内置默认);
        client:     DataFeed 客户端 (必须注入; 无则返回 None);
        ma_context: 已构建的均线上下文 (复用 zone/days_above/zone_changes_30d);
        regime:     str / dict / 带 get_regime 的引擎对象 (供阶段判定);
        now:        参考日期 (复现/测试用);
    返回 None 并打印 [Cycle] 错误日志的条件: 无客户端 / 异常 / ok=false / stale /
    数据不足 min_bars。不抛异常, 不影响主流程。
    """
    cfg = cfg or {}
    if client is None:
        print("[Cycle] DataFeed 客户端未注入, 跳过周期上下文")
        return None
    try:
        bars, info = _get_daily_bars(client, cfg)
    except Exception as exc:
        print(f"[Cycle] DataFeed 取数异常: {exc}")
        return None
    if not info.get("ok"):
        print(f"[Cycle] DataFeed 不可用 ({info.get('error') or 'ok=false'}), 跳过周期上下文")
        return None
    if info.get("stale"):
        print(f"[Cycle] DataFeed 数据 stale (source={info.get('source') or '?'}), 跳过周期上下文")
        return None
    min_bars = int(cfg.get("min_bars", _DEFAULT_MIN_BARS) or 0)
    if not bars or len(bars) < min_bars:
        print(f"[Cycle] 日线数据不足 (需 ≥{min_bars} 根, 实际 {len(bars)}), 跳过周期上下文")
        return None

    regime_name, alpha = _extract_regime(regime, None)
    snapshot = ((ma_context or {}).get("snapshot") or {}) \
        if isinstance(ma_context, dict) else {}
    cache_key = (
        getattr(client, "endpoint", ""), getattr(client, "symbol", ""),
        str(cfg.get("interval") or "1d"), int(cfg.get("kline_limit", 3000) or 3000),
        info.get("source"), bars[-1], len(bars), str(now or ""),
        regime_name, alpha,
        snapshot.get("zone"), snapshot.get("zone_changes_30d"),
        snapshot.get("days_above_long_30d"), snapshot.get("days_above_long_90d"),
    )
    if _CONTEXT_CACHE["key"] == cache_key:
        return _CONTEXT_CACHE["value"]

    context = compute_cycle_context(bars, cfg, ma_context=ma_context,
                                    regime=regime, alpha=alpha, now=now)
    if not context:
        print("[Cycle] 周期上下文计算失败 (数据不足), 跳过")
        return None
    context["source"] = "datafeed"
    _CONTEXT_CACHE["key"] = cache_key
    _CONTEXT_CACHE["value"] = context
    return context


# ---- 渲染 ----

def _fmt_price(value):
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "?"


def _fmt_pct(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    if abs(number) < 0.05:
        return "0.0%"
    return f"{number:+.1f}%"


def _fmt_days(value):
    try:
        return f"{int(value)} 天"
    except (TypeError, ValueError):
        return "?"


def _fmt_returns(window_stats):
    if not window_stats or window_stats.get("median") is None:
        return "n/a"
    text = f"{_fmt_pct(window_stats['median'])}"
    if window_stats.get("pct_positive") is not None:
        text += f"({float(window_stats['pct_positive']):.0f}%正)"
    return text


def format_cycle_context(ctx):
    """渲染周期上下文中文小节 (≤45 行); 无上下文返回空字符串."""
    if not isinstance(ctx, dict) or not ctx:
        return ""
    cycle = ctx.get("cycle") or {}
    phase = ctx.get("phase") or {}
    trend = ctx.get("trend") or {}
    analogs = ctx.get("analogs") or {}
    caveats = ctx.get("caveats") or []

    lines = ["## 周期定位与历史类比 (日线, 参考)"]
    lines.append(
        f"- 周期位置: ATH收盘 {_fmt_price(cycle.get('ath_close'))}"
        f" ({cycle.get('ath_close_date') or '?'})"
        f" | 最高价ATH {_fmt_price(cycle.get('ath_high'))}"
        f" | 现价 {_fmt_price(cycle.get('close'))}"
        f" | 距ATH {_fmt_pct(cycle.get('drawdown_pct'))}")
    lines.append(
        f"- 周期低点: {_fmt_price(cycle.get('low_close'))}"
        f" ({cycle.get('low_date') or '?'}, 近{ctx.get('low_lookback_days') or _DEFAULT_LOW_LOOKBACK_DAYS}日最低收盘)"
        f" | 低点恢复 {_fmt_pct(cycle.get('recovery_pct'))}"
        f" | 距低点 {_fmt_days(cycle.get('days_since_low'))}"
        f" | 距ATH {_fmt_days(cycle.get('days_since_ath'))}"
        f" | 距减半 {_fmt_days(cycle.get('days_since_halving'))}")
    phase_reason = "；".join(phase.get("reasons") or []) or "?"
    lines.append(f"- 阶段: {phase.get('label') or '?'} "
                 f"(code={phase.get('code', 0)}) | 依据: {phase_reason}")
    lines.append(
        f"- 趋势: 200SMA {_fmt_price(trend.get('sma200'))}"
        f"({_fmt_pct(trend.get('dist_200_pct'))},{trend.get('slope200') or '?'})"
        f" | 250SMA {_fmt_price(trend.get('sma250'))}({trend.get('slope250') or '?'})"
        f" | 近30/90日站上200SMA {trend.get('days_above_200_30d')}/"
        f"{trend.get('days_above_200_90d')} 天")
    zone_line = f"- 当前区间: {trend.get('zone') or '?'}"
    if trend.get("zone_changes_30d") is not None:
        zone_line += f" | 近30日区间变更 {trend['zone_changes_30d']} 次"
    lines.append(zone_line)

    lines.append("- 历史类比 (收盘口径, 前瞻收益=入场收盘→N日后收盘):")
    windows = [30, 90, 180, 365]
    for key in ("A", "B", "C", "D"):
        item = analogs.get(key) or {}
        if not item:
            continue
        header = f"  - {key} {item.get('label') or _ANALOG_LABELS.get(key, key)}" \
                 f" (n={item.get('n', 0)}, 去抖{item.get('n_merged', 0)}"
        if item.get("hit_now"):
            header += ", 当前命中"
        header += ")"
        stats = item.get("returns") or {}
        cells = [f"{window}d {_fmt_returns(stats.get(window))}"
                 for window in windows if stats.get(window)]
        line = header + ": " + " | ".join(cells)
        dd = item.get("fwd_180d_max_dd")
        if dd:
            line += (f" | 未来{dd.get('window', 180)}d最大回撤 "
                     f"中位{_fmt_pct(dd.get('median'))} 最差{_fmt_pct(dd.get('worst'))}")
        lines.append(line)

    for caveat in caveats:
        lines.append(f"- 风险提示: {caveat}")
    return "\n".join(lines) + "\n"
