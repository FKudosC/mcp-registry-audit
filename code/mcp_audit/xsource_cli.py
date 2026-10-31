"""mcp-audit-xsource · 跨源一致性审计 CLI（E8 可部署 artifact）。

把项目里三条跨源分析线封装成一个零依赖、可 ``pip install`` 的独立命令行工具：

- :mod:`mcp_audit.xsource_fidelity`     概念 1+3 跨源保真度（同期横向分歧 / 可见性上界 V(f)）
- :mod:`mcp_audit.consensus_analysis`   概念 4.4 危险面跨源共识（并产出注册表 source_blame 排名）
- :mod:`mcp_audit.consistency_analysis` C3 跨源声明–行为一致性（声明三角）

输入 = **任意注册表导出的 JSON**；输出 = 跨源分歧报告 + source_blame 排名
（text / Markdown / JSON / HTML）。

两种运行模式
------------
1. 基准模式（``--db``）：直接读本项目采集库（默认 ``config.DB_PATH``），复现论文 7 源基准。
2. 零适配模式（``--input SOURCE=PATH``）：把一份或多份**外部注册表导出 JSON** 灌进临时库
   （``snapshots`` + ``servers``）→ 重建跨源对齐键（``repo_index``）→ 复用上述三条分析线，
   **无需为每个新注册表写适配器**。

安全边界（不可破）
----------------
只读分析：不触盘爬取、**不执行**任何注册表/仓库代码；基准模式对采集库**只读**
（``apply=False``，不写 findings）；零适配模式只写**临时**库。

用法：
    mcp-audit-xsource                                 # 基准：读默认采集库，打印报告
    mcp-audit-xsource --db D:\\Data\\mcp-audit\\mcp_audit.db --out report.md --format md
    mcp-audit-xsource --input acme=acme_export.json --input rust=rust_export.json
    mcp-audit-xsource --export official --out official_export.json --format json
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import sys
import tempfile
from pathlib import Path

from . import config, db
from . import consensus_analysis, consistency_analysis, xsource_fidelity

TOOL = "mcp-audit-xsource"
KNOWN_SOURCES = ("official", "smithery", "modelscope", "mcpso", "glama",
                 "cursor", "aibase")
ENTRY_LIST_KEYS = ("servers", "entries", "items", "data", "results", "list",
                   "records", "packages")
NAME_KEYS = ("qualifiedName", "full_name", "fullName", "name", "slug", "path", "id")
REPO_KEYS = ("repository", "repo_url", "repoUrl", "repo", "github", "github_url",
             "githubUrl", "homepage", "source_url", "sourceUrl",
             "codeRepository", "url")
DESC_KEYS = ("description", "desc", "abstract", "Abstract", "summary")
TITLE_KEYS = ("title", "displayName", "display_name", "ChineseName", "name")


# --------------------------------------------------------- 外部 JSON 归一化 --

def _first_str(raw: dict, keys) -> str | None:
    for k in keys:
        v = raw.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _entry_list(obj):
    """从导出 JSON 里取出条目列表（兼容多种常见包装键）。"""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for k in ENTRY_LIST_KEYS:
            v = obj.get(k)
            if isinstance(v, list):
                return v
            if isinstance(v, dict):
                return list(v.values())
    return None


def _guess_source(obj) -> str | None:
    if isinstance(obj, dict):
        for k in ("source", "registry", "registry_name", "source_name"):
            v = obj.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return None


def _norm_tools(tools):
    if isinstance(tools, dict):
        tools = list(tools.values())
    out = []
    for t in tools or []:
        if isinstance(t, str):
            out.append({"name": t})
        elif isinstance(t, dict):
            out.append(t)
    return out


def _norm_remotes(remotes):
    if isinstance(remotes, dict):
        remotes = [remotes]
    out = []
    for r in remotes or []:
        if isinstance(r, str):
            out.append({"type": "http", "url": r})
        elif isinstance(r, dict):
            out.append(r)
    return out


def _native_official(raw):
    if isinstance(raw, dict) and isinstance(raw.get("server"), dict):
        return db.extract_official_entry(raw)
    return None


def _native_smithery(raw):
    if isinstance(raw, dict) and raw.get("qualifiedName"):
        return db.extract_smithery_entry(raw, raw.get("detail"))
    return None


def _native_modelscope(raw):
    if isinstance(raw, dict) and ("Tools" in raw or "Publisher" in raw):
        return db.extract_modelscope_entry(raw)
    return None


def _native_aibase(raw):
    if isinstance(raw, dict) and ("html_url" in raw or "full_name" in raw) \
            and "stargazers_count" in raw:
        return db.extract_aibase_entry(raw)
    return None


def _native_glama(raw):
    if isinstance(raw, str):
        return db.extract_glama_entry(raw)
    if isinstance(raw, dict):
        u = raw.get("url") or raw.get("source_url")
        if isinstance(u, str):
            return db.extract_glama_entry(u)
    return None


def _native_mcpso(raw):
    if isinstance(raw, dict) and "jsonld" in raw:
        return db.extract_mcpso_entry(
            raw.get("url") or "", raw.get("jsonld"),
            raw.get("meta_desc") or raw.get("description"), raw.get("repo_url"))
    return None


def _native_cursor(raw):
    if isinstance(raw, dict):
        u = raw.get("url")
        if isinstance(u, str):
            gh = raw.get("github") or raw.get("github_urls") \
                or raw.get("githubUrls") or []
            if isinstance(gh, str):
                gh = [gh]
            return db.extract_cursor_entry(
                u, raw.get("meta_desc") or raw.get("description"),
                raw.get("og_title") or raw.get("title"), gh)
    return None


_NATIVE = {
    "official": _native_official,
    "smithery": _native_smithery,
    "modelscope": _native_modelscope,
    "mcpso": _native_mcpso,
    "glama": _native_glama,
    "cursor": _native_cursor,
    "aibase": _native_aibase,
}


def _generic_entry(raw, source: str) -> dict | None:
    """通用归一化：把任意注册表导出条目映射到统一结构（零适配兜底）。

    对齐键 = GitHub 仓库 URL（取自 repository/repo/github/homepage/url 等常见字段，
    亦写入 ``market_json`` / ``code_json`` 供 :func:`db.repo_url_for_row` 识别）。
    """
    if not isinstance(raw, dict):
        return None
    repo = None
    for k in REPO_KEYS:
        v = raw.get(k)
        if isinstance(v, dict):
            v = v.get("url") or v.get("html_url")
        if isinstance(v, str) and v.strip():
            repo = v.strip()
            break
    name = _first_str(raw, NAME_KEYS) or (db.norm_repo_url(repo) if repo else None)
    if not name:
        return None

    version = raw.get("version") or raw.get("Version") or "latest"
    tools = raw.get("tools") or raw.get("Tools") or []
    remotes = raw.get("remotes") or raw.get("Remotes") or []
    dep = raw.get("deploymentUrl") or raw.get("DeployedUrl")
    if dep:
        remotes = list(remotes) + [{"type": "http", "url": dep}]
    packages = raw.get("packages") or raw.get("Packages") or []
    code = raw.get("code") or []
    if repo and not code:
        code = [{"type": "git", "url": repo}]

    market = {
        "source": source,
        "github": repo,
        "url": repo,
        "repo_url": repo,
        "homepage": raw.get("homepage") or raw.get("Homepage"),
        "repository": {"url": repo} if repo else None,
    }
    market = {k: v for k, v in market.items() if v not in (None, "")}

    return {
        "name": str(name),
        "version": str(version or "latest"),
        "title": _first_str(raw, TITLE_KEYS),
        "description": _first_str(raw, DESC_KEYS),
        "status": raw.get("status") or "active",
        "published_at": raw.get("published_at") or raw.get("created_at")
        or raw.get("publishedAt"),
        "updated_at": raw.get("updated_at") or raw.get("updatedAt"),
        "is_latest": raw.get("is_latest", 1),
        "remotes_json": json.dumps(_norm_remotes(remotes), ensure_ascii=False),
        "code_json": json.dumps(code, ensure_ascii=False),
        "tools_json": json.dumps(_norm_tools(tools), ensure_ascii=False),
        "packages_json": json.dumps(packages, ensure_ascii=False),
        "market_json": json.dumps(market, ensure_ascii=False),
        "raw_json": json.dumps(raw, ensure_ascii=False),
    }


def normalize_entry(source: str, raw) -> dict | None:
    """按源选原生适配器；未识别则回退通用归一化（这就是「零适配」）。"""
    fn = _NATIVE.get(source)
    if fn:
        e = fn(raw)
        if e and e.get("name"):
            return e
    return _generic_entry(raw, source)


# --------------------------------------------------------------- 数据接入 --

def _load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def ingest_exports(conn, pairs) -> dict:
    """把外部导出 JSON 灌进（临时）库：snapshots + servers，再重建 repo_index。"""
    per_source: dict[str, list] = {}
    files = []
    for source, path in pairs:
        obj = _load_json(path)
        src = source or _guess_source(obj)
        if not src:
            raise SystemExit(
                f"[{TOOL}] 无法从 {path} 推断 source；请用 `--input SOURCE=PATH` 指定")
        lst = _entry_list(obj)
        if lst is None:
            lst = [obj]
        kept = 0
        for raw in lst:
            e = normalize_entry(src, raw)
            if e and e.get("name"):
                per_source.setdefault(src, []).append(e)
                kept += 1
        files.append({"source": src, "path": str(path),
                      "records": len(lst), "kept": kept})

    total = 0
    for src, entries in per_source.items():
        rid = db.start_round(conn, src)
        total += db.save_entries(conn, rid, src, entries)
        db.mark_round_done(conn, rid)
    indexed = db.rebuild_repo_index(conn)
    return {"files": files, "sources": sorted(per_source),
            "entries": total, "indexed": indexed}


def export_source(conn, source: str) -> dict:
    """把一个源的每-name 最新快照导出成通用 JSON（供零适配自测 / 交换）。"""
    rows = conn.execute(
        "SELECT name, version, title, description, status, published_at, updated_at,"
        " remotes_json, code_json, tools_json, packages_json, market_json"
        " FROM snapshots WHERE source = ? AND id IN"
        " (SELECT MAX(id) FROM snapshots WHERE source = ? GROUP BY name)"
        " ORDER BY name",
        (source, source),
    ).fetchall()
    servers = []
    for r in rows:
        market = db._loads(r["market_json"]) or {}
        code = db._loads(r["code_json"]) or []
        repo = db.repo_url_for_row(source, market, code)
        servers.append({
            "name": r["name"],
            "version": r["version"],
            "title": r["title"],
            "description": r["description"],
            "status": r["status"],
            "published_at": r["published_at"],
            "updated_at": r["updated_at"],
            "repository": repo,
            "remotes": db._loads(r["remotes_json"]) or [],
            "tools": db._loads(r["tools_json"]) or [],
            "packages": db._loads(r["packages_json"]) or [],
            "code": db._loads(r["code_json"]) or [],
        })
    return {"source": source, "count": len(servers), "servers": servers}


# ----------------------------------------------------------------- 分析 --

def analyze(conn, examples: int = 3, limit: int | None = None) -> dict:
    """复用三条跨源分析线（只读；不写 findings）。"""
    return {
        "fidelity": xsource_fidelity.run(conn, apply=False, examples=examples,
                                         limit=limit),
        "consensus": consensus_analysis.run(conn, apply=False, examples=examples,
                                            limit=limit),
        "consistency": consistency_analysis.run(conn, apply=False, examples=examples,
                                                limit=limit),
    }


def db_sources(conn) -> list[str]:
    return [r["source"] for r in conn.execute(
        "SELECT DISTINCT source FROM snapshots ORDER BY source")]


# --------------------------------------------------------------- 渲染 --

def _pct(x) -> str:
    return f"{x * 100:.1f}%" if isinstance(x, (int, float)) else "—"


def render_text(result: dict, info: dict | None = None, mode: str = "baseline",
                examples: int = 3, table: int = 0) -> None:
    f, c, k = result["fidelity"], result["consensus"], result["consistency"]
    print()
    print("=" * 78)
    print(f"{TOOL} · 跨源一致性审计报告（{mode}）")
    print("=" * 78)
    if info:
        print(f"外部导出源  : {', '.join(info['sources'])}")
        print(f"入库 / 可对齐: {info['entries']} 条 / {info['indexed']} 条 norm_url")
        for it in info["files"]:
            print(f"  · {it['source']:<12} {it['kept']}/{it['records']} 条可用  {it['path']}")
    print(f"跨源单元（≥2 源对齐）: {f['total']}")
    print()
    print("── source_blame 排名（注册表欠披露率，分母=有代码证据的跨源单元） ──")
    print(f"  {db._pad('源', 14)} {db._pad('成员', 6, right=True)} "
          f"{db._pad('有码', 6, right=True)} {db._pad('欠披露', 7, right=True)} "
          f"{db._pad('欠披露率', 9, right=True)}")
    for r in c["source_blame"]:
        print(f"  {db._pad(r['source'], 14)} {db._pad(str(r['members']), 6, right=True)} "
              f"{db._pad(str(r['members_code']), 6, right=True)} "
              f"{db._pad(str(r['blamed']), 7, right=True)} "
              f"{db._pad(_pct(r['rate']), 9, right=True)}")
    print()

    xsource_fidelity.show(result["fidelity"], examples, table=table)
    consistency_analysis.show(result["consistency"], examples, table=table)
    consensus_analysis.show(result["consensus"], examples, table=table)


def _md_table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v) for v in r) + " |")
    return "\n".join(out)


def render_md(result: dict, info: dict | None = None, mode: str = "baseline") -> str:
    f, c, k = result["fidelity"], result["consensus"], result["consistency"]
    L = [f"# 跨源一致性审计报告（{TOOL}）", ""]
    L.append(f"- 运行模式：**{mode}**")
    if info:
        L.append(f"- 外部导出源：{', '.join('`%s`' % s for s in info['sources'])}")
        L.append(f"- 入库 {info['entries']} 条；可跨源对齐（norm_url）{info['indexed']} 条")
    L.append(f"- 跨源单元（≥2 源对齐）：**{f['total']}**")
    L.append("")

    L.append("## 1. 跨源分歧（概念 1+3，Fidelity）")
    L.append("")
    stats = f["stats"]
    rows = []
    for b in xsource_fidelity.BUCKETS:
        s = stats[b]
        rows.append([xsource_fidelity.BUCKET_LABEL[b], s["units"], s["comparable"],
                     s["different"],
                     _pct(s["different"] / s["comparable"] if s["comparable"] else None)])
    L.append(_md_table(["分桶", "单元", "可比", "different", "different 占比"], rows))
    L.append("")
    L.append("可见性上界 V(f)（分母 = 全部跨源单元）：")
    L.append("")
    rows = []
    for key in xsource_fidelity.FIELD_ORDER:
        v = f["visibility"][key]
        rows.append([key, v["ge1"], v["ge2"], v["single"],
                     _pct(v["single_ratio"])])
    L.append(_md_table(["字段", "≥1源", "≥2源", "单源", "单源占比"], rows))
    L.append("")
    farm = f["farm"]
    L.append(f"镜像农场：distinct owner {farm['owners']}，"
             f"top-10 占比 {_pct(farm['top10_share'])}，"
             f"农场单元 {farm['farm_units']}（{_pct(farm['farm_share'])}）；"
             f"同期子集中农场 {farm['same_day_farm_units']}/{farm['same_day_units']}")
    L.append("")

    L.append("## 2. 声明–行为一致性（C3）")
    L.append("")
    L.append(f"- C3 声明子集（≥1 源声明 tools）：{k['decl_total']}")
    L.append(f"- 其中 ≥2 源声明：{k['decl_ge2']}")
    L.append(f"- 声明三角单元：{k['tri_total']}")
    L.append(f"- 名级可验证子集：{k['name_verifiable']}；"
             f"声明名级平均兑现率 {_pct(k['mean_coverage'])}")
    L.append("")

    L.append("## 3. 危险面跨源共识（概念 4.4，Consensus）")
    L.append("")
    L.append(f"- 有代码画像：{c['with_code']}；其中含危险面：{c['danger_units']}")
    L.append(f"- 声明子集：{c['declaring_units']}")
    L.append("")

    L.append("## 4. source_blame 排名（注册表欠披露率）")
    L.append("")
    rows = [[r["source"], r["members"], r["members_code"], r["blamed"],
             _pct(r["rate"])] for r in c["source_blame"]]
    L.append(_md_table(["源", "成员", "有码", "欠披露", "欠披露率"], rows))
    L.append("")
    L.append(f"> 生成命令：`{TOOL} {' '.join(sys.argv[1:]) or '(baseline)'}`")
    return "\n".join(L)


def render_json(result: dict, info: dict | None = None, mode: str = "baseline") -> str:
    f, c, k = result["fidelity"], result["consensus"], result["consistency"]
    payload = {
        "mode": mode,
        "sources": info["sources"] if info else None,
        "indexed": info["indexed"] if info else None,
        "cross_units": f["total"],
        "fidelity": {
            "stats": {b: {"units": s["units"], "comparable": s["comparable"],
                          "different": s["different"]}
                      for b, s in f["stats"].items()},
            "visibility": f["visibility"],
            "farm": {kk: f["farm"][kk] for kk in
                     ("owners", "top10_share", "farm_units", "farm_share",
                      "same_day_units", "same_day_farm_units")},
        },
        "consensus": {
            "with_code": c["with_code"],
            "danger_units": c["danger_units"],
            "declaring_units": c["declaring_units"],
            "source_blame": c["source_blame"],
        },
        "consistency": {
            "decl_total": k["decl_total"],
            "decl_ge2": k["decl_ge2"],
            "tri_total": k["tri_total"],
            "name_verifiable": k["name_verifiable"],
            "mean_coverage": k["mean_coverage"],
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


_CSS = """
body{margin:0;background:#eef2f6;color:#1a2027;
font:15px/1.6 -apple-system,"Segoe UI",Roboto,"Noto Sans CJK SC",sans-serif}
header{background:#0f1419;color:#fff;padding:26px 32px}
header h1{margin:0 0 6px;font-size:22px}
header p{margin:0;color:#9fb0c0;font-size:13px}
.wrap{max-width:1000px;margin:0 auto;padding:24px 20px 60px}
section{background:#fff;border:1px solid #e3e8ee;border-radius:10px;margin:18px 0;
padding:18px 20px;box-shadow:0 1px 3px rgba(16,24,40,.06)}
h2{font-size:17px;margin:0 0 10px}
table{width:100%;border-collapse:collapse;margin:6px 0;font-size:13.5px}
th,td{border:1px solid #e3e8ee;padding:6px 9px;text-align:left}
th{background:#f6f9fc;color:#5b6875}
td.n{text-align:right}
.foot{color:#5b6875;font-size:12.5px;text-align:center;margin-top:28px}
"""


def _html_table(headers, rows) -> str:
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape('' if v is None else str(v))}</td>"
                         for v in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def render_html(result: dict, info: dict | None = None, mode: str = "baseline") -> str:
    f, c, k = result["fidelity"], result["consensus"], result["consistency"]
    fs = []
    for b in xsource_fidelity.BUCKETS:
        s = f["stats"][b]
        fs.append([xsource_fidelity.BUCKET_LABEL[b], s["units"], s["comparable"],
                   s["different"],
                   _pct(s["different"] / s["comparable"] if s["comparable"] else None)])
    vis = []
    for key in xsource_fidelity.FIELD_ORDER:
        v = f["visibility"][key]
        vis.append([key, v["ge1"], v["ge2"], v["single"], _pct(v["single_ratio"])])
    blame = [[r["source"], r["members"], r["members_code"], r["blamed"], _pct(r["rate"])]
             for r in c["source_blame"]]
    src_line = ", ".join(info["sources"]) if info else "（本项目采集库）"
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{TOOL} · 跨源一致性审计报告</title><style>{_CSS}</style></head>
<body>
<header><h1>{TOOL} · 跨源一致性审计报告</h1>
<p>模式 {mode} · 数据源 {html.escape(src_line)} · 跨源单元 {f["total"]} ·
只读分析，不执行任何仓库代码</p></header>
<div class="wrap">
<section><h2>1. 跨源分歧（Fidelity）</h2>
{_html_table(["分桶", "单元", "可比", "different", "different 占比"], fs)}
<p>可见性上界 V(f)：</p>
{_html_table(["字段", "≥1源", "≥2源", "单源", "单源占比"], vis)}</section>
<section><h2>2. 声明–行为一致性（C3）</h2>
<p>声明子集 {k["decl_total"]} · ≥2 源声明 {k["decl_ge2"]} · 声明三角 {k["tri_total"]} ·
名级兑现率 {_pct(k["mean_coverage"])}</p></section>
<section><h2>3. 危险面跨源共识（Consensus）</h2>
<p>有代码画像 {c["with_code"]} · 含危险面 {c["danger_units"]} · 声明子集 {c["declaring_units"]}</p>
{_html_table(["源", "成员", "有码", "欠披露", "欠披露率"], blame)}</section>
<p class="foot">{TOOL} · 跨源一致性审计（只读 artifact）</p>
</div></body></html>"""


# ------------------------------------------------------------------ CLI --

def _parse_input(arg: str) -> tuple[str | None, str]:
    if "=" in arg:
        left, _, right = arg.partition("=")
        if left and ":" not in left and "/" not in left and "\\" not in left:
            return left.strip(), right.strip()
    return None, arg


def _infer_format(out: str | None, fmt: str | None) -> str:
    if fmt:
        return fmt
    if out:
        ext = Path(out).suffix.lower()
        if ext in (".md", ".markdown"):
            return "md"
        if ext == ".json":
            return "json"
        if ext in (".html", ".htm"):
            return "html"
    return "text"


def main() -> None:
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description="跨源一致性审计：输入任意注册表导出 JSON，输出跨源分歧报告 + "
                    "source_blame 排名（只读，不执行第三方代码）")
    parser.add_argument("--input", action="append", default=[], metavar="SOURCE=PATH",
                        help="外部注册表导出 JSON（可重复；SOURCE 省略时从 JSON 推断）")
    parser.add_argument("--db", default=None, help="采集库路径（默认 config.DB_PATH）")
    parser.add_argument("--export", default=None, metavar="SOURCE",
                        help="把某源最新快照导出为通用 JSON 后退出（不分析）")
    parser.add_argument("--out", default=None, help="报告输出路径")
    parser.add_argument("--format", default=None, choices=["text", "md", "json", "html"],
                        help="报告格式（默认按 --out 扩展名推断，否则 text）")
    parser.add_argument("--examples", type=int, default=3, help="样例条数（默认 3）")
    parser.add_argument("--limit", type=int, default=None, help="限制处理单元数（抽样）")
    parser.add_argument("--table", type=int, default=0, help="明细表行数（默认 0）")
    parser.add_argument("--keep-db", action="store_true",
                        help="保留零适配模式生成的临时库（调试用）")
    args = parser.parse_args()

    fmt = _infer_format(args.out, args.format)

    if args.export:
        conn = db.connect(args.db) if args.db else db.connect()
        db.init_db(conn)
        data = export_source(conn, args.export)
        conn.close()
        text = json.dumps(data, ensure_ascii=False, indent=2)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"[{TOOL}] 已导出 {data['count']} 条 {args.export} 快照 -> {args.out}")
        else:
            print(text)
        return

    tmpdir = None
    if args.input:
        pairs = [_parse_input(a) for a in args.input]
        tmpdir = tempfile.mkdtemp(prefix="mcp-xsource-")
        conn = db.connect(Path(tmpdir) / "external.db")
        db.init_db(conn)
        info = ingest_exports(conn, pairs)
        mode = "zero-shot"
    else:
        conn = db.connect(args.db) if args.db else db.connect()
        db.init_db(conn)
        info = None
        mode = "baseline"

    result = analyze(conn, examples=args.examples, limit=args.limit)

    if fmt == "text":
        render_text(result, info=info, mode=mode, examples=args.examples,
                    table=args.table)
    else:
        if fmt == "md":
            text = render_md(result, info=info, mode=mode)
        elif fmt == "json":
            text = render_json(result, info=info, mode=mode)
        else:
            text = render_html(result, info=info, mode=mode)
        if args.out:
            Path(args.out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"[{TOOL}] 报告（{fmt}）已写出 -> {args.out}")
        else:
            print(text)

    conn.close()
    if tmpdir and not args.keep_db:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    main()
