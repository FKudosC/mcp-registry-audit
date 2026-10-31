"""C3 跨源声明–行为一致性形式化（声明层 vs 代码层，横截面）。

研究问题
--------
跨源货号（被 ≥2 个源声明）在登记表里**声明**了哪些工具（应然），与它实际
落地的代码里**注册/实现**了哪些工具（实然）是否一致？"声明–行为一致性"
是 MCP 供应链的信任前提：分发方按声明选型，而代码可能**多**（未声明的隐
能力）、**少**（声明的幽灵工具）或**完全对不上**（名不对实）。

口径（由 §9 实测定死）
--------------------
- 单元 = 跨源货号（与 C1/C2 同口径，v_cross_source_repo，22,104）。
- 声明层（应然）：各源**最新快照** ``tools_json`` 的并集 —— 仅 smithery /
  modelscope 提供；跨源单元中 **≥1 源有声明者 435**（≥2 源仅 2）⇒ 源间
  声明互比不可行，故本模块以**「单源声明 × 跨源实体」**为主口径，样本 435。
  （同源可把同一仓库登记成多个 name，故 (货号,源) 须**跨名聚合并集**。）
- 代码层（实然）：``code_capability`` 逐货号画像（``tool_signals`` /
  ``tool_names``）；同一 norm_url 的代码是该跨源实体唯一的落地物，天然承担
  「跨源共识」角色。
- 名级比对做**归一化**（小写 + 去非字母数字），吸收 ``get_weather`` /
  ``getWeather`` 一类命名风格差；归一化不改变「有/无」判定，只降低假阳。

判定（优先级自上而下）
--------------------
  no_code       该货号无代码画像（未克隆 / 克隆失败）→ 无法评估；
  no_decl       无任何源声明 tools → 不在 C3 样本内，仅登记不评判；
  no_impl       声明了工具，代码却**零工具注册信号** → 幽灵声明
                （声明–行为最严重不一致）；
  decl_opaque   声明只给了工具数、**抽不到工具名** → 不可名级验证；
  code_opaque   代码有注册信号但**抽不到工具名** → 不可名级验证；
  name_mismatch 声明名与代码名**零交集** → 名不对实；
  partial       声明名**部分**在代码可抽名中命中（0 < 命中 < 声明数）；
  full          声明名**全部**在代码可抽名中命中 → 声明兑现。

判定（声明三角，概念 4.2；仅当同一货号 ≥2 源声明 tools 时可达）
------------------------------------------------------------
  source_conflict_supported_A / _B
                各源声明**不一致**，且恰有一源与代码完全一致 ⇒
                另一源元数据陈旧 / 残缺（A / B = 声明源按名升序第 1 / 2 个）；
  source_conflict_supported_neither
                各源声明**完全一致**且 ``D ⊊ A`` ⇒ 注册表**集体欠声明**。
  （条件不满足者仍走上面原 8 档，回归逐项不变；payload 另存
   ``D_by_source``：源名 → 该源归一化声明名集。）

叠加信号（不单独成判定）
----------------------
  undeclared_extra    代码里出现声明之外的其它工具名 → 隐能力 / 声明不全；
  decl_sources_vary   同一货号不同源的声明工具集不一致 → 跨源声明分歧。

写库：finding_type='cross_source_consistency'，name=norm_url，version=verdict。
预演默认（只统计不写），--apply 才写；--decl-only 只落 435 声明子集。

用法：
    python -m mcp_audit consistency                     # 预演全量
    python -m mcp_audit consistency --table 435         # 附「435 单元」对照表
    python -m mcp_audit consistency --examples 8        # 样例条数
    python -m mcp_audit consistency --apply             # 写库（全部跨源单元）
    python -m mcp_audit consistency --apply --decl-only # 只写 435 声明子集
"""

import json
import re
import sqlite3
from collections import defaultdict

from . import db
from . import code_analysis
from . import cap_extract

FINDING_TYPE = "cross_source_consistency"
CODE_TYPE = code_analysis.FINDING_TYPE

VERDICT_ORDER = ("no_code", "no_decl", "no_impl", "decl_opaque",
                 "code_opaque",
                 "source_conflict_supported_A", "source_conflict_supported_B",
                 "source_conflict_supported_neither",
                 "name_mismatch", "partial", "full")

EX_VERDICTS = ("no_impl", "decl_opaque", "code_opaque",
               "name_mismatch", "partial")

# 声明三角（概念 4.2）新增档：仅当同一单元 ≥2 源声明时可达
CONFLICT_A = "source_conflict_supported_A"
CONFLICT_B = "source_conflict_supported_B"
CONFLICT_NEITHER = "source_conflict_supported_neither"
TRIANGLE_VERDICTS = (CONFLICT_A, CONFLICT_B, CONFLICT_NEITHER)

# 对照表排序：问题案例在前，便于人工过目
_TABLE_ORDER = ("no_impl", "source_conflict_supported_A",
                "source_conflict_supported_B",
                "source_conflict_supported_neither",
                "name_mismatch", "partial", "code_opaque",
                "decl_opaque", "full", "no_decl", "no_code")

_NORM_RE = re.compile(r"[^a-z0-9]+")

# tools_json 元素取名的候选键（smithery / modelscope / official 结构略异）
_NAME_KEYS = ("name", "tool", "toolName", "tool_name")


def _loads(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _norm_tool_name(s: str | None) -> str:
    """工具名归一化：小写 + 去非字母数字（get_weather == getWeather）。"""
    return _NORM_RE.sub("", (s or "").lower())


def _tools_count(tools_json: str | None) -> int:
    data = _loads(tools_json)
    return len(data) if isinstance(data, list) else 0


def _tool_names_of(tools_json: str | None) -> list[str]:
    """从 tools_json 抽取工具字面名（兼容 str / dict / function.name）。"""
    data = _loads(tools_json)
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for it in data:
        nm = None
        if isinstance(it, str):
            nm = it
        elif isinstance(it, dict):
            for k in _NAME_KEYS:
                v = it.get(k)
                if isinstance(v, str) and v.strip():
                    nm = v
                    break
            if nm is None:
                fn = it.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    nm = fn["name"]
        nm = (nm or "").strip()
        if nm:
            out.append(nm)
    return out


def _triangle_verdict(per_source_norm: dict[str, frozenset],
                      code_norm: set[str]) -> str | None:
    """声明三角（概念 4.2；E2 起声明集扩展到 desc + remotes）。

    仅当同一单元 **≥2 源有非空声明集** 且代码可名级验证（``code_norm`` 非空）
    时可判定；条件不满足时返回 ``None``（该单元仍走原 8 档，回归不变）。

    E2 把声明集 D_s 从 `tools_json` 扩到 ``tools 名 ∪ remotes(type/url) 抽词
    ∪ 描述抽词``（见 ``cap_extract.decl_caps``）。扩展后 D_s 是词袋，逐位
    ``== A`` 过严（实测仅 5 例），故把"与代码一致"由**相等**松弛为**与代码
    能力面的集合相似度**，用 Jaccard 归一并消除规模偏置：

    - 各源声明**完全一致**且 ``D ⊊ A`` ⇒ ``source_conflict_supported_neither``
      （注册表集体欠声明，代码比所有源都声明得多）；
    - 各源声明**不一致**且**唯一**存在一个与代码 Jaccard 最高的源 ⇒
      ``source_conflict_supported_A`` / ``_B``：该源最贴合代码，其余源元数据
      陈旧 / 残缺。A / B = 该源为声明源按名升序的第 1 / 其余（两源时即第 2）。
    """
    if len(per_source_norm) < 2 or not code_norm:
        return None
    groups = [frozenset(v) for v in per_source_norm.values()]
    if len(set(groups)) == 1:
        d = groups[0]
        return CONFLICT_NEITHER if d and d < code_norm else None
    srcs = sorted(per_source_norm)
    jac = {s: cap_extract.jaccard(per_source_norm[s], code_norm) for s in srcs}
    best = max(jac.values())
    if best <= 0:
        return None
    winners = [s for s in srcs if jac[s] == best]
    if len(winners) != 1:
        return None
    return CONFLICT_A if winners[0] == srcs[0] else CONFLICT_B


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
    """货号 -> {source: 声明层画像}（各源最新快照的 tools 声明）。

    同一 (norm_url, source) 可能在源里被登记成**多个 server name**（如
    modelscope 把同一仓库记成 ``@owner/repo`` 与 ``other/repo``），须跨名
    聚合并集（+ 取最大工具数），否则逐行覆盖会漏掉声明。
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
        names = _tool_names_of(r["tools_json"])
        caps = cap_extract.decl_caps(r["tools_json"], r["remotes_json"],
                                     r["description"])
        slot = acc.get(key)
        if slot is None:
            acc[key] = {
                "tools_count": cnt,
                "names": set(names),
                "caps": set(caps),
                "name": r["name"],
                "title": r["title"],
                "version": r["version"],
            }
        else:
            slot["names"].update(names)
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
            "tool_names": sorted(slot["names"]),
            "decl_caps": sorted(slot["caps"]),
        }
    return out


def collect(conn: sqlite3.Connection) -> dict:
    """构建跨源货号 -> 声明–行为一致性判定结果。"""
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
        decl_sources = sorted(s for s, n in decl_tools.items() if n > 0)
        declared_tools_max = max(decl_tools.values(), default=0)

        declared_names: list[str] = []
        per_source_norm: dict[str, frozenset] = {}
        for s in decl_sources:
            names = decls[s]["tool_names"]
            declared_names.extend(names)
            per_source_norm[s] = frozenset(
                _norm_tool_name(x) for x in names if _norm_tool_name(x))
        decl_norm = {_norm_tool_name(x) for x in declared_names if _norm_tool_name(x)}
        decl_sources_vary = (len(decl_sources) > 1
                             and len(set(per_source_norm.values())) > 1)

        # E2：声明三角的声明集 D_s = tools ∪ remotes(type/url) ∪ 描述抽词
        tri_norm: dict[str, frozenset] = {}
        for s in sources:
            d = decls.get(s)
            if not d:
                continue
            caps = d.get("decl_caps") or []
            if caps:
                tri_norm[s] = frozenset(caps)

        signals: dict[str, bool] = {}
        metrics: dict = {}
        code_brief = None

        if not c or "tool_signals" not in c:
            verdict = "no_code"
        else:
            tool_signals = c.get("tool_signals") or 0
            tool_names_total = c.get("tool_names_total") or 0
            code_names = c.get("tool_names") or []
            code_norm = {_norm_tool_name(x) for x in code_names if _norm_tool_name(x)}
            # 代码侧能力词集（与 tri_norm / cap_extract 同口径：驼峰/下划线拆词）
            code_caps_tri: set[str] = set()
            for x in code_names:
                code_caps_tri |= cap_extract.normalize_tool_name(x)

            # E2：声明三角可达性。声明集 D_s = tools 名 ∪ remotes ∪ 描述抽词，
            # ≥2 源有非空 D_s 即可入三角，不再被 tools_json 缺失（no_decl）卡死。
            tri_ok = len(tri_norm) >= 2
            overlap = decl_norm & code_norm
            extra = code_norm - decl_norm

            signals = {
                "no_impl": declared_tools_max > 0 and tool_signals == 0,
                "undeclared_extra": bool(decl_norm) and bool(extra),
                "decl_sources_vary": decl_sources_vary,
            }
            metrics = {
                "declared_names": len(decl_norm),
                "code_names": len(code_norm),
                "overlap": len(overlap),
                "coverage": (len(overlap) / len(decl_norm)) if decl_norm else None,
            }

            if declared_tools_max == 0 and not tri_ok:
                verdict = "no_decl"
            elif (declared_tools_max > 0 or tri_ok) and tool_signals == 0:
                verdict = "no_impl"
            elif not decl_norm and not tri_ok:
                verdict = "decl_opaque"
            elif tool_names_total == 0:
                verdict = "code_opaque"
            else:
                # 声明三角（概念 4.2；E2 起含 desc + remotes 声明集）
                verdict = _triangle_verdict(tri_norm, code_caps_tri)
                if verdict is None:
                    if not overlap:
                        verdict = "name_mismatch"
                    elif len(overlap) < len(decl_norm):
                        verdict = "partial"
                    else:
                        verdict = "full"

            code_brief = {
                "files_scanned": c.get("files_scanned"),
                "langs": c.get("langs"),
                "tool_signals": tool_signals,
                "tool_names_total": tool_names_total,
                "tool_names": code_names[:20],
                "overlap_names": sorted(overlap)[:12],
                "extra_names": sorted(extra)[:12],
                "danger_categories": c.get("danger_categories"),
                "declared_env_vars": c.get("declared_env_vars"),
                "declared_secrets": c.get("declared_secrets"),
                "manifests": c.get("manifests"),
                "declared_name": c.get("declared_name"),
                "declared_version": c.get("declared_version"),
                "declared_repo": c.get("declared_repo"),
            }

        out[nu] = {
            "norm_url": nu,
            "verdict": verdict,
            "sources": sources,
            "source_count": len(sources),
            "declared_tools_max": declared_tools_max,
            "declared_tools_sources": decl_sources,
            "declared_names": declared_names[:40],
            "declared_names_total": len(decl_norm),
            "declared": decls,
            "D_by_source": {s: sorted(v) for s, v in tri_norm.items()},
            "tri_decl_sources": sorted(tri_norm),
            "tri_declared_total": (len(set().union(*tri_norm.values()))
                                   if tri_norm else 0),
            "signals": signals,
            "metrics": metrics,
            "code": code_brief,
        }
    return out


def run(conn: sqlite3.Connection, apply: bool = False, examples: int = 5,
        limit: int | None = None, decl_only: bool = False) -> dict:
    """逐货号判定。apply=False 只统计不写库；apply=True 幂等重建 findings。"""
    data = collect(conn)
    counts: dict[str, int] = defaultdict(int)
    decl_counts: dict[str, int] = defaultdict(int)
    signal_stats: dict[str, int] = defaultdict(int)
    sig_examples: dict[str, list] = defaultdict(list)
    coverages: list[float] = []
    name_verifiable = 0
    decl_total = 0
    decl_ge2 = 0
    tri_total = 0
    tri_counts: dict[str, int] = defaultdict(int)
    tri_examples: dict[str, list] = defaultdict(list)
    written = 0

    for p in data.values():
        if p["declared_tools_max"] > 0:
            decl_total += 1
            if len(p["declared_tools_sources"]) > 1:
                decl_ge2 += 1

    ts = db.now()
    if apply:
        conn.execute("DELETE FROM findings WHERE finding_type = ?", (FINDING_TYPE,))

    items = list(data.items())
    if limit:
        items = items[:limit]

    table: list[dict] = []
    for nu, payload in items:
        verdict = payload["verdict"]
        counts[verdict] += 1
        tri_ok = len(payload.get("tri_decl_sources") or []) >= 2
        if payload["declared_tools_max"] > 0:
            decl_counts[verdict] += 1
        if payload["declared_tools_max"] > 0 or tri_ok:
            table.append(payload)
        if tri_ok:
            tri_total += 1
            tri_counts[verdict] += 1
            if verdict in TRIANGLE_VERDICTS and len(tri_examples[verdict]) < examples:
                tri_examples[verdict].append(payload)
        cov = payload["metrics"].get("coverage")
        if cov is not None:
            coverages.append(cov)
            name_verifiable += 1
        for k, v in payload["signals"].items():
            if v:
                signal_stats[k] += 1
        if apply and (not decl_only or payload["declared_tools_max"] > 0):
            conn.execute(
                "INSERT INTO findings (source, name, version, finding_type, data_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("+".join(payload["sources"]), nu, verdict, FINDING_TYPE,
                 json.dumps(payload, ensure_ascii=False), ts),
            )
            written += 1
        if payload["declared_tools_max"] > 0 and verdict in EX_VERDICTS:
            if len(sig_examples[verdict]) < examples:
                sig_examples[verdict].append(payload)

    if apply:
        conn.commit()

    table.sort(key=lambda p: (_TABLE_ORDER.index(p["verdict"])
                              if p["verdict"] in _TABLE_ORDER else len(_TABLE_ORDER),
                              -p["metrics"].get("overlap", 0)))

    return {
        "total": len(data),
        "scanned": len(items),
        "decl_total": decl_total,
        "decl_ge2": decl_ge2,
        "tri_total": tri_total,
        "tri_counts": dict(tri_counts),
        "tri_examples": dict(tri_examples),
        "counts": dict(counts),
        "decl_counts": dict(decl_counts),
        "signal_stats": dict(signal_stats),
        "name_verifiable": name_verifiable,
        "mean_coverage": (sum(coverages) / len(coverages)) if coverages else None,
        "examples": dict(sig_examples),
        "table": table,
        "written": written,
        "applied": apply,
        "decl_only": decl_only,
    }


def _print_table(table: list[dict], limit: int) -> None:
    rows = table[:limit] if limit else table[:]
    print()
    print(f"===== C3 声明 × 代码 对照表（声明子集 {len(table)} 个，"
          f"显示前 {len(rows)} 行） =====")
    print(f"{'货号':<44} {'源':>2} {'声明':>4} {'信号':>6} {'码名':>4} {'命中':>4} "
          f"{'判定':<13} 声明名 → 代码名")
    for p in rows:
        c = p["code"] or {}
        dn = ",".join(p["declared_names"][:6]) or "-"
        cn = ",".join((c.get("tool_names") or [])[:6]) or "-"
        print(f"{p['norm_url'][:44]:<44} {p['source_count']:>2} "
              f"{p['declared_names_total']:>4} {c.get('tool_signals', 0):>6} "
              f"{c.get('tool_names_total', 0):>4} {p['metrics'].get('overlap', 0):>4} "
              f"{p['verdict']:<13} {dn[:46]} → {cn[:46]}")


def show(result: dict, examples: int, table: int = 0) -> None:
    counts = result["decl_counts"]
    print()
    print(f"=== C3 跨源声明–行为一致性（声明层 vs 代码层，"
          f"{'写库' if result['applied'] else '预演'}） ===")
    print(f"跨源货号（单元）           : {result['total']}")
    print(f"其中 ≥1 源声明 tools（C3 样本）: {result['decl_total']}")
    print(f"  └ ≥2 源声明              : {result['decl_ge2']}")
    print(f"名级可验证子集             : {result['name_verifiable']}")
    mc = result["mean_coverage"]
    print(f"声明名级平均兑现率         : "
          f"{'%.1f%%' % (mc * 100) if mc is not None else '—'}")
    if result["applied"]:
        print(f"写库行数                   : {result['written']}"
              f"{'（仅声明子集）' if result['decl_only'] else '（全部跨源单元）'}")
    print()
    print("判定分布（全部跨源单元）:")
    for v in VERDICT_ORDER:
        print(f"  {v:<16} {result['counts'].get(v, 0)}")
    print()
    print(f"判定分布（C3 声明子集，n={result['decl_total']}）:")
    for v in VERDICT_ORDER:
        n = counts.get(v, 0)
        if n:
            print(f"  {v:<16} {n}")
    print()
    print(f"声明三角单元（≥2 源非空声明集，E2）: {result['tri_total']}")
    tc = result["tri_counts"]
    for v in TRIANGLE_VERDICTS:
        print(f"  {v:<28} {tc.get(v, 0)}")
    print()
    print("叠加信号命中货号数:")
    for k in ("undeclared_extra", "decl_sources_vary"):
        print(f"  {k:<20} {result['signal_stats'].get(k, 0)}")

    if examples:
        for verdict in EX_VERDICTS:
            subset = result["examples"].get(verdict)
            if not subset:
                continue
            print(f"\n===== {verdict} 样例 =====")
            for p in subset[:examples]:
                c = p["code"] or {}
                print(f"\n● {p['norm_url']}  源 {p['source_count']}"
                      f"  {','.join(p['sources'])}")
                detail = ", ".join(
                    f"{s}={p['declared'].get(s, {}).get('tools_count')}"
                    for s in p["declared_tools_sources"])
                print(f"    声明 tools: {detail}"
                      f"（名 {p['declared_names_total']}）")
                if p["declared_names"]:
                    print(f"    声明名样例: {', '.join(p['declared_names'][:8])}")
                print(f"    代码: signals={c.get('tool_signals')}"
                      f" names={c.get('tool_names_total')}"
                      f" 命中={p['metrics'].get('overlap')}"
                      f" 额外={p['metrics'].get('code_names', 0) - p['metrics'].get('overlap', 0)}"
                      f" danger={c.get('danger_categories')}")
                if c.get("tool_names"):
                    print(f"    代码名样例: {', '.join(c['tool_names'][:8])}")

        for verdict in TRIANGLE_VERDICTS:
            subset = result["tri_examples"].get(verdict)
            if not subset:
                continue
            print(f"\n===== {verdict} 样例（声明三角，E2） =====")
            for p in subset[:examples]:
                print(f"\n● {p['norm_url']}  源 {p['source_count']}"
                      f"  {','.join(p['sources'])}")
                print(f"    声明源（非空 D_s）: "
                      f"{', '.join(p['tri_decl_sources'])}"
                      f"（并集 {p['tri_declared_total']} 词）")
                for s in p["tri_decl_sources"]:
                    terms = p["D_by_source"].get(s) or []
                    print(f"      {s:<12} ({len(terms):>3} 词)"
                          f" {', '.join(terms[:6])}")
                c = p["code"] or {}
                print(f"    代码名 ({c.get('tool_names_total')}): "
                      f"{', '.join((c.get('tool_names') or [])[:6]) or '-'}")

    if table:
        _print_table(result["table"], table)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp_audit consistency",
        description="C3 跨源声明–行为一致性（写入 findings，"
                    "finding_type='cross_source_consistency'）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    parser.add_argument("--decl-only", action="store_true",
                        help="只写/统计 C3 声明子集（≥1 源声明 tools）")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制处理的货号数（抽样试跑用）")
    parser.add_argument("--examples", type=int, default=5,
                        help="打印样例数（默认 5，0 不打印）")
    parser.add_argument("--table", type=int, default=0,
                        help="打印声明×代码对照表的行数（0 不打印）")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    result = run(conn, apply=args.apply, examples=args.examples,
                 limit=args.limit, decl_only=args.decl_only)
    show(result, args.examples, table=args.table)
    conn.close()


if __name__ == "__main__":
    main()
