"""
Glassnode Alpha 估值引擎 — 主入口.

AI 直接输出 0–1 市场估值 (0=极度低估, 1=极度高估); 无状态机/无仓位/无发单。

用法:
    python -m src.alpha
    nohup python3 -u -m src.alpha > engine.log 2>&1 &
"""
import atexit
import json
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from src.config_loader import load_config, resolve_data_dir
from src.memory import Memory
from src.singleton_lock import SingletonLock
from src.state_manager import StateManager
from src.fetcher import Fetcher
from src.analyzer import Analyzer
from src.knowledge import Knowledge
from src.ma_context import build_ma_context, summarize_ma_context
from src.pending_analysis import PendingAnalysis
from src.cycle_context import build_cycle_context
from src.datafeed import DataFeed
from src.notify import DingTalk
from src.scorecard import build_scorecard
from src.thesis import (adopt_thesis, append_event as append_thesis_event,
                        evaluate_thesis, load_current as load_current_thesis,
                        save_current as save_current_thesis)
from src.valuation import band_of, change_text, describe, is_cross_band, is_valid
from src import manifest as run_manifest

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


def _apply_promo_term_replacements(text: str, promo_cfg: dict) -> str:
    """对**仅发往 Promo 的载荷文本**应用 promo.term_replacements (品牌词避让)。

    规则按序应用且大小写不敏感 (pattern 按字面量匹配, 不做正则解释);
    钉钉消息/记忆/知识库/AI 原始输出保持原文。
    配置缺失/非法、单条规则非法、替换异常或替换后为空 → 原文透传 + 日志。
    """
    rules = promo_cfg.get("term_replacements")
    if rules is None:
        print("[Promo] 未配置 promo.term_replacements, 原文透传")
        return text
    if not isinstance(rules, list):
        print(f"[Promo] promo.term_replacements 非列表 ({type(rules).__name__}), 原文透传")
        return text
    result = text
    applied = 0
    for rule in rules:
        if not isinstance(rule, dict):
            print(f"[Promo] 忽略非法术语规则 (非对象): {rule!r}")
            continue
        pattern = rule.get("pattern")
        replacement = rule.get("replacement")
        if not isinstance(pattern, str) or not pattern:
            print(f"[Promo] 忽略非法术语规则 (pattern 缺失): {rule!r}")
            continue
        if not isinstance(replacement, str):
            print(f"[Promo] 忽略非法术语规则 (replacement 缺失): {rule!r}")
            continue
        try:
            result = re.sub(re.escape(pattern), lambda _m: replacement,
                            result, flags=re.IGNORECASE)
        except re.error as exc:
            print(f"[Promo] 术语替换异常 ({pattern!r}: {exc}), 原文透传")
            return text
        applied += 1
    if not result.strip():
        print("[Promo] 术语替换后文本为空, 原文透传")
        return text
    if result != text:
        print(f"[Promo] 术语替换已应用 (规则 {applied} 条)")
    return result


def _write_promo_post(cfg: dict, post_text: str, post_no: int, valuation):
    """把编号帖子推送给 Promo: 仅 HTTP 推送, 失败即丢弃 (不写文件桥, 过期不候)。

    载荷文本在此应用 promo.term_replacements (品牌词避让): 仅影响 Promo 载荷,
    钉钉消息/记忆/知识库/AI 原始输出不受影响; 替换失败/为空时原文透传。
    """
    promo_cfg = cfg.get("promo") or {}
    if not isinstance(promo_cfg, dict):
        return
    if not promo_cfg.get("enabled", False) or not post_text:
        return
    payload_text = _apply_promo_term_replacements(post_text, promo_cfg)
    try:
        record = build_event("AlphaEngine", "alpha_post", {
            "ts": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
            "post_no": post_no,
            "content": payload_text,
            "valuation": valuation if is_valid(valuation) else None,
            "valuation_label": band_of(valuation),
        })
        if _push_promo_event(record):
            print(f"[Promo] 帖子 No.{post_no} 已推送到 Promo 事件服务")
        else:
            print(f"[Promo] 帖子 No.{post_no} 推送失败, 已丢弃")
    except Exception as e:
        print(f"[Promo] 推送帖子失败: {e}")


def _record_ai_call_meta(sm, analyzer) -> None:
    """把本轮 AI 调用元数据 (prompt 指纹) 同步到 state.runtime (可追溯)."""
    sm.set("runtime.last_prompt_hash", getattr(analyzer, "prompt_hash", "") or "")


def _fetch_retry_delay(value) -> float:
    """fetch 重试等待秒数 = clamp(value, 0, 30); 非法值回退 0 (不等待)."""
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return 0.0
    if delay != delay:  # NaN
        return 0.0
    return max(0.0, min(30.0, delay))


def _alert_state_recovery(dingtalk, info: dict) -> None:
    """state 损坏恢复后的钉钉告警 (通知失败不影响启动)."""
    reason = info.get("reason", "未知")
    backup = info.get("backup_path") or "备份失败"
    if info.get("rebuilt"):
        title = "state 已从记忆重建"
        body = (f"原因: {reason}\n备份: {backup}\n"
                f"重建: 估值={info.get('valuation')}")
    else:
        title = "state 损坏且无记忆可重建"
        body = (f"原因: {reason}\n备份: {backup}\n"
                "已回到默认状态 (估值待下一轮 AI 评估)")
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
    ai_cfg["cross_check"] = (cfg.get("valuation") or {}).get("cross_check") or {}
    # Phase 1 门控: thesis=false 时 prompt/输出契约回到旧版 (config 注释"关闭即回退")
    ai_cfg["thesis_enabled"] = _feature_enabled(cfg, "thesis")
    analyzer = Analyzer(ai_cfg)
    knowledge = Knowledge(cfg.get("knowledge", {}), data_dir, analyzer)

    datafeed = DataFeed(cfg.get("datafeed", {}))

    return {
        "cfg": cfg, "data_dir": data_dir,
        "memory": memory, "state": state_mgr,
        "fetcher": fetcher, "analyzer": analyzer,
        "knowledge": knowledge,
        "datafeed": datafeed, "dingtalk": dingtalk,
        "pending": pending,
    }


def print_status(components: dict):
    sm = components["state"]
    valuation = sm.get_valuation()
    count = sm.get("runtime.analysis_count", 0)
    print(f"\n[Status] 估值={describe(valuation)} | Analyses={count}")
    line = "        "
    outage = sm.get("runtime.outage") or {}
    if isinstance(outage, dict) and outage:
        line += (f" | Outage={outage.get('reason', '?')}"
                 f"(since {outage.get('since', '?')})")
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
    print(line.lstrip() if line.strip() else "")


def build_market_state(components: dict, price: float = None,
                       persist_ma: bool = True) -> dict:
    """组装当前市场状态 (估值锚点 + 价格/均线/周期上下文), 供 AI 判断.

    persist_ma=False (--test-ai / 回溯等路径): 只读均线历史, 不写 ma_history.jsonl。
    """
    sm = components["state"]
    state = {
        "valuation": sm.get_valuation(),
        "valuation_label": sm.get("valuation.label", ""),
        "valuation_updated_at": sm.get("valuation.updated_at", "") or "",
    }
    if price is not None:
        state["price"] = price
    # 价格趋势 (从 price_history 计算 7d/30d 涨跌)
    knowledge = components.get("knowledge")
    if knowledge is not None:
        try:
            trend = knowledge.price_trend()
        except Exception as exc:
            print(f"[Price] 价格趋势计算失败 (不影响本轮): {exc}")
            trend = {}
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
    # 周期定位与历史类比 (日线, 纯数据): 辅助 AI 校准估值; 失败不阻塞主流程
    cycle_cfg = (components.get("cfg") or {}).get("cycle_context") or {}
    if cycle_cfg.get("enabled", False):
        try:
            cycle_context = build_cycle_context(
                cycle_cfg, client=components.get("datafeed"),
                ma_context=state.get("ma_context"),
                valuation=state.get("valuation"))
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
    summary["updated_at"] = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    sm.set("ma", summary)
    return summary


def _feature_enabled(cfg: dict, name: str) -> bool:
    """feature flag: 缺失/异常一律关闭 (Phase 回滚用)。"""
    features = (cfg or {}).get("features")
    if not isinstance(features, dict):
        return False
    return bool(features.get(name, False))


def _thesis_manifest(thesis):
    if not isinstance(thesis, dict) or not thesis.get("thesis_id"):
        return None
    return {
        "thesis_id": thesis.get("thesis_id"),
        "status": thesis.get("status"),
        "label": thesis.get("label"),
        "conditions_met": thesis.get("conditions_met"),
        "unmet_count": thesis.get("unmet_count"),
        "unknown_count": thesis.get("unknown_count"),
        "invalidated_reason": thesis.get("invalidated_reason"),
    }


def _refresh_current_thesis(components: dict, market_state: dict):
    """评估 state 中的当前命题; 状态变化落 theses.jsonl (Phase 1 不影响仓位)."""
    if not _feature_enabled(components.get("cfg") or {}, "thesis"):
        return None
    sm = components["state"]
    try:
        current = load_current_thesis(sm)
    except Exception as exc:
        print(f"[Thesis] 读取当前命题失败 (不影响主流程): {exc}")
        return None
    if not current:
        return None
    try:
        evaluated = evaluate_thesis(current, market_state or {})
    except Exception as exc:
        print(f"[Thesis] 评估失败 (不影响主流程): {exc}")
        return current
    old_status = current.get("status")
    try:
        if evaluated.get("status") != old_status:
            append_thesis_event(components["data_dir"],
                                evaluated.get("status") or "updated", evaluated)
            print(f"[Thesis] {evaluated.get('thesis_id')}: {old_status} → "
                  f"{evaluated.get('status')} "
                  f"({evaluated.get('conditions_met')}/{len(evaluated.get('conditions') or [])} 满足)")
        if evaluated != current:
            save_current_thesis(sm, evaluated)
    except Exception as exc:
        print(f"[Thesis] 状态保存失败 (不影响主流程): {exc}")
    return evaluated


def _adopt_analysis_thesis(components: dict, raw_thesis, market_state: dict,
                           label: str = None):
    """把 AI 本轮 thesis 并入当前命题, 评估并落盘; 非法输出保留旧命题。

    label: 本轮估值档位; 命题标签以本地估值档位为准 (仅追溯用, 不信任 AI 自报)。
    """
    if not _feature_enabled(components.get("cfg") or {}, "thesis"):
        return None
    sm = components["state"]
    if isinstance(raw_thesis, dict) and label:
        raw_thesis = dict(raw_thesis, label=label)
    try:
        current = load_current_thesis(sm)
        thesis, event, previous = adopt_thesis(raw_thesis, current)
        if event == "rejected":
            print("[Thesis] AI 输出无效, 保留当前命题")
            return current
        if previous is not None:
            append_thesis_event(components["data_dir"], "superseded", previous)
        thesis = evaluate_thesis(thesis, market_state or {})
        append_thesis_event(components["data_dir"], event, thesis)
        save_current_thesis(sm, thesis)
        print(f"[Thesis] {event}: {thesis.get('thesis_id')} "
              f"status={thesis.get('status')} "
              f"({thesis.get('conditions_met')}/{len(thesis.get('conditions') or [])} 满足)")
        return thesis
    except Exception as exc:
        print(f"[Thesis] 本轮命题处理失败 (不影响主流程): {exc}")
        return None


def _build_scorecard_text(components: dict, btc_price) -> str:
    if not _feature_enabled(components.get("cfg") or {}, "scorecard"):
        return ""
    try:
        sm = components.get("state")
        valuation_now = sm.get_valuation() if sm is not None else None
        return build_scorecard(
            components.get("knowledge"), components.get("memory"),
            valuation_now, btc_price)
    except Exception as exc:
        print(f"[Scorecard] 构建失败 (不影响主流程): {exc}")
        return ""


def _append_manifest_safe(components: dict, entry: dict) -> None:
    try:
        run_manifest.append_manifest(components["data_dir"], entry)
    except Exception as exc:
        print(f"[Manifest] 写入失败 (不影响主流程): {exc}")


def _write_manifest(components: dict, *, cycle_id: str, mode: str,
                    valuation_before, btc_price, thesis=None, **extra) -> None:
    """本轮 manifest 落盘 (feature flag 关闭时零开销; 失败不阻断主流程)。

    记录估值前后值与档位, 不含任何仓位/regime 字段 (2026-11 估值重构)。
    """
    if not _feature_enabled(components.get("cfg") or {}, "manifest"):
        return
    sm = components["state"]
    try:
        valuation_after = sm.get_valuation()
        entry = run_manifest.build_manifest(
            mode=mode,
            cycle_id=cycle_id,
            btc_price=btc_price,
            valuation_before=valuation_before if is_valid(valuation_before) else None,
            valuation_after=valuation_after if is_valid(valuation_after) else None,
            valuation_label=sm.get("valuation.label", ""),
            valuation_confidence=sm.get("valuation.confidence", ""),
            thesis=_thesis_manifest(thesis),
            **extra)
    except Exception as exc:
        print(f"[Manifest] 构建失败 (不影响主流程): {exc}")
        return
    _append_manifest_safe(components, entry)


def run_backfill(components: dict, force: bool = False) -> bool:
    """回溯历史推文，建立初始估值。返回是否执行了回填.

    force=True 时忽略已有分析记录，重新回溯并重设估值。
    估值语义: 取最近一批有效分析的 AI 估值 (最新信息), 不做投票/插值/确认。
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

    batch_size = backfill_cfg.get("batch_size", 10)
    bulk_limit = backfill_cfg.get("bulk_fetch_limit", 2000)
    max_samples = backfill_cfg.get("max_analysis_samples", 200)

    print(f"\n{'='*60}")
    print("[Backfill] 首次启动 — 回溯历史推文建立初始估值")
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

    # 3. 分批分析 — 仅采集, 不产生任何执行动作
    batches = [samples[i:i+batch_size] for i in range(0, len(samples), batch_size)]
    print(f"\n[Backfill] 阶段3: 分 {len(batches)} 批采集数据 ...")

    analyzer = c["analyzer"]
    knowledge = c["knowledge"]
    dingtalk = c["dingtalk"]
    kb = knowledge.load_knowledge_base()

    collected = []  # [{valuation, label, scores, summary, conf, evidence, ...}]

    for bi, batch in enumerate(batches):
        print(f"\n[Backfill] 批次 {bi+1}/{len(batches)} ({len(batch)} 推文) ...")

        ctx = memory.get_context_for_ai()
        analysis = analyzer.analyze(batch, ctx, kb,
                                    market_state=build_market_state(c, persist_ma=False))

        if not analysis:
            print("[Backfill]   分析失败，跳过本批")
            continue

        valuation = analysis.get("valuation")
        if not is_valid(valuation):
            print(f"[Backfill]   非法 valuation={valuation!r}, 拒绝本批")
            continue

        sm.set("runtime.last_deepseek_at", datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
        _record_ai_call_meta(sm, analyzer)

        scores = analysis.get("evidence_scores", {})
        meta = analysis.get("meta", {})
        print(f"[Backfill]   valuation={float(valuation):.2f} ({band_of(valuation)}), "
              f"scores={json.dumps(scores)}, quality={meta.get('analysis_quality','?')}")

        collected.append({
            "valuation": round(float(valuation), 4),
            "label": band_of(valuation),
            "scores": scores,
            "summary": analysis.get("summary", ""),
            "conf": analysis.get("valuation_confidence", "low"),
            "evidence": analysis.get("valuation_evidence", ""),
            "signal_board": analysis.get("signal_board", []),
            "risks": analysis.get("risks", []),
            "meta": meta,
            "forecast": analysis.get("forecast"),
        })

        sm.update_runtime()

    if not collected:
        print("[Backfill] 无有效分析结果，跳过")
        sm.save()
        return False

    # 4. 整合 — 最终估值取最后一个有效批次 (最新信息)
    print(f"\n[Backfill] 阶段4: 整合 {len(collected)} 批数据 ...")
    rc = collected[-1]
    final_valuation = rc["valuation"]
    sm.set_valuation(final_valuation,
                     confidence=rc.get("conf", "low"),
                     evidence=rc.get("evidence", ""))
    print(f"[Backfill] 估值设为 {describe(final_valuation)} "
          f"(置信={rc.get('conf', 'low')})")

    # 回溯完成 → 输出编号帖子
    post_no = int(sm.get("runtime.post_count", 0)) + 1
    post_text = dingtalk.analysis(
        rc.get("summary", ""),
        valuation=final_valuation,
        confidence=rc.get("conf", "medium"),
        valuation_evidence=rc.get("evidence", ""),
        signal_board=rc.get("signal_board", []),
        post_no=post_no,
        forecast=rc.get("forecast"),
    )
    sm.set("runtime.post_count", post_no)
    _write_promo_post(cfg, post_text, post_no, final_valuation)

    # 写一条综合 memory 条目
    memory.append_valuation({
        "date": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "valuation": final_valuation,
        "label": band_of(final_valuation),
        "confidence": rc.get("conf", "low"),
        "note": rc.get("evidence", ""),
    })

    summaries = [c["summary"] for c in collected[-5:] if c["summary"]]
    all_signals = {}
    for c in collected:
        for s in c.get("signal_board", []):
            cat = s.get("category", "?")
            all_signals.setdefault(cat, []).append(s)

    entry = [f"**[回溯完成] 估值={describe(final_valuation)}**"]
    if summaries:
        entry.append(f"最近分析: {'; '.join(summaries[:3])}")
    entry.append(f"\n覆盖 {len(all_tweets)} 条推文, {len(collected)} 批分析")
    entry.append(f"估值序列: {[item['valuation'] for item in collected[-10:]]}")

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
    print(f"\n[Backfill] 完成 — 估值={describe(final_valuation)}")
    print("=" * 60)
    return True


# ---- 智能采样 ----

_VALUATION_KEYWORDS = [
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
        if any(kw.lower() in content for kw in _VALUATION_KEYWORDS):
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
    """首次正式运行: 抓取尽量多的历史推文并分析, 建立首个估值.

    与 backfill 的区别: 分批分析后仅采用最后一个有效批次 (最新信息) 的估值,
    不做投票/插值; 估值直接写入 state, 无任何执行动作。
    """
    c = components
    cfg = c["cfg"]
    sm = c["state"]
    fetcher = c["fetcher"]
    memory = c["memory"]
    analyzer = c["analyzer"]
    knowledge = c["knowledge"]
    dingtalk = c["dingtalk"]

    backfill_cfg = cfg.get("backfill", {})
    batch_size = backfill_cfg.get("batch_size", 10)
    bulk_limit = backfill_cfg.get("bulk_fetch_limit", 2000)
    max_samples = min(max_samples, backfill_cfg.get("max_analysis_samples", 200))

    print(f"\n{'='*60}")
    print("[首次分析] 抓取尽量多的历史推文, 建立初始估值")
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

    # 3. 分批分析 — 每批一次 AI 调用, 保留最后一个有效批次
    batches = [samples[i:i+batch_size] for i in range(0, len(samples), batch_size)]
    kb = knowledge.load_knowledge_base()
    last_analysis = None
    for bi, batch in enumerate(batches, 1):
        print(f"[首次分析] 批次 {bi}/{len(batches)} ({len(batch)} 推文) ...")
        ctx = memory.get_context_for_ai()
        a = analyzer.analyze(batch, ctx, kb,
                             market_state=build_market_state(c, persist_ma=False))
        if a:
            valuation = a.get("valuation")
            if not is_valid(valuation):
                print(f"[首次分析]   非法 valuation={valuation!r}, 拒绝本批")
                continue
            last_analysis = a
            print(f"[首次分析]   valuation={float(valuation):.2f} "
                  f"({band_of(valuation)}), conf={a.get('valuation_confidence','?')}")
            sm.set("runtime.last_deepseek_at", datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
            _record_ai_call_meta(sm, analyzer)
            sm.update_runtime()
    if not last_analysis:
        print("[首次分析] 分析失败, 等待每日轮询重试")
        sm.save()
        return False

    # 4. 写入估值
    valuation = round(float(last_analysis["valuation"]), 4)
    conf = last_analysis.get("valuation_confidence", "low")
    evidence_text = last_analysis.get("valuation_evidence", "")
    sm.set_valuation(valuation, confidence=conf, evidence=evidence_text)
    print(f"[首次分析] 估值定位为 {describe(valuation)} (置信={conf})")

    # 5. 发编号帖子 (钉钉 + Promo)
    post_no = int(sm.get("runtime.post_count", 0)) + 1
    post_text = dingtalk.analysis(
        last_analysis.get("summary", ""),
        valuation=valuation,
        confidence=conf,
        valuation_evidence=evidence_text,
        signal_board=last_analysis.get("signal_board", []),
        post_no=post_no,
        forecast=last_analysis.get("forecast"))
    sm.set("runtime.post_count", post_no)
    _write_promo_post(cfg, post_text, post_no, valuation)

    # 6. memory 条目
    memory.append_valuation({
        "date": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
        "valuation": valuation,
        "label": band_of(valuation),
        "confidence": conf,
        "note": evidence_text,
    })
    entry_parts = [f"**[首次分析] 估值={describe(valuation)}**"]
    if last_analysis.get("summary"):
        entry_parts.append(last_analysis["summary"])
    entry_parts.append(f"估值依据: {evidence_text}")
    memory.append_entry("\n".join(entry_parts))

    sm.save()
    print(f"[首次分析] 完成 — 估值={describe(valuation)}, 帖子 No.{post_no}")
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
    record = {"since": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
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
    sm.set("runtime.last_tick_at", datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
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
    """运行一次完整分析循环。返回是否有新推文被分析.

    估值语义 (2026-11 重构):
    - 有新推文且分析成功 → AI 直接给出 0–1 估值, 覆盖写入 state;
    - 无新推文 / 数据源故障 → 估值保持不变 (无时间漂移、无平滑)。
    """
    c = components
    cfg = c["cfg"]
    memory = c["memory"]
    sm = c["state"]
    fetcher = c["fetcher"]
    analyzer = c["analyzer"]
    knowledge = c["knowledge"]
    datafeed = c["datafeed"]
    dingtalk = c["dingtalk"]

    cycle_id = datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"
    valuation_cycle_start = sm.get_valuation()
    scorecard_text = ""
    knowledge_chars = 0
    thesis_current = None
    evaluated_this_cycle = False
    idle_reason = "no_new_tweets"

    print_status(c)

    # 0. 分析锁: 距上次成功 AI 分析不足 min_analysis_interval_hours 小时 → 本轮跳过抓取/分析
    lock_hours = float(cfg.get("schedule", {}).get("min_analysis_interval_hours", 0) or 0)
    locked = False
    last_ds = sm.get("runtime.last_deepseek_at", "")
    if lock_hours > 0 and last_ds:
        try:
            last_dt = datetime.fromisoformat(last_ds.replace("Z", "+00:00")).replace(tzinfo=None)
            elapsed_h = (datetime.now(timezone.utc).replace(tzinfo=None) - last_dt).total_seconds() / 3600
            if elapsed_h < lock_hours:
                locked = True
                daily = cfg.get("schedule", {}).get("daily_time", "")
                lock_msg = f"[Lock] 距上次 AI 分析 {elapsed_h:.1f}h < {lock_hours:.0f}h, 本轮跳过抓取"
                if daily:
                    lock_msg += f" (下次 {_schedule_desc(cfg)})"
                print(lock_msg)
        except ValueError:
            pass

    # 1. 获取价格 (价格快照用于趋势/预测审计, 与估值无关)
    btc_price = datafeed.get_price()
    if btc_price:
        print(f"[BTC] ${btc_price:,.2f}")
        try:
            knowledge.record_price(btc_price, sm.get_valuation())
        except Exception as exc:
            print(f"[Price] 价格快照写入失败 (不影响本轮): {exc}")

    # 1.5 预测审计 (幂等): 到期预测写回结果侧车。
    # 原先只在周日蒸馏时执行, 而当前调度为周二/周四 → 永不触发, 这里改为每轮执行。
    if btc_price:
        try:
            audit = knowledge.audit_predictions(btc_price)
            if audit.get("new_audited") or audit.get("skipped"):
                message = f"新增审计 {audit.get('new_audited', 0)} 条"
                if audit.get("skipped"):
                    message += f" | 跳过 {audit['skipped']} 条(缺价格)"
                message += f" | 命中率 {audit.get('hit_rate', 0):.0%}"
                if audit.get("forecast_total"):
                    message += f" | 区间覆盖 {audit.get('interval_coverage', 0):.0%}"
                    if audit.get("forecast_brier") is not None:
                        message += f" | Brier {audit['forecast_brier']:.3f}"
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
        sched_cfg = cfg.get("schedule", {}) or {}
        try:
            new_tweets = fetcher.fetch()
            fetched_ok = True
        except Exception as e:
            fetch_error = True
            fetch_detail = f"{type(e).__name__}: {e}"
            print(f"[Fetch] Error: {e}")
            new_tweets = []
            # 抓取重试 (schedule.retry_on_fetch_failure): 本轮内重试 1 次,
            # 等待 clamp(retry_delay_seconds, 0, 30) 秒; 仍失败 → 走 outage 路径
            if sched_cfg.get("retry_on_fetch_failure", False):
                delay = _fetch_retry_delay(sched_cfg.get("retry_delay_seconds", 0))
                print(f"[Fetch] 重试 1/1 (等待 {delay:g}s) ...")
                if delay > 0:
                    time.sleep(delay)
                try:
                    new_tweets = fetcher.fetch()
                    fetched_ok = True
                    fetch_error = False
                    fetch_detail = ""
                    print("[Fetch] 重试成功")
                except Exception as retry_exc:
                    fetch_detail = f"{type(retry_exc).__name__}: {retry_exc}"
                    print(f"[Fetch] 重试失败: {retry_exc}")
                    new_tweets = []

    # 2.5 失败推文重放: 队列非空时从 tweets.jsonl 取回并与当轮新推文合并
    new_tweets = _merge_pending_replay(c, new_tweets, locked)

    has_analysis = False
    analysis = None
    idle_cycle = not new_tweets

    if new_tweets:
        # 3. 分析 (只在有新推文/重放推文时)
        print("\n--- Analyze ---")
        kb = knowledge.load_knowledge_base()
        ctx = memory.get_context_for_ai()
        market_state = build_market_state(c, btc_price)
        _record_ma_state(sm, market_state)
        # Phase 1: 先评估当前命题, 再把成绩单+命题注入本轮 prompt (只读)
        thesis_current = _refresh_current_thesis(c, market_state)
        evaluated_this_cycle = True
        scorecard_text = _build_scorecard_text(c, btc_price)
        knowledge_chars = len(kb)
        if scorecard_text:
            market_state["scorecard"] = scorecard_text
        if thesis_current:
            market_state["thesis"] = thesis_current
        try:
            analysis = analyzer.analyze(new_tweets, ctx, kb, market_state=market_state)
        except Exception as exc:
            print(f"  Analysis error: {type(exc).__name__}: {exc}")
            analysis = None

        if analysis:
            # Phase 1: AI 命题并入当前命题 (非法则保留旧命题), 评估后落 theses.jsonl
            thesis_current = (_adopt_analysis_thesis(
                c, analysis.get("thesis"), market_state,
                label=band_of(analysis.get("valuation"))) or thesis_current)
            if thesis_current:
                market_state["thesis"] = thesis_current
            sm.set("runtime.last_deepseek_at", datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z")
            _record_ai_call_meta(sm, analyzer)
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

        if analysis and not is_valid(analysis.get("valuation")):
            print(f"  [Valuation] AI 估值非法 {analysis.get('valuation')!r}, 本轮作废")
            analysis = None

        if analysis:
            valuation = round(float(analysis.get("valuation")), 4)
            conf = analysis.get("valuation_confidence", "low")
            meta = analysis.get("meta", {})
            valuation_evidence = analysis.get("valuation_evidence", "")
            label = band_of(valuation)

            print(f"  valuation: {valuation:.2f} ({label}) confidence: {conf}")
            print(f"  evidence_scores: {json.dumps(analysis.get('evidence_scores', {}))}")
            print(f"  quality: {meta.get('analysis_quality', '?')}")

            # 4. 漂移检测
            knowledge.log_drift_meta(meta, label)
            drift_alerts = knowledge.check_drift()
            if drift_alerts:
                print("\n[DRIFF ALERTS]")
                for a in drift_alerts:
                    print(f"  [WARN] {a}")
                    dingtalk.alert("风格漂移", a)

            # 5. 写入估值 (AI 直接输出, 无平滑/无确认/无状态机)
            print("\n--- Valuation ---")
            previous = sm.get_valuation()
            sm.set_valuation(valuation, confidence=conf, evidence=valuation_evidence)
            try:
                memory.append_valuation({
                    "date": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
                    "valuation": valuation,
                    "label": label,
                    "confidence": conf,
                    "btc_price": btc_price,
                    "note": valuation_evidence,
                })
            except Exception as exc:
                print(f"[Memory] 估值历史写入失败 (不影响本轮): {exc}")
            change = change_text(previous, valuation)
            if previous is None:
                print(f"  首次估值: {describe(valuation)} (置信={conf})")
            elif change:
                print(f"  估值: {change} (置信={conf})")
            else:
                print(f"  估值保持 {describe(valuation)} (置信={conf})")
            if is_cross_band(previous, valuation):
                dingtalk.valuation_change(previous, valuation, conf,
                                          valuation_evidence, btc_price)

            # 6. 预测留痕 (K-03: 带来源标注/区间/重跑离散)
            knowledge.log_prediction(
                valuation, conf, btc_price,
                forecast=analysis.get("forecast"),
                source_type_counts=analysis.get("source_type_counts"),
                cross_check=analysis.get("cross_check"),
                valuation_label=label,
            )

            has_analysis = True
        elif _all_tweets_filtered(analyzer):
            # 全部被 BTC 过滤 ≠ 故障: 视为无有效新闻, 走 idle (不入队/不置 outage);
            # 同时把被过滤条目从重放队列移出 (与成功轮 sent∪filtered 语义对齐,
            # 否则纯 altcoin 条目会每轮重放且永远失败)
            print("  [Analyzer] 本批全部被 BTC 过滤跳过 (视为无有效新闻)")
            idle_reason = "all_filtered"
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
            print("  Analysis failed, skipping valuation update.")
            stats = _enqueue_pending_analysis(c, new_tweets, "analysis_failed")
            detail = "AI 分析失败"
            if stats.get("total") is not None:
                detail += (f", {stats.get('added', 0)} 条入重放队列 "
                           f"(队列 {stats['total']} 条)")
            _set_outage(c, "analysis_failed", detail)
            sm.update_runtime()
            sm.save()
            _write_manifest(
                c, cycle_id=cycle_id, mode="analysis_failed",
                valuation_before=valuation_cycle_start,
                btc_price=btc_price, thesis=thesis_current,
                error=detail, tweets=len(new_tweets),
                tweet_ids=[str(t.get("id", "")) for t in new_tweets[-20:]] or None,
                knowledge_chars=knowledge_chars or None,
                scorecard_chars=len(scorecard_text) or None,
                prompt_hash=getattr(analyzer, "prompt_hash", None),
                model=getattr(analyzer, "last_call_model", None),
                tokens=getattr(analyzer, "last_call_usage", None))
            return False

    if idle_cycle:
        # 无新推文 (或全部被过滤跳过): 估值保持不变, 只刷新 MA/命题锚点
        idle_market_state = None
        try:
            idle_market_state = build_market_state(c, btc_price, persist_ma=False)
        except Exception as exc:
            print(f"[MA] 空闲周期均线摘要更新失败 (不影响本轮): {exc}")

        # Phase 1: 空闲轮也评估命题 (无新推文也能发现失效), 只记录不执行任何动作。
        # all_filtered 轮在分析块已评估过 → 跳过, 避免同一轮双评估。
        if not evaluated_this_cycle:
            thesis_current = _refresh_current_thesis(c, idle_market_state)
            evaluated_this_cycle = True
        if idle_market_state is not None and thesis_current:
            idle_market_state["thesis"] = thesis_current

        if fetch_error:
            # 数据源故障: 估值/价格锚点全部冻结; 价格/审计/MA 摘要与 state 保存照常
            print("\n--- Idle (fetch_error) ---")
            _set_outage(c, "fetch_error", fetch_detail)
            print("  [Outage] 数据缺失: 本轮估值保持不变")
        else:
            print("\n--- Idle (no new tweets) ---")
            if fetched_ok:
                _clear_outage(c, "抓取成功 (无新推文)")
            print(f"  估值保持 {describe(sm.get_valuation())} (无新推文, 不推进)")

        # 空闲周期也刷新 K 线趋势摘要 (Web 展示用): 只读历史不写 ma_history,
        # DataFeed 不可用/异常不阻塞估值 (构建失败时跳过写入, 保持旧语义).
        if idle_market_state is not None:
            try:
                ma_summary = _record_ma_state(sm, idle_market_state)
                if ma_summary.get("available"):
                    print(f"  MA 趋势: {ma_summary.get('zone')} "
                          f"(截至 {ma_summary.get('as_of')})")
            except Exception as exc:
                print(f"[MA] 空闲周期均线摘要更新失败 (不影响本轮): {exc}")

        if not fetch_error:
            # 故障轮不计入分析计数, 仅保存状态 (outage/MA 摘要)
            sm.update_runtime()
        sm.save()
        idle_mode = "idle"
        if locked:
            idle_mode = "idle_locked"
        elif fetch_error:
            idle_mode = "idle_fetch_error"
        elif idle_reason == "all_filtered":
            idle_mode = "idle_filtered"
        _write_manifest(
            c, cycle_id=cycle_id, mode=idle_mode,
            valuation_before=valuation_cycle_start,
            btc_price=btc_price, thesis=thesis_current,
            fetched_ok=fetched_ok,
            error=fetch_detail or None)
        return False

    # 9. 更新 memory + 发帖 (仅当有新分析)
    if has_analysis and analysis:
        valuation = sm.get_valuation()
        post_no = int(sm.get("runtime.post_count", 0)) + 1
        post_text = dingtalk.analysis(
            analysis.get("summary", ""),
            valuation=valuation,
            confidence=sm.get("valuation.confidence", "low"),
            valuation_evidence=analysis.get("valuation_evidence", ""),
            signal_board=analysis.get("signal_board", []),
            post_no=post_no,
            forecast=analysis.get("forecast"),
        )
        sm.set("runtime.post_count", post_no)
        _write_promo_post(cfg, post_text, post_no, valuation)

        entry_parts = [f"**{analysis.get('summary', 'N/A')}**"]
        entry_parts.append(f"估值: {describe(valuation)} "
                           f"(置信: {sm.get('valuation.confidence', 'low')})")
        entry_parts.append(f"估值依据: {analysis.get('valuation_evidence', '')}")

        s = analysis.get("signal_board", [])
        if s:
            entry_parts.append("\n**信号板**:")
            for si in s[:5]:
                entry_parts.append(f"- [{si.get('category','?')}] {si.get('name','?')}: "
                                   f"{si.get('signal',0):+.2f} — {si.get('detail','')}")

        memory.append_entry("\n".join(entry_parts))

        # 指标提取
        for si in s:
            name = si.get("name", "")
            if name:
                memory.add_metric(name, si.get("signal", 0))

    # 10. 每周蒸馏 (按 knowledge.distill 计划; 分析周期错过时下一轮补偿)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if knowledge.distill_due(sm, now):
        last_distill = sm.get("runtime.last_distill_at", "")
        if not last_distill or last_distill[:10] != now.strftime("%Y-%m-%d"):
            print("\n--- Weekly Distill ---")
            knowledge.compress_memory(memory)
            knowledge.distill(memory, sm, btc_price)
            sm.set("runtime.last_distill_at", now.isoformat() + "Z")

    # 11. 保存状态
    sm.update_runtime()
    sm.save()

    _write_manifest(
        c, cycle_id=cycle_id,
        mode="analysis",
        valuation_before=valuation_cycle_start,
        btc_price=btc_price, thesis=thesis_current,
        label=sm.get("valuation.label"),
        confidence=sm.get("valuation.confidence"),
        tweets=len(new_tweets),
        tweet_ids=[str(t.get("id", "")) for t in new_tweets[-20:]] or None,
        knowledge_chars=knowledge_chars or None,
        scorecard_chars=len(scorecard_text) or None,
        prompt_hash=getattr(analyzer, "prompt_hash", None),
        model=getattr(analyzer, "last_call_model", None),
        tokens=getattr(analyzer, "last_call_usage", None))

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
        wait = _seconds_until_next_run(cfg, datetime.now(timezone.utc).replace(tzinfo=None))
        next_dt = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=wait)
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

        wait = _seconds_until_next_run(cfg, datetime.now(timezone.utc).replace(tzinfo=None))
        next_dt = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=wait)
        next_str = next_dt.isoformat(timespec="minutes") + "Z"
        daily = cfg.get("schedule", {}).get("daily_time", "")
        if daily:
            print(f"\n[Main] Next check at {next_str} ({_schedule_desc(cfg)}, in {wait/60:.0f} min)\n")
        else:
            print(f"\n[Main] Next check at {next_str} (in {wait/60:.0f} min)\n")
        time.sleep(wait)


if __name__ == "__main__":
    main()
