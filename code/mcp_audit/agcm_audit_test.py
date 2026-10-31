"""M2 测试：AGCM 准确率分层普查——抽样层（S2）。

覆盖（里程碑 §八.2 M2；计划 §二/§五）：
  1. 键映射 disk_dir：norm_url → 磁盘目录 <owner>__<repo>
  2. 分层函数：severity 分桶 / 仓库类型四桶 / 语言组（成员判定）/ 来源富集度
  3. 分配 allocate：合计 = N、非空格保底 1、比例贴近
  4. 抽样 draw_sample：同种子可复现、规模 = N、按 repo_key 排序
  5. 冻结清单 sample()：产 sample.csv + sample.sha256；SHA-256 可复核；force 护栏

运行（在 mcp_audit/ 上级目录下）：py -3 mcp_audit/agcm_audit_test.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from mcp_audit import agcm_audit as m2  # noqa: E402

_FAILURES: list[str] = []
_PASSED = 0


def check(cond: bool, msg: str) -> None:
    global _PASSED
    if cond:
        _PASSED += 1
    else:
        _FAILURES.append(msg)
        print(f"  FAIL {msg}")


def _mk_repo(dual: Path, norm_url: str) -> None:
    d = dual / m2.disk_dir(norm_url)
    d.mkdir(parents=True, exist_ok=True)
    (d / "README.md").write_text("x", encoding="utf-8")


def _fixture() -> tuple[sqlite3.Connection, Path]:
    """构造合成冻结快照 + dual 目录，覆盖全部四桶与语言组。"""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE findings (
            id INTEGER PRIMARY KEY, source TEXT, name TEXT,
            finding_type TEXT, data_json TEXT, created_at TEXT
        );
        CREATE TABLE repo_index (norm_url TEXT, source TEXT);
        """
    )
    tmp = Path(tempfile.mkdtemp(prefix="m2_dual_"))
    # (owner__repo, danger_categories, langs, transports, manifests, tools, n_sources)
    specs = [
        ("acme__a", ["network_egress", "shell_true", "db_access", "subprocess"],
         ["javascript", "typescript"], ["stdio"], ["package.json"], ["t1"], 2),
        ("acme__b", ["env_access"], ["python"], [], ["pyproject.toml"], [], 1),
        ("acme__c", [], ["go"], ["http"], [], ["t1", "t2"], 3),
        ("acme__d", ["credential_paths", "fs_destructive", "arbitrary_code",
                     "unsafe_deserialize", "env_access", "network_egress"],
         [], [], [], [], 4),
        ("acme__e", ["db_access"], ["rust"], ["sse"], [], [], 2),
        ("acme__f", ["network_egress"], ["javascript"], [], ["package.json"],
         ["t1"], 5),
    ]
    for owner_repo, cats, langs, transports, manifests, tools, nsrc in specs:
        nu = "github.com/" + owner_repo.replace("__", "/")
        _mk_repo(tmp, nu)
        payload = {
            "norm_url": nu,
            "danger_categories": cats,
            "langs": langs,
            "declared_transports": transports,
            "manifest_kinds": manifests,
            "tool_names": tools,
            "tool_names_total": len(tools),
            "declared_env_vars": 1 if "env_access" in cats else 0,
            "declared_secrets": 1 if "credential_paths" in cats else 0,
        }
        conn.execute(
            "INSERT INTO findings (source, name, finding_type, data_json, created_at)"
            " VALUES (?,?,?,?,?)",
            ("official", owner_repo, m2.SOURCE_TYPE, json.dumps(payload), "2026-10-04"),
        )
        for s in ["official", "glama", "mcpso"][:nsrc]:
            conn.execute("INSERT INTO repo_index (norm_url, source) VALUES (?,?)", (nu, s))
    conn.commit()
    return conn, tmp


def test_helpers() -> None:
    check(m2.disk_dir("github.com/acme/a") == "acme__a", "disk_dir 映射")
    check(m2.disk_dir("") == "", "disk_dir 空值")
    check(m2.sev_bin(0) == "0" and m2.sev_bin(1) == "1-2" and m2.sev_bin(2) == "1-2",
          "sev_bin 0/1-2")
    check(m2.sev_bin(3) == "3-5" and m2.sev_bin(5) == "3-5", "sev_bin 3-5")
    check(m2.sev_bin(6) == "6-9" and m2.sev_bin(9) == "6-9", "sev_bin 6-9")
    check(m2.lang_group(["javascript"]) == "js/ts", "lang js")
    check(m2.lang_group(["typescript", "python"]) == "js/ts", "lang js 优先于 python")
    check(m2.lang_group(["python", "go"]) == "python", "lang python 优先于 go")
    check(m2.lang_group(["go"]) == "go", "lang go")
    check(m2.lang_group([]) == "unknown", "lang unknown")
    check(m2.lang_group(["rust"]) == "other", "lang other")
    check(m2.repo_type_of({"declared_transports": ["stdio"], "tool_names": ["t"],
                           "tool_names_total": 1}) == "server+tools", "repo server+tools")
    check(m2.repo_type_of({"declared_transports": ["stdio"], "tool_names": [],
                           "tool_names_total": 0}) == "server_only", "repo server_only")
    check(m2.repo_type_of({"declared_transports": [], "tool_names": ["t"],
                           "tool_names_total": 1}) == "tools_only", "repo tools_only")
    check(m2.repo_type_of({"declared_transports": [], "tool_names": [],
                           "tool_names_total": 0}) == "lib_or_other", "repo lib_or_other")
    check(m2.source_tier(1) == "1" and m2.source_tier(2) == "2" and m2.source_tier(5) == "3+",
          "source_tier")


def test_allocate() -> None:
    counts = {"a": 3000, "b": 1000, "c": 500, "d": 400, "e": 100, "f": 50, "g": 10, "h": 9}
    alloc = m2.allocate(counts, 500)
    check(sum(alloc.values()) == 500, f"allocate 合计=500（实得 {sum(alloc.values())}）")
    check(all(alloc[k] >= 1 for k in counts), "allocate 非空格保底 1")
    check(alloc["a"] > alloc["b"] > alloc["g"], "allocate 比例单调")
    check(set(alloc) == set(counts), "allocate 键完整")
    # 非空格数 > N 时点名报错
    try:
        m2.allocate({f"k{i}": 1 for i in range(600)}, 500)
        check(False, "allocate 非空格>N 应报错")
    except ValueError:
        check(True, "allocate 非空格>N 报错")


def test_draw_and_sample() -> None:
    conn, dual = _fixture()
    pool = m2.load_pool(conn, dual)
    check(len(pool) == 6, f"load_pool 6 仓（实得 {len(pool)}）")
    check(all(p["repo_key"] for p in pool), "load_pool repo_key 完整")
    check(all(p["agcm_present"] == set(p["danger_categories"]) for p in pool),
          "load_pool AGCM 判定 = danger_categories")

    # 小 N 强制走比例 + 保底
    d1 = m2.draw_sample(pool, 6, m2.SEED_DEFAULT)
    d2 = m2.draw_sample(pool, 6, m2.SEED_DEFAULT)
    check([r["repo_key"] for r in d1] == [r["repo_key"] for r in d2], "draw_sample 同种子可复现")
    check(len(d1) == 6, f"draw_sample 规模=6（实得 {len(d1)}）")
    check([r["repo_key"] for r in d1] == sorted(r["repo_key"] for r in d1),
          "draw_sample 按 repo_key 排序")

    out = Path(tempfile.mkdtemp(prefix="m2_out_"))
    res = m2.sample(conn, dual, out, n=6, seed=m2.SEED_DEFAULT, force=False)
    sheet = out / "sample.csv"
    check(sheet.exists(), "sample() 产 sample.csv")
    sha = out / "sample.sha256"
    check(sha.exists(), "sample() 产 sample.sha256")
    got = hashlib.sha256(sheet.read_bytes()).hexdigest()
    check(sha.read_text(encoding="utf-8").split()[0] == got, "sample.sha256 可复核")
    check(res["n"] == 6, "sample() 返回 n")
    rows = list(m2._read_csv(sheet))
    check(len(rows) == 6, f"sample.csv 6 行（实得 {len(rows)}）")
    check(rows[0]["item_id"] == "M2-0001", "item_id 前缀 M2")

    # 无 --force 时防覆盖
    try:
        m2.sample(conn, dual, out, n=6, seed=m2.SEED_DEFAULT, force=False)
        check(False, "sample() 应防覆盖")
    except SystemExit:
        check(True, "sample() 防覆盖已标注包")


def test_repo_context() -> None:
    conn, dual = _fixture()
    names, snip = m2.repo_context(dual / "acme__a", maxlen=200)
    check("README.md" in names, "repo_context 文件清单含 README.md")
    check(isinstance(snip, str), "repo_context snippet 为字符串")


def test_declared_of() -> None:
    base = {"declared_env_vars": 0, "declared_secrets": 0, "declared_transports": []}
    check(m2.declared_of("db_access", base) == "no_decl", "declared db_access → no_decl")
    check(m2.declared_of("arbitrary_code", base) == "no_decl", "declared arbitrary_code → no_decl")
    check(m2.declared_of("env_access", {**base, "declared_env_vars": 2}) == "present",
          "declared env_access 有声明 → present")
    check(m2.declared_of("env_access", base) == "absent", "declared env_access 无声明 → absent")
    check(m2.declared_of("credential_paths", {**base, "declared_secrets": 1}) == "present",
          "declared credential_paths 有密钥 → present")
    check(m2.declared_of("network_egress", {**base, "declared_transports": ["http"]}) == "present",
          "declared network_egress http → present")
    check(m2.declared_of("network_egress", {**base, "declared_transports": ["sse"]}) == "present",
          "declared network_egress sse → present")
    check(m2.declared_of("network_egress", {**base, "declared_transports": ["stdio"]}) == "absent",
          "declared network_egress stdio → absent")


def test_sheet() -> None:
    conn, dual = _fixture()
    out = Path(tempfile.mkdtemp(prefix="m2_sheet_"))
    m2.sample(conn, dual, out, n=6, seed=m2.SEED_DEFAULT, force=False)
    res = m2.sheet(conn, dual, out, force=False)

    sheet = out / "sheet.csv"
    key = out / "key.csv"
    cb = out / "codebook.csv"
    check(sheet.exists(), "sheet() 产 sheet.csv")
    check(key.exists(), "sheet() 产 key.csv")
    check(cb.exists(), "sheet() 产 codebook.csv")

    ncat = len(m2.DANGER_CATEGORIES)
    srows = list(m2._read_csv(sheet))
    check(len(srows) == 6 * ncat, f"sheet.csv {6 * ncat} 行（实得 {len(srows)}）")
    check(set(srows[0].keys()) == {"item_id", "repo_key", "category", "category_zh",
                                   "file_list", "snippet"}, "sheet 列不含 AGCM/声明/分层")
    check(all("#" in r["item_id"] for r in srows), "item_id 形如 repo#category")
    check(len({r["repo_key"] for r in srows}) == 6, "sheet 覆盖 6 仓")
    check({r["category"] for r in srows} == set(m2.DANGER_CATEGORIES), "sheet 覆盖 9 类")
    check(all("README.md" in r["file_list"] for r in srows), "sheet 带中性文件清单")
    check("danger_categories" not in sheet.read_text(encoding="utf-8-sig"),
          "sheet 不泄露 danger_categories")

    krows = list(m2._read_csv(key))
    check(len(krows) == 6 * ncat, f"key.csv {6 * ncat} 行（实得 {len(krows)}）")
    check({"agcm_present", "declared", "stratum", "sev_bin", "repo_type"} <= set(krows[0].keys()),
          "key.csv 含 AGCM/声明/分层")
    by_id = {r["item_id"]: r for r in krows}
    check(by_id["acme__a#network_egress"]["agcm_present"] == "1", "key AGCM 命中 network_egress")
    check(by_id["acme__b#db_access"]["agcm_present"] == "0", "key AGCM 未命中 db_access")
    check(by_id["acme__a#db_access"]["declared"] == "no_decl", "key db_access 官方失语")
    check(by_id["acme__b#env_access"]["declared"] == "present", "key env_access 官方声明")

    crows = list(m2._read_csv(cb))
    check(len(crows) == ncat, f"codebook.csv {ncat} 行（实得 {len(crows)}）")
    check({r["category"] for r in crows} == set(m2.DANGER_CATEGORIES), "codebook 覆盖 9 类")
    check({"定义", "正例", "反例", "边界"} <= set(crows[0].keys()),
          "codebook 含定义/正例/反例/边界")
    check(all(crows_i["定义"] and crows_i["边界"] for crows_i in crows), "codebook 定义/边界非空")

    for name in m2.ROSTER_DEFAULT:
        card = out / f"label_{name}.csv"
        check(card.exists(), f"sheet() 产 label_{name}.csv")
        lrows = list(m2._read_csv(card))
        check(len(lrows) == 6 * ncat, f"label_{name}.csv {6 * ncat} 行")
        check(all(r["label"] == "" for r in lrows), f"label_{name}.csv 空卡")

    try:
        m2.sheet(conn, dual, out, force=False)
        check(False, "sheet() 应防覆盖")
    except SystemExit:
        check(True, "sheet() 防覆盖已标注包")


def _mk_score_fixture() -> Path:
    """合成 key.csv + 5 张 label 卡，覆盖：TP/FP/FN/TN、争议未决、unsure、官方声明。"""
    out = Path(tempfile.mkdtemp(prefix="m2_score_"))
    cats = m2.DANGER_CATEGORIES
    repos = ["r__1", "r__2", "r__3", "r__4"]
    agcm = {"r__1": {"arbitrary_code", "env_access", "shell_true"},
            "r__2": {"arbitrary_code", "env_access"},
            "r__3": {"shell_true"}, "r__4": set()}
    gold = {
        "arbitrary_code": {"r__1": "present", "r__2": "absent",
                           "r__3": "present", "r__4": "absent"},
        "env_access": {"r__1": "present", "r__3": "absent", "r__4": "present"},
        "shell_true": {"r__1": "present", "r__2": "absent",
                       "r__3": "present", "r__4": "absent"},
        "db_access": {"r__1": "unsure", "r__2": "absent",
                      "r__3": "absent", "r__4": "absent"},
    }
    for c in cats:
        if c not in gold:
            gold[c] = {r: "absent" for r in repos}
    declared = {(c, r): "no_decl" for c in cats for r in repos}
    for r in repos:
        declared[("env_access", r)] = "present" if r in ("r__1", "r__2") else "absent"

    key_rows, me, other, ds = [], [], [], []
    for r in repos:
        for c in cats:
            it = f"{r}#{c}"
            key_rows.append({
                "item_id": it, "repo_key": r, "category": c,
                "stratum": "0×server_only", "sev_bin": "0", "repo_type": "server_only",
                "lang_group": "js/ts", "source_tier": "1",
                "agcm_present": 1 if c in agcm[r] else 0,
                "declared": declared[(c, r)], "n_danger": len(agcm[r])})
            gl = gold[c].get(r, "")
            m_lab, o_lab = gl, gl
            if c == "env_access" and r == "r__2":
                m_lab, o_lab = "present", "absent"
            me.append({"item_id": it, "label": m_lab, "note": ""})
            other.append({"item_id": it, "label": o_lab, "note": ""})
            ds.append({"item_id": it,
                       "label": gl if gl in ("present", "absent") else "absent", "note": ""})
    m2._write_csv(out / "key.csv", key_rows)
    m2._write_csv(out / "label_Me.csv", me)
    m2._write_csv(out / "label_other.csv", other)
    m2._write_csv(out / "label_DeepSeek.csv", ds)
    return out


def test_stats_helpers() -> None:
    a = {"1": "present", "2": "absent", "3": "present", "4": "absent"}
    b = {"1": "present", "2": "absent", "3": "absent", "4": "present"}
    k, po, pe = m2.cohen_kappa_l(a, b, ["1", "2", "3", "4"])
    check(abs(po - 0.5) < 1e-9, f"cohen_kappa_l Po=0.5（实得 {po}）")
    check(abs(pe - 0.5) < 1e-9, f"cohen_kappa_l Pe=0.5（实得 {pe}）")
    check(abs(k - 0.0) < 1e-9, f"cohen_kappa_l kappa=0（实得 {k}）")
    k2, _, _ = m2.cohen_kappa_l(a, a, ["1", "2", "3", "4"])
    check(abs(k2 - 1.0) < 1e-9, "cohen_kappa_l 完全一致 = 1")
    check(m2.cohen_kappa_l(a, b, [])[0] != m2.cohen_kappa_l(a, b, [])[0],
          "cohen_kappa_l 空 common → nan")

    p, r, f = m2._prf(8, 2, 1)
    check(abs(p - 0.8) < 1e-9, f"_prf precision=0.8（实得 {p}）")
    check(abs(r - 8 / 9) < 1e-9, f"_prf recall=8/9（实得 {r}）")
    check(abs(f - 2 * 0.8 * (8 / 9) / (0.8 + 8 / 9)) < 1e-9, "_prf f1")
    check(m2._prf(0, 0, 0)[0] != m2._prf(0, 0, 0)[0], "_prf 空分母 → nan")
    check(m2._prf(2, 0, 0) == (1.0, 1.0, 1.0), "_prf 全对 = 1")


def test_score() -> None:
    out = _mk_score_fixture()
    res = m2.score(out)
    pc = res["per_class"]

    ac = pc["arbitrary_code"]
    check((ac["tp"], ac["fp"], ac["fn"], ac["tn"]) == (1, 1, 1, 1), "arbitrary_code TP/FP/FN/TN")
    check(abs(ac["precision"] - 0.5) < 1e-9, f"arbitrary_code precision=0.5（实得 {ac['precision']}）")
    check(abs(ac["recall"] - 0.5) < 1e-9, f"arbitrary_code recall=0.5（实得 {ac['recall']}）")
    check(abs(ac["f1"] - 0.5) < 1e-9, f"arbitrary_code f1=0.5（实得 {ac['f1']}）")
    check(ac["recall_lo"] is not None and ac["recall_lo"] < 0.5 < ac["recall_hi"],
          "arbitrary_code recall 配 Wilson CI")
    check(abs(ac["kappa_hh"] - 1.0) < 1e-9, f"arbitrary_code 双真人 κ=1（实得 {ac['kappa_hh']}）")

    ev = pc["env_access"]
    check(ev["n_unresolved"] == 1, f"env_access 争议未决=1（实得 {ev['n_unresolved']}）")
    check(ev["n_gold"] == 3, f"env_access 金标分母=3（实得 {ev['n_gold']}）")
    check(abs(ev["recall"] - 0.5) < 1e-9, f"env_access recall=0.5（实得 {ev['recall']}）")
    check(abs(ev["precision"] - 1.0) < 1e-9, "env_access precision=1")
    check(abs(ev["kappa_hh"] - 0.5) < 1e-9, f"env_access 双真人 κ=0.5（实得 {ev['kappa_hh']}）")

    check(pc["db_access"]["n_unsure"] == 1, "db_access unsure=1")
    st = pc["shell_true"]
    check((st["precision"], st["recall"]) == (1.0, 1.0), "shell_true P/R=1")

    ov = res["overall"]
    check(ov["kappa_hh"] > 0.9, f"总体双真人 κ>0.9（实得 {ov['kappa_hh']}）")
    check(res["gold_source"] == "consensus", "无金标文件时用真人共识")
    check("fails" in res["gate"] and "verdict" in res["gate"], "gate 含 verdict/fails")

    sc = out / "score.csv"
    check(sc.exists(), "score() 产 score.csv")
    rows = list(m2._read_csv(sc))
    check(any(r["category"] == "__overall__" for r in rows), "score.csv 含 overall 行")
    check(any(r["category"] == "arbitrary_code" for r in rows), "score.csv 含逐类行")


def test_score_gold_file() -> None:
    out = _mk_score_fixture()
    gold_rows = []
    for r in m2._read_csv(out / "key.csv"):
        gold_rows.append({"item_id": r["item_id"], "label": "absent", "note": ""})
    m2._write_csv(out / "label_gold.csv", gold_rows)
    res = m2.score(out)
    check(res["gold_source"] == "label_gold.csv", "有金标文件时优先使用")
    check(res["per_class"]["arbitrary_code"]["tp"] == 0, "金标全 absent → tp=0")
    check(res["per_class"]["arbitrary_code"]["fp"] == 2, "金标全 absent → fp=2")


def test_matrix() -> None:
    out = _mk_score_fixture()
    res = m2.matrix(out)
    rows = {(r["category"], r["declared"], r["agcm"], r["gold"]): int(r["count"])
            for r in res["rows"]}
    check(rows[("arbitrary_code", "no_decl", "1", "present")] == 1, "matrix arbitrary TP 格")
    check(rows[("arbitrary_code", "no_decl", "1", "absent")] == 1, "matrix arbitrary FP 格")
    check(rows[("arbitrary_code", "no_decl", "0", "present")] == 1, "matrix arbitrary FN 格")
    check(rows[("arbitrary_code", "no_decl", "0", "absent")] == 1, "matrix arbitrary TN 格")
    check(("env_access", "present", "1", "absent") not in rows, "matrix 未决项被剔除")
    check(rows[("env_access", "present", "1", "present")] == 1, "matrix env 官方声明+AGCM 命中")
    check(rows[("env_access", "absent", "0", "absent")] == 1, "matrix env 双失语")
    check(rows[("env_access", "absent", "0", "present")] == 1, "matrix env AGCM 漏报")
    mx = out / "matrix.csv"
    check(mx.exists(), "matrix() 产 matrix.csv")


def test_gold_helpers() -> None:
    check(m2._llm_majority(["present", "present", "absent"]) == "present", "llm 多数=present")
    check(m2._llm_majority(["absent", "absent", "present"]) == "absent", "llm 多数=absent")
    check(m2._llm_majority(["present", "absent"]) == "", "llm 平票 → 空")
    check(m2._llm_majority(["unsure", "unsure"]) == "", "llm 无有效票 → 空")
    check(m2._llm_majority([]) == "", "llm 空输入 → 空")


def test_gold() -> None:
    out = _mk_score_fixture()
    res = m2.gold(out)
    check(res["n_items"] == 36, f"gold 覆盖全部 item（实得 {res['n_items']}）")
    check(res["n_consensus"] == 35, f"gold 共识=35（实得 {res['n_consensus']}）")
    check(res["n_disputed"] == 1, f"gold 争议=1（实得 {res['n_disputed']}）")
    check(res["n_unresolved"] == 1, f"gold 未裁决=1（实得 {res['n_unresolved']}）")
    check(res["gold_source"] == "label_gold.csv", "gold 后复算用金标文件")

    gp = out / "label_gold.csv"
    check(gp.exists(), "gold() 产 label_gold.csv")
    gm = {r["item_id"]: r["label"] for r in m2._read_csv(gp)}
    check(len(gm) == 36, "label_gold.csv 36 行")
    check(gm["r__1#arbitrary_code"] == "present", "金标：双真人一致 present")
    check(gm["r__1#db_access"] == "unsure", "金标：双真人一致 unsure")
    check(gm["r__2#env_access"] == "", "金标：未裁决争议留空")

    dp = out / "disputes.csv"
    check(dp.exists(), "gold() 产 disputes.csv 裁决清单")
    dmap = {r["item_id"]: r for r in m2._read_csv(dp)}
    check(set(dmap) == {"r__2#env_access"}, "裁决清单仅含争议项")
    check(dmap["r__2#env_access"]["label_Me"] == "present", "裁决清单带 Me 标")
    check(dmap["r__2#env_access"]["label_other"] == "absent", "裁决清单带 other 标")
    check(dmap["r__2#env_access"]["llm_majority"] == "absent", "裁决清单带 LLM 多数（仅作参考）")
    check("adjudication" in dmap["r__2#env_access"], "裁决清单含 adjudication 列")

    m2._write_csv(out / "adjudication.csv", [
        {"item_id": "r__2#env_access", "label": "present", "note": "复读源码确证"}])
    res2 = m2.gold(out, force=True)
    check(res2["n_adjudicated"] == 1, f"gold 裁决=1（实得 {res2['n_adjudicated']}）")
    check(res2["n_unresolved"] == 0, "裁决后无未决")
    gm2 = {r["item_id"]: r["label"] for r in m2._read_csv(out / "label_gold.csv")}
    check(gm2["r__2#env_access"] == "present", "裁决后金标=present")
    check(res2["score"]["gold_source"] == "label_gold.csv", "裁决后复算仍用金标文件")

    try:
        m2.gold(out)
        check(False, "gold() 应防覆盖金标卡")
    except SystemExit:
        check(True, "gold() 防覆盖金标卡")


def test_m2_llm_helpers() -> None:
    check(m2.M2_LABELS == ("present", "absent", "unsure"), "M2_LABELS 三值域")
    check(m2._norm_m2_label("present") == "present", "norm present")
    check(m2._norm_m2_label("  PRESENT ") == "present", "norm 大小写/空白")
    check(m2._norm_m2_label("存在") == "present", "norm 中文别名 present")
    check(m2._norm_m2_label("absent") == "absent", "norm absent")
    check(m2._norm_m2_label("无") == "absent", "norm 中文别名 absent")
    check(m2._norm_m2_label("unsure") == "unsure", "norm unsure")
    check(m2._norm_m2_label("不确定") == "unsure", "norm 中文别名 unsure")
    check(m2._norm_m2_label("") == "", "norm 空 → 空")
    check(m2._norm_m2_label("bogus") == "", "norm 非法 → 空")

    cb = m2._m2_codebook_text("shell_true")
    check("shell_true" in cb and "Shell 执行" in cb, "codebook 含类名与中文名")
    check("定义" in cb and "正例" in cb and "反例" in cb and "边界" in cb,
          "codebook 含定义/正例/反例/边界")

    it = {"item_id": "acme__a#network_egress", "repo_key": "acme__a",
          "category": "network_egress", "category_zh": "网络外联",
          "file_list": "a.py;b.js", "snippet": "--- a.py ---\nrequests.get(url)"}
    pr = m2._m2_llm_prompt(it)
    check("acme__a" in pr and "requests.get(url)" in pr, "prompt 含仓库与片段")
    check("网络外联" in pr, "prompt 含类中文名")
    check("agcm" not in pr.lower() and "declared" not in pr.lower(),
          "prompt 不泄露 AGCM/声明")

    lb, note = m2._parse_m2_json('{"label": "present", "note": "有 requests 调用"}', "")
    check(lb == "present" and note.startswith("有"), "parse 正常 JSON")
    lb2, _ = m2._parse_m2_json("```json\n{\"label\": \"absent\"}\n```", "")
    check(lb2 == "absent", "parse 容忍代码块包裹")
    lb3, _ = m2._parse_m2_json("模型说：不确定", "")
    check(lb3 == "", "parse 无 JSON → 空")
    lb4, _ = m2._parse_m2_json('{"label": "有"}', "")
    check(lb4 == "present", "parse 中文标签归一")
    lb5, _ = m2._parse_m2_json('{"label": "bogus"}', "unsure")
    check(lb5 == "unsure", "parse 非法标签用 fallback")

    out = Path(tempfile.mkdtemp(prefix="m2_llm_"))
    items = [{"item_id": "r__1#shell_true"}, {"item_id": "r__1#db_access"}]
    m2._dump_m2_rows(out / "label_X.csv", items,
                     {"r__1#shell_true": {"label": "present", "note": "n"}})
    rows = {r["item_id"]: r for r in m2._read_csv(out / "label_X.csv")}
    check(rows["r__1#shell_true"]["label"] == "present", "dump_m2 回写已填")
    check(rows["r__1#db_access"]["label"] == "", "dump_m2 未填留空")

    runs = {("DeepSeek", 1, "r__1#shell_true"): {"label": "present", "note": "n"}}
    m2._write_m2_runs(out / "reps" / "runs.csv", runs)
    back = m2._read_m2_runs(out / "reps" / "runs.csv")
    check(back.get(("DeepSeek", 1, "r__1#shell_true"), {}).get("label") == "present",
          "runs 往返一致")


def test_llm_runner() -> None:
    """以 monkeypatch 替换网络调用，验证缓存续跑 + 落卡 + runs 长表。"""
    out = Path(tempfile.mkdtemp(prefix="m2_llmrun_"))
    items = []
    for c in ("shell_true", "db_access"):
        items.append({"item_id": f"r__1#{c}", "repo_key": "r__1", "category": c,
                      "category_zh": m2.CATEGORY_ZH[c], "file_list": "a.py",
                      "snippet": "code"})
    m2._write_csv(out / "sheet.csv", items)

    calls = {"n": 0}

    def fake_call(prov, prompt, api_key, timeout, system=m2.M2_SYSTEM_PROMPT,
                  minimal=False, retries=None, on_retry=None, client=None):
        calls["n"] += 1
        return '{"label": "present", "note": "fake"}'

    orig = m2._m2_llm_call
    m2._m2_llm_call = fake_call
    try:
        res = m2.llm(out, ["DeepSeek"], providers={"DeepSeek": {"key": "sk-test"}},
                     sleep=0.0)
        check(res["items"] == 2, "llm 处理 2 条")
        e = res["models"]["DeepSeek"]
        check(e["filled"] == 2 and e["errors"] == 0, "llm 填卡 2 条无错")
        card = out / "label_DeepSeek.csv"
        check(card.exists(), "llm 产 label_DeepSeek.csv")
        got = {r["item_id"]: r["label"] for r in m2._read_csv(card)}
        check(got.get("r__1#shell_true") == "present", "llm 卡写入 present")
        runs = out / "reps" / "runs.csv"
        check(runs.exists(), "llm 产 reps/runs.csv 长表")
        n_first = calls["n"]
        res2 = m2.llm(out, ["DeepSeek"], providers={"DeepSeek": {"key": "sk-test"}},
                      sleep=0.0)
        check(calls["n"] == n_first, "llm 缓存续跑不重复调用")
        check(res2["models"]["DeepSeek"]["cached"] == 2, "llm 缓存命中 2 条")
    finally:
        m2._m2_llm_call = orig


def test_llm_parallel_models() -> None:
    """parallel=True：三家模型**同时**进入网络调用（屏障证明并行，串行必超时/报错）。"""
    import threading

    out = Path(tempfile.mkdtemp(prefix="m2_llmpar_"))
    items = [{"item_id": "r__1#shell_true", "repo_key": "r__1", "category": "shell_true",
              "category_zh": m2.CATEGORY_ZH["shell_true"], "file_list": "a.py",
              "snippet": "code"}]
    m2._write_csv(out / "sheet.csv", items)

    models = ["DeepSeek", "Claude", "GLM"]
    barrier = threading.Barrier(len(models), timeout=6)   # 三家到齐才放行 → 串行必超时
    seen: list[str] = []
    seen_lock = threading.Lock()

    def fake_call(prov, prompt, api_key, timeout, system=m2.M2_SYSTEM_PROMPT,
                  minimal=False, retries=None, on_retry=None, client=None):
        with seen_lock:
            seen.append(str(prov.get("model") or ""))
        barrier.wait()
        return '{"label": "absent", "note": "par"}'

    orig = m2._m2_llm_call
    m2._m2_llm_call = fake_call
    try:
        res = m2.llm(out, models, providers={m: {"key": "sk-test"} for m in models},
                     sleep=0.0, parallel=True)
        for m in models:
            check(res["models"][m]["filled"] == 1, f"parallel {m} 填卡 1 条")
            check(res["models"][m]["errors"] == 0, f"parallel {m} 无错")
            check((out / f"label_{m}.csv").exists(), f"parallel {m} 落卡")
        check(len(seen) == 3, "parallel 三家各调用一次（同时进入）")
    finally:
        m2._m2_llm_call = orig


def test_load_providers() -> None:
    """providers JSON：default 先合并、具名覆盖优先；缺文件 / 坏 JSON / 顶层非对象报错。"""
    out = Path(tempfile.mkdtemp(prefix="m2_prov_"))
    p = out / "providers.json"
    p.write_text(json.dumps({
        "default": {"url": "https://relay.example/v1/chat/completions", "key": "sk-relay"},
        "DeepSeek": {"model": "ds-chat"},
        "Claude": {"url": "https://other.example/v1/chat/completions"},
    }, ensure_ascii=False), encoding="utf-8")
    got = m2._load_providers(p)
    check(got["DeepSeek"]["url"] == "https://relay.example/v1/chat/completions",
          "providers default.url 合并进 DeepSeek")
    check(got["DeepSeek"]["key"] == "sk-relay", "providers default.key 合并进 DeepSeek")
    check(got["DeepSeek"]["model"] == "ds-chat", "providers 具名 model 覆盖")
    check(got["Claude"]["url"] == "https://other.example/v1/chat/completions",
          "providers 具名 url 优先于 default")
    check(got["Claude"]["key"] == "sk-relay", "providers Claude 仍继承 default.key")
    check("default" not in got, "providers 结果不含 default 键")

    raised = False
    try:
        m2._load_providers(out / "nope.json")
    except SystemExit:
        raised = True
    check(raised, "providers 缺文件 → SystemExit")

    bad = out / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    raised = False
    try:
        m2._load_providers(bad)
    except SystemExit:
        raised = True
    check(raised, "providers 坏 JSON → SystemExit")

    bad2 = out / "bad2.json"
    bad2.write_text("[1, 2, 3]", encoding="utf-8")
    raised = False
    try:
        m2._load_providers(bad2)
    except SystemExit:
        raised = True
    check(raised, "providers 顶层非对象 → SystemExit")


def test_llm_providers_override() -> None:
    """llm() 用 providers 覆盖 url/model，且 key 取自覆盖（中转站场景）。"""
    out = Path(tempfile.mkdtemp(prefix="m2_llmov_"))
    m2._write_csv(out / "sheet.csv", [
        {"item_id": "r__1#shell_true", "repo_key": "r__1", "category": "shell_true",
         "category_zh": m2.CATEGORY_ZH["shell_true"], "file_list": "a.py", "snippet": "code"}])
    seen: dict = {}

    def fake_call(prov, prompt, api_key, timeout, system=m2.M2_SYSTEM_PROMPT,
                  minimal=False, retries=None, on_retry=None, client=None):
        seen["url"] = prov.get("url")
        seen["model"] = prov.get("model")
        seen["key"] = api_key
        return '{"label": "absent", "note": "fake"}'

    orig = m2._m2_llm_call
    m2._m2_llm_call = fake_call
    try:
        m2.llm(out, ["DeepSeek"], sleep=0.0, providers={
            "DeepSeek": {"url": "https://relay.example/v1/chat/completions",
                         "model": "ds-chat", "key": "sk-relay"}})
        check(seen["url"] == "https://relay.example/v1/chat/completions",
              "llm 用覆盖 url 发起调用")
        check(seen["model"] == "ds-chat", "llm 用覆盖 model 发起调用")
        check(seen["key"] == "sk-relay", "llm 用覆盖 key 发起调用")
    finally:
        m2._m2_llm_call = orig


def _mk_route2_fixture() -> Path:
    """路线 2 合成夹具：4 仓 × 9 类 = 36 项 + 3 家 LLM 卡（含 2 项分歧）。"""
    out = Path(tempfile.mkdtemp(prefix="m2_route2_"))
    cats = m2.DANGER_CATEGORIES
    repos = ["r__1", "r__2", "r__3", "r__4"]
    key_rows, srows = [], []
    for r in repos:
        for c in cats:
            it = f"{r}#{c}"
            key_rows.append({
                "item_id": it, "repo_key": r, "category": c,
                "stratum": "0×server_only", "sev_bin": "0", "repo_type": "server_only",
                "lang_group": "js/ts", "source_tier": "1",
                "agcm_present": 0, "declared": "no_decl", "n_danger": 0})
            srows.append({
                "item_id": it, "repo_key": r, "category": c,
                "category_zh": m2.CATEGORY_ZH[c], "file_list": "a.py", "snippet": "code"})
    m2._write_csv(out / "key.csv", key_rows)
    m2._write_csv(out / "sheet.csv", srows)

    def card(name: str, overrides: dict) -> None:
        rows = [{"item_id": f"{r}#{c}", "label": overrides.get(f"{r}#{c}", "absent"),
                 "note": ""} for r in repos for c in cats]
        m2._write_csv(out / f"label_{name}.csv", rows)

    card("DeepSeek", {"r__1#arbitrary_code": "present", "r__2#env_access": "present",
                      "r__3#db_access": "present", "r__4#shell_true": "unsure"})
    card("Claude", {"r__1#arbitrary_code": "present", "r__2#env_access": "absent",
                    "r__3#db_access": "present", "r__4#shell_true": "unsure"})
    card("GLM", {"r__1#arbitrary_code": "present", "r__2#env_access": "absent",
                 "r__3#db_access": "unsure", "r__4#shell_true": "unsure"})
    return out


def test_triage() -> None:
    out = _mk_route2_fixture()
    res = m2.triage(out)
    check(res["n_items"] == 36, f"triage 覆盖 36 项（实得 {res['n_items']}）")
    check(res["n_consensus"] == 34, f"triage 一致=34（实得 {res['n_consensus']}）")
    check(res["n_disputed"] == 2, f"triage 分歧=2（实得 {res['n_disputed']}）")

    tp = out / "triage.csv"
    check(tp.exists(), "triage() 产 triage.csv")
    tmap = {r["item_id"]: r for r in m2._read_csv(tp)}
    check(len(tmap) == 36, "triage.csv 36 行")
    check(tmap["r__1#arbitrary_code"]["status"] == "consensus", "3/3 一致 → consensus")
    check(tmap["r__1#arbitrary_code"]["llm_label"] == "present", "一致项带 LLM 共识标签")
    check(tmap["r__4#shell_true"]["llm_label"] == "unsure", "一致 unsure 也是共识")
    check(tmap["r__2#env_access"]["status"] == "disputed", "2/1 分歧 → disputed")
    check(tmap["r__2#env_access"]["llm_label"] == "", "分歧项无共识标签")
    check(tmap["r__3#db_access"]["status"] == "disputed", "present/present/unsure → disputed")

    ap = out / "adjudication.csv"
    check(ap.exists(), "triage() 产 adjudication.csv")
    amap = {r["item_id"]: r for r in m2._read_csv(ap)}
    check(set(amap) == {"r__2#env_access", "r__3#db_access"}, "裁决工作表仅含分歧项")
    check(amap["r__2#env_access"]["label"] == "", "裁决工作表标签留空待人工填")
    check(amap["r__2#env_access"]["llm_DeepSeek"] == "present", "裁决表带各家 LLM 票")
    check("agcm" in amap["r__2#env_access"], "裁决表带 AGCM 参考")

    # 人工填裁决后重跑 triage 不得覆盖
    m2._write_csv(ap, [{"item_id": "r__2#env_access", "label": "present", "note": "人工裁决"}])
    m2.triage(out)
    keep = {r["item_id"]: r["label"] for r in m2._read_csv(ap)}
    check(keep.get("r__2#env_access") == "present", "重跑 triage 不覆盖人工裁决")


def test_audit_sample() -> None:
    out = _mk_route2_fixture()
    m2.triage(out)
    res = m2.audit_sample(out, fraction=0.10, seed=m2.SEED_DEFAULT)
    check(res["n_pool"] == 34, f"盲审池=34 一致项（实得 {res['n_pool']}）")
    check(res["n_sampled"] == 3, f"盲审抽样=3（实得 {res['n_sampled']}）")

    ap = out / "audit_sample.csv"
    check(ap.exists(), "audit_sample() 产 audit_sample.csv")
    rows = list(m2._read_csv(ap))
    check(len(rows) == 3, "audit_sample.csv 3 行")
    check(all("snippet" in r and "file_list" in r for r in rows), "盲审样本带中性上下文")
    check("label" not in rows[0] and "llm_label" not in rows[0], "盲审样本不泄露 LLM 标签")
    tmap = {r["item_id"]: r for r in m2._read_csv(out / "triage.csv")}
    check(all(tmap[r["item_id"]]["status"] == "consensus" for r in rows),
          "盲审只从一致项抽样")

    for h in m2.HUMANS_DEFAULT:
        c = out / f"label_audit_{h}.csv"
        check(c.exists(), f"audit_sample() 产 label_audit_{h}.csv")
        lr = list(m2._read_csv(c))
        check(len(lr) == 3 and all(x["label"] == "" for x in lr), f"label_audit_{h}.csv 空卡")

    ids1 = [r["item_id"] for r in rows]
    try:
        m2.audit_sample(out, fraction=0.10, seed=m2.SEED_DEFAULT)
        check(False, "audit_sample() 应防覆盖")
    except SystemExit:
        check(True, "audit_sample() 防覆盖盲审包")
    m2.audit_sample(out, fraction=0.10, seed=m2.SEED_DEFAULT, force=True)
    ids2 = [r["item_id"] for r in m2._read_csv(ap)]
    check(ids1 == ids2, "audit_sample 同种子可复现")


def test_llm_gold() -> None:
    out = _mk_route2_fixture()
    m2.triage(out)
    m2.audit_sample(out, fraction=0.10, seed=m2.SEED_DEFAULT)
    audited = [r["item_id"] for r in m2._read_csv(out / "audit_sample.csv")]

    me, other = [], []
    for idx, it in enumerate(audited):
        if idx == 0:
            me.append({"item_id": it, "label": "present", "note": ""})
            other.append({"item_id": it, "label": "absent", "note": ""})
        else:
            me.append({"item_id": it, "label": "absent", "note": ""})
            other.append({"item_id": it, "label": "absent", "note": ""})
    m2._write_csv(out / "label_audit_Me.csv", me)
    m2._write_csv(out / "label_audit_other.csv", other)
    m2._write_csv(out / "adjudication.csv", [
        {"item_id": "r__2#env_access", "label": "present", "note": "复读源码"}])

    res = m2.llm_gold(out)
    gp = out / "label_gold.csv"
    check(gp.exists(), "llm_gold() 产 label_gold.csv")
    gm = {r["item_id"]: r["label"] for r in m2._read_csv(gp)}
    check(len(gm) == 36, "label_gold.csv 36 行")
    check(gm["r__1#arbitrary_code"] == "present", "一致项取 LLM 共识")
    check(gm["r__4#shell_true"] == "unsure", "一致 unsure 进金标")
    check(gm["r__2#env_access"] == "present", "分歧项取人工裁决")
    check(gm["r__3#db_access"] == "", "未裁决分歧留空")
    check(gm[audited[1]] == "absent", "盲审一致项人工共识覆盖 LLM")
    check(gm[audited[0]] == "", "盲审双人分歧 → 未决")
    note = {r["item_id"]: r["note"] for r in m2._read_csv(gp)}
    check(note[audited[1]] == "audit-human", "盲审覆盖来源标注 audit-human")
    check(note["r__1#arbitrary_code"] == "llm-consensus", "一致来源标注 llm-consensus")

    check(res["n_llm_consensus"] == 31, f"LLM 共识=31（实得 {res['n_llm_consensus']}）")
    check(res["n_audit_human"] == 2, f"盲审人工覆盖=2（实得 {res['n_audit_human']}）")
    check(res["n_adjudicated"] == 1, f"人工裁决=1（实得 {res['n_adjudicated']}）")
    check(res["n_unresolved"] == 2, f"未决=2（实得 {res['n_unresolved']}）")
    check(res["score"]["gold_source"] == "label_gold.csv", "llm_gold 后复算用金标文件")

    try:
        m2.llm_gold(out)
        check(False, "llm_gold() 应防覆盖金标卡")
    except SystemExit:
        check(True, "llm_gold() 防覆盖金标卡")


# --- 路线 2b：加厚重标 + 第 4 家否决式裁定 + 跨层人工盲审 ----------------
# 计划：docs/superpowers/plans/2026-10-10-m2-route2b-thick-adjudication.md

# 厚层三家 + 第 4 家票（仅 3 个分歧项）：r__1 由 2/1 加第4家转 3/1；r__2 加第4家成 2/2 未决；
# r__3 厚层 3/0 直接锁定。用于验证规则①「第 4 家只能否决、不能 overrule」。
_R2B_VOTES = {
    "DeepSeek": {"r__1#network_egress": "present", "r__2#env_access": "present",
                 "r__3#db_access": "absent"},
    "Claude":   {"r__1#network_egress": "present", "r__2#env_access": "absent",
                 "r__3#db_access": "absent"},
    "GLM":      {"r__1#network_egress": "absent",  "r__2#env_access": "present",
                 "r__3#db_access": "absent"},
    "GPT-4o":   {"r__1#network_egress": "present", "r__2#env_access": "absent",
                 "r__3#db_access": "absent"},
}
_R2B_D = {"r__1#network_egress", "r__2#env_access", "r__3#db_access"}

# 规则② WVA（C0 coequal）夹具（计划 §七.2/§七.3）：
#   r__1 = P/A/U + 第4家 P → W+=2,W-=1,D=3,ratio=2/3 → 采纳 present（规则① n_max=2 → 未决）；
#   r__2 = P/P/A + 第4家 A → W+=2,W-=2,D=4,ratio=1/2 → 未决（否决保留，不 overrule 多数）；
#   r__3 = 厚层 3/0(A) → thick-consensus（第 4 家免疫）。
_R2B_VOTES_WVA = {
    "DeepSeek": {"r__1#network_egress": "present", "r__2#env_access": "present",
                 "r__3#db_access": "absent"},
    "Claude":   {"r__1#network_egress": "absent",  "r__2#env_access": "present",
                 "r__3#db_access": "absent"},
    "GLM":      {"r__1#network_egress": "unsure",  "r__2#env_access": "absent",
                 "r__3#db_access": "absent"},
    "GPT-4o":   {"r__1#network_egress": "present", "r__2#env_access": "absent",
                 "r__3#db_access": "absent"},
}


def _mk_route2b_fixture() -> tuple[Path, Path]:
    """路线 2b 夹具：路线 2 夹具 + 第 3 个分歧（``r__1#network_egress``）+ dual 源码目录。

    在 ``_mk_route2_fixture()``（已含 2 项分歧）上追加第 3 分歧并跑 triage，故 **|D| = 3**；
    另建 ``dual/<repo>/main.py``（>600 字符，供加厚材料生效）。返回 ``(thin_dir, dual_dir)``。
    """
    thin = _mk_route2_fixture()
    patch = {"DeepSeek": "present", "Claude": "present", "GLM": "absent"}
    for name, lab in patch.items():
        rows = list(m2._read_csv(thin / f"label_{name}.csv"))
        for r in rows:
            if r["item_id"] == "r__1#network_egress":
                r["label"] = lab
        m2._write_csv(thin / f"label_{name}.csv", rows)
    m2.triage(thin)

    dual = Path(tempfile.mkdtemp(prefix="m2b_dual_"))
    body = "\n".join(f"def f{i}(x):\n    return x + {i}" for i in range(60)) + "\n"
    for rk in ("r__1", "r__2", "r__3", "r__4"):
        d = dual / rk
        d.mkdir(parents=True, exist_ok=True)
        (d / "main.py").write_text(body, encoding="utf-8")
    return thin, dual


def _write_thick_votes(thick: Path, votes: dict) -> None:
    """按 thick/key.csv 的 item_id 写厚层各家卡（模拟 llm 跑完后的成品）。"""
    ids = [r["item_id"] for r in m2._read_csv(thick / m2.KEY_CSV)]
    for model, ov in votes.items():
        m2._write_csv(thick / f"label_{model}.csv",
                      [{"item_id": it, "label": ov.get(it, ""), "note": ""} for it in ids])


def test_thick_sheet() -> None:
    thin, dual = _mk_route2b_fixture()
    thick = Path(tempfile.mkdtemp(prefix="m2b_thick_"))
    res = m2.thick_sheet(thin, dual, thick)
    check(res["n_items"] == 3, f"thick_sheet 只含 D=3 项（实得 {res['n_items']}）")
    check(res["maxlen"] == m2.THICK_MAXLEN, "thick_sheet 用加厚 maxlen")
    check(res["max_files"] == m2.THICK_MAX_FILES, "thick_sheet 用加厚 max_files")

    sp = thick / m2.SHEET_CSV
    check(sp.exists(), "thick_sheet 产 sheet.csv")
    srows = list(m2._read_csv(sp))
    check(len(srows) == 3, "thick sheet.csv 3 行")
    check({r["item_id"] for r in srows} == _R2B_D, "thick sheet 只含分歧项")
    check(all(r["snippet"] for r in srows), "thick sheet 带上下文")
    check(max(len(r["snippet"]) for r in srows) > m2.DEFAULT_MAXLEN,
          "thick 材料长于薄层上限（加厚生效）")
    krows = list(m2._read_csv(thick / m2.KEY_CSV))
    check(len(krows) == 3, "thick key.csv 3 行")
    check({r["category"] for r in krows} == {"network_egress", "env_access", "db_access"},
          "thick key 携带原类别")
    check((thick / m2.CODEBOOK_CSV).exists(), "thick_sheet 产 codebook.csv")
    for n in ("DeepSeek", "Claude", "GLM"):
        c = thick / f"label_{n}.csv"
        check(c.exists(), f"thick_sheet 产 label_{n}.csv")
        check(len(list(m2._read_csv(c))) == 3, f"label_{n}.csv 空卡 3 行")

    check(len(list(m2._read_csv(thin / m2.TRIAGE_CSV))) == 36, "薄层只读：triage 未被改写")

    try:
        m2.thick_sheet(thin, dual, thick)
        check(False, "thick_sheet() 应防覆盖")
    except SystemExit:
        check(True, "thick_sheet() 防覆盖")


def test_adjudicate4() -> None:
    thin, dual = _mk_route2b_fixture()
    thick = Path(tempfile.mkdtemp(prefix="m2b_adj4_"))
    m2.thick_sheet(thin, dual, thick)
    _write_thick_votes(thick, _R2B_VOTES)

    res = m2.adjudicate4(thick, "GPT-4o")
    check(res["n_thick_consensus"] == 1,
          f"厚层 3/0 → thick-consensus=1（实得 {res['n_thick_consensus']}）")
    check(res["n_adjudicated"] == 1,
          f"2/1 加第4家 → adj4-llm=1（实得 {res['n_adjudicated']}）")
    check(res["n_unresolved"] == 1,
          f"2/2 → unresolved=1（实得 {res['n_unresolved']}）")

    amap = {r["item_id"]: r for r in m2._read_csv(thick / m2.ADJUDICATION_CSV)}
    check(amap["r__3#db_access"]["label"] == "absent", "厚层 3/0 采纳 absent")
    check(amap["r__3#db_access"]["note"] == "thick-consensus", "3/0 来源 thick-consensus")
    check(amap["r__1#network_egress"]["label"] == "present", "2/1+第4家 present → 采纳")
    check(amap["r__1#network_egress"]["note"] == "adj4-llm", "第4家采纳来源 adj4-llm")
    check(amap["r__2#env_access"]["label"] == "", "2/1+第4家反向 → 未决留空")
    check(amap["r__2#env_access"]["note"] == "unresolved", "2/2 来源 unresolved")

    dp = thick / m2.ADJ4_CSV
    check(dp.exists(), "adjudicate4 产 adjudication4.csv 明细")
    dmap = {r["item_id"]: r for r in m2._read_csv(dp)}
    check(dmap["r__1#network_egress"]["llm_GPT-4o"] == "present", "明细带第4家票")
    check(dmap["r__1#network_egress"]["n_max"] == "3", "明细最高票数 n_max=3")
    check(dmap["r__2#env_access"]["n_max"] == "2", "2/2 最高票数 n_max=2")
    check(dmap["r__3#db_access"]["status"] == "thick-consensus", "明细带状态")

    try:
        m2.adjudicate4(thick, "GPT-4o")
        check(False, "adjudicate4() 应防覆盖裁决表")
    except SystemExit:
        check(True, "adjudicate4() 防覆盖裁决表")


def test_adjudicate4_wva() -> None:
    """规则② WVA（C0 coequal）：破 1:1:1 平局 / 否决保留 / 3-0 免疫 / rule1 对照 / 三旋钮。"""
    thin, dual = _mk_route2b_fixture()
    thick = Path(tempfile.mkdtemp(prefix="m2b_wva_"))
    m2.thick_sheet(thin, dual, thick)

    # (1) coequal 默认：破平局采纳 / 2:1 否决保留 / 3/0 免疫
    _write_thick_votes(thick, _R2B_VOTES_WVA)
    res = m2.adjudicate4(thick, "GPT-4o", force=True)
    check(res["n_thick_consensus"] == 1,
          f"WVA 3/0 第4家免疫=1（实得 {res['n_thick_consensus']}）")
    check(res["n_adjudicated"] == 1,
          f"WVA 破 1:1:1 平局采纳=1（实得 {res['n_adjudicated']}）")
    check(res["n_unresolved"] == 1,
          f"WVA 2:1 遇反向未决=1（实得 {res['n_unresolved']}）")
    amap = {r["item_id"]: r for r in m2._read_csv(thick / m2.ADJUDICATION_CSV)}
    check(amap["r__1#network_egress"]["label"] == "present", "coequal 破 1:1:1 → present")
    check(amap["r__1#network_egress"]["note"] == "adj4-llm", "破平局来源 adj4-llm")
    check(amap["r__2#env_access"]["label"] == "", "2:1 遇第4家反向 → 未决留空")
    check(amap["r__2#env_access"]["note"] == "unresolved", "否决保留（不 overrule 多数）")
    check(amap["r__3#db_access"]["note"] == "thick-consensus", "3/0 第4家免疫")

    dmap = {r["item_id"]: r for r in m2._read_csv(thick / m2.ADJ4_CSV)}
    check(dmap["r__1#network_egress"]["w_present"] == "2", "明细 w_present=2")
    check(dmap["r__1#network_egress"]["w_absent"] == "1", "明细 w_absent=1")
    check(dmap["r__1#network_egress"]["n_max"] == "2", "保留 n_max 兼容规则①")
    check(dmap["r__1#network_egress"]["status"] == "adj4-llm", "明细带状态")

    # (2) 规则① 对照：同一票型下 r__1（n_max=2<3）转未决
    _write_thick_votes(thick, _R2B_VOTES_WVA)
    r1 = m2.adjudicate4(thick, "GPT-4o", mode="rule1", force=True)
    check(r1["n_adjudicated"] == 0 and r1["n_unresolved"] == 2,
          f"rule1 对照：r__1 未决（adj={r1['n_adjudicated']} unres={r1['n_unresolved']}）")

    # (3) tau 旋钮：τ=3/4 → r__1 的 2/3 < 3/4 → 未决
    _write_thick_votes(thick, _R2B_VOTES_WVA)
    rt = m2.adjudicate4(thick, "GPT-4o", tau=0.75, force=True)
    check(rt["n_adjudicated"] == 0, f"τ=3/4 收紧 → r__1 未决（实得 {rt['n_adjudicated']}）")

    # (4) beta 旋钮：β=1 计入弃权 → D=4 → r__1 的 2/4 < 2/3 → 未决
    _write_thick_votes(thick, _R2B_VOTES_WVA)
    rb = m2.adjudicate4(thick, "GPT-4o", beta=1.0, force=True)
    check(rb["n_adjudicated"] == 0, f"β=1 计入弃权 → r__1 未决（实得 {rb['n_adjudicated']}）")

    # (5) weights 旋钮：给 Claude 加权重 → 打破 r__1 的 present 优势 → 未决
    _write_thick_votes(thick, _R2B_VOTES_WVA)
    m2.adjudicate4(thick, "GPT-4o", weights={"Claude": 3.0}, force=True)
    wmap = {r["item_id"]: r for r in m2._read_csv(thick / m2.ADJUDICATION_CSV)}
    check(wmap["r__1#network_egress"]["note"] == "unresolved",
          "加权（Claude=3）→ r__1 转未决")


def test_adjudicate4_missing_vote() -> None:
    """四家闸 fail-closed：Stage-2 缺第 4 家票 → 未决（**不得以 3 票冒充四家**）。"""
    thin, dual = _mk_route2b_fixture()
    thick = Path(tempfile.mkdtemp(prefix="m2b_miss_"))
    m2.thick_sheet(thin, dual, thick)
    # 三家重标：r__1=2/1 分歧、r__2=2/1 分歧、r__3=3/0 一致
    _write_thick_votes(thick, {n: _R2B_VOTES[n] for n in ("DeepSeek", "Claude", "GLM")})
    # 第 4 家卡只覆盖 r__2（缺 r__1/r__3），模拟残余子集漏项
    m2._write_csv(thick / "label_GPT-4o.csv",
                  [{"item_id": "r__2#env_access", "label": "absent", "note": ""}])

    res = m2.adjudicate4(thick, "GPT-4o")
    check(res["n_missing_vote"] >= 1,
          f"Stage-2 缺票计数≥1（实得 {res['n_missing_vote']}）")
    amap = {r["item_id"]: r for r in m2._read_csv(thick / m2.ADJUDICATION_CSV)}
    check(amap["r__1#network_egress"]["label"] == "",
          "缺第 4 家票 → r__1 未决（不静默按 3 票采纳）")
    check(amap["r__1#network_egress"]["note"] == "unresolved", "缺票来源 unresolved")
    check(amap["r__3#db_access"]["label"] == "absent", "3/0 免疫项不受缺票影响")
    check(amap["r__3#db_access"]["note"] == "thick-consensus", "3/0 仍 thick-consensus")
    check(res["n_thick_consensus"] == 1, "缺票不影响 3/0 免疫计数")

    # 第 4 家卡整份缺失 → 四家闸拒绝
    thick2 = Path(tempfile.mkdtemp(prefix="m2b_miss2_"))
    m2.thick_sheet(thin, dual, thick2)
    _write_thick_votes(thick2, {n: _R2B_VOTES[n] for n in ("DeepSeek", "Claude", "GLM")})
    try:
        m2.adjudicate4(thick2, "GPT-4o")
        check(False, "缺第 4 家卡应 SystemExit")
    except SystemExit:
        check(True, "缺第 4 家卡 → SystemExit")


def test_residual_sheet() -> None:
    """残余分歧子集：剔出厚层三家 3/0 一致项（第 4 家免疫），仅供第 4 家省 token。"""
    thin, dual = _mk_route2b_fixture()
    thick = Path(tempfile.mkdtemp(prefix="m2b_resid_"))
    m2.thick_sheet(thin, dual, thick)
    _write_thick_votes(thick, _R2B_VOTES)

    res = m2.residual_sheet(thick)
    check(res["total"] == 3, f"厚层共 3 项（实得 {res['total']}）")
    check(res["consensus"] == 1, f"3/0 免疫 1 项（实得 {res['consensus']}）")
    check(res["residual"] == 2, f"残余 2 项（实得 {res['residual']}）")

    rp = thick / m2.RESIDUAL_SHEET_CSV
    check(rp.exists(), "residual_sheet 产 sheet_residual.csv")
    rows = list(m2._read_csv(rp))
    check(len(rows) == 2, "sheet_residual.csv 2 行")
    check({r["item_id"] for r in rows} == {"r__1#network_egress", "r__2#env_access"},
          "残余只含仍分歧项（剔除 3/0 的 r__3）")
    check(all(r.get("snippet") for r in rows), "残余保留厚层上下文列")

    try:
        m2.residual_sheet(thick)
        check(False, "residual_sheet() 应防覆盖")
    except SystemExit:
        check(True, "residual_sheet() 防覆盖")

    empty = Path(tempfile.mkdtemp(prefix="m2b_resid_empty_"))
    m2._write_csv(empty / m2.SHEET_CSV, [{"item_id": "x#network_egress"}])
    try:
        m2.residual_sheet(empty)
        check(False, "缺三家卡应报错")
    except SystemExit:
        check(True, "缺三家卡 → SystemExit")


def test_cross_audit() -> None:
    thin, dual = _mk_route2b_fixture()
    thick = Path(tempfile.mkdtemp(prefix="m2b_cross_"))
    m2.thick_sheet(thin, dual, thick)
    _write_thick_votes(thick, _R2B_VOTES)
    m2.adjudicate4(thick, "GPT-4o")

    res = m2.cross_audit(thin, thick, thick, fraction=0.2, seed=m2.SEED_DEFAULT)
    check(res["n_thin_pool"] == 33, f"薄池=33（实得 {res['n_thin_pool']}）")
    check(res["n_thick_pool"] == 2, f"厚池=已采纳 2 项（实得 {res['n_thick_pool']}）")
    check(res["n_sampled"] == 7, f"抽样=7（实得 {res['n_sampled']}）")

    ap = thick / m2.AUDIT_B_CSV
    check(ap.exists(), "cross_audit 产 audit_b.csv")
    rows = list(m2._read_csv(ap))
    check(len(rows) == 7, "audit_b.csv 7 行")
    check(all("layer" in r for r in rows), "audit_b 带 layer 列")
    n_thin = sum(1 for r in rows if r["layer"] == "thin")
    n_thick = sum(1 for r in rows if r["layer"] == "thick")
    check(n_thin == 6 and n_thick == 1, f"层分配 薄6/厚1（实得 薄{n_thin}/厚{n_thick}）")
    check(all(r["snippet"] == "code" for r in rows if r["layer"] == "thin"),
          "薄层取薄材料")
    check(all(len(r["snippet"]) > m2.DEFAULT_MAXLEN for r in rows if r["layer"] == "thick"),
          "厚层取加厚材料")
    for h in m2.HUMANS_DEFAULT:
        c = thick / f"label_audit_b_{h}.csv"
        check(c.exists(), f"cross_audit 产 label_audit_b_{h}.csv")
        check(len(list(m2._read_csv(c))) == 7, f"label_audit_b_{h}.csv 空卡 7 行")

    ids1 = sorted(r["item_id"] for r in rows)
    try:
        m2.cross_audit(thin, thick, thick, fraction=0.2, seed=m2.SEED_DEFAULT)
        check(False, "cross_audit() 应防覆盖盲审包")
    except SystemExit:
        check(True, "cross_audit() 防覆盖盲审包")
    m2.cross_audit(thin, thick, thick, fraction=0.2, seed=m2.SEED_DEFAULT, force=True)
    ids2 = sorted(r["item_id"] for r in m2._read_csv(ap))
    check(ids1 == ids2, "cross_audit 同种子可复现")


def test_route2b_gold() -> None:
    thin, dual = _mk_route2b_fixture()
    thick = Path(tempfile.mkdtemp(prefix="m2b_gold_"))
    m2.thick_sheet(thin, dual, thick)
    _write_thick_votes(thick, _R2B_VOTES)
    m2.adjudicate4(thick, "GPT-4o")
    m2.cross_audit(thin, thick, thick, fraction=0.2, seed=m2.SEED_DEFAULT)

    rows = list(m2._read_csv(thick / m2.AUDIT_B_CSV))
    me, other = [], []
    for i, r in enumerate(rows):
        me.append({"item_id": r["item_id"],
                   "label": "present" if i == 0 else "absent", "note": ""})
        other.append({"item_id": r["item_id"], "label": "absent", "note": ""})
    m2._write_csv(thick / "label_audit_b_Me.csv", me)
    m2._write_csv(thick / "label_audit_b_other.csv", other)
    disagree = rows[0]["item_id"]

    res = m2.route2b_gold(thin, thick, thick)
    gp = thick / "label_gold.csv"
    check(gp.exists(), "route2b_gold 产 label_gold.csv")
    gm = {r["item_id"]: r for r in m2._read_csv(gp)}
    check(len(gm) == 36, f"label_gold 36 行（实得 {len(gm)}）")
    src = Counter(r["note"] for r in gm.values())

    check(src["audit-b-human"] == 6, f"盲审双人共识=6（实得 {src['audit-b-human']}）")
    check(src["audit-b-unresolved"] == 1, "盲审双人分歧=1")
    check(gm[disagree]["label"] == "", "盲审分歧项留空")
    check(src["unresolved"] == 1, "厚层未决=1（r__2#env_access）")

    audited = {r["item_id"] for r in rows}
    tmap = {r["item_id"]: r for r in m2._read_csv(thin / m2.TRIAGE_CSV)}
    thin_pool = {it for it, r in tmap.items() if r["status"] == "consensus"}
    adjmap = {r["item_id"]: r["note"]
              for r in m2._read_csv(thick / m2.ADJUDICATION_CSV) if r["label"]}
    check(src["thin-consensus"] == len(thin_pool - audited), "薄层一致数 = 池 - 盲审命中")
    check(src["thick-consensus"] == sum(1 for it, s in adjmap.items()
          if s == "thick-consensus" and it not in audited), "厚层 3/0 数正确")
    check(src["adj4-llm"] == sum(1 for it, s in adjmap.items()
          if s == "adj4-llm" and it not in audited), "第4家采纳数正确")
    check(sum(src.values()) == 36, "来源合计=36")
    sample_it = sorted(thin_pool - audited)[0]
    check(gm[sample_it]["note"] == "thin-consensus", "非分歧项来源 thin-consensus")
    check(gm[sample_it]["label"] == tmap[sample_it]["llm_label"], "非分歧项沿用薄层共识")

    rj, rc = thick / m2.GOLD_REPORT_JSON, thick / m2.GOLD_REPORT_CSV
    check(rj.exists(), "route2b_gold 产 gold_report.json")
    check(rc.exists(), "route2b_gold 产 gold_report.csv")
    rep = json.loads(rj.read_text(encoding="utf-8"))
    check(rep["n_items"] == 36, "报告 n_items=36")
    check(set(rep["layers"]) == {"thin", "thick"}, "报告按材料层 thin/thick 分层")
    check(set(rep["sensitivity"]) == {"S-a", "S-b", "S-c"}, "报告含敏感性 S-a/S-b/S-c")
    check(res["n_items"] == 36, "route2b_gold 返回 n_items=36")

    try:
        m2.route2b_gold(thin, thick, thick)
        check(False, "route2b_gold() 应防覆盖金标")
    except SystemExit:
        check(True, "route2b_gold() 防覆盖金标")


def test_route2b_score() -> None:
    thin, dual = _mk_route2b_fixture()
    thick = Path(tempfile.mkdtemp(prefix="m2b_cls_"))
    m2.thick_sheet(thin, dual, thick)
    _write_thick_votes(thick, _R2B_VOTES)
    m2.adjudicate4(thick, "GPT-4o")
    m2.cross_audit(thin, thick, thick, fraction=0.2, seed=m2.SEED_DEFAULT)
    rows = list(m2._read_csv(thick / m2.AUDIT_B_CSV))
    me = [{"item_id": r["item_id"],
           "label": "present" if i == 0 else "absent", "note": ""}
          for i, r in enumerate(rows)]
    other = [{"item_id": r["item_id"], "label": "absent", "note": ""} for r in rows]
    m2._write_csv(thick / "label_audit_b_Me.csv", me)
    m2._write_csv(thick / "label_audit_b_other.csv", other)
    m2.route2b_gold(thin, thick, thick)

    res = m2.route2b_score(thin, thick, thick)
    cp = thick / m2.GOLD_CLASS_CSV
    check(cp.exists(), "route2b_score 产 gold_class_report.csv")
    crow = list(m2._read_csv(cp))
    check(set(res["layers"]) == {"thin", "thick"}, "分层键 thin/thick（不合并）")
    check(any(r["layer"] == "thin" for r in crow), "报告含薄层行")
    check(any(r["layer"] == "thick" for r in crow), "报告含厚层行")
    check(sum(1 for r in crow if r["category"] == "__overall__") == 2,
          "每层各一行 __overall__")
    check(all("recall_lo" in r and "precision_lo" in r for r in crow),
          "逐类行含 Wilson CI 列")

    # 逐层 overall 计数/recall 与 gold_report.json 对齐（同口径）
    rep = json.loads((thick / m2.GOLD_REPORT_JSON).read_text(encoding="utf-8"))
    for layer in ("thin", "thick"):
        ov = res["layers"][layer]["overall"]
        ref = rep["layers"][layer]
        check(ov["tp"] == ref["tp"] and ov["fp"] == ref["fp"]
              and ov["fn"] == ref["fn"] and ov["tn"] == ref["tn"],
              f"{layer} 层 overall 计数对齐 gold_report")
        if ref["recall"] is not None:
            check(abs(ov["recall"] - ref["recall"]) < 1e-9, f"{layer} 层 recall 对齐")
        else:
            check(ov["recall"] != ov["recall"], f"{layer} 层 recall 均为 nan（无正例）")

    # 逐类 Wilson CI 合法（分母>0 时）
    for r in crow:
        if r["category"] == "__overall__" or not r["recall_lo"]:
            continue
        check(0.0 <= float(r["recall_lo"]) <= float(r["recall_hi"]) <= 1.0,
              "recall Wilson CI ∈ [0,1]")
        check(0.0 <= float(r["precision_lo"]) <= float(r["precision_hi"]) <= 1.0,
              "precision Wilson CI ∈ [0,1]")

    check(res["gate"]["verdict"] in ("pass", "fail-closed"), "gate verdict 合法")
    check(res["gate"]["recall_min_critical"] == m2.RECALL_MIN_CRITICAL,
          "gate 安全关键类阈值=0.90")

    # 缺报告 → fail-closed 报错
    empty = Path(tempfile.mkdtemp(prefix="m2b_cls_empty_"))
    try:
        m2.route2b_score(thin, empty, empty)
        check(False, "缺 gold_report.csv 应报错")
    except SystemExit:
        check(True, "缺 gold_report.csv 报错（先跑 route2b-gold）")


def test_route2b_report() -> None:
    thin, dual = _mk_route2b_fixture()
    thick = Path(tempfile.mkdtemp(prefix="m2b_rep_"))
    m2.thick_sheet(thin, dual, thick)
    _write_thick_votes(thick, _R2B_VOTES)
    m2.adjudicate4(thick, "GPT-4o")
    m2.cross_audit(thin, thick, thick, fraction=0.2, seed=m2.SEED_DEFAULT)
    rows = list(m2._read_csv(thick / m2.AUDIT_B_CSV))
    me = [{"item_id": r["item_id"],
           "label": "present" if i == 0 else "absent", "note": ""}
          for i, r in enumerate(rows)]
    other = [{"item_id": r["item_id"], "label": "absent", "note": ""} for r in rows]
    m2._write_csv(thick / "label_audit_b_Me.csv", me)
    m2._write_csv(thick / "label_audit_b_other.csv", other)
    m2.route2b_gold(thin, thick, thick)
    m2.route2b_score(thin, thick, thick)

    res = m2.route2b_report(thick, thick)
    rp = thick / m2.ROUTE2B_REPORT_JSON
    check(rp.exists(), "route2b_report 产 route2b_report.json")
    rep = json.loads(rp.read_text(encoding="utf-8"))
    check(rep["experiment"] == "M2" and rep.get("route") == "2b",
          "experiment=M2 / route=2b")
    check(set(rep["layers"]) == {"thin", "thick"}, "分层键 thin/thick（不合并）")
    check(rep["fail_closed_accepted"] is True, "默认声明接受 fail-closed")
    check(set(rep["safety_critical"]) == set(m2.DANGER_CATEGORIES),
          "safety_critical == 9 类危险面")
    check(res["path"] == rp, "返回落点 == route2b_report.json")

    # 与 route2b_score 产物（gold_class_report.csv）逐层逐类对齐
    cls = {(r["layer"], r["category"]): r
           for r in m2._read_csv(thick / m2.GOLD_CLASS_CSV)
           if r["category"] != "__overall__"}
    n_cls = 0
    for layer in ("thin", "thick"):
        lv = rep["layers"][layer]
        check("kappa_hh" in lv, f"{layer} 层含 kappa_hh")
        for cat, m in lv["per_class"].items():
            src = cls[(layer, cat)]
            n_cls += 1
            check(m["gate"] == src["gate"], f"{layer}/{cat} gate 对齐")
            if src["recall"]:
                check(abs(m["recall"] - float(src["recall"])) < 1e-9,
                      f"{layer}/{cat} recall 对齐")
            else:
                check(m["recall"] is None,
                      f"{layer}/{cat} no_denominator → recall=None")
    check(n_cls == len(cls), f"逐类覆盖与逐类报告一致（{n_cls}/{len(cls)}）")

    # 缺逐类报告 → 报错
    empty = Path(tempfile.mkdtemp(prefix="m2b_rep_empty_"))
    try:
        m2.route2b_report(empty, empty)
        check(False, "缺 gold_class_report.csv 应报错")
    except SystemExit:
        check(True, "缺逐类报告报错（先跑 route2b-score）")


# --- R2：M2-R2 可估计性补做（A 线零标注 + C-lite 补样）--------------------
# 计划：docs/superpowers/plans/2026-10-11-m2r2-estimability-prereg.md
# 落点：D:\Data\prov-mcp\m2r2\（m2 / m2b 只读）


def _r2_key_row(it: str, agcm) -> dict:
    """R2 夹具用 key 行（列同 thick_sheet 的 key.csv）。"""
    repo, _, cat = it.partition("#")
    return {"item_id": it, "repo_key": repo, "category": cat,
            "stratum": "0×server_only", "sev_bin": "0", "repo_type": "server_only",
            "lang_group": "js/ts", "source_tier": "1",
            "agcm_present": agcm, "declared": "no_decl", "n_danger": 0}


def test_gold_error_audit() -> None:
    """R2 A 线：LLM 侧 vs 跨层盲审双人共识的逐(层,类)误差率 + Wilson CI。"""
    thin = Path(tempfile.mkdtemp(prefix="m2r2_gea_thin_"))
    thick = Path(tempfile.mkdtemp(prefix="m2r2_gea_thick_"))
    out = Path(tempfile.mkdtemp(prefix="m2r2_gea_out_"))

    audit = [
        {"item_id": "t1#shell_true", "layer": "thin", "repo_key": "t1",
         "category": "shell_true", "category_zh": "", "file_list": "", "snippet": ""},
        {"item_id": "t2#shell_true", "layer": "thin", "repo_key": "t2",
         "category": "shell_true", "category_zh": "", "file_list": "", "snippet": ""},
        {"item_id": "t3#db_access", "layer": "thin", "repo_key": "t3",
         "category": "db_access", "category_zh": "", "file_list": "", "snippet": ""},
        {"item_id": "k1#shell_true", "layer": "thick", "repo_key": "k1",
         "category": "shell_true", "category_zh": "", "file_list": "", "snippet": ""},
    ]
    m2._write_csv(thick / m2.AUDIT_B_CSV, audit)
    m2._write_csv(thin / m2.TRIAGE_CSV, [
        {"item_id": "t1#shell_true", "repo_key": "t1", "category": "shell_true",
         "category_zh": "", "llm_label": "present", "n_agree": "3", "status": "consensus"},
        {"item_id": "t2#shell_true", "repo_key": "t2", "category": "shell_true",
         "category_zh": "", "llm_label": "absent", "n_agree": "3", "status": "consensus"},
        {"item_id": "t3#db_access", "repo_key": "t3", "category": "db_access",
         "category_zh": "", "llm_label": "present", "n_agree": "3", "status": "consensus"},
    ])
    m2._write_csv(thick / m2.ADJUDICATION_CSV, [
        {"item_id": "k1#shell_true", "label": "present", "note": m2.SRC_THICK},
    ])
    m2._write_csv(thick / "label_audit_b_Me.csv", [
        {"item_id": "t1#shell_true", "label": "present", "note": ""},
        {"item_id": "t2#shell_true", "label": "absent", "note": ""},
        {"item_id": "t3#db_access", "label": "present", "note": ""},
        {"item_id": "k1#shell_true", "label": "absent", "note": ""},
    ])
    m2._write_csv(thick / "label_audit_b_other.csv", [
        {"item_id": "t1#shell_true", "label": "present", "note": ""},
        {"item_id": "t2#shell_true", "label": "absent", "note": ""},
        {"item_id": "t3#db_access", "label": "absent", "note": ""},  # 与 Me 分歧 → 未决
        {"item_id": "k1#shell_true", "label": "absent", "note": ""},
    ])

    res = m2.gold_error_audit(thin, thick, out)
    p = out / m2.GOLD_ERROR_CSV
    check(p.exists(), "gold_error_audit 产 gold_error_audit.csv")
    rows = {(r["layer"], r["category"]): r for r in m2._read_csv(p)}
    check(("thin", "shell_true") in rows, "含 (thin, shell_true) 行")
    check(("thick", "shell_true") in rows, "含 (thick, shell_true) 行")
    check(("thin", "db_access") in rows, "含 (thin, db_access) 行")

    r_ts = rows[("thin", "shell_true")]
    check(r_ts["n_audit"] == "2", f"thin/shell_true n_audit=2（实得 {r_ts['n_audit']}）")
    check(r_ts["n_compared"] == "2", "thin/shell_true n_compared=2")
    check(r_ts["n_mismatch"] == "0", "thin/shell_true n_mismatch=0")
    check(abs(float(r_ts["error_rate"]) - 0.0) < 1e-9, "thin/shell_true 误差率=0")

    r_ks = rows[("thick", "shell_true")]
    check(r_ks["n_compared"] == "1" and r_ks["n_mismatch"] == "1",
          "thick/shell_true 1 比对 1 不一致")
    check(abs(float(r_ks["error_rate"]) - 1.0) < 1e-9, "thick/shell_true 误差率=1.0")

    r_td = rows[("thin", "db_access")]
    check(r_td["n_human_unresolved"] == "1", "thin/db_access 人分歧=1")
    check(r_td["n_compared"] == "0", "thin/db_access 无误比对（fail-closed 剔除）")
    check(r_td["error_rate"] == "", "thin/db_access 误差率留空（分母 0）")
    check("n_llm_unresolved" in r_td, "逐行含 n_llm_unresolved 列")

    ov_thin = rows[("thin", "__overall__")]
    ov_thick = rows[("thick", "__overall__")]
    check(ov_thin["n_audit"] == "3" and ov_thin["n_mismatch"] == "0",
          "薄层 __overall__ n_audit=3 / mismatch=0")
    check(ov_thick["n_audit"] == "1" and ov_thick["n_mismatch"] == "1",
          "厚层 __overall__ n_audit=1 / mismatch=1")
    check(res["n_audit"] == 4, f"返回 n_audit=4（实得 {res['n_audit']}）")


def test_operating_points() -> None:
    """R2 A 线：AGCM×LLM 一致率联合门槛 —— θ 21 点工作点曲线（零标注、可复算）。"""
    thick = Path(tempfile.mkdtemp(prefix="m2r2_op_"))
    out = Path(tempfile.mkdtemp(prefix="m2r2_op_out_"))

    # P1(agcm1,3/3) P2(agcm1,2/3) P3(agcm0,3/3) | N1(agcm1,0/3) N2(agcm1,0/3) N3(agcm0,0/3)
    spec = {
        "r__p1#shell_true": (1, "present", ["present", "present", "present"]),
        "r__p2#shell_true": (1, "present", ["present", "present", "absent"]),
        "r__p3#shell_true": (0, "present", ["present", "present", "present"]),
        "r__n1#shell_true": (1, "absent", ["absent", "absent", "absent"]),
        "r__n2#shell_true": (1, "absent", ["absent", "absent", "absent"]),
        "r__n3#shell_true": (0, "absent", ["absent", "absent", "absent"]),
    }
    m2._write_csv(thick / m2.KEY_CSV, [_r2_key_row(it, a) for it, (a, _, _) in spec.items()])
    m2._write_csv(thick / m2.GOLD_REPORT_CSV, [
        {"item_id": it, "layer": "thick", "source": m2.SRC_THICK, "gold": g,
         "agcm": "1" if a else "0"}
        for it, (a, g, _) in spec.items()])
    for i, name in enumerate(m2.LLM_DEFAULT):
        m2._write_csv(thick / f"label_{name}.csv", [
            {"item_id": it, "label": v[i], "note": ""}
            for it, (_, _, v) in spec.items()])

    res = m2.operating_points(thick, out, layer="thick", holdout=1.0,
                              seed=m2.SEED_DEFAULT)
    op = out / m2.OPERATING_POINTS_CSV
    check(op.exists(), "operating_points 产 operating_points.csv")
    rows = list(m2._read_csv(op))
    check(len(rows) == 21, f"θ 21 点（实得 {len(rows)}）")
    by_theta = {round(float(r["theta"]), 6): r for r in rows}

    r0 = by_theta[0.0]
    check((r0["tp"], r0["fp"], r0["fn"], r0["tn"]) == ("2", "2", "1", "1"),
          f"θ=0 混淆 (2,2,1,1)（实得 {r0['tp']},{r0['fp']},{r0['fn']},{r0['tn']}）")
    check(abs(float(r0["precision"]) - 0.5) < 1e-9, "θ=0 precision=0.5")

    r5 = by_theta[0.05]
    check((r5["tp"], r5["fp"], r5["fn"], r5["tn"]) == ("2", "0", "1", "3"),
          f"θ=0.05 混淆 (2,0,1,3)（实得 {r5['tp']},{r5['fp']},{r5['fn']},{r5['tn']}）")
    check(abs(float(r5["precision"]) - 1.0) < 1e-9, "θ=0.05 precision=1.0")

    r70 = by_theta[0.70]
    check(r70["tp"] == "1" and r70["fn"] == "2", "θ=0.70 tp=1 / fn=2")

    check(r0["workpoint"] == "recall-first" and r5["workpoint"] == "recall-first",
          "θ≤1/3 → recall-first")
    check(by_theta[0.50]["workpoint"] == "balanced", "1/3<θ≤2/3 → balanced")
    check(r70["workpoint"] == "precision-first", "θ>2/3 → precision-first")

    items = list(m2._read_csv(out / m2.OP_ITEMS_CSV))
    check(len(items) == 6, f"伴生明细 6 项（实得 {len(items)}）")
    imap = {r["item_id"]: r for r in items}
    check(imap["r__p2#shell_true"]["n_present"] == "2", "p2 三家 present 票=2")
    check(imap["r__p2#shell_true"]["p_llm"] != "", "p2 落盘 p_llm（可复算）")

    sc = out / m2.SPLIT_CSV
    check(sc.exists(), "产 split.csv")
    sh = out / m2.SPLIT_SHA
    check(sh.exists(), "产 split.sha256")
    check(m2.sha256_file(sc) in sh.read_text(encoding="utf-8"),
          "split.sha256 与 split.csv 一致")

    # 同种子可复现（用 holdout=0.5 抽真随机分割，两次独立目录比对）
    d1 = Path(tempfile.mkdtemp(prefix="m2r2_op_r1_"))
    d2 = Path(tempfile.mkdtemp(prefix="m2r2_op_r2_"))
    m2.operating_points(thick, d1, layer="thick", holdout=0.5, seed=m2.SEED_DEFAULT)
    m2.operating_points(thick, d2, layer="thick", holdout=0.5, seed=m2.SEED_DEFAULT)
    check(m2.sha256_file(d1 / m2.SPLIT_CSV) == m2.sha256_file(d2 / m2.SPLIT_CSV),
          "holdout=0.5 同种子 split 可复现")
    check(res["n_test"] == 6, f"holdout=1.0 → 测试集 6 项（实得 {res['n_test']}）")
    check(res["layer"] == "thick", "返回 layer=thick")


def test_enrich_sample() -> None:
    """R2 C-lite：稀有类补样候选池 = AGCM 判正 ∪ 任一 LLM present ∪ adj4 采纳。"""
    thin = Path(tempfile.mkdtemp(prefix="m2r2_en_thin_"))
    thick = Path(tempfile.mkdtemp(prefix="m2r2_en_thick_"))
    out = Path(tempfile.mkdtemp(prefix="m2r2_en_out_"))

    m2._write_csv(thin / m2.KEY_CSV, [
        _r2_key_row("e1#shell_true", 1),
        _r2_key_row("e2#shell_true", 0),
        _r2_key_row("e3#shell_true", 0)])
    m2._write_csv(thin / m2.SHEET_CSV, [
        {"item_id": it, "repo_key": it.partition("#")[0], "category": "shell_true",
         "category_zh": "", "file_list": "a.py", "snippet": "code"}
        for it in ("e1#shell_true", "e2#shell_true", "e3#shell_true")])
    for name in m2.LLM_DEFAULT:
        m2._write_csv(thin / f"label_{name}.csv", [
            {"item_id": "e1#shell_true", "label": "absent", "note": ""},
            {"item_id": "e2#shell_true",
             "label": "present" if name == "DeepSeek" else "absent", "note": ""},
            {"item_id": "e3#shell_true", "label": "absent", "note": ""}])

    res = m2.enrich_sample(thin, thick, out, targets=[("thin", "shell_true")])
    p = out / m2.ENRICH_SHEET_CSV
    check(p.exists(), "enrich_sample 产 enrich_sheet.csv")
    rows = list(m2._read_csv(p))
    check(len(rows) == 2, f"候选池=2（agcm 正 e1 ∪ LLM present e2）（实得 {len(rows)}）")
    check(all("layer" in r for r in rows), "enrich_sheet 带 layer 列")
    check({r["item_id"] for r in rows} == {"e1#shell_true", "e2#shell_true"},
          "候选 = e1(agcm 正) + e2(LLM present)，排除 e3")
    check(all(r["layer"] == "thin" for r in rows), "候选 layer=thin")
    check("label" not in rows[0] and "agcm" not in rows[0],
          "盲审材料不泄露 LLM/AGCM 标签")
    for h in m2.HUMANS_DEFAULT:
        c = out / f"label_enrich_{h}.csv"
        check(c.exists(), f"enrich_sample 产 label_enrich_{h}.csv")
        lr = list(m2._read_csv(c))
        check(len(lr) == 2 and all(x["label"] == "" for x in lr), f"label_enrich_{h} 空卡")
    check(res["n_items"] == 2, "返回 n_items=2")


def test_post_strat() -> None:
    """R2 C-lite：逐类 P/R **两套口径分列** —— 主=条件口径（给定候选池，不加权）；
    参考=全总体加权（``pop_*``，不成立）。缺失总体层 fail-closed。"""
    thin = Path(tempfile.mkdtemp(prefix="m2r2_ps_thin_"))
    thick = Path(tempfile.mkdtemp(prefix="m2r2_ps_thick_"))
    out = Path(tempfile.mkdtemp(prefix="m2r2_ps_out_"))

    # 薄层：shell_true 总体 agcm1×2 + agcm0×2（全采，两口径同值）；
    #       fs_destructive 总体 agcm1×3 + agcm0×1，仅采 2（1 正 1 负）→ 两口径必然不同
    m2._write_csv(thin / m2.KEY_CSV, [
        _r2_key_row("s1#shell_true", 1), _r2_key_row("s2#shell_true", 1),
        _r2_key_row("s3#shell_true", 0), _r2_key_row("s4#shell_true", 0),
        _r2_key_row("f1#fs_destructive", 1), _r2_key_row("f2#fs_destructive", 1),
        _r2_key_row("f3#fs_destructive", 1), _r2_key_row("f4#fs_destructive", 0)])
    # 厚层 db_access 总体：agcm1×1（有样）+ agcm0×1（无样 → 缺失层）
    m2._write_csv(thick / m2.KEY_CSV, [
        _r2_key_row("d1#db_access", 1), _r2_key_row("d2#db_access", 0)])

    def _er(it: str, layer: str) -> dict:
        repo, _, cat = it.partition("#")
        return {"item_id": it, "layer": layer, "repo_key": repo,
                "category": cat, "category_zh": "", "file_list": "", "snippet": ""}

    m2._write_csv(out / m2.ENRICH_SHEET_CSV, [
        _er("s1#shell_true", "thin"), _er("s2#shell_true", "thin"),
        _er("s3#shell_true", "thin"), _er("s4#shell_true", "thin"),
        _er("f1#fs_destructive", "thin"), _er("f4#fs_destructive", "thin"),
        _er("d1#db_access", "thick")])
    card = [
        {"item_id": "s1#shell_true", "label": "present", "note": ""},
        {"item_id": "s2#shell_true", "label": "absent", "note": ""},
        {"item_id": "s3#shell_true", "label": "present", "note": ""},
        {"item_id": "s4#shell_true", "label": "absent", "note": ""},
        {"item_id": "f1#fs_destructive", "label": "present", "note": ""},
        {"item_id": "f4#fs_destructive", "label": "present", "note": ""},
        {"item_id": "d1#db_access", "label": "present", "note": ""}]
    for h in m2.HUMANS_DEFAULT:
        m2._write_csv(out / f"label_enrich_{h}.csv", card)

    res = m2.post_strat(thin, thick, out)
    p = out / m2.POST_STRAT_CSV
    check(p.exists(), "post_strat 产 post_strat_report.csv")
    rows = {(r["layer"], r["category"]): r for r in m2._read_csv(p)}

    # --- 主口径：条件口径（给定候选池，不加权）---
    ts = rows[("thin", "shell_true")]
    check(ts["n_pop"] == "4", f"thin/shell_true n_pop=4（实得 {ts['n_pop']}）")
    check(ts["n_sampled"] == "4", "thin/shell_true n_sampled=4")
    check(ts["n_gold"] == "4", "thin/shell_true n_gold=4")
    check(ts["n_undecided"] == "0", "thin/shell_true n_undecided=0")
    check(ts["tp"] == "1" and ts["fp"] == "1" and ts["fn"] == "1" and ts["tn"] == "1",
          "thin/shell_true 条件计数 tp/fp/fn/tn=1/1/1/1")
    check(abs(float(ts["precision"]) - 0.5) < 1e-9, "thin/shell_true 条件 precision=0.5")
    check(abs(float(ts["recall"]) - 0.5) < 1e-9, "thin/shell_true 条件 recall=0.5")

    # --- 判别性：条件口径 ≠ 全总体加权（fs_destructive 层采样比≠总体比）---
    tf = rows[("thin", "fs_destructive")]
    check(abs(float(tf["precision"]) - 1.0) < 1e-9, "fs_destructive 条件 precision=1.0")
    check(abs(float(tf["recall"]) - 0.5) < 1e-9, "fs_destructive 条件 recall=0.5")
    check(abs(float(tf["pop_precision"]) - 1.0) < 1e-9, "fs_destructive 参考加权 precision=1.0")
    check(abs(float(tf["pop_recall"]) - 0.75) < 1e-9,
          f"fs_destructive 参考加权 recall=0.75（实得 {tf['pop_recall']}）")
    check(tf["recall"] != tf["pop_recall"], "两口径分列且数值不同（条件 0.5 vs 加权 0.75）")
    check("cond" in tf.get("estimand", ""), "带 estimand 标注")

    # --- fail-closed（总体层无采样）---
    td = rows[("thick", "db_access")]
    check(td["n_strata_missing"] == "1", "thick/db_access 缺失 1 层")
    check(td["gate"] == "incomplete", "缺失层 → gate=incomplete（fail-closed）")
    check(res["n_incomplete"] == 1, "返回 n_incomplete=1")


def test_consensus_gold() -> None:
    """方向 D 聚合：双人（min_votes=2）逐位等价现状；三人多数表决 + 无多数 fail-closed。"""
    g = m2._consensus_gold
    # 双人：等价「全一致且非-unsure」
    check(g(["present", "present"]) == "present", "双双 present → present")
    check(g(["absent", "absent"]) == "absent", "双双 absent → absent")
    check(g(["present", "absent"]) is None, "硬冲突 → None")
    check(g(["present", "unsure"]) is None, "一人 unsure → None")
    check(g(["unsure", "unsure"]) is None, "双 unsure → None")
    check(g(["present", ""]) is None, "空标签不构成票 → None")
    # 三人：多数表决（严格多数 + 票数≥2）
    check(g(["present", "present", "absent"]) == "present", "3人 2:1 → present")
    check(g(["absent", "absent", "present"]) == "absent", "3人 1:2 → absent")
    check(g(["present", "absent", "unsure"]) is None, "3人 1:1 + unsure → 无多数 fail-closed")
    check(g(["present", "present", "unsure"]) == "present", "3人 2 present + unsure → present")
    check(g(["present", "absent", "absent"]) == "absent", "3人 1:2 → absent")


def test_post_strat_min_votes() -> None:
    """方向 D：第 3 人多数表决把争议项回收进分母（min_votes 默认不改现有行为）。"""
    thin = Path(tempfile.mkdtemp(prefix="m2r2_ps3_thin_"))
    thick = Path(tempfile.mkdtemp(prefix="m2r2_ps3_thick_"))
    out = Path(tempfile.mkdtemp(prefix="m2r2_ps3_out_"))

    m2._write_csv(thin / m2.KEY_CSV, [
        _r2_key_row("s1#shell_true", 1), _r2_key_row("s2#shell_true", 1)])

    def _er(it: str) -> dict:
        repo, _, cat = it.partition("#")
        return {"item_id": it, "layer": "thin", "repo_key": repo, "category": cat,
                "category_zh": "", "file_list": "", "snippet": ""}

    m2._write_csv(out / m2.ENRICH_SHEET_CSV, [_er("s1#shell_true"), _er("s2#shell_true")])
    # 双人：s1 硬冲突，s2 一致 present
    m2._write_csv(out / "label_enrich_Me.csv", [
        {"item_id": "s1#shell_true", "label": "present", "note": ""},
        {"item_id": "s2#shell_true", "label": "present", "note": ""}])
    m2._write_csv(out / "label_enrich_other.csv", [
        {"item_id": "s1#shell_true", "label": "absent", "note": ""},
        {"item_id": "s2#shell_true", "label": "present", "note": ""}])

    m2.post_strat(thin, thick, out)
    row = {r["category"]: r for r in m2._read_csv(out / m2.POST_STRAT_CSV)}["shell_true"]
    check(row["n_undecided"] == "1", f"无共识 s1 → n_undecided=1（实得 {row['n_undecided']}）")
    check(row["n_gold"] == "1", "仅 s2 入分母")

    # 加第 3 人裁决 s1=present → 多数 2:1
    m2._write_csv(out / "label_enrich_third.csv", [
        {"item_id": "s1#shell_true", "label": "present", "note": ""}])
    m2.post_strat(thin, thick, out, humans=("Me", "other", "third"))
    row = {r["category"]: r for r in m2._read_csv(out / m2.POST_STRAT_CSV)}["shell_true"]
    check(row["n_undecided"] == "0", f"第 3 人裁决后 n_undecided=0（实得 {row['n_undecided']}）")
    check(row["n_gold"] == "2", "争议项已回收进分母")


def test_enrich_disputes() -> None:
    """方向 D：提取争议项（跨层重复取 thick）→ 产 enrich_disputes.csv + 第 3 人空卡。"""
    out = Path(tempfile.mkdtemp(prefix="m2r2_ed_out_"))

    def _er(it: str, layer: str) -> dict:
        repo, _, cat = it.partition("#")
        return {"item_id": it, "layer": layer, "repo_key": repo, "category": cat,
                "category_zh": "", "file_list": "", "snippet": layer}

    m2._write_csv(out / m2.ENRICH_SHEET_CSV, [
        _er("a#arbitrary_code", "thin"), _er("a#arbitrary_code", "thick"),  # 跨层争议
        _er("b#shell_true", "thin"),   # 一致 present → 非争议
        _er("c#db_access", "thin")])   # 含 unsure → 争议
    m2._write_csv(out / "label_enrich_Me.csv", [
        {"item_id": "a#arbitrary_code", "label": "present", "note": ""},
        {"item_id": "b#shell_true", "label": "present", "note": ""},
        {"item_id": "c#db_access", "label": "present", "note": ""}])
    m2._write_csv(out / "label_enrich_other.csv", [
        {"item_id": "a#arbitrary_code", "label": "absent", "note": ""},
        {"item_id": "b#shell_true", "label": "present", "note": ""},
        {"item_id": "c#db_access", "label": "unsure", "note": ""}])

    res = m2.enrich_disputes(out)
    p = out / m2.ENRICH_DISPUTES_CSV
    check(p.exists(), "enrich_disputes 产 enrich_disputes.csv")
    rows = list(m2._read_csv(p))
    ids = {r["item_id"] for r in rows}
    check(ids == {"a#arbitrary_code", "c#db_access"}, f"仅争议项（实得 {ids}）")
    check(len(rows) == 2, "跨层重复项去重为 1 行")
    a = [r for r in rows if r["item_id"] == "a#arbitrary_code"][0]
    check(a["layer"] == "thick", "跨层重复取 thick 行材料")
    check(res["n_disputed"] == 2, "返回 n_disputed=2")
    card = out / "label_enrich_third.csv"
    check(card.exists(), "产第 3 人空卡 label_enrich_third.csv")
    lr = list(m2._read_csv(card))
    check(len(lr) == 2 and all(x["label"] == "" for x in lr), "第 3 人空卡 2 行且全空")


def test_enrich_supp() -> None:
    """方向 B（预注册 §十）：仅对**缺层**（总体非空但 0 采样）补样，产独立 supp sheet + 补样卡。"""
    thin = Path(tempfile.mkdtemp(prefix="m2r2_es_thin_"))
    thick = Path(tempfile.mkdtemp(prefix="m2r2_es_thick_"))
    out = Path(tempfile.mkdtemp(prefix="m2r2_es_out_"))

    # thin/unsafe_deserialize：av=True×2（已采样）+ av=False×2（缺层）；
    # thin/shell_true：两层均已采样（不应补样）
    m2._write_csv(thin / m2.KEY_CSV, [
        _r2_key_row("u1#unsafe_deserialize", 1), _r2_key_row("u2#unsafe_deserialize", 1),
        _r2_key_row("u4#unsafe_deserialize", 0), _r2_key_row("u5#unsafe_deserialize", 0),
        _r2_key_row("h1#shell_true", 1), _r2_key_row("h2#shell_true", 0)])
    m2._write_csv(thin / m2.SHEET_CSV, [
        {"item_id": it, "repo_key": it.partition("#")[0], "category": it.partition("#")[2],
         "category_zh": "", "file_list": "a.py", "snippet": "code"}
        for it in ("u1#unsafe_deserialize", "u2#unsafe_deserialize",
                   "u4#unsafe_deserialize", "u5#unsafe_deserialize",
                   "h1#shell_true", "h2#shell_true")])

    def _er(it: str) -> dict:
        repo, _, cat = it.partition("#")
        return {"item_id": it, "layer": "thin", "repo_key": repo, "category": cat,
                "category_zh": "", "file_list": "", "snippet": ""}

    # 现有富集包：unsafe_deserialize 只覆盖 av=True；shell_true 两层都覆盖
    m2._write_csv(out / m2.ENRICH_SHEET_CSV, [
        _er("u1#unsafe_deserialize"), _er("u2#unsafe_deserialize"),
        _er("h1#shell_true"), _er("h2#shell_true")])
    before = (out / m2.ENRICH_SHEET_CSV).read_bytes()

    res = m2.enrich_supp(thin, thick, out,
                         targets=[("thin", "unsafe_deserialize"), ("thin", "shell_true")])
    p = out / m2.ENRICH_SUPP_SHEET_CSV
    check(p.exists(), "enrich_supp 产 enrich_supp_sheet.csv")
    rows = list(m2._read_csv(p))
    ids = {r["item_id"] for r in rows}
    check(ids == {"u4#unsafe_deserialize", "u5#unsafe_deserialize"},
          f"仅补缺层(av=False)（实得 {ids}）")
    check(all(r["layer"] == "thin" for r in rows), "补样 layer=thin")
    check("label" not in rows[0] and "agcm" not in rows[0], "补样材料不泄露标签")
    check((out / m2.ENRICH_SHEET_CSV).read_bytes() == before, "不覆盖 enrich_sheet.csv")
    for h in m2.HUMANS_DEFAULT:
        c = out / f"label_enrich_supp_{h}.csv"
        check(c.exists(), f"产补样卡 label_enrich_supp_{h}.csv")
        lr = list(m2._read_csv(c))
        check(len(lr) == 2 and all(x["label"] == "" for x in lr), f"补样卡 {h} 2 行全空")
    check(res["n_items"] == 2, "返回 n_items=2")


def test_post_strat_supp() -> None:
    """方向 B（预注册 §十）：补样**仅并入参考口径缺层**；主口径**逐位不变**。"""
    thin = Path(tempfile.mkdtemp(prefix="m2r2_pss_thin_"))
    thick = Path(tempfile.mkdtemp(prefix="m2r2_pss_thick_"))
    out = Path(tempfile.mkdtemp(prefix="m2r2_pss_out_"))

    # thick/db_access：av=True×1（采样）+ av=False×1（缺层）
    m2._write_csv(thick / m2.KEY_CSV, [
        _r2_key_row("d1#db_access", 1), _r2_key_row("d2#db_access", 0)])

    def _er(it: str) -> dict:
        repo, _, cat = it.partition("#")
        return {"item_id": it, "layer": "thick", "repo_key": repo, "category": cat,
                "category_zh": "", "file_list": "", "snippet": ""}

    m2._write_csv(out / m2.ENRICH_SHEET_CSV, [_er("d1#db_access")])
    for h in m2.HUMANS_DEFAULT:
        m2._write_csv(out / f"label_enrich_{h}.csv",
                      [{"item_id": "d1#db_access", "label": "present", "note": ""}])

    # 补样前：缺层 → incomplete
    m2.post_strat(thin, thick, out)
    r0 = {r["category"]: r for r in m2._read_csv(out / m2.POST_STRAT_CSV)}["db_access"]
    check(r0["n_strata_missing"] == "1" and r0["gate"] == "incomplete", "补样前缺层 → incomplete")

    # 补样：d2（av=False），双人标 absent
    m2._write_csv(out / m2.ENRICH_SUPP_SHEET_CSV, [_er("d2#db_access")])
    for h in m2.HUMANS_DEFAULT:
        m2._write_csv(out / f"label_enrich_supp_{h}.csv",
                      [{"item_id": "d2#db_access", "label": "absent", "note": ""}])

    res = m2.post_strat(thin, thick, out, supp_sheet=out / m2.ENRICH_SUPP_SHEET_CSV)
    r = {r["category"]: r for r in m2._read_csv(out / m2.POST_STRAT_CSV)}["db_access"]

    # 参考口径：缺层补齐
    check(r["n_strata_missing"] == "0", "补样后 n_strata_missing=0")
    check(r["gate"] == "complete", "补样后 gate=complete")
    check(r["n_supp"] == "1", "n_supp=1（本格并入参考口径补样行）")
    check(r["n_supp_gold"] == "1", "n_supp_gold=1（补样行金标非空）")
    check(abs(float(r["pop_tn_w"]) - 1.0) < 1e-9, "参考口径并入 d2 → pop_tn_w=1.0")

    # 主口径逐位不变
    for k in ("n_pop", "n_sampled", "n_gold", "n_undecided", "tp", "fp", "fn", "tn",
              "precision", "recall", "f1"):
        check(r[k] == r0[k], f"主口径 {k} 逐位不变（{r0[k]} vs {r[k]}）")
    check(res["n_incomplete"] == 0, "补样后 n_incomplete=0")


def main() -> int:
    test_helpers()
    test_allocate()
    test_draw_and_sample()
    test_repo_context()
    test_declared_of()
    test_sheet()
    test_stats_helpers()
    test_score()
    test_score_gold_file()
    test_matrix()
    test_gold_helpers()
    test_gold()
    test_m2_llm_helpers()
    test_llm_runner()
    test_llm_parallel_models()
    test_load_providers()
    test_llm_providers_override()
    test_triage()
    test_audit_sample()
    test_llm_gold()
    test_thick_sheet()
    test_adjudicate4()
    test_adjudicate4_wva()
    test_adjudicate4_missing_vote()
    test_residual_sheet()
    test_cross_audit()
    test_route2b_gold()
    test_route2b_score()
    test_route2b_report()
    test_gold_error_audit()
    test_operating_points()
    test_enrich_sample()
    test_post_strat()
    test_consensus_gold()
    test_post_strat_min_votes()
    test_enrich_disputes()
    test_enrich_supp()
    test_post_strat_supp()
    print(f"\n{_PASSED} passed, {len(_FAILURES)} failed")
    if _FAILURES:
        for f in _FAILURES:
            print(f"  - {f}")
        return 1
    print("M2 抽样层测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
