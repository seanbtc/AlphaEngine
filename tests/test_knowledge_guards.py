"""知识库守卫 / 预测 verdict 语义回归 (Phase 0).

背景 (2026-10):
- DeepSeek reasoning 吃满 max_tokens → AIService 回退 reasoning_content →
  12/12 次蒸馏产物为无 Markdown 结构的推理文本, 被注入每轮 prompt。
- point verdict 的 early/late 与 wrong_direction 同义, 分拆稀释样本。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.knowledge import (Knowledge, build_calibration_summary,
                           normalize_point_verdict, validate_knowledge_text)


VALID_KB = """# 知识库

## 可靠的 Bull 信号
- ETF 连续净流入 + 价格站上 200SMA: 历史 3/4 命中
- 稳定币供应 30 日增长转正 + 交易所余额下降: 历史 2/3 命中
- Market Compass 从 Defensive 切到 Risk-On 且维持 5 日以上: 历史 3/5 命中

## 可靠的 Bear/Bottom 信号
- 长期持有者净头寸转负 + 价格跌破 200SMA: 历史 4/5 命中
- 稳定币供应萎缩 + 实际利率走高: 历史 2/2 命中

## 常见误判
- 在 RECOVERY 阶段把反弹当 BULL 确认
- 把 ETF 单周流入当成趋势确认, 忽略价格结构
"""

POLLUTED = ("我们需要分析这些数据。首先看预测审计结果, 用户的命中率偏低, "
            "这说明模型过度自信。接下来要考虑 regime 变更阈值是否应该调整……" * 5)


def test_validate_knowledge_text_accepts_markdown():
    ok, reason = validate_knowledge_text(VALID_KB)
    assert ok is True
    assert reason == ""


def test_validate_knowledge_text_rejects_reasoning_fallback():
    ok, reason = validate_knowledge_text(POLLUTED)
    assert ok is False
    assert "markdown" in reason


def test_validate_knowledge_text_rejects_empty_and_short():
    assert validate_knowledge_text("")[0] is False
    assert validate_knowledge_text("# 标题\n太短")[0] is False


def test_load_knowledge_base_blocks_polluted_file(tmp_path):
    kb_file = tmp_path / "knowledge_base.md"
    kb_file.write_text(POLLUTED, encoding="utf-8")
    knowledge = Knowledge({}, str(tmp_path), analyzer=None)

    assert knowledge.load_knowledge_base() == ""
    # 只拦截注入, 不删除/改写文件
    assert kb_file.read_text(encoding="utf-8") == POLLUTED


def test_load_knowledge_base_returns_valid_markdown(tmp_path):
    kb_file = tmp_path / "knowledge_base.md"
    kb_file.write_text(VALID_KB, encoding="utf-8")
    knowledge = Knowledge({}, str(tmp_path), analyzer=None)

    assert knowledge.load_knowledge_base().startswith("# 知识库")


class _FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _FakeAnalyzer:
    enabled = True
    endpoint = "http://fake"
    timeout = 10

    def __init__(self, response):
        self.client = _FakeClient(response)


class _FakeMemory:
    def load_memory_md(self):
        return "# memory\n- RECOVERY"

    def load_valuation_history(self):
        return []


def _distill(tmp_path, response):
    knowledge = Knowledge({"distill": {"max_tokens": 4096}}, str(tmp_path),
                          _FakeAnalyzer(response))
    return knowledge, knowledge.distill(_FakeMemory(), state_manager=None)


def test_distill_rejects_truncated_output_and_keeps_old_kb(tmp_path):
    old = tmp_path / "knowledge_base.md"
    old.write_text(VALID_KB, encoding="utf-8")
    response = {"ok": True, "content": POLLUTED,
                "usage": {"completion_tokens": 4096}}
    knowledge, content = _distill(tmp_path, response)

    assert content is None
    assert old.read_text(encoding="utf-8") == VALID_KB
    assert knowledge.analyzer.client.calls[0]["max_tokens"] == 4096


def test_distill_rejects_invalid_structure(tmp_path):
    response = {"ok": True, "content": POLLUTED,
                "usage": {"completion_tokens": 800}}
    _, content = _distill(tmp_path, response)

    assert content is None
    assert not (tmp_path / "knowledge_base.md").exists()


def test_distill_saves_valid_markdown(tmp_path):
    response = {"ok": True, "content": VALID_KB,
                "usage": {"completion_tokens": 800}}
    _, content = _distill(tmp_path, response)

    assert content == VALID_KB
    assert (tmp_path / "knowledge_base.md").read_text(encoding="utf-8") == VALID_KB


def test_prediction_verdict_collapses_early_late_to_wrong_direction():
    assert Knowledge._prediction_verdict("up", -5.0) == "wrong_direction"
    assert Knowledge._prediction_verdict("down", 5.0) == "wrong_direction"
    assert Knowledge._prediction_verdict("up", 3.0) == "correct"
    assert Knowledge._prediction_verdict("up", 0.5) == "flat"


def test_normalize_point_verdict_legacy_mapping():
    assert normalize_point_verdict("early") == "wrong_direction"
    assert normalize_point_verdict("late") == "wrong_direction"
    assert normalize_point_verdict("correct") == "correct"
    assert normalize_point_verdict(None) is None


def test_calibration_summary_merges_legacy_early_late():
    predictions = [{"prediction_id": "p1", "ts": "2026-01-01T00:00:00Z"},
                   {"prediction_id": "p2", "ts": "2026-01-02T00:00:00Z"}]
    outcomes = [
        {"prediction_id": "p1", "verdict": "early"},
        {"prediction_id": "p2", "verdict": "late"},
    ]
    report = build_calibration_summary(predictions, outcomes)

    assert report["point"]["wrong_direction"] == 2
    assert report["point"]["decided"] == 2
    assert report["point"]["hit_rate"] == 0.0
