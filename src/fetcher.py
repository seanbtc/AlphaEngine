"""推文抓取 — X 网页抓取 + 本地网页解析 + 批量历史 (snscrape) + 去重."""
import json
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

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
        self.usernames = config.get("usernames") or [self.username]
        self.max_tweets = config.get("max_tweets_per_fetch", 20)
        self.timeout = int(config.get("timeout_seconds", 20) or 20)
        self.retweet_whitelist = {h.lower() for h in config.get("retweet_whitelist", [])}
        self.web_fallback = config.get("web_fallback", True)
        web_dir = config.get("web_dir", "web")
        if not os.path.isabs(web_dir):
            web_dir = os.path.normpath(
                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), web_dir))
        self.web_dir = web_dir
        self.data_dir = data_dir
        self.tweets_file = os.path.join(data_dir, "tweets.jsonl")
        self._x_com_reachable = None

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

    @staticmethod
    def _extract_rt_handle(text: str):
        """从转推内容中提取原作者 handle。返回 handle 或 None (非转推)."""
        t = text.strip()
        # 1. 纯转推: RT @user / rsshub 转发自: @user / nitter 英文 Reposted by
        m = re.match(r"^RT\s+@([A-Za-z0-9_]+)", t)
        if m:
            return m.group(1)
        m = re.search(r"转发自:?\s*@([A-Za-z0-9_]+)", t)
        if m:
            return m.group(1)
        m = re.search(r"^Reposted\s+(by\s+)?@([A-Za-z0-9_]+)", t, re.IGNORECASE)
        if m:
            return m.group(2)
        # 2. 引用转推 (nitter): 内嵌原作者链接, 以 "— https://..." 分隔
        m = re.search(
            r"—\s*https?://(?:nitter\.[^/\s]+|x\.com|twitter\.com)/"
            r"([A-Za-z0-9_]+)/status/\d+", t)
        if m:
            return m.group(1)
        # 3. 引用转推 (其他格式): "作者 (@handle)" 标记与同名 status 链接同时出现
        m = re.search(r"\(@([A-Za-z0-9_]+)\)", t)
        if m:
            h = m.group(1)
            if re.search(
                rf"https?://(?:nitter\.[^/\s]+|x\.com|twitter\.com)/{re.escape(h)}/status/\d+",
                t, re.IGNORECASE):
                return h
        return None

    def _is_retweet(self, content: str) -> bool:
        """判断 nitter/rsshub 描述是否为转推 (纯转推或引用转推).

        白名单 (retweet_whitelist) 中的原作者转推保留, 其余剔除.
        返回 True = 应过滤, False = 保留.
        """
        if not content:
            return False
        handle = self._extract_rt_handle(content)
        if handle is None:
            return False  # 非转推, 保留
        if handle.lower() in self.retweet_whitelist:
            return False  # 白名单作者的转推, 保留
        return True

    def _check_x_com(self) -> bool:
        """检测 x.com 是否可达 (详细版)."""
        try:
            resp = requests.get("https://x.com", timeout=8,
                                headers={"User-Agent": "Mozilla/5.0"})
            print(f"[Fetcher] x.com 可达性检测: HTTP {resp.status_code}, "
                  f"长度={len(resp.text)}, URL={resp.url}")
            if resp.status_code != 200:
                print(f"[Fetcher]   响应头: {dict(resp.headers)}")
            return resp.status_code == 200
        except Exception as e:
            print(f"[Fetcher] x.com 不可达: {type(e).__name__}: {e}")
            return False

    def _fetch_x_web(self, user: str) -> str:
        """抓取 X 主页 HTML, 返回页面内容或空字符串."""
        url = f"https://x.com/{user}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=self.timeout)
            print(f"[Fetcher]   HTTP {resp.status_code}, 长度={len(resp.text)}, "
                  f"最终URL={resp.url}")
            if resp.status_code != 200:
                print(f"[Fetcher]   响应头 Content-Type: "
                      f"{resp.headers.get('content-type', '?')}")
                return ""

            import re
            has_article = len(re.findall(r'<article', resp.text))
            has_testid = len(re.findall(r'data-testid=', resp.text))
            has_status = len(re.findall(r'/status/\d+', resp.text))
            has_tweet = "tweet" in resp.text.lower()
            print(f"[Fetcher]   <article>标签: {has_article}, "
                  f"data-testid: {has_testid}, /status/链接: {has_status}, "
                  f"含'tweet'文本: {has_tweet}")
            if len(resp.text) > 100:
                m = re.search(r'<title>([^<]*)</title>', resp.text)
                if m:
                    print(f"[Fetcher]   页面标题: {m.group(1).strip()}")

            return resp.text
        except Exception as e:
            print(f"[Fetcher]   Error: {type(e).__name__}: {str(e)[:120]}")
        return ""

    def fetch(self) -> list[dict]:
        print("[Fetcher] === 开始抓取推文 ===")
        existing_ids = self._load_existing_ids()
        all_tweets = []
        seen_ids = set()
        tracked = {u.lower() for u in self.usernames}

        # 检测 x.com 可达性 (仅首次)
        if self._x_com_reachable is None:
            print("[Fetcher] 检测 x.com 可达性...")
            self._x_com_reachable = self._check_x_com()
            if self._x_com_reachable:
                print("[Fetcher]   ✓ x.com 可达, 将直接读取网页内容")
            else:
                print("[Fetcher]   ✗ x.com 不可达, 将使用本地 web/ 目录")

        # 尝试从 x.com 抓取
        if self._x_com_reachable:
            for user in self.usernames:
                print(f"[Fetcher] 读取 https://x.com/{user} ...")
                html = self._fetch_x_web(user)
                if not html:
                    print(f"[Fetcher]   ✗ 无法读取 {user} 的主页")
                    continue
                articles = self._parse_articles(html)
                if len(articles) == 0 and "<article" in html:
                    # 标准解析失败 (可能无 data-testid), 尝试宽松解析
                    lenient = self._parse_articles_lenient(html, owner=user)
                    if lenient:
                        print(f"[Fetcher]   ✓ 标准解析 0 条, 宽松解析 {len(lenient)} 条 "
                              f"(无 data-testid, 用宽松解析)")
                        articles = lenient
                print(f"[Fetcher]   ✓ 解析到 {len(articles)} 条推文")
                owner = user.lower()
                for art in articles:
                    tid = art["id"]
                    if tid in seen_ids:
                        continue
                    author = art["author"] or owner
                    if author != owner and author not in tracked and author not in self.retweet_whitelist:
                        continue
                    content = art["content"]
                    if self._is_retweet(content):
                        continue
                    seen_ids.add(tid)
                    all_tweets.append({
                        "id": tid,
                        "date": art["date"],
                        "content": content,
                        "url": f"https://x.com/{author}/status/{tid}",
                        "author": author,
                        "source": f"x.com/{user}",
                        "fetched_at": datetime.now(timezone.utc).isoformat(),
                    })

        # 兜底: x.com 无数据时读取本地 web/ 目录
        if not all_tweets and self.web_fallback:
            print("[Fetcher] --- 回退到本地网页 (web/) ---")
            web_new = self.fetch_web()
            for t in web_new:
                if t["id"] not in seen_ids and t["id"] not in existing_ids:
                    all_tweets.append(t)
                    seen_ids.add(t["id"])

        all_tweets.sort(key=lambda t: t["id"])
        if len(all_tweets) > self.max_tweets:
            all_tweets = all_tweets[-self.max_tweets:]

        new_tweets = [t for t in all_tweets if t["id"] not in existing_ids]
        if new_tweets:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.tweets_file, "a", encoding="utf-8") as f:
                for tweet in new_tweets:
                    f.write(json.dumps(tweet, ensure_ascii=False) + "\n")

        print(f"[Fetcher] === 结果: {len(new_tweets)} 条新推文 "
              f"(本次获取: {len(all_tweets)}, 已有: {len(existing_ids)}) ===")
        for t in new_tweets:
            snippet = (t.get("content", "") or "").replace("\n", " ")[:60]
            print(f"[Fetcher]   NEW: {t.get('url','?')} | {snippet}")
        return new_tweets

    # ---- 本地网页兜底 (web/ 目录保存的 X 主页) ----

    @staticmethod
    def _dedup(items: list) -> list:
        seen = set()
        out = []
        for it in items:
            if it not in seen:
                seen.add(it)
                out.append(it)
        return out

    @staticmethod
    def _page_owner(html: str, filename: str) -> str:
        """从保存网页注释或文件名推断主页所属账号 (小写)."""
        m = re.search(r"saved from url=\([^)]*\)\s*https?://[^\s'\"]*"
                      r"(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)", html)
        if m:
            return m.group(1).lower()
        m = re.search(r"\(@([A-Za-z0-9_]+)\)", filename)
        if m:
            return m.group(1).lower()
        return ""

    def _parse_articles(self, html: str) -> list[dict]:
        """解析 X 主页 HTML 中每条 <article>, 返回主推文信息列表."""
        tweets = []
        for m in re.finditer(r"<article\b", html):
            rest = html[m.start():]
            end = rest.find("</article>")
            if end < 0:
                break
            block = rest[:end]

            parser = _SavedPageParser()
            try:
                parser.feed(block)
            except Exception:
                continue

            ids = self._dedup(parser.status_ids)
            if not ids or not parser.tweet_texts:
                continue
            content = parser.tweet_texts[0]
            # 附加图片 alt 文本 + 外链 URL (信息补全)
            extra = self._format_media_info(
                parser.img_alts, parser.external_links)
            if extra:
                content = content + "\n" + extra
            tweets.append({
                "id": ids[0],
                "content": content,
                "date": parser.times[0] if parser.times else "",
                "author": parser.avatars[0].lower() if parser.avatars else "",
                "images": self._dedup(parser.image_urls),
            })
        return tweets

    @staticmethod
    def _format_media_info(img_alts: list, links: list) -> str:
        """把图片说明和外链格式化为附加信息 (均去重、去空)."""
        parts = []
        seen_alt = set()
        for a in img_alts:
            a = a.strip()
            if a and a not in seen_alt:
                seen_alt.add(a)
                parts.append(a)
        seen_link = set()
        for u in links:
            u = u.strip()
            if u and u not in seen_link:
                seen_link.add(u)
                parts.append(f"[链接] {u}")
        if not parts:
            return ""
        return " | ".join(parts)

    @staticmethod
    def debug_dump_first_article(html: str, max_chars: int = 3000) -> None:
        """打印第一个 <article> 块的原始 HTML, 用于诊断解析失败原因."""
        m = re.search(r"<article\b", html)
        if not m:
            print("[Fetcher]   [debug] 页面无 <article> 标签")
            return
        rest = html[m.start():]
        end = rest.find("</article>")
        if end < 0:
            print("[Fetcher]   [debug] <article> 未闭合")
            return
        block = rest[:end]
        # 压缩空白便于查看
        block = re.sub(r"\s+", " ", block)
        print(f"[Fetcher]   [debug] 第一个 <article> 块 ({len(block)} chars):")
        print(f"[Fetcher]   [debug] {block[:max_chars]}")
        print(f"[Fetcher]   [debug] --- 块结束 ---")

    def _parse_articles_lenient(self, html: str, owner: str = "") -> list[dict]:
        """宽松解析: 不依赖 data-testid, 从 <article> 内直接提取.

        适用于 X 实时返回的无 JS 版本 HTML (无 data-testid 属性).
        owner: 页面所属账号 handle, 用于清除 header 噪音 (显示名/@handle/时间).
        """
        tweets = []
        for m in re.finditer(r"<article\b", html):
            rest = html[m.start():]
            end = rest.find("</article>")
            if end < 0:
                break
            block = rest[:end]

            # 提取 status id
            status_ids = []
            for sm in re.finditer(r'href="[^"]*/status/(\d+)"', block):
                status_ids.append(sm.group(1))
            if not status_ids:
                continue
            status_ids = self._dedup(status_ids)

            # 提取时间 datetime 属性
            times = re.findall(r'<time[^>]*datetime="([^"]*)"', block)
            time_val = times[0] if times else ""

            # 提取作者: 取第一个 avatar alt 中的 handle (转推卡=原作者, 普通推=主人)
            author = ""
            avatars = re.findall(r'alt="@?([A-Za-z0-9_]+)"', block)
            if avatars:
                author = avatars[0].lower()
            if not author:
                author = owner.lower() if owner else ""

            # 提取纯文本: 去掉 script/style/标签
            text = re.sub(r"<script[^>]*>.*?</script>", "", block, flags=re.S)
            text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.S)
            text = re.sub(r"<br\s*/?>", "\n", text)
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n\s*", "\n", text).strip()

            # 去掉 header: "显示名 @handle 时间" 前缀 (首个 @提及 之前的部分)
            mh = re.search(r"@\w+", text[:250])
            if mh:
                text = text[mh.end():].strip()
                # @handle 之后通常是相对时间 (如 1h/2d), 一并清除 (含残留数字)
                text = re.sub(r"^[\s·.,•|:-]*\d+[smhd]?[\s·.,•|:-]*", "", text).strip()
            else:
                text = re.sub(r"^[\s·.,•|:-]*", "", text).strip()
            text = re.sub(r"^(?:Replying\s+to\s+@[A-Za-z0-9_]+\.?)\s*", "", text,
                          flags=re.IGNORECASE).strip()

            # 截断底部操作计数: "... 1.2K Reposts 56 Likes ..." 保留正文
            m_foot = re.search(
                r"\d+(?:[\d,\.]+)?[KMB]?\s*(?:Reposts?|Replies?|Likes?|Views?|"
                r"Quotes?|Bookmarks?|Shares?)",
                text, re.IGNORECASE)
            if m_foot:
                text = text[:m_foot.start()].strip()

            # 去掉残留行: 纯数字/纯计数 或 单独 "Reposts/Likes" 词
            lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
            cleaned = []
            for ln in lines:
                if re.fullmatch(r"[\d,\.]+[KMB]?", ln):
                    continue
                if re.fullmatch(r"(Reposts?|Replies?|Likes?|Views?|Quotes?|Bookmarks?|Shares?)",
                                ln, re.IGNORECASE):
                    continue
                cleaned.append(ln)
            text = " ".join(cleaned).strip()

            # 提取图片 alt + 外链, 附加到正文
            img_alts = []
            image_urls = []
            for im in re.finditer(r'<img\b[^>]*\balt="([^"]*)"[^>]*>', block):
                alt = im.group(1).strip()
                if alt and not alt.startswith("@"):
                    img_alts.append(alt)
                m_src = re.search(r'\bsrc="([^"]+)"', im.group(0))
                if m_src and self._is_image_url(m_src.group(1)):
                    image_urls.append(m_src.group(1))
            links = []
            for lm in re.finditer(r'<a\b[^>]*\bhref="([^"]*)"', block):
                u = lm.group(1).strip()
                if u.startswith("http") and \
                        not re.match(r"^https?://(?:[a-z0-9.-]*\.)?(x|twitter)\.com/", u):
                    links.append(u)
            extra = self._format_media_info(img_alts, links)
            if extra:
                text = text + "\n" + extra

            if not text:
                continue

            tweets.append({
                "id": status_ids[0],
                "content": text,
                "date": time_val,
                "author": author.lower(),
                "images": self._dedup(image_urls),
            })
        return tweets

    def fetch_web(self) -> list[dict]:
        """解析 web/ 目录保存的 X 主页 HTML, 提取推文写入 tweets.jsonl.

        离线兜底: 抓不到在线数据时用. 返回新写入的推文列表.
        """
        if not self.web_fallback:
            return []
        if not os.path.isdir(self.web_dir):
            print(f"[Fetcher] Web fallback: 目录不存在 {self.web_dir}")
            return []

        existing_ids = self._load_existing_ids()
        tracked = {u.lower() for u in self.usernames}
        all_tweets = []
        seen = set()

        for fn in sorted(os.listdir(self.web_dir)):
            if not fn.lower().endswith(".html"):
                continue
            path = os.path.join(self.web_dir, fn)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    html = f.read()
            except Exception as e:
                print(f"[Fetcher] Web fallback: 读取 {fn} 失败: {e}")
                continue
            owner = self._page_owner(html, fn)
            articles = self._parse_articles(html)
            print(f"[Fetcher] Web fallback: {fn} owner=@{owner or '?'} articles={len(articles)}")
            for art in articles:
                tid = art["id"]
                if tid in seen:
                    continue
                author = art["author"] or owner
                # 页面主人以外账号的主推文 → 视为转推/引用:
                # 仅白名单/跟踪账号保留, 其余剔除
                if owner and author and author != owner:
                    if author not in tracked and author not in self.retweet_whitelist:
                        print(f"[Fetcher] Web fallback: skip 第三方转推 @{author} {tid}")
                        continue
                content = art["content"]
                if self._is_retweet(content):
                    print(f"[Fetcher] Web fallback: skip retweet {tid}")
                    continue
                seen.add(tid)
                all_tweets.append({
                    "id": tid,
                    "date": art["date"],
                    "content": content,
                    "url": f"https://x.com/{author or owner}/status/{tid}",
                    "author": author,
                    "source": f"web/{fn}",
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                })

        if not all_tweets:
            print("[Fetcher] Web fallback: 无有效推文")
            return []
        all_tweets.sort(key=lambda t: t["id"])
        new_tweets = [t for t in all_tweets if t["id"] not in existing_ids]
        if new_tweets:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self.tweets_file, "a", encoding="utf-8") as f:
                for tweet in new_tweets:
                    f.write(json.dumps(tweet, ensure_ascii=False) + "\n")
        print(f"[Fetcher] Web fallback: {len(new_tweets)} new tweets "
              f"(parsed: {len(all_tweets)}, known: {len(existing_ids)})")
        for t in new_tweets:
            snippet = (t.get("content", "") or "").replace("\n", " ")[:60]
            print(f"[Fetcher] Web fallback: NEW: {t.get('url','?')} | "
                  f"{t.get('date','?')[:16]} | {snippet}")
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
                    rt = getattr(tweet, "retweetedTweet", None)
                    if rt is not None:
                        orig = getattr(rt, "username", "") or ""
                        if orig.lower() not in self.retweet_whitelist:
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


class _SavedPageParser(HTMLParser):
    """解析保存的 X 主页 HTML 中单个 <article> 块的关键信息.

    记录文档顺序出现的: 状态 ID、tweetText 全文 (处理嵌套 div)、时间、作者头像账号.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.status_ids: list[str] = []
        self.tweet_texts: list[str] = []
        self.times: list[str] = []
        self.avatars: list[str] = []
        self.img_alts: list[str] = []
        self.external_links: list[str] = []
        self.image_urls: list[str] = []
        self._tweettext_depth = 0
        self._tweettext_buf: list[str] = []
        self._in_time = False
        self._time_val = ""

    @staticmethod
    def _is_external(url: str) -> bool:
        return bool(re.match(r"^https?://", url)) and \
            not re.match(r"^https?://(?:[a-z0-9.-]*\.)?(x|twitter)\.com/", url)

    @staticmethod
    def _is_image_url(url: str) -> bool:
        return bool(re.match(r"^https?://", url)) and \
            bool(re.search(r"\.(jpe?g|png|gif|webp)(\?|$)", url, re.IGNORECASE))

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        href = d.get("href", "")
        m = re.search(r"/status/(\d+)", href)
        if m:
            self.status_ids.append(m.group(1))
        if tag == "a" and self._is_external(href):
            self.external_links.append(href)
        if tag == "img":
            alt = d.get("alt", "")
            if alt and not alt.startswith("@"):
                self.img_alts.append(alt)
            src = d.get("src", "")
            if self._is_image_url(src):
                self.image_urls.append(src)
        testid = d.get("data-testid", "")
        if testid == "tweetText":
            self._tweettext_depth = 1
            self._tweettext_buf = []
        elif self._tweettext_depth and tag == "div":
            self._tweettext_depth += 1
        elif self._tweettext_depth and tag == "br":
            self._tweettext_buf.append("\n")
        if tag == "time" and not self._in_time:
            self._in_time = True
            self._time_val = d.get("datetime", "")
        if testid.startswith("UserAvatar-Container-"):
            self.avatars.append(testid[len("UserAvatar-Container-"):])

    def handle_endtag(self, tag):
        if self._tweettext_depth and tag == "div":
            self._tweettext_depth -= 1
            if self._tweettext_depth == 0:
                text = "".join(self._tweettext_buf).strip()
                if text:
                    self.tweet_texts.append(text)
        if tag == "time" and self._in_time:
            self.times.append(self._time_val)
            self._in_time = False
            self._time_val = ""

    def handle_data(self, data):
        if self._tweettext_depth:
            self._tweettext_buf.append(data)
