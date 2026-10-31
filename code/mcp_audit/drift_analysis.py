"""C1 跨源纵向漂移 / 纵向 rug-pull（时间 × 跨源）。

研究问题
--------
一个 MCP 在多个源都被声明（跨源镜像）时，它的元数据会**随时间漂移**。
其中风险最高的一类是"链接换芯"（rug-pull）：同一 (source, name) 在第一个
时间点指向仓库 A、在最后一个时间点指向仓库 B —— 声明的身份没变，底下
的代码却已被替换。本模块把这种纵向漂移**限定在跨源单元内**度量，因为
跨源镜像正是供应链上"一处上游改动、多处下游受害"的关键节点。

口径
----
- 单元 = 跨源货号（同一 norm_url 被 ≥2 个源声明，即 v_cross_source_repo）。
- 每个单元由若干「成员」构成，一个成员 = 一条 (source, name) 映射。
- 成员的时间序列取自 snapshots：按 round 分组，**每轮取一个代表快照**
  （优先 is_latest=1，无则取该轮最大 id），按 round 起始时间排序，取
  **首/末时间点**对比。仅当成员有 ≥2 个时间点时才可评估。

  为什么必须"逐轮代表"而非"首行/末行"：官方注册表一次爬取会落库同一
  server 的**全部版本**（如 ai.auxen/auxen 一轮内即有 v1.0.0/v1.0.1/v1.0.2
  三条快照）。若直接拿"全局第一行 vs 全局最后一行"对比，就会把注册表自身
  的**版本历史**（同轮内 v1.0.0 与 v1.0.2 声明了不同仓库）误当成跨时间点
  漂移：实测这样得到 375 个"换芯"，其中 368 个为假阳；按"逐轮代表"仅 15 个。
  本模块采用后者 —— 它比较的是"各爬取时间点上观测到的当前状态"。

- 漂移信号（逐成员）：
    repo_swap       首/末时间点解析出的仓库货号不同（两者都非空）—— rug-pull；
    repo_swap_kind  repo_swap 的严重性分档：
                      cross_owner  换芯前后 owner（货号第二段）不同 —— 真·供应链转移；
                      same_owner   仅仓库名变化、owner 不变 —— 多为 GitHub 改名。
                    跨 owner 才是高风险 rug-pull 的签名。
    desc_changed    描述文本（折叠空白 + 小写后）不同；
    title_changed   标题不同；
    status_changed  状态不同（如 active -> inactive）；
    tools_changed   声明 tools 发生变化。
- 单元判定（优先级从上到下）：
    repo_swap     任一成员换芯；
    divergent     ≥2 个可评估成员中，仅部分发生漂移 —— 各源不同步（镜像陈旧分叉）；
    coordinated   ≥2 个可评估成员全部同步漂移 —— 上游统一更新；
    changed       仅 1 个可评估成员且发生漂移；
    stable        可评估但无任何漂移；
    no_time_series 无任何成员具备 ≥2 个时间点（无法评估）。

边界
----
clone_log 对每个 norm_url 只保留一份克隆（无代码时间序列），故 C1 只能
测元数据/描述层漂移；代码层纵向漂移留待 C3。

写库：finding_type='cross_source_drift'，name=norm_url，version=verdict。
预演默认（只统计不写），--apply 才写。

用法：
    python -m mcp_audit drift                 # 预演全量
    python -m mcp_audit drift --limit 2000    # 抽样试跑
    python -m mcp_audit drift --examples 8    # 样例条数
    python -m mcp_audit drift --apply         # 写库
"""

import json
import re
import sqlite3
from collections import defaultdict

from . import db

FINDING_TYPE = "cross_source_drift"

# 判定输出顺序（show 打印用）
VERDICT_ORDER = ("repo_swap", "divergent", "coordinated", "changed",
                 "stable", "no_time_series")


def _norm(text: str | None) -> str:
    """文本归一化：折叠空白 + 转小写，用于变更判定。"""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def _loads(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _repo_id(source: str, market_json, code_json, raw_json) -> str | None:
    """从快照的 market/code/raw 解析出该条目的归一化仓库货号。"""
    url = db.repo_url_for_row(
        source, _loads(market_json), _loads(code_json), _loads(raw_json))
    return db.norm_repo_url(url)


def _owner(repo_id: str | None) -> str | None:
    """取货号的 owner（路径第二段，小写）；用于换芯的严重性分档。"""
    if not repo_id:
        return None
    parts = repo_id.split("/")
    return parts[1].lower() if len(parts) == 3 else None


def _reps(rows: list) -> list:
    """把某成员的快照按 round 分组，取每轮代表快照，按轮起始时间排序。

    同一轮内可能有多版本快照：优先 is_latest=1，无则取该轮最大 id。
    """
    groups: dict[int, dict] = {}
    for r in rows:
        rid = r["round_id"]
        if rid not in groups:
            groups[rid] = {"started_at": r["started_at"] or "", "rows": []}
        groups[rid]["rows"].append(r)
    order = sorted(groups, key=lambda rid: (groups[rid]["started_at"], rid))
    out = []
    for rid in order:
        grp = groups[rid]["rows"]
        latest = [x for x in grp if x["is_latest"]]
        out.append(latest[-1] if latest else grp[-1])
    return out


def collect(conn: sqlite3.Connection) -> dict:
    """构建跨源货号 -> 漂移判定结果。"""
    cross: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT norm_url, GROUP_CONCAT(DISTINCT source) AS sources FROM repo_index "
        "WHERE norm_url IS NOT NULL GROUP BY norm_url HAVING COUNT(DISTINCT source) > 1"
    ):
        cross[r["norm_url"]] = sorted(r["sources"].split(","))
    if not cross:
        return {}

    members: dict[str, list[tuple]] = defaultdict(list)
    pairs: list[tuple] = []
    for r in conn.execute(
        "SELECT norm_url, source, name FROM repo_index WHERE norm_url IS NOT NULL"
    ):
        nu = r["norm_url"]
        if nu in cross:
            members[nu].append((r["source"], r["name"]))
            pairs.append((r["source"], r["name"]))
    if not pairs:
        return {}

    conn.execute(
        "CREATE TEMP TABLE IF NOT EXISTS _c1_members "
        "(source TEXT, name TEXT, PRIMARY KEY (source, name))")
    conn.execute("DELETE FROM _c1_members")
    conn.executemany(
        "INSERT OR IGNORE INTO _c1_members (source, name) VALUES (?, ?)", pairs)

    rows = conn.execute(
        """
        SELECT sn.source, sn.name, sn.round_id, sn.version, sn.title, sn.description,
               sn.status, sn.is_latest, sn.tools_json, sn.market_json, sn.code_json,
               sn.raw_json, rd.started_at
        FROM snapshots sn
        JOIN _c1_members m ON m.source = sn.source AND m.name = sn.name
        JOIN rounds rd ON rd.id = sn.round_id
        ORDER BY sn.source, sn.name, rd.started_at, rd.id, sn.id
        """
    ).fetchall()

    series: dict[tuple, list] = defaultdict(list)
    for r in rows:
        series[(r["source"], r["name"])].append(r)

    out: dict[str, dict] = {}
    for nu, mlist in members.items():
        mem_out = []
        for source, name in sorted(mlist):
            reps = _reps(series.get((source, name), []))
            entry = {"source": source, "name": name, "timepoints": len(reps)}
            if len(reps) < 2:
                entry.update({"repo_swap": False, "repo_swap_kind": None,
                              "desc_changed": False, "title_changed": False,
                              "status_changed": False, "tools_changed": False})
            else:
                a, b = reps[0], reps[-1]
                ra = _repo_id(source, a["market_json"], a["code_json"], a["raw_json"])
                rb = _repo_id(source, b["market_json"], b["code_json"], b["raw_json"])
                swap = bool(ra and rb and ra != rb)
                if swap:
                    swap_kind = ("cross_owner"
                                 if _owner(ra) != _owner(rb) else "same_owner")
                else:
                    swap_kind = None
                ta, tb = _norm(a["tools_json"]), _norm(b["tools_json"])
                entry.update({
                    "repo_before": ra, "repo_after": rb,
                    "repo_swap": swap,
                    "repo_swap_kind": swap_kind,
                    "desc_changed": _norm(a["description"]) != _norm(b["description"]),
                    "title_changed": _norm(a["title"]) != _norm(b["title"]),
                    "status_changed": (a["status"] or "") != (b["status"] or ""),
                    "tools_changed": (ta != tb
                                      and (ta not in ("", "[]") or tb not in ("", "[]"))),
                    "from": {"round_id": a["round_id"],
                             "started_at": a["started_at"], "version": a["version"]},
                    "to": {"round_id": b["round_id"],
                           "started_at": b["started_at"], "version": b["version"]},
                })
            mem_out.append(entry)

        eligible = [m for m in mem_out if m["timepoints"] >= 2]
        changed = [m for m in eligible
                   if (m["desc_changed"] or m["title_changed"]
                       or m["status_changed"] or m["tools_changed"])]
        if any(m["repo_swap"] for m in mem_out):
            verdict = "repo_swap"
        elif not eligible:
            verdict = "no_time_series"
        elif len(eligible) >= 2 and 0 < len(changed) < len(eligible):
            verdict = "divergent"
        elif len(eligible) >= 2 and changed and len(changed) == len(eligible):
            verdict = "coordinated"
        elif changed:
            verdict = "changed"
        else:
            verdict = "stable"

        n_cross = sum(1 for m in mem_out if m.get("repo_swap_kind") == "cross_owner")
        n_same = sum(1 for m in mem_out if m.get("repo_swap_kind") == "same_owner")
        if n_cross:
            swap_kind = "cross_owner"
        elif n_same:
            swap_kind = "same_owner"
        else:
            swap_kind = None

        out[nu] = {
            "norm_url": nu,
            "verdict": verdict,
            "sources": cross[nu],
            "source_count": len(cross[nu]),
            "member_count": len(mem_out),
            "assessable_count": len(eligible),
            "changed_count": len(changed),
            "repo_swap_count": sum(1 for m in mem_out if m.get("repo_swap")),
            "repo_swap_cross_owner_count": n_cross,
            "repo_swap_same_owner_count": n_same,
            "swap_kind": swap_kind,
            "members": mem_out,
        }
    return out


def run(conn: sqlite3.Connection, apply: bool = False, examples: int = 5,
        limit: int | None = None) -> dict:
    """逐货号判定。apply=False 只统计不写库；apply=True 幂等重建 findings。"""
    data = collect(conn)
    counts: dict[str, int] = defaultdict(int)
    member_stats: dict[str, int] = defaultdict(int)
    unit_swap_stats: dict[str, int] = defaultdict(int)
    repo_swap_ex: list[dict] = []
    divergent_ex: list[dict] = []

    ts = db.now()
    if apply:
        conn.execute("DELETE FROM findings WHERE finding_type = ?", (FINDING_TYPE,))

    items = list(data.items())
    if limit:
        items = items[:limit]

    for nu, payload in items:
        verdict = payload["verdict"]
        counts[verdict] += 1
        member_stats["members"] += payload["member_count"]
        member_stats["assessable"] += payload["assessable_count"]
        if payload["swap_kind"]:
            unit_swap_stats[payload["swap_kind"]] += 1
        for m in payload["members"]:
            if m["timepoints"] < 2:
                continue
            if m.get("repo_swap"):
                member_stats["repo_swap"] += 1
            if m.get("repo_swap_kind") == "cross_owner":
                member_stats["repo_swap_cross_owner"] += 1
            elif m.get("repo_swap_kind") == "same_owner":
                member_stats["repo_swap_same_owner"] += 1
            if m.get("desc_changed"):
                member_stats["desc_changed"] += 1
            if m.get("title_changed"):
                member_stats["title_changed"] += 1
            if m.get("status_changed"):
                member_stats["status_changed"] += 1
            if m.get("tools_changed"):
                member_stats["tools_changed"] += 1
        if apply:
            conn.execute(
                "INSERT INTO findings (source, name, version, finding_type, data_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("+".join(payload["sources"]), nu, verdict, FINDING_TYPE,
                 json.dumps(payload, ensure_ascii=False), ts),
            )
        if payload["verdict"] == "repo_swap" and len(repo_swap_ex) < examples:
            repo_swap_ex.append(payload)
        if payload["verdict"] == "divergent" and len(divergent_ex) < examples:
            divergent_ex.append(payload)

    if apply:
        conn.commit()

    return {
        "total": len(data),
        "counts": dict(counts),
        "member_stats": dict(member_stats),
        "unit_swap_stats": dict(unit_swap_stats),
        "repo_swap_examples": repo_swap_ex,
        "divergent_examples": divergent_ex,
        "applied": apply,
    }


def show(result: dict, examples: int) -> None:
    counts = result["counts"]
    ms = result["member_stats"]
    us = result["unit_swap_stats"]
    total = result["total"]
    print()
    print(f"=== C1 跨源纵向漂移 / rug-pull（{'写库' if result['applied'] else '预演'}） ===")
    print(f"跨源货号（单元）        : {total}")
    print(f"成员条目（source, name）: {ms.get('members', 0)}"
          f"（可评估 {ms.get('assessable', 0)}）")
    print()
    print("单元判定分布:")
    for v in VERDICT_ORDER:
        print(f"  {v:<14} {counts.get(v, 0)}")
    print()
    print("单元级换芯严重性（repo_swap 单元）:")
    print(f"  跨 owner 转移   {us.get('cross_owner', 0)}")
    print(f"  同 owner 改名   {us.get('same_owner', 0)}")
    print()
    print("成员级漂移信号（跨源单元内，仅可评估成员）:")
    for key, label in (("repo_swap", "换芯 repo_swap"),
                       ("repo_swap_cross_owner", "  └ 跨 owner 转移"),
                       ("repo_swap_same_owner", "  └ 同 owner 改名"),
                       ("desc_changed", "描述变化"),
                       ("title_changed", "标题变化"),
                       ("status_changed", "状态变化"),
                       ("tools_changed", "声明 tools 变化")):
        print(f"  {label:<18} {ms.get(key, 0)}")

    if not examples:
        return
    if result["repo_swap_examples"]:
        print("\n===== 换芯样例（rug-pull：首末时间点货号不同，跨 owner 优先） =====")
        for p in result["repo_swap_examples"][:examples]:
            print(f"\n● {p['norm_url']}  源 {p['source_count']}  成员 {p['member_count']}")
            swaps = [m for m in p["members"] if m.get("repo_swap")]
            swaps.sort(key=lambda m: 0 if m.get("repo_swap_kind") == "cross_owner" else 1)
            for m in swaps:
                print(f"    [{m['source']:<10}] {m['name']}  ({m.get('repo_swap_kind')})")
                print(f"        {m['repo_before']}  ->  {m['repo_after']}")
    if result["divergent_examples"]:
        print("\n===== 源间不同步样例（divergent：仅部分源漂移） =====")
        for p in result["divergent_examples"][:examples]:
            print(f"\n● {p['norm_url']}  源 {p['source_count']}  可评估 {p['assessable_count']}"
                  f"  变化 {p['changed_count']}")
            for m in p["members"]:
                if m["timepoints"] < 2:
                    continue
                flags = ",".join(k for k in
                                 ("repo_swap", "desc_changed", "title_changed",
                                  "status_changed", "tools_changed") if m.get(k))
                print(f"    [{m['source']:<10}] {m['name']:<28} {flags or '-'}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp_audit drift",
        description="C1 跨源纵向漂移 / rug-pull（写入 findings，"
                    "finding_type='cross_source_drift'）")
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
