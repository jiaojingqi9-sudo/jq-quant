#!/usr/bin/env python3
"""newspanel_verify - 验证交易 app 里的新闻页能正常加载与渲染。

不启动 Streamlit UI，而是：
  1. 用假的 streamlit 模块顶替，捕获所有渲染调用
  2. 真实读取 news collector 的报告
  3. 跑一遍 render_news，确认不抛异常、且确实渲染出了内容
"""
import json
import sys
import types
from pathlib import Path

TRADE = Path.home() / "All here" / "trade"
sys.path.insert(0, str(TRADE / "src"))

calls = []


class _Ctx:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _rec(name):
    def fn(*args, **kwargs):
        text = ""
        if args and isinstance(args[0], str):
            text = args[0][:70]
        calls.append((name, text))
        return _Ctx()
    return fn


class FakeCol(_Ctx):
    """列对象：输入类控件要返回和真 streamlit 一致的类型，否则测出来的是假错误。"""

    def text_input(self, *a, **k): calls.append(("col.text_input", "")); return ""
    def multiselect(self, *a, **k): calls.append(("col.multiselect", "")); return []
    def selectbox(self, *a, **k): calls.append(("col.selectbox", "")); return None
    def button(self, *a, **k): calls.append(("col.button", "")); return False
    def checkbox(self, *a, **k): calls.append(("col.checkbox", "")); return False

    def __getattr__(self, item): return _rec(f"col.{item}")


def make_fake_streamlit():
    st = types.ModuleType("streamlit")
    for name in ("subheader", "warning", "error", "info", "success", "caption",
                 "markdown", "divider", "metric", "json", "write", "title"):
        setattr(st, name, _rec(name))
    st.columns = lambda spec, **k: [FakeCol() for _ in (range(spec) if isinstance(spec, int) else spec)]
    st.tabs = lambda labels, **k: [_Ctx() for _ in labels]
    st.expander = lambda *a, **k: _Ctx()
    st.container = lambda *a, **k: _Ctx()
    st.text_input = lambda *a, **k: ""
    st.multiselect = lambda *a, **k: []
    st.button = lambda *a, **k: False
    st.toggle = lambda *a, **k: False
    st.rerun = _rec("rerun")
    st.session_state = {}
    st.spinner = lambda *a, **k: _Ctx()
    return st


def make_fake_components():
    """假的 streamlit.components.v1，用来确认原版看板真的被嵌入、且体量正确。"""
    mod = types.ModuleType("streamlit.components.v1")

    def html(src, **kwargs):
        calls.append(("components.html", f"len={len(src)} height={kwargs.get('height')}"))
    mod.html = html
    mod.iframe = lambda *a, **k: calls.append(("components.iframe", ""))
    return mod


def main():
    out = {"kind": "newspanel_verify"}
    fake_st = make_fake_streamlit()
    fake_components = make_fake_components()
    fake_st.components = types.SimpleNamespace(v1=fake_components)
    sys.modules["streamlit"] = fake_st
    sys.modules["streamlit.components"] = types.ModuleType("streamlit.components")
    sys.modules["streamlit.components.v1"] = fake_components

    # 1. 模块能否导入
    try:
        from taa_futu.news_panel import render_news, news_root
        out["import_ok"] = True
        out["news_root"] = str(news_root())
        out["report_exists"] = (news_root() / "reports" / "live" / "latest_report.json").exists()
    except Exception as exc:
        out["import_ok"] = False
        out["import_error"] = f"{type(exc).__name__}: {exc}"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    # 2. 渲染完整页
    try:
        render_news(None)
        out["render_ok"] = True
    except Exception as exc:
        import traceback
        out["render_ok"] = False
        out["render_error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc()[-900:]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    # 2b. 渲染首页新闻块
    try:
        from taa_futu.news_panel import render_news_home_block, news_status
        render_news_home_block()
        out["home_block_ok"] = True
        s = news_status()
        out["status_summary"] = {
            k: s.get(k) for k in ("ok", "age_text", "events", "screened", "backends",
                                  "delivery_status", "instruments")
        }
        out["urgent_alerts"] = len(s.get("urgent") or [])
        out["total_alerts"] = len(s.get("alerts") or [])
    except Exception as exc:
        import traceback
        out["home_block_ok"] = False
        out["home_block_error"] = f"{type(exc).__name__}: {exc}"
        out["home_traceback"] = traceback.format_exc()[-600:]

    # 2c. 确认嵌入的是原版看板（功能才不会丢）
    embedded = [t for n, t in calls if n == "components.html"]
    out["board_embedded"] = any("html" in n for n, _ in calls) or bool(embedded)

    out["render_calls"] = len(calls)
    out["sample_output"] = [f"{n}: {t}" for n, t in calls[:14] if t]

    # 3. 导航是否注册
    try:
        from taa_futu.dashboard_extras import SIDEBAR_OPTIONS, VIEW_NEWS
        out["sidebar_has_news"] = any(k == VIEW_NEWS for _, k in SIDEBAR_OPTIONS)
        out["sidebar_options"] = [lbl for lbl, _ in SIDEBAR_OPTIONS]
    except Exception as exc:
        out["nav_error"] = str(exc)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
