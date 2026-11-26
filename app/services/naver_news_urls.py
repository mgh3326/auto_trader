"""Canonical URL identity shared by Naver's KR news feeds."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

_N_NEWS_PATH = re.compile(r"^/(?:mnews/)?article/(?P<office>\d+)/(?P<article>\d+)/?$")


def canonicalize_naver_news_url(raw_url: str) -> str:
    parts = urlsplit(raw_url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        raise ValueError("invalid_article_url")
    host = parts.hostname.lower()
    if host == "finance.naver.com" and parts.path.endswith("/news_read.naver"):
        params = parse_qs(parts.query)
        office = (params.get("office_id") or [""])[0]
        article = (params.get("article_id") or [""])[0]
        if office.isdigit() and article.isdigit():
            return f"https://n.news.naver.com/article/{office}/{article}"
        return raw_url
    if host == "n.news.naver.com":
        match = _N_NEWS_PATH.fullmatch(parts.path)
        if match:
            return (
                "https://n.news.naver.com/article/"
                f"{match.group('office')}/{match.group('article')}"
            )
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() != "sid"
            and not key.lower().startswith("utm_")
            and key.lower() not in {"from", "trackingcode"}
        ]
    )
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, query, ""))
