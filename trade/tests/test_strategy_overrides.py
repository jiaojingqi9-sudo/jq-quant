"""Unit tests for the learning-to-strategy promotion bridge.

These tests are self-contained: they use a tiny stand-in dataclass and temp
files, so they run without a broker, network, or .env. A final guarded test
exercises the real ``Settings`` dataclass when python-dotenv is importable.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from taa_futu import strategy_overrides as so


# ── a minimal stand-in for the real Settings dataclass ────────────────────────


@dataclasses.dataclass(frozen=True)
class FakeSettings:
    auto_trader_min_order_value_usd: float = 500.0
    fusion_top_k: int = 3
    ofim_entry_threshold: float = 0.20
    futu_enable_real_trading: bool = False  # must never be overridable


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def _write_json(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj), encoding="utf-8")


def _candidate(**overrides) -> dict:
    base = {
        "action_type": "raise_min_order_value",
        "candidate_id": "cand_minorder",
        "confidence": 0.55,
        "current_value": 500.0,
        "evidence": {"trades": 54, "net_pnl": -423.7},
        "param": "AUTO_TRADER_MIN_ORDER_VALUE_USD",
        "proposed_value": 750.0,
        "status": "research",
    }
    base.update(overrides)
    return base


def _promotion(candidate_id: str, *, paper_allowed: bool, blockers=None) -> dict:
    return {
        "decisions": [
            {
                "candidate_id": candidate_id,
                "decision": "eligible_for_paper_replay" if paper_allowed else "needs_more_data",
                "live_allowed": False,
                "paper_allowed": paper_allowed,
                "blockers": blockers or [],
            }
        ],
        "policy": {"live_auto_promotion": False, "paper_auto_promotion": True},
    }


@pytest.fixture()
def files(tmp_path: Path):
    return {
        "candidates": tmp_path / "candidates.jsonl",
        "promotion": tmp_path / "promotion.json",
        "overrides": tmp_path / "overrides.json",
    }


# ── whitelist / guard tests ───────────────────────────────────────────────────


def test_safety_fields_are_never_overridable():
    assert so.is_overridable("auto_trader_min_order_value_usd")
    for field in (
        "futu_enable_real_trading",
        "futu_allow_auto_real",
        "futu_unlock_trade_password_md5",
        "futu_acc_id",
        "futu_host",
        "futu_port",
        "ofim_crypto_api_key",
        "ofim_crypto_api_secret",
    ):
        assert not so.is_overridable(field), field


def test_param_to_field_lowercases_env_name():
    assert so.param_to_field("AUTO_TRADER_MIN_ORDER_VALUE_USD") == "auto_trader_min_order_value_usd"
    assert so.param_to_field("") == ""
    assert so.param_to_field(None) == ""


# ── promote tests ─────────────────────────────────────────────────────────────


def test_promote_eligible_candidate_writes_override(files):
    _write_jsonl(files["candidates"], [_candidate()])
    _write_json(files["promotion"], _promotion("cand_minorder", paper_allowed=True))

    entry = so.promote_candidate(
        "cand_minorder",
        approved_by="jiao",
        candidates_path=files["candidates"],
        promotion_path=files["promotion"],
        overrides_path=files["overrides"],
    )
    assert entry["field"] == "auto_trader_min_order_value_usd"
    assert entry["value"] == 750.0
    assert entry["previous_value"] == 500.0
    assert entry["approved_by"] == "jiao"
    assert entry["scope"] == "paper"
    assert entry["evidence_digest"]

    doc = json.loads(files["overrides"].read_text())
    assert doc["overrides"]["auto_trader_min_order_value_usd"]["value"] == 750.0


def test_promote_refuses_when_paper_not_allowed(files):
    _write_jsonl(files["candidates"], [_candidate()])
    _write_json(
        files["promotion"],
        _promotion("cand_minorder", paper_allowed=False, blockers=["candidate_evidence_sample_too_small"]),
    )
    with pytest.raises(so.PromotionError):
        so.promote_candidate(
            "cand_minorder",
            approved_by="jiao",
            candidates_path=files["candidates"],
            promotion_path=files["promotion"],
            overrides_path=files["overrides"],
        )
    assert not files["overrides"].exists()


def test_promote_refuses_advisory_only_candidate(files):
    cand = _candidate(
        candidate_id="cand_symbol",
        action_type="review_universe_symbol",
        param="",
        proposed_value=None,
        current_value=None,
    )
    _write_jsonl(files["candidates"], [cand])
    _write_json(files["promotion"], _promotion("cand_symbol", paper_allowed=True))
    with pytest.raises(so.PromotionError):
        so.promote_candidate(
            "cand_symbol",
            approved_by="jiao",
            candidates_path=files["candidates"],
            promotion_path=files["promotion"],
            overrides_path=files["overrides"],
        )


def test_promote_refuses_non_whitelisted_param(files):
    cand = _candidate(
        candidate_id="cand_evil",
        param="FUTU_ENABLE_REAL_TRADING",
        proposed_value=1,
        current_value=0,
    )
    _write_jsonl(files["candidates"], [cand])
    _write_json(files["promotion"], _promotion("cand_evil", paper_allowed=True))
    with pytest.raises(so.PromotionError):
        so.promote_candidate(
            "cand_evil",
            approved_by="attacker",
            candidates_path=files["candidates"],
            promotion_path=files["promotion"],
            overrides_path=files["overrides"],
        )


def test_promote_missing_candidate(files):
    _write_jsonl(files["candidates"], [_candidate()])
    _write_json(files["promotion"], _promotion("cand_minorder", paper_allowed=True))
    with pytest.raises(so.PromotionError):
        so.promote_candidate(
            "does_not_exist",
            approved_by="jiao",
            candidates_path=files["candidates"],
            promotion_path=files["promotion"],
            overrides_path=files["overrides"],
        )


# ── apply tests ───────────────────────────────────────────────────────────────


def test_apply_changes_whitelisted_field(files):
    _write_json(
        files["overrides"],
        {
            "overrides": {
                "auto_trader_min_order_value_usd": {"field": "auto_trader_min_order_value_usd", "value": 750.0}
            }
        },
    )
    out = so.apply_promoted_overrides(FakeSettings(), overrides_path=files["overrides"])
    assert out.auto_trader_min_order_value_usd == 750.0
    assert isinstance(out.auto_trader_min_order_value_usd, float)


def test_apply_ignores_forbidden_field_even_if_present(files):
    _write_json(
        files["overrides"],
        {
            "overrides": {
                "futu_enable_real_trading": {"field": "futu_enable_real_trading", "value": True},
                "fusion_top_k": {"field": "fusion_top_k", "value": 5},
            }
        },
    )
    base = FakeSettings()
    out = so.apply_promoted_overrides(base, overrides_path=files["overrides"])
    assert out.futu_enable_real_trading is False  # untouched
    assert out.fusion_top_k == 5
    assert isinstance(out.fusion_top_k, int)


def test_apply_no_file_returns_same_settings(tmp_path):
    base = FakeSettings()
    out = so.apply_promoted_overrides(base, overrides_path=tmp_path / "missing.json")
    assert out == base


def test_apply_ignores_unknown_field(files):
    _write_json(
        files["overrides"],
        {"overrides": {"not_a_real_field": {"value": 1}}},
    )
    base = FakeSettings()
    assert so.apply_promoted_overrides(base, overrides_path=files["overrides"]) == base


# ── revert + round trip ───────────────────────────────────────────────────────


def test_revert_by_candidate_id(files):
    _write_jsonl(files["candidates"], [_candidate()])
    _write_json(files["promotion"], _promotion("cand_minorder", paper_allowed=True))
    so.promote_candidate(
        "cand_minorder",
        approved_by="jiao",
        candidates_path=files["candidates"],
        promotion_path=files["promotion"],
        overrides_path=files["overrides"],
    )
    removed = so.revert_override(candidate_id="cand_minorder", overrides_path=files["overrides"])
    assert removed == ["auto_trader_min_order_value_usd"]
    assert load_overrides_count(files["overrides"]) == 0


def test_promote_then_apply_round_trip(files):
    _write_jsonl(files["candidates"], [_candidate()])
    _write_json(files["promotion"], _promotion("cand_minorder", paper_allowed=True))
    so.promote_candidate(
        "cand_minorder",
        approved_by="jiao",
        candidates_path=files["candidates"],
        promotion_path=files["promotion"],
        overrides_path=files["overrides"],
    )
    out = so.apply_promoted_overrides(FakeSettings(), overrides_path=files["overrides"])
    assert out.auto_trader_min_order_value_usd == 750.0


def load_overrides_count(path: Path) -> int:
    return len(json.loads(path.read_text())["overrides"])


# ── guarded integration test against the real Settings dataclass ──────────────


def test_apply_on_real_settings_when_available(files):
    pytest.importorskip("dotenv")
    from taa_futu.config import load_settings

    settings = load_settings(env_file=files["candidates"].parent / "nonexistent.env")
    _write_json(
        files["overrides"],
        {"overrides": {"auto_trader_min_order_value_usd": {"value": 777.0}}},
    )
    out = so.apply_promoted_overrides(settings, overrides_path=files["overrides"])
    assert out.auto_trader_min_order_value_usd == 777.0
    # safety switch unchanged
    assert out.futu_enable_real_trading == settings.futu_enable_real_trading
