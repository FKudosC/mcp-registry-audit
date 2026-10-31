"""概念 1 + 概念 3 + 4.5：跨源保真度（同期横向分歧 CSCD ／ 可见性上界 V(f) ／ 镜像农场图谱）。

研究问题
--------
把「抓取日」当对照组固定住，只让「源」变化——同一实体 u 的各成员源**在同一天**
对同一字段的声明是否一致？若一致率与「非同期」（源间抓取日不同）几乎一样，就说
明分歧是**结构性**的（源本身在说谎），而不是时间造成的漂移。这是所有"单源随时间
漂移"工作（如 2609.14119 silent drift）的**结构性盲区**。

同时回答一个方法论问题：**只看一个注册表会漏掉多少**？
    V(f) = 单源可见 / 全部可见（按字段分别算）
给出「prior single-registry audits 系统性低估 X%」的可计算上界。

口径（由 §二 实测定死，本模块验收线）
------------------------------------
- 单元 = 跨源货号（v_cross_source_repo，22,104）。
- **同期判定**（不得改用 repo_index.updated_at）：对每个跨源单元，取其各成员源
  **最新快照**（snapshots 中每 (source, name) 的 MAX(id)）的抓取日
  （substr(fetched_at,1,10)），数出该单元内**不同抓取日的个数 span**：
    span = 1           -> 同期（天然同期对照组，20,203）
    span = 2           -> 跨 2 日（1,880）
    span >= 3          -> 跨 3+ 日（21）
- **字段可用性**（快照口径）：某字段在某源**非空**即计入该源；逐单元按「有该字段
  的源数」聚合。全部 22,104 单元实测：desc 22,094 / 6,928（≥1源 / ≥2源）；
  code 5,409 / 617；remotes 5,885 / 156；tools 435 / 2；packages 8,640 / 0。
- **同期分歧率**（复用 analysis.py 的 desc_diff 判定，**不得**裸表重算）：以
  sources 表描述为口径，同期（span=1）子集中「≥2 源有描述」者 = 可比 5,420，
  其中 different 4,418（81.5%）；非同期对照 cross-2 可比 1,487→1,144（76.9%）、
  cross-3+ 可比 21→11（52.4%）。同期（无时间混淆）分歧率不低于跨期 ⇒
  **源本身**才是主因。

写库：finding_type='cross_source_fidelity'，name=norm_url，version=desc 口径判定
（identical / near / partial / different / insufficient）。source 列写
'+'.join(sorted(payload['sources']))。预演默认（只统计不写），--apply 才写。

另含概念 4.5「镜像农场跨源图谱」的只读聚合 `farm`（owner 集中度 top10_share +
农场四家单元 top10_share / farm_share / insufficient 占比 + 含·去农场双口径），
仅在 `run()` 返回体与报告中呈现：**不新增 finding_type、不改变写库行为**。

用法：
    python -m mcp_audit xsource                        # 预演全量
    python -m mcp_audit xsource --examples 8           # 样例条数
    python -m mcp_audit xsource --table 20             # 同期可比单元对照表
    python -m mcp_audit xsource --apply                # 写库（全部跨源单元）
    python -m mcp_audit xsource --apply --same-day-only # 只写同期子集
"""

import json
import sqlite3
from collections import Counter, defaultdict

from . import analysis, db

FINDING_TYPE = "cross_source_fidelity"

# 字段顺序（与 §二 可见性表一致）
FIELD_ORDER = ("desc", "code", "remotes", "tools", "packages")

# 镜像农场 owner 集（§一 #6 实测定死：1159+873+301+251 = 2,584）
FARM_OWNERS = ("pipeworx-io", "mcp-dir", "mcp-mirror", "csoai-org")

# 分桶（按 span 天数）
BUCKETS = ("same", "cross2", "cross3plus")
BUCKET_LABEL = {"same": "同期（span=1）", "cross2": "跨 2 日",
                "cross3plus": "跨 3+ 日"}

VERDICT_ORDER = ("identical", "near", "partial", "different",
                 analysis.INSUFFICIENT)


def _ne(v) -> bool:
    """字段非空判定：JSON 数组须非空列表；文本须非空白。"""
    if not v:
        return False
    try:
        return bool(json.loads(v))
    except (ValueError, TypeError):
        return bool(str(v).strip())


def _build_latest(conn: sqlite3.Connection) -> None:
    """建 TEMP TABLE _latest：每 (source, name) 的最新快照 + 抓取日。

    只读语义：TEMP TABLE 随连接销毁，不落盘、不改动既有表（§五 允许）。
    不用 repo_index.updated_at（那是登记时间，不是抓取日）。
    """
    conn.execute("DROP TABLE IF EXISTS _latest")
    conn.execute(
        "CREATE TEMP TABLE _latest AS "
        "SELECT s.source, s.name, substr(s.fetched_at, 1, 10) AS d, "
        "       s.description, s.tools_json, s.remotes_json, "
        "       s.code_json, s.packages_json "
        "FROM snapshots s "
        "JOIN (SELECT source, name, MAX(id) AS mid FROM snapshots "
        "      GROUP BY source, name) t ON t.mid = s.id"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS _ix_latest ON _latest(source, name)")


def collect(conn: sqlite3.Connection) -> list[dict]:
    """返回 list[dict]，每个跨源单元一条。

    必含键：norm_url / sources(list) / fetch_days(list) / same_day(bool)
            / pair_agree(float|None) / field_avail(dict) / visibility(dict)
    """
    _build_latest(conn)

    cross: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT norm_url, GROUP_CONCAT(DISTINCT source) AS sources FROM repo_index "
        "WHERE norm_url IS NOT NULL GROUP BY norm_url HAVING COUNT(DISTINCT source) > 1"
    ):
        cross[r["norm_url"]] = sorted(r["sources"].split(","))
    if not cross:
        return []

    days: dict[str, set] = defaultdict(set)
    avail: dict[str, dict] = defaultdict(lambda: defaultdict(set))
    for r in conn.execute(
        "SELECT r.norm_url, r.source, l.d, l.tools_json, l.remotes_json, "
        "       l.code_json, l.packages_json, l.description "
        "FROM repo_index r JOIN _latest l ON l.source = r.source AND l.name = r.name "
        "WHERE r.norm_url IS NOT NULL"
    ):
        nu = r["norm_url"]
        if nu not in cross:
            continue
        if r["d"]:
            days[nu].add(r["d"])
        for key, val in (("tools", r["tools_json"]), ("remotes", r["remotes_json"]),
                         ("code", r["code_json"]), ("packages", r["packages_json"]),
                         ("desc", r["description"])):
            if _ne(val):
                avail[nu][key].add(r["source"])   # 入集的是「真实源名」

    desc_by_source = analysis.collect(conn)

    out: list[dict] = []
    for nu, sources in cross.items():
        fetch_days = sorted(days.get(nu, ()))
        span = len(fetch_days)
        same_day = (span == 1)

        field_avail = {k: len(avail[nu].get(k, ())) for k in FIELD_ORDER}
        visibility = {
            k: ("multi" if field_avail[k] >= 2
                else "single" if field_avail[k] == 1 else "none")
            for k in FIELD_ORDER
        }

        by_source = desc_by_source.get(nu, {})
        verdict, sim, reps, meta, _sim_meta = (
            analysis.classify(by_source) if by_source
            else (analysis.INSUFFICIENT, None, {}, {}, {}))
        pair_agree = None if verdict == analysis.INSUFFICIENT else sim

        out.append({
            "norm_url": nu,
            "sources": sources,
            "source_count": len(sources),
            "fetch_days": fetch_days,
            "span_days": span,
            "same_day": same_day,
            "spot": ("same" if span <= 1 else "cross2" if span == 2
                     else "cross3plus"),
            "verdict": verdict,
            "pair_agree": pair_agree,
            "desc_sources": sorted(reps),
            "desc_source_count": len(reps),
            "field_avail": field_avail,
            "visibility": visibility,
            "len_min": meta.get("len_min"),
            "len_max": meta.get("len_max"),
            "sim_ceiling": meta.get("sim_ceiling"),
        })
    return out


def _bucket_stats(data: list[dict]) -> dict[str, dict]:
    """按 span 分桶统计：单元数 / 可比 / different / 判定分布。"""
    stats = {b: {"units": 0, "comparable": 0, "different": 0,
                 "verdicts": defaultdict(int)} for b in BUCKETS}
    for p in data:
        b = stats[p["spot"]]
        b["units"] += 1
        b["verdicts"][p["verdict"]] += 1
        if p["verdict"] != analysis.INSUFFICIENT:
            b["comparable"] += 1
            if p["verdict"] == "different":
                b["different"] += 1
    return stats


def _visibility(data: list[dict]) -> dict[str, dict]:
    """逐字段聚合 ≥1源 / ≥2源（分母 = 全部跨源单元）与单源占比。"""
    vis: dict[str, dict] = {}
    for k in FIELD_ORDER:
        ge1 = sum(1 for p in data if p["field_avail"][k] >= 1)
        ge2 = sum(1 for p in data if p["field_avail"][k] >= 2)
        single = ge1 - ge2
        vis[k] = {
            "ge1": ge1,
            "ge2": ge2,
            "single": single,
            "single_ratio": (single / ge1) if ge1 else None,
        }
    return vis


def _owner(norm_url: str) -> str:
    """取 norm_url 的 owner 段（host/owner/repo → owner）。"""
    parts = norm_url.split("/")
    return parts[1] if len(parts) > 1 else norm_url


def _farm(data: list[dict]) -> dict:
    """镜像农场跨源图谱 + 含/去农场双口径（概念 4.5，全只读聚合）。"""
    total = len(data)
    by_owner: dict[str, int] = defaultdict(int)
    for p in data:
        by_owner[_owner(p["norm_url"])] += 1
    ranked = sorted(by_owner.items(), key=lambda kv: (-kv[1], kv[0]))
    top10 = [[o, n] for o, n in ranked[:10]]
    top10_units = sum(n for _, n in top10)

    farm = [p for p in data if _owner(p["norm_url"]) in FARM_OWNERS]
    rest = [p for p in data if _owner(p["norm_url"]) not in FARM_OWNERS]

    def caliber(grp: list[dict]) -> dict:
        c = Counter(p["verdict"] for p in grp)
        comp = sum(v for k, v in c.items() if k != analysis.INSUFFICIENT)
        diff = c.get("different", 0)
        return {
            "units": len(grp),
            "comparable": comp,
            "different": diff,
            "different_ratio": (diff / comp) if comp else None,
            "verdicts": dict(c),
        }

    fv = Counter(p["verdict"] for p in farm)
    ins = fv.get(analysis.INSUFFICIENT, 0)
    same = [p for p in data if p["same_day"]]
    same_farm = [p for p in same if _owner(p["norm_url"]) in FARM_OWNERS]
    return {
        "owners": len(by_owner),
        "top10": top10,
        "top10_units": top10_units,
        "top10_share": (top10_units / total) if total else None,
        "farm_owners": {o: by_owner.get(o, 0) for o in FARM_OWNERS},
        "farm_units": len(farm),
        "farm_share": (len(farm) / total) if total else None,
        "farm_insufficient": ins,
        "farm_insufficient_ratio": (ins / len(farm)) if farm else None,
        "farm_verdicts": dict(fv),
        "same_day_units": len(same),
        "same_day_farm_units": len(same_farm),
        "with_farm": caliber(data),
        "without_farm": caliber(rest),
    }


def run(conn: sqlite3.Connection, apply: bool = False, examples: int = 5,
        limit: int | None = None, same_day_only: bool = False) -> dict:
    """逐跨源单元构建保真度画像。apply=False 只统计不写库。"""
    data = collect(conn)
    vis = _visibility(data)
    farm = _farm(data)
    stats = _bucket_stats(data)

    items = list(data)
    if same_day_only:
        items = [p for p in items if p["same_day"]]
    if limit:
        items = items[:limit]

    counts: dict[str, int] = defaultdict(int)
    for p in items:
        counts[p["verdict"]] += 1

    ts = db.now()
    if apply:
        conn.execute("DELETE FROM findings WHERE finding_type = ?", (FINDING_TYPE,))

    ex_diff = sorted(
        (p for p in items if p["same_day"] and p["verdict"] == "different"
         and p["pair_agree"] is not None),
        key=lambda p: p["pair_agree"])
    examples_out = ex_diff[:examples] if examples else []

    table = sorted(
        (p for p in items if p["same_day"] and p["verdict"] != analysis.INSUFFICIENT),
        key=lambda p: p["pair_agree"] if p["pair_agree"] is not None else 1.0)

    written = 0
    if apply:
        for p in items:
            conn.execute(
                "INSERT INTO findings (source, name, version, finding_type, "
                "data_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("+".join(p["sources"]), p["norm_url"], p["verdict"],
                 FINDING_TYPE, json.dumps(p, ensure_ascii=False), ts),
            )
            written += 1
        conn.commit()

    return {
        "total": len(data),
        "scanned": len(items),
        "counts": dict(counts),
        "stats": stats,
        "visibility": vis,
        "farm": farm,
        "examples": examples_out,
        "table": table,
        "written": written,
        "applied": apply,
        "same_day_only": same_day_only,
    }


def _pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "—"


def _print_table(table: list[dict], limit: int) -> None:
    rows = table[:limit] if limit else table[:]
    print()
    print(f"===== 同期可比单元对照表（共 {len(table)} 个，显示前 {len(rows)} 行） =====")
    print(f"{'货号':<46} {'源':>2} {'相似':>5} {'判定':<13} 抓取日")
    for p in rows:
        sim = f"{p['pair_agree']:.3f}" if p["pair_agree"] is not None else "—"
        print(f"{p['norm_url'][:46]:<46} {p['source_count']:>2} {sim:>5} "
              f"{p['verdict']:<13} {','.join(p['fetch_days'])}")


def show(result: dict, examples: int, table: int = 0) -> None:
    stats = result["stats"]
    print()
    print(f"=== 概念 1+3 跨源保真度（CSCD 同期横向分歧 / 可见性上界 V(f)，"
          f"{'写库' if result['applied'] else '预演'}） ===")
    print(f"跨源单元（主样本池）        : {result['total']}")
    for b in BUCKETS:
        print(f"  ├ {BUCKET_LABEL[b]:<14}    : {stats[b]['units']}")

    same = stats["same"]
    print()
    print("同期分歧（CSCD 核心，固定抓取日、只让源变化）:")
    print(f"  可比（≥2 源有描述）       : {same['comparable']}")
    dr = (same["different"] / same["comparable"]) if same["comparable"] else None
    print(f"  └ different               : {same['different']} ({_pct(dr)})")
    hist = " / ".join(f"{v} {same['verdicts'].get(v, 0)}" for v in VERDICT_ORDER)
    print(f"  判定分布                  : {hist}")

    print()
    print("非同期对照（分歧不随时间变 ⇒ 结构性）:")
    for b in ("cross2", "cross3plus"):
        s = stats[b]
        r = (s["different"] / s["comparable"]) if s["comparable"] else None
        print(f"  {BUCKET_LABEL[b]:<14} 可比 {s['comparable']:>6} → "
              f"different {s['different']:>6} ({_pct(r)})")

    print()
    print(f"跨源可见性上界 V(f)（分母 = 全部 {result['total']} 单元）:")
    print(f"  {'字段':<10}{'≥1源':>8}{'≥2源':>8}{'单源':>8}{'单源占比':>10}")
    for k in FIELD_ORDER:
        v = result["visibility"][k]
        print(f"  {k:<10}{v['ge1']:>8}{v['ge2']:>8}{v['single']:>8}"
              f"{_pct(v['single_ratio']):>10}")

    f = result["farm"]
    print()
    print("镜像农场跨源图谱（偏差敏感性，概念 4.5）:")
    print(f"  distinct owner            : {f['owners']}")
    print(f"  top-10 owner 占比         : {_pct(f['top10_share'])}"
          f"（{f['top10_units']} / {result['total']}）")
    print("  top-10 owner              : "
          + " / ".join(f"{o} {n}" for o, n in f["top10"]))
    print(f"  农场单元（{' + '.join(FARM_OWNERS)}）: {f['farm_units']}"
          f"（{_pct(f['farm_share'])}），其中 insufficient "
          f"{f['farm_insufficient']}（{_pct(f['farm_insufficient_ratio'])}）")
    print("  含农场 / 去农场（desc 分档双口径）:")
    for label, key in (("含农场", "with_farm"), ("去农场", "without_farm")):
        g = f[key]
        print(f"    {label}  单元 {g['units']:>6}  可比 {g['comparable']:>5} → "
              f"different {g['different']:>5}（{_pct(g['different_ratio'])}）")
    print(f"  同期子集中农场单元        : {f['same_day_farm_units']}"
          f" / {f['same_day_units']}（农场不影响 CSCD 同期口径）")

    if result["applied"]:
        print()
        print(f"写库行数                    : {result['written']}"
              f"{'（仅同期子集）' if result['same_day_only'] else '（全部跨源单元）'}")

    if examples and result["examples"]:
        print(f"\n===== 同期 different 样例（相似度升序） =====")
        for p in result["examples"]:
            print(f"\n● {p['norm_url']}  最高相似 {p['pair_agree']}"
                  f"  源 {p['desc_source_count']}/{p['source_count']} 有描述"
                  f"  抓取日 {','.join(p['fetch_days'])}")

    if table:
        _print_table(result["table"], table)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp_audit xsource",
        description="概念 1+3 跨源保真度（写入 findings，"
                    "finding_type='cross_source_fidelity'）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    parser.add_argument("--same-day-only", action="store_true",
                        help="只统计/写同期（span=1）子集")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制处理的单元数（抽样试跑用）")
    parser.add_argument("--examples", type=int, default=5,
                        help="打印样例数（默认 5，0 不打印）")
    parser.add_argument("--table", type=int, default=0,
                        help="打印同期可比对照表的行数（0 不打印）")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    result = run(conn, apply=args.apply, examples=args.examples,
                 limit=args.limit, same_day_only=args.same_day_only)
    show(result, args.examples, table=args.table)
    conn.close()


if __name__ == "__main__":
    main()
