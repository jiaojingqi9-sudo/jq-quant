from types import SimpleNamespace

import pandas as pd

import taa_futu.intraday_replay as replay_mod
import taa_futu.ofim_intraday as ofim_mod
from taa_futu.config import load_settings


def test_run_ofim_replay_uses_public_generate_plan_and_disables_crypto(monkeypatch, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "OFIM_UNIVERSE=US.AAPL,US.MSFT",
                "OFIM_CRYPTO_UNIVERSE=BTC/USDT,ETH/USDT",
            ]
        ),
        encoding="utf-8",
    )
    settings = load_settings(env_file)
    seen: dict[str, object] = {}

    class FakeOfimStrategy:
        def __init__(self, replay_settings) -> None:
            seen["crypto_universe"] = replay_settings.ofim_crypto_universe
            seen["crypto_proxy"] = replay_settings.ofim_crypto_to_proxy

        def generate_plan(self, trader, held_symbols):
            seen["generate_plan_called"] = True
            return SimpleNamespace(target_weights={"US.AAPL": 0.5})

    def fake_run_intraday_replay(generate_plan_fn, start, end, **kwargs):
        plan = generate_plan_fn(object(), set())
        seen["plan_weights"] = plan.target_weights
        seen["range"] = (start, end)
        seen["kwargs"] = kwargs
        return "ok"

    monkeypatch.setattr(ofim_mod, "OfimIntradayStrategy", FakeOfimStrategy)
    monkeypatch.setattr(replay_mod, "run_intraday_replay", fake_run_intraday_replay)

    result = replay_mod.run_ofim_replay("2026-03-23", "2026-03-24", settings)

    assert result == "ok"
    assert seen["generate_plan_called"] is True
    assert seen["crypto_universe"] == ()
    assert seen["crypto_proxy"] == ()
    assert seen["plan_weights"] == {"US.AAPL": 0.5}
    assert seen["range"] == ("2026-03-23", "2026-03-24")


def test_run_intraday_replay_emits_progress_updates(monkeypatch, tmp_path) -> None:
    day_dir = tmp_path / "2026-03-23"
    day_dir.mkdir()
    seen: list[dict[str, object]] = []

    class FakeStore:
        def __init__(self, path) -> None:
            self.path = path

        def advance_to(self, ts: str) -> None:
            return None

        def get_snapshot(self, code: str) -> dict:
            return {"last_price": 100.0, "bid_price": 99.9, "ask_price": 100.1, "price_spread": 0.2}

    monkeypatch.setattr(replay_mod, "_iter_day_dirs", lambda start, end: [day_dir])
    monkeypatch.setattr(replay_mod, "ReplayDataStore", FakeStore)
    monkeypatch.setattr(replay_mod, "_get_cycle_timestamps", lambda path: ["2026-03-23T14:30:00+00:00"])

    result = replay_mod.run_intraday_replay(
        lambda trader, held_symbols: SimpleNamespace(target_weights={}),
        "2026-03-23",
        "2026-03-23",
        progress_callback=seen.append,
    )

    assert not result.equity_curve.empty
    assert [item["phase"] for item in seen] == ["start", "day_complete", "complete"]
    assert seen[-1]["progress"] == 1.0
