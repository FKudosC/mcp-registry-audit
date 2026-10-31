"""E1-refined：跨源「声明冲突」检出（数值矛盾探针）。

为什么重做 E1
-------------
E1（``desc_diff``）用「跨源描述最高两两相似度」把货号分档，并以
``P(有害 | different)`` 作头条精确率。E1 消融（``mcp_audit e1-ablation``）
发现该头条**结构性失效**：

- 换更好的相似度度量（embed 多语言句向量）能把**跨语言假分歧**清零
  （``different`` 桶 360→72、桶内 equivalent 118→0），但精确率只从
  0.033 抬到 **0.042**（最优 cut 0.103）。
- 逐例核验 20 条有害发现：有害冲突**绝大多数是"同能力、数值自相矛盾"**
  （``101 tools`` vs ``82 tools``、``76,000+ 供应商`` vs ``103,000+``、
  ``50+ 语言`` vs ``20+ 语言``），其**语义高度相似** ⇒ 相似度越高反而
  越**不**判 different。**相似度与"有害"近乎反相关。**

⇒ 结论：E1 不能靠"换更好的相似度度量"修好。正确修法是**拆分信号**——
相似度负责证"声明一致/不一致"（保真度主轴），**有害冲突另用"数值矛盾
探针"**检出。本模块即该探针的实现，头条从"分歧率"改写为
「**声明冲突检出 P/R**」。

探针口径（可在 600 条金标上复现）
--------------------------------
对每个跨源单元（同货号被 ≥2 源声明且 ≥2 源有非空描述）：

1. 逐源从描述抽取**数值声明** ``(数值, 单位)``：显式单位表
   （tools / languages / vendors·providers / servers / models / clients），
   数值与单位间**最多允许 MAX_GAP 个修饰词**（如 ``30 mcp tools``、
   ``271 agent-payable tools``）。
2. 按单位聚合各源数值集合；**同一单位被 ≥2 源声明且取值跨源不一致
   ⇒ 判 ``capability_conflict``**（数值自相矛盾是跨源对齐层面的硬证据，
   不依赖语义阈值）。

设计取舍（有实测支撑，非拍脑袋）
--------------------------------
- **不加 L1 语义门**：原设想「embed ≥ τ 才做 L2」，但数值单位是显式
  锚点，本身已锁定"同一能力"，加语义门在实测中**反而掉精度**
  （``+embed best<0.45`` 由 P=1.000 掉到 0.110）。
- **不加作用域规则**：``read-only`` 不一致实测 P 从 1.000 掉到 0.294。
- ⇒ **只保留数值矛盾探针**。身份面（identity_conflict）漏检为主，
  需**实体（owner/repo）级**判据，不能靠描述相似度，留作后续。

实测（2026-09-21，N=600 金标，有害 20）：**tp=8 fp=0 fn=12，
P=1.000 R=0.400 F1=0.571**（见 ``milestones-B.md`` E1-refined 表）。

只读预演：``mcp_audit desc-conflict``；写库：``mcp_audit desc-conflict --apply``；
金标效度：``mcp_audit desc-conflict --eval``。
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from . import analysis, annotation, db

FINDING_TYPE = "desc_conflict"

# 与 E1 分档阈值/有害标签口径一致（避免漂移）。
HARMFUL = annotation.HARMFUL

# 数值与单位之间允许的修饰词上限（实验标定：1 即复现 milestone 的 8/0/12）。
MAX_GAP = 1

# 显式单位表：规范化单位名 -> 表面形式（英/中）。
# 只收录"跨源可就同一能力对齐的计数型声明"，避免把版本号/价格等噪声引入。
UNIT_TABLE = {
    "tools": r"tools?|工具",
    "languages": r"languages?|语言|语种",
    "vendors": r"vendors?|providers?|suppliers?|供应商|厂商|商家",
    "servers": r"servers?|服务器",
    "models": r"models?|模型",
    "clients": r"clients?|客户端",
}

_UNIT_ALT = "|".join(UNIT_TABLE.values())
# 数值声明：整数（可带千分位）或小数 + 可选 k/K 后缀 + 可选 '+' + 修饰词 + 单位。
_NUM_UNIT_RE = re.compile(
    r"(\d[\d,]*(?:\.\d+)?)\s*([kK]?)\s*\+?\s*"
    r"(?:[A-Za-z][A-Za-z-]*\s+){0,%d}" % MAX_GAP
    + r"(%s)" % _UNIT_ALT,
    re.I,
)


def _canon_unit(surface: str) -> str | None:
    for canon, pat in UNIT_TABLE.items():
        if re.fullmatch(pat, surface, re.I):
            return canon
    return None


def _to_int(numstr: str, suffix: str | None) -> int:
    v = float(numstr.replace(",", ""))
    if suffix and suffix.lower() == "k":
        v *= 1000
    return int(round(v))


def extract_numbers(text: str | None) -> dict[str, set[int]]:
    """从单条描述抽取 {规范化单位: {数值, ...}}。"""
    out: dict[str, set[int]] = defaultdict(set)
    for m in _NUM_UNIT_RE.finditer(text or ""):
        canon = _canon_unit(m.group(3))
        if not canon:
            continue
        out[canon].add(_to_int(m.group(1), m.group(2)))
    return dict(out)


def detect(by_source: dict[str, list[str]]) -> list[dict]:
    """对单个跨源单元做数值矛盾探针，返回冲突列表（空 = 未发现冲突）。

    by_source 口径同 ``analysis.collect``：{source: [归一化描述, ...]}。
    每源取代表描述（该源内去重排序后的第一个）参与抽取。
    """
    reps = {s: descs[0] for s, descs in by_source.items() if descs}
    if len(reps) < 2:
        return []

    per_source = {s: extract_numbers(t) for s, t in reps.items()}
    units = set()
    for vals in per_source.values():
        units |= set(vals)

    conflicts: list[dict] = []
    for unit in sorted(units):
        srcs = {s: vals[unit] for s, vals in per_source.items() if unit in vals}
        if len(srcs) < 2:
            continue
        union: set[int] = set()
        for v in srcs.values():
            union |= v
        if len(union) > 1:
            conflicts.append({
                "unit": unit,
                "values": {s: sorted(v) for s, v in sorted(srcs.items())},
            })
    return conflicts


def collect(conn: sqlite3.Connection) -> dict[str, dict[str, list[str]]]:
    """跨源单元分母 = 与 E1 ``analysis.collect`` 完全一致（≥2 源声明）。"""
    return analysis.collect(conn)


def run(conn: sqlite3.Connection, apply: bool = False, examples: int = 8,
        limit: int | None = None) -> dict:
    """预演 / 写库：逐跨源单元跑数值矛盾探针。"""
    data = collect(conn)
    n_conflict = 0
    unit_counter: dict[str, int] = defaultdict(int)
    sample: list[dict] = []

    ts = db.now()
    if apply:
        conn.execute("DELETE FROM findings WHERE finding_type = ?",
                     (FINDING_TYPE,))

    items = list(data.items())
    if limit:
        items = items[:limit]

    for nu, by_source in items:
        conflicts = detect(by_source)
        if conflicts:
            n_conflict += 1
            for c in conflicts:
                unit_counter[c["unit"]] += 1

        desc_sources = sum(1 for descs in by_source.values() if descs)

        if len(sample) < examples and conflicts:
            sample.append({
                "norm_url": nu,
                "source_count": len(by_source),
                "conflicts": conflicts,
            })

        if apply:
            payload = {
                "norm_url": nu,
                "source_count": len(by_source),
                "desc_source_count": desc_sources,
                "conflict": bool(conflicts),
                "signals": conflicts,
                "probe": "numeric_contradiction",
            }
            conn.execute(
                "INSERT INTO findings "
                "(source, name, version, finding_type, data_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("+".join(sorted(by_source)), nu,
                 "conflict" if conflicts else "consistent",
                 FINDING_TYPE,
                 json.dumps(payload, ensure_ascii=False), ts),
            )

    if apply:
        conn.commit()

    return {
        "total": len(data),
        "n_conflict": n_conflict,
        "unit_counter": dict(unit_counter),
        "sample": sample,
        "applied": apply,
    }


def _read_csv_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return list(csv.DictReader(io.StringIO(raw.decode(enc))))
        except UnicodeDecodeError:
            continue
    return []


def evaluate(conn: sqlite3.Connection, out_dir: Path) -> dict:
    """在 600 条金标上算探针的 P / R / F1（正例 = 有害，即能力/身份冲突）。

    只读：不写 DB。案例来源与 ``e1_ablation._load_cases`` 同口径
    （key.csv + label_gold.csv + analysis.collect）。
    """
    key_rows = _read_csv_rows(out_dir / "key.csv")
    gold_rows = _read_csv_rows(out_dir / "label_gold.csv")
    if not key_rows or not gold_rows:
        return {"error": "缺 key.csv / label_gold.csv"}
    gold = {r["item_id"]: (r.get("label") or "").strip()
            for r in gold_rows if (r.get("label") or "").strip()}
    key = {r["item_id"]: r for r in key_rows}

    desc = collect(conn)
    tp = fp = fn = tn = 0
    by_label: dict[str, dict[str, int]] = defaultdict(
        lambda: {"n": 0, "flagged": 0})
    fp_cases: list[dict] = []
    tp_cases: list[dict] = []

    for iid, lab in gold.items():
        meta = key.get(iid)
        if not meta:
            continue
        by_source = desc.get(meta.get("norm_url"))
        if not by_source:
            continue
        reps = {s: d for s, d in by_source.items() if d}
        if len(reps) < 2:
            continue

        conflicts = detect(by_source)
        flag = bool(conflicts)
        harm = lab in HARMFUL

        by_label[lab]["n"] += 1
        if flag:
            by_label[lab]["flagged"] += 1

        if flag and harm:
            tp += 1
            tp_cases.append({"item_id": iid, "label": lab, "conflicts": conflicts})
        elif flag and not harm:
            fp += 1
            fp_cases.append({"item_id": iid, "label": lab, "conflicts": conflicts})
        elif not flag and harm:
            fn += 1
        else:
            tn += 1

    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    n_harm = sum(1 for lab in gold.values() if lab in HARMFUL)
    return {
        "n_total": len(gold),
        "n_harm": n_harm,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "p": p, "r": r, "f1": f1,
        "by_label": {k: dict(v) for k, v in sorted(by_label.items())},
        "tp_cases": tp_cases,
        "fp_cases": fp_cases,
    }


def show(result: dict) -> None:
    print()
    print(f"=== 跨源声明冲突探针（"
          f"{'写库' if result['applied'] else '预演'}） ===")
    print(f"跨源单元（≥2 源）: {result['total']}")
    n = result["n_conflict"]
    rate = (n / result["total"] * 100) if result["total"] else 0.0
    print(f"  └ 数值矛盾检出   {n}  ({rate:.2f}%)")
    if result["unit_counter"]:
        parts = ", ".join(f"{u}={c}" for u, c in
                          sorted(result["unit_counter"].items(),
                                 key=lambda kv: -kv[1]))
        print(f"     按单位: {parts}")
    print()
    ex = result["sample"]
    if ex:
        print(f"===== 样例（前 {len(ex)} 个） =====")
        for s in ex:
            print(f"\n● {s['norm_url'][:60]}  源 {s['source_count']}")
            for c in s["conflicts"]:
                print(f"    [{c['unit']}] " +
                      "  ".join(f"{src}={vals}" for src, vals in
                                c["values"].items()))


def show_eval(result: dict) -> None:
    print()
    if result.get("error"):
        print(f"[!] {result['error']}")
        return
    print("=== 声明冲突探针 · 金标效度（正例=有害） ===")
    print(f"金标 {result['n_total']}（有害 {result['n_harm']}）")
    print(f"tp={result['tp']}  fp={result['fp']}  fn={result['fn']}  "
          f"tn={result['tn']}")
    print(f"P={result['p']:.3f}  R={result['r']:.3f}  F1={result['f1']:.3f}")
    print()
    print("按金标类别（n / 被探针标出）:")
    for lab, m in result["by_label"].items():
        print(f"  {lab:<20} {m['n']:>4} / {m['flagged']:>3}")
    print()
    if result["fp_cases"]:
        print("假阳（被标出但金标为良性）:")
        for c in result["fp_cases"]:
            print(f"  {c['item_id']}  [{c['label']}]  "
                  + "; ".join(f"{x['unit']}="
                              + " ".join(f"{s}:{v}" for s, v in x["values"].items())
                              for x in c["conflicts"]))
    else:
        print("假阳: 无")
    print()
    print("命中样例（前 8）:")
    for c in result["tp_cases"][:8]:
        print(f"  {c['item_id']}  [{c['label']}]  "
              + "; ".join(f"{x['unit']}="
                          + " ".join(f"{s}:{v}" for s, v in x["values"].items())
                          for x in c["conflicts"]))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp_audit desc-conflict",
        description="E1-refined 跨源声明冲突探针（数值矛盾）")
    parser.add_argument("--apply", action="store_true", help="写库")
    parser.add_argument("--eval", action="store_true",
                        help="在 600 条金标上算 P/R/F1（只读）")
    parser.add_argument("--examples", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--dir", default=None,
                        help="标注目录（默认 D:\\Data\\mcp-audit\\annotation）")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)

    if args.eval:
        show_eval(evaluate(conn, annotation._dir(args.dir)))
        conn.close()
        return

    result = run(conn, apply=args.apply, examples=args.examples,
                 limit=args.limit)
    show(result)
    conn.close()


if __name__ == "__main__":
    main()
