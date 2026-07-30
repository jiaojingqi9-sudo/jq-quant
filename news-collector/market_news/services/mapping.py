from __future__ import annotations

import json
from pathlib import Path

from market_news.common import clamp, unique_preserve
from market_news.domain.models import EventCluster, ImpactAssessment, InstrumentDescriptor, InstrumentMatch, Market


class ConfigDrivenInstrumentMapper:
    def __init__(self, instruments: list[InstrumentDescriptor]) -> None:
        self.instruments = instruments

    @classmethod
    def from_file(cls, path: Path) -> "ConfigDrivenInstrumentMapper":
        payload = json.loads(path.read_text(encoding="utf-8"))
        instruments = [
            InstrumentDescriptor(
                symbol=item["symbol"],
                market=Market(item["market"]),
                asset_type=item["asset_type"],
                name=item["name"],
                sectors=item.get("sectors", []),
                themes=item.get("themes", []),
                aliases=item.get("aliases", []),
                liquidity_score=float(item.get("liquidity_score", 0.5)),
                metadata=item.get("metadata", {}),
            )
            for item in payload
        ]
        return cls(instruments)

    def map(self, cluster: EventCluster, impact: ImpactAssessment) -> list[InstrumentMatch]:
        cluster_text = cluster.combined_text.lower()
        cluster_entities = {entity.lower() for entity in cluster.entities}
        cluster_themes = {theme.lower() for theme in unique_preserve(cluster.themes + impact.affected_themes)}
        cluster_sectors = {sector.lower() for sector in unique_preserve(cluster.sectors + impact.affected_sectors)}
        preferred_markets = {market.value for market in impact.affected_markets}
        candidate_instruments = self._merge_dynamic_instruments(cluster)
        direct_symbols = self._resolve_direct_symbols(cluster)

        matches: list[InstrumentMatch] = []
        for instrument in candidate_instruments:
            sector_overlap = len(cluster_sectors & {sector.lower() for sector in instrument.sectors})
            theme_overlap = len(cluster_themes & {theme.lower() for theme in instrument.themes})
            entity_overlap = len(cluster_entities & {alias.lower() for alias in instrument.aliases})
            alias_hits = [alias for alias in instrument.aliases if alias.lower() in cluster_text]
            market_bonus = 1.0 if instrument.market.value in preferred_markets else 0.3
            direct_match = instrument.symbol in direct_symbols

            exposure = clamp(
                0.28 * min(1.0, sector_overlap)
                + 0.24 * min(1.0, theme_overlap)
                + 0.28 * min(1.0, entity_overlap)
                + 0.10 * min(1.0, len(alias_hits))
                + 0.10 * market_bonus
                + 0.22 * (1.0 if direct_match else 0.0)
            )
            if direct_match:
                exposure = max(exposure, 0.78)
            if exposure < 0.4:
                continue

            reasons = []
            if sector_overlap:
                reasons.append(f"sector overlap: {', '.join(instrument.sectors)}")
            if theme_overlap:
                reasons.append(f"theme overlap: {', '.join(instrument.themes)}")
            if entity_overlap or alias_hits:
                reasons.append(f"entity/alias match: {', '.join(unique_preserve(alias_hits + instrument.aliases[:1]))}")
            if instrument.market.value in preferred_markets:
                reasons.append(f"preferred market: {instrument.market.value}")
            if direct_match:
                reasons.append("direct code from source")
            matches.append(
                InstrumentMatch(
                    cluster_id=cluster.cluster_id,
                    instrument=instrument,
                    direction=impact.direction,
                    exposure_score=exposure,
                    reasons=reasons or ["broad market mapping"],
                )
            )

        matches.sort(key=lambda item: item.exposure_score, reverse=True)
        return matches[:8]

    def _merge_dynamic_instruments(self, cluster: EventCluster) -> list[InstrumentDescriptor]:
        combined: dict[str, InstrumentDescriptor] = {
            instrument.symbol: instrument for instrument in self.instruments
        }
        for instrument in self._dynamic_instruments(cluster):
            combined.setdefault(instrument.symbol, instrument)
        return list(combined.values())

    def _dynamic_instruments(self, cluster: EventCluster) -> list[InstrumentDescriptor]:
        dynamic: list[InstrumentDescriptor] = []
        for document in cluster.documents:
            stock_code = str(document.metadata.get("stock_code", "")).strip()
            stock_name = str(document.metadata.get("stock_name", "")).strip()
            market = str(document.metadata.get("instrument_market", "")).strip()
            if not stock_code or not stock_name or not market:
                continue
            normalized_symbol = self._normalize_symbol(stock_code, market)
            if not normalized_symbol:
                continue
            dynamic.append(
                InstrumentDescriptor(
                    symbol=normalized_symbol,
                    market=Market(market),
                    asset_type="stock",
                    name=stock_name,
                    sectors=document.metadata.get("sectors", []),
                    themes=document.themes or cluster.themes,
                    aliases=unique_preserve([stock_name, stock_code, normalized_symbol]),
                    liquidity_score=float(document.metadata.get("liquidity_score", 0.65)),
                    metadata={"generated_from": document.source_id},
                )
            )
        unique: dict[str, InstrumentDescriptor] = {}
        for instrument in dynamic:
            unique.setdefault(instrument.symbol, instrument)
        return list(unique.values())

    def _resolve_direct_symbols(self, cluster: EventCluster) -> set[str]:
        symbols: set[str] = set()
        for document in cluster.documents:
            for raw_code in document.metadata.get("direct_codes", []):
                code = str(raw_code).strip()
                if not code:
                    continue
                for instrument in self.instruments:
                    digits = "".join(char for char in instrument.symbol if char.isdigit())
                    if digits == code:
                        symbols.add(instrument.symbol)
        return symbols

    def _normalize_symbol(self, stock_code: str, market: str) -> str | None:
        digits = "".join(char for char in stock_code if char.isdigit())
        if market == Market.CN_A.value:
            if digits.startswith("6"):
                return f"{digits}.SH"
            if digits.startswith(("0", "3")):
                return f"{digits}.SZ"
        if market == Market.HK.value:
            return f"{digits.zfill(4)}.HK"
        if market == Market.US.value:
            return stock_code.upper()
        return None
