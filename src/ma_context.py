"""移动平均线区间/趋势上下文 (日线) — 辅助 AI 判断周期位置与价格结构是否一致.

数据流:
    DataFeed 服务 /klines (1d, Binance U 本位) → 日线收盘 → 5/10/20 EMA + 50/100/200/250 SMA
    → 区间标签 / 穿越事件 / 历史统计 → 按日去重的 JSONL 记忆 (跨轮连续性)

DataFeed 不可用 (ok=false / stale=true / 数据不足) → 不输出均线段并打印错误日志,
不回退读取本地归档 (用户决策: 宁缺毋滥)。

备忘单语义 (写入 prompt): 5 EMA ⚡动能 | 10 EMA 🔍短期趋势 | 20 EMA 🎯均值回归 |
50 SMA 🛡️强劲上升趋势支撑 | 100 SMA 📉回调买入警报 | 200 SMA 🔄趋势转变 | 250 SMA 💰公允价值

本模块只通过注入的 DataFeed 客户端取数 + 纯计算, 失败返回空值, 不抛异常阻塞主流程。
"""
import json
import os
import time
from datetime import datetime, timezone

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_MA_LABELS = {
    "ema5": "5EMA", "ema10": "10EMA", "ema20": "20EMA",
    "sma50": "50SMA", "sma100": "100SMA", "sma200": "200SMA", "sma250": "250SMA",
}

_ZONE_STRONG = "强势多头区"
_ZONE_PULLBACK = "上升趋势回调区"
_ZONE_TRANSITION = "趋势转变观察区"
_ZONE_WEAK_REBOUND = "转弱/反抽区"
_ZONE_BEAR = "空头区"

_DEFAULT_PERIODS = {"ema": [5, 10, 20], "sma": [50, 100, 200, 250]}
_DEFAULT_ZONE_THRESHOLDS = {"long_trend": "sma200", "fair_value": "sma250",
                            "mid_term": "sma100"}

_KLINE_CACHE = {}
_CONTEXT_CACHE = {"key": None, "value": None}


def clear_cache():
    """清空 K 线/上下文缓存 (测试与手工刷新用)."""
    _KLINE_CACHE.clear()
    _CONTEXT_CACHE["key"] = None
    _CONTEXT_CACHE["value"] = None


def ema_series(values, period):
    """EMA 数列 (与 pandas ewm(span=period, adjust=False) 一致: 首值为种子)."""
    out = [None] * len(values)
    if not values or period <= 0:
        return out
    alpha = 2.0 / (period + 1.0)
    prev = float(values[0])
    for i, value in enumerate(values):
        if i == 0:
            out[i] = prev
            continue
        prev = alpha * float(value) + (1.0 - alpha) * prev
        out[i] = prev
    return out


def sma_series(values, period):
    """SMA 数列 (前 period-1 项为 None)."""
    out = [None] * len(values)
    if not values or period <= 0:
        return out
    running = 0.0
    for i, value in enumerate(values):
        running += float(value)
        if i >= period:
            running -= float(values[i - period])
        if i >= period - 1:
            out[i] = running / period
    return out


def _split_series(series):
    """归一化 [(date, close), ...] 或 [close, ...] → (dates, closes)."""
    dates, closes = [], []
    for item in series or []:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            dates.append(str(item[0]))
            closes.append(float(item[1]))
        else:
            dates.append("")
            closes.append(float(item))
    return dates, closes


def _slope_label(values, index, lookback, flat_pct):
    """与 lookback 日前比较: up / flat / down; 数据不足返回 None."""
    prev_index = index - lookback
    if prev_index < 0 or index >= len(values):
        return None
    current, previous = values[index], values[prev_index]
    if current is None or previous is None:
        return None
    if not previous:
        return "flat"
    change_pct = (current - previous) / previous * 100.0
    if change_pct > flat_pct:
        return "up"
    if change_pct < -flat_pct:
        return "down"
    return "flat"


def classify_zone(price, ma_values, cfg=None):
    """价格相对均线的区间档位 (阈值角色可在 cfg.zone_thresholds 调整)."""
    thresholds = (cfg or {}).get("zone_thresholds") or _DEFAULT_ZONE_THRESHOLDS
    long_key = thresholds.get("long_trend", "sma200")
    fair_key = thresholds.get("fair_value", "sma250")
    mid_key = thresholds.get("mid_term", "sma100")
    if not ma_values:
        return ""
    above = {key: (value is not None and price > value)
             for key, value in ma_values.items()}
    if above and all(above.values()):
        return _ZONE_STRONG
    if above.get(long_key) and above.get(fair_key):
        return _ZONE_PULLBACK
    if above.get(long_key) or above.get(fair_key):
        return _ZONE_TRANSITION
    if above.get(mid_key):
        return _ZONE_WEAK_REBOUND
    return _ZONE_BEAR


def _detect_events(dates, prices, series_by_key, lookback, flat_pct, window):
    """近 window 个交易日的均线事件: 穿越 / 50-200 金叉死叉 / 50、200 斜率翻转."""
    events = []
    total = len(prices)
    start = max(1, total - max(1, window))
    for j in range(start, total):
        date = dates[j] if j < len(dates) else ""
        price, prev_price = prices[j], prices[j - 1]
        for key, values in series_by_key.items():
            current, previous = values[j], values[j - 1]
            if current is None or previous is None:
                continue
            label = _MA_LABELS.get(key, key.upper())
            if prev_price <= previous and price > current:
                events.append({"date": date, "text": f"上穿 {label}"})
            elif prev_price >= previous and price < current:
                events.append({"date": date, "text": f"下穿 {label}"})
        s50, s200 = series_by_key.get("sma50"), series_by_key.get("sma200")
        if s50 and s200 and None not in (s50[j - 1], s50[j], s200[j - 1], s200[j]):
            if s50[j - 1] <= s200[j - 1] and s50[j] > s200[j]:
                events.append({"date": date, "text": "50/200 金叉"})
            elif s50[j - 1] >= s200[j - 1] and s50[j] < s200[j]:
                events.append({"date": date, "text": "50/200 死叉"})
        for key in ("sma50", "sma200"):
            values = series_by_key.get(key)
            if not values:
                continue
            current_slope = _slope_label(values, j, lookback, flat_pct)
            prev_slope = _slope_label(values, j - 1, lookback, flat_pct)
            if current_slope is None or prev_slope is None:
                continue
            label = _MA_LABELS.get(key, key.upper())
            if current_slope == "down" and prev_slope != "down":
                events.append({"date": date, "text": f"{label} 斜率转下"})
            elif current_slope == "up" and prev_slope != "up":
                events.append({"date": date, "text": f"{label} 斜率转上"})
    events.reverse()  # 新的在前
    deduped, seen = [], set()
    for event in events:
        signature = (event["date"], event["text"])
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(event)
    return deduped


def _count_days_above(prices, ma_values, window):
    if not ma_values:
        return None
    start = max(0, len(prices) - window)
    count = 0
    for i in range(start, len(prices)):
        if ma_values[i] is not None and prices[i] > ma_values[i]:
            count += 1
    return count


def _count_days_above_streak(prices, ma_values):
    """连续站上长均线天数 (自最新一根向前); 均线未就绪/无数据 → None.

    WP8 节奏门控 G1 用: "连续 reclaim_confirm_days 日收盘 > SMA200" 的结构修复确认。
    """
    if not prices or not ma_values or ma_values[-1] is None:
        return None
    count = 0
    for i in range(len(prices) - 1, -1, -1):
        value = ma_values[i] if i < len(ma_values) else None
        if value is None or prices[i] <= value:
            break
        count += 1
    return count


def _zone_series(prices, series_by_key, cfg):
    keys = list(series_by_key.keys())
    zones = []
    for i in range(len(prices)):
        values, ready = {}, True
        for key in keys:
            value = series_by_key[key][i]
            if value is None:
                ready = False
                break
            values[key] = value
        zones.append(classify_zone(prices[i], values, cfg) if ready else "")
    return zones


def _count_zone_changes(zones, window):
    tail = [zone for zone in zones if zone][-(window + 1):]
    return sum(1 for a, b in zip(tail, tail[1:]) if a != b)


def compute_ma_snapshot(series, cfg=None, as_of=None):
    """计算最新均线快照: 各均线值/位置/距离/斜率 + 区间 + 事件 + 历史统计.

    series: [(YYYY-MM-DD, close), ...] 或 [close, ...] (升序);
    as_of:  只使用该 UTC 日期 (含) 之前的数据;
    数据不足 (少于最大均线周期) 返回 None。
    """
    cfg = cfg or {}
    dates, prices = _split_series(series)
    cutoff = str(as_of) if as_of else ""
    if cutoff:
        pairs = [(d, p) for d, p in zip(dates, prices) if not d or d <= cutoff]
        dates = [d for d, _ in pairs]
        prices = [p for _, p in pairs]
    if not prices:
        return None

    periods = cfg.get("periods") or _DEFAULT_PERIODS
    ema_periods = [int(p) for p in (periods.get("ema") or [])]
    sma_periods = [int(p) for p in (periods.get("sma") or [])]
    all_periods = ema_periods + sma_periods
    if not all_periods or len(prices) < max(all_periods):
        return None

    lookback = int(cfg.get("slope_lookback", 5) or 5)
    flat_pct = float(cfg.get("slope_flat_pct", 0.2) or 0.0)
    events_lookback = int(cfg.get("events_lookback", 5) or 5)
    max_events = int(cfg.get("max_events", 5) or 5)

    series_by_key = {}
    for period in ema_periods:
        series_by_key[f"ema{period}"] = ema_series(prices, period)
    for period in sma_periods:
        series_by_key[f"sma{period}"] = sma_series(prices, period)

    index = len(prices) - 1
    price = prices[index]
    mas = {}
    for key in [f"ema{p}" for p in ema_periods] + [f"sma{p}" for p in sma_periods]:
        values = series_by_key.get(key)
        if not values or values[index] is None:
            continue
        value = values[index]
        dist_pct = (price - value) / value * 100.0 if value else 0.0
        mas[key] = {
            "label": _MA_LABELS.get(key, key.upper()),
            "value": round(value, 2),
            "pos": "above" if price > value else "below",
            "dist_pct": round(dist_pct, 2),
            "slope": _slope_label(values, index, lookback, flat_pct),
        }

    thresholds = cfg.get("zone_thresholds") or _DEFAULT_ZONE_THRESHOLDS
    long_key = thresholds.get("long_trend", "sma200")
    long_item = mas.get(long_key) or {}
    zone = classify_zone(price, {k: v["value"] for k, v in mas.items()}, cfg)
    events = _detect_events(dates, prices, series_by_key, lookback, flat_pct,
                            events_lookback)

    return {
        "as_of": dates[index] if index < len(dates) else "",
        "price": round(price, 2),
        "mas": mas,
        "zone": zone,
        "long_ma": long_key,
        "long_ma_label": long_item.get("label", long_key),
        "dist_long_pct": long_item.get("dist_pct"),
        "events": events[:max_events],
        "days_above_long_30d": _count_days_above(prices, series_by_key.get(long_key), 30),
        "days_above_long_90d": _count_days_above(prices, series_by_key.get(long_key), 90),
        "days_above_long_streak": _count_days_above_streak(
            prices, series_by_key.get(long_key)),
        "zone_changes_30d": _count_zone_changes(
            _zone_series(prices, series_by_key, cfg), 30),
    }


def load_daily_closes(client, interval="1d", limit=None):
    """经 DataFeed 客户端取日线 K 线 → ([(YYYY-MM-DD, close), ...], info).

    info = {ok, stale, error, source}; DataFeed 不可用/空数据时 series=[]。
    同一 UTC 日期多根时取时间戳最后一根 close, 输出按日期升序。
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
        date = datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc) \
            .strftime("%Y-%m-%d")
        previous = daily.get(date)
        if previous is None or timestamp_ms >= previous[0]:
            daily[date] = (timestamp_ms, close)
    return [(date, daily[date][1]) for date in sorted(daily)], info


def load_history(history_file):
    """读取历史快照 JSONL (跳过坏行), 返回条目列表."""
    entries = []
    if not history_file or not os.path.exists(history_file):
        return entries
    try:
        with open(history_file, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("as_of"):
                    entries.append(obj)
    except OSError as exc:
        print(f"[MA] 历史读取失败 ({history_file}): {exc}")
    return entries


def update_history(history_file, snapshot, keep=400):
    """按 as_of 去重追加快照, 仅保留最近 keep 条; 返回更新后的历史列表."""
    if not history_file or not snapshot:
        return []
    entries = [e for e in load_history(history_file)
               if e.get("as_of") != snapshot.get("as_of")]
    entries.append(snapshot)
    if keep and keep > 0:
        entries = entries[-int(keep):]
    try:
        os.makedirs(os.path.dirname(history_file) or ".", exist_ok=True)
        tmp = history_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        os.replace(tmp, history_file)
    except OSError as exc:
        print(f"[MA] 历史写入失败 ({history_file}): {exc}")
    return entries


def _resolve_history_file(cfg, override=None):
    raw = override or cfg.get("history_file") or "data/ma_history.jsonl"
    return raw if os.path.isabs(raw) \
        else os.path.normpath(os.path.join(_PROJECT_ROOT, raw))


def _get_daily_series(client, cfg):
    """取日线序列 (TTL 缓存, 避免一轮内重复请求); 返回 (series, info)."""
    interval = str(cfg.get("interval") or "1d")
    limit = int(cfg.get("kline_limit", 400) or 400)
    ttl = float(cfg.get("cache_ttl_seconds", 300) or 0)
    key = (getattr(client, "endpoint", ""), getattr(client, "symbol", ""),
           interval, limit)
    now = time.monotonic()
    cached = _KLINE_CACHE.get(key)
    if cached and ttl > 0 and (now - cached[0]) <= ttl:
        return cached[1], cached[2]
    series, info = load_daily_closes(client, interval=interval, limit=limit)
    if ttl > 0:
        _KLINE_CACHE[key] = (now, series, info)
        if len(_KLINE_CACHE) > 8:
            _KLINE_CACHE.pop(next(iter(_KLINE_CACHE)))
    return series, info


def _history_digest(entry):
    return {
        "as_of": entry.get("as_of", ""),
        "zone": entry.get("zone", ""),
        "price": entry.get("price"),
        "dist_long_pct": entry.get("dist_long_pct"),
    }


def build_ma_context(cfg=None, client=None, as_of=None, history_file=None,
                     persist=True):
    """组装均线上下文: 最新快照 + 近期事件 + 历史统计 + 最近 3 条历史摘要.

    数据来自注入的 DataFeed 客户端 (不回退本地归档);
    DataFeed 不可用 / stale / 数据不足 → 返回 None 并打印错误日志;
    persist=False 时只读历史 (不写 JSONL), 供 --test-ai / 回溯等路径使用。
    """
    cfg = cfg or {}
    if client is None:
        print("[MA] DataFeed 客户端未注入, 跳过均线上下文")
        return None
    try:
        series, info = _get_daily_series(client, cfg)
    except Exception as exc:
        print(f"[MA] DataFeed 取数异常: {exc}")
        return None
    if not info.get("ok"):
        print(f"[MA] DataFeed 不可用 ({info.get('error') or 'ok=false'}), 跳过均线上下文")
        return None
    if info.get("stale"):
        print(f"[MA] DataFeed 数据 stale (source={info.get('source') or '?'}), 跳过均线上下文")
        return None
    if not series:
        print("[MA] DataFeed 返回空 bars, 跳过均线上下文")
        return None

    history_file = _resolve_history_file(cfg, history_file)
    interval = str(cfg.get("interval") or "1d")
    limit = int(cfg.get("kline_limit", 400) or 400)
    freshness = (series[-1][0], series[-1][1], len(series))
    cache_key = (getattr(client, "endpoint", ""), getattr(client, "symbol", ""),
                 interval, limit, str(as_of or ""), history_file, bool(persist),
                 info.get("source"), bool(info.get("stale")), freshness)
    if _CONTEXT_CACHE["key"] == cache_key:
        return _CONTEXT_CACHE["value"]

    if as_of:
        series = [(date, close) for date, close in series if date <= str(as_of)]
    snapshot = compute_ma_snapshot(series, cfg)
    if not snapshot:
        print("[MA] 日线数据不足 (需至少最大均线周期根), 跳过均线上下文")
        return None

    if persist:
        keep = int(cfg.get("history_keep", 400) or 400)
        history = update_history(history_file, snapshot, keep=keep)
    else:
        history = load_history(history_file)
    prior = [e for e in history if e.get("as_of") != snapshot.get("as_of")]
    last = prior[-1] if prior else None
    events = snapshot.get("events") or []
    zone_changed = bool(last and last.get("zone") != snapshot.get("zone"))
    context = {
        "as_of": snapshot.get("as_of", ""),
        "source": "datafeed",
        "snapshot": snapshot,
        "events": events,
        "stats": {
            "long_ma": snapshot.get("long_ma"),
            "long_ma_label": snapshot.get("long_ma_label"),
            "days_above_long_30d": snapshot.get("days_above_long_30d"),
            "days_above_long_90d": snapshot.get("days_above_long_90d"),
            "zone_changes_30d": snapshot.get("zone_changes_30d"),
        },
        "last_zone": last.get("zone") if last else None,
        "last_as_of": last.get("as_of") if last else None,
        "zone_changed": zone_changed,
        "zone_change_reason": events[0].get("text", "") if (zone_changed and events) else "",
        "recent_history": [_history_digest(e) for e in prior[-3:]][::-1],
    }
    _CONTEXT_CACHE["key"] = cache_key
    _CONTEXT_CACHE["value"] = context
    return context


def summarize_ma_context(ctx):
    """从均线上下文提取 Web 展示用摘要; 无上下文 → available=False."""
    if not isinstance(ctx, dict) or not ctx:
        return {"available": False, "as_of": None, "zone": None, "price": None}
    snapshot = ctx.get("snapshot")
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    as_of = ctx.get("as_of") or snapshot.get("as_of")
    zone = snapshot.get("zone")
    price = snapshot.get("price")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    return {
        "available": True,
        "as_of": str(as_of) if as_of else None,
        "zone": str(zone) if zone else None,
        "price": price,
    }
