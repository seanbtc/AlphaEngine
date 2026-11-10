"""Phase 1 反馈闭环单测: thesis / scorecard / manifest / analyzer 注入.

原则: Phase 1 只记录与评估, 不改变 alpha; 所有异常不得阻断主分析。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analyzer import Analyzer
from src.knowledge import Knowledge
from src.manifest import append_manifest, build_manifest
from src.memory import Memory
from src.scorecard import build_scorecard
from src.thesis import (adopt_thesis, evaluate_thesis, format_thesis_for_prompt,
                        normalize_thesis)


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _raw(close=78000, slope="down", claim="复苏延续"):
    deadline = (_now() + timedelta(days=30)).date().isoformat()
    return {
        "label": "RECOVERY",
        "claim": claim,
        "conditions": [
            {"id": "c1", "metric": "close", "op": ">=", "value": close,
             "deadline": deadline},
            {"id": "c2", "metric": "sma200_slope", "op": "!=", "value": slope,
             "deadline": deadline},
        ],
        "confidence": 0.6,
    }


def test_normalize_thesis_accepts_valid_conditions():
    thesis = normalize_thesis(_raw())

    assert thesis["status"] == "active"
    assert len(thesis["conditions"]) == 2
    assert thesis["thesis_id"].startswith("T-")
    assert thesis["conditions"][0]["deadline"]


def test_normalize_thesis_rejects_single_or_non_price_condition():
    raw = _raw()
    raw["conditions"] = raw["conditions"][:1]
    assert normalize_thesis(raw) is None

    raw = _raw()
    raw["conditions"] = [
        {"metric": "price_change_7d", "op": ">", "value": 0},
        {"metric": "price_change_30d", "op": ">", "value": 0},
    ]
    assert normalize_thesis(raw) is None


def test_normalize_thesis_clamps_deadline_to_90_days():
    raw = _raw()
    far = (_now() + timedelta(days=365)).date().isoformat()
    for cond in raw["conditions"]:
        cond["deadline"] = far
    thesis = normalize_thesis(raw)

    max_deadline = (_now() + timedelta(days=90)).date()
    for cond in thesis["conditions"]:
        assert datetime.fromisoformat(cond["deadline"]).date() <= max_deadline


def test_adopt_thesis_keeps_same_id_for_same_conditions():
    first = normalize_thesis(_raw())
    raw = _raw(claim="同样的条件, 换个说法")
    merged, event, previous = adopt_thesis(raw, first)

    assert event == "updated"
    assert previous is None
    assert merged["thesis_id"] == first["thesis_id"]
    assert merged["claim"] == "同样的条件, 换个说法"


def test_adopt_thesis_supersedes_changed_conditions():
    first = normalize_thesis(_raw(close=78000))
    second, event, previous = adopt_thesis(_raw(close=80000), first)

    assert event == "created"
    assert previous["status"] == "superseded"
    assert second["thesis_id"] != first["thesis_id"]
    assert second["supersedes"] == first["thesis_id"]


def _market(price=74000, slope="down", sma200=75000):
    return {
        "price": price,
        "ma_context": {"snapshot": {
            "price": price,
            "mas": {"sma200": {"value": sma200, "slope": slope,
                               "dist_pct": -1.33}},
        }},
        "cycle_context": {"top_risk": {"drawdown_from_90d_high_pct": -12.7}},
    }


def test_evaluate_thesis_invalidates_when_half_conditions_unmet():
    thesis = normalize_thesis(_raw(close=78000, slope="down"))
    evaluated = evaluate_thesis(thesis, _market(price=74000, slope="down"))

    assert evaluated["status"] == "invalidated"
    assert evaluated["unmet_count"] == 2
    assert evaluated["invalidated_reason"]


def test_evaluate_thesis_weakened_below_threshold():
    raw = _raw()
    raw["conditions"].append({"metric": "drawdown_90d_pct", "op": ">=", "value": -10})
    thesis = normalize_thesis(raw)
    evaluated = evaluate_thesis(thesis, _market(price=80000, slope="up"))

    assert evaluated["status"] == "weakened"
    assert evaluated["conditions_met"] == 2
    assert evaluated["unmet_count"] == 1


def test_evaluate_thesis_unknown_does_not_invalidate():
    thesis = normalize_thesis(_raw())
    evaluated = evaluate_thesis(thesis, {})

    assert evaluated["unknown_count"] == 2
    assert evaluated["unmet_count"] == 0
    assert evaluated["status"] == "active"


def test_evaluate_thesis_unknown_does_not_upgrade_weakened():
    """数据缺失不能把已转弱的命题洗白成 active (Phase 1 观测口径)。"""
    thesis = normalize_thesis(_raw())
    thesis["status"] = "weakened"
    evaluated = evaluate_thesis(thesis, {})

    assert evaluated["unknown_count"] == 2
    assert evaluated["unmet_count"] == 0
    assert evaluated["status"] == "weakened"


def test_format_thesis_tolerates_malformed_fields():
    # 旧/损坏 state 中 evaluations/conditions 非 list 时不得抛异常
    text = format_thesis_for_prompt({
        "thesis_id": "T-bad", "status": "active", "claim": "x",
        "evaluations": "oops", "conditions": "oops",
    })
    assert "T-bad" in text

    # 非数值 actual (斜率) 应原样渲染, 不显示为"缺失"
    text = format_thesis_for_prompt({
        "thesis_id": "T-slope", "status": "active", "claim": "x",
        "evaluations": [{"id": "c1", "metric": "sma200_slope", "op": "==",
                         "value": "up", "actual": "up", "status": "met"}],
    })
    assert "(实际 up)" in text


def test_evaluate_thesis_expires_after_max_deadline():
    raw = _raw()
    past = (_now() - timedelta(days=1)).date().isoformat()
    for cond in raw["conditions"]:
        cond["deadline"] = past
    thesis = normalize_thesis(raw)
    evaluated = evaluate_thesis(thesis, _market(price=90000, slope="up"))

    assert evaluated["status"] == "expired"


def test_format_thesis_for_prompt_mentions_status_and_reason():
    thesis = normalize_thesis(_raw(close=78000))
    thesis = evaluate_thesis(thesis, _market(price=74000))
    text = format_thesis_for_prompt(thesis)

    assert "当前命题" in text
    assert "invalidated" in text
    assert "close" in text


class _FakeEngine:
    def __init__(self, alpha):
        self.alpha = alpha

    def get_alpha(self):
        return self.alpha


def test_build_scorecard_includes_result_streak_and_thesis(tmp_path):
    knowledge = Knowledge({}, str(tmp_path), analyzer=None)
    now = _now()
    predictions = []
    outcomes = []
    for index, (verdict, move) in enumerate([
            ("wrong_direction", -5.6), ("wrong_direction", -2.1), ("correct", 3.2)]):
        ts = (now - timedelta(days=index * 5)).isoformat() + "Z"
        pid = f"p{index}"
        predictions.append({"prediction_id": pid, "ts": ts,
                            "cycle_position": "RECOVERY", "confidence": "high"})
        outcomes.append({"prediction_id": pid, "verdict": verdict,
                         "expected": "up", "move_pct": move})
    (tmp_path / "prediction_log.jsonl").write_text(
        "\n".join(json.dumps(row) for row in predictions) + "\n", encoding="utf-8")
    (tmp_path / "prediction_outcomes.jsonl").write_text(
        "\n".join(json.dumps(row) for row in outcomes) + "\n", encoding="utf-8")
    (tmp_path / "price_history.jsonl").write_text(
        json.dumps({"ts": (now - timedelta(days=60)).isoformat() + "Z",
                    "btc_price": 100000}) + "\n"
        + json.dumps({"ts": now.isoformat() + "Z", "btc_price": 80000}) + "\n",
        encoding="utf-8")
    memory = Memory(str(tmp_path))
    memory.save_alpha_history([{
        "date": (now - timedelta(days=30)).isoformat() + "Z",
        "alpha": 0.9, "btc_price": 90000, "regime": "RECOVERY"}])
    thesis = evaluate_thesis(normalize_thesis(_raw()), _market(price=74000))

    text = build_scorecard(knowledge, memory, _FakeEngine(0.7), 80000)

    assert "你的近期成绩" in text
    assert "命中率 33.3%" in text
    assert "连续方向错 2 条" in text
    assert "当前回撤 -20.0%" in text
    assert "α=+0.900" in text and "α=+0.700" in text
    # 命题由 format_thesis_for_prompt 单独注入, 成绩单不重复渲染
    assert "当前命题" not in text
    assert len(text) <= 900  # max_chars + 截断标记余量


def test_manifest_append_is_jsonl(tmp_path):
    entry = build_manifest(mode="analysis", cycle_id="c1", alpha_before=0.5,
                           thesis={"thesis_id": "T-1", "status": "active"})
    append_manifest(str(tmp_path), entry)

    rows = [json.loads(line) for line in
            (tmp_path / "run_manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["cycle_id"] == "c1"
    assert rows[0]["mode"] == "analysis"
    assert rows[0]["thesis"]["thesis_id"] == "T-1"


def test_analyzer_market_state_injects_scorecard_and_thesis():
    analyzer = object.__new__(Analyzer)
    thesis = evaluate_thesis(normalize_thesis(_raw()), _market(price=74000))
    text = analyzer._format_market_state({
        "regime": "RECOVERY",
        "alpha": 0.7,
        "price": 74000,
        "scorecard": "## 你的近期成绩\n- 10/17 预期up 实际-5.6% → 方向错\n",
        "thesis": thesis,
    })

    assert "你的近期成绩" in text
    assert "当前命题" in text
    assert "方向错" in text


def test_analyzer_normalizes_thesis_field():
    result = Analyzer._normalize_audit_fields({"thesis": _raw()})

    assert result["thesis"]["thesis_id"].startswith("T-")
    assert Analyzer._normalize_audit_fields({"thesis": {"claim": "x"}})["thesis"] is None


def test_analyzer_thesis_disabled_reverts_prompt_and_field():
    from src.analyzer import PROMPT_HASH

    analyzer = Analyzer({"thesis_enabled": False})
    assert "可证伪命题" not in analyzer.system_prompt
    assert "## 输出格式 (严格 JSON)" in analyzer.system_prompt
    assert analyzer.prompt_hash != PROMPT_HASH

    result = Analyzer._normalize_audit_fields({"thesis": _raw()}, thesis_enabled=False)
    assert "thesis" not in result

    enabled = Analyzer({})
    assert "可证伪命题" in enabled.system_prompt
    assert enabled.prompt_hash == PROMPT_HASH
