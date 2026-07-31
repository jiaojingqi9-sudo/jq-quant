#!/usr/bin/env python3
"""make_icon - 生成「寻宝猫」应用图标（.icns）并装到 app 上。

为什么直接用 PIL 画而不是渲染 SVG：macOS 上没有可靠的现成 SVG 光栅化工具
（sips 不支持，rsvg-convert 要另装）。直接按坐标绘制反而可控，而且能针对每个
尺寸单独渲染，小图不会因为缩放而糊。

画法：在 4 倍分辨率下绘制再缩小（LANCZOS），得到平滑边缘。

配色沿用富途那套观感：橙底 + 白猫 + 金尾。
"""
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
APP = HOME / "Desktop" / "寻宝猫.app"
OLD_APP = HOME / "Desktop" / "JQ Quant.app"
WORK = HOME / "All here" / "trade" / "runtime" / "icon_build"

ORANGE = (250, 100, 0, 255)
WHITE = (255, 255, 255, 255)
GOLD = (255, 197, 61, 255)

# 设计坐标基于 120×120 画布（与预览稿一致）
def draw_icon(size: int):
    from PIL import Image, ImageDraw

    ss = 4  # 超采样倍数
    S = size * ss
    k = S / 120.0            # 120 单位 → 像素
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def P(x, y):
        return (x * k, y * k)

    # 底：圆角方块。macOS 的圆角半径约为边长的 22%
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.225), fill=ORANGE)

    # 金尾：先画，让猫身压在上面
    tail_w = int(10 * k)
    pts = []
    # 二次贝塞尔 (84,84) → 控制点 (101,84) → (101,62)，再直上到 (101,38)
    for i in range(41):
        t = i / 40
        x = (1 - t) ** 2 * 84 + 2 * (1 - t) * t * 101 + t ** 2 * 101
        y = (1 - t) ** 2 * 84 + 2 * (1 - t) * t * 84 + t ** 2 * 62
        pts.append(P(x, y))
    pts.append(P(101, 38))
    d.line(pts, fill=GOLD, width=tail_w, joint="curve")
    for pt in (pts[0], pts[-1]):          # 圆头
        r = tail_w / 2
        d.ellipse([pt[0] - r, pt[1] - r, pt[0] + r, pt[1] + r], fill=GOLD)

    # 耳朵（两个三角）
    d.polygon([P(28, 56), P(30, 22), P(57, 43)], fill=WHITE)
    d.polygon([P(85, 56), P(83, 22), P(56, 43)], fill=WHITE)

    # 脸：略扁的椭圆
    d.ellipse([P(26, 36)[0], P(26, 36)[1], P(86, 92)[0], P(86, 92)[1]], fill=WHITE)

    # 眼睛：竖椭圆，猫的神气全在这里
    for cx in (45.5, 66.5):
        d.ellipse([P(cx - 5, 57)[0], P(cx - 5, 57)[1],
                   P(cx + 5, 70)[0], P(cx + 5, 70)[1]], fill=ORANGE)

    return img.resize((size, size), Image.LANCZOS)


def main():
    out = {"kind": "make_icon"}
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        out["error"] = "缺少 Pillow，无法绘图（pip install Pillow）"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1

    iconset = WORK / "AppIcon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)

    # macOS 要求的全套尺寸
    specs = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
             (256, 1), (256, 2), (512, 1), (512, 2)]
    made = []
    for base, scale in specs:
        px = base * scale
        name = f"icon_{base}x{base}.png" if scale == 1 else f"icon_{base}x{base}@2x.png"
        draw_icon(px).save(iconset / name)
        made.append(name)
    out["png_count"] = len(made)

    # 预览大图，方便肉眼确认
    preview = WORK / "preview_512.png"
    draw_icon(512).save(preview)
    out["preview"] = str(preview)

    icns = WORK / "AppIcon.icns"
    r = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                       capture_output=True, text=True)
    out["iconutil_rc"] = r.returncode
    if r.returncode != 0:
        out["iconutil_err"] = r.stderr[:200]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 1
    out["icns_kb"] = round(icns.stat().st_size / 1024)

    # 改名：JQ Quant → 寻宝猫
    if OLD_APP.exists() and not APP.exists():
        subprocess.run(["/bin/mv", str(OLD_APP), str(APP)], capture_output=True)
    target = APP if APP.exists() else OLD_APP
    out["app"] = str(target)

    res = target / "Contents" / "Resources"
    res.mkdir(parents=True, exist_ok=True)
    shutil.copy2(icns, res / "AppIcon.icns")

    import plistlib
    pl = target / "Contents" / "Info.plist"
    data = plistlib.loads(pl.read_bytes())
    data["CFBundleIconFile"] = "AppIcon"
    data["CFBundleName"] = "寻宝猫"
    data["CFBundleDisplayName"] = "寻宝猫"
    pl.write_bytes(plistlib.dumps(data))

    # 让 Finder 与 Dock 重新读取图标
    subprocess.run(["touch", str(target)], capture_output=True)
    subprocess.run(["/System/Library/Frameworks/CoreServices.framework/Frameworks/"
                    "LaunchServices.framework/Support/lsregister", "-f", str(target)],
                   capture_output=True)
    subprocess.run(["killall", "Dock"], capture_output=True)

    out["installed"] = (res / "AppIcon.icns").exists()
    out["display_name"] = "寻宝猫"
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
