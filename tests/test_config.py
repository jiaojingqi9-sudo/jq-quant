from pathlib import Path

from taa_futu.config import load_settings


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
