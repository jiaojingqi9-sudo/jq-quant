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


# 组件 iframe 的首帧高度。真实高度由看板自己按剩余视口算（见 _FIT_JS），
# 这个值只影响第一帧，别当成布局参数去调。
INITIAL_FRAME_PX = 900
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

def _page_chrome() -> None:
    """新闻页的外壳样式：收掉多余留白，并且把滚动条画出来。

    排版：三栏各自带一条常显滑块、按窗口高度定高，整页还有一条主滚动条。
    两层滚动是刻意的——三栏定高，新闻才始终停在一屏之内，把卡片拖到上面的
    「主动问AI」只要挪几百像素；而整页能滚，展开的面板就不必封顶、不必裁剪
    （封顶和手风琴那一版被否掉了，别再退回去）。

    选择器必须盯 data-testid：Streamlit 的 emotion 类名每版都变，旧版按
    ``section.main > div.block-container`` 写的那套在 1.55 上根本没命中，
    主容器上下 96/160px 的默认内边距一直还在。

    滚动条得自己画。macOS 的系统滚动条是覆盖式的，静止时完全不画；而
    Chromium 只要看到 ``scrollbar-width`` 或 ``scrollbar-color``，就改走标准
    滚动条、``::-webkit-scrollbar`` 那一整套直接失效。所以先把这两个属性还原
    成 auto，再用 ``::-webkit-scrollbar`` 画一条常显的。
    """

    st.markdown(
        """
        <style data-jq-news-chrome="1">
          /* Streamlit 自带顶栏在这一页收掉：它只剩「折叠侧栏」和 Deploy 菜单，
             而折叠侧栏 app 自己有按钮，是重复的。连同为它让出的内边距，
             这一条就是五十多像素。 */
          [data-testid="stHeader"] { display: none !important; }

          /* 主容器：贴着顶开始，底部不留 160px 的空白尾巴 */
          [data-testid="stMainBlockContainer"],
          section.main > div.block-container {
              padding-top: 0.75rem !important;
              padding-bottom: 1rem !important;
              max-width: 100% !important;
          }
          /* 纵向元素间距 16px → 6px */
          [data-testid="stMainBlockContainer"] [data-testid="stVerticalBlock"] {
              gap: 0.375rem !important;
          }
          /* 注入的 <style> 不占高度，但 Streamlit 仍给它一个元素容器，
             每个白吃一份 16px 行间距。选择器写细一点：只收「整块内容就是一个
             style 标签」的容器，别误伤正常的 markdown。 */
          [data-testid="stElementContainer"]:has(
              [data-testid="stMarkdownContainer"] > style:only-child) {
              display: none !important;
          }

          /* 页面滚动条常显。这是整页唯一的滚动条，看不见它就等于「滑不动」。 */
          [data-testid="stMain"] {
              scrollbar-width: auto !important;
              scrollbar-color: auto !important;
          }
          [data-testid="stMain"]::-webkit-scrollbar {
              width: 14px !important;
          }
          [data-testid="stMain"]::-webkit-scrollbar-track {
              background: rgba(24, 37, 52, 0.10) !important;
          }
          [data-testid="stMain"]::-webkit-scrollbar-thumb {
              background: rgba(24, 37, 52, 0.42) !important;
              border-radius: 8px !important;
              border: 3px solid transparent !important;
              background-clip: content-box !important;
          }
          [data-testid="stMain"]::-webkit-scrollbar-thumb:hover {
              background: rgba(24, 37, 52, 0.62) !important;
              background-clip: content-box !important;
          }

          /* 组件 iframe 铺满宽度。高度由看板自己量完写进宿主 head（见 _FIT_JS），
             这里只兜个底，免得脚本没跑起来时是个 0 高的空框。 */
          iframe[data-testid="stIFrame"],
          iframe[title="streamlit_component"] {
              width: 100% !important;
              min-height: 70vh !important;
              border: 0 !important;
              display: block;
          }
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

    _page_chrome()

    # 只留一行。别的看板自己都会显示，这里再画一遍就是两层页头。
    status = news_status()
    bar = st.columns([8, 1])
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
    if bar[1].button("刷新", key="news_reload"):
        st.rerun()

    try:
        html = board_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        st.error(f"看板读取失败：{exc}")
        return

    if REVIEW_API_BASE not in html:
        st.caption("提示：看板里没有找到 review API 地址，问 AI 功能可能不可用。")

    # scrolling 关掉：iframe 高度等于看板内容的真实高度，它自己不需要滚。
    # 留着的话就是页面里再套一个小窗口，正是「像嵌进去的」那种观感。
    components.html(_blend_into_app(html), height=INITIAL_FRAME_PX, scrolling=False)


# 嵌入时注入的样式。只在 app 里生效，独立打开看板时不受影响——
# 那种场景下它就该是一张完整的网页，有自己的背景和大标题。
_EMBED_CSS = """
<style data-jq-embed="1">
  /* 让看板融进 app，而不是「页面里套一张页面」。
     配色令牌两边本来就是同一套（ink / muted / 阴影 / 圆角 / 字体），
     剩下的全是接缝问题：

     1) 自带的渐变页面背景，在 app 的浅底上会显出一块明显的矩形
     2) body 的外边距，让内容离 app 的容器边缘忽宽忽窄
     3) 顶部那张 hero 大标题（MARKET NEWS BOARD / 可点击市场消息控制台），
        它是给独立页面用的门面，嵌进来就成了第二个页头
     4) 内容宽度上限居中，两侧留白与 app 的栅格对不齐 */

  html, body {
    background: transparent !important;
    margin: 0 !important;
    padding: 0 !important;
    height: auto !important;
    min-height: 0 !important;
    overflow: visible !important;
  }
  body::before, body::after { display: none !important; }

  /* 独立页面的门面，嵌入时收掉 */
  section.hero { display: none !important; }

  /* 跟着 app 的容器走，不再自己限宽居中。
     .shell 原本是 width: min(1440px, 100vw - 28px) + margin: 18px auto 32px，
     那是独立网页的排版；嵌进来就成了「里面还有一张卡在居中」。 */
  .shell {
    width: 100% !important;
    margin: 0 !important;
    padding: 0 !important;
    gap: 12px !important;
  }
  .layout, .workspace, main, .container, .page {
    max-width: none !important;
    margin-left: 0 !important;
    margin-right: 0 !important;
    padding-left: 0 !important;
    padding-right: 0 !important;
  }

  /* 第一块内容不要再顶一段外边距，否则和上面的工具栏之间空一大截 */
  section.toolbar { margin-top: 0 !important; padding-top: 0 !important; }

  /* ── 三栏各自带滑块 ────────────────────────────────────────────────
     每栏高度按窗口算（脚本里量，见 _FIT_JS），各自滚动、各自一条常显滑块。
     这样新闻始终停在一屏之内：把卡片拖到上面的「主动问AI」只要挪几百像素，
     不必跨过整页。下面这行是脚本没跑起来时的兜底。 */
  .column-scroll {
    height: calc(100vh - 190px);
    max-height: none !important;
    overflow-y: scroll !important;
    overflow-x: hidden !important;
    /* 栏滚到底之后把滚动交给整页，别在这里断掉 */
    overscroll-behavior: auto !important;
  }
  .layout { align-items: start !important; }

  /* 滑块必须画出来。macOS 的系统滚动条是覆盖式的，静止时完全不画；而
     Chromium 只要看到 `scrollbar-width` 或 `scrollbar-color`，就改走标准
     滚动条、`::-webkit-scrollbar` 整套失效——看板原本就在 .column-scroll 上
     写了 `scrollbar-width: thin`，正好踩中。先还原成 auto，再自己画。 */
  .column-scroll {
    scrollbar-width: auto !important;
    scrollbar-color: auto !important;
    scrollbar-gutter: stable !important;
  }
  .column-scroll::-webkit-scrollbar {
    width: 12px !important;
    display: block !important;
  }
  .column-scroll::-webkit-scrollbar-track {
    background: rgba(24, 37, 52, 0.11) !important;
    border-radius: 7px !important;
  }
  .column-scroll::-webkit-scrollbar-thumb {
    background: rgba(24, 37, 52, 0.55) !important;
    border-radius: 7px !important;
    border: 2px solid transparent !important;
    background-clip: content-box !important;
  }
  .column-scroll::-webkit-scrollbar-thumb:hover {
    background: rgba(24, 37, 52, 0.72) !important;
    background-clip: content-box !important;
  }

  /* ── 可折叠面板 ─────────────────────────────────────────────────────
     Runtime Status 635px + 主动问AI 388px + 工具栏 43px = 1066px，全压在
     新闻卡片上面。这两块不是每次都要看的东西，默认折成一行标题，点标题展开，
     功能一个没删。展开时不封顶——整页能滚，不必跟新闻抢高度。

     两条折叠栏并排放一行（.jq-foldbar，由脚本建）：上下摞着要 104px，
     并排只要 49px。展开的那条自动占满整行。 */
  .jq-foldbar {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: stretch;
  }
  .jq-foldbar > .panel { flex: 1 1 0; min-width: 0; }
  .jq-foldbar > .panel:not(.jq-folded) { flex-basis: 100%; }

  .jq-fold > .section-header {
    cursor: pointer;
    user-select: none;
    margin-bottom: 0 !important;
    align-items: center !important;
  }
  .jq-fold > .section-header h2::before {
    content: "▾";
    display: inline-block;
    width: 1em;
    margin-right: 2px;
    color: var(--muted);
    font-size: 0.72em;
    transform: translateY(-1px);
  }
  .jq-fold.jq-folded > .section-header h2::before { content: "▸"; }
  .jq-fold.jq-folded > *:not(.section-header) { display: none !important; }
  .jq-fold.jq-folded .panel-subtitle { display: none !important; }
  .jq-fold.jq-folded {
    padding: 6px 14px !important;
    background: rgba(255, 255, 255, 0.62) !important;
    box-shadow: none !important;
    display: flex !important;
    align-items: center;
  }
  .jq-fold.jq-folded > .section-header { width: 100%; }
  .jq-fold.jq-folded h2 {
    font-size: 15px !important;
    margin: 0 !important;
    font-weight: 600;
  }
  .jq-fold > .section-header h2 { margin-bottom: 0 !important; }
</style>
"""


# iframe 高度 = 看板内容的真实高度，让外层那一条滚动条成为唯一的滚动条。
#
# 为什么不用 Streamlit 的官方通道：`st.components.v1.html` **不监听**
# `streamlit:setFrameHeight`。那套消息只对 `declare_component` 注册过的正式
# 组件有效，raw HTML 渲染的是普通 IFrame 元素，前端根本没挂监听器，消息发过去
# 石沉大海。实测过：iframe 停在 1500px，而内容 2269px，底下 769px 连滚都滚不到。
#
# 能走通的是另一条路：这个 iframe 是 srcdoc + sandbox 带 allow-same-origin，
# 与宿主同源，里面的脚本可以直接操作宿主页面。
_FIT_JS = """
<script data-jq-fit="1">
(function () {
  var MIN_FRAME = 420;
  var MIN_COLUMN = 320;   // 栏再矮就一屏放不下一张卡片，宁可让整页滚起来
  var GAP = 10;

  function frameEl() {
    try { return window.frameElement; } catch (e) { return null; }
  }

  // 量的是 .shell 的实际底边，不能用 documentElement.scrollHeight/offsetHeight：
  // 内容比视口矮时，那两个值会**返回视口高度**，于是「内容高度」永远等于当前
  // iframe 高度，iframe 就再也缩不回去了（实测卡在 900px 不动）。
  function contentHeight() {
    var shell = document.querySelector(".shell");
    if (shell) {
      return Math.ceil(docTop(shell) + shell.getBoundingClientRect().height);
    }
    var b = document.body;
    return Math.ceil(b ? b.scrollHeight : 600);
  }

  // 把高度写成宿主页面里的一条 CSS 规则，**不要写行内样式**。
  // Streamlit 切页面时会复用同一批 DOM 节点：留在容器上的
  // `height: 828px !important` 会套到别的页的元素上，表现为那一页顶上凭空
  // 多出七八百像素空白。这里用 `body:has([data-jq-news-chrome])` 圈住——那个
  // style 标签只在新闻页存在，离开新闻页规则自动失效；选择器还要求容器里
  // 确实装着组件 iframe，节点被复用给别的元素时就不再命中。
  function applyHeight(h) {
    var fe = frameEl();
    if (!fe) return;
    var pd = window.parent.document;
    var el = pd.getElementById("jq-news-frame-fit");
    if (!el) {
      el = pd.createElement("style");
      el.id = "jq-news-frame-fit";
      pd.head.appendChild(el);
    }
    var scope = 'body:has([data-jq-news-chrome]) ';
    // 外层元素容器是 flex 项，Streamlit 给它写死了 `flex: 0 0 <首帧高度>px`。
    // flex-basis 说了算，只改 height 不起作用——页面底下会留一截空白。
    el.textContent =
      scope + 'iframe[data-testid="stIFrame"]{height:' + h + 'px !important;}' +
      scope + '[data-testid="stElementContainer"]:has(> iframe[data-testid="stIFrame"])' +
      '{height:' + h + 'px !important;flex:0 0 ' + h + 'px !important;}';
    // 清掉早期版本可能残留的行内样式
    fe.style.removeProperty("height");
    if (fe.parentElement) {
      fe.parentElement.style.removeProperty("height");
      fe.parentElement.style.removeProperty("flex");
    }
  }

  // 元素在看板文档里的纵向位置。不能直接用 getBoundingClientRect().top：
  // 外层一滚它就变，量出来的高度会一次比一次小。
  function docTop(el) {
    return el.getBoundingClientRect().top
         - document.documentElement.getBoundingClientRect().top;
  }

  // 三栏高度按**宿主窗口**算，不能用 100vh——iframe 高度等于内容高度，
  // 它自己的 100vh 是整张长页，算出来会离谱。
  // 目标：什么都折叠时，三栏正好铺到窗口底；展开面板把栏挤矮到下限之后，
  // 多出来的部分交给整页滚动，不裁剪、不封顶。
  function fitColumns() {
    var ws = document.querySelector(".workspace.active")
          || document.querySelector(".workspace");
    if (!ws) return;
    var cols = ws.querySelectorAll(".column-scroll");
    if (!cols.length) return;      // 「AI判断」那类视图不是三栏，不用管
    var fe = frameEl();
    if (!fe) return;
    var pw = window.parent;
    var scroller = pw.document.querySelector('[data-testid="stMain"]');
    // iframe 顶端在滚动容器内容里的位置（与当前滚到哪儿无关）
    var frameOffset = fe.getBoundingClientRect().top
                    + (scroller ? scroller.scrollTop : 0);
    // 栏高只按「面板都折叠」时的布局算，之后不再跟着面板变。
    // 否则展开一块面板栏就被压到下限，看起来像新闻又被挤没了——而现在整页
    // 能滚，展开的面板往下推就行，栏没必要跟着缩。
    var top = docTop(ws);
    if (!document.querySelector(".jq-foldbar > .panel:not(.jq-folded)")) {
      foldedWsTop = top;
    }
    var base = (foldedWsTop === null) ? top : foldedWsTop;
    var h = Math.max(MIN_COLUMN,
                     Math.round(pw.innerHeight - frameOffset - base - GAP));
    for (var i = 0; i < cols.length; i++) {
      cols[i].style.setProperty("height", h + "px", "important");
    }
  }

  var lastH = 0;
  var foldedWsTop = null;
  function relayout() {
    var fe = frameEl();
    if (!fe) return;
    fitColumns();                          // 先定栏高，内容高度才是最终的
    var h = Math.max(MIN_FRAME, contentHeight() + 4);
    if (Math.abs(h - lastH) < 6) return;   // 抖动不足 6px 不动，免得来回跳
    lastH = h;
    applyHeight(h);
  }

  // Runtime Status 和 主动问AI 折成一行，点标题展开；两条并排放进一个 flex 行
  function setupFold() {
    var panels = document.querySelectorAll(".status-panel, .ask-panel");
    if (!panels.length) return;
    var shell = document.querySelector(".shell");
    var bar = document.querySelector(".jq-foldbar");
    if (!bar && shell) {
      bar = document.createElement("div");
      bar.className = "jq-foldbar";
      var tb = shell.querySelector("section.toolbar");
      if (tb && tb.nextSibling) shell.insertBefore(bar, tb.nextSibling);
      else shell.appendChild(bar);
    }
    for (var i = 0; i < panels.length; i++) {
      (function (panel) {
        if (bar && panel.parentNode !== bar) bar.appendChild(panel);
        if (panel.getAttribute("data-jq-fold")) return;
        panel.setAttribute("data-jq-fold", "1");
        panel.classList.add("jq-fold", "jq-folded");
        var head = panel.querySelector(".section-header");
        if (!head) return;
        head.addEventListener("click", function (ev) {
          // 标题栏里还坐着「分析这条新闻」按钮，点它不该顺手把面板收起来
          if (ev.target.closest("button, a, input, textarea, select")) return;
          panel.classList.toggle("jq-folded");
          relayout();
        });
      })(panels[i]);
    }
  }

  function expandAsk() {
    var p = document.querySelector(".ask-panel.jq-folded");
    if (!p) return;
    p.classList.remove("jq-folded");
    relayout();
  }

  // 折起来也不能挡住「拖卡片问 AI」：一开始拖就自动展开，拖到哪都行。
  // 同理，收着的时候点「分析这条新闻」，得让人看见结果。
  ["dragstart", "dragenter", "dragover"].forEach(function (t) {
    document.addEventListener(t, expandAsk, true);
  });
  document.addEventListener("click", function (ev) {
    if (ev.target.closest("#aiAskButton")) expandAsk();
  }, true);

  function boot() { setupFold(); relayout(); }

  // 卡片是脚本异步填的，切视图（全市场消息 / AI判断 / 港A股…）会换掉整块
  // workspace，所以多量几次，并盯着 DOM 变化重排。
  [0, 120, 350, 800, 1600, 3000].forEach(function (t) { setTimeout(boot, t); });
  window.addEventListener("load", boot);
  window.addEventListener("resize", relayout);
  if (window.MutationObserver) {
    var pending = 0;
    new MutationObserver(function () {
      if (pending) return;
      pending = setTimeout(function () { pending = 0; boot(); }, 150);
    }).observe(document.body, {childList: true, subtree: true});
  }
})();
</script>
"""


def _blend_into_app(html: str) -> str:
    """把嵌入用样式与量高脚本塞进看板 HTML。

    改这里而不是改生成器：生成器产出的那张页面还要独立打开、还要给推送用，
    它有自己的门面是对的。只有嵌进 app 这一种场景才需要去掉接缝。
    """
    if 'data-jq-embed="1"' in html:
        return html
    if "</head>" in html:
        html = html.replace("</head>", _EMBED_CSS + "</head>", 1)
    else:
        html = _EMBED_CSS + html
    # 脚本放 body 末尾，确保它量到的是已经排好版的高度
    if "</body>" in html:
        return html.replace("</body>", _FIT_JS + "</body>", 1)
    return html + _FIT_JS
