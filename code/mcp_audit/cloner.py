"""批量浅克隆模块（M2a）：从 snapshots 提取 GitHub 仓库并批量 clone。

用法：
    python -m mcp_audit clone --plan            # 仅生成克隆计划（写入 clone_log）
    python -m mcp_audit clone --limit 20        # 试跑 20 个
    python -m mcp_audit clone                    # 全量（断点续传）
    python -m mcp_audit clone --refresh 100     # 已存在的也重拉最新 100 个（rug pull 观测）
    python -m mcp_audit clone --retry-failed    # 统一重试上一轮 failed 的（默认并发更高，见下）

    python -m mcp_audit clone --abandon-failed           # 放弃重试：failed -> abandoned（预演）
    python -m mcp_audit clone --abandon-failed --apply   # 真正写库（克隆轴收工）

    python -m mcp_audit clone --backfill-sha          # 回填历史 ok 仓库的 commit_sha（预演）
    python -m mcp_audit clone --backfill-sha --apply  # 真正写库

去重口径（关键）：
- 全源（official/glama/mcpso/cursor/smithery/modelscope）统一提取仓库；
- 计划按归一化货号 norm_url（github.com/owner/repo）去重：
  同一仓库不管出现在几个源、挂几个 name，只保留一份克隆（省磁盘省时间）；
- 每个 (name, source) 的归属留在 repo_index 映射表里，可回查"谁指向这个仓库"；
- 元数据（描述等）逐源逐条保留在 snapshots，不受克隆去重影响。

特性：
- 浅克隆（--depth 1）省磁盘省时间
- 并发克隆（信号量控制 git 进程数）
- 断点：clone_log.status 记录，重跑只处理未完成
- 已有磁盘文件按 disk_path 沿用，不重下
- token：从 .env 读 GITHUB_TOKEN（可选）
- 每个 repo 存 meta.txt（origin + 克隆时间），便于后续溯源
- 体积闸（双闸）：超 --max-size-mb 的仓库不做分析，
  克隆前用 GitHub API size 预筛（省下载）+ 克隆后实测兜底（防低估），
  超限则删除并登记 status='skipped_large'
"""

import argparse
import asyncio
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

import httpx

from . import config, db


def load_github_token() -> str | None:
    """从 .env 读 GITHUB_TOKEN（一行：GITHUB_TOKEN=xxx）。"""
    f = config.GITHUB_TOKEN_FILE
    if not f.exists():
        return None
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("GITHUB_TOKEN="):
            token = line.split("=", 1)[1].strip()
            return token or None
    return None


def safe_dir_name(name: str) -> str:
    """server name（可能含 / 和特殊字符）-> 安全目录名。"""
    return re.sub(r"[^A-Za-z0-9._-]+", "__", name)


def _repo_full_name(norm_url: str) -> str | None:
    """`github.com/owner/repo` -> `owner/repo`（供 GitHub API 用）。"""
    prefix = "github.com/"
    if not norm_url.startswith(prefix):
        return None
    full = norm_url[len(prefix):].strip("/")
    return full if full.count("/") == 1 else None


class SizeProbe:
    """克隆前体积预筛：查 GitHub API /repos/{owner}/{repo} 的 size（KB）省下载。

    仅在配置了 token 时启用（匿名 60 次/小时，不足以支撑批量预筛）；
    一旦触发 403/429 限流即自动停用，退回"先克隆再实测"的兜底路径，
    避免把整批克隆拖死。未知体积一律返回 None（不误杀）。
    """

    def __init__(self, token: str | None, client: httpx.AsyncClient, enabled: bool):
        self._token = token
        self._client = client
        self.enabled = enabled

    async def fetch(self, norm_url: str) -> int | None:
        """返回预估体积（字节）；未知/停用/出错返回 None。"""
        if not self.enabled:
            return None
        full = _repo_full_name(norm_url)
        if not full:
            return None
        headers = {"Accept": "application/vnd.github+json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        try:
            resp = await self._client.get(
                f"{config.GITHUB_API_BASE}/repos/{full}", headers=headers
            )
        except httpx.HTTPError:
            return None
        if resp.status_code == 200:
            kb = resp.json().get("size")
            return int(kb) * 1024 if kb is not None else None
        if resp.status_code in (403, 429):
            self.enabled = False  # 限流：停用预筛，避免整批雪崩
        return None


def build_clone_plan(conn: sqlite3.Connection) -> int:
    """全源提取 GitHub 仓库，按归一化货号写入 clone_log（跨源去重）。

    先刷新 repo_index 映射，再按 norm_url 去重插入：同一仓库无论出现在几个源、
    挂几个 name，只占一条计划。返回新增计划数。
    """
    db.rebuild_repo_index(conn)
    rows = conn.execute(
        """
        SELECT norm_url, MIN(repo_url) AS repo_url,
               MIN(name) AS name, MIN(source) AS source
        FROM repo_index
        WHERE norm_url IS NOT NULL
        GROUP BY norm_url
        """
    ).fetchall()
    added = 0
    for r in rows:
        try:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO clone_log
                    (norm_url, repo_url, name, source, status, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (r["norm_url"], r["repo_url"], r["name"], r["source"], db.now()),
            )
            added += cur.rowcount if cur.rowcount > 0 else 0
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return added


def _authed_url(repo_url: str, token: str | None) -> str:
    """把 token 注入 clone URL（仅 https github）。"""
    if token and repo_url.startswith("https://github.com/"):
        return repo_url.replace(
            "https://github.com/", f"https://x-access-token:{token}@github.com/", 1
        )
    return repo_url


def _clean_repo_url(repo_url: str) -> str:
    """把抓取到的网页链接还原成可克隆的仓库根 URL。

    上游抓到的常是浏览器地址而非仓库地址，例如：
      https://github.com/o/r#readme
      https://github.com/o/r?tab=readme-ov-file
      https://github.com/o/r/tree/main/src/x
    直接交给 git clone 会报 `not valid: is this a git repository?`，
    且该错误不在永久失败清单里，会导致该仓库每次重跑都被重试、永远进不了终态。
    """
    url = repo_url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    m = re.match(r"^(https?://github\.com/[^/]+/[^/]+)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    return url


def _resolve_git() -> str | None:
    """定位 git 可执行文件：优先 PATH，其次常见安装位置（含 GitHub Desktop 内置）。"""
    found = shutil.which("git")
    if found:
        return found
    for p in (
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files (x86)\Git\cmd\git.exe",
        r"C:\Users\Fan\AppData\Local\Programs\Git\cmd\git.exe",
    ):
        if Path(p).is_file():
            return p
    base = Path(r"C:\ProgramData\Fan\GitHubDesktop")
    if base.is_dir():
        for d in sorted(base.glob("app-*"), reverse=True):
            exe = d / "resources" / "app" / "git" / "cmd" / "git.exe"
            if exe.is_file():
                return str(exe)
    return None


# 进程内解析一次；PATH 缺失时用上面的回退路径，避免每条仓库都抛 FileNotFoundError。
_GIT = _resolve_git()


async def _git_clone_once(
    repo_url: str, dest: Path, token: str | None
) -> tuple[bool, str]:
    """执行一次 git clone，返回 (成功?, stderr摘要)。"""
    import os

    cmd = [
        _GIT or "git",
        "-c", "credential.helper=",   # 关键：清空凭据助手，GCM 无从调起
        "-c", "core.askPass=",        # 兜底：清空 askpass
        "-c", "core.longpaths=true",  # Windows MAX_PATH：放宽 260 字符路径限制
        "clone", "--depth", "1", "--quiet",
        _authed_url(repo_url, token), str(dest),
    ]
    # 双保险：环境变量层面也禁掉所有交互
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",   # 终端询问 -> 直接失败
        "GCM_INTERACTIVE": "never",   # Git Credential Manager 不弹 UI
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except FileNotFoundError:
        return False, f"找不到 git 可执行文件（已尝试 PATH 与内置路径，解析结果={_GIT!r}）"
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=config.CLONE_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        return False, f"超时（>{config.CLONE_TIMEOUT}s）"
    if proc.returncode != 0:
        return False, stderr.decode(errors="replace")[:500]
    return True, ""


# 永久性失败关键词：重试无意义（仓库被删/改名/私有，或链接根本不是仓库）
_PERMANENT_ERRS = (
    "repository not found", "does not exist", "not found",
    "is this a git repository",
)


def _is_complete_clone(dest: Path) -> bool:
    """判断 dest 是否为一次完整克隆（断点续传的安全判据）。

    中断（Ctrl-C / 断电）会留下半成品 .git：可能 HEAD 还解析不到提交；
    也可能对象已 fetch 完但 checkout 未结束（此时 index 尚未生成）。
    git 以「写临时文件 + rename」原子更新 index，因此 index 要么完整
    要么不存在。仅当 HEAD 能解析成合法 sha 且 index 为非空文件时才
    视为完成，避免把残骸误判为 ok 污染数据。
    """
    if not (dest / ".git").is_dir():
        return False
    if _head_sha(dest) is None:
        return False
    index = dest / ".git" / "index"
    return index.is_file() and index.stat().st_size > 0


def _force_rmtree(root: Path) -> None:
    """递归强制删除目录。

    Windows 上 git 对象文件带只读属性，shutil.rmtree(ignore_errors=True)
    会静默失败，导致残留目录永久占位、后续 clone 报
    "destination path already exists and is not an empty directory"。
    这里先清只读位，再用 onerror 兜底，确保删除真正生效。
    """
    import os
    import stat

    if not root.exists():
        return

    for dp, dirs, files in os.walk(root, topdown=False):
        for name in dirs + files:
            p = Path(dp) / name
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IEXEC)
            except OSError:
                pass

    def onerror(func, path, _exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except OSError:
            pass

    shutil.rmtree(root, onerror=onerror)


def _valid_sha(s: str) -> str | None:
    """校验是否为完整 40 位 hex 提交 sha。"""
    return s if re.fullmatch(r"[0-9a-f]{40}", s) else None


def _head_sha(dest: Path) -> str | None:
    """从 .git 目录直接读取 HEAD 指向的提交 sha（无需 git 命令）。

    浅克隆（--depth 1）下 HEAD 形如 `ref: refs/heads/<branch>`，该 ref 通常
    是松散文件（.git/refs/heads/<branch>）；若被 pack 进 packed-refs，则回退
    查表；detached HEAD 时 HEAD 本身即 sha。读不到一律返回 None，
    绝不影响克隆主流程。
    """
    git_dir = dest / ".git"
    if not git_dir.exists():
        return None
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None
    if not head:
        return None
    if not head.startswith("ref:"):
        return _valid_sha(head)  # detached HEAD
    ref = head[4:].strip()
    loose = git_dir / ref
    if loose.exists():
        try:
            sha = _valid_sha(loose.read_text(encoding="utf-8", errors="replace").strip())
            if sha:
                return sha
        except OSError:
            pass
    packed = git_dir / "packed-refs"
    if packed.exists():
        try:
            for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "^")):
                    continue
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1].strip() == ref:
                    return _valid_sha(parts[0].strip())
        except OSError:
            pass
    return None


def reclassify_large(
    conn: sqlite3.Connection, max_size_mb: float, apply: bool = False
) -> tuple[int, int]:
    """回溯把"已克隆且超体积上限"的仓库改判为 skipped_large 并登记、删盘。

    体积闸只对新克隆生效，此函数用于统一历史口径：把 status='ok' 且
    repo_size > 上限 的行改判为 skipped_large，登记 error 说明并释放磁盘。
    apply=False 为预演（只统计不动作）。返回 (命中数, 涉及字节数)。
    """
    cap = int(max_size_mb * 1024**2)
    rows = conn.execute(
        "SELECT norm_url, disk_path, repo_size FROM clone_log "
        "WHERE status='ok' AND repo_size > ? ORDER BY repo_size DESC", (cap,)
    ).fetchall()
    freed = 0
    now = db.now()
    for r in rows:
        size = r["repo_size"] or 0
        freed += size
        if not apply:
            continue
        if r["disk_path"]:
            dest = Path(db.rebase_disk_path(r["disk_path"]))
            if dest.exists():
                _force_rmtree(dest)
        conn.execute(
            "UPDATE clone_log SET status='skipped_large', disk_path=NULL, error=?, "
            "updated_at=? WHERE norm_url=?",
            (f"回溯改判：实测 {size / 1024**2:.1f} MB > 上限 {max_size_mb:g} MB（已删盘）",
             now, r["norm_url"]),
        )
    if apply:
        conn.commit()
    return len(rows), freed


def sample_skipped_large(
    conn: sqlite3.Connection, ratio: float = 0.1, seed: int = 42
) -> list[str]:
    """从 skipped_large 登记表中按比例随机抽样，供后续少量深挖分析。

    固定 seed 保证可复现：同一 seed + 同一库 -> 同一批仓库（研究可追溯）。
    返回按 norm_url 排序的样本（便于对照复现）。
    """
    import random

    urls = [
        r["norm_url"] for r in conn.execute(
            "SELECT norm_url FROM clone_log WHERE status='skipped_large' "
            "ORDER BY norm_url"
        )
    ]
    if not urls:
        return []
    k = max(1, round(len(urls) * ratio))
    k = min(k, len(urls))
    return sorted(random.Random(seed).sample(urls, k))


def backfill_commit_sha(
    conn: sqlite3.Connection, apply: bool = False
) -> tuple[int, int]:
    """回填历史 ok 仓库的 HEAD 提交 sha（从 .git 直读，无需 git 命令）。

    用途：纵向观测（rug pull / 演化）要求记录每个仓库被克隆时的确切快照，
    sha 是唯一且可外部核验的锚点。此前克隆未记录，这里对已落盘的 ok 仓库
    补录。apply=False 为预演（只统计不写库）。返回 (可读回填数, 读不到的数目)。
    """
    rows = conn.execute(
        "SELECT norm_url, disk_path FROM clone_log "
        "WHERE status='ok' AND (commit_sha IS NULL OR commit_sha='')"
    ).fetchall()
    filled = missing = 0
    ts = db.now()
    for r in rows:
        path = db.rebase_disk_path(r["disk_path"]) if r["disk_path"] else None
        sha = _head_sha(Path(path)) if path else None
        if sha is None:
            missing += 1
            continue
        filled += 1
        if apply:
            conn.execute(
                "UPDATE clone_log SET commit_sha=?, updated_at=? WHERE norm_url=?",
                (sha, ts, r["norm_url"]),
            )
    if apply:
        conn.commit()
    return filled, missing


def abandon_failed(
    conn: sqlite3.Connection, apply: bool = False
) -> tuple[int, dict[str, int]]:
    """把反复重试仍失败的 failed 统一改判为终态 abandoned（放弃重试、收工）。

    为什么需要这一步：failed 是非终态，普通 `clone`（不带 --retry-failed）的
    捞取条件是 status NOT IN ('ok','gone','not_retrieved','skipped_large')，
    failed 不在排除名单里，于是每跑一次全量都会把它们重新捞起来、连一遍
    GitHub、再失败一遍。当失败原因已属平台/环境硬限制（Windows 非法文件名、
    git-lfs 大文件、仓库失效）时，继续重试只消耗连接与时间，故改判终态。

    error 原文保留不动，作为"为何放弃"的证据（论文 §Threats to Validity 用）。
    apply=False 为预演（只统计不写库）。返回 (总数, {源: 数量})。
    """
    rows = conn.execute(
        "SELECT COALESCE(source, '(未归属)') AS source, COUNT(*) AS c "
        "FROM clone_log WHERE status='failed' GROUP BY source ORDER BY c DESC"
    ).fetchall()
    total = sum(r["c"] for r in rows)
    if apply and total:
        conn.execute(
            "UPDATE clone_log SET status='abandoned', updated_at=? "
            "WHERE status='failed'",
            (db.now(),),
        )
        conn.commit()
    return total, {r["source"]: r["c"] for r in rows}


async def clone_one(
    sem: asyncio.Semaphore, norm_url: str, repo_url: str, name: str | None,
    dest: Path, token: str | None, conn: sqlite3.Connection, refresh: bool,
    max_size_mb: float | None = None, probe: SizeProbe | None = None
) -> None:
    """克隆单个仓库：体积闸 + 克隆级重试 + 死链单独标记（gone 不再重试）。

    体积闸（双闸，超限登记 status='skipped_large' 并跳过分析）：
    - 闸1 预筛：克隆前查 GitHub API size，超限直接跳过（省下载）；
    - 闸2 兜底：克隆后实测目录体积，超限删除（防 API 低估）。
    """
    cap = int(max_size_mb * 1024**2) if max_size_mb else None
    repo_url = _clean_repo_url(repo_url)
    async with sem:
        status, err = "ok", None
        try:
            if dest.exists():
                if not refresh and _is_complete_clone(dest):
                    # 断点续传：确认是完整克隆才跳过（沿用已知磁盘路径，不重下）；
                    # 顺带补录 HEAD sha（COALESCE 保留已有值）
                    conn.execute(
                        "UPDATE clone_log SET status='ok', disk_path=?, "
                        "commit_sha=COALESCE(commit_sha, ?), updated_at=? "
                        "WHERE norm_url=?",
                        (str(dest), _head_sha(dest), db.now(), norm_url),
                    )
                    return
                # 上次中断残留的半成品（或 --refresh）：清掉重下
                _force_rmtree(dest)

            # 闸1：克隆前预筛（仅在有上限且启用时）
            if cap and probe is not None:
                known = await probe.fetch(norm_url)
                if known is not None and known > cap:
                    conn.execute(
                        "UPDATE clone_log SET status='skipped_large', repo_size=?, "
                        "error=?, attempts=attempts+1, updated_at=? WHERE norm_url=?",
                        (known, f"预筛：API size={known / 1024**2:.1f} MB "
                         f"> 上限 {max_size_mb:g} MB", db.now(), norm_url),
                    )
                    print(f"  [skip] {norm_url} {known / 1024**2:.1f} MB "
                          f"> {max_size_mb:g} MB（预筛跳过）")
                    return

            # 单次尝试：失败立即登记并把并发槽让出去，不在原地重试。
            # 原地重试（3 次 × 连接超时 ~21s + 退避 5/10s）会让一个仓库占住并发槽
            # 1 分多钟，遇到大面积网络故障时整批进度会长时间钉在同一个数字上。
            # failed 不是终态，下一轮批量重跑（或 --retry-failed）会统一重新尝试。
            if dest.exists():
                _force_rmtree(dest)
            ok, last_err = await _git_clone_once(repo_url, dest, token)
            if not ok:
                if any(k in last_err.lower() for k in _PERMANENT_ERRS):
                    status, err = "gone", last_err  # 仓库已删/私有/非仓库：重试无意义
                else:
                    status, err = "failed", last_err  # 网络类：留给下一轮统一重试

            if status == "ok":
                # 统计体积
                size = sum(
                    f.stat().st_size
                    for f in dest.rglob("*") if f.is_file()
                )
                # 闸2：实测兜底（API 估小 / 未预筛时）
                if cap and size > cap:
                    _force_rmtree(dest)
                    conn.execute(
                        "UPDATE clone_log SET status='skipped_large', disk_path=NULL, "
                        "repo_size=?, attempts=attempts+1, updated_at=?, error=? "
                        "WHERE norm_url=?",
                        (size, db.now(), f"实测：{size / 1024**2:.1f} MB "
                         f"> 上限 {max_size_mb:g} MB", norm_url),
                    )
                    print(f"  [skip] {norm_url} {size / 1024**2:.1f} MB "
                          f"> {max_size_mb:g} MB（实测删除）")
                    return
                # 记录溯源信息
                sha = _head_sha(dest)
                (dest / ".mcp_audit_meta.json").write_text(
                    json.dumps(
                        {"norm_url": norm_url, "name": name, "origin": repo_url,
                         "commit_sha": sha, "cloned_at": db.now()},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                conn.execute(
                    "UPDATE clone_log SET status='ok', disk_path=?, repo_size=?, "
                    "commit_sha=?, attempts=attempts+1, cloned_at=?, updated_at=?, "
                    "error=NULL WHERE norm_url=?",
                    (str(dest), size, sha, db.now(), db.now(), norm_url),
                )
                print(f"  [ok] {norm_url} ({size/1024:.0f} KB)")
                return
        except Exception as exc:  # noqa: BLE001
            status, err = "failed", f"{exc!r}"[:500]

        conn.execute(
            "UPDATE clone_log SET status=?, error=?, attempts=attempts+1, "
            "updated_at=? WHERE norm_url=?",
            (status, err, db.now(), norm_url),
        )
        print(f"  [{status}] {norm_url}: {err}")


_COMMIT_INTERVAL = 5.0  # 秒：批量克隆期间自动提交间隔（断点续跑用）


def print_clone_summary(conn: sqlite3.Connection) -> None:
    """克隆状态汇总：先按全局状态，再按「源 × 状态」矩阵展开。

    源取 clone_log.source（按货号去重后的归属源，同一仓库只归属一个源），
    故同一行的各状态计数之和 = 该源名下仓库总数，行与行之间互斥不重复。
    矩阵渲染复用 db.format_clone_by_source，与 `mcp_audit stats` 输出同源。
    """
    if not conn.execute("SELECT 1 FROM clone_log LIMIT 1").fetchone():
        print("汇总：clone_log 为空")
        return

    print("汇总（按状态）:")
    for row in conn.execute(
        "SELECT status, COUNT(*) AS c, SUM(COALESCE(repo_size,0)) AS s "
        "FROM clone_log GROUP BY status ORDER BY c DESC"
    ):
        print(f"  {row['status']}: {row['c']} 个（{(row['s'] or 0) / 1024**3:.2f} GB）")

    print("汇总（按源 × 状态）:")
    for line in db.format_clone_by_source(conn):
        print(line)


async def run_clones(
    limit: int | None, refresh_limit: int | None, concurrency: int | None,
    max_size_mb: float | None = None, source: str | None = None,
    retry_failed: bool = False
) -> None:
    conn = db.connect()
    db.init_db(conn)

    if _GIT is None:
        raise SystemExit(
            "未找到 git 可执行文件，无法克隆。请先安装 Git，或把 git 所在目录加入 PATH，例如：\n"
            r'  $env:Path += ";C:\ProgramData\Fan\GitHubDesktop\app-3.6.4\resources\app\git\cmd"'
        )
    print(f"git: {_GIT}")

    token = load_github_token()
    print(f"GITHUB_TOKEN: {'已加载' if token else '未配置（匿名模式，限速风险）'}")

    config.REPOS_DIR.mkdir(parents=True, exist_ok=True)
    # 重试批多为网络抖动类失败，默认用更高并发加速回收；显式 --concurrency 优先。
    eff_conc = concurrency or (
        config.CLONE_RETRY_CONCURRENCY if retry_failed else config.CLONE_CONCURRENCY
    )
    sem = asyncio.Semaphore(eff_conc)

    # 体积闸：有上限时才启用预筛；预筛需 token（匿名 60/h 不足以批量查）
    cap = max_size_mb if max_size_mb else None
    client: httpx.AsyncClient | None = None
    probe: SizeProbe | None = None
    if cap:
        client = httpx.AsyncClient(timeout=config.TIMEOUT)
        probe = SizeProbe(token, client, enabled=bool(token))
        print(f"体积上限: {cap:g} MB（克隆前预筛"
              f"{'已启用' if token else '未启用，匿名限额低，仅克隆后实测兜底'}）")

    # refresh 模式：重拉已成功的（观测 rug pull，最新代码）
    if refresh_limit:
        where = "status='ok'"
        params: list = [refresh_limit]
        if source:
            where += " AND source = ?"
            params.insert(0, source)
        rows = conn.execute(
            f"SELECT norm_url, repo_url, name, disk_path FROM clone_log "
            f"WHERE {where} ORDER BY RANDOM() LIMIT ?", params
        ).fetchall()
        mode = "refresh"
    else:
        if retry_failed:
            # 统一重试：只捞上一轮登记为 failed 的（网络故障等非终态）
            where = "status = 'failed'"
            mode = "retry-failed"
        else:
            where = ("status NOT IN ('ok', 'gone', 'abandoned', "
                     "'not_retrieved', 'skipped_large')")
            mode = "pending"
        params = []
        if source:
            where += " AND source = ?"
            params.append(source)
        rows = conn.execute(
            f"SELECT norm_url, repo_url, name, disk_path FROM clone_log "
            f"WHERE {where} ORDER BY norm_url" + (f" LIMIT {limit}" if limit else ""),
            params,
        ).fetchall()

    total = len(rows)
    print(f"克隆模式: {mode}（{'源=' + source if source else '全源'}），"
          f"本批 {total} 个仓库，并发 {eff_conc}")
    if source and not total:
        known = [r["source"] for r in conn.execute(
            "SELECT DISTINCT source FROM clone_log WHERE source IS NOT NULL ORDER BY 1"
        )]
        print(f"提示：源 {source!r} 已无待克隆仓库。可用源：{', '.join(known)}")

    # 进度计数：每条仓库完成（不论 ok/gone/failed/skip）都 +1，供心跳回显。
    prog = {"done": 0}
    started = time.monotonic()

    async def _track(coro) -> None:
        try:
            await coro
        finally:
            prog["done"] += 1

    tasks = []
    for r in rows:
        # 已有磁盘路径的沿用（不重下）；旧根目录的按当前 REPOS_DIR 重定位；
        # 新计划按货号命名目录
        if r["disk_path"]:
            dest = Path(db.rebase_disk_path(r["disk_path"]))
        else:
            dest = config.REPOS_DIR / safe_dir_name(r["norm_url"])
        tasks.append(
            _track(clone_one(sem, r["norm_url"], r["repo_url"], r["name"], dest,
                             token, conn, refresh=bool(refresh_limit),
                             max_size_mb=cap, probe=probe))
        )
    # 写库串行化：SQLite 单连接 + asyncio 单线程，无真并发问题。
    # 断点续跑：若只在整批结束时提交一次，进程被杀（Ctrl-C / 断电）会把整批
    # clone_log 更新一起回滚，磁盘上已克隆好的仓库在库里仍显示 pending，
    # 续跑虽能靠 _is_complete_clone 重新认领，但观测不到进度且要重扫整表。
    # 故加一个后台协程每 _COMMIT_INTERVAL 秒提交一次，把丢失窗口从「整批」
    # 收敛到「几秒」，同时仍摊薄了 IO。
    stop = asyncio.Event()

    async def _autocommit() -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=_COMMIT_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            try:
                conn.commit()
            except sqlite3.Error:
                pass
            # 心跳：即使有仓库卡在 git 超时/退避上，也保证每几秒有一行回显，
            # 便于判断"是在跑"还是"卡死"。flush 防止重定向到文件时被块缓冲吞掉。
            if total and prog["done"] < total:
                print(f"  ...进度 {prog['done']}/{total}"
                      f"（已用 {time.monotonic() - started:.0f}s）", flush=True)

    committer = asyncio.create_task(_autocommit())
    try:
        if tasks:
            await asyncio.gather(*tasks)
        stop.set()
        if not committer.done():
            committer.cancel()
        try:
            await committer
        except asyncio.CancelledError:
            pass

        conn.commit()

        # 汇总（按状态 + 按源）
        print_clone_summary(conn)
    finally:
        stop.set()
        if not committer.done():
            committer.cancel()
        try:
            conn.commit()  # 兜底：异常/中断也把已判定的结果落盘
        except sqlite3.Error:
            pass
        if client is not None:
            await client.aclose()
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp_audit clone", description="批量克隆仓库")
    parser.add_argument("--plan", action="store_true", help="只生成计划不克隆")
    parser.add_argument("--limit", type=int, default=None, help="限制本批数量")
    parser.add_argument("--refresh", type=int, default=None,
                        help="重拉 N 个已克隆的（rug pull 观测）")
    parser.add_argument("--concurrency", type=int, default=None,
                        help=f"并发数（默认：普通模式 {config.CLONE_CONCURRENCY}，"
                             f"--retry-failed 模式 {config.CLONE_RETRY_CONCURRENCY}）")
    parser.add_argument("--source", default=None,
                        help="只克隆该源名下的仓库（official/smithery/modelscope/mcpso/"
                             "glama/cursor/aibase；按货号去重后的归属源）")
    parser.add_argument("--retry-failed", action="store_true",
                        help="只重试上一轮登记为 failed 的仓库（统一重试；可与 --source 组合）")
    parser.add_argument("--max-size-mb", type=float, default=config.CLONE_MAX_SIZE_MB,
                        help="单仓库体积上限（MB），超过则跳过并登记 skipped_large；0 表示不限制")
    parser.add_argument("--reclassify-large", action="store_true",
                        help="回溯：把已克隆且超过上限的仓库改判为 skipped_large（默认预演，不删盘）")
    parser.add_argument("--abandon-failed", action="store_true",
                        help="把反复重试仍失败的 failed 统一改判为终态 abandoned"
                             "（放弃重试、收工；默认预演，加 --apply 才写库）")
    parser.add_argument("--apply", action="store_true",
                        help="配合 --reclassify-large / --abandon-failed：真正写库")
    parser.add_argument("--sample-large", action="store_true",
                        help="从 skipped_large 登记表中按比例随机抽样（配合 --ratio/--seed）")
    parser.add_argument("--backfill-sha", action="store_true",
                        help="回填历史 ok 仓库的 commit_sha（从 .git 直读，默认预演）")
    parser.add_argument("--ratio", type=float, default=0.1, help="抽样比例（默认 0.1=10%%）")
    parser.add_argument("--seed", type=int, default=42, help="抽样随机种子（固定=可复现）")
    args = parser.parse_args()

    if args.reclassify_large:
        conn = db.connect()
        db.init_db(conn)
        hit, freed = reclassify_large(conn, args.max_size_mb, apply=args.apply)
        verb = "已改判并删盘" if args.apply else "预演（未改动，加 --apply 才执行）"
        print(f"回溯体积上限 {args.max_size_mb:g} MB：命中 {hit} 个，"
              f"涉及 {freed / 1024**3:.2f} GB —— {verb}")
        if not args.apply and hit:
            print("提示：确认无误后执行 "
                  f"`python -m mcp_audit clone --reclassify-large --apply "
                  f"--max-size-mb {args.max_size_mb:g}`")
        conn.close()
        return

    if args.backfill_sha:
        conn = db.connect()
        db.init_db(conn)
        filled, missing = backfill_commit_sha(conn, apply=args.apply)
        verb = "已回填" if args.apply else "预演（未写库，加 --apply 才执行）"
        print(f"commit_sha 回填：可读 {filled} 个，读不到 {missing} 个 —— {verb}")
        conn.close()
        return

    if args.sample_large:
        conn = db.connect()
        db.init_db(conn)
        total = conn.execute(
            "SELECT COUNT(*) AS c FROM clone_log WHERE status='skipped_large'"
        ).fetchone()["c"]
        samples = sample_skipped_large(conn, args.ratio, args.seed)
        print(f"登记表 skipped_large 共 {total} 个，按 {args.ratio:.0%} 抽 {len(samples)} 个"
              f"（seed={args.seed}，可复现）：")
        for u in samples:
            print(f"  {u}")
        conn.close()
        return

    if args.abandon_failed:
        conn = db.connect()
        db.init_db(conn)
        total, by_source = abandon_failed(conn, apply=args.apply)
        verb = "已改判为 abandoned" if args.apply else "预演（未写库，加 --apply 才执行）"
        print(f"放弃重试：{total} 个 failed 仓库 —— {verb}")
        if by_source:
            print("  分布：" + "，".join(f"{s} {n}" for s, n in by_source.items()))
        if not args.apply and total:
            print("提示：确认无误后执行 "
                  "`python -m mcp_audit clone --abandon-failed --apply`")
        conn.close()
        return

    if args.plan:
        conn = db.connect()
        db.init_db(conn)
        added = build_clone_plan(conn)
        total = conn.execute("SELECT COUNT(*) AS c FROM clone_log").fetchone()["c"]
        mapped = conn.execute("SELECT COUNT(*) AS c FROM repo_index").fetchone()["c"]
        uniq = conn.execute(
            "SELECT COUNT(DISTINCT norm_url) AS c FROM repo_index WHERE norm_url IS NOT NULL"
        ).fetchone()["c"]
        cross = conn.execute(
            "SELECT COUNT(*) AS c FROM v_cross_source_repo"
        ).fetchone()["c"]
        print(f"仓库映射：{mapped} 条 (name,source) -> {uniq} 个唯一货号")
        print(f"跨源重叠：{cross} 个货号出现在 2 个及以上源")
        print(f"克隆计划：新增 {added}，总计 {total}")
        conn.close()
        return

    asyncio.run(run_clones(args.limit, args.refresh, args.concurrency,
                           args.max_size_mb, args.source, args.retry_failed))


if __name__ == "__main__":
    main()
