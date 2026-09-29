#!/usr/bin/env python3
"""
prtsc-ocr daemon —— 常驻的本地 OCR 服务。

设计目标
--------
* **后台常运行**：模型只加载一次并常驻内存，首次请求后再无冷启动开销。
* **随叫随到**：通过 UNIX socket 提供请求/响应，单次识别目标 < 300ms。
* **零网络**：模型随包分发自本地，断网可用，截图不出本机。

协议
----
一条 JSON = 一帧，请求与响应都是一行（NDJSON）。

请求::

    {"id": 1, "cmd": "ocr", "image": "/abs/path.png", "region": [x, y, w, h]}
    {"id": 2, "cmd": "ocr", "image_b64": "<png bytes in base64>"}
    {"id": 3, "cmd": "ping"}
    {"id": 4, "cmd": "shutdown"}

响应::

    {"id": 1, "ok": true, "text": "...", "lines": [...], "ms": 137.2}

运行环境为 ~/PrtScOCR/.venv（Python 3.12 + rapidocr + onnxruntime）。
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
import time
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

# --------------------------------------------------------------------------
# 常量与默认配置
# --------------------------------------------------------------------------

APP_NAME = "prtsc-ocr"
HOME = Path.home()
BASE_DIR = Path(os.environ.get("PRTSC_OCR_HOME", HOME / "PrtScOCR"))
LOG_DIR = BASE_DIR / "logs"


def default_socket_path() -> str:
    explicit = os.environ.get("PRTSC_OCR_SOCK")
    if explicit:
        return explicit
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(runtime) if runtime and os.path.isdir(runtime) else Path(f"/tmp/{APP_NAME}-{os.getuid()}")
    base.mkdir(parents=True, exist_ok=True)
    return str(base / f"{APP_NAME}.sock")


SOCKET_PATH = default_socket_path()

log = logging.getLogger(APP_NAME)


def setup_logging(verbose: bool = False, foreground: bool = False) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    fh = RotatingFileHandler(LOG_DIR / "ocrd.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if foreground:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)

    # rapidocr 的 INFO 日志很吵（每次推理都打印），压到 WARNING
    logging.getLogger("RapidOCR").setLevel(logging.WARNING)
    # PIL 在 DEBUG 级别会把每个 PNG chunk 都打出来
    logging.getLogger("PIL").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# 版面重排：把零散的识别框还原成可读的多行文本
# --------------------------------------------------------------------------

def _box_metrics(box) -> tuple[float, float, float, float, float]:
    xs = [float(p[0]) for p in box]
    ys = [float(p[1]) for p in box]
    return min(xs), max(xs), min(ys), max(ys), (min(ys) + max(ys)) / 2.0


def group_into_lines(boxes, txts, scores, y_tolerance: float = 0.6, gap_ratio: float = 0.6):
    """按几何位置把识别结果聚成视觉行，行内按 x 排序。

    返回 [[item, ...], ...]，item 含 text / score / box / x0 / x1 / cy / h。
    """
    items = []
    for box, txt, sc in zip(boxes, txts, scores):
        if not txt:
            continue
        x0, x1, y0, y1, cy = _box_metrics(box)
        items.append(
            {
                "text": str(txt),
                "score": round(float(sc), 4),
                "box": [[round(float(x), 1), round(float(y), 1)] for x, y in box],
                "x0": x0,
                "x1": x1,
                "cy": cy,
                "h": max(y1 - y0, 1.0),
            }
        )

    items.sort(key=lambda i: (i["cy"], i["x0"]))

    lines: list[dict] = []
    for it in items:
        best, best_dy = None, None
        for ln in lines:
            dy = abs(it["cy"] - ln["cy_sum"] / ln["n"])
            tol = max(it["h"], ln["h_sum"] / ln["n"]) * y_tolerance
            if dy <= tol and (best_dy is None or dy < best_dy):
                best, best_dy = ln, dy
        if best is None:
            lines.append({"items": [it], "cy_sum": it["cy"], "h_sum": it["h"], "n": 1})
        else:
            best["items"].append(it)
            best["cy_sum"] += it["cy"]
            best["h_sum"] += it["h"]
            best["n"] += 1

    lines.sort(key=lambda ln: ln["cy_sum"] / ln["n"])

    ordered: list[list[dict]] = []
    for ln in lines:
        row = sorted(ln["items"], key=lambda i: i["x0"])
        ordered.append(row)
    return ordered


def join_row(row: list[dict]) -> str:
    """行内拼接：拉丁文本间隔大时补空格，CJK 之间直接相连。"""
    if not row:
        return ""
    parts = [row[0]["text"]]
    for prev, cur in zip(row, row[1:]):
        gap = cur["x0"] - prev["x1"]
        ref = max(prev["h"], cur["h"])
        need_space = gap > ref * 0.6
        # 两侧都是 CJK 时不补空格，更接近原文
        a = prev["text"][-1:]
        b = cur["text"][:1]
        if need_space and not (_is_cjk(a) and _is_cjk(b)):
            parts.append(" ")
        parts.append(cur["text"])
    return "".join(parts)


def _is_cjk(ch: str) -> bool:
    if not ch:
        return False
    o = ord(ch)
    return (
        0x3000 <= o <= 0x303F
        or 0x3400 <= o <= 0x4DBF
        or 0x4E00 <= o <= 0x9FFF
        or 0xF900 <= o <= 0xFAFF
        or 0xFF00 <= o <= 0xFFEF
    )


# --------------------------------------------------------------------------
# 引擎封装
# --------------------------------------------------------------------------

_ENUM_CACHE: dict = {}


def _enum(kind: str, raw):
    """把环境变量里的字符串转成 rapidocr 要求的枚举类型。

    踩过的坑：rapidocr 的 ParseParams.update_batch() 对 engine_type /
    model_type / ocr_version / task_type 强制要求 Enum 实例，直接传字符串
    （哪怕值完全正确）会抛 "must be Enum Type"。所以这里统一转换，
    并且同时接受枚举名（TINY / PPOCRV6）和枚举值（tiny / PP-OCRv6）。
    """
    try:
        from rapidocr.utils import typings as _typings
    except Exception:  # noqa: BLE001 —— 版本对不上就原样传，别把用户卡死
        return raw

    cls = _ENUM_CACHE.get(kind)
    if cls is None:
        cls = getattr(_typings, kind, False)
        _ENUM_CACHE[kind] = cls
    if not cls:
        return raw

    key = str(raw).strip()
    for member in cls:
        if key in (member.name, member.value) or key.lower() == member.name.lower():
            return member
    valid = "、".join(f"{m.name}({m.value})" for m in cls)
    raise ValueError(f"{kind} 不接受 {raw!r}；可选：{valid}")


class OcrEngine:
    """RapidOCR 的薄封装：懒加载 + 常驻 + 串行化推理。"""

    def __init__(self) -> None:
        self._engine = None
        self._lock = threading.Lock()
        self.loaded_at: float | None = None
        self.load_error: str | None = None
        self.warm = False
        self.calls = 0
        self.total_ms = 0.0
        self.describe = "not loaded"

    # -- 参数：默认面向"屏幕截图里的文本"调优 ----------------------------
    # 关键点：limit_type 用 max 而不是库默认的 min。
    # min/736 会把小图放大到最小边 736（420x120 直接被放大 6 倍），
    # 一次框选识别要 1.5~2.4 秒；而实测精度毫无提升（bench/RESULTS.md §6.2）。
    # 屏幕截图本来就清晰，只是面积小，"放大短边"那套是整页扫描文档的假设。
    # limit_side_len 用 960：与 1280 逐图结果完全相同，小图上还更快。
    def _params(self) -> dict:
        env = os.environ
        params: dict = {
            "Det.limit_type": env.get("PRTSC_OCR_LIMIT_TYPE", "max"),
            "Det.limit_side_len": int(env.get("PRTSC_OCR_LIMIT_SIDE", "960")),
            "Det.box_thresh": float(env.get("PRTSC_OCR_BOX_THRESH", "0.45")),
            "Det.thresh": float(env.get("PRTSC_OCR_THRESH", "0.25")),
            "Global.log_level": "error",
        }
        if env.get("PRTSC_OCR_DET_MODEL"):
            params["Det.model_type"] = _enum("ModelType", env["PRTSC_OCR_DET_MODEL"])
        if env.get("PRTSC_OCR_REC_MODEL"):
            params["Rec.model_type"] = _enum("ModelType", env["PRTSC_OCR_REC_MODEL"])
        if env.get("PRTSC_OCR_OCR_VERSION"):
            ver = _enum("OCRVersion", env["PRTSC_OCR_OCR_VERSION"])
            params["Det.ocr_version"] = ver
            params["Rec.ocr_version"] = ver
        return params

    def ensure(self):
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            t0 = time.perf_counter()
            from rapidocr import RapidOCR  # 延迟导入，缩短 --help 等路径

            params = self._params()
            log.info("loading RapidOCR params=%s", params)
            self._engine = RapidOCR(params=params)
            self.loaded_at = time.time()
            self.describe = "rapidocr %s / det=%s / limit=%s" % (
                _module_version("rapidocr"),
                params.get("Det.ocr_version", "default"),
                params.get("Det.limit_type", "?"),
            )
            log.info("engine ready in %.2fs", time.perf_counter() - t0)
            return self._engine

    def warmup(self) -> None:
        """按真实截图尺寸预热。

        onnxruntime 在**首次遇到某个输入形状**时要分配内存并做图优化，
        实测第一张真实截图会因此多花 4 秒多——那样"随叫随到"就废了。
        这里用几档常见分辨率各跑一遍，把这份开销提前到服务启动时。
        """
        import numpy as np

        try:
            eng = self.ensure()
            # 模拟屏幕上"有文字"的画面：白底 + 若干深色横条，
            # 保证 det 真的跑完整条链路而不是提前返回。
            for w, h in ((1280, 720), (640, 360)):
                img = np.full((h, w, 3), 245, dtype=np.uint8)
                for i in range(6):
                    y = 40 + i * (h // 8)
                    img[y:y + max(8, h // 40), 60:w - 60] = 40
                t0 = time.perf_counter()
                # 必须占同一把锁：rapidocr 推理管线不是线程安全的，
                # 预热和第一个真实请求并发会互相踩状态（曾观察到 det 返回空）。
                with self._lock:
                    eng(img, use_cls=False)
                log.info("warmup %dx%d ok in %.0fms", w, h, (time.perf_counter() - t0) * 1000)
            self.warm = True
            log.info("warmup complete")
        except Exception:
            log.exception("warmup failed (daemon stays up, will retry on demand)")

    def run(self, image, region=None, use_cls: bool = False, min_score: float = 0.0,
            single_line: bool = False, upscale: float | None = None):
        from PIL import Image
        import numpy as np

        eng = self.ensure()

        if isinstance(image, (bytes, bytearray)):
            pil = Image.open(io.BytesIO(bytes(image)))
        elif isinstance(image, Image.Image):
            pil = image
        else:
            pil = Image.open(str(image))
        pil = pil.convert("RGB")

        if region:
            x, y, w, h = (int(round(float(v))) for v in region)
            x = max(0, min(x, pil.width - 1))
            y = max(0, min(y, pil.height - 1))
            w = max(1, min(w, pil.width - x))
            h = max(1, min(h, pil.height - y))
            pil = pil.crop((x, y, x + w, y + h))

        # 默认不放大。
        # 曾经这里有个"最长边 <480px 就自动 2× 放大"的启发式，以为能救小字；
        # 实测（bench/RESULTS.md §4.8，以及本机复验）在 12px 样本上识别结果
        # 逐字相同、延迟却翻 2~3 倍（420x120 区域 250ms → 91ms），纯属浪费。
        # 参数保留，个别极端场景可以手动开。
        if upscale is None:
            upscale = 1.0
        if upscale and upscale > 1.0:
            pil = pil.resize((int(pil.width * upscale), int(pil.height * upscale)), Image.LANCZOS)

        arr = np.asarray(pil)

        t0 = time.perf_counter()
        with self._lock:
            # 关键：每次都必须显式传 use_det / use_cls。
            # rapidocr 的 RapidOCR.__call__ 会通过 update_params() 把这两个开关
            # **写进实例状态**，而传 None 表示"保持不变"。于是只要有一次单行识别
            # （use_det=False），后续所有调用都会一直跳过检测，表现为识别结果
            # 变成一两个乱码字。显式传参才能让每次调用互相隔离。
            result = eng(arr, use_det=not single_line, use_cls=use_cls)
        elapsed = (time.perf_counter() - t0) * 1000.0

        # 注意：boxes/txts/scores 往往是 numpy 数组，直接 `x or []` 会触发
        # "truth value of an array is ambiguous"，必须显式判 None / 判长度。
        boxes = _as_list(getattr(result, "boxes", None))
        txts = _as_list(getattr(result, "txts", None))
        scores = _as_list(getattr(result, "scores", None))
        log.debug("raw result: type=%s boxes=%d txts=%d", type(result).__name__, len(boxes), len(txts))

        # use_det=False 时 rapidocr 返回的是 TextRecOutput，没有 boxes 字段；
        # 这时按"整块就是一行"处理，自己补一个覆盖全图的框。
        if not boxes and txts:
            w, h = pil.size
            boxes = [[[0.0, 0.0], [float(w), 0.0], [float(w), float(h)], [0.0, float(h)]]] * len(txts)

        rows = group_into_lines(boxes, txts, scores)
        lines = []
        for row in rows:
            text = join_row(row)
            if not text:
                continue
            score = sum(i["score"] for i in row) / len(row)
            if score < min_score:
                continue
            lines.append(
                {
                    "text": text,
                    "score": round(score, 4),
                    "box": _union_box(row),
                }
            )

        self.calls += 1
        self.total_ms += elapsed
        return {
            "text": "\n".join(l["text"] for l in lines),
            "lines": lines,
            "ms": round(elapsed, 1),
            "image_size": list(pil.size),
            "upscale": upscale or 1.0,
        }


def _as_list(value):
    """把 numpy 数组 / tuple / None 统一成 list，且不触发数组的真值歧义。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        return list(value)  # numpy 数组、其它可迭代对象
    except TypeError:
        return [value]


def _union_box(row: list[dict]):
    xs = [p[0] for i in row for p in i["box"]]
    ys = [p[1] for i in row for p in i["box"]]
    return [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]


def _module_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return "?"


ENGINE = OcrEngine()


# --------------------------------------------------------------------------
# socket 服务
# --------------------------------------------------------------------------

class Handler(socketserver.StreamRequestHandler):
    timeout = 120

    def handle(self) -> None:
        peer = self.client_address
        while True:
            try:
                raw = self.rfile.readline()
            except (socket.timeout, TimeoutError):
                return
            if not raw:
                return
            raw = raw.strip()
            if not raw:
                continue
            req_id = None
            try:
                req = json.loads(raw)
                req_id = req.get("id")
                resp = self.dispatch(req)
            except Exception as exc:  # noqa: BLE001 - 任何异常都不能让守护进程倒下
                log.error("request failed: %s\n%s", exc, traceback.format_exc())
                resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            resp["id"] = req_id
            try:
                self.wfile.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    # -- 路由 -------------------------------------------------------------
    def dispatch(self, req: dict) -> dict:
        cmd = (req.get("cmd") or "ocr").lower()

        if cmd == "ping":
            return {
                "ok": True,
                "pong": True,
                "pid": os.getpid(),
                "ready": ENGINE._engine is not None,
                "warm": ENGINE.warm,
                "engine": ENGINE.describe,
                "calls": ENGINE.calls,
                "avg_ms": round(ENGINE.total_ms / ENGINE.calls, 1) if ENGINE.calls else None,
                "socket": SOCKET_PATH,
            }

        if cmd == "shutdown":
            threading.Thread(target=shutdown_later, daemon=True).start()
            return {"ok": True, "bye": True}

        if cmd == "ocr":
            region = req.get("region")
            if req.get("image_b64"):
                image = base64.b64decode(req["image_b64"])
            elif req.get("image"):
                image = req["image"]
            else:
                return {"ok": False, "error": "need 'image' or 'image_b64'"}
            out = ENGINE.run(
                image,
                region=region,
                use_cls=bool(req.get("use_cls", False)),
                min_score=float(req.get("min_score", 0.0)),
                single_line=bool(req.get("single_line", False)),
                upscale=req.get("upscale"),
            )
            out["ok"] = True
            return out

        return {"ok": False, "error": f"unknown cmd: {cmd!r}"}


class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


_STOP = threading.Event()


def shutdown_later() -> None:
    time.sleep(0.15)
    _STOP.set()
    threading.Thread(target=lambda: os.kill(os.getpid(), signal.SIGTERM), daemon=True).start()


def serve(socket_path: str, foreground: bool) -> int:
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        # 旧进程残留的 socket：先尝试连一下，活着就让位
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                probe.connect(str(path))
            log.error("another daemon is already listening on %s", path)
            return 3
        except OSError:
            log.warning("removing stale socket %s", path)
            path.unlink(missing_ok=True)

    server = Server(str(path), Handler)
    os.chmod(path, 0o600)

    def on_term(signum, _frame):
        log.info("signal %s -> shutting down", signum)
        _STOP.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    log.info("listening on %s (pid %d)", path, os.getpid())

    # 后台预热，避免第一次识别付冷启动成本
    threading.Thread(target=ENGINE.warmup, name="warmup", daemon=True).start()

    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        server.server_close()
        path.unlink(missing_ok=True)
        log.info("stopped")
    return 0


def main(argv=None) -> int:
    global SOCKET_PATH

    ap = argparse.ArgumentParser(description="prtsc-ocr 常驻 OCR 服务")
    ap.add_argument("--socket", default=SOCKET_PATH, help=f"UNIX socket 路径（默认 {SOCKET_PATH}）")
    ap.add_argument("--foreground", "-f", action="store_true", help="日志同时打到 stderr")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--warm-only", action="store_true", help="只加载模型后退出（用于验证安装）")
    args = ap.parse_args(argv)

    setup_logging(args.verbose, args.foreground)
    SOCKET_PATH = args.socket

    if args.warm_only:
        ENGINE.warmup()
        print("engine:", ENGINE.describe)
        return 0

    try:
        return serve(args.socket, args.foreground)
    except Exception:
        log.exception("fatal")
        return 1


if __name__ == "__main__":
    sys.exit(main())
