#!/usr/bin/env python3
"""
GNOME 快捷键管理：给截图 OCR 工具注册一个**不冲突**的键。

为什么必须动 gsettings
----------------------
Wayland 下普通程序**不能**监听全局键盘（X11 的 XGrabKey 被彻底去掉了），
所以"按快捷键唤起"只能走合成器这条路：在 GNOME 里注册一个自定义快捷键。
这是所有 Wayland 截图/OCR 工具（flameshot、normcap、spectacle）的共同做法。
备选方案是 xdg-desktop-portal 的 GlobalShortcuts 接口，但它需要走一次
用户授权会话，而且键位管理不如 gsettings 可审计、可回滚。

**默认不碰 GNOME 自带的截图键。**
GNOME 的 Print 会打开原生截图 UI，那里有区域、窗口、整屏**和录屏**——
这些能力我们没有、也不该替用户丢掉。所以本工具默认用 <Super><Shift>S，
和原生功能并存：
    Print          → GNOME 原生截图 UI（区域/窗口/整屏/录屏）
    Shift+Print    → GNOME 区域截图
    Alt+Print      → GNOME 窗口截图
    Super+Shift+S  → 本工具（冻结帧 + 框选 + OCR 按钮）
    Super+Shift+Y  → 识别剪贴板里的图片（配合原生截图用）

只有显式传 --take-print 才会把 Print 从原生 UI 上摘下来（值会被备份，可回滚）。

用法：
    keys.py status
    keys.py install [--with-clip-hotkey] [--binding '<Super><Shift>s']
    keys.py install --take-print          # 明确要求占用 Print（不推荐）
    keys.py remove
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"
BACKUP = STATE_DIR / "gnome-keys-backup.json"

MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"
SHELL_KEYS = "org.gnome.shell.keybindings"
SHELL_SCREENSHOT_UI_KEY = "show-screenshot-ui"

#: 默认唤起键。刻意避开 Print，把原生截图 UI（含录屏）留给用户。
DEFAULT_BINDING = "<Super><Shift>s"
DEFAULT_CLIP_BINDING = "<Super><Shift>y"
#: GNOME 原生截图 UI 的出厂键位，用于恢复。
NATIVE_SCREENSHOT_UI = ["Print"]

PATH_PREFIX = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
ENTRY_ID = "prtsc-ocr"
ENTRY_ID_CLIP = "prtsc-ocr-clip"
ENTRY_PATH = f"{PATH_PREFIX}{ENTRY_ID}/"
ENTRY_PATH_CLIP = f"{PATH_PREFIX}{ENTRY_ID_CLIP}/"


def media_settings() -> Gio.Settings:
    return Gio.Settings.new(MEDIA_KEYS)


def shell_settings() -> Gio.Settings:
    return Gio.Settings.new(SHELL_KEYS)


def entry_settings(path: str) -> Gio.Settings:
    return Gio.Settings.new_with_path(CUSTOM_SCHEMA, path)


def get_bindings() -> list[str]:
    return list(media_settings().get_strv("custom-keybindings"))


def set_bindings(paths: list[str]) -> None:
    s = media_settings()
    s.set_strv("custom-keybindings", paths)
    s.sync()


def load_backup() -> dict:
    if BACKUP.exists():
        try:
            return json.loads(BACKUP.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_backup(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_entry(path: str, name: str, command: str, binding: str) -> None:
    s = entry_settings(path)
    s.set_string("name", name)
    s.set_string("command", command)
    s.set_string("binding", binding)
    s.sync()


def cmd_status(_args) -> int:
    paths = get_bindings()
    print("自定义快捷键：")
    for p in paths:
        s = entry_settings(p)
        print(f"  {p}\n    名称={s.get_string('name')!r} 键={s.get_string('binding')!r} 命令={s.get_string('command')!r}")
    if not paths:
        print("  （无）")
    sh = shell_settings()
    print(f"{SHELL_KEYS} {SHELL_SCREENSHOT_UI_KEY} = {list(sh.get_strv(SHELL_SCREENSHOT_UI_KEY))}")
    print(f"备份文件：{BACKUP} {'存在' if BACKUP.exists() else '不存在'}")
    if BACKUP.exists():
        print(f"  原始值：{load_backup()}")
    print(f"我们的条目是否已注册：{ENTRY_PATH in paths}")
    print(f"剪贴板条目是否已注册：{ENTRY_PATH_CLIP in paths}")
    return 0


def cmd_install(args) -> int:
    backup = load_backup()
    sh = shell_settings()

    # GNOME 自带的截图 UI 键：默认原样保留。
    # 但如果之前被旧版本（默认抢占 Print 的那版）清空过，这里顺手恢复回来，
    # 免得用户升级后一直用不上原生的区域/窗口/录屏。
    if args.take_print:
        if "show-screenshot-ui" not in backup:
            backup["show-screenshot-ui"] = list(sh.get_strv(SHELL_SCREENSHOT_UI_KEY))
            save_backup(backup)
            print(f"已备份 {SHELL_SCREENSHOT_UI_KEY} 原值：{backup['show-screenshot-ui']}")
        current = list(sh.get_strv(SHELL_SCREENSHOT_UI_KEY))
        if args.binding in current:
            sh.set_strv(SHELL_SCREENSHOT_UI_KEY, [k for k in current if k != args.binding])
            sh.sync()
            print(f"已解绑 GNOME 自带的 {args.binding} → {SHELL_SCREENSHOT_UI_KEY}（--take-print）")
        if args.binding == "Print":
            print("⚠ 注意：Print 被本工具占用了，GNOME 原生截图 UI（含录屏）不再响应 Print。")
            print("  原生区域/窗口截图仍在：Shift+Print / Alt+Print。")
    else:
        current = list(sh.get_strv(SHELL_SCREENSHOT_UI_KEY))
        wanted = backup.get("show-screenshot-ui", NATIVE_SCREENSHOT_UI)
        if not current and wanted:
            sh.set_strv(SHELL_SCREENSHOT_UI_KEY, wanted)
            sh.sync()
            print(f"已恢复 GNOME 原生截图 UI 键 {SHELL_SCREENSHOT_UI_KEY} = {wanted}")
        elif args.binding in current:
            # 用户把我们的键设成了 Print，但没加 --take-print：拒绝，避免又抢占
            print(f"✗ {args.binding} 是 GNOME 原生截图 UI 的键，本工具不占用它。")
            print(f"  换个键（默认 {DEFAULT_BINDING}），或明确加 --take-print。")
            return 1

    paths = get_bindings()
    command = str(BASE_DIR / "bin" / "prtsc-ocr")
    write_entry(ENTRY_PATH, "截图 OCR（框选 → 提取文字）", command, args.binding)
    if ENTRY_PATH not in paths:
        paths.append(ENTRY_PATH)
        print(f"已注册快捷键 {args.binding} → {command}")

    if args.with_clip_hotkey:
        clip_cmd = str(BASE_DIR / "bin" / "ocr-clip")
        write_entry(ENTRY_PATH_CLIP, "识别剪贴板图片", clip_cmd, args.clip_binding)
        if ENTRY_PATH_CLIP not in paths:
            paths.append(ENTRY_PATH_CLIP)
            print(f"已注册快捷键 {args.clip_binding} → {clip_cmd}")

    # 去重后再写回，避免重复 install 把同一个条目塞两遍
    seen, deduped = set(), []
    for p in paths:
        if p not in seen:
            seen.add(p)
            deduped.append(p)
    set_bindings(deduped)
    return 0


def cmd_remove(_args) -> int:
    paths = [p for p in get_bindings() if p not in (ENTRY_PATH, ENTRY_PATH_CLIP)]
    set_bindings(paths)

    backup = load_backup()
    if "show-screenshot-ui" in backup:
        sh = shell_settings()
        sh.set_strv(SHELL_SCREENSHOT_UI_KEY, backup["show-screenshot-ui"])
        sh.sync()
        print(f"已恢复 {SHELL_SCREENSHOT_UI_KEY} = {backup['show-screenshot-ui']}")
    else:
        print("没有备份，跳过 GNOME 截图键恢复")

    # 清空我们写过的条目，避免残留
    for path in (ENTRY_PATH, ENTRY_PATH_CLIP):
        try:
            s = entry_settings(path)
            for key in ("name", "command", "binding"):
                s.reset(key)
            s.sync()
        except Exception:  # noqa: BLE001
            pass
    print("已移除截图 OCR 的快捷键")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="配置/回滚 GNOME 快捷键")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_status = sub.add_parser("status", help="查看当前快捷键状态")
    p_status.set_defaults(func=cmd_status)

    p_install = sub.add_parser("install", help="注册一个不与 GNOME 原生截图冲突的键")
    p_install.add_argument("--binding", default=DEFAULT_BINDING,
                           help=f"唤起截图 OCR 的键（默认 {DEFAULT_BINDING}）")
    p_install.add_argument("--with-clip-hotkey", action="store_true", help="额外注册剪贴板图片识别快捷键")
    p_install.add_argument("--clip-binding", default=DEFAULT_CLIP_BINDING, help="剪贴板识别的键")
    p_install.add_argument("--take-print", action="store_true",
                           help="明确要求占用 Print（会顶掉 GNOME 原生截图 UI，含录屏；不推荐）")
    p_install.set_defaults(func=cmd_install)

    p_remove = sub.add_parser("remove", help="回滚到安装前")
    p_remove.set_defaults(func=cmd_remove)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
