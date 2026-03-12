from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from string import Template

from market_news.domain.models import Direction, EventCluster, NewsDocument, PipelineSnapshot


class MarkdownJsonReporter:
    def __init__(self, output_dir: Path, top_n: int = 8) -> None:
        self.output_dir = output_dir
        self.top_n = top_n
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write(self, snapshot: PipelineSnapshot) -> dict[str, Path]:
        json_path = self.output_dir / "latest_report.json"
        markdown_path = self.output_dir / "latest_report.md"
        html_path = self.output_dir / "latest_dashboard.html"

        cluster_by_id = {cluster.cluster_id: cluster for cluster in snapshot.clusters}

        grouped_instruments: dict[str, list[dict[str, object]]] = defaultdict(list)
        for instrument in snapshot.ranked_instruments:
            grouped_instruments[instrument.cluster_id].append(
                {
                    "symbol": instrument.symbol,
                    "market": instrument.market.value,
                    "name": instrument.name,
                    "direction": instrument.direction.value,
                    "final_score": instrument.final_score,
                    "reasons": instrument.reasons,
                }
            )

        positive_events = [
            event for event in snapshot.ranked_events if event.impact.direction == Direction.POSITIVE
        ][: self.top_n]
        negative_events = [
            event for event in snapshot.ranked_events if event.impact.direction == Direction.NEGATIVE
        ][: self.top_n]
        neutral_events = [
            event for event in snapshot.ranked_events if event.impact.direction == Direction.NEUTRAL
        ][: self.top_n]
        latest_feed = snapshot.documents[: self.top_n * 2]

        alert_counts = {
            "critical": sum(1 for item in snapshot.alerts if item.level.value == "critical"),
            "high": sum(1 for item in snapshot.alerts if item.level.value == "high"),
            "medium": sum(1 for item in snapshot.alerts if item.level.value == "medium"),
            "new": sum(1 for item in snapshot.alerts if item.is_new),
        }

        json_payload = {
            "run_id": snapshot.run_id,
            "created_at": snapshot.created_at.isoformat(),
            "source": snapshot.source_name,
            "counts": {
                "raw_records": len(snapshot.raw_records),
                "documents": len(snapshot.documents),
                "clusters": len(snapshot.clusters),
                "ranked_events": len(snapshot.ranked_events),
                "ranked_instruments": len(snapshot.ranked_instruments),
            },
            "alerts": [
                {
                    "cluster_id": item.cluster_id,
                    "headline": item.headline,
                    "level": item.level.value,
                    "direction": item.direction.value,
                    "event_type": item.event_type.value,
                    "is_new": item.is_new,
                    "final_score": item.final_score,
                    "symbols": item.symbols,
                    "reason": item.reason,
                }
                for item in snapshot.alerts
            ],
            "alert_counts": alert_counts,
            "top_events": [
                self._event_payload(
                    event,
                    cluster_by_id.get(event.cluster_id),
                    grouped_instruments.get(event.cluster_id, [])[:5],
                )
                for event in snapshot.ranked_events[: self.top_n]
            ],
            "positive_catalysts": [
                self._event_payload(
                    event,
                    cluster_by_id.get(event.cluster_id),
                    grouped_instruments.get(event.cluster_id, [])[:5],
                )
                for event in positive_events
            ],
            "negative_risks": [
                self._event_payload(
                    event,
                    cluster_by_id.get(event.cluster_id),
                    grouped_instruments.get(event.cluster_id, [])[:5],
                )
                for event in negative_events
            ],
            "watchlist": [
                self._event_payload(
                    event,
                    cluster_by_id.get(event.cluster_id),
                    grouped_instruments.get(event.cluster_id, [])[:5],
                )
                for event in neutral_events
            ],
            "top_instruments": [
                {
                    "cluster_id": instrument.cluster_id,
                    "headline": instrument.cluster_headline,
                    "symbol": instrument.symbol,
                    "market": instrument.market.value,
                    "name": instrument.name,
                    "direction": instrument.direction.value,
                    "final_score": instrument.final_score,
                    "reasons": instrument.reasons,
                }
                for instrument in snapshot.ranked_instruments[: self.top_n]
            ],
            "latest_feed": [
                self._document_payload(document)
                for document in latest_feed
            ],
        }
        json_path.write_text(json.dumps(json_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [
            "# Market News Collector Report",
            "",
            f"- Run ID: `{snapshot.run_id}`",
            f"- Source: `{snapshot.source_name}`",
            f"- Raw records: `{len(snapshot.raw_records)}`",
            f"- Deduplicated documents: `{len(snapshot.documents)}`",
            f"- Event clusters: `{len(snapshot.clusters)}`",
            f"- Alerts: `critical={alert_counts['critical']}` `high={alert_counts['high']}` `medium={alert_counts['medium']}` `new={alert_counts['new']}`",
            "",
            "## Alert Summary",
            "",
        ]

        if snapshot.alerts:
            for index, alert in enumerate(snapshot.alerts[: self.top_n], start=1):
                prefix = "NEW " if alert.is_new else ""
                symbols = ", ".join(alert.symbols) if alert.symbols else "n/a"
                lines.extend(
                    [
                        f"{index}. `{alert.level.value.upper()}` {prefix}`{alert.direction.value}` {alert.headline}",
                        f"   symbols: {symbols}",
                        f"   why: {alert.reason}",
                    ]
                )
            lines.append("")
        else:
            lines.extend(["No active alerts.", ""])

        self._append_event_section(
            lines,
            title="## Negative Risks",
            events=negative_events,
            grouped_instruments=grouped_instruments,
            cluster_by_id=cluster_by_id,
        )
        self._append_event_section(
            lines,
            title="## Positive Catalysts",
            events=positive_events,
            grouped_instruments=grouped_instruments,
            cluster_by_id=cluster_by_id,
        )
        self._append_event_section(
            lines,
            title="## Watchlist",
            events=neutral_events,
            grouped_instruments=grouped_instruments,
            cluster_by_id=cluster_by_id,
        )

        lines.extend(["## Latest Feed", ""])
        for index, document in enumerate(latest_feed, start=1):
            themes = ", ".join(document.themes[:3]) or "n/a"
            entities = ", ".join(document.entities[:3]) or "n/a"
            lines.extend(
                [
                    f"{index}. `{document.published_at.isoformat()}` `{document.source_id}` {document.title}",
                    f"   themes: {themes}",
                    f"   entities: {entities}",
                ]
            )
        lines.append("")

        markdown_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        html_path.write_text(self._build_html(json_payload), encoding="utf-8")
        return {
            "json_report": json_path,
            "markdown_report": markdown_path,
            "html_report": html_path,
        }

    def _append_event_section(
        self,
        lines: list[str],
        *,
        title: str,
        events: list[object],
        grouped_instruments: dict[str, list[dict[str, object]]],
        cluster_by_id: dict[str, EventCluster],
    ) -> None:
        lines.extend([title, ""])
        if not events:
            lines.extend(["No items.", ""])
            return
        for index, event in enumerate(events[: self.top_n], start=1):
            cluster = cluster_by_id.get(event.cluster_id)
            summary = cluster.summary if cluster is not None else ""
            lines.extend(
                [
                    f"{index}. `{event.final_score}` {event.headline}",
                    f"   direction: {event.impact.direction.value} | type: {event.impact.event_type.value}",
                    f"   themes: {', '.join(event.impact.affected_themes) or 'n/a'}",
                    f"   why: {'; '.join(event.impact.rationale)}",
                ]
            )
            if summary:
                lines.append(f"   summary: {self._truncate(summary, 160)}")
            related = grouped_instruments.get(event.cluster_id, [])[:4]
            if related:
                lines.append(
                    "   instruments: "
                    + ", ".join(
                        f"{instrument['symbol']}({instrument['market']})"
                        for instrument in related
                    )
                )
        lines.append("")

    def _event_payload(
        self,
        event: object,
        cluster: EventCluster | None,
        top_instruments: list[dict[str, object]],
    ) -> dict[str, object]:
        related_documents = []
        if cluster is not None:
            related_documents = [
                self._document_payload(document)
                for document in cluster.documents[:6]
            ]

        return {
            "cluster_id": event.cluster_id,
            "headline": event.headline,
            "summary": cluster.summary if cluster is not None else "",
            "direction": event.impact.direction.value,
            "event_type": event.impact.event_type.value,
            "final_score": event.final_score,
            "heat_score": event.heat_score,
            "importance_score": event.importance_score,
            "confidence_score": event.confidence_score,
            "markets": [market.value for market in event.impact.affected_markets],
            "themes": event.impact.affected_themes,
            "entities": cluster.entities[:8] if cluster is not None else [],
            "sectors": cluster.sectors[:6] if cluster is not None else [],
            "regions": cluster.regions[:6] if cluster is not None else [],
            "rationale": event.impact.rationale,
            "source_ids": cluster.source_ids if cluster is not None else [],
            "doc_count": cluster.doc_count if cluster is not None else len(related_documents),
            "first_seen_at": cluster.first_seen_at.isoformat() if cluster is not None else "",
            "last_seen_at": cluster.last_seen_at.isoformat() if cluster is not None else "",
            "top_instruments": top_instruments,
            "related_documents": related_documents,
        }

    def _document_payload(self, document: NewsDocument) -> dict[str, object]:
        summary = document.summary.strip() or self._truncate(document.body.strip(), 220)
        return {
            "doc_id": document.doc_id,
            "published_at": document.published_at.isoformat(),
            "source_id": document.source_id,
            "title": document.title,
            "summary": summary,
            "themes": document.themes,
            "entities": document.entities[:4],
            "url": document.url,
        }

    def _truncate(self, value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: limit - 3].rstrip() + "..."

    def _build_html(self, payload: dict[str, object]) -> str:
        payload_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
        template = Template(
            """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Market News Board</title>
  <style>
    :root {
      --bg: #f4efe6;
      --bg-soft: #fbf8f2;
      --ink: #11202d;
      --muted: #5f6d77;
      --line: rgba(17, 32, 45, 0.12);
      --card: rgba(255, 252, 247, 0.92);
      --accent: #0d6f6f;
      --accent-soft: rgba(13, 111, 111, 0.12);
      --danger: #b5483d;
      --danger-soft: rgba(181, 72, 61, 0.12);
      --success: #1e7c57;
      --success-soft: rgba(30, 124, 87, 0.12);
      --warning: #be7b1d;
      --warning-soft: rgba(190, 123, 29, 0.14);
      --shadow: 0 18px 40px rgba(17, 32, 45, 0.08);
      --radius-lg: 26px;
      --radius-md: 18px;
      --radius-sm: 12px;
      --headline: "Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif;
      --ui: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      font-family: var(--ui);
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(13, 111, 111, 0.12), transparent 28%),
        radial-gradient(circle at top right, rgba(190, 123, 29, 0.16), transparent 22%),
        linear-gradient(180deg, #f7f3ea 0%, #eee4d3 100%);
      min-height: 100vh;
      overflow-x: hidden;
      overflow-y: auto;
    }

    a {
      color: var(--accent);
      text-decoration: none;
    }

    a:hover {
      text-decoration: underline;
    }

    .shell {
      width: min(1440px, calc(100vw - 28px));
      margin: 18px auto 32px;
      display: grid;
      gap: 18px;
      grid-template-rows: auto auto auto;
    }

    .hero,
    .panel {
      border: 1px solid var(--line);
      background: var(--card);
      backdrop-filter: blur(14px);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow);
    }

    .hero {
      padding: 24px 26px;
      display: grid;
      gap: 20px;
      grid-template-columns: 1.5fr 1fr;
      overflow: hidden;
      position: relative;
    }

    .hero::after {
      content: "";
      position: absolute;
      inset: auto -120px -120px auto;
      width: 280px;
      height: 280px;
      background: radial-gradient(circle, rgba(13, 111, 111, 0.18), transparent 65%);
      pointer-events: none;
    }

    .eyebrow {
      margin: 0 0 8px;
      text-transform: uppercase;
      letter-spacing: 0.16em;
      font-size: 12px;
      color: var(--muted);
    }

    h1 {
      margin: 0;
      font-family: var(--headline);
      font-weight: 700;
      font-size: clamp(32px, 5vw, 58px);
      line-height: 0.96;
      max-width: 12ch;
    }

    .hero-copy p {
      margin: 16px 0 0;
      max-width: 68ch;
      color: var(--muted);
      font-size: 15px;
      line-height: 1.65;
    }

    .ticker {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border-radius: 999px;
      background: #fff8ee;
      border: 1px solid rgba(190, 123, 29, 0.18);
      color: #6f551f;
      font-size: 13px;
      margin-top: 18px;
    }

    .metric-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      align-self: start;
    }

    .metric-card {
      padding: 16px;
      border-radius: var(--radius-md);
      background: var(--bg-soft);
      border: 1px solid var(--line);
    }

    .metric-card .label {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.12em;
      margin-bottom: 8px;
    }

    .metric-card .value {
      font-size: 30px;
      font-weight: 700;
      line-height: 1;
      margin-bottom: 6px;
    }

    .metric-card .meta {
      color: var(--muted);
      font-size: 13px;
    }

    .toolbar {
      display: grid;
      gap: 12px;
      grid-template-columns: 1fr auto;
      align-items: center;
    }

    .filters {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
    }

    .chip {
      border: 1px solid var(--line);
      background: #fff;
      color: var(--muted);
      border-radius: 999px;
      padding: 10px 14px;
      font-size: 13px;
      cursor: pointer;
      transition: transform 120ms ease, background 120ms ease, color 120ms ease;
    }

    .chip:hover {
      transform: translateY(-1px);
    }

    .chip.active {
      color: #fff;
      background: var(--ink);
      border-color: var(--ink);
    }

    .tools {
      display: flex;
      gap: 10px;
      align-items: center;
    }

    .search {
      width: min(340px, 46vw);
      padding: 11px 14px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.9);
      color: var(--ink);
      font-size: 14px;
      outline: none;
    }

    .search:focus {
      border-color: rgba(13, 111, 111, 0.4);
      box-shadow: 0 0 0 4px rgba(13, 111, 111, 0.08);
    }

    .layout {
      display: grid;
      grid-template-columns: minmax(280px, 1fr) minmax(520px, 1.45fr) minmax(360px, 1.08fr);
      gap: 18px;
      align-items: start;
    }

    .column {
      display: grid;
      gap: 18px;
      align-content: start;
      min-height: 0;
    }

    .column-scroll {
      height: clamp(760px, calc(100vh - 220px), 1200px);
      overflow-y: scroll;
      overflow-x: hidden;
      overscroll-behavior: contain;
      -webkit-overflow-scrolling: touch;
      padding-right: 8px;
      margin-right: -6px;
      scrollbar-gutter: stable;
      scrollbar-width: thin;
      scrollbar-color: rgba(17, 32, 45, 0.22) transparent;
    }

    .column-scroll::-webkit-scrollbar {
      width: 10px;
    }

    .column-scroll::-webkit-scrollbar-thumb {
      background: rgba(17, 32, 45, 0.18);
      border-radius: 999px;
      border: 2px solid rgba(255, 255, 255, 0.3);
    }

    .panel {
      padding: 20px;
      animation: rise 280ms ease;
    }

    @keyframes rise {
      from {
        opacity: 0;
        transform: translateY(8px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    .panel h2 {
      margin: 0 0 14px;
      font-family: var(--headline);
      font-size: 26px;
    }

    .panel-subtitle {
      color: var(--muted);
      font-size: 13px;
      margin-bottom: 14px;
    }

    .left-column .panel,
    .middle-column .panel,
    .right-column .panel {
      width: 100%;
    }

    .stack {
      display: grid;
      gap: 12px;
      align-content: start;
    }

    .alert-card,
    .event-card,
    .instrument-card,
    .feed-card,
    .doc-card {
      border-radius: var(--radius-md);
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.88);
      padding: 14px 16px;
      transition: transform 140ms ease, border-color 140ms ease, box-shadow 140ms ease;
    }

    button.card-button {
      width: 100%;
      text-align: left;
      background: transparent;
      border: none;
      padding: 0;
      cursor: pointer;
      color: inherit;
      font: inherit;
    }

    .alert-card:hover,
    .event-card:hover,
    .instrument-card:hover,
    .feed-card:hover {
      transform: translateY(-2px);
      border-color: rgba(13, 111, 111, 0.25);
      box-shadow: 0 12px 28px rgba(17, 32, 45, 0.08);
    }

    .event-card.active,
    .alert-card.active,
    .instrument-card.active {
      border-color: rgba(13, 111, 111, 0.42);
      box-shadow: 0 0 0 4px rgba(13, 111, 111, 0.08);
    }

    .card-topline,
    .card-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      letter-spacing: 0.01em;
    }

    .badge.level-critical,
    .badge.dir-negative {
      background: var(--danger-soft);
      color: var(--danger);
    }

    .badge.level-high,
    .badge.type-regulation {
      background: var(--warning-soft);
      color: var(--warning);
    }

    .badge.level-medium,
    .badge.dir-neutral {
      background: rgba(17, 32, 45, 0.08);
      color: var(--ink);
    }

    .badge.dir-positive,
    .badge.type-company {
      background: var(--success-soft);
      color: var(--success);
    }

    .headline {
      margin: 10px 0 8px;
      font-size: 18px;
      line-height: 1.35;
      font-weight: 700;
    }

    .headline-serif {
      font-family: var(--headline);
      font-size: 30px;
      line-height: 1.12;
      margin: 0 0 12px;
    }

    .summary {
      color: var(--muted);
      font-size: 14px;
      line-height: 1.6;
      margin: 0;
    }

    .chip-row,
    .tag-row {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }

    .mini-tag {
      padding: 6px 10px;
      border-radius: 999px;
      background: rgba(17, 32, 45, 0.06);
      color: var(--muted);
      font-size: 12px;
    }

    .score-strip {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin: 16px 0;
    }

    .score-box {
      border-radius: var(--radius-sm);
      background: var(--bg-soft);
      border: 1px solid var(--line);
      padding: 12px;
    }

    .score-box .label {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      margin-bottom: 6px;
    }

    .score-box .value {
      font-size: 24px;
      font-weight: 700;
    }

    .detail-grid {
      display: grid;
      gap: 14px;
    }

    .detail-block {
      padding: 14px 16px;
      border-radius: var(--radius-md);
      border: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.78);
    }

    #detailView {
      min-height: 0;
    }

    .detail-section-title {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: center;
      margin-bottom: 10px;
      font-size: 12px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
    }

    .detail-times {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
      margin-top: 12px;
    }

    .detail-section-title strong {
      color: var(--ink);
      letter-spacing: 0.02em;
      text-transform: none;
      font-size: 15px;
    }

    .doc-list,
    .rationale-list {
      display: grid;
      gap: 10px;
      margin: 0;
      padding: 0;
      list-style: none;
    }

    .doc-card p,
    .instrument-note,
    .feed-card p {
      margin: 8px 0 0;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.55;
    }

    .rationale-list li {
      padding: 10px 12px;
      border-radius: var(--radius-sm);
      background: rgba(17, 32, 45, 0.05);
      line-height: 1.55;
    }

    .empty {
      padding: 22px;
      border-radius: var(--radius-md);
      background: var(--bg-soft);
      border: 1px dashed var(--line);
      color: var(--muted);
      text-align: center;
    }

    .section-header {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: end;
      margin-bottom: 14px;
    }

    .tiny {
      color: var(--muted);
      font-size: 12px;
    }

    @media (max-width: 1180px) {
      .hero {
        grid-template-columns: 1fr;
      }

      .metric-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .layout {
        grid-template-columns: 1fr;
      }

      .column-scroll {
        height: auto;
        overflow: visible;
        padding-right: 0;
        margin-right: 0;
        scrollbar-gutter: auto;
      }
    }

    @media (max-width: 720px) {
      .shell {
        width: min(100vw - 18px, 100%);
      }

      .hero,
      .panel {
        border-radius: 22px;
        padding: 18px;
      }

      .toolbar {
        grid-template-columns: 1fr;
      }

      .tools {
        flex-wrap: wrap;
      }

      .search {
        width: 100%;
      }

      .metric-grid,
      .score-strip {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="hero-copy">
        <p class="eyebrow">Market News Board</p>
        <h1>可点击市场消息控制台</h1>
        <p id="heroMeta"></p>
        <div class="ticker">
          <span id="refreshText">页面会自动刷新</span>
        </div>
      </div>
      <div class="metric-grid" id="metricGrid"></div>
    </section>

    <section class="toolbar">
      <div class="filters" id="filterChips"></div>
      <div class="tools">
        <input id="searchInput" class="search" type="search" placeholder="搜索新闻、主题、股票代码、公司名">
        <button id="resetButton" class="chip" type="button">重置</button>
      </div>
    </section>

    <section class="layout">
      <section class="column column-scroll left-column">
        <section class="panel alerts-panel">
          <div class="section-header">
            <div>
              <h2>Alert Radar</h2>
              <div class="panel-subtitle">先看提醒，再点进去看事件链和原文。</div>
            </div>
            <div class="tiny" id="alertCountLabel"></div>
          </div>
          <div class="stack" id="alertsList"></div>
        </section>

        <section class="panel instruments-panel">
          <div class="section-header">
            <div>
              <h2>Instrument Ladder</h2>
              <div class="panel-subtitle">按候选标的强度排序，点击会联动右侧详情。</div>
            </div>
            <div class="tiny" id="instrumentCountLabel"></div>
          </div>
          <div class="stack" id="instrumentList"></div>
        </section>

        <section class="panel feed-panel">
          <div class="section-header">
            <div>
              <h2>Latest Feed</h2>
              <div class="panel-subtitle">原始消息流，保留发布时间和原文跳转。</div>
            </div>
            <div class="tiny" id="feedCountLabel"></div>
          </div>
          <div class="stack" id="feedList"></div>
        </section>
      </section>

      <section class="column column-scroll middle-column">
        <section class="panel events-panel">
          <div class="section-header">
            <div>
              <h2>Event Deck</h2>
              <div class="panel-subtitle">事件卡片支持筛选、搜索和点击展开。</div>
            </div>
            <div class="tiny" id="eventCountLabel"></div>
          </div>
          <div class="stack" id="eventsGrid"></div>
        </section>
      </section>

      <section class="column column-scroll right-column">
        <section class="panel detail-panel">
          <div class="section-header">
            <div>
              <h2>Event Detail</h2>
              <div class="panel-subtitle">这里会聚合理由、标的、相关新闻原文链接。</div>
            </div>
          </div>
          <div id="detailView"></div>
        </section>
      </section>
    </section>
  </div>

  <script id="report-data" type="application/json">$payload_json</script>
  <script>
    const report = JSON.parse(document.getElementById("report-data").textContent);
    const unionEvents = [];
    const seenEventIds = new Set();
    ["top_events", "negative_risks", "positive_catalysts", "watchlist"].forEach(function (key) {
      (report[key] || []).forEach(function (event) {
        if (!seenEventIds.has(event.cluster_id)) {
          seenEventIds.add(event.cluster_id);
          unionEvents.push(event);
        }
      });
    });

    const eventByCluster = new Map(unionEvents.map(function (event) {
      return [event.cluster_id, event];
    }));

    const state = {
      direction: "all",
      query: "",
      selectedClusterId: (report.alerts && report.alerts[0] && report.alerts[0].cluster_id)
        || (unionEvents[0] && unionEvents[0].cluster_id)
        || null
    };

    const directionLabels = {
      all: "全部",
      negative: "利空",
      positive: "利好",
      neutral: "中性"
    };

    function escapeHtml(value) {
      return String(value || "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }

    function score(value) {
      const number = Number(value || 0);
      return String(Math.round(number * 100) / 100);
    }

    function tokensForEvent(event) {
      return [
        event.headline,
        event.summary,
        (event.themes || []).join(" "),
        (event.entities || []).join(" "),
        (event.sectors || []).join(" "),
        (event.regions || []).join(" "),
        (event.top_instruments || []).map(function (item) { return item.symbol + " " + item.name; }).join(" ")
      ].join(" ").toLowerCase();
    }

    function matchesDirection(event) {
      return state.direction === "all" || event.direction === state.direction;
    }

    function matchesQuery(text) {
      if (!state.query) {
        return true;
      }
      return String(text || "").toLowerCase().indexOf(state.query) !== -1;
    }

    function filteredEvents() {
      return unionEvents.filter(function (event) {
        return matchesDirection(event) && matchesQuery(tokensForEvent(event));
      });
    }

    function filteredInstruments() {
      return (report.top_instruments || []).filter(function (instrument) {
        const text = [
          instrument.symbol,
          instrument.name,
          instrument.headline,
          (instrument.reasons || []).join(" ")
        ].join(" ").toLowerCase();
        const event = eventByCluster.get(instrument.cluster_id);
        return matchesQuery(text) && (!event || matchesDirection(event));
      });
    }

    function filteredFeed() {
      return (report.latest_feed || []).filter(function (item) {
        const text = [
          item.title,
          item.summary,
          item.source_id,
          (item.themes || []).join(" "),
          (item.entities || []).join(" ")
        ].join(" ").toLowerCase();
        return matchesQuery(text);
      });
    }

    function selectedEvent() {
      const current = eventByCluster.get(state.selectedClusterId);
      if (current) {
        return current;
      }
      const first = filteredEvents()[0] || unionEvents[0] || null;
      if (first) {
        state.selectedClusterId = first.cluster_id;
      }
      return first;
    }

    function rightColumn() {
      return document.querySelector(".right-column");
    }

    function detailView() {
      return document.getElementById("detailView");
    }

    function renderHero() {
      const counts = report.counts || {};
      document.getElementById("heroMeta").textContent =
        "更新于 " + report.created_at + "，来源 " + report.source + "。全局新闻映射到 A 股、港股、美股候选标的。";

      const metrics = [
        { label: "事件", value: counts.ranked_events || 0, meta: "当前排序后的事件数" },
        { label: "候选标的", value: counts.ranked_instruments || 0, meta: "港美 A 股相关标的" },
        { label: "高优先级提醒", value: (report.alert_counts || {}).high || 0, meta: "当前 high 级提醒" },
        { label: "紧急提醒", value: (report.alert_counts || {}).critical || 0, meta: "当前 critical 级提醒" }
      ];
      document.getElementById("metricGrid").innerHTML = metrics.map(function (metric) {
        return '<div class="metric-card">'
          + '<div class="label">' + escapeHtml(metric.label) + '</div>'
          + '<div class="value">' + escapeHtml(metric.value) + '</div>'
          + '<div class="meta">' + escapeHtml(metric.meta) + '</div>'
          + '</div>';
      }).join("");
    }

    function renderFilters() {
      const filterHost = document.getElementById("filterChips");
      const directions = ["all", "negative", "positive", "neutral"];
      filterHost.innerHTML = directions.map(function (key) {
        const active = state.direction === key ? " active" : "";
        return '<button class="chip' + active + '" type="button" data-direction="' + key + '">'
          + escapeHtml(directionLabels[key])
          + "</button>";
      }).join("");
      filterHost.querySelectorAll("[data-direction]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.direction = button.getAttribute("data-direction") || "all";
          render();
        });
      });
    }

    function renderAlerts() {
      const alerts = (report.alerts || []).filter(function (alert) {
        const text = [alert.headline, alert.reason, (alert.symbols || []).join(" ")].join(" ").toLowerCase();
        return (state.direction === "all" || alert.direction === state.direction) && matchesQuery(text);
      });
      document.getElementById("alertCountLabel").textContent = alerts.length + " 条";

      const host = document.getElementById("alertsList");
      if (!alerts.length) {
        host.innerHTML = '<div class="empty">当前筛选条件下没有提醒。</div>';
        return;
      }

      host.innerHTML = alerts.map(function (alert) {
        const active = alert.cluster_id === state.selectedClusterId ? " active" : "";
        const prefix = alert.is_new ? '<span class="badge level-medium">NEW</span>' : "";
        return '<div class="alert-card' + active + '">'
          + '<button class="card-button" type="button" data-cluster="' + escapeHtml(alert.cluster_id) + '">'
          + '<div class="card-topline">'
          + '<span class="badge level-' + escapeHtml(alert.level) + '">' + escapeHtml(alert.level.toUpperCase()) + '</span>'
          + '<span class="badge dir-' + escapeHtml(alert.direction) + '">' + escapeHtml(alert.direction) + '</span>'
          + prefix
          + "</div>"
          + '<div class="headline">' + escapeHtml(alert.headline) + "</div>"
          + '<div class="card-meta">分数 ' + escapeHtml(score(alert.final_score)) + " · "
          + escapeHtml((alert.symbols || []).join(", ") || "n/a") + "</div>"
          + '<p class="summary">' + escapeHtml(alert.reason) + "</p>"
          + "</button></div>";
      }).join("");

      host.querySelectorAll("[data-cluster]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.selectedClusterId = button.getAttribute("data-cluster");
          render();
        });
      });
    }

    function renderEvents() {
      const events = filteredEvents();
      document.getElementById("eventCountLabel").textContent = events.length + " 个";

      const host = document.getElementById("eventsGrid");
      if (!events.length) {
        host.innerHTML = '<div class="empty">没有匹配的事件，试试更宽松的搜索词。</div>';
        return;
      }

      host.innerHTML = events.map(function (event) {
        const active = event.cluster_id === state.selectedClusterId ? " active" : "";
        const topSymbols = (event.top_instruments || []).map(function (item) { return item.symbol; }).slice(0, 4).join(", ") || "n/a";
        return '<div class="event-card' + active + '">'
          + '<button class="card-button" type="button" data-cluster="' + escapeHtml(event.cluster_id) + '">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(event.direction) + '">' + escapeHtml(event.direction) + '</span>'
          + '<span class="badge type-' + escapeHtml(event.event_type) + '">' + escapeHtml(event.event_type) + '</span>'
          + '<span>score ' + escapeHtml(score(event.final_score)) + "</span>"
          + '<span>docs ' + escapeHtml(event.doc_count) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(event.headline) + "</div>"
          + '<p class="summary">' + escapeHtml(event.summary || "暂无事件摘要，先看右侧原文列表。") + "</p>"
          + '<div class="tag-row">'
          + (event.themes || []).slice(0, 4).map(function (theme) {
              return '<span class="mini-tag">' + escapeHtml(theme) + "</span>";
            }).join("")
          + "</div>"
          + '<div class="card-meta">候选标的: ' + escapeHtml(topSymbols) + "</div>"
          + "</button></div>";
      }).join("");

      host.querySelectorAll("[data-cluster]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.selectedClusterId = button.getAttribute("data-cluster");
          render();
        });
      });
    }

    function renderDetail() {
      const event = selectedEvent();
      const host = detailView();
      if (!event) {
        host.innerHTML = '<div class="empty">暂无事件可展示。</div>';
        host.scrollTop = 0;
        const column = rightColumn();
        if (column) {
          column.scrollTop = 0;
        }
        return;
      }

      const instruments = (event.top_instruments || []).map(function (item) {
        return '<div class="instrument-card">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(item.direction) + '">' + escapeHtml(item.direction) + '</span>'
          + '<span>' + escapeHtml(item.market) + "</span>"
          + '<span>score ' + escapeHtml(score(item.final_score)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(item.symbol + " · " + item.name) + "</div>"
          + '<p class="instrument-note">' + escapeHtml((item.reasons || []).join("；") || "暂无说明") + "</p>"
          + "</div>";
      }).join("") || '<div class="empty">当前事件还没有映射到候选标的。</div>';

      const docs = (event.related_documents || []).map(function (doc) {
        const summary = doc.summary || "无摘要";
        const link = doc.url
          ? '<a href="' + escapeHtml(doc.url) + '" target="_blank" rel="noreferrer">打开原文</a>'
          : '<span class="tiny">暂无原文链接</span>';
        return '<li class="doc-card">'
          + '<div class="card-meta">' + escapeHtml(doc.published_at) + " · " + escapeHtml(doc.source_id) + "</div>"
          + '<div class="headline">' + escapeHtml(doc.title) + "</div>"
          + '<p>' + escapeHtml(summary) + "</p>"
          + '<div class="tag-row">'
          + (doc.themes || []).slice(0, 4).map(function (theme) {
              return '<span class="mini-tag">' + escapeHtml(theme) + "</span>";
            }).join("")
          + "</div>"
          + '<div class="tag-row">' + link + "</div>"
          + "</li>";
      }).join("") || '<div class="empty">当前事件没有展开到原文。</div>';

      const rationale = (event.rationale || []).map(function (item) {
        return "<li>" + escapeHtml(item) + "</li>";
      }).join("") || "<li>暂无解释。</li>";

      host.innerHTML = '<div class="detail-grid">'
        + '<div class="detail-block">'
        + '<div class="detail-section-title"><strong>事件摘要</strong><span>核心信息</span></div>'
        + '<div class="card-topline">'
        + '<span class="badge dir-' + escapeHtml(event.direction) + '">' + escapeHtml(event.direction) + '</span>'
        + '<span class="badge type-' + escapeHtml(event.event_type) + '">' + escapeHtml(event.event_type) + '</span>'
        + '<span>docs ' + escapeHtml(event.doc_count) + "</span>"
        + '<span>' + escapeHtml((event.source_ids || []).join(", ") || "source n/a") + "</span>"
        + "</div>"
        + '<h3 class="headline-serif">' + escapeHtml(event.headline) + "</h3>"
        + '<p class="summary">' + escapeHtml(event.summary || "暂无摘要。") + "</p>"
        + '<div class="chip-row">'
        + (event.entities || []).slice(0, 6).map(function (entity) {
            return '<span class="mini-tag">' + escapeHtml(entity) + "</span>";
          }).join("")
        + (event.markets || []).map(function (market) {
            return '<span class="mini-tag">' + escapeHtml(market) + "</span>";
          }).join("")
        + "</div>"
        + '<div class="detail-times">'
        + '<span>首次出现: ' + escapeHtml(event.first_seen_at || "n/a") + "</span>"
        + '<span>最近更新: ' + escapeHtml(event.last_seen_at || "n/a") + "</span>"
        + "</div>"
        + "</div>"
        + '<div class="detail-block">'
        + '<div class="detail-section-title"><strong>评分拆解</strong><span>为什么它排在这里</span></div>'
        + '<div class="score-strip">'
        + '<div class="score-box"><div class="label">Final</div><div class="value">' + escapeHtml(score(event.final_score)) + "</div></div>"
        + '<div class="score-box"><div class="label">Heat</div><div class="value">' + escapeHtml(score(event.heat_score)) + "</div></div>"
        + '<div class="score-box"><div class="label">Importance</div><div class="value">' + escapeHtml(score(event.importance_score)) + "</div></div>"
        + "</div>"
        + "</div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>影响逻辑</strong><span>打分依据</span></div><ul class="rationale-list">' + rationale + "</ul></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>候选标的</strong><span>可能受影响的交易对象</span></div><div class="stack">' + instruments + "</div></div>"
        + '<div class="detail-block"><div class="detail-section-title"><strong>相关新闻原文</strong><span>点击可跳转</span></div><ul class="doc-list">' + docs + "</ul></div>"
        + "</div>";
      host.scrollTop = 0;
      const column = rightColumn();
      if (column) {
        column.scrollTop = 0;
      }
    }

    function renderInstruments() {
      const instruments = filteredInstruments();
      document.getElementById("instrumentCountLabel").textContent = instruments.length + " 个";
      const host = document.getElementById("instrumentList");
      if (!instruments.length) {
        host.innerHTML = '<div class="empty">当前筛选下没有候选标的。</div>';
        return;
      }

      host.innerHTML = instruments.map(function (item) {
        const active = item.cluster_id === state.selectedClusterId ? " active" : "";
        return '<div class="instrument-card' + active + '">'
          + '<button class="card-button" type="button" data-cluster="' + escapeHtml(item.cluster_id) + '">'
          + '<div class="card-topline">'
          + '<span class="badge dir-' + escapeHtml(item.direction) + '">' + escapeHtml(item.direction) + '</span>'
          + '<span>' + escapeHtml(item.market) + "</span>"
          + '<span>score ' + escapeHtml(score(item.final_score)) + "</span>"
          + "</div>"
          + '<div class="headline">' + escapeHtml(item.symbol + " · " + item.name) + "</div>"
          + '<p class="summary">' + escapeHtml(item.headline) + "</p>"
          + "</button></div>";
      }).join("");

      host.querySelectorAll("[data-cluster]").forEach(function (button) {
        button.addEventListener("click", function () {
          state.selectedClusterId = button.getAttribute("data-cluster");
          render();
        });
      });
    }

    function renderFeed() {
      const items = filteredFeed();
      document.getElementById("feedCountLabel").textContent = items.length + " 条";
      const host = document.getElementById("feedList");
      if (!items.length) {
        host.innerHTML = '<div class="empty">当前筛选下没有消息流内容。</div>';
        return;
      }

      host.innerHTML = items.map(function (item) {
        const link = item.url
          ? '<a href="' + escapeHtml(item.url) + '" target="_blank" rel="noreferrer">打开原文</a>'
          : '<span class="tiny">暂无原文链接</span>';
        return '<div class="feed-card">'
          + '<div class="card-meta">' + escapeHtml(item.published_at) + " · " + escapeHtml(item.source_id) + "</div>"
          + '<div class="headline">' + escapeHtml(item.title) + "</div>"
          + '<p>' + escapeHtml(item.summary || "无摘要") + "</p>"
          + '<div class="tag-row">'
          + (item.themes || []).slice(0, 4).map(function (theme) {
              return '<span class="mini-tag">' + escapeHtml(theme) + "</span>";
            }).join("")
          + (item.entities || []).slice(0, 4).map(function (entity) {
              return '<span class="mini-tag">' + escapeHtml(entity) + "</span>";
            }).join("")
          + '</div><div class="tag-row">' + link + "</div></div>";
      }).join("");
    }

    function render() {
      renderHero();
      renderFilters();
      renderAlerts();
      renderEvents();
      renderDetail();
      renderInstruments();
      renderFeed();
    }

    document.getElementById("searchInput").addEventListener("input", function (event) {
      state.query = String(event.target.value || "").trim().toLowerCase();
      render();
    });

    document.getElementById("resetButton").addEventListener("click", function () {
      state.direction = "all";
      state.query = "";
      document.getElementById("searchInput").value = "";
      render();
    });

    render();

    let countdown = 60;
    function refreshPage() {
      const url = new URL(window.location.href);
      url.searchParams.set("ts", String(Date.now()));
      window.location.replace(url.toString());
    }
    function renderRefreshText() {
      document.getElementById("refreshText").textContent =
        "页面每 60 秒自动刷新一次，距下次刷新 " + countdown + " 秒";
    }
    renderRefreshText();
    setInterval(function () {
      countdown -= 1;
      if (countdown <= 0) {
        refreshPage();
        return;
      }
      renderRefreshText();
    }, 1000);
  </script>
</body>
</html>
"""
        )
        return template.substitute(payload_json=payload_json)
