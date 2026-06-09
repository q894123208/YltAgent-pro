from __future__ import annotations

import asyncio
import re
from html import unescape
from urllib.parse import quote_plus
from typing import List

import httpx

from app.schemas.chat import Evidence


def _search_sync(query: str, limit: int) -> List[Evidence]:
    from duckduckgo_search import DDGS

    rows = []
    with DDGS() as ddgs:
        for item in ddgs.text(query + " 医学 指南 健康 科普", max_results=limit):
            rows.append(
                Evidence(
                    source=item.get("href", "web"),
                    title=item.get("title", "联网资料"),
                    score=0.5,
                    content=item.get("body", ""),
                )
            )
    return rows


async def web_search(query: str, limit: int = 2, timeout: float = 8.0) -> List[Evidence]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_search_sync, query, limit), timeout=timeout)
    except Exception:
        try:
            return await _search_duckduckgo_html(query, limit=limit, timeout=timeout)
        except Exception:
            return [
                Evidence(
                    source="web-search-fallback",
                    title="联网搜索自动降级",
                    score=0.0,
                    content="当前环境未成功获取联网资料，系统将基于本地医学知识库和安全规则回答。",
                )
            ]


async def _search_duckduckgo_html(query: str, limit: int = 2, timeout: float = 6.0) -> List[Evidence]:
    url = "https://duckduckgo.com/html/?q=" + quote_plus(query + " 医学 指南 健康 科普")
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    html = resp.text
    pattern = re.compile(r'<a rel="nofollow" class="result__a" href="(?P<href>.*?)".*?>(?P<title>.*?)</a>.*?<a class="result__snippet".*?>(?P<body>.*?)</a>', re.S)
    rows = []
    for match in pattern.finditer(html):
        title = re.sub("<.*?>", "", unescape(match.group("title"))).strip()
        body = re.sub("<.*?>", "", unescape(match.group("body"))).strip()
        href = unescape(match.group("href")).strip()
        if title and body:
            rows.append(Evidence(source=href, title=title, score=0.45, content=body))
        if len(rows) >= limit:
            break
    if not rows:
        raise RuntimeError("no web result parsed")
    return rows
