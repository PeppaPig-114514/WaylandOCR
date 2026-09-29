#!/usr/bin/env python3
"""
prtsc-ocr 客户端：只用标准库，任何 Python 都能跑（GUI 走系统 python，
守护进程走 venv python，两边共用这个模块）。

对上层隐藏"守护进程没起来就自动拉起"这件事，做到真正随叫随到。
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
BASE_DIR = Path(os.environ.get("PRTSC_OCR_HOME", HOME / "PrtScOCR"))
VENV_PY = BASE_DIR / ".venv" / "bin" / "python"
DAEMON = BASE_DIR / "lib" / "ocrd.py"


def socket_path() -> str:
    explicit = os.environ.get("PRTSC_OCR_SOCK")
    if explicit:
        return explicit
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "prtsc-ocr.sock")
    return f"/tmp/prtsc-ocr-{os.getuid()}.sock"


class DaemonNotRunning(RuntimeError):
    pass


def _connect(timeout: float = 1.0):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(socket_path())
    return s


def is_running(timeout: float = 0.4) -> bool:
    try:
        with _connect(timeout) as s:
            pass
        return True
    except OSError:
        return False


def start_daemon(wait: float = 30.0) -> bool:
    """拉起守护进程并等它就绪。优先交给 systemd 用户服务（受管、会自愈）。"""
    if is_running():
        return True

    started = False
    try:
        unit = subprocess.run(
            ["systemctl", "--user", "start", "prtsc-ocr.service"],
            capture_output=True, text=True, timeout=20,
        )
        started = unit.returncode == 0
        if not started and unit.stderr.strip():
            sys.stderr.write(f"[ocr] systemctl start 失败：{unit.stderr.strip()}\n")
    except (OSError, subprocess.TimeoutExpired) as exc:
        sys.stderr.write(f"[ocr] 无法通过 systemd 启动：{exc}\n")

    if not started:
        # 退路：裸启动（例如还没装 systemd 单元时）
        py = VENV_PY if VENV_PY.exists() else Path(sys.executable)
        log_dir = BASE_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "ocrd-boot.log", "ab") as fh:
            subprocess.Popen(
                [str(py), str(DAEMON)],
                stdout=fh, stderr=fh, stdin=subprocess.DEVNULL,
                start_new_session=True,
            )

    deadline = time.time() + wait
    # 预热失败的兜底：ready 之后最多再等这么久，避免永远卡住
    soft_deadline = None
    while time.time() < deadline:
        if is_running():
            try:
                info = request({"cmd": "ping"}, timeout=3.0, autostart=False)
                if info.get("warm"):
                    return True
                if info.get("ready"):
                    # 引擎已加载但预热还没跑完，再给它一点时间
                    if soft_deadline is None:
                        soft_deadline = time.time() + 30.0
                    elif time.time() > soft_deadline:
                        return True
            except Exception:
                pass
        time.sleep(0.25)
    return is_running()


_ID_LOCK = threading.Lock()
_ID_SEQ = itertools.count(1)


def _next_id() -> int:
    """进程内单调递增的请求 id。

    之前用 int(time.time()*1000) % 1e6：同一毫秒内的两个请求会拿到同一个 id，
    GUI 里并发发请求时不好排查问题，干脆换成计数器。
    """
    with _ID_LOCK:
        return next(_ID_SEQ)


def request(payload: dict, timeout: float = 120.0, autostart: bool = True) -> dict:
    payload.setdefault("id", _next_id())
    try:
        with _connect(1.0) as s:
            s.settimeout(timeout)
            s.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8"))
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as exc:
        if autostart and start_daemon():
            return request(payload, timeout=timeout, autostart=False)
        raise DaemonNotRunning(f"OCR 守护进程不可用：{exc}") from exc
    except OSError as exc:
        if autostart and start_daemon():
            return request(payload, timeout=timeout, autostart=False)
        raise DaemonNotRunning(f"OCR 守护进程不可用：{exc}") from exc


def ping(autostart: bool = False) -> dict:
    return request({"cmd": "ping"}, timeout=3.0, autostart=autostart)


def ocr_path(path: str, region=None, **kw) -> dict:
    # 必须绝对化：守护进程是独立进程（systemd 服务的 cwd 是 $HOME），
    # 相对路径会按**它的**工作目录解析，于是报 FileNotFoundError。
    # 路径的含义属于调用方，所以在这里就定死。
    p = Path(path).expanduser().resolve()
    return request({"cmd": "ocr", "image": str(p), "region": list(region) if region else None, **kw})


def ocr_bytes(data: bytes, region=None, **kw) -> dict:
    return request(
        {"cmd": "ocr", "image_b64": base64.b64encode(data).decode("ascii"),
         "region": list(region) if region else None, **kw}
    )


def shutdown() -> dict:
    try:
        return request({"cmd": "shutdown"}, timeout=3.0, autostart=False)
    except DaemonNotRunning:
        return {"ok": True, "already_stopped": True}
