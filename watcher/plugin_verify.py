#!/usr/bin/env python3
"""plugin_verify - 验证插件架构：发现、注册、外壳分发、两种形态。

用假的 streamlit 顶替真库，不启动 UI 就能验证。

**这个假库的能力边界要说清楚**：它能可靠验证「发现、注册、可用性检查、外壳
分发、错误隔离、首页组装」——这些只依赖控制流。但它无法模拟真实控件的返回
值（number_input 该返回数字、date_input 该返回日期），所以重度依赖控件输入的
页面（股票、加密、历史模拟）在这里报错属于假阳性，它们必须在真实 Streamlit
里验证。因此本脚本的结论只对「插件骨架」有效，页面本身的正确性要靠实际启动。
"""
import json
import sys
import types
from pathlib import Path

TRADE = Path.home() / "All here" / "trade"
sys.path.insert(0, str(TRADE / "src"))

calls = []


class Ctx:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def rec(name):
    def fn(*a, **k):
        txt = a[0][:60] if a and isinstance(a[0], str) else ""
        calls.append((name, txt))
        return Ctx()
    return fn


class Col(Ctx):
    def text_input(self, *a, **k): return ""
    def multiselect(self, *a, **k): return []
    def selectbox(self, *a, **k): return None
    def button(self, *a, **k): return False
    def toggle(self, *a, **k): return False
    def checkbox(self, *a, **k): return False
    def __getattr__(self, n): return rec(f"col.{n}")


class Sidebar:
    def __getattr__(self, n):
        if n == "button":
            return lambda *a, **k: False
        return rec(f"sidebar.{n}")


class AutoModule(types.ModuleType):
    """缺什么属性就自动补一个记录器。

    手工枚举 streamlit 的 API 是测不完的——漏一个就报「module has no attribute」，
    看起来像被测代码有问题，其实是假库不全。自动补全后，失败才真的代表代码有问题。
    """

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        fn = rec(f"auto.{name}")
        setattr(self, name, fn)
        return fn


def fake_streamlit():
    st = AutoModule("streamlit")
    for n in ("subheader", "warning", "error", "info", "success", "caption",
              "markdown", "divider", "metric", "json", "write", "title",
              "code", "set_page_config", "rerun", "dataframe", "plotly_chart",
              "line_chart", "bar_chart", "table", "image", "text", "header"):
        setattr(st, n, rec(n))
    st.columns = lambda spec, **k: [Col() for _ in (range(spec) if isinstance(spec, int) else spec)]
    st.tabs = lambda labels, **k: [Ctx() for _ in labels]
    st.expander = lambda *a, **k: Ctx()
    st.container = lambda *a, **k: Ctx()
    st.form = lambda *a, **k: Ctx()
    st.spinner = lambda *a, **k: Ctx()
    st.empty = lambda *a, **k: Ctx()
    st.text_input = lambda *a, **k: ""
    st.text_area = lambda *a, **k: ""
    st.number_input = lambda *a, **k: 0
    st.multiselect = lambda *a, **k: []
    st.selectbox = lambda *a, **k: None
    st.radio = lambda *a, **k: None
    st.button = lambda *a, **k: False
    st.toggle = lambda *a, **k: False
    st.checkbox = lambda *a, **k: False
    st.slider = lambda *a, **k: 0
    st.session_state = {}
    st.sidebar = Sidebar()
    st.cache_data = lambda *a, **k: (lambda f: f)
    st.cache_resource = lambda *a, **k: (lambda f: f)
    return st


def fake_components():
    m = types.ModuleType("streamlit.components.v1")
    m.html = lambda src, **k: calls.append(("components.html", f"len={len(src)}"))
    m.iframe = lambda *a, **k: calls.append(("components.iframe", ""))
    return m


def main():
    out = {"kind": "plugin_verify"}
    st = fake_streamlit()
    comp = fake_components()
    st.components = types.SimpleNamespace(v1=comp)
    sys.modules["streamlit"] = st
    sys.modules["streamlit.components"] = types.ModuleType("streamlit.components")
    sys.modules["streamlit.components.v1"] = comp

    # 1. 发现
    try:
        from taa_futu.plugin import registry
        from taa_futu.shell import list_features, run_standalone, render_home
        feats = list_features()
        out["discovered"] = [
            {"id": f.id, "label": f.label, "order": f.order,
             "has_home_block": f.home_block is not None,
             "has_check": f.check is not None}
            for f in feats
        ]
        out["discovery_errors"] = registry.errors
    except Exception as exc:
        import traceback
        out["discover_error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc()[-800:]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    # 2. 可用性
    out["availability"] = {}
    for f in feats:
        s = f.availability()
        out["availability"][f.id] = {"ok": s.ok, "detail": s.detail[:90]}

    # 3. 逐个渲染（独立版路径），用真实配置——多数功能需要 settings，
    #    传 None 测出来的会是假失败。
    settings = None
    try:
        from taa_futu.config import load_settings
        settings = load_settings()
        out["settings_loaded"] = True
    except Exception as exc:
        out["settings_loaded"] = False
        out["settings_error"] = f"{type(exc).__name__}: {exc}"[:150]

    out["standalone_render"] = {}
    for f in feats:
        calls.clear()
        try:
            run_standalone(f.id, settings if f.needs_settings else None)
            errs = [t for n, t in calls if n == "error"]
            out["standalone_render"][f.id] = {
                "calls": len(calls),
                "errors": errs[:2],
                "ok": not errs and len(calls) > 0,
            }
        except Exception as exc:
            out["standalone_render"][f.id] = {"exception": f"{type(exc).__name__}: {exc}"[:130]}

    # 4. 首页（统一版路径）
    calls.clear()
    try:
        render_home(settings)
        errs = [t for n, t in calls if n == "error"]
        out["home"] = {"calls": len(calls), "errors": errs[:2], "ok": not errs}
    except Exception as exc:
        import traceback
        out["home"] = {"exception": f"{type(exc).__name__}: {exc}",
                       "tb": traceback.format_exc()[-400:]}

    # 5. 未知功能应被优雅处理
    calls.clear()
    run_standalone("does_not_exist", None)
    out["unknown_feature_handled"] = any(n == "error" for n, _ in calls)

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
