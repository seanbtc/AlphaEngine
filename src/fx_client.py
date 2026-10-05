"""fxtwitter API v2 客户端 (免费层, 加装式推文源).

端点 (响应结构以官方 docs.fxembed.com 为准, 已实测):
- 用户时间线: GET /2/profile/{handle}/statuses
  (count <= 100, 支持 since/with_replies/cursor)
- 关键词搜索: GET /2/search
  (q/feed=latest/count/cursor; 支持 from:/min_faves: 等操作符)

约束:
- 仅依赖 requests/stdlib, 不引入新依赖; 单页 v1 (cursor 仅可选透传).
- 异常隔离: 超时/非 200/坏 JSON/结构变化 -> 打印 [FxClient] 并返回 [],
  绝不抛出异常 (不制造 fetcher outage)。
- 输出与 fetcher 完全一致的 tweet schema:
  {id, content, date, url, author, source, fetched_at, images}。
- 转推: 结构化识别 (reposted_by), 原作者在 retweet_whitelist 中保留,
  否则过滤 — 与 Fetcher._is_retweet/retweet_whitelist 语义对齐。
- 纯函数式优先: build_*/map_* 可独立单测, FxClient 仅做请求与异常隔离。
"""
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

API_BASE = "https://api.fxtwitter.com"
DEFAULT_USER_AGENT = "AlphaEngine/1.0"
MAX_COUNT = 100
_VALID_FEEDS = ("latest", "top", "media")


# ---- 纯函数: 参数/响应映射 ----

def _clamp_count(count, default: int = 20) -> int:
    """count 归一化到 [1, 100]; 非法回退 default (含 OverflowError: 1e400->inf)."""
    try:
        n = int(count)
    except (TypeError, ValueError, OverflowError):
        n = default
    return max(1, min(MAX_COUNT, n))


def _coerce_timeout(raw, default: float = 20.0) -> float:
    """超时归一化到 [1, 120] 秒; 非法/非有限回退 default.

    OverflowError: 病态大整数 (如 10**400) 转 float 时抛出。
    """
    try:
        v = float(raw)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    if not math.isfinite(v):
        return float(default)
    return max(1.0, min(120.0, v))


def _iso_from_unix(ts) -> str:
    """Unix 时间戳 (秒/毫秒) -> ISO8601 UTC; 非法返回 ""."""
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(v) or v <= 0:
        return ""
    if v >= 1e12:  # 毫秒
        v /= 1000.0
    try:
        return datetime.fromtimestamp(v, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _iso_from_created_at(raw) -> str:
    """Twitter 格式时间串 (如 'Sun Oct 04 16:56:39 +0000 2026') -> ISO8601 UTC."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError, OverflowError):
        return ""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return ""


def _is_content_image(url) -> bool:
    """内容图判定 (与 Fetcher._is_image_url 一致): 排除头像/资料图."""
    if not isinstance(url, str) or not re.match(r"^https?://", url):
        return False
    if re.search(r"/profile_images/|/profile_img/", url, re.IGNORECASE):
        return False
    if re.search(r"\.(jpe?g|png|gif|webp)(\?|$)", url, re.IGNORECASE):
        return True
    if re.search(r"pbs\.twimg\.com/media/", url, re.IGNORECASE):
        return True
    return False


def _dedup(items) -> list:
    seen, out = set(), []
    for it in items or []:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _format_media_info(alts, links) -> str:
    """与 Fetcher._format_media_info 同风格: alt 原样 + '[链接] url', ' | ' 连接."""
    parts, seen_alt = [], set()
    for a in alts or []:
        if not isinstance(a, str):
            continue
        a = a.strip()
        if a and a not in seen_alt:
            seen_alt.add(a)
            parts.append(a)
    seen_link = set()
    for u in links or []:
        if not isinstance(u, str):
            continue
        u = u.strip()
        if u and u not in seen_link:
            seen_link.add(u)
            parts.append(f"[链接] {u}")
    return " | ".join(parts)


def _media_entries(item) -> list:
    """取 media.all; 缺失时回退 photos/videos (兼容结构变化)."""
    if not isinstance(item, dict):
        return []
    media = item.get("media")
    if not isinstance(media, dict):
        return []
    allm = media.get("all")
    if isinstance(allm, list) and allm:
        return [m for m in allm if isinstance(m, dict)]
    entries = []
    for key in ("photos", "videos"):
        val = media.get(key)
        if isinstance(val, list):
            entries.extend(m for m in val if isinstance(m, dict))
    return entries


def _collect_images(item) -> list:
    """内容图 URL 列表 (仅 pbs.twimg.com/media 类, 排除头像; 无扩展名也可)."""
    urls = []
    for m in _media_entries(item):
        for key in ("url", "thumbnail_url"):
            u = m.get(key)
            if _is_content_image(u):
                urls.append(u)
    return _dedup(urls)


def _collect_media_extra(item, content: str) -> str:
    """媒体 alt + 外链附加信息 (外链已在正文中的不重复)."""
    alts = []
    for m in _media_entries(item):
        alt = m.get("altText")
        if alt is None:
            alt = m.get("alt_text")
        if isinstance(alt, str) and alt.strip():
            alts.append(alt)
    links = []
    raw_text = item.get("raw_text")
    facets = raw_text.get("facets") if isinstance(raw_text, dict) else None
    if isinstance(facets, list):
        for f in facets:
            if not isinstance(f, dict) or f.get("type") != "url":
                continue
            u = f.get("replacement") or f.get("original")
            if not isinstance(u, str) or not u.startswith("http"):
                continue
            if re.match(r"^https?://(?:[a-z0-9.-]*\.)?(x|twitter)\.com/", u, re.IGNORECASE):
                continue
            if u in content:
                continue
            links.append(u)
    return _format_media_info(alts, links)


def map_status(item, source: str, retweet_whitelist=None, fetched_at=None):
    """把单条 API v2 status 映射为 fetcher tweet schema.

    - 非法条目/无法救回 -> None; 任何内部异常打印 [FxClient] 并返回 None。
    - 转推 (reposted_by 非空): 仅原作者在 retweet_whitelist 中保留。
    """
    try:
        if not isinstance(item, dict):
            return None
        item_type = item.get("type")
        if isinstance(item_type, str) and item_type != "status":
            return None  # tombstone/thread 等非单帖结构
        tid = str(item.get("id") or "").strip()
        text = item.get("text")
        if not tid or not isinstance(text, str) or not text.strip():
            return None
        author = ""
        author_obj = item.get("author")
        if isinstance(author_obj, dict):
            author = str(author_obj.get("screen_name") or "").strip().lower()
        if item.get("reposted_by"):
            whitelist = {str(h).lower() for h in (retweet_whitelist or [])}
            if not author or author not in whitelist:
                return None
        content = text.strip()
        extra = _collect_media_extra(item, content)
        if extra:
            content = content + "\n" + extra
        url = item.get("url")
        url = str(url).strip() if isinstance(url, str) else ""
        if not url:
            url = f"https://x.com/{author or 'i'}/status/{tid}"
        date = _iso_from_unix(item.get("created_timestamp")) or \
            _iso_from_created_at(item.get("created_at"))
        if fetched_at is None:
            fetched_at = datetime.now(timezone.utc).isoformat()
        return {
            "id": tid,
            "content": content,
            "date": date,
            "url": url,
            "author": author,
            "source": source,
            "fetched_at": fetched_at,
            "images": _collect_images(item),
        }
    except Exception as exc:  # 防御: 结构变化不得外抛
        print(f"[FxClient] 映射异常: {type(exc).__name__}: {str(exc)[:120]}")
        return None


def map_results(results, source: str, retweet_whitelist=None) -> list:
    """批量映射 + 批内按 id 去重 (保持 API 顺序, 同一 fetched_at)."""
    if not isinstance(results, list):
        return []
    fetched_at = datetime.now(timezone.utc).isoformat()
    out, seen = [], set()
    for item in results:
        tweet = map_status(item, source, retweet_whitelist, fetched_at=fetched_at)
        if not tweet or tweet["id"] in seen:
            continue
        seen.add(tweet["id"])
        out.append(tweet)
    return out


def build_user_timeline_request(handle, count=20, since=None,
                                with_replies=False, cursor=None):
    """构造时间线请求 (url, params); count clamp [1,100]."""
    url = f"{API_BASE}/2/profile/{quote(str(handle), safe=':')}/statuses"
    params = {"count": _clamp_count(count, 20)}
    if since is not None:
        params["since"] = since
    if with_replies:
        params["with_replies"] = "1"
    if cursor:
        params["cursor"] = str(cursor)
    return url, params


def build_search_request(query, count=30, feed="latest", cursor=None):
    """构造搜索请求 (url, params); feed 非法回退 latest."""
    url = f"{API_BASE}/2/search"
    params = {
        "q": str(query),
        "feed": feed if feed in _VALID_FEEDS else "latest",
        "count": _clamp_count(count, 30),
    }
    if cursor:
        params["cursor"] = str(cursor)
    return url, params


# ---- 客户端 (请求 + 异常隔离) ----

class FxClient:
    """fxtwitter API v2 瘦客户端; 所有公开方法保证不抛异常, 失败返回 []."""

    def __init__(self, config: dict = None, retweet_whitelist=None):
        cfg = config if isinstance(config, dict) else {}
        ua = str(cfg.get("user_agent") or DEFAULT_USER_AGENT).strip()
        self.user_agent = ua or DEFAULT_USER_AGENT
        self.timeout = _coerce_timeout(cfg.get("timeout_seconds"), 20.0)
        self.retweet_whitelist = {str(h).lower() for h in (retweet_whitelist or [])}

    def _get_json(self, url, params):
        """GET + JSON 解析; 任何异常/非 200 打印 [FxClient] 并返回 None (204 -> None)."""
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        try:
            resp = requests.get(url, params=params, headers=headers,
                                timeout=self.timeout)
        except Exception as exc:
            print(f"[FxClient] 请求异常 {type(exc).__name__}: {str(exc)[:160]}")
            return None
        if resp.status_code == 204:
            return None  # since 之后无新帖, 正常空结果
        if resp.status_code != 200:
            print(f"[FxClient] HTTP {resp.status_code}: {url}")
            return None
        try:
            payload = resp.json()
        except Exception as exc:
            print(f"[FxClient] JSON 解析失败 {type(exc).__name__}: {str(exc)[:120]}")
            return None
        if not isinstance(payload, dict):
            print(f"[FxClient] 响应结构异常: 顶层为 {type(payload).__name__}")
            return None
        return payload

    def _extract(self, payload, source: str) -> list:
        code = payload.get("code")
        if code is not None and code != 200:
            print(f"[FxClient] 响应 code={code} ({source})")
            return []
        results = payload.get("results")
        if not isinstance(results, list):
            print(f"[FxClient] 响应结构异常: results 非列表 ({source})")
            return []
        return map_results(results, source, self.retweet_whitelist)

    def fetch_user_timeline(self, handle, count=20, since=None,
                            with_replies=False, cursor=None) -> list:
        """用户时间线 (单页, 客户端按 count 截断). 失败返回 []."""
        handle = str(handle or "").strip()
        if not handle:
            print("[FxClient] handle 为空, 跳过时间线")
            return []
        url, params = build_user_timeline_request(
            handle, count=count, since=since,
            with_replies=with_replies, cursor=cursor)
        payload = self._get_json(url, params)
        if payload is None:
            return []
        # 实测时间线端点可能忽略 count 返回整页, 客户端截断保证上限
        return self._extract(payload, f"fxtwitter/{handle}")[
            :_clamp_count(count, 20)]

    def search(self, query, count=30, feed="latest", cursor=None) -> list:
        """关键词搜索 (单页, 支持操作符). 失败返回 []."""
        q = str(query or "").strip()
        if not q:
            print("[FxClient] 搜索词为空, 跳过")
            return []
        url, params = build_search_request(q, count=count, feed=feed, cursor=cursor)
        payload = self._get_json(url, params)
        if payload is None:
            return []
        return self._extract(payload, f"fxtwitter/search:{q}")[
            :_clamp_count(count, 30)]
