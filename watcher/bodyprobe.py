#!/usr/bin/env python3
"""bodyprobe - 探测详情页真实 HTML 结构，用来修正正文提取规则。

对每个源：抓列表页 → 按 include_link_patterns 找一个详情页 → 抓详情页
→ 列出页面里所有 div 的 class 名和长度，看正文到底装在哪个容器里。
"""
import json
import re
import ssl
import sys
from urllib import error, request
from urllib.parse import urljoin

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

SOURCES = {
    "sse_announcements": {
        "list": "https://www.sse.com.cn/disclosure/announcement/general/index.shtml",
        "link_re": r"/disclosure/[^\"'>\s]+\.s?html",
    },
    "xinhua-tech": {
        "list": "https://www.news.cn/tech/",
        "link_re": r"https?://www\.news\.cn/tech/\d{8}/[^/]+/c\.html",
    },
    "gov-most": {
        "list": "https://www.most.gov.cn/xxgk/xinxifenlei/fdzdgknr/fgzc/gfxwj/index.html",
        "link_re": r"/xxgk/[^\"'>\s]+\.html",
    },
}


def fetch(url, timeout=20):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with request.urlopen(req, timeout=timeout, context=ctx) as resp:
        raw = resp.read(400_000)
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc), resp.geturl()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), url


def analyse(html):
    """列出所有带 class 的 div，按内部文本长度排序——正文通常在最长的那个。"""
    found = []
    for m in re.finditer(r'<div[^>]*\bclass=["\']([^"\']+)["\'][^>]*>', html):
        cls = m.group(1).strip()
        start = m.end()
        segment = html[start:start + 8000]
        text = re.sub(r"<[^>]+>", "", segment)
        text = re.sub(r"\s+", "", text)
        found.append((len(text), cls))
    found.sort(reverse=True)
    seen, top = set(), []
    for length, cls in found:
        if cls in seen:
            continue
        seen.add(cls)
        top.append({"class": cls, "text_len": length})
        if len(top) >= 12:
            break
    return top


def main():
    out = {"kind": "bodyprobe", "sources": {}}
    for sid, conf in SOURCES.items():
        entry = {}
        try:
            html, final = fetch(conf["list"])
            entry["list_ok"] = True
            links = re.findall(conf["link_re"], html)
            links = [urljoin(final, l) for l in links]
            uniq = []
            for l in links:
                if l not in uniq and "index" not in l.rsplit("/", 1)[-1]:
                    uniq.append(l)
            entry["links_found"] = len(uniq)
            entry["sample_links"] = uniq[:3]
            if uniq:
                dhtml, durl = fetch(uniq[0])
                entry["detail_url"] = durl
                entry["detail_bytes"] = len(dhtml)
                entry["containers"] = analyse(dhtml)
                entry["has_article_tag"] = "<article" in dhtml.lower()
                # 常见正文容器 id
                ids = re.findall(r'<div[^>]*\bid=["\']([^"\']+)["\']', dhtml)
                entry["div_ids"] = ids[:12]
        except error.HTTPError as exc:
            entry["error"] = f"HTTP {exc.code}"
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {str(exc)[:90]}"
        out["sources"][sid] = entry
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
