#!/bin/bash
# 双击运行：OFIM walk-forward 研究（先 30 秒快速试跑，再全量 A/B）。
# 结果存到 runtime/report_*.json —— 这些文件 Claude 能直接读，你不用复制粘贴。
# 注意：全量会跑挺久（5 月那些 4GB 大日子很吃时间），请让这个窗口一直开着别关。
set -u
cd "$(cd "$(dirname "$0")/../.." && pwd)" || { echo "找不到项目根目录，请确认本启动器仍放在 stock/launchers/ 下"; exit 1; }
PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"
mkdir -p runtime
LOG="runtime/ofim_research_run.log"
echo "OFIM 研究开始 $(date)" | tee "$LOG"
echo "用的 Python: $PY" | tee -a "$LOG"

run() {            # $1 = tag(用于报告文件名)  其余 = 命令行参数
  local tag="$1"; shift
  echo "" | tee -a "$LOG"
  echo "================= $tag : $(date +%H:%M:%S) =================" | tee -a "$LOG"
  "$PY" -m taa_futu.ofim_research_loop "$@" 2>&1 | tee -a "$LOG"
  if cp -f runtime/ofim_research_report.json "runtime/report_${tag}.json" 2>/dev/null; then
    echo ">> 已保存 runtime/report_${tag}.json" | tee -a "$LOG"
  fi
}

# ── 1) 30 秒快速试跑（先确认能跑、拿到初步结果）──────────────────────────────
SANITY="--train 2026-03-13:2026-03-16 --val 2026-03-17:2026-03-18 --test 2026-03-19:2026-03-20 --max-trials 24"
run sanity_A $SANITY
run sanity_B $SANITY --flat-by-close

# ── 2) 全量 A（持隔夜）/ B（收盘平仓）────────────────────────────────────────
FULL="--train 2026-03-11:2026-04-15 --val 2026-04-17:2026-05-02 --test 2026-05-05:2026-05-29 --max-trials 24"
run full_A $FULL
run full_B $FULL --flat-by-close

echo "" | tee -a "$LOG"
echo "================= 全部完成 $(date) =================" | tee -a "$LOG"
echo "结果文件（Claude 可直接读，无需复制）：" | tee -a "$LOG"
echo "  runtime/report_sanity_A.json   runtime/report_sanity_B.json" | tee -a "$LOG"
echo "  runtime/report_full_A.json     runtime/report_full_B.json" | tee -a "$LOG"
echo "" | tee -a "$LOG"
echo "跑完了。回 Claude 说一声「OFIM 跑完了」，我直接读报告给你解读。"
echo "按回车键关闭本窗口。"
read -r _
