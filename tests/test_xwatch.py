"""xwatch 独立采集工具 — 配置/去重/异常隔离/dry-run/状态原子写 (离线单测, 不触网).

覆盖:
- 配置解析: 默认值/非法值回退/accounts 清洗去重/缺文件与坏 JSON;
- 随仓库配置: 17 账号名单 + pool 治理元数据一致性;
- 采集: 跨两次运行按 id 去重只追加新条目;
- 单账号异常隔离: 失败账号记 last_error, 其它账号照常, 账号间 sleep;
- dry-run 零写盘 (run_once 与 CLI 两级);
- state.json 结构 + 原子写 (无 .tmp 残留);
- 汇总计数与报告文本; main 退出码 (2/1/0);
- 账号验证卡 --check: 转推过滤计数/留存样本/频率/失败路径/CLI;
- 坏数据行跳过。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from tools.xwatch import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    check_account,
    format_check,
    format_summary,
    load_config,
    main,
    normalize_config,
    read_tweet_index,
    run_once,
)


class _FakeClient:
    """按 handle 返回固定推文; errors 中的 handle 抛错 (不触网)."""

    def __init__(self, by_handle=None, errors=None):
        self.by_handle = by_handle or {}
        self.errors = errors or {}
        self.calls = []

    def fetch_user_timeline(self, handle, count=20, since=None,
                            with_replies=False, cursor=None):
        self.calls.append({"handle": handle, "count": count,
                           "with_replies": with_replies})
        if handle in self.errors:
            raise self.errors[handle]
        return list(self.by_handle.get(handle, []))


def _tweet(tid, author="glassnode", date="2026-10-05T00:00:00+00:00"):
    return {
        "id": str(tid),
        "content": f"tweet {tid}",
        "date": date,
        "url": f"https://x.com/{author}/status/{tid}",
        "author": author,
        "source": f"fxtwitter/{author}",
        "fetched_at": "2026-10-05T01:00:00+00:00",
        "images": [],
    }


def _run(config, client, tweets_path, state_path, **kwargs):
    return run_once(config, client=client, tweets_path=str(tweets_path),
                    state_path=str(state_path),
                    sleep_fn=kwargs.pop("sleep_fn", lambda _s: None),
                    now_fn=kwargs.pop("now_fn",
                                      lambda: "2026-10-05T02:00:00+00:00"),
                    **kwargs)


# ---- 配置解析 ----

def test_normalize_config_defaults():
    config = normalize_config({})
    assert config["accounts"] == []
    assert config["data_dir"] == "data/xwatch"
    assert config["max_per_account"] == 20
    assert config["with_replies"] is False
    assert config["request_interval_seconds"] == 0.5
    # 非 dict 输入同样回退默认
    assert normalize_config(None) == config


def test_normalize_config_clamps_and_cleans_accounts():
    config = normalize_config({
        "accounts": [" a ", "A", "", 3, "b", None, "  "],
        "data_dir": "   ",
        "max_per_account": 999,
        "with_replies": "yes",
        "request_interval_seconds": -5,
    })
    assert config["accounts"] == ["a", "b"]  # 去空白/大小写去重/过滤非字符串
    assert config["data_dir"] == "data/xwatch"
    assert config["max_per_account"] == 100
    assert config["with_replies"] is False  # 仅接受 JSON true
    assert config["request_interval_seconds"] == 0.0
    # accounts 非法类型不逐字符解析
    assert normalize_config({"accounts": "abc"})["accounts"] == []

    config = normalize_config({
        "max_per_account": 0,
        "request_interval_seconds": float("inf"),
    })
    assert config["max_per_account"] == 1
    assert config["request_interval_seconds"] == 0.5  # inf 回退默认

    config = normalize_config({
        "max_per_account": "bad",
        "request_interval_seconds": "bad",
    })
    assert (config["max_per_account"], config["request_interval_seconds"]) \
        == (20, 0.5)


def test_load_config_missing_and_invalid(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(str(tmp_path / "nope.json"))
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(str(bad))


def test_shipped_config_matches_pool():
    config = load_config(DEFAULT_CONFIG_PATH)
    expected = [
        "ai_9684xtpa", "evilcos", "0xCryptoChan", "EmberCN", "lookonchain",
        "FarsideUK", "hupzy_agent", "Dune", "DefiLlama", "glassnode",
        "willywoo", "nansen_ai", "MessariCrypto", "cryptoquant_com",
        "SlowMist_Team", "l2beat", "tokenterminal",
    ]
    assert config["accounts"] == expected
    assert config["data_dir"] == "data/xwatch"
    assert config["max_per_account"] == 20
    assert config["with_replies"] is False
    assert config["request_interval_seconds"] == 0.5

    raw = json.loads(Path(DEFAULT_CONFIG_PATH).read_text(encoding="utf-8"))
    pool = raw.get("pool") or {}
    assert pool, "治理元数据 pool 缺失"
    active = {key.lower() for key, meta in pool.items()
              if meta.get("tier") != "removed"}
    assert active == {handle.lower() for handle in expected}
    removed = {key.lower() for key, meta in pool.items()
               if meta.get("tier") == "removed"}
    assert {"whale_alert", "murphychen888"} <= removed
    for key, meta in pool.items():
        if meta.get("tier") == "trial":
            assert meta.get("review_after"), f"{key} 缺 review_after"


# ---- 采集: 去重 ----

def test_run_once_dedupes_across_runs(tmp_path):
    config = normalize_config({"accounts": ["a"], "request_interval_seconds": 0})
    tweets_path, state_path = tmp_path / "tweets.jsonl", tmp_path / "state.json"

    first = _run(config, _FakeClient({"a": [_tweet(1), _tweet(2)]}),
                 tweets_path, state_path)
    assert first["accounts"]["a"]["new"] == 2
    assert first["accounts"]["a"]["total"] == 2
    assert first["total_new"] == 2

    second = _run(config, _FakeClient({"a": [_tweet(2), _tweet(3)]}),
                  tweets_path, state_path)
    assert second["accounts"]["a"]["new"] == 1
    assert second["accounts"]["a"]["total"] == 3
    assert second["total_new"] == 1

    rows = [json.loads(line) for line in
            tweets_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["id"] for row in rows] == ["1", "2", "3"]


# ---- 采集: 单账号异常隔离 ----

def test_run_once_isolates_account_failure(tmp_path):
    config = normalize_config({"accounts": ["ok1", "bad", "ok2"],
                               "request_interval_seconds": 0.5})
    tweets_path, state_path = tmp_path / "tweets.jsonl", tmp_path / "state.json"
    sleeps = []
    client = _FakeClient(
        {"ok1": [_tweet("1", author="ok1")],
         "ok2": [_tweet("2", author="ok2")]},
        errors={"bad": RuntimeError("boom")})

    summary = run_once(config, client=client, tweets_path=str(tweets_path),
                       state_path=str(state_path), sleep_fn=sleeps.append,
                       now_fn=lambda: "2026-10-05T02:00:00+00:00")

    assert sleeps == [0.5, 0.5]  # 账号之间 sleep; 末账号后不 sleep
    assert (summary["success"], summary["failed"]) == (2, 1)
    assert summary["accounts"]["ok1"]["new"] == 1
    assert summary["accounts"]["ok2"]["new"] == 1
    assert "RuntimeError: boom" in summary["accounts"]["bad"]["error"]
    assert summary["accounts"]["bad"]["new"] == 0

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "boom" in state["accounts"]["bad"]["last_error"]
    assert state["accounts"]["ok1"]["last_error"] is None
    rows = [json.loads(line)["id"] for line in
            tweets_path.read_text(encoding="utf-8").splitlines()]
    assert rows == ["1", "2"]


# ---- state.json 结构 / 原子写 ----

def test_state_structure_and_atomic_write(tmp_path):
    config = normalize_config({"accounts": ["a"], "request_interval_seconds": 0})
    tweets_path, state_path = tmp_path / "tweets.jsonl", tmp_path / "state.json"
    _run(config, _FakeClient({"a": [_tweet(101, author="a"),
                                    _tweet(100, author="a")]}),
         tweets_path, state_path)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_run_at"] == "2026-10-05T02:00:00+00:00"
    entry = state["accounts"]["a"]
    assert set(entry) == {"last_id", "last_created_at", "total_stored",
                          "last_error"}
    assert entry["last_id"] == "101"  # 首条=最新
    assert entry["last_created_at"] == "2026-10-05T00:00:00+00:00"
    assert entry["total_stored"] == 2
    assert entry["last_error"] is None
    assert not list(tmp_path.glob("*.tmp"))  # 原子写无 tmp 残留


# ---- dry-run 零写盘 ----

def test_run_once_dry_run_writes_nothing(tmp_path):
    config = normalize_config({"accounts": ["a"], "request_interval_seconds": 0})
    data_dir = tmp_path / "data"
    tweets_path, state_path = data_dir / "tweets.jsonl", data_dir / "state.json"

    summary = _run(config, _FakeClient({"a": [_tweet(1), _tweet(2)]}),
                   tweets_path, state_path, dry_run=True)

    assert summary["accounts"]["a"]["new"] == 2  # 统计按"将要写入"
    assert summary["accounts"]["a"]["total"] == 2
    assert not data_dir.exists()  # 连目录都不创建


def test_main_dry_run_no_files(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "xwatch_config.json"
    data_dir = tmp_path / "out"
    config_path.write_text(json.dumps({
        "accounts": ["a"],
        "data_dir": str(data_dir),
        "max_per_account": 5,
        "with_replies": True,
        "request_interval_seconds": 0,
    }), encoding="utf-8")
    client = _FakeClient({"a": [_tweet(1)]})
    monkeypatch.setattr("tools.xwatch.FxClient", lambda cfg=None: client)

    code = main(["--config", str(config_path), "--dry-run"])

    assert code == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "本次新增 1 / 累计 1" in out
    assert not data_dir.exists()
    assert client.calls == [{"handle": "a", "count": 5, "with_replies": True}]


# ---- main 退出码 ----

def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    # 配置缺失 -> 2
    assert main(["--config", str(tmp_path / "missing.json")]) == 2

    config_path = tmp_path / "cfg.json"
    config_path.write_text(json.dumps({
        "accounts": ["bad1", "bad2"],
        "data_dir": str(tmp_path / "data"),
        "request_interval_seconds": 0,
    }), encoding="utf-8")

    # 全部失败 -> 1
    monkeypatch.setattr("tools.xwatch.FxClient", lambda cfg=None: _FakeClient(
        errors={"bad1": RuntimeError("x"), "bad2": RuntimeError("y")}))
    assert main(["--config", str(config_path)]) == 1
    out = capsys.readouterr().out
    assert "失败账号" in out and "RuntimeError: x" in out

    # 部分成功 -> 0, 且已写盘
    monkeypatch.setattr("tools.xwatch.FxClient", lambda cfg=None: _FakeClient(
        {"bad1": [_tweet(1, author="bad1")]},
        errors={"bad2": RuntimeError("y")}))
    assert main(["--config", str(config_path)]) == 0
    data_dir = tmp_path / "data"
    assert (data_dir / "tweets.jsonl").exists()
    assert (data_dir / "state.json").exists()


# ---- 汇总文本 / 坏行 ----

def test_format_summary_counts_and_failures(tmp_path):
    config = normalize_config({"accounts": ["a", "b"],
                               "request_interval_seconds": 0})
    summary = _run(config, _FakeClient(
        {"a": [_tweet(1), _tweet(2)]},
        errors={"b": TimeoutError("timed out")}),
        tmp_path / "t.jsonl", tmp_path / "s.json")

    assert summary["total_new"] == 2
    assert (summary["success"], summary["failed"]) == (1, 1)
    assert summary["accounts"]["a"]["latest"] == "2026-10-05T00:00:00+00:00"

    text = format_summary(summary)
    assert "本次新增 2 / 累计 2" in text
    assert "合计: 新增 2 条, 2 账号 (成功 1 / 失败 1)" in text
    assert "失败账号" in text and "TimeoutError: timed out" in text


def test_read_tweet_index_skips_bad_lines(tmp_path):
    path = tmp_path / "tweets.jsonl"
    path.write_text("\n".join([
        json.dumps(_tweet(1, author="A")),
        "{not json",
        json.dumps([1, 2, 3]),
        json.dumps({"content": "no id"}),
        "",
        json.dumps(_tweet(2, author="a")),
    ]), encoding="utf-8")

    ids, counts = read_tweet_index(str(path))
    assert ids == {"1", "2"}
    assert counts == {"a": 2}
    assert read_tweet_index(str(tmp_path / "missing.jsonl")) == (set(), {})


# ---- 账号验证卡 (--check; 只读) ----

class _FakeRawClient:
    """--check 用假客户端: 固定返回 payload, 记录请求 (不触网)."""

    def __init__(self, payload=None):
        self.payload = payload
        self.calls = []

    def _get_json(self, url, params):
        self.calls.append({"url": url, "params": dict(params)})
        return self.payload


_BASE = datetime(2026, 10, 10, 10, 0, tzinfo=timezone.utc)


def _raw_item(tid, author="Dune", text="tweet", reposted_by=None,
              minutes_ago=0, kind="status"):
    item = {
        "type": kind,
        "id": str(tid),
        "text": text,
        "author": {"screen_name": author},
        "created_timestamp": int(
            (_BASE - timedelta(minutes=minutes_ago)).timestamp()),
        "url": f"https://x.com/{author}/status/{tid}",
    }
    if reposted_by:
        item["reposted_by"] = {"screen_name": reposted_by}
    return item


def test_check_account_counts_samples_and_span():
    payload = {"code": 200, "results": [
        _raw_item(3, text="brand new data tool", minutes_ago=0),
        _raw_item(2, text="analysis thread", minutes_ago=1440),
        _raw_item(1, text="reposted thing", minutes_ago=2880,
                  author="someone", reposted_by="Dune"),
    ]}
    client = _FakeRawClient(payload)
    result = check_account("@Dune", client=client, count=7)
    assert result["ok"] is True and result["handle"] == "Dune"
    assert (result["raw"], result["reposts"], result["kept"]) == (3, 1, 2)
    assert client.calls[0]["params"]["count"] == 7
    assert "Dune" in client.calls[0]["url"]
    assert len(result["samples"]) == 2
    assert result["latest"].startswith("2026-10-10T")
    assert result["span_days"] == 1.0
    assert result["rate_per_day"] == 2.0


def test_check_account_failure_paths():
    assert check_account("  ", client=_FakeRawClient(None))["error"] == "空 handle"
    missing = check_account("x", client=_FakeRawClient(None))
    assert missing["ok"] is False and "拉取失败" in missing["error"]
    bad = check_account("x", client=_FakeRawClient({"code": 404}))
    assert bad["ok"] is False and "结构异常" in bad["error"]
    empty = check_account("x", client=_FakeRawClient({"code": 200, "results": []}))
    assert empty["ok"] is True and empty["kept"] == 0 and empty["samples"] == []


def test_check_account_skips_non_status_and_bad_items():
    payload = {"code": 200, "results": [
        _raw_item(1, text="good", minutes_ago=0),
        {"type": "tombstone", "id": "2", "text": "x"},
        "junk",
        _raw_item(3, text="", minutes_ago=10),  # 空文本 -> 映射丢弃
    ]}
    result = check_account("Dune", client=_FakeRawClient(payload))
    assert result["raw"] == 3  # dict 条目计数 (含 tombstone/空文本)
    assert result["kept"] == 1


def test_format_check_renders_ok_and_failure():
    ok = check_account("Dune", client=_FakeRawClient({"code": 200, "results": [
        _raw_item(1, text="hello world", minutes_ago=5)]}))
    fail = check_account("bad", client=_FakeRawClient(None))
    text = format_check([ok, fail])
    assert "== @Dune | 拉取 1 条 (转推 0) | 留存 1 条" in text
    assert "hello world" in text
    assert "@bad | [失败]" in text
    assert "合计: 2 账号 (可拉取 1)" in text


def test_main_check_mode(tmp_path, monkeypatch, capsys):
    payload = {"code": 200,
               "results": [_raw_item(1, text="hello", minutes_ago=0)]}
    config_path = tmp_path / "cfg.json"
    config_path.write_text(json.dumps({
        "accounts": [], "request_interval_seconds": 0}), encoding="utf-8")

    monkeypatch.setattr("tools.xwatch.FxClient",
                        lambda cfg=None: _FakeRawClient(payload))
    assert main(["--config", str(config_path),
                 "--check", "Dune,nobody", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["handle"] for row in rows] == ["Dune", "nobody"]
    assert all(row["ok"] for row in rows)

    # 全部失败 -> 退出码 1
    monkeypatch.setattr("tools.xwatch.FxClient",
                        lambda cfg=None: _FakeRawClient(None))
    assert main(["--config", str(config_path), "--check", "Dune"]) == 1
