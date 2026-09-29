#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PrtScOCR —— OCR 引擎/参数「精度-延迟」基准测试（纯测量脚本）。

运行方式（无需参数）：
    /home/peppapig/PrtScOCR/.venv/bin/python bench/bench.py

本脚本只做三件事，不修改项目其它任何文件：
  1. 用 PIL 合成一批贴近「屏幕截图区域」的测试图，写到 bench/samples/，
     Ground Truth 文本直接写在脚本里（不依赖人工转录）。
  2. 对若干 RapidOCR 配置逐图跑推理（预热 1 次 + 计时 3 次取中位数），
     用 Levenshtein 编辑距离计算 CER。
  3. 在 stdout 打印完整结果表，并把原始数据写入 bench/results.json。

指标定义、归一化规则、阅读顺序重建规则见文件内注释与 bench/RESULTS.md。
"""

from __future__ import annotations

import argparse
import difflib
import json
import math
import platform
import re
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, __version__ as PIL_VERSION

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

HERE = Path(__file__).resolve().parent
SAMPLES_DIR = HERE / "samples"
RESULTS_JSON = HERE / "results.json"

CJK_TTC = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
CJK_TTC_SC_INDEX = 2  # index 2 = "Noto Sans CJK SC"
MONO_TTF = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

WARMUP_RUNS = 1
TIMED_RUNS = 3

# 阅读顺序重建：同一「行」的判定阈值（相对于行高）与「同一行内两段之间是否补空格」的间隙阈值
ROW_CENTER_TOL = 0.60
SPACE_GAP_RATIO = 0.55

DARK_BG = (16, 20, 26)
DARK_FG = (216, 222, 233)
GRAY_FG = (153, 153, 153)  # #999999


# --------------------------------------------------------------------------- #
# 字体
# --------------------------------------------------------------------------- #

_font_cache: Dict[Tuple[str, int], ImageFont.FreeTypeFont] = {}


def cjk_font(px: int) -> ImageFont.FreeTypeFont:
    key = ("cjk", px)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(CJK_TTC, px, index=CJK_TTC_SC_INDEX)
    return _font_cache[key]


def mono_font(px: int) -> ImageFont.FreeTypeFont:
    key = ("mono", px)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(MONO_TTF, px)
    return _font_cache[key]


# --------------------------------------------------------------------------- #
# 样本定义与渲染
# --------------------------------------------------------------------------- #


@dataclass
class SampleDef:
    sid: str
    title: str
    category: str
    font_px: int
    layout: str  # "row"（普通单栏） | "twocol"（两栏，GT 顺序 = 左栏整体 -> 右栏整体）
    build: Callable[[], Tuple[Image.Image, List[str]]]
    small_font: bool = False  # 是否纳入「2 倍放大」实验中（font_px <= 15）
    note: str = ""


def _metrics(font: ImageFont.FreeTypeFont) -> Tuple[int, int, int]:
    asc, desc = font.getmetrics()
    return asc, desc, asc + desc


def _text_width(font: ImageFont.FreeTypeFont, s: str) -> int:
    box = font.getbbox(s)
    return box[2] - box[0]


def render_block(
    lines: Sequence[str],
    font_px: int,
    *,
    width: Optional[int] = None,
    height: Optional[int] = None,
    fg=(17, 17, 17),
    bg=(255, 255, 255),
    font: Optional[ImageFont.FreeTypeFont] = None,
    pad_x: int = 14,
    pad_y: int = 10,
    spacing: float = 1.55,
    x_positions: Optional[Sequence[int]] = None,
    y_positions: Optional[Sequence[int]] = None,
) -> Image.Image:
    """按行渲染文本块；宽度/高度可显式指定（模拟真实截图区域的留白）。"""
    font = font or cjk_font(font_px)
    lh = max(1, int(round(font_px * spacing)))
    asc, desc, _ = _metrics(font)

    if y_positions is None:
        y_positions = [pad_y + i * lh for i in range(len(lines))]
    if x_positions is None:
        x_positions = [pad_x] * len(lines)

    need_w = max(x_positions[i] + _text_width(font, ln) for i, ln in enumerate(lines))
    need_h = max(y_positions) + asc + desc + pad_y

    W = max(width or 0, need_w + pad_x)
    H = max(height or 0, need_h)
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    for i, ln in enumerate(lines):
        draw.text((x_positions[i], y_positions[i]), ln, font=font, fill=fg)
    return img


# ---- 各样本的渲染函数 ------------------------------------------------------ #


def _s_crop_12px() -> Tuple[Image.Image, List[str]]:
    lines = ["批量重命名已完成", "共处理 128 个文件"]
    return render_block(lines, 12, width=420, height=120, pad_x=16, pad_y=20), lines


def _s_body_12px() -> Tuple[Image.Image, List[str]]:
    lines = [
        "系统设置已更新，请重新启动应用以生效",
        "本次扫描共发现 3 个待处理项目",
        "详细日志已保存到本地目录",
    ]
    return render_block(lines, 12, width=720, height=112, pad_y=16), lines


def _s_body_14px() -> Tuple[Image.Image, List[str]]:
    lines = [
        "用户反馈：截图识别在部分场景下存在偏差",
        "建议优先检查字体渲染与缩放比例设置",
        "如果问题仍然存在，请导出诊断信息",
        "我们会在两个工作日内回复处理结果",
    ]
    return render_block(lines, 14, width=760, height=150, pad_y=16), lines


def _s_body_16px() -> Tuple[Image.Image, List[str]]:
    lines = [
        "项目进度汇总：本周已完成接口联调",
        "剩余任务包括压力测试与文档整理",
        "预计下周三提交测试版本给客户",
        "风险项：第三方服务稳定性不足",
    ]
    return render_block(lines, 16, width=820, height=170, pad_y=16), lines


def _s_body_20px() -> Tuple[Image.Image, List[str]]:
    lines = [
        "生产环境部署前必须完成回归测试",
        "回滚方案需要提前写入发布单",
        "值班同学请关注监控告警面板",
    ]
    return render_block(lines, 20, width=900, height=180, pad_y=18), lines


def _s_mixed_cnen() -> Tuple[Image.Image, List[str]]:
    lines = [
        "Total=1,234.56 USD (含税)",
        "https://example.com/docs/api?tab=rate",
        "/home/user/projects/app/config.yaml",
        "订单号 A20240917-0083 状态 PAID",
    ]
    return render_block(lines, 15, width=900, height=160, pad_y=16), lines


def _s_terminal_dark() -> Tuple[Image.Image, List[str]]:
    lines = [
        "$ sudo apt install python3-opencv",
        "Reading package lists... Done",
        "def main():",
        "    engine = RapidOCR()",
        "    return engine(img)",
        "OK: 6 packages installed in 3.4s",
    ]
    img = render_block(
        lines,
        14,
        width=920,
        height=175,
        fg=DARK_FG,
        bg=DARK_BG,
        font=mono_font(14),
        pad_x=16,
        pad_y=14,
        spacing=1.45,
    )
    return img, lines


def _s_lowcontrast_gray() -> Tuple[Image.Image, List[str]]:
    lines = [
        "提示：该操作不可撤销，请谨慎确认",
        "已自动保存草稿于 5 分钟前",
        "若继续，将清除本地缓存数据",
    ]
    return render_block(lines, 13, width=700, height=110, fg=GRAY_FG, pad_y=14), lines


def _s_dense_twocol() -> Tuple[Image.Image, List[str]]:
    left = [
        "一、背景说明",
        "现有识别流程在处理小字号文本时准确率下降明显，尤其是在",
        "深色背景与低对比度场景下，误识率会进一步升高。我们回放",
        "了 128 张真实截图，对比了三种检测尺寸策略的效果，发现",
        "对小图进行过度放大不仅没有带来精度收益，反而让单张耗时",
        "增加了数倍，因此需要在分辨率与延迟之间重新寻找平衡点。",
        "本节结论将作为后续默认参数调整与回归测试的判定依据。",
    ]
    right = [
        "二、改进方案",
        "调整检测输入尺寸策略，避免对小图进行过度放大，同时保留",
        "足够的文本行分辨率；识别模型可选用更小的量化版本，以换",
        "取明显的延迟收益。评估指标采用字符错误率与 P95 延迟的",
        "组合，并要求在中文正文、中英混排与低对比度三类场景下均",
        "不出现明显退化。实验还表明，关闭方向分类器可以再节省约",
        "一成左右的耗时，而屏幕截图通常不存在整体倒置的情况。",
    ]
    font = cjk_font(14)
    lh = 24
    y0 = 18
    W, H = 1200, 230
    img = Image.new("RGB", (W, H), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i, ln in enumerate(left):
        draw.text((40, y0 + i * lh), ln, font=font, fill=(17, 17, 17))
    for i, ln in enumerate(right):
        draw.text((640, y0 + i * lh), ln, font=font, fill=(17, 17, 17))
    # 中间分隔线，模拟分栏布局
    draw.line([(600, 12), (600, H - 12)], fill=(220, 220, 220), width=2)
    return img, left + right


def _s_symbols_fullwidth() -> Tuple[Image.Image, List[str]]:
    lines = ["特殊符号：《测试》【结果】★ © 100% #tag @user ~/tmp，全角标点：！？；、（）"]
    return render_block(lines, 16, width=1000, height=64, pad_y=16), lines


def _s_settings_dialog() -> Tuple[Image.Image, List[str]]:
    fields = [
        "常规设置",
        "语言：简体中文",
        "主题：跟随系统",
        "最大历史记录：50 条",
        "自动更新：已开启",
        "快捷键：Ctrl+Shift+O",
    ]
    buttons = "取消        确定"
    title = "首选项"

    W, H = 620, 300
    img = Image.new("RGB", (W, H), (245, 246, 248))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W - 1, H - 1], outline=(200, 204, 210), width=1)
    # 标题栏
    draw.rectangle([0, 0, W - 1, 40], fill=(232, 234, 238))
    draw.line([(0, 40), (W, 40)], fill=(200, 204, 210), width=1)
    draw.text((16, 12), title, font=cjk_font(16), fill=(20, 20, 20))

    font = cjk_font(14)
    y = 60
    for ln in fields:
        draw.text((28, y), ln, font=font, fill=(30, 30, 30))
        y += 32
    # 底部按钮
    draw.text((W - 200, H - 46), buttons, font=font, fill=(30, 30, 30))

    gt = [title] + fields + [buttons]
    return img, gt


def _s_full_1080p() -> Tuple[Image.Image, List[str]]:
    W, H = 1920, 1080
    bg = (255, 255, 255)
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)

    title = "月度运营报告 - 2024 年 9 月"
    subtitle = "生成时间：2024-09-30 18:42    数据来源：内部统计平台 /metrics"
    body = [
        "本月核心业务指标整体保持增长态势，其中移动端贡献了主要增量。",
        "需要注意的风险点：新用户次日留存率连续两周下滑，建议排查引导流程。",
        "付费转化方面，年付套餐占比提升至 38%，客单价同比上涨 5.2%。",
        "客服工单量环比上升 17%，主要集中在账号同步与导出失败两个问题。",
    ]
    table_head = "指标              本月数值        同比        环比"
    table_rows = [
        "访问量            1,284,930       +12.4%      +3.1%",
        "活跃用户          342,118         +8.7%       +1.9%",
        "付费转化率        3.86%           +0.42pp     -0.21pp",
        "客单价            128.50 USD      +5.2%       +0.8%",
    ]
    footer = "详细数据请查看附件 report_2024_09.xlsx，如有疑问请联系数据平台组。"
    apx_title = "附录：分渠道明细"
    apx_head = "渠道          新增用户      活跃用户      付费率"
    apx_rows = [
        "应用商店      48,201        132,904       4.12%",
        "官网直链      12,884        31,220        5.03%",
        "社交媒体      26,417        58,731        2.88%",
        "合作伙伴      8,902         19,455        6.21%",
        "其他          3,116         7,808         1.74%",
    ]
    apx_note = "备注：渠道归因窗口为 7 天，数据每日 03:00 更新。"

    draw.rectangle([0, 0, W - 1, 84], fill=(240, 242, 245))
    draw.text((40, 24), title, font=cjk_font(26), fill=(15, 15, 15))
    draw.text((40, 110), subtitle, font=cjk_font(13), fill=(120, 124, 130))

    y = 150
    for ln in body:
        draw.text((40, y), ln, font=cjk_font(15), fill=(25, 25, 25))
        y += 32

    y += 24
    draw.text((40, y), table_head, font=cjk_font(15), fill=(40, 40, 40))
    y += 30
    for ln in table_rows:
        draw.text((40, y), ln, font=cjk_font(15), fill=(25, 25, 25))
        y += 28

    y += 30
    draw.text((40, y), footer, font=cjk_font(13), fill=(120, 124, 130))

    # 附录区块：填满页面下半部分，同时拉长 GT 文本量
    y = 600
    draw.line([(40, y - 24), (W - 40, y - 24)], fill=(225, 227, 230), width=1)
    draw.text((40, y), apx_title, font=cjk_font(17), fill=(15, 15, 15))
    y += 36
    draw.text((40, y), apx_head, font=cjk_font(15), fill=(40, 40, 40))
    y += 30
    for ln in apx_rows:
        draw.text((40, y), ln, font=cjk_font(15), fill=(25, 25, 25))
        y += 28
    y += 24
    draw.text((40, y), apx_note, font=cjk_font(13), fill=(120, 124, 130))

    gt = ([title, subtitle] + body + [table_head] + table_rows + [footer]
          + [apx_title, apx_head] + apx_rows + [apx_note])
    return img, gt


SAMPLE_DEFS: List[SampleDef] = [
    SampleDef("01_crop_small_12px", "小区域截图 12px（420x120）", "小字号正文", 12, "row",
              _s_crop_12px, small_font=True),
    SampleDef("02_body_12px_dense", "12px 中文正文三行（720x112）", "小字号正文", 12, "row",
              _s_body_12px, small_font=True),
    SampleDef("03_body_14px", "14px 中文正文四行（760x150）", "小字号正文", 14, "row",
              _s_body_14px, small_font=True),
    SampleDef("04_body_16px", "16px 中文正文四行（820x170）", "常规正文", 16, "row",
              _s_body_16px, small_font=False),
    SampleDef("05_body_20px", "20px 中文正文三行（900x180）", "常规正文", 20, "row",
              _s_body_20px, small_font=False),
    SampleDef("06_mixed_cnen", "中英混排/金额/URL/路径（900x160）", "中英混排", 15, "row",
              _s_mixed_cnen, small_font=True),
    SampleDef("07_terminal_dark", "深色终端等宽字体（920x175）", "深色背景", 14, "row",
              _s_terminal_dark, small_font=True),
    SampleDef("08_lowcontrast_gray", "低对比度 #999 灰字（700x110）", "低对比度", 13, "row",
              _s_lowcontrast_gray, small_font=True),
    SampleDef("09_dense_twocol", "密集两栏段落（1200x230）", "两栏阅读顺序", 14, "twocol",
              _s_dense_twocol, small_font=True),
    SampleDef("10_symbols_fullwidth", "特殊符号与全角标点（1000x64）", "特殊符号", 16, "row",
              _s_symbols_fullwidth, small_font=False),
    SampleDef("11_settings_dialog", "设置对话框（620x300, 14px）", "对话框 UI", 14, "row",
              _s_settings_dialog, small_font=True),
    SampleDef("12_full_1080p", "整屏报告页（1920x1080, 13-26px）", "整屏", 15, "row",
              _s_full_1080p, small_font=False),
]


def build_samples(force: bool = True) -> List[Tuple[SampleDef, Path, List[str], Tuple[int, int]]]:
    """渲染全部样本到 bench/samples/，返回 (定义, 路径, GT 行, (W,H))。"""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for sd in SAMPLE_DEFS:
        img, lines = sd.build()
        path = SAMPLES_DIR / f"{sd.sid}.png"
        if force or not path.exists():
            img.save(path)
        out.append((sd, path, list(lines), img.size))
    return out


# --------------------------------------------------------------------------- #
# 文本归一化 / CER
# --------------------------------------------------------------------------- #

_SPACE_RE = re.compile(r" +")
_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\ufeff"}


def normalize_ws(text: str) -> str:
    """主归一化规则（报告中使用的主指标）：

    1. 删除零宽字符（U+200B/200C/200D/FEFF）。
    2. 所有 Unicode 空白（含全角空格 U+3000、\t、\xa0）统一替换为半角空格。
    3. 按行切分，每行去掉首尾空格，把行内连续空格压成一个空格，丢弃空行。
    4. 用 "\\n" 重新拼接（即多行结果按行拼接后比较）。
    不做大小写折叠，不做全角/半角标点互转（NFKC 结果单独输出，仅作参考）。
    """
    s = "".join(ch for ch in text if ch not in _ZERO_WIDTH)
    s = "".join(" " if (ch.isspace() or ch == "\u3000") else ch for ch in s)
    lines = []
    for raw in s.split("\n"):
        line = _SPACE_RE.sub(" ", raw).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def normalize_ws_insensitive(text: str) -> str:
    """辅助指标：完全删除空白（消除「切框位置不同导致多/少一个空格」的测量噪声）。"""
    return re.sub(r"\s+", "", normalize_ws(text))


def normalize_nfkc(text: str) -> str:
    """辅助指标：在 normalize_ws 基础上再做 NFKC（全角标点/字母数字统一为半角）。"""
    import unicodedata

    return normalize_ws(unicodedata.normalize("NFKC", text))


def levenshtein(a: str, b: str) -> int:
    """字符级 Levenshtein 编辑距离（两行滚动数组，O(len(a)*len(b))）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1]


def cer(gt: str, hyp: str) -> float:
    """CER = 编辑距离 / GT 长度。GT 为空时：hyp 也为空记 0，否则记 1。"""
    if not gt:
        return 0.0 if not hyp else 1.0
    return levenshtein(gt, hyp) / len(gt)


# --------------------------------------------------------------------------- #
# 阅读顺序重建（把 boxes+txts 还原成多行文本）
# --------------------------------------------------------------------------- #


@dataclass
class BoxItem:
    x0: float
    y0: float
    x1: float
    y1: float
    txt: str

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return max(self.y1 - self.y0, 1e-6)


def _rows_to_lines(items: List[BoxItem]) -> List[str]:
    rows: List[Dict] = []
    for it in sorted(items, key=lambda z: z.cy):
        for row in rows:
            if abs(it.cy - row["cy"]) <= ROW_CENTER_TOL * max(row["h"], it.h):
                n = len(row["items"])
                row["cy"] = (row["cy"] * n + it.cy) / (n + 1)
                row["h"] = max(row["h"], it.h)
                row["items"].append(it)
                break
        else:
            rows.append({"cy": it.cy, "h": it.h, "items": [it]})

    rows.sort(key=lambda r: r["cy"])
    lines: List[str] = []
    for row in rows:
        segs = sorted(row["items"], key=lambda z: z.x0)
        buf = segs[0].txt
        for prev, cur in zip(segs, segs[1:]):
            gap = cur.x0 - prev.x1
            ref = max(prev.h, cur.h)
            buf += (" " if gap > SPACE_GAP_RATIO * ref else "") + cur.txt
        lines.append(buf)
    return lines


def assemble_text(boxes, txts, img_w: int, mode: str = "row") -> str:
    """几何阅读顺序重建。

    mode="row"    : 全图按 y 分行、行内按 x 排序（普通单栏文本的正确顺序）。
    mode="twocol" : 先按图片中线把文本框分成左右两栏，各自按行重建，再左栏整体接右栏整体。
    """
    items = [BoxItem(float(b[:, 0].min()), float(b[:, 1].min()),
                     float(b[:, 0].max()), float(b[:, 1].max()), t)
             for b, t in zip(boxes, txts)]
    if not items:
        return ""
    if mode == "twocol":
        mid = img_w / 2.0
        left = [it for it in items if (it.x0 + it.x1) / 2 < mid]
        right = [it for it in items if (it.x0 + it.x1) / 2 >= mid]
        return "\n".join(_rows_to_lines(left) + _rows_to_lines(right))
    return "\n".join(_rows_to_lines(items))


# --------------------------------------------------------------------------- #
# 引擎配置
# --------------------------------------------------------------------------- #


@dataclass
class EngineConfig:
    key: str
    label: str
    params: Dict[str, object]
    upscale: float = 1.0
    group: str = "main"
    desc: str = ""


def _params(model_type, ocr_version, limit_type, limit_side_len) -> Dict[str, object]:
    from rapidocr import ModelType, OCRVersion  # noqa: F401  (类型仅用于构造 Enum)

    return {
        "Det.model_type": model_type,
        "Det.ocr_version": ocr_version,
        "Rec.model_type": model_type,
        "Rec.ocr_version": ocr_version,
        "Det.limit_type": limit_type,
        "Det.limit_side_len": limit_side_len,
        "Global.log_level": "error",
    }


def build_configs() -> List[EngineConfig]:
    from rapidocr import ModelType, OCRVersion

    v6, v5 = OCRVersion.PPOCRV6, OCRVersion.PPOCRV5
    S, T, M, MOB = ModelType.SMALL, ModelType.TINY, ModelType.MEDIUM, ModelType.MOBILE

    def mixed(det_type, rec_type, limit=960) -> Dict[str, object]:
        return {
            "Det.model_type": det_type, "Det.ocr_version": v6,
            "Rec.model_type": rec_type, "Rec.ocr_version": v6,
            "Det.limit_type": "max", "Det.limit_side_len": limit,
            "Global.log_level": "error",
        }

    cfgs = [
        EngineConfig("A_default", "A 默认(v6 small, min/736)", {},
                     desc="不传 params，RapidOCR 出厂默认：PP-OCRv6 + small，limit_type=min，limit_side_len=736"),
        EngineConfig("B_v6_small_max960", "B v6 small max960",
                     _params(S, v6, "max", 960), desc="PP-OCRv6 small，limit_type=max，limit_side_len=960"),
        EngineConfig("C_v6_small_max1280", "C v6 small max1280",
                     _params(S, v6, "max", 1280), desc="PP-OCRv6 small，limit_type=max，limit_side_len=1280"),
        EngineConfig("D_v6_tiny_max960", "D v6 tiny max960",
                     _params(T, v6, "max", 960), desc="PP-OCRv6 tiny，limit_type=max，limit_side_len=960"),
        EngineConfig("E_v6_tiny_max1280", "E v6 tiny max1280",
                     _params(T, v6, "max", 1280), desc="PP-OCRv6 tiny，limit_type=max，limit_side_len=1280"),
        EngineConfig("F_v5_mobile_max960", "F v5 mobile max960",
                     _params(MOB, v5, "max", 960), desc="PP-OCRv5 mobile，limit_type=max，limit_side_len=960"),
        EngineConfig("G_v6_medium_max960", "G v6 medium max960",
                     _params(M, v6, "max", 960), desc="PP-OCRv6 medium（精度优先候选），limit_type=max，960"),
    ]
    # 「先放大 2 倍」消融实验（仅跑 font_px<=15 的样本）
    cfgs += [
        EngineConfig("B2x_max960", "B + 2x放大 (max960)", _params(S, v6, "max", 960),
                     upscale=2.0, group="upscale",
                     desc="同 B，但在送入引擎前用 INTER_CUBIC 把图放大 2 倍（max960 会把大图再压回来）"),
        EngineConfig("B2x_max1920", "B + 2x放大 (max1920)", _params(S, v6, "max", 1920),
                     upscale=2.0, group="upscale",
                     desc="同 B 的模型，但 limit_side_len=1920，保证 2 倍放大后的像素真正进入 det"),
        EngineConfig("D2x_max960", "D + 2x放大 (max960)", _params(T, v6, "max", 960),
                     upscale=2.0, group="upscale",
                     desc="tiny 模型的 2 倍放大对照组"),
    ]
    # 混合 det/rec 配置：det 与 rec 用的是两个独立模型，可分别选型（额外探索，非任务要求项）
    cfgs += [
        EngineConfig("H_mix_tinyDet_smallRec", "H tiny det + small rec max960",
                     mixed(T, S), group="mix",
                     desc="检测用 tiny（快），识别用 small（准）"),
        EngineConfig("I_mix_smallDet_tinyRec", "I small det + tiny rec max960",
                     mixed(S, T), group="mix",
                     desc="检测用 small（准），识别用 tiny（快）"),
    ]
    return cfgs


# --------------------------------------------------------------------------- #
# 运行
# --------------------------------------------------------------------------- #


@dataclass
class ImageResult:
    sid: str
    gt: str
    hyp: str
    hyp_row: str
    runs_ms: List[float] = field(default_factory=list)
    det_ms: List[float] = field(default_factory=list)
    rec_ms: List[float] = field(default_factory=list)
    scores: List[float] = field(default_factory=list)
    n_boxes: int = 0

    @property
    def median_ms(self) -> float:
        return statistics.median(self.runs_ms)


def to_bgr(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def run_image(engine, img_bgr: np.ndarray, upscale: float) -> Tuple[float, Optional[list], object]:
    """返回 (wall_ms, elapse_list, result)。"""
    src = img_bgr
    if upscale != 1.0:
        src = cv2.resize(img_bgr, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    t0 = time.perf_counter()
    res = engine(src, use_cls=False)
    wall = (time.perf_counter() - t0) * 1000.0
    elapse = list(getattr(res, "elapse_list", []) or []) if res is not None else None
    return wall, elapse, res


def evaluate(engine, sample_meta, cfg: EngineConfig, timed_runs: int = TIMED_RUNS) -> ImageResult:
    sd, path, gt_lines, (W, H) = sample_meta
    img_bgr = to_bgr(Image.open(path))
    gt = normalize_ws("\n".join(gt_lines))

    for _ in range(WARMUP_RUNS):
        run_image(engine, img_bgr, cfg.upscale)

    out = ImageResult(sid=sd.sid, gt=gt, hyp="", hyp_row="")
    last_res = None
    for _ in range(timed_runs):
        wall, elapse, res = run_image(engine, img_bgr, cfg.upscale)
        out.runs_ms.append(wall)
        last_res = res if res is not None else last_res
        if elapse:
            # 已确认（rapidocr 3.9.2 源码）：elapse_list 的元素是 time.perf_counter() 差值，单位「秒」
            det = elapse[0]
            rec = elapse[2] if len(elapse) > 2 else None
            if det is not None:
                out.det_ms.append(float(det) * 1000.0)
            if rec is not None:
                out.rec_ms.append(float(rec) * 1000.0)

    if last_res is not None:
        txts = tuple(getattr(last_res, "txts", ()) or ())
        boxes = getattr(last_res, "boxes", None)
        scores = tuple(getattr(last_res, "scores", ()) or ())
        out.scores = [float(s) for s in scores]
        out.n_boxes = len(txts)
        if boxes is not None and len(boxes) == len(txts) and len(txts) > 0:
            boxes = np.asarray(boxes, dtype=float)
            out.hyp = normalize_ws(assemble_text(boxes, txts, W, sd.layout))
            out.hyp_row = normalize_ws(assemble_text(boxes, txts, W, "row"))
        else:
            out.hyp = normalize_ws("\n".join(txts))
            out.hyp_row = out.hyp
    return out


# --------------------------------------------------------------------------- #
# 统计与打印
# --------------------------------------------------------------------------- #


def pct(values: Sequence[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=float), q))


def table(headers: Sequence[str], rows: Sequence[Sequence[str]], align: str = "l") -> str:
    cols = len(headers)
    widths = [len(str(headers[i])) for i in range(cols)]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(r[i])))
    line = "| " + " | ".join(str(headers[i]).ljust(widths[i]) for i in range(cols)) + " |"
    sep = "|" + "|".join("-" * (widths[i] + 2) for i in range(cols)) + "|"
    body = ["| " + " | ".join(str(r[i]).ljust(widths[i]) for i in range(cols)) + " |" for r in rows]
    return "\n".join([line, sep] + body)


def mark_diff(gt: str, hyp: str, limit: int = 300) -> str:
    sm = difflib.SequenceMatcher(None, gt, hyp, autojunk=False)
    parts = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            parts.append(gt[i1:i2])
        elif tag == "replace":
            parts.append(f"[-{gt[i1:i2]}-]{{+{hyp[j1:j2]}+}}")
        elif tag == "delete":
            parts.append(f"[-{gt[i1:i2]}-]")
        else:
            parts.append(f"{{+{hyp[j1:j2]}+}}")
    s = "".join(parts).replace("\n", "\\n")
    return s if len(s) <= limit else s[:limit] + " …"


def env_report() -> List[str]:
    import onnxruntime as ort
    from importlib.metadata import version

    cpu = ""
    try:
        for ln in Path("/proc/cpuinfo").read_text().splitlines():
            if ln.lower().startswith("model name"):
                cpu = ln.split(":", 1)[1].strip()
                break
    except Exception:
        cpu = "unknown"

    lines = [
        f"- Python: {sys.version.split()[0]} ({sys.executable})",
        f"- 平台: {platform.platform()}",
        f"- CPU: {cpu} (逻辑核心 {os_cpu_count()})",
        f"- rapidocr: {version('rapidocr')}",
        f"- onnxruntime: {ort.__version__} / 可用 provider: {ort.get_available_providers()}",
        f"- opencv-python: {cv2.__version__}",
        f"- numpy: {np.__version__}",
        f"- Pillow: {PIL_VERSION}",
        f"- Det/Rec 额外参数: use_cls=False（关闭方向分类），送入 ndarray（不含磁盘 IO）",
        f"- 计时: 预热 {WARMUP_RUNS} 次，计时 {TIMED_RUNS} 次取中位数",
    ]
    return lines


def os_cpu_count() -> int:
    import os

    return os.cpu_count() or 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #


def loadavg() -> Tuple[float, float, float]:
    try:
        a, b, c = Path("/proc/loadavg").read_text().split()[:3]
        return float(a), float(b), float(c)
    except Exception:
        return (float("nan"),) * 3


def evaluate_interleaved(engines: Dict[str, object], cfg_by_key: Dict[str, EngineConfig],
                         metas, timed_runs: int, quiet: bool = False
                         ) -> Tuple[Dict[str, Dict[str, ImageResult]], List[float]]:
    """按「图片外层、配置内层」交错执行：让时间上变化的系统负载对各配置影响均等。

    返回 ({config_key: {sid: ImageResult}}, 每张图开始时的 1 分钟负载均值列表)。
    """
    data: Dict[str, Dict[str, ImageResult]] = {k: {} for k in engines}
    loads: List[float] = []
    for meta in metas:
        sd = meta[0]
        loads.append(loadavg()[0])
        for key, engine in engines.items():
            cfg = cfg_by_key[key]
            try:
                r = evaluate(engine, meta, cfg, timed_runs=timed_runs)
            except Exception as ex:
                print(f"  !! {key} / {sd.sid} 推理失败：{type(ex).__name__}: {ex}")
                continue
            data[key][sd.sid] = r
            if not quiet:
                extra = ""
                if sd.layout == "twocol":
                    extra = f"  CER_row={cer(r.gt, r.hyp_row):6.3f}"
                print(f"  [{sd.sid:<22}] {key:<20} CER={cer(r.gt, r.hyp):6.3f} "
                      f"med={r.median_ms:8.1f}ms boxes={r.n_boxes:3d}{extra}")
    return data, loads


def main() -> int:
    ap = argparse.ArgumentParser(description="PrtScOCR OCR 精度-延迟基准测试")
    ap.add_argument("--filter", default="", help="只跑 key 含该子串的配置")
    ap.add_argument("--timed-runs", type=int, default=TIMED_RUNS)
    ap.add_argument("--only-sids", default="",
                    help="逗号分隔的样本 id 子串，只跑匹配的样本（用于专项延迟测量/复现单张图）")
    ap.add_argument("--json", default=str(RESULTS_JSON))
    ap.add_argument("--quiet", action="store_true", help="只打印汇总，不打印每图明细")
    args = ap.parse_args()

    if not Path(CJK_TTC).exists() or not Path(MONO_TTF).exists():
        print(f"[FATAL] 缺少字体文件：{CJK_TTC} 或 {MONO_TTF}", file=sys.stderr)
        return 2

    print("=" * 100)
    print("PrtScOCR OCR 基准测试 —— 环境")
    print("=" * 100)
    for ln in env_report():
        print(ln)

    samples = build_samples(force=True)
    if args.only_sids:
        pats = [p.strip() for p in args.only_sids.split(",") if p.strip()]
        samples = [m for m in samples if any(p in m[0].sid for p in pats)]
        print(f"[--only-sids] 只评测 {len(samples)} 张样本：{[m[0].sid for m in samples]}")
    print()
    print("=" * 100)
    print(f"测试集：{len(samples)} 张合成截图（写入 {SAMPLES_DIR}）")
    print("=" * 100)
    srows = []
    for sd, path, gt_lines, (W, H) in samples:
        srows.append([sd.sid, f"{W}x{H}", f"{sd.font_px}px", sd.category, sd.layout, len(gt_lines)])
    print(table(["样本", "尺寸", "字号", "类别", "阅读顺序", "GT行数"], srows))

    from rapidocr import RapidOCR

    configs = build_configs()
    if args.filter:
        configs = [c for c in configs if args.filter in c.key]
    main_cfgs = [c for c in configs if c.group == "main"]
    up_cfgs = [c for c in configs if c.group == "upscale"]
    mix_cfgs = [c for c in configs if c.group == "mix"]

    # ---------------- 主实验（交错执行，抵御时间变化的系统负载） ---------------- #
    print()
    print("=" * 100)
    print("主实验：构建引擎（模型缺失时 rapidocr 会自动下载）")
    print("=" * 100)
    data: Dict[str, Dict[str, ImageResult]] = {c.key: {} for c in main_cfgs}
    build_info: Dict[str, str] = {}
    engines: Dict[str, object] = {}
    eval_cfgs = main_cfgs + mix_cfgs
    for cfg in eval_cfgs + up_cfgs:
        t0 = time.perf_counter()
        try:
            engines[cfg.key] = RapidOCR(params=dict(cfg.params))
            build_info[cfg.key] = f"ok ({time.perf_counter() - t0:.2f}s)"
            print(f"  OK   {cfg.key:<20} build={time.perf_counter() - t0:5.2f}s")
        except Exception as ex:  # 诚实报告：某个配置不可用就直接跳过
            build_info[cfg.key] = f"build failed: {type(ex).__name__}: {ex}"
            print(f"  FAIL {cfg.key:<20} {type(ex).__name__}: {ex}")

    usable_main = [c for c in eval_cfgs if c.key in engines]
    cfg_by_key = {c.key: c for c in eval_cfgs + up_cfgs}
    engines_main = {c.key: engines[c.key] for c in usable_main}
    usable_mix = [c for c in mix_cfgs if c.key in engines]

    print()
    print(f"开始评测：{len(usable_main)} 个配置 x {len(samples)} 张图，"
          f"每图每配置预热 {WARMUP_RUNS} 次 + 计时 {args.timed_runs} 次（取中位数）")
    print(f"评测前 load(1/5/15) = {loadavg()}")
    t_start = time.perf_counter()
    data, loads = evaluate_interleaved(engines_main, cfg_by_key, samples,
                                       timed_runs=args.timed_runs, quiet=args.quiet)
    print(f"评测结束，用时 {time.perf_counter() - t_start:.0f}s，load(1/5/15) = {loadavg()}")
    if loads:
        print(f"各图开始时 1 分钟负载：min={min(loads):.2f} 中位={statistics.median(loads):.2f} max={max(loads):.2f}")
    usable_main_main = [c for c in main_cfgs if c.key in engines]
    usable_mix = [c for c in usable_mix]
    main_cfgs = usable_main_main

    # 追加：两栏样本在「朴素行优先」顺序下的 CER（用于说明阅读顺序问题）
    twocol_row_cer: Dict[str, float] = {}
    for key, per_img in data.items():
        for sid, r in per_img.items():
            if sid == "09_dense_twocol":
                twocol_row_cer[key] = cer(r.gt, r.hyp_row)

    # ---------------- 汇总表 ---------------- #
    def agg_rows(keys: List[str]) -> List[List[str]]:
        rows = []
        for key in keys:
            per_img = data.get(key, {})
            if not per_img:
                continue
            cers = {sid: cer(r.gt, r.hyp) for sid, r in per_img.items()}
            cers_ws = {sid: cer(normalize_ws_insensitive(r.gt), normalize_ws_insensitive(r.hyp))
                       for sid, r in per_img.items()}
            cers_nfkc = {sid: cer(normalize_nfkc(r.gt), normalize_nfkc(r.hyp))
                         for sid, r in per_img.items()}
            worst_sid = max(cers, key=lambda s: cers[s])
            med_per_img = [r.median_ms for r in per_img.values()]
            all_runs = [t for r in per_img.values() for t in r.runs_ms]
            det_all = [t for r in per_img.values() for t in r.det_ms]
            rec_all = [t for r in per_img.values() for t in r.rec_ms]
            det_med = statistics.median(det_all) if det_all else float("nan")
            rec_med = statistics.median(rec_all) if rec_all else float("nan")
            tot = (det_med if det_med == det_med else 0) + (rec_med if rec_med == rec_med else 0)
            rows.append([
                key,
                f"{statistics.fmean(cers.values()):.3f}",
                f"{statistics.median(cers.values()):.3f}",
                f"{max(cers.values()):.3f} ({worst_sid})",
                f"{statistics.fmean(cers_ws.values()):.3f}",
                f"{statistics.fmean(cers_nfkc.values()):.3f}",
                f"{statistics.median(med_per_img):.0f}",
                f"{pct(all_runs, 95):.0f}",
                f"{max(all_runs):.0f}",
                f"{det_med:.0f}",
                f"{rec_med:.0f}",
                (f"{100 * det_med / tot:.0f}/{100 * rec_med / tot:.0f}" if tot > 0 else "n/a"),
            ])
        return rows

    print()
    print("=" * 100)
    print("表 1  各配置总体指标（宏平均 CER = 12 张图 CER 的算术平均）")
    print("=" * 100)
    print(table(
        ["配置", "宏平均CER", "CER中位", "最差CER(图)", "宏平均CER(去空白)", "宏平均CER(NFKC)",
         "中位延迟ms", "P95延迟ms", "最大延迟ms", "det中位ms", "rec中位ms", "det/rec占比%"],
        agg_rows([c.key for c in main_cfgs])))

    if usable_mix:
        print()
        print("=" * 100)
        print("表 1b  混合 det/rec 配置（det 与 rec 选用不同规模模型；额外探索项）")
        print("=" * 100)
        print(table(
            ["配置", "宏平均CER", "CER中位", "最差CER(图)", "宏平均CER(去空白)", "宏平均CER(NFKC)",
             "中位延迟ms", "P95延迟ms", "最大延迟ms", "det中位ms", "rec中位ms", "det/rec占比%"],
            agg_rows([c.key for c in usable_mix])))
        print()
        print("表 1c  混合配置分图明细（CER 主口径 / 去空白 / 中位延迟 ms）")
        print("-" * 100)
        mrows = []
        for key in [c.key for c in usable_mix]:
            per_img = data.get(key, {})
            for sid in [s[0].sid for s in samples]:
                r = per_img.get(sid)
                if r:
                    mrows.append([key, sid, f"{cer(r.gt, r.hyp):.3f}",
                                  f"{cer(normalize_ws_insensitive(r.gt), normalize_ws_insensitive(r.hyp)):.3f}",
                                  f"{r.median_ms:.0f}"])
        print(table(["配置", "样本", "CER(主)", "CER(去空白)", "中位延迟ms"], mrows))

    print()
    print("表 2  分图 CER 矩阵（主归一化；单元格 = CER）")
    print("=" * 100)
    hdr = ["样本", "尺寸", "字号", "类别"] + [c.key for c in main_cfgs]
    rows2 = []
    for sd, path, gt_lines, (W, H) in samples:
        row = [sd.sid, f"{W}x{H}", f"{sd.font_px}px", sd.category]
        for c in main_cfgs:
            r = data.get(c.key, {}).get(sd.sid)
            row.append(f"{cer(r.gt, r.hyp):.3f}" if r else "n/a")
        rows2.append(row)
    print(table(hdr, rows2))

    print()
    print("表 3  分图「中位延迟」矩阵（ms）")
    print("=" * 100)
    rows3 = []
    for sd, path, gt_lines, (W, H) in samples:
        row = [sd.sid, f"{W}x{H}"]
        for c in main_cfgs:
            r = data.get(c.key, {}).get(sd.sid)
            row.append(f"{r.median_ms:.0f}" if r else "n/a")
        rows3.append(row)
    print(table(["样本", "尺寸"] + [c.key for c in main_cfgs], rows3))

    # 小区域（<=900x350，贴近「随手框选」）子集
    print()
    print("表 4  子集：小区域截图（宽<=920 且 高<=350，共 "
          f"{sum(1 for sd, p, g, (W, H) in samples if W <= 920 and H <= 350)} 张）")
    print("=" * 100)
    subset = {sd.sid for sd, p, g, (W, H) in samples if W <= 920 and H <= 350}
    rows4 = []
    for c in main_cfgs:
        per_img = {k: v for k, v in data.get(c.key, {}).items() if k in subset}
        if not per_img:
            continue
        cers = [cer(r.gt, r.hyp) for r in per_img.values()]
        meds = [r.median_ms for r in per_img.values()]
        allr = [t for r in per_img.values() for t in r.runs_ms]
        rows4.append([c.key, f"{statistics.fmean(cers):.3f}", f"{max(cers):.3f}",
                      f"{statistics.median(meds):.0f}", f"{pct(allr, 95):.0f}"])
    print(table(["配置", "宏平均CER", "最差CER", "中位延迟ms", "P95延迟ms"], rows4))

    print()
    print("表 5  两栏样本（09_dense_twocol）两种文本重建顺序对比")
    print("=" * 100)
    rows5 = []
    for c in main_cfgs:
        r = data.get(c.key, {}).get("09_dense_twocol")
        if not r:
            continue
        rows5.append([c.key, f"{cer(r.gt, r.hyp):.3f}", f"{cer(r.gt, r.hyp_row):.3f}"])
    print(table(["配置", "栏感知顺序 CER", "朴素行优先顺序 CER"], rows5))

    # ---------------- 2x 放大消融实验 ---------------- #
    print()
    print("=" * 100)
    print("表 6  「先放大 2 倍」消融实验（仅 font_px<=15 的样本；对比项含主实验中的 B / D）")
    print("=" * 100)
    up_data: Dict[str, Dict[str, ImageResult]] = {}
    small_metas = [m for m in samples if m[0].small_font]
    # 复用主实验里 B / D 的结果作为对照，无需重跑
    base_keys = ["B_v6_small_max960", "D_v6_tiny_max960"]
    usable_up = [c for c in up_cfgs if c.key in engines]
    up_main = {c.key: engines[c.key] for c in usable_up}
    if up_main:
        print(f"消融实验：{len(usable_up)} 个配置 x {len(small_metas)} 张小字样本")
        up_data, up_loads = evaluate_interleaved(up_main, cfg_by_key, small_metas,
                                                 timed_runs=args.timed_runs, quiet=args.quiet)
        print(f"消融实验结束时 load(1/5/15) = {loadavg()}")

    def abl_rows(keys: List[str]) -> List[List[str]]:
        rows = []
        for key in keys:
            per_img = up_data.get(key) or {k: v for k, v in data.get(key, {}).items()
                                           if k in {m[0].sid for m in small_metas}}
            if not per_img:
                continue
            cers = {sid: cer(r.gt, r.hyp) for sid, r in per_img.items()}
            meds = [r.median_ms for r in per_img.values()]
            cers12 = {sid: c for sid, c in cers.items() if "12px" in sid}
            meds12 = [r.median_ms for sid, r in per_img.items() if "12px" in sid]
            rows.append([
                key,
                f"{statistics.fmean(cers.values()):.3f}",
                f"{max(cers.values()):.3f}",
                f"{statistics.median(meds):.0f}",
                (f"{statistics.fmean(cers12.values()):.3f}" if cers12 else "n/a"),
                (f"{statistics.median(meds12):.0f}" if meds12 else "n/a"),
            ])
        return rows

    print()
    print(table(["配置", "宏平均CER(小字样本)", "最差CER", "中位延迟ms", "宏平均CER(仅12px)", "中位延迟ms(12px)"],
                abl_rows(base_keys + [c.key for c in up_cfgs])))

    print()
    print("表 7  放大消融分图明细（CER / 中位延迟ms）")
    print("=" * 100)
    hdr7 = ["样本", "字号", "原图尺寸"]
    for key in base_keys + [c.key for c in up_cfgs]:
        hdr7 += [f"{key} CER", f"{key} ms"]
    rows7 = []
    for meta in small_metas:
        sd, path, gt_lines, (W, H) = meta
        row = [sd.sid, f"{sd.font_px}px", f"{W}x{H}"]
        for key in base_keys + [c.key for c in up_cfgs]:
            src = up_data.get(key) or data.get(key, {})
            r = src.get(sd.sid)
            row += [f"{cer(r.gt, r.hyp):.3f}" if r else "n/a",
                    f"{r.median_ms:.0f}" if r else "n/a"]
        rows7.append(row)
    print(table(hdr7, rows7))

    # ---------------- 错误样例 ---------------- #
    print()
    print("=" * 100)
    print("表 8  实际错误样例（按推荐配置 B 与最省的 D 分别列出 CER 最大的若干张图）")
    print("=" * 100)
    for key in ["B_v6_small_max960", "D_v6_tiny_max960", "G_v6_medium_max960"]:
        per_img = data.get(key)
        if not per_img:
            continue
        print()
        print(f"--- {key} ---")
        ranked = sorted(per_img.values(), key=lambda r: -cer(r.gt, r.hyp))[:4]
        for r in ranked:
            c = cer(r.gt, r.hyp)
            print(f"  [{r.sid}] CER={c:.3f}")
            print(f"    GT : {r.gt!r}")
            print(f"    OCR: {r.hyp!r}")
            print(f"    差异: {mark_diff(r.gt, r.hyp, 400)}")

    # ---------------- 落盘 ---------------- #
    payload = {
        "env": env_report(),
        "load1_min": min(loads) if loads else None,
        "load1_median": statistics.median(loads) if loads else None,
        "load1_max": max(loads) if loads else None,
        "load1_per_image": loads,
        "build_info": build_info,
        "configs": [
            {"key": c.key, "label": c.label, "desc": c.desc, "upscale": c.upscale,
             "group": c.group,
             "params": {k: (v.value if hasattr(v, "value") else v) for k, v in c.params.items()}}
            for c in configs
        ],
        "samples": [
            {"sid": sd.sid, "title": sd.title, "category": sd.category, "font_px": sd.font_px,
             "layout": sd.layout, "size": [W, H], "small_font": sd.small_font,
             "path": str(path), "gt_lines": gt_lines,
             "gt": normalize_ws("\n".join(gt_lines))}
            for sd, path, gt_lines, (W, H) in samples
        ],
        "runs": {
            key: {
                sid: {
                    "cer": cer(r.gt, r.hyp),
                    "cer_row_order": cer(r.gt, r.hyp_row),
                    "cer_ws_insensitive": cer(normalize_ws_insensitive(r.gt),
                                              normalize_ws_insensitive(r.hyp)),
                    "cer_nfkc": cer(normalize_nfkc(r.gt), normalize_nfkc(r.hyp)),
                    "runs_ms": r.runs_ms,
                    "median_ms": r.median_ms,
                    "det_ms": r.det_ms,
                    "rec_ms": r.rec_ms,
                    "n_boxes": r.n_boxes,
                    "score_mean": (statistics.fmean(r.scores) if r.scores else None),
                    "gt": r.gt,
                    "hyp": r.hyp,
                    "hyp_row_order": r.hyp_row,
                }
                for sid, r in per_img.items()
            }
            for key, per_img in list(data.items()) + list(up_data.items())
        },
        "twocol_row_order_cer": twocol_row_cer,
    }
    Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"[已写出原始数据] {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
