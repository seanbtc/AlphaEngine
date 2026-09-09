"""月度复盘引擎 — 评估预测准确率 + AI 自动校准参数."""
import json
import os
from datetime import datetime, timedelta

import requests


class ReviewEngine:
    def __init__(self, cfg: dict, data_dir: str, state_manager, alpha_engine, knowledge):
        self.data_dir = data_dir
        self.sm = state_manager
        self.engine = alpha_engine
        self.knowledge = knowledge
        self.price_file = os.path.join(data_dir, "price_history.jsonl")
        self.review_file = os.path.join(data_dir, "review_log.jsonl")
        self.review_day = cfg.get("schedule_day", 1)
        self.min_cycles = cfg.get("min_cycles_before_review", 20)

    def record_price(self, price: float, regime: str, alpha: float):
        entry = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "btc_price": price,
            "regime": regime,
            "alpha": alpha,
        }
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.price_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def should_review(self) -> bool:
        now = datetime.utcnow()
        # 必须是每月最后一天
        from calendar import monthrange
        last_day = monthrange(now.year, now.month)[1]
        if now.day != last_day:
            return False
        # 晚上 20:00 之后
        if now.hour < 20:
            return False
        # 检查是否已在本月复盘过
        last = self.sm.get("runtime.last_review_at", "")
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                if last_dt.year == now.year and last_dt.month == now.month:
                    return False  # 本月已复盘
            except ValueError:
                pass
        # 最低分析次数门槛
        count = self.sm.get("runtime.analysis_count", 0)
        return count >= self.min_cycles

    def run_review(self) -> dict | None:
        print("\n" + "=" * 60)
        print("[Review] 月度复盘开始")
        print("=" * 60)

        prices = self._load_recent_prices(days=30)
        if len(prices) < 5:
            print("[Review] 价格数据不足 (< 5 条), 跳过复盘")
            return None

        regime_stats = self._analyze_regime_accuracy(prices)
        alpha_stats = self._analyze_alpha_performance(prices)
        calibration = self._suggest_calibration(regime_stats, alpha_stats)

        review_result = {
            "ts": datetime.utcnow().isoformat() + "Z",
            "period_days": 30,
            "price_count": len(prices),
            "price_change_pct": self._price_change(prices),
            "regime_accuracy": regime_stats,
            "alpha_performance": alpha_stats,
            "calibration": calibration,
        }

        self._save_review(review_result)
        self._print_review(review_result)

        self.sm.set("runtime.last_review_at", datetime.utcnow().isoformat() + "Z")
        self.sm.save()

        return review_result

    def _load_recent_prices(self, days: int = 30) -> list[dict]:
        if not os.path.exists(self.price_file):
            return []
        cutoff = datetime.utcnow() - timedelta(days=days)
        results = []
        with open(self.price_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = datetime.fromisoformat(entry["ts"].replace("Z", "+00:00"))
                    if ts.replace(tzinfo=None) >= cutoff:
                        results.append(entry)
                except (json.JSONDecodeError, ValueError, KeyError):
                    continue
        return results

    def _price_change(self, prices: list[dict]) -> float:
        if len(prices) < 2:
            return 0.0
        first = prices[0]["btc_price"]
        last = prices[-1]["btc_price"]
        return round((last - first) / first * 100, 2)

    def _analyze_regime_accuracy(self, prices: list[dict]) -> dict:
        """评估 regime 判断准确性.

        分类:
        - 牛市侧 (alpha>0): RECOVERY, BULL → 期望价格上涨
        - 熊市侧 (alpha<0): BEAR, BEAR_DEEP → 期望价格下跌
        - 中性 (alpha=0): BEAR_BOTTOM, BULL_COOLING → 期望价格稳定
        """
        from src.alpha_engine import REGIME_ALPHA_MAP

        bull_regimes = set()
        bear_regimes = set()
        neutral_regimes = set()
        for r, a in REGIME_ALPHA_MAP.items():
            if a > 0:
                bull_regimes.add(r)
            elif a < 0:
                bear_regimes.add(r)
            else:
                neutral_regimes.add(r)

        regime_periods = []
        current_regime = None
        start_price = None
        start_ts = None

        for p in prices:
            regime = p.get("regime", "UNKNOWN")
            if regime != current_regime:
                if current_regime is not None:
                    regime_periods.append({
                        "regime": current_regime,
                        "start_price": start_price,
                        "end_price": p["btc_price"],
                        "start_ts": start_ts,
                        "end_ts": p["ts"],
                    })
                current_regime = regime
                start_price = p["btc_price"]
                start_ts = p["ts"]

        if current_regime is not None:
            regime_periods.append({
                "regime": current_regime,
                "start_price": start_price,
                "end_price": prices[-1]["btc_price"],
                "start_ts": start_ts,
                "end_ts": prices[-1]["ts"],
            })

        correct = 0
        wrong = 0
        neutral_count = 0
        details = []
        for period in regime_periods:
            regime = period["regime"]
            change = (period["end_price"] - period["start_price"]) / period["start_price"]

            if regime in bull_regimes:
                if change > 0.02:
                    correct += 1
                    verdict = "correct"
                elif change < -0.05:
                    wrong += 1
                    verdict = "wrong"
                else:
                    neutral_count += 1
                    verdict = "neutral"
            elif regime in bear_regimes:
                if change < -0.02:
                    correct += 1
                    verdict = "correct"
                elif change > 0.05:
                    wrong += 1
                    verdict = "wrong"
                else:
                    neutral_count += 1
                    verdict = "neutral"
            elif regime in neutral_regimes:
                if abs(change) < 0.03:
                    correct += 1
                    verdict = "correct"
                else:
                    neutral_count += 1
                    verdict = "neutral"
            else:
                neutral_count += 1
                verdict = "unknown"

            details.append({
                "regime": regime,
                "price_change_pct": round(change * 100, 2),
                "verdict": verdict,
            })

        total_evaluated = correct + wrong
        accuracy = round(correct / total_evaluated, 2) if total_evaluated > 0 else 0.0
        return {
            "accuracy": accuracy,
            "total_periods": len(regime_periods),
            "correct": correct,
            "wrong": wrong,
            "neutral": neutral_count,
            "details": details,
        }

    def _analyze_alpha_performance(self, prices: list[dict]) -> dict:
        if len(prices) < 2:
            return {"correlation": 0.0, "note": "数据不足"}

        n = len(prices)
        alphas = [p.get("alpha", 0.0) for p in prices]
        price_changes = []
        for i in range(1, n):
            change = (prices[i]["btc_price"] - prices[i-1]["btc_price"]) / prices[i-1]["btc_price"]
            price_changes.append(change)

        alphas_aligned = alphas[1:]
        if len(alphas_aligned) < 2:
            return {"correlation": 0.0, "note": "数据不足"}

        mean_a = sum(alphas_aligned) / len(alphas_aligned)
        mean_p = sum(price_changes) / len(price_changes)

        cov = sum((a - mean_a) * (p - mean_p) for a, p in zip(alphas_aligned, price_changes))
        std_a = sum((a - mean_a) ** 2 for a in alphas_aligned) ** 0.5
        std_p = sum((p - mean_p) ** 2 for p in price_changes) ** 0.5

        if std_a == 0 or std_p == 0:
            correlation = 0.0
        else:
            correlation = round(cov / (std_a * std_p), 3)

        total_alpha = sum(alphas_aligned) / len(alphas_aligned)
        total_return = sum(price_changes)
        direction_match = (total_alpha > 0 and total_return > 0) or (total_alpha < 0 and total_return < 0)

        return {
            "correlation": correlation,
            "avg_alpha": round(total_alpha, 3),
            "total_price_return_pct": round(total_return * 100, 2),
            "direction_match": direction_match,
        }

    def _suggest_calibration(self, regime_stats: dict, alpha_stats: dict) -> dict:
        """使用 AI 评估所有参数并给出校准建议."""
        return self._ai_calibration(regime_stats, alpha_stats)

    def _ai_calibration(self, regime_stats: dict, alpha_stats: dict) -> dict:
        """调用 DeepSeek 分析复盘数据, 输出参数调整建议."""
        from src.alpha_engine import REGIME_EXPECTED_DAYS

        analyzer = self.knowledge.analyzer
        if not getattr(analyzer, "api_key", None):
            return self._fallback_calibration(regime_stats, alpha_stats)

        # 读取上次复盘记录 (用于对比调整效果)
        previous_adjustments = self._load_last_adjustments()

        current_params = {
            "smoothing": {
                "max_change_per_step": self.engine.smoothing.get("max_change_per_step", 0.05),
                "min_daily_step": self.engine.smoothing.get("min_daily_step", 0.015),
                "cooldown_cycles_after_regime_change": self.engine.smoothing.get("cooldown_cycles_after_regime_change", 10),
            },
            "evidence": {
                "min_categories_for_regime_change": self.engine.evidence_cfg.get("min_categories_for_regime_change", 3),
                "min_total_score_for_regime_change": self.engine.evidence_cfg.get("min_total_score_for_regime_change", 2.0),
                "high_conf_min_categories": self.engine.evidence_cfg.get("high_conf_min_categories", 2),
                "high_conf_min_total": self.engine.evidence_cfg.get("high_conf_min_total", 1.5),
                "decay_per_cycle": self.engine.evidence_cfg.get("decay_per_cycle", 0.01),
            },
            "confidence_gate": {
                "low_confidence_blocks_regime_change": self.engine.conf_gate.get("low_confidence_blocks_regime_change", True),
                "low_confidence_max_alpha_abs": self.engine.conf_gate.get("low_confidence_max_alpha_abs", 0.3),
            },
            "regime_expected_days": REGIME_EXPECTED_DAYS,
            "regime_alpha_map": self.engine.alpha_map,
        }

        review_data = {
            "regime_accuracy": regime_stats,
            "alpha_performance": alpha_stats,
            "previous_adjustments": previous_adjustments,
        }

        prompt = f"""你是 Alpha 引擎的参数校准专家。基于以下复盘数据和当前参数, 分析哪些参数需要调整以提升预测准确性。

## 复盘数据 (过去30天)
{json.dumps(review_data, ensure_ascii=False, indent=2)}

## 当前参数
{json.dumps(current_params, ensure_ascii=False, indent=2)}

## 可调参数说明与边界
1. **smoothing.max_change_per_step**: 每日最大 alpha 变化, 范围 [0.01, 0.10], 控制仓位调整速度
2. **smoothing.min_daily_step**: 每日最小 alpha 变化, 范围 [0.005, 0.05], 确保无推文时也推进
3. **smoothing.cooldown_cycles_after_regime_change**: regime 变更后的冷却轮数, 范围 [3, 30]
4. **evidence.min_categories_for_regime_change**: 触发 regime 变更所需最少证据类别数, 范围 [1, 5]
5. **evidence.min_total_score_for_regime_change**: 触发 regime 变更所需证据总分, 范围 [0.5, 3.0]
6. **evidence.high_conf_min_categories**: 高置信时最少类别数, 范围 [1, 3]
7. **evidence.high_conf_min_total**: 高置信时最少总分, 范围 [0.5, 2.5]
8. **evidence.decay_per_cycle**: 每轮证据衰减量, 范围 [0.005, 0.05]
9. **confidence_gate.low_confidence_blocks_regime_change**: 低置信时是否阻止 regime 变更, 布尔
10. **confidence_gate.low_confidence_max_alpha_abs**: 低置信时最大仓位绝对值, 范围 [0.1, 0.5]
11. **regime_expected_days.REGIME_NAME**: 各 regime 预期持续天数, 范围 [30, 500]
12. **regime_alpha_map.REGIME_NAME**: 各 regime 目标 alpha, 范围 [-1.0, 1.0]

## 分析要求
1. 如果 regime 准确率低 (< 50%): 考虑调整 evidence 阈值或 regime_expected_days
2. 如果 alpha 与价格负相关: 考虑减小 max_change_per_step 或调整 regime_alpha_map
3. 如果 alpha 与价格正相关弱 (< 0.3): 考虑增大 max_change_per_step
4. 如果 regime 切换太频繁: 增大 cooldown 或提高 evidence 阈值
5. 如果 regime 切换太少: 减小 cooldown 或降低 evidence 阈值
6. 如果上次调整方向正确但效果不足, 可继续同方向调整
7. 每次最多调整 3-4 个参数, 避免过度调整
8. 所有数值必须在上述范围内

## 输出格式 (纯 JSON, 不要 markdown)
{{
  "analysis": "简要分析当前问题 (100字内)",
  "adjustments": [
    {{"param": "smoothing.max_change_per_step", "old": 0.05, "new": 0.06, "reason": "正相关良好, 可加速建仓"}}
  ],
  "warnings": ["警告信息 (如有)"]
}}

如果无需调整, adjustments 为空数组。"""

        payload = {
            "model": analyzer.model,
            "messages": [
                {"role": "system", "content": "你是参数量化校准专家, 只输出纯 JSON, 不要 markdown 代码块."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 1536,
        }
        headers = {
            "Authorization": f"Bearer {analyzer.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{analyzer.base_url}/chat/completions"

        try:
            print(f"[API-CALL][review-calibrate] POST {analyzer.model} {datetime.utcnow().isoformat()}Z")
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            content = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
            if not content:
                return self._fallback_calibration(regime_stats, alpha_stats)

            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content[3:]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()

            result = json.loads(content)
            if "adjustments" not in result:
                result["adjustments"] = []
            if "warnings" not in result:
                result["warnings"] = []
            return result

        except json.JSONDecodeError as e:
            print(f"[Review] AI 返回 JSON 解析失败: {e}")
            return self._fallback_calibration(regime_stats, alpha_stats)
        except Exception as e:
            print(f"[Review] AI 校准失败: {e}, 使用规则兜底")
            return self._fallback_calibration(regime_stats, alpha_stats)

    def _fallback_calibration(self, regime_stats: dict, alpha_stats: dict) -> dict:
        """规则兜底 (AI 不可用时)."""
        calibration = {"adjustments": [], "warnings": [], "analysis": "规则兜底模式"}
        accuracy = regime_stats.get("accuracy", 0.0)
        correlation = alpha_stats.get("correlation", 0.0)

        if accuracy < 0.4:
            calibration["warnings"].append("准确率低于 40%, 建议检查 regime 判断逻辑")
            current = self.engine.evidence_cfg.get("min_total_score_for_regime_change", 2.0)
            new_val = max(0.5, current - 0.3)
            calibration["adjustments"].append({
                "param": "evidence.min_total_score_for_regime_change",
                "old": current,
                "new": round(new_val, 2),
                "reason": "准确率过低, 降低阈值让 regime 更灵活",
            })

        if correlation < -0.2:
            calibration["warnings"].append("alpha 与价格负相关, 可能存在方向错误")
            current = self.engine.smoothing.get("max_change_per_step", 0.05)
            new_val = max(0.01, current - 0.02)
            calibration["adjustments"].append({
                "param": "smoothing.max_change_per_step",
                "old": current,
                "new": round(new_val, 3),
                "reason": "负相关, 减小步长降低错误仓位速度",
            })

        if accuracy > 0.7 and correlation > 0.5:
            current = self.engine.smoothing.get("max_change_per_step", 0.05)
            new_val = min(0.10, current + 0.01)
            calibration["adjustments"].append({
                "param": "smoothing.max_change_per_step",
                "old": current,
                "new": round(new_val, 3),
                "reason": "准确率高且正相关, 可适当加速建仓",
            })

        if not calibration["adjustments"]:
            calibration["adjustments"].append({
                "param": "none",
                "reason": "当前参数表现正常, 无需调整",
            })

        return calibration

    def _load_last_adjustments(self) -> list:
        """读取上次复盘的调整记录."""
        if not os.path.exists(self.review_file):
            return []
        try:
            with open(self.review_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if not lines:
                return []
            last = json.loads(lines[-1].strip())
            return last.get("calibration", {}).get("adjustments", [])
        except (json.JSONDecodeError, IOError):
            return []

    def apply_calibration(self, review_result: dict):
        """应用 AI 给出的参数调整 (带安全边界)."""
        from src.alpha_engine import REGIME_EXPECTED_DAYS

        calibration = review_result.get("calibration", {})
        adjustments = calibration.get("adjustments", [])
        applied = []
        errors = []

        # 参数安全边界
        bounds = {
            "smoothing.max_change_per_step": (0.01, 0.10),
            "smoothing.min_daily_step": (0.005, 0.05),
            "smoothing.cooldown_cycles_after_regime_change": (3, 30),
            "evidence.min_categories_for_regime_change": (1, 5),
            "evidence.min_total_score_for_regime_change": (0.5, 3.0),
            "evidence.high_conf_min_categories": (1, 3),
            "evidence.high_conf_min_total": (0.5, 2.5),
            "evidence.decay_per_cycle": (0.005, 0.05),
            "confidence_gate.low_confidence_max_alpha_abs": (0.1, 0.5),
        }

        def clamp(param, value):
            if param in bounds:
                lo, hi = bounds[param]
                return max(lo, min(hi, value))
            if param.startswith("regime_expected_days."):
                return max(30, min(500, int(value)))
            if param.startswith("regime_alpha_map."):
                return max(-1.0, min(1.0, float(value)))
            return value

        for adj in adjustments:
            param = adj.get("param", "")
            new_val = adj.get("new")
            old_val = adj.get("old")

            if param == "none" or new_val is None:
                continue

            try:
                if param == "smoothing.max_change_per_step":
                    self.engine.smoothing["max_change_per_step"] = clamp(param, float(new_val))
                elif param == "smoothing.min_daily_step":
                    self.engine.smoothing["min_daily_step"] = clamp(param, float(new_val))
                elif param == "smoothing.cooldown_cycles_after_regime_change":
                    self.engine.smoothing["cooldown_cycles_after_regime_change"] = clamp(param, int(new_val))
                elif param == "evidence.min_categories_for_regime_change":
                    self.engine.evidence_cfg["min_categories_for_regime_change"] = clamp(param, int(new_val))
                elif param == "evidence.min_total_score_for_regime_change":
                    self.engine.evidence_cfg["min_total_score_for_regime_change"] = clamp(param, float(new_val))
                elif param == "evidence.high_conf_min_categories":
                    self.engine.evidence_cfg["high_conf_min_categories"] = clamp(param, int(new_val))
                elif param == "evidence.high_conf_min_total":
                    self.engine.evidence_cfg["high_conf_min_total"] = clamp(param, float(new_val))
                elif param == "evidence.decay_per_cycle":
                    val = clamp(param, float(new_val))
                    self.engine.evidence_cfg["decay_per_cycle"] = val
                    self.knowledge.drift_cfg["decay_per_cycle"] = val
                    self.engine.evidence.decay = val
                elif param == "confidence_gate.low_confidence_blocks_regime_change":
                    self.engine.conf_gate["low_confidence_blocks_regime_change"] = bool(new_val)
                elif param == "confidence_gate.low_confidence_max_alpha_abs":
                    self.engine.conf_gate["low_confidence_max_alpha_abs"] = clamp(param, float(new_val))
                elif param.startswith("regime_expected_days."):
                    regime_key = param.split(".", 1)[1]
                    REGIME_EXPECTED_DAYS[regime_key] = clamp(param, int(new_val))
                elif param.startswith("regime_alpha_map."):
                    regime_key = param.split(".", 1)[1]
                    self.engine.alpha_map[regime_key] = clamp(param, float(new_val))
                else:
                    errors.append(f"未知参数: {param}")
                    continue

                final_val = adj.get("new")
                if param in bounds or param.startswith("regime_"):
                    final_val = clamp(param, float(new_val) if "." in str(new_val) else new_val)
                applied.append(f"{param}: {old_val} → {final_val}")

            except (ValueError, TypeError) as e:
                errors.append(f"{param}: {e}")

        if applied:
            print("[Review] AI 校准已应用:")
            for a in applied:
                print(f"  ✓ {a}")
        else:
            print("[Review] 无需校准")

        if errors:
            print("[Review] 应用中的问题:")
            for e in errors:
                print(f"  ✗ {e}")

        return applied

    def _save_review(self, result: dict):
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.review_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False) + "\n")

    def _print_review(self, result: dict):
        print(f"\n[Review] 复盘结果:")
        print(f"  期间价格变化: {result['price_change_pct']:+.2f}%")
        print(f"  价格数据点: {result['price_count']}")

        regime = result.get("regime_accuracy", {})
        total_eval = regime.get("correct", 0) + regime.get("wrong", 0)
        print(f"\n  Regime 准确率: {regime.get('accuracy', 0):.0%} "
              f"(正确={regime.get('correct', 0)}, 错误={regime.get('wrong', 0)}, "
              f"中性={regime.get('neutral', 0)})")
        for d in regime.get("details", []):
            print(f"    {d['regime']}: 价格变化 {d['price_change_pct']:+.2f}% → {d['verdict']}")

        alpha = result.get("alpha_performance", {})
        print(f"\n  Alpha 表现:")
        print(f"    与价格相关性: {alpha.get('correlation', 0)}")
        print(f"    平均 alpha: {alpha.get('avg_alpha', 0)}")
        print(f"    价格总回报: {alpha.get('total_price_return_pct', 0):+.2f}%")
        print(f"    方向一致: {'是' if alpha.get('direction_match') else '否'}")

        cal = result.get("calibration", {})
        if cal.get("analysis"):
            print(f"\n  AI 分析: {cal['analysis']}")
        if cal.get("warnings"):
            print(f"\n  ⚠ 警告:")
            for w in cal["warnings"]:
                print(f"    - {w}")
        if cal.get("adjustments"):
            print(f"\n  建议调整:")
            for a in cal["adjustments"]:
                if a.get("param") != "none":
                    print(f"    {a['param']}: {a.get('old')} → {a.get('new')} ({a['reason']})")

        print("=" * 60)
