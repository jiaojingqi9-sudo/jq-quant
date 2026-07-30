#!/usr/bin/env python3
"""sourcecheck - 从本机实测每个报错采集源，判断到底哪坏了、怎么修。

只发 GET 请求读首屏，不改任何配置。对每个 URL 报告：
  HTTP 状态 / 重定向到哪 / 证书是否有问题 / DNS 是否解析 / 页面是否有内容
并给出修复建议（换 URL、放宽证书、加重试、还是该停用）。
"""
import json
import socket
import ssl
import sys
from urllib import error, request
from urllib.parse import urlparse

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"

TARGETS = {
    # 报错源
    "mohurd-news":   "https://www.mohurd.gov.cn/xinwen/jsyw/",
    "mot-news":      "https://www.mot.gov.cn/xw/",
    "sasac-news":    "https://www.sasac.gov.cn/n2588025/n2588119/index.html",
    "gacc-news":     "https://www.customs.gov.cn/customs/xwfb34/index.html",
    "reuters-tech":  "https://www.reutersagency.com/feed/?taxonomy=best-sectors&post_type=best",
    "csrc-home":     "https://www.csrc.gov.cn/",
    "cninfo":        "https://www.cninfo.com.cn/",
    "nhsa-news":     "https://www.nhsa.gov.cn/col/col14/index.html",
    "sec-xbrl":      "https://www.sec.gov/Archives/edgar/usgaap.rss.xml",
    "hkex-news":     "https://www.hkex.com.hk/Services/RSS-Feeds/News-Releases?sc_lang=en",
    # 抓到但正文为空
    "sse_announce":  "https://www.sse.com.cn/disclosure/announcement/general/index.shtml",
    "xinhua-tech":   "https://www.news.cn/tech/",
    "gov-most":      "https://www.most.gov.cn/xxgk/xinxifenlei/fdzdgknr/fgzc/gfxwj/index.html",
    # 几个可能的替代地址，一并试
    "ALT_mot":       "https://www.mot.gov.cn/jiaotongyaowen/",
    "ALT_mohurd":    "https://www.mohurd.gov.cn/xinwen/",
    "ALT_reuters":   "https://www.reuters.com/technology/",
}


def probe(name, url, timeout=15, insecure=False):
    out = {"id": name, "url": url}
    host = urlparse(url).hostname or ""

    # DNS
    try:
        socket.gethostbyname(host)
        out["dns"] = "ok"
    except Exception as exc:
        out["dns"] = f"FAIL: {type(exc).__name__}"
        out["verdict"] = "DNS 解析不了——域名可能已变更，或本机 DNS/网络限制"
        return out

    ctx = None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

    req = request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    try:
        with request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read(60000)
            out["http"] = resp.status
            out["final_url"] = resp.geturl()
            out["redirected"] = resp.geturl().rstrip("/") != url.rstrip("/")
            out["bytes"] = len(body)
            text = body.decode("utf-8", errors="replace")
            out["looks_like_html"] = "<html" in text.lower() or "<!doctype" in text.lower()
            out["looks_like_feed"] = "<rss" in text.lower() or "<feed" in text.lower()
            # 粗略判断有没有正文链接
            out["anchor_count"] = text.count("<a ")
            out["verdict"] = "可访问"
            if insecure:
                out["verdict"] += "（但需跳过证书校验）"
    except error.HTTPError as exc:
        out["http"] = exc.code
        out["verdict"] = f"HTTP {exc.code}——地址失效，需要换 URL"
    except ssl.SSLError as exc:
        out["ssl_error"] = str(exc)[:120]
        if not insecure:
            return probe(name, url, timeout, insecure=True)
        out["verdict"] = "证书校验失败"
    except error.URLError as exc:
        reason = str(getattr(exc, "reason", exc))
        out["url_error"] = reason[:140]
        if "CERTIFICATE_VERIFY_FAILED" in reason and not insecure:
            return probe(name, url, timeout, insecure=True)
        if "timed out" in reason:
            out["verdict"] = "超时——站点慢或封锁，建议加长超时/重试，或降低频率"
        else:
            out["verdict"] = "连不上"
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"[:140]
        out["verdict"] = "未知错误"
    return out


def main():
    results = [probe(n, u) for n, u in TARGETS.items()]
    ok = [r for r in results if str(r.get("http")) == "200"]
    print(json.dumps({
        "kind": "sourcecheck",
        "ok_count": len(ok),
        "total": len(results),
        "results": results,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
