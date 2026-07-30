from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_isolated_news_learning_launcher_only_targets_learning_line() -> None:
    launcher = ROOT / "scripts" / "market_news_learning_auto.command"
    stopper = ROOT / "scripts" / "market_news_learning_auto_stop.command"

    launcher_text = launcher.read_text(encoding="utf-8")
    stopper_text = stopper.read_text(encoding="utf-8")

    assert "news-learning-auto --no-copy" in launcher_text
    assert "ai.codex.marketnews.newslearning" in launcher_text
    assert "reports/live/news_learning_status.json" in launcher_text
    assert "news_learning_codex_handoff.md" in launcher_text

    combined = launcher_text + stopper_text
    assert "market_news collect" not in combined
    assert "market_news notify" not in combined
    assert "market_news health" not in combined
    assert "ai.codex.marketnews.collect" not in combined
    assert "ai.codex.marketnews.notify" not in combined
    assert "ai.codex.marketnews.health" not in combined


def test_codex_review_launcher_runs_hourly_review_line() -> None:
    launcher = ROOT / "scripts" / "market_news_codex_review_auto.command"
    stopper = ROOT / "scripts" / "market_news_codex_review_auto_stop.command"

    launcher_text = launcher.read_text(encoding="utf-8")
    stopper_text = stopper.read_text(encoding="utf-8")

    assert "news-learning-codex-review --notify" in launcher_text
    assert "ai.codex.marketnews.codexreview" in launcher_text
    assert "<integer>3600</integer>" in launcher_text
    assert "news_learning_codex_analysis.md" in launcher_text
    assert "news_learning_codex_review_status.json" in launcher_text
    assert "news-learning-codex-review" in stopper_text

    combined = launcher_text + stopper_text
    assert "market_news collect" not in combined
    assert "market_news health" not in combined
    assert "ai.codex.marketnews.collect" not in combined
    assert "ai.codex.marketnews.health" not in combined


def test_thread_review_launcher_resumes_news_collector_thread() -> None:
    launcher = ROOT / "scripts" / "market_news_thread_review_auto.command"
    stopper = ROOT / "scripts" / "market_news_thread_review_auto_stop.command"

    launcher_text = launcher.read_text(encoding="utf-8")
    stopper_text = stopper.read_text(encoding="utf-8")

    assert "ai.codex.marketnews.threadreview" in launcher_text
    assert "exec resume" in launcher_text
    assert "019ce1c2-34f3-7e60-b324-bf7422ef1506" in launcher_text
    assert "news-learning-auto --no-copy" in launcher_text
    assert "news_learning_thread_review_status.json" in launcher_text
    assert "news_learning_thread_review_runner.sh" in stopper_text

    combined = launcher_text + stopper_text
    assert "message send" not in combined
    assert "--notify" not in combined
