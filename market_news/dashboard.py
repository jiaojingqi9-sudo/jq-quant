from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Callable


def render_dashboard(report_path: Path, *, top_n: int = 8) -> str:
    if not report_path.exists():
        return (
            "Market News Dashboard\n"
            "=====================\n\n"
            f"Report not found: {report_path}\n"
            "Run `python3 -m market_news live` first.\n"
        )
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    created_at = payload.get("created_at", "")
    counts = payload.get("counts", {})
    alert_counts = payload.get("alert_counts", {})
    lines = [
        "Market News Dashboard",
        "=====================",
        "",
        f"Updated: {created_at}",
        f"Source: {payload.get('source', 'n/a')}",
        f"Events: {counts.get('ranked_events', 0)} | Instruments: {counts.get('ranked_instruments', 0)}",
        "Alerts: "
        f"critical={alert_counts.get('critical', 0)} "
        f"high={alert_counts.get('high', 0)} "
        f"medium={alert_counts.get('medium', 0)} "
        f"new={alert_counts.get('new', 0)}",
        "",
        "Alert Summary",
        "-------------",
    ]

    alerts = payload.get("alerts", [])[:top_n]
    if alerts:
        for index, alert in enumerate(alerts, start=1):
            marker = "NEW " if alert.get("is_new") else ""
            symbols = ", ".join(alert.get("symbols", [])) or "n/a"
            lines.append(
                f"{index}. [{alert.get('level', 'n/a').upper():<8}] {marker}{alert.get('direction', 'n/a'):>8} | "
                f"{alert.get('headline', '')}"
            )
            lines.append(f"   symbols={symbols}")
            lines.append(f"   why={alert.get('reason', '')}")
    else:
        lines.append("No active alerts.")

    lines.extend(["", "Negative Risks", "--------------"])
    _append_event_lines(lines, payload.get("negative_risks", []), top_n=top_n)
    lines.extend(["", "Positive Catalysts", "------------------"])
    _append_event_lines(lines, payload.get("positive_catalysts", []), top_n=top_n)
    lines.extend(["", "Latest Feed", "-----------"])
    latest_feed = payload.get("latest_feed", [])[:top_n]
    for index, item in enumerate(latest_feed, start=1):
        lines.append(
            f"{index}. {item.get('published_at', '')} | {item.get('source_id', '')} | {item.get('title', '')}"
        )
    return "\n".join(lines) + "\n"


def _append_event_lines(lines: list[str], events: list[dict[str, object]], *, top_n: int) -> None:
    if not events:
        lines.append("No items.")
        return
    for index, event in enumerate(events[:top_n], start=1):
        instruments = ", ".join(
            instrument.get("symbol", "") for instrument in event.get("top_instruments", [])[:4]
        ) or "n/a"
        lines.append(
            f"{index}. [{event.get('final_score', 0):>5}] {event.get('direction', 'n/a'):>8} | "
            f"{event.get('headline', '')}"
        )
        lines.append(f"   type={event.get('event_type', 'n/a')} instruments={instruments}")


def watch_dashboard(
    report_path: Path,
    *,
    top_n: int,
    interval_seconds: int,
    refresh_callback: Callable[[], None] | None = None,
) -> None:
    while True:
        if refresh_callback is not None:
            refresh_callback()
        os.system("clear")
        print(render_dashboard(report_path, top_n=top_n))
        print(f"Next refresh in {interval_seconds}s. Press Ctrl+C to stop.")
        time.sleep(interval_seconds)
