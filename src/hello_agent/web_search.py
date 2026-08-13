"""联网搜索：优先必应中国（国内服务器可达），可选 Brave，再回退 DuckDuckGo。"""

from __future__ import annotations

import os
import re
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

import httpx

MAX_QUERY_LENGTH = 200
MAX_RESULTS = 5
DEFAULT_TIMEOUT = 12.0
USER_AGENT = (
    "Mozilla/5.0 (compatible; HelloAgentLab/1.0; +https://github.com/hello-agent-lab)"
)


def web_search_enabled() -> bool:
    value = os.getenv("WEB_SEARCH_ENABLED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def search_web(query: str, limit: int = 3) -> dict[str, object]:
    cleaned = query.strip()
    if not cleaned:
        raise ValueError("搜索词不能为空。")
    if len(cleaned) > MAX_QUERY_LENGTH:
        raise ValueError(f"搜索词不能超过 {MAX_QUERY_LENGTH} 个字符。")
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise ValueError("limit 必须是整数。")
    limit = max(1, min(limit, MAX_RESULTS))
    if not web_search_enabled():
        return {
            "query": cleaned,
            "results": [],
            "provider": "disabled",
            "message": "联网搜索已关闭。",
        }

    errors: list[str] = []

    api_key = (
        os.getenv("BRAVE_SEARCH_API_KEY", "").strip()
        or os.getenv("WEB_SEARCH_API_KEY", "").strip()
    )
    if api_key:
        try:
            return _brave_search(cleaned, limit, api_key)
        except Exception as error:
            errors.append(f"brave: {error}")

    try:
        return _bing_cn_search(cleaned, limit)
    except Exception as error:
        errors.append(f"bing_cn: {error}")

    try:
        return _duckduckgo_html_search(cleaned, limit)
    except Exception as error:
        errors.append(f"duckduckgo: {error}")

    detail = "；".join(errors)[:240] if errors else "未知错误"
    return {
        "query": cleaned,
        "results": [],
        "provider": "unavailable",
        "message": f"联网搜索暂时不可用：{detail}",
    }


def _brave_search(query: str, limit: int, api_key: str) -> dict[str, object]:
    response = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": limit},
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
            "User-Agent": USER_AGENT,
        },
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        trust_env=False,
    )
    response.raise_for_status()
    payload = response.json()
    web = payload.get("web") if isinstance(payload, dict) else None
    items = web.get("results") if isinstance(web, dict) else None
    results: list[dict[str, object]] = []
    if isinstance(items, list):
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            snippet = str(item.get("description") or "").strip()
            if not title or not url:
                continue
            results.append(
                {
                    "title": title[:200],
                    "url": url[:500],
                    "snippet": snippet[:400],
                    "source": "web",
                }
            )
    return {
        "query": query,
        "results": results,
        "provider": "brave",
        "message": None if results else "联网搜索未找到相关结果。",
    }


def _bing_cn_search(query: str, limit: int) -> dict[str, object]:
    response = httpx.get(
        "https://cn.bing.com/search",
        params={"q": query, "setlang": "zh-CN"},
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        trust_env=False,
    )
    response.raise_for_status()
    results = _parse_bing_cn_html(response.text, limit)
    return {
        "query": query,
        "results": results,
        "provider": "bing_cn",
        "message": None if results else "联网搜索未找到相关结果。",
    }


def _parse_bing_cn_html(html: str, limit: int) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    seen: set[str] = set()

    for match in re.finditer(
        r'<a class="tilk"[^>]*href="(https?://[^"]+)"[^>]*>',
        html,
        re.I,
    ):
        href = match.group(1).strip()
        if not href or href in seen:
            continue
        if _is_bing_internal(href):
            continue
        rest = html[match.end() : match.end() + 1600]
        title_match = re.search(r"<h2[^>]*>([\s\S]*?)</h2>", rest, re.I)
        snippet_match = re.search(r"<p[^>]*>([\s\S]*?)</p>", rest, re.I)
        title = _strip_html(title_match.group(1) if title_match else "")
        snippet = _strip_html(snippet_match.group(1) if snippet_match else "")
        if not title:
            continue
        seen.add(href)
        results.append(
            {
                "title": title[:200],
                "url": href[:500],
                "snippet": snippet[:400],
                "source": "web",
            }
        )
        if len(results) >= limit:
            return results

    # 兼容另一种标题结构：<h2><a href="...">title</a></h2>
    if len(results) < limit:
        for match in re.finditer(
            r'<h2[^>]*>\s*<a[^>]*href="(https?://[^"]+)"[^>]*>([\s\S]*?)</a>',
            html,
            re.I,
        ):
            href = match.group(1).strip()
            title = _strip_html(match.group(2))
            if not href or not title or href in seen or _is_bing_internal(href):
                continue
            rest = html[match.end() : match.end() + 900]
            snippet_match = re.search(r"<p[^>]*>([\s\S]*?)</p>", rest, re.I)
            snippet = _strip_html(snippet_match.group(1) if snippet_match else "")
            seen.add(href)
            results.append(
                {
                    "title": title[:200],
                    "url": href[:500],
                    "snippet": snippet[:400],
                    "source": "web",
                }
            )
            if len(results) >= limit:
                break

    return results


def _is_bing_internal(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("bing.com") or host.endswith("microsoft.com")


def _duckduckgo_html_search(query: str, limit: int) -> dict[str, object]:
    response = httpx.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": USER_AGENT},
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        trust_env=False,
    )
    response.raise_for_status()
    results = _parse_duckduckgo_html(response.text, limit)
    return {
        "query": query,
        "results": results,
        "provider": "duckduckgo",
        "message": None if results else "联网搜索未找到相关结果。",
    }


def _parse_duckduckgo_html(html: str, limit: int) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for match in re.finditer(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        re.S | re.I,
    ):
        href = _unwrap_duckduckgo_url(match.group(1))
        title = _strip_html(match.group(2))
        if not href or not title:
            continue
        rest = html[match.end() : match.end() + 1200]
        snippet_match = re.search(
            r'class="result__snippet"[^>]*>(.*?)</(?:a|td|div|span)>',
            rest,
            re.S | re.I,
        )
        snippet = _strip_html(snippet_match.group(1)) if snippet_match else ""
        results.append(
            {
                "title": title[:200],
                "url": href[:500],
                "snippet": snippet[:400],
                "source": "web",
            }
        )
        if len(results) >= limit:
            break
    return results


def _unwrap_duckduckgo_url(href: str) -> str:
    parsed = urlparse(href)
    if "uddg=" in href:
        values = parse_qs(parsed.query).get("uddg") or []
        if values:
            return unquote(values[0]).strip()
    return href.strip()


def _strip_html(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", unescape(value or ""))
    return re.sub(r"\s+", " ", text).strip()
