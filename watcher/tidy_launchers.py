#!/usr/bin/env python3
"""tidy_launchers - 把冗余启动器与陈旧目录移进回收站，并留下说明。

不加 --apply 时只打印计划，不动任何文件。

判断依据（2026-07-31 实测，不是猜的）：
  · 已装的 launchd 任务全部直接调 python 模块，没有一个指向 .command
    → 移走 .command 不会断掉任何后台任务
  · app 首页「启动桌面控制台」按钮调用 Launch_Trading_Control_Panel.command
    → 这一个必须留
  · app 已有 股票/历史模拟/加密交易/实时建议/市场新闻/选股器 六个功能页
    → 只为打开这些页面而存在的启动器都是冗余
  · Codex 订阅已停，调 /Applications/Codex.app 的脚本永远失败
  · collect / notify / health 已由 launchd 常驻，对应的 .command 会先 pkill
    再前台重跑，与后台任务打架

一律移动而非删除，随时可从回收站取回。
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
ALL = HOME / "All here"
TRASH = ALL / "_回收站_20260730" / "冗余启动器_20260731"

L01 = ALL / "01_启动器"
LSTOCK = ALL / "trade" / "stock" / "launchers"
LCRYPTO = ALL / "trade" / "crypto" / "launchers"
LNEWS = ALL / "news collector" / "scripts"

# (路径, 移走的理由)
MOVE = [
    (L01 / "打开交易App.command",
     "寻宝猫.app 做同一件事，且用 Chrome 应用窗口而不是浏览器标签页"),
    (L01 / "总控制台.command",
     "与 stock/launchers/Launch_Trading_Control_Panel.command 同功能，保留后者"),

    (LSTOCK / "Open_TAA_Quant_Trading_App.command",
     "全文只有一行 open 那个 .app；寻宝猫已取代"),
    (LSTOCK / "Open_Trading_Dashboard.command",
     "起 taa_futu.cli dashboard；寻宝猫已取代"),
    (LSTOCK / "启动量化交易控制台.command",
     "与 Launch_Trading_Control_Panel.command 重复"),
    (LSTOCK / "Open_Stock_Screener.command",
     "app 里已有「选股器 / Screener」功能页"),
    (LSTOCK / "修复启动脚本.command",
     "它要修的是桌面上的「启动量化交易控制台.command」，那个文件已不在桌面"),

    (LCRYPTO / "Open_Crypto_OFIM_App.command",
     "app 里已有「加密交易 / Crypto」页，且 launchd 已把它常驻在 8503 端口"),

    (LNEWS / "market_news_codex_review_auto.command",
     "Codex 订阅已停，该任务只会反复失败"),
    (LNEWS / "market_news_codex_review_auto_stop.command",
     "配套的停止脚本，随主脚本一起移走"),
    (LNEWS / "market_news_thread_review_auto.command",
     "直接调用 /Applications/Codex.app/Contents/Resources/codex，已不可用"),
    (LNEWS / "market_news_thread_review_auto_stop.command",
     "配套的停止脚本，随主脚本一起移走"),
    (LNEWS / "market_news_board.command",
     "app 里已有「市场新闻」页"),
    (LNEWS / "market_news_console.command",
     "会 pkill 掉 launchd 常驻的 collect 再前台重跑；launchd 随后又拉起自己那份，"
     "结果两个采集器同时写库"),
    (LNEWS / "market_news_delivery.command",
     "同上，与常驻的 notify 任务冲突"),
    (LNEWS / "market_news_health.command",
     "同上，与常驻的 health 任务冲突"),
]

# 目录级清理
MOVE_DIRS = [
    (ALL / "trade" / "runtime" / "desktop_launcher_archive_20260310",
     "2026-03-10 的桌面入口备份，文件名已叠成 .app.bak-20260310-v2.app.bak-20260310-v2"),
    (ALL / ".venv",
     "根目录的 Python 3.13 环境（bs4/curl_cffi），全仓库无任何脚本引用；"
     "trade 与 news collector 各有自己的 .venv"),
]

# 说明文件：留下的每个启动器干什么
README = {
    L01: """# 01_启动器

日常开 app 请用桌面上的**寻宝猫**，不要用这里的脚本。
这里放的是 app 里没有、也不该放进 app 的运维操作。

| 脚本 | 什么时候用 |
| --- | --- |
| 关闭交易App.command | 关掉 streamlit 与相关进程。app 只管开不管关 |
| 重启邮差.command | 改过 futu_watcher 里的代码后 |
| 安装富途邮差.command | 第一次装邮差，或它的后台任务丢了 |
| 升级futu-api.command | 富途 SDK 需要跟 OpenD 版本对齐时 |
| 跑富途分析.command | 手动跑一次 skills 下的分析脚本 |
| setup_futu_108.sh | 重装系统或换机后配环境 |
| install.command | 把 skills/ 装进 ~/.claude/skills |

2026-07-31 移走了「打开交易App」和「总控制台」，理由见
`_回收站_20260730/冗余启动器_20260731/移走清单.md`。
""",

    LSTOCK: """# stock/launchers

日常操作在**寻宝猫**里。这里是开关与应急。

| 脚本 | 什么时候用 |
| --- | --- |
| Launch_Trading_Control_Panel.command | **不要移动**：app 首页「启动桌面控制台」按钮直接调用它 |
| Cancel_All_Orders.command | 应急全撤。刻意保持成一个双击就跑的独立文件，不依赖 app 能不能打开 |
| Pregate_Active / LogOnly / Off | 下单前置闸门三档：真拦 / 只记录 / 关闭 |
| Start_All_Day_Auto_Run / Stop_ | 全天自动运行的开与关 |
| Install_Login_Auto_Start / Uninstall_ | 开机自启的装与卸 |
| 重启Dashboard.command | streamlit 卡住时重启，比关了再开快 |
| 跑OFIM研究.command | 跑一整轮 OFIM 研究批处理，结果写 runtime/ofim_research_run.log |

2026-07-31 移走了 4 个只为打开页面而存在的启动器，理由见
`_回收站_20260730/冗余启动器_20260731/移走清单.md`。
""",

    LNEWS: """# news collector/scripts

采集、推送、健康检查、学习**已经由后台任务常驻**，不需要手动启动。
这里剩下的是安装器和功能开关。

| 脚本 | 什么时候用 |
| --- | --- |
| market_news_stack.command | 安装/重装整套后台任务（采集·推送·健康·学习·看板接口）。改过配置后跑一次 |
| market_news_stack_stop.command | 全部停掉 |
| market_news_learning_auto.command / _stop | 单独装/卸学习任务 |
| Enable_Dynamic_Universe / Disable_ | AH 催化板块用扫描器动态生成成分股，还是用静态文件 |
| Enable_Futu_Enrichment / Disable_ | 推送里是否附带富途行情 |
| AH_Multi_Factor_Scanner.command | 手动跑一次 AH 多因子扫描（Enable_Dynamic_Universe 会自动调用） |

想看采集器实时输出，用邮差的 `collectdoctor`，不要双击脚本前台跑——
那会先 pkill 掉常驻任务，然后和 launchd 拉起的新实例撞在一起。

2026-07-31 移走了 8 个（4 个 Codex 遗留 + 看板 + 三个与常驻任务冲突的），理由见
`_回收站_20260730/冗余启动器_20260731/移走清单.md`。
""",
}


def move(src: Path, dst_dir: Path):
    """移动，跨卷时退回复制+删除。返回 (成功, 说明)。"""
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    if dst.exists():
        if dst.is_dir():
            shutil.rmtree(dst, ignore_errors=True)
        else:
            dst.unlink()
    r = subprocess.run(["/bin/mv", str(src), str(dst)], capture_output=True, text=True)
    if r.returncode != 0:
        r2 = subprocess.run(["/bin/cp", "-RP", str(src), str(dst)],
                            capture_output=True, text=True)
        if r2.returncode == 0:
            subprocess.run(["/bin/rm", "-rf", str(src)], capture_output=True)
        else:
            return False, r2.stderr.strip()[:120]
    return not src.exists(), ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    # watcher 给每个技能都追加 --time-range/--language-id/--json，
    # 用 parse_known_args 忽略掉，否则 argparse 直接报错退出。
    args, _ = ap.parse_known_args()

    out = {"kind": "tidy_launchers", "applied": args.apply}
    plan, missing = [], []
    for p, why in MOVE:
        (plan if p.exists() else missing).append(
            {"path": str(p.relative_to(ALL)), "why": why,
             "kb": round(p.stat().st_size / 1024, 1) if p.exists() else None})
    dir_plan = []
    for p, why in MOVE_DIRS:
        if p.exists():
            r = subprocess.run(["du", "-sk", str(p)], capture_output=True, text=True)
            mb = round(int(r.stdout.split()[0]) / 1024) if r.stdout.strip() else None
            dir_plan.append({"path": str(p.relative_to(ALL)), "why": why, "mb": mb})
    out["files_to_move"] = plan
    out["dirs_to_move"] = dir_plan
    out["already_absent"] = [m["path"] for m in missing]
    out["total_files"] = len(plan)

    if not args.apply:
        out["note"] = "这是计划，没有动任何文件。加 --apply 才执行。"
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    moved, failed = [], []
    lines = ["# 移走的启动器 — 2026-07-31", "",
             "全部只是移动，没有删除。想恢复就把文件拖回原位置。", ""]
    for p, why in MOVE:
        if not p.exists():
            continue
        sub = TRASH / p.parent.name
        ok, err = move(p, sub)
        (moved if ok else failed).append(str(p.relative_to(ALL)))
        lines.append(f"- `{p.relative_to(ALL)}`  \n  {why}")
    lines.append("")
    for p, why in MOVE_DIRS:
        if not p.exists():
            continue
        ok, err = move(p, TRASH / "目录")
        (moved if ok else failed).append(str(p.relative_to(ALL)))
        lines.append(f"- `{p.relative_to(ALL)}/`  \n  {why}")

    TRASH.mkdir(parents=True, exist_ok=True)
    (TRASH / "移走清单.md").write_text("\n".join(lines), encoding="utf-8")

    for d, text in README.items():
        if d.exists():
            (d / "README.md").write_text(text, encoding="utf-8")

    out["moved"] = moved
    out["failed"] = failed
    out["trash"] = str(TRASH)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
