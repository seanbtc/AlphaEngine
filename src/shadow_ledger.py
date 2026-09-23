"""WP8 8C 影子账本 (shadow ledger) — 纯观察四轨迹, 永不下单.

用途: 在"不接实盘"前提下长期观察不同节奏门控组合的盈利表现:
    baseline / +G1 / +G1+G2 / +G1+G2+G3
每条轨迹独立维护 regime/progress/alpha/方向/仓位/净值; regime 与引擎共享
(引擎换挡时同步换挡与引擎门控后的 progress, 同起点), 由此产生分叉。
推进语义与引擎对齐 (baseline 轨迹与引擎逐点一致; 引擎"未步进"分支——低置信
锁定/侧向兜底——不在镜像范围, 影子按自身规则推进):
- 分析轮: progress 取引擎落盘值 (= AI 输入, 不叠加自然日), 再应用本轨门控 →
  定位/步进 (对齐引擎 step_alpha);
- 空闲轮: 先门控 (作用于进入本轮 progress) → 再 +days/expected (对齐
  run_cycle 空闲路径的 gate→tick_alpha);
- 换挡轮: 同步引擎 progress, 不消费 days, 锚点对齐换挡时刻 (对齐
  execute_regime_change);
- outage 轮: 不推进、不刷新锚点; 下一推进轮一次性重置锚点且 days=0
  (对齐 _clear_outage, 故障时长不补记)。

成交成本 = taker + 滑点, 参考本金固定 (不复利); 目标仓位变化 >=
rebalance_threshold_pp 才记事件 (复刻 TradeSync 去重)。

产物 (data/shadow/, 已 gitignore):
- state.json   四轨迹当前状态 (原子写)
- ledger.jsonl 事件流 (init|enter|adjust|exit; 原子 append)
- equity.jsonl 每轮净值快照 (按当前价 mark-to-market)

隔离红线:
- 只读引擎纯函数/状态 (calculate_target_alpha/_dynamic_step/expected_days) 与
  自身影子状态; 绝不写引擎 state.json、不碰发单、不影响 AI 输出/通知/发帖;
- 全部路径 try/except, 失败只打印日志, 绝不阻断主流程;
- 任何配置非法值回退默认, 不抛异常。
"""
import json
import os
from datetime import datetime, timezone

from src.alpha_engine import NEUTRAL_REGIMES
from src.atomic_io import atomic_write_text
from src.rhythm_gates import apply_rhythm_gates

TRACKS = ("baseline", "G1", "G1_G2", "G1_G2_G3")
TRACK_GATES = {
    "baseline": (),
    "G1": ("bear_reclaim_gate",),
    "G1_G2": ("bear_reclaim_gate", "recovery_completion_gate"),
    "G1_G2_G3": ("bear_reclaim_gate", "recovery_completion_gate",
                 "bull_top_gate"),
}
_GATE_NAMES = ("bear_reclaim_gate", "recovery_completion_gate", "bull_top_gate")

# 方向口径与 TradeSync 一致 (|alpha| <= 0.05 → cash)
CASH_THRESHOLD = 0.05
# 引擎 step_alpha 的"已达标"阈值 (同一常数)
_ALPHA_EPS = 0.005

_DEFAULT_SHADOW = {"enabled": False, "notional": 10000.0, "fee_pct": 0.05,
                   "slip_pct": 0.02, "rebalance_threshold_pp": 1.0}


def _iso(now: datetime) -> str:
    return now.isoformat() + "Z"


def _parse_utc(iso):
    """解析 UTC iso 时间串; 非法/空返回 None (与引擎同口径)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _as_float(value, default, min_value=None):
    """数值化: 非法/NaN → default; min_value 给定时取下界."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    if min_value is not None:
        number = max(min_value, number)
    return number


def _clamp_alpha(value) -> float:
    return max(-1.0, min(1.0, _as_float(value, 0.0)))


def _clamp_progress(value) -> float:
    return max(0.0, min(1.0, _as_float(value, 0.5)))


def _direction(alpha: float) -> str:
    if alpha > CASH_THRESHOLD:
        return "long"
    if alpha < -CASH_THRESHOLD:
        return "short"
    return "cash"


def _size_pct(alpha: float) -> float:
    return round(abs(alpha) * 100, 1)


def _phase_code(market_state) -> str:
    """当轮 cycle_context 阶段码 (1-4; 过渡/缺失 → "")."""
    if not isinstance(market_state, dict):
        return ""
    context = market_state.get("cycle_context")
    if not isinstance(context, dict):
        return ""
    phase = context.get("phase")
    if not isinstance(phase, dict):
        return ""
    code = phase.get("code")
    if isinstance(code, bool):
        return ""
    if isinstance(code, int) and 1 <= code <= 4:
        return str(code)
    return ""


class ShadowLedger:
    """四轨迹影子账本; 由 run_cycle 轮末调用 on_cycle (失败只日志)."""

    def __init__(self, alpha_cfg: dict, data_dir: str, engine,
                 state_manager=None):
        alpha_cfg = alpha_cfg if isinstance(alpha_cfg, dict) else {}
        shadow_cfg = alpha_cfg.get("shadow")
        self.cfg = shadow_cfg if isinstance(shadow_cfg, dict) else {}
        self.enabled = bool(self.cfg.get("enabled", False))
        self.notional = _as_float(self.cfg.get("notional"),
                                  _DEFAULT_SHADOW["notional"], min_value=0.0)
        if self.notional <= 0:
            self.notional = _DEFAULT_SHADOW["notional"]
        self.fee_pct = _as_float(self.cfg.get("fee_pct"),
                                 _DEFAULT_SHADOW["fee_pct"], min_value=0.0)
        self.slip_pct = _as_float(self.cfg.get("slip_pct"),
                                  _DEFAULT_SHADOW["slip_pct"], min_value=0.0)
        self.rebalance_threshold_pp = _as_float(
            self.cfg.get("rebalance_threshold_pp"),
            _DEFAULT_SHADOW["rebalance_threshold_pp"], min_value=0.0)
        rhythm = alpha_cfg.get("rhythm")
        self.rhythm_cfg = rhythm if isinstance(rhythm, dict) else {}
        smoothing = alpha_cfg.get("smoothing")
        self.smoothing = smoothing if isinstance(smoothing, dict) else {}
        self.engine = engine
        self.sm = state_manager
        shadow_dir = os.path.join(data_dir, "shadow")
        self.state_file = os.path.join(shadow_dir, "state.json")
        self.ledger_file = os.path.join(shadow_dir, "ledger.jsonl")
        self.equity_file = os.path.join(shadow_dir, "equity.jsonl")

    # ---- 主入口 ----

    def on_cycle(self, *, price=None, market_state=None, advance=True,
                 mode="idle", now=None) -> bool:
        """轮末记录一轮; 返回是否写入。任何异常都不外抛 (失败只日志)。"""
        if not self.enabled:
            return False
        try:
            now = now or datetime.utcnow()
            phase = _phase_code(market_state)
            state = self._load_or_default(now)
            engine_regime = self._engine_regime()
            engine_progress = self._engine_progress()
            events, snapshots = [], []
            for name in TRACKS:
                track = state["tracks"].get(name)
                if not isinstance(track, dict):
                    # 首次初始化: 纯快照 (与引擎当前状态同起点), 本轮不再步进
                    track, event = self._init_track(name, price, now)
                    if not advance:
                        # 首轮即 outage: 下一推进轮重置锚点, days=0
                        track["outage_pending"] = True
                    state["tracks"][name] = track
                    if event:
                        events.append(event)
                    if price is not None:
                        snapshots.append(self._snapshot(name, track, price,
                                                        phase, now))
                    continue
                event = self._advance_track(
                    name, track, engine_regime, engine_progress, price,
                    market_state, phase, now, advance, mode)
                if event:
                    events.append(event)
                if price is not None:
                    snapshots.append(self._snapshot(name, track, price, phase,
                                                    now))
            state["last_cycle_at"] = _iso(now)
            self._save_state(state)
            if events:
                self._append_records(self.ledger_file, events)
            if snapshots:
                self._append_records(self.equity_file, snapshots)
            return True
        except Exception as exc:  # 隔离: 失败只日志, 绝不阻断主流程
            print(f"[Shadow] 本轮记录失败 (不影响主流程): "
                  f"{type(exc).__name__}: {exc}")
            return False

    # ---- 引擎读取 (只读) ----

    def _engine_regime(self) -> str:
        try:
            regime = self.engine.get_regime()
        except Exception:
            return "BEAR"
        return regime if isinstance(regime, str) and regime else "BEAR"

    def _engine_alpha(self) -> float:
        try:
            return _clamp_alpha(self.engine.get_alpha())
        except Exception:
            return 0.0

    def _engine_progress(self) -> float:
        try:
            if self.sm is not None:
                return _clamp_progress(self.sm.get("alpha.regime_progress", 0.5))
        except Exception:
            pass
        return 0.5

    def _expected_days(self, regime: str) -> float:
        try:
            return float(self.engine.expected_days.get(regime, 180))
        except Exception:
            return 180.0

    def max_catchup_days(self) -> float:
        try:
            return float(self.engine.max_catchup_days())
        except Exception:
            return 7.0

    def _dynamic_step(self, regime: str, days: float) -> float:
        try:
            return float(self.engine._dynamic_step(regime, days))
        except Exception:
            expected = self._expected_days(regime)
            min_step = _as_float(self.smoothing.get("min_daily_step"), 0.02)
            max_step = _as_float(self.smoothing.get("max_change_per_step"), 0.10)
            return max(min_step, min(days / expected, max_step))

    # ---- 状态加载/初始化 ----

    def _load_state(self):
        if not os.path.exists(self.state_file):
            return None
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except Exception as exc:
            print(f"[Shadow] 状态文件读取失败, 重新初始化: {exc}")
            return None
        if not isinstance(data, dict) or not isinstance(data.get("tracks"), dict):
            print("[Shadow] 状态文件结构非法, 重新初始化")
            return None
        return data

    def _load_or_default(self, now):
        """读取影子状态; 缺失/损坏 → 空状态 (轨迹在 on_cycle 循环内惰性初始化)."""
        state = self._load_state()
        if state is None:
            state = {"version": 1, "created_at": _iso(now),
                     "last_cycle_at": "", "tracks": {}}
        if not isinstance(state.get("tracks"), dict):
            state["tracks"] = {}
        return state

    def _init_track(self, name, price, now):
        """初始化轨迹: 与引擎同起点 (regime/progress/alpha), 仓位=alpha*本金."""
        now_iso = _iso(now)
        regime = self._engine_regime()
        alpha = self._engine_alpha()
        progress = self._engine_progress()
        direction = _direction(alpha)
        size = _size_pct(alpha)
        position = 0.0 if direction == "cash" else alpha * self.notional
        track = {
            "regime": regime,
            "progress": progress,
            "alpha": alpha,
            "direction": direction,
            "size_pct": size,
            "position": round(position, 6),
            "equity": self.notional,
            "last_order": {"direction": direction, "size_pct": size,
                           "ts": now_iso, "alpha": alpha},
            "started_at": now_iso,
            "last_tick_at": now_iso,
            "last_price": price,
            "deferred_build": False,
            "outage_pending": False,
        }
        event = {
            "ts": now_iso, "track": name, "event": "init", "price": price,
            "alpha": alpha, "size_pct": size, "direction": direction,
            "reason": "初始化(同步引擎)", "phase": "",
            "fee_pct": self.fee_pct, "slip_pct": self.slip_pct,
            "notional": round(abs(position), 6), "cost": 0.0,
        }
        return track, event

    # ---- 单轨迹推进 ----

    def _consume_days(self, track, now) -> float:
        """自然日折算 (与引擎 consume_elapsed_days 同口径/常数)."""
        last = _parse_utc(track.get("last_tick_at"))
        if last is None:
            track["last_tick_at"] = _iso(now)
            return 0.0
        elapsed = (now - last).total_seconds() / 86400.0
        max_catchup = self.max_catchup_days()
        if elapsed < -max_catchup:
            track["last_tick_at"] = _iso(now)
            return 0.0
        if elapsed <= 0:
            return 0.0
        track["last_tick_at"] = _iso(now)
        return min(elapsed, max_catchup)

    def _gate_cfg(self, name) -> dict:
        """轨迹门控组合: 参数取自 alpha.rhythm, enabled 按轨迹强制."""
        result = {}
        for gate in _GATE_NAMES:
            section = self.rhythm_cfg.get(gate) if isinstance(self.rhythm_cfg,
                                                              dict) else None
            section = dict(section) if isinstance(section, dict) else {}
            section["enabled"] = gate in TRACK_GATES.get(name, ())
            result[gate] = section
        return result

    def _apply_gates(self, name, track, market_state):
        """对本轨当前 progress 应用门控组合; 返回门控 reasons (fail-open 不变)."""
        regime = track.get("regime")
        progress = _clamp_progress(track.get("progress", 0.5))
        try:
            gated, reasons = apply_rhythm_gates(regime, progress, market_state,
                                                self._gate_cfg(name))
        except Exception as exc:  # 门控异常 fail-open
            print(f"[Shadow] 门控异常 (fail-open, 不施加): {exc}")
            track["progress"] = progress
            return []
        track["progress"] = (_clamp_progress(gated)
                             if isinstance(gated, (int, float))
                             and not isinstance(gated, bool) else progress)
        return list(reasons or [])

    def _step_toward(self, track, target, days):
        """向 target 步进 (引擎 step_alpha 同一阈值/clamp); 返回 reason."""
        current = _as_float(track.get("alpha"), 0.0)
        if abs(current - target) < _ALPHA_EPS:
            return ""
        step = self._dynamic_step(track["regime"], days)
        diff = target - current
        track["alpha"] = round(current + max(-step, min(step, diff)), 4)
        return "自然日推进"

    def _sync_regime(self, track, engine_regime, engine_progress):
        """引擎换挡同步: progress 取引擎门控后落盘值 (同起点) + 引擎定位语义."""
        track["regime"] = engine_regime
        track["progress"] = engine_progress
        target = self.engine.calculate_target_alpha(engine_regime,
                                                    engine_progress)
        alpha = _as_float(track.get("alpha"), 0.0)
        if engine_regime in NEUTRAL_REGIMES:
            # 中性确认位: 保留当前仓位, 冻结观望
            track["deferred_build"] = False
        elif alpha * target < 0:
            # 跨零换仓: 先平仓归零, 次日定位 (deferred_build)
            track["alpha"] = 0.0
            track["deferred_build"] = True
        else:
            # 同侧/同符号/空仓: 确认即定位
            track["alpha"] = target
            track["deferred_build"] = False

    def _advance_track(self, name, track, engine_regime, engine_progress,
                       price, market_state, phase, now, advance, mode):
        now_iso = _iso(now)
        reasons = []
        reason = ""
        current_regime = track.get("regime")
        if not isinstance(current_regime, str) \
                or current_regime not in self.engine.expected_days \
                or engine_regime != current_regime:
            # 换挡同步 (含脏 regime 对齐): 不消费 days, 锚点对齐换挡时刻
            # (对齐 execute_regime_change 的 runtime.last_tick_at=now)
            self._sync_regime(track, engine_regime, engine_progress)
            track["last_tick_at"] = now_iso
            track["outage_pending"] = False
            reason = f"引擎换挡→{engine_regime}"
        elif not advance:
            # outage 轮: 不推进也不刷新锚点 (下一推进轮重置, 对齐 _clear_outage)
            track["outage_pending"] = True
        else:
            if track.get("outage_pending"):
                # 故障恢复轮: 一次性重置锚点, days=0 (故障时长不补记)
                track["outage_pending"] = False
                track["last_tick_at"] = now_iso
                days = 0.0
            else:
                days = self._consume_days(track, now)
            if track.get("regime") in NEUTRAL_REGIMES:
                # 中性确认位冻结: progress 不推进, 仓位不随 progress 变化
                # (引擎 tick_alpha 先消费 days 再返回, 故此处仍需消费锚点)
                track["deferred_build"] = False
            elif mode == "analysis":
                # 分析轮: progress = 引擎落盘值 (AI 输入, 不叠加 days)
                # → 本轨门控 → 定位/步进 (对齐引擎 step_alpha)
                track["progress"] = engine_progress
                reasons = self._apply_gates(name, track, market_state)
                target = self.engine.calculate_target_alpha(track["regime"],
                                                            track["progress"])
                if track.get("deferred_build"):
                    # 引擎 step_alpha 的 deferred_build: 直接定位到目标
                    track["deferred_build"] = False
                    track["alpha"] = target
                    reason = "换挡次日定位"
                else:
                    reason = self._step_toward(track, target, days)
            else:
                # 空闲轮: 先门控 (作用于进入本轮 progress) → 再 +days/expected
                # (对齐 run_cycle 空闲路径 gate → tick_alpha)
                reasons = self._apply_gates(name, track, market_state)
                if days > 0:
                    expected = self._expected_days(track["regime"])
                    if expected > 0:
                        track["progress"] = min(
                            1.0, _clamp_progress(track["progress"])
                            + days / expected)
                    target = self.engine.calculate_target_alpha(
                        track["regime"], track["progress"])
                    reason = self._step_toward(track, target, days)
        if reasons:
            reason = "; ".join(reasons)
        event = None
        if price is not None:
            self._mark_to_market(track, price)
            event = self._maybe_emit(name, track, price, phase, now_iso, reason)
        return event

    # ---- 净值/事件 ----

    def _mark_to_market(self, track, price):
        """按当前价 mark-to-market (signed position × 价格收益)."""
        try:
            price = float(price)
        except (TypeError, ValueError):
            return
        last_price = track.get("last_price")
        position = _as_float(track.get("position"), 0.0)
        if last_price not in (None, "") and price > 0:
            try:
                last_price = float(last_price)
                if last_price > 0:
                    ret = price / last_price - 1.0
                    track["equity"] = round(
                        _as_float(track.get("equity"), self.notional)
                        + position * ret, 6)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        track["last_price"] = price

    def _maybe_emit(self, name, track, price, phase, now_iso, reason):
        """目标仓位变化 >= 阈值 (或方向变化) 才记事件 (复刻 TradeSync 去重)."""
        alpha = _clamp_alpha(track.get("alpha", 0.0))
        direction = _direction(alpha)
        size = _size_pct(alpha)
        last_order = track.get("last_order")
        last_order = last_order if isinstance(last_order, dict) else {}
        prev_direction = track.get("direction") or last_order.get("direction") \
            or "cash"
        if last_order:
            last_size = _as_float(last_order.get("size_pct"), 0.0)
            if last_order.get("direction") == direction \
                    and abs(last_size - size) < self.rebalance_threshold_pp:
                return None
        old_position = _as_float(track.get("position"), 0.0)
        new_position = 0.0 if direction == "cash" else alpha * self.notional
        trade = abs(new_position - old_position)
        cost = round(trade * (self.fee_pct + self.slip_pct) / 100.0, 6)
        if not last_order:
            event_type = "init"
        elif direction == "cash" and prev_direction != "cash":
            event_type = "exit"
        elif direction != "cash" and prev_direction == "cash":
            event_type = "enter"
        else:
            event_type = "adjust"
        track["equity"] = round(
            _as_float(track.get("equity"), self.notional) - cost, 6)
        track["position"] = round(new_position, 6)
        track["direction"] = direction
        track["size_pct"] = size
        track["last_order"] = {"direction": direction, "size_pct": size,
                               "ts": now_iso, "alpha": alpha}
        return {
            "ts": now_iso, "track": name, "event": event_type, "price": price,
            "alpha": alpha, "size_pct": size, "direction": direction,
            "reason": reason or "自然日推进", "phase": phase,
            "fee_pct": self.fee_pct, "slip_pct": self.slip_pct,
            "notional": round(trade, 6), "cost": cost,
        }

    def _snapshot(self, name, track, price, phase, now):
        return {
            "ts": _iso(now), "track": name, "price": price,
            "equity": round(_as_float(track.get("equity"), self.notional), 6),
            "position": round(_as_float(track.get("position"), 0.0), 6),
            "alpha": _clamp_alpha(track.get("alpha", 0.0)),
            "regime": track.get("regime", ""),
            "phase": phase,
        }

    # ---- 原子落盘 ----

    def _save_state(self, state):
        atomic_write_text(self.state_file,
                          json.dumps(state, ensure_ascii=False, indent=2))

    def _append_records(self, path, records):
        """原子 append: 读旧文 + 追加新行 + 原子替换 (单实例写, 量小)."""
        existing = ""
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                existing = handle.read()
        lines = "".join(json.dumps(record, ensure_ascii=False) + "\n"
                        for record in records)
        atomic_write_text(path, existing + lines)
