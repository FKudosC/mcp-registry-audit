"""E4 · C1 定向重解析：声明冻结版本下的实现层"换芯"验证。

研究问题
--------
C1「同版本静默换芯」的可计算口径（`findings.cross_source_drift`）只看**声明的**
代码指针（`code_json` / `remotes_json`）是否跨轮变化；而 `clone_log` 的 PK =
`norm_url`（单行、原地更新、**无克隆历史**）⇒ 已克隆仓库的**实现层**是否在同
版本下被悄悄替换，在既有数据里**不可算**，只能作案例佐证（Table.32 已注明）。

本模块补上这一层。对「声明冻结」仓库单元：

    版本确定（非 latest/unknown）且跨 ≥2 轮 version 不变、
    `code_json` 与 `remotes_json` 声明的指针冻结

取 `clone_log` 在 **T2 克隆时**捕获的 `commit_sha`（= 当时默认分支 HEAD），
在 **T3** 用 `git ls-remote <url> HEAD` 重新解析当前 HEAD：

    sha 不变                -> unchanged（声明与实现一致）
    sha 变化（fast-forward）-> changed ⇒ 声明版本冻结而代码前移 = 实现层"换芯"

为什么这正是 C1：各源的 code 指针是**未固定 ref 的 git URL**（如官方
`{"type":"git","url":"https://github.com/o/r"}`），其"所指"就是默认分支 HEAD；
**声明元数据冻结 ≠ 消费者拿到的代码冻结**——注册表声称的 version 与真实可取
代码之间没有不可变绑定。缓存/CI/npx(github:) 等未 pin 的消费路径直接受此影响。

口径（本模块验收线）
------------------
- 单元 = 声明冻结仓库单元（实测 64,316 单元行 -> **14,797 个 norm_url**，全为
  `github.com`）。
- T2 sha = `clone_log.commit_sha`（86% 非空，来自 `.mcp_audit_meta` / `.git` 直读）。
- T3 sha = `git ls-remote <authed_url> HEAD`（**只读网络**：不 clone、不写磁盘）。
- diff 摘要：仅对 top-N（默认 50）changed 案例调 GitHub compare API
  （`/repos/{o}/{r}/compare/{old}...{new}`），取 `ahead_by / files / commits`；
  其余 changed 仅记 sha 变化（元数据层），以规避 5,000/hr 限流。
- 排名：`source_count` 降序 -> `repo_size` 降序 -> `norm_url`（曝光面越大越值得配 diff）。
- 探测结果缓存至 `DATA_DIR/c1_reclone_probe.json`：预演 / 落库共用同一批 sha，
  避免两次分钟级网络探测；`--refresh` 强制重探。

写库：`finding_type='c1_reclone'`，`name=norm_url`，`version=声明的 version`，
`source='+'.join(sorted(sources))`。**预演默认（只统计不写），`--apply` 才写**；
写库前 `DELETE ... WHERE finding_type=?` 保持幂等（与 xsource_fidelity 同款）。

用法：
    python -m mcp_audit c1-reclone                    # 预演全量（含网络探测）
    python -m mcp_audit c1-reclone --limit 200        # 只探前 200 个单元（冒烟）
    python -m mcp_audit c1-reclone --diff-topn 80     # 取 diff 摘要的案例数
    python -m mcp_audit c1-reclone --apply            # 写库
    python -m mcp_audit c1-reclone --apply --refresh  # 忽略缓存重探后写库
"""

import json
import os
import sqlite3
import subprocess
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import cloner, config, db

FINDING_TYPE = "c1_reclone"

# 判定（按"信号强弱"排序）
VERDICT_ORDER = ("changed", "unchanged", "unresolved", "error")

# 版本值哨兵：非确定性版本不构成"声明冻结"
_VERSION_SENTINELS = ("latest", "unknown")

# 探测缓存：预演与落库共用同一批 sha（分钟级网络探测，重跑代价高）
_CACHE_PATH = config.DATA_DIR / "c1_reclone_probe.json"


# ------------------------------------------------------------------ 总体 --
# 「声明冻结」单元：版本确定且跨 ≥2 轮不变、code/remotes 声明的指针均冻结。
_UNIT_SQL = """
WITH frozen AS (
  SELECT s.source, s.name, s.version
  FROM snapshots s
  WHERE s.version IS NOT NULL AND s.version <> ''
    AND s.version NOT IN ('latest', 'unknown')
  GROUP BY s.source, s.name, s.version
  HAVING COUNT(DISTINCT s.round_id) >= 2
     AND COUNT(DISTINCT COALESCE(s.code_json, '')) = 1
     AND COUNT(DISTINCT COALESCE(s.remotes_json, '')) = 1
)
SELECT ri.norm_url                      AS norm_url,
       GROUP_CONCAT(DISTINCT f.source)  AS sources,
       GROUP_CONCAT(DISTINCT f.version) AS versions,
       MIN(cl.repo_url)                 AS repo_url,
       MIN(cl.commit_sha)               AS commit_sha,
       MIN(cl.cloned_at)                AS cloned_at,
       MIN(cl.repo_size)                AS repo_size
FROM frozen f
JOIN repo_index ri ON ri.source = f.source AND ri.name = f.name
JOIN clone_log cl ON cl.norm_url = ri.norm_url
WHERE cl.status = 'ok' AND cl.commit_sha IS NOT NULL AND cl.commit_sha <> ''
GROUP BY ri.norm_url
ORDER BY ri.norm_url
"""


def collect(conn: sqlite3.Connection) -> list[dict]:
    """返回声明冻结仓库单元（每 norm_url 一条，含 T2 commit_sha）。"""
    out: list[dict] = []
    for r in conn.execute(_UNIT_SQL):
        versions = sorted({v for v in (r["versions"] or "").split(",") if v})
        sources = sorted({s for s in (r["sources"] or "").split(",") if s})
        out.append({
            "norm_url": r["norm_url"],
            "sources": sources,
            "source_count": len(sources),
            "versions": versions,
            "declared_version": versions[0] if versions else "",
            "repo_url": r["repo_url"],
            "stored_sha": r["commit_sha"],
            "repo_size": r["repo_size"],
            "cloned_at": r["cloned_at"],
            "host": r["norm_url"].split("/")[0],
        })
    return out


# ------------------------------------------------------------ 网络探测 --
def _ls_remote_head(repo_url: str, token: str | None,
                    timeout: int) -> tuple[str | None, str | None]:
    """`git ls-remote <authed_url> HEAD` -> (sha, error)。只读，不落盘。"""
    if not cloner._GIT:
        return None, "git-not-found"
    cmd = [
        cloner._GIT,
        "-c", "credential.helper=",
        "-c", "core.askPass=",
        "-c", "core.longpaths=true",
        "ls-remote", cloner._authed_url(repo_url, token), "HEAD",
    ]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        return None, f"timeout(>{timeout}s)"
    except FileNotFoundError:
        return None, "git-not-found"
    if proc.returncode != 0:
        return None, (proc.stderr or "").strip()[:160] or f"exit={proc.returncode}"
    first = proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else ""
    sha = first.split()[0] if first else None
    return (sha, None) if sha else (None, "empty")


# 瞬时网络抖动（并发过高 / GitHub 连接重置）：值得重试，避免误判为"解析失败"
_TRANSIENT_MARKERS = (
    "Recv failure", "Connection was reset", "curl 28", "curl 56", "curl 92",
    "Failed to connect", "Connection timed out", "Operation timed out",
    "Empty reply from server", "Could not resolve host", "GnuTLS", "SSL",
)


def _ls_remote_head_retry(repo_url: str, token: str | None, timeout: int,
                          retries: int) -> tuple[str | None, str | None]:
    """对瞬时网络错误重试 `retries` 次（线性退避）；确定性失败（404/无权限）不重试。"""
    sha, err = _ls_remote_head(repo_url, token, timeout)
    attempts = 0
    while not sha and err and attempts < retries:
        if not any(m in err for m in _TRANSIENT_MARKERS):
            break
        attempts += 1
        time.sleep(0.6 * attempts)
        sha, err = _ls_remote_head(repo_url, token, timeout)
    return sha, err


def _load_cache() -> dict:
    if not _CACHE_PATH.exists():
        return {}
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def _save_cache(cache: dict) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


def probe(units: list[dict], token: str | None, workers: int, timeout: int,
          refresh: bool, retries: int = 2) -> dict[str, dict]:
    """批量重解析 HEAD（带磁盘缓存）。返回 {norm_url: {head_sha, error, probed_at}}。"""
    cache = _load_cache()
    todo = [u for u in units
            if refresh or u["norm_url"] not in cache]
    fresh: dict[str, dict] = {}
    if todo:
        print(f"[probe] 重解析 {len(todo)} 个单元（并发 {workers}）..."
              + ("" if not refresh else " --refresh"))
        pairs: list[tuple[dict, tuple]] = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_ls_remote_head_retry, u["repo_url"], token,
                              timeout, retries): u for u in todo}
            for fut in as_completed(futs):
                pairs.append((futs[fut], fut.result()))
                if len(pairs) % 500 == 0 or len(pairs) == len(todo):
                    print(f"  ... 已重解析 {len(pairs)}/{len(todo)}", flush=True)
        ts = db.now()
        for u, (sha, err) in pairs:
            entry = {"head_sha": sha, "error": err, "probed_at": ts}
            fresh[u["norm_url"]] = entry
            if not sha and err and any(m in err for m in _TRANSIENT_MARKERS):
                continue  # 瞬时失败不入缓存，下次重探（避免一次抖动被永久固化）
            cache[u["norm_url"]] = entry
        _save_cache(cache)
    return {u["norm_url"]: fresh.get(u["norm_url"]) or cache.get(u["norm_url"], {})
            for u in units}


def _compare(repo_url: str, old: str, new: str, token: str | None) -> dict | None:
    """GitHub compare API -> diff 摘要（files/commits/ahead_by）。非 github 返回 None。"""
    if not repo_url:
        return None
    slug = repo_url.split("://", 1)[-1].strip("/")
    if not slug.startswith("github.com/"):
        return None
    owner_repo = "/".join(slug.split("/")[1:3])
    if owner_repo.count("/") != 1:
        return None
    url = f"{config.GITHUB_API_BASE}/repos/{owner_repo}/compare/{old}...{new}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": config.USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=45) as resp:
            d = json.loads(resp.read().decode())
    except Exception as e:  # noqa: BLE001 - 网络/限流一律降级为"无摘要"
        return {"status": "err", "error": f"{type(e).__name__}: {str(e)[:80]}"}
    files = d.get("files") or []
    commits = d.get("commits") or []
    return {
        "status": d.get("status"),
        "ahead_by": d.get("ahead_by"),
        "behind_by": d.get("behind_by"),
        "total_commits": d.get("total_commits"),
        "files_changed": len(files),
        "files": [
            {"filename": f.get("filename"), "status": f.get("status"),
             "additions": f.get("additions"), "deletions": f.get("deletions")}
            for f in files[:20]
        ],
        "commits": [
            (c.get("commit", {}).get("message", "") or "").splitlines()[0][:120]
            for c in commits[-5:]
        ],
    }


# ---------------------------------------------------------------- 运行 --
def run(conn: sqlite3.Connection, apply: bool = False, limit: int | None = None,
        workers: int = 8, diff_topn: int = 50, timeout: int = 60,
        refresh: bool = False, examples: int = 8, retries: int = 2) -> dict:
    """逐单元重解析 HEAD 并比对 T2 sha。apply=False 只统计不写库。"""
    units = collect(conn)
    pool_total = len(units)
    if limit:
        units = units[:limit]

    token = cloner.load_github_token()
    probes = probe(units, token, workers, timeout, refresh, retries)

    rows: list[dict] = []
    for u in units:
        pr = probes.get(u["norm_url"]) or {}
        head = pr.get("head_sha")
        err = pr.get("error")
        stored = (u["stored_sha"] or "").strip().lower()
        if not head and err:
            verdict = "error"
        elif not head:
            verdict = "unresolved"
        elif head.lower() == stored:
            verdict = "unchanged"
        else:
            verdict = "changed"
        rows.append({**u, "head_sha": head, "error": err, "verdict": verdict,
                     "probed_at": pr.get("probed_at")})

    counts = Counter(r["verdict"] for r in rows)
    changed = [r for r in rows if r["verdict"] == "changed"]
    changed_sorted = sorted(
        changed,
        key=lambda r: (-r["source_count"], -(r["repo_size"] or 0), r["norm_url"]))

    top = changed_sorted[:diff_topn] if diff_topn else []
    if top:
        print(f"[diff] 取 {len(top)} 个 changed 案例的 compare 摘要...")
        with ThreadPoolExecutor(max_workers=min(8, len(top))) as ex:
            diffs = list(ex.map(
                lambda r: _compare(r["repo_url"], r["stored_sha"], r["head_sha"],
                                   token), top))
        for r, d in zip(top, diffs):
            r["diff"] = d

    # 聚合：解析成功（changed+unchanged）为分母，changed 为分子
    resolved = counts["changed"] + counts["unchanged"]
    change_rate = (counts["changed"] / resolved) if resolved else None

    ahead_vals = [r["diff"]["ahead_by"] for r in top
                  if isinstance(r.get("diff"), dict)
                  and isinstance(r["diff"].get("ahead_by"), int)]
    files_vals = [r["diff"]["files_changed"] for r in top
                  if isinstance(r.get("diff"), dict)
                  and isinstance(r["diff"].get("files_changed"), int)]

    ts = db.now()
    written = 0
    if apply:
        conn.execute("DELETE FROM findings WHERE finding_type = ?", (FINDING_TYPE,))
        for r in rows:
            payload = {
                "norm_url": r["norm_url"],
                "sources": r["sources"],
                "declared_version": r["declared_version"],
                "versions": r["versions"],
                "stored_sha": r["stored_sha"],
                "head_sha": r["head_sha"],
                "changed": r["verdict"] == "changed",
                "verdict": r["verdict"],
                "repo_size": r["repo_size"],
                "cloned_at": r["cloned_at"],
                "probed_at": r["probed_at"],
                "error": r["error"],
            }
            if isinstance(r.get("diff"), dict):
                payload["diff"] = r["diff"]
            conn.execute(
                "INSERT INTO findings (source, name, version, finding_type, "
                "data_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                ("+".join(r["sources"]), r["norm_url"], r["declared_version"],
                 FINDING_TYPE, json.dumps(payload, ensure_ascii=False), ts),
            )
            written += 1
        conn.commit()

    return {
        "pool_total": pool_total,
        "scanned": len(units),
        "counts": dict(counts),
        "resolved": resolved,
        "change_rate": change_rate,
        "top": top,
        "examples": changed_sorted[:examples],
        "example_names": [],
        "ahead_median": (sorted(ahead_vals)[len(ahead_vals) // 2]
                         if ahead_vals else None),
        "files_median": (sorted(files_vals)[len(files_vals) // 2]
                         if files_vals else None),
        "written": written,
        "applied": apply,
        "diff_topn": diff_topn,
    }


# ---------------------------------------------------------------- 展示 --
def _pct(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "—"


def show(result: dict, examples: int) -> None:
    counts = result["counts"]
    print()
    print(f"=== E4 C1 定向重解析（声明冻结版本下的实现层换芯，"
          f"{'写库' if result['applied'] else '预演'}） ===")
    print(f"声明冻结单元总体          : {result['pool_total']}")
    print(f"本次重解析                : {result['scanned']}")
    hist = " / ".join(f"{v} {counts.get(v, 0)}" for v in VERDICT_ORDER)
    print(f"判定分布                  : {hist}")
    print(f"解析成功（chg+unchg）分母 : {result['resolved']}")
    print(f"changed（实现层换芯）     : {counts.get('changed', 0)}"
          f"（{_pct(result['change_rate'])}）")

    if result["top"]:
        print()
        print(f"top-{len(result['top'])} changed 案例 diff 摘要"
              f"（ahead 中位 {result['ahead_median']}，"
              f"files 中位 {result['files_median']}）:")
        for r in result["top"][:examples]:
            d = r.get("diff") or {}
            ahead = d.get("ahead_by")
            files = d.get("files_changed")
            extra = (f"ahead={ahead} files={files} status={d.get('status')}"
                     if ahead is not None else f"diff={d.get('status') or '—'}")
            print(f"  v{r['declared_version']:<10} {r['stored_sha'][:10]}->"
                  f"{r['head_sha'][:10] if r['head_sha'] else '—'}  {extra}")
            print(f"      {r['norm_url']}  源×{r['source_count']}")

    if examples and result["examples"] and not result["top"]:
        print()
        print("changed 样例:")
        for r in result["examples"]:
            print(f"  v{r['declared_version']:<10} {r['stored_sha'][:10]}->"
                  f"{r['head_sha'][:10] if r['head_sha'] else '—'}  {r['norm_url']}")

    if result["applied"]:
        print()
        print(f"写库行数                  : {result['written']}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp_audit c1-reclone",
        description="E4 C1 定向重解析：声明冻结版本下重解析 HEAD 比对 T2 sha，"
                    "写库 finding_type='c1_reclone'")
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制处理的单元数（冒烟试跑用）")
    parser.add_argument("--workers", type=int, default=8,
                        help="并发 git ls-remote 进程数（默认 8；过高易触发连接重置）")
    parser.add_argument("--diff-topn", type=int, default=50,
                        help="取 compare diff 摘要的 changed 案例数（默认 50；0 不取）")
    parser.add_argument("--timeout", type=int, default=60,
                        help="单次 ls-remote 超时秒数（默认 60）")
    parser.add_argument("--retries", type=int, default=2,
                        help="瞬时网络错误重试次数（默认 2）")
    parser.add_argument("--refresh", action="store_true",
                        help="忽略探测缓存、强制重探")
    parser.add_argument("--examples", type=int, default=8,
                        help="打印样例数（默认 8，0 不打印）")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    result = run(conn, apply=args.apply, limit=args.limit, workers=args.workers,
                 diff_topn=args.diff_topn, timeout=args.timeout,
                 refresh=args.refresh, examples=args.examples,
                 retries=args.retries)
    show(result, args.examples)
    conn.close()


if __name__ == "__main__":
    main()
