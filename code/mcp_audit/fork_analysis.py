"""C2 同码异面 / 跨源分叉（声明面分歧 vs 代码层，横截面）。

研究问题
--------
一个 MCP 货号（norm_url）被 ≥2 个源同时声明 —— 各注册表给出的**声明面**
（能力名 / 远程接口 / 身份）是否一致？"同码异面"指：底层是**同一份代码**
（norm_url 去重后同属一个实体），但各源声明的能力面并不相同。本模块把
**声明层**（各源对该货号的声明集）与**代码层**（对已克隆仓库静态抽取的
真实能力，来自 code_capability）对齐，刻画其分叉。

口径（E3 重定义）
----------------
- 单元 = 跨源货号（同一 norm_url 被 ≥2 个源声明，即 v_cross_source_repo）。
- **声明面分歧（同码异面，头条判定）**：同一 norm_url 下，各源声明集
  ``D_s = tools 名 ∪ remotes(type/url) 抽词 ∪ 描述抽词``（见
  ``cap_extract.decl_caps``）。≥2 源有非空 D_s 且两两 Jaccard < 1 ⇒
  ``decl_surface_divergence``。这是 C2 的**直接测量**：同一份代码，注册表
  把能力面描述得不一致。
- 代码层画像取自 code_capability（finding_type='code_capability'，逐货号一条）。
- 无法评估声明面分歧者（<2 源有非空 D_s），退回**代码侧 6 档**（回归不变）：
    no_code            该货号无代码画像（未克隆 / 克隆失败）→ 无法评估；
    repo_fork          代码内 server.json 自述的仓库 ≠ 货号 —— 代码自称另一仓库；
    hollow             代码可扫但 tool_signals=0、无工具名、无 manifest —— 空壳
                       （挂 MCP 之名，无 MCP 之实）；
    decl_tools_no_code 有源声明了工具，代码却无任何工具注册信号 —— 声明–代码错位；
    opaque_capability  代码有危险面（任意代码执行/子进程/外联/凭据路径等），
                       但无任何工具注册信号 —— 能力不透明；
    consistent         未检出分叉。

说明
----
原 C2 在 norm_url 组内比对 **code_capability**（每 norm_url 仅一行 ⇒ 组内恒等
⇒ Jaccard ≡ 1，结构性空转）。E3 把比较对象换成**各源声明面**，才真正命中
"同码异面"的字面含义（详见 milestones-B.md §E3）。代码侧 6 档作为子口径保留
于 payload 的 ``code_verdict`` 字段，供下游再切片。

写库：finding_type='cross_source_fork'，name=norm_url，version=verdict。
预演默认（只统计不写），--apply 才写。

用法：
    python -m mcp_audit fork                 # 预演全量
    python -m mcp_audit fork --limit 2000    # 抽样试跑
    python -m mcp_audit fork --examples 8    # 样例条数
    python -m mcp_audit fork --apply         # 写库
"""

import json
import sqlite3
from collections import defaultdict

from . import db
from . import code_analysis
from . import cap_extract

FINDING_TYPE = "cross_source_fork"
CODE_TYPE = code_analysis.FINDING_TYPE

# E3：C2 重定义为「同码异面」——同一 norm_url（同一份代码）下，各注册表声明的
# 能力面（tools 名 ∪ remotes(type/url) 抽词 ∪ 描述抽词，见 cap_extract.decl_caps）
# 是否不同；只要 ≥2 源有非空声明集且两两 Jaccard < 1，即为同码异面。
SURFACE_DIVERGENCE = "decl_surface_divergence"

VERDICT_ORDER = ("decl_surface_divergence", "repo_fork", "hollow",
                 "decl_tools_no_code", "opaque_capability",
                 "consistent", "no_code")

# 代码侧判定的判定档（与声明面分歧解耦，供下游再切片）
CODE_VERDICTS = ("repo_fork", "hollow", "decl_tools_no_code",
                 "opaque_capability", "consistent", "no_code")


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


# 模板占位 owner：server.json 里常见 __owner__ / your-org 之类，非真实货号，
# 不能当作「代码自述另一仓库」的证据（否则 repo_fork 会出现假阳）。
_PLACEHOLDER_OWNERS = frozenset({
    "owner", "your-org", "your_org", "your-username", "your_username",
    "username", "your-org-name", "example", "example-org", "org",
    "your-github-username", "your-github-org", "changeme",
})


def _is_real_repo_id(repo_id: str | None) -> bool:
    """判断 server.json 自述的货号是否可信（非模板占位）。"""
    if not repo_id:
        return False
    parts = repo_id.split("/")
    if len(parts) != 3:
        return False
    owner = parts[1]
    if owner.startswith("__") and owner.endswith("__"):
        return False
    return owner not in _PLACEHOLDER_OWNERS


def _code_index(conn: sqlite3.Connection) -> dict[str, dict]:
    """货号 -> 代码层画像（取自 code_capability findings）。"""
    out: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT name, data_json FROM findings WHERE finding_type = ?", (CODE_TYPE,)
    ):
        d = _loads(r["data_json"])
        if not isinstance(d, dict):
            continue
        out[d.get("norm_url") or r["name"]] = d
    return out


def _decl_index(conn: sqlite3.Connection) -> dict[str, dict]:
    """货号 -> {source: 声明层画像}（各源最新快照的 tools/身份/声明集）。

    E3：同一 (norm_url, source) 可被登记成多个 server name，须跨名聚合
    ``decl_caps``（tools 名 ∪ remotes(type/url) 抽词 ∪ 描述抽词）并集，
    否则逐行覆盖会漏掉声明面。
    """
    acc: dict[tuple[str, str], dict] = {}
    rows = conn.execute(
        """
        SELECT ri.norm_url, ri.source, ri.name, sn.tools_json, sn.title,
               sn.version, sn.description, sn.remotes_json
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
        caps = cap_extract.decl_caps(r["tools_json"], r["remotes_json"],
                                     r["description"])
        slot = acc.get(key)
        if slot is None:
            acc[key] = {
                "name": r["name"],
                "title": r["title"],
                "version": r["version"],
                "tools_count": cnt,
                "caps": set(caps),
            }
        else:
            slot["caps"].update(caps)
            if cnt > slot["tools_count"]:
                slot["tools_count"] = cnt
                slot["name"] = r["name"]
                slot["title"] = r["title"]
                slot["version"] = r["version"]

    out: dict[str, dict] = defaultdict(dict)
    for (nu, src), slot in acc.items():
        out[nu][src] = {
            "name": slot["name"],
            "title": slot["title"],
            "version": slot["version"],
            "tools_count": slot["tools_count"],
            "decl_caps": sorted(slot["caps"]),
        }
    return out


def collect(conn: sqlite3.Connection) -> dict:
    """构建跨源货号 -> 分叉判定结果。"""
    cross: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT norm_url, GROUP_CONCAT(DISTINCT source) AS sources FROM repo_index "
        "WHERE norm_url IS NOT NULL GROUP BY norm_url HAVING COUNT(DISTINCT source) > 1"
    ):
        cross[r["norm_url"]] = sorted(r["sources"].split(","))
    if not cross:
        return {}

    code = _code_index(conn)
    decl = _decl_index(conn)

    out: dict[str, dict] = {}
    for nu, sources in cross.items():
        c = code.get(nu)
        decls = {s: decl.get(nu, {}).get(s) for s in sources}
        decl_tools = {s: d["tools_count"] for s, d in decls.items() if d}
        decl_tool_sources = sorted(s for s, n in decl_tools.items() if n > 0)
        declared_tools_max = max(decl_tools.values(), default=0)

        # E3：各源声明集 D_s（tools 名 ∪ remotes 抽词 ∪ 描述抽词）。同一 norm_url
        # = 同一份代码，各源声明面本该一致；两两 Jaccard < 1 即「同码异面」。
        decl_caps_src: dict[str, frozenset] = {
            s: frozenset(d["decl_caps"])
            for s, d in decls.items() if d and d.get("decl_caps")
        }
        cap_sources = sorted(decl_caps_src)
        decl_min_jaccard = None
        if len(cap_sources) >= 2:
            jacs = []
            for i, a in enumerate(cap_sources):
                for b in cap_sources[i + 1:]:
                    jacs.append(cap_extract.jaccard(decl_caps_src[a],
                                                    decl_caps_src[b]))
            decl_min_jaccard = min(jacs) if jacs else None
        decl_surface_vary = (decl_min_jaccard is not None
                             and decl_min_jaccard < 1.0)

        # 代码侧判定（与声明面分歧解耦，仍按原 6 档算，供下游再切片 / 展示）
        code_brief = None
        code_signals: dict[str, bool] = {}
        if not c or "tool_signals" not in c:
            code_verdict = "no_code"
        else:
            tool_signals = c.get("tool_signals") or 0
            tool_names_total = c.get("tool_names_total") or 0
            manifests = c.get("manifests") or []
            danger = c.get("danger_categories") or []
            declared_repo = c.get("declared_repo")
            repo_id = db.norm_repo_url(declared_repo) if declared_repo else None
            real_repo_id = repo_id if _is_real_repo_id(repo_id) else None
            code_signals = {
                "repo_fork": bool(real_repo_id and real_repo_id != nu),
                "hollow": (tool_signals == 0 and tool_names_total == 0
                           and not manifests),
                "decl_tools_no_code": declared_tools_max > 0 and tool_signals == 0,
                "opaque_capability": bool(danger) and tool_signals == 0,
            }
            if code_signals["repo_fork"]:
                code_verdict = "repo_fork"
            elif code_signals["hollow"]:
                code_verdict = "hollow"
            elif code_signals["decl_tools_no_code"]:
                code_verdict = "decl_tools_no_code"
            elif code_signals["opaque_capability"]:
                code_verdict = "opaque_capability"
            else:
                code_verdict = "consistent"
            code_brief = {
                "files_scanned": c.get("files_scanned"),
                "truncated": c.get("truncated"),
                "langs": c.get("langs"),
                "tool_signals": tool_signals,
                "tool_names_total": tool_names_total,
                "tool_names_sample": (c.get("tool_names") or [])[:12],
                "danger_categories": danger,
                "declared_env_vars": c.get("declared_env_vars"),
                "declared_secrets": c.get("declared_secrets"),
                "declared_transports": c.get("declared_transports"),
                "manifests": manifests,
                "manifest_kinds": c.get("manifest_kinds"),
                "declared_name": c.get("declared_name"),
                "declared_version": c.get("declared_version"),
                "declared_repo": declared_repo,
                "declared_repo_id": real_repo_id,
            }

        # E3：同码异面是 C2 重定义后的头条判定，优先于代码侧判定；无法评估者
        # （<2 源有非空声明集）退回原代码侧 6 档（回归不变）。
        if decl_surface_vary:
            verdict = SURFACE_DIVERGENCE
            signals = dict(code_signals)
            signals["decl_surface_divergence"] = True
        elif code_verdict == "no_code":
            verdict = "no_code"
            signals = {}
        else:
            verdict = code_verdict
            signals = code_signals

        declared_brief = {
            s: ({"name": v.get("name"), "title": v.get("title"),
                 "version": v.get("version"), "tools_count": v.get("tools_count")}
                if v else None)
            for s, v in decls.items()
        }
        out[nu] = {
            "norm_url": nu,
            "verdict": verdict,
            "code_verdict": code_verdict,
            "sources": sources,
            "source_count": len(sources),
            "declared_tools_max": declared_tools_max,
            "declared_tools_sources": decl_tool_sources,
            "declared": declared_brief,
            "decl_cap_sources": cap_sources,
            "decl_cap_count": len(cap_sources),
            "decl_cap_size": {s: len(decl_caps_src[s]) for s in cap_sources},
            "decl_caps_sample": {s: sorted(decl_caps_src[s])[:40]
                                 for s in cap_sources},
            "decl_min_jaccard": decl_min_jaccard,
            "decl_surface_vary": decl_surface_vary,
            "signals": signals,
            "code": code_brief,
        }
    return out


def run(conn: sqlite3.Connection, apply: bool = False, examples: int = 5,
        limit: int | None = None) -> dict:
    """逐货号判定。apply=False 只统计不写库；apply=True 幂等重建 findings。"""
    data = collect(conn)
    counts: dict[str, int] = defaultdict(int)
    signal_stats: dict[str, int] = defaultdict(int)
    sig_examples: dict[str, list] = defaultdict(list)
    with_decl = 0
    surface_assessable = 0

    ts = db.now()
    if apply:
        conn.execute("DELETE FROM findings WHERE finding_type = ?", (FINDING_TYPE,))

    items = list(data.items())
    if limit:
        items = items[:limit]

    for nu, payload in items:
        verdict = payload["verdict"]
        counts[verdict] += 1
        if payload["declared_tools_sources"]:
            with_decl += 1
        if payload["decl_cap_count"] >= 2:
            surface_assessable += 1
        for k, v in payload["signals"].items():
            if v:
                signal_stats[k] += 1
        if apply:
            conn.execute(
                "INSERT INTO findings (source, name, version, finding_type, data_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("+".join(payload["sources"]), nu, verdict, FINDING_TYPE,
                 json.dumps(payload, ensure_ascii=False), ts),
            )
        if payload["verdict"] != "consistent" and payload["verdict"] != "no_code":
            if len(sig_examples[verdict]) < examples:
                sig_examples[verdict].append(payload)

    if apply:
        conn.commit()

    return {
        "total": len(data),
        "counts": dict(counts),
        "signal_stats": dict(signal_stats),
        "with_declared_tools": with_decl,
        "surface_assessable": surface_assessable,
        "examples": dict(sig_examples),
        "applied": apply,
    }


def show(result: dict, examples: int) -> None:
    counts = result["counts"]
    print()
    print(f"=== C2 跨源分叉 / 同码异面（声明面分歧 vs 代码层，"
          f"{'写库' if result['applied'] else '预演'}） ===")
    print(f"跨源货号（单元）          : {result['total']}")
    print(f"其中有源声明 tools 者     : {result['with_declared_tools']}")
    print(f"可评估声明面分歧（≥2 源非空声明集）: {result['surface_assessable']}")
    print()
    print("判定分布:")
    for v in VERDICT_ORDER:
        print(f"  {v:<28} {counts.get(v, 0)}")
    print()
    print("分叉信号命中货号数:")
    for k in (SURFACE_DIVERGENCE, "repo_fork", "hollow",
              "decl_tools_no_code", "opaque_capability"):
        print(f"  {k:<28} {result['signal_stats'].get(k, 0)}")

    if not examples:
        return
    div = result["examples"].get(SURFACE_DIVERGENCE)
    if div:
        print(f"\n===== {SURFACE_DIVERGENCE} 样例（同码异面，声明三角） =====")
        for p in div[:examples]:
            sizes = p.get("decl_cap_size") or {}
            print(f"\n● {p['norm_url']}  源 {p['source_count']}"
                  f"  {','.join(p['sources'])}"
                  f"  min_J={p.get('decl_min_jaccard')}")
            for s in p.get("decl_cap_sources", []):
                terms = (p.get("decl_caps_sample") or {}).get(s) or []
                print(f"    {s:<12} ({sizes.get(s, 0):>3} 词) {', '.join(terms[:8])}")
    for verdict in ("repo_fork", "hollow", "decl_tools_no_code", "opaque_capability"):
        subset = result["examples"].get(verdict)
        if not subset:
            continue
        print(f"\n===== {verdict} 样例 =====")
        for p in subset[:examples]:
            print(f"\n● {p['norm_url']}  源 {p['source_count']}"
                  f"  {','.join(p['sources'])}")
            c = p["code"] or {}
            print(f"    代码: files={c.get('files_scanned')}"
                  f" tool_signals={c.get('tool_signals')}"
                  f" names={c.get('tool_names_total')}"
                  f" danger={c.get('danger_categories')}")
            if c.get("declared_repo_id"):
                print(f"    代码自述仓库: {c['declared_repo_id']}")
            if p["declared_tools_sources"]:
                detail = ", ".join(
                    f"{s}={p['declared'].get(s, {}).get('tools_count')}"
                    for s in p["declared_tools_sources"])
                print(f"    声明 tools: {detail}")
            if c.get("tool_names_sample"):
                print(f"    工具名样例: {', '.join(c['tool_names_sample'])}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp_audit fork",
        description="C2 跨源分叉 / 同名不同芯（写入 findings，"
                    "finding_type='cross_source_fork'）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制处理的货号数（抽样试跑用）")
    parser.add_argument("--examples", type=int, default=5,
                        help="打印样例数（默认 5，0 不打印）")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    result = run(conn, apply=args.apply, examples=args.examples, limit=args.limit)
    show(result, args.examples)
    conn.close()


if __name__ == "__main__":
    main()
