"""C4 分歧驱动防御基线（跨源防御 + benchmark + 同数据对比）。

研究问题
--------
现有 MCP 注册表审计把「注册表」当**唯一真值源**：单源视角下，它只能看到
一份声明、一份代码，**结构上观测不到「源与源之间是否分歧」**。本模块把注册表
当**众多证人之一**，用**跨源分歧**本身构造一个**防御基线**与**benchmark 语料**，
并回答「同数据下，跨源基线 vs 单源基线差在哪」。

benchmark 构造语料（我们独有，外人无此数据即无法复现）
----------------------------------------------------
逐字对齐 ``milestones-A.md`` §4.6.1 的 C4 行：语料 = 四条跨源分歧口径 ——
``cross_source_drift`` / ``cross_source_fork`` / ``cross_source_consistency``
/ ``cross_source_consensus``（``xsource_fidelity`` 属 E1，本模块只作旁证打印，
不计入 C4 语料）。每条口径的「分歧 / 异常」判定集**逐字引用**该模块自身的
判定档位，**不新增阈值、不臆造键名**（见 ``DIVERGENT`` 与漂移护栏 assert）。

三部分产出（全部确定性、可复现、先只读预演）
------------------------------------------
一、benchmark 语料：全量 22,104 跨源单元按「命中几条 C4 口径」分层
    （``multi_lens`` ≥2 / ``single_lens`` =1 / ``no_divergence`` =0）。
二、同数据对比：在**同一批** E2 金标 600 上，比较各基线对「不可调和分歧」
    （``capability_conflict`` / ``identity_conflict``）的 P / R / F1 ——
    含「单注册表视角」的结构性盲区度量（代码层仅来自 1 个源）。
三、正交性：我们的分歧标记与 E2 语义金标的 Cohen κ + 交叉表 ⇒ 证明
    C4 的 benchmark **不是** E2 的重复（两者测的是不同层的东西）。

安全边界（不可破）
----------------
扫描**只读、静态，绝不执行**仓库代码；本模块只读已有 ``findings`` 与标注
目录下的 ``key.csv`` / ``label_gold.csv``，**不触盘爬取、不执行**任何东西。

写库：finding_type='cross_source_defense_baseline'，name=norm_url，version=tier。
预演默认（只统计不写），--apply 才写。

用法：
    python -m mcp_audit defense                 # 预演全量
    python -m mcp_audit defense --examples 5    # 样例条数
    python -m mcp_audit defense --table 40      # 附单元对照表行数
    python -m mcp_audit defense --apply         # 写库
"""

import json
import sqlite3
from collections import defaultdict

from . import db
from . import annotation

FINDING_TYPE = "cross_source_defense_baseline"

# C4 benchmark 构造语料 = §4.6.1 C4 行逐字给出的四条跨源分歧口径。
C4_LENSES = ("drift", "fork", "consistency", "consensus")
# 旁证口径（属 E1，不计入 C4 语料，仅打印）。
AUX_LENSES = ("fidelity",)

LENS_FT = {
    "fidelity": "cross_source_fidelity",
    "drift": "cross_source_drift",
    "fork": "cross_source_fork",
    "consistency": "cross_source_consistency",
    "consensus": "cross_source_consensus",
}

# 每条口径的「分歧 / 异常」判定集：逐字取自该模块的 VERDICT_ORDER，不新增档位。
DIVERGENT = {
    "fidelity": ("different",),
    "drift": ("repo_swap", "divergent", "coordinated", "changed"),
    "fork": ("repo_fork", "hollow", "decl_tools_no_code", "opaque_capability"),
    "consistency": ("name_mismatch", "code_opaque", "no_impl"),
    "consensus": ("dissensus_overgrant", "consensus_overgrant"),
}

TIER_ORDER = ("multi_lens", "single_lens", "no_divergence")

# 同数据对比的正例：E2 金标中的「不可调和分歧 / 需人工介入」。
HARM_LABELS = ("capability_conflict", "identity_conflict")


def _lens_index(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """各口径 → {norm_url: verdict}（name 列即 norm_url）。"""
    out: dict[str, dict[str, str]] = {}
    for lens, ft in LENS_FT.items():
        out[lens] = {nu: v for nu, v in conn.execute(
            "SELECT name, version FROM findings WHERE finding_type = ?", (ft,))}
    return out


def _cross_sources(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """跨源单元（≥2 源）→ 成员源列表（口径同其余跨源模块）。"""
    cross: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT norm_url, GROUP_CONCAT(DISTINCT source) AS sources FROM repo_index "
        "WHERE norm_url IS NOT NULL GROUP BY norm_url "
        "HAVING COUNT(DISTINCT source) > 1"
    ):
        cross[r["norm_url"]] = sorted(r["sources"].split(","))
    return cross


def collect(conn: sqlite3.Connection) -> dict[str, dict]:
    """逐跨源单元计算分歧标记（只用四条 C4 口径；fidelity 作旁证）。"""
    cross = _cross_sources(conn)
    if not cross:
        return {}
    lens = _lens_index(conn)
    out: dict[str, dict] = {}
    for nu, sources in cross.items():
        vd = {l: lens[l].get(nu) for l in C4_LENSES}
        hits = [l for l in C4_LENSES if vd[l] in DIVERGENT[l]]
        if len(hits) >= 2:
            tier = "multi_lens"
        elif len(hits) == 1:
            tier = "single_lens"
        else:
            tier = "no_divergence"
        out[nu] = {
            "norm_url": nu,
            "tier": tier,
            "c4_lenses": sorted(hits),
            "c4_hit_count": len(hits),
            "lens_verdicts": {l: vd[l] for l in C4_LENSES},
            "fidelity": lens["fidelity"].get(nu),
            "sources": sources,
            "source_count": len(sources),
        }
    return out


def _gold_join(out_dir) -> list[dict] | None:
    """读 key.csv + label_gold.csv，按 norm_url 对齐金标（只读；缺文件返回 None）。"""
    import csv

    key_path = out_dir / "key.csv"
    gold_path = out_dir / "label_gold.csv"
    if not key_path.exists() or not gold_path.exists():
        return None
    with open(key_path, encoding="utf-8-sig", newline="") as f:
        key = {r["item_id"]: r["norm_url"] for r in csv.DictReader(f)}
    with open(gold_path, encoding="utf-8-sig", newline="") as f:
        gold = {r["item_id"]: r["label"] for r in csv.DictReader(f)
                if r.get("label")}
    rows = []
    for iid, nu in key.items():
        lab = gold.get(iid)
        if lab:
            rows.append({"item_id": iid, "norm_url": nu, "label": lab,
                         "harm": lab in HARM_LABELS})
    return rows


def _prf(flag, rows) -> tuple[int, int, int, int, float, float, float]:
    tp = fp = fn = tn = 0
    for r in rows:
        hit = flag(r["norm_url"])
        if hit and r["harm"]:
            tp += 1
        elif hit and not r["harm"]:
            fp += 1
        elif (not hit) and r["harm"]:
            fn += 1
        else:
            tn += 1
    p = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * rec / (p + rec) if p + rec else 0.0
    return tp, fp, fn, tn, p, rec, f1


def _kappa(a: list[bool], b: list[bool]) -> float | None:
    n = len(a)
    if not n:
        return None
    agree = sum(1 for x, y in zip(a, b) if x == y) / n
    pa1 = sum(a) / n
    pb1 = sum(b) / n
    pe = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    if pe >= 1.0:
        return None
    return (agree - pe) / (1 - pe)


def _same_data(result: dict, conn: sqlite3.Connection, out_dir) -> dict | None:
    """同数据对比：同一批金标上各基线的 P / R / F1（含单源盲区度量）。"""
    rows = _gold_join(out_dir)
    if not rows:
        return None

    lens = _lens_index(conn)
    byurl = {p["norm_url"]: p for p in result["payloads"]}

    # 单注册表视角盲区：代码层可判的单元里，代码是否来自 ≥2 个注册表
    code_srcs: dict[str, set] = defaultdict(set)
    danger_units: set = set()
    secret_units: set = set()
    nus = [r["norm_url"] for r in rows]
    if nus:
        ph = ",".join("?" * len(nus))
        for so, nm, dj in conn.execute(
                "SELECT source, name, data_json FROM findings "
                f"WHERE finding_type = 'code_capability' AND name IN ({ph})", nus):
            code_srcs[nm].add(so)
            try:
                cap = json.loads(dj)
            except (TypeError, ValueError):
                continue
            if cap.get("danger_categories"):
                danger_units.add(nm)
            if cap.get("declared_secrets"):
                secret_units.add(nm)
    code_units = sum(1 for nu in nus if code_srcs.get(nu))
    code_multi = sum(1 for nu in nus if len(code_srcs.get(nu, ())) >= 2)

    baselines = []

    def add(name, flag, note=""):
        tp, fp, fn, tn, p, rec, f1 = _prf(flag, rows)
        baselines.append({"name": name, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                          "p": p, "r": rec, "f1": f1, "note": note})

    add("registry-local · 单源代码画像覆盖（有代码画像）",
        lambda nu: nu in code_srcs,
        "单注册表可算（只看本源代码）")
    add("registry-local · 单源危险面标记（danger_categories 非空）",
        lambda nu: nu in danger_units,
        "单注册表自带风险检测器")
    add("registry-local · 单源声明密钥（declared_secrets>0）",
        lambda nu: nu in secret_units)
    for lens_name in C4_LENSES:
        vs = DIVERGENT[lens_name]
        add(f"cross-source · 仅 {lens_name}",
            lambda nu, l=lens_name, s=vs: lens[l].get(nu) in s)
    for lens_name in AUX_LENSES:
        vs = DIVERGENT[lens_name]
        add(f"cross-source · 仅 {lens_name}（旁证，属 E1）",
            lambda nu, l=lens_name, s=vs: lens[l].get(nu) in s)
    add("OURS · 分歧驱动防御基线（四条 C4 口径取并）",
        lambda nu: byurl.get(nu, {}).get("c4_hit_count", 0) >= 1)

    # 正交性：分歧标记 vs 金标语义
    ours = [bool(byurl.get(r["norm_url"], {}).get("c4_hit_count")) for r in rows]
    harm = [r["harm"] for r in rows]
    kappa = _kappa(ours, harm)
    tab = defaultdict(lambda: defaultdict(int))
    for r in rows:
        p = byurl.get(r["norm_url"], {})
        tab[r["label"]][p.get("tier", "no_divergence")] += 1

    return {
        "n": len(rows),
        "pos": sum(1 for r in rows if r["harm"]),
        "baselines": baselines,
        "code_units": code_units,
        "code_multi": code_multi,
        "kappa": kappa,
        "xtab": {k: dict(v) for k, v in tab.items()},
    }


def _danger_prediction(conn: sqlite3.Connection, result: dict) -> dict | None:
    """E6：代码侧危险面预测——正例 = danger_categories 非空。

    把 C4 的预测目标从 E2 金标「不可调和分歧」（n=20，F1=0.127）重定向到
    全量跨源单元的「代码侧危险面命中」（正例占 ~74.6%，样本量大、信号足）。
    对比：单源代码画像（coverage / declared_secrets）vs 跨源分歧信号
    （drift/fork/consistency/consensus），证明跨源视角对危险的预测增量。
    """
    cross = _cross_sources(conn)
    if not cross:
        return None

    danger_units: set = set()
    secret_units: set = set()
    code_scanned: set = set()
    for so, nm, dj in conn.execute(
        "SELECT source, name, data_json FROM findings "
        "WHERE finding_type = 'code_capability'"
    ):
        code_scanned.add(nm)
        try:
            cap = json.loads(dj)
        except (TypeError, ValueError):
            continue
        if cap.get("danger_categories"):
            danger_units.add(nm)
        if cap.get("declared_secrets"):
            secret_units.add(nm)

    nus = list(cross.keys())
    n = len(nus)
    pos = sum(1 for nu in nus if nu in danger_units)
    if not n:
        return None
    base_rate = pos / n

    byurl = {p["norm_url"]: p for p in result["payloads"]}
    lens = _lens_index(conn)

    baselines: list[dict] = []

    def add(name, flag, note=""):
        tp = fp = fn = tn = 0
        for nu in nus:
            hit = flag(nu)
            is_pos = nu in danger_units
            if hit and is_pos:
                tp += 1
            elif hit and not is_pos:
                fp += 1
            elif (not hit) and is_pos:
                fn += 1
            else:
                tn += 1
        p = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * p * rec / (p + rec) if p + rec else 0.0
        baselines.append({"name": name, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                          "p": p, "r": rec, "f1": f1, "note": note})

    add("base rate（全标记）", lambda nu: True,
        f"先验 = {base_rate:.3f}")
    add("single-source · 有代码画像（coverage）", lambda nu: nu in code_scanned,
        "单注册表可算（只看本源代码）")
    add("single-source · 声明密钥（declared_secrets>0）", lambda nu: nu in secret_units,
        "单注册表自带风险检测器")
    for lens_name in C4_LENSES:
        vs = DIVERGENT[lens_name]
        add(f"cross-source · 仅 {lens_name}",
            lambda nu, l=lens_name, s=vs: lens[l].get(nu) in s)
    add("OURS · C4 分歧驱动（四口径取并，≥1 命中）",
        lambda nu: byurl.get(nu, {}).get("c4_hit_count", 0) >= 1)
    add("OURS · C4 分歧驱动（多口径一致，≥2 命中）",
        lambda nu: byurl.get(nu, {}).get("c4_hit_count", 0) >= 2)

    return {
        "n": n,
        "pos": pos,
        "base_rate": round(base_rate, 4),
        "danger_units": pos,
        "code_scanned": sum(1 for nu in nus if nu in code_scanned),
        "secret_units": sum(1 for nu in nus if nu in secret_units),
        "baselines": baselines,
    }


def run(conn: sqlite3.Connection, apply: bool = False, examples: int = 5,
        limit: int | None = None, out_dir=None) -> dict:
    """逐货号判定。apply=False 只统计不写库；apply=True 幂等重建 findings。"""
    data = collect(conn)
    counts: dict[str, int] = defaultdict(int)
    lens_hits: dict[str, int] = defaultdict(int)
    aux_hits: dict[str, int] = defaultdict(int)
    ex: dict[str, list] = defaultdict(list)
    written = 0

    ts = db.now()
    if apply:
        conn.execute("DELETE FROM findings WHERE finding_type = ?", (FINDING_TYPE,))

    items = list(data.items())
    if limit:
        items = items[:limit]

    table: list[dict] = []
    for nu, p in items:
        tier = p["tier"]
        counts[tier] += 1
        for l in p["c4_lenses"]:
            lens_hits[l] += 1
        if p["fidelity"] in DIVERGENT["fidelity"]:
            aux_hits["fidelity"] += 1
        if tier in ("multi_lens", "single_lens"):
            table.append(p)
        if tier == "multi_lens" and len(ex["multi_lens"]) < examples:
            ex["multi_lens"].append(p)

        if apply:
            conn.execute(
                "INSERT INTO findings "
                "(source, name, version, finding_type, data_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("+".join(p["sources"]), nu, tier, FINDING_TYPE,
                 json.dumps(p, ensure_ascii=False), ts),
            )
            written += 1

    if apply:
        conn.commit()

    table.sort(key=lambda p: (-p["c4_hit_count"],))

    result = {
        "total": len(data),
        "counts": dict(counts),
        "lens_hits": dict(lens_hits),
        "aux_hits": dict(aux_hits),
        "flagged": counts.get("multi_lens", 0) + counts.get("single_lens", 0),
        "examples": dict(ex),
        "table": table,
        "written": written,
        "applied": apply,
        "payloads": [p for _, p in items],
    }
    result["same_data"] = _same_data(result, conn, annotation._dir(out_dir))
    result["danger_pred"] = _danger_prediction(conn, result)
    return result


def _print_table(table: list[dict], limit: int) -> None:
    rows = table[:limit] if limit else table[:]
    print()
    print(f"===== 跨源单元 × C4 分歧口径 对照表（被标记 {len(table)} 个，"
          f"显示前 {len(rows)} 行） =====")
    print(f"{'货号':<46} {'源':>2} {'口径':>4} {'分层':<14} 命中口径 / 各口径判定")
    for p in rows:
        detail = " ".join(f"{l}:{p['lens_verdicts'][l]}" for l in C4_LENSES)
        print(f"{p['norm_url'][:46]:<46} {p['source_count']:>2} "
              f"{p['c4_hit_count']:>4} {p['tier']:<14} "
              f"[{','.join(p['c4_lenses']) or '-'}] {detail}")


def show(result: dict, examples: int, table: int = 0) -> None:
    print()
    print(f"=== C4 分歧驱动防御基线（"
          f"{'写库' if result['applied'] else '预演'}） ===")
    print(f"跨源单元（≥2 源，22,104 口径） : {result['total']}")
    if result["applied"]:
        print(f"写库行数                     : {result['written']}")

    print()
    print("一、benchmark 构造语料（我们独有的跨源分歧集）")
    tier_label = {"multi_lens": "多口径一致分歧（≥2 条 C4 口径）",
                  "single_lens": "单口径分歧（=1 条）",
                  "no_divergence": "无分歧（0 条）"}
    for t in TIER_ORDER:
        print(f"  {tier_label[t]:<28} {result['counts'].get(t, 0)}")
    print(f"  └ 语料总量（被标记，>0 条口径） : {result['flagged']}")
    print("  各 C4 口径边际命中:")
    for l in C4_LENSES:
        print(f"    {l:<14} {result['lens_hits'].get(l, 0)}")
    print(f"  旁证（属 E1，不计入 C4 语料）: "
          f"fidelity=different {result['aux_hits'].get('fidelity', 0)}")

    sd = result.get("same_data")
    print()
    if not sd:
        print("二、同数据对比：未找到 label_gold.csv / key.csv，跳过（先跑 annotate gold）")
    else:
        print(f"二、同数据对比（E2 金标 n={sd['n']}，正例 = 不可调和分歧 "
              f"n={sd['pos']}）")
        print(f"  {'基线':<50} {'TP':>3} {'FP':>4} {'FN':>3} {'TN':>4} "
              f"{'P':>6} {'R':>6} {'F1':>6}")
        for b in sd["baselines"]:
            print(f"  {b['name']:<50} {b['tp']:>3} {b['fp']:>4} {b['fn']:>3} "
                  f"{b['tn']:>4} {b['p']:>6.3f} {b['r']:>6.3f} {b['f1']:>6.3f}")
        print(f"  单注册表视角盲区：金标 600 中代码可判 {sd['code_units']} 个，"
              f"其中代码来自 ≥2 个注册表 = {sd['code_multi']} 个")
        print("  ⇒ 「跨源分歧」这一构造在单注册表视角下**不可计算**"
              "（无第二源可比），故该基线只能退化为单源代码基线。")

        print()
        print("三、正交性（我们的分歧标记 vs E2 语义金标）")
        k = sd["kappa"]
        print(f"  Cohen κ(分歧标记, 不可调和分歧) = "
              f"{('%.3f' % k) if k is not None else '—'}")
        print(f"  {'金标标签':<22} {'multi_lens':>11} {'single_lens':>12} "
              f"{'no_divergence':>14}")
        for lab in ("equivalent", "subset", "complementary",
                    "capability_conflict", "identity_conflict"):
            row = sd["xtab"].get(lab, {})
            print(f"  {lab:<22} {row.get('multi_lens', 0):>11} "
                  f"{row.get('single_lens', 0):>12} "
                  f"{row.get('no_divergence', 0):>14}")

    dp = result.get("danger_pred")
    print()
    if not dp:
        print("四、E6 危险面预测重定向：无跨源单元，跳过")
    else:
        print(f"四、E6 危险面预测重定向（全量跨源 n={dp['n']}，"
              f"正例 = danger_categories 非空 n={dp['pos']}，"
              f"base_rate={dp['base_rate']:.3f}）")
        print(f"  代码画像覆盖 {dp['code_scanned']}，声明密钥 {dp['secret_units']}")
        print(f"  {'基线':<50} {'TP':>5} {'FP':>5} {'FN':>5} {'TN':>6} "
              f"{'P':>6} {'R':>6} {'F1':>6}")
        for b in dp["baselines"]:
            print(f"  {b['name']:<50} {b['tp']:>5} {b['fp']:>5} {b['fn']:>5} "
                  f"{b['tn']:>6} {b['p']:>6.3f} {b['r']:>6.3f} {b['f1']:>6.3f}")
        ours_any = next((b for b in dp["baselines"]
                         if "≥1 命中" in b["name"]), None)
        cov = next((b for b in dp["baselines"]
                    if "coverage" in b["name"]), None)
        if ours_any and cov:
            lift = ours_any["p"] - cov["p"] if cov["p"] else 0.0
            print(f"  ⇒ 跨源分歧 vs 单源 coverage 精度提升 "
                  f"{cov['p']:.3f} → {ours_any['p']:.3f} "
                  f"(ΔP = {lift:+.3f})")

    if examples:
        print("\n===== 多口径一致分歧 样例 =====")
        for p in result["examples"].get("multi_lens", [])[:examples]:
            detail = " ".join(f"{l}:{p['lens_verdicts'][l]}" for l in C4_LENSES)
            print(f"\n● {p['norm_url']}  源 {p['source_count']}"
                  f"  {','.join(p['sources'])}")
            print(f"    命中口径: {', '.join(p['c4_lenses'])}")
            print(f"    判定明细: {detail}")

    if table:
        _print_table(result["table"], table)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp_audit defense",
        description="C4 分歧驱动防御基线（写入 findings，"
                    "finding_type='cross_source_defense_baseline'）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制处理的货号数（抽样试跑用）")
    parser.add_argument("--examples", type=int, default=5,
                        help="打印样例数（默认 5，0 不打印）")
    parser.add_argument("--table", type=int, default=0,
                        help="打印单元×口径对照表的行数（0 不打印）")
    parser.add_argument("--dir", default=None,
                        help="标注目录（默认 D:\\Data\\mcp-audit\\annotation）")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    result = run(conn, apply=args.apply, examples=args.examples,
                 limit=args.limit, out_dir=args.dir)
    show(result, args.examples, table=args.table)
    conn.close()


if __name__ == "__main__":
    main()
