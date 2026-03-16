from __future__ import annotations

from dataclasses import dataclass
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import time
from typing import Any
from urllib.parse import urlparse

from market_news.common import utcnow
from market_news.services.reporting import refresh_runtime_status_views
from market_news.services.unknown_term_detector import UnknownTermDetector


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@dataclass(slots=True)
class ReviewApiStateWriter:
    status_path: Path
    history_path: Path

    def write(
        self,
        *,
        host: str,
        port: int,
        overall_status: str,
        detail: str,
        lexicon_path: Path,
        discovery_path: Path,
        last_action: str | None = None,
        errors: list[str] | None = None,
    ) -> dict[str, object]:
        payload = {
            "timestamp": utcnow().isoformat(),
            "overall_status": overall_status,
            "artifacts": {
                "lexicon": str(lexicon_path),
                "discovery_file": str(discovery_path),
            },
            "modules": [
                {
                    "name": "lexicon_review_api",
                    "status": overall_status,
                    "detail": detail,
                    "host": host,
                    "port": port,
                    "last_action": last_action or "",
                }
            ],
            "errors": list(errors or []),
        }
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return payload


class LexiconReviewService:
    def __init__(
        self,
        *,
        lexicon_path: Path,
        discovery_path: Path,
        report_path: Path,
        tech_block_config: dict[str, Any] | None = None,
        status_writer: ReviewApiStateWriter | None = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        min_score: float = 2.0,
        list_limit: int = 100,
    ) -> None:
        self.lexicon_path = lexicon_path
        self.discovery_path = discovery_path
        self.report_path = report_path
        self.detector_config = dict((tech_block_config or {}).get("unknown_term_detector", {}) or {})
        self.status_writer = status_writer
        self.host = host
        self.port = port
        self.min_score = min_score
        self.list_limit = list_limit
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._last_action = "ready"

    def pending_payload(self, *, message: str = "") -> dict[str, object]:
        detector = self._build_detector()
        detector.prune_noise(self.discovery_path)
        candidates = detector.list_pending(
            self.discovery_path,
            min_score=self.min_score,
            limit=self.list_limit,
        )
        payload = {
            "ok": True,
            "summary": {
                "pending_count": len(candidates),
                "discovery_path": str(self.discovery_path),
                "lexicon_path": str(self.lexicon_path),
            },
            "candidates": candidates,
            "message": message or "待审核队列已刷新。",
        }
        return payload

    def add_term(self, term: str, *, term_type: str = "theme") -> dict[str, object]:
        with self._lock:
            lexicon_payload = self._load_lexicon()
            detector = self._build_detector(lexicon_payload)
            candidates = detector.load(self.discovery_path)
            candidate = next(
                (
                    item
                    for item in candidates
                    if str(item.get("text", "")).strip().lower() == term.strip().lower()
                ),
                None,
            )
            if candidate is None:
                raise ValueError(f"Candidate not found: {term}")

            known_terms = {
                str(item.get("canonical_text", "")).strip().lower()
                for item in lexicon_payload
                if str(item.get("canonical_text", "")).strip()
            }
            known_terms.update(
                str(synonym).strip().lower()
                for item in lexicon_payload
                for synonym in item.get("synonyms", [])
                if str(synonym).strip()
            )

            if term.strip().lower() not in known_terms:
                lexicon_payload.append(detector.build_lexicon_entry(candidate, term_type=term_type))
                _write_json(self.lexicon_path, lexicon_payload)

            detector.set_status(self.discovery_path, term, "accepted")
            self._last_action = f"accepted {term} as {term_type}"
            self._touch_status("ok", self._last_action)
            return self.pending_payload(message=f"已收录：{term}（{term_type}）")

    def reject_term(self, term: str) -> dict[str, object]:
        with self._lock:
            detector = self._build_detector()
            ok = detector.set_status(self.discovery_path, term, "rejected")
            if not ok:
                raise ValueError(f"Candidate not found: {term}")
            self._last_action = f"rejected {term}"
            self._touch_status("ok", self._last_action)
            return self.pending_payload(message=f"已忽略：{term}")

    def health_payload(self) -> dict[str, object]:
        return {
            "ok": True,
            "status": "ok",
            "host": self.host,
            "port": self.port,
            "last_action": self._last_action,
        }

    def start_heartbeat(self, *, interval_seconds: int = 60) -> None:
        if self.status_writer is None or self._heartbeat_thread is not None:
            return

        def loop() -> None:
            while not self._stop_event.wait(interval_seconds):
                self._touch_status("ok", f"listening on {self.host}:{self.port}")

        self._touch_status("ok", f"listening on {self.host}:{self.port}")
        self._heartbeat_thread = threading.Thread(target=loop, daemon=True, name="lexicon-review-heartbeat")
        self._heartbeat_thread.start()

    def stop_heartbeat(self) -> None:
        self._stop_event.set()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=1.0)
            self._heartbeat_thread = None

    def _touch_status(self, overall_status: str, detail: str, *, errors: list[str] | None = None) -> None:
        if self.status_writer is None:
            return
        self.status_writer.write(
            host=self.host,
            port=self.port,
            overall_status=overall_status,
            detail=detail,
            lexicon_path=self.lexicon_path,
            discovery_path=self.discovery_path,
            last_action=self._last_action,
            errors=errors,
        )
        refresh_runtime_status_views(self.report_path)

    def _load_lexicon(self) -> list[dict[str, Any]]:
        payload = _load_json(self.lexicon_path)
        if not isinstance(payload, list):
            raise ValueError(f"Lexicon file must be a JSON array: {self.lexicon_path}")
        return payload

    def _build_detector(self, lexicon_payload: list[dict[str, Any]] | None = None) -> UnknownTermDetector:
        payload = lexicon_payload if lexicon_payload is not None else self._load_lexicon()
        return UnknownTermDetector(lexicon=payload, config=self.detector_config)


def make_review_api_handler(service: LexiconReviewService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            return

        def end_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            super().end_headers()

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.end_headers()

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path == "/health":
                self._send_json(service.health_payload())
                return
            if path == "/api/lexicon/pending":
                self._send_json(service.pending_payload())
                return
            self._send_json({"ok": False, "error": "not-found"}, status_code=404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            payload = self._read_json_body()
            try:
                if path == "/api/lexicon/add":
                    term = str(payload.get("term", "")).strip()
                    term_type = str(payload.get("term_type", "theme") or "theme").strip()
                    if not term:
                        raise ValueError("Missing term")
                    self._send_json(service.add_term(term, term_type=term_type))
                    return
                if path == "/api/lexicon/reject":
                    term = str(payload.get("term", "")).strip()
                    if not term:
                        raise ValueError("Missing term")
                    self._send_json(service.reject_term(term))
                    return
                self._send_json({"ok": False, "error": "not-found"}, status_code=404)
            except ValueError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status_code=400)
            except Exception as exc:  # pragma: no cover - defensive
                service._touch_status("error", "review api request failed", errors=[str(exc)])
                self._send_json({"ok": False, "error": str(exc)}, status_code=500)

        def _read_json_body(self) -> dict[str, Any]:
            raw_length = self.headers.get("Content-Length", "0").strip()
            length = int(raw_length) if raw_length.isdigit() else 0
            if length <= 0:
                return {}
            body = self.rfile.read(length)
            if not body:
                return {}
            payload = json.loads(body.decode("utf-8"))
            return payload if isinstance(payload, dict) else {}

        def _send_json(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    return Handler


def serve_review_api(
    service: LexiconReviewService,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    heartbeat_interval: int = 60,
) -> None:
    server = ThreadingHTTPServer((host, port), make_review_api_handler(service))
    service.host = host
    service.port = port
    service.start_heartbeat(interval_seconds=heartbeat_interval)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        service.stop_heartbeat()
        server.server_close()
