#!/usr/bin/env python3
"""AlphaEngine 预测校准报告（K-03，只读）。

用法:
    python3 tools/calibration_report.py                 # 默认 data/ 目录
    python3 tools/calibration_report.py --data-dir data --json
    python3 tools/calibration_report.py --prediction-log ... --outcomes ...

输出: point 命中率 / forecast 区间覆盖 / Brier / ECE / 分桶 / 重跑离散。
不写任何文件、不调用 AI、不影响引擎。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.knowledge import build_calibration_summary  # noqa: E402


def _read_jsonl(path: Path) -> list:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _fmt(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def _print_report(report: dict):
    print("AlphaEngine · 预测校准报告 (K-03)")
    print(f"生成: {report.get('generated_at')}")
    print(f"predictions: {report.get('n_predictions')} | outcomes: {report.get('n_outcomes')}")
    print(f"pending: {report.get('pending')} | awaiting_horizon: {report.get('awaiting_horizon')} "
          f"| skipped: {report.get('skipped')}")

    point = report.get("point") or {}
    print("\n[point]")
    print(f"  n={point.get('decided')} correct={point.get('correct')} "
          f"early={point.get('early')} late={point.get('late')} "
          f"flat={point.get('flat')} hit_rate={_fmt(point.get('hit_rate'))}")
    for kind, item in (report.get("point_by_kind") or {}).items():
        print(f"  {kind}: n={item.get('decided')} hit_rate={_fmt(item.get('hit_rate'))}")

    forecast = report.get("forecast") or {}
    print("\n[forecast]")
    print(f"  n={forecast.get('n')} coverage={_fmt(forecast.get('coverage'))} "
          f"direction_hit_rate={_fmt(forecast.get('direction_hit_rate'))}")
    print(f"  mean_width_pct={_fmt(forecast.get('mean_width_pct'))} "
          f"brier={_fmt(forecast.get('mean_brier'))} ece={_fmt(forecast.get('ece'))} "
          f"probability_n={forecast.get('probability_n')}")

    for horizon, item in (report.get("by_horizon") or {}).items():
        label = f"{horizon}d" if str(horizon).isdigit() else str(horizon)
        print(f"  horizon {label}: n={item.get('n')} coverage={_fmt(item.get('coverage'))} "
              f"brier={_fmt(item.get('mean_brier'))} ece={_fmt(item.get('ece'))} "
              f"width={_fmt(item.get('mean_width_pct'))}")

    for section in ("by_confidence", "by_prompt_hash", "by_model"):
        rows = report.get(section) or {}
        if not rows:
            continue
        print(f"\n[{section}]")
        for key, item in rows.items():
            print(f"  {key}: point n={item.get('point_n')} hit={_fmt(item.get('point_hit_rate'))} "
                  f"| forecast n={item.get('forecast_n')} coverage={_fmt(item.get('forecast_coverage'))} "
                  f"| brier={_fmt(item.get('brier'))}")

    cross = report.get("cross_check") or {}
    print("\n[cross_check]")
    print(f"  n={cross.get('n')} dispersed={cross.get('dispersed')} "
          f"rate={_fmt(cross.get('dispersed_rate'))} "
          f"position_mismatch={cross.get('position_mismatch')} "
          f"soft={cross.get('soft_dispersed')}")
    print(f"  position_agreement={_fmt(cross.get('mean_position_agreement'))} "
          f"progress_std={_fmt(cross.get('mean_progress_std'))} "
          f"direction_agreement={_fmt(cross.get('mean_forecast_direction_agreement'))}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="AlphaEngine 预测校准报告（只读）")
    parser.add_argument("--data-dir", default=str(ROOT / "data"),
                        help="数据目录（默认 <repo>/data）")
    parser.add_argument("--prediction-log", default=None)
    parser.add_argument("--outcomes", default=None)
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir).expanduser()
    prediction_log = Path(args.prediction_log).expanduser() if args.prediction_log \
        else data_dir / "prediction_log.jsonl"
    outcomes = Path(args.outcomes).expanduser() if args.outcomes \
        else data_dir / "prediction_outcomes.jsonl"
    report = build_calibration_summary(_read_jsonl(prediction_log), _read_jsonl(outcomes))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
