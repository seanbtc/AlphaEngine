"""
Glassnode Alpha Engine — 主入口.

用法:
    python -m src.run
    nohup python3 -u -m src.run > engine.log 2>&1 &
"""
import json
import os
import sys
import time
from datetime import datetime

from src.config_loader import load_config, resolve_data_dir
from src.memory import Memory
from src.state_manager import StateManager
from src.fetcher import Fetcher
from src.analyzer import Analyzer
from src.alpha_engine import AlphaEngine, EvidenceAccumulator
from src.knowledge import Knowledge
from src.tradesync import TradeSync
from src.datafeed import DataFeed
from src.notify import DingTalk


def _write_promo_post(cfg: dict, post_text: str, post_no: int, cycle: str, alpha: float):
    """把编号帖子写入 Promo 桥文件 (JSONL)."""
    promo_cfg = cfg.get("promo", {})
    if not promo_cfg.get("enabled", False) or not post_text:
        return
    posts_file = promo_cfg.get("posts_file", "")
    if not posts_file:
        return
    if not os.path.isabs(posts_file):
        posts_file = os.path.normpath(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         posts_file))
    try:
        os.makedirs(os.path.dirname(posts_file), exist_ok=True)
        with open(posts_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": datetime.utcnow().isoformat() + "Z",
                "post_no": post_no,
                "content": post_text,
                "cycle": cycle,
                "alpha": alpha,
            }, ensure_ascii=False) + "\n")
        print(f"[Promo] 帖子 No.{post_no} 已写入 {posts_file}")
    except Exception as e:
        print(f"[Promo] 写帖子失败: {e}")


def init_components(cfg: dict):
    data_dir = resolve_data_dir(cfg)
    os.makedirs(data_dir, exist_ok=True)

    memory = Memory(data_dir)
    state_mgr = StateManager(data_dir, cfg.get("paths", {}).get("state_file", "state.json"))
    state_mgr.load()

    fetcher = Fetcher(cfg.get("fetcher", {}), data_dir)
    analyzer = Analyzer(cfg.get("deepseek", {}))
    engine = AlphaEngine(cfg.get("alpha", {}), state_mgr)
    evidence = EvidenceAccumulator(
        state_mgr, cfg.get("alpha", {}).get("evidence", {}).get("decay_per_cycle", 0.02))
    knowledge = Knowledge(cfg.get("knowledge", {}), data_dir, analyzer)
    tradesync = TradeSync(cfg.get("tradesync", {}), data_dir)
    datafeed = DataFeed(cfg.get("datafeed", {}))
    dingtalk = DingTalk(cfg.get("dingtalk", {}))

    return {
        "cfg": cfg, "data_dir": data_dir,
        "memory": memory, "state": state_mgr,
        "fetcher": fetcher, "analyzer": analyzer,
        "engine": engine, "evidence": evidence,
        "knowledge": knowledge, "tradesync": tradesync,
        "datafeed": datafeed, "dingtalk": dingtalk,
    }


def print_status(components: dict):
    sm = components["state"]
    regime = sm.get_regime()
    alpha = sm.get_alpha()
    count = sm.get("runtime.analysis_count", 0)
    print(f"\n[Status] Regime={regime} | Alpha={alpha:+.4f} | Analyses={count}")
    print(f"         Cooldown={sm.get('regime.cooldown_remaining',0)} | "
          f"Stability={sm.get('regime.stability_counter',0)}")


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

    # 1. 获取数据: snscrape > RSS > 文件
    print("[Backfill] 阶段1: 获取历史推文 ...")
    total = fetcher.count_tweets()

    if total < 20:
        bulk_count = fetcher.fetch_bulk(limit=bulk_limit)
        total = fetcher.count_tweets()
        print(f"[Backfill] snscrape 批量抓取: +{bulk_count} 条, 总计 {total} 条")

    if total < 5:
        print("[Backfill] RSS 补充抓取...")
        fetcher.fetch()
        total = fetcher.count_tweets()

    if total == 0:
        print("[Backfill] 无历史推文，跳过回溯 (改天 RSS/snscrape 可用时再跑)")
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
        analysis = analyzer.analyze(batch, ctx, kb)

        if not analysis:
            print("[Backfill]   分析失败，跳过本批")
            continue

        sm.set("runtime.last_deepseek_at", datetime.utcnow().isoformat() + "Z")

        cp = analysis.get("cycle_position", "BEAR")
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

    sm.set("alpha.regime_progress", progress)
    sm.set("alpha.current", target)
    sm.set("alpha.target", target)
    sm.set("alpha.transition_progress", 1.0)
    sm.set("alpha.last_change_at", datetime.utcnow().isoformat() + "Z")
    sm.set("regime.cooldown_remaining", 0)
    sm.set("regime.stability_counter", 0)

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

    # 1. 获取数据: snscrape 批量 > RSS > 文件
    total = fetcher.count_tweets()
    if total < 20:
        bulk_count = fetcher.fetch_bulk(limit=bulk_limit)
        total = fetcher.count_tweets()
        print(f"[首次分析] snscrape 批量抓取: +{bulk_count} 条, 总计 {total} 条")
    if total < 5:
        print("[首次分析] RSS 补充抓取...")
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
        a = analyzer.analyze(batch, ctx, kb)
        if a:
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
    target = engine.calculate_target_alpha(engine.get_regime(), progress)
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
        last_analysis.get("signal_board", []), post_no=post_no)
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
                print(f"[Lock] 距上次 DeepSeek 分析 {elapsed_h:.1f}h < {lock_hours:.0f}h, 本轮跳过抓取")
        except ValueError:
            pass

    # 1. 获取价格
    btc_price = datafeed.get_price()
    if btc_price:
        print(f"[BTC] ${btc_price:,.2f}")

    # 2. 抓取新推文
    new_tweets = []
    print("\n--- Fetch ---")
    if locked:
        print("[Fetch] Skipped (analysis lock)")
    else:
        try:
            new_tweets = fetcher.fetch()
        except Exception as e:
            print(f"[Fetch] Error: {e}")
            new_tweets = []

    has_analysis = False
    analysis = None

    if new_tweets:
        # 3. 分析 (只在有新推文时)
        print("\n--- Analyze ---")
        kb = knowledge.load_knowledge_base()
        ctx = memory.get_context_for_ai()
        analysis = analyzer.analyze(new_tweets, ctx, kb)

        if analysis:
            sm.set("runtime.last_deepseek_at", datetime.utcnow().isoformat() + "Z")

            # 周期内进度 → 动态目标 (状态感知)
            rp = analysis.get("regime_progress")
            if rp is not None:
                sm.set("alpha.regime_progress", float(rp))
            progress = float(sm.get("alpha.regime_progress", 0.5))
            print(f"  regime_progress: {progress:.2f}")

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
            ok, reason = engine.request_regime_change(cp, scores, conf,
                                                       meta.get("analysis_quality", 8))
            if ok and cp != current_regime:
                alpha_before = engine.get_alpha()
                new_regime = engine.execute_regime_change(cp, progress)
                print(f"  [REGIME] {current_regime} → {new_regime}")
                print(f"           原因: {regime_evidence}")
                dingtalk.regime_change(current_regime, new_regime, regime_evidence,
                                       engine.get_alpha(),
                                       engine.calculate_target_alpha(new_regime, progress),
                                       old_alpha=alpha_before)
            elif cp != current_regime:
                print(f"  [REGIME] 请求 {cp} 被拒绝: {reason}")

            # 6. 跟踪预测
            knowledge.log_prediction(cp, conf, btc_price)

            has_analysis = True
        else:
            print("  Analysis failed, skipping evidence update.")
            sm.update_runtime()
            sm.save()
            return False
    else:
        # 无新推文: 只做维护 —— 不调 alpha
        print("\n--- Idle (no new tweets) ---")
        evidence.decay_all()
        engine.tick_cooldown()
        engine.tick_stability()
        sm.update_runtime()
        sm.save()
        return False

    # 6.5 右侧纪律兜底: 仓位符号与周期方向侧冲突 → 先平仓, 本轮不再步进
    clamp_old = engine.get_alpha()
    if engine.enforce_side_constraint():
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
    elif conf == "low":
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

    # 8. 发送交易指令
    if alpha_changed:
        order = tradesync.send_order(new_alpha, engine.get_regime(), btc_price)

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

    # 10. 每周蒸馏 (周日 0 UTC)
    now = datetime.utcnow()
    if now.weekday() == 6 and now.hour == 0:
        last_distill = sm.get("runtime.last_distill_at", "")
        if not last_distill or last_distill[:10] != now.strftime("%Y-%m-%d"):
            print("\n--- Weekly Distill ---")
            knowledge.compress_memory(memory)
            knowledge.distill(memory, sm)
            sm.set("runtime.last_distill_at", now.isoformat() + "Z")

    # 11. 保存状态
    sm.update_runtime()
    sm.save()

    return has_analysis


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print("Glassnode Alpha Engine")
        print("  python -m src.run              # 运行监控循环")
        print("  python -m src.run --once       # 只跑一次分析")
        print("  python -m src.run --backfill   # 强制重新回溯历史推文, 重设 alpha")
        print("  python -m src.run --status     # 查看当前状态")
        print("  python -m src.run --import <file.jsonl>  # 导入历史推文文件")
        print("  python -m src.run --bulk <limit>   # 批量抓取并退出")
        sys.exit(0)

    once = "--once" in sys.argv
    status_only = "--status" in sys.argv
    backfill_force = "--backfill" in sys.argv
    import_file = None
    bulk_only = None

    for i, arg in enumerate(sys.argv):
        if arg == "--import" and i + 1 < len(sys.argv):
            import_file = sys.argv[i + 1]
        if arg == "--bulk" and i + 1 < len(sys.argv):
            bulk_only = int(sys.argv[i + 1])

    cfg = load_config()
    c = init_components(cfg)

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

        next_check = (datetime.utcnow().isoformat(timespec="minutes") + "Z")
        print(f"\n[Main] Next check at {next_check} (in {interval//60} min)\n")
        time.sleep(interval)


if __name__ == "__main__":
    main()
