"""概念 4.4 危险面跨源共识：能力超配的「共识 / 反共识」与注册表归因（安全向）。

研究问题
--------
把一个仓库在各注册表里的**声明**（应然 expected）与它代码里**静态抽到的危险面**
（实然 actual）对照：注册表集体或个别地**少报了**它实际具备的高风险能力吗？
这是 MCP 供应链里**可归因的披露缺陷** —— 不是「这个包可能有问题」这种线索层
描述，而是「注册表 X 漏报了 N 类能力，代码可核查」这种实证论断。

能力词表（由 code_analysis 的 9 类危险面键名**逐字**给出，不得臆造）
----------------------------------------------------------------
  arbitrary_code / subprocess / shell_true / fs_destructive / network_egress /
  env_access / unsafe_deserialize / db_access / credential_paths

声明通道（应然）与实然
--------------------
  - actual  = 代码画像 ``code_capability`` 的 ``danger_categories``（实然危险面）；
  - expected= 清单 / 注册表**可声明**的权限字段映射到危险面类别：
      ``declared_env_vars``  > 0          -> env_access
      ``declared_secrets``   > 0          -> credential_paths
      ``declared_transports`` 含 http/sse -> network_egress
      （另有逐源通道：某源快照 ``remotes_json`` 非空 -> 该源声明 network_egress）
    其余 6 类危险面**注册表结构上无从声明** —— 这本身即结论的一部分。

三类输出
--------
  consensus_overgrant  有声明源列出该仓库，但代码危险面超出（全体）声明 ⇒
                       **集体权限超配**（注册表集体少报高风险能力）；
  dissensus_overgrant  同一单元各声明源的声明能力集**不一致**，且差异被代码证实
                       ⇒ 至少一个源**系统性欠披露**（可归因到具体注册表）；
  source_blame         按注册表统计「欠披露率」（该源所列、且有代码证据的跨源单元
                       中，代码危险面未被声明覆盖的占比）—— **最强攻击性结果**。

样本边界（硬约束）
----------------
  声明子集 = **435**（≥1 源声明 tools；≥2 源仅 **2**）—— 结论受声明稀疏度限制，
  **不得外推全量**；本模块所有占比均同时打印分母，供论文标注样本量上界。

安全边界（不可破）
----------------
  扫描**只读、静态，绝不执行**仓库代码；本模块只读已有 ``findings``，
  **不触盘、不执行**任何东西。

写库：finding_type='cross_source_consensus'，name=norm_url，version=verdict。
预演默认（只统计不写），--apply 才写。

用法：
    python -m mcp_audit consensus                 # 预演全量
    python -m mcp_audit consensus --examples 8    # 样例条数
    python -m mcp_audit consensus --table 40      # 附单元对照表行数
    python -m mcp_audit consensus --apply         # 写库
"""

import json
import sqlite3
from collections import defaultdict

from . import db
from . import code_analysis
from . import consistency_analysis as consistency

FINDING_TYPE = "cross_source_consensus"
CODE_TYPE = code_analysis.FINDING_TYPE

# 危险面能力词表：逐字复用 code_analysis 的 9 类键；下方 assert 作漂移护栏。
DANGER_CATEGORIES = (
    "arbitrary_code", "credential_paths", "db_access", "env_access",
    "fs_destructive", "network_egress", "shell_true", "subprocess",
    "unsafe_deserialize",
)
assert set(DANGER_CATEGORIES) == set(code_analysis._DANGER_PATTERNS), (
    "危险面键名与 code_analysis 不一致（不得臆造）")

# 判定档（优先级自上而下）
VERDICT_ORDER = ("no_code", "no_decl", "dissensus_overgrant",
                 "consensus_overgrant", "covered")

# 对照表排序：问题案例在前
_TABLE_ORDER = ("dissensus_overgrant", "consensus_overgrant", "covered",
                "no_decl", "no_code")

# 网络传输类型提示：只有 http / sse 类才算「声明了联网能力」（stdio 是本地）
_NET_TRANSPORT_HINTS = ("http", "sse")


def _loads(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _tools_count(tools_json: str | None) -> int:
    data = _loads(tools_json)
    return len(data) if isinstance(data, list) else 0


def _remotes(remotes_json: str | None) -> list:
    data = _loads(remotes_json)
    return data if isinstance(data, list) else []


def _declared_caps(profile: dict | None) -> set[str]:
    """清单/注册表级可声明能力（应然）—— 仅 3 类有语义声明通道。"""
    if not profile:
        return set()
    out: set[str] = set()
    if (profile.get("declared_env_vars") or 0) > 0:
        out.add("env_access")
    if (profile.get("declared_secrets") or 0) > 0:
        out.add("credential_paths")
    for t in (profile.get("declared_transports") or []):
        lt = str(t).lower()
        if any(h in lt for h in _NET_TRANSPORT_HINTS):
            out.add("network_egress")
            break
    return out


def _decl_index(conn: sqlite3.Connection) -> dict[str, dict]:
    """货号 -> {来源: 该源最新快照的声明}（tools_count / has_remote / remotes）。

    同一 (norm_url, source) 可能在源里被登记成多个 server name，须跨名取最大
    tools_count、并集 remote；否则逐行覆盖会漏掉声明。
    """
    acc: dict[tuple[str, str], dict] = {}
    rows = conn.execute(
        """
        SELECT ri.norm_url, ri.source, ri.name, sn.tools_json, sn.remotes_json,
               sn.title, sn.version
        FROM repo_index ri
        JOIN snapshots sn ON sn.source = ri.source AND sn.name = ri.name
        WHERE ri.norm_url IS NOT NULL
          AND sn.id = (SELECT MAX(s2.id) FROM snapshots s2
                       WHERE s2.source = ri.source AND s2.name = ri.name)
        """
    )
    for r in rows:
        key = (r["norm_url"], r["source"])
        cnt = _tools_count(r["tools_json"])
        rem = _remotes(r["remotes_json"])
        slot = acc.get(key)
        if slot is None:
            acc[key] = {
                "tools_count": cnt,
                "has_remote": bool(rem),
                "remotes": rem,
                "name": r["name"],
                "title": r["title"],
                "version": r["version"],
            }
        else:
            if cnt > slot["tools_count"]:
                slot["tools_count"] = cnt
                slot["name"] = r["name"]
                slot["title"] = r["title"]
                slot["version"] = r["version"]
            if rem:
                slot["has_remote"] = True
                slot["remotes"] = rem

    out: dict[str, dict] = defaultdict(dict)
    for (nu, src), slot in acc.items():
        out[nu][src] = {
            "name": slot["name"],
            "title": slot["title"],
            "version": slot["version"],
            "tools_count": slot["tools_count"],
            "has_remote": slot["has_remote"],
            "remotes": [x for x in slot["remotes"]][:6],
        }
    return out


def collect(conn: sqlite3.Connection) -> dict:
    """跨源货号 -> 危险面超配的共识/反共识判定（按 norm_url 分组）。"""
    cross: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT norm_url, GROUP_CONCAT(DISTINCT source) AS sources FROM repo_index "
        "WHERE norm_url IS NOT NULL GROUP BY norm_url HAVING COUNT(DISTINCT source) > 1"
    ):
        cross[r["norm_url"]] = sorted(r["sources"].split(","))
    if not cross:
        return {}

    code = consistency._code_index(conn)
    decl = _decl_index(conn)

    out: dict[str, dict] = {}
    for nu, sources in cross.items():
        c = code.get(nu)
        has_code = bool(c) and "danger_categories" in c
        per_src = decl.get(nu, {})

        actual = set((c or {}).get("danger_categories") or []) if has_code else set()
        danger_hits = (c or {}).get("danger") or {}

        declared_collective = _declared_caps(c) if has_code else set()
        per_source_declared: dict[str, set[str]] = {}
        for s in sources:
            d = set(declared_collective)
            if per_src.get(s, {}).get("has_remote"):
                d.add("network_egress")
            per_source_declared[s] = d

        declaring = sorted(s for s in sources
                           if per_src.get(s, {}).get("tools_count", 0) > 0)
        overgrant = sorted(actual - declared_collective)

        # 反共识（概念 4.4）：各声明源声明能力集不一致，差异被代码证实
        blame: dict[str, list[str]] = {}
        dissensus = False
        if len(declaring) > 1:
            groups = {s: frozenset(per_source_declared[s]) for s in declaring}
            if len(set(groups.values())) > 1:
                for s in declaring:
                    others = set().union(
                        *(per_source_declared[t] for t in declaring if t != s))
                    under = (others - per_source_declared[s]) & actual
                    if under:
                        dissensus = True
                        blame[s] = sorted(under)
        # 反共识亦含「某源声明的能力集被代码证实、另一声明源完全没提」
        consensus = bool(declaring) and bool(overgrant)

        if not has_code:
            verdict = "no_code"
        elif not declaring:
            verdict = "no_decl"
        elif dissensus:
            verdict = "dissensus_overgrant"
        elif consensus:
            verdict = "consensus_overgrant"
        else:
            verdict = "covered"

        out[nu] = {
            "norm_url": nu,
            "verdict": verdict,
            "sources": sources,
            "source_count": len(sources),
            "has_code": has_code,
            "declaring_sources": declaring,
            "actual": sorted(actual),
            "declared_collective": sorted(declared_collective),
            "per_source_declared": {s: sorted(v)
                                    for s, v in per_source_declared.items()},
            "overgrant": overgrant,
            "danger_hits": danger_hits,
            "blame": blame,
            "consensus_overgrant": consensus,
            "dissensus_overgrant": dissensus,
            "declared_env_vars": (c or {}).get("declared_env_vars"),
            "declared_secrets": (c or {}).get("declared_secrets"),
            "declared_transports": (c or {}).get("declared_transports"),
            "declared_name": (c or {}).get("declared_name"),
        }
    return out


def run(conn: sqlite3.Connection, apply: bool = False, examples: int = 5,
        limit: int | None = None) -> dict:
    """逐货号判定。apply=False 只统计不写库；apply=True 幂等重建 findings。"""
    data = collect(conn)
    counts: dict[str, int] = defaultdict(int)
    decl_counts: dict[str, int] = defaultdict(int)
    overgrant_dist: dict[str, int] = defaultdict(int)
    members: dict[str, int] = defaultdict(int)
    members_code: dict[str, int] = defaultdict(int)
    blamed: dict[str, int] = defaultdict(int)
    ex: dict[str, list] = defaultdict(list)
    with_code = 0
    danger_units = 0
    declaring_units = 0
    written = 0

    ts = db.now()
    if apply:
        conn.execute("DELETE FROM findings WHERE finding_type = ?", (FINDING_TYPE,))

    items = list(data.items())
    if limit:
        items = items[:limit]

    table: list[dict] = []
    for nu, p in items:
        v = p["verdict"]
        counts[v] += 1
        if p["has_code"]:
            with_code += 1
            if p["actual"]:
                danger_units += 1
        if p["declaring_sources"]:
            declaring_units += 1
            decl_counts[v] += 1
            table.append(p)
        for cat in p["overgrant"]:
            overgrant_dist[cat] += 1
        for s in p["sources"]:
            members[s] += 1
            if p["has_code"]:
                members_code[s] += 1
                if p["overgrant"] or s in p["blame"]:
                    blamed[s] += 1
        if p["consensus_overgrant"] and len(ex["consensus_overgrant"]) < examples:
            ex["consensus_overgrant"].append(p)
        if p["dissensus_overgrant"] and len(ex["dissensus_overgrant"]) < examples:
            ex["dissensus_overgrant"].append(p)

        if apply:
            conn.execute(
                "INSERT INTO findings "
                "(source, name, version, finding_type, data_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("+".join(p["sources"]), nu, v, FINDING_TYPE,
                 json.dumps(p, ensure_ascii=False), ts),
            )
            written += 1

    if apply:
        conn.commit()

    table.sort(key=lambda p: (
        _TABLE_ORDER.index(p["verdict"]) if p["verdict"] in _TABLE_ORDER
        else len(_TABLE_ORDER), -len(p["overgrant"])))

    blame_rows = []
    for s in members:
        mc = members_code.get(s, 0)
        b = blamed.get(s, 0)
        blame_rows.append({
            "source": s,
            "members": members[s],
            "members_code": mc,
            "blamed": b,
            "rate": (b / mc) if mc else None,
        })
    blame_rows.sort(key=lambda r: (-(r["rate"] or 0.0), -r["blamed"], r["source"]))

    return {
        "total": len(data),
        "scanned": len(items),
        "with_code": with_code,
        "danger_units": danger_units,
        "declaring_units": declaring_units,
        "counts": dict(counts),
        "decl_counts": dict(decl_counts),
        "overgrant_dist": dict(sorted(overgrant_dist.items(),
                                      key=lambda x: -x[1])),
        "source_blame": blame_rows,
        "examples": dict(ex),
        "table": table,
        "written": written,
        "applied": apply,
    }


def _print_table(table: list[dict], limit: int) -> None:
    rows = table[:limit] if limit else table[:]
    print()
    print(f"===== 危险面 × 声明 对照表（声明子集 {len(table)} 个，"
          f"显示前 {len(rows)} 行） =====")
    print(f"{'货号':<44} {'源':>2} {'声明':>4} {'实然':>4} {'超配':>4} "
          f"{'判定':<20} 超配类别 / 归因")
    for p in rows:
        blame = ";".join(f"{s}:{','.join(c)}" for s, c in p["blame"].items())
        note = f"超配={','.join(p['overgrant'])}" + (f" | 欠披露 {blame}" if blame else "")
        print(f"{p['norm_url'][:44]:<44} {p['source_count']:>2} "
              f"{len(p['declaring_sources']):>4} {len(p['actual']):>4} "
              f"{len(p['overgrant']):>4} {p['verdict']:<20} {note[:80]}")


def show(result: dict, examples: int, table: int = 0) -> None:
    print()
    print(f"=== 概念 4.4 危险面跨源共识（'"
          f"{'写库' if result['applied'] else '预演'}） ===")
    print(f"跨源单元（≥2 源，22,104 口径） : {result['total']}")
    print(f"  └ 有代码画像                : {result['with_code']}")
    print(f"  └ 其中含危险面              : {result['danger_units']}")
    print(f"声明子集（≥1 源声明 tools）    : {result['declaring_units']}"
          f"  ← 结论边界，不得外推全量")
    if result["applied"]:
        print(f"写库行数                     : {result['written']}")
    print()
    print("判定分布（全部跨源单元）:")
    for v in VERDICT_ORDER:
        print(f"  {v:<20} {result['counts'].get(v, 0)}")
    print()
    print(f"判定分布（声明子集，n={result['declaring_units']}）:")
    for v in VERDICT_ORDER:
        n = result["decl_counts"].get(v, 0)
        if n:
            print(f"  {v:<20} {n}")
    print()
    print("超配类别分布（声明未覆盖、代码可核查的单元数）:")
    if not result["overgrant_dist"]:
        print("  （无）")
    for cat in DANGER_CATEGORIES:
        n = result["overgrant_dist"].get(cat, 0)
        if n:
            print(f"  {cat:<22} {n}")
    print()
    print("source_blame（按注册表欠披露率排序；分母=该源所列且有代码证据的跨源单元）:")
    print(f"  {'源':<14} {'成员':>6} {'有码':>6} {'欠披露':>6} {'欠披露率':>8}")
    for r in result["source_blame"]:
        rate = f"{r['rate'] * 100:.1f}%" if r["rate"] is not None else "—"
        print(f"  {r['source']:<14} {r['members']:>6} {r['members_code']:>6} "
              f"{r['blamed']:>6} {rate:>8}")

    if examples:
        for verdict in ("dissensus_overgrant", "consensus_overgrant"):
            subset = result["examples"].get(verdict)
            if not subset:
                continue
            print(f"\n===== {verdict} 样例 =====")
            for p in subset[:examples]:
                print(f"\n● {p['norm_url']}  源 {p['source_count']}"
                      f"  {','.join(p['sources'])}")
                print(f"    实然危险面: {', '.join(p['actual']) or '-'}")
                print(f"    声明能力  : {', '.join(p['declared_collective']) or '-'}"
                      f"（env={p['declared_env_vars']} secret={p['declared_secrets']}"
                      f" transports={p['declared_transports']}）")
                print(f"    超配类别  : {', '.join(p['overgrant']) or '-'}")
                if p["blame"]:
                    for s, cats in p["blame"].items():
                        print(f"    欠披露归因: {s} 未声明 {', '.join(cats)}"
                              f"（代码证实）")

    if table:
        _print_table(result["table"], table)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp_audit consensus",
        description="概念 4.4 危险面跨源共识（写入 findings，"
                    "finding_type='cross_source_consensus'）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制处理的货号数（抽样试跑用）")
    parser.add_argument("--examples", type=int, default=5,
                        help="打印样例数（默认 5，0 不打印）")
    parser.add_argument("--table", type=int, default=0,
                        help="打印危险面×声明对照表的行数（0 不打印）")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    result = run(conn, apply=args.apply, examples=args.examples, limit=args.limit)
    show(result, args.examples, table=args.table)
    conn.close()


if __name__ == "__main__":
    main()
