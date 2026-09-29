#!/usr/bin/env bash
#
# prtsc-ocr 一键安装（全程不需要 sudo，所有东西都在用户目录里）
#
#   ./install.sh                      安装服务 + 快捷方式（原生截图键一概不动）
#   ./install.sh --with-extension     装 Shell 扩展：在原生截图 UI 上加「提取文字」按钮
#   ./install.sh --with-clip-hotkey   绑定 Super+Shift+Y = 识别剪贴板图片
#   ./install.sh --no-keybinding      只装服务，不动快捷键
#   ./install.sh --binding '<Super>o' 换掉备用覆盖层的键（默认 <Super><Shift>s）
#   ./install.sh --take-print         占用 Print（会顶掉 GNOME 原生截图 UI 和录屏，不推荐）
#
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$BASE_DIR/.venv"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
DESKTOP_DIR="$HOME/.local/share/applications"
UNIT_NAME="prtsc-ocr.service"

WITH_CLIP_HOTKEY=0
WITH_EXTENSION=0
DO_KEYBINDING=1
TAKE_PRINT=0
BINDING="<Super><Shift>s"   # 刻意不用 Print：把原生截图 UI（含录屏）留给用户
CLIP_BINDING="<Super><Shift>y"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-extension)   WITH_EXTENSION=1; shift ;;
    --with-clip-hotkey) WITH_CLIP_HOTKEY=1; shift ;;
    --no-keybinding)    DO_KEYBINDING=0; shift ;;
    --binding)          BINDING="$2"; shift 2 ;;
    --clip-binding)     CLIP_BINDING="$2"; shift 2 ;;
    --take-print)       TAKE_PRINT=1; shift ;;
    -h|--help)          sed -n '3,11p' "$0"; exit 0 ;;
    *) echo "未知参数：$1" >&2; exit 2 ;;
  esac
done

c_ok=$'\033[32m'; c_info=$'\033[36m'; c_warn=$'\033[33m'; c_err=$'\033[31m'; c_off=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$c_info" "$*" "$c_off"; }
ok()   { printf '    %s✓%s %s\n' "$c_ok" "$c_off" "$*"; }
warn() { printf '    %s!%s %s\n' "$c_warn" "$c_off" "$*"; }
die()  { printf '\n%s✗ %s%s\n' "$c_err" "$*" "$c_off" >&2; exit 1; }

# ---------------------------------------------------------------------------
step "1/7 环境检查"
[[ $EUID -ne 0 ]] || die "请以桌面用户身份运行，不要用 sudo/root（这套东西全在用户目录里）"
[[ -n "${WAYLAND_DISPLAY:-}" ]] || warn "当前不是 Wayland 会话；截图走的是 portal，X11 下也能用，但本项目针对 GNOME Wayland 调的"

PY_BIN="$(command -v python3 || true)"
[[ -n "$PY_BIN" ]] || die "找不到 python3"
"$PY_BIN" - <<'PY' || die "系统 python3 缺少 PyGObject/GTK4（Ubuntu 桌面版自带，若是最小安装请装 python3-gi gir1.2-gtk-4.0）"
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gdk, Gio, Graphene  # noqa: F401
PY
ok "系统 python3 + GTK4 + Gio 就绪：$PY_BIN ($($PY_BIN -V 2>&1))"

for tool in notify-send wl-copy wl-paste; do
  command -v "$tool" >/dev/null && ok "$tool 存在" || warn "$tool 缺失，相关功能会降级"
done

if ! command -v gnome-shell >/dev/null; then
  warn "没检测到 gnome-shell，快捷键注册步骤可能不适用"
fi

# ---------------------------------------------------------------------------
step "2/7 准备隔离的 Python 运行环境（uv，不需要 sudo）"
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null 2>&1 || die "uv 安装失败（需要联网，或用代理）"
fi
ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"

if [[ ! -x "$VENV/bin/python" ]]; then
  uv venv --python 3.12 "$VENV" >/dev/null 2>&1 || die "创建 venv 失败"
fi
ok "虚拟环境：$VENV ($("$VENV/bin/python" -V 2>&1))"

if "$VENV/bin/python" -c "import rapidocr, onnxruntime" >/dev/null 2>&1; then
  ok "OCR 依赖已安装（rapidocr + onnxruntime）"
else
  echo "    正在安装 rapidocr + onnxruntime（约 150MB，首次会慢一点）…"
  uv pip install --python "$VENV/bin/python" rapidocr onnxruntime >/dev/null 2>&1 \
    || die "依赖安装失败"
  ok "OCR 依赖安装完成"
fi

# ---------------------------------------------------------------------------
step "3/7 注册常驻后台服务（systemd 用户单元，开机/登录自启）"
mkdir -p "$SYSTEMD_USER_DIR" "$BASE_DIR/logs"
sed "s|@BASE_DIR@|$BASE_DIR|g" "$BASE_DIR/config/prtsc-ocr.service.in" > "$SYSTEMD_USER_DIR/$UNIT_NAME"
ok "写入 $SYSTEMD_USER_DIR/$UNIT_NAME"

chmod +x "$BASE_DIR/bin/"* "$BASE_DIR/lib/"*.py 2>/dev/null || true

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME" >/dev/null 2>&1 || die "systemd 服务启动失败，用 'bin/ocrd log' 看日志"
ok "服务已启用并启动"

echo "    等待模型加载…"
for _ in $(seq 1 120); do
  # 等的是 warm 而不是 ready：ready 只代表模型加载了，
  # warm 才代表真实尺寸的首次推理也跑过了，第一枪才不会慢。
  if "$VENV/bin/python" -c "
import sys; sys.path.insert(0, '$BASE_DIR/lib')
import ocr_client
info = ocr_client.ping(autostart=False)
sys.exit(0 if info.get('warm') else 1)
" 2>/dev/null; then break; fi
  sleep 0.5
done
"$VENV/bin/python" -c "
import sys; sys.path.insert(0, '$BASE_DIR/lib')
import ocr_client
i = ocr_client.ping(autostart=False)
print(f\"    引擎 {i.get('engine')} · pid {i.get('pid')} · 已预热={i.get('warm')}\")
" || true

# ---------------------------------------------------------------------------
step "4/7 安装应用入口（应用列表里可搜索「截图 OCR」）"
mkdir -p "$DESKTOP_DIR"
sed "s|@BASE_DIR@|$BASE_DIR|g" "$BASE_DIR/config/prtsc-ocr.desktop.in" > "$DESKTOP_DIR/prtsc-ocr.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
ok "写入 $DESKTOP_DIR/prtsc-ocr.desktop"

# ---------------------------------------------------------------------------
step "5/7 安装 GNOME Shell 扩展（原生截图 UI 上的「提取文字」按钮）"
if [[ "$WITH_EXTENSION" == "1" ]]; then
  if "$BASE_DIR/bin/install-extension" install; then
    ok "扩展已装；首次安装需要注销重登一次才会加载（Wayland 限制）"
  else
    warn "扩展安装失败，原生截图功能不受影响；可稍后重跑 ./bin/install-extension"
  fi
else
  warn "已跳过（未加 --with-extension）；原生截图工具不会有「提取文字」按钮"
fi

# ---------------------------------------------------------------------------
step "6/7 绑定快捷键"
if [[ "$DO_KEYBINDING" == "1" ]]; then
  args=(install --binding "$BINDING")
  [[ "$TAKE_PRINT" == "1" ]] && args+=(--take-print)
  [[ "$WITH_CLIP_HOTKEY" == "1" ]] && args+=(--with-clip-hotkey --clip-binding "$CLIP_BINDING")
  "$PY_BIN" "$BASE_DIR/lib/keys.py" "${args[@]}"
  ok "$BINDING → 备用覆盖层截图工具（GNOME 原生截图键未被改动）"
  [[ "$WITH_CLIP_HOTKEY" == "1" ]] && ok "$CLIP_BINDING → 识别剪贴板图片"
else
  warn "已跳过（--no-keybinding）"
fi

# ---------------------------------------------------------------------------
step "7/7 自检"
"$BASE_DIR/bin/ocrd" status || true

TEST_IMG="$BASE_DIR/samples/test_mixed.png"
if [[ -f "$TEST_IMG" ]]; then
  echo
  echo "    跑一张样图验证端到端："
  "$BASE_DIR/bin/ocr" "$TEST_IMG" --quiet 2>/dev/null | sed 's/^/      /' || warn "识别失败，看 bin/ocrd log"
fi

cat <<EOF

$(printf '%s' "$c_ok")安装完成。$(printf '%s' "$c_off")

  GNOME 原生截图键一概没动：Print / Shift+Print / Alt+Print 都是出厂设置。
EOF

if [[ "$WITH_EXTENSION" == "1" ]]; then
cat <<EOF

  · 【首次需要注销重登一次】重登后按 Print 打开 GNOME 截图工具，
    工具栏上会多出第四个按钮「提取文字」：框选 → 点它 → 文字进剪贴板。
    验证：gnome-extensions info prtsc-ocr@peppapig   （状态应为 ACTIVE）
EOF
fi

cat <<EOF
  · 备用覆盖层：按 ${BINDING} → 拖拽框选 → 点「提取文字」（不依赖扩展）
EOF
if [[ "$WITH_CLIP_HOTKEY" == "1" ]]; then
cat <<EOF
  · 剪贴板图片识别：按 ${CLIP_BINDING}（配合 Shift+Print / Alt+Print 很好用）
EOF
fi

cat <<EOF
  · 命令行：  $BASE_DIR/bin/ocr 图片.png
  · 服务管理：$BASE_DIR/bin/ocrd {status|restart|log}
  · 回滚：    $BASE_DIR/uninstall.sh

EOF
