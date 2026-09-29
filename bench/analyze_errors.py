#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对 bench/results.json 做错误分类统计（真实字符错误 vs 空白差异 vs 全角半角混淆）。

用法：
    .venv/bin/python bench/analyze_errors.py [--json bench/results_quiet.json]
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import sys
import unicodedata
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_bench():
    spec = importlib.util.spec_from_file_location("benchmod2", HERE / "bench.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["benchmod2"] = mod
    spec.loader.exec_module(mod)
    return mod


B = _load_bench()


def classify(gt: str, hyp: str):
    """返回 (替换数, 插入数, 删除数, 全角半角混淆数, 混淆明细 Counter, 其他错误明细 Counter)。"""
    gt_ws = B.normalize_ws_insensitive(gt)
    hyp_ws = B.normalize_ws_insensitive(hyp)
    sm = difflib.SequenceMatcher(None, gt_ws, hyp_ws, autojunk=False)
    n_sub = n_ins = n_del = n_fw = 0
    fw = Counter()
    other = Counter()
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            a, b = gt_ws[i1:i2], hyp_ws[j1:j2]
            n_sub += max(len(a), len(b))
            if len(a) == len(b):
                for ca, cb in zip(a, b):
                    if unicodedata.normalize("NFKC", ca) == unicodedata.normalize("NFKC", cb):
                        n_fw += 1
                        fw[f"{ca}→{cb}"] += 1
                    else:
                        other[f"{ca}→{cb}"] += 1
            else:
                other[f"{a}→{b}"] += 1
        elif tag == "insert":
            n_ins += j2 - j1
            other[f"(多出){hyp_ws[j1:j2]}"] += j2 - j1
        else:
            n_del += i2 - i1
            other[f"(漏掉){gt_ws[i1:i2]}"] += i2 - i1
    return n_sub, n_ins, n_del, n_fw, fw, other


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(HERE / "results_quiet.json"),
                    help="结果文件，可用逗号分隔多个文件")
    args = ap.parse_args()
    paths = [p for p in args.json.split(",") if p]
    d = json.loads(Path(paths[0]).read_text(encoding="utf-8"))
    runs = {k: dict(v) for k, v in d["runs"].items()}
    for extra in paths[1:]:
        dx = json.loads(Path(extra).read_text(encoding="utf-8"))
        for k, v in dx["runs"].items():
            runs.setdefault(k, {}).update(v)
        keys = {c["key"] for c in d["configs"]}
        d["configs"] += [c for c in dx["configs"] if c["key"] not in keys]
    d["runs"] = runs
    cfg_order = [c["key"] for c in d["configs"]
                 if c["group"] in ("main", "mix") and c["key"] in runs]

    print(f"数据来源: {args.json}")
    print()
    print("| 配置 | 去空白编辑距离总数 | GT 总字符数 | 微平均 CER(去空白) | 替换 | 插入 | 删除 | 其中全角/半角混淆 | 真正的字符错误 | 空白差异(主口径) |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for k in cfg_order:
        tot_edit = tot_gt = n_sub = n_ins = n_del = n_fw = 0
        space_err = 0
        fwall = Counter()
        otherall = Counter()
        per_img_other = {}
        for sid, r in runs[k].items():
            gt_ws = B.normalize_ws_insensitive(r["gt"])
            hyp_ws = B.normalize_ws_insensitive(r["hyp"])
            tot_gt += len(gt_ws)
            tot_edit += B.levenshtein(gt_ws, hyp_ws)
            s, i, dl, f, fwc, oth = classify(r["gt"], r["hyp"])
            n_sub += s
            n_ins += i
            n_del += dl
            n_fw += f
            fwall.update(fwc)
            otherall.update(oth)
            if oth:
                per_img_other[sid] = dict(oth)
            # 主口径（空白压缩）下的空白差异数
            space_err += B.levenshtein(r["gt"], r["hyp"]) - B.levenshtein(gt_ws, hyp_ws)
        print(f"| {k} | {tot_edit} | {tot_gt} | {tot_edit / tot_gt:.4f} | {n_sub} | {n_ins} | {n_del} | "
              f"{n_fw} | {n_sub + n_ins + n_del - n_fw} | {space_err} |")
    print()

    print("## 各配置的非空白「真实/混淆」错误明细（按出现次数）")
    for k in cfg_order:
        fwall = Counter()
        otherall = Counter()
        for sid, r in runs[k].items():
            _, _, _, _, fwc, oth = classify(r["gt"], r["hyp"])
            fwall.update(fwc)
            otherall.update(oth)
        print(f"\n### {k}")
        print(f"- 全角/半角混淆（NFKC 后相同）：{dict(fwall) if fwall else '无'}")
        print(f"- 其他字符错误：{dict(otherall) if otherall else '无'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
