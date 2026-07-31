"""Tests for the Crypto / Screener / Live-Signal Streamlit pages.

We can't drive Streamlit's render loop in headless CI, so we test the
non-render helpers — those are the parts a regression would silently break.
The page renderers themselves are smoke-checked by importing the module
(catches syntax / top-level import errors).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest


"""这三个测试原本盯的是 dashboard_extras 里的 EXTRA_PAGE_OPTIONS /
PAGE_RENDERERS / maybe_render —— 插件架构之前那套手写分发。它已被注册表取代且
无任何调用方，2026-07-31 随死代码一起删除。

测试没有跟着删，而是改盯注册表：它们要防的事情没变（页面被漏注册、渲染函数
签名不对、未知页面把外壳搞崩），只是"页面清单"的真实来源换了地方。
"""


def test_registry_exposes_the_three_extra_pages() -> None:
    from taa_futu.plugin import registry
    from taa_futu.shell import _ensure_discovered

    _ensure_discovered()
    ids = {f.id for f in registry.all()}
    # dashboard_extras 负责渲染的三个页面必须都在注册表里
    for expected in ("crypto", "screener", "live_signal"):
        assert expected in ids, f"{expected} 没有注册，界面上会进不去"


def test_render_unknown_feature_does_not_crash() -> None:
    """未知页面 id 不能把外壳搞崩。

    以前由 maybe_render 返回 False 让宿主回退，现在由注册表自己兜住。
    """
    from taa_futu.plugin import registry

    assert registry.get("某个不存在的页面") is None
    assert registry.get("") is None


def test_read_json_missing_returns_none(tmp_path: Path) -> None:
    from taa_futu.dashboard_extras import _read_json

    assert _read_json(tmp_path / "missing.json") is None


def test_read_json_invalid_returns_none(tmp_path: Path) -> None:
    from taa_futu.dashboard_extras import _read_json

    p = tmp_path / "broken.json"
    p.write_text("{this is not json", encoding="utf-8")
    assert _read_json(p) is None


def test_read_json_valid(tmp_path: Path) -> None:
    from taa_futu.dashboard_extras import _read_json

    p = tmp_path / "status.json"
    p.write_text(json.dumps({"action": "polling", "n": 7}), encoding="utf-8")
    data = _read_json(p)
    assert data == {"action": "polling", "n": 7}


def test_age_str_buckets() -> None:
    from taa_futu.dashboard_extras import _age_str

    now = datetime.now(timezone.utc)
    assert _age_str(None) == "—"
    assert "s" in _age_str(now - timedelta(seconds=10))
    assert "min" in _age_str(now - timedelta(minutes=10))
    assert "h" in _age_str(now - timedelta(hours=2))


def test_parse_ts_handles_naive_and_zulu() -> None:
    from taa_futu.dashboard_extras import _parse_ts

    a = _parse_ts("2026-05-27T12:00:00")
    b = _parse_ts("2026-05-27T12:00:00Z")
    c = _parse_ts("2026-05-27T12:00:00+00:00")
    assert a is not None and b is not None and c is not None
    # All three should normalise to UTC and represent the same moment
    assert a == b == c
    # Bad input returns None, never raises
    assert _parse_ts("not-a-date") is None
    assert _parse_ts(None) is None
    assert _parse_ts("") is None


def test_tail_jsonl_missing_returns_empty(tmp_path: Path) -> None:
    from taa_futu.dashboard_extras import _tail_jsonl

    assert _tail_jsonl(tmp_path / "absent.jsonl", n=5) == []


def test_tail_jsonl_returns_last_n_in_order(tmp_path: Path) -> None:
    from taa_futu.dashboard_extras import _tail_jsonl

    p = tmp_path / "events.jsonl"
    lines = [json.dumps({"i": i}) for i in range(100)]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    tail = _tail_jsonl(p, n=5)
    assert len(tail) == 5
    assert [row["i"] for row in tail] == [95, 96, 97, 98, 99]


def test_tail_jsonl_skips_malformed_lines(tmp_path: Path) -> None:
    from taa_futu.dashboard_extras import _tail_jsonl

    p = tmp_path / "events.jsonl"
    p.write_text(
        json.dumps({"i": 1}) + "\n"
        "broken line that's not json\n"
        + json.dumps({"i": 3}) + "\n",
        encoding="utf-8",
    )
    rows = _tail_jsonl(p, n=10)
    assert [r["i"] for r in rows] == [1, 3]


def test_state_badge_has_known_states() -> None:
    from taa_futu.dashboard_extras import _state_badge

    assert "OK" in _state_badge("ok")
    assert "FAIL" in _state_badge("fail")
    assert "WARN" in _state_badge("warn")
    assert "IDLE" in _state_badge("idle")
    # Unknown state must not crash; returns a placeholder
    assert _state_badge("xxx") != ""


def test_registered_renderers_callable() -> None:
    """每个注册进来的功能，render 必须是可调用且能接 settings。

    没有 Streamlit 上下文就没法真调，但查签名足以拦住「注册了个 None」
    或「render 少个参数」这类会在点进页面那一刻才炸的问题。
    """
    import inspect
    from taa_futu.plugin import registry
    from taa_futu.shell import _ensure_discovered

    _ensure_discovered()
    features = registry.all()
    assert features, "注册表是空的，首页会没有任何入口"
    for feat in features:
        assert callable(feat.render), f"{feat.id} 的 render 不可调用"
        params = list(inspect.signature(feat.render).parameters.values())
        assert len(params) >= 1, f"{feat.id} 的 render 应该接一个 settings 参数"
