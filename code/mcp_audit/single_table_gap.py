# -*- coding: utf-8 -*-
"""实验 A · 单表审计漏报的危险面命中数（危险面 × 跨源覆盖）。

背景（评审意见 §二 2.1 A，必做）：
    现有证据回答"分歧有多大 / 归因到谁 / 上界 / 换芯"，但未直接回答 COSE 审稿人
    最关心的 so-what——**若只查一个注册表，会漏掉多少带危险代码的 MCP 单元**。

做法（纯计算，只读，无需新采集）：
    1) 取 findings(finding_type='code_capability') 每一条 = 一个已扫描的
       norm_url 单元；其 data_json.danger_categories（9 类键名）非空即"危险面命中"，
       其 data_json.sources 即该单元被哪些注册表列出（repo_index 跨源覆盖）。
    2) 对每个注册表 R：覆盖数 = sources ∋ R 的危险面命中单元数；
       漏报数 = 危险面命中单元总数 − 覆盖数。
    3) 最佳单表 = 覆盖最多的 R；最差单表 = 覆盖最少的 R。
    4) 另统计"单一注册表独占"（仅 1 源可见）的规模与归属。

安全：只读 SQLite，不执行任何第三方代码，不改库。

用法：
    python -m mcp_audit.single_table_gap              # 打印统计
    python -m mcp_audit.single_table_gap --write      # 打印并写 paper/tables/table51_*.md
"""

import argparse
import json
import sqlite3
from collections import Counter

from . import config

FINDING_TYPE = "code_capability"
TABLE_OUT = config.PROJECT_ROOT / "paper" / "tables" / "table51_single_table_gap.md"

SOURCE_LABEL = {
    "official": "official（官方注册表）",
    "smithery": "smithery",
    "glama": "glama",
    "modelscope": "modelscope",
    "mcpso": "mcpso",
    "aibase": "aibase",
    "cursor": "cursor",
}


def pct(n: int, d: int) -> str:
    return f"{n / d * 100:.1f}%" if d else "—"


def collect(db_path=None) -> dict:
    conn = sqlite3.connect(str(db_path or config.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT name, data_json FROM findings WHERE finding_type = ?",
            (FINDING_TYPE,),
        ).fetchall()
        # 跨源对齐单元（≥2 源的去重 norm_url 数）
        cross_units = conn.execute(
            "SELECT COUNT(*) AS c FROM (SELECT norm_url FROM repo_index "
            "WHERE norm_url IS NOT NULL GROUP BY norm_url "
            "HAVING COUNT(DISTINCT source) > 1)"
        ).fetchone()["c"]
    finally:
        conn.close()

    scanned = len(rows)
    danger_units = 0
    pair_hits = 0
    cat_units = Counter()
    per_src = Counter()
    src_len = Counter()
    exclusive_owner = Counter()

    for r in rows:
        try:
            d = json.loads(r["data_json"])
        except (ValueError, TypeError):
            continue
        cats = d.get("danger_categories") or list((d.get("danger") or {}).keys())
        if not cats:
            continue
        danger_units += 1
        pair_hits += len(cats)
        for c in cats:
            cat_units[c] += 1
        srcs = [s for s in (d.get("sources") or []) if s]
        src_len[len(srcs)] += 1
        for s in srcs:
            per_src[s] += 1
        if len(srcs) == 1:
            exclusive_owner[srcs[0]] += 1

    sources = sorted(per_src)
    best = max(sources, key=lambda s: per_src[s])
    worst = min(sources, key=lambda s: per_src[s])

    return {
        "scanned": scanned,
        "danger_units": danger_units,
        "pair_hits": pair_hits,
        "cat_units": cat_units,
        "cross_units": cross_units,
        "per_src": per_src,
        "src_len": src_len,
        "exclusive_owner": exclusive_owner,
        "sources": sources,
        "best": best,
        "worst": worst,
    }


def render_md(st: dict) -> str:
    D = st["danger_units"]
    lines = []
    lines.append("# Table.51 单表审计漏报的危险面命中数")
    lines.append("")
    lines.append(
        "**表题**：把「跨源分歧」翻译成安全后果 —— 若只查单一注册表，会漏掉多少"
        "带危险代码的 MCP 单元。危险面命中 = 静态扫描（`code_capability`）中 "
        "`danger_categories` 非空的 `norm_url` 单元；覆盖 = `repo_index` 中列出该"
        "单元的注册表。最佳单表仍漏 %s，最差单表漏 %s。"
        % (pct(D - st["per_src"][st["best"]], D),
           pct(D - st["per_src"][st["worst"]], D))
    )
    lines.append("")

    lines.append("## A · 危险面命中基数")
    lines.append("")
    lines.append("| 口径 | 数值 |")
    lines.append("|---|---|")
    lines.append(f"| 代码侧扫描成功单元（`code_capability`） | {st['scanned']:,} |")
    lines.append(f"| 含 ≥1 类危险面标记的单元（危险面命中） | **{D:,}**（{pct(D, st['scanned'])}） |")
    lines.append(f"| 危险面 单元×类别 命中数 | {st['pair_hits']:,} |")
    exclusive = st["src_len"].get(1, 0)
    lines.append(f"| 其中：单一注册表独占（仅 1 源可见） | **{exclusive:,}**（{pct(exclusive, D)}） |")
    lines.append(f"| 跨源对齐单元（`repo_index` ≥2 源） | {st['cross_units']:,} |")
    lines.append("")

    lines.append("## B · 单表审计场景（7 注册表逐个视角，分母 = 危险面命中 %s）" % f"{D:,}")
    lines.append("")
    lines.append("| 单表（注册表） | 覆盖危险面命中单元 | 漏报单元 | 漏报率 |")
    lines.append("|---|---|---|---|")
    ordered = sorted(st["sources"], key=lambda s: -st["per_src"][s])
    for s in ordered:
        cov = st["per_src"][s]
        miss = D - cov
        tag = ""
        if s == st["best"]:
            tag = "（最佳单表）"
        elif s == st["worst"]:
            tag = "（最差单表）"
        label = SOURCE_LABEL.get(s, s) + tag
        covc = f"**{cov:,}**" if s == st["best"] else f"{cov:,}"
        lines.append(f"| {label} | {covc} | {miss:,} | {'**' + pct(miss, D) + '**' if s in (st['best'], st['worst']) else pct(miss, D)} |")
    lines.append("")

    lines.append("## C · 危险面命中单元的跨源可见度")
    lines.append("")
    lines.append("| 可见源数 k | 单元数 | 占比 | 含义 |")
    lines.append("|---|---|---|---|")
    k1 = st["src_len"].get(1, 0)
    k2 = st["src_len"].get(2, 0)
    k3p = D - k1 - k2
    lines.append(f"| 1（单一注册表独占） | **{k1:,}** | **{pct(k1, D)}** | 仅 1 源收录，其余 6 源全部漏报 |")
    lines.append(f"| 2 | {k2:,} | {pct(k2, D)} | 5 源漏报 |")
    lines.append(f"| ≥3 | {k3p:,} | {pct(k3p, D)} | 多源交叉可见 |")
    lines.append("")

    lines.append("## D · 单一注册表独占（k=1）的归属源")
    lines.append("")
    lines.append("| 独占源 | 独占的危险面命中单元 | 占 k=1 的比例 |")
    lines.append("|---|---|---|")
    for s, n in st["exclusive_owner"].most_common():
        lines.append(f"| {SOURCE_LABEL.get(s, s)} | {n:,} | {pct(n, k1)} |")
    lines.append("")

    lines.append(
        "- **口径**：危险面 = `code_capability.danger_categories`（9 类键名）非空；"
        "单元 = `norm_url`；覆盖源 = `repo_index` 中列出该 `norm_url` 的注册表集合。"
    )
    lines.append(
        f"- **读数**：最佳单表 `{st['best']}` 仍漏 {D - st['per_src'][st['best']]:,}"
        f"（{pct(D - st['per_src'][st['best']], D)}）；最差单表 `{st['worst']}` 漏 "
        f"{D - st['per_src'][st['worst']]:,}（{pct(D - st['per_src'][st['worst']], D)}）；"
        f"**{pct(k1, D)}** 的危险面命中单元仅被单一注册表收录（其余 6 源全漏）。"
    )
    lines.append(
        "- **数据源**：`D:\\Data\\mcp-audit\\mcp_audit.db` —— `findings(finding_type='code_capability')` "
        "＋ `repo_index`；脚本 `mcp_audit/single_table_gap.py`（只读，可复算）。"
    )
    lines.append(
        "- **说明**：单元覆盖只统计可解析出 GitHub 货号（进 `repo_index`）的条目；"
        "纯托管/无仓库条目不计。危险面为静态标记的「具备何种高风险能力」，不判善恶。"
    )
    lines.append("- **落点**：§5 新增小节「安全后果分析」或 §6 讨论。")
    lines.append("")
    return "\n".join(lines)


def show(st: dict) -> None:
    D = st["danger_units"]
    print(f"扫描成功单元        : {st['scanned']:,}")
    print(f"危险面命中单元      : {D:,}（{pct(D, st['scanned'])}）")
    print(f"单元×类别 命中      : {st['pair_hits']:,}")
    print(f"跨源对齐单元        : {st['cross_units']:,}")
    print("可见源数分布        : " + ", ".join(
        f"k={k}:{v:,}" for k, v in sorted(st["src_len"].items())))
    print("单表覆盖 / 漏报：")
    for s in sorted(st["sources"], key=lambda x: -st["per_src"][x]):
        cov = st["per_src"][s]
        print(f"  {s:<12} 覆盖 {cov:,}  漏报 {D - cov:,}  {pct(D - cov, D)}")
    print(f"最佳单表 = {st['best']}；最差单表 = {st['worst']}")


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="mcp_audit.single_table_gap",
        description="实验 A：单表审计漏报的危险面命中数（只读）")
    ap.add_argument("--write", action="store_true",
                    help="写出 paper/tables/table51_single_table_gap.md")
    args = ap.parse_args()

    st = collect()
    show(st)
    if args.write:
        TABLE_OUT.parent.mkdir(parents=True, exist_ok=True)
        TABLE_OUT.write_text(render_md(st), encoding="utf-8")
        print(f"\n已写 {TABLE_OUT}")


if __name__ == "__main__":
    main()
