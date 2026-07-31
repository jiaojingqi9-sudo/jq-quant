"""独立版入口：只跑一个功能。

用法（Streamlit 不接受自定义命令行参数，所以用环境变量指定要跑哪个功能）：

    JQ_FEATURE=news streamlit run src/taa_futu/standalone.py
    JQ_FEATURE=crypto streamlit run src/taa_futu/standalone.py

不设 JQ_FEATURE 时列出所有可用功能，方便查看。

和统一版共用同一份 ``Feature.render``，所以两种形态的行为一致。
"""

from __future__ import annotations

import os

import streamlit as st

from taa_futu.shell import list_features, run_standalone


def main() -> None:
    feature_id = (os.environ.get("JQ_FEATURE") or "").strip()
    features = list_features()

    if not feature_id:
        st.set_page_config(page_title="JQ Quant", layout="wide")
        st.title("JQ Quant · 独立模式")
        st.caption("用环境变量 JQ_FEATURE 指定要单独运行的功能。")
        st.markdown("可用功能：")
        for f in features:
            status = f.availability()
            mark = "✅" if status.ok else "⚠️"
            st.markdown(f"- {mark} `{f.id}` — {f.icon} {f.label}")
            if not status.ok:
                st.caption(f"　　{status.detail}")
        st.code("JQ_FEATURE=news streamlit run src/taa_futu/standalone.py", language="bash")
        return

    match = next((f for f in features if f.id == feature_id), None)
    title = match.title() if match else f"JQ Quant · {feature_id}"
    st.set_page_config(page_title=title, layout="wide", initial_sidebar_state="collapsed")

    settings = None
    if match is not None and match.needs_settings:
        try:
            from taa_futu.config import load_settings
            settings = load_settings()
        except Exception as exc:
            st.warning(f"配置加载失败，功能可能受限：{exc}")

    run_standalone(feature_id, settings)


if __name__ == "__main__":
    main()
