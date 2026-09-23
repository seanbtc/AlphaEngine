"""
Alpha 引擎 — Regime 状态机 + 证据累积 + Alpha 平滑.

AI 判断 → regime 生成 → target_alpha 映射 → 平滑输出

时间语义 (WP6):
- progress/alpha 按**自然日**推进: 每轮按距上次推进的 (now - runtime.last_tick_at)
  折算天数 (clamp [0, max_catchup_days]), 不再按"运行轮次"计数 —— 调度为
  3 次/周时, 周期钟与实际日历一致 (旧逻辑 3 次/周 → 慢 7/3≈2.33×)。
- tick_cooldown / tick_stability 仍保留**轮次**语义 (决策机会数): 冷却期与
  连续确认制配套, 按"有新推文且分析成功"的轮次计数, 与自然日推进相互独立。
"""
from datetime import datetime, timedelta, timezone


# ---- Regime 定义 ----

# BTC 4年周期 (~1461天) 各阶段预期持续时间 (天)
# 基于历史数据: 牛市~1年, 熊市~1年, 底部/顶部确认~3-6个月;
# 各值保持原相对比例等比放大 (1270 → 1461, ≈×1.15) 后取整, 合计=1461天=4年。
# 用途: progress 推进速率 = days_elapsed / expected_days; 动态步长同口径。
REGIME_EXPECTED_DAYS = {
    "BEAR_BOTTOM": 207,   # 熊底确认: ~6.8个月 (积累区)
    "RECOVERY": 103,      # 恢复确认: ~3.4个月 (牛市初期)
    "BULL": 420,          # 牛市确认: ~1.15年 (主升浪)
    "DEEP_BULL": 104,     # 深牛/近顶: ~3.4个月 (牛市后期)
    "BULL_COOLING": 103,  # 牛顶确认: ~3.4个月 (派发区)
    "BEAR": 420,          # 熊市确认: ~1.15年 (主跌浪)
    "BEAR_DEEP": 104,     # 深熊: ~3.4个月 (熊市后期)
}

REGIME_TRANSITIONS = {
    # regime → [possible_next_regimes]
    # 严格逐步推进 (相邻 ±1): 每次只允许前进一步、回退一步或保持;
    # 正向流程 = FORWARD_NEXT_REGIME (BULL → DEEP_BULL → BULL_COOLING → BEAR →
    # BEAR_DEEP → BEAR_BOTTOM → RECOVERY → BULL), 回退仅限上一步.
    # INIT 为特例: 仅允许进入 BEAR (无保持/回退).
    "INIT":         ["BEAR"],
    "DEEP_BULL":    ["DEEP_BULL", "BULL_COOLING", "BULL"],
    "BULL":         ["RECOVERY", "BULL", "DEEP_BULL"],
    "BULL_COOLING": ["DEEP_BULL", "BULL_COOLING", "BEAR"],
    "BEAR":         ["BULL_COOLING", "BEAR", "BEAR_DEEP"],
    "BEAR_DEEP":    ["BEAR", "BEAR_DEEP", "BEAR_BOTTOM"],
    "BEAR_BOTTOM":  ["BEAR_DEEP", "BEAR_BOTTOM", "RECOVERY"],
    "RECOVERY":     ["BEAR_BOTTOM", "RECOVERY", "BULL"],
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

# 中性确认位: 仓位冻结观望, 不做插值/不强制清仓/不随进度收敛.
# 熊底确认后保持现状等待 (RECOVERY 确认 → 翻多; 信号恶化 → 回深熊加空);
# 牛顶确认后保持现状等待 (BEAR 确认 → 翻空; 信号增强 → 回 DEEP_BULL).
NEUTRAL_REGIMES = ("INIT", "BEAR_BOTTOM", "BULL_COOLING")

EVIDENCE_CATEGORIES = ["profitability", "institutional", "onchain", "derivatives", "macro"]


class AlphaEngine:
    def __init__(self, cfg: dict, state_manager):
        self.cfg = cfg
        self.sm = state_manager
        self.smoothing = cfg.get("smoothing", {})
        self.evidence_cfg = cfg.get("evidence", {})
        self.conf_gate = cfg.get("confidence_gate", {})
        self.stability_cfg = cfg.get("stability", {})
        self.time_cfg = cfg.get("time_semantics", {}) or {}
        self.alpha_map = cfg.get("regime_alpha_map", REGIME_ALPHA_MAP)
        # 各 regime 预期天数: 代码锚表为基准, config.regime_expected_days 可覆盖
        # (校准落盘 overlay 亦写入本字典, 重启后由 params_store 恢复)
        # config 值 clamp [1, 500] (与校准上界一致, 防止 0/负值除零或超大值)
        self.expected_days = dict(REGIME_EXPECTED_DAYS)
        override = cfg.get("regime_expected_days")
        if isinstance(override, dict):
            for key, value in override.items():
                if key in self.expected_days:
                    try:
                        self.expected_days[key] = max(1, min(500, int(value)))
                    except (TypeError, ValueError):
                        continue

    # ---- 时间语义 (自然日推进) ----

    def max_catchup_days(self) -> float:
        """单轮最多折算的自然日数 (alpha.time_semantics.max_catchup_days, 默认 7).

        非法值回退 7; 0=不追赶 (冻结时间推进)。
        """
        try:
            value = float(self.time_cfg.get("max_catchup_days", 7))
        except (TypeError, ValueError):
            return 7.0
        if value != value:  # NaN
            return 7.0
        return max(0.0, value)

    @staticmethod
    def _parse_utc(iso: str):
        """解析 UTC iso 时间串; 非法/空返回 None."""
        if not iso:
            return None
        try:
            dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    def consume_elapsed_days(self, now: datetime = None) -> float:
        """计算并记录距上次推进的自然日数 (clamp [0, max_catchup_days]).

        - 旧 state 无 runtime.last_tick_at: 首轮仅初始化锚点, 返回 0 (不推进)
        - 同秒/同日重复运行: elapsed≈0 → 不重复推进 (幂等)
        - 时间倒流: 小偏差不推进且不回退锚点; 回拨超过 max_catchup_days 视为
          锚点不可信 → 重置为 now (不记负时长, 后续从 now 正常推进)
        - 超过上限的停摆天数直接丢弃 (不补记)
        """
        now = now or datetime.utcnow()
        last = self._parse_utc(self.sm.get("runtime.last_tick_at", ""))
        if last is None:
            self.sm.set("runtime.last_tick_at", now.isoformat() + "Z")
            return 0.0
        elapsed = (now - last).total_seconds() / 86400.0
        if elapsed < -self.max_catchup_days():
            # 大时钟回拨 (如 NTP 校时/系统时间错误): 重置锚点, 不记负时长
            self.sm.set("runtime.last_tick_at", now.isoformat() + "Z")
            return 0.0
        if elapsed <= 0:
            return 0.0
        self.sm.set("runtime.last_tick_at", now.isoformat() + "Z")
        return min(elapsed, self.max_catchup_days())

    def _dynamic_step(self, regime: str, days_elapsed: float) -> float:
        """动态步长 = clamp(days_elapsed / expected_days, min_daily_step, max_change_per_step).

        min/max 为"单轮"边界; 按自然日折算后短周期 regime 的动态值可超过下限
        (如 RECOVERY 2.33天 → ≈0.0226 > 0.015), 不再恒为下限。
        """
        expected_days = self.expected_days.get(regime, 180)
        min_step = self.smoothing.get("min_daily_step", 0.02)
        max_step = self.smoothing.get("max_change_per_step", 0.10)
        return max(min_step, min(days_elapsed / expected_days, max_step))

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

        if self.evidence_cfg.get("require_quality", False):
            min_quality = self.min_quality_for_regime_change()
            if meta_quality < min_quality:
                return False, (f"分析质量太低 (quality={meta_quality} < "
                               f"min_quality_for_regime_change={min_quality})，"
                               f"拒绝 regime 变更")

        if not self._check_evidence_consensus(evidence_scores, confidence):
            return False, "证据共识不足"

        return True, ""

    def min_quality_for_regime_change(self) -> int:
        """质量门阈值 (alpha.evidence.min_quality_for_regime_change, 默认 5)."""
        try:
            value = int(self.evidence_cfg.get("min_quality_for_regime_change", 5))
        except (TypeError, ValueError):
            value = 5
        return max(0, value)

    # ---- 连续同向确认 (pending proposal) ----

    def required_confirmations(self) -> int:
        """连续同向确认次数 (alpha.stability.required_confirmations, 默认 2; 1=关闭)."""
        try:
            value = int(self.stability_cfg.get("required_confirmations", 2))
        except (TypeError, ValueError):
            value = 2
        return max(1, value)

    def get_pending_proposal(self) -> dict:
        pending = self.sm.get("regime.pending_proposal", {})
        return pending if isinstance(pending, dict) else {}

    @staticmethod
    def pending_count(pending: dict) -> int:
        """pending 计数安全读取: 非法值归零, 不因脏数据中断轮次."""
        try:
            return int((pending or {}).get("count", 0) or 0)
        except (TypeError, ValueError):
            return 0

    def note_regime_proposal(self, proposed_regime: str) -> dict:
        """记录一次同向提议 (仅"有新推文且分析成功"的轮次调用), 返回更新后的 pending.

        同一 cp 连续出现 → count+1; 换 cp → 重置为 1; 提议回到当前 regime → 清除。
        idle 轮次不调用本方法 (不计数不重置)。
        """
        current = self.get_regime()
        if proposed_regime == current:
            self.clear_pending_proposal()
            return {}
        now = datetime.utcnow().isoformat() + "Z"
        pending = self.get_pending_proposal()
        if pending.get("cp") == proposed_regime:
            pending["count"] = self.pending_count(pending) + 1
            pending["last_at"] = now
        else:
            pending = {"cp": proposed_regime, "count": 1,
                       "first_at": now, "last_at": now}
        self.sm.set("regime.pending_proposal", pending)
        return pending

    def clear_pending_proposal(self):
        self.sm.set("regime.pending_proposal", {})

    def proposal_ready(self, pending: dict = None) -> bool:
        """pending 计数是否达到 required_confirmations (可执行 request_regime_change)."""
        pending = pending if pending is not None else self.get_pending_proposal()
        return self.pending_count(pending) >= self.required_confirmations()

    def _check_evidence_consensus(self, evidence_scores: dict, confidence: str = "medium") -> bool:
        """检查是否有足够多的类别达成共识.

        按置信度分级门槛: high 置信放宽 (AI 已高度确认, 避免被总分卡住),
        medium 用标准门槛, low 走低置信拒绝路径不会到这里.
        """
        min_cats = self.evidence_cfg.get("min_categories_for_regime_change", 2)
        threshold = self.evidence_cfg.get("min_total_score_for_regime_change", 1.2)
        if confidence == "high":
            min_cats = self.evidence_cfg.get("high_conf_min_categories", 2)
            threshold = self.evidence_cfg.get("high_conf_min_total", 1.5)

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
        self.clear_pending_proposal()
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
        self.sm.set("alpha.deferred_build", False)
        target = self.calculate_target_alpha(new_regime, progress)
        current_alpha = self.get_alpha()

        if new_regime in NEUTRAL_REGIMES:
            # 进入中性确认位: 保留当前仓位, 冻结观望 (target=current),
            # 不强制平仓/不收敛, 等 AI 确认方向 (RECOVERY→翻多; 信号恶化→回深熊)
            self.sm.set("alpha.current", current_alpha)
            self.sm.set("alpha.target", current_alpha)
            self.sm.set("alpha.transition_progress", 0.0)
        elif current_alpha * target < 0:
            # 仓位符号翻转 (空→多 或 多→空): 分两步快速换仓,
            # 变更当天先平仓归零并置 deferred_build 标记,
            # 次日 step_alpha 直接从 0 定位到目标 (而非 0.02/天爬坡)
            self.sm.set("alpha.current", 0.0)
            self.sm.set("alpha.target", target)
            self.sm.set("alpha.transition_progress", 0.0)
            self.sm.set("alpha.deferred_build", True)
        else:
            # 同侧/跨侧同符号/空仓 (如 BEAR_BOTTOM→BEAR_DEEP 直接加空): 确认即定位
            self.sm.set("alpha.current", target)
            self.sm.set("alpha.target", target)
            self.sm.set("alpha.transition_progress", 1.0)
        self.sm.set("alpha.last_change_at", now)
        # 变更轮消费自然日锚点: 旧 regime 停留的时间不计入新 regime 的
        # progress/步长 (下一轮从变更时刻起算, 避免"跨 regime 继承时间")
        self.sm.set("runtime.last_tick_at", now)

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

    def step_alpha(self, now: datetime = None) -> (float, bool):
        """将 alpha 向目标推进。返回 (新alpha, 是否变化).

        deferred_build 标记 (分两步换仓): 直接从 0 定位到目标, 不走爬坡.

        步进规则基于 BTC 4年周期 (自然日折算):
        - 动态步长 = clamp(days_elapsed / 当前regime预期天数,
          min_daily_step, max_change_per_step), days_elapsed 为距上次推进的自然日数
        - 首轮 (无 last_tick_at, days=0) 取下限 min_daily_step
        - 最大步长 max_change_per_step 仍为"单轮"上限
        """
        current = self.get_alpha()
        days_elapsed = self.consume_elapsed_days(now)
        progress = self.sm.get("alpha.regime_progress", 0.5)
        target = self.calculate_target_alpha(self.get_regime(), progress)
        now_iso = (now or datetime.utcnow()).isoformat() + "Z"

        if self.sm.get("alpha.deferred_build", False):
            self.sm.set("alpha.deferred_build", False)
            self.sm.set("alpha.current", target)
            self.sm.set("alpha.target", target)
            self.sm.set("alpha.transition_progress", 1.0)
            self.sm.set("alpha.last_change_at", now_iso)
            return target, True

        if self.get_regime() in NEUTRAL_REGIMES:
            # 中性确认位冻结: 保持现状观望, 仓位不随 progress 变化,
            # 等 AI 确认方向后由 execute_regime_change 处理
            self.sm.set("alpha.target", current)
            return current, False

        if abs(current - target) < 0.005:
            self.sm.set("alpha.target", target)
            self.sm.set("alpha.transition_progress", 1.0)
            return current, False

        # 动态步长: 按距上次推进的自然日数折算 (不再是"每轮 1/预期天数")
        dynamic_step = self._dynamic_step(self.get_regime(), days_elapsed)

        diff = target - current
        step = max(-dynamic_step, min(dynamic_step, diff))
        new_alpha = round(current + step, 4)

        self.sm.set("alpha.current", new_alpha)
        self.sm.set("alpha.target", target)
        self.sm.set("alpha.last_change_at", now_iso)
        self.sm.set("alpha.transition_progress", abs((new_alpha - current) / diff) if diff != 0 else 1.0)

        return new_alpha, True

    def tick_alpha(self, now: datetime = None) -> (float, bool):
        """无新推文时的 alpha 时间推进。返回 (新alpha, 是否变化).

        按**自然日**推进 (与调度频率解耦):
        - 进度增量 = days_elapsed / 预期天数 (days_elapsed 见 consume_elapsed_days)
        - days_elapsed≈0 (同日重复运行/旧 state 首轮初始化) → 不推进 (幂等)
        - 更新 progress 后重新计算 target 并按自然日折算步进
        """
        regime = self.get_regime()
        days_elapsed = self.consume_elapsed_days(now)
        if regime in NEUTRAL_REGIMES:
            return self.get_alpha(), False
        if days_elapsed <= 0:
            # 同秒/同日重复运行: 不重复推进 (幂等)
            return self.get_alpha(), False

        expected_days = self.expected_days.get(regime, 180)
        current_progress = self.sm.get("alpha.regime_progress", 0.5)
        new_progress = min(1.0, current_progress + days_elapsed / expected_days)
        self.sm.set("alpha.regime_progress", new_progress)

        # 基于新进度计算 target 并步进 (步长同样按自然日折算)
        current = self.get_alpha()
        target = self.calculate_target_alpha(regime, new_progress)
        dynamic_step = self._dynamic_step(regime, days_elapsed)

        if abs(current - target) < 0.005:
            self.sm.set("alpha.target", target)
            self.sm.set("alpha.transition_progress", 1.0)
            return current, False

        diff = target - current
        step = max(-dynamic_step, min(dynamic_step, diff))
        new_alpha = round(current + step, 4)
        now_iso = (now or datetime.utcnow()).isoformat() + "Z"

        self.sm.set("alpha.current", new_alpha)
        self.sm.set("alpha.target", target)
        self.sm.set("alpha.last_change_at", now_iso)
        self.sm.set("alpha.transition_progress", abs((new_alpha - current) / diff) if diff != 0 else 1.0)

        return new_alpha, True

    def tick_cooldown(self):
        """冷却期按**轮次**递减 (决策机会数; 与自然日推进相互独立)."""
        cd = self.sm.get("regime.cooldown_remaining", 0)
        if cd > 0:
            self.sm.set("regime.cooldown_remaining", cd - 1)

    def tick_stability(self):
        """稳定计数按**轮次**递增 (决策机会数; 与自然日推进相互独立)."""
        sc = self.sm.get("regime.stability_counter", 0)
        self.sm.set("regime.stability_counter", sc + 1)


class EvidenceAccumulator:
    def __init__(self, state_manager, decay_per_cycle: float = 0.02):
        self.sm = state_manager
        self.decay = decay_per_cycle

    def set_decay(self, value: float):
        """设置每轮证据衰减量 (月度复盘校准用)."""
        self.decay = float(value)

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
