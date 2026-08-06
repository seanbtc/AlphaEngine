"""推文抓取 — RSS 多源 + 批量历史 (snscrape) + 去重."""
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests


def _read_jsonl_tail(filepath: str, count: int) -> list[dict]:
    """读取 JSONL 文件末尾 N 行，不加载全部."""
    if not os.path.exists(filepath) or count <= 0:
        return []
    items = []
    with open(filepath, "r", encoding="utf-8") as f:
        # seek to near end, read last ~2x buffer, parse from there
        f.seek(0, os.SEEK_END)
        size = f.tell()
        buf_size = min(size, max(8192, count * 512))  # ~512 bytes per tweet line
        f.seek(max(0, size - buf_size))
        raw = f.read()
    for line in raw.splitlines():
        line = line.strip()
        if line:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return items[-count:]


class Fetcher:
    def __init__(self, config: dict, data_dir: str):
        self.username = config.get("username", "glassnode")
        self.max_tweets = config.get("max_tweets_per_fetch", 20)
        self.timeout = int(config.get("timeout_seconds", 20) or 20)
        self.rss_sources = config.get("rss_sources", [])
        self.data_dir = data_dir
        self.tweets_file = os.path.join(data_dir, "tweets.jsonl")

    def _load_existing_ids(self) -> set:
        ids = set()
        os.makedirs(self.data_dir, exist_ok=True)
        if os.path.exists(self.tweets_file) and os.path.getsize(self.tweets_file) > 0:
            with open(self.tweets_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            ids.add(str(json.loads(line).get("id", "")))
                        except json.JSONDecodeError:
                            continue
        return ids

    @staticmethod
    def _strip_html(text: str) -> str:
        text = re.sub(r"<br\s*/?>", "\n", text)
        text = re.sub(r"<[^>]+>", "", text)
        return text.strip()

    def fetch(self) -> list[dict]:
        existing_ids = self._load_existing_ids()
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        }

        all_tweets = []
        for template in self.rss_sources:
            url = template.format(user=self.username)
            print(f"[Fetcher] Trying RSS: {url}")
            try:
                resp = requests.get(url, headers=headers, timeout=self.timeout)
                if resp.status_code != 200:
                    print(f"[Fetcher]   HTTP {resp.status_code}")
                    continue
                root = ET.fromstring(resp.content)
                items = root.findall(".//item")
                if not items:
                    print(f"[Fetcher]   No items")
                    continue
                print(f"[Fetcher]   Got {len(items)} items")
                for it in items:
                    link = (it.findtext("link") or "").strip()
                    m = re.search(r"/status/(\d+)", link)
                    if not m:
                        continue
                    tid = m.group(1)
                    title = (it.findtext("title") or "").strip()
                    desc = (it.findtext("description") or "").strip()
                    pub = (it.findtext("pubDate") or "").strip()
                    content = self._strip_html(desc or title)
                    all_tweets.append({
                        "id": tid,
                        "date": pub,
                        "content": content,
                        "url": f"https://x.com/{self.username}/status/{tid}",
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    })
                all_tweets.sort(key=lambda t: t["id"])
                break
            except Exception as e:
                print(f"[Fetcher]   Error: {type(e).__name__}")
                continue
        else:
            print("[Fetcher] All RSS sources failed")

        new_tweets = [t for t in all_tweets if t["id"] not in existing_ids]
        if new_tweets:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.tweets_file, "a", encoding="utf-8") as f:
                for tweet in new_tweets:
                    f.write(json.dumps(tweet, ensure_ascii=False) + "\n")
        print(f"[Fetcher] {len(new_tweets)} new tweets "
              f"(total fetched: {len(all_tweets)}, known: {len(existing_ids)})")
        return new_tweets

    # ---- 批量历史抓取 (snscrape) ----

    def fetch_bulk(self, limit: int = 2000) -> int:
        """使用 snscrape 批量抓取历史推文。返回新写入数量。"""
        try:
            import snscrape.modules.twitter as sntwitter
        except ImportError:
            print("[Fetcher] snscrape not installed. Install: pip install snscrape")
            print("[Fetcher] 回退方案: 将历史推文 JSONL 复制到 data/tweets.jsonl "
                  "然后运行 --import")
            return 0

        existing_ids = self._load_existing_ids()
        print(f"[Fetcher] Bulk fetch: @{self.username} (limit={limit}, known={len(existing_ids)})")
        new_count = 0
        os.makedirs(self.data_dir, exist_ok=True)

        try:
            with open(self.tweets_file, "a", encoding="utf-8") as f:
                for i, tweet in enumerate(sntwitter.TwitterUserScraper(self.username).get_items()):
                    tid = str(tweet.id)
                    if tid in existing_ids:
                        continue
                    existing_ids.add(tid)
                    obj = {
                        "id": tid,
                        "date": tweet.date.isoformat() if tweet.date else "",
                        "content": tweet.rawContent or tweet.content or "",
                        "url": tweet.url or f"https://x.com/{self.username}/status/{tid}",
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    }
                    f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                    new_count += 1
                    if i % 100 == 0 and i > 0:
                        print(f"[Fetcher]   {i} scanned, {new_count} new ...")
                    if new_count >= limit:
                        break
        except Exception as e:
            print(f"[Fetcher] Bulk fetch error: {type(e).__name__}: {str(e)[:120]}")
            if new_count == 0:
                print("[Fetcher] 如果 snscrape 不可用，使用文件导入: "
                      "python -m src.run --import tweets_backup.jsonl")

        print(f"[Fetcher] Bulk fetch complete: {new_count} new tweets written")
        return new_count

    # ---- 文件导入 ----

    def import_file(self, filepath: str) -> int:
        """从 JSONL 或 JSON 数组文件导入推文。返回导入数量。"""
        if not os.path.exists(filepath):
            print(f"[Fetcher] File not found: {filepath}")
            return 0

        existing_ids = self._load_existing_ids()
        imported = 0
        os.makedirs(self.data_dir, exist_ok=True)

        with open(filepath, "r", encoding="utf-8") as src:
            content = src.read().strip()

        if content.startswith("["):
            items = json.loads(content)
        else:
            items = []
            for line in content.splitlines():
                line = line.strip()
                if line:
                    try:
                        items.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        with open(self.tweets_file, "a", encoding="utf-8") as dst:
            for obj in items:
                tid = str(obj.get("id", ""))
                if not tid or tid in existing_ids:
                    continue
                existing_ids.add(tid)
                obj.setdefault("fetched_at", datetime.now(timezone.utc).isoformat())
                dst.write(json.dumps(obj, ensure_ascii=False) + "\n")
                imported += 1

        print(f"[Fetcher] Imported {imported} tweets from {filepath}")
        return imported

    def get_recent(self, count: int = 20) -> list[dict]:
        """读取最近 N 条推文 (尾部读取，不加载全部)."""
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.tweets_file):
            return []
        tweets = _read_jsonl_tail(self.tweets_file, count)
        tweets.sort(key=lambda t: t.get("id", ""))
        return tweets

    def load_all_tweets(self) -> list[dict]:
        """一次性加载所有推文 (仅在 backfill/import 时使用)."""
        os.makedirs(self.data_dir, exist_ok=True)
        if not os.path.exists(self.tweets_file):
            return []
        tweets = []
        with open(self.tweets_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        tweets.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        tweets.sort(key=lambda t: t.get("id", ""))
        return tweets

    def count_tweets(self) -> int:
        """O(1) 统计推文总数 (不加载内容)."""
        if not os.path.exists(self.tweets_file):
            return 0
        with open(self.tweets_file, "r", encoding="utf-8") as f:
            return sum(1 for _ in f)
