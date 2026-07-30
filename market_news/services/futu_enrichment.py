"""Futu enrichment for outbound news alerts.

This is a sidecar workflow attached to the ``notify`` runtime line. For each
alert that the existing pipeline already decided to send, we ask Futu for a
short list of complementary signals (capital flow, capital distribution, a
quick community-feed pulse). The result is written to a separate sidecar
JSON file under ``reports/live/``. The existing phone-alert preview file is
NEVER touched, and notify continues to send exactly the same message it
would have sent without this module.

Why a sidecar instead of inlining the data into the alert message? Two
reasons:

1. Reversibility. If the enrichment data is wrong or Futu is down, the
   user's WhatsApp message is identical to today's. Nothing breaks.
2. Iteration. The enrichment payload schema can evolve without churning the
   alert format the user has already gotten used to reading.

When the user wants the enrichment surfaced in the message itself, that is
a follow-up change that can read the sidecar file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any, Iterable


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class AlertEnrichment:
    cluster_id: str
    headline: str
    level: str
    direction: str
    symbols: list[str] = field(default_factory=list)
    per_symbol: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "headline": self.headline,
            "level": self.level,
            "direction": self.direction,
            "symbols": list(self.symbols),
            "per_symbol": list(self.per_symbol),
            "note": self.note,
        }


@dataclass(slots=True)
class EnrichmentReport:
    generated_at: str
    enabled: bool
    skipped_reason: str | None
    alerts: list[AlertEnrichment] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "enabled": self.enabled,
            "skipped_reason": self.skipped_reason,
            "alerts": [a.to_dict() for a in self.alerts],
        }


# ---------------------------------------------------------------------------
# Enrichment logic
# ---------------------------------------------------------------------------


def _extract_symbols(alert: dict[str, Any]) -> list[str]:
    raw = alert.get("symbols") or alert.get("top_instruments") or []
    symbols: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            token = (
                item.get("symbol")
                or item.get("code")
                or item.get("ticker")
                or item.get("name")
            )
        else:
            token = item
        if not token:
            continue
        text = str(token).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        symbols.append(text)
    return symbols


def _summarize_capital_flow(flow: dict[str, Any] | None) -> dict[str, Any] | None:
    if not flow:
        return None
    main = flow.get("main_in_flow", 0.0)
    super_in = flow.get("super_in_flow", 0.0)
    direction = "in" if (main + super_in) > 0 else "out" if (main + super_in) < 0 else "flat"
    return {
        "period": flow.get("period_type"),
        "main_in_flow": main,
        "super_in_flow": super_in,
        "big_in_flow": flow.get("big_in_flow"),
        "direction": direction,
        "as_of": flow.get("capital_flow_item_time"),
    }


def _summarize_distribution(dist: dict[str, Any] | None) -> dict[str, Any] | None:
    if not dist:
        return None
    net = (
        (dist.get("capital_in_super", 0.0) - dist.get("capital_out_super", 0.0))
        + (dist.get("capital_in_big", 0.0) - dist.get("capital_out_big", 0.0))
    )
    return {
        "net_super_plus_big": net,
        "in_super": dist.get("capital_in_super"),
        "in_big": dist.get("capital_in_big"),
        "out_super": dist.get("capital_out_super"),
        "out_big": dist.get("capital_out_big"),
        "as_of": dist.get("update_time"),
    }


def _summarize_sentiment(sent: dict[str, Any] | None, *, top_n: int = 3) -> dict[str, Any] | None:
    if not sent:
        return None
    return {
        "post_count": sent.get("post_count", 0),
        "recent_headlines": (sent.get("recent_headlines") or [])[:top_n],
        "fetched_at": sent.get("fetched_at"),
    }


def enrich_alert(alert: dict[str, Any]) -> AlertEnrichment:
    """Look up Futu data for every symbol referenced in the alert.

    All sub-calls return ``None`` on any failure; we capture whatever came
    back without raising. The caller can write the result regardless.
    """

    # Import lazily so the module can be imported on machines where the futu
    # SDK is unavailable.
    from market_news.infrastructure import futu_client

    symbols = _extract_symbols(alert)
    per_symbol: list[dict[str, Any]] = []
    for symbol in symbols[:6]:  # cap to avoid hammering OpenD on big clusters
        capital_flow = futu_client.get_capital_flow(symbol, period_type="INTRADAY")
        if capital_flow is None:
            # Fall back to the daily aggregate so we still say *something*.
            capital_flow = futu_client.get_capital_flow(symbol, period_type="DAY")
        distribution = futu_client.get_capital_distribution(symbol)
        sentiment = futu_client.futunn_community_sentiment(symbol, size=20)
        per_symbol.append(
            {
                "input_symbol": symbol,
                "futu_code": futu_client.normalize_to_futu_code(symbol),
                "capital_flow": _summarize_capital_flow(capital_flow),
                "capital_distribution": _summarize_distribution(distribution),
                "community_sentiment": _summarize_sentiment(sentiment),
            }
        )
    return AlertEnrichment(
        cluster_id=str(alert.get("cluster_id", "")),
        headline=str(alert.get("headline", "")),
        level=str(alert.get("level", "")),
        direction=str(alert.get("direction", "")),
        symbols=symbols,
        per_symbol=per_symbol,
        note="best-effort enrichment; missing fields mean no data from Futu",
    )


def build_report(payload: dict[str, Any], *, enabled: bool) -> EnrichmentReport:
    now = datetime.now(timezone.utc).isoformat()
    if not enabled:
        return EnrichmentReport(generated_at=now, enabled=False, skipped_reason="flag-off")
    alerts = payload.get("alerts") or []
    if not isinstance(alerts, list) or not alerts:
        return EnrichmentReport(generated_at=now, enabled=True, skipped_reason="no-alerts")
    enriched: list[AlertEnrichment] = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        try:
            enriched.append(enrich_alert(alert))
        except Exception as exc:  # defensive — never crash notify
            logger.warning("enrich_alert failed for cluster=%s: %s", alert.get("cluster_id"), exc)
            enriched.append(
                AlertEnrichment(
                    cluster_id=str(alert.get("cluster_id", "")),
                    headline=str(alert.get("headline", "")),
                    level=str(alert.get("level", "")),
                    direction=str(alert.get("direction", "")),
                    note=f"enrichment-error: {exc}",
                )
            )
    return EnrichmentReport(generated_at=now, enabled=True, skipped_reason=None, alerts=enriched)


def write_sidecar(report: EnrichmentReport, *, preview_path: Path) -> Path:
    """Write the enrichment sidecar next to the preview file.

    For ``reports/live/latest_phone_alert.txt`` the sidecar is
    ``reports/live/latest_phone_alert_enriched.json``.
    """

    sidecar_path = preview_path.with_name(preview_path.stem + "_enriched.json")
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return sidecar_path
