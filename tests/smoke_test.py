#!/usr/bin/env python3
"""
prtsc-ocr 自检：在私有 socket 上起一个临时守护进程，跑一组断言。

    .venv/bin/python tests/smoke_test.py

不碰系统上正在跑的那个服务（用 PRTSC_OCR_SOCK 指向临时 socket），
所以可以随时跑，不影响你用 PrtSc。
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
VENV_PY = BASE_DIR / ".venv" / "bin" / "python"
sys.path.insert(0, str(BASE_DIR / "lib"))

FONT_CJK = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = ""):
    (PASS if cond else FAIL).append(name)
    mark = "\033[32mPASS\033[0m" if cond else "\033[31mFAIL\033[0m"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))
    return cond


def make_samples(d: Path):
    from PIL import Image, ImageDraw, ImageFont

    def cjk(sz):
        return ImageFont.truetype(FONT_CJK, sz)

    def mono(sz):
        return ImageFont.truetype(FONT_MONO, sz)

    mixed = Image.new("RGB", (900, 260), "white")
    dr = ImageDraw.Draw(mixed)
    dr.text((30, 20), "第一行：中文识别检验", font=cjk(24), fill="black")
    dr.text((30, 70), "Second line: Total=1,234.56 USD", font=cjk(24), fill="black")
    dr.text((30, 120), "第三行 sudo apt install tesseract-ocr", font=mono(22), fill="black")
    dr.text((30, 190), "底部：不应出现在区域裁剪结果里", font=cjk(22), fill="black")
    mixed.save(d / "mixed.png")

    twocol = Image.new("RGB", (1000, 200), "white")
    dr = ImageDraw.Draw(twocol)
    dr.text((30, 20), "左栏标题", font=cjk(24), fill="black")
    dr.text((560, 20), "右栏标题", font=cjk(24), fill="black")
    dr.text((30, 90), "左栏正文", font=cjk(20), fill="black")
    dr.text((560, 90), "右栏正文", font=cjk(20), fill="black")
    dr.text((30, 150), "底部通栏", font=cjk(20), fill="black")
    twocol.save(d / "twocol.png")

    Image.new("RGB", (400, 120), "white").save(d / "blank.png")
    return d / "mixed.png", d / "twocol.png"


def wait_warm(sock: str, timeout: float = 120.0) -> dict:
    import ocr_client

    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        try:
            last = ocr_client.ping(autostart=False)
            if last.get("warm"):
                return last
        except Exception:
            pass
        time.sleep(0.4)
    return last


def main() -> int:
    if not VENV_PY.exists():
        print(f"找不到虚拟环境：{VENV_PY}\n请先跑 ./install.sh")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="prtsc-ocr-smoke-"))
    sock = str(tmp / "test.sock")
    os.environ["PRTSC_OCR_SOCK"] = sock
    os.environ["PRTSC_OCR_HOME"] = str(BASE_DIR)

    print("== 生成测试图 ==")
    mixed, twocol = make_samples(tmp)
    print(f"   {tmp}")

    log = open(tmp / "daemon.log", "wb")
    # 故意让守护进程的工作目录和测试进程不同：这样才能测出
    # "客户端必须把相对路径绝对化"这个回归（曾经因此报 FileNotFoundError）。
    proc = subprocess.Popen(
        [str(VENV_PY), str(BASE_DIR / "lib" / "ocrd.py"), "--socket", sock],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, cwd="/",
    )
    import ocr_client

    try:
        print("== 等待守护进程预热 ==")
        info = wait_warm(sock)
        print(f"   engine={info.get('engine')} warm={info.get('warm')}")

        print("== 协议与引擎 ==")
        check("守护进程应答 ping", bool(info.get("pong")), f"pid={info.get('pid')}")
        check("引擎已加载并预热", bool(info.get("warm")), str(info.get("engine")))

        print("== 整图识别 ==")
        r = ocr_client.ocr_path(str(mixed))
        txt = r.get("text", "")
        check("整图返回 ok", r.get("ok") is True)
        check("识别出第一行中文", "中文识别检验" in txt.replace(" ", ""), repr(txt.split("\n")[0] if txt else ""))
        check("识别出英文与金额", "1,234.56" in txt)
        check("识别出命令行内容", "tesseract-ocr" in txt.replace(" ", ""))
        check("返回了耗时统计", isinstance(r.get("ms"), (int, float)) and r["ms"] > 0, f"{r.get('ms')}ms")

        print("== 区域裁剪：只应包含框内的文字 ==")
        rr = ocr_client.ocr_path(str(mixed), region=[20, 10, 860, 60])
        rtxt = rr.get("text", "")
        check("区域结果包含第一行", "中文识别检验" in rtxt.replace(" ", ""))
        check("区域结果不含框外内容", "tesseract" not in rtxt and "底部" not in rtxt.replace(" ", ""),
              repr(rtxt))
        check("区域尺寸正确", rr.get("image_size") == [860, 60], str(rr.get("image_size")))

        print("== 版面重排：同一视觉行左→右，行间上→下 ==")
        t = ocr_client.ocr_path(str(twocol)).get("text", "").split("\n")
        joined = "".join(t)
        check("左右同高行合并且左栏在前",
              "左栏标题" in joined and "右栏标题" in joined
              and joined.index("左栏标题") < joined.index("右栏标题"),
              repr(t[0] if t else ""))
        check("底部通栏在最后一行", bool(t) and "底部通栏" in t[-1], repr(t[-1] if t else ""))

        print("== 单行模式（跳过检测）==")
        sl = ocr_client.ocr_path(str(mixed), region=[20, 60, 860, 50], single_line=True)
        check("单行模式返回 ok", sl.get("ok") is True)
        check("单行模式命中目标行", "1,234.56" in (sl.get("text") or ""))

        print("== 二进制（剪贴板图片）通路 ==")
        data = mixed.read_bytes()
        rb = ocr_client.ocr_bytes(data)
        # ONNX 多线程推理在边缘样本上偶有微小差异，比较归一化后的文本
        norm = lambda s: "".join((s or "").split())  # noqa: E731
        check("base64 通路结果与路径通路一致",
              norm(rb.get("text")) == norm(r.get("text")),
              f"b64={rb.get('text')!r} path={r.get('text')!r}")

        print("== 相对路径必须按调用方的 cwd 解析 ==")
        # 守护进程的 cwd 是 "/"，测试进程的 cwd 是 BASE_DIR；
        # 客户端做过绝对化，所以下面这个相对路径必须能通。
        rel = "samples/test_mixed.png"
        if (BASE_DIR / rel).exists():
            rr3 = ocr_client.ocr_path(rel)
            check("相对路径能正确解析", rr3.get("ok") is True
                  and "Ubuntu" in (rr3.get("text") or ""), str(rr3.get("error"))[:80])
        else:
            print("   （跳过：没有 samples/test_mixed.png）")

        print("== 健壮性：异常输入不应让服务倒下 ==")
        blank = ocr_client.ocr_path(str(tmp / "blank.png"))
        check("空白图返回 ok 且无文字", blank.get("ok") is True and not (blank.get("text") or "").strip())
        bad = ocr_client.ocr_path(str(tmp / "not-exist.png"))
        check("不存在的文件返回错误而不是崩溃", bad.get("ok") is False and "error" in bad, str(bad.get("error"))[:60])
        unknown = ocr_client.request({"cmd": "no-such-cmd"})
        check("未知指令返回错误", unknown.get("ok") is False)
        still = ocr_client.ping(autostart=False)
        check("服务在异常输入后依然存活", bool(still.get("pong")))

        print("== 性能参考（数值随机器负载波动，这里只做「没坏掉」的量级兜底）==")
        import statistics
        samples = []
        for _ in range(5):
            rr2 = ocr_client.ocr_path(str(mixed))
            samples.append(rr2["ms"])
        med = statistics.median(samples)
        check("整图 900x260 中位耗时 < 10s（量级兜底）", med < 10000, f"{med:.0f}ms")
        print(f"         中位 {med:.0f}ms  样本 {[round(s) for s in samples]}")

        print("== 关闭服务 ==")
        ocr_client.shutdown()
        time.sleep(1.2)
        check("shutdown 后 socket 已清理", not Path(sock).exists())
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        log.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"结果：\033[32m{len(PASS)} 通过\033[0m，"
          + (f"\033[31m{len(FAIL)} 失败\033[0m" if FAIL else "0 失败"))
    if FAIL:
        for name in FAIL:
            print(f"  失败：{name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
