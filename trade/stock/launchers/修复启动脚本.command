#!/bin/zsh
# 自动给桌面的启动脚本加上 disown，防止关闭 Terminal 时弹终止对话框

TARGET="$HOME/Desktop/启动量化交易控制台.command"

if [[ ! -f "$TARGET" ]]; then
  osascript -e 'display alert "找不到文件" message "桌面上没有找到「启动量化交易控制台.command」，请手动检查文件名。" as critical'
  exit 1
fi

# 已经有 disown 就不重复加
if grep -q "disown" "$TARGET"; then
  osascript -e 'display alert "已经修复过了" message "启动脚本已包含 disown，无需重复修复。" as informational'
  exit 0
fi

# 在 nohup 那行后面插入 disown $!
sed -i '' '/nohup.*taa_futu.control_panel/a\
disown $!   # detach from shell so Terminal wont show terminate dialog
' "$TARGET"

if grep -q "disown" "$TARGET"; then
  osascript -e 'display alert "修复成功 ✓" message "已添加 disown，下次启动时 Terminal 窗口会自动关闭，不再弹对话框。" as informational'
else
  osascript -e 'display alert "修复失败" message "sed 替换未生效，请手动在 nohup 那行下方加一行：disown $!" as critical'
fi
