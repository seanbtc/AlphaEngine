"""外链内容读取器 — Papermark 查看器 (签名 PDF) + 通用 HTML 元信息.

策略链 (逐级 try/except 降级, 永不抛出):
  1. Papermark: 短链跳转 → 解析 __NEXT_DATA__ → POST /api/views 换签名 PDF → pypdf 抽取文本
  2. 通用 HTML: title / meta description / og:description / ld+json / 去标签正文
  3. 全部失败返回 ("", "")

依赖: requests (已有) + pypdf (可选, 缺失时不下载 PDF 直接降级)。
每个 URL 只抓取一次页面 HTML (Papermark 探测与 HTML 回退复用同一响应);
仅成功结果进入进程内缓存 (失败不缓存, 下次调用自动重试)。
"""
import io
import json
import re
import time
from html import unescape
from urllib.parse import urlparse

import requests

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

_PAPERMARK_HOST = "papermark.com"
_PAPERMARK_HOST_SUFFIX = ".papermark.com"
_PAPERMARK_VIEWS_API = "https://www.papermark.com/api/views"

_MAX_PDF_BYTES = 30 * 1024 * 1024  # 单个 PDF 下载上限 (超限放弃)
_MAX_PDF_PAGES = 40                # 单个 PDF 抽取页数上限

_DEFAULTS = {
    "link_timeout_seconds": 15,
    "pdf_timeout_seconds": 30,
    "papermark_max_chars": 6000,
    "link_max_chars": 1500,
    "link_viewer_email": "reader@example.com",
}

# 同 URL 进程内缓存 (仅成功结果; 失败不缓存, 允许下次重试); 超过上限整体清空
_CACHE: dict[str, tuple[str, str]] = {}
_CACHE_MAX = 256


def read_link_content(url: str, config: dict | None = None,
                      session=None) -> tuple[str, str]:
    """读取外链正文。返回 (text, source), source ∈ {"papermark-pdf", "html-meta", ""}。

    config 键: link_timeout_seconds / pdf_timeout_seconds / papermark_max_chars /
    link_max_chars / link_viewer_email; session 可注入 requests.Session (默认 requests)。
    """
    if not isinstance(url, str) or not re.match(r"^https?://", url):
        return "", ""
    cached = _CACHE.get(url)
    if cached is not None:
        return cached
    cfg = _merge_config(config)
    http = session if session is not None else requests

    # 只抓一次页面 HTML: Papermark 探测与 HTML 回退复用同一响应
    resp = None
    try:
        resp = http.get(url, headers=_headers(),
                        timeout=_as_int(cfg.get("link_timeout_seconds"), 15),
                        allow_redirects=True)
    except Exception as exc:
        print(f"[LinkReader] 页面抓取失败: {_safe_error(exc)}")

    result: tuple[str, str] = ("", "")

    # a) Papermark 适配 (短链 → papermark.com)
    if resp is not None:
        try:
            text = _read_papermark(resp, url, cfg, http)
            if text:
                result = (text, "papermark-pdf")
        except Exception as exc:
            print(f"[LinkReader] Papermark 适配失败: {_safe_error(exc)}")

    # b) 通用 HTML (复用已抓取的 HTML, 不再重复请求)
    if not result[0]:
        try:
            html = resp.text if (resp is not None and resp.status_code == 200) else ""
            text = _read_html(url, cfg, html=html)
            if text:
                result = (text, "html-meta")
        except Exception as exc:
            print(f"[LinkReader] HTML 提取失败: {_safe_error(exc)}")

    if result[0]:
        print(f"[LinkReader] {result[1]}, {len(result[0])} chars | {url}")
        _cache_store(url, result)  # 仅成功结果缓存
    else:
        print(f"[LinkReader] 无可用内容: {url}")
    return result


def clear_cache():
    """清空进程内 URL 缓存 (测试/排障用)."""
    _CACHE.clear()


# ---- Papermark 链路 ----

def _read_papermark(resp, url: str, cfg: dict, http) -> str:
    """解析已抓取的查看器响应, 换签名 PDF 并抽取文本; 不适用/失败返回 ""."""
    final_url = str(getattr(resp, "url", "") or url)
    if getattr(resp, "status_code", None) != 200 or not _is_papermark_url(final_url):
        return ""
    html = resp.text or ""
    link_id, version_id = _parse_next_data(html, final_url)
    if not link_id or not version_id:
        return ""
    if not _pypdf_available():
        print("[LinkReader] pypdf 未安装, 跳过 Papermark PDF 下载")
        return ""

    payload = {
        "documentVersionId": version_id,
        "linkId": link_id,
        "email": str(cfg.get("link_viewer_email") or "").strip() or "reader@example.com",
    }
    timeout = _as_int(cfg.get("link_timeout_seconds"), 15)
    api_resp = http.post(_PAPERMARK_VIEWS_API, json=payload,
                         headers=_headers(final_url), timeout=timeout)
    if api_resp.status_code != 200:
        return ""
    try:
        data = api_resp.json() or {}
    except ValueError:
        return ""
    file_url = _pick_file_url(data)
    file_type = str(data.get("fileType") or "").lower()
    if not file_url or (file_type and file_type != "pdf"):
        return ""

    pdf_resp = http.get(file_url, headers=_headers(final_url),
                        timeout=_as_int(cfg.get("pdf_timeout_seconds"), 30), stream=True)
    if pdf_resp.status_code != 200:
        return ""
    content = _read_limited_bytes(pdf_resp, _MAX_PDF_BYTES)
    if not content or not content.startswith(b"%PDF"):
        return ""
    text = _clean_text(_extract_pdf_text(content))
    if not text:
        return ""
    return _truncate(text, _as_int(cfg.get("papermark_max_chars"), 6000))


def _is_papermark_url(url: str) -> bool:
    """严格校验主机名: 仅 papermark.com 及其子域 (防 papermark.com.evil.io 绕过)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host == _PAPERMARK_HOST or host.endswith(_PAPERMARK_HOST_SUFFIX)


def _parse_next_data(html: str, final_url: str) -> tuple[str, str]:
    """从查看器页面解析 (linkId, documentVersionId); 失败返回 ("", "")."""
    link_id = ""
    m = re.search(r"/view/([A-Za-z0-9_-]+)", final_url or "")
    if m:
        link_id = m.group(1)
    version_id = ""
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([\s\S]*?)</script>', html or "")
    if m:
        try:
            data = json.loads(m.group(1))
            link = (((data.get("props") or {}).get("pageProps") or {})
                    .get("linkData") or {}).get("link") or {}
            link_id = str(link.get("id") or link_id)
            versions = ((link.get("document") or {}).get("versions") or [])
            if versions:
                version_id = str((versions[0] or {}).get("id") or "")
        except (ValueError, TypeError, AttributeError):
            pass
    if not version_id:
        # 兜底: 直接从原始 HTML 正则取第一个 version id
        m = re.search(r'"versions"\s*:\s*\[\s*\{[^}]*?"id"\s*:\s*"([^"]+)"', html or "")
        if m:
            version_id = m.group(1)
    return link_id, version_id


def _pick_file_url(data: dict) -> str:
    value = (data or {}).get("file")
    if isinstance(value, dict):
        value = value.get("url") or value.get("file")
    return str(value or "")


def _pypdf_available() -> bool:
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def _read_limited_bytes(resp, limit: int) -> bytes:
    """流式读取响应体, 超过 limit 返回 b"" (避免超大文件占用内存)."""
    iter_content = getattr(resp, "iter_content", None)
    if iter_content is None:
        data = resp.content or b""
        return data if len(data) <= limit else b""
    chunks = []
    size = 0
    for chunk in iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > limit:
            return b""
        chunks.append(chunk)
    return b"".join(chunks)


def _extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        print("[LinkReader] pypdf 未安装, 跳过 PDF 提取")
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = []
        for index, page in enumerate(reader.pages):
            if index >= _MAX_PDF_PAGES:
                break
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(pages)
    except Exception as exc:
        print(f"[LinkReader] PDF 解析失败: {_safe_error(exc)}")
        return ""


# ---- 通用 HTML 链路 ----

def _read_html(url: str, cfg: dict, html: str | None = None) -> str:
    """从 HTML 提取文本; html=None 时自行抓取 (调用方通常复用已抓取响应)."""
    if html is None:
        resp = requests.get(url, headers=_headers(),
                            timeout=_as_int(cfg.get("link_timeout_seconds"), 15),
                            allow_redirects=True)
        if resp.status_code != 200:
            return ""
        html = resp.text or ""
    if not html:
        return ""

    parts: list[str] = []
    seen: set[str] = set()

    def _add(value: str):
        value = _clean_text(unescape(value or ""))
        if value and value not in seen:
            seen.add(value)
            parts.append(value)

    _add(_first_group(re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.I)))
    meta_patterns = (
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']*)["\']',
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+name=["\']description["\']',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']*)["\']',
        r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:description["\']',
    )
    for pattern in meta_patterns:
        _add(_first_group(re.search(pattern, html, re.I)))
    for block in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>([\s\S]*?)</script>',
            html, re.I):
        for value in _ld_json_texts(block):
            _add(value)
    _add(_strip_html(html))

    text = _clean_text("\n".join(parts))
    if not text:
        return ""
    return _truncate(text, _as_int(cfg.get("link_max_chars"), 1500))


def _ld_json_texts(block: str) -> list[str]:
    """递归提取 ld+json 中的 articleBody / description 文本."""
    try:
        data = json.loads(block)
    except (ValueError, TypeError):
        return []
    values: list[str] = []

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("articleBody", "description") and isinstance(value, str):
                    values.append(value)
                else:
                    _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(data)
    return values


def _strip_html(html: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", html, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean_text(text)


# ---- 通用工具 ----

def _merge_config(config) -> dict:
    cfg = dict(_DEFAULTS)
    if isinstance(config, dict):
        for key in _DEFAULTS:
            value = config.get(key)
            if value not in (None, ""):
                cfg[key] = value
    return cfg


def _headers(referer: str = "") -> dict:
    headers = {"User-Agent": _UA, "Accept-Language": "en-US,en;q=0.9"}
    if referer:
        headers["Referer"] = referer
    return headers


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_group(match) -> str:
    return match.group(1).strip() if match else ""


def _safe_error(exc) -> str:
    """异常摘要 (脱敏): 去除 URL query 与签名类参数, 避免日志泄露签名链接."""
    message = f"{type(exc).__name__}: {exc}"
    message = re.sub(r"(https?://[^\s\"'<>?]+)\?[^\s\"'<>]*", r"\1?<redacted>", message)
    message = re.sub(r"(?i)\b(?:signature|key-pair-id|policy|token|expires)=[^\s&\"'<>]*",
                     "<redacted>", message)
    return message[:300]


def _clean_text(text: str) -> str:
    """清理连续空白: 折叠空格/制表符, 压缩空行."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _truncate(text: str, limit: int) -> str:
    if limit > 0 and len(text) > limit:
        return text[:limit] + "..."
    return text


def _cache_store(url: str, result: tuple[str, str]):
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[url] = result


if __name__ == "__main__":
    import sys

    _url = sys.argv[1] if len(sys.argv) > 1 else ""
    _start = time.time()
    _text, _source = read_link_content(_url)
    print(f"[LinkReader] source={_source!r} chars={len(_text)} "
          f"elapsed={time.time() - _start:.1f}s")
    if _text:
        print(_text[:300])
