#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WP8 8C 节奏校准报告 — 结构段长/里程碑/现表对照 + 门控参数敏感性扫描 (只读).

复用 `%TEMP%\\opencode\\ae_rhythm_calib_report.md` 的方法:
- 结构相位 (与 classify_phase 优先级一致, 不依赖 proxy regime):
  ④熊市 = close<SMA200 且 200SMA斜率↓; ③顶部风险 = top_risk.active 且非④;
  ②主升 = close>SMA200 且 250SMA斜率↑ 且非③;
  ①复苏 = close>SMA200 且 250SMA斜率↓ 且非③; 过渡 = 其余 (反抽/预热)。
- 门控参数敏感性: cap {0.3,0.5,0.7} × confirm {5,10,20} × threshold {0.6,0.7,0.8},
  在 K 线代理 regime 时间线上回放 (真实 apply_rhythm_gates), 输出差异矩阵
  (熊段 alpha 均值 / 牛市段 alpha 均值 / 首次分叉点 / 末值差), 供参数稳健性判断。

数据源优先级: --csv > 归档目录最新 CSV.gz > DataFeed HTTP (config.json)。
纯只读: 不写 data/、不触网 (除 DataFeed 回退取数)、不改任何引擎状态。

用法:
    py -3 tools/rhythm_report.py
    py -3 tools/rhythm_report.py --csv <path.csv[.gz]> --out report.md
"""
import argparse
import csv
import gzip
import json
import os
import statistics
import sys
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.alpha_engine import (FORWARD_NEXT_REGIME, NEUTRAL_REGIMES,  # noqa: E402
                              REGIME_ALPHA_MAP, REGIME_EXPECTED_DAYS)
from src.cycle_context import _slope_diff, _slope_dir, compute_top_risk  # noqa: E402
from src.ma_context import sma_series  # noqa: E402
from src.rhythm_gates import apply_rhythm_gates  # noqa: E402

SLOPE_LOOKBACK = 5
_ARCHIVE_REL = os.path.join("KlinesData", "BTCUSDT_futures_daily_1d")
_PHASE_BEAR = "④熊市"
_PHASE_TOP = "③顶部风险"
_PHASE_BULL = "②主升"
_PHASE_RECOVERY = "①复苏"
_PHASE_WARM = "预热"
_PHASE_OTHER = "过渡"
_BEAR_REGIMES = ("BEAR", "BEAR_DEEP", "BEAR_BOTTOM")
_BULL_REGIMES = ("BULL", "DEEP_BULL")
_NEUTRAL = set(NEUTRAL_REGIMES)


# ---- 数据加载 ----

def load_rows(path):
    """读取 CSV/CSV.GZ → [(date, close, high)] 升序; 坏行跳过."""
    opener = gzip.open if str(path).lower().endswith(".gz") else open
    rows = []
    with opener(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            date = (row.get("timestamp") or row.get("date")
                    or row.get("open_time") or "")[:10]
            close = row.get("close")
            if not date or close in (None, ""):
                continue
            try:
                close = float(close)
            except (TypeError, ValueError):
                continue
            high = row.get("high")
            try:
                high = float(high) if high not in (None, "") else close
            except (TypeError, ValueError):
                high = close
            rows.append((date, close, high))
    rows.sort(key=lambda item: item[0])
    return rows


def resolve_csv(args):
    """返回 (path, source); 未指定 --csv 时找归档目录最新文件."""
    if args.csv:
        return args.csv, "csv"
    archive_dir = args.archive_dir or os.path.normpath(
        os.path.join(REPO_ROOT, "..", _ARCHIVE_REL))
    if os.path.isdir(archive_dir):
        candidates = [name for name in os.listdir(archive_dir)
                      if name.startswith("backtest_data_BTCUSDT_1d_daily_")
                      and name.endswith(".csv.gz")]
        if candidates:
            candidates.sort()
            return os.path.join(archive_dir, candidates[-1]), "archive"
    return None, ""


def load_from_datafeed():
    """DataFeed HTTP 回退 (config.json datafeed 段); 失败返回 []."""
    try:
        with open(os.path.join(REPO_ROOT, "config.json"), "r",
                  encoding="utf-8") as handle:
            config = json.load(handle)
    except Exception as exc:
        print(f"[Rhythm] config.json 读取失败: {exc}")
        return []
    datafeed = config.get("datafeed") or {}
    endpoint = str(datafeed.get("endpoint") or "").rstrip("/")
    if not endpoint:
        return []
    try:
        import requests
        response = requests.get(
            endpoint + "/klines",
            params={"symbol": "BTCUSDT", "interval": "1d", "limit": 3000},
            timeout=15)
        payload = response.json()
    except Exception as exc:
        print(f"[Rhythm] DataFeed 取数失败: {exc}")
        return []
    rows = []
    for bar in payload.get("bars") or []:
        try:
            timestamp = int(bar.get("open_time"))
            close = float(bar.get("close"))
        except (TypeError, ValueError):
            continue
        high_raw = bar.get("high")
        try:
            high = float(high_raw) if high_raw not in (None, "") else close
        except (TypeError, ValueError):
            high = close
        date = datetime.fromtimestamp(timestamp / 1000.0, tz=timezone.utc) \
            .strftime("%Y-%m-%d")
        rows.append((date, close, high))
    rows.sort(key=lambda item: item[0])
    return rows


# ---- 序列与结构 ----

def runs_of(mask):
    out, start = [], None
    for index, flag in enumerate(mask):
        if flag and start is None:
            start = index
        elif not flag and start is not None:
            out.append((start, index - 1))
            start = None
    if start is not None:
        out.append((start, len(mask) - 1))
    return out


def bridge(runs, gap=5):
    merged = []
    for start, end in runs:
        if merged and start - merged[-1][1] - 1 <= gap:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def compute_series(dates, closes, top_cfg):
    n = len(closes)
    sma50 = sma_series(closes, 50)
    sma200 = sma_series(closes, 200)
    sma250 = sma_series(closes, 250)

    def slope(series, index):
        return _slope_dir(_slope_diff(series, index, SLOPE_LOOKBACK))

    slope200 = [slope(sma200, i) for i in range(n)]
    slope250 = [slope(sma250, i) for i in range(n)]
    top = compute_top_risk(dates, closes, sma200, sma50, cfg=top_cfg)
    top_active = [False] * n
    # compute_top_risk 只返回最新状态; 逐前缀重放代价高, 这里用纯规则近似:
    # 距滚动 90 日最高收盘回撤 >= 10% 且 close < sma50 且 close > sma200
    window = int(top_cfg.get("drawdown_window_days", 90) or 90)
    enter_dd = -abs(float(top_cfg.get("enter_drawdown_pct", 0.10) or 0.10)) * 100
    immediate_dd = -abs(float(top_cfg.get("enter_immediate_drawdown_pct", 0.11)
                              or 0.11)) * 100
    exit_dd = -abs(float(top_cfg.get("exit_drawdown_pct", 0.09) or 0.09)) * 100
    confirm_days = max(1, int(top_cfg.get("enter_confirm_days", 2) or 2))
    require_above = bool(top_cfg.get("require_above_200sma", True))
    base_flags, immediate, exits = [], [], []
    for i, close in enumerate(closes):
        start = max(0, i - window + 1)
        peak = max(closes[start:i + 1])
        drawdown = (close / peak - 1.0) * 100 if peak else 0.0
        is_base = bool(drawdown <= enter_dd + 1e-9 and sma50[i] is not None
                       and close < sma50[i]
                       and (not require_above or (sma200[i] is not None
                                                  and close > sma200[i])))
        base_flags.append(is_base)
        immediate.append(bool(is_base and drawdown <= immediate_dd + 1e-9))
        exits.append(bool((sma50[i] is not None and close > sma50[i])
                          or drawdown >= exit_dd - 1e-9))
    for i in range(n):
        top_active[i] = bool(i > 0 and top_active[i - 1] and not exits[i])
        if not top_active[i]:
            confirmed = all(base_flags[i - k] for k in range(confirm_days)) \
                if i - confirm_days + 1 >= 0 else False
            if immediate[i] or confirmed:
                top_active[i] = True
    # 逐前缀调用一次以校验近似 (与引擎口径交叉检查, 仅报告用)
    check = compute_top_risk(dates, closes, sma200, sma50, cfg=top_cfg)
    streak = [0] * n
    for i in range(n):
        if sma200[i] is not None and closes[i] > sma200[i]:
            streak[i] = (streak[i - 1] + 1) if i > 0 else 1
    ath = []
    running = 0.0
    for close in closes:
        running = max(running, close)
        ath.append(running)
    return {"sma50": sma50, "sma200": sma200, "sma250": sma250,
            "slope200": slope200, "slope250": slope250,
            "top_active": top_active, "top_now": bool(check.get("active")),
            "streak": streak, "ath": ath}


def classify_phases(closes, series):
    phases = []
    for i, close in enumerate(closes):
        sma200 = series["sma200"][i]
        slope200 = series["slope200"][i]
        sma250 = series["sma250"][i]
        slope250 = series["slope250"][i]
        if sma200 is None or slope200 is None:
            phases.append(_PHASE_WARM)
        elif close < sma200 and slope200 == "down":
            phases.append(_PHASE_BEAR)
        elif series["top_active"][i]:
            phases.append(_PHASE_TOP)
        elif sma250 is None or slope250 is None:
            phases.append(_PHASE_WARM)
        elif close > sma200 and slope250 == "up":
            phases.append(_PHASE_BULL)
        elif close > sma200 and slope250 == "down":
            phases.append(_PHASE_RECOVERY)
        else:
            phases.append(_PHASE_OTHER)
    return phases


def find_cycles(closes, series, start, n):
    """主熊周期里程碑: 顶/破200/深熊(-40%)/低点/收复200/250转正/末段风险簇."""
    sma200 = series["sma200"]
    slope200 = series["slope200"]
    slope250 = series["slope250"]
    top_active = series["top_active"]
    ath = series["ath"]
    mask = [i >= start and sma200[i] is not None and closes[i] < sma200[i]
            and slope200[i] == "down" for i in range(n)]
    runs = [run for run in bridge(runs_of(mask), gap=5)
            if run[1] - run[0] + 1 >= 30]
    cycles = []
    for begin, end in runs:
        low_bound = max(start, begin - 200)
        top = max(range(low_bound, begin), key=lambda i: closes[i]) \
            if begin > low_bound else begin
        # 破200SMA: 顶后首次收盘 < SMA200 (可能早于 ④ 相位起点, 因 ③ 优先级更高)
        break_index = begin
        for i in range(top, end + 1):
            if sma200[i] is not None and closes[i] < sma200[i]:
                break_index = i
                break
        reclaim = None
        for i in range(end + 1, n):
            if sma200[i] is not None and closes[i] > sma200[i]:
                reclaim = i
                break
        last = reclaim if reclaim is not None else n - 1
        low = min(range(begin, last + 1), key=lambda i: closes[i])
        d40 = None
        for i in range(top, low + 1):
            if closes[i] / ath[i] - 1 <= -0.40:
                d40 = i
                break
        s250up = None
        if reclaim is not None:
            for i in range(reclaim, n):
                if slope250[i] == "up":
                    s250up = i
                    break
        deep = None
        j = top - 1
        while j >= max(start, top - 60):
            if top_active[j]:
                k = j
                while k - 1 >= start and top_active[k - 1]:
                    k -= 1
                deep = k
                break
            j -= 1
        cycles.append({"top": top, "break": break_index, "d40": d40,
                       "low": low, "reclaim": reclaim, "s250up": s250up,
                       "deep": deep})
    return cycles


def build_timeline(cycles, start, n):
    """K 线代理 regime 时间线 (与校准报告口径一致)."""
    entries = [(start, "BULL")]
    for cycle in cycles:
        if cycle["deep"] is not None and cycle["deep"] < cycle["top"]:
            entries.append((cycle["deep"], "DEEP_BULL"))
        entries.append((cycle["top"], "BULL_COOLING"))
        entries.append((cycle["break"], "BEAR"))
        if cycle["d40"] is not None and cycle["break"] <= cycle["d40"] \
                <= cycle["low"]:
            entries.append((cycle["d40"], "BEAR_DEEP"))
        entries.append((cycle["low"], "BEAR_BOTTOM"))
        if cycle["reclaim"] is not None:
            entries.append((cycle["reclaim"], "RECOVERY"))
        if cycle["s250up"] is not None:
            entries.append((cycle["s250up"], "BULL"))
    entries.sort(key=lambda item: item[0])
    regimes = [None] * n
    for k, (index, regime) in enumerate(entries):
        stop = entries[k + 1][0] if k + 1 < len(entries) else n
        for i in range(max(0, index), min(n, stop)):
            regimes[i] = regime
    return regimes


# ---- 回放 (真实 apply_rhythm_gates) ----

def _target(regime, progress, alpha_map):
    base = float(alpha_map.get(regime, 0.0))
    if regime in _NEUTRAL:
        return base
    nxt = FORWARD_NEXT_REGIME.get(regime)
    if nxt is None:
        return base
    progress = max(0.0, min(1.0, progress))
    return round(base + (float(alpha_map.get(nxt, base)) - base) * progress, 4)


def _ctx_for(series, index):
    slopes = {}
    if series["slope250"][index]:
        slopes["sma250"] = {"slope": series["slope250"][index]}
    snapshot = {"mas": slopes,
                "days_above_long_streak": series["streak"][index]}
    return {
        "ma_context": {"snapshot": snapshot},
        "cycle_context": {
            "top_risk": {"active": bool(series["top_active"][index])},
            "trend": {"slope250": series["slope250"][index]},
        },
    }


def replay(regimes, series, gate_cfg, days_map, start, alpha_map,
           min_step=0.015, max_step=0.05):
    progress, alpha, current, deferred = 0.0, None, None, False
    out = []
    for i in range(start, len(regimes)):
        regime = regimes[i]
        if regime is None:
            continue
        if regime != current:
            current = regime
            progress = 0.0
            target = _target(regime, progress, alpha_map)
            if alpha is None:
                alpha = target if regime not in _NEUTRAL else 0.0
                deferred = False
            elif regime in _NEUTRAL:
                deferred = False
            elif alpha * target < 0:
                alpha, deferred = 0.0, True
            else:
                alpha, deferred = target, False
        else:
            if deferred and regime not in _NEUTRAL:
                alpha = _target(regime, progress, alpha_map)
                deferred = False
            if regime not in _NEUTRAL:
                expected = days_map.get(regime, 180)
                progress = min(1.0, progress + 1.0 / expected)
                gated, _reasons = apply_rhythm_gates(regime, progress,
                                                     _ctx_for(series, i),
                                                     gate_cfg)
                if isinstance(gated, (int, float)) \
                        and not isinstance(gated, bool):
                    progress = max(0.0, min(1.0, gated))
                target = _target(regime, progress, alpha_map)
                if abs(alpha - target) >= 0.005:
                    step = max(min_step, min(1.0 / expected, max_step))
                    diff = target - alpha
                    alpha = round(alpha + max(-step, min(step, diff)), 4)
        out.append((i, regime, progress, alpha))
    return out


def _mean(values):
    return sum(values) / len(values) if values else 0.0


def _summary(sim):
    bear = [alpha for _i, regime, _p, alpha in sim if regime in _BEAR_REGIMES]
    bull = [alpha for _i, regime, _p, alpha in sim if regime in _BULL_REGIMES]
    final = sim[-1][3] if sim else 0.0
    return _mean(bear), _mean(bull), final


def _first_divergence(base_sim, sim, dates):
    for (_bi, _br, _bp, base_alpha), (i, _r, _p, alpha) in zip(base_sim, sim):
        if abs(alpha - base_alpha) > 1e-9:
            return dates[i]
    return "-"


# ---- 报告 ----

def build_report(dates, closes, series, phases, cycles, regimes, config, source):
    lines = []
    n = len(closes)
    warm = [i for i in range(n) if phases[i] == _PHASE_WARM]
    warm_end = max(warm) if warm else -1
    start = warm_end + 1
    alpha_map = dict(REGIME_ALPHA_MAP)
    alpha_map.update(config.get("regime_alpha_map") or {})
    days_map = dict(REGIME_EXPECTED_DAYS)
    days_map.update(config.get("regime_expected_days") or {})
    smoothing = config.get("smoothing") or {}
    min_step = float(smoothing.get("min_daily_step", 0.015) or 0.015)
    max_step = float(smoothing.get("max_change_per_step", 0.05) or 0.05)

    last = n - 1
    lines += ["# AlphaEngine 节奏校准报告 (WP8 8C, 只读)", "",
              f"- 生成: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC",
              f"- 数据: `{source}` — {n} 根, {dates[0]} → {dates[-1]}",
              f"- 可判定起点: {dates[start] if 0 <= start < n else '-'} "
              f"(预热结束; 200/250SMA 就绪)",
              f"- 最新: 相位={phases[last]} | close={closes[last]:,.0f} | "
              f"距ATH {(closes[last] / series['ath'][last] - 1) * 100:+.1f}% | "
              f"top_risk={'激活' if series['top_now'] else '无'}",
              f"- 现表: " + ", ".join(
                  f"{key}={days_map.get(key)}" for key in
                  ("BEAR", "BEAR_DEEP", "BEAR_BOTTOM", "RECOVERY", "BULL",
                   "DEEP_BULL", "BULL_COOLING")),
              f"- 步长: clamp(1/预期天数, {min_step:g}, {max_step:g})",
              "- 结构相位: ④熊市=close<SMA200 且 200SMA↓; ③=top_risk.active; "
              "②=close>SMA200 且 250SMA↑; ①=close>SMA200 且 250SMA↓; 其余过渡",
              ""]

    # ---- 1. 结构段长统计 ----
    lines += ["## 1. 结构段长统计 (交易日)", ""]
    lines += ["| 相位 | 原始段数 | 主段数(桥接≤5+≥30) | 主段中位 | 主段均值 | "
              "主段范围 |", "|---|---|---|---|---|---|"]
    for phase in (_PHASE_BEAR, _PHASE_RECOVERY, _PHASE_BULL, _PHASE_TOP):
        raw = runs_of([p == phase for p in phases])
        major = [run for run in bridge(raw, 5) if run[1] - run[0] + 1 >= 30]
        lengths = [end - begin + 1 for begin, end in major]
        if lengths:
            lines.append(
                f"| {phase} | {len(raw)} | {len(major)} | "
                f"{statistics.median(lengths):.0f} | "
                f"{statistics.mean(lengths):.1f} | "
                f"{min(lengths)}-{max(lengths)} |")
        else:
            lines.append(f"| {phase} | {len(raw)} | 0 | - | - | - |")
    lines.append("")

    # ---- 2. 里程碑 ----
    lines += ["## 2. 关键里程碑 (主熊周期)", "",
              "| # | 顶 | 破200SMA | -40% | 低点 | 收复200SMA | 250SMA转正 | "
              "顶→破线 | 破线→低 | 低→收复 |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for k, cycle in enumerate(cycles, 1):
        def date_at(index):
            return dates[index] if index is not None else "-"
        span = (cycle["low"] - cycle["break"]) if cycle["low"] else "-"
        reclaim_span = (cycle["reclaim"] - cycle["low"]) \
            if cycle["reclaim"] is not None else "-"
        lines.append(
            f"| {k} | {date_at(cycle['top'])} | {date_at(cycle['break'])} | "
            f"{date_at(cycle['d40'])} | {date_at(cycle['low'])} | "
            f"{date_at(cycle['reclaim'])} | {date_at(cycle['s250up'])} | "
            f"{cycle['break'] - cycle['top']} | {span} | {reclaim_span} |")
    lines.append("")

    # ---- 3. 现表对照 ----
    # 每周期窗口 (上一顶→本顶; 不含最后一个进行中窗口) 内的
    # ②主升 / ③顶部风险 累计天数 (口径与校准报告一致)
    tops = [cycle["top"] for cycle in cycles]
    windows, previous = [], start
    for top in tops:
        windows.append((previous, top))
        previous = top
    bull_cumulative = [sum(1 for i in range(a, b + 1)
                           if phases[i] == _PHASE_BULL) for a, b in windows]
    top_cumulative = [sum(1 for i in range(a, b + 1)
                          if phases[i] == _PHASE_TOP) for a, b in windows]

    def _segment_median(phase):
        raw = runs_of([p == phase for p in phases])
        major = [run for run in bridge(raw, 5) if run[1] - run[0] + 1 >= 30]
        lengths = [end - begin + 1 for begin, end in major]
        return statistics.median(lengths) if lengths else None

    lines += ["## 3. 现表对照 (结构相位口径)", "",
              "| 现表状态 | 结构对应 | 实测中位 | 现表值 | 差异 |",
              "|---|---|---|---|---|"]
    mapping = [
        ("BEAR+BEAR_DEEP+BEAR_BOTTOM", f"{_PHASE_BEAR} 主段",
         _segment_median(_PHASE_BEAR),
         days_map.get("BEAR", 420) + days_map.get("BEAR_DEEP", 104)
         + days_map.get("BEAR_BOTTOM", 207)),
        ("RECOVERY", f"{_PHASE_RECOVERY} 主段",
         _segment_median(_PHASE_RECOVERY), days_map.get("RECOVERY", 103)),
        ("BULL", f"{_PHASE_BULL} 每周期累计",
         statistics.median(bull_cumulative) if bull_cumulative else None,
         days_map.get("BULL", 420)),
        ("DEEP_BULL+BULL_COOLING", f"{_PHASE_TOP} 每周期累计",
         statistics.median(top_cumulative) if top_cumulative else None,
         days_map.get("DEEP_BULL", 104) + days_map.get("BULL_COOLING", 103)),
    ]
    for label, structure, median, table_value in mapping:
        if median is None:
            lines.append(f"| {label} | {structure} | - | {table_value} | - |")
            continue
        diff = (median / table_value - 1.0) * 100 if table_value else 0.0
        lines.append(f"| {label} | {structure} | {median:.0f} | {table_value} | "
                     f"{diff:+.1f}% |")
    lines += ["", "> 注: 熊侧/复苏取主段中位, 牛侧/顶部取每周期累计中位 (口径与"
              "校准报告一致); 中位数不可加, 仅作方向性对照; 样本量小 (完整周期 n≤3), "
              "结论需结合 2026-2027 后续数据复核。", ""]

    # ---- 4. 敏感性扫描 ----
    lines += ["## 4. 门控参数敏感性扫描", "",
              "回放口径: K 线代理 regime 时间线 + 真实 `apply_rhythm_gates`; "
              "换挡日 progress 归零并定位 (跨零次日 deferred), 中性位冻结; "
              "步长 = clamp(1/预期天数, min, max)。三门控全开, 逐个组合:",
              ""]
    baseline_cfg = {name: {"enabled": False} for name in
                    ("bear_reclaim_gate", "recovery_completion_gate",
                     "bull_top_gate")}
    base_sim = replay(regimes, series, baseline_cfg, days_map, start, alpha_map,
                      min_step, max_step)
    base_bear, base_bull, base_final = _summary(base_sim)
    lines += [f"基线 (三门控全关): 熊段 alpha 均值 {base_bear:+.3f} | "
              f"牛市段 alpha 均值 {base_bull:+.3f} | 末值 {base_final:+.3f}", ""]
    lines += ["| cap | confirm | threshold | 熊段均值 | Δ熊 | 牛市均值 | Δ牛 | "
              "首次分叉 | 末值差 |",
              "|---|---|---|---|---|---|---|---|---|"]
    combo_rows = []
    for cap in (0.3, 0.5, 0.7):
        for confirm in (5, 10, 20):
            for threshold in (0.6, 0.7, 0.8):
                gate_cfg = {
                    "bear_reclaim_gate": {
                        "enabled": True, "progress_cap": cap,
                        "reclaim_confirm_days": confirm},
                    "recovery_completion_gate": {"enabled": True},
                    "bull_top_gate": {"enabled": True,
                                      "progress_threshold": threshold},
                }
                sim = replay(regimes, series, gate_cfg, days_map, start,
                             alpha_map, min_step, max_step)
                bear, bull, final = _summary(sim)
                divergence = _first_divergence(base_sim, sim, dates)
                row = {"cap": cap, "confirm": confirm, "threshold": threshold,
                       "bear": bear, "bull": bull, "final": final,
                       "divergence": divergence}
                combo_rows.append(row)
                lines.append(
                    f"| {cap:g} | {confirm} | {threshold:g} | {bear:+.3f} | "
                    f"{bear - base_bear:+.3f} | {bull:+.3f} | "
                    f"{bull - base_bull:+.3f} | {divergence} | "
                    f"{final - base_final:+.3f} |")
    lines.append("")

    # 参数轴稳健性
    lines += ["### 参数轴稳健性 (组合范围)", "",
              "| 轴 | 取值 | 熊段均值范围 | 牛市均值范围 | 末值差范围 | "
              "分叉点样本 |", "|---|---|---|---|---|---|"]
    for axis, values in (("cap", (0.3, 0.5, 0.7)),
                         ("confirm", (5, 10, 20)),
                         ("threshold", (0.6, 0.7, 0.8))):
        for value in values:
            subset = [row for row in combo_rows if row[axis] == value]
            if not subset:
                continue
            bears = [row["bear"] for row in subset]
            bulls = [row["bull"] for row in subset]
            finals = [row["final"] - base_final for row in subset]
            divergences = sorted({row["divergence"] for row in subset})
            lines.append(
                f"| {axis} | {value:g} | {min(bears):+.3f} ~ {max(bears):+.3f} | "
                f"{min(bulls):+.3f} ~ {max(bulls):+.3f} | "
                f"{min(finals):+.3f} ~ {max(finals):+.3f} | "
                f"{len(divergences)} 个 |")
    lines.append("")
    lines += ["> 判断提示: 同一参数轴内各组合的熊/牛市均值范围越窄, 参数越不敏感"
              " (稳健); 范围宽说明该参数显著改变回放轨迹, 需结合样本量谨慎取值。",
              "> 回放为 K 线代理时间线, AI 实际进出点可能提前/滞后; 本报告仅作"
              "参数方向性校准, 不构成实盘依据。", ""]
    return "\n".join(lines) + "\n"


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="AlphaEngine 节奏校准报告 (只读)")
    parser.add_argument("--csv", default=None, help="K 线 CSV/CSV.GZ 路径")
    parser.add_argument("--archive-dir", default=None,
                        help="归档目录 (默认 ../KlinesData/BTCUSDT_futures_daily_1d)")
    parser.add_argument("--out", default=None, help="输出文件 (缺省 stdout)")
    parser.add_argument("--limit", type=int, default=0,
                        help="只取最后 N 根 (调试用, 0=全部)")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    path, source = resolve_csv(args)
    if args.csv and not os.path.exists(args.csv):
        print(f"[Rhythm] 无可用 K 线数据 (CSV 不存在: {args.csv})")
        return 1
    rows = load_rows(path) if path else []
    if not rows:
        rows = load_from_datafeed()
        source = "datafeed:/klines" if rows else ""
    if not rows:
        print("[Rhythm] 无可用 K 线数据 (--csv / 归档目录 / DataFeed 均不可用)")
        return 1
    if args.limit and args.limit > 0:
        rows = rows[-args.limit:]
    dates = [row[0] for row in rows]
    closes = [row[1] for row in rows]
    if len(closes) < 300:
        print(f"[Rhythm] 数据不足 ({len(closes)} 根 < 300), 报告可能不完整")

    try:
        with open(os.path.join(REPO_ROOT, "config.json"), "r",
                  encoding="utf-8") as handle:
            config_all = json.load(handle)
    except Exception:
        config_all = {}
    alpha_cfg = config_all.get("alpha") or {}
    top_cfg = dict(((config_all.get("cycle_context") or {}).get("top_risk")
                    or {}))
    top_cfg.setdefault("enabled", True)

    series = compute_series(dates, closes, top_cfg)
    phases = classify_phases(closes, series)
    warm = [i for i in range(len(closes)) if phases[i] == _PHASE_WARM]
    start = (max(warm) + 1) if warm else 0
    cycles = find_cycles(closes, series, start, len(closes))
    regimes = build_timeline(cycles, start, len(closes))

    report = build_report(dates, closes, series, phases, cycles, regimes,
                          alpha_cfg, source or path or "?")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"[RhythmReport] 已写入 {args.out} ({len(closes)} 根, "
              f"{len(cycles)} 个主熊周期)")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
