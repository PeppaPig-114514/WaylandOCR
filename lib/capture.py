#!/usr/bin/env python3
"""
屏幕抓取（Wayland 专用）。

为什么不用 maim / scrot / gnome-screenshot
------------------------------------------
* `maim` / `scrot` / `import` 都依赖 X11，Wayland 会话下要么报错要么只抓到黑屏；
* `grim` 只支持 wlroots 合成器（Sway/Hyprland），GNOME 用不了；
* `org.gnome.Shell.Screenshot` 这个 D-Bus 接口自 GNOME 41 起对非 shell 客户端
  返回 AccessDenied（本机实测："Screenshot is not allowed"）。

唯一在 GNOME Wayland 下对普通程序开放的正路是 xdg-desktop-portal：

    org.freedesktop.portal.Screenshot -> Screenshot(parent_window, options)

其中 `interactive=false` 在 GNOME 50 上**静默**完成：不弹授权框、不弹 shell
截图 UI，约 0.5s 返回一个 file:// URI（落在 XDG 图片目录）。我们用这一张
"冻结帧"自己画框选遮罩，从而可以像 Win11 截图工具那样挂自己的工具栏
（包括 OCR 按钮）——GNOME 内置截图 UI 是 shell 的一部分，加不了按钮。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
PORTAL_IFACE = "org.freedesktop.portal.Screenshot"
REQUEST_IFACE = "org.freedesktop.portal.Request"


class CaptureError(RuntimeError):
    pass


def identity_hint() -> str:
    """出错时打印"portal 眼里的我们是谁"。

    portal 允许谁截图，是按调用者的 **app-id** 判定的，而 app-id 是从进程的
    cgroup 推出来的：只有 app-*.service / dbus-*.service 这类命名才算"某个应用"，
    其它（包括 run-*.scope）都算 host，app-id 为空。空 app-id 在权限库里是有授权的，
    所以 bin/prtsc-ocr 会先把自己塞进一个中性 scope 再跑。这里把事实打出来，
    下次再出问题就不用靠猜。
    """
    try:
        lines = Path("/proc/self/cgroup").read_text().strip().splitlines()
        cgroup = lines[-1].split("::", 1)[-1] if lines else "?"
    except OSError:
        cgroup = "(读不到)"
    scoped = "是" if os.environ.get("PRTSC_OCR_SCOPED") else "否"
    return f"cgroup={cgroup}　中性 scope 包装={scoped}"


def _rejection_hint(code: int) -> str:
    """把 portal 的 response 码翻译成能照着做的说明。

    response=2 在本机（Ubuntu 的 portal）几乎总是这一件事：调用者的 app-id 没有被
    授权截图，portal 于是要弹一个"允许截图吗"的系统对话框，而该对话框只能由**前台
    应用**弹出（gnome-shell 的 com.canonical.Shell.PermissionPrompting 会拒绝后台
    请求）。本工具是先抓屏后建窗，请求发出时还没有窗口 → 不是前台 → 对话框弹不出来
    → 直接拒绝。日志里能看到：
        Failed to show access dialog: AccessDenied:
          Only the focused app is allowed to show a system access dialog
    """
    if code == 1:
        return "你在 portal 的对话框里选择了取消。"
    if code == 2:
        return (
            "portal 拒绝了这次静默截图（response=2）。\n"
            "  最常见的原因：调用者的 app-id 没被授权截图，portal 想弹\"允许截图\"对话框，\n"
            "  但那个对话框只能由前台应用弹出，而本工具是先抓屏、后建窗，此刻没有焦点。\n"
            f"  诊断：{identity_hint()}\n"
            "  正常情况 cgroup 应是 run-*.scope（中性 scope，app-id 为空、命中已有授权）。\n"
            "  若 cgroup 是 app-*.service / dbus-*.service，说明它没经过 bin/prtsc-ocr 的\n"
            "  中性 scope 包装——检查是否直接调用了 lib/snipper.py。\n"
            "  排查：看 logs/snipper.log，以及\n"
            "    journalctl --user -u xdg-desktop-portal -n 20"
        )
    return f"portal 返回了未知的 response={code}。"


def _gi():
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    return Gio, GLib


def grab_screen(timeout: float = 20.0, token: str | None = None, attempts: int = 2) -> Path:
    """静默抓取整屏，返回 PNG 路径。

    portal 偶尔会在刚登录、xdg-desktop-portal-gnome 还没起来的瞬间失败，
    所以默认重试两次；每次尝试约 0.5s，代价很小，换来"按下就有"的确定性。
    """
    last: Exception | None = None
    for i in range(max(1, attempts)):
        try:
            return _grab_screen_once(timeout=timeout, token=token)
        except CaptureError as exc:
            last = exc
            if i + 1 < max(1, attempts):
                time.sleep(0.4)
    raise last if last else CaptureError("抓屏失败")


def _grab_screen_once(timeout: float = 20.0, token: str | None = None) -> Path:
    """静默抓取整屏，返回 PNG 路径（单次尝试）。"""
    Gio, GLib = _gi()

    token = token or f"prtscocr{int(time.time() * 1000) % 10_000_000}"
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    state: dict = {}
    loop = GLib.MainLoop()

    def on_response(_conn, _sender, path, iface, _signal, params):
        if iface != REQUEST_IFACE:
            return
        code, results = params.unpack()
        state["code"] = code
        state["results"] = results
        loop.quit()

    sub = bus.signal_subscribe(
        PORTAL_BUS, REQUEST_IFACE, "Response", None, None, Gio.DBusSignalFlags.NONE, on_response
    )
    try:
        res = bus.call_sync(
            PORTAL_BUS,
            PORTAL_PATH,
            PORTAL_IFACE,
            "Screenshot",
            GLib.Variant("(sa{sv})", ("", {
                "interactive": GLib.Variant("b", False),
                "modal": GLib.Variant("b", False),
                "handle_token": GLib.Variant("s", token),
            })),
            None,
            Gio.DBusCallFlags.NONE,
            8000,
            None,
        )
        request_path = res.unpack()[0]
        state["request_path"] = request_path

        def on_timeout():
            state.setdefault("timeout", True)
            loop.quit()
            return False

        GLib.timeout_add(int(timeout * 1000), on_timeout)
        loop.run()
    except Exception as exc:  # noqa: BLE001
        raise CaptureError(f"调用 portal 截图失败：{exc}") from exc
    finally:
        bus.signal_unsubscribe(sub)

    if state.get("timeout"):
        raise CaptureError(f"portal 截图超时（{timeout:.0f}s 未响应）")
    code = state.get("code")
    if code not in (0, None):
        raise CaptureError(f"portal 截图被拒绝：{_rejection_hint(int(code))}")
    results = state.get("results") or {}
    uri = results.get("uri")
    if not uri:
        raise CaptureError(f"portal 未返回图片 URI：{results}")

    local = Gio.File.new_for_uri(uri).get_path()
    if not local:
        # 非本地 URI（例如文档门户给出的沙箱路径）：我们没有能力读它
        raise CaptureError(f"portal 返回的不是本地文件路径：{uri}")
    path = Path(local)
    if not path.exists():
        raise CaptureError(f"portal 返回的路径不存在：{path}")
    return path


def cleanup_capture(path: Path, keep: bool | None = None) -> None:
    """删除 portal 落盘的临时截图，避免 ~/图片 被刷屏。

    GNOME 自己的静默截图会把文件留在图片目录，我们用完即弃；
    `PRTSC_OCR_KEEP_CAPTURE=1` 可保留。
    """
    if keep is None:
        keep = os.environ.get("PRTSC_OCR_KEEP_CAPTURE", "0") == "1"
    if keep:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


if __name__ == "__main__":
    p = grab_screen()
    print(p, p.stat().st_size, "bytes")
