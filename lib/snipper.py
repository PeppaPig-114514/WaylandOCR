#!/usr/bin/env python3
"""
prtsc-ocr 截图工具 —— 仿 Win11 截图工具，但多了一个 OCR 按钮。

交互流程
--------
1. 按下 PrtSc（或运行 `prtsc-ocr`）→ 通过 portal 静默抓下整屏（约 0.5s）。
2. 把这张"冻结帧"铺满全屏，用户拖拽框选区域（不会拍到工具自身，因为
   截屏发生在窗口出现之前——这也是冻结帧方案相对"先开窗口再截图"的优势）。
3. 底部悬浮工具栏：复制图片 / **提取文字** / 保存 / 全屏 / 取消。
   点「提取文字」→ 只把框选的那块像素交给常驻 OCR 守护进程 → 文字进剪贴板
   → 通知 + 结果面板（可核对、可改）。

为什么不用 GNOME 自带截图 UI
---------------------------
GNOME 50 的截图 UI 是 gnome-shell 内部实现，没有插件点可挂按钮；而
org.gnome.Shell.Screenshot D-Bus 接口对普通程序返回 AccessDenied。
所以"加一个 OCR 按钮"这件事，只能由我们自己画这个覆盖层来实现。

运行环境：系统 python3（需要 python3-gi / GTK4 / libadwaita），
OCR 部分通过 UNIX socket 交给 ~/PrtScOCR/.venv 里的守护进程。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")

from gi.repository import Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402

LIB_DIR = Path(__file__).resolve().parent
BASE_DIR = LIB_DIR.parent
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

import ocr_client  # noqa: E402  (本地模块，纯标准库)
from capture import cleanup_capture, grab_screen, identity_hint  # noqa: E402


def log(msg: str) -> None:
    """写一行诊断日志。

    被 PrtSc 快捷键唤起时 stderr 只会进 journal、很容易被忽略，而"抓屏失败"这类
    问题恰恰只在那个上下文里出现（app-id / 焦点相关）。所以额外落一份文件，
    出问题直接看 logs/snipper.log 就行。
    """
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(f"[prtsc-ocr] {msg}", file=sys.stderr)
    try:
        logdir = BASE_DIR / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        with (logdir / "snipper.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass  # 日志失败绝不能影响主流程


log(f"启动 pid={os.getpid()} args={sys.argv[1:]} {identity_hint()}")

APP_ID = "org.prtscocr.Snipper"
CSS = b"""
.toolbar {
  background: rgba(20, 20, 22, 0.86);
  border-radius: 16px;
  padding: 7px;
  border: 1px solid rgba(255, 255, 255, 0.10);
}
.toolbar button {
  color: #f2f2f2;
  background: transparent;
  border: none;
  border-radius: 10px;
  padding: 7px 13px;
  box-shadow: none;
  font-size: 14px;
}
.toolbar button:hover { background: rgba(255, 255, 255, 0.14); }
.toolbar button:active { background: rgba(255, 255, 255, 0.22); }
button.ocr-button {
  background: #3584e4;
  color: #ffffff;
  font-weight: bold;
  padding: 7px 18px;
}
button.ocr-button:hover { background: #4a90e2; }
button.ocr-button:disabled { background: rgba(53, 132, 228, 0.45); }
.badge {
  background: rgba(20, 20, 22, 0.86);
  color: #ffffff;
  border-radius: 7px;
  padding: 3px 9px;
  font-size: 12px;
  border: 1px solid rgba(255, 255, 255, 0.10);
}
.hint {
  background: rgba(20, 20, 22, 0.72);
  color: #eaeaea;
  border-radius: 8px;
  padding: 6px 14px;
  font-size: 13px;
}
.result-text { font-size: 14px; }
.result-meta { color: #6a6a6a; font-size: 12px; }
"""


# ---------------------------------------------------------------------------
# 画布：冻结帧 + 框选遮罩
# ---------------------------------------------------------------------------

class ShotView(Gtk.Widget):
    """整屏冻结帧 + 框选矩形（框外压暗、框线高亮）。"""

    __gtype_name__ = "PrtscOcrShotView"

    def __init__(self, texture: Gdk.Texture, on_change=None):
        super().__init__()
        self._texture = texture
        self._sel: tuple[float, float, float, float] | None = None
        self._on_change = on_change
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_cursor_from_name("crosshair")

    # -- 状态 -------------------------------------------------------------
    @property
    def texture(self) -> Gdk.Texture:
        return self._texture

    @property
    def selection(self):
        return self._sel

    def clear_selection(self):
        self._sel = None
        self.queue_draw()
        if self._on_change:
            self._on_change(None)

    def set_selection(self, x, y, w, h):
        if w < 1 or h < 1:
            return
        self._sel = (float(x), float(y), float(w), float(h))
        self.queue_draw()
        if self._on_change:
            self._on_change(self._sel)

    def select_all(self):
        self.set_selection(0, 0, self.get_width(), self.get_height())

    # -- 坐标换算：控件坐标 -> 图片像素坐标 --------------------------------
    def fitted_geometry(self):
        """纹理等比居中铺放后的 (offset_x, offset_y, scale)。

        正常情况下冻结帧尺寸等于屏幕尺寸，scale 恰好是 1；但多显示器、
        分数缩放或 `--image` 调试时两者可能不等，所以统一等比适配。
        绝不能拉伸——拉伸会让"框到的像素"和"送去识别的像素"对不上。
        """
        iw = max(self._texture.get_width(), 1)
        ih = max(self._texture.get_height(), 1)
        vw = max(self.get_width(), 1)
        vh = max(self.get_height(), 1)
        scale = min(vw / iw, vh / ih)
        return (vw - iw * scale) / 2.0, (vh - ih * scale) / 2.0, scale

    def to_image_rect(self, sel=None):
        sel = sel or self._sel
        if not sel:
            return None
        ox, oy, scale = self.fitted_geometry()
        iw = self._texture.get_width()
        ih = self._texture.get_height()

        x, y, w, h = sel
        ix = (x - ox) / scale
        iy = (y - oy) / scale
        iw2 = w / scale
        ih2 = h / scale

        ix = min(max(ix, 0.0), float(iw))
        iy = min(max(iy, 0.0), float(ih))
        iw2 = min(iw2, iw - ix)
        ih2 = min(ih2, ih - iy)
        if iw2 < 1 or ih2 < 1:
            return None
        return (int(round(ix)), int(round(iy)), max(1, int(round(iw2))), max(1, int(round(ih2))))

    # -- 绘制 -------------------------------------------------------------
    def do_snapshot(self, snapshot: Gtk.Snapshot):
        w = self.get_width()
        h = self.get_height()
        full = Graphene.Rect().init(0, 0, w, h)
        ox, oy, scale = self.fitted_geometry()
        snapshot.append_texture(
            self._texture,
            Graphene.Rect().init(
                ox, oy, self._texture.get_width() * scale, self._texture.get_height() * scale
            ),
        )

        sel = self._sel
        if not sel:
            # 未框选时整体轻微压暗，突出"这是冻结画面、可框选"
            dim = Gdk.RGBA()
            dim.parse("rgba(0,0,0,0.28)")
            snapshot.append_color(dim, full)
            self._draw_hint_guides(snapshot, w, h)
            return

        x, y, sw, sh = sel
        shade = Gdk.RGBA()
        shade.parse("rgba(0,0,0,0.55)")
        strips = [
            (0, 0, w, y),
            (0, y + sh, w, h - (y + sh)),
            (0, y, x, sh),
            (x + sw, y, w - (x + sw), sh),
        ]
        for rx, ry, rw, rh in strips:
            if rw > 0 and rh > 0:
                snapshot.append_color(shade, Graphene.Rect().init(rx, ry, rw, rh))

        accent = Gdk.RGBA()
        accent.parse("#3584e4")
        t = 2.0
        for rx, ry, rw, rh in [
            (x, y, sw, t),
            (x, y + sh - t, sw, t),
            (x, y, t, sh),
            (x + sw - t, y, t, sh),
        ]:
            snapshot.append_color(accent, Graphene.Rect().init(rx, ry, rw, rh))

        handle = Gdk.RGBA()
        handle.parse("#ffffff")
        hs = 5.0
        for cx, cy in [
            (x, y),
            (x + sw, y),
            (x, y + sh),
            (x + sw, y + sh),
            (x + sw / 2, y),
            (x + sw / 2, y + sh),
            (x, y + sh / 2),
            (x + sw, y + sh / 2),
        ]:
            snapshot.append_color(
                handle, Graphene.Rect().init(cx - hs / 2, cy - hs / 2, hs, hs)
            )

    def _draw_hint_guides(self, snapshot: Gtk.Snapshot, w: float, h: float):
        """三分线，帮助对齐。"""
        line = Gdk.RGBA()
        line.parse("rgba(255,255,255,0.16)")
        for i in (1, 2):
            snapshot.append_color(
                line, Graphene.Rect().init(w * i / 3, 0, 1, h)
            )
            snapshot.append_color(
                line, Graphene.Rect().init(0, h * i / 3, w, 1)
            )


# ---------------------------------------------------------------------------
# OCR 结果面板
# ---------------------------------------------------------------------------

class ResultPanel(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, text: str, meta: str, copy_cb):
        super().__init__(application=app, title="识别结果 · 截图 OCR")
        self._copy_cb = copy_cb
        self.set_default_size(620, 420)

        header = Gtk.HeaderBar()
        self.set_titlebar(header)
        copy_btn = Gtk.Button(label="复制")
        copy_btn.add_css_class("suggested-action")
        copy_btn.connect("clicked", lambda *_: self._copy_cb(self.buffer_text()))
        header.pack_start(copy_btn)
        single_btn = Gtk.Button(label="合并为单行")
        single_btn.set_tooltip_text("把多行合并成一行，适合粘贴进搜索框")
        single_btn.connect("clicked", lambda *_: self._copy_cb(" ".join(self.buffer_text().split())))
        header.pack_start(single_btn)
        close_btn = Gtk.Button(label="关闭")
        close_btn.connect("clicked", lambda *_: self.close())
        header.pack_end(close_btn)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)

        self.view = Gtk.TextView()
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.get_buffer().set_text(text)
        self.view.add_css_class("result-text")
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.set_child(self.view)
        sw.add_css_class("card")
        box.append(sw)

        label = Gtk.Label(label=meta, xalign=0)
        label.add_css_class("result-meta")
        box.append(label)

        self.set_child(box)
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

    def buffer_text(self) -> str:
        buf = self.view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _on_key(self, _c, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------

class Snipper(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, png: bytes, texture: Gdk.Texture,
                 preselect=None, auto_ocr: bool = False):
        super().__init__(application=app)
        # 截图以字节形式常驻内存：portal 落盘的那份文件在读进来之后就删掉，
        # 这样 ~/图片 不会被我们的截图刷屏，进程被强杀也不会留垃圾。
        self._png = png
        self._busy = False
        self._preselect = preselect
        self._auto_ocr = auto_ocr

        self.set_decorated(False)
        self.set_resizable(False)
        self.set_title("截图 OCR")
        self.add_css_class("snipper")

        self.view = ShotView(texture, on_change=self._on_selection_change)

        self.size_badge = Gtk.Label(label="")
        self.size_badge.add_css_class("badge")
        self.size_badge.set_visible(False)

        self.hint = Gtk.Label(label="拖拽框选要识别的区域　·　点「提取文字」后文字自动进剪贴板")
        self.hint.add_css_class("hint")

        self.ocr_btn = self._button("insert-text-symbolic", "提取文字", "win.ocr", primary=True)
        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        toolbar.add_css_class("toolbar")
        for w in (
            self._button("edit-copy-symbolic", "复制图片", "win.copy"),
            self.ocr_btn,
            self._button("document-save-symbolic", "保存", "win.save"),
            self._button("view-fullscreen-symbolic", "全屏", "win.select-all"),
        ):
            toolbar.append(w)
        sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        sep.set_margin_top(6)
        sep.set_margin_bottom(6)
        toolbar.append(sep)
        toolbar.append(self.spinner)
        toolbar.append(self._button("window-close-symbolic", "取消", "win.quit"))

        self.overlay = Gtk.Overlay()
        self.overlay.set_child(self.view)
        self.overlay.add_overlay(self.hint)
        self.overlay.add_overlay(self.size_badge)
        self.overlay.add_overlay(toolbar)

        self.hint.set_halign(Gtk.Align.CENTER)
        self.hint.set_valign(Gtk.Align.START)
        self.hint.set_margin_top(28)

        self.size_badge.set_halign(Gtk.Align.START)
        self.size_badge.set_valign(Gtk.Align.START)

        toolbar.set_halign(Gtk.Align.CENTER)
        toolbar.set_valign(Gtk.Align.END)
        toolbar.set_margin_bottom(46)

        self.set_child(self.overlay)
        self._install_actions(app)
        self._install_gestures()
        self._install_keys()

        if self._preselect or self._auto_ocr:
            # 测试/脚本用：必须等控件真正拿到分配尺寸再算坐标，
            # 固定延时不可靠（全屏切换的耗时随合成器而变）。
            self._map_tries = 0
            GLib.timeout_add(80, self._after_first_layout)

    def _after_first_layout(self):
        if self.view.get_width() < 2 or self.view.get_height() < 2:
            self._map_tries += 1
            if self._map_tries > 80:  # ~6.4s
                print("[prtsc-ocr] 窗口尺寸始终为 0，放弃预置操作", file=sys.stderr)
                return False
            return True

        if self._preselect:
            self._apply_preselect()
        if self._auto_ocr:
            self.run_ocr()
        return False

    def _apply_preselect(self):
        """--select 给的是**图片像素**坐标，这里换算成控件坐标再套用。"""
        ix, iy, iw, ih = self._preselect
        ox, oy, scale = self.view.fitted_geometry()
        self.hint.set_visible(False)
        self.view.set_selection(ox + ix * scale, oy + iy * scale, iw * scale, ih * scale)

    # -- 构件 -------------------------------------------------------------
    def _button(self, icon: str, label: str, action: str, primary: bool = False) -> Gtk.Button:
        btn = Gtk.Button()
        btn.set_action_name(action)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        display = self.get_display() or Gdk.Display.get_default()
        if display is not None and Gtk.IconTheme.get_for_display(display).has_icon(icon):
            row.append(Gtk.Image.new_from_icon_name(icon))
        row.append(Gtk.Label(label=label))
        btn.set_child(row)
        if primary:
            btn.add_css_class("ocr-button")
        return btn

    def _install_actions(self, app: Gtk.Application):
        def add(name, cb):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", cb)
            self.add_action(act)

        add("copy", lambda *_: self.copy_image())
        add("ocr", lambda *_: self.run_ocr())
        add("save", lambda *_: self.save_image())
        add("select-all", lambda *_: self.view.select_all())
        add("quit", lambda *_: self.close())

        app.set_accels_for_action("win.copy", ["<Control>c"])
        app.set_accels_for_action("win.ocr", ["<Control>t", "Return", "KP_Enter"])
        app.set_accels_for_action("win.save", ["<Control>s"])
        app.set_accels_for_action("win.select-all", ["<Control>a"])
        app.set_accels_for_action("win.quit", ["Escape"])

    def _install_gestures(self):
        drag = Gtk.GestureDrag.new()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.view.add_controller(drag)

        click = Gtk.GestureClick.new()
        click.connect("released", self._on_click)
        self.view.add_controller(click)

    def _install_keys(self):
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_key)
        self.add_controller(key)

    # -- 交互 -------------------------------------------------------------
    def _drag_begin(self, _g, x, y):
        self._drag_origin = (x, y)
        self.hint.set_visible(False)

    def _drag_update(self, _g, ox, oy):
        x0, y0 = getattr(self, "_drag_origin", (0, 0))
        x, y = x0 + ox, y0 + oy
        self.view.set_selection(min(x0, x), min(y0, y), abs(ox), abs(oy))

    def _drag_end(self, _g, ox, oy):
        if abs(ox) < 4 and abs(oy) < 4:
            self.view.clear_selection()
            self.hint.set_visible(True)
            return
        # 先点了「提取文字」再来框选：松手即识别（对应"点 OCR 按钮再划定区域"）
        if getattr(self, "_ocr_mode", False):
            self._ocr_mode = False
            self.view.set_cursor_from_name("default")
            self.run_ocr()

    def _on_click(self, _g, n_press, _x, _y):
        if n_press >= 2:
            self.view.select_all()

    def _on_key(self, _c, keyval, _code, _state):
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def _on_selection_change(self, sel):
        if not sel:
            self.size_badge.set_visible(False)
            return
        x, y, w, h = sel
        r = self.view.to_image_rect(sel)
        self.size_badge.set_label(f"{r[2]} × {r[3]} px")
        self.size_badge.set_visible(True)
        bx = min(max(x, 8), max(self.get_width() - 130, 8))
        by = y + h + 8
        if by > self.get_height() - 60:
            by = max(y - 34, 8)
        self.size_badge.set_margin_start(int(bx))
        self.size_badge.set_margin_top(int(by))

    # -- 动作 -------------------------------------------------------------
    def copy_image(self):
        rect = self.view.to_image_rect()
        if not rect:
            self._notify("请先框选区域", "拖拽鼠标选择要复制的范围")
            return
        try:
            data = self._crop_png(rect)
            _set_clipboard_image(data)
            self._notify("图片已复制", f"{rect[2]} × {rect[3]} px")
        except Exception as exc:  # noqa: BLE001
            self._notify("复制失败", str(exc))
        self.close()

    def save_image(self):
        rect = self.view.to_image_rect() or (0, 0, self.view.texture.get_width(), self.view.texture.get_height())
        data = self._crop_png(rect)

        def on_done(dialog, result):
            try:
                gfile = dialog.save_finish(result)
            except GLib.Error:
                return  # 用户取消：留在覆盖层里继续操作
            if not gfile:
                return
            path = gfile.get_path()
            try:
                if path:
                    Path(path).write_bytes(data)
                else:
                    # 非本地路径（例如 portal 文档后端给出的 URI）
                    gfile.replace_contents(
                        data, None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None
                    )
                self._notify("已保存", path or gfile.get_uri())
            except Exception as exc:  # noqa: BLE001
                self._notify("保存失败", str(exc))
            self.close()

        dialog = Gtk.FileDialog()
        dialog.set_initial_name(time.strftime("Screenshot-%Y%m%d-%H%M%S.png"))
        dialog.save(self, None, on_done)

    def run_ocr(self):
        if self._busy:
            return
        rect = self.view.to_image_rect()
        if rect is None or rect[2] < 3 or rect[3] < 3:
            # 没有框选就点「提取文字」：进入 OCR 模式，等用户框完自动识别。
            # 这样两种顺序都成立——先框后点，或先点后框。
            if not getattr(self, "_ocr_mode", False):
                self._ocr_mode = True
                self.view.set_cursor_from_name("crosshair")
                self.hint.set_label("现在拖拽框选要识别文字的区域，松开鼠标立即识别　·　Esc 取消")
                self.hint.set_visible(True)
            return
        self._set_busy(True)
        threading.Thread(target=self._ocr_worker, args=(rect,), daemon=True).start()

    def _ocr_worker(self, rect):
        t0 = time.perf_counter()
        try:
            # 只把框选的那块像素编码成 PNG 发给守护进程，
            # 全屏图不进 socket，传输量最小。
            res = ocr_client.ocr_bytes(self._crop_png(rect))
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._ocr_failed, str(exc))
            return
        GLib.idle_add(self._ocr_done, res, (time.perf_counter() - t0) * 1000.0)

    def _ocr_done(self, res, wall_ms):
        self._set_busy(False)
        if not res.get("ok"):
            self._notify("识别失败", str(res.get("error")))
            return
        text = (res.get("text") or "").strip()
        if not text:
            self._notify("没有识别到文字", "换一块更清晰、包含文字的区域再试")
            return

        _set_clipboard_text(text)
        n_lines = len(res.get("lines") or [])
        self._notify(
            "文字已复制到剪贴板",
            text if len(text) <= 120 else text[:117] + "…",
        )

        if os.environ.get("PRTSC_OCR_PANEL", "1") == "1":
            try:
                engine = ocr_client.ping().get("engine", "?")
            except Exception:  # noqa: BLE001 - 面板信息不该因为探测失败而炸掉
                engine = "?"
            size = res.get("image_size") or ["?", "?"]
            meta = (
                f"{n_lines} 行 · {len(text)} 字 · 识别 {res.get('ms', 0):.0f}ms"
                f"（端到端 {wall_ms:.0f}ms）· 区域 {size[0]}×{size[1]} · 引擎 {engine}"
            )
            panel = ResultPanel(self.get_application(), text, meta, self._copy_text)
            panel.present()
        self.close()

    def _ocr_failed(self, message):
        self._set_busy(False)
        self._notify("OCR 服务不可用", message)

    def _copy_text(self, text):
        _set_clipboard_text(text)
        self._notify("已复制", text[:80])

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.spinner.set_visible(busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.ocr_btn.set_sensitive(not busy)

    # -- 工具 -------------------------------------------------------------
    def _crop_png(self, rect) -> bytes:
        import io as _io

        from PIL import Image

        im = Image.open(_io.BytesIO(self._png)).convert("RGB")
        x, y, w, h = rect
        im = im.crop((x, y, x + w, y + h))
        buf = _io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue()

    def _notify(self, title: str, body: str):
        try:
            subprocess.Popen(
                ["notify-send", "-a", "截图 OCR", "-i", "insert-text", title, body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass

    def do_close_request(self) -> bool:
        self._png = b""
        return False

    def close(self):
        self._png = b""
        super().close()


# ---------------------------------------------------------------------------
# 剪贴板（优先 wl-copy：应用退出后内容依然由 wl-copy 进程托管）
# ---------------------------------------------------------------------------

def _set_clipboard_text(text: str) -> None:
    try:
        subprocess.run(["wl-copy", "--type", "text/plain;charset=utf-8"],
                       input=text.encode("utf-8"), check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    except (OSError, subprocess.SubprocessError):
        pass
    clip = Gdk.Display.get_default().get_clipboard()
    clip.set(text)


def _set_clipboard_image(png: bytes) -> None:
    try:
        subprocess.run(["wl-copy", "--type", "image/png"], input=png, check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    except (OSError, subprocess.SubprocessError):
        pass
    texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
    Gdk.Display.get_default().get_clipboard().set_texture(texture)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def load_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


class SnipperApp(Gtk.Application):
    def __init__(self, image: str | None = None, preselect=None, auto_ocr: bool = False):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self._image = image
        self._preselect = preselect
        self._auto_ocr = auto_ocr
        self._win = None

    def do_activate(self):
        if self._win is not None:
            return
        load_css()
        owns_capture = self._image is None
        try:
            if owns_capture:
                path = grab_screen()
            else:
                path = Path(self._image)
            png = path.read_bytes()
        except Exception as exc:  # noqa: BLE001
            log(f"抓屏失败：{exc}")
            subprocess.Popen(
                ["notify-send", "-a", "截图 OCR", "-u", "critical",
                 "抓屏失败", f"{exc}\n\n详情见 ~/PrtScOCR/logs/snipper.log"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self.quit()
            return
        finally:
            # 我们自己抓的那份立刻删掉，只保留内存里的字节
            if owns_capture and "path" in locals():
                cleanup_capture(path)

        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(png))
        win = Snipper(self, png, texture, preselect=self._preselect,
                      auto_ocr=self._auto_ocr)
        self._win = win

        # 关键顺序：Wayland 下未映射的窗口发 fullscreen 请求会被合成器忽略，
        # 必须先把窗口 present 出去，再请求全屏。同时用显示器尺寸兜底，
        # 万一合成器拒绝全屏，窗口也至少铺满整个桌面。
        width, height = _desktop_size()
        if width and height:
            win.set_default_size(width, height)

        # 注意：所有 "map" 处理器都必须在 present() **之前**连接。
        # present() 有可能在这一帧就同步完成映射，之后再 connect("map")
        # 就永远收不到信号（这条全屏兜底曾经因此是死代码）。
        def _on_map(w):
            w.fullscreen()

        win.connect("map", _on_map)

        if os.environ.get("PRTSC_OCR_DEBUG_FOCUS"):
            # 排查"由快捷键唤起时 Esc 不生效"用的探针：
            # active=False 说明合成器没把键盘焦点给过来。
            def _probe(w):
                print(f"[focus] active={w.is_active()} fullscreen={w.is_fullscreen()} "
                      f"token={os.environ.get('XDG_ACTIVATION_TOKEN', '(无)')}", flush=True)
                if not w.is_active():
                    w.present()  # 再要一次焦点
                return False

            win.connect("map", lambda w: GLib.timeout_add(1000, _probe, w))

        win.present()
        win.fullscreen()


def _desktop_size():
    """所有显示器的并集尺寸（Wayland 下用 Gdk 的 monitor 几何）。"""
    display = Gdk.Display.get_default()
    if display is None:
        return None, None
    monitors = display.get_monitors()
    if monitors.get_n_items() == 0:
        return None, None
    xs, ys, xe, ye = [], [], [], []
    for i in range(monitors.get_n_items()):
        g = monitors.get_item(i).get_geometry()
        xs.append(g.x)
        ys.append(g.y)
        xe.append(g.x + g.width)
        ye.append(g.y + g.height)
    return max(xe) - min(xs), max(ye) - min(ys)


def _parse_rect(text: str):
    try:
        parts = [int(float(p)) for p in text.replace("x", ",").split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("需要 4 个数字：x,y,w,h") from exc
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("需要 4 个数字：x,y,w,h")
    return tuple(parts)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="截图 + OCR 工具（PrtSc 唤起）")
    ap.add_argument("--image", help="不抓屏，直接用这张图片进入框选界面（调试/复用）")
    ap.add_argument("--no-panel", action="store_true", help="识别后不弹结果面板，只进剪贴板")
    ap.add_argument("--select", type=_parse_rect, metavar="X,Y,W,H",
                    help="进入界面时预置框选（配合 --auto-ocr 可做无人值守自检）")
    ap.add_argument("--auto-ocr", action="store_true", help="显示后自动对框选区域做一次识别（自检用）")
    args = ap.parse_args(argv)

    if args.no_panel:
        os.environ["PRTSC_OCR_PANEL"] = "0"

    app = SnipperApp(image=args.image, preselect=args.select, auto_ocr=args.auto_ocr)
    return app.run([])


if __name__ == "__main__":
    sys.exit(main())
