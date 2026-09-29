#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 bench/results.json 生成 Markdown 表格，供 bench/RESULTS.md 直接引用。

用法：
    .venv/bin/python bench/summarize.py [--json bench/results.json] [--section all|tables|errors]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load_bench():
    spec = importlib.util.spec_from_file_location("benchmod", HERE / "bench.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["benchmod"] = mod
    spec.loader.exec_module(mod)
    return mod


B = _load_bench()


def md_table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join(["---"] * len(headers)) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=str(HERE / "results.json"),
                    help="主结果文件，可用逗号分隔多个文件（后面的文件补充/覆盖前面的配置结果）")
    ap.add_argument("--json2", default="", help="第二轮结果（用于两轮取优的延迟统计）")
    ap.add_argument("--section", default="all")
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
    d2 = json.loads(Path(args.json2).read_text(encoding="utf-8")) if args.json2 else None
    d["runs"] = runs
    cfg_order = [c["key"] for c in d["configs"]
                 if c["group"] in ("main", "mix") and c["key"] in runs]
    up_order = [c["key"] for c in d["configs"] if c["group"] == "upscale" and c["key"] in runs]
    samples = d["samples"]
    sids = [s["sid"] for s in samples]

    def agg(keys):
        rows = []
        for k in keys:
            per = runs[k]
            cers = {sid: per[sid]["cer"] for sid in per}
            cers_ws = {sid: per[sid]["cer_ws_insensitive"] for sid in per}
            cers_nfkc = {sid: per[sid]["cer_nfkc"] for sid in per}
            worst = max(cers, key=lambda s: cers[s])
            meds = [per[s]["median_ms"] for s in per]
            all_runs = [t for s in per for t in per[s]["runs_ms"]]
            det = [t for s in per for t in per[s]["det_ms"]]
            rec = [t for s in per for t in per[s]["rec_ms"]]
            det_med = statistics.median(det) if det else float("nan")
            rec_med = statistics.median(rec) if rec else float("nan")
            tot = det_med + rec_med
            rows.append([
                k,
                f"{statistics.fmean(cers.values()):.3f}",
                f"{statistics.median(cers.values()):.3f}",
                f"{max(cers.values()):.3f}",
                worst,
                f"{statistics.fmean(cers_ws.values()):.3f}",
                f"{statistics.fmean(cers_nfkc.values()):.3f}",
                f"{statistics.median(meds):.0f}",
                f"{B.pct(all_runs, 95):.0f}",
                f"{max(all_runs):.0f}",
                f"{det_med:.0f}",
                f"{rec_med:.0f}",
                f"{100 * det_med / tot:.0f}/{100 * rec_med / tot:.0f}" if tot > 0 else "n/a",
            ])
        return rows

    print("## 表 A｜各配置总体指标\n")
    print(md_table(["配置", "宏平均 CER", "CER 中位", "最差 CER", "最差图", "宏平均 CER(去空白)",
                    "宏平均 CER(NFKC)", "中位延迟 ms", "P95 延迟 ms", "最大延迟 ms",
                    "det 中位 ms", "rec 中位 ms", "det/rec 占比 %"], agg(cfg_order)))
    print()

    print("## 表 B｜分图 CER（主归一化：空白压缩）\n")
    rows = []
    for s in samples:
        row = [s["sid"], f'{s["size"][0]}x{s["size"][1]}', f'{s["font_px"]}px', s["category"]]
        for k in cfg_order:
            r = runs[k].get(s["sid"])
            row.append(f'{r["cer"]:.3f}' if r else "n/a")
        rows.append(row)
    print(md_table(["样本", "尺寸", "字号", "类别"] + cfg_order, rows))
    print()

    print("## 表 B2｜分图 CER（忽略全部空白差异）\n")
    rows = []
    for s in samples:
        row = [s["sid"]]
        for k in cfg_order:
            r = runs[k].get(s["sid"])
            row.append(f'{r["cer_ws_insensitive"]:.3f}' if r else "n/a")
        rows.append(row)
    print(md_table(["样本"] + cfg_order, rows))
    print()

    print("## 表 C｜分图中位延迟（ms）\n")
    rows = []
    for s in samples:
        row = [s["sid"], f'{s["size"][0]}x{s["size"][1]}']
        for k in cfg_order:
            r = runs[k].get(s["sid"])
            row.append(f'{r["median_ms"]:.0f}' if r else "n/a")
        rows.append(row)
    print(md_table(["样本", "尺寸"] + cfg_order, rows))
    print()

    sub = [s["sid"] for s in samples if s["size"][0] <= 920 and s["size"][1] <= 350]
    print(f"## 表 D｜小区域子集（{len(sub)} 张：{'、'.join(sub)}）\n")
    rows = []
    for k in cfg_order:
        per = {sid: runs[k][sid] for sid in sub if sid in runs[k]}
        cers = [per[s]["cer"] for s in per]
        cers_ws = [per[s]["cer_ws_insensitive"] for s in per]
        meds = [per[s]["median_ms"] for s in per]
        allr = [t for s in per for t in per[s]["runs_ms"]]
        rows.append([k, f"{statistics.fmean(cers):.3f}", f"{max(cers):.3f}",
                     f"{statistics.fmean(cers_ws):.3f}",
                     f"{statistics.median(meds):.0f}", f"{B.pct(allr, 95):.0f}"])
    print(md_table(["配置", "宏平均 CER", "最差 CER", "宏平均 CER(去空白)", "中位延迟 ms", "P95 延迟 ms"], rows))
    print()

    if d2 is not None:
        runs2 = d2["runs"]
        print("## 表 D2｜两轮复测：每图取两轮较低中位数后的延迟统计（抑制外部负载影响）\n")
        print(f"- 第一轮 {args.json}：load1 min/中位/max = "
              f"{d.get('load1_min'):.2f}/{d.get('load1_median'):.2f}/{d.get('load1_max'):.2f}")
        print(f"- 第二轮 {args.json2}：load1 min/中位/max = "
              f"{d2.get('load1_min'):.2f}/{d2.get('load1_median'):.2f}/{d2.get('load1_max'):.2f}")
        print(f"- 表内「中位延迟」= 9 张小区域图各自 min(第一轮中位, 第二轮中位) 的中位数；"
              f"「P95」= 这些 min 值的 95 分位；「波动」= 两轮中位数的相对差异中位数\n")
        rows = []
        for k in cfg_order:
            pairs = [(runs[k][s]["median_ms"], runs2[k][s]["median_ms"])
                     for s in sub if s in runs[k] and k in runs2 and s in runs2[k]]
            if not pairs:
                continue
            best = [min(a, b) for a, b in pairs]
            worst = [max(a, b) for a, b in pairs]
            var = statistics.median([(b - a) / a for a, b in pairs])
            rows.append([k, f"{statistics.median(best):.0f}", f"{B.pct(best, 95):.0f}",
                         f"{max(best):.0f}", f"{statistics.median(worst):.0f}",
                         f"{100 * var:+.0f}%"])
        print(md_table(["配置", "中位延迟 ms(两轮取优)", "P95 ms(两轮取优)", "最差图 ms",
                        "两轮取差中位 ms", "第二轮相对第一轮波动"], rows))
        print()
        rows = []
        for k in cfg_order:
            row = [k]
            for s in sub:
                if k in runs and k in runs2 and s in runs[k] and s in runs2[k]:
                    a, b = runs[k][s]["median_ms"], runs2[k][s]["median_ms"]
                    row.append(f"{min(a, b):.0f}/{max(a, b):.0f}")
                else:
                    row.append("n/a")
            rows.append(row)
        print(md_table(["配置(取优/取差)"] + sub, rows))
        print()

    print("## 表 E｜两栏样本文本重建顺序对比（09_dense_twocol）\n")
    rows = []
    for k in cfg_order:
        r = runs[k].get("09_dense_twocol")
        if not r:
            continue
        rows.append([k, f'{r["cer"]:.3f}', f'{r["cer_row_order"]:.3f}'])
    print(md_table(["配置", "栏感知顺序 CER", "朴素行优先顺序 CER"], rows))
    print()

    if up_order:
        base = [k for k in ["B_v6_small_max960", "D_v6_tiny_max960"] if k in runs]
        small_ids = [s["sid"] for s in samples if s["small_font"]]
        print(f"## 表 F｜「先放大 2 倍」消融（{len(small_ids)} 张小字样本）\n")
        rows = []
        for k in base + up_order:
            per = {sid: runs[k][sid] for sid in small_ids if sid in runs[k]}
            if not per:
                continue
            cers = [per[s]["cer"] for s in per]
            cers_ws = [per[s]["cer_ws_insensitive"] for s in per]
            meds = [per[s]["median_ms"] for s in per]
            c12 = [per[s]["cer"] for s in per if "12px" in s]
            m12 = [per[s]["median_ms"] for s in per if "12px" in s]
            rows.append([k, f"{statistics.fmean(cers):.3f}", f"{max(cers):.3f}",
                         f"{statistics.fmean(cers_ws):.3f}", f"{statistics.median(meds):.0f}",
                         f"{statistics.fmean(c12):.3f}" if c12 else "n/a",
                         f"{statistics.median(m12):.0f}" if m12 else "n/a"])
        print(md_table(["配置", "宏平均 CER", "最差 CER", "宏平均 CER(去空白)", "中位延迟 ms",
                        "12px 样本宏平均 CER", "12px 样本中位延迟 ms"], rows))
        print()

        print("## 表 G｜放大消融分图明细\n")
        rows = []
        for sid in small_ids:
            s = next(x for x in samples if x["sid"] == sid)
            row = [sid, f'{s["font_px"]}px', f'{s["size"][0]}x{s["size"][1]}']
            for k in base + up_order:
                r = runs[k].get(sid)
                row += [f'{r["cer"]:.3f}' if r else "n/a", f'{r["median_ms"]:.0f}' if r else "n/a"]
            rows.append(row)
        hdrs = ["样本", "字号", "尺寸"]
        for k in base + up_order:
            hdrs += [f"{k} CER", f"{k} ms"]
        print(md_table(hdrs, rows))
        print()

    if args.section in ("all", "errors"):
        for k in ["B_v6_small_max960", "D_v6_tiny_max960", "G_v6_medium_max960"]:
            if k not in runs:
                continue
            print(f"## 表 H｜{k} 错误样例（CER 降序前 4 张）\n")
            for sid in sorted(runs[k], key=lambda s: -runs[k][s]["cer"])[:4]:
                r = runs[k][sid]
                print(f"- **{sid}** CER={r['cer']:.3f}（去空白 {r['cer_ws_insensitive']:.3f}）")
                print(f"  - GT ：`{r['gt']}`")
                print(f"  - OCR：`{r['hyp']}`")
                print(f"  - 差异：`{B.mark_diff(r['gt'], r['hyp'], 220)}`")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
