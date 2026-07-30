from __future__ import annotations

from taa_futu.cli import _print_crypto_ofim_payload, _print_crypto_perp_payload


def test_crypto_ofim_cli_prints_plan_reason(capsys) -> None:
    _print_crypto_ofim_payload(
        {
            "mode": "testnet",
            "status": "planned",
            "plan_reason": "loss_guard_learning_estimated_fees_trade_count",
            "account": {},
        }
    )

    assert "Plan reason: loss_guard_learning_estimated_fees_trade_count" in capsys.readouterr().out


def test_crypto_perp_cli_prints_guard_reason(capsys) -> None:
    _print_crypto_perp_payload(
        {
            "mode": "paper",
            "status": "submitted",
            "reason": "perp_loss_guard_fees_trade_count",
            "account": {},
        }
    )

    assert "Reason: perp_loss_guard_fees_trade_count" in capsys.readouterr().out
