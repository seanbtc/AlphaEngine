"""自我进化系统 — 知识蒸馏 + 漂移检测 + 预测审计 + 自动校准."""
import json
import math
import os
from datetime import datetime, timedelta, timezone

from src.atomic_io import atomic_copy, atomic_write_text


def _prediction_key(pred: dict) -> str:
    return str(pred.get("prediction_id") or f"{pred.get('ts','')}|{pred.get('valuation','')}")


def _rate(numerator, denominator):
    return round(numerator / denominator, 3) if denominator else None


# point 判定口径 (2026-11 起): 方向错统一记 wrong_direction。
# 旧 outcomes 里的 early/late 语义相同 (只是"过早/过晚"的修饰), 读侧归一兼容。
_LEGACY_POINT_VERDICTS = {"early": "wrong_direction", "late": "wrong_direction"}


def normalize_point_verdict(verdict):
    """把旧 early/late 映射为 wrong_direction; 其它值原样返回。"""
    if not isinstance(verdict, str):
        return None
    return _LEGACY_POINT_VERDICTS.get(verdict, verdict)


def _ece(pairs):
    """ECE（5 箱）; pairs = [(probability, hit_bool)]。"""
    if not pairs:
        return None
    bins = [[] for _ in range(5)]
    for probability, hit in pairs:
        index = min(4, max(0, int(probability * 5)))
        bins[index].append((probability, hit))
    total = len(pairs)
    value = 0.0
    for bucket in bins:
        if not bucket:
            continue
        avg_probability = sum(item[0] for item in bucket) / len(bucket)
        avg_hit = sum(1 for item in bucket if item[1]) / len(bucket)
        value += abs(avg_probability - avg_hit) * len(bucket) / total
    return round(value, 6)


# 知识库最小结构要求: 用于拦截"reasoning 回退文本 / 截断残片"注入 prompt。
_KB_MIN_CHARS = 200
_KB_MIN_HEADINGS = 2


def validate_knowledge_text(content: str) -> tuple[bool, str]:
    """粗校验知识库文本是否为正常 Markdown 蒸馏结果。

    2026-10 故障: DeepSeek reasoning 吃满 max_tokens, AIService 回退
    reasoning_content, 12/12 次污染文本 (0 个 Markdown 标题) 被注入每轮
    prompt。此守卫在**读取**与**保存前**各拦一次:
    - 空/过短 → 无效
    - 少于 2 个 Markdown 标题 → 视为非结构化推理文本
    """
    text = (content or "").strip()
    if not text:
        return False, "empty"
    if len(text) < _KB_MIN_CHARS:
        return False, f"too short ({len(text)} chars)"
    headings = sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))
    if headings < _KB_MIN_HEADINGS:
        return False, f"no markdown structure (headings={headings})"
    return True, ""


# memory 压缩结果最小结构: 空/过短/无 Markdown 结构 → 拒绝覆盖旧 memory。
_MEMORY_MIN_CHARS = 50


def validate_memory_summary(content: str) -> tuple[bool, str]:
    """校验 memory 压缩输出: 拦截 reasoning 回退文本/截断残片覆盖 memory.md。"""
    text = (content or "").strip()
    if not text:
        return False, "empty"
    if len(text) < _MEMORY_MIN_CHARS:
        return False, f"too short ({len(text)} chars)"
    structured = any(line.lstrip().startswith(("#", "-", "*"))
                     for line in text.splitlines())
    if not structured:
        return False, "no markdown structure"
    return True, ""


def build_calibration_summary(predictions: list, outcomes: list) -> dict:
    """K-03 预测校准汇总：point 命中 / 区间覆盖 / Brier / ECE / 分桶 / 重跑离散。

    输入为 prediction_log.jsonl 与 prediction_outcomes.jsonl 的行列表；
    旧记录缺字段一律跳过对应指标，不抛异常。outcomes 按 prediction_id 去重（保留最后一条），
    防止侧车并发/手工补写导致重复计数。
    """
    predictions = [item for item in (predictions or []) if isinstance(item, dict)]
    raw_outcomes = [item for item in (outcomes or []) if isinstance(item, dict)]
    deduped = {}
    for index, outcome in enumerate(raw_outcomes):
        key = str(outcome.get("prediction_id") or f"__unknown__{index}")
        deduped[key] = outcome
    outcomes = list(deduped.values())
    pred_by_id = {_prediction_key(item): item for item in predictions}

    point_counts = {"correct": 0, "wrong_direction": 0, "flat": 0}
    point_by_kind = {
        "legacy": {"correct": 0, "wrong_direction": 0, "flat": 0},
        "forecast": {"correct": 0, "wrong_direction": 0, "flat": 0},
    }
    forecast_rows = []
    confidence = {}
    by_prompt = {}
    by_model = {}
    by_horizon = {}
    by_price_source = {}

    def _bucket(container, key):
        return container.setdefault(
            str(key or "unknown"),
            {"point_n": 0, "point_hits": 0, "forecast_n": 0,
             "forecast_hits": 0, "probabilities": []},
        )

    def _horizon_bucket(key):
        return by_horizon.setdefault(
            str(key if key is not None else "unknown"),
            {"n": 0, "hits": 0, "widths": [], "briers": [], "probabilities": []},
        )

    for outcome in outcomes:
        pid = str(outcome.get("prediction_id") or "")
        pred = pred_by_id.get(pid, {})
        conf = outcome.get("confidence") or pred.get("confidence") or "unknown"
        prompt_hash = pred.get("prompt_hash") or outcome.get("prompt_hash") or "unknown"
        model = pred.get("model") or "unknown"
        verdict = normalize_point_verdict(outcome.get("verdict"))
        if verdict in point_counts:
            point_counts[verdict] += 1
            kind = ("forecast"
                    if isinstance(outcome.get("forecast"), dict)
                    and outcome.get("interval_hit") is not None
                    else "legacy")
            point_by_kind[kind][verdict] += 1
            _bucket(confidence, conf)["point_n"] += 1
            _bucket(by_prompt, prompt_hash)["point_n"] += 1
            _bucket(by_model, model)["point_n"] += 1
            if verdict == "correct":
                _bucket(confidence, conf)["point_hits"] += 1
                _bucket(by_prompt, prompt_hash)["point_hits"] += 1
                _bucket(by_model, model)["point_hits"] += 1

        if isinstance(outcome.get("forecast"), dict) and outcome.get("interval_hit") is not None:
            interval_hit = bool(outcome.get("interval_hit"))
            direction_hit = outcome.get("direction_hit")
            width = outcome.get("width_pct")
            probability = outcome.get("forecast", {}).get("hit_probability")
            try:
                probability = float(probability) if probability is not None else None
            except (TypeError, ValueError, OverflowError):
                probability = None
            horizon = outcome.get("horizon_days")
            if horizon is None:
                horizon = outcome.get("forecast", {}).get("horizon_days")
            price_offset = outcome.get("horizon_price_offset_days")
            price_source = outcome.get("horizon_price_source") or "unknown"
            row = {
                "pid": pid,
                "confidence": conf,
                "prompt_hash": prompt_hash,
                "model": model,
                "horizon": horizon,
                "price_offset": price_offset,
                "price_source": price_source,
                "hit": interval_hit,
                "direction_hit": direction_hit,
                "width": float(width) if isinstance(width, (int, float)) else None,
                "probability": probability,
                "brier": outcome.get("brier"),
            }
            forecast_rows.append(row)
            _bucket(confidence, conf)["forecast_n"] += 1
            _bucket(by_prompt, prompt_hash)["forecast_n"] += 1
            _bucket(by_model, model)["forecast_n"] += 1
            if interval_hit:
                _bucket(confidence, conf)["forecast_hits"] += 1
                _bucket(by_prompt, prompt_hash)["forecast_hits"] += 1
                _bucket(by_model, model)["forecast_hits"] += 1
            if probability is not None:
                _bucket(confidence, conf)["probabilities"].append((probability, interval_hit))
                _bucket(by_prompt, prompt_hash)["probabilities"].append((probability, interval_hit))
                _bucket(by_model, model)["probabilities"].append((probability, interval_hit))

            hb = _horizon_bucket(horizon)
            hb["n"] += 1
            if interval_hit:
                hb["hits"] += 1
            if row["width"] is not None:
                hb["widths"].append(row["width"])
            if isinstance(row["brier"], (int, float)):
                hb["briers"].append(row["brier"])
            if probability is not None:
                hb["probabilities"].append((probability, interval_hit))

            ps = by_price_source.setdefault(
                str(price_source), {"n": 0, "hits": 0, "offsets": []})
            ps["n"] += 1
            if interval_hit:
                ps["hits"] += 1
            if isinstance(price_offset, (int, float)):
                ps["offsets"].append(abs(float(price_offset)))

    point_decided = point_counts["correct"] + point_counts["wrong_direction"]
    forecast_n = len(forecast_rows)
    forecast_hits = sum(1 for row in forecast_rows if row["hit"])
    direction_rows = [row for row in forecast_rows if row["direction_hit"] is not None]
    direction_hits = sum(1 for row in direction_rows if row["direction_hit"])
    widths = [row["width"] for row in forecast_rows if row["width"] is not None]
    briers = [row["brier"] for row in forecast_rows if isinstance(row["brier"], (int, float))]
    probability_pairs = [(row["probability"], row["hit"])
                         for row in forecast_rows if row["probability"] is not None]

    cross_rows = [pred.get("cross_check") for pred in predictions
                  if isinstance(pred.get("cross_check"), dict)]

    def _band_mismatch(item):
        """跨档判定: 新字段 bands 优先, 旧字段 positions 兜底。"""
        bands = item.get("bands")
        if isinstance(bands, list):
            return len(set(bands)) > 1
        return len(set(item.get("positions") or [])) > 1

    def _first_number(item, *keys):
        for key in keys:
            value = item.get(key)
            if isinstance(value, (int, float)):
                return value
        return None

    dispersed = sum(1 for item in cross_rows if item.get("dispersed"))
    position_mismatch = sum(1 for item in cross_rows if _band_mismatch(item))
    soft_dispersed = sum(
        1 for item in cross_rows
        if item.get("dispersed") and not _band_mismatch(item)
    )
    position_agreements = [value for value in
                           (_first_number(item, "band_agreement", "position_agreement")
                            for item in cross_rows)
                           if value is not None]
    progress_stds = [value for value in
                     (_first_number(item, "valuation_spread", "progress_std")
                      for item in cross_rows)
                     if value is not None]
    direction_agreements = [item.get("forecast_direction_agreement") for item in cross_rows
                            if isinstance(item.get("forecast_direction_agreement"), (int, float))]

    def _bucket_output(container):
        output = {}
        for key, item in sorted(container.items()):
            probabilities = item.pop("probabilities", [])
            item["point_hit_rate"] = _rate(item["point_hits"], item["point_n"])
            item["forecast_coverage"] = _rate(item["forecast_hits"], item["forecast_n"])
            item["brier"] = (
                round(sum((p - (1.0 if hit else 0.0)) ** 2 for p, hit in probabilities)
                      / len(probabilities), 6)
                if probabilities else None
            )
            output[key] = item
        return output

    def _point_kind_output(kind):
        counts = point_by_kind[kind]
        decided = counts["correct"] + counts["wrong_direction"]
        return {**counts, "decided": decided,
                "hit_rate": _rate(counts["correct"], decided)}

    horizon_output = {}
    for key, item in sorted(by_horizon.items(), key=lambda pair: pair[0]):
        widths_h = item["widths"]
        briers_h = item["briers"]
        horizon_output[key] = {
            "n": item["n"],
            "hits": item["hits"],
            "coverage": _rate(item["hits"], item["n"]),
            "mean_width_pct": round(sum(widths_h) / len(widths_h), 2) if widths_h else None,
            "mean_brier": round(sum(briers_h) / len(briers_h), 6) if briers_h else None,
            "ece": _ece(item["probabilities"]),
            "probability_n": len(item["probabilities"]),
        }

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    outcome_ids = set(deduped)
    pending = 0
    awaiting_horizon = 0
    for pred in predictions:
        pid = _prediction_key(pred)
        if pid in outcome_ids or pred.get("verified"):
            continue
        try:
            pred_time = datetime.fromisoformat(str(pred.get("ts", "")).replace("Z", "+00:00"))
            age_days = (now - pred_time.replace(tzinfo=None)).days
        except (TypeError, ValueError):
            pending += 1
            continue
        forecast = pred.get("forecast") if isinstance(pred.get("forecast"), dict) else None
        horizon = None
        if forecast:
            try:
                horizon = int(forecast.get("horizon_days"))
            except (TypeError, ValueError, OverflowError):
                horizon = None
        if horizon and age_days < horizon:
            awaiting_horizon += 1
        else:
            pending += 1

    return {
        "generated_at": now.isoformat() + "Z",
        "n_predictions": len(predictions),
        "n_outcomes": len(outcomes),
        "pending": pending,
        "awaiting_horizon": awaiting_horizon,
        "skipped": sum(1 for item in outcomes if item.get("verdict") == "skipped"),
        "point": {
            **point_counts,
            "decided": point_decided,
            "hit_rate": _rate(point_counts["correct"], point_decided),
        },
        "point_by_kind": {
            "legacy": _point_kind_output("legacy"),
            "forecast": _point_kind_output("forecast"),
        },
        "forecast": {
            "n": forecast_n,
            "hits": forecast_hits,
            "coverage": _rate(forecast_hits, forecast_n),
            "direction_n": len(direction_rows),
            "direction_hits": direction_hits,
            "direction_hit_rate": _rate(direction_hits, len(direction_rows)),
            "mean_width_pct": round(sum(widths) / len(widths), 2) if widths else None,
            "mean_brier": round(sum(briers) / len(briers), 6) if briers else None,
            "ece": _ece(probability_pairs),
            "probability_n": len(probability_pairs),
        },
        "by_horizon": horizon_output,
        "by_price_source": {
            key: {
                "n": item["n"],
                "hits": item["hits"],
                "coverage": _rate(item["hits"], item["n"]),
                "mean_abs_offset_days": (
                    round(sum(item["offsets"]) / len(item["offsets"]), 2)
                    if item["offsets"] else None
                ),
            }
            for key, item in sorted(by_price_source.items())
        },
        "by_confidence": _bucket_output(confidence),
        "by_prompt_hash": _bucket_output(by_prompt),
        "by_model": _bucket_output(by_model),
        "cross_check": {
            "n": len(cross_rows),
            "dispersed": dispersed,
            "dispersed_rate": _rate(dispersed, len(cross_rows)),
            "band_mismatch": position_mismatch,
            "band_mismatch_rate": _rate(position_mismatch, len(cross_rows)),
            "position_mismatch": position_mismatch,
            "position_mismatch_rate": _rate(position_mismatch, len(cross_rows)),
            "soft_dispersed": soft_dispersed,
            "soft_dispersed_rate": _rate(soft_dispersed, len(cross_rows)),
            "mean_band_agreement": (
                round(sum(position_agreements) / len(position_agreements), 4)
                if position_agreements else None
            ),
            "mean_position_agreement": (
                round(sum(position_agreements) / len(position_agreements), 4)
                if position_agreements else None
            ),
            "mean_valuation_spread": (
                round(sum(progress_stds) / len(progress_stds), 4)
                if progress_stds else None
            ),
            "mean_progress_std": (
                round(sum(progress_stds) / len(progress_stds), 4)
                if progress_stds else None
            ),
            "mean_forecast_direction_agreement": (
                round(sum(direction_agreements) / len(direction_agreements), 4)
                if direction_agreements else None
            ),
        },
    }


class Knowledge:
    def __init__(self, cfg: dict, data_dir: str, analyzer):
        self.cfg = cfg
        self.data_dir = data_dir
        self.analyzer = analyzer
        self.kb_file = os.path.join(data_dir, "knowledge_base.md")
        self.drift_log_file = os.path.join(data_dir, "drift_log.jsonl")
        self.prediction_log_file = os.path.join(data_dir, "prediction_log.jsonl")
        self.prediction_outcome_file = os.path.join(data_dir, "prediction_outcomes.jsonl")
        self.distill_cfg = cfg.get("distill", {})
        self.drift_cfg = cfg.get("drift", {})
        self._kb_guard_warned = ""

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
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        items.append(item)
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
        """读取知识库; 结构不合法 (reasoning 回退/截断残片) 时返回空串。

        只拦截注入, 不删除/改写文件 —— 旧文件保留供人工排查。
        """
        self._ensure_dir()
        if not os.path.exists(self.kb_file):
            return ""
        try:
            with open(self.kb_file, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as exc:
            print(f"[Knowledge] 知识库读取失败: {exc}")
            return ""
        ok, reason = validate_knowledge_text(content)
        if not ok:
            if self._kb_guard_warned != reason:
                print(f"[Knowledge] 知识库内容无效, 已拦截注入 ({reason}; "
                      f"{len(content)} chars, file={self.kb_file})")
                self._kb_guard_warned = reason
            return ""
        self._kb_guard_warned = ""
        return content

    def save_knowledge_base(self, content: str):
        """原子覆盖 knowledge_base.md; 覆盖前把旧版备份为 knowledge_base.md.bak."""
        self._ensure_dir()
        if os.path.exists(self.kb_file):
            try:
                atomic_copy(self.kb_file, self.kb_file + ".bak")
            except OSError as exc:
                print(f"[Knowledge] knowledge_base.md 备份失败: {exc}")
        atomic_write_text(self.kb_file, content)

    # ---- 漂移检测 (每轮分析) ----

    def log_drift_meta(self, meta: dict, valuation_label: str = ""):
        entry = {
            "ts": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            "quality": meta.get("analysis_quality", 0),
            "new_terms": meta.get("unrecognized_topics", []),
            "deprecated_candidates": meta.get("deprecated_terms", []),
            "drift_suspected": meta.get("threshold_drift_suspected", False),
            "valuation_label": valuation_label,
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

    def log_prediction(self, valuation, confidence: str, btc_price: float = None,
                       prompt_hash: str = None, model: str = None,
                       temperature: float = None,
                       forecast: dict = None, source_type_counts: dict = None,
                       cross_check: dict = None, valuation_label: str = None):
        """记录一条预测 (valuation = 本轮 AI 估值 0~1)。

        追溯字段: prompt_hash 可选 (缺省取 analyzer.prompt_hash, 再回退模块常量
        PROMPT_HASH; 最终兜底 "unknown"), 写入时必带该键;
        model/temperature 从 AIService 调用链取 (响应 model / 请求 temperature),
        拿不到时省略字段 (不写 null, 便于按字段存在性过滤)。

        K-03 可审计字段 (均可选, 旧调用兼容):
        - forecast: {horizon_days, direction, price_low, price_high, confidence,
          hit_probability, basis}
        - source_type_counts: {retrieved, remembered, inferred, unknown}
        - cross_check: {samples, valuations, valuation_spread, bands,
          band_agreement, forecast_direction_agreement, dispersed, ...}
        """
        if btc_price is None:
            # DataFeed 不可用时用历史最后已知价格兜底, 避免预测价格全为 null (审计空转)
            btc_price = self._last_known_price()
        if prompt_hash is None:
            prompt_hash = getattr(self.analyzer, "prompt_hash", None)
        if prompt_hash is None:
            try:
                from src.analyzer import PROMPT_HASH
                prompt_hash = PROMPT_HASH
            except Exception:
                prompt_hash = "unknown"
        if model is None:
            model = getattr(self.analyzer, "last_call_model", None)
        if temperature is None:
            temperature = getattr(self.analyzer, "last_call_temperature", None)
        from src.valuation import band_of, is_valid
        value_text = f"{float(valuation):.4f}" if is_valid(valuation) else "na"
        label = valuation_label or (band_of(valuation) if is_valid(valuation) else "")
        ts = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
        entry = {
            "ts": ts,
            "prediction_id": f"{ts}|{value_text}",
            "valuation": round(float(valuation), 4) if is_valid(valuation) else None,
            "valuation_label": label,
            "confidence": confidence,
            "btc_price": btc_price,
            "verified": False,
            "prompt_hash": prompt_hash,
        }
        model_text = str(model or "").strip()
        if model_text:
            entry["model"] = model_text
        if temperature is not None:
            try:
                entry["temperature"] = float(temperature)
            except (TypeError, ValueError):
                pass
        if isinstance(forecast, dict) and forecast:
            entry["forecast"] = forecast
        if isinstance(source_type_counts, dict) and source_type_counts:
            entry["source_type_counts"] = source_type_counts
        if isinstance(cross_check, dict) and cross_check:
            entry["cross_check"] = cross_check
        self._append_jsonl(self.prediction_log_file, entry)

    def _prediction_id(self, pred: dict) -> str:
        return str(pred.get("prediction_id") or f"{pred.get('ts','')}|{pred.get('valuation','')}")

    def _load_prediction_outcomes(self) -> dict:
        """读取预测审计侧车 (prediction_outcomes.jsonl), 返回 prediction_id -> 结果。"""
        outcomes = {}
        for item in self._read_jsonl(self.prediction_outcome_file):
            pid = str(item.get("prediction_id") or "").strip()
            if pid:
                outcomes[pid] = item
        return outcomes

    def _price_history(self) -> list:
        """读取 price_history.jsonl, 返回 [(datetime, price)] 升序列表。"""
        path = os.path.join(self.data_dir, "price_history.jsonl")
        if not os.path.exists(path):
            return []
        entries = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    ts = datetime.fromisoformat(str(item["ts"]).replace("Z", "+00:00")).replace(tzinfo=None)
                    price = float(item["btc_price"])
                except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                    continue
                entries.append((ts, price))
        entries.sort(key=lambda e: e[0])
        return entries

    def _last_known_price(self):
        history = self._price_history()
        return history[-1][1] if history else None

    def _price_at(self, ts_text: str, history=None):
        """取预测时刻(之前最近)的价格, 用于补全历史预测的入场价。"""
        try:
            target = datetime.fromisoformat(str(ts_text).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
        history = history if history is not None else self._price_history()
        selected = None
        for ts, price in history:
            if ts <= target:
                selected = price
            else:
                break
        return selected

    # ---- 价格快照 (原 review_engine.record_price/price_trend 并入) ----

    def record_price(self, price, valuation=None) -> None:
        """追加一条价格快照到 price_history.jsonl (趋势/预测审计共用)。"""
        try:
            value = float(price)
        except (TypeError, ValueError):
            return
        if not (value > 0):
            return
        from src.valuation import is_valid
        entry = {
            "ts": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            "btc_price": value,
        }
        if is_valid(valuation):
            entry["valuation"] = round(float(valuation), 4)
        self._append_jsonl(os.path.join(self.data_dir, "price_history.jsonl"), entry)

    def price_trend(self, days_7: int = 7, days_30: int = 30) -> dict:
        """返回近 N 天价格趋势: {price, change_7d, change_30d}; 数据不足时对应项为 None."""
        result = {"price": None, "change_7d": None, "change_30d": None}
        history = self._price_history()
        if not history:
            return result
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        window = [(ts, price) for ts, price in history
                  if (now - ts).total_seconds() >= 0]
        if not window:
            return result
        result["price"] = window[-1][1]
        cutoff7 = now - timedelta(days=days_7)
        recent7 = [price for ts, price in window if ts >= cutoff7]
        if len(recent7) >= 2 and recent7[0]:
            result["change_7d"] = round((recent7[-1] - recent7[0]) / recent7[0] * 100, 2)
        cutoff30 = now - timedelta(days=days_30)
        recent30 = [price for ts, price in window if ts >= cutoff30]
        if len(recent30) >= 2 and recent30[0]:
            result["change_30d"] = round((recent30[-1] - recent30[0]) / recent30[0] * 100, 2)
        return result

    # 估值 → 价格预期死区: 0.45/0.55 之间视为"合理", 不押方向 (避免边界抖动制造假预测)
    EXPECT_UP_BELOW = 0.45
    EXPECT_DOWN_ABOVE = 0.55
    # 旧 (v1, regime 语义) 预测的审计口径兜底: 保证历史记录仍可被方向性审计
    _LEGACY_REGIME_EXPECTATION = {
        "RECOVERY": "up", "BULL": "up", "DEEP_BULL": "up",
        "BEAR": "down", "BEAR_DEEP": "down",
        "BEAR_BOTTOM": "flat", "BULL_COOLING": "flat",
    }

    @classmethod
    def _expected_direction(cls, valuation, legacy_cycle_position=None):
        """按估值判断价格预期: 低估→up, 高估→down, 合理→flat; 非法返回 None。

        旧记录 (无 valuation) 按 cycle_position 映射兜底, 不改变历史审计结论。
        """
        from src.valuation import is_valid
        if not is_valid(valuation):
            return cls._LEGACY_REGIME_EXPECTATION.get(
                str(legacy_cycle_position or ""))
        value = float(valuation)
        if value < cls.EXPECT_UP_BELOW:
            return "up"
        if value > cls.EXPECT_DOWN_ABOVE:
            return "down"
        return "flat"

    @staticmethod
    def _prediction_verdict(expected: str, move_pct: float) -> str:
        """按预期方向与实际涨跌幅给出 correct/wrong_direction/flat。

        2026-11 起不再区分 early/late: 二者都是"方向判断错误",
        分拆只会稀释样本、掩盖连续反向 (历史数据里 11/14 条都是 early)。
        """
        if expected == "up":
            if move_pct >= 2.0:
                return "correct"
            if move_pct <= -2.0:
                return "wrong_direction"
            return "flat"
        if expected == "down":
            if move_pct <= -2.0:
                return "correct"
            if move_pct >= 2.0:
                return "wrong_direction"
            return "flat"
        if expected == "flat":
            return "correct" if abs(move_pct) <= 3.0 else "flat"
        return "flat"

    def _price_near_target(self, target, history=None, tolerance_days: int = 7):
        """取 target 附近最近价（默认 ±7 天，平局偏好 after）。

        返回 (price, offset_days, source)；source='after'/'before'。
        用 ±7 天而不是严格 target，避免分析日与 horizon 日错位导致永久 skipped；
        before 表示 horizon 被缩短，outcome 会留痕。
        """
        history = history if history is not None else self._price_history()
        best = None
        for ts, price in history:
            offset_days = (ts - target).total_seconds() / 86400.0
            if abs(offset_days) > tolerance_days:
                continue
            key = (abs(offset_days), 0 if offset_days >= 0 else 1)
            if best is None or key < best[0]:
                best = (key, price, offset_days)
        if best is None:
            return None
        _, price, offset_days = best
        return price, round(offset_days, 2), "after" if offset_days >= 0 else "before"

    def audit_predictions(self, current_price: float = None) -> dict:
        """审计到期预测并写回结果侧车; 返回统计 (含 point 命中率 / 区间覆盖 / Brier)。

        - current_price 缺省时用 price_history 最后价格兜底;
        - 已审计的预测 (prediction_outcomes.jsonl 中已有 prediction_id) 不重复审计;
        - 结果写侧车而非改写主日志, 保持 append-only;
        - K-03: 带 forecast 的预测按 horizon_days 到期审计（区间命中/方向/Brier/宽度），
          旧的无 forecast 预测仍按 max_prediction_age_days 做 point 审计。
        """
        preds = self._read_jsonl(self.prediction_log_file)
        outcomes = self._load_prediction_outcomes()
        try:
            max_age_days = int(self.distill_cfg.get("max_prediction_age_days", 30))
        except (TypeError, ValueError):
            max_age_days = 30
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if current_price is None:
            current_price = self._last_known_price()
        price_history = self._price_history()
        results = {
            "correct": 0, "wrong_direction": 0, "flat": 0, "total": 0,
            "hit_rate": 0.0, "new_audited": 0, "skipped": 0,
            "forecast_total": 0, "forecast_hits": 0,
            "forecast_direction_hits": 0, "forecast_direction_n": 0,
            "forecast_brier_sum": 0.0,
            "forecast_brier_n": 0, "forecast_width_sum": 0.0,
            "interval_coverage": 0.0, "forecast_direction_hit_rate": 0.0,
            "forecast_brier": None, "forecast_mean_width_pct": 0.0,
        }
        audited = 0

        new_outcomes = []
        for pred in preds:
            pid = self._prediction_id(pred)
            if pid in outcomes or pred.get("verified"):
                continue
            ts = pred.get("ts", "")
            if not ts:
                continue
            try:
                pred_time = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                pred_time = pred_time.replace(tzinfo=None)
                age = (now - pred_time).days
            except ValueError:
                continue

            forecast = pred.get("forecast") if isinstance(pred.get("forecast"), dict) else None
            horizon = None
            forecast_range = None
            if forecast:
                try:
                    horizon = int(forecast.get("horizon_days"))
                    low = float(forecast.get("price_low"))
                    high = float(forecast.get("price_high"))
                except (TypeError, ValueError, OverflowError):
                    forecast, horizon = None, None
                else:
                    if (horizon not in (30, 90)
                            or not (math.isfinite(low) and math.isfinite(high))
                            or not (low > 0 and high > 0 and low <= high)):
                        forecast, horizon = None, None
                    else:
                        forecast_range = (low, high)
            wait_days = horizon if forecast else max_age_days
            if age < wait_days:
                continue

            entry_price = pred.get("btc_price") or self._price_at(ts, price_history)
            try:
                entry_price = float(entry_price) if entry_price else None
            except (TypeError, ValueError):
                entry_price = None

            audit_price = None
            horizon_price_offset = None
            horizon_price_source = None
            if forecast:
                target = pred_time + timedelta(days=horizon)
                nearest = self._price_near_target(target, price_history)
                if nearest is None:
                    if age < wait_days + 30:
                        continue
                    outcome = {
                        "prediction_id": pid,
                        "ts": ts,
                        "valuation": pred.get("valuation"),
                        "valuation_label": pred.get("valuation_label"),
                        "confidence": pred.get("confidence"),
                        "forecast": forecast,
                        "horizon_days": horizon,
                        "verdict": "skipped",
                        "reason": "missing horizon price",
                        "audited_at": now.isoformat() + "Z",
                    }
                    new_outcomes.append(outcome)
                    outcomes[pid] = outcome
                    results["skipped"] += 1
                    continue
                audit_price, horizon_price_offset, horizon_price_source = nearest
            else:
                try:
                    audit_price = float(current_price) if current_price else None
                except (TypeError, ValueError):
                    audit_price = None

            if not entry_price or not audit_price:
                # 无入场价(且价格历史也没有) → 标记 skipped, 避免永久悬空
                outcome = {
                    "prediction_id": pid,
                    "ts": ts,
                    "valuation": pred.get("valuation"),
                    "valuation_label": pred.get("valuation_label"),
                    "confidence": pred.get("confidence"),
                    "verdict": "skipped",
                    "reason": "missing entry/audit price",
                    "audited_at": now.isoformat() + "Z",
                }
                new_outcomes.append(outcome)
                outcomes[pid] = outcome
                results["skipped"] += 1
                continue

            move_pct = (audit_price - entry_price) / entry_price * 100.0
            expected = self._expected_direction(pred.get("valuation"),
                                                pred.get("cycle_position"))
            verdict = self._prediction_verdict(expected, move_pct)
            outcome = {
                "prediction_id": pid,
                "ts": ts,
                "valuation": pred.get("valuation"),
                "valuation_label": pred.get("valuation_label"),
                "confidence": pred.get("confidence"),
                "entry_price": round(entry_price, 2),
                "audit_price": round(audit_price, 2),
                "move_pct": round(move_pct, 2),
                "expected": expected,
                "verdict": verdict,
                "age_days": age,
                "audited_at": now.isoformat() + "Z",
            }

            forecast_fields = {}
            if forecast:
                low, high = forecast_range
                interval_hit = bool(low <= audit_price <= high)
                width_pct = (high - low) / entry_price * 100.0
                direction = str(forecast.get("direction") or "").strip().lower() or None
                direction_hit = self._forecast_direction_hit(direction, move_pct)
                hit_probability = forecast.get("hit_probability")
                try:
                    hit_probability = float(hit_probability)
                except (TypeError, ValueError, OverflowError):
                    hit_probability = None
                if hit_probability is not None:
                    if not math.isfinite(hit_probability):
                        hit_probability = None
                    else:
                        hit_probability = min(1.0, max(0.0, hit_probability))
                brier = None
                if hit_probability is not None:
                    brier = (hit_probability - (1.0 if interval_hit else 0.0)) ** 2
                forecast_fields = {
                    "forecast": forecast,
                    "horizon_days": horizon,
                    "horizon_price": round(audit_price, 2),
                    "horizon_price_offset_days": horizon_price_offset,
                    "horizon_price_source": horizon_price_source,
                    "interval_hit": interval_hit,
                    "forecast_direction": direction,
                    "direction_hit": direction_hit,
                    "forecast_verdict": "hit" if interval_hit else "miss",
                    "width_pct": round(width_pct, 2),
                }
                if brier is not None:
                    forecast_fields["brier"] = round(brier, 6)
                outcome.update(forecast_fields)

                results["forecast_total"] += 1
                if interval_hit:
                    results["forecast_hits"] += 1
                if direction_hit is not None:
                    results["forecast_direction_n"] += 1
                    if direction_hit:
                        results["forecast_direction_hits"] += 1
                results["forecast_width_sum"] += width_pct
                if brier is not None:
                    results["forecast_brier_sum"] += brier
                    results["forecast_brier_n"] += 1

            new_outcomes.append(outcome)
            outcomes[pid] = outcome
            results[verdict] = results.get(verdict, 0) + 1
            results["total"] += 1
            audited += 1

        for outcome in new_outcomes:
            self._append_jsonl(self.prediction_outcome_file, outcome)
        results["new_audited"] = audited

        decided = results["correct"] + results["wrong_direction"]
        results["hit_rate"] = round(results["correct"] / decided, 3) if decided else 0.0
        forecast_total = results["forecast_total"]
        if forecast_total:
            results["interval_coverage"] = round(
                results["forecast_hits"] / forecast_total, 3)
            results["forecast_direction_hit_rate"] = round(
                results["forecast_direction_hits"] / results["forecast_direction_n"], 3) \
                if results["forecast_direction_n"] else 0.0
            results["forecast_mean_width_pct"] = round(
                results["forecast_width_sum"] / forecast_total, 2)
        if results["forecast_brier_n"]:
            results["forecast_brier"] = round(
                results["forecast_brier_sum"] / results["forecast_brier_n"], 6)
        results.pop("forecast_brier_sum", None)
        results.pop("forecast_brier_n", None)
        results.pop("forecast_width_sum", None)
        return results

    @staticmethod
    def _forecast_direction_hit(direction, move_pct: float):
        if direction == "up":
            return bool(move_pct >= 2.0)
        if direction == "down":
            return bool(move_pct <= -2.0)
        if direction == "flat":
            return bool(abs(move_pct) <= 3.0)
        return None

    def calibration_summary(self) -> dict:
        """K-03 校准汇总（只读；供 review/distill/tools 使用）。"""
        return build_calibration_summary(
            self._read_jsonl(self.prediction_log_file),
            self._read_jsonl(self.prediction_outcome_file),
        )

    # ---- 知识蒸馏 (每周) ----

    _WEEKDAY_INDEX = {
        'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
        'friday': 4, 'saturday': 5, 'sunday': 6,
    }

    def distill_due(self, state_manager, now=None) -> bool:
        """按 knowledge.distill 配置判断是否到期。

        分析周期(周二/周四)与计划时刻(默认周日 0 点 UTC)不重合, 因此按
        "最近一个计划时刻" 比较: 上一轮分析错过后在下一轮分析时补偿执行。
        """
        now = now or datetime.now(timezone.utc).replace(tzinfo=None)
        cfg = self.distill_cfg or {}
        day_name = str(cfg.get('schedule_day') or 'sunday').strip().lower()
        target_weekday = self._WEEKDAY_INDEX.get(day_name, 6)
        try:
            hour = int(cfg.get('schedule_hour_utc') or 0)
        except (TypeError, ValueError):
            hour = 0
        hour = max(0, min(hour, 23))
        boundary = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        boundary -= timedelta(days=(now.weekday() - target_weekday) % 7)
        if boundary > now:
            boundary -= timedelta(days=7)
        last_text = str(state_manager.get('runtime.last_distill_at', '') or '').strip()
        if not last_text:
            return True
        try:
            last_dt = datetime.fromisoformat(last_text.replace('Z', '+00:00')).replace(tzinfo=None)
        except ValueError:
            return True
        return last_dt < boundary

    def distill(self, memory, state_manager, current_price: float = None) -> str | None:
        """蒸馏知识: 压缩 memory.md + 生成新 knowledge_base.md."""
        if not getattr(self.analyzer, "enabled", True):
            print("[Knowledge] Cannot distill: AI 服务已禁用")
            return None

        memory_content = memory.load_memory_md()
        audit = self.audit_predictions(current_price)
        try:
            calibration = self.calibration_summary()
        except Exception as exc:  # 审计只读，失败不阻断蒸馏
            calibration = {"error": f"{type(exc).__name__}: {exc}"}
        drift_log = self._read_jsonl_tail(self.drift_log_file, 20)

        valuation_hist = memory.load_valuation_history()
        valuation_history = " → ".join(
            f"{h.get('date','')[:10]}:{h.get('valuation','?')}({h.get('label','?')})"
            for h in valuation_hist[-10:])

        prompts = [
            "## 近期记忆摘要",
            memory_content[-3000:] if len(memory_content) > 3000 else memory_content,
            "",
            "## 预测审计",
            json.dumps(audit, ensure_ascii=False),
            "",
            "## 预测校准",
            json.dumps(calibration, ensure_ascii=False),
            "",
            "## 漂移检测",
            json.dumps(drift_log[-5:], ensure_ascii=False),
            "",
            "## 估值变迁 (0=极度低估 1=极度高估)",
            valuation_history,
        ]
        context = "\n".join(prompts)

        distill_prompt = f"""你是知识蒸馏引擎。基于以下历史数据, 生成新的知识库。

要求:
1. 归纳出 "历史上指向低估最可靠的信号" (按后续涨幅排序)
2. 归纳出 "历史上指向高估最可靠的信号" (按后续跌幅排序)
3. 列出 "常见误判模式" (如过早判断低估/高估, 估值调整滞后等)
4. 给出 "当前市场特殊结构" (与历史周期的差异, 如 ETF 改变供需结构等)
5. 建议 "估值档位阈值是否需要复核" (口径固定为 0-0.2/0.2-0.5/0.5-0.8/0.8-1, 只评估证据是否支持)
6. 根据「预测校准」归纳模型的过/欠置信区间与重跑离散问题
7. 更新 ## 知识库 (自动进化) 章节

格式: Markdown, 清晰简洁.

{context}
"""

        print(f"[API-CALL][distill] POST {self.analyzer.endpoint} purpose=knowledge "
              f"{datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}Z")
        try:
            max_tokens = int(self.distill_cfg.get("max_tokens")
                             or self.cfg.get("distill_max_tokens") or 4096)
        except (TypeError, ValueError):
            max_tokens = 4096
        max_tokens = max(512, min(16384, max_tokens))
        response = self.analyzer.client.chat(
            messages=[
                {"role": "system", "content": "你是知识蒸馏引擎, 输出 Markdown 格式的知识库内容."},
                {"role": "user", "content": distill_prompt},
            ],
            purpose="knowledge",
            project="AlphaEngine",
            temperature=0.2,
            max_tokens=max_tokens,
            json_mode=False,
            timeout_seconds=self.analyzer.timeout,
        )
        if not response.get("ok"):
            print(f"[Knowledge] Distill AI服务调用失败: {response.get('error')}")
            return None
        content = response.get("content") or ""
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        completion_tokens = usage.get("completion_tokens")
        # reasoning 模型会把预算吃在 reasoning 上, content 为空时 AIService 回退
        # reasoning_content (污染文本)。completion 恰好等于 max_tokens 是截断证据。
        if isinstance(completion_tokens, (int, float)) and completion_tokens >= max_tokens:
            print(f"[Knowledge] Distill 输出疑似被截断 "
                  f"(completion_tokens={completion_tokens} >= max_tokens={max_tokens}), "
                  f"保留旧知识库")
            return None
        ok, reason = validate_knowledge_text(content)
        if not ok:
            print(f"[Knowledge] Distill 输出结构无效, 保留旧知识库 ({reason}; "
                  f"{len(content)} chars)")
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

        if not getattr(self.analyzer, "enabled", True):
            return False

        compress_prompt = f"""压缩以下分析历史为关键转折点摘要。
保留: 估值档位变化时刻、估值大幅变动、关键信号出现/消失、错误判断修正。
丢弃: 重复的日常更新、无变化的分析。
输出: 简洁 Markdown (必须包含 ## 标题与 - 列表), 按时间倒序, 每条 1-2 行。

{existing[-5000:] if len(existing) > 5000 else existing}
"""

        print(f"[API-CALL][compress] POST {self.analyzer.endpoint} purpose=knowledge "
              f"{datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}Z")
        try:
            max_tokens = int(self.distill_cfg.get("memory_compress_max_tokens")
                             or self.distill_cfg.get("max_tokens") or 4096)
        except (TypeError, ValueError):
            max_tokens = 4096
        max_tokens = max(512, min(16384, max_tokens))
        response = self.analyzer.client.chat(
            messages=[
                {"role": "system", "content": "你是文本压缩引擎, 输出简洁 Markdown."},
                {"role": "user", "content": compress_prompt},
            ],
            purpose="knowledge",
            project="AlphaEngine",
            temperature=0.1,
            max_tokens=max_tokens,
            json_mode=False,
            timeout_seconds=self.analyzer.timeout,
        )
        if not response.get("ok"):
            print(f"[Knowledge] Compress AI服务调用失败: {response.get('error')}")
            return False
        compressed = response.get("content") or ""
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        completion_tokens = usage.get("completion_tokens")
        # 与 distill 同源风险: reasoning 吃满预算 → 回退 reasoning_content 污染 memory
        if isinstance(completion_tokens, (int, float)) and completion_tokens >= max_tokens:
            print(f"[Knowledge] Compress 输出疑似被截断 "
                  f"(completion_tokens={completion_tokens} >= max_tokens={max_tokens}), "
                  f"保留旧 memory")
            return False
        ok, reason = validate_memory_summary(compressed)
        if not ok:
            print(f"[Knowledge] Compress 输出无效, 保留旧 memory ({reason}; "
                  f"{len(compressed)} chars)")
            return False

        if compressed:
            ts = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M UTC")
            header = f"# Glassnode Alpha Engine Memory\n\n> 上次压缩: {ts}\n\n"
            backup = memory.backup_memory_md()
            if backup:
                print(f"[Knowledge] memory.md 已备份到 {backup}")
            memory.save_memory_md(header + compressed)
            print(f"[Knowledge] Memory compressed from {len(existing)} to {len(compressed)} chars")
            return True
        return False
