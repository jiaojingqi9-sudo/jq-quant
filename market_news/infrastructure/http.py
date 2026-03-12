from __future__ import annotations

from dataclasses import dataclass
import ssl
from typing import Mapping
from urllib import request


DEFAULT_USER_AGENT = "MarketNewsCollector/0.1 (set MARKET_NEWS_USER_AGENT for production)"


@dataclass(slots=True)
class HttpResponse:
    url: str
    status: int
    text: str
    headers: dict[str, str]


class UrllibHttpClient:
    def __init__(self, user_agent: str | None = None, timeout: int = 20) -> None:
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.timeout = timeout
        self.ssl_context = ssl.create_default_context()

    def get_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        encoding: str = "utf-8",
    ) -> HttpResponse:
        merged_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if headers:
            merged_headers.update(headers)
        req = request.Request(url, headers=merged_headers)
        with request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as response:
            body = response.read()
            charset = response.headers.get_content_charset() or encoding
            text = body.decode(charset, errors="replace")
            return HttpResponse(
                url=response.geturl(),
                status=response.status,
                text=text,
                headers=dict(response.headers.items()),
            )


def default_user_agent() -> str:
    return DEFAULT_USER_AGENT
