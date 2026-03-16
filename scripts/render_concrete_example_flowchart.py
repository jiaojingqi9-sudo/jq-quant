from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "reports/港A科技新闻系统-具体例子流程图.png"


PALETTE = {
    "bg": "#f7f1e7",
    "ink": "#193447",
    "muted": "#627788",
    "accent": "#173f5f",
    "line": "#cfdae3",
    "shadow": "#e8dccd",
    "green": "#e7f4ef",
    "blue": "#e9f0fb",
    "purple": "#efeafb",
    "peach": "#fbefe2",
    "rose": "#f8ebf1",
    "cyan": "#e6f5f7",
    "cream": "#fff9f1",
}


def get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Supplemental/Songti.ttc",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
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
    lines: list[str] = []
    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for ch in paragraph:
            candidate = current + ch
            if current and text_width(draw, candidate, font) > max_width:
                lines.append(current)
                current = ch
            else:
                current = candidate
        if current:
            lines.append(current)
    return lines


def box_height(
    draw: ImageDraw.ImageDraw,
    title: str,
    bullets: list[str],
    width: int,
    title_font: ImageFont.ImageFont,
    body_font: ImageFont.ImageFont,
) -> int:
    inner = width - 100
    height = 32
    height += len(wrap_text(draw, title, title_font, inner)) * 44
    height += 8
    for bullet in bullets:
        wrapped = wrap_text(draw, bullet, body_font, inner - 24)
        height += len(wrapped) * 32
        height += 12
    height += 18
    return height


def draw_box(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    title: str,
    bullets: list[str],
    fill: str,
    title_font: ImageFont.ImageFont,
    body_font: ImageFont.ImageFont,
) -> int:
    height = box_height(draw, title, bullets, width, title_font, body_font)
    draw.rounded_rectangle((x + 10, y + 12, x + width + 10, y + height + 12), 26, fill=PALETTE["shadow"])
    draw.rounded_rectangle((x, y, x + width, y + height), 26, fill=fill, outline=PALETTE["line"], width=3)

    cursor_y = y + 30
    inner_x = x + 46
    inner_width = width - 100
    for line in wrap_text(draw, title, title_font, inner_width):
        draw.text((inner_x, cursor_y), line, font=title_font, fill=PALETTE["ink"])
        cursor_y += 44
    cursor_y += 6

    for bullet in bullets:
        wrapped = wrap_text(draw, bullet, body_font, inner_width - 24)
        if wrapped:
            draw.text((inner_x, cursor_y), "•", font=body_font, fill=PALETTE["accent"])
        line_y = cursor_y
        for index, line in enumerate(wrapped):
            offset_x = inner_x + 22 if index == 0 else inner_x + 22
            draw.text((offset_x, line_y), line, font=body_font, fill=PALETTE["ink"])
            line_y += 32
        cursor_y = line_y + 12
    return height


def draw_arrow(draw: ImageDraw.ImageDraw, x: int, y1: int, y2: int) -> None:
    draw.line((x, y1, x, y2 - 18), fill=PALETTE["accent"], width=8)
    draw.polygon([(x, y2), (x - 15, y2 - 24), (x + 15, y2 - 24)], fill=PALETTE["accent"])


def render() -> Path:
    title_font = get_font(40)
    subtitle_font = get_font(24)
    section_font = get_font(32)
    body_font = get_font(24)
    footer_font = get_font(26)

    image = Image.new("RGB", (1800, 3200), PALETTE["bg"])
    draw = ImageDraw.Draw(image)

    draw.text((110, 80), "港A科技新闻系统流程图", font=title_font, fill=PALETTE["ink"])
    subtitle = "这次不讲当前运行情况，只讲一个具体消息是怎样被系统一步步处理、筛选和学习的。"
    draw.text((110, 146), subtitle, font=subtitle_font, fill=PALETTE["muted"])

    example_band = (
        "例子消息：某 AIDC 算力电源公司完成融资，并推进与国际云厂商合作。"
        " 这类消息为什么值得看？因为它可能同时牵动 AI算力、数据中心、服务器、电源设备这些港A科技链条。"
    )
    draw.rounded_rectangle((120, 210, 1680, 336), 28, fill="#17384f")
    for i, line in enumerate(wrap_text(draw, example_band, subtitle_font, 1480)):
        draw.text((158, 246 + i * 36), line, font=subtitle_font, fill="#ffffff")

    steps = [
        (
            "1. 消息进入系统",
            [
                "公告监管源：CNInfo、HKEX News Releases、CSRC、SEC Press Releases、SEC XBRL US GAAP。",
                "媒体快讯源：Eastmoney（A股公告 / 港股公告 / 新闻）、36Kr、TMTPost、财联社、格隆汇、新华科技、虎嗅科技、凤凰科技。",
                "社交源：微博、雪球。",
                "这一层还不判断值不值得交易，先保证该进来的消息都能进来。",
            ],
            PALETTE["green"],
        ),
        (
            "2. 收集与清洗",
            [
                "系统会统一格式、去掉重复转载、把同一件事的多条报道并成一个事件。",
                "同时先打上大方向标签，例如：AI算力、数据中心、服务器、电源设备、国产替代。",
                "如果换成别的新闻，也可能先落到机器人、半导体、网络安全、信创、新材料这些板块。",
            ],
            PALETTE["blue"],
        ),
        (
            "3. 热度筛选机制",
            [
                "系统先看这条消息热不热：够不够新、有没有多个来源同时提、来源是否可靠、事件是不是足够具体。",
                "拿这个例子来说，如果 36Kr 先发、微博和雪球随后讨论、标题里又带“融资”“国际合作”这类强事件词，它就会进入热点池。",
                "纯例行公告、空泛表态、重复转载通常会在这里被压下去，不进入重点展示。",
            ],
            PALETTE["peach"],
        ),
        (
            "4. 热点怎么和词库匹配",
            [
                "到了科技专题层，系统会去词库里找这条消息打中了哪些词。",
                "这个例子里，可能命中的词有：AIDC、AI算力、数据中心、电源设备、国际大厂合作、订单导入。",
                "这些词不是为了好看，而是为了让系统知道：这条消息更像在讲哪条科技题材，也就是它是不是当前热点主线的一部分。"
            ],
            PALETTE["purple"],
        ),
        (
            "5. 匹配后怎么展开热点链",
            [
                "词库命中后，系统会顺着影响链往下推：AI算力 -> 服务器链 / 数据中心 / 电源链。",
                "然后再去找对应的港A科技候选股，而不是只停留在“这是个科技新闻”。",
                "比如最后可能会落到工业富联、中际旭创、中兴通讯这类更接近这条链的标的。",
            ],
            PALETTE["rose"],
        ),
        (
            "6. 输出给你看的结果",
            [
                "网页里展示的不是原始新闻堆，而是整理后的重点事件、对应题材、候选股票，以及“为什么相关”。",
                "手机提醒也不是所有消息都推，只推新的高优先级结果，所以你看到的是筛过的线索，不是噪音。"
            ],
            PALETTE["green"],
        ),
        (
            "7. 词库怎么学习新热点",
            [
                "如果系统连续看到一个新词，比如 800V HVDC，在 AIDC、数据中心、电源这些消息里反复和已知热点词一起出现，",
                "它就会推测：这个新词大概率也属于 AI算力 / 电源链方向，于是先把它放进待审核队列。",
                "你确认收录后，它就进入正式词库。下次再遇到 800V HVDC，系统会更快把它识别成热点相关词，而不是普通技术名词。"
            ],
            PALETTE["cyan"],
        ),
    ]

    x = 170
    width = 1460
    center_x = x + width // 2
    cursor_y = 388

    for index, (title, bullets, fill) in enumerate(steps):
        height = draw_box(draw, x, cursor_y, width, title, bullets, fill, section_font, body_font)
        if index < len(steps) - 1:
            arrow_start = cursor_y + height + 18
            arrow_end = arrow_start + 42
            draw_arrow(draw, center_x, arrow_start, arrow_end)
            cursor_y = arrow_end + 16
        else:
            cursor_y = cursor_y + height + 40

    footer = (
        "一句话总结：这套系统的价值，不是把新闻搬给你看，而是把一条具体消息拆成“属于哪个板块、"
        "打中了哪些词、会传到哪些港A科技标的、值不值得提醒你”。"
    )
    draw.rounded_rectangle((120, cursor_y, 1680, cursor_y + 150), 24, fill=PALETTE["cream"], outline=PALETTE["line"], width=3)
    for i, line in enumerate(wrap_text(draw, footer, footer_font, 1480)):
        draw.text((160, cursor_y + 38 + i * 38), line, font=footer_font, fill=PALETTE["ink"])

    final = image.crop((0, 0, 1800, cursor_y + 190))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    final.save(OUTPUT)
    return OUTPUT


if __name__ == "__main__":
    print(render())
