#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""xwatch — 独立 X 观察通道 (fxtwitter 时间线采集).

**独立观察通道, 勿接主分析管线**:
- 不 import src.fetcher / src.alpha 等主管线模块, 不参与抓取/分析/发单;
- 仅复用 src.fx_client.FxClient (免费 fxtwitter API v2; 异常内部消化);
- 数据只写 <repo>/data/xwatch/ (tweets.jsonl + state.json), 与主管线隔离。

用法 (从仓库根运行):
    py -3 tools/xwatch.py
    python3 tools/xwatch.py
    py -3 tools/xwatch.py --dry-run
    py -3 tools/xwatch.py --config tools/xwatch_config.json

行为:
- 逐账号拉取时间线 (count=max_per_account, with_replies 可配), 账号间 sleep;
- 保留原始返回 (不做转推/BTC 过滤; 仅按 id 去重, 只追加新条目;
  schema 与 fx_client 映射一致);
- 单账号异常隔离 (记录 last_error 后继续其它账号, 整体不崩);
- state.json 原子写; --dry-run 零写盘;
- 退出码: 0=全部成功或部分成功; 1=全部失败/状态写失败; 2=配置缺失或非法。
"""
import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from src.atomic_io import atomic_write_json  # noqa: E402
from src.fx_client import FxClient  # noqa: E402

DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "xwatch_config.json")
TWEETS_FILENAME = "tweets.jsonl"
STATE_FILENAME = "state.json"

DEFAULT_CONFIG = {
    "accounts": [],
    "data_dir": "data/xwatch",
    "max_per_account": 20,
    "with_replies": False,
    "request_interval_seconds": 0.5,
}


# ---- 配置 ----

def _clamp_int(raw, default, lo, hi):
    """整数归一化到 [lo, hi]; 非法/溢出回退 default."""
    try:
        value = int(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(lo, min(hi, value))


def _clamp_float(raw, default, lo, hi):
    """浮点归一化到 [lo, hi]; 非法/非有限 (inf/NaN) 回退 default."""
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(value):
        return default
    return max(lo, min(hi, value))


def normalize_config(raw):
    """归一化配置 (缺省/非法回退默认值; accounts 去空白/去重, 保持顺序)."""
    raw = raw if isinstance(raw, dict) else {}
    accounts, seen = [], set()
    raw_accounts = raw.get("accounts")
    for item in raw_accounts if isinstance(raw_accounts, list) else []:
        if not isinstance(item, str):
            continue
        handle = item.strip()
        key = handle.lower()
        if handle and key not in seen:
            seen.add(key)
            accounts.append(handle)
    data_dir = raw.get("data_dir")
    if not isinstance(data_dir, str) or not data_dir.strip():
        data_dir = DEFAULT_CONFIG["data_dir"]
    return {
        "accounts": accounts,
        "data_dir": data_dir.strip(),
        "max_per_account": _clamp_int(
            raw.get("max_per_account"), DEFAULT_CONFIG["max_per_account"], 1, 100),
        "with_replies": raw.get("with_replies") is True,
        "request_interval_seconds": _clamp_float(
            raw.get("request_interval_seconds"),
            DEFAULT_CONFIG["request_interval_seconds"], 0.0, 60.0),
    }


def load_config(path):
    """读取并归一化配置; 文件缺失 (FileNotFoundError) / JSON 非法 (ValueError) 抛出."""
    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    return normalize_config(raw)


def resolve_data_dir(config, base_dir=None):
    """data_dir 相对仓库根解析; 绝对路径原样 (空/非法回退默认)."""
    base = base_dir or REPO_ROOT
    data_dir = config.get("data_dir") if isinstance(config, dict) else None
    if not isinstance(data_dir, str) or not data_dir.strip():
        data_dir = DEFAULT_CONFIG["data_dir"]
    data_dir = data_dir.strip()
    return data_dir if os.path.isabs(data_dir) else os.path.join(base, data_dir)


# ---- 数据读写 ----

def read_tweet_index(path):
    """读取已有 tweets.jsonl: (id 集合, 每作者条数[小写 author]).

    文件缺失返回空; 坏行/非 dict/无 id 跳过 (观察通道不因坏数据中断)。
    """
    ids, counts = set(), {}
    if not os.path.exists(path):
        return ids, counts
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if not isinstance(item, dict):
                continue
            tid = item.get("id")
            if tid is not None and str(tid).strip():
                ids.add(str(tid))
            author = str(item.get("author") or "").strip().lower()
            if author:
                counts[author] = counts.get(author, 0) + 1
    return ids, counts


def load_state(path):
    """读取 state.json; 缺失/损坏回退初始结构 (不抛)."""
    initial = {"last_run_at": None, "accounts": {}}
    if not os.path.exists(path):
        return initial
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return initial
    if not isinstance(raw, dict):
        return initial
    accounts = raw.get("accounts")
    return {
        "last_run_at": raw.get("last_run_at"),
        "accounts": accounts if isinstance(accounts, dict) else {},
    }


def append_tweets(path, tweets):
    """追加 JSONL (每条一行); 返回写入条数; 目录不存在时创建."""
    if not tweets:
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        for tweet in tweets:
            handle.write(json.dumps(tweet, ensure_ascii=False) + "\n")
        handle.flush()
    return len(tweets)


def _safe_total(value):
    """非负整数归一化; 非法返回 None."""
    try:
        total = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return total if total >= 0 else None


# ---- 采集 ----

def run_once(config, *, client, tweets_path, state_path, dry_run=False,
             sleep_fn=time.sleep, now_fn=None):
    """执行一轮采集, 返回汇总 dict (不打印/不退出, 便于测试).

    dry_run=True 时不写 tweets.jsonl / state.json, 统计按"将要写入"口径。
    单账号 fetch/落盘异常均记录为该账号 last_error 并继续其它账号。
    返回: {order, accounts{handle:{new,total,fetched,latest,error}},
           total_new, success, failed, state_error}
    """
    now_fn = now_fn or (lambda: datetime.now(timezone.utc).isoformat())
    raw_accounts = config.get("accounts")
    accounts = list(raw_accounts) if isinstance(raw_accounts, list) else []
    count = _clamp_int(config.get("max_per_account"),
                       DEFAULT_CONFIG["max_per_account"], 1, 100)
    with_replies = config.get("with_replies") is True
    interval = _clamp_float(config.get("request_interval_seconds"),
                            DEFAULT_CONFIG["request_interval_seconds"], 0.0, 60.0)

    previous = load_state(state_path).get("accounts") or {}
    existing_ids, author_counts = read_tweet_index(tweets_path)

    summary = {
        "order": accounts,
        "accounts": {},
        "total_new": 0,
        "success": 0,
        "failed": 0,
        "state_error": None,
    }
    new_state_accounts = {}
    for index, handle in enumerate(accounts):
        if index > 0 and interval > 0:
            sleep_fn(interval)
        fetched, error, new_tweets = [], None, []
        try:
            fetched = client.fetch_user_timeline(
                handle, count=count, with_replies=with_replies)
        except Exception as exc:  # 单账号异常隔离: 不中断其它账号
            error = f"{type(exc).__name__}: {str(exc)[:160]}"
        if error is None and not isinstance(fetched, list):
            error = f"返回值非列表: {type(fetched).__name__}"
            fetched = []
        if error is None:
            new_tweets = [
                tweet for tweet in fetched
                if isinstance(tweet, dict)
                and str(tweet.get("id") or "").strip()
                and str(tweet["id"]) not in existing_ids
            ]
            for tweet in new_tweets:
                existing_ids.add(str(tweet["id"]))
            if new_tweets and not dry_run:
                try:
                    append_tweets(tweets_path, new_tweets)
                except OSError as exc:
                    error = f"写入失败 {type(exc).__name__}: {str(exc)[:120]}"
                    new_tweets = []

        previous_entry = previous.get(handle)
        previous_entry = previous_entry if isinstance(previous_entry, dict) else {}
        previous_total = _safe_total(previous_entry.get("total_stored"))
        if previous_total is None:
            previous_total = author_counts.get(handle.lower(), 0)

        # last_id/last_created_at: 本轮有返回则取首条 (API 倒序=最新);
        # 本轮无返回/失败则沿用上次值。
        latest_id = previous_entry.get("last_id")
        latest_at = previous_entry.get("last_created_at")
        if fetched and isinstance(fetched[0], dict):
            head = fetched[0]
            if str(head.get("id") or "").strip():
                latest_id = str(head["id"])
            if head.get("date"):
                latest_at = str(head["date"])

        stored_new = len(new_tweets) if error is None else 0
        total_stored = previous_total + stored_new
        summary["accounts"][handle] = {
            "new": stored_new,
            "total": total_stored,
            "fetched": len(fetched),
            "latest": latest_at,
            "error": error,
        }
        summary["total_new"] += stored_new
        if error:
            summary["failed"] += 1
        else:
            summary["success"] += 1
        new_state_accounts[handle] = {
            "last_id": latest_id,
            "last_created_at": latest_at,
            "total_stored": total_stored,
            "last_error": error,
        }

    if not dry_run:
        try:
            atomic_write_json(state_path, {
                "last_run_at": now_fn(),
                "accounts": new_state_accounts,
            })
        except OSError as exc:
            summary["state_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
            print(f"[XWatch] 状态写入失败: {summary['state_error']}")
    return summary


def format_summary(summary, dry_run=False):
    """把 run_once 汇总格式化为多行报告文本."""
    tag = "[XWatch][dry-run]" if dry_run else "[XWatch]"
    order = summary.get("order") or []
    width = max((len(str(handle)) for handle in order), default=0)
    lines = [f"{tag} 独立观察通道 (fxtwitter 时间线; 勿接主分析管线)"]
    for handle in order:
        item = summary["accounts"].get(handle) or {}
        if item.get("error"):
            lines.append(f"  {str(handle):<{width}}  [失败] {item['error']}")
            continue
        latest = item.get("latest") or "-"
        lines.append(f"  {str(handle):<{width}}  本次新增 {item.get('new', 0)}"
                     f" / 累计 {item.get('total', 0)}  最新 {latest}")
    lines.append(f"{tag} 合计: 新增 {summary.get('total_new', 0)} 条, "
                 f"{len(order)} 账号 "
                 f"(成功 {summary.get('success', 0)} / 失败 {summary.get('failed', 0)})")
    failed = [handle for handle in order
              if (summary["accounts"].get(handle) or {}).get("error")]
    if failed:
        lines.append(f"{tag} 失败账号: " + ", ".join(
            f"{handle} ({(summary['accounts'].get(handle) or {}).get('error')})"
            for handle in failed))
    if summary.get("state_error"):
        lines.append(f"{tag} 状态写失败: {summary['state_error']}")
    return "\n".join(lines)


# ---- CLI ----

def _parse_args(argv):
    parser = argparse.ArgumentParser(
        description="xwatch — 独立 X 观察通道 (fxtwitter 时间线采集; 勿接主分析管线)")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH,
                        help="配置文件 (默认 tools/xwatch_config.json)")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要写入的统计, 不写任何文件")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"[XWatch] 配置缺失: {args.config} (退出码 2)")
        return 2
    except ValueError as exc:
        print(f"[XWatch] 配置非法: {args.config}: {type(exc).__name__}: "
              f"{str(exc)[:160]} (退出码 2)")
        return 2

    if not config["accounts"]:
        print("[XWatch] 配置未包含账号; 无操作")
        return 0

    data_dir = resolve_data_dir(config)
    tweets_path = os.path.join(data_dir, TWEETS_FILENAME)
    state_path = os.path.join(data_dir, STATE_FILENAME)
    summary = run_once(
        config, client=FxClient({}), tweets_path=tweets_path,
        state_path=state_path, dry_run=args.dry_run)
    print(format_summary(summary, dry_run=args.dry_run))
    if args.dry_run:
        print(f"[XWatch][dry-run] 未写任何文件 (目标: {tweets_path} | {state_path})")
    else:
        print(f"[XWatch] 数据: {tweets_path} | 状态: {state_path}")

    if summary.get("state_error"):
        return 1
    if summary["order"] and summary["failed"] == len(summary["order"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
