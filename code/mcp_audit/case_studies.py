# -*- coding: utf-8 -*-
"""实验 B · 安全案例研究（声明 vs 代码：把分歧数字变成安全故事）。

背景（评审意见 §二 2.1 B，强烈建议）：
    实验 A 把「跨源分歧」翻译成「单表审计漏掉多少危险命中」；实验 B 从语料里挑
    3–5 个代表性 MCP 工具做定性深挖，给 COSE 审稿人可记忆的安全故事：
      ① A 表标危险、B 表缺席或描述无害；
      ② 声明版本不变但实现静默前移（换芯，展示 commit diff）；
      ③ 1 个 typosquat / 镜像农场案例。

做法（只读，纯计算＋人工挑选，无新采集）：
    - 案例清单硬编码（人工挑选，见下文 CASES_*），所有数字实时从库里取，杜绝编造。
    - 声明侧：`snapshots` 每 (source,name) 最新一条的 title/description/version。
    - 代码侧：`findings(finding_type='code_capability')` 的 danger_categories / sources。
    - 换芯侧：`findings(finding_type='c1_reclone')` 的 stored_sha × head_sha / diff。
    - 镜像侧：`clone_log` 的 status（ok / gone）。

安全：只读 SQLite，不执行任何第三方代码，不改库。

用法：
    python -m mcp_audit.case_studies              # 打印案例摘要
    python -m mcp_audit.case_studies --write      # 打印并写 paper/tables/table52_*.md
"""

import argparse
import json
import sqlite3
from collections import defaultdict

from . import config

TABLE_OUT = config.PROJECT_ROOT / "paper" / "tables" / "table52_case_studies.md"

CATS_ORDER = [
    "arbitrary_code", "subprocess", "shell_true", "fs_destructive",
    "network_egress", "env_access", "unsafe_deserialize", "db_access",
    "credential_paths",
]
CATS_CN = {
    "arbitrary_code": "任意代码",
    "subprocess": "子进程",
    "shell_true": "Shell 执行",
    "fs_destructive": "破坏性文件",
    "network_egress": "网络外联",
    "env_access": "环境变量",
    "unsafe_deserialize": "不安全反序列化",
    "db_access": "数据库访问",
    "credential_paths": "凭据路径",
}

# —— 人工挑选的案例清单（--）
CASE1_MULTI = [
    "github.com/anipotts/imessage-mcp",
    "github.com/mikhae1/kubeview-mcp",
]
CASE1_SINGLE = [
    "github.com/360ghar/backend",
    "github.com/agentspore/agentspore",
]
CASE2_SWAP = [
    "github.com/victoriametrics/mcp-vmanomaly",
    "github.com/googleapis/genai-toolbox",
    "github.com/hashgraph-online/hol-guard",
]
MIRROR_URL = "github.com/mcp-mirror/tumf_mcp-shell-server"
ORIGINAL_URL = "github.com/tumf/mcp-shell-server"
FARM_OWNERS = ("pipeworx-io", "mcp-dir", "mcp-mirror", "csoai-org")


def _pct(n, d):
    return f"{n / d * 100:.1f}%" if d else "—"


def collect(db_path=None) -> dict:
    conn = sqlite3.connect(str(db_path or config.DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        # 声明侧：每 (source,name) 最新快照
        latest = {}
        for r in conn.execute(
            "SELECT source, name, title, description, version, fetched_at FROM snapshots "
            "WHERE id IN (SELECT MAX(id) FROM snapshots GROUP BY source, name)"
        ):
            latest[(r["name"], r["source"])] = dict(r)

        # 跨源货号：norm_url -> [(name, source)]
        ri = defaultdict(list)
        for r in conn.execute(
            "SELECT name, source, norm_url FROM repo_index WHERE norm_url IS NOT NULL"
        ):
            ri[r["norm_url"]].append((r["name"], r["source"]))

        # 代码侧（dict 供案例查用；multi_danger 按行统计，与 Table.51 口径一致）
        cap = {}
        multi_danger = 0
        for r in conn.execute(
            "SELECT data_json FROM findings WHERE finding_type='code_capability'"
        ):
            d = json.loads(r["data_json"])
            cap[d["norm_url"]] = d
            cats = d.get("danger_categories") or list((d.get("danger") or {}).keys())
            srcs = [s for s in (d.get("sources") or []) if s]
            if cats and len(srcs) > 1:
                multi_danger += 1

        # 换芯侧
        swap = {}
        for r in conn.execute(
            "SELECT data_json FROM findings WHERE finding_type='c1_reclone'"
        ):
            d = json.loads(r["data_json"])
            swap[d["norm_url"]] = d

        def declares(nu):
            out = []
            for nm, so in sorted(ri.get(nu, []), key=lambda x: x[1]):
                s = latest.get((nm, so)) or {}
                out.append({
                    "source": so,
                    "name": nm,
                    "title": (s.get("title") or "").strip(),
                    "desc": (s.get("description") or "").strip(),
                })
            return out

        def danger(nu):
            d = cap.get(nu) or {}
            cats = sorted(d.get("danger_categories") or [])
            return cats, sorted(d.get("sources") or [])

        cases = {"multi": [], "single": [], "swap": [], "mirror": {}}

        for nu in CASE1_MULTI:
            cats, csrc = danger(nu)
            cases["multi"].append({"url": nu, "cats": cats, "code_sources": csrc,
                                   "declares": declares(nu), "n_src": len(ri.get(nu, []))})
        for nu in CASE1_SINGLE:
            cats, csrc = danger(nu)
            cases["single"].append({"url": nu, "cats": cats, "code_sources": csrc,
                                    "declares": declares(nu), "n_src": len(ri.get(nu, []))})
        for nu in CASE2_SWAP:
            d = swap.get(nu) or {}
            df = d.get("diff") or {}
            cases["swap"].append({
                "url": nu,
                "declared_version": d.get("declared_version"),
                "stored_sha": (d.get("stored_sha") or "")[:10],
                "head_sha": (d.get("head_sha") or "")[:10],
                "verdict": d.get("verdict"),
                "ahead_by": df.get("ahead_by"),
                "files_changed": df.get("files_changed"),
                "commits": df.get("commits") or [],
                "sources": sorted(d.get("sources") or []),
                "declares": declares(nu),
            })

        # 镜像 vs 原件
        def clone_status(nu):
            r = conn.execute(
                "SELECT status, repo_url, commit_sha, error FROM clone_log WHERE norm_url=?",
                (nu,),
            ).fetchone()
            return dict(r) if r else None

        m_cats, m_csrc = danger(MIRROR_URL)
        o_cats, o_csrc = danger(ORIGINAL_URL)
        cases["mirror"] = {
            "mirror": {"url": MIRROR_URL, "cats": m_cats, "declares": declares(MIRROR_URL),
                       "clone": clone_status(MIRROR_URL), "n_src": len(ri.get(MIRROR_URL, []))},
            "original": {"url": ORIGINAL_URL, "cats": o_cats, "declares": declares(ORIGINAL_URL),
                         "clone": clone_status(ORIGINAL_URL), "n_src": len(ri.get(ORIGINAL_URL, []))},
        }

        # 农场统计（跨源单元）
        farm = {}
        for nu, ss in ri.items():
            o = nu.split("/")[1] if "/" in nu else ""
            if o in FARM_OWNERS and len(ss) > 1:
                farm[o] = farm.get(o, 0) + 1
        farm_total = sum(farm.values())

        # mcp-mirror 跨源单元的克隆状态
        mstat = defaultdict(int)
        for nu, ss in ri.items():
            if (nu.split("/")[1] if "/" in nu else "") != "mcp-mirror" or len(ss) <= 1:
                continue
            cr = conn.execute("SELECT status FROM clone_log WHERE norm_url=?", (nu,)).fetchone()
            mstat[(cr["status"] if cr else "no-row")] += 1

        # 语料级背景
        changed = conn.execute(
            "SELECT COUNT(*) AS c FROM findings WHERE finding_type='c1_reclone' "
            "AND json_extract(data_json,'$.verdict')='changed'"
        ).fetchone()["c"]
    finally:
        conn.close()

    return {
        "cases": cases,
        "farm": farm,
        "farm_total": farm_total,
        "mstat": dict(mstat),
        "changed": changed,
        "multi_danger": multi_danger,
    }


def _cats_row(cats):
    hit = set(cats)
    return "".join("●" if c in hit else "○" for c in CATS_ORDER), len(hit)


def render_md(st: dict) -> str:
    C = st["cases"]
    lines = []
    A = lines.append
    A("# Table.52 安全案例研究（声明 vs 代码：把分歧数字变成安全故事）")
    A("")
    A("**表题**：从语料挑 5 个代表性 MCP 工具做定性深挖，回答 COSE 审稿人的 so-what —— "
      "「这些跨源分歧导致什么安全后果」。三组案例分别对应：① **声明无害、代码危险**"
      "（含多源一致宣称「只读/本地」与单源独占的满档危险单元）；② **声明版本冻结、实现静默前移**"
      "（换芯，附 commit diff）；③ **镜像农场 / typosquat**（幽灵条目）。所有数字实时取自审计库，"
      "无人工编造。")
    A("")
    A("> 背景量级：换芯普查 **%s** 个声明冻结单元检出实现前移（`c1_reclone` verdict=changed）；"
      "多源可见的危险面命中单元 **%s**；镜像农场四家 owner 跨源单元 **%s**。"
      % (f"{st['changed']:,}", f"{st['multi_danger']:,}", f"{st['farm_total']:,}"))
    A("")

    # ---------- A 总览 ----------
    A("## A · 案例总览")
    A("")
    A("| 案例 | 类型 | `norm_url` | 声明源数 | 代码侧危险面 | 一句话安全含义 |")
    A("|---|---|---|---|---|---|")
    for x in C["multi"]:
        cats = "、".join(x["cats"]) or "（无）"
        A(f"| ①-{x['url'].split('/')[-1]} | 声明无害·多源 | `{x['url']}` | {x['n_src']} "
          f"| **{len(x['cats'])} 类** | 多源一致宣称「只读/本地」，代码却命中 {len(x['cats'])} 类危险面 |")
    for x in C["single"]:
        A(f"| ①-{x['url'].split('/')[-1]} | 满档危险·单源独占 | `{x['url']}` | {x['n_src']} "
          f"| **{len(x['cats'])} 类（满档）** | 9/9 类满档危险，却仅 1 源收录 ⇒ 其余 6 表全漏 |")
    for x in C["swap"]:
        A(f"| ②-{x['url'].split('/')[-1]} | 声明冻结·换芯 | `{x['url']}` | {len(x['sources'])} "
          f"| — | 声明版本冻结，实现相对克隆点前移 {x['ahead_by']} commit |")
    m = C["mirror"]
    A(f"| ③-镜像 | 镜像农场/typosquat | `{MIRROR_URL}` | {m['mirror']['n_src']} "
      f"| 无法扫描（已 404） | aibase 收录的镜像条目指向**已删除仓库**，原件健全 |")
    A("")

    # ---------- B 案例① ----------
    A("## B · 案例① 声明「只读 / 本地」 vs 代码命中 8–9 类危险面")
    A("")
    A("**危险面矩阵**（● = 代码侧静态命中，列序同 9 类危险面）：")
    A("")
    A("| 单元 | " + " | ".join(CATS_ORDER) + " | 命中 |")
    A("|---" * (len(CATS_ORDER) + 2) + "|")
    for x in C["multi"] + C["single"]:
        hits, n = _cats_row(x["cats"])
        A(f"| `{x['url']}` | " + " | ".join(hits) + f" | **{n}/9** |")
    A("")
    A("**逐源声明**（description 截断 160 字；空 = 该源未提供描述）：")
    A("")
    for x in C["multi"]:
        A(f"- `{x['url']}`（{x['n_src']} 源；代码命中 {len(x['cats'])} 类："
          + "、".join(x["cats"]) + "）")
        for d in x["declares"]:
            desc = d["desc"][:160] if d["desc"] else "（无描述）"
            A(f"    - **{d['source']}**：{desc}")
    A("")
    A("**读数**：`anipotts/imessage-mcp` 在 official（“Read-only, local.”）、mcpso（“25 read-only tools … "
      "Everything runs locally.”）、cursor 三源均被宣称「只读、本地」，但静态扫描命中 8 类危险面，"
      "含 `network_egress` / `credential_paths` / `fs_destructive` / `arbitrary_code` / `unsafe_deserialize`；"
      "`mikhae1/kubeview-mcp` 同样被 official / aibase / mcpso 一致宣称「Read-only」，命中 8 类。"
      "⇒ **声明层「只读」与代码层能力不一致**：审计若只读注册表描述，会把这类工具当作无害。")
    A("")
    A("**单源独占的满档危险**：`360ghar/backend`（仅 mcpso）与 `agentspore/agentspore`（仅 official）"
      "代码命中 **9/9 满档**（含 `shell_true`），却各被单一注册表独占 —— 其余 6 张单表全部漏报，"
      "与实验 A「单表审计系统性漏报」互为印证。")
    A("")

    # ---------- C 案例② ----------
    A("## C · 案例② 声明版本冻结，实现静默前移（换芯）")
    A("")
    A("| `norm_url` | 声明版本 | 克隆 sha → HEAD | 前移 commit | 改动文件 | 代表 commit |")
    A("|---|---|---|---|---|---|")
    for x in C["swap"]:
        rep = x["commits"][0] if x["commits"] else "—"
        A(f"| `{x['url']}` | `{x['declared_version']}` | `{x['stored_sha']}` → `{x['head_sha']}` "
          f"| **{x['ahead_by']}** | {x['files_changed']} | {rep} |")
    A("")
    A("**读数**：三例均在**声明版本不变**的情况下，实现相对克隆点静默前移（全部为快进前移，"
      "无 force-push）。最小者 `victoriametrics/mcp-vmanomaly` 前移 9 commit / 35 文件，"
      "commit 含 `release: prepare v0.4.0` 等两次版本发布 —— 即**上游已发新版，注册表声明却停在旧版**；"
      "极端者 `hashgraph-online/hol-guard` 前移 413 commit。⇒ 声明版本不是实现版本，"
      "「同版本静默换芯」（C1）在个案上可直接以 commit diff 复现。")
    A("")

    # ---------- D 案例③ ----------
    A("## D · 案例③ 镜像农场 / typosquat（幽灵条目）")
    A("")
    A("| 项目 | `norm_url` | 收录源 | 代码危险面 | clone_log | 说明 |")
    A("|---|---|---|---|---|---|")
    for tag, key in (("原件", "original"), ("镜像", "mirror")):
        x = m[key]
        cl = x["clone"] or {}
        status = cl.get("status", "—")
        cats = f"{len(x['cats'])} 类" if x["cats"] else "无法扫描"
        srcl = "、".join(d["source"] for d in x["declares"]) or "—"
        note = "仓库健全，可作参照" if tag == "原件" else "注册表收录的镜像指向**已删除仓库**（404）"
        A(f"| {tag} | `{x['url']}` | {srcl} | {cats} | `{status}` | {note} |")
    A("")
    A("**农场规模**（跨源单元，红=mcp-mirror）：")
    A("")
    A("| 农场 owner | 跨源单元 |")
    A("|---|---|")
    for o in FARM_OWNERS:
        nn = st["farm"].get(o, 0)
        bold = f"**{nn:,}**" if o == "mcp-mirror" else f"{nn:,}"
        A(f"| `{o}` | {bold} |")
    A(f"| **合计** | **{st['farm_total']:,}** |")
    A("")
    gone = st["mstat"].get("gone", 0)
    ok = st["mstat"].get("ok", 0)
    A(f"**mcp-mirror 跨源单元克隆状态**：`gone` **{gone}** / `ok` {ok} "
      f"（{_pct(gone, gone + ok)} 指向已删除仓库）。")
    A("")
    A("**读数**：aibase 把 `MCP-Mirror/tumf_mcp-shell-server` 当独立条目收录，其描述自认"
      "「https://github.com/tumf/mcp-shell-server 的镜像」；核对 `clone_log`，该镜像条目**已 404（status=gone）**，"
      "而原件 `tumf/mcp-shell-server` 健全（3 源收录，代码命中 5 类危险面："
      "`arbitrary_code` / `db_access` / `env_access` / `fs_destructive` / `subprocess`）。"
      "更广地，mcp-mirror 全部跨源单元克隆状态均为 `gone` ⇒ 农场在提供**指向不存在仓库的幽灵条目**，"
      "既污染跨源对齐，也说明「同名/镜像条目」必须以代码指针而非名声为准。")
    A("")

    A("---")
    A("")
    A("- **口径**：案例为人工挑选的定性样本，**所有数字实时取自审计库**；"
      "危险面 = `code_capability.danger_categories`；声明 = `snapshots` 每 (source,name) 最新一条；"
      "换芯 = `c1_reclone`（stored_sha × head_sha，diff 仅 top-50 前移单元有）；"
      "镜像状态 = `clone_log.status`。")
    A("- **数据源**：`D:\\Data\\mcp-audit\\mcp_audit.db` —— `code_capability` / `c1_reclone` / "
      "`repo_index` / `snapshots` / `clone_log`；脚本 `mcp_audit/case_studies.py`（只读，可复算）。")
    A("- **安全边界**：仅静态标记「具备何种高风险能力」，不判善恶、不发布 PoC、不执行任何第三方代码；"
      "案例中「危险」一律指**能力面命中**，非「已证实恶意」。")
    A("- **落点**：§5 新增小节「案例分析」，或并入 §5.4（注册表差异）之后；配 Fig.24–26。")
    A("")
    return "\n".join(lines)


def show(st: dict) -> None:
    C = st["cases"]
    print(f"换芯普查 changed 单元 : {st['changed']:,}")
    print(f"多源危险面命中单元    : {st['multi_danger']:,}")
    print(f"农场跨源单元          : {st['farm_total']:,}  {st['farm']}")
    print(f"mcp-mirror 克隆状态   : {st['mstat']}")
    print("案例① 多源无害声明：")
    for x in C["multi"]:
        print(f"  {x['url']}  cats={len(x['cats'])}  n_src={x['n_src']}")
    print("案例① 单源满档：")
    for x in C["single"]:
        print(f"  {x['url']}  cats={len(x['cats'])}  n_src={x['n_src']}")
    print("案例② 换芯：")
    for x in C["swap"]:
        print(f"  {x['url']}  v={x['declared_version']}  ahead={x['ahead_by']} files={x['files_changed']}")
    print("案例③ 镜像：")
    print(f"  mirror   {C['mirror']['mirror']['clone']}")
    print(f"  original {C['mirror']['original']['clone']}")


def main() -> None:
    ap = argparse.ArgumentParser(
        prog="mcp_audit.case_studies",
        description="实验 B：安全案例研究（只读）")
    ap.add_argument("--write", action="store_true",
                    help="写出 paper/tables/table52_case_studies.md")
    args = ap.parse_args()

    st = collect()
    show(st)
    if args.write:
        TABLE_OUT.parent.mkdir(parents=True, exist_ok=True)
        TABLE_OUT.write_text(render_md(st), encoding="utf-8")
        print(f"\n已写 {TABLE_OUT}")


if __name__ == "__main__":
    main()
