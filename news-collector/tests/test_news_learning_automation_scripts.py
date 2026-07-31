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


def test_codex_launchers_are_gone() -> None:
    """四个 Codex 审阅启动器必须不存在。

    这两个测试原来断言它们的内容（调用 codex 二进制、resume 某个会话 id 等）。
    Codex 订阅 2026-06 到期，模型层已换成本机 Claude，那些脚本只会反复失败，
    2026-07-31 随启动器整理一起移走。

    测试没有删掉而是反转：断言它们确实不在了。否则下次有人从回收站捞回来、
    或者照着旧文档重新造一个，不会有任何东西拦住。
    """
    for name in (
        "market_news_codex_review_auto.command",
        "market_news_codex_review_auto_stop.command",
        "market_news_thread_review_auto.command",
        "market_news_thread_review_auto_stop.command",
    ):
        assert not (ROOT / "scripts" / name).exists(), (
            f"{name} 又出现了。Codex 已停用，模型层走本机 Claude；"
            "需要自动审阅请另建走 Claude 的脚本，不要恢复这个。"
        )


def test_no_launcher_invokes_codex_binary() -> None:
    """剩下的启动器里不能再有人调 Codex。"""
    offenders = []
    for path in sorted((ROOT / "scripts").glob("*.command")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "Codex.app" in text or "codex_bin" in text:
            offenders.append(path.name)
    assert not offenders, f"这些脚本仍在调用 Codex：{offenders}"
