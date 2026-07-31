"""JQ Quant 外壳：导航、首页、以及两种运行形态。

统一版 (``run_unified``)：所有功能装进一个窗口，侧边栏切换。
独立版 (``run_standalone``)：只跑一个功能，没有导航。

两者调用的是同一个 ``Feature.render``，所以不会出现「统一版好使、独立版行为
不一样」这种两套实现互相偏离的问题。

导航与首页卡片都是从注册表生成的，不存在需要手工维护的功能清单——加一个功能
只要往 ``taa_futu/features/`` 丢一个文件。
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from taa_futu.plugin import Feature, registry


VIEW_KEY = "view"
HOME = "home"


def _ensure_discovered() -> None:
    registry.discover()


def current_view() -> str:
    """当前页面。

    这里刻意不用 ``st.session_state.get(...)``：Streamlit 的 AppTest 用
    SafeSessionState 顶替 session_state，它没有 ``.get`` 方法，属性访问会被
    当成键查找并抛 AttributeError——脚本直接中断，测试表现为「运行超时」而不是
    报错，极难定位。``in`` 加下标是两种运行时都支持的写法。
    """
    if VIEW_KEY not in st.session_state:
        return HOME
    return st.session_state[VIEW_KEY]


def go_to(view: str) -> None:
    st.session_state[VIEW_KEY] = view
    st.rerun()


# ── 首页 ──────────────────────────────────────────────────────────────────────

def render_home(settings: Any = None) -> None:
    _ensure_discovered()
    st.markdown("## 🏠 JQ Quant 总控")
    st.caption("选择一个子系统进入完整功能。")

    try:
        from taa_futu.dashboard_extras import render_top_status_bar
        render_top_status_bar()
    except Exception as exc:
        st.caption(f"状态栏暂不可用：{exc}")

    st.divider()

    features = [f for f in registry.all() if f.id != HOME]
    cards = [f for f in features if f.placement == "card"]
    quick = [f for f in features if f.placement == "quick"]
    with_blocks = [f for f in features if f.home_block is not None]

    # 主力子系统：大卡片，三列一排
    for row_start in range(0, len(cards), 3):
        cols = st.columns(3, gap="large")
        for col, feat in zip(cols, cards[row_start:row_start + 3]):
            with col:
                st.markdown(f"### {feat.icon} {feat.short_label()}")
                if feat.summary:
                    st.markdown(feat.summary)
                status = feat.availability()
                if not status.ok:
                    st.caption(f"⚠️ {status.detail}")
                if st.button(
                    f"进入{feat.short_label()} →",
                    key=feat.button_key(),
                    use_container_width=True,
                    type="primary",
                    disabled=not status.ok,
                ):
                    go_to(feat.id)

    # 有内容区块的功能各占一整行——它们展示的是内容本身（例如当日告警），
    # 不只是一个入口。
    for feat in with_blocks:
        st.divider()
        status = feat.availability()
        if not status.ok:
            st.markdown(f"### {feat.icon} {feat.short_label()}")
            st.info(status.detail)
            continue
        try:
            feat.home_block()
        except Exception as exc:  # 首页区块出错不该影响整个首页
            st.caption(f"{feat.label} 区块渲染失败：{exc}")

    # 辅助功能与工具动作：底部快捷链接
    actions = _extra_quick_actions()
    if quick or actions:
        st.divider()
        st.markdown("##### 快速链接 / Quick Links")
        cols = st.columns(max(1, len(quick) + len(actions)))
        idx = 0
        for feat in quick:
            if cols[idx].button(f"{feat.icon} {feat.short_label()}",
                                use_container_width=True, key=feat.button_key()):
                go_to(feat.id)
            idx += 1
        for label, key, fn in actions:
            if cols[idx].button(label, use_container_width=True, key=key):
                fn()
            idx += 1


# 宿主可以往首页快捷区注册「不是功能」的工具动作（启动外部程序、跑体检等）。
_QUICK_ACTIONS: list[tuple[str, str, Any]] = []


def register_quick_action(label: str, key: str, fn) -> None:
    if any(k == key for _, k, _ in _QUICK_ACTIONS):
        return
    _QUICK_ACTIONS.append((label, key, fn))


def _extra_quick_actions():
    return list(_QUICK_ACTIONS)

    errors = registry.errors
    if errors:
        st.divider()
        with st.expander(f"⚠️ {len(errors)} 个功能模块加载失败"):
            for name, err in errors:
                st.caption(f"`{name}`：{err}")


# ── 侧边栏 ────────────────────────────────────────────────────────────────────

def render_sidebar() -> None:
    _ensure_discovered()
    st.sidebar.markdown("### 页面 / View")
    view = current_view()

    if st.sidebar.button(
        ("● " if view == HOME else "○ ") + "🏠 首页 / Home",
        key="sidebar_btn_home", use_container_width=True,
        type="primary" if view == HOME else "secondary",
        disabled=view == HOME,
    ):
        go_to(HOME)

    for feat in registry.all():
        is_current = feat.id == view
        status = feat.availability()
        label = f"{feat.icon} {feat.label}"
        if st.sidebar.button(
            ("● " if is_current else "○ ") + label,
            key=f"sidebar_btn_{feat.id}",
            use_container_width=True,
            type="primary" if is_current else "secondary",
            disabled=is_current,
            help=None if status.ok else status.detail,
        ):
            go_to(feat.id)


# ── 两种形态 ──────────────────────────────────────────────────────────────────

def run_unified(settings: Any = None, *, render_header=None) -> None:
    """统一版：一个窗口装下所有功能。"""
    _ensure_discovered()

    if VIEW_KEY not in st.session_state:
        st.session_state[VIEW_KEY] = HOME
    view = current_view()

    # 新闻工作台自带完整页头，外壳再画一层会重复，所以把页头交给各功能决定。
    feat = registry.get(view)
    suppress_header = bool(feat and feat.meta.get("full_bleed"))
    if render_header is not None and not suppress_header:
        render_header()

    render_sidebar()

    if view == HOME:
        render_home(settings)
        return
    if not registry.render(view, settings):
        st.error(f"未知页面：{view}")
        if st.button("回到首页"):
            go_to(HOME)


def run_standalone(feature_id: str, settings: Any = None) -> None:
    """独立版：只跑一个功能，不显示导航。"""
    _ensure_discovered()
    feat = registry.get(feature_id)
    if feat is None:
        st.error(
            f"没有这个功能：{feature_id}\n\n"
            f"可用的有：{', '.join(registry.ids()) or '（无）'}"
        )
        return
    status = feat.availability()
    if not status.ok:
        st.warning(f"{feat.label} 当前不可用：{status.detail}")
        return
    registry.render(feature_id, settings)


def list_features() -> list[Feature]:
    _ensure_discovered()
    return registry.all()
