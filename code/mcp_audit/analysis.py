"""M3 分析层：跨源描述对比。

把"同一个 MCP"（按 norm_url 归一化货号对齐）在各源的描述拉出来比对，
判定一致性档位并写入 findings 表（finding_type='desc_diff'），供后续查询/统计。

口径与边界（P2 定稿）：
- 分母 = 「结构可比」集合：同一个 norm_url（有 GitHub 仓库）被 ≥2 个源声明。
  这是跨源研究的全集，不再按描述可得性收缩分母。
- 在分母内按"有非空描述的源数"分流：
    · ≥2 个源有描述  -> 真做描述一致性比较，落 identical / near / partial / different；
    · <2 个源有描述  -> 落 insufficient（显式记账，不再静默丢弃），
                         并区分 0 源有描述 / 1 源有描述两档。
- 纯托管/无仓库条目天然不参与（无法按货号对齐）。
- 元数据逐源逐条取自 servers（每源最新状态），不合并、不改写。
- 归一化：仅折叠空白 + 转小写，保留原始文本用于证据。
- 每条的 source_count / desc_source_count 一并入库，便于复算漏斗。
- 长度混淆对照（E1 冻结）：相似度用 SequenceMatcher，其 ratio = 2M/(len1+len2)
  受**较短串**长度限制——短描述即便内容一致也达不到 NEAR，会被推向 different。
  故同时记录各源代表描述长度 len_min/len_max 与理论上限 sim_ceiling，并按 len_min
  分带给出判决分布，用于证明（或披露）判决不是"描述长度"造成的假象。
"""

import json
import re
import sqlite3
from collections import defaultdict
from difflib import SequenceMatcher

from . import config, db, sim_backends

FINDING_TYPE = "desc_diff"
# 相似度分档（跨源描述最高两两相似度）
NEAR = 0.9
PARTIAL = 0.5
# 有非空描述的源不足 2 个 -> 无法做跨源比较，显式记账（不参与分档）
INSUFFICIENT = "insufficient"
# 长度混淆对照分带（按最短代表描述的字符数），左闭右开
LEN_BANDS = (("<50", 0, 50), ("50-149", 50, 150),
             ("150-399", 150, 400), ("≥400", 400, 10 ** 9))


def _norm(text: str | None) -> str:
    """描述归一化：折叠空白 + 转小写，用于一致性判定。"""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip().lower()


def collect(conn: sqlite3.Connection) -> dict:
    """收集「结构可比」集合：norm_url -> {source: [归一化描述, ...]}。

    分母口径 = 出现在 ≥2 个源的货号（即 v_cross_source_repo 同一集合）。
    每个源一条 key：有非空描述则为去重排序后的描述列表，无描述则为空列表
    （空列表即"该源不提供描述"，如 glama 身份收割源）。
    """
    rows = conn.execute(
        """
        SELECT r.norm_url, r.source, sv.description
        FROM repo_index r
        LEFT JOIN servers sv ON sv.source = r.source AND sv.name = r.name
        WHERE r.norm_url IS NOT NULL
        """
    ).fetchall()

    per: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for r in rows:
        per[r["norm_url"]][r["source"]].add(_norm(r["description"]))

    out: dict[str, dict[str, list[str]]] = {}
    for norm_url, by_source in per.items():
        if len(by_source) < 2:
            continue
        out[norm_url] = {
            source: sorted(d for d in descs if d)
            for source, descs in by_source.items()
        }
    return out


def _len_meta(reps: dict[str, str]) -> dict:
    """长度混淆对照元数据：代表描述长度 + 相似度长度上限。

    SequenceMatcher.ratio = 2M/(len1+len2)，其值被**较短串**限制：一侧描述很短时
    即便内容完全一致也达不到 NEAR。故记录：
      desc_lens   —— 各源代表描述的字符数；
      len_min/max —— 最短/最长代表描述长度；
      sim_ceiling —— 该长度对下相似度理论上限 2*len_min/(len_min+len_max)。
    best 远低于 ceiling = 判决与长度无关；best≈ceiling = 判决可能只是"描述太短"。
    """
    if not reps:
        return {"desc_lens": {}, "len_min": None, "len_max": None,
                "sim_ceiling": None}
    lens = [len(d) for d in reps.values()]
    len_min, len_max = min(lens), max(lens)
    ceiling = (2 * len_min / (len_min + len_max)) if (len_min + len_max) else 0.0
    return {
        "desc_lens": {s: len(d) for s, d in reps.items()},
        "len_min": len_min,
        "len_max": len_max,
        "sim_ceiling": round(ceiling, 3),
    }


def classify(by_source: dict[str, list[str]],
             backend: str = "seq") -> tuple[str, float | None, dict, dict, dict]:
    """判定档位，给出跨源最高两两相似度，以及长度混淆对照元数据。

    先按"有非空描述的源"过滤：不足 2 个 -> insufficient（similarity=None）。
    每源取一个代表描述（该源内去重排序后的第一个）参与相似度计算；
    identical 以"参与比较的描述并集只有一个元素"为准。

    backend（E1 消融，见 sim_backends）：
      - seq   字符级 SequenceMatcher（基线，保持既有结果可复现）；
      - token 语言感知词集 Dice（纯标准库，跨语言经中英词典归一）；
      - embed 多语言句向量余弦（需 sentence-transformers）。
    额外返回 sim_meta：参与比较的代表描述语言 + 是否跨语言对。
    """
    desc_sources = {s: descs for s, descs in by_source.items() if descs}
    reps = {source: descs[0] for source, descs in desc_sources.items()}
    meta = _len_meta(reps)
    if len(desc_sources) < 2:
        return INSUFFICIENT, None, reps, meta, {"cross_lang": False, "langs": {}}

    union = {desc for descs in desc_sources.values() for desc in descs}
    langs = {s: sim_backends.detect_lang(d) for s, d in reps.items()}
    cross_lang = len(set(langs.values())) >= 2
    sim_meta = {"cross_lang": cross_lang, "langs": langs}
    if len(union) == 1:
        return "identical", 1.0, reps, meta, sim_meta

    best = sim_backends.best_pairwise(list(reps.values()), backend)
    verdict = sim_backends.verdict_of(best)
    return verdict, round(best, 3), reps, meta, sim_meta


def run(conn: sqlite3.Connection, backend: str = "seq") -> tuple[dict[str, int], int]:
    """执行对比并重建 findings（幂等：先清同类型旧记录再写）。

    backend 见 classify；默认 'seq' 保持既有 desc_diff 结果可复现。
    消融时用不同 backend 重跑，payload 内记录 backend 与 sim_meta。
    """
    ok, why = sim_backends.backend_available(backend)
    if not ok:
        raise RuntimeError(f"backend '{backend}' 不可用：{why}")
    data = collect(conn)
    conn.execute("DELETE FROM findings WHERE finding_type = ?", (FINDING_TYPE,))
    ts = db.now()
    counts: dict[str, int] = defaultdict(int)
    for norm_url, by_source in data.items():
        verdict, similarity, reps, meta, sim_meta = classify(by_source, backend)
        struct_sources = sorted(by_source)
        desc_sources = sorted(reps)
        counts[verdict] += 1
        payload = {
            "norm_url": norm_url,
            "verdict": verdict,
            "similarity": similarity,
            "backend": backend,
            "source_count": len(struct_sources),
            "desc_source_count": len(desc_sources),
            "sources": struct_sources,
            "desc_sources": desc_sources,
            "descriptions": {s: by_source[s] for s in desc_sources},
            **meta,
            **sim_meta,
        }
        conn.execute(
            "INSERT INTO findings (source, name, version, finding_type, data_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("+".join(struct_sources), norm_url, verdict, FINDING_TYPE,
             json.dumps(payload, ensure_ascii=False), ts),
        )
    conn.commit()
    return counts, len(data)


def show(conn: sqlite3.Connection, examples: int) -> None:
    """打印漏斗摘要 + 差异样例。"""
    rows = conn.execute(
        "SELECT version, data_json FROM findings WHERE finding_type = ?", (FINDING_TYPE,)
    ).fetchall()
    items = [json.loads(r["data_json"]) for r in rows]
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["version"]] += 1

    total = len(items)
    insuff = counts.get(INSUFFICIENT, 0)
    comparable = total - insuff
    zero_desc = sum(1 for it in items
                    if it["verdict"] == INSUFFICIENT and it["desc_source_count"] == 0)
    one_desc = insuff - zero_desc

    print(f"结构可比货号（≥2 源指向同一货号）{total} 个"
          f"  → findings(finding_type='{FINDING_TYPE}')")
    print(f"  ├ 描述可比（≥2 源有描述）      {comparable}")
    print(f"  └ insufficient（有描述的源<2） {insuff}")
    print(f"       ├ 0 源有描述             {zero_desc}")
    print(f"       └ 1 源有描述             {one_desc}")
    print()
    verd = ("identical", "near", "partial", "different")
    for v in verd:
        print(f"     {v:<11} {counts.get(v, 0)}")

    comp = [it for it in items if it["verdict"] != INSUFFICIENT]
    print()
    print("长度混淆对照（分母 = 描述可比，按最短代表描述长度分带）：")
    print(f"     {'带':<10}" + "".join(f"{v:>11}" for v in verd) + f"{'合计':>9}")
    for label, lo, hi in LEN_BANDS:
        sub = [it for it in comp
               if it.get("len_min") is not None and lo <= it["len_min"] < hi]
        cells = "".join(f"{sum(1 for x in sub if x['verdict'] == v):>11}" for v in verd)
        print(f"     {label:<10}{cells}{len(sub):>9}")
    for v in verd:
        lens = sorted(x["len_min"] for x in comp
                      if x["verdict"] == v and x.get("len_min") is not None)
        if lens:
            print(f"     {v:<11} 最短描述中位数 {lens[len(lens) // 2]:>5} 字符")
    capped = [x for x in comp
              if x.get("similarity") is not None and x.get("sim_ceiling")
              and x["similarity"] >= x["sim_ceiling"] - 0.02]
    print(f"     长度受限（best 达该长度理论上限 98% 以上）{len(capped)} 个")

    if not examples:
        return
    print(f"\n===== 差异样例（verdict=different，相似度升序） =====")
    diff = sorted((it for it in items if it["verdict"] == "different"),
                  key=lambda x: x["similarity"])[:examples]
    for it in diff:
        print(f"\n● {it['norm_url']}  最高相似 {it['similarity']}"
              f"  源 {it['desc_source_count']}/{it['source_count']} 有描述")
        for source in it["desc_sources"]:
            desc = it["descriptions"][source][0]
            print(f"    [{source:<10}] {desc[:110]}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="mcp_audit compare-desc",
                                     description="跨源描述层对比（写入 findings）")
    parser.add_argument("--examples", type=int, default=5,
                        help="打印的差异样例数（默认 5，0 表示不打印）")
    parser.add_argument("--backend", choices=sim_backends.BACKENDS, default="seq",
                        help="相似度后端：seq（基线）/ token（标准库）/"
                             "embed（多语言句向量），用于 E1 消融")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    run(conn, backend=args.backend)
    show(conn, args.examples)
    conn.close()


# ------------------------------------------------- 实体歧义（同名不同仓） --
#
# 与上面按 norm_url 对齐互为对照：这里专门刻画「name 相同、货号不同」的
# 情形，用来量化「用 name 当对齐键」的风险，作为论文的效度（threat to
# validity）控制项。注意本层只打标签、不做任何合并。

ENTITY_TYPE = "entity_ambiguity"
# 「多 MCP 聚合仓」阈值：同一仓库被 ≥HUB_MIN 个不同 MCP 名共用，说明
# name↔repo 是多对一，该映射不可靠（聚合仓 / 误抓），归为 noise。
HUB_MIN = 5


def _owner(norm_url: str) -> str:
    parts = norm_url.split("/")
    return parts[1] if len(parts) > 1 else ""


def collect_ambiguity(conn: sqlite3.Connection) -> list[dict]:
    """找出「同名不同仓」的 MCP：name -> >1 个 distinct norm_url。

    每条返回：各映射的源 / 货号 / owner，以及该仓被多少个不同 MCP 名共用
    （用于判定聚合仓）。标签口径（deterministic，无外部 API）：
      - same_owner : 所有映射同 owner —— 同厂商改造/改名，疑似同一实体；
      - noise      : 至少一个映射命中「多 MCP 聚合仓」(≥HUB_MIN 名共用)，
                     属 name↔repo 多对一的不可靠对齐；
      - name_only  : 其余（不同 owner、各自是独立仓）—— 真实的同名撞车。
    """
    rows = conn.execute(
        "SELECT name, source, norm_url FROM repo_index WHERE norm_url IS NOT NULL"
    ).fetchall()

    names_per_repo: dict[str, set] = defaultdict(set)
    by_name: dict[str, dict] = defaultdict(dict)
    for r in rows:
        names_per_repo[r["norm_url"]].add(r["name"])
        by_name[r["name"]][r["source"]] = r["norm_url"]

    out: list[dict] = []
    for name, by_source in by_name.items():
        repos = sorted(set(by_source.values()))
        if len(repos) < 2:
            continue
        owners = sorted({_owner(u) for u in repos})
        if len(owners) == 1:
            label = "same_owner"
        elif any(len(names_per_repo[u]) >= HUB_MIN for u in repos):
            label = "noise"
        else:
            label = "name_only"
        out.append({
            "name": name,
            "label": label,
            "owners": owners,
            "n_repos": len(repos),
            "n_sources": len(by_source),
            "mappings": [
                {"source": s, "norm_url": u, "repo_shared_by": len(names_per_repo[u])}
                for s, u in sorted(by_source.items())
            ],
        })
    return out


def run_ambiguity(conn: sqlite3.Connection) -> tuple[dict[str, int], int]:
    """重建实体歧义标签（幂等：先清同类型旧记录再写）。"""
    items = collect_ambiguity(conn)
    conn.execute("DELETE FROM findings WHERE finding_type = ?", (ENTITY_TYPE,))
    ts = db.now()
    counts: dict[str, int] = defaultdict(int)
    for it in items:
        counts[it["label"]] += 1
        sources = sorted({m["source"] for m in it["mappings"]})
        conn.execute(
            "INSERT INTO findings (source, name, version, finding_type, data_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("+".join(sources), it["name"], it["label"], ENTITY_TYPE,
             json.dumps(it, ensure_ascii=False), ts),
        )
    conn.commit()
    return counts, len(items)


def show_ambiguity(conn: sqlite3.Connection, examples: int) -> None:
    """打印实体歧义摘要 + 分标签样例。"""
    rows = conn.execute(
        "SELECT data_json FROM findings WHERE finding_type = ?", (ENTITY_TYPE,)
    ).fetchall()
    items = [json.loads(r["data_json"]) for r in rows]
    counts: dict[str, int] = defaultdict(int)
    for it in items:
        counts[it["label"]] += 1

    print(f"同名不同仓（实体歧义）{len(items)} 例，已写入 findings"
          f"（finding_type='{ENTITY_TYPE}'）")
    for lab in ("same_owner", "name_only", "noise"):
        print(f"  {lab:<11} {counts.get(lab, 0)}")

    if not examples:
        return
    print("\n===== 样例 =====")
    for lab in ("noise", "same_owner", "name_only"):
        subset = [it for it in items if it["label"] == lab][:examples]
        if not subset:
            continue
        print(f"\n--- {lab} ---")
        for it in subset:
            print(f"● {it['name']}   owners={it['owners']}")
            for m in it["mappings"]:
                print(f"    [{m['source']:<9}] {m['norm_url']}  "
                      f"(repo_shared_by={m['repo_shared_by']})")


def ambiguity_main() -> None:
    import argparse

    parser = argparse.ArgumentParser(prog="mcp_audit entity-ambiguity",
                                     description="同名不同仓实体歧义打标（写入 findings）")
    parser.add_argument("--examples", type=int, default=5,
                        help="每个标签打印的样例数（默认 5，0 表示不打印）")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    run_ambiguity(conn)
    show_ambiguity(conn, args.examples)
    conn.close()


if __name__ == "__main__":
    main()
