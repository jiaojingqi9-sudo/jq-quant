from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import unescape
import json
import re
from pathlib import Path
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from market_news.domain.models import RawNewsRecord
from market_news.infrastructure.http import UrllibHttpClient


class ClsTelegraphCollector:
    name = "cls"

    def __init__(
        self,
        *,
        http_client: UrllibHttpClient,
        page_size: int = 20,
        min_level: int = 1,
        last_time_file: Path | None = None,
    ) -> None:
        self.http_client = http_client
        self.page_size = page_size
        self.min_level = min_level
        self.last_time_file = last_time_file

    def collect(self) -> list[RawNewsRecord]:
        payload = self._fetch_json_payload()
        if payload is not None:
            records = self._records_from_json_payload(payload)
            if records:
                return records
        return self._records_from_html_page()

    def _fetch_json_payload(self) -> dict[str, object] | None:
        last_time = self._load_last_time()
        url = (
            "https://www.cls.cn/nodeapi/updateTelegraph"
            f"?app=CLS&os=web&sv=7.7.5&rn={self.page_size}&last_time={last_time}"
        )
        try:
            response = self.http_client.get_text(url, headers=self._headers())
        except HTTPError:
            return None
        try:
            return json.loads(response.text)
        except json.JSONDecodeError:
            return None

    def _records_from_json_payload(self, payload: dict[str, object]) -> list[RawNewsRecord]:
        data = payload.get("data", {})
        if not isinstance(data, dict):
            return []
        items = data.get("roll_data", [])
        if not isinstance(items, list):
            return []
        last_time = int(data.get("last_time", 0) or 0)
        records: list[RawNewsRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            level = int(item.get("level", 1) or 1)
            if level < self.min_level:
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            records.append(
                RawNewsRecord(
                    source_id="cls",
                    external_id=str(item.get("id", content[:32])),
                    title=self._headline_from_content(content),
                    summary=content[:160],
                    body=content,
                    url=f"https://www.cls.cn/detail/{item.get('id')}",
                    published_at=datetime.fromtimestamp(int(item.get("ctime", 0) or 0), tz=UTC),
                    language="zh",
                    source_trust=self._cls_trust(level),
                    entities=[],
                    themes=[],
                    regions=["CN"],
                    metadata={
                        "direct_codes": [
                            str(stock.get("code", "")).strip()
                            for stock in item.get("stock_list", [])
                            if isinstance(stock, dict) and str(stock.get("code", "")).strip()
                        ],
                        "subjects": [
                            str(subject.get("name", "")).strip()
                            for subject in item.get("subjects", [])
                            if isinstance(subject, dict) and str(subject.get("name", "")).strip()
                        ],
                        "level": level,
                    },
                )
            )
        self._save_last_time(last_time)
        return records

    def _records_from_html_page(self) -> list[RawNewsRecord]:
        response = self.http_client.get_text("https://www.cls.cn/telegraph", headers=self._headers())
        html = response.text
        items = re.findall(
            r"<div class=\"p-t-20 p-b-20 b-b-w-1 b-b-s-s b-c-e6e7ea\">(.*?)</div></div></div>",
            html,
            flags=re.S,
        )
        last_seen_id = self._load_last_seen_id()
        latest_seen_id = last_seen_id
        records: list[RawNewsRecord] = []
        for block in items:
            detail_match = re.search(r'href=\"/detail/(\d+)\"', block)
            time_match = re.search(r'telegraph-time-box\">([0-9:]{8})</span>', block)
            content_match = re.search(r'<span class=\"c-34304b\"><div>(.*?)<br ?/?></div></span>', block, flags=re.S)
            if detail_match is None or time_match is None or content_match is None:
                continue
            external_id = detail_match.group(1)
            if last_seen_id and external_id == last_seen_id:
                break
            latest_seen_id = latest_seen_id or external_id
            content = self._clean_html(content_match.group(1))
            if not content:
                continue
            subjects = [
                self._clean_html(match)
                for match in re.findall(r'class=\"f-s-12 bg-c-f1f1f1 b-c-e6e7ea label-item\"[^>]*>(.*?)</a>', block, flags=re.S)
            ]
            records.append(
                RawNewsRecord(
                    source_id="cls",
                    external_id=external_id,
                    title=self._headline_from_content(content),
                    summary=content[:160],
                    body=content,
                    url=f"https://www.cls.cn/detail/{external_id}",
                    published_at=self._parse_page_time(time_match.group(1)),
                    language="zh",
                    source_trust=0.84,
                    entities=[],
                    themes=[],
                    regions=["CN"],
                    metadata={
                        "direct_codes": [],
                        "subjects": [item for item in subjects if item],
                        "level": 1,
                    },
                )
            )
            if len(records) >= self.page_size:
                break
        if latest_seen_id:
            self._save_last_seen_id(latest_seen_id)
        return records

    def _load_last_time(self) -> int:
        if self.last_time_file is None or not self.last_time_file.exists():
            return 0
        try:
            return int(self.last_time_file.read_text(encoding="utf-8").strip() or "0")
        except ValueError:
            return 0

    def _save_last_time(self, last_time: int) -> None:
        if self.last_time_file is None or last_time <= 0:
            return
        self.last_time_file.parent.mkdir(parents=True, exist_ok=True)
        self.last_time_file.write_text(str(last_time), encoding="utf-8")

    def _load_last_seen_id(self) -> str:
        if self.last_time_file is None:
            return ""
        html_marker = self.last_time_file.with_suffix(".last_id")
        if not html_marker.exists():
            return ""
        return html_marker.read_text(encoding="utf-8").strip()

    def _save_last_seen_id(self, external_id: str) -> None:
        if self.last_time_file is None or not external_id:
            return
        html_marker = self.last_time_file.with_suffix(".last_id")
        html_marker.parent.mkdir(parents=True, exist_ok=True)
        html_marker.write_text(external_id, encoding="utf-8")

    def _parse_page_time(self, value: str) -> datetime:
        now_cst = datetime.now(ZoneInfo("Asia/Shanghai"))
        hour, minute, second = (int(part) for part in value.split(":"))
        candidate = now_cst.replace(hour=hour, minute=minute, second=second, microsecond=0)
        if candidate > now_cst + timedelta(minutes=5):
            candidate = candidate - timedelta(days=1)
        return candidate.astimezone(UTC)

    def _headers(self) -> dict[str, str]:
        return {
            "Referer": "https://www.cls.cn/telegraph",
            "Accept": "application/json, text/plain, */*",
        }

    @staticmethod
    def _clean_html(text: str) -> str:
        cleaned = re.sub(r"<[^>]+>", "", text)
        cleaned = unescape(cleaned).replace("\xa0", " ")
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned.strip()

    @staticmethod
    def _headline_from_content(content: str) -> str:
        strong = re.match(r"^【([^】]+)】", content)
        if strong:
            return strong.group(1)[:120]
        return content[:120]

    @staticmethod
    def _cls_trust(level: int) -> float:
        return {1: 0.82, 2: 0.90, 3: 0.96}.get(level, 0.82)
