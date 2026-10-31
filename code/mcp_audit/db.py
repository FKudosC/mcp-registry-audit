"""SQLite 存储层：schema 初始化、迁移与读写助手。

表结构（M2 后）：
- rounds       每轮爬取元数据（多源：official / smithery）
- servers      server 最新状态（每源独立，name + source 主键）
- snapshots    每轮每版本完整快照（含 Smithery 的 tools 声明与市场指标）
- repo_index   每个 (name, source) -> 归一化仓库货号 norm_url 的映射（跨源对齐键）
- findings     M3+ 阶段的分析发现
- clone_log    M2a 仓库克隆状态（norm_url 为主键：同一仓库全源只克隆一份）

跨源对比口径：
- 描述/元数据逐源逐条保留在 servers / snapshots（对比证据底账，不合并）；
- "同一个 MCP"按 norm_url（归一化 GitHub 仓库货号）对齐；
- 视图 v_source_repo（同源内去重后的实体清单）、
  v_cross_source_repo（跨源对齐：同一货号出现在多个源）。

迁移：init_db() 对已存在的旧库自动 ALTER 补列 + 重建 clone_log 主键 +
重定位历史 disk_path（并剔除磁盘上已不存在的"假成功"记录），无需手工干预。
"""

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    done          INTEGER NOT NULL DEFAULT 0,
    last_cursor   TEXT,
    servers_seen INTEGER NOT NULL DEFAULT 0,
    pages_fetched INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS servers (
    name          TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'official',
    title         TEXT,
    description   TEXT,
    version       TEXT,
    status        TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    PRIMARY KEY (name, source)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id      INTEGER NOT NULL REFERENCES rounds(id),
    source        TEXT NOT NULL DEFAULT 'official',
    name          TEXT NOT NULL,
    version       TEXT NOT NULL,
    title         TEXT,
    description   TEXT,
    status        TEXT,
    published_at  TEXT,
    updated_at    TEXT,
    is_latest     INTEGER,
    remotes_json  TEXT,
    code_json     TEXT,
    tools_json    TEXT,
    packages_json TEXT,
    market_json   TEXT,
    raw_json      TEXT,
    fetched_at    TEXT NOT NULL,
    UNIQUE(round_id, source, name, version)
);

CREATE TABLE IF NOT EXISTS findings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id     INTEGER,
    source       TEXT,
    name         TEXT,
    version      TEXT,
    finding_type TEXT NOT NULL,
    data_json    TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clone_log (
    norm_url     TEXT PRIMARY KEY,
    repo_url     TEXT NOT NULL,
    name         TEXT,
    source       TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    disk_path    TEXT,
    repo_size    INTEGER,
    commit_sha   TEXT,
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    cloned_at    TEXT,
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS repo_index (
    name         TEXT NOT NULL,
    source       TEXT NOT NULL,
    repo_url     TEXT,
    norm_url     TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (name, source)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_name ON snapshots(name, fetched_at);
CREATE INDEX IF NOT EXISTS idx_findings_name ON findings(name);
CREATE INDEX IF NOT EXISTS idx_clone_status ON clone_log(status);
CREATE INDEX IF NOT EXISTS idx_repo_index_norm ON repo_index(norm_url);

CREATE VIEW IF NOT EXISTS v_source_repo AS
SELECT source, norm_url, MIN(repo_url) AS repo_url,
       COUNT(*) AS entry_count,
       GROUP_CONCAT(name, ' | ') AS names
FROM repo_index
WHERE norm_url IS NOT NULL
GROUP BY source, norm_url;

CREATE VIEW IF NOT EXISTS v_cross_source_repo AS
SELECT norm_url,
       COUNT(DISTINCT source) AS source_count,
       COUNT(*) AS entry_count,
       GROUP_CONCAT(DISTINCT source) AS sources
FROM repo_index
WHERE norm_url IS NOT NULL
GROUP BY norm_url
HAVING COUNT(DISTINCT source) > 1;
"""

# 旧库（M1）迁移：列 -> 目标类型/默认
_MIGRATIONS = {
    "servers": {
        "source": "TEXT NOT NULL DEFAULT 'official'",
    },
    "snapshots": {
        "source": "TEXT NOT NULL DEFAULT 'official'",
        "market_json": "TEXT",
    },
    "findings": {
        "source": "TEXT",
    },
    "clone_log": {
        "commit_sha": "TEXT",
    },
}


def now() -> str:
    """UTC ISO 时间戳，全程统一。"""
    return datetime.now(timezone.utc).isoformat()


def rebase_disk_path(path: str | None) -> str | None:
    """把历史 disk_path 重定位到当前 REPOS_DIR。

    旧库记录的 disk_path 指向迁移前的仓库根目录（如从 C 盘挪到 D 盘前的位置），
    文件其实已随 REPOS_DIR 变更整体搬迁：同名的目录若存在于当前 REPOS_DIR 下，
    就改写为新根下的路径，避免"已下载"被误判为不存在而重复下载。
    """
    if not path:
        return path
    p = Path(path)
    if p.exists():
        return str(p)
    cand = config.REPOS_DIR / p.name
    if cand.exists():
        return str(cand)
    return path


def _migrate(conn: sqlite3.Connection) -> None:
    """对已有表补缺失列 + 重建单列主键表（M1 -> M2 升级）。"""
    # 1) 补列
    for table, cols in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    conn.commit()

    # 2) servers 表：M1 版主键是 name（单列），M2 需要 (name, source) 复合主键。
    #    SQLite 不能 ALTER 主键 -> 重建并搬数据。
    pk = [r["name"] for r in conn.execute("PRAGMA table_info(servers)") if r["pk"]]
    if pk == ["name"]:
        conn.executescript("""
            ALTER TABLE servers RENAME TO servers_old;
        """)
        # 用 SCHEMA 里的新表定义重建（先删再建，避免索引残留）
        conn.execute("DROP TABLE IF EXISTS servers")
        conn.executescript(SCHEMA)
        conn.execute(
            """
            INSERT OR IGNORE INTO servers (
                name, source, title, description, version, status,
                first_seen_at, last_seen_at
            ) SELECT name, 'official', title, description, version, status,
                      first_seen_at, last_seen_at FROM servers_old
            """
        )
        conn.execute("DROP TABLE servers_old")
        conn.commit()

    # 3) 回填 M1 旧快照的 market_json（repository 存在 raw_json 里）
    conn.execute(
        """
        UPDATE snapshots SET market_json = json_object(
            'repository', json_extract(raw_json, '$.server.repository')
        ) WHERE source = 'official' AND market_json IS NULL
        """
    )
    conn.commit()

    # 4) clone_log 表：旧版主键是 name（同一仓库换 name 就重复克隆）。
    #    M2a 改为按归一化仓库货号 norm_url 去重 -> 重建并搬数据。
    #    同货号多条旧记录合并为一条：优先保留 ok、其次有 disk_path、再次 attempts 大者，
    #    其 disk_path 直接沿用（已有磁盘文件不重下）；其余名字仍留在 repo_index 里可回溯。
    pk = [r["name"] for r in conn.execute("PRAGMA table_info(clone_log)") if r["pk"]]
    if pk == ["name"]:
        old = [dict(r) for r in conn.execute("SELECT * FROM clone_log")]
        conn.execute("DROP INDEX IF EXISTS idx_clone_status")
        conn.execute("ALTER TABLE clone_log RENAME TO clone_log_old")
        conn.execute(
            """
            CREATE TABLE clone_log (
                norm_url     TEXT PRIMARY KEY,
                repo_url     TEXT NOT NULL,
                name         TEXT,
                source       TEXT,
                status       TEXT NOT NULL DEFAULT 'pending',
                disk_path    TEXT,
                repo_size    INTEGER,
                commit_sha   TEXT,
                error        TEXT,
                attempts     INTEGER NOT NULL DEFAULT 0,
                cloned_at    TEXT,
                updated_at   TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_clone_status ON clone_log(status)")

        best: dict[str, tuple] = {}
        for r in old:
            repo_url = (r.get("repo_url") or "").strip()
            key = norm_repo_url(repo_url) or repo_url.lower()
            if not key:
                continue
            rank = (
                1 if r.get("status") == "ok" else 0,
                1 if r.get("disk_path") else 0,
                r.get("attempts") or 0,
            )
            if key not in best or rank > best[key][0]:
                best[key] = (rank, r)
        for key, (_, r) in best.items():
            conn.execute(
                """
                INSERT OR IGNORE INTO clone_log (
                    norm_url, repo_url, name, source, status, disk_path,
                    repo_size, error, attempts, cloned_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key, r.get("repo_url") or key, r.get("name"), r.get("source"),
                    r.get("status") or "pending", r.get("disk_path"),
                    r.get("repo_size"), r.get("error"), r.get("attempts") or 0,
                    r.get("cloned_at"), r.get("updated_at"),
                ),
            )
        conn.execute("DROP TABLE clone_log_old")
        conn.commit()

    # 5) disk_path 重定位 + 成功记录体检：
    #    - 旧根目录（迁移前的 REPOS_DIR）下的路径改写为当前 REPOS_DIR；
    #    - 标为 ok 但磁盘上已找不到代码的，降级为 pending 待重下，
    #      避免"假成功"（后续分析读不到文件、又以为已下载）。
    fixed = demoted = 0
    for r in conn.execute(
        "SELECT norm_url, disk_path, status FROM clone_log"
    ).fetchall():
        disk_path = r["disk_path"]
        new_path = rebase_disk_path(disk_path)
        if new_path != disk_path:
            conn.execute(
                "UPDATE clone_log SET disk_path=?, updated_at=? WHERE norm_url=?",
                (new_path, now(), r["norm_url"]),
            )
            disk_path = new_path
            fixed += 1
        if r["status"] == "ok" and not (disk_path and Path(disk_path).exists()):
            conn.execute(
                "UPDATE clone_log SET status='pending', disk_path=NULL, updated_at=? "
                "WHERE norm_url=?",
                (now(), r["norm_url"]),
            )
            demoted += 1
    conn.commit()


def connect(db_path=None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)


# -------------------------------------------------- 仓库货号 / 跨源映射 --

# 只认 github.com，且 host 后必须是 / 或 :（挡掉 github.com.evil.com 之类）
_GITHUB_RE = re.compile(
    r"^(?:https?://)?(?:git@)?(?:www\.)?github\.com[/:]([^/]+)/([^/?#]+?)"
    r"(?:\.git)?(?:[/?#].*)?$",
    re.IGNORECASE,
)


_GITHUB_RESERVED_OWNERS = frozenset({
    "user-attachments", "orgs", "apps", "marketplace", "features", "topics",
    "sponsors", "settings", "about", "pricing", "enterprise", "collections",
    "trending", "notifications", "login", "join", "new", "search", "explore",
    "dashboard", "account", "organizations", "site", "contact",
})

def norm_repo_url(url: str | None) -> str | None:
    """GitHub 仓库 URL -> 归一化货号 `github.com/owner/repo`（小写）。

    统一 https/http、sslv、www、.git 后缀、尾部斜杠、/tree/xxx 子路径、
    git@ 形式；非 GitHub 返回 None。

    只剔除 GitHub **系统命名空间** 下的 URL（user-attachments 附件 CDN、
    orgs / apps / marketplace 等组织级页面）——这些 owner 是 GitHub 保留名，
    不可能是真实用户，因而绝不可能是仓库。

    注意：repo 名**不做**黑名单。实测 `issues` / `releases` / `wiki` /
    `projects` / `network` / `pulse` 等既可能是 GitHub UI 路径，也确实是
    真实存在的仓库名（已逐个核实），一律黑名单会误杀真实 MCP 仓库。
    """
    if not url or "github.com" not in url.lower():
        return None
    m = _GITHUB_RE.match(url.strip())
    if not m:
        return None
    owner, repo = m.group(1).lower(), m.group(2).lower()
    if owner in _GITHUB_RESERVED_OWNERS:
        return None
    return f"github.com/{owner}/{repo}"


_MEDIA_FILE_RE = re.compile(
    r"\.(?:jpe?g|png|gif|svg|webp|bmp|ico|mp4|mov|webm|avi|pdf|zip|gz)(?:$|[?#])",
    re.IGNORECASE,
)


def _github_ref_rank(url: str) -> int:
    """GitHub URL 作为「MCP 仓库货号」的可信度排序（越小越好）。

    0 = 仓库根链接（github.com/owner/repo，可带 ?query / #hash）；
    1 = 仓库内深层路径（/tree/、/blob/ 指向代码文件）；
    2 = 指向媒体/压缩包的 blob|raw|download 链接（README 截图，几乎必是误抓）；
    9 = 非 GitHub / 命中保留 owner，不可用作货号。
    """
    if not url or "github.com" not in url.lower():
        return 9
    m = _GITHUB_RE.match(url.strip())
    if not m:
        return 9
    if m.group(1).lower() in _GITHUB_RESERVED_OWNERS:
        return 9
    rest = re.sub(r"\.git$", "", url.strip()[m.end(2):], flags=re.IGNORECASE)
    rest = rest.strip("/")
    if not rest or rest[0] in "?#":
        return 0
    low = rest.lower()
    if ("blob/" in low or "raw/" in low) and _MEDIA_FILE_RE.search(low):
        return 2
    return 1


def _loads(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def repo_url_for_row(source: str, market, code, raw=None) -> str | None:
    """按源从 market/code 里挑出最可信的 GitHub 仓库 URL。

    各源线索字段不同：official=repository.url；glama=owner/repo；cursor=github；
    smithery=homepage；modelscope=fromSiteUrl/FromSitePath；
    另外统一从 code_json 的 git 项兜底。

    `raw` 为 verbatim 原始 JSON 兜底：cursor 详情页会把 README 截图 CDN 链接
    （github.com/user-attachments/assets/...）排在真仓库之前，market/code 会挑错；
    raw["github"] 保留完整出现顺序，这里按 _github_ref_rank 过滤后取**首个可用**
    （保持页面原始顺序，不做重排）：剔除保留 owner 页与媒体文件 blob/raw 链接，
    但保留仓库根与仓库内代码深链。
    """
    m = market if isinstance(market, dict) else {}
    cands: list = []

    repo = m.get("repository")
    if isinstance(repo, dict):
        cands.append(repo.get("url"))
    for key in ("url", "github", "homepage", "repo_url", "fromSiteUrl",
                "codeRepository"):
        v = m.get(key)
        if isinstance(v, str):
            cands.append(v)
    if source == "glama":
        owner, name = m.get("owner"), m.get("repo")
        if owner and name:
            cands.append(f"https://github.com/{owner}/{name}")
    if source == "modelscope" and "github" in str(m.get("fromSite") or "").lower():
        path = str(m.get("fromSitePath") or "").strip("/")
        if path.count("/") >= 1:
            cands.append("https://github.com/" + path)
    if isinstance(code, list):
        for item in code:
            if isinstance(item, dict) and isinstance(item.get("url"), str):
                cands.append(item["url"])

    raw_seq: list = []
    if isinstance(raw, dict):
        for key in ("github", "github_urls", "githubUrls"):
            seq = raw.get(key)
            if isinstance(seq, list):
                raw_seq.extend(u for u in seq if isinstance(u, str))

    for c in cands + raw_seq:
        if not isinstance(c, str) or "cursor/community-plugins" in c:
            continue
        if _github_ref_rank(c) <= 1:
            return c.strip()

    return None


def rebuild_repo_index(conn: sqlite3.Connection) -> int:
    """全量重建 (name, source) -> norm_url 映射（取每 (source, name) 的最新快照）。

    只收录能确定 GitHub 仓库的条目；无仓库的（纯托管/远程）留空，不参与跨源对齐。
    返回写入条数。
    """
    rows = conn.execute(
        "SELECT source, name, market_json, code_json, raw_json FROM snapshots "
        "WHERE id IN (SELECT MAX(id) FROM snapshots GROUP BY source, name)"
    ).fetchall()
    conn.execute("DELETE FROM repo_index")
    ts = now()
    written = 0
    for r in rows:
        url = repo_url_for_row(
            r["source"], _loads(r["market_json"]), _loads(r["code_json"]),
            _loads(r["raw_json"]),
        )
        if not url:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO repo_index "
            "(name, source, repo_url, norm_url, updated_at) VALUES (?, ?, ?, ?, ?)",
            (r["name"], r["source"], url, norm_repo_url(url), ts),
        )
        written += 1
    conn.commit()
    return written


# ---------------------------------------------------------------- rounds --

def start_round(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO rounds (source, started_at) VALUES (?, ?)",
        (source, now()),
    )
    conn.commit()
    return cur.lastrowid


def get_open_round(conn: sqlite3.Connection, source: str):
    """取该源最近一轮未完成的 round（断点续爬），无则返回 None。"""
    return conn.execute(
        "SELECT * FROM rounds WHERE source = ? AND done = 0 "
        "ORDER BY id DESC LIMIT 1",
        (source,),
    ).fetchone()


def mark_round_done(conn: sqlite3.Connection, round_id: int) -> None:
    conn.execute(
        "UPDATE rounds SET done = 1, finished_at = ? WHERE id = ?",
        (now(), round_id),
    )
    conn.commit()


def update_round_progress(
    conn: sqlite3.Connection, round_id: int, cursor: str | None, seen: int, pages: int
) -> None:
    conn.execute(
        "UPDATE rounds SET last_cursor = ?, servers_seen = ?, pages_fetched = ? "
        "WHERE id = ?",
        (cursor, seen, pages, round_id),
    )
    conn.commit()


# ------------------------------------------------------------- snapshots --

def extract_official_entry(entry: dict) -> dict:
    """官方 registry 条目 -> 统一结构。"""
    s = entry.get("server") or {}
    meta = entry.get("_meta") or {}
    official = meta.get("io.modelcontextprotocol.registry/official") or {}
    return {
        "name": s.get("name") or "",
        "version": s.get("version") or "",
        "title": s.get("title"),
        "description": s.get("description"),
        "status": official.get("status"),
        "published_at": official.get("publishedAt"),
        "updated_at": official.get("updatedAt"),
        "is_latest": official.get("isLatest"),
        "remotes_json": json.dumps(s.get("remotes") or [], ensure_ascii=False),
        "code_json": json.dumps(s.get("code") or [], ensure_ascii=False),
        "tools_json": json.dumps(s.get("tools") or [], ensure_ascii=False),
        "packages_json": json.dumps(s.get("packages") or [], ensure_ascii=False),
        "market_json": json.dumps(
            {"repository": s.get("repository")}, ensure_ascii=False
        ),
        "raw_json": json.dumps(entry, ensure_ascii=False),
    }


def extract_smithery_entry(list_item: dict, detail: dict | None) -> dict:
    """Smithery 列表条目（+ 可选详情）-> 统一结构。

    tools 声明与市场指标（useCount/verified 等）来自详情端点。
    """
    detail = detail or {}
    tools = detail.get("tools") or []
    market = {
        "useCount": list_item.get("useCount"),
        "verified": list_item.get("verified"),
        "createdAt": list_item.get("createdAt"),
        "remote": list_item.get("remote"),
        "isDeployed": list_item.get("isDeployed"),
        "bySmithery": list_item.get("bySmithery"),
        "unlisted": list_item.get("unlisted"),
        "inactive": list_item.get("inactive"),
        "homepage": list_item.get("homepage"),
        "owner": list_item.get("owner"),
        "id": list_item.get("id"),
    }
    # 详情端点的额外信息（安全扫描结论 / 部署地址 / 工具总数）
    if detail:
        market["deploymentUrl"] = detail.get("deploymentUrl")
        market["security"] = detail.get("security")
        market["tools_count"] = len(tools)
        market["resources_count"] = len(detail.get("resources") or [])
        market["prompts_count"] = len(detail.get("prompts") or [])
    return {
        "name": list_item.get("qualifiedName") or "",
        "version": "latest",  # Smithery 列表无版本概念，以轮次为时间轴
        "title": list_item.get("displayName"),
        "description": list_item.get("description"),
        "status": "inactive" if list_item.get("inactive") else "active",
        "published_at": list_item.get("createdAt"),
        "updated_at": None,
        "is_latest": 1,
        "remotes_json": json.dumps(
            [{"type": "http", "url": detail.get("deploymentUrl")}]
            if detail.get("deploymentUrl") else [],
            ensure_ascii=False,
        ),
        "code_json": "[]",
        "tools_json": json.dumps(tools, ensure_ascii=False),
        "packages_json": "[]",
        "market_json": json.dumps(market, ensure_ascii=False),
        "raw_json": json.dumps(
            {"list": list_item, "detail": detail}, ensure_ascii=False
        ),
    }


def extract_modelscope_entry(m: dict) -> dict:
    """魔搭 ModelScope MCP 广场条目 -> 统一结构。

    该 API 单条即含 51 字段（Tools 声明、FromSite 跨市场溯源、
    CallVolume/Stars 等市场指标），无需二次详情请求。
    Publisher 是唯一限定名（如 @modelcontextprotocol/fetch）。
    """
    tools = m.get("Tools") or []
    market = {
        "id": m.get("Id"),
        "chineseName": m.get("ChineseName"),
        "callVolume": m.get("CallVolume"),
        "stars": m.get("Stars"),
        "viewCount": m.get("ViewCount"),
        "hosted": m.get("Hosted"),
        "fromSite": m.get("FromSite"),
        "fromSiteUrl": m.get("FromSiteUrl"),
        "fromSiteAuthor": m.get("FromSiteAuthor"),
        "fromSitePath": m.get("FromSitePath"),
        "category": m.get("Category"),
        "tags": m.get("Tags"),
        "license": m.get("License"),
        "verified": m.get("Verifed"),  # 魔搭 API 自身拼写
        "organization": m.get("Organization"),
        "gmtCreated": m.get("GmtCreated"),
        "gmtUpdated": m.get("GmtUpdated"),
        "lang": m.get("Lang"),
        "tools_count": len(tools),
    }
    deployed = m.get("DeployedUrl")
    return {
        "name": m.get("Publisher") or f"{m.get('Path')}/{m.get('Name')}",
        "version": "latest",
        "title": m.get("ChineseName") or m.get("Name"),
        "description": m.get("AbstractCN") or m.get("Abstract"),
        "status": "active",
        "published_at": m.get("GmtCreated"),
        "updated_at": m.get("GmtUpdated"),
        "is_latest": 1,
        "remotes_json": json.dumps(
            [{"type": "http", "url": deployed}] if deployed else [],
            ensure_ascii=False,
        ),
        "code_json": "[]",
        "tools_json": json.dumps(tools, ensure_ascii=False),
        "packages_json": "[]",
        "market_json": json.dumps(market, ensure_ascii=False),
        "raw_json": json.dumps(m, ensure_ascii=False),
    }


def extract_mcpso_entry(url: str, jsonld: dict | None, meta_desc: str | None,
                        repo_url: str | None = None) -> dict:
    """mcp.so 详情页（sitemap URL + JSON-LD + 页内 repoUrl）-> 统一结构。

    repo_url 取自详情页 RSC 数据块的 `repoUrl:` 字段（JSON-LD 的 url 是
    mcp.so 自身页面地址，不含仓库地址），是 mcp.so 唯一可用于跨源对齐的线索。
    """
    jsonld = jsonld or {}
    slug = url.rstrip("/").split("/")[-1]
    author = jsonld.get("author") or {}
    market = {
        "slug": slug,
        "category": jsonld.get("applicationCategory"),
        "author": author.get("name"),
        "author_type": author.get("@type"),
        "image": jsonld.get("image"),
        "operatingSystem": jsonld.get("operatingSystem"),
        "repo_url": repo_url,
        "source": "mcp.so",
    }
    return {
        "name": slug,
        "version": "latest",
        "title": jsonld.get("name"),
        "description": jsonld.get("description") or meta_desc,
        "status": "active",
        "published_at": None,
        "updated_at": None,
        "is_latest": 1,
        "remotes_json": "[]",
        "code_json": "[]",
        "tools_json": "[]",
        "packages_json": "[]",
        "market_json": json.dumps(market, ensure_ascii=False),
        "raw_json": json.dumps(
            {"url": url, "repo_url": repo_url, "jsonld": jsonld},
            ensure_ascii=False,
        ),
    }


def extract_glama_entry(url: str) -> dict:
    """Glama sitemap URL -> 统一结构（身份收割：URL 即 owner/repo）。

    https://glama.ai/mcp/servers/{owner}/{repo} —— 跨市场 join 键。
    """
    tail = url.rstrip("/").split("/mcp/servers/")[-1]
    parts = tail.split("/")
    owner = parts[0] if parts else ""
    repo = "/".join(parts[1:]) if len(parts) > 1 else ""
    market = {
        "owner": owner,
        "repo": repo,
        "url": url,
        "source": "glama",
    }
    return {
        "name": tail.replace("/", "__") or owner,
        "version": "latest",
        "title": repo or owner,
        "description": None,
        "status": "active",
        "published_at": None,
        "updated_at": None,
        "is_latest": 1,
        "remotes_json": "[]",
        "code_json": "[]",
        "tools_json": "[]",
        "packages_json": "[]",
        "market_json": json.dumps(market, ensure_ascii=False),
        "raw_json": json.dumps({"url": url}, ensure_ascii=False),
    }


def extract_aibase_entry(item: dict) -> dict:
    """AIbase 仓库索引条目（GitHub API 风格 JSON）-> 统一结构。

    每条本身就是一个 GitHub 仓库：html_url 即跨源 join 键，因此 name 取
    `owner/repo`（避免不同 owner 的同名仓库在 snapshots 里主键冲突）。
    """
    item = item or {}
    html_url = item.get("html_url") or ""
    m = _GITHUB_RE.match(html_url.strip())
    owner = m.group(1) if m else item.get("full_name")
    repo = m.group(2) if m else item.get("name")
    full = f"{owner}/{repo}" if owner and repo else (item.get("full_name")
                                                     or item.get("name"))
    lic = item.get("license")
    market = {
        "id": item.get("Id"),
        "owner": owner,
        "repo": repo,
        "full_name": item.get("full_name"),
        "github": html_url,
        "homepage": item.get("homepage"),
        "language": item.get("language"),
        "topics": item.get("topics"),
        "stars": item.get("stargazers_count"),
        "forks": item.get("forks_count"),
        "license": lic.get("spdx_id") if isinstance(lic, dict) else lic,
        "owner_type": item.get("type"),
        "source": "aibase",
    }
    return {
        "name": full,
        "version": "latest",
        "title": item.get("name"),
        "description": item.get("description") or item.get("desc"),
        "status": "active",
        "published_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "is_latest": 1,
        "remotes_json": "[]",
        "code_json": json.dumps(
            [{"type": "git", "url": html_url}] if html_url else [],
            ensure_ascii=False,
        ),
        "tools_json": "[]",
        "packages_json": "[]",
        "market_json": json.dumps(market, ensure_ascii=False),
        "raw_json": json.dumps(item, ensure_ascii=False),
    }


def extract_cursor_entry(url: str, meta_desc: str | None,
                         og_title: str | None, github_urls: list[str]) -> dict:
    """Cursor Directory plugin 详情页（og meta + GitHub 链接）-> 统一结构。"""
    slug = url.rstrip("/").split("/")[-1]
    # og: "Drizzle Docs | Cursor Directory" -> 标题；排除社区仓库链接
    title = og_title.split("|")[0].strip() if og_title else None
    main_gh = None
    for u in github_urls:
        if "cursor/community-plugins" in u:
            continue
        if norm_repo_url(u) is None:
            continue
        main_gh = u
        break
    market = {
        "slug": slug,
        "is_mcp": "mcp" in slug.lower() or (meta_desc or "").lower().startswith("mcp"),
        "github": main_gh,
        "source": "cursor",
    }
    return {
        "name": slug,
        "version": "latest",
        "title": title,
        "description": meta_desc,
        "status": "active",
        "published_at": None,
        "updated_at": None,
        "is_latest": 1,
        "remotes_json": "[]",
        "code_json": json.dumps(
            [{"type": "git", "url": main_gh}] if main_gh else [],
            ensure_ascii=False,
        ),
        "tools_json": "[]",
        "packages_json": "[]",
        "market_json": json.dumps(market, ensure_ascii=False),
        "raw_json": json.dumps(
            {"url": url, "og_title": og_title, "github": github_urls},
            ensure_ascii=False,
        ),
    }


def save_entries(
    conn: sqlite3.Connection, round_id: int, source: str, entries: list[dict]
) -> int:
    """批量写入快照 + 更新 servers 最新状态。返回新写入条数。"""
    ts = now()
    written = 0
    for e in entries:
        if not e["name"]:
            continue
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO snapshots (
                round_id, source, name, version, title, description, status,
                published_at, updated_at, is_latest,
                remotes_json, code_json, tools_json, packages_json,
                market_json, raw_json, fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                round_id, source, e["name"], e["version"], e["title"],
                e["description"], e["status"], e["published_at"], e["updated_at"],
                1 if e["is_latest"] else 0,
                e["remotes_json"], e["code_json"], e["tools_json"],
                e["packages_json"], e["market_json"], e["raw_json"], ts,
            ),
        )
        written += cur.rowcount if cur.rowcount > 0 else 0
        conn.execute(
            """
            INSERT INTO servers (
                name, source, title, description, version, status,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name, source) DO UPDATE SET
                title = excluded.title,
                description = excluded.description,
                version = excluded.version,
                status = excluded.status,
                last_seen_at = excluded.last_seen_at
            """,
            (e["name"], source, e["title"], e["description"], e["version"],
             e["status"], ts, ts),
        )
    conn.commit()
    return written


# ------------------------------------------------------------------ stats --

def stats(conn: sqlite3.Connection) -> dict:
    out = {}
    out["rounds"] = conn.execute(
        "SELECT id, source, started_at, finished_at, done, servers_seen, "
        "pages_fetched FROM rounds ORDER BY id"
    ).fetchall()
    out["total_servers"] = conn.execute("SELECT COUNT(*) AS c FROM servers").fetchone()["c"]
    out["by_source"] = conn.execute(
        "SELECT source, COUNT(*) AS c FROM servers GROUP BY source"
    ).fetchall()
    out["total_snapshots"] = conn.execute(
        "SELECT COUNT(*) AS c FROM snapshots"
    ).fetchone()["c"]
    out["total_findings"] = conn.execute(
        "SELECT COUNT(*) AS c FROM findings"
    ).fetchone()["c"]
    out["with_tools_declared"] = conn.execute(
        "SELECT source, COUNT(*) AS c FROM snapshots "
        "WHERE tools_json != '[]' GROUP BY source"
    ).fetchall()
    out["clone_status"] = conn.execute(
        "SELECT status, COUNT(*) AS c FROM clone_log GROUP BY status"
    ).fetchall()
    return out


# 按「源 × 状态」展开 clone 状态（供 crawl / clone 两条 CLI 汇总复用）
# 生命周期：pending -> ok / failed -> abandoned（放弃重试）
#                        \-> gone（仓库已删/私有/非仓库，永久）
#                        \-> skipped_large（超体积上限，有意跳过）
#           not_retrieved 为历史遗留终态（无法取得代码）
_CLONE_STATUS_ORDER = ("ok", "pending", "failed", "abandoned", "gone",
                       "not_retrieved", "skipped_large")


def clone_status_by_source(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """聚合 clone_log：源取归属源（按货号去重后唯一），行间互斥不重复。"""
    return conn.execute(
        "SELECT COALESCE(source, '(未归属)') AS source, status, COUNT(*) AS c, "
        "SUM(COALESCE(repo_size,0)) AS s FROM clone_log GROUP BY source, status"
    ).fetchall()


def _disp_width(s: str) -> int:
    """终端显示宽度：CJK 全角字符占 2 列，其余占 1 列（用于表格对齐）。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _pad(s: str, width: int, right: bool = False) -> str:
    gap = " " * max(0, width - _disp_width(s))
    return gap + s if right else s + gap


def format_clone_by_source(conn: sqlite3.Connection, indent: str = "  ") -> list[str]:
    """把 clone_log 渲染成「源 × 状态」文本表（含表头，不含标题）。"""
    rows = clone_status_by_source(conn)
    if not rows:
        return [f"{indent}（clone_log 为空）"]

    agg: dict[str, dict[str, int]] = {}
    sizes: dict[str, int] = {}
    seen: set[str] = set()
    for r in rows:
        agg.setdefault(r["source"], {})[r["status"]] = r["c"]
        sizes[r["source"]] = sizes.get(r["source"], 0) + (r["s"] or 0)
        seen.add(r["status"])
    statuses = [s for s in _CLONE_STATUS_ORDER if s in seen]
    statuses += sorted(seen - set(statuses))

    src_w = max([_disp_width("源")] + [_disp_width(s) for s in agg])
    col_w = {s: max(_disp_width(s), 6) for s in statuses}
    total_w, size_w = 7, 9

    def _row(cells: list[tuple[str, int]]) -> str:
        # 首列左对齐，其余右对齐；列间留一个空格，避免表头文字贴连
        return indent + " ".join(
            _pad(t, w, right=(i > 0)) for i, (t, w) in enumerate(cells)
        )

    out = [_row([("源", src_w)] + [(s, col_w[s]) for s in statuses]
                + [("合计", total_w), ("体积", size_w)])]
    for src in sorted(agg, key=lambda s: (-sum(agg[s].values()), s)):
        counts = agg[src]
        out.append(_row(
            [(src, src_w)]
            + [(str(counts.get(s, 0)), col_w[s]) for s in statuses]
            + [(str(sum(counts.values())), total_w),
               (f"{sizes[src] / 1024**3:.2f}G", size_w)]
        ))
    return out
