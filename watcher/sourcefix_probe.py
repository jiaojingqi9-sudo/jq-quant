#!/usr/bin/env python3
"""sourcefix_probe - 为失效的采集源寻找可用的替代地址 / 验证修复方案。"""
import json
import ssl
import sys
from urllib import error, request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
SEC_UA = "Jiao Jingqi jiaojingqi9@gmail.com"  # SEC 强制要求：UA 必须含真实联系方式

CANDIDATES = {
    # 交通运输部：原 /xw/ 404，试其他栏目
    "mot_A": ("https://www.mot.gov.cn/xinwen/", UA),
    "mot_B": ("https://www.mot.gov.cn/zhengcejiedu/", UA),
    "mot_C": ("http://www.mot.gov.cn/xw/", UA),
    # 住建部：DNS 失败，试 http / 不同主机
    "mohurd_A": ("http://www.mohurd.gov.cn/xinwen/jsyw/", UA),
    "mohurd_B": ("https://www.mohurd.gov.cn/", UA),
    # SEC：403 用带邮箱的 UA 应可解
    "sec_xbrl_fixedUA": ("https://www.sec.gov/Archives/edgar/usgaap.rss.xml", SEC_UA),
    "sec_press_fixedUA": ("https://www.sec.gov/news/pressreleases.rss", SEC_UA),
    # 国资委 / 海关：慢站，加长超时重试一次
    "sasac_retry": ("https://www.sasac.gov.cn/n2588025/n2588119/index.html", UA),
    "gacc_retry": ("https://www.customs.gov.cn/customs/xwfb34/index.html", UA),
    # 路透替代：官方 agency feed 已废
    "reuters_alt_sitemap": ("https://www.reuters.com/arc/outboundfeeds/sitemap-index/?outputType=xml", UA),
}


def probe(url, ua, timeout=25):
    req = request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with request.urlopen(req, timeout=timeout, context=ctx) as resp:
            body = resp.read(40000)
            text = body.decode("utf-8", errors="replace")
            return {
                "http": resp.status,
                "final": resp.geturl()[:90],
                "bytes": len(body),
                "anchors": text.count("<a "),
                "is_feed": "<rss" in text.lower() or "<feed" in text.lower(),
                "ok": True,
            }
    except error.HTTPError as exc:
        return {"http": exc.code, "ok": False}
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:70]}", "ok": False}


def main():
    out = {"kind": "sourcefix_probe", "results": {}}
    for name, (url, ua) in CANDIDATES.items():
        out["results"][name] = {"url": url, **probe(url, ua)}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
