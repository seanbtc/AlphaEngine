"""推文时间戳兜底回归 (Phase 0).

X 宽松解析路径 2026-09 起常拿不到 <time datetime> (72/72 空),
空时间戳会让 AI 无法建立"何时发生"的锚点。fetch 落盘前用 fetched_at 兜底,
analyzer 对历史残行再兜一层。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analyzer import Analyzer
from src.fetcher import Fetcher


def test_normalize_dates_fills_missing_and_keeps_existing():
    tweets = [
        {"id": "1", "date": "", "content": "a"},
        {"id": "2", "date": None, "content": "b"},
        {"id": "3", "date": "2026-09-26T12:00:00Z", "content": "c"},
    ]
    Fetcher._normalize_dates(tweets, fetched_at="2026-11-10T04:00:00Z")

    assert tweets[0]["date"] == "2026-11-10T04:00:00Z"
    assert tweets[1]["date"] == "2026-11-10T04:00:00Z"
    assert tweets[2]["date"] == "2026-09-26T12:00:00Z"
    assert all(t["fetched_at"] == "2026-11-10T04:00:00Z" for t in tweets)


class _FetcherStub(Fetcher):
    """绕过 __init__, 只验证 fetch() 落盘前会补 date。"""

    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.tweets_file = str(Path(data_dir) / "tweets.jsonl")
        self.max_tweets = 100
        self.web_fallback = False
        self.usernames = ["glassnode"]

    def _load_existing_ids(self):
        return set()

    def _fetch_live_tweets(self):
        return [{"id": "1", "content": "hello", "date": "", "author": "glassnode"}]

    def _merge_fx(self, tweets):
        return tweets


def test_fetch_persists_non_empty_date(tmp_path):
    fetcher = _FetcherStub(tmp_path)
    new_tweets = fetcher.fetch()

    assert new_tweets[0]["date"], "date 不应为空"
    row = json.loads((tmp_path / "tweets.jsonl").read_text(encoding="utf-8").strip())
    assert row["date"] == row["fetched_at"]
    assert row["date"]


def _analyzer():
    analyzer = object.__new__(Analyzer)
    analyzer.send_tweet_content = True
    analyzer.last_sent_tweet_ids = []
    analyzer.link_max_total_chars = 10000
    analyzer.papermark_max_chars = 6000
    return analyzer


def test_format_tweets_falls_back_to_fetched_at():
    text = _analyzer()._format_tweets([
        {"id": "1", "date": None, "fetched_at": "2026-11-10T04:00:00Z",
         "content": "hello"},
        {"id": "2", "content": "no date at all"},
    ])

    assert "[2026-11-10T04:00]" in text
    assert "[?]" in text
