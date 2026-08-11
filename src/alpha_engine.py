"""
Alpha 引擎 — Regime 状态机 + 证据累积 + Alpha 平滑.

AI 判断 → regime 生成 → target_alpha 映射 → 平滑输出
"""
from datetime import datetime


# ---- Regime 定义 ----

REGIME_TRANSITIONS = {
    # regime → [possible_next_regimes]
    # 正向流程: BULL → DEEP_BULL → BULL_COOLING → BEAR → BEAR_DEEP → BEAR_BOTTOM → RECOVERY → BULL
    # 允许回退 (信号误判纠正)
    "INIT":         ["BEAR"],
    "DEEP_BULL":    ["DEEP_BULL", "BULL_COOLING", "BULL"],
    "BULL":         ["DEEP_BULL", "BULL", "BULL_COOLING"],
    "BULL_COOLING": ["DEEP_BULL", "BULL_COOLING", "BEAR"],
    "BEAR":         ["BULL_COOLING", "BEAR", "BEAR_DEEP"],
    "BEAR_DEEP":    ["BEAR", "BEAR_DEEP", "BEAR_BOTTOM"],
    "BEAR_BOTTOM":  ["BEAR_DEEP", "BEAR_BOTTOM", "RECOVERY"],
    "RECOVERY":     ["BEAR_DEEP", "RECOVERY", "BULL"],
}

REGIME_ALPHA_MAP = {
    # ===== 牛市侧 (alpha >= 0, 不为空) =====
    # BEAR_BOTTOM(0.00): 熊市底部确认 → 清仓做空, 准备翻多 (中性)
    # RECOVERY(+0.70):   恢复确认 → 建仓多单 (多次确认牛市中)
    # BULL(+1.00):       牛市确认 → 满仓做多
    # DEEP_BULL(+0.30):  接近牛顶 → 减仓多单, 暖待顶部

    "BEAR_BOTTOM":   0.00,
    "RECOVERY":      0.70,
    "BULL":          1.00,
    "DEEP_BULL":     0.30,

    # ===== 熊市侧 (alpha <= 0, 不为多) =====
    # BULL_COOLING(0.00): 牛顶确认 → 清仓多单, 准备做空 (中性)
    # BEAR(-1.00):        熊市确认 → 满仓做空 (多次确认熊市中)
    # BEAR_DEEP(-0.30):   深熊 → 减仓做空, 等底部确认

    "BULL_COOLING":  0.00,
    "BEAR":         -1.00,
    "BEAR_DEEP":    -0.30,
}

# 牛市侧 (alpha >= 0): 不为空
BULL_SIDE_REGIMES = ("BEAR_BOTTOM", "RECOVERY", "BULL", "DEEP_BULL")
# 熊市侧 (alpha <= 0): 不为多
BEAR_SIDE_REGIMES = ("BULL_COOLING", "BEAR", "BEAR_DEEP")

# 正向流程: 当前 regime → 下一位置 (用于周期内进度插值)
# BEAR_DEEP 临近熊底 → alpha 从 -0.3 向 0 靠拢, 而非先满仓空再翻多
FORWARD_NEXT_REGIME = {
    "INIT":         "BEAR",
    "DEEP_BULL":    "BULL_COOLING",
    "BULL":         "DEEP_BULL",
    "BULL_COOLING": "BEAR",
    "BEAR":         "BEAR_DEEP",
    "BEAR_DEEP":    "BEAR_BOTTOM",
    "BEAR_BOTTOM":  "RECOVERY",
    "RECOVERY":     "BULL",
}

# 中性确认位: 目标固定为基准 alpha (0), 不做向下一位置的插值.
# 熊底确认后应清仓等待 (=0), 牛市恢复 (RECOVERY) 确认后才建仓做多;
# 牛顶确认后应清仓等待 (=0), 熊市确认 (BEAR) 后才建仓做空.
NEUTRAL_REGIMES = ("INIT", "BEAR_BOTTOM", "BULL_COOLING")

EVIDENCE_CATEGORIES = ["profitability", "institutional", "onchain", "derivatives", "macro"]


class AlphaEngine:
    def __init__(self, cfg: dict, state_manager):
        self.cfg = cfg
        self.sm = state_manager
        self.smoothing = cfg.get("smoothing", {})
        self.evidence_cfg = cfg.get("evidence", {})
        self.conf_gate = cfg.get("confidence_gate", {})
        self.alpha_map = cfg.get("regime_alpha_map", REGIME_ALPHA_MAP)

    # ---- Regime ----

    def get_regime(self) -> str:
        return self.sm.get_regime()

    def _can_transition(self, current: str, target: str) -> bool:
        allowed = REGIME_TRANSITIONS.get(current, [])
        return target in allowed

    def request_regime_change(self, proposed_regime: str, evidence_scores: dict,
                              confidence: str, meta_quality: int = 8) -> (bool, str):
        """请求变更 regime。返回 (是否同意, 拒绝原因)."""
        current = self.get_regime()
        if not self._can_transition(current, proposed_regime):
            return False, f"非法转换: {current} → {proposed_regime}"

        cd = self.sm.get("regime.cooldown_remaining", 0)
        if cd > 0:
            return False, f"冷却期剩余 {cd} 轮"

        if self.conf_gate.get("low_confidence_blocks_regime_change", True) and confidence == "low":
            return False, "置信度为 low，拒绝 regime 变更"

        if self.evidence_cfg.get("require_quality", False) and meta_quality < 5:
            return False, f"分析质量太低 ({meta_quality})，拒绝 regime 变更"

        if not self._check_evidence_consensus(evidence_scores):
            return False, "证据共识不足"

        return True, ""

    def _check_evidence_consensus(self, evidence_scores: dict) -> bool:
        """检查是否有足够多的类别达成共识."""
        min_cats = self.evidence_cfg.get("min_categories_for_regime_change", 2)
        threshold = self.evidence_cfg.get("min_total_score_for_regime_change", 1.2)

        total = sum(abs(v) for v in evidence_scores.values())
        significant = sum(1 for v in evidence_scores.values() if abs(v) >= 0.3)

        return significant >= min_cats and total >= threshold

    def _side(self, regime: str) -> int:
        """周期方向侧: 牛市侧=+1, 熊市侧=-1, 未知=0."""
        if regime in BULL_SIDE_REGIMES:
            return 1
        if regime in BEAR_SIDE_REGIMES:
            return -1
        return 0

    def enforce_side_constraint(self) -> bool:
        """右侧纪律兜底: 仓位符号与周期方向侧冲突时, 直接定位到目标 (不先归零爬坡). 返回是否修正.

        熊市侧 (BULL_COOLING/BEAR/BEAR_DEEP) 不允许做多 (alpha>0),
        牛市侧 (BEAR_BOTTOM/RECOVERY/BULL/DEEP_BULL) 不允许做空 (alpha<0).
        例如深熊 (BEAR_DEEP) 却持有多单 → 直接平到当前目标 (如 -0.3 或按进度的 -0.18).
        中性确认位 (BEAR_BOTTOM/BULL_COOLING) 除外: 残余仓位不强平,
        由 step_alpha 逐步向 0 收敛 (确认制清仓过程).
        """
        alpha = self.get_alpha()
        regime = self.get_regime()
        if regime in NEUTRAL_REGIMES:
            return False
        if (regime in BEAR_SIDE_REGIMES and alpha > 0) or \
           (regime in BULL_SIDE_REGIMES and alpha < 0):
            progress = self.sm.get("alpha.regime_progress", 0.5)
            target = self.calculate_target_alpha(regime, progress)
            now = datetime.utcnow().isoformat() + "Z"
            self.sm.set("alpha.current", target)
            self.sm.set("alpha.target", target)
            self.sm.set("alpha.transition_progress", 1.0)
            self.sm.set("alpha.last_change_at", now)
            return True
        return False

    def execute_regime_change(self, new_regime: str, progress: float = 0.0) -> str:
        current = self.get_regime()
        now = datetime.utcnow().isoformat() + "Z"
        self.sm.set("regime.current", new_regime)
        self.sm.set("regime.entered_from", current)
        self.sm.set("regime.started_at", now)
        self.sm.set("regime.last_changed_at", now)
        self.sm.set("regime.stability_counter", 0)
        self.sm.set("regime.cooldown_remaining",
                    self.smoothing.get("cooldown_cycles_after_regime_change", 10))
        self.sm.set("alpha.locked", False)
        self.sm.set("alpha.lock_reason", "")
        self.sm.set("alpha.regime_progress", progress)
        target = self.calculate_target_alpha(new_regime, progress)

        # 右侧纪律: 跨越零线 (牛↔熊) 时先平仓, 再向新方向建仓;
        # 同向变更 (如 BEAR→BEAR_DEEP) 直接定位到目标, 避免从 0 爬坡
        if self._side(new_regime) != self._side(current):
            self.sm.set("alpha.current", 0.0)
            self.sm.set("alpha.target", target)
            self.sm.set("alpha.transition_progress", 0.0)
        else:
            self.sm.set("alpha.current", target)
            self.sm.set("alpha.target", target)
            self.sm.set("alpha.transition_progress", 1.0)
        self.sm.set("alpha.last_change_at", now)

        self.sm.save(force=True)
        return new_regime

    # ---- Alpha ----

    def get_alpha(self) -> float:
        return self.sm.get_alpha()

    def calculate_target_alpha(self, regime: str, progress: float = None) -> float:
        """计算目标 alpha.

        progress=None 时返回该 regime 的基准 alpha;
        progress 为周期内进度 (0~1) 时, 在当前位 alpha 与下一位置 alpha 之间线性插值:
        例如 BEAR_DEEP (基准 -0.3, 下一位置 BEAR_BOTTOM=0), progress=0.8 → -0.06,
        即深熊临近熊底时减空至接近中性, 而不是先做到 -0.3 再回头.
        中性确认位 (BEAR_BOTTOM/BULL_COOLING) 固定基准值, 不随进度插值:
        熊底/牛顶确认后清仓等待, 下一位置确认后才变仓.
        """
        base = float(self.alpha_map.get(regime, 0.0))
        if progress is None or regime in NEUTRAL_REGIMES:
            return base
        nxt = FORWARD_NEXT_REGIME.get(regime)
        if nxt is None:
            return base
        nxt_alpha = float(self.alpha_map.get(nxt, base))
        p = max(0.0, min(1.0, float(progress)))
        return round(base + (nxt_alpha - base) * p, 4)

    def step_alpha(self) -> (float, bool):
        """将 alpha 向目标平滑推进一步。返回 (新alpha, 是否变化)."""
        current = self.get_alpha()
        progress = self.sm.get("alpha.regime_progress", 0.5)
        target = self.calculate_target_alpha(self.get_regime(), progress)
        max_step = self.smoothing.get("max_change_per_step", 0.10)

        if abs(current - target) < 0.005:
            self.sm.set("alpha.target", target)
            self.sm.set("alpha.transition_progress", 1.0)
            return current, False

        diff = target - current
        step = max(-max_step, min(max_step, diff))
        new_alpha = round(current + step, 4)

        now = datetime.utcnow().isoformat() + "Z"
        self.sm.set("alpha.current", new_alpha)
        self.sm.set("alpha.target", target)
        self.sm.set("alpha.last_change_at", now)
        self.sm.set("alpha.transition_progress", abs((new_alpha - current) / diff) if diff != 0 else 1.0)

        return new_alpha, True

    def tick_cooldown(self):
        cd = self.sm.get("regime.cooldown_remaining", 0)
        if cd > 0:
            self.sm.set("regime.cooldown_remaining", cd - 1)

    def tick_stability(self):
        sc = self.sm.get("regime.stability_counter", 0)
        self.sm.set("regime.stability_counter", sc + 1)


class EvidenceAccumulator:
    def __init__(self, state_manager, decay_per_cycle: float = 0.02):
        self.sm = state_manager
        self.decay = decay_per_cycle

    def update(self, regime: str, category: str, score: float):
        """累积某 regime 在某类别上的证据分."""
        by_regime = self.sm.get("evidence.accumulators", {})
        current = by_regime.get(regime, 0.0)
        by_regime[regime] = current + score
        self.sm.set("evidence.accumulators", by_regime)

        by_cat = self.sm.get("evidence.by_category", {})
        cat_entry = by_cat.setdefault(category, {})
        cat_entry[regime] = cat_entry.get(regime, 0.0) + score
        self.sm.set("evidence.by_category", by_cat)

    def decay_all(self):
        by_regime = self.sm.get("evidence.accumulators", {})
        for regime in list(by_regime.keys()):
            val = by_regime[regime]
            if val > 0:
                by_regime[regime] = max(0.0, val - self.decay)
            elif val < 0:
                by_regime[regime] = min(0.0, val + self.decay)
        self.sm.set("evidence.accumulators", by_regime)

        by_cat = self.sm.get("evidence.by_category", {})
        for cat in list(by_cat.keys()):
            for regime in list(by_cat[cat].keys()):
                val = by_cat[cat][regime]
                if val > 0:
                    by_cat[cat][regime] = max(0.0, val - self.decay)
                elif val < 0:
                    by_cat[cat][regime] = min(0.0, val + self.decay)
        self.sm.set("evidence.by_category", by_cat)

    def reset(self):
        """清空全部证据累加器 (强制回溯时使用)."""
        self.sm.set("evidence.accumulators", {})
        self.sm.set("evidence.by_category", {})

    def get_top_regime(self, min_score: float = 1.2) -> str | None:
        by_regime = self.sm.get("evidence.accumulators", {})
        best_regime, best_score = None, 0.0
        for regime, score in by_regime.items():
            if score > best_score and score >= min_score:
                best_score = score
                best_regime = regime
        return best_regime
