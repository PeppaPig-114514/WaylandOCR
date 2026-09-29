#!/usr/bin/env bash
#
# prtsc-ocr 卸载 / 回滚
#
#   ./uninstall.sh            停服务、删入口、恢复 GNOME 截图快捷键（保留 venv 以便重装）
#   ./uninstall.sh --purge    连虚拟环境和日志一起删掉（脚本目录本身保留）
#
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
DESKTOP_DIR="$HOME/.local/share/applications"
UNIT_NAME="prtsc-ocr.service"
PURGE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --purge) PURGE=1; shift ;;
    -h|--help) sed -n '3,9p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

c_info=$'\033[36m'; c_ok=$'\033[32m'; c_off=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$c_info" "$*" "$c_off"; }
ok()   { printf '    %s✓%s %s\n' "$c_ok" "$c_off" "$*"; }

step "1/4 停止并移除后台服务"
systemctl --user disable --now "$UNIT_NAME" >/dev/null 2>&1 || true
rm -f "$SYSTEMD_USER_DIR/$UNIT_NAME"
systemctl --user daemon-reload || true
ok "服务已移除"

step "2/4 移除应用入口"
rm -f "$DESKTOP_DIR/prtsc-ocr.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
ok "应用入口已移除"

step "3/5 卸载 Shell 扩展"
"$BASE_DIR/bin/install-extension" --remove || true

step "4/5 恢复 GNOME 快捷键"
python3 "$BASE_DIR/lib/keys.py" remove || true

step "5/5 清理"
rm -f "${XDG_RUNTIME_DIR:-/tmp}/prtsc-ocr.sock"
if [[ "$PURGE" == "1" ]]; then
  rm -rf "$BASE_DIR/.venv" "$BASE_DIR/logs" "$BASE_DIR/tmp"
  ok "已删除虚拟环境与日志（重新安装会再次下载依赖）"
else
  ok "保留了 .venv 和 logs（加 --purge 可一并删除）"
fi

cat <<EOF

回滚完成。整个项目目录（含源码、样例、文档）仍在：
  $BASE_DIR
确认不需要后，直接 rm -rf "$BASE_DIR" 即可，系统里没有留下任何其它痕迹。

EOF
