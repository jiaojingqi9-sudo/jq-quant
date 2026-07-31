"""News workspace for the JQ Quant terminal.

Design note — why this embeds instead of re-implements:

The collector already generates a full interactive board
(``reports/live/latest_dashboard.html``): click an alert to jump to its event,
click an instrument to jump back to the driving event, drag a card into the
"ask the AI" box, search, direction filters, workspace switching. Rebuilding
that in Streamlit lost most of it — Streamlit has no cross-linking and no drag
and drop. So the board is embedded as-is and keeps every feature, while this
module adds a native status strip above it.

The board talks to the review API on 127.0.0.1:8765, which serves
``Access-Control-Allow-Origin: *``, so the ask-the-AI panel keeps working from
inside the embedded frame.

Coupling stays one-way and file-based: read the artifacts the collector writes,
never import or call its code, and degrade to an explanation when it is absent.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

import streamlit as st
import streamlit.components.v1 as components


DEFAULT_BOARD_HEIGHT = 1500
REVIEW_API_BASE = "http://127.0.0.1:8765"


# ── locating the collector ────────────────────────────────────────────────────

def news_root() -> Path:
    """Where the news collector lives. Env var wins so the tree can move."""
    override = (os.environ.get("JQ_NEWS_ROOT") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "All here" / "news collector"


def _live_dir() -> Path:
    return news_root() / "reports" / "live"


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _age_text(iso: str) -> tuple[str, int | None]:
    try:
        dt = datetime.fromisoformat(str(iso))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return "时间未知", None
    if secs < 90:
        return "刚刚", secs
    if secs < 3600:
        return f"{secs // 60} 分钟前", secs
    if secs < 86400:
        return f"{secs // 3600} 小时前", secs
    return f"{secs // 86400} 天前", secs


# ── shared status logic ───────────────────────────────────────────────────────

def news_status() -> dict:
    """Collector health in one dict. Used by both this page and the home block."""
    report = _load(_live_dir() / "latest_report.json")
    if not isinstance(report, dict):
        return {"ok": False, "reason": "报告不可读"}

    age_txt, age_secs = _age_text(str(report.get("created_at", "")))
    counts = report.get("counts") or {}
    alert_counts = report.get("alert_counts") or {}

    backends: dict[str, int] = {}
    screened = 0
    for key in ("top_events", "negative_risks", "positive_catalysts", "watchlist"):
        for ev in report.get(key, []) or []:
            if not isinstance(ev, dict):
                continue
            mj = ev.get("model_judgement") or {}
            if str(mj.get("screening_status", "")).lower() == "used":
                screened += 1
            b = mj.get("_model_backend")
            if b:
                backends[str(b)] = backends.get(str(b), 0) + 1

    delivery = _load(_live_dir() / "delivery_status.json") or {}
    note = delivery.get("notification") or {}

    alerts = [a for a in (report.get("alerts") or []) if isinstance(a, dict)]
    urgent = [a for a in alerts if str(a.get("level")) in ("critical", "high")]

    return {
        "ok": True,
        "age_text": age_txt,
        "age_seconds": age_secs,
        "events": counts.get("ranked_events"),
        "instruments": counts.get("ranked_instruments"),
        "alerts": alerts,
        "urgent": urgent,
        "alert_counts": alert_counts,
        "screened": screened,
        "backends": backends,
        "delivery_status": str(note.get("status", "")),
        "delivery_count": note.get("alert_count", 0),
        "report": report,
    }


def _render_status_strip(status: dict) -> None:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("更新", status["age_text"])
    c2.metric("事件", status.get("events", "-"))
    c3.metric("告警", len(status.get("alerts") or []))
    c4.metric("AI 已判定", status.get("screened", 0))
    c5.metric("标的", status.get("instruments", "-"))

    age = status.get("age_seconds")
    if age is not None and age > 3600:
        st.warning(f"新闻数据已 {status['age_text']}未更新——采集线可能停了。")

    bits = []
    backends = status.get("backends") or {}
    if backends:
        bits.append("AI 筛选：" + "、".join(f"{k} ({v})" for k, v in backends.items()))
    elif status.get("screened", 0) == 0:
        bits.append("⚠️ 本轮无 AI 判定，推送退到规则降级模式")
    if status.get("delivery_status") == "sent" and status.get("delivery_count"):
        bits.append(f"已推送 {status['delivery_count']} 条到手机")
    elif status.get("delivery_status") == "skipped":
        bits.append("无新的高优先级提醒可发")
    if bits:
        st.caption("　｜　".join(bits))


# ── home-page block ───────────────────────────────────────────────────────────

def render_news_home_block() -> None:
    """Compact news block for the terminal home page."""
    st.markdown("### 📰 市场新闻 / Market News")

    status = news_status()
    if not status.get("ok"):
        st.info("新闻收集器暂无数据。")
        return

    _render_status_strip(status)

    urgent = status.get("urgent") or []
    alerts = status.get("alerts") or []
    show = urgent or alerts

    if not show:
        st.caption("本轮没有告警。只有达到级别门槛的新闻才会成为告警。")
    else:
        label = "高优先告警" if urgent else "本轮告警"
        st.markdown(f"**{label}**")
        for a in show[:5]:
            emoji = {"critical": "🚨", "high": "⚠️", "medium": "📌"}.get(str(a.get("level")), "📌")
            arrow = {"positive": "📈", "negative": "📉"}.get(str(a.get("direction")), "➡️")
            syms = [str(s) for s in (a.get("symbols") or []) if str(s).strip()][:4]
            line = f"{emoji} {arrow} {str(a.get('headline',''))[:70]}"
            if syms:
                line += f"　`{' · '.join(syms)}`"
            st.markdown(line)

    # 用功能 id 而不是从导航模块 import 常量：本模块是插件，不该反过来依赖外壳，
    # 否则就成了循环依赖（外壳发现插件、插件又要 import 外壳）。
    if st.button("打开新闻工作台 →", key="home_open_news", use_container_width=True):
        st.session_state["view"] = "news"
        st.rerun()


# ── full page ─────────────────────────────────────────────────────────────────

def _collapse_page_chrome() -> None:
    """Give the embedded board the whole viewport.

    The board ships its own header, its own metric cards and its own controls.
    Stacking Streamlit's app header and a second status strip on top pushed the
    actual news ~600px down the page and printed the same numbers twice. On this
    one view the surrounding chrome is trimmed so the board starts near the top.
    """

    st.markdown(
        """
        <style>
          /* trim the default top padding of the main block on this view */
          section.main > div.block-container {
              padding-top: 1rem;
              padding-bottom: 0rem;
              max-width: 100%;
          }
          /* the host page prints a title + captions above every view; on the
             news view they duplicate the board's own header */
          div[data-testid="stAppViewContainer"] .jq-news-hide { display: none; }
          /* let the iframe span the full content width */
          iframe[title="streamlit_component"] { width: 100% !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_news(settings=None) -> None:
    board_path = _live_dir() / "latest_dashboard.html"
    report_path = _live_dir() / "latest_report.json"

    if not report_path.exists():
        st.warning(
            f"没有找到新闻数据。\n\n预期位置：`{report_path}`\n\n"
            "如果新闻收集器装在别处，设置环境变量 `JQ_NEWS_ROOT` 指向它。"
        )
        return

    if not board_path.exists():
        st.error(
            "交互看板文件不存在。\n\n"
            f"预期位置：`{board_path}`\n\n"
            "它由采集线每轮自动生成，跑一次 `python3 -m market_news collect` 就会出现。"
        )
        return

    _collapse_page_chrome()

    # One thin line only — everything else the board already shows itself.
    status = news_status()
    bar = st.columns([6, 1, 1])
    if status.get("ok"):
        bits = [f"更新于 {status['age_text']}"]
        if status.get("screened"):
            bits.append(f"AI 已判定 {status['screened']} 条")
        if status.get("delivery_status") == "sent" and status.get("delivery_count"):
            bits.append(f"已推送 {status['delivery_count']} 条到手机")
        bar[0].caption("　·　".join(bits))
        age = status.get("age_seconds")
        if age is not None and age > 3600:
            bar[0].warning(f"数据已 {status['age_text']}未更新，采集线可能停了。")
    tall = bar[1].toggle("加高", key="news_tall", value=False, help="2400px，适合大屏")
    if bar[2].button("刷新", key="news_reload"):
        st.rerun()

    try:
        html = board_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        st.error(f"看板读取失败：{exc}")
        return

    if REVIEW_API_BASE not in html:
        st.caption("提示：看板里没有找到 review API 地址，问 AI 功能可能不可用。")

    board_height = 2400 if tall else DEFAULT_BOARD_HEIGHT
    html = _blend_into_app(html, board_height)
    components.html(html, height=board_height, scrolling=True)


# 嵌入时注入的样式。只在 app 里生效，独立打开看板时不受影响——
# 那种场景下它就该是一张完整的网页，有自己的背景和大标题。
_EMBED_CSS = """
<style data-jq-embed="1">
  /* 让看板融进 app，而不是「页面里套一张页面」。
     配色令牌两边本来就是同一套（ink / muted / 阴影 / 圆角 / 字体），
     所以剩下的全是接缝问题：

     1) 自带的渐变页面背景，在 app 的浅底上会显出一块明显的矩形
     2) body 的外边距，让内容离 app 的容器边缘忽宽忽窄
     3) 顶部那张 hero 大标题（MARKET NEWS BOARD / 可点击市场消息控制台），
        它是给独立页面用的门面，嵌进来就成了第二个页头
     4) 内容宽度上限居中，两侧留白与 app 的栅格对不齐 */

  html, body {
    background: transparent !important;
    margin: 0 !important;
    padding: 0 !important;
  }
  body::before, body::after { display: none !important; }

  /* 独立页面的门面，嵌入时收掉 */
  section.hero { display: none !important; }

  /* 跟着 app 的容器走，不再自己限宽居中 */
  .layout, .workspace, main, .container, .page {
    max-width: none !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
    padding-left: 0 !important;
    padding-right: 0 !important;
  }

  /* 第一块内容不要再顶一段外边距，否则和上面的工具栏之间空一大截 */
  section.toolbar { margin-top: 0 !important; padding-top: 0 !important; }

  /* 三栏各自滚动是看板的布局模型，不要动。
     这里一度加过 `.column-scroll { overflow: visible }` 想消掉「双滚动条」，
     结果卡片被拦腰切断：.column-scroll 上有明确的
     height: clamp(760px, calc(100vh - 220px), 1200px)，只把 overflow 改成
     visible 而高度限制还在，超出的内容就溢出并被祖先裁掉，既不滚动也看不全。
     实际上并不存在双滚动条问题——列在内部滚，iframe 自己不需要再滚。 */
</style>
"""


# 看板顶部（工具栏 + 卡片间距）大约占这么高，剩下的才是三栏能用的高度。
# 数字来自看板 CSS 里 `calc(100vh - 220px)` 的那个 220。
_BOARD_CHROME_PX = 220


def _column_height_css(board_height: int) -> str:
    """让三栏的高度跟着 iframe 走。

    看板原本写死 `height: clamp(760px, calc(100vh - 220px), 1200px)`。嵌进
    iframe 之后 100vh 就是 iframe 的高度，但上限 1200px 会把它卡死——于是
    「加高」开关把 iframe 撑到 2400px，列还是 1200px，多出来的一千多像素
    全是空白。开关名义上说「2400px，适合大屏」，实际什么也没变。

    这里按 iframe 高度重算，去掉那个上限。下限保留 760px：再矮就一栏只剩
    两三张卡片，翻起来比滚动还累。
    """
    usable = max(760, board_height - _BOARD_CHROME_PX)
    return (f"  .column-scroll {{ height: {usable}px !important; "
            f"max-height: {usable}px !important; }}\n")


def _blend_into_app(html: str, board_height: int) -> str:
    """把嵌入用样式塞进看板 HTML 的 head。

    改这里而不是改生成器：生成器产出的那张页面还要独立打开、还要给推送用，
    它有自己的门面是对的。只有嵌进 app 这一种场景才需要去掉接缝。
    """
    if 'data-jq-embed="1"' in html:
        return html
    css = _EMBED_CSS.replace(
        "</style>", _column_height_css(board_height) + "</style>")
    if "</head>" in html:
        return html.replace("</head>", css + "</head>", 1)
    return css + html
