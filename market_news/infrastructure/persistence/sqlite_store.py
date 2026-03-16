from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from datetime import timedelta

from market_news.common import stable_id, utcnow
from market_news.domain.models import PipelineSnapshot


class SQLiteRunStore:
    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _initialize(self) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS pipeline_runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    raw_count INTEGER NOT NULL,
                    document_count INTEGER NOT NULL,
                    cluster_count INTEGER NOT NULL,
                    instrument_count INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS documents (
                    run_id TEXT NOT NULL,
                    doc_id TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    url TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    source_trust REAL NOT NULL,
                    themes_json TEXT NOT NULL,
                    entities_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, doc_id)
                );

                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL,
                    cluster_id TEXT NOT NULL,
                    headline TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    heat_score REAL NOT NULL,
                    importance_score REAL NOT NULL,
                    confidence_score REAL NOT NULL,
                    final_score REAL NOT NULL,
                    markets_json TEXT NOT NULL,
                    themes_json TEXT NOT NULL,
                    rationale_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, cluster_id)
                );

                CREATE TABLE IF NOT EXISTS ranked_instruments (
                    run_id TEXT NOT NULL,
                    cluster_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    asset_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    exposure_score REAL NOT NULL,
                    impact_score REAL NOT NULL,
                    confidence_score REAL NOT NULL,
                    final_score REAL NOT NULL,
                    reasons_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, cluster_id, symbol, market)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_ranked_instruments_pk
                ON ranked_instruments (run_id, cluster_id, symbol, market);

                CREATE TABLE IF NOT EXISTS alert_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    cluster_id TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    target TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    message_text TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_alert_deliveries_lookup
                ON alert_deliveries (channel, target, cluster_id, delivered_at);
                """
            )
            connection.commit()

    def load_recent_event_ids(self) -> set[str]:
        with closing(sqlite3.connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT run_id
                FROM pipeline_runs
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return set()
            events = connection.execute(
                """
                SELECT cluster_id
                FROM events
                WHERE run_id = ?
                """,
                (row[0],),
            ).fetchall()
        return {event_row[0] for event_row in events}

    def persist(self, snapshot: PipelineSnapshot) -> None:
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO pipeline_runs (
                    run_id, created_at, source_name, raw_count, document_count, cluster_count, instrument_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.run_id,
                    snapshot.created_at.isoformat(),
                    snapshot.source_name,
                    len(snapshot.raw_records),
                    len(snapshot.documents),
                    len(snapshot.clusters),
                    len(snapshot.ranked_instruments),
                ),
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO documents (
                    run_id, doc_id, source_id, title, url, published_at, source_trust, themes_json, entities_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot.run_id,
                        document.doc_id,
                        document.source_id,
                        document.title,
                        document.url,
                        document.published_at.isoformat(),
                        document.source_trust,
                        json.dumps(document.themes, ensure_ascii=False),
                        json.dumps(document.entities, ensure_ascii=False),
                    )
                    for document in snapshot.documents
                ],
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO events (
                    run_id, cluster_id, headline, event_type, direction, heat_score,
                    importance_score, confidence_score, final_score, markets_json, themes_json, rationale_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot.run_id,
                        event.cluster_id,
                        event.headline,
                        event.impact.event_type.value,
                        event.impact.direction.value,
                        event.heat_score,
                        event.importance_score,
                        event.confidence_score,
                        event.final_score,
                        json.dumps([market.value for market in event.impact.affected_markets], ensure_ascii=False),
                        json.dumps(event.impact.affected_themes, ensure_ascii=False),
                        json.dumps(event.impact.rationale, ensure_ascii=False),
                    )
                    for event in snapshot.ranked_events
                ],
            )
            connection.execute(
                "DELETE FROM ranked_instruments WHERE run_id = ?",
                (snapshot.run_id,),
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO ranked_instruments (
                    run_id, cluster_id, symbol, market, asset_type, name, direction,
                    exposure_score, impact_score, confidence_score, final_score, reasons_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot.run_id,
                        instrument.cluster_id,
                        instrument.symbol,
                        instrument.market.value,
                        instrument.asset_type,
                        instrument.name,
                        instrument.direction.value,
                        instrument.exposure_score,
                        instrument.impact_score,
                        instrument.confidence_score,
                        instrument.final_score,
                        json.dumps(instrument.reasons, ensure_ascii=False),
                    )
                    for instrument in snapshot.ranked_instruments
                ],
            )
            connection.commit()

    def load_sent_alert_cluster_ids(
        self,
        channel: str,
        target: str,
        *,
        lookback_hours: int = 72,
    ) -> set[str]:
        threshold = (utcnow() - timedelta(hours=lookback_hours)).isoformat()
        with closing(sqlite3.connect(self.database_path)) as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT cluster_id
                FROM alert_deliveries
                WHERE channel = ? AND target = ? AND delivered_at >= ?
                """,
                (channel, target, threshold),
            ).fetchall()
        return {row[0] for row in rows}

    def persist_alert_delivery(
        self,
        *,
        run_id: str,
        channel: str,
        target: str,
        cluster_ids: list[str],
        message_text: str,
    ) -> None:
        delivered_at = utcnow().isoformat()
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.executemany(
                """
                INSERT OR REPLACE INTO alert_deliveries (
                    delivery_id, run_id, cluster_id, channel, target, delivered_at, message_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        stable_id(run_id, channel, target, cluster_id),
                        run_id,
                        cluster_id,
                        channel,
                        target,
                        delivered_at,
                        message_text,
                    )
                    for cluster_id in cluster_ids
                ],
            )
            connection.commit()
