"""10 档细分状态映射测试（只做展示，不改 4 档估值口径）。"""
import json

import pytest

from src.state_manager import StateManager
from src.valuation import UNASSESSED_LABEL, describe_state, state_of


@pytest.mark.parametrize("value,expected", [
    (0.0, "极端投降"),
    (0.0999, "极端投降"),
    (0.1, "深度低估"),
    (0.1999, "深度低估"),
    (0.2, "低估"),
    (0.3, "偏低估"),
    (0.4, "中性偏下"),
    (0.5, "中性偏上"),
    (0.6, "偏贵"),
    (0.7, "高估"),
    (0.8, "极度高估"),
    (0.9, "泡沫/狂热"),
    (1.0, "泡沫/狂热"),
])
def test_state_of_boundaries(value, expected):
    assert state_of(value) == expected


@pytest.mark.parametrize("value", [None, -0.1, 1.1, "0.5", True])
def test_state_of_invalid(value):
    assert state_of(value) == UNASSESSED_LABEL


def test_describe_state():
    assert describe_state(0.35) == "0.35 · 偏低估"
    assert describe_state(0.62) == "0.62 · 偏贵"
    assert describe_state(None) == UNASSESSED_LABEL


def test_set_valuation_persists_state(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")
    sm.load()
    block = sm.set_valuation(0.35, confidence="medium", evidence="test")
    sm.save()
    assert block["label"] == "偏低估"
    assert block["state"] == "偏低估"
    persisted = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert persisted["valuation"]["state"] == "偏低估"


def test_default_state_has_state_field(tmp_path):
    sm = StateManager(str(tmp_path), "state.json")
    state = sm.load()
    assert state["valuation"]["state"] == UNASSESSED_LABEL


def test_legacy_label_normalized_on_load(tmp_path):
    import json
    from src.state_manager import STATE_VERSION
    state = {
        "version": STATE_VERSION,
        "valuation": {"current": 0.28, "label": "轻度低估",
                      "state": "低估", "confidence": "medium"},
        "runtime": {"analysis_count": 1, "post_count": 0},
    }
    (tmp_path / "state.json").write_text(json.dumps(state), encoding="utf-8")
    sm = StateManager(str(tmp_path), "state.json")
    loaded = sm.load()
    assert loaded["valuation"]["label"] == "低估"
    assert loaded["valuation"]["state"] == "低估"
