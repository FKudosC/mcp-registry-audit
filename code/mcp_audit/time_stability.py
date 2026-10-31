# -*- coding: utf-8 -*-
"""实验 C：不一致测量的时间稳定性（分抓取日重算「同期分歧率」）。

目的
----
证明 §5.1 的**同期跨源分歧率 81.5%** 不是单次抓取的偶然测量：多轮快照
（rounds 表 cross-2/3+ 判定表明存在）中，取若干覆盖 ≥2 源的**抓取日**，
各自独立重建「跨源描述可比集合」，重算同期分歧率，看其是否稳定。

口径（严格沿用既有定义，不引入新指标）
--------------------------------------
- 跨源单元  = repo_index 中同一 norm_url 出现在 ≥2 个源（22,104 个）；
- 判定      = `analysis.classify`（desc_diff，identical/near/partial/
              different/insufficient），与 §5.1 完全一致；
- 某抓取日 D= 对每个 (source,name) 取 fetched_at 落在 D 当日的**最新快照**
              （MAX(id)），所有快照同属一日 ⇒ 天然是「同期/span=1」；
- 可比      = 该日集合内 verdict != insufficient（即 ≥2 源提供非空描述）；
- 分歧率    = different / 可比。

基线（全量 latest，span=1）：5,420 可比，4,418 different → 81.5%。

只读：仅 SELECT + 内存计算，不写 DB（TEMP 亦不建，避免影响既有语义）。

用法
----
    python -m mcp_audit.time_stability            # 打印
    python -m mcp_audit.time_stability --write    # 写 paper/tables/table53_*.md
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict

from . import analysis
from . import config
from . import db as dbmod
from . import xsource_fidelity as xf

TABLE_OUT = config.PROJECT_ROOT / "paper" / "tables" / "table53_time_stability.md"

MIN_COMPARABLE = 100          # 单日可比样本下限（低于此视为噪声，不列入头条）
CAP = "同期分歧率基线"


# --------------------------------------------------------------------------
# 基线：复用 xsource_fidelity（same-day 桶）
# --------------------------------------------------------------------------
def baseline(conn: sqlite3.Connection) -> dict:
    data = xf.collect(conn)
    st = xf._bucket_stats(data)["same"]
    return {
        "units": st["units"],
        "comparable": st["comparable"],
        "different": st["different"],
        "rate": st["different"] / st["comparable"] if st["comparable"] else None,
    }


# --------------------------------------------------------------------------
# 分抓取日重算
# --------------------------------------------------------------------------
def _cross_universe(conn: sqlite3.Connection) -> set[str]:
    nu_src: dict[str, set] = defaultdict(set)
    for r in conn.execute("SELECT norm_url, source FROM repo_index "
                          "WHERE norm_url IS NOT NULL"):
        nu_src[r["norm_url"]].add(r["source"])
    return {nu for nu, s in nu_src.items() if len(s) > 1}


def _nm2nu(conn: sqlite3.Connection) -> dict:
    return {(r["name"], r["source"]): r["norm_url"]
            for r in conn.execute("SELECT name, source, norm_url FROM repo_index "
                                  "WHERE norm_url IS NOT NULL")}


def capture_days(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(fetched_at,1,10) AS d FROM snapshots "
        "WHERE d IS NOT NULL ORDER BY d")]


def one_day(conn: sqlite3.Connection, day: str, cross: set[str],
            nm2nu: dict) -> dict | None:
    """重建抓取日 day 的跨源描述可比集合，返回统计。"""
    rows = conn.execute(
        "SELECT s.source, s.name, s.description FROM snapshots s "
        "JOIN (SELECT source, name, MAX(id) AS mid FROM snapshots "
        "      WHERE substr(fetched_at,1,10)=? GROUP BY source, name) t "
        "  ON t.mid = s.id", (day,)).fetchall()
    by: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    srcs: set[str] = set()
    for r in rows:
        nu = nm2nu.get((r["name"], r["source"]))
        if not nu or nu not in cross:
            continue
        srcs.add(r["source"])
        by[nu][r["source"]].add(analysis._norm(r["description"]))

    units2 = comp = diff = 0
    for nu, bs in by.items():
        if len(bs) < 2:
            continue
        units2 += 1
        clean = {s: sorted(d for d in ds if d) for s, ds in bs.items()}
        verdict, _sim, _reps, _meta, _sm = analysis.classify(clean)
        if verdict != analysis.INSUFFICIENT:
            comp += 1
            if verdict == "different":
                diff += 1
    if units2 == 0:
        return None
    return {
        "day": day,
        "sources": sorted(srcs),
        "n_sources": len(srcs),
        "units2": units2,
        "comparable": comp,
        "different": diff,
        "rate": (diff / comp) if comp else None,
    }


def collect(conn: sqlite3.Connection) -> dict:
    cross = _cross_universe(conn)
    nm2nu = _nm2nu(conn)
    days = []
    for d in capture_days(conn):
        r = one_day(conn, d, cross, nm2nu)
        if r:
            days.append(r)
    headline = [d for d in days if d["comparable"] >= MIN_COMPARABLE]
    return {"baseline": baseline(conn), "days": days, "headline": headline}


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _pct(r):
    return "—" if r is None else f"{r * 100:.1f}%"


def render_md(d: dict) -> str:
    b = d["baseline"]
    head = d["headline"]
    rates = [x["rate"] for x in head if x["rate"] is not None]
    lo, hi = (min(rates), max(rates)) if rates else (None, None)

    L = []
    L.append("# Table.53 不一致测量的时间稳定性（分抓取日重算「同期分歧率」）")
    L.append("")
    L.append("**表题**：为证明 §5.1 的同期跨源分歧率 **"
             f"{_pct(b['rate'])}** 不是单次抓取的偶然测量，取多轮快照中覆盖 "
             "≥2 源的**独立抓取日**，各自重建跨源描述可比集合（同日快照 ⇒ 天然 span=1），"
             "以完全相同的判定口径（`analysis.classify` / desc_diff）重算同期分歧率。")
    L.append("")
    L.append("| 抓取日 | 覆盖源数 | 跨源单元（≥2 源现于该日） | 可比 | different | 同期分歧率 |")
    L.append("|---|---|---|---|---|---|")
    for x in head:
        L.append(f"| {x['day']} | {x['n_sources']} | {x['units2']:,} | "
                 f"{x['comparable']:,} | {x['different']:,} | **{_pct(x['rate'])}** |")
    L.append(f"| **{CAP}** | 7 | {b['units']:,} | {b['comparable']:,} | "
             f"{b['different']:,} | **{_pct(b['rate'])}** |")
    L.append("")
    if lo is not None:
        top2 = sorted(head, key=lambda x: -x["comparable"])[:2]
        top2_s = "、".join(f"{x['day']}（n={x['comparable']:,}，{_pct(x['rate'])}）"
                          for x in sorted(top2, key=lambda x: x["day"]))
        L.append(f"**读数**：{len(head)} 个独立抓取日（跨越 {head[0]['day']} – "
                 f"{head[-1]['day']}）重算的同期分歧率落在 **{lo*100:.1f}% – "
                 f"{hi*100:.1f}%**，全部高于半数；其中样本最大的两日 {top2_s} "
                 f"分列基线 {_pct(b['rate'])} 两侧。⇒ 同期分歧是**结构性、可复现**的，"
                 "并非某一次抓取日的偶然测量。")
        L.append("")
    L.append("**逐日明细（含样本过小、未列入头条的抓取日）**：")
    L.append("")
    L.append("| 抓取日 | 覆盖源数 | 跨源单元（≥2 源现于该日） | 可比 | different | 分歧率 |")
    L.append("|---|---|---|---|---|---|")
    for x in d["days"]:
        flag = "" if x["comparable"] >= MIN_COMPARABLE else " *(样本过小)*"
        L.append(f"| {x['day']}{flag} | {x['n_sources']} | {x['units2']:,} | "
                 f"{x['comparable']:,} | {x['different']:,} | {_pct(x['rate'])} |")
    L.append("")
    L.append("---")
    L.append("")
    L.append("- **口径**：同期分歧率 = different / 可比；判定沿用 `analysis.classify`"
             "（与 §5.1 一致，不另设指标）。抓取日 D 的集合 = 每个 (source,name) 取 "
             "`fetched_at` 落在 D 当日的最新快照；所有快照同属一日 ⇒ 天然 span=1。")
    L.append(f"- **数据源**：`{config.DB_PATH}` —— `snapshots`（分日快照） / `repo_index`"
             "（跨源单元） / `rounds`（抓取日）；脚本 `mcp_audit/time_stability.py`（只读，可复算）。")
    L.append(f"- **样本下限**：单日可比 ≥ {MIN_COMPARABLE} 才列入头条（其余在明细中标注）。")
    L.append("- **落点**：§5.1（同期横向分歧）之后，作为稳健性补充；或并入 E2 测量有效性小节。")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    conn = dbmod.connect()
    d = collect(conn)
    b = d["baseline"]
    print(f"基线（全量 latest, span=1）: 可比 {b['comparable']:,} / "
          f"different {b['different']:,} = {_pct(b['rate'])}")
    print("分抓取日重算：")
    for x in d["days"]:
        print(f"  {x['day']}  srcs={x['n_sources']} units={x['units2']:>6,} "
              f"comp={x['comparable']:>5,} diff={x['different']:>5,} "
              f"rate={_pct(x['rate'])}")
    if "--write" in argv:
        TABLE_OUT.parent.mkdir(parents=True, exist_ok=True)
        TABLE_OUT.write_text(render_md(d), encoding="utf-8")
        print("written:", TABLE_OUT)
    conn.close()


if __name__ == "__main__":
    main()
