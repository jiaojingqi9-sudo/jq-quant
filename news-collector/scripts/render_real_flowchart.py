from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = ROOT / "reports/live/latest_report.json"
DEFAULT_COLLECT = ROOT / "reports/live/collect_status.json"
DEFAULT_DELIVERY = ROOT / "reports/live/delivery_status.json"
DEFAULT_OUTPUT = ROOT / "reports/港A科技新闻系统实战流程图.png"


PALETTE = {
    "bg": "#f8f1e4",
    "ink": "#1f3850",
    "muted": "#5f7387",
    "accent": "#103651",
    "soft_green": "#e7f4ef",
    "soft_blue": "#e9f0fb",
    "soft_purple": "#efeafb",
    "soft_peach": "#fbefe3",
    "soft_rose": "#f8eaf0",
    "soft_cyan": "#e7f5f7",
    "line": "#d0dbe5",
    "shadow": "#eadfcf",
}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def font_candidates() -> list[str]:
    return [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ]


def get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in font_candidates():
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size=size)
    return ImageFont.load_default()


def text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), text, font=font)
    return int(box[2] - box[0])


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    if not text:
        return [""]

    paragraphs = text.split("\n")
    wrapped: list[str] = []
    for paragraph in paragraphs:
        if not paragraph:
            wrapped.append("")
            continue
        current = ""
        for ch in paragraph:
            candidate = current + ch
            if current and text_width(draw, candidate, font) > max_width:
                wrapped.append(current)
                current = ch
            else:
                current = candidate
        if current:
            wrapped.append(current)
    return wrapped


def trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def friendly_sources() -> list[str]:
    cfg = load_json(ROOT / "config/live_sources.json")

    official = ["巨潮资讯", "港交所", "证监会", "SEC"]
    media = ["东方财富", "36Kr", "TMTPost", "虎嗅", "新华科技", "凤凰科技", "财联社", "格隆汇"]
    social = []
    if cfg.get("weibo", {}).get("enabled"):
        social.append("微博")
    if cfg.get("xueqiu", {}).get("enabled"):
        social.append("雪球")

    lines = [
        "官方源：" + "、".join(official),
        "媒体源：" + "、".join(media),
        "社交源：" + ("、".join(social) if social else "当前未开启"),
    ]
    return lines


def friendly_asset_names(symbols: Iterable[str]) -> str:
    name_map = {
        "NVDA": "英伟达",
        "AMD": "AMD",
        "QQQ": "纳指ETF",
        "SOXX": "半导体ETF",
        "0700.HK": "腾讯",
        "1810.HK": "小米",
        "9988.HK": "阿里",
        "300308.SZ": "中际旭创",
        "603986.SH": "兆易创新",
        "688256.SH": "寒武纪",
        "0981.HK": "中芯国际",
        "688981.SH": "中芯国际A",
    }
    names = [name_map.get(symbol, symbol) for symbol in symbols]
    return " / ".join(names[:4])


def build_steps(report: dict, collect: dict, delivery: dict) -> tuple[str, list[dict], str]:
    counts = collect.get("counts", {})
    modules = {module["name"]: module for module in collect.get("modules", [])}
    tech_block = modules.get("tech_block", {})
    lexicon_block = modules.get("lexicon_discovery", {})

    positive = (report.get("positive_catalysts") or [{}])[0]
    positive_assets = friendly_asset_names(positive.get("instruments") or [])
    positive_title = trim_text(positive.get("headline") or "当前正向催化样例", 28)

    tech_signals = (
        ((report.get("feature_blocks") or {}).get("tech_block") or {}).get("signals") or [{}]
    )
    top_tech = tech_signals[0] if tech_signals else {}
    tech_assets = friendly_asset_names(
        [item.get("symbol", "") for item in top_tech.get("candidate_assets", []) if item.get("symbol")]
    )
    tech_title = trim_text(top_tech.get("headline") or "当前科技专题样例", 28)

    lexicon = ((report.get("feature_blocks") or {}).get("lexicon_discovery") or {})
    candidates = [item.get("text", "") for item in lexicon.get("candidates", []) if item.get("text")]
    pending_text = "、".join(candidates[:3]) if candidates else "当前暂无待审新词"

    delivery_note = (delivery.get("notification") or {}).get("detail") or "按规则触发时才会发手机。"

    summary = (
        f"这套系统现在会把 {counts.get('raw_records', 0)} 条原始消息，整理成 "
        f"{counts.get('documents', 0)} 条标准文档、{counts.get('clusters', 0)} 个事件，"
        f"再压缩到 {tech_block.get('signal_count', 0)} 条港A科技信号。"
    )

    steps = [
        {
            "title": "1. 消息入口",
            "color": PALETTE["soft_green"],
            "lines": friendly_sources()
            + [
                "系统不是只盯一个网站，而是先把会冒泡的消息尽量接全。",
            ],
        },
        {
            "title": "2. 收集与清洗",
            "color": PALETTE["soft_blue"],
            "lines": [
                "先统一格式，再去重，再把“同一件事的多条消息”合成一个事件。",
                f"当前一轮真实结果：{counts.get('raw_records', 0)} 条原始消息 -> {counts.get('documents', 0)} 条标准文档 -> {counts.get('clusters', 0)} 个事件。",
                "这一步解决的是：不让你被重复转载、标题党和搬运稿刷屏。",
            ],
        },
        {
            "title": "3. 第一层筛选：全市场主线",
            "color": PALETTE["soft_peach"],
            "lines": [
                "先看三件事：这条消息新不新，影响大不大，来源硬不硬。",
                f"当前产出：{counts.get('ranked_events', 0)} 个排序事件，{counts.get('ranked_instruments', 0)} 个基础候选标的，{counts.get('alerts', 0)} 条提醒。",
                f"例子：{positive_title}",
                f"系统会先把它映射成可交易线索，比如 {positive_assets or '相关候选标的'}。",
            ],
        },
        {
            "title": "4. 第二层筛选：港A科技专题",
            "color": PALETTE["soft_purple"],
            "lines": [
                "科技专题会再挑一遍：能不能挂到 AI算力、机器人、国产替代、信创、网络安全这些题材上。",
                "词库和影响链会继续往下问：这条消息最可能带动哪些港A科技主题和股票？",
                f"当前产出：{tech_block.get('signal_count', 0)} 条科技信号，{tech_block.get('theme_count', 0)} 个主题，{tech_block.get('asset_count', 0)} 个候选港A标的。",
                f"例子：{tech_title} -> {tech_assets or '对应港A科技候选股'}。",
            ],
        },
        {
            "title": "5. 网页与提醒",
            "color": PALETTE["soft_rose"],
            "lines": [
                "网页上看到的是：重点事件、科技主题、候选股票，以及“为什么相关”。",
                "手机不会推所有新闻，只会推新的高优先级结果，避免被噪音轰炸。",
                f"当前状态：{delivery_note}",
            ],
        },
        {
            "title": "6. 词库升级闭环",
            "color": PALETTE["soft_cyan"],
            "lines": [
                "系统会从科技相关新闻里找新词，但不会直接写进正式词库。",
                "新词会先进审核队列；你确认后，下轮系统才正式学会它。",
                f"当前待审：{pending_text}。",
                f"这一步的价值：系统会越来越懂新题材，但不会自己把词库学脏。当前待审 {lexicon_block.get('pending_count', 0)} 个。",
            ],
        },
    ]

    footer = (
        "一句人话：这套系统不是“给你更多新闻”，而是先把噪音压掉，再把更可能带动港A科技板块的事件、主题和股票线索筛出来。"
    )
    return summary, steps, footer


def measure_box_height(
    draw: ImageDraw.ImageDraw,
    title: str,
    lines: list[str],
    width: int,
    title_font: ImageFont.ImageFont,
    body_font: ImageFont.ImageFont,
) -> int:
    inner_width = width - 96
    wrapped_title = wrap_text(draw, title, title_font, inner_width)
    total = 34
    total += len(wrapped_title) * 44
    total += 10
    for line in lines:
        wrapped = wrap_text(draw, line, body_font, inner_width)
        total += len(wrapped) * 33
        total += 10
    total += 24
    return total


def draw_box(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    title: str,
    lines: list[str],
    fill: str,
    title_font: ImageFont.ImageFont,
    body_font: ImageFont.ImageFont,
) -> int:
    height = measure_box_height(draw, title, lines, width, title_font, body_font)
    shadow_box = (x + 10, y + 12, x + width + 10, y + height + 12)
    box = (x, y, x + width, y + height)
    draw.rounded_rectangle(shadow_box, radius=26, fill=PALETTE["shadow"])
    draw.rounded_rectangle(box, radius=26, fill=fill, outline=PALETTE["line"], width=3)

    cursor_y = y + 34
    inner_x = x + 48
    inner_width = width - 96

    for line in wrap_text(draw, title, title_font, inner_width):
        draw.text((inner_x, cursor_y), line, font=title_font, fill=PALETTE["ink"])
        cursor_y += 44
    cursor_y += 6

    for item in lines:
        wrapped = wrap_text(draw, item, body_font, inner_width)
        for line in wrapped:
            draw.text((inner_x, cursor_y), line, font=body_font, fill=PALETTE["ink"])
            cursor_y += 33
        cursor_y += 10
    return height


def draw_arrow(
    draw: ImageDraw.ImageDraw,
    x: int,
    y1: int,
    y2: int,
    color: str,
) -> None:
    draw.line((x, y1, x, y2 - 20), fill=color, width=8)
    draw.polygon(
        [
            (x, y2),
            (x - 16, y2 - 26),
            (x + 16, y2 - 26),
        ],
        fill=color,
    )


def render(report_path: Path, collect_path: Path, delivery_path: Path, output_path: Path) -> Path:
    report = load_json(report_path)
    collect = load_json(collect_path)
    delivery = load_json(delivery_path)

    title_font = get_font(42)
    subtitle_font = get_font(24)
    band_font = get_font(26)
    box_title_font = get_font(32)
    body_font = get_font(24)
    footer_font = get_font(26)

    image = Image.new("RGB", (1800, 3000), PALETTE["bg"])
    draw = ImageDraw.Draw(image)

    summary, steps, footer = build_steps(report, collect, delivery)

    title = "港A科技新闻系统实战流程图"
    subtitle = "这张图不讲空概念，只讲这套系统现在到底怎么收、怎么筛、怎么只留下值得你看的东西。"

    draw.text((120, 90), title, font=title_font, fill=PALETTE["ink"])
    draw.text((120, 154), subtitle, font=subtitle_font, fill=PALETTE["muted"])

    band_box = (120, 210, 1680, 320)
    draw.rounded_rectangle((130, 222, 1690, 332), radius=28, fill=PALETTE["shadow"])
    draw.rounded_rectangle(band_box, radius=28, fill="#16344a")
    for i, line in enumerate(wrap_text(draw, summary, band_font, 1480)):
        draw.text((160, 244 + i * 38), line, font=band_font, fill="#ffffff")

    x = 180
    width = 1440
    center_x = x + width // 2
    cursor_y = 380

    for index, step in enumerate(steps):
        height = draw_box(
            draw,
            x,
            cursor_y,
            width,
            step["title"],
            step["lines"],
            step["color"],
            box_title_font,
            body_font,
        )
        if index < len(steps) - 1:
            arrow_start = cursor_y + height + 20
            arrow_end = arrow_start + 44
            draw_arrow(draw, center_x, arrow_start, arrow_end, PALETTE["accent"])
            cursor_y = arrow_end + 18
        else:
            cursor_y = cursor_y + height + 40

    footer_box = (120, cursor_y, 1680, cursor_y + 140)
    draw.rounded_rectangle((130, cursor_y + 12, 1690, cursor_y + 152), radius=24, fill=PALETTE["shadow"])
    draw.rounded_rectangle(footer_box, radius=24, fill="#fffaf2", outline=PALETTE["line"], width=3)
    for i, line in enumerate(wrap_text(draw, footer, footer_font, 1480)):
        draw.text((160, cursor_y + 34 + i * 36), line, font=footer_font, fill=PALETTE["ink"])

    cropped = image.crop((0, 0, 1800, footer_box[3] + 80))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(output_path)
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Render a concrete flowchart for the A/H tech news system.")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--collect", type=Path, default=DEFAULT_COLLECT)
    parser.add_argument("--delivery", type=Path, default=DEFAULT_DELIVERY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    output_path = render(args.report, args.collect, args.delivery, args.output)
    print(output_path)


if __name__ == "__main__":
    main()
