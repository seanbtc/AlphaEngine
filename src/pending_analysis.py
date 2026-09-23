"""失败推文待分析队列 — 分析失败时落盘, 下轮优先重放 (不丢数据).

存储: data/pending_analysis.jsonl, 每行 {"id", "added_at", "reason"};
去重 (同 ID 保留首次 added_at), 超龄 (max_age_days) 与超上限 (max_items)
的条目在 add() 时清理, 由调用方决定是否告警。
"""
import json
import os
from datetime import datetime, timedelta

from src.atomic_io import atomic_write_text


class PendingAnalysis:
    def __init__(self, data_dir: str, max_items: int = 100, max_age_days: float = 7):
        self.path = os.path.join(data_dir, "pending_analysis.jsonl")
        try:
            self.max_items = max(1, int(max_items))
        except (TypeError, ValueError):
            self.max_items = 100
        try:
            self.max_age_days = max(0.0, float(max_age_days))
        except (TypeError, ValueError):
            self.max_age_days = 7.0
        self._corrupt_printed = False
        self._corrupt_alerted = False

    @staticmethod
    def _parse_ts(value):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except (TypeError, ValueError):
            return None

    def _report_corrupt(self, on_error, message: str) -> None:
        """首次损坏时打印; on_error 只回调一次 (文件恢复后可再次告警)."""
        if not self._corrupt_printed:
            self._corrupt_printed = True
            print(f"[Pending] {message}")
        if on_error is not None and not self._corrupt_alerted:
            self._corrupt_alerted = True
            try:
                on_error(message)
            except Exception as exc:
                print(f"[Pending] 损坏告警发送失败 (不影响主流程): {exc}")

    def load(self, on_error=None) -> list[dict]:
        """读取队列 (损坏行跳过, 同 ID 去重保留首条; 不抛出).

        非 UTF-8 行/IO 错误: 跳过损坏内容并按需回调 on_error(message) 一次。
        """
        if not os.path.exists(self.path):
            self._corrupt_printed = False
            self._corrupt_alerted = False
            return []
        items = []
        seen = set()
        bad_lines = 0
        try:
            with open(self.path, "rb") as f:
                for raw in f:
                    try:
                        line = raw.decode("utf-8").strip()
                    except UnicodeDecodeError:
                        bad_lines += 1
                        continue
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    tid = str(obj.get("id", "") or "")
                    if not tid or tid in seen:
                        continue
                    seen.add(tid)
                    items.append({
                        "id": tid,
                        "added_at": str(obj.get("added_at", "") or ""),
                        "reason": str(obj.get("reason", "") or ""),
                    })
        except OSError as exc:
            self._report_corrupt(
                on_error, f"队列文件读取失败: {type(exc).__name__}: {exc}")
            return []
        if bad_lines:
            self._report_corrupt(
                on_error, f"队列文件含 {bad_lines} 行无法解码 (非 UTF-8), 已跳过")
        else:
            self._corrupt_printed = False
            self._corrupt_alerted = False
        return items

    def count(self) -> int:
        return len(self.load())

    def _save(self, items: list) -> None:
        text = "".join(json.dumps(it, ensure_ascii=False) + "\n" for it in items)
        atomic_write_text(self.path, text)

    def add(self, ids, reason: str = "analysis_failed", now: datetime = None) -> dict:
        """入队一批推文 ID (去重), 并清理超龄/超上限条目.

        返回 {"added", "expired", "overflow", "total", 各丢弃 ID 列表}。
        同 ID 已在队列中时保留原 added_at/reason, 不重复入队。
        """
        now = now or datetime.utcnow()
        now_iso = now.isoformat() + "Z"
        by_id = {it["id"]: it for it in self.load()}
        added = 0
        for raw in ids:
            tid = str(raw or "")
            if not tid or tid in by_id:
                continue
            by_id[tid] = {"id": tid, "added_at": now_iso, "reason": reason}
            added += 1
        items = list(by_id.values())

        cutoff = now - timedelta(days=self.max_age_days)
        kept, expired = [], []
        for item in items:
            ts = self._parse_ts(item.get("added_at"))
            if ts is not None and ts < cutoff:
                expired.append(item)
            else:
                kept.append(item)

        kept.sort(key=lambda it: it.get("added_at", ""))
        overflow = []
        if len(kept) > self.max_items:
            overflow = kept[:len(kept) - self.max_items]
            kept = kept[-self.max_items:]

        if added or expired or overflow:
            self._save(kept)
        return {
            "added": added,
            "expired": len(expired),
            "overflow": len(overflow),
            "total": len(kept),
            "expired_ids": [it["id"] for it in expired],
            "overflow_ids": [it["id"] for it in overflow],
        }

    def remove(self, ids) -> int:
        """移出指定 ID, 返回移除数量 (文件未变化时不重写)."""
        wanted = {str(i) for i in ids if str(i)}
        if not wanted:
            return 0
        existing = self.load()
        kept = [it for it in existing if it["id"] not in wanted]
        removed = len(existing) - len(kept)
        if removed:
            self._save(kept)
        return removed
