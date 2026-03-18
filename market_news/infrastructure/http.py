from __future__ import annotations

from dataclasses import dataclass
import gzip
import ssl
from typing import Mapping
from urllib import request
import zlib


DEFAULT_USER_AGENT = "MarketNewsCollector/0.1 (set MARKET_NEWS_USER_AGENT for production)"


@dataclass(slots=True)
class HttpResponse:
    url: str
    status: int
    body: bytes
    text: str
    headers: dict[str, str]


class UrllibHttpClient:
    def __init__(self, user_agent: str | None = None, timeout: int = 8) -> None:
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
        return self.request_text(url, headers=headers, encoding=encoding)

    def post_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | None = b"",
        encoding: str = "utf-8",
    ) -> HttpResponse:
        return self.request_text(
            url,
            headers=headers,
            encoding=encoding,
            method="POST",
            data=data,
        )

    def request_text(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        encoding: str = "utf-8",
        method: str = "GET",
        data: bytes | None = None,
    ) -> HttpResponse:
        merged_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/rss+xml, application/xml, text/xml, text/html;q=0.9, */*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if headers:
            merged_headers.update(headers)
        req = request.Request(url, headers=merged_headers, method=method, data=data)
        with request.urlopen(req, timeout=self.timeout, context=self.ssl_context) as response:
            body = response.read()
            body = self._decode_body(body, response.headers.get("Content-Encoding", ""))
            charset = response.headers.get_content_charset() or encoding
            text = body.decode(charset, errors="replace")
            return HttpResponse(
                url=response.geturl(),
                status=response.status,
                body=body,
                text=text,
                headers=dict(response.headers.items()),
            )

    def _decode_body(self, body: bytes, content_encoding: str) -> bytes:
        encoding = (content_encoding or "").lower().strip()
        if encoding == "gzip":
            return gzip.decompress(body)
        if encoding == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        return body


def default_user_agent() -> str:
    return DEFAULT_USER_AGENT
