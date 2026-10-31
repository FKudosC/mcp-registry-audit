"""E1 相似度后端对照消融（seq / token / embed）。

问题（为什么要做这个消融）
------------------------
E1 原用 ``SequenceMatcher``（字符级）判决 desc_diff，效度实验显示
``different`` 档的**真有害率仅 0.033** —— 因为在 600 条金标里，绝大多数
被标 ``different`` 的其实是**良性跨语言对**（中英各写一遍同一能力，字符
重叠≈0 ⇒ 被判 different，但人工判 equivalent）。这是**度量器**的问题，
不是结论的问题。

本模块在同一批金标上，把相似度度量当作**唯一自变量**做对照：
  seq   ↔ token（标准库词集 + 中英词典归一）↔ embed（多语言句向量）
并对每个后端**独立标定判决阈值**（因为三种度量的数值刻度不同，
沿用同一 0.5/0.9 阈值不公平）。报告：
  1. 各后端在默认阈值下的 ``different`` 真有害率 / P / R / F1；
  2. 各后端在其最优阈值下的 P / R / F1（公平对比）；
  3. **跨语言子集**单独拆分——直接量化「跨语言假分歧」被修掉多少。

只读：不改 DB、不写 findings；仅读 key.csv / label_gold.csv + servers 表。
"""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from pathlib import Path

from . import analysis, annotation, db, sim_backends

HARMFUL = ("capability_conflict", "identity_conflict")


def _read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            import io
            return list(csv.DictReader(io.StringIO(raw.decode(enc))))
        except UnicodeDecodeError:
            continue
    return []


def _load_cases(conn, out_dir: Path) -> list[dict]:
    """金标条目 + 其描述 + 语言构成（供逐后端重算判决）。"""
    key_rows = _read_csv_rows(out_dir / "key.csv")
    gold_rows = _read_csv_rows(out_dir / "label_gold.csv")
    if not key_rows or not gold_rows:
        return []
    gold = {r["item_id"]: (r.get("label") or "").strip()
            for r in gold_rows if (r.get("label") or "").strip()}
    key = {r["item_id"]: r for r in key_rows}

    desc = analysis.collect(conn)
    cases: list[dict] = []
    for iid, lab in gold.items():
        meta = key.get(iid)
        if not meta:
            continue
        nu = meta.get("norm_url")
        by_source = desc.get(nu)
        if not by_source:
            continue
        reps_by_source = {s: ds[0] for s, ds in by_source.items() if ds}
        if len(reps_by_source) < 2:
            continue
        texts = list(reps_by_source.values())
        langs = {s: sim_backends.detect_lang(t) for s, t in reps_by_source.items()}
        cases.append({
            "item_id": iid,
            "norm_url": nu,
            "label": lab,
            "harm": lab in HARMFUL,
            "texts": texts,
            "verdict_seq": meta.get("verdict"),
            "cross_lang": len(set(langs.values())) >= 2,
            "langs": langs,
        })
    return cases


def _prf(cases: list[dict], sims: list[float], cutoff: float) -> dict:
    """flag = (best_sim < cutoff)；正例 = harm。"""
    tp = fp = fn = tn = 0
    for c, s in zip(cases, sims):
        flag = s < cutoff
        if flag and c["harm"]:
            tp += 1
        elif flag and not c["harm"]:
            fp += 1
        elif (not flag) and c["harm"]:
            fn += 1
        else:
            tn += 1
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "p": p, "r": r, "f1": f1,
            "cutoff": cutoff}


def _best_cutoff(cases: list[dict], sims: list[float]) -> dict:
    """在 [0,1] 上扫 cutoff，取 F1 最大（并列取 P 高者）。"""
    best = None
    c = 0.0
    while c <= 1.0001:
        m = _prf(cases, sims, round(c, 3))
        if best is None or (m["f1"], m["p"]) > (best["f1"], best["p"]):
            best = m
        c += 0.005
    return best


def _harm_rate(cases: list[dict], sims: list[float], cutoff: float) -> dict:
    """different 桶的真有害率（对齐 annotation.score 的口径）。"""
    flagged = [(c, s) for c, s in zip(cases, sims) if s < cutoff]
    tot = len(flagged)
    harm = sum(1 for c, _ in flagged if c["harm"])
    return {"n": tot, "harm": harm,
            "rate": (harm / tot) if tot else float("nan")}


def evaluate(conn, out_dir, backends=None, sub=None) -> dict:
    backends = backends or ["seq", "token", "embed"]
    cases_all = _load_cases(conn, out_dir)
    if not cases_all:
        return {"error": "缺 key.csv / label_gold.csv 或无匹配描述", "n": 0}

    subset = cases_all
    if sub == "cross":
        subset = [c for c in cases_all if c["cross_lang"]]
    elif sub == "same":
        subset = [c for c in cases_all if not c["cross_lang"]]

    out: dict = {
        "n_total": len(cases_all),
        "n_subset": len(subset),
        "n_cross": sum(1 for c in cases_all if c["cross_lang"]),
        "n_harm": sum(1 for c in cases_all if c["harm"]),
        "subset": sub or "all",
        "backends": {},
    }
    for b in backends:
        ok, why = sim_backends.backend_available(b)
        if not ok:
            out["backends"][b] = {"unavailable": why}
            continue
        sims = [sim_backends.best_pairwise(c["texts"], b) for c in subset]
        default_cut = sim_backends.PARTIAL
        at_default = _prf(subset, sims, default_cut)
        best = _best_cutoff(subset, sims)
        flagged = [c for c, s in zip(subset, sims) if s < default_cut]
        out["backends"][b] = {
            "default": at_default,
            "harm_rate_default": _harm_rate(subset, sims, default_cut),
            "best": best,
            "harm_rate_best": _harm_rate(subset, sims, best["cutoff"]),
            "sim_mean": sum(sims) / len(sims) if sims else 0.0,
            "n_flagged": len(flagged),
            "flag_labels": dict(Counter(c["label"] for c in flagged)),
        }
    return out


def show(result: dict) -> None:
    print()
    if result.get("error"):
        print(f"[!] {result['error']}")
        return
    print("=== E1 相似度后端对照消融 ===")
    print(f"金标条目 {result['n_total']}（有害 {result['n_harm']}，"
          f"跨语言 {result['n_cross']}）；当前子集 = {result['subset']} "
          f"(n={result['n_subset']})")
    print()
    print(f"{'后端':<8}{'默认cut':>8}{'真有害率':>10}{'P':>8}{'R':>8}{'F1':>8}"
          f"   |{'最优cut':>9}{'真有害率':>10}{'P':>8}{'R':>8}{'F1':>8}")
    for b, m in result["backends"].items():
        if "unavailable" in m:
            print(f"{b:<8}  不可用：{m['unavailable']}")
            continue
        d, bst = m["default"], m["best"]
        hd, hb = m["harm_rate_default"], m["harm_rate_best"]
        print(f"{b:<8}{d['cutoff']:>8.2f}{hd['rate'] * 100:>9.1f}%"
              f"{d['p']:>8.3f}{d['r']:>8.3f}{d['f1']:>8.3f}"
              f"   |{bst['cutoff']:>9.2f}{hb['rate'] * 100:>9.1f}%"
              f"{bst['p']:>8.3f}{bst['r']:>8.3f}{bst['f1']:>8.3f}")
    print()
    print("判 different 桶的构成（默认 cut=0.5）——用来分辨『假分歧』与『真冲突』：")
    for b, m in result["backends"].items():
        if "unavailable" in m:
            continue
        fl = m["flag_labels"]
        harm = sum(v for k, v in fl.items() if k in HARMFUL)
        equiv = fl.get("equivalent", 0)
        print(f"  {b:<6} 桶={m['n_flagged']:>4}  有害={harm:>3}  假分歧(equivalent)={equiv:>3}"
              f"  构成={fl}")
    print()
    print("读法：『默认cut』= 沿用 E1 原阈值 0.5；『最优cut』= 该度量自身刻度上"
          "F1 最高的阈值。")
    print("      真有害率 = P(有害 | 判 different)，即 E1 头条精确率。")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp_audit e1-ablation",
        description="E1 相似度后端对照消融（只读，不写库）")
    parser.add_argument("--backends", default="seq,token,embed")
    parser.add_argument("--subset", choices=("all", "cross", "same"), default="all",
                        help="all / cross(仅跨语言) / same(仅同语言)")
    parser.add_argument("--dir", default=None,
                        help="标注目录（默认 D:\\Data\\mcp-audit\\annotation）")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    backs = [b.strip() for b in args.backends.split(",") if b.strip()]
    result = evaluate(conn, annotation._dir(args.dir), backends=backs,
                      sub=None if args.subset == "all" else args.subset)
    show(result)
    conn.close()


if __name__ == "__main__":
    main()
