#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WP8 8C 影子账本报告 — 读 data/shadow/ledger.jsonl + equity.jsonl, 输出 markdown (只读).

用法:
    py -3 tools/shadow_report.py
    py -3 tools/shadow_report.py --out data/shadow/report.md
    py -3 tools/shadow_report.py --ledger <path> --equity <path>

内容:
- 四轨迹当前方向/仓位/净值/收益/交易次数/累计成本;
- 观察期净值曲线 (表格采样, 含每轨迹收益%);
- 按结构阶段 (①-④) 的盈亏归因 + 分阶段交易成本;
- 首个分叉点与原因 (哪条轨迹/门控在哪天与 baseline 产生差异);
- 最近事件明细。
"""
import argparse
import json
import os
import sys
from collections import OrderedDict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

TRACKS = ("baseline", "G1", "G1_G2", "G1_G2_G3")
TRACK_LABELS = {
    "baseline": "baseline (无门控)",
    "G1": "+G1 (熊侧收复)",
    "G1_G2": "+G1+G2 (熊侧+复苏)",
    "G1_G2_G3": "+G1+G2+G3 (全开)",
}
PHASE_ORDER = ("4", "3", "2", "1", "")
PHASE_LABELS = {
    "4": "④熊市",
    "3": "③中后期/顶部",
    "2": "②结构确认",
    "1": "①复苏早期",
    "": "未判定",
}
EVENT_LABELS = {"init": "初始化", "enter": "开仓", "adjust": "调仓",
                "exit": "清仓"}
_DEFAULT_NOTIONAL = 10000.0


def load_jsonl(path):
    """读取 JSONL; 文件缺失/坏行跳过 (只读工具, 不抛异常)."""
    records = []
    if not path or not os.path.exists(path):
        return records
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                records.append(item)
    return records


def _fmt_num(value, digits=2):
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "?"


def _fmt_signed(value, digits=2):
    try:
        return f"{float(value):+,.{digits}f}"
    except (TypeError, ValueError):
        return "?"


def _fmt_ts(ts):
    text = str(ts or "")
    return text[:16].replace("T", " ") if text else "?"


def _phase_of(record):
    return str(record.get("phase", "") or "")


def _load_notional(explicit=None):
    if explicit:
        try:
            return float(explicit)
        except (TypeError, ValueError):
            pass
    try:
        config_path = os.path.join(REPO_ROOT, "config.json")
        with open(config_path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        shadow = ((config.get("alpha") or {}).get("shadow") or {})
        return float(shadow.get("notional", _DEFAULT_NOTIONAL))
    except Exception:
        return _DEFAULT_NOTIONAL


def _group_snapshots(equity):
    """ts → {track: snapshot}, 保持时间顺序."""
    groups = OrderedDict()
    for record in equity:
        ts = str(record.get("ts", "") or "")
        track = str(record.get("track", "") or "")
        if not ts or not track:
            continue
        groups.setdefault(ts, {})[track] = record
    return groups


def _sample_indices(count, limit=24):
    if count <= limit:
        return list(range(count))
    step = (count - 1) / (limit - 1)
    return sorted({int(round(i * step)) for i in range(limit)})


def _cost_model(ledger):
    for event in ledger:
        fee = event.get("fee_pct")
        slip = event.get("slip_pct")
        if fee is not None or slip is not None:
            return fee, slip
    return 0.05, 0.02


def build_report(ledger, equity, notional=None):
    """合成 markdown 报告 (纯函数, 便于测试)."""
    notional = _load_notional(notional)
    groups = _group_snapshots(equity)
    fee, slip = _cost_model(ledger)

    last_snapshot, last_event, trades, total_cost = {}, {}, {}, {}
    for track in TRACKS:
        trades[track] = 0
        total_cost[track] = 0.0
    for event in ledger:
        track = str(event.get("track", "") or "")
        if track not in TRACKS:
            continue
        last_event[track] = event
        if event.get("event") != "init":
            trades[track] += 1
            total_cost[track] += float(event.get("cost", 0) or 0)
    for _ts, group in groups.items():
        for track in TRACKS:
            if track in group:
                last_snapshot[track] = group[track]

    lines = ["# AlphaEngine 影子账本报告 (WP8 8C)",
             "",
             f"- 事件数: {len(ledger)} (ledger.jsonl) | 净值快照: {len(equity)} "
             f"(equity.jsonl)",
             f"- 观察轮次: {len(groups)} | 参考本金: {_fmt_num(notional)} U "
             f"(固定, 不复利)",
             f"- 成本模型: taker {_fmt_num(fee, 2)}% + 滑点 {_fmt_num(slip, 2)}%"
             f" (单边, 按成交名义计)",
             "- 口径: 四轨迹 regime 与引擎共享 (换挡同步), 其余按自然日推进 + "
             "各自门控组合; 事件按 TradeSync 去重口径 (同向且 <1.0pp 不记)。",
             ""]
    if not ledger and not equity:
        lines.append("> 暂无数据: 引擎尚未产生影子账 (需 `alpha.shadow.enabled=true` "
                     "且至少运行一轮)。")
        return "\n".join(lines) + "\n"

    # ---- 1. 四轨迹当前状态 ----
    lines += ["## 1. 四轨迹当前状态", "",
              "| 轨迹 | regime | 方向 | 仓位% | 持仓(U) | 净值(U) | 收益% | "
              "交易次数 | 累计成本(U) |",
              "|---|---|---|---|---|---|---|---|---|"]
    for track in TRACKS:
        snap = last_snapshot.get(track) or {}
        event = last_event.get(track) or {}
        equity_value = snap.get("equity")
        try:
            return_pct = (float(equity_value) / notional - 1.0) * 100
            return_text = f"{return_pct:+.2f}%"
        except (TypeError, ValueError):
            return_text = "?"
        direction = event.get("direction") or snap.get("direction") or "?"
        size = event.get("size_pct")
        lines.append(
            f"| {TRACK_LABELS[track]} | {snap.get('regime') or '?'} | "
            f"{direction} | {size if size is not None else '?'} | "
            f"{_fmt_num(snap.get('position'))} | {_fmt_num(equity_value)} | "
            f"{return_text} | {trades[track]} | {_fmt_num(total_cost[track])} |")
    lines.append("")

    # ---- 2. 净值曲线 ----
    lines += ["## 2. 观察期净值曲线 (采样)", ""]
    rows = list(groups.items())
    if not rows:
        lines += ["> 无净值快照 (缺少价格或首轮未写入)。", ""]
    else:
        indices = _sample_indices(len(rows))
        lines.append("| 时间 | " + " | ".join(TRACKS) + " |")
        lines.append("|---|" + "---|" * len(TRACKS))
        for index in indices:
            ts, group = rows[index]
            cells = []
            for track in TRACKS:
                snap = group.get(track)
                if not snap:
                    cells.append("-")
                    continue
                value = snap.get("equity")
                try:
                    pct = (float(value) / notional - 1.0) * 100
                    cells.append(f"{_fmt_num(value)} ({pct:+.2f}%)")
                except (TypeError, ValueError):
                    cells.append(_fmt_num(value))
            lines.append(f"| {_fmt_ts(ts)} | " + " | ".join(cells) + " |")
        lines.append("")
        if len(rows) > len(indices):
            lines += [f"> 共 {len(rows)} 轮, 上表等距采样 {len(indices)} 轮 "
                      f"(首/末轮在内)。", ""]

    # ---- 3. 结构阶段盈亏归因 ----
    lines += ["## 3. 按结构阶段盈亏归因 (净值差分)", "",
              "| 阶段 | " + " | ".join(TRACKS) + " |",
              "|---|" + "---|" * len(TRACKS)]
    phase_pnl = {track: {} for track in TRACKS}
    for (prev_ts, prev_group), (cur_ts, cur_group) in zip(rows, rows[1:]):
        for track in TRACKS:
            prev = prev_group.get(track)
            cur = cur_group.get(track)
            if not prev or not cur:
                continue
            try:
                delta = float(cur.get("equity")) - float(prev.get("equity"))
            except (TypeError, ValueError):
                continue
            phase = _phase_of(cur)
            phase_pnl[track][phase] = phase_pnl[track].get(phase, 0.0) + delta
    for phase in PHASE_ORDER:
        cells = []
        for track in TRACKS:
            value = phase_pnl[track].get(phase)
            cells.append(_fmt_signed(value) if value is not None else "-")
        lines.append(f"| {PHASE_LABELS[phase]} | " + " | ".join(cells) + " |")
    totals = []
    for track in TRACKS:
        value = sum(phase_pnl[track].values())
        totals.append(_fmt_signed(value))
    lines.append("| 合计 | " + " | ".join(totals) + " |")
    lines.append("")
    lines += ["分阶段交易成本 (U):", "",
              "| 阶段 | " + " | ".join(TRACKS) + " |",
              "|---|" + "---|" * len(TRACKS)]
    phase_cost = {track: {} for track in TRACKS}
    for event in ledger:
        if event.get("event") == "init":
            continue
        track = str(event.get("track", "") or "")
        if track not in TRACKS:
            continue
        phase = _phase_of(event)
        phase_cost[track][phase] = phase_cost[track].get(phase, 0.0) \
            + float(event.get("cost", 0) or 0)
    for phase in PHASE_ORDER:
        cells = []
        for track in TRACKS:
            value = phase_cost[track].get(phase)
            cells.append(_fmt_num(value) if value is not None else "-")
        lines.append(f"| {PHASE_LABELS[phase]} | " + " | ".join(cells) + " |")
    lines.append("")

    # ---- 4. 首个分叉点与原因 ----
    lines += ["## 4. 首个分叉点 (相对 baseline)", ""]
    reason_by_key = {}
    for event in ledger:
        key = (str(event.get("ts", "") or ""),
               str(event.get("track", "") or ""))
        reason_by_key.setdefault(key, str(event.get("reason", "") or ""))
    first_div = {}
    for ts, group in groups.items():
        base = group.get("baseline")
        if not base:
            continue
        try:
            base_alpha = float(base.get("alpha", 0.0))
        except (TypeError, ValueError):
            continue
        for track in TRACKS[1:]:
            if track in first_div:
                continue
            record = group.get(track)
            if not record:
                continue
            try:
                alpha = float(record.get("alpha", 0.0))
            except (TypeError, ValueError):
                continue
            if abs(alpha - base_alpha) > 1e-9:
                first_div[track] = (ts, base_alpha, alpha,
                                    reason_by_key.get((ts, track), ""))
    if not first_div:
        lines += ["> 观察期内四轨迹尚未分叉 (门控未触发或进度未推进到差异区)。", ""]
    else:
        lines += ["| 轨迹 | 分叉时间 | baseline alpha | 本轨迹 alpha | 原因 |",
                  "|---|---|---|---|---|"]
        for track in TRACKS[1:]:
            item = first_div.get(track)
            if not item:
                lines.append(f"| {TRACK_LABELS[track]} | 未分叉 | - | - | - |")
                continue
            ts, base_alpha, alpha, reason = item
            lines.append(
                f"| {TRACK_LABELS[track]} | {_fmt_ts(ts)} | "
                f"{base_alpha:+.4f} | {alpha:+.4f} | {reason or '-'} |")
        lines.append("")

    # ---- 5. 最近事件 ----
    lines += ["## 5. 最近事件 (末尾 12 条)", ""]
    if not ledger:
        lines += ["> 无事件。", ""]
    else:
        lines += ["| 时间 | 轨迹 | 事件 | 方向/仓位% | alpha | 价格 | 成本 | "
                  "阶段 | 原因 |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for event in ledger[-12:]:
            lines.append(
                f"| {_fmt_ts(event.get('ts'))} | {event.get('track')} | "
                f"{EVENT_LABELS.get(event.get('event'), event.get('event'))} | "
                f"{event.get('direction')}/{event.get('size_pct')} | "
                f"{_fmt_signed(event.get('alpha'), 4)} | "
                f"{_fmt_num(event.get('price'))} | "
                f"{_fmt_num(event.get('cost'))} | "
                f"{PHASE_LABELS.get(_phase_of(event), _phase_of(event) or '未判定')} | "
                f"{event.get('reason') or '-'} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="AlphaEngine 影子账本报告 (只读)")
    parser.add_argument("--data-dir", default=os.path.join(REPO_ROOT, "data"),
                        help="data 目录 (读取 <dir>/shadow/)")
    parser.add_argument("--ledger", default=None, help="ledger.jsonl 路径 (覆盖)")
    parser.add_argument("--equity", default=None, help="equity.jsonl 路径 (覆盖)")
    parser.add_argument("--notional", type=float, default=None,
                        help="参考本金 (默认读 config.alpha.shadow.notional)")
    parser.add_argument("--out", default=None, help="输出文件 (缺省 stdout)")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    shadow_dir = os.path.join(args.data_dir, "shadow")
    ledger_path = args.ledger or os.path.join(shadow_dir, "ledger.jsonl")
    equity_path = args.equity or os.path.join(shadow_dir, "equity.jsonl")
    ledger = load_jsonl(ledger_path)
    equity = load_jsonl(equity_path)
    report = build_report(ledger, equity, notional=args.notional)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"[ShadowReport] 已写入 {args.out} "
              f"(事件 {len(ledger)} / 快照 {len(equity)})")
    else:
        sys.stdout.write(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
