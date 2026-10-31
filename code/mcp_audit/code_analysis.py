"""M3 代码侧能力抽取：从已克隆仓库中静态抽取「真实能力」信号。

动机（为什么必须有这一层）：
跨源对比的「声明侧」极薄 —— 七个源里只有 smithery(6,839) 与 modelscope(293)
声明了 tools，其余五源为 0；连官方 `server.json` 也只有 packages /
environmentVariables 而没有工具清单。因此「这个 MCP 到底能做什么」无法只靠
声明得到，必须从代码里静态抽取 —— 本模块即那份 ground truth。

产物 finding_type = 'code_capability'，供三条主线共用：
- C2 跨源分叉 / 同名不同芯：同一货号在各源被声明成什么 vs 代码里真实的什么；
- C3 跨源声明–行为一致性：声明能力集是否 ⊄ 代码真实能力集；
- C1 纵向漂移：逐轮代码能力的纵向变化。

安全边界：只读、静态，绝不执行仓库里的任何代码，也不改动仓库目录。
预演默认（只统计），--apply 才写 findings。

用法：
    python -m mcp_audit code-scan                     # 预演全量
    python -m mcp_audit code-scan --source smithery   # 限定源（分批用）
    python -m mcp_audit code-scan --limit 200         # 抽样试跑
    python -m mcp_audit code-scan --workers 16        # 调并行度
    python -m mcp_audit code-scan --apply             # 写库
"""

import json
import os
import re
import sqlite3
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from . import db

FINDING_TYPE = "code_capability"

# 扫描闸：单文件与单仓库的上限，防止个别巨型/生成代码仓库拖垮整轮 ----------
MAX_FILE_BYTES = 512 * 1024
MAX_FILES = 6000
MAX_TOOL_NAMES = 200

# 目录黑名单：依赖/产物/虚拟环境，既无分析价值又极大拖慢扫描
SKIP_DIRS = frozenset({
    ".git", "node_modules", "vendor", "dist", "dist-ssr", "build", "out",
    "target", ".venv", "venv", "__pycache__", ".next", ".nuxt", ".cache",
    "coverage", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "site-packages", ".idea", ".vscode", "third_party", "bower_components",
})

CODE_EXT = {
    ".py", ".js", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx", ".jsx",
    ".go", ".rs", ".java", ".rb", ".cs", ".php", ".kt", ".swift", ".scala",
}

LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript",
    ".cjs": "javascript", ".ts": "typescript", ".mts": "typescript",
    ".cts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".cs": "csharp", ".php": "php", ".kt": "kotlin", ".swift": "swift",
    ".scala": "scala",
}

MANIFEST_NAMES = frozenset({
    "server.json", "package.json", "pyproject.toml", "mcp.json",
    ".mcp.json", "smithery.yaml", "smithery.yml", "glama.json",
    "manifest.json", "setup.py", "cargo.toml", "go.mod",
})

# 工具注册：显式带名字的写法（捕获字面量名，质量高）------------------------
_TOOL_NAME_PATTERNS = (
    re.compile(r"@\w+\.tool\(\s*[\"']([A-Za-z0-9_.\-]{1,64})[\"']"),
    re.compile(r"\bTool\s*\(\s*name\s*=\s*[\"']([A-Za-z0-9_.\-]{1,64})[\"']"),
    re.compile(r"\b(?:tool|registerTool|addTool)\s*\(\s*[\"'`]([A-Za-z0-9_.\-]{1,64})[\"'`]"),
    re.compile(r"\bNewTool\s*\(\s*[\"'`]([A-Za-z0-9_.\-]{1,64})[\"'`]"),
)

# 工具注册：只表明「这里在注册工具」的信号（计数用，不取名字）--------------
_TOOL_SIGNAL_PATTERNS = (
    re.compile(r"@\w+\.tool\b"),
    re.compile(r"\btypes\.Tool\s*\("),
    re.compile(r"\badd_tool\s*\("),
    re.compile(r"\blist_tools\b"),
    re.compile(r"\bcall_tool\b"),
    re.compile(r"\bFastMCP\b"),
    re.compile(r"\bMcpServer\b"),
    re.compile(r"\bserver\.tool\b"),
    re.compile(r"\bsetRequestHandler\s*\("),
    re.compile(r"\bmcp\.NewTool\b"),
    re.compile(r"\bListToolsRequestSchema\b"),
)

# 危险面：静态标记「该仓库具备何种高风险能力」，不判善恶，只记事实 --------
_DANGER_PATTERNS = {
    "arbitrary_code": (
        re.compile(r"\beval\s*\("),
        re.compile(r"\bexec\s*\("),
        re.compile(r"\bnew\s+Function\s*\("),
        re.compile(r"\bcompile\s*\("),
    ),
    "subprocess": (
        re.compile(r"\bsubprocess\b"),
        re.compile(r"os\.system\s*\("),
        re.compile(r"os\.popen\s*\("),
        re.compile(r"\bPopen\s*\("),
        re.compile(r"child_process"),
        re.compile(r"\bexecSync\s*\("),
        re.compile(r"\bspawnSync\s*\("),
        re.compile(r"\.execFile\s*\("),
    ),
    "shell_true": (
        re.compile(r"shell\s*=\s*True"),
    ),
    "fs_destructive": (
        re.compile(r"\bshutil\.rmtree\b"),
        re.compile(r"os\.remove\s*\("),
        re.compile(r"os\.unlink\s*\("),
        re.compile(r"\bunlinkSync\s*\("),
        re.compile(r"\brmtree\s*\("),
        re.compile(r"\bwriteFileSync\s*\("),
        re.compile(r"\.writeFile\s*\("),
    ),
    "network_egress": (
        re.compile(r"requests\.(?:get|post|put|delete|request)\s*\("),
        re.compile(r"\bhttpx\."),
        re.compile(r"\burllib\b"),
        re.compile(r"\bfetch\s*\("),
        re.compile(r"\baxios\b"),
        re.compile(r"http\.request\s*\("),
        re.compile(r"\bsocket\b"),
        re.compile(r"\bwget\b"),
        re.compile(r"\bcurl\b"),
    ),
    "env_access": (
        re.compile(r"os\.environ"),
        re.compile(r"process\.env"),
        re.compile(r"\bgetenv\s*\("),
        re.compile(r"\bdotenv\b"),
    ),
    "unsafe_deserialize": (
        re.compile(r"pickle\.loads?\s*\("),
        re.compile(r"marshal\.loads?\s*\("),
        re.compile(r"yaml\.load\s*\("),
        re.compile(r"\bunserialize\s*\("),
        re.compile(r"\bJSON\.parse\s*\("),
    ),
    "db_access": (
        re.compile(r"\bsqlite3\b"),
        re.compile(r"\bpsycopg"),
        re.compile(r"\bmysql\b"),
        re.compile(r"execute\s*\(\s*[\"'](?:SELECT|INSERT|UPDATE|DELETE)", re.I),
    ),
    "credential_paths": (
        re.compile(r"\.ssh\b"),
        re.compile(r"\.aws\b"),
        re.compile(r"id_rsa"),
        re.compile(r"\.npmrc\b"),
        re.compile(r"\.netrc\b"),
        re.compile(r"\bcredentials\b"),
    ),
}


def _walk_files(root: Path):
    """就地遍历仓库，跳过黑名单目录，产出 (path, rel, ext)，带上限。"""
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            if fn == ".mcp_audit_meta.json":
                continue
            full = Path(dirpath) / fn
            count += 1
            yield full, fn, full.suffix.lower()
            if count >= MAX_FILES:
                return


def _parse_manifest(path: Path, filename: str) -> dict | None:
    """解析单个 manifest；只取对「能力/权限」有意义的字段，失败即静默跳过。"""
    low = filename.lower()
    if low.endswith((".yaml", ".yml", ".toml")):
        try:
            head = path.read_text(encoding="utf-8", errors="ignore")[:8000]
        except OSError:
            return None
        m = re.search(r"^\s*name\s*[:=]\s*[\"']?([^\"'\n]+)", head, re.M)
        return {"file": filename, "name": m.group(1).strip() if m else None}
    if not low.endswith(".json"):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    if low == "server.json":
        pkgs = data.get("packages") if isinstance(data.get("packages"), list) else []
        envs, transports = [], []
        for p in pkgs:
            if not isinstance(p, dict):
                continue
            tr = p.get("transport")
            if isinstance(tr, dict) and tr.get("type"):
                transports.append(tr["type"])
            for e in (p.get("environmentVariables") or []):
                if isinstance(e, dict) and e.get("name"):
                    envs.append({"name": e["name"], "secret": bool(e.get("isSecret"))})
        repo = data.get("repository")
        return {
            "file": filename,
            "kind": "registry_server",
            "name": data.get("name"),
            "version": data.get("version"),
            "repo": repo.get("url") if isinstance(repo, dict) else None,
            "transports": sorted(set(transports)),
            "env_vars": envs,
        }

    if low == "package.json":
        deps = data.get("dependencies") or {}
        dev = data.get("devDependencies") or {}
        return {
            "file": filename,
            "kind": "npm",
            "name": data.get("name"),
            "version": data.get("version"),
            "bin": bool(data.get("bin")),
            "deps": len(deps) if isinstance(deps, dict) else 0,
            "dev_deps": len(dev) if isinstance(dev, dict) else 0,
        }

    if low in ("mcp.json", ".mcp.json"):
        servers = data.get("mcpServers")
        return {
            "file": filename,
            "kind": "mcp_config",
            "servers": sorted(servers)[:50] if isinstance(servers, dict) else [],
        }

    if low == "glama.json":
        return {"file": filename, "kind": "glama", "name": data.get("name")}

    return {"file": filename, "kind": "json", "name": data.get("name")}


def scan_repo(root: Path) -> dict:
    """静态扫描一个仓库目录，返回能力/危险面信号。不抛异常、不碰磁盘写入。"""
    langs: set[str] = set()
    tool_names: set[str] = set()
    tool_signals = 0
    tool_name_count = 0
    danger: dict[str, int] = defaultdict(int)
    danger_ex: dict[str, str] = {}
    manifests: list[dict] = []
    files_scanned = 0
    truncated = False

    for path, filename, ext in _walk_files(root):
        if filename.lower() in MANIFEST_NAMES:
            m = _parse_manifest(path, filename)
            if m:
                manifests.append(m)

        if ext not in CODE_EXT:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        files_scanned += 1
        langs.add(LANG_BY_EXT.get(ext, ext.lstrip(".")))

        for pat in _TOOL_SIGNAL_PATTERNS:
            tool_signals += len(pat.findall(text))
        for pat in _TOOL_NAME_PATTERNS:
            for name in pat.findall(text):
                tool_name_count += 1
                if len(tool_names) < MAX_TOOL_NAMES:
                    tool_names.add(name)

        rel = os.path.relpath(path, root)
        for cat, pats in _DANGER_PATTERNS.items():
            for pat in pats:
                hits = len(pat.findall(text))
                if hits:
                    danger[cat] += hits
                    danger_ex.setdefault(cat, rel)

    if files_scanned >= MAX_FILES:
        truncated = True

    server_decl: dict | None = None
    env_vars = 0
    secrets = 0
    for m in manifests:
        if m.get("kind") == "registry_server":
            server_decl = m
            evs = m.get("env_vars") or []
            env_vars += len(evs)
            secrets += sum(1 for e in evs if e.get("secret"))

    return {
        "files_scanned": files_scanned,
        "truncated": truncated,
        "langs": sorted(langs),
        "tool_signals": tool_signals,
        "tool_names": sorted(tool_names),
        "tool_names_total": tool_name_count,
        "danger": dict(sorted(danger.items())),
        "danger_categories": sorted(danger),
        "danger_examples": danger_ex,
        "manifests": [m.get("file") for m in manifests],
        "manifest_kinds": sorted({m.get("kind") for m in manifests if m.get("kind")}),
        "declared_env_vars": env_vars,
        "declared_secrets": secrets,
        "declared_transports": (server_decl or {}).get("transports") or [],
        "declared_name": (server_decl or {}).get("name"),
        "declared_version": (server_decl or {}).get("version"),
        "declared_repo": (server_decl or {}).get("repo"),
    }


def _source_map(conn: sqlite3.Connection) -> dict:
    """norm_url -> 声明了该货号的源集合（跨源上下文，C2/C3 直接可用）。"""
    return {
        r["norm_url"]: r["sources"]
        for r in conn.execute(
            "SELECT norm_url, GROUP_CONCAT(DISTINCT source) AS sources "
            "FROM repo_index WHERE norm_url IS NOT NULL GROUP BY norm_url"
        )
    }


def _select_repos(conn: sqlite3.Connection, source: str | None, limit: int | None):
    sql = ("SELECT norm_url, disk_path, commit_sha, repo_size, source "
           "FROM clone_log WHERE status='ok' AND disk_path IS NOT NULL")
    params: list = []
    if source:
        sql += " AND source = ?"
        params.append(source)
    sql += " ORDER BY (repo_size IS NULL), repo_size ASC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()


def _scan_path(disk_path: str):
    """进程池工作单元：只做「定位目录 + 读盘 + 正则」，返回可 pickle 的 dict。

    必须是模块级函数（进程池要按名字重新导入），且刻意不碰数据库连接
    —— 写库一律回到主进程串行完成，SQLite 连接不跨进程。

    返回：None=目录缺失；{"error": True}=该仓扫描抛异常。绝不向上抛 ——
    无人值守的长跑里，任何一个畸形仓库都不能拖垮整池。
    """
    if not disk_path:
        return None
    try:
        root = Path(db.rebase_disk_path(disk_path))
        if not root.is_dir():
            return None
        return scan_repo(root)
    except Exception:  # noqa: BLE001 —— 兜底是刻意为之，见 docstring
        return {"error": True}


def run(conn: sqlite3.Connection, rows, apply: bool = False,
        examples: int = 5, workers: int = 1) -> dict:
    """逐仓扫描。apply=False 只统计不写库；apply=True 幂等重建 findings。

    workers>1 时用【进程池】并行扫描 —— 抽取器是正则密集的 CPU 工作，受 GIL
    限制线程池实测无加速，故用多进程。写库仍在主进程串行完成。
    """
    if apply:
        # 幂等重建，但只删「本批扫到的 norm_url」对应的旧记录 ——
        # 这样 --source 分批跑时，后一批不会把前一批的结果清掉（可分批、可续跑）。
        # 用临时表做半连接，避开 SQLite 999 个绑定参数的上限。
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _scan_urls (norm_url TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _scan_urls")
        conn.executemany("INSERT OR IGNORE INTO _scan_urls (norm_url) VALUES (?)",
                         [(r["norm_url"],) for r in rows])
        conn.execute(
            "DELETE FROM findings WHERE finding_type = ? "
            "AND name IN (SELECT norm_url FROM _scan_urls)", (FINDING_TYPE,))
        conn.commit()

    src_map = _source_map(conn)
    stats: dict = defaultdict(int)
    danger_dist: dict[str, int] = defaultdict(int)
    kind_dist: dict[str, int] = defaultdict(int)
    tool_examples: list[dict] = []
    danger_examples: list[dict] = []
    total = len(rows)

    insert_sql = ("INSERT INTO findings "
                  "(source, name, finding_type, data_json, created_at) "
                  "VALUES (?, ?, ?, ?, ?)")
    ts = db.now()

    def absorb(r, res, i: int) -> None:
        if res is None:
            stats["missing_dir"] += 1
            return
        if res.get("error"):
            stats["error"] += 1
            return
        stats["scanned"] += 1
        stats["files"] += res["files_scanned"]
        if res["tool_signals"]:
            stats["with_tool_signal"] += 1
        if res["tool_names"]:
            stats["with_tool_names"] += 1
        if res["manifests"]:
            stats["with_manifest"] += 1
        if res["danger"]:
            stats["with_danger"] += 1
        if res["truncated"]:
            stats["truncated"] += 1
        if res["declared_secrets"]:
            stats["declared_secrets"] += 1
        for cat in res["danger"]:
            danger_dist[cat] += 1
        for k in res["manifest_kinds"]:
            kind_dist[k] += 1

        if res["tool_names"] and len(tool_examples) < examples:
            tool_examples.append({"norm_url": r["norm_url"],
                                  "names": res["tool_names"][:6]})
        if res["danger"] and len(danger_examples) < examples:
            danger_examples.append({"norm_url": r["norm_url"],
                                    "cats": res["danger_categories"],
                                    "example": res["danger_examples"]})

        if apply:
            srcs = sorted(filter(
                None, (src_map.get(r["norm_url"]) or "").split(",")))
            payload = {
                "norm_url": r["norm_url"],
                "commit_sha": r["commit_sha"],
                "sources": srcs,
                **res,
            }
            conn.execute(insert_sql, (
                "+".join(srcs), r["norm_url"], FINDING_TYPE,
                json.dumps(payload, ensure_ascii=False), ts,
            ))
            if i % 500 == 0:
                conn.commit()

        if i % 200 == 0 or i == total:
            print(f"  ...进度 {i}/{total}（已扫 {stats['scanned']}，"
                  f"有工具信号 {stats['with_tool_signal']}）", flush=True)

    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for i, (r, res) in enumerate(zip(
                    rows,
                    ex.map(_scan_path, [r["disk_path"] for r in rows],
                           chunksize=4)), 1):
                absorb(r, res, i)
    else:
        for i, r in enumerate(rows, 1):
            absorb(r, _scan_path(r["disk_path"]), i)

    if apply:
        conn.commit()

    return {
        "total": total,
        "stats": dict(stats),
        "danger_dist": dict(sorted(danger_dist.items(),
                                   key=lambda x: -x[1])),
        "kind_dist": dict(sorted(kind_dist.items(), key=lambda x: -x[1])),
        "tool_examples": tool_examples,
        "danger_examples": danger_examples,
    }


def show(result: dict, examples: int) -> None:
    """打印覆盖率与分布。"""
    total = result["total"]
    st = result["stats"]
    scanned = st.get("scanned", 0)
    print()
    print(f"=== 代码侧能力抽取（{'写库' if result.get('applied') else '预演'}） ===")
    print(f"候选 ok 仓库      : {total}")
    print(f"实际扫描成功      : {scanned}"
          f"（目录缺失 {st.get('missing_dir', 0)}）")
    print(f"代码文件累计      : {st.get('files', 0)}")
    if scanned:
        pct = lambda n: f"{n / scanned * 100:.1f}%"  # noqa: E731
        print(f"检出工具注册信号  : {st.get('with_tool_signal', 0)}  ({pct(st.get('with_tool_signal', 0))})")
        print(f"抽到工具字面名    : {st.get('with_tool_names', 0)}  ({pct(st.get('with_tool_names', 0))})")
        print(f"含 manifest       : {st.get('with_manifest', 0)}  ({pct(st.get('with_manifest', 0))})")
        print(f"含危险面标记      : {st.get('with_danger', 0)}  ({pct(st.get('with_danger', 0))})")
        print(f"声明了必填密钥    : {st.get('declared_secrets', 0)}")
        print(f"扫描被上限截断    : {st.get('truncated', 0)}")
        print(f"扫描异常跳过      : {st.get('error', 0)}")

    print()
    print("危险面类别（命中仓库数）:")
    for cat, n in result["danger_dist"].items():
        print(f"  {cat:<22} {n}")

    print()
    print("manifest 类型分布:")
    for k, n in result["kind_dist"].items():
        print(f"  {k:<22} {n}")

    if examples and result["tool_examples"]:
        print(f"\n===== 抽到工具名样例 =====")
        for e in result["tool_examples"][:examples]:
            print(f"● {e['norm_url']}")
            print(f"    {', '.join(e['names'])}")
    if examples and result["danger_examples"]:
        print(f"\n===== 危险面样例 =====")
        for e in result["danger_examples"][:examples]:
            print(f"● {e['norm_url']}  {', '.join(e['cats'])}")
            print(f"    证据: {e['example']}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp_audit code-scan",
        description="代码侧能力抽取（写入 findings，finding_type='code_capability'）")
    parser.add_argument("--source", default=None,
                        help="限定源（按 clone_log.source 分批，非归属口径）")
    parser.add_argument("--limit", type=int, default=None, help="限制仓库数（抽样）")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    parser.add_argument("--workers", type=int, default=8,
                        help="并行扫描进程数（默认 8；机械盘/磁盘慢可调小到 4）")
    parser.add_argument("--examples", type=int, default=5,
                        help="打印样例数（默认 5，0 不打印）")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    rows = _select_repos(conn, args.source, args.limit)
    print(f"待扫描 ok 仓库 {len(rows)} 个（source={args.source or '全源'}，"
          f"workers={args.workers}）")
    result = run(conn, rows, apply=args.apply, examples=args.examples,
                 workers=args.workers)
    result["applied"] = args.apply
    show(result, args.examples)
    conn.close()
