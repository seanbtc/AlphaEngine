"""长期记忆管理 — metrics.json + memory.md + alpha_history.json."""
import json
import os
from datetime import datetime


class Memory:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.metrics_file = os.path.join(data_dir, "metrics.json")
        self.memory_file = os.path.join(data_dir, "memory.md")
        self.alpha_history_file = os.path.join(data_dir, "alpha_history.json")

    def _read_json(self, filepath, default=None):
        if default is None:
            default = {}
        if os.path.exists(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return default
        return default

    def _write_json(self, filepath, data):
        os.makedirs(self.data_dir, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_memory_md(self) -> str:
        os.makedirs(self.data_dir, exist_ok=True)
        if os.path.exists(self.memory_file):
            with open(self.memory_file, "r", encoding="utf-8") as f:
                return f.read()
        return "# Glassnode Alpha Engine Memory\n\n(No analyses yet)\n"

    def save_memory_md(self, content: str):
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.memory_file, "w", encoding="utf-8") as f:
            f.write(content)

    def append_entry(self, entry: str):
        existing = self.load_memory_md()
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        block = f"\n---\n## {ts}\n\n{entry}\n"
        self.save_memory_md(existing.rstrip() + block)

    def load_metrics(self) -> dict:
        return self._read_json(self.metrics_file, {})

    def save_metrics(self, metrics: dict):
        self._write_json(self.metrics_file, metrics)

    def add_metric(self, name: str, value, source_tweet_id: str = ""):
        metrics = self.load_metrics()
        if name not in metrics:
            metrics[name] = []
        metrics[name].append({
            "date": datetime.utcnow().isoformat() + "Z",
            "value": value,
            "source_tweet_id": source_tweet_id,
        })
        self.save_metrics(metrics)

    def save_alpha_history(self, alpha_history: list[dict]):
        self._write_json(self.alpha_history_file, alpha_history)

    def load_alpha_history(self) -> list[dict]:
        return self._read_json(self.alpha_history_file, [])

    def append_alpha(self, entry: dict):
        history = self.load_alpha_history()
        history.append(entry)
        if len(history) > 500:
            history = history[-500:]
        self.save_alpha_history(history)

    def get_context_for_ai(self, max_chars: int = 4000) -> str:
        parts = []

        metrics = self.load_metrics()
        if metrics:
            parts.append("## 历史指标\n")
            for name, entries in metrics.items():
                recent = entries[-5:] if len(entries) > 5 else entries
                vals = ", ".join(f"{e['date'][:10]}={e['value']}" for e in recent)
                parts.append(f"- {name}: {vals}")

        alpha_hist = self.load_alpha_history()
        if alpha_hist:
            parts.append("\n## Alpha 历史趋势\n")
            for h in alpha_hist[-10:]:
                parts.append(f"- {h.get('date','?')}: alpha={h.get('alpha','?')}, "
                             f"regime={h.get('regime','?')}")

        narrative = self._read_tail(self.memory_file, max_chars)
        if narrative:
            parts.append(f"\n## 历史分析\n\n{narrative}")

        return "\n".join(parts)

    @staticmethod
    def _read_tail(filepath: str, max_chars: int) -> str:
        """读取文件末尾 ~max_chars 字符, 不加载全文."""
        if not os.path.exists(filepath) or max_chars <= 0:
            return ""
        with open(filepath, "r", encoding="utf-8") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return ""
            buf_size = min(size, max_chars * 2 + 4096)
            f.seek(max(0, size - buf_size))
            raw = f.read()
            if len(raw) <= max_chars:
                return raw.strip()
            return raw[-max_chars:].strip()
