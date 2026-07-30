from pathlib import Path

from taa_futu.config import _resolve_env_file, load_settings


def test_resolve_env_file_falls_back_to_repo_root_when_cwd_env_missing(
    monkeypatch, tmp_path: Path
) -> None:
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text("X=1\n", encoding="utf-8")

    assert _resolve_env_file(".env", fallback_root=root) == root / ".env"


def test_resolve_env_file_prefers_cwd_env_when_present(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("X=1\n", encoding="utf-8")

    assert _resolve_env_file(".env", fallback_root=tmp_path / "repo") == Path(".env")


def test_resolve_env_file_keeps_explicit_path_unchanged(tmp_path: Path) -> None:
    explicit = tmp_path / "custom" / "missing.env"

    assert _resolve_env_file(explicit, fallback_root=tmp_path) == explicit


def test_load_settings_uses_latest_env_file_values(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STACK_BASELINE_ENABLED", "true")
    monkeypatch.setenv("STACK_BASELINE_WEIGHT", "0.55")
    monkeypatch.setenv("STACK_FUSION_WEIGHT", "0.35")

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_BASELINE_ENABLED=false",
                "STACK_BASELINE_WEIGHT=0.0000",
                "STACK_FUSION_WEIGHT=1.0000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.stack_baseline_enabled is False
    assert settings.stack_baseline_weight == 0.0
    assert settings.stack_fusion_weight == 1.0
    assert settings.stack_cascade_weight == 0.0
    assert settings.trade_costs_enabled is True


def test_load_settings_reads_active_strategy_plug(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("STACK_ACTIVE_STRATEGY", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "STACK_ACTIVE_STRATEGY=ofim",
                "STACK_BASELINE_ENABLED=true",
                "STACK_BASELINE_WEIGHT=0.25",
                "STACK_FUSION_WEIGHT=0.25",
                "STACK_OFIM_WEIGHT=0.25",
                "STACK_CASCADE_WEIGHT=0.25",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.stack_active_strategy == "ofim"


def test_load_settings_reads_stock_runtime_guards(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AUTO_TRADER_EXIT_CONFIRM_CYCLES", raising=False)
    monkeypatch.delenv("AUTO_TRADER_MIN_SYMBOL_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("AUTO_TRADER_MAX_TARGET_GROSS_EXPOSURE", raising=False)
    monkeypatch.delenv("AUTO_TRADER_MAX_TARGET_WEIGHT", raising=False)
    monkeypatch.delenv("AUTO_TRADER_MAX_ORDER_VALUE_USD", raising=False)
    monkeypatch.delenv("AUTO_TRADER_MAX_CYCLE_TURNOVER_USD", raising=False)
    monkeypatch.delenv("AUTO_TRADER_MAX_EPOCH_LOSS_USD", raising=False)
    monkeypatch.delenv("AUTO_TRADER_MAX_EPOCH_LOSS_PCT", raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "AUTO_TRADER_EXIT_CONFIRM_CYCLES=3",
                "AUTO_TRADER_MIN_SYMBOL_INTERVAL_SECONDS=600",
                "AUTO_TRADER_MAX_TARGET_GROSS_EXPOSURE=0.85",
                "AUTO_TRADER_MAX_TARGET_WEIGHT=0.40",
                "AUTO_TRADER_MAX_ORDER_VALUE_USD=25000",
                "AUTO_TRADER_MAX_CYCLE_TURNOVER_USD=75000",
                "AUTO_TRADER_MAX_EPOCH_LOSS_USD=5000",
                "AUTO_TRADER_MAX_EPOCH_LOSS_PCT=0.05",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file)

    assert settings.auto_trader_exit_confirm_cycles == 3
    assert settings.auto_trader_min_symbol_interval_seconds == 600
    assert settings.auto_trader_max_target_gross_exposure == 0.85
    assert settings.auto_trader_max_target_weight == 0.40
    assert settings.auto_trader_max_order_value_usd == 25000
    assert settings.auto_trader_max_cycle_turnover_usd == 75000
    assert settings.auto_trader_max_epoch_loss_usd == 5000
    assert settings.auto_trader_max_epoch_loss_pct == 0.05
