# -*- coding: utf-8 -*-
"""K-03：预测校准（forecast 区间审计 / Brier / ECE / 分桶 / 重跑离散）。"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from src.knowledge import Knowledge, build_calibration_summary  # noqa: E402


class _Analyzer:
    enabled = True
    prompt_hash = "deadbeefcafe"
    last_call_model = "deepseek-v3"
    last_call_temperature = 0.3


def _read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _write_jsonl(path, rows):
    Path(path).write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _prediction(ts, cycle_position="RECOVERY", confidence="high", forecast=None,
                prompt_hash="abc123", model="deepseek-v3"):
    row = {
        "ts": ts,
        "prediction_id": f"{ts}|{cycle_position}",
        "cycle_position": cycle_position,
        "confidence": confidence,
        "btc_price": 100000.0,
        "verified": False,
        "prompt_hash": prompt_hash,
        "model": model,
    }
    if forecast is not None:
        row["forecast"] = forecast
    return row


def test_calibration_summary_empty():
    report = build_calibration_summary([], [])
    assert report["n_predictions"] == 0
    assert report["n_outcomes"] == 0
    assert report["pending"] == 0
    assert report["awaiting_horizon"] == 0
    assert report["skipped"] == 0
    assert report["point"]["hit_rate"] is None
    assert report["forecast"]["n"] == 0
    assert report["forecast"]["coverage"] is None
    assert report["forecast"]["mean_brier"] is None
    assert report["cross_check"]["n"] == 0
    assert report["cross_check"]["dispersed_rate"] is None


def test_calibration_summary_point_forecast_buckets():
    forecast_hit = {
        "horizon_days": 30, "direction": "up", "price_low": 102000,
        "price_high": 108000, "confidence": 0.7, "hit_probability": 0.7,
    }
    forecast_miss = {
        "horizon_days": 30, "direction": "up", "price_low": 102000,
        "price_high": 108000, "confidence": 0.8, "hit_probability": 0.8,
    }
    predictions = [
        _prediction("2026-01-01T00:00:00Z", "RECOVERY", "high", forecast_hit),
        _prediction("2026-02-01T00:00:00Z", "BULL", "medium", forecast_miss),
        _prediction("2026-03-01T00:00:00Z", "BEAR", "low"),
    ]
    predictions[0]["cross_check"] = {"samples": 2, "position_agreement": 1.0,
                                     "progress_std": 0.0, "dispersed": False}
    outcomes = [
        {"prediction_id": predictions[0]["prediction_id"], "confidence": "high",
         "verdict": "correct", "forecast": forecast_hit, "interval_hit": True,
         "direction_hit": True, "width_pct": 10.0, "brier": 0.09},
        {"prediction_id": predictions[1]["prediction_id"], "confidence": "medium",
         "verdict": "early", "forecast": forecast_miss, "interval_hit": False,
         "direction_hit": False, "width_pct": 20.0, "brier": 0.64},
        {"prediction_id": predictions[2]["prediction_id"], "confidence": "low",
         "verdict": "flat"},
    ]

    report = build_calibration_summary(predictions, outcomes)

    assert report["point"]["correct"] == 1
    assert report["point"]["early"] == 1
    assert report["point"]["decided"] == 2
    assert report["point"]["hit_rate"] == 0.5

    assert report["forecast"]["n"] == 2
    assert report["forecast"]["hits"] == 1
    assert report["forecast"]["coverage"] == 0.5
    assert report["forecast"]["direction_hit_rate"] == 0.5
    assert report["forecast"]["mean_width_pct"] == 15.0
    assert report["forecast"]["mean_brier"] == 0.365
    assert round(report["forecast"]["ece"], 3) == 0.55

    assert report["by_confidence"]["high"]["point_hit_rate"] == 1.0
    assert report["by_confidence"]["medium"]["point_hit_rate"] == 0.0
    assert report["by_confidence"]["high"]["forecast_coverage"] == 1.0
    assert report["by_confidence"]["medium"]["forecast_coverage"] == 0.0
    assert report["cross_check"]["n"] == 1
    assert report["cross_check"]["dispersed_rate"] == 0.0
    assert report["point_by_kind"]["legacy"]["decided"] == 0
    assert report["point_by_kind"]["legacy"]["hit_rate"] is None
    assert report["point_by_kind"]["forecast"]["decided"] == 2
    assert report["point_by_kind"]["forecast"]["hit_rate"] == 0.5
    assert report["by_horizon"]["30"]["n"] == 2
    assert report["by_horizon"]["30"]["coverage"] == 0.5
    assert report["by_horizon"]["30"]["mean_brier"] == 0.365
    assert round(report["by_horizon"]["30"]["ece"], 3) == 0.55
    assert report["by_prompt_hash"]["abc123"]["brier"] is not None
    assert report["by_model"]["deepseek-v3"]["brier"] is not None


def test_audit_forecast_interval_hit_and_brier(tmp_path):
    knowledge = Knowledge({}, str(tmp_path), _Analyzer())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pred_time = now - timedelta(days=40)
    target = pred_time + timedelta(days=30)
    ts = pred_time.isoformat() + "Z"
    forecast = {
        "horizon_days": 30, "direction": "up", "price_low": 102000,
        "price_high": 108000, "confidence": 0.7, "hit_probability": 0.7,
    }
    _write_jsonl(tmp_path / "price_history.jsonl", [
        {"ts": ts, "btc_price": 100000.0},
        {"ts": (target + timedelta(hours=1)).isoformat() + "Z", "btc_price": 105000.0},
        {"ts": now.isoformat() + "Z", "btc_price": 110000.0},
    ])
    _write_jsonl(tmp_path / "prediction_log.jsonl", [
        _prediction(ts, "RECOVERY", "high", forecast),
    ])

    result = knowledge.audit_predictions(current_price=110000.0)

    assert result["new_audited"] == 1
    assert result["forecast_total"] == 1
    assert result["interval_coverage"] == 1.0
    assert result["forecast_direction_hit_rate"] == 1.0
    assert result["forecast_brier"] == 0.09
    outcome = _read_jsonl(tmp_path / "prediction_outcomes.jsonl")[0]
    assert outcome["interval_hit"] is True
    assert outcome["direction_hit"] is True
    assert outcome["forecast_verdict"] == "hit"
    assert outcome["horizon_price"] == 105000.0
    assert outcome["brier"] == 0.09
    assert outcome["verdict"] == "correct"


def test_audit_forecast_waits_for_horizon(tmp_path):
    knowledge = Knowledge({}, str(tmp_path), _Analyzer())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pred_time = now - timedelta(days=10)
    forecast = {
        "horizon_days": 30, "direction": "up", "price_low": 102000,
        "price_high": 108000, "hit_probability": 0.7,
    }
    _write_jsonl(tmp_path / "price_history.jsonl", [
        {"ts": pred_time.isoformat() + "Z", "btc_price": 100000.0},
        {"ts": now.isoformat() + "Z", "btc_price": 110000.0},
    ])
    _write_jsonl(tmp_path / "prediction_log.jsonl", [
        _prediction(pred_time.isoformat() + "Z", "RECOVERY", "high", forecast),
    ])

    result = knowledge.audit_predictions(current_price=110000.0)

    assert result["new_audited"] == 0
    assert not (tmp_path / "prediction_outcomes.jsonl").exists()


def test_audit_forecast_missing_horizon_price_waits(tmp_path):
    knowledge = Knowledge({}, str(tmp_path), _Analyzer())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pred_time = now - timedelta(days=40)
    forecast = {
        "horizon_days": 30, "direction": "up", "price_low": 102000,
        "price_high": 108000, "hit_probability": 0.7,
    }
    _write_jsonl(tmp_path / "price_history.jsonl", [
        {"ts": pred_time.isoformat() + "Z", "btc_price": 100000.0},
        {"ts": now.isoformat() + "Z", "btc_price": 110000.0},
    ])
    _write_jsonl(tmp_path / "prediction_log.jsonl", [
        _prediction(pred_time.isoformat() + "Z", "RECOVERY", "high", forecast),
    ])

    result = knowledge.audit_predictions(current_price=110000.0)

    # 目标日 now-10；唯一价格 now（偏离 10 天 > 7 天容差）→ 等待补价，不永久 skipped
    assert result["new_audited"] == 0
    assert result["skipped"] == 0
    assert not (tmp_path / "prediction_outcomes.jsonl").exists()


def test_audit_forecast_missing_horizon_price_skips_late(tmp_path):
    knowledge = Knowledge({}, str(tmp_path), _Analyzer())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pred_time = now - timedelta(days=70)
    forecast = {
        "horizon_days": 30, "direction": "up", "price_low": 102000,
        "price_high": 108000, "hit_probability": 0.7,
    }
    _write_jsonl(tmp_path / "price_history.jsonl", [
        {"ts": pred_time.isoformat() + "Z", "btc_price": 100000.0},
        {"ts": now.isoformat() + "Z", "btc_price": 110000.0},
    ])
    _write_jsonl(tmp_path / "prediction_log.jsonl", [
        _prediction(pred_time.isoformat() + "Z", "RECOVERY", "high", forecast),
    ])

    result = knowledge.audit_predictions(current_price=110000.0)

    assert result["skipped"] == 1
    outcome = _read_jsonl(tmp_path / "prediction_outcomes.jsonl")[0]
    assert outcome["verdict"] == "skipped"
    assert outcome["reason"] == "missing horizon price"


def test_audit_forecast_price_three_days_after_target(tmp_path):
    knowledge = Knowledge({}, str(tmp_path), _Analyzer())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pred_time = now - timedelta(days=40)
    target = pred_time + timedelta(days=30)
    forecast = {
        "horizon_days": 30, "direction": "up", "price_low": 102000,
        "price_high": 108000, "hit_probability": 0.7,
    }
    _write_jsonl(tmp_path / "price_history.jsonl", [
        {"ts": pred_time.isoformat() + "Z", "btc_price": 100000.0},
        {"ts": (target + timedelta(days=3)).isoformat() + "Z", "btc_price": 105000.0},
    ])
    _write_jsonl(tmp_path / "prediction_log.jsonl", [
        _prediction(pred_time.isoformat() + "Z", "RECOVERY", "high", forecast),
    ])

    result = knowledge.audit_predictions(current_price=110000.0)

    assert result["new_audited"] == 1
    outcome = _read_jsonl(tmp_path / "prediction_outcomes.jsonl")[0]
    assert outcome["horizon_price"] == 105000.0
    assert outcome["horizon_price_offset_days"] == 3.0
    assert outcome["horizon_price_source"] == "after"
    assert outcome["interval_hit"] is True


def test_log_prediction_persists_audit_fields(tmp_path):
    knowledge = Knowledge({}, str(tmp_path), _Analyzer())
    forecast = {"horizon_days": 30, "direction": "up", "price_low": 1,
                "price_high": 2, "hit_probability": 0.6}
    knowledge.log_prediction(
        "RECOVERY", "high", 100000.0,
        forecast=forecast,
        source_type_counts={"retrieved": 2, "remembered": 1, "inferred": 1, "unknown": 0},
        cross_check={"samples": 2, "dispersed": False, "position_agreement": 1.0},
    )
    entry = _read_jsonl(tmp_path / "prediction_log.jsonl")[0]
    assert entry["forecast"] == forecast
    assert entry["source_type_counts"]["retrieved"] == 2
    assert entry["cross_check"]["samples"] == 2


def test_calibration_summary_pending_awaiting_and_skipped():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    recent = (now - timedelta(days=10)).isoformat() + "Z"
    old = (now - timedelta(days=70)).isoformat() + "Z"
    predictions = [
        _prediction(recent, "RECOVERY", "high", {
            "horizon_days": 30, "direction": "up", "price_low": 1,
            "price_high": 2, "hit_probability": 0.6}),
        _prediction(old, "BEAR", "low"),
        _prediction(old, "BULL", "medium"),
    ]
    outcomes = [
        {"prediction_id": predictions[2]["prediction_id"], "verdict": "skipped",
         "confidence": "medium"},
    ]

    report = build_calibration_summary(predictions, outcomes)

    assert report["awaiting_horizon"] == 1
    assert report["pending"] == 1
    assert report["skipped"] == 1


def test_calibration_summary_dedupes_outcomes():
    predictions = [_prediction("2026-01-01T00:00:00Z", "RECOVERY", "high")]
    pid = predictions[0]["prediction_id"]
    outcomes = [
        {"prediction_id": pid, "verdict": "early", "confidence": "high"},
        {"prediction_id": pid, "verdict": "correct", "confidence": "high"},
    ]
    report = build_calibration_summary(predictions, outcomes)
    assert report["n_outcomes"] == 1
    assert report["point"]["correct"] == 1
    assert report["point"]["decided"] == 1


def test_audit_forecast_90d_waits_at_40d(tmp_path):
    knowledge = Knowledge({}, str(tmp_path), _Analyzer())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pred_time = now - timedelta(days=40)
    forecast = {
        "horizon_days": 90, "direction": "up", "price_low": 102000,
        "price_high": 108000, "hit_probability": 0.7,
    }
    _write_jsonl(tmp_path / "price_history.jsonl", [
        {"ts": pred_time.isoformat() + "Z", "btc_price": 100000.0},
        {"ts": now.isoformat() + "Z", "btc_price": 110000.0},
    ])
    _write_jsonl(tmp_path / "prediction_log.jsonl", [
        _prediction(pred_time.isoformat() + "Z", "RECOVERY", "high", forecast),
    ])

    result = knowledge.audit_predictions(current_price=110000.0)

    assert result["new_audited"] == 0
    assert result["skipped"] == 0
    assert not (tmp_path / "prediction_outcomes.jsonl").exists()


def test_calibration_summary_tolerates_overflow_values():
    huge = 10 ** 1000
    pred = _prediction("2026-01-01T00:00:00Z", "RECOVERY", "high",
                       {"horizon_days": float("inf"), "direction": "up",
                        "price_low": 1, "price_high": 2})
    outcome = {
        "prediction_id": pred["prediction_id"],
        "confidence": "high",
        "verdict": "correct",
        "forecast": {"horizon_days": 90, "direction": "up",
                     "price_low": 1, "price_high": 2,
                     "hit_probability": huge},
        "interval_hit": True,
        "direction_hit": True,
        "width_pct": 1.0,
    }
    report = build_calibration_summary([pred], [outcome])
    assert report["n_outcomes"] == 1
    assert report["forecast"]["n"] == 1
    assert report["by_horizon"]["90"]["n"] == 1


def test_calibration_summary_cross_check_split():
    predictions = [
        _prediction("2026-01-01T00:00:00Z", "RECOVERY", "high"),
        _prediction("2026-01-02T00:00:00Z", "RECOVERY", "high"),
    ]
    predictions[0]["cross_check"] = {
        "positions": ["RECOVERY", "BULL"], "position_agreement": 0.5,
        "progress_std": 0.0, "forecast_direction_agreement": 1.0,
        "dispersed": True,
    }
    predictions[1]["cross_check"] = {
        "positions": ["RECOVERY", "RECOVERY"], "position_agreement": 1.0,
        "progress_std": 0.3, "forecast_direction_agreement": 0.5,
        "dispersed": True,
    }
    report = build_calibration_summary(predictions, [])
    assert report["cross_check"]["position_mismatch"] == 1
    assert report["cross_check"]["soft_dispersed"] == 1
    assert report["cross_check"]["dispersed"] == 2


def test_calibration_summary_price_source():
    pred = _prediction("2026-01-01T00:00:00Z", "RECOVERY", "high")
    outcome = {
        "prediction_id": pred["prediction_id"], "confidence": "high",
        "verdict": "correct", "forecast": {"horizon_days": 30,
                                           "hit_probability": 0.7},
        "horizon_days": 30, "interval_hit": True, "direction_hit": True,
        "width_pct": 6.0, "brier": 0.09,
        "horizon_price_offset_days": 3.0, "horizon_price_source": "after",
    }
    report = build_calibration_summary([pred], [outcome])
    assert report["by_price_source"]["after"]["n"] == 1
    assert report["by_price_source"]["after"]["coverage"] == 1.0
    assert report["by_price_source"]["after"]["mean_abs_offset_days"] == 3.0


def test_calibration_summary_pending_excludes_verified():
    pred = _prediction("2026-01-01T00:00:00Z", "RECOVERY", "high")
    pred["verified"] = True
    report = build_calibration_summary([pred], [])
    assert report["pending"] == 0


def test_audit_forecast_uses_before_price_when_no_after_within_tolerance(tmp_path):
    knowledge = Knowledge({}, str(tmp_path), _Analyzer())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    pred_time = now - timedelta(days=40)
    target = pred_time + timedelta(days=30)
    forecast = {
        "horizon_days": 30, "direction": "up", "price_low": 102000,
        "price_high": 108000, "hit_probability": 0.7,
    }
    _write_jsonl(tmp_path / "price_history.jsonl", [
        {"ts": pred_time.isoformat() + "Z", "btc_price": 100000.0},
        {"ts": (target - timedelta(days=2)).isoformat() + "Z", "btc_price": 104000.0},
        {"ts": (target + timedelta(days=20)).isoformat() + "Z", "btc_price": 110000.0},
    ])
    _write_jsonl(tmp_path / "prediction_log.jsonl", [
        _prediction(pred_time.isoformat() + "Z", "RECOVERY", "high", forecast),
    ])

    result = knowledge.audit_predictions(current_price=110000.0)

    assert result["new_audited"] == 1
    outcome = _read_jsonl(tmp_path / "prediction_outcomes.jsonl")[0]
    assert outcome["horizon_price"] == 104000.0
    assert outcome["horizon_price_offset_days"] == -2.0
    assert outcome["horizon_price_source"] == "before"
