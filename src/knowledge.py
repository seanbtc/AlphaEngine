"""自我进化系统 — 知识蒸馏 + 漂移检测 + 预测审计 + 自动校准."""
import json
import os
from datetime import datetime

import requests


class Knowledge:
    def __init__(self, cfg: dict, data_dir: str, analyzer):
        self.cfg = cfg
        self.data_dir = data_dir
        self.analyzer = analyzer
        self.kb_file = os.path.join(data_dir, "knowledge_base.md")
        self.drift_log_file = os.path.join(data_dir, "drift_log.jsonl")
        self.prediction_log_file = os.path.join(data_dir, "prediction_log.jsonl")
        self.calibration_log_file = os.path.join(data_dir, "calibration_log.jsonl")
        self.distill_cfg = cfg.get("distill", {})
        self.drift_cfg = cfg.get("drift", {})

    def _ensure_dir(self):
        os.makedirs(self.data_dir, exist_ok=True)

    def _append_jsonl(self, filepath, data: dict):
        self._ensure_dir()
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")

    def _read_jsonl(self, filepath) -> list[dict]:
        if not os.path.exists(filepath):
            return []
        items = []
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return items

    def _read_jsonl_tail(self, filepath, count: int) -> list[dict]:
        """尾部读取 JSONL，用于漂移检查等只需最近 N 条的场景."""
        if not os.path.exists(filepath) or count <= 0:
            return []
        items = []
        with open(filepath, "r", encoding="utf-8") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            buf_size = min(size, max(8192, count * 256))
            f.seek(max(0, size - buf_size))
            raw = f.read()
        for line in raw.splitlines():
            line = line.strip()
            if line:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return items[-count:]

    # ---- 知识库 ----

    def load_knowledge_base(self) -> str:
        self._ensure_dir()
        if os.path.exists(self.kb_file):
            with open(self.kb_file, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def save_knowledge_base(self, content: str):
        self._ensure_dir()
        with open(self.kb_file, "w", encoding="utf-8") as f:
            f.write(content)

    # ---- 漂移检测 (每轮分析) ----

    def log_drift_meta(self, meta: dict, cycle_position: str):
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "quality": meta.get("analysis_quality", 0),
            "new_terms": meta.get("unrecognized_topics", []),
            "deprecated_candidates": meta.get("deprecated_terms", []),
            "drift_suspected": meta.get("threshold_drift_suspected", False),
            "cycle_position": cycle_position,
        }
        self._append_jsonl(self.drift_log_file, entry)

    def check_drift(self) -> list[str]:
        """检查是否需要触发校准。返回告警列表."""
        alerts = []
        drift_log = self._read_jsonl_tail(self.drift_log_file, 30)

        # 新术语检查
        term_counts = {}
        for entry in drift_log:
            for term in entry.get("new_terms", []):
                term_counts[term] = term_counts.get(term, 0) + 1
        trigger = self.drift_cfg.get("new_term_trigger_count", 5)
        for term, count in term_counts.items():
            if count >= trigger:
                alerts.append(f"新术语 '{term}' 出现 {count} 次, 建议更新指标库")

        # 质量下降检查
        recent = drift_log[-10:] if len(drift_log) > 10 else drift_log
        persist = self.drift_cfg.get("quality_drop_persistence", 3)
        threshold = self.drift_cfg.get("quality_drop_trigger", 5)
        low_quality_run = sum(1 for e in recent[-persist:] if e.get("quality", 10) <= threshold)
        if low_quality_run >= persist:
            alerts.append(f"分析质量连续 {persist} 轮 <= {threshold}, 建议整体校准")

        return alerts

    # ---- 预测审计 ----

    def log_prediction(self, cycle_position: str, confidence: str, btc_price: float = None):
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "cycle_position": cycle_position,
            "confidence": confidence,
            "btc_price": btc_price,
            "verified": False,
        }
        self._append_jsonl(self.prediction_log_file, entry)

    def audit_predictions(self, current_price: float = None) -> dict:
        """审计旧预测的准确性。"""
        preds = self._read_jsonl(self.prediction_log_file)
        max_age_days = self.distill_cfg.get("max_prediction_age_days", 30)
        now = datetime.utcnow()
        results = {"correct": 0, "early": 0, "late": 0, "total": 0}

        for pred in preds:
            if pred.get("verified"):
                continue
            ts = pred.get("ts", "")
            if not ts:
                continue
            try:
                pred_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age = (now - pred_time.replace(tzinfo=None)).days
            except ValueError:
                continue
            if age < max_age_days:
                continue

            results["total"] += 1
            if current_price and pred.get("btc_price"):
                if pred["cycle_position"] in ("BEAR_DEEP", "BEAR_BOTTOM"):
                    if current_price > pred["btc_price"]:
                        results["correct"] += 1
                    else:
                        results["early"] += 1
                elif pred["cycle_position"] in ("DEEP_BULL", "BULL"):
                    if current_price < pred["btc_price"]:
                        results["correct"] += 1
                    else:
                        results["late"] += 1

        return results

    # ---- 知识蒸馏 (每周) ----

    def distill(self, memory, state_manager) -> str | None:
        """蒸馏知识: 压缩 memory.md + 生成新 knowledge_base.md."""
        if not self.analyzer.api_key:
            print("[Knowledge] Cannot distill: no API key")
            return None

        memory_content = memory.load_memory_md()
        audit = self.audit_predictions()
        drift_log = self._read_jsonl_tail(self.drift_log_file, 20)

        alpha_hist = memory.load_alpha_history()
        regime_history = " → ".join(
            f"{h.get('date','')[:10]}:{h.get('regime','')}" for h in alpha_hist[-10:])

        prompts = [
            "## 近期记忆摘要",
            memory_content[-3000:] if len(memory_content) > 3000 else memory_content,
            "",
            "## 预测审计",
            json.dumps(audit, ensure_ascii=False),
            "",
            "## 漂移检测",
            json.dumps(drift_log[-5:], ensure_ascii=False),
            "",
            "## Regime 变迁",
            regime_history,
        ]
        context = "\n".join(prompts)

        distill_prompt = f"""你是知识蒸馏引擎。基于以下历史数据, 生成新的知识库。

要求:
1. 归纳出 "最可靠的 Bull 判定信号" (历史准确率排序)
2. 归纳出 "最可靠的 Bear/Bottom 判定信号" (历史准确率排序)
3. 列出 "常见误判模式" (如过早翻多, regime 变更滞后等)
4. 给出 "当前周期特殊结构" (与历史周期的差异, 如 ETF 改变供需结构等)
5. 建议 "Regime 变更阈值是否调整" (当前为 1.2)
6. 更新 ## 知识库 (自动进化) 章节

格式: Markdown, 清晰简洁.

{context}
"""

        payload = {
            "model": self.analyzer.model,
            "messages": [
                {"role": "system", "content": "你是知识蒸馏引擎, 输出 Markdown 格式的知识库内容."},
                {"role": "user", "content": distill_prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 2048,
        }
        headers = {
            "Authorization": f"Bearer {self.analyzer.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.analyzer.base_url}/chat/completions"

        try:
            print(f"[API-CALL][distill] POST {self.analyzer.model} {datetime.utcnow().isoformat()}Z")
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            print(f"[Knowledge] Distill API error: {e}")
            return None

        if content:
            self.save_knowledge_base(content)
            print(f"[Knowledge] New knowledge_base.md saved ({len(content)} chars)")
            return content
        return None

    # ---- memory.md 压缩 ----

    def compress_memory(self, memory) -> bool:
        """压缩 memory.md 为关键转折点摘要."""
        existing = memory.load_memory_md()
        threshold = self.distill_cfg.get("memory_compress_threshold_chars", 10000)
        if len(existing) <= threshold:
            return False

        if not self.analyzer.api_key:
            return False

        compress_prompt = f"""压缩以下分析历史为关键转折点摘要。
保留: regime 变更时刻、alpha 方向性变化、关键信号出现/消失、错误判断修正。
丢弃: 重复的日常更新、无变化的分析。
输出: 简洁 Markdown, 按时间倒序, 每条 1-2 行。

{existing[-5000:] if len(existing) > 5000 else existing}
"""

        payload = {
            "model": self.analyzer.model,
            "messages": [
                {"role": "system", "content": "你是文本压缩引擎, 输出简洁 Markdown."},
                {"role": "user", "content": compress_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        }
        headers = {
            "Authorization": f"Bearer {self.analyzer.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.analyzer.base_url}/chat/completions"

        try:
            print(f"[API-CALL][compress] POST {self.analyzer.model} {datetime.utcnow().isoformat()}Z")
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            compressed = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            print(f"[Knowledge] Compress error: {e}")
            return False

        if compressed:
            ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            header = f"# Glassnode Alpha Engine Memory\n\n> 上次压缩: {ts}\n\n"
            memory.save_memory_md(header + compressed)
            print(f"[Knowledge] Memory compressed from {len(existing)} to {len(compressed)} chars")
            return True
        return False
