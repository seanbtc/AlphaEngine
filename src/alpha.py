"""
Glassnode Alpha Engine — 主入口.

用法:
    python -m src.alpha
    nohup python3 -u -m src.alpha > engine.log 2>&1 &
"""
import atexit
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta

import requests

from src.config_loader import load_config, resolve_data_dir
from src.memory import Memory
from src.params_store import apply_overlay, load_overlay
from src.singleton_lock import SingletonLock
from src.state_manager import StateManager
from src.fetcher import Fetcher
from src.analyzer import Analyzer
from src.alpha_engine import (AlphaEngine, EvidenceAccumulator,
                              REGIME_ALPHA_MAP, REGIME_TRANSITIONS)
from src.knowledge import Knowledge
from src.ma_context import build_ma_context, summarize_ma_context
from src.pending_analysis import PendingAnalysis
from src.cycle_context import build_cycle_context
from src.tradesync import TradeSync
from src.datafeed import DataFeed
from src.notify import DingTalk
from src.review_engine import ReviewEngine

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
from commons.event_contract import build_event


def _push_promo_event(record: dict) -> bool:
    """推送事件到 Promo HTTP 服务 (env PROMO_EVENTS_URL); 未配置/失败返回 False。"""
    url = str(os.getenv("PROMO_EVENTS_URL", "") or "").strip()
    if not url:
        return False
    headers = {"Content-Type": "application/json"}
    token = str(os.getenv("PROMO_EVENTS_TOKEN", "") or "").strip()
    if token:
        headers["X-Webhook-Token"] = token
    try:
        resp = requests.post(url, json=record, headers=headers, timeout=5)
    except requests.RequestException as exc:
        print(f"[Promo] 事件推送异常: {exc}")
        return False
    if not (200 <= resp.status_code < 300):
        print(f"[Promo] 事件推送失败 HTTP {resp.status_code}")
        return False
    try:
        body = resp.json()
    except ValueError:
        return True
    return bool(body.get("ok", True))


def _write_promo_post(cfg: dict, post_text: str, post_no: int, cycle: str, alpha: float):
    """把编号帖子推送给 Promo: 仅 HTTP 推送, 失败即丢弃 (不写文件桥, 过期不候)。"""
    promo_cfg = cfg.get("promo", {})
    if not promo_cfg.get("enabled", False) or not post_text:
        return
    try:
        record = build_event("AlphaEngine", "alpha_post", {
            "ts": datetime.utcnow().isoformat() + "Z",
            "post_no": post_no,
            "content": post_text,
            "cycle": cycle,
            "alpha": alpha,
        })
        if _push_promo_event(record):
            print(f"[Promo] 帖子 No.{post_no} 已推送到 Promo 事件服务")
        else:
            print(f"[Promo] 帖子 No.{post_no} 推送失败, 已丢弃")
    except Exception as e:
        print(f"[Promo] 推送帖子失败: {e}")


def _valid_cycle_position(cp) -> bool:
    """AI 输出 cp 合法性守卫 (与 analyzer 校验同口径): 仅 7 个 regime."""
    return cp in REGIME_ALPHA_MAP


def _select_progress(rp, ok: bool, cp: str, current_regime: str,
                     old_progress: float) -> float:
    """选择本轮要落盘的 regime_progress (纯函数, 便于测试).

    仅当 regime 提议被接受 (ok) 或 AI 描述的就是当前 regime 时才采用新值;
    提议被拒且指向其它 regime 时保留旧值 —— 被拒的跨级提议若用新进度参与
    target 计算, 会把"下一阶段"的语义错位到"当前 regime"上。
    """
    if rp is None:
        return old_progress
    try:
        new_progress = float(rp)
    except (TypeError, ValueError):
        return old_progress
    if ok or cp == current_regime:
        return max(0.0, min(1.0, new_progress))
    return old_progress


# 发单一致性: alpha 变化判定阈值 (浮点误差同量级)
_ALPHA_ORDER_EPS = 1e-9


def _send_alpha_order_if_changed(engine, tradesync, alpha_cycle_start: float,
                                 btc_price, *, reason: str):
    """统一发单钩子: 本轮 alpha 相对周期起点变化时, 把最终值发给 TradeSync.

    任何会改 alpha 的路径 (regime 变更 / deferred_build / SIDE-FIX / step /
    idle 时间推进) 都在该轮结束时调用本钩子一次, 保证引擎 alpha 与发单指令一致;
    多点变更只发本轮最终值 (每轮至多一次)。
    引导路径 (run_first_analysis / run_backfill) 豁免, 不调用本钩子。
    发送异常或 None (未启用/同向去重/发送失败) 不影响主流程。
    """
    final = engine.get_alpha()
    if abs(final - alpha_cycle_start) <= _ALPHA_ORDER_EPS:
        return None
    regime = engine.get_regime()
    try:
        order = tradesync.send_order(final, regime, btc_price)
    except Exception as exc:
        print(f"  [TradeSync] alpha {alpha_cycle_start:+.4f} → {final:+.4f} | "
              f"regime={regime} | reason={reason} | 发送异常 (不影响本轮): {exc}")
        return None
    status = "已发送" if order else "未生效 (未启用/同向去重/发送失败)"
    print(f"  [TradeSync] alpha {alpha_cycle_start:+.4f} → {final:+.4f} | "
          f"regime={regime} | reason={reason} | {status}")
    return order


def _alert_state_recovery(dingtalk, info: dict) -> None:
    """state 损坏恢复后的钉钉告警 (通知失败不影响启动)."""
    reason = info.get("reason", "未知")
    backup = info.get("backup_path") or "备份失败"
    if info.get("rebuilt"):
        title = "state 已从记忆重建"
        body = (f"原因: {reason}\n备份: {backup}\n"
                f"重建: regime={info.get('regime')}, alpha={info.get('alpha')}")
    else:
        title = "state 损坏且无记忆可重建"
        body = (f"原因: {reason}\n备份: {backup}\n"
                f"已使用默认状态 (regime=BEAR, alpha=0.0)")
    print(f"[State] {title} | " + body.replace("\n", " | "))
    try:
        dingtalk.alert(title, body)
    except Exception as exc:
        print(f"[State] 恢复告警发送失败 (不影响启动): {exc}")


def init_components(cfg: dict):
    data_dir = resolve_data_dir(cfg)
    os.makedirs(data_dir, exist_ok=True)

    memory = Memory(data_dir)
    state_mgr = StateManager(data_dir, cfg.get("paths", {}).get("state_file", "state.json"))
    dingtalk = DingTalk(cfg.get("dingtalk", {}))
    state_mgr.load(on_recovered=lambda info: _alert_state_recovery(dingtalk, info),
                   memory=memory)

    fetcher_cfg = cfg.get("fetcher", {})
    fetcher = Fetcher(fetcher_cfg, data_dir)
    pending = PendingAnalysis(
        data_dir,
        max_items=fetcher_cfg.get("pending_max_items", 100),
        max_age_days=fetcher_cfg.get("pending_max_age_days", 7),
    )
    ai_cfg = dict(cfg.get("ai_service") or cfg.get("deepseek") or {})
    ai_cfg["cross_check"] = (cfg.get("alpha") or {}).get("cross_check") or {}
    analyzer = Analyzer(ai_cfg)
    engine = AlphaEngine(cfg.get("alpha", {}), state_mgr)
    evidence = EvidenceAccumulator(
        state_mgr, cfg.get("alpha", {}).get("evidence", {}).get("decay_per_cycle", 0.02))
    knowledge = Knowledge(cfg.get("knowledge", {}), data_dir, analyzer)

    # 校准覆盖恢复 (data/params.json): 只读加载, 重启后仍生效;
    # 非法文件容错告警, 不影响启动。--test-ai 等只读入口同样只读不写。
    overlay = load_overlay(data_dir)
    if overlay:
        applied_overlay = apply_overlay(engine, overlay, evidence=evidence,
                                        knowledge=knowledge)
        if applied_overlay:
            print(f"[Params] 已加载校准覆盖 {len(applied_overlay)} 项: "
                  + ", ".join(applied_overlay))

    tradesync = TradeSync(cfg.get("tradesync", {}), data_dir)
    datafeed = DataFeed(cfg.get("datafeed", {}))
    review = ReviewEngine(cfg.get("review", {}), data_dir, state_mgr, engine,
                          knowledge, evidence=evidence)

    return {
        "cfg": cfg, "data_dir": data_dir,
        "memory": memory, "state": state_mgr,
        "fetcher": fetcher, "analyzer": analyzer,
        "engine": engine, "evidence": evidence,
        "knowledge": knowledge, "tradesync": tradesync,
        "datafeed": datafeed, "dingtalk": dingtalk,
        "review": review, "pending": pending,
    }


def print_status(components: dict):
    sm = components["state"]
    regime = sm.get_regime()
    alpha = sm.get_alpha()
    count = sm.get("runtime.analysis_count", 0)
    print(f"\n[Status] Regime={regime} | Alpha={alpha:+.4f} | Analyses={count}")
    line = (f"         Cooldown={sm.get('regime.cooldown_remaining',0)} | "
            f"Stability={sm.get('regime.stability_counter',0)}")
    outage = sm.get("runtime.outage") or {}
    if isinstance(outage, dict) and outage:
        line += (f" | Outage={outage.get('reason', '?')}"
                 f"(since {outage.get('since', '?')})")
    engine = components.get("engine")
    if engine is not None:
        pending = engine.get_pending_proposal()
        if pending:
            line += (f" | Pending={pending.get('cp')}"
                     f"({engine.pending_count(pending)}/"
                     f"{engine.required_confirmations()})")
    dingtalk = components.get("dingtalk")
    failures = int(getattr(dingtalk, "failure_count", 0) or 0)
    if failures > 0:
        last_at = getattr(dingtalk, "last_failure_at", "") or "?"
        line += f" | Notify失败={failures}次 (最近 {last_at})"
    pending_store = components.get("pending")
    if pending_store is not None:
        try:
            pending_tweets = pending_store.count()
        except Exception:
            pending_tweets = 0
        if pending_tweets > 0:
            line += f" | PendingTweets={pending_tweets}"
    print(line)


def build_market_state(components: dict, price: float = None,
                       persist_ma: bool = True) -> dict:
    """组装当前引擎状态, 作为 AI 判断的连续性锚点.

    persist_ma=False (--test-ai / 回溯等路径): 只读均线历史, 不写 ma_history.jsonl。
    """
    sm = components["state"]
    engine = components["engine"]
    regime = engine.get_regime()
    state = {
        "regime": regime,
        "alpha": engine.get_alpha(),
        "entered_from": sm.get("regime.entered_from", "") or "",
        "progress": sm.get("alpha.regime_progress", 0.5),
        "last_change_at": sm.get("regime.last_changed_at", "") or "",
        "allowed_transitions": list(REGIME_TRANSITIONS.get(regime, [])),
    }
    # regime 已持续天数
    started_at = sm.get("regime.started_at", "")
    if started_at:
        try:
            start_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            state["regime_days"] = max(0, int((datetime.utcnow() - start_dt.replace(tzinfo=None)).total_seconds() // 86400))
        except ValueError:
            state["regime_days"] = None
    if price is not None:
        state["price"] = price
    # 价格趋势 (从 price_history 计算 7d/30d 涨跌)
    review = components.get("review")
    if review is not None:
        trend = review.price_trend()
        if trend.get("price") is not None and state.get("price") is None:
            state["price"] = trend["price"]
        if trend.get("change_7d") is not None:
            state["price_change_7d"] = trend["change_7d"]
        if trend.get("change_30d") is not None:
            state["price_change_30d"] = trend["change_30d"]
    # 移动均线结构 (日线): 辅助 AI 判断价格区间/趋势; 失败不阻塞主流程
    ma_cfg = (components.get("cfg") or {}).get("ma_context") or {}
    if ma_cfg.get("enabled", False):
        try:
            ma_context = build_ma_context(ma_cfg, client=components.get("datafeed"),
                                          persist=persist_ma)
            if ma_context:
                state["ma_context"] = ma_context
        except Exception as exc:
            print(f"[MA] 均线上下文构建失败 (不影响本轮): {exc}")
    # 周期定位与历史类比 (日线): 辅助 AI 结合判断; 失败不阻塞主流程
    cycle_cfg = (components.get("cfg") or {}).get("cycle_context") or {}
    if cycle_cfg.get("enabled", False):
        try:
            cycle_context = build_cycle_context(
                cycle_cfg, client=components.get("datafeed"),
                ma_context=state.get("ma_context"),
                regime={"name": regime, "alpha": engine.get_alpha()})
            if cycle_context:
                state["cycle_context"] = cycle_context
        except Exception as exc:
            print(f"[Cycle] 周期上下文构建失败 (不影响本轮): {exc}")
    return state


def _record_ma_state(sm, market_state: dict) -> dict:
    """把均线摘要写入 state["ma"] (与 regime/alpha 同一保存点落盘).

    摘要来自本轮 build_market_state 的 ma_context; 无上下文时 available=false。
    """
    summary = summarize_ma_context((market_state or {}).get("ma_context"))
    summary["updated_at"] = datetime.utcnow().isoformat() + "Z"
    sm.set("ma", summary)
    return summary


def run_backfill(components: dict, force: bool = False) -> bool:
    """回溯历史推文，构建初始状态。返回是否执行了回填.

    force=True 时忽略已有分析记录，重新回溯并重设 regime/alpha.
    """
    c = components
    cfg = c["cfg"]
    sm = c["state"]

    backfill_cfg = cfg.get("backfill", {})
    if not backfill_cfg.get("enabled", True):
        print("[Backfill] 已禁用 (config)")
        return False

    if not force and sm.get("runtime.analysis_count", 0) > 0:
        print("[Backfill] 已有分析记录，跳过回溯 (用 --backfill 强制重跑)")
        return False

    if force:
        print("[Backfill] 强制模式: 重新回溯历史推文并重置证据")
        c["evidence"].reset()

    batch_size = backfill_cfg.get("batch_size", 10)
    max_total = backfill_cfg.get("max_total_tweets", 50)
    bulk_limit = backfill_cfg.get("bulk_fetch_limit", 2000)
    max_samples = backfill_cfg.get("max_analysis_samples", 200)

    print(f"\n{'='*60}")
    print("[Backfill] 首次启动 — 回溯历史推文建立初始状态")
    print(f"[Backfill] bulk_limit={bulk_limit}, max_samples={max_samples}, batch_size={batch_size}")
    print("=" * 60)

    fetcher = c["fetcher"]
    memory = c["memory"]

    # 1. 获取数据: snscrape > 网页 > 文件
    print("[Backfill] 阶段1: 获取历史推文 ...")
    total = fetcher.count_tweets()

    if total < 20:
        bulk_count = fetcher.fetch_bulk(limit=bulk_limit)
        total = fetcher.count_tweets()
        print(f"[Backfill] snscrape 批量抓取: +{bulk_count} 条, 总计 {total} 条")

    if total < 5:
        print("[Backfill] 网页补充抓取...")
        fetcher.fetch()
        total = fetcher.count_tweets()

    if total == 0:
        print("[Backfill] 无历史推文，跳过回溯 (改天 snscrape/网页 可用时再跑)")
        return False

    # 2. 智能采样
    print(f"\n[Backfill] 阶段2: 从 {total} 条中智能采样 ≤{max_samples} 条 ...")
    all_tweets = fetcher.load_all_tweets()
    all_tweets.sort(key=lambda t: t.get("id", ""))
    samples = _smart_sample(all_tweets, max_samples)
    print(f"[Backfill] 采样 {len(samples)} 条 (覆盖 {len(all_tweets)} 条全量)")

    # 3. 分批分析 — 仅采集，不执行 regime/alpha 变更
    batches = [samples[i:i+batch_size] for i in range(0, len(samples), batch_size)]
    print(f"\n[Backfill] 阶段3: 分 {len(batches)} 批采集数据 ...")

    engine = c["engine"]
    evidence = c["evidence"]
    analyzer = c["analyzer"]
    knowledge = c["knowledge"]
    dingtalk = c["dingtalk"]
    kb = knowledge.load_knowledge_base()

    collected = []  # [{cp, scores, summary, conf, regime_evidence, signal_board, meta}]

    for bi, batch in enumerate(batches):
        print(f"\n[Backfill] 批次 {bi+1}/{len(batches)} ({len(batch)} 推文) ...")

        ctx = memory.get_context_for_ai()
        analysis = analyzer.analyze(batch, ctx, kb,
                                    market_state=build_market_state(c, persist_ma=False))

        if not analysis:
            print("[Backfill]   分析失败，跳过本批")
            continue

        cp = analysis.get("cycle_position", "BEAR")
        if not _valid_cycle_position(cp):
            print(f"[Backfill]   非法 cycle_position={cp!r}, 拒绝本批")
            continue

        sm.set("runtime.last_deepseek_at", datetime.utcnow().isoformat() + "Z")

        scores = analysis.get("evidence_scores", {})
        meta = analysis.get("meta", {})
        print(f"[Backfill]   cycle_position={cp}, scores={json.dumps(scores)}, "
              f"quality={meta.get('analysis_quality','?')}")

        collected.append({
            "cp": cp,
            "scores": scores,
            "progress": analysis.get("regime_progress", 0.5),
            "summary": analysis.get("summary", ""),
            "conf": analysis.get("cycle_confidence", "low"),
            "regime_evidence": analysis.get("regime_evidence", ""),
            "signal_board": analysis.get("signal_board", []),
            "risks": analysis.get("risks", []),
            "meta": meta,
        })

        sm.update_runtime()

    if not collected:
        print("[Backfill] 无有效分析结果，跳过")
        sm.save()
        return False

    # 4. 整合 — 最终 regime 用最近 30% 批次的众数
    print(f"\n[Backfill] 阶段4: 整合 {len(collected)} 批数据 ...")
    recent_n = max(1, len(collected) // 3)
    recent = collected[-recent_n:]
    regime_votes = {}
    for c in recent:
        cp = c["cp"]
        regime_votes[cp] = regime_votes.get(cp, 0) + 1
    final_regime = max(regime_votes, key=regime_votes.get)
    print(f"[Backfill] 最近 {recent_n} 批 regime 投票: {regime_votes} → {final_regime}")

    # 累积全部证据
    for c in collected:
        for cat, sc in c["scores"].items():
            evidence.update(c["cp"], cat, float(sc))

    # 一次性执行 regime 变更
    current = engine.get_regime()
    rc = collected[-1]

    # 先按最后一批的周期内进度计算目标 alpha, 再执行变更 (回溯不走步进)
    progress = float(rc.get("progress", 0.5))
    target = engine.calculate_target_alpha(final_regime, progress)

    if final_regime != current:
        engine.execute_regime_change(final_regime, progress)
        print(f"[Backfill] REGIME: {current} → {final_regime} ({rc['regime_evidence']})")
        dingtalk.regime_change(current, final_regime, rc["regime_evidence"], target)

    # 引导路径豁免发单: 回溯属一次性状态引导, 不调用 run_cycle 的统一发单钩子,
    # 不产生 TradeSync 交易指令
    sm.set("alpha.regime_progress", progress)
    sm.set("alpha.deferred_build", False)
    sm.set("alpha.current", target)
    sm.set("alpha.target", target)
    sm.set("alpha.transition_progress", 1.0)
    sm.set("alpha.last_change_at", datetime.utcnow().isoformat() + "Z")
    sm.set("regime.cooldown_remaining", 0)
    sm.set("regime.stability_counter", 0)
    # 回溯为一次性引导: 统一清除待确认提议 (含 final_regime == current 路径)
    engine.clear_pending_proposal()

    print(f"[Backfill] Alpha 直接设为 {target:+.4f} (regime={final_regime})")

    # 回溯完成 → 输出编号帖子 (与正式分析一致的格式, 发钉钉 + 写 Promo 桥文件)
    post_no = int(sm.get("runtime.post_count", 0)) + 1
    post_text = dingtalk.analysis(
        rc.get("summary", ""),
        final_regime,
        rc.get("conf", "medium"),
        target,
        rc.get("signal_board", []),
        post_no=post_no,
        engine_regime=final_regime,
    )
    sm.set("runtime.post_count", post_no)
    _write_promo_post(cfg, post_text, post_no, final_regime, target)

    # 写一条综合 memory 条目
    memory.append_alpha({
        "date": datetime.utcnow().isoformat() + "Z",
        "alpha": target,
        "regime": final_regime,
        "target_alpha": target,
    })

    summaries = [c["summary"] for c in collected[-5:] if c["summary"]]
    all_signals = {}
    for c in collected:
        for s in c.get("signal_board", []):
            cat = s.get("category", "?")
            all_signals.setdefault(cat, []).append(s)

    entry = [f"**[回溯完成] {final_regime} (alpha={target:+.4f})**"]
    if summaries:
        entry.append(f"最近分析: {'; '.join(summaries[:3])}")
    entry.append(f"\nRegime投票: {regime_votes} → {final_regime}")
    entry.append(f"覆盖 {len(all_tweets)} 条推文, {len(collected)} 批分析")

    top_signals = []
    for cat, sigs in all_signals.items():
        if sigs:
            top_signals.append(sigs[-1])
    if top_signals:
        entry.append("\n**代表性信号**:")
        for s in top_signals[:5]:
            entry.append(f"- [{s.get('category','?')}] {s.get('name','?')}: {s.get('detail','')}")

    memory.append_entry("\n".join(entry))

    sm.save()
    print(f"\n[Backfill] 完成 — regime={final_regime}, alpha={target:+.4f}")
    print("=" * 60)
    return True


# ---- 智能采样 ----

_REGIME_KEYWORDS = [
    "bottom", "top", "cycle", "regime", "capitulation", "euphoria",
    "bull", "bear", "accumulation", "distribution", "MVRV", "NUPL",
    "SOPR", "ETF flow", "record", "unprecedented", "historic",
    "extreme", "all-time", "breakdown", "reversal", "trend change",
    "sell-off", "rally", "crash", "floor", "ceiling",
]


def _smart_sample(tweets: list[dict], max_samples: int) -> list[dict]:
    """从推文列表中智能采样：关键推文全保留，常规推文均匀抽样."""
    if len(tweets) <= max_samples:
        return tweets

    key_tweets = []
    regular = []
    for t in tweets:
        content = (t.get("content", "") or "").lower()
        if any(kw.lower() in content for kw in _REGIME_KEYWORDS):
            key_tweets.append(t)
        else:
            regular.append(t)

    remaining = max_samples - len(key_tweets)
    if remaining <= 0:
        key_tweets.sort(key=lambda t: t.get("id", ""))
        return key_tweets[:max_samples]

    if len(regular) <= remaining:
        result = key_tweets + regular
        result.sort(key=lambda t: t.get("id", ""))
        return result

    step = max(1, len(regular) // remaining)
    sampled_regular = regular[::step][:remaining]

    result = key_tweets + sampled_regular
    result.sort(key=lambda t: t.get("id", ""))
    return result


def run_first_analysis(components: dict, max_samples: int = 100) -> bool:
    """首次正式运行: 抓取尽量多的历史推文并分析, 确认当前市场状态与 alpha.

    与 backfill 的区别: 不做分批投票重建初始状态, 而是按正常分析流程走一次 —
    分批分析 (尽量多分析内容), 用最新批次的结果确认 regime (走过渡图校验),
    并把 alpha 直接定位到当前目标值.
    """
    c = components
    cfg = c["cfg"]
    sm = c["state"]
    fetcher = c["fetcher"]
    memory = c["memory"]
    analyzer = c["analyzer"]
    engine = c["engine"]
    evidence = c["evidence"]
    knowledge = c["knowledge"]
    dingtalk = c["dingtalk"]

    backfill_cfg = cfg.get("backfill", {})
    batch_size = backfill_cfg.get("batch_size", 10)
    bulk_limit = backfill_cfg.get("bulk_fetch_limit", 2000)
    max_samples = min(max_samples, backfill_cfg.get("max_analysis_samples", 200))

    print(f"\n{'='*60}")
    print("[首次分析] 抓取尽量多的历史推文, 确认当前市场状态与 alpha")
    print("=" * 60)

    # 1. 获取数据: snscrape 批量 > 网页 > 文件
    total = fetcher.count_tweets()
    if total < 20:
        bulk_count = fetcher.fetch_bulk(limit=bulk_limit)
        total = fetcher.count_tweets()
        print(f"[首次分析] snscrape 批量抓取: +{bulk_count} 条, 总计 {total} 条")
    if total < 5:
        print("[首次分析] 网页补充抓取...")
        fetcher.fetch()
        total = fetcher.count_tweets()
    if total == 0:
        print("[首次分析] 无历史推文, 等待每日轮询抓取")
        return False

    # 2. 智能采样
    all_tweets = fetcher.load_all_tweets()
    all_tweets.sort(key=lambda t: t.get("id", ""))
    samples = _smart_sample(all_tweets, max_samples)
    print(f"[首次分析] 采样 {len(samples)} 条 (覆盖 {len(all_tweets)} 条全量)")

    # 3. 分批分析 — 每批一次 DeepSeek, 用最后一批结果确认当前状态
    batches = [samples[i:i+batch_size] for i in range(0, len(samples), batch_size)]
    kb = knowledge.load_knowledge_base()
    last_analysis = None
    for bi, batch in enumerate(batches, 1):
        print(f"[首次分析] 批次 {bi}/{len(batches)} ({len(batch)} 推文) ...")
        ctx = memory.get_context_for_ai()
        a = analyzer.analyze(batch, ctx, kb,
                             market_state=build_market_state(c, persist_ma=False))
        if a:
            cp = a.get("cycle_position")
            if not _valid_cycle_position(cp):
                print(f"[首次分析]   非法 cycle_position={cp!r}, 拒绝本批")
                continue
            last_analysis = a
            print(f"[首次分析]   cycle_position={a.get('cycle_position','?')}, "
                  f"progress={a.get('regime_progress','?')}, "
                  f"conf={a.get('cycle_confidence','?')}")
            sm.set("runtime.last_deepseek_at", datetime.utcnow().isoformat() + "Z")
            sm.update_runtime()
    if not last_analysis:
        print("[首次分析] 分析失败, 等待每日轮询重试")
        sm.save()
        return False

    # 4. 证据累积 + regime 变更
    # 首次确认: 初始状态为默认值 (无历史连续性), 直接执行 AI 判定,
    # 不走过渡图/证据共识校验 (否则 BEAR → BEAR_BOTTOM 等会被非法转换拦截)
    cp = last_analysis.get("cycle_position", "BEAR")
    conf = last_analysis.get("cycle_confidence", "low")
    scores = last_analysis.get("evidence_scores", {})
    meta = last_analysis.get("meta", {})
    rp = last_analysis.get("regime_progress")
    if rp is not None:
        sm.set("alpha.regime_progress", float(rp))
    progress = float(sm.get("alpha.regime_progress", 0.5))

    for cat, score in scores.items():
        evidence.update(cp, cat, float(score))

    current = engine.get_regime()
    if cp != current:
        alpha_before = engine.get_alpha()
        new_regime = engine.execute_regime_change(cp, progress)
        print(f"[首次分析] REGIME: {current} → {new_regime}")
        dingtalk.regime_change(current, new_regime,
                               last_analysis.get("regime_evidence", ""),
                               engine.get_alpha(),
                               engine.calculate_target_alpha(new_regime, progress),
                               old_alpha=alpha_before)
    else:
        print(f"[首次分析] Regime 保持 {cp}")

    # 5. alpha 直接定位到当前目标 (首次不走步进)
    # 引导路径豁免发单: 首次分析属状态引导, 不调用 run_cycle 的统一发单钩子
    target = engine.calculate_target_alpha(engine.get_regime(), progress)
    sm.set("alpha.deferred_build", False)
    sm.set("alpha.current", target)
    sm.set("alpha.target", target)
    sm.set("alpha.transition_progress", 1.0)
    sm.set("alpha.last_change_at", datetime.utcnow().isoformat() + "Z")
    sm.set("regime.cooldown_remaining", 0)
    print(f"[首次分析] Alpha 定位为 {target:+.4f} "
          f"(regime={engine.get_regime()}, progress={progress:.2f})")

    # 6. 发编号帖子 (钉钉 + Promo 桥文件)
    post_no = int(sm.get("runtime.post_count", 0)) + 1
    post_text = dingtalk.analysis(
        last_analysis.get("summary", ""), cp, conf, target,
        last_analysis.get("signal_board", []), post_no=post_no,
        engine_regime=engine.get_regime())
    sm.set("runtime.post_count", post_no)
    _write_promo_post(cfg, post_text, post_no, cp, target)

    # 7. memory 条目
    memory.append_alpha({
        "date": datetime.utcnow().isoformat() + "Z",
        "alpha": target,
        "regime": cp,
        "target_alpha": target,
    })
    entry_parts = [f"**[首次分析] {cp} (alpha={target:+.4f})**"]
    if last_analysis.get("summary"):
        entry_parts.append(last_analysis["summary"])
    entry_parts.append(f"Regime证据: {last_analysis.get('regime_evidence', '')}")
    memory.append_entry("\n".join(entry_parts))

    sm.save()
    print(f"[首次分析] 完成 — regime={cp}, alpha={target:+.4f}, 帖子 No.{post_no}")
    print("=" * 60)
    return True


# ---- 故障语义: outage 标记 + 失败推文重放 ----

def _set_outage(components: dict, reason: str, detail: str = "") -> dict:
    """进入/维持数据源故障态 (runtime.outage).

    首次进入按 reason 钉钉告警一次; 故障持续期间只更新 reason/detail,
    保留原 since 且不重复告警; 恢复由 _clear_outage 清除。
    """
    sm = components["state"]
    current = sm.get("runtime.outage") or {}
    if isinstance(current, dict) and current:
        updated = dict(current)
        if updated.get("reason") != reason or updated.get("detail") != str(detail)[:200]:
            updated["reason"] = reason
            updated["detail"] = str(detail)[:200]
            sm.set("runtime.outage", updated)
        return updated
    record = {"since": datetime.utcnow().isoformat() + "Z",
              "reason": reason, "detail": str(detail)[:200]}
    sm.set("runtime.outage", record)
    print(f"[Outage] 数据源故障: {reason} | {record['detail']} (since {record['since']})")
    dingtalk = components.get("dingtalk")
    if dingtalk is not None:
        try:
            dingtalk.alert("数据源故障",
                           f"原因: {reason}\n详情: {record['detail']}\n开始: {record['since']}")
        except Exception as exc:
            print(f"[Outage] 故障告警发送失败 (不影响主流程): {exc}")
    return record


def _clear_outage(components: dict, detail: str = "") -> bool:
    """清除故障态 (抓取/分析已恢复); 仅在实际处于故障态时恢复告警一次.

    故障时长不计入周期钟: 同步把自然日锚点重置为 now —— 与 WP5 "故障轮不推进"
    语义一致, 恢复轮 progress 增量≈0 (不补记故障期间的天数)。
    """
    sm = components["state"]
    current = sm.get("runtime.outage") or {}
    if not (isinstance(current, dict) and current):
        return False
    sm.set("runtime.outage", {})
    sm.set("runtime.last_tick_at", datetime.utcnow().isoformat() + "Z")
    reason = current.get("reason", "?")
    since = current.get("since", "?")
    suffix = f", {detail}" if detail else ""
    print(f"[Outage] 数据源恢复: {reason} → OK (since {since}{suffix})")
    dingtalk = components.get("dingtalk")
    if dingtalk is not None:
        try:
            dingtalk.alert("数据源恢复",
                           f"故障原因: {reason}\n开始: {since}\n"
                           f"本轮: {detail or '抓取/分析成功'}")
        except Exception as exc:
            print(f"[Outage] 恢复告警发送失败 (不影响主流程): {exc}")
    return True


def _alert_pending_drops(components: dict, pending, stats: dict) -> None:
    """队列超龄/超上限丢弃时告警一次 (发送失败不影响主流程)."""
    dropped = int(stats.get("expired", 0)) + int(stats.get("overflow", 0))
    if not dropped:
        return
    body = (f"超龄(>{pending.max_age_days:g}天)丢弃 {stats.get('expired', 0)} 条 | "
            f"超上限({pending.max_items})丢弃 {stats.get('overflow', 0)} 条")
    print(f"[Pending] {body}")
    dingtalk = components.get("dingtalk")
    if dingtalk is not None:
        try:
            dingtalk.alert("推文重放队列丢弃", body)
        except Exception as exc:
            print(f"[Pending] 丢弃告警发送失败 (不影响主流程): {exc}")


def _enqueue_pending_analysis(components: dict, tweets: list,
                              reason: str = "analysis_failed") -> dict:
    """把本轮未被分析的推文 (分析失败/未进窗口) 落入待分析队列.

    队列 I/O 异常只记日志不抛出; 超龄/超上限丢弃时告警一次。
    """
    pending = components.get("pending")
    if pending is None:
        return {}
    ids = [str(t.get("id", "") or "") for t in (tweets or []) if t.get("id")]
    try:
        stats = pending.add(ids, reason=reason)
    except Exception as exc:
        print(f"[Pending] 队列写入失败 (不影响主流程): {exc}")
        return {}
    if stats.get("added"):
        print(f"[Pending] 入队 {stats['added']} 条 "
              f"(原因: {reason}, 队列 {stats['total']} 条)")
    _alert_pending_drops(components, pending, stats)
    return stats


def _merge_pending_replay(components: dict, new_tweets: list, locked: bool) -> list:
    """把待分析队列中的推文从 tweets.jsonl 取回, 与当轮新推文合并 (优先重放).

    合并顺序: 当轮新推文在前、重放项在后 —— `Analyzer._format_tweets` 只取
    `tweets[-20:]`, 该顺序保证重放项优先进入分析窗口; 未进窗口的部分由
    run_cycle 保留/回队列下轮重试 (不静默丢弃)。
    锁定轮/队列为空时原样返回; 已不在 tweets.jsonl 的 ID 直接移出;
    队列 I/O 异常只记日志不抛出 (返回原列表)。
    """
    if locked:
        return new_tweets
    pending = components.get("pending")
    if pending is None:
        return new_tweets

    def _on_corrupt(message: str):
        dingtalk = components.get("dingtalk")
        if dingtalk is not None:
            dingtalk.alert("推文重放队列损坏", message)

    try:
        queued = pending.load(on_error=_on_corrupt)
        if not queued:
            return new_tweets
        have = {str(t.get("id", "") or "") for t in new_tweets}
        ids = [it["id"] for it in queued if it["id"] not in have]
        if not ids:
            return new_tweets
        found = components["fetcher"].get_tweets_by_ids(ids)
        found_ids = {str(t.get("id", "") or "") for t in found}
        missing = [i for i in ids if i not in found_ids]
        if missing:
            pending.remove(missing)
            print(f"[Pending] {len(missing)} 条推文不在 tweets.jsonl, 已移出队列")
        if not found:
            return new_tweets
        merged = new_tweets + found
        print(f"[Pending] 重放 {len(found)} 条失败推文 (队列 {len(queued)} 条)")
        return merged
    except Exception as exc:
        print(f"[Pending] 重放队列处理失败 (不影响主流程): {exc}")
        return new_tweets


def _handled_tweet_ids(analyzer, tweets: list) -> list:
    """本轮真正被处理的推文 ID (进入 prompt 窗口的 + 被 BTC 过滤的).

    Analyzer 未记录 (兼容无该接口的 stub) 时回退为全部入参, 保持旧语义;
    未进窗口的 ID 不在返回值内 → 保留队列下轮重试。
    """
    ids = [str(t.get("id", "") or "") for t in tweets if t.get("id")]
    sent = getattr(analyzer, "last_sent_tweet_ids", None)
    filtered = getattr(analyzer, "last_filtered_tweet_ids", None)
    if sent is None or filtered is None:
        return ids
    handled = {str(i) for i in sent} | {str(i) for i in filtered}
    return [i for i in ids if i in handled]


def _all_tweets_filtered(analyzer) -> bool:
    """本批是否全部被 BTC 过滤 (无推文进入 prompt 但有被过滤项).

    该情形是"无有效新闻"而非故障: 不入队、不置 outage。
    """
    sent = getattr(analyzer, "last_sent_tweet_ids", None)
    filtered = getattr(analyzer, "last_filtered_tweet_ids", None)
    if sent is None or filtered is None:
        return False
    return not sent and bool(filtered)


def run_cycle(components: dict) -> bool:
    """运行一次完整分析循环。返回是否有新推文被分析."""
    c = components
    cfg = c["cfg"]
    memory = c["memory"]
    sm = c["state"]
    fetcher = c["fetcher"]
    analyzer = c["analyzer"]
    engine = c["engine"]
    evidence = c["evidence"]
    knowledge = c["knowledge"]
    tradesync = c["tradesync"]
    datafeed = c["datafeed"]
    dingtalk = c["dingtalk"]
    review = c["review"]

    print_status(c)

    # 0. 分析锁: 距上次成功 DeepSeek 分析不足 min_analysis_interval_hours 小时 → 本轮跳过抓取/分析
    lock_hours = float(cfg.get("schedule", {}).get("min_analysis_interval_hours", 0) or 0)
    locked = False
    last_ds = sm.get("runtime.last_deepseek_at", "")
    if lock_hours > 0 and last_ds:
        try:
            last_dt = datetime.fromisoformat(last_ds.replace("Z", "+00:00")).replace(tzinfo=None)
            elapsed_h = (datetime.utcnow() - last_dt).total_seconds() / 3600
            if elapsed_h < lock_hours:
                locked = True
                daily = cfg.get("schedule", {}).get("daily_time", "")
                lock_msg = f"[Lock] 距上次 DeepSeek 分析 {elapsed_h:.1f}h < {lock_hours:.0f}h, 本轮跳过抓取"
                if daily:
                    lock_msg += f" (下次 {_schedule_desc(cfg)})"
                print(lock_msg)
        except ValueError:
            pass

    # 1. 获取价格
    btc_price = datafeed.get_price()
    if btc_price:
        print(f"[BTC] ${btc_price:,.2f}")
        review.record_price(btc_price, engine.get_regime(), engine.get_alpha())

    # 1.1 发单一致性基准: btc_price 确定后、任何 alpha 变更之前记录本轮起点
    alpha_cycle_start = engine.get_alpha()

    # 1.5 预测审计 (幂等): 到期预测写回结果侧车。
    # 原先只在周日蒸馏时执行, 而当前调度为周二/周四 → 永不触发, 这里改为每轮执行。
    if btc_price:
        try:
            audit = knowledge.audit_predictions(btc_price)
            if audit.get("new_audited") or audit.get("skipped"):
                message = f"新增审计 {audit.get('new_audited', 0)} 条"
                if audit.get("skipped"):
                    message += f" | 跳过 {audit['skipped']} 条(无入场价)"
                message += f" | 命中率 {audit.get('hit_rate', 0):.0%}"
                print(f"[Audit] {message}")
        except Exception as e:
            print(f"[Audit] 预测审计异常 (不影响本轮分析): {e}")

    # 2. 抓取新推文 (区分"数据源故障"与"无新闻": 异常 → fetch_error)
    new_tweets = []
    fetch_error = False
    fetched_ok = False
    fetch_detail = ""
    print("\n--- Fetch ---")
    if locked:
        print("[Fetch] Skipped (analysis lock)")
    else:
        try:
            new_tweets = fetcher.fetch()
            fetched_ok = True
        except Exception as e:
            fetch_error = True
            fetch_detail = f"{type(e).__name__}: {e}"
            print(f"[Fetch] Error: {e}")
            new_tweets = []

    # 2.5 失败推文重放: 队列非空时从 tweets.jsonl 取回并与当轮新推文合并
    new_tweets = _merge_pending_replay(c, new_tweets, locked)

    has_analysis = False
    analysis = None
    regime_changed_this_cycle = False
    idle_cycle = not new_tweets

    if new_tweets:
        # 3. 分析 (只在有新推文/重放推文时)
        print("\n--- Analyze ---")
        kb = knowledge.load_knowledge_base()
        ctx = memory.get_context_for_ai()
        market_state = build_market_state(c, btc_price)
        _record_ma_state(sm, market_state)
        try:
            analysis = analyzer.analyze(new_tweets, ctx, kb, market_state=market_state)
        except Exception as exc:
            print(f"  Analysis error: {type(exc).__name__}: {exc}")
            analysis = None

        if analysis:
            sm.set("runtime.last_deepseek_at", datetime.utcnow().isoformat() + "Z")
            pending_store = c.get("pending")
            if pending_store is not None:
                try:
                    # 只移除真正被处理的 (进入 prompt 窗口 + 被 BTC 过滤);
                    # 未进 20 条窗口的保留队列下轮重试 (防静默丢推文)
                    handled_ids = _handled_tweet_ids(analyzer, new_tweets)
                    handled_set = set(handled_ids)
                    removed = pending_store.remove(handled_ids)
                    if removed:
                        print(f"[Pending] 分析成功, 移出队列 {removed} 条")
                    unhandled = [t for t in new_tweets
                                 if str(t.get("id", "") or "") not in handled_set]
                    if unhandled:
                        print(f"[Pending] {len(unhandled)} 条未进分析窗口, 留队列下轮重试")
                        _enqueue_pending_analysis(c, unhandled, "window_overflow")
                except Exception as exc:
                    print(f"[Pending] 队列清理失败 (不影响主流程): {exc}")
            if fetched_ok:
                _clear_outage(c, "抓取/分析成功")
            else:
                _set_outage(c, "fetch_error", fetch_detail)

        if analysis:
            cp = analysis.get("cycle_position", "BEAR")
            conf = analysis.get("cycle_confidence", "low")
            scores = analysis.get("evidence_scores", {})
            meta = analysis.get("meta", {})
            summary = analysis.get("summary", "")
            regime_evidence = analysis.get("regime_evidence", "")

            print(f"  cycle_position: {cp} (confidence: {conf})")
            print(f"  evidence_scores: {json.dumps(scores)}")
            print(f"  quality: {meta.get('analysis_quality', '?')}")

            # 4. 漂移检测
            knowledge.log_drift_meta(meta, cp)
            drift_alerts = knowledge.check_drift()
            if drift_alerts:
                print("\n[DRIFF ALERTS]")
                for a in drift_alerts:
                    print(f"  ⚠ {a}")
                    dingtalk.alert("风格漂移", a)

            # 5. 证据累积 + Regime 变更
            print("\n--- Alpha Engine ---")
            for cat, score in scores.items():
                evidence.update(cp, cat, float(score))

            current_regime = engine.get_regime()
            # 连续同向确认 (alpha.stability.required_confirmations):
            # 同一提议需连续 N 轮"有新推文且分析成功"才执行; 被其它门拒绝时 pending 保留;
            # idle 轮次不计数不重置; 提议回到当前 regime 或执行成功 → 清除。
            if cp == current_regime:
                engine.clear_pending_proposal()
                ok, reason = True, ""
            else:
                pending = engine.note_regime_proposal(cp)
                if engine.proposal_ready(pending):
                    ok, reason = engine.request_regime_change(
                        cp, scores, conf, meta.get("analysis_quality", 8))
                    if ok:
                        engine.clear_pending_proposal()
                else:
                    count = engine.pending_count(pending)
                    ok, reason = False, (f"待确认 {count}/"
                                         f"{engine.required_confirmations()}")
            # 周期内进度 → 动态目标 (状态感知):
            # 仅当提议被接受或 AI 描述的正是当前 regime 时才采用新进度;
            # 被拒且指向其它 regime 时保留旧值, 不参与 target 计算.
            old_progress = float(sm.get("alpha.regime_progress", 0.5))
            progress = _select_progress(analysis.get("regime_progress"), ok, cp,
                                        current_regime, old_progress)
            if progress != old_progress:
                sm.set("alpha.regime_progress", progress)

            if ok and cp != current_regime:
                alpha_before = engine.get_alpha()
                new_regime = engine.execute_regime_change(cp, progress)
                regime_changed_this_cycle = True
                print(f"  [REGIME] {current_regime} → {new_regime}")
                print(f"           原因: {regime_evidence}")
                dingtalk.regime_change(current_regime, new_regime, regime_evidence,
                                       engine.get_alpha(),
                                       engine.calculate_target_alpha(new_regime, progress),
                                       old_alpha=alpha_before)
            elif cp != current_regime:
                print(f"  [REGIME] 请求 {cp} 被拒绝: {reason}")

            if cp != current_regime and not ok:
                print(f"  regime_progress: {progress:.2f} (提议被拒, 保留旧值)")
            else:
                print(f"  regime_progress: {progress:.2f}")

            # 6. 跟踪预测
            knowledge.log_prediction(cp, conf, btc_price)

            has_analysis = True
        elif _all_tweets_filtered(analyzer):
            # 全部被 BTC 过滤 ≠ 故障: 视为无有效新闻, 走 idle (不入队/不置 outage);
            # 同时把被过滤条目从重放队列移出 (与成功轮 sent∪filtered 语义对齐,
            # 否则纯 altcoin 条目会每轮重放且永远失败)
            print("  [Analyzer] 本批全部被 BTC 过滤跳过 (视为无有效新闻)")
            pending_store = c.get("pending")
            if pending_store is not None:
                try:
                    skipped_ids = getattr(analyzer, "last_filtered_tweet_ids", []) or []
                    removed = pending_store.remove(skipped_ids)
                    if removed:
                        print(f"[Pending] 过滤跳过, 移出队列 {removed} 条")
                except Exception as exc:
                    print(f"[Pending] 队列清理失败 (不影响主流程): {exc}")
            idle_cycle = True
        else:
            print("  Analysis failed, skipping evidence update.")
            stats = _enqueue_pending_analysis(c, new_tweets, "analysis_failed")
            detail = "AI 分析失败"
            if stats.get("total") is not None:
                detail += (f", {stats.get('added', 0)} 条入重放队列 "
                           f"(队列 {stats['total']} 条)")
            _set_outage(c, "analysis_failed", detail)
            sm.update_runtime()
            sm.save()
            return False

    if idle_cycle:
        # 无新推文 (或全部被过滤跳过)
        if fetch_error:
            # 数据源故障: 视为数据缺失, 本轮不推进周期钟 (alpha/progress/cooldown/
            # stability/evidence 全部冻结), 价格/审计/MA 摘要与 state 保存照常
            print("\n--- Idle (fetch_error) ---")
            _set_outage(c, "fetch_error", fetch_detail)
            print("  [Outage] 数据缺失: 本轮不推进 "
                  "alpha/progress/cooldown/stability/evidence")
        else:
            # 基于4年周期时间推进 alpha (按自然日折算, 与调度频率解耦;
            # cooldown/stability 仍按轮次=决策机会数, 与自然日推进相互独立)
            print("\n--- Idle (no new tweets) ---")
            if fetched_ok:
                _clear_outage(c, "抓取成功 (无新推文)")
            old_alpha = engine.get_alpha()
            new_alpha, alpha_changed = engine.tick_alpha()
            evidence.decay_all()
            engine.tick_cooldown()
            engine.tick_stability()

            if alpha_changed:
                progress = float(sm.get("alpha.regime_progress", 0.5))
                target = engine.calculate_target_alpha(engine.get_regime(), progress)
                print(f"  Alpha (time-based): {old_alpha:+.4f} → {new_alpha:+.4f} "
                      f"(target={target:+.2f}, progress={progress:.2f})")
                memory.append_alpha({
                    "date": datetime.utcnow().isoformat() + "Z",
                    "alpha": new_alpha,
                    "regime": engine.get_regime(),
                    "target_alpha": target,
                    "btc_price": btc_price,
                    "note": "时间推进: 无推文时按4年周期(自然日折算)推进alpha",
                })
                dingtalk.alpha_change(old_alpha, new_alpha, engine.get_regime(),
                                      btc_price, target)

        # 空闲周期也刷新 K 线趋势摘要 (Web 展示用): 只读历史不写 ma_history,
        # DataFeed 不可用/异常不阻塞 alpha 时间推进.
        try:
            ma_summary = _record_ma_state(
                sm, build_market_state(c, btc_price, persist_ma=False))
            if ma_summary.get("available"):
                print(f"  MA 趋势: {ma_summary.get('zone')} "
                      f"(截至 {ma_summary.get('as_of')})")
        except Exception as exc:
            print(f"[MA] 空闲周期均线摘要更新失败 (不影响本轮): {exc}")

        # 发单一致性: 时间推进改 alpha 也要与 TradeSync 指令保持一致
        _send_alpha_order_if_changed(engine, tradesync, alpha_cycle_start,
                                     btc_price, reason="idle_tick")

        if not fetch_error:
            # 故障轮不计入分析计数, 仅保存状态 (outage/MA 摘要)
            sm.update_runtime()
        sm.save()
        return False

    # 6.5 右侧纪律兜底: 仓位符号与周期方向侧冲突 → 先平仓, 本轮不再步进
    clamp_old = engine.get_alpha()
    order_reason = "step"
    if engine.enforce_side_constraint():
        order_reason = "side_fix"
        clamp_new = engine.get_alpha()
        clamp_target = engine.calculate_target_alpha(engine.get_regime(), progress)
        print(f"  [SIDE-FIX] Alpha {clamp_old:+.4f} → {clamp_new:+.4f} "
              f"(regime={engine.get_regime()} 方向冲突, 先平仓, 目标 {clamp_target:+.2f})")
        memory.append_alpha({
            "date": datetime.utcnow().isoformat() + "Z",
            "alpha": clamp_new,
            "regime": engine.get_regime(),
            "target_alpha": clamp_target,
            "btc_price": btc_price,
            "note": "右侧纪律: 方向冲突平仓",
        })
        dingtalk.alpha_change(clamp_old, clamp_new, engine.get_regime(), btc_price, clamp_target)

    # 7. Alpha 推进一步 (仅在有新分析 + 非低置信时)
    old_alpha = engine.get_alpha()
    alpha_changed = False
    new_alpha = old_alpha

    if clamp_old != engine.get_alpha():
        # 已平仓, 本轮不步进, 下轮再向新方向建仓
        print(f"  Alpha: 本轮已平仓至 {old_alpha:+.4f}, 下轮开始向目标建仓")
        engine.tick_cooldown()
        engine.tick_stability()
    elif regime_changed_this_cycle:
        # 分两步快速换仓: 变更日只平仓归零, 次日再由 step_alpha 直接定位到目标
        order_reason = ("deferred_build"
                        if sm.get("alpha.deferred_build", False)
                        else "regime_change")
        target_alpha = engine.calculate_target_alpha(engine.get_regime(), progress)
        print(f"  Alpha: 变更日平仓至 {old_alpha:+.4f} (deferred_build), "
              f"次日定位到 {target_alpha:+.4f}")
        memory.append_alpha({
            "date": datetime.utcnow().isoformat() + "Z",
            "alpha": old_alpha,
            "regime": engine.get_regime(),
            "target_alpha": target_alpha,
            "btc_price": btc_price,
            "note": "周期变更日先平仓, 次日开始建仓",
        })
        engine.tick_cooldown()
        engine.tick_stability()
    elif conf == "low":
        order_reason = "low_confidence"
        print(f"  Alpha: 保持不变 (置信度 low, 锁定)")
        engine.tick_cooldown()
        engine.tick_stability()
    else:
        new_alpha, alpha_changed = engine.step_alpha()
        engine.tick_cooldown()
        engine.tick_stability()

        if alpha_changed:
            print(f"  Alpha: {old_alpha:+.4f} → {new_alpha:+.4f} "
                  f"(target={engine.calculate_target_alpha(engine.get_regime(), progress):+.2f})")
            memory.append_alpha({
                "date": datetime.utcnow().isoformat() + "Z",
                "alpha": new_alpha,
                "regime": engine.get_regime(),
                "target_alpha": engine.calculate_target_alpha(engine.get_regime(), progress),
                "btc_price": btc_price,
            })
            dingtalk.alpha_change(old_alpha, new_alpha, engine.get_regime(), btc_price,
                                  engine.calculate_target_alpha(engine.get_regime(), progress))

    # 8. 发送交易指令 (统一发单钩子: 任何 alpha 变更路径都补发, 每轮至多一次;
    #    多点变更只发本轮最终值, 客户端内存去重)
    _send_alpha_order_if_changed(engine, tradesync, alpha_cycle_start, btc_price,
                                 reason=order_reason)

    # 9. 更新 memory (仅当有新分析)
    if has_analysis and analysis:
        post_no = int(sm.get("runtime.post_count", 0)) + 1
        post_text = dingtalk.analysis(
            analysis.get("summary", ""),
            analysis.get("cycle_position", "?"),
            analysis.get("cycle_confidence", "?"),
            new_alpha,
            analysis.get("signal_board", []),
            post_no=post_no,
            engine_regime=engine.get_regime(),
        )
        sm.set("runtime.post_count", post_no)
        _write_promo_post(cfg, post_text, post_no,
                          analysis.get("cycle_position", "?"), new_alpha)

        entry_parts = [f"**{analysis.get('summary', 'N/A')}**"]
        entry_parts.append(f"Regime: {analysis.get('cycle_position', '?')} → alpha → {new_alpha:+.4f}")
        entry_parts.append(f"Regime证据: {analysis.get('regime_evidence', '')}")

        s = analysis.get("signal_board", [])
        if s:
            entry_parts.append("\n**信号板**:")
            for si in s[:5]:
                entry_parts.append(f"- [{si.get('category','?')}] {si.get('name','?')}: "
                                   f"{si.get('signal',0):+.2f} — {si.get('detail','')}")

        pos = analysis.get("position_narrative", "")
        if pos:
            entry_parts.append(f"\n**仓位叙述**: {pos}")

        memory.append_entry("\n".join(entry_parts))

        # 指标提取
        for si in s:
            name = si.get("name", "")
            if name:
                memory.add_metric(name, si.get("signal", 0))

    # 10. 每周蒸馏 (按 knowledge.distill 计划; 分析周期错过时下一轮补偿)
    now = datetime.utcnow()
    if knowledge.distill_due(sm, now):
        last_distill = sm.get("runtime.last_distill_at", "")
        if not last_distill or last_distill[:10] != now.strftime("%Y-%m-%d"):
            print("\n--- Weekly Distill ---")
            knowledge.compress_memory(memory)
            knowledge.distill(memory, sm, btc_price)
            sm.set("runtime.last_distill_at", now.isoformat() + "Z")

    # 11. 月度复盘
    if review.should_review():
        review_result = review.run_review()
        if review_result:
            review.apply_calibration(review_result)
            if review_result.get("calibration", {}).get("warnings"):
                for w in review_result["calibration"]["warnings"]:
                    dingtalk.alert("月度复盘", w)

    # 12. 保存状态
    sm.update_runtime()
    sm.save()

    return has_analysis


_WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _schedule_desc(cfg: dict) -> str:
    """格式化调度描述, 如 '周二/周四 12:25 北京时间' 或 '每天 12:25'."""
    sched = cfg.get("schedule", {})
    daily = str(sched.get("daily_time", "") or "").strip()
    weekdays = [int(w) for w in (sched.get("weekdays") or []) if str(w).isdigit()]
    if not daily:
        return f"每 {int(sched.get('poll_interval_seconds', 86400))} 秒"
    if weekdays:
        names = "/".join(_WEEKDAY_NAMES[w] for w in sorted(weekdays) if 0 <= w <= 6)
        return f"{names} {daily} 北京时间"
    return f"每天 {daily} 北京时间"


def _seconds_until_next_run(cfg: dict, now_utc: datetime) -> float:
    """计算距下次运行的秒数.

    schedule.daily_time 指定运行时间 (北京时间, 如 "12:25");
    schedule.weekdays 限定星期 (0=周一...6=周日), 空/未设置=每天;
    未设置 daily_time 时回退到 poll_interval_seconds 固定间隔.
    """
    sched = cfg.get("schedule", {})
    daily = str(sched.get("daily_time", "") or "").strip()
    if not daily or ":" not in daily:
        return float(sched.get("poll_interval_seconds", 86400))
    try:
        hh, mm = map(int, daily.split(":"))
    except ValueError:
        return float(sched.get("poll_interval_seconds", 86400))

    tz_offset = float(sched.get("utc_offset_hours", 8))  # 默认北京时间 UTC+8
    weekdays = [int(w) for w in (sched.get("weekdays") or []) if str(w).isdigit()]

    now_local = now_utc + timedelta(hours=tz_offset)
    # 从今天起最多看 7 天
    for d in range(8):
        candidate = now_local + timedelta(days=d)
        if weekdays and candidate.weekday() not in weekdays:
            continue
        target = candidate.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target > now_local:
            return (target - now_local).total_seconds()
    # 兜底: 找不到匹配星期 → 用固定间隔
    return float(sched.get("poll_interval_seconds", 86400))


def _run_test_ai(c: dict, urls_only: bool = False):
    """测试模式: 读取推文 → 解析 → 发给 AI 分析 → 打印结果.

    不存历史记录、不写 tweets.jsonl、不发钉钉/Promo、不改状态.
    urls_only=True 时只传推文 URL, 不传正文.
    """
    print("\n" + "=" * 60)
    mode = "只传 URL" if urls_only else "传正文+URL"
    print(f"[Test-AI] 测试模式开始 ({mode}, 只读, 不保存任何数据)")
    print("=" * 60)

    memory = c["memory"]
    analyzer = c["analyzer"]
    knowledge = c["knowledge"]
    fetcher = c["fetcher"]

    # urls_only 模式: 临时关闭正文传输 (不落盘)
    if urls_only:
        analyzer.send_tweet_content = False
        analyzer.vision_enabled = False

    # 1. 读取 x.com 推文 (不落盘)
    tweets = fetcher.preview_live()
    if not tweets:
        print("[Test-AI] 没有读取到推文, 退出")
        return

    # 2. 组装上下文 (只读, 不修改)
    ctx = memory.get_context_for_ai()
    kb = knowledge.load_knowledge_base()
    print(f"[Test-AI] 上下文: 记忆 {len(ctx)} chars, 知识库 {len(kb)} chars")

    # 3. 发给 AI 分析
    print("\n--- Test-AI: 调用 AI 分析 ---")
    price = None
    try:
        price = c["datafeed"].get_price()
        if price:
            print(f"[Test-AI] 当前 BTC 价格: ${price:,.2f}")
    except Exception:
        pass
    analysis = analyzer.analyze(tweets, ctx, kb,
                                market_state=build_market_state(c, price,
                                                                persist_ma=False))
    if not analysis:
        print("[Test-AI] AI 分析失败")
        return

    # 4. 打印结果
    print("\n--- Test-AI: 分析结果 ---")
    print(json.dumps(analysis, ensure_ascii=False, indent=2))

    print("\n" + "=" * 60)
    print("[Test-AI] 测试完成 — 未保存任何数据")
    print("=" * 60)


# ---- 单例锁 ----

def _lock_mode_for_args(*, once: bool, backfill: bool, bulk_only, import_file,
                        test_ai: bool, status_only: bool) -> str:
    """按 CLI 语义决定锁模式; 返回 "" 表示豁免 (只读入口).

    --test-ai/--status 只读 (不写状态/帖子), 豁免锁, 允许在常驻实例运行时查询;
    其余会写 data/ 的入口 (常驻/--once/--backfill/--bulk/--import) 均持锁。
    """
    if test_ai or status_only:
        return ""
    if backfill:
        return "backfill"
    if once:
        return "once"
    if bulk_only:
        return "bulk"
    if import_file:
        return "import"
    return "daemon"


def _acquire_lock_or_exit(cfg: dict, mode: str) -> SingletonLock:
    """获取单例锁; 已有存活实例时打印+钉钉告警并 exit 非 0."""
    lock = SingletonLock(resolve_data_dir(cfg))
    ok, holder = lock.acquire(mode)
    if not ok:
        pid = (holder or {}).get("pid", "?")
        started = (holder or {}).get("started_at", "?")
        held_mode = (holder or {}).get("mode", "?")
        print(f"[Lock] 已有实例运行中 (pid={pid}, mode={held_mode}, "
              f"started_at={started}), 拒绝启动")
        try:
            DingTalk(cfg.get("dingtalk", {})).alert(
                "AlphaEngine 启动被拒",
                f"已有实例运行中 (pid={pid}, mode={held_mode}, started_at={started})")
        except Exception as exc:
            print(f"[Lock] 告警发送失败 (不影响退出): {exc}")
        sys.exit(2)
    _install_lock_cleanup(lock)
    print(f"[Lock] 已获取单例锁 (pid={os.getpid()}, mode={mode})")
    return lock


def _install_lock_cleanup(lock: SingletonLock) -> None:
    """注册退出清理: atexit 覆盖正常/异常退出; SIGTERM 覆盖被 kill (Linux)."""
    atexit.register(lock.release)

    def _handle_term(signum, frame):
        lock.release()
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, _handle_term)
    except (ValueError, OSError, AttributeError):
        pass


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print("Glassnode Alpha Engine")
        print("  python -m src.alpha              # 运行监控循环")
        print("  python -m src.alpha --once       # 只跑一次分析")
        print("  python -m src.alpha --backfill   # 强制重新回溯历史推文, 重设 alpha")
        print("  python -m src.alpha --status     # 查看当前状态")
        print("  python -m src.alpha --import <file.jsonl>  # 导入历史推文文件")
        print("  python -m src.alpha --bulk <limit>   # 批量抓取并退出")
        print("  python -m src.alpha --test-ai        # 测试: 读取推文→发给AI分析, 不存记录/不发帖")
        print("  python -m src.alpha --test-ai-urls   # 测试: 只传推文URL, 不传正文")
        sys.exit(0)

    once = "--once" in sys.argv
    status_only = "--status" in sys.argv
    backfill_force = "--backfill" in sys.argv
    test_ai = "--test-ai" in sys.argv
    test_ai_urls = "--test-ai-urls" in sys.argv
    import_file = None
    bulk_only = None

    for i, arg in enumerate(sys.argv):
        if arg == "--import" and i + 1 < len(sys.argv):
            import_file = sys.argv[i + 1]
        if arg == "--bulk" and i + 1 < len(sys.argv):
            bulk_only = int(sys.argv[i + 1])

    cfg = load_config()
    lock_mode = _lock_mode_for_args(
        once=once, backfill=backfill_force, bulk_only=bulk_only,
        import_file=import_file, test_ai=(test_ai or test_ai_urls),
        status_only=status_only)
    if lock_mode:
        _acquire_lock_or_exit(cfg, lock_mode)
    c = init_components(cfg)

    if test_ai_urls:
        _run_test_ai(c, urls_only=True)
        sys.exit(0)

    if test_ai:
        _run_test_ai(c)
        sys.exit(0)

    if bulk_only:
        count = c["fetcher"].fetch_bulk(limit=bulk_only)
        print(f"\nBulk fetch complete: {count} tweets written to tweets.jsonl")
        print("Now run: python -m src.run  to start the engine with backfill")
        sys.exit(0)

    if import_file:
        count = c["fetcher"].import_file(import_file)
        print(f"\nImported {count} tweets")
        if count == 0:
            print("Nothing imported. Check file format (JSONL or JSON array).")
        else:
            print("Now run: python -m src.run  to start the engine with backfill")
        sys.exit(0)

    if status_only:
        print_status(c)
        sys.exit(0)

    interval = int(cfg.get("schedule", {}).get("poll_interval_seconds", 1800))

    daily_time = cfg.get("schedule", {}).get("daily_time", "")
    if daily_time:
        print(f"\nGlassnode Alpha Engine started ({_schedule_desc(cfg)})")
    else:
        print(f"\nGlassnode Alpha Engine started (interval={interval}s)")
    print("=" * 60)

    # 首次启动: --backfill 才走历史回溯; 否则首次正式运行走全量分析确认状态
    if backfill_force:
        run_backfill(c, force=True)
    elif c["state"].get("runtime.analysis_count", 0) == 0:
        run_first_analysis(c)

    if once:
        run_cycle(c)
        print("\nDone.")
        return

    # 定时模式: 已有状态时, 启动后先对齐到下一个定时点再进入循环,
    # 避免启动即空转触发 [Lock] 跳过
    if daily_time and c["state"].get("runtime.analysis_count", 0) > 0:
        wait = _seconds_until_next_run(cfg, datetime.utcnow())
        next_dt = datetime.utcnow() + timedelta(seconds=wait)
        print(f"[Main] 启动后等待至 {next_dt.isoformat(timespec='minutes')}Z "
              f"({_schedule_desc(cfg)}, 约 {wait/3600:.2f} 小时后)")
        time.sleep(wait)

    while True:
        try:
            run_cycle(c)
        except KeyboardInterrupt:
            print("\n[Main] Shutting down gracefully...")
            c["state"].save(force=True)
            print("[Main] State saved. Goodbye.")
            break
        except Exception as e:
            print(f"[Main] Cycle error: {e}")
            import traceback
            traceback.print_exc()

        wait = _seconds_until_next_run(cfg, datetime.utcnow())
        next_dt = datetime.utcnow() + timedelta(seconds=wait)
        next_str = next_dt.isoformat(timespec="minutes") + "Z"
        daily = cfg.get("schedule", {}).get("daily_time", "")
        if daily:
            print(f"\n[Main] Next check at {next_str} ({_schedule_desc(cfg)}, in {wait/60:.0f} min)\n")
        else:
            print(f"\n[Main] Next check at {next_str} (in {wait/60:.0f} min)\n")
        time.sleep(wait)


if __name__ == "__main__":
    main()
