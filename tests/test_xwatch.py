"""xwatch 独立采集工具 — 配置/去重/异常隔离/dry-run/状态原子写 (离线单测, 不触网).

覆盖:
- 配置解析: 默认值/非法值回退/accounts 清洗去重/缺文件与坏 JSON;
- 随仓库配置: 9 观察账号与固定参数;
- 采集: 跨两次运行按 id 去重只追加新条目;
- 单账号异常隔离: 失败账号记 last_error, 其它账号照常, 账号间 sleep;
- dry-run 零写盘 (run_once 与 CLI 两级);
- state.json 结构 + 原子写 (无 .tmp 残留);
- 汇总计数与报告文本; main 退出码 (2/1/0);
- 坏数据行跳过。
"""
import json
import sys
from pathlib import Path

import pytest

_ALPHA_ROOT = Path(__file__).resolve().parents[1]
if str(_ALPHA_ROOT) not in sys.path:
    sys.path.insert(0, str(_ALPHA_ROOT))

from tools.xwatch import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
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


def test_shipped_config_matches_observation_accounts():
    config = load_config(DEFAULT_CONFIG_PATH)
    assert config["accounts"] == [
        "ai_9684xtpa", "Murphychen888", "evilcos", "0xCryptoChan", "EmberCN",
        "lookonchain", "whale_alert", "FarsideUK", "hupzy_agent"]
    assert config["data_dir"] == "data/xwatch"
    assert config["max_per_account"] == 20
    assert config["with_replies"] is False
    assert config["request_interval_seconds"] == 0.5


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
