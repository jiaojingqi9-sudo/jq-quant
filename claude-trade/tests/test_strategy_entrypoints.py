from pathlib import Path

import pytest

from claude_trade.config import load_settings
from claude_trade.engine.auto_trader import AutoTrader


def test_load_settings_prefers_env_file_over_existing_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ACTIVE_STRATEGIES", "cascade")
    env_file = tmp_path / ".env"
    env_file.write_text("ACTIVE_STRATEGIES=dual_momentum,rsi_mean_reversion\n", encoding="utf-8")

    settings = load_settings(env_file)

    assert settings.active_strategies == ["dual_momentum", "rsi_mean_reversion"]


def test_autotrader_reports_unknown_strategy_names_clearly(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ACTIVE_STRATEGIES=does_not_exist\n", encoding="utf-8")
    settings = load_settings(env_file)

    with pytest.raises(ValueError) as excinfo:
        AutoTrader(settings, dry_run=True)

    message = str(excinfo.value)
    assert "ACTIVE_STRATEGIES contains unknown strategy names" in message
    assert "does_not_exist" in message
    assert "cascade" in message
