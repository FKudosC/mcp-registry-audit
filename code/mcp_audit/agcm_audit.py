"""M2：AGCM 准确率分层普查与双人标注。

里程碑出处：``ProvMCP_milestones.md`` §八.2 M2（最关键）；闸门 M9 Gate-B。
设计冻结见 ``docs/superpowers/plans/2026-10-08-m2-agcm-stratified-audit.md``。

本模块承担 M2 的脚本层（S2–S6）：

- **S2 抽样**：从冻结代码池 ``dual/`` 5,069 仓按「危险面严重度 × 仓库类型」主分层
  比例抽取 N=500 仓，产仓库级 ``sample.csv`` + ``sample.sha256``（先冻结后标注）。
- **S3 标注包**（``sheet``）：把冻结清单按 ``<repo_key>#<category>`` 展开为 (仓,类)
  判定行，产盲标卡 ``sheet.csv``（中性上下文，不泄露 AGCM/官方声明/分层）、隐藏键
  ``key.csv``、9 类 ``codebook.csv``、各标注者空卡 ``label_<name>.csv``。
- **S5 统计**（``score``/``matrix``）：以人工金标为真值、AGCM 判定为预测，逐类算
  Cohen's κ / precision / recall / F1 + Wilson CI，并产「官方声明 vs AGCM vs 人工」
  三方混淆矩阵；按预注册阈值给 gate 判定。
- **S6 金标**（``gold``）：双真人一致 → 共识采信；分歧 → 裁决工作表 ``disputes.csv``，
  人工填 ``adjudication.csv`` 裁决后产金标卡 ``label_gold.csv`` 并复算（LLM 仅复核、不进金标）。
- **路线 2**（``triage``/``audit-sample``/``llm-gold``）：省力范式——3-LLM 一致性分流
  （``triage``）→ 一致项暂定采信、分歧项进裁决表；再从一致项随机抽 ~10% 双人盲审
  （``audit-sample``，估 LLM 一致标签错误率 + 算双人 κ）→ 组装 LLM 金标（``llm-gold``）。

治理约束：结论数字只认冻结快照 ``mcp_audit_20261004.db``；种子固定可复现；
抽样清单先冻结（SHA-256）；凭据不写入仓库。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import random
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from . import code_analysis, config

FROZEN_DB = Path(r"D:\Data\prov-mcp\snapshots\mcp_audit_20261004.db")
DUAL_DIR = Path(r"D:\Data\prov-mcp\dual")
M2_DIR = Path(r"D:\Data\prov-mcp\m2")
M2B_DIR = Path(r"D:\Data\prov-mcp\m2b")   # 路线 2b 落点（隔离 m2 的 llm_cache.json）

SOURCE_TYPE = "code_capability"
N_DEFAULT = 500
SEED_DEFAULT = 20260917
ITEM_PREFIX = "M2"
SAMPLE_CSV = "sample.csv"
SAMPLE_SHA = "sample.sha256"

# 9 类危险面：权威键名与 consensus_analysis.DANGER_CATEGORIES（第 66 行）
# 及 code_analysis._DANGER_PATTERNS（第 71 行 assert 漂移护栏）一致，不得臆造。
DANGER_CATEGORIES = (
    "arbitrary_code", "credential_paths", "db_access", "env_access",
    "fs_destructive", "network_egress", "shell_true", "subprocess",
    "unsafe_deserialize",
)

# 主分层轴取值（池内实测分布见计划 §2.2）
SEV_BINS = ("0", "1-2", "3-5", "6-9")
REPO_TYPES = ("server+tools", "server_only", "tools_only", "lib_or_other")
LANG_GROUPS = ("js/ts", "python", "go", "other", "unknown")
SOURCE_TIERS = ("1", "2", "3+")

# --- S3 标注包 ----------------------------------------------------------
SHEET_CSV = "sheet.csv"
KEY_CSV = "key.csv"
CODEBOOK_CSV = "codebook.csv"
LABEL_PREFIX = "label_"
DEFAULT_MAXLEN = 600
CONTEXT_MAX_FILES = 80
# 标注者名册（沿用 E2 协议：2 真人 + 3 家跨家族 LLM 交叉核验）
ROSTER_DEFAULT = ("Me", "other", "DeepSeek", "Claude", "GLM")
# 仅这 3 类有官方语义声明通道（其余 6 类官方失语 → no_decl）
_DECLARABLE = ("env_access", "credential_paths", "network_egress")

# --- S5 统计口径 --------------------------------------------------------
SCORE_CSV = "score.csv"
MATRIX_CSV = "matrix.csv"
LABEL_GOLD = "gold"          # 争议裁决后的金标卡 label_gold.csv（S6 产出）
HUMANS_DEFAULT = ("Me", "other")
# --- S6 争议裁决 --------------------------------------------------------
DISPUTES_CSV = "disputes.csv"        # 双真人分歧清单（裁决工作表，脚本产出）
ADJUDICATION_CSV = "adjudication.csv"  # 人工裁决输入（item_id,label,note，人工填）
ADJUDICABLE = ("present", "absent", "unsure")
# --- 路线 2：LLM 一致性分流 + 随机盲审 + LLM 金标组装 ---------------------
TRIAGE_CSV = "triage.csv"            # LLM 一致性分流（item_id,llm_label,n_agree,status）
AUDIT_CSV = "audit_sample.csv"       # 随机盲审抽样包（中性上下文，不泄露 LLM 标签）
AUDIT_PREFIX = "audit_"              # 盲审人工卡名前缀 → label_audit_<human>.csv
AUDIT_FRACTION_DEFAULT = 0.10        # 盲审抽样比例（从 LLM 一致项中随机抽）
# --- 路线 2b：加厚重标 + 第 4 家否决式裁定 + 跨层人工盲审 ------------------
# 计划：docs/superpowers/plans/2026-10-10-m2-route2b-thick-adjudication.md
THICK_MAXLEN = 4000                  # 厚层材料长度预算（薄层 DEFAULT_MAXLEN=600）
THICK_MAX_FILES = 200                # 厚层文件清单上限（薄层 CONTEXT_MAX_FILES=80）
ADJ4_CSV = "adjudication4.csv"       # 第 4 家裁定明细（item_id,llm_<各家>,n_max,status）
RESIDUAL_SHEET_CSV = "sheet_residual.csv"  # 残余分歧子集（厚层三家仍分歧，仅供第 4 家省 token）
AUDIT_B_CSV = "audit_b.csv"          # 跨层盲审抽样包（含 layer 列）
AUDIT_B_PREFIX = "audit_b_"          # 跨层盲审人工卡名前缀 → label_audit_b_<human>.csv
AUDIT_B_FRACTION_DEFAULT = 0.10      # 跨层盲审抽样比例
GOLD_REPORT_JSON = "gold_report.json"  # 分层金标报告（分层指标 + 敏感性 S-a/S-b/S-c）
GOLD_REPORT_CSV = "gold_report.csv"
GOLD_CLASS_CSV = "gold_class_report.csv"  # 分层逐能力类 P/R/F1 + Wilson CI 报告
ROUTE2B_REPORT_JSON = "route2b_report.json"  # M9 Gate-B 判定材料（分层，不合并两层）
MODEL4_DEFAULT = "StepFun"           # 第 4 家异家族 LLM（仅裁定分歧，须能力≥前三家）；口径见 §7.8
# 分层金标来源标签（写 ``label_gold.csv`` 的 ``note`` 列）——代码常量 LAYERS
SRC_THIN = "thin-consensus"          # 薄层 3-LLM 一致（非 D）
SRC_THICK = "thick-consensus"        # 厚层 3-LLM 一致（重标后由分歧转一致）
SRC_ADJ4 = "adj4-llm"                # 厚层仍分歧，经第 4 家按规则① 采纳
SRC_AUDIT_HUMAN = "audit-b-human"    # 跨层盲审双人共识
SRC_AUDIT_UNRES = "audit-b-unresolved"  # 跨层盲审双人分歧
SRC_UNRESOLVED = "unresolved"        # 未决（fail-closed，不计入点估计分母）
LAYERS = (SRC_THIN, SRC_THICK, SRC_ADJ4, SRC_AUDIT_HUMAN, SRC_AUDIT_UNRES, SRC_UNRESOLVED)
# --- S4 LLM 交叉核验（M2 版运行器）--------------------------------------
M2_LABELS = ("present", "absent", "unsure")  # 三值判定域（与真金标同卷）
LLM_DEFAULT = ("DeepSeek", "Claude", "GLM")    # 三家跨家族 LLM（镜像 annotation.LLM_NAMES）
# 第 4 家跨家族 LLM（路线2b 裁定用）：阶跃星辰 step-5-preview（OpenAI 兼容、异底座）。
# 口径见路线2b 计划 §7.8；**不改** annotation.LLM_PROVIDERS（第一篇已冻结），仅在其上叠加。
EXTRA_PROVIDERS = {
    "StepFun": {
        "env": "STEPFUN_API_KEY",
        "url": "https://api.stepfun.com/v1/chat/completions",
        "model": "step-5-preview",
        "temp": 0.0,
    },
}
LLM_CACHE_JSON = "llm_cache.json"            # 按 model+item_id 缓存 ⇒ 可断点续跑
RERUN_DIR = "reps"                           # 重复轮次留档目录（镜像 annotation.RERUN_DIR）
# 预注册阈值（M2 计划 §一）：安全关键类召回 ≥0.90，其余 ≥0.80；信度 Cohen's κ ≥0.80。
# 决策冻结（2026-10-08，用户确认）：**全部 9 类均为安全关键类** → 召回阈值统一 0.90。
SAFETY_CRITICAL = frozenset(DANGER_CATEGORIES)
RECALL_MIN_CRITICAL = 0.90
RECALL_MIN_OTHER = 0.80
KAPPA_MIN = 0.80

# --- M2-R2：可估性 + 人锚定补做（A 线零标注 + C-lite 补样）-----------------
# 预注册：docs/superpowers/plans/2026-10-11-m2r2-estimability-prereg.md
# 隔离：R2 全部产物落 M2R2_DIR；m2\ / m2b\ 只读（不得改写冻结产物）。
M2R2_DIR = Path(r"D:\Data\prov-mcp\m2r2")
GOLD_ERROR_CSV = "gold_error_audit.csv"       # A1：LLM-gold 误差率审计
OPERATING_POINTS_CSV = "operating_points.csv"  # A2：θ 工作点曲线（整条落盘）
OP_ITEMS_CSV = "operating_points_items.csv"    # A2：伴生明细（票数可复算）
SPLIT_CSV = "split.csv"                        # A2：按 repo_key 的 60/40 划分（冻结）
SPLIT_SHA = "split.sha256"
ENRICH_SHEET_CSV = "enrich_sheet.csv"          # C-lite：稀有类富集抽样包
ENRICH_PREFIX = "enrich_"                      # 富集盲审人工卡名前缀 → label_enrich_<human>.csv
POST_STRAT_CSV = "post_strat_report.csv"       # C-lite：逐类 P/R（条件口径主列 + pop_* 参考列）
# 方向 D（预注册 §九）：争议项第 3 人裁决
ENRICH_DISPUTES_CSV = "enrich_disputes.csv"    # 争议项送审包（唯一 item_id，跨层重复取 thick）
ENRICH_THIRD_DEFAULT = "third"                 # 第 3 位真人标注者名 → 卡 label_enrich_third.csv
# 方向 B（预注册 §十）：参考口径缺层补样
ENRICH_SUPP_SHEET_CSV = "enrich_supp_sheet.csv"   # 缺层补样包（独立文件，不覆盖 enrich_sheet.csv）
ENRICH_SUPP_PREFIX = "enrich_supp_"               # 补样卡前缀 → label_enrich_supp_<human>.csv
# C-lite estimand 标注（预注册 §4.1）：主口径=条件召回（给定候选池）；pop_* 参考口径不成立
ESTIMAND_COND_POOL = "cond_pool;pop_ref_invalid"
FINAL_LAYER = "thick"                          # 系统最终输出层（对外主指标取厚层）
HOLDOUT_FRACTION = 0.40                        # A2：held-out 比例（60% / 40%）
OP_POINTS_DEFAULT = 21                         # A2：θ 等距点数（含两端）
ENRICH_N_CAND_DEFAULT = 50                     # C-lite：逐类候选池抽样上限
# C-lite 覆盖类（预注册 §八定稿，2026-10-11）：3 个 no_denominator + 6 个 n_pos ≤ 8
C_LITE_TARGETS = (
    ("thin", "arbitrary_code"), ("thin", "unsafe_deserialize"),
    ("thick", "unsafe_deserialize"),
    ("thin", "shell_true"), ("thin", "fs_destructive"), ("thin", "db_access"),
    ("thick", "arbitrary_code"), ("thick", "credential_paths"), ("thick", "shell_true"),
)

CATEGORY_ZH = {
    "arbitrary_code": "任意代码执行",
    "credential_paths": "凭据文件访问",
    "db_access": "数据库访问",
    "env_access": "环境变量访问",
    "fs_destructive": "破坏性文件操作",
    "network_egress": "网络外联",
    "shell_true": "Shell 执行",
    "subprocess": "子进程调用",
    "unsafe_deserialize": "不安全反序列化",
}
CATEGORY_DEF = {
    "arbitrary_code": "把字符串当作代码动态求值/编译执行（eval / exec / new Function / compile）。",
    "credential_paths": "访问密钥/凭据文件路径（.ssh / .aws / id_rsa / .npmrc / .netrc / credentials）。",
    "db_access": "连接或读写数据库（sqlite3 / psycopg / mysql，或 execute(SELECT|INSERT|UPDATE|DELETE)）。",
    "env_access": "读取运行环境变量（os.environ / process.env / getenv / dotenv）。",
    "fs_destructive": "对文件系统做删除或覆盖写等破坏性操作（rmtree / remove / unlink / writeFile）。",
    "network_egress": "主动发起网络外联（requests / httpx / urllib / fetch / axios / socket / curl）。",
    "shell_true": "以 shell 解释器执行命令（subprocess 且 shell=True）。",
    "subprocess": "启动子进程执行外部命令（subprocess / os.system / Popen / child_process / execSync）。",
    "unsafe_deserialize": "对不受信输入做反序列化且可能执行代码或构造任意对象"
                          "（pickle / marshal / yaml.load / unserialize）。",
}
CATEGORY_POS = {
    "arbitrary_code": "eval(user_input) / exec(src) / new Function(body)()",
    "credential_paths": "open(os.path.expanduser('~/.ssh/id_rsa'))",
    "db_access": "sqlite3.connect('app.db') / cursor.execute('SELECT ...')",
    "env_access": "os.environ['API_KEY'] / process.env.TOKEN",
    "fs_destructive": "shutil.rmtree(path) / fs.writeFileSync(dst, data)",
    "network_egress": "requests.get(url) / fetch(url) / axios.post(url, body)",
    "shell_true": "subprocess.run(cmd, shell=True)",
    "subprocess": "subprocess.Popen([...]) / os.system(cmd) / spawn(cmd)",
    "unsafe_deserialize": "pickle.loads(blob) / yaml.load(s)",
}
CATEGORY_NEG = {
    "arbitrary_code": "普通函数调用 f(x)、import 语句、类型注解",
    "credential_paths": "仅变量名含 token/secret，但无任何密钥文件路径",
    "db_access": "仅定义 ORM 模型/表结构，未建立连接或执行语句",
    "env_access": "文档/注释里出现环境变量名，代码未读取",
    "fs_destructive": "只读 open(f, 'r')、纯路径拼接 os.path.join",
    "network_egress": "只定义本地 HTTP 服务端，未发起出站请求",
    "shell_true": "subprocess.run([...], shell=False) 或列表参数形式",
    "subprocess": "线程/协程并发（threading / asyncio），未启动外部进程",
    "unsafe_deserialize": "json.loads / yaml.safe_load（只构造数据、不执行代码）",
}
CATEGORY_BOUND = {
    "arbitrary_code": "须为对字符串的可执行求值调用；仅在字符串/注释中出现 eval 字样不算。",
    "credential_paths": "须指向具体密钥文件路径；泛指 'credentials' 普通词不算。",
    "db_access": "须有连接或语句执行；仅在文本中提及 SQL 关键字不算。",
    "env_access": "须有实际读取动作；仅声明环境变量（如 server.json）而未读取不算。",
    "fs_destructive": "删除/覆盖已有文件计入；仅新建文件、仅读路径拼接不计入。",
    "network_egress": "须有出站请求调用；仅导入库名或定义服务端不算。",
    "shell_true": "须显式 shell=True；shell=False 或列表参数不计入。",
    "subprocess": "须实际启动外部进程；并发原语不计入。",
    "unsafe_deserialize": "pickle/marshal/yaml.load/unserialize 计入；JSON.parse 通常仅构造数据，"
                          "是否计入按 codebook 语义从严判断。",
}


# --- 键与分层函数 -------------------------------------------------------

def disk_dir(norm_url: str | None) -> str:
    """``norm_url`` → ``dual/`` 磁盘目录名：``github.com/<owner>/<repo>`` → ``<owner>__<repo>``。"""
    return norm_url.removeprefix("github.com/").replace("/", "__") if norm_url else ""


def sev_bin(n: int) -> str:
    if n <= 0:
        return "0"
    if n <= 2:
        return "1-2"
    if n <= 5:
        return "3-5"
    return "6-9"


def repo_type_of(d: dict) -> str:
    """仓库类型四桶：``server_like`` × ``has_tools``。"""
    server_like = bool(d.get("declared_transports")) or bool(d.get("manifest_kinds"))
    has_tools = bool(d.get("tool_names")) or (d.get("tool_names_total") or 0) > 0
    if server_like and has_tools:
        return "server+tools"
    if server_like:
        return "server_only"
    if has_tools:
        return "tools_only"
    return "lib_or_other"


def lang_group(langs: list | None) -> str:
    """语言组（成员判定，优先级 js/ts > python > go > other > unknown）。

    ``code_capability.langs`` 是语言名列表（``sorted(set)``，无计数），故按成员判定分组。
    """
    s = set(langs or [])
    if s & {"javascript", "typescript"}:
        return "js/ts"
    if "python" in s:
        return "python"
    if "go" in s:
        return "go"
    if not s:
        return "unknown"
    return "other"


def source_tier(n: int) -> str:
    if n <= 1:
        return "1"
    if n == 2:
        return "2"
    return "3+"


# --- 池加载 -------------------------------------------------------------

def load_pool(conn: sqlite3.Connection, dual_dir: Path = DUAL_DIR) -> list[dict]:
    """从冻结快照 ``code_capability`` 取候选仓，过滤到 ``dual/`` 物理存在的仓。

    AGCM 直接复用 ``code_analysis.scan_repo``，故快照的 ``danger_categories``
    **即 AGCM 判定**——无需重扫磁盘。
    """
    srcs: dict[str, set] = defaultdict(set)
    for r in conn.execute(
            "SELECT norm_url, source FROM repo_index WHERE norm_url IS NOT NULL"):
        srcs[r[0]].add(r[1])

    out: list[dict] = []
    for r in conn.execute(
            "SELECT data_json FROM findings WHERE finding_type = ?", (SOURCE_TYPE,)):
        d = json.loads(r[0])
        nu = d.get("norm_url") or ""
        key = disk_dir(nu)
        if not (key and (dual_dir / key).is_dir()):
            continue
        cats = list(d.get("danger_categories") or [])
        n_src = len(srcs.get(nu) or ())
        out.append({
            "norm_url": nu,
            "repo_key": key,
            "danger_categories": cats,
            "n_danger": len(cats),
            "sev_bin": sev_bin(len(cats)),
            "repo_type": repo_type_of(d),
            "lang_group": lang_group(d.get("langs")),
            "n_sources": n_src,
            "source_tier": source_tier(n_src),
            "agcm_present": set(cats),
            "declared_transports": list(d.get("declared_transports") or []),
            "declared_env_vars": d.get("declared_env_vars") or 0,
            "declared_secrets": d.get("declared_secrets") or 0,
        })
    out.sort(key=lambda x: x["repo_key"])
    return out


# --- 配额分配 -----------------------------------------------------------

def allocate(counts: dict, n: int) -> dict:
    """比例分配 + 最大余数法；每个非空格保底 1，合计恰为 ``n``。

    非空格数 > ``n`` 时无法保底，点名 ``ValueError``。
    """
    cells = sorted(k for k, v in counts.items() if v > 0)
    m = len(cells)
    if m == 0:
        return {}
    if n < m:
        raise ValueError(f"N={n} < 非空格数 {m}，无法每格保底 1")
    total = sum(counts[k] for k in cells)
    q = {k: n * counts[k] / total for k in cells}
    alloc = {k: max(1, int(math.floor(q[k]))) for k in cells}
    rem = n - sum(alloc.values())
    order = sorted(cells, key=lambda k: (-(q[k] - math.floor(q[k])), k))
    i = 0
    while rem > 0:
        alloc[order[i % m]] += 1
        i += 1
        rem -= 1
    if rem < 0:
        over = sorted(cells, key=lambda k: (-(alloc[k] - q[k]), k))
        j = 0
        while rem < 0 and j < 10 * m + n:
            k = over[j % m]
            if alloc[k] > 1:
                alloc[k] -= 1
                rem += 1
            j += 1
    return alloc


def _allocate_nomin(counts: dict, a: int) -> dict:
    """层内二次配额：比例 + 最大余数，不设保底（可为 0）；上限由池容量兜底。"""
    total = sum(counts.values())
    if total == 0 or a <= 0:
        return {}
    if a >= total:
        return dict(counts)
    q = {k: a * counts[k] / total for k in counts}
    alloc = {k: int(math.floor(q[k])) for k in counts}
    rem = a - sum(alloc.values())
    order = sorted(counts, key=lambda k: (-(q[k] - math.floor(q[k])), k))
    i = 0
    while rem > 0:
        alloc[order[i % len(order)]] += 1
        i += 1
        rem -= 1
    return alloc


def _draw_cell(cell_pool: list[dict], a: int, rnd: random.Random) -> list[dict]:
    """主层内二次配额（语言组 × 来源富集度）+ 层内随机抽取；不足由本层余量补足。"""
    if a <= 0:
        return []
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in cell_pool:
        groups[(r["lang_group"], r["source_tier"])].append(r)
    keys = sorted(groups)
    alloc = _allocate_nomin({k: len(groups[k]) for k in keys}, a)
    picked: list[dict] = []
    for k in keys:
        g = groups[k]
        rnd.shuffle(g)
        picked.extend(g[:min(alloc.get(k, 0), len(g))])
    short = a - len(picked)
    if short > 0:
        used = {id(x) for x in picked}
        leftover = [r for r in cell_pool if id(r) not in used]
        rnd.shuffle(leftover)
        picked.extend(leftover[:short])
    return picked[:a]


def draw_sample(pool: list[dict], n: int, seed: int) -> list[dict]:
    """主分层 = 严重度(4) × 仓库类型(4) 比例抽样；返回按 ``repo_key`` 排序的清单。"""
    cells: dict[tuple, list[dict]] = defaultdict(list)
    for r in pool:
        cells[(r["sev_bin"], r["repo_type"])].append(r)
    counts = {k: len(v) for k, v in cells.items()}
    alloc = allocate(counts, n)
    rnd = random.Random(seed)
    picked: list[dict] = []
    for k in sorted(cells):
        picked.extend(_draw_cell(cells[k], alloc.get(k, 0), rnd))
    short = n - len(picked)
    if short > 0:
        used = {r["repo_key"] for r in picked}
        leftover = [r for r in pool if r["repo_key"] not in used]
        rnd.shuffle(leftover)
        picked.extend(leftover[:short])
    picked.sort(key=lambda r: r["repo_key"])
    return picked


# --- CSV 读写（镜像 annotation.py 约定）--------------------------------

def _write_csv(path: Path, rows: list[dict], enc: str = "utf-8-sig") -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding=enc) as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _read_csv(path: Path) -> csv.DictReader:
    """读 CSV，兼容 Excel 在中文 Windows 上另存的 GBK/ANSI 编码。"""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return csv.DictReader(io.StringIO(raw.decode(enc)))
        except UnicodeDecodeError:
            continue
    return csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace")))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


# --- S3 中性上下文与官方声明 -------------------------------------------

def repo_context(repo_dir: Path, maxlen: int = DEFAULT_MAXLEN,
                 max_files: int = CONTEXT_MAX_FILES) -> tuple[str, str]:
    """中性仓库上下文：文件清单 + 关键源码片段（受长度上限约束）。

    刻意**不**按危险面挑选文件——避免向标注者泄露 AGCM 判定，防止锚定偏差。
    复用 ``code_analysis._walk_files``（同一 ``SKIP_DIRS`` / ``MAX_FILES`` 口径）。
    """
    repo_dir = Path(repo_dir)
    names: list[str] = []
    snips: list[str] = []
    budget = maxlen
    for full, _fn, ext in code_analysis._walk_files(repo_dir):
        rel = os.path.relpath(full, repo_dir).replace("\\", "/")
        if len(names) < max_files:
            names.append(rel)
        if ext in code_analysis.CODE_EXT and budget > 0:
            try:
                if full.stat().st_size > code_analysis.MAX_FILE_BYTES:
                    continue
                text = full.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            chunk = text[:budget]
            snips.append(f"--- {rel} ---\n{chunk}")
            budget -= len(chunk)
    return ";".join(names), "\n".join(snips)


def _declared_caps_of(repo: dict) -> set:
    """复用 ``consensus_analysis._declared_caps``——官方声明口径的权威实现。"""
    from . import consensus_analysis as ca
    return ca._declared_caps({
        "declared_env_vars": repo.get("declared_env_vars") or 0,
        "declared_secrets": repo.get("declared_secrets") or 0,
        "declared_transports": repo.get("declared_transports") or [],
    })


def declared_of(category: str, repo: dict) -> str:
    """官方声明侧判定：``present`` / ``absent``；无声明通道的类记 ``no_decl``（官方失语）。"""
    if category not in _DECLARABLE:
        return "no_decl"
    return "present" if category in _declared_caps_of(repo) else "absent"


# --- 抽样主流程 ---------------------------------------------------------

def _sheet_rows(picked: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for i, r in enumerate(picked, 1):
        rows.append({
            "item_id": f"{ITEM_PREFIX}-{i:04d}",
            "repo_key": r["repo_key"],
            "norm_url": r["norm_url"],
            "stratum": f"{r['sev_bin']}×{r['repo_type']}",
            "sev_bin": r["sev_bin"],
            "n_danger": r["n_danger"],
            "repo_type": r["repo_type"],
            "lang_group": r["lang_group"],
            "n_sources": r["n_sources"],
            "source_tier": r["source_tier"],
            "danger_categories": "|".join(r["danger_categories"]),
        })
    return rows


def sample(conn: sqlite3.Connection, dual_dir: Path, out_dir: Path, n: int,
           seed: int, force: bool) -> dict:
    """S2：分层抽样并落**冻结清单** ``sample.csv`` + ``sample.sha256``。

    已存在 ``sample.csv`` 且无 ``force`` 则 ``SystemExit``——勿覆盖已标注的包。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet = out_dir / SAMPLE_CSV
    if sheet.exists() and not force:
        raise SystemExit(f"[x] {sheet} 已存在（加 --force 覆盖）—— 勿覆盖已标注的包")

    pool = load_pool(conn, dual_dir)
    picked = draw_sample(pool, n, seed)
    _write_csv(sheet, _sheet_rows(picked))

    digest = sha256_file(sheet)
    (out_dir / SAMPLE_SHA).write_text(f"{digest}  {SAMPLE_CSV}\n", encoding="utf-8")

    sev_rt = Counter((r["sev_bin"], r["repo_type"]) for r in picked)
    return {"dir": out_dir, "n": len(picked), "pop": len(pool), "seed": seed,
            "sha256": digest, "tab": sev_rt,
            "files": [sheet, out_dir / SAMPLE_SHA]}


def sheet(conn: sqlite3.Connection, dual_dir: Path, out_dir: Path,
          sample_csv: str = SAMPLE_CSV, maxlen: int = DEFAULT_MAXLEN,
          max_files: int = CONTEXT_MAX_FILES,
          roster: tuple[str, ...] = ROSTER_DEFAULT, force: bool = False) -> dict:
    """S3：把冻结清单的 500 仓按 ``<repo_key>#<category>`` 展开为 (仓,类) 判定行。

    产**盲标卡** ``sheet.csv``（中性仓库上下文，**不含** AGCM/官方声明/分层键）、
    **隐藏键** ``key.csv``（分层 + AGCM 判定 + 官方声明）、9 类 ``codebook.csv``、
    各标注者空卡 ``label_<name>.csv``。复用 ``_write_csv``/``_read_csv`` 约定。
    """
    out_dir = Path(out_dir)
    dual_dir = Path(dual_dir)
    sample_path = out_dir / sample_csv
    if not sample_path.exists():
        raise SystemExit(f"[x] 冻结清单不存在：{sample_path}（先跑 sample）")
    sheet_path = out_dir / SHEET_CSV
    if sheet_path.exists() and not force:
        raise SystemExit(f"[x] {sheet_path} 已存在（加 --force 覆盖）—— 勿覆盖已标注的包")

    pool = {p["repo_key"]: p for p in load_pool(conn, dual_dir)}
    sheet_rows: list[dict] = []
    key_rows: list[dict] = []
    label_rows: list[dict] = []
    for row in _read_csv(sample_path):
        repo_key = row["repo_key"]
        p = pool.get(repo_key)
        if p:
            present = p["agcm_present"]
        else:
            present = {c for c in (row.get("danger_categories") or "").split("|") if c}
        file_list, snippet = repo_context(dual_dir / repo_key, maxlen, max_files)
        for cat in DANGER_CATEGORIES:
            item_id = f"{repo_key}#{cat}"
            sheet_rows.append({
                "item_id": item_id, "repo_key": repo_key, "category": cat,
                "category_zh": CATEGORY_ZH[cat],
                "file_list": file_list, "snippet": snippet,
            })
            key_rows.append({
                "item_id": item_id, "repo_key": repo_key, "category": cat,
                "stratum": row.get("stratum") or "",
                "sev_bin": row.get("sev_bin") or "",
                "repo_type": row.get("repo_type") or "",
                "lang_group": row.get("lang_group") or "",
                "source_tier": row.get("source_tier") or "",
                "agcm_present": 1 if cat in present else 0,
                "declared": declared_of(cat, p or {}),
                "n_danger": row.get("n_danger") or 0,
            })
            label_rows.append({"item_id": item_id, "label": "", "note": ""})

    _write_csv(sheet_path, sheet_rows)
    _write_csv(out_dir / KEY_CSV, key_rows)
    _write_csv(out_dir / CODEBOOK_CSV, [
        {"category": c, "名称": CATEGORY_ZH[c], "定义": CATEGORY_DEF[c],
         "正例": CATEGORY_POS[c], "反例": CATEGORY_NEG[c], "边界": CATEGORY_BOUND[c]}
        for c in DANGER_CATEGORIES])

    cards: list[Path] = []
    for name in roster:
        card = out_dir / f"{LABEL_PREFIX}{name}.csv"
        if force or not card.exists():
            _write_csv(card, label_rows)
        cards.append(card)

    return {"dir": out_dir, "items": len(sheet_rows),
            "repos": len(sheet_rows) // len(DANGER_CATEGORIES),
            "roster": list(roster),
            "files": [sheet_path, out_dir / KEY_CSV, out_dir / CODEBOOK_CSV] + cards}


# --- S5 统计：信度（Cohen κ）/ 效度（P·R·F1 + Wilson CI）/ 三方混淆矩阵 ---

def cohen_kappa_l(a: dict, b: dict, common: list) -> tuple[float, float, float]:
    """双标注者 Cohen's κ（标签集自适应）。

    与 ``annotation.cohen_kappa`` 的差异：Pe 用**实际出现的标签集**计算（而非固定
    ``LABELS``），适配 M2 的三值域 ``present``/``absent``/``unsure``。``common`` 为空
    → ``(nan, nan, nan)``；两标注者完全一致且无变异 → κ=1.0。
    """
    common = list(common)
    if not common:
        return float("nan"), float("nan"), float("nan")
    n = len(common)
    po = sum(1 for k in common if a.get(k) == b.get(k)) / n
    ca = Counter(a.get(k) for k in common)
    cb = Counter(b.get(k) for k in common)
    pe = sum((ca.get(l, 0) / n) * (cb.get(l, 0) / n) for l in set(ca) | set(cb))
    if pe >= 1.0:
        kappa = 1.0 if po >= 1.0 else 0.0
    else:
        kappa = (po - pe) / (1.0 - pe)
    return kappa, po, pe


def _prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    """precision / recall / F1；分母为 0 → nan（不臆造 0 或 1）。"""
    p = tp / (tp + fp) if (tp + fp) else float("nan")
    r = tp / (tp + fn) if (tp + fn) else float("nan")
    f1 = 2.0 * p * r / (p + r) if (p == p and r == r and (p + r)) else float("nan")
    return p, r, f1


def _load_keys(out_dir: Path) -> list[dict]:
    key_path = Path(out_dir) / KEY_CSV
    if not key_path.exists():
        raise SystemExit(f"[x] 隐藏键不存在：{key_path}（先跑 sheet）")
    return list(_read_csv(key_path))


def _read_card_map(path: Path) -> dict:
    """读 ``(item_id,label,note)`` 卡 → ``{item_id: label}``；缺文件 → ``{}``。"""
    path = Path(path)
    if not path.exists():
        return {}
    return {r["item_id"]: (r.get("label") or "").strip() for r in _read_csv(path)}


def _read_label_map(out_dir: Path, name: str) -> dict:
    return _read_card_map(Path(out_dir) / f"{LABEL_PREFIX}{name}.csv")


def _resolve_items(key_rows: list[dict], humans: tuple, label_maps: dict,
                   gold_map: dict) -> list[dict]:
    """逐项解析金标，返回带 ``status`` 的项列表。

    ``status`` ∈ ``gold``（有有效金标）/ ``unresolved``（双真人分歧）/ ``unsure``（标 unsure）。
    gold 来源：有 ``gold_map``（``label_gold.csv``，S6 争议裁决产物）→ 直接用；
    否则用**双真人共识**（两人一致且非 unsure）。
    """
    items: list[dict] = []
    for row in key_rows:
        it = row["item_id"]
        agcm = str(row.get("agcm_present")) == "1"
        if gold_map:
            gl = gold_map.get(it, "")
            if gl == "unsure":
                gold, status = None, "unsure"
            elif gl in ("present", "absent"):
                gold, status = gl, "gold"
            else:
                gold, status = None, "unresolved"
        else:
            labs = [label_maps[h].get(it, "") for h in humans]
            if len(set(labs)) == 1 and labs[0] in ("present", "absent"):
                gold, status = labs[0], "gold"
            elif len(set(labs)) == 1 and labs[0] == "unsure":
                gold, status = None, "unsure"
            else:
                gold, status = None, "unresolved"
        items.append({"item_id": it, "category": row["category"],
                      "declared": row.get("declared") or "no_decl",
                      "agcm": agcm, "gold": gold, "status": status})
    return items


def _fmt(x) -> str:
    if x is None:
        return ""
    if isinstance(x, float) and x != x:
        return ""
    if isinstance(x, float):
        return f"{x:.6g}"
    return str(x)


def score(out_dir: Path, humans: tuple = HUMANS_DEFAULT,
          gold_name: str = LABEL_GOLD) -> dict:
    """S5：以人工金标为真值、AGCM 判定为预测，逐类算 P/R/F1 + Wilson CI 与双真人 κ。

    - **信度**：per-class + 总体 Cohen's κ（双真人，主口径）。
    - **效度**：per-class TP/FP/FN/TN → precision/recall/F1，recall/precision 配 Wilson CI。
    - **金标来源**：``label_gold.csv`` 存在则优先（``gold_source='label_gold.csv'``），
      否则用双真人共识（``'consensus'``）。
    - **gate**：按预注册阈值逐类判定召回（安全关键类 ≥0.90），产 ``verdict``/``fails``。
    产 ``score.csv``（含 ``__overall__`` 行与逐类行）。
    """
    from .stats_tests import wilson_ci

    out_dir = Path(out_dir)
    key_rows = _load_keys(out_dir)
    label_maps = {h: _read_label_map(out_dir, h) for h in humans}
    gold_path = out_dir / f"{LABEL_PREFIX}{gold_name}.csv"
    use_gold = gold_path.exists()
    gold_map = _read_label_map(out_dir, gold_name) if use_gold else {}
    gold_source = gold_path.name if use_gold else "consensus"

    items = _resolve_items(key_rows, humans, label_maps, gold_map)

    a_all, b_all = label_maps[humans[0]], label_maps[humans[1]]
    per_class: dict[str, dict] = {}
    rows: list[dict] = []
    pooled = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    all_common: list[str] = []
    tot_unres = tot_unsure = 0
    for cat in DANGER_CATEGORIES:
        cls = [i for i in items if i["category"] == cat]
        tp = fp = fn = tn = 0
        for i in cls:
            if i["gold"] is None:
                continue
            pred, truth = i["agcm"], i["gold"] == "present"
            if pred and truth:
                tp += 1
            elif pred and not truth:
                fp += 1
            elif not pred and truth:
                fn += 1
            else:
                tn += 1
        precision, recall, f1 = _prf(tp, fp, fn)
        recall_lo, recall_hi = wilson_ci(tp, tp + fn)
        precision_lo, precision_hi = wilson_ci(tp, tp + fp)
        common = [i["item_id"] for i in cls
                  if a_all.get(i["item_id"]) and b_all.get(i["item_id"])]
        kappa_hh = cohen_kappa_l(a_all, b_all, common)[0]
        n_unresolved = sum(1 for i in cls if i["status"] == "unresolved")
        n_unsure = sum(1 for i in cls if i["status"] == "unsure")
        thr = RECALL_MIN_CRITICAL if cat in SAFETY_CRITICAL else RECALL_MIN_OTHER
        per_class[cat] = {
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": precision, "recall": recall, "f1": f1,
            "precision_lo": precision_lo, "precision_hi": precision_hi,
            "recall_lo": recall_lo, "recall_hi": recall_hi,
            "kappa_hh": kappa_hh, "n_unresolved": n_unresolved,
            "n_unsure": n_unsure, "n_gold": tp + fp + fn + tn,
            "recall_min": thr,
        }
        pooled["tp"] += tp; pooled["fp"] += fp; pooled["fn"] += fn; pooled["tn"] += tn
        all_common.extend(common)
        tot_unres += n_unresolved
        tot_unsure += n_unsure
        rows.append({
            "category": cat, "n_gold": tp + fp + fn + tn,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": _fmt(precision), "recall": _fmt(recall), "f1": _fmt(f1),
            "recall_lo": _fmt(recall_lo), "recall_hi": _fmt(recall_hi),
            "precision_lo": _fmt(precision_lo), "precision_hi": _fmt(precision_hi),
            "kappa_hh": _fmt(kappa_hh), "n_unresolved": n_unresolved,
            "n_unsure": n_unsure, "recall_min": _fmt(thr),
        })

    overall_kappa = cohen_kappa_l(a_all, b_all, all_common)[0]
    op, orr, of1 = _prf(pooled["tp"], pooled["fp"], pooled["fn"])
    or_lo, or_hi = wilson_ci(pooled["tp"], pooled["tp"] + pooled["fn"])
    overall = {"tp": pooled["tp"], "fp": pooled["fp"], "fn": pooled["fn"],
               "tn": pooled["tn"], "precision": op, "recall": orr, "f1": of1,
               "recall_lo": or_lo, "recall_hi": or_hi, "kappa_hh": overall_kappa,
               "n_gold": pooled["tp"] + pooled["fp"] + pooled["fn"] + pooled["tn"],
               "n_unresolved": tot_unres, "n_unsure": tot_unsure}
    rows.append({
        "category": "__overall__", "n_gold": overall["n_gold"],
        "tp": pooled["tp"], "fp": pooled["fp"], "fn": pooled["fn"], "tn": pooled["tn"],
        "precision": _fmt(op), "recall": _fmt(orr), "f1": _fmt(of1),
        "recall_lo": _fmt(or_lo), "recall_hi": _fmt(or_hi),
        "precision_lo": "", "precision_hi": "",
        "kappa_hh": _fmt(overall_kappa), "n_unresolved": tot_unres,
        "n_unsure": tot_unsure, "recall_min": "",
    })

    fails: list[dict] = []
    for cat in DANGER_CATEGORIES:
        pc = per_class[cat]
        r, thr = pc["recall"], pc["recall_min"]
        if r != r:
            fails.append({"category": cat, "reason": "no_denominator",
                          "recall": None, "threshold": thr})
        elif r < thr:
            fails.append({"category": cat, "reason": "below_threshold",
                          "recall": r, "threshold": thr})
    gate = {"verdict": "pass" if not fails else "fail-closed",
            "fails": fails, "kappa_min": KAPPA_MIN,
            "recall_min_critical": RECALL_MIN_CRITICAL,
            "recall_min_other": RECALL_MIN_OTHER,
            "kappa_ok": (overall_kappa == overall_kappa and overall_kappa >= KAPPA_MIN)}

    score_path = out_dir / SCORE_CSV
    _write_csv(score_path, rows)
    return {"dir": out_dir, "per_class": per_class, "overall": overall,
            "gold_source": gold_source, "gate": gate,
            "files": [score_path]}


def matrix(out_dir: Path, humans: tuple = HUMANS_DEFAULT,
           gold_name: str = LABEL_GOLD) -> dict:
    """S5：三方混淆矩阵——官方声明（declared）× AGCM 判定 × 人工金标。

    逐类按 ``(category, declared, agcm, gold)`` 聚合计数；未决项（双真人分歧/unsure）
    剔除。产 ``matrix.csv``。
    """
    out_dir = Path(out_dir)
    key_rows = _load_keys(out_dir)
    label_maps = {h: _read_label_map(out_dir, h) for h in humans}
    gold_path = out_dir / f"{LABEL_PREFIX}{gold_name}.csv"
    gold_map = _read_label_map(out_dir, gold_name) if gold_path.exists() else {}

    items = _resolve_items(key_rows, humans, label_maps, gold_map)
    counter: Counter = Counter()
    for i in items:
        if i["gold"] is None:
            continue
        counter[(i["category"], i["declared"], "1" if i["agcm"] else "0", i["gold"])] += 1
    rows = [{"category": c, "declared": d, "agcm": a, "gold": g, "count": n}
            for (c, d, a, g), n in sorted(counter.items())]
    matrix_path = out_dir / MATRIX_CSV
    _write_csv(matrix_path, rows)
    return {"dir": out_dir, "rows": rows, "files": [matrix_path]}


def _llm_majority(labels: list) -> str:
    """LLM 复核票的多数值（**仅作裁决参考**，不参与金标）。

    只统计 ``present``/``absent`` 有效票；无有效票或最高票并列 → ``""``。
    """
    votes = [l for l in labels if l in ("present", "absent")]
    if not votes:
        return ""
    top = Counter(votes).most_common()
    if len(top) > 1 and top[0][1] == top[1][1]:
        return ""
    return top[0][0]


def gold(out_dir: Path, humans: tuple = HUMANS_DEFAULT, roster: tuple = ROSTER_DEFAULT,
         gold_name: str = LABEL_GOLD, adj_name: str = ADJUDICATION_CSV,
         force: bool = False) -> dict:
    """S6：争议裁决 → 金标卡 ``label_gold.csv`` → 复算（``score``/``matrix``）。

    规则（确定性、可复现）：
    - **双真人一致**（含一致 ``unsure``）→ 直接采信为金标（``consensus``）。
    - **双真人分歧** → 争议项，写入裁决工作表 ``disputes.csv``（附双方标 + LLM 多数 + AGCM
      + 官方声明作参考）；由人工填 ``adjudication.csv``（``item_id,label,note``）裁决。
    - 已裁决（``present``/``absent``/``unsure``）→ 采信（``adjudicated``）；未裁决 → 留空
      （复算时记为未决，分母如实报告，不得臆造）。

    LLM 仅作第三方复核，**不参与金标**（沿用 E2 协议）。产 ``label_gold.csv`` +
    ``disputes.csv``，并对 ``score``/``matrix`` 复算（``gold_source`` 自动切到金标文件）。
    """
    out_dir = Path(out_dir)
    key_rows = _load_keys(out_dir)
    humans = tuple(humans)
    roster = tuple(roster)
    label_maps = {name: _read_label_map(out_dir, name) for name in roster}
    llm_names = [n for n in roster if n not in humans]
    adj_map = _read_card_map(out_dir / adj_name)

    gold_path = out_dir / f"{LABEL_PREFIX}{gold_name}.csv"
    if gold_path.exists() and not force:
        raise SystemExit(f"[x] {gold_path} 已存在（加 --force 覆盖）—— 勿覆盖已裁决的金标卡")

    gold_rows: list[dict] = []
    disputes: list[dict] = []
    n_cons = n_adj = n_unres = 0
    for row in key_rows:
        it = row["item_id"]
        labs = [label_maps[h].get(it, "") for h in humans]
        adj = adj_map.get(it, "")
        if len(set(labs)) == 1 and labs[0] in ("present", "absent"):
            gl, src = labs[0], "consensus"
            n_cons += 1
        elif len(set(labs)) == 1 and labs[0] == "unsure":
            gl, src = "unsure", "consensus"
            n_cons += 1
        elif adj in ADJUDICABLE:
            gl, src = adj, "adjudicated"
            n_adj += 1
        else:
            gl, src = "", "unresolved"
            n_unres += 1
        gold_rows.append({"item_id": it, "label": gl, "note": src})
        if src != "consensus":
            llm = [label_maps[n].get(it, "") for n in llm_names]
            disputes.append({
                "item_id": it, "repo_key": row.get("repo_key", ""),
                "category": row["category"], "category_zh": CATEGORY_ZH[row["category"]],
                f"label_{humans[0]}": labs[0], f"label_{humans[1]}": labs[1],
                **{f"llm_{n}": label_maps[n].get(it, "") for n in llm_names},
                "llm_majority": _llm_majority(llm),
                "agcm": row.get("agcm_present", ""), "declared": row.get("declared", ""),
                "adjudication": adj, "note": "",
            })

    _write_csv(gold_path, gold_rows)
    _write_csv(out_dir / DISPUTES_CSV, disputes)

    res = score(out_dir, humans=humans, gold_name=gold_name)
    mx = matrix(out_dir, humans=humans, gold_name=gold_name)
    return {"dir": out_dir, "n_items": len(gold_rows), "n_consensus": n_cons,
            "n_disputed": len(disputes), "n_adjudicated": n_adj, "n_unresolved": n_unres,
            "gold_source": res["gold_source"], "score": res, "matrix": mx,
            "files": [gold_path, out_dir / DISPUTES_CSV, out_dir / SCORE_CSV,
                      out_dir / MATRIX_CSV]}


# --- 路线 2：LLM 一致性分流 → 随机盲审 → LLM 金标组装 --------------------
# 省力范式：3-LLM 投票 → 一致项暂定采信、分歧项交人工裁决；再从一致项随机抽 ~10%
# 双人盲审，估「LLM 一致标签」的错误率并算双人 κ。金标 = LLM 共识 + 人工裁决 + 盲审覆盖。

def _load_triage(out_dir: Path) -> list[dict]:
    p = Path(out_dir) / TRIAGE_CSV
    if not p.exists():
        raise SystemExit(f"[x] 分流结果不存在：{p}（先跑 triage）")
    return list(_read_csv(p))


def _audit_humans(humans: tuple) -> tuple:
    """盲审人工卡名（``label_audit_<human>.csv``）。"""
    return tuple(f"{AUDIT_PREFIX}{h}" for h in humans)


def triage(out_dir: Path, llms: tuple = LLM_DEFAULT, force: bool = False) -> dict:
    """路线 2 第一步：3-LLM 一致性分流（triage）。

    - 逐 ``item_id`` 收集 ``llms`` 三值票（``present``/``absent``/``unsure``）。
    - **完全一致** → 暂定金标 = 该票（``consensus``），写入 ``triage.csv``。
    - **不一致**（2/1 或三方分歧，含未填）→ ``disputed``，写入裁决工作表
      ``adjudication.csv``（``label`` 留空待人工填；附各家 LLM 票 + AGCM + 声明作参考）。

    ``adjudication.csv`` 已存在且非 ``force`` → **不覆盖**（保护人工裁决）。
    产 ``triage.csv`` + ``adjudication.csv``。
    """
    out_dir = Path(out_dir)
    key_rows = _load_keys(out_dir)
    llms = tuple(llms)
    llm_maps = {n: _read_label_map(out_dir, n) for n in llms}

    triage_rows: list[dict] = []
    adj_rows: list[dict] = []
    n_cons = 0
    for row in key_rows:
        it = row["item_id"]
        votes = [llm_maps[n].get(it, "") for n in llms]
        if len(set(votes)) == 1 and votes[0] in M2_LABELS:
            label, status, n_agree = votes[0], "consensus", len(votes)
            n_cons += 1
        else:
            label, status = "", "disputed"
            n_agree = Counter(votes).most_common(1)[0][1] if votes else 0
        triage_rows.append({
            "item_id": it, "repo_key": row.get("repo_key", ""),
            "category": row["category"], "category_zh": CATEGORY_ZH[row["category"]],
            "llm_label": label, "n_agree": n_agree, "status": status})
        if status == "disputed":
            adj_rows.append({
                "item_id": it, "label": "", "note": "",
                **{f"llm_{n}": llm_maps[n].get(it, "") for n in llms},
                "agcm": row.get("agcm_present", ""), "declared": row.get("declared", ""),
                "repo_key": row.get("repo_key", ""), "category": row["category"],
                "category_zh": CATEGORY_ZH[row["category"]]})

    triage_path = out_dir / TRIAGE_CSV
    _write_csv(triage_path, triage_rows)
    adj_path = out_dir / ADJUDICATION_CSV
    if adj_rows and (force or not adj_path.exists()):
        _write_csv(adj_path, adj_rows)
    files = [triage_path] + ([adj_path] if adj_path.exists() else [])
    return {"dir": out_dir, "n_items": len(triage_rows), "n_consensus": n_cons,
            "n_disputed": len(adj_rows), "files": files}


def audit_sample(out_dir: Path, fraction: float = AUDIT_FRACTION_DEFAULT,
                 seed: int = SEED_DEFAULT, humans: tuple = HUMANS_DEFAULT,
                 force: bool = False) -> dict:
    """路线 2 第二步：从 LLM **一致项**中随机抽 ``fraction`` 做双人盲审。

    固定 ``seed`` → 可复现。产 ``audit_sample.csv``（中性上下文，**不泄露 LLM 标签**）
    + 各盲审者空卡 ``label_audit_<human>.csv``（供 2 人独立盲标）。防覆盖已下发的盲审包。
    """
    out_dir = Path(out_dir)
    triage_rows = _load_triage(out_dir)
    audit_path = out_dir / AUDIT_CSV
    if audit_path.exists() and not force:
        raise SystemExit(f"[x] {audit_path} 已存在（加 --force 覆盖）—— 勿覆盖已下发的盲审包")

    pool = sorted(r["item_id"] for r in triage_rows if r["status"] == "consensus")
    k = max(0, min(int(round(fraction * len(pool))), len(pool)))
    picked = set(random.Random(seed).sample(pool, k))

    sheet_path = out_dir / SHEET_CSV
    sheet_map = ({r["item_id"]: r for r in _read_csv(sheet_path)}
                 if sheet_path.exists() else {})
    rows: list[dict] = []
    for it in pool:
        if it not in picked:
            continue
        s = sheet_map.get(it, {})
        rows.append({
            "item_id": it, "repo_key": s.get("repo_key", ""),
            "category": s.get("category", ""), "category_zh": s.get("category_zh", ""),
            "file_list": s.get("file_list", ""), "snippet": s.get("snippet", "")})

    if rows:
        _write_csv(audit_path, rows)
        for h in humans:
            _write_csv(out_dir / f"{LABEL_PREFIX}{AUDIT_PREFIX}{h}.csv",
                       [{"item_id": r["item_id"], "label": "", "note": ""} for r in rows])
    files = [audit_path] + [out_dir / f"{LABEL_PREFIX}{AUDIT_PREFIX}{h}.csv" for h in humans]
    return {"dir": out_dir, "n_pool": len(pool), "n_sampled": len(rows),
            "fraction": fraction, "seed": seed, "files": files}


def llm_gold(out_dir: Path, humans: tuple = HUMANS_DEFAULT, llms: tuple = LLM_DEFAULT,
             gold_name: str = LABEL_GOLD, adj_name: str = ADJUDICATION_CSV,
             force: bool = False) -> dict:
    """路线 2 第三步：组装 LLM 金标 ``label_gold.csv`` → 复算（``score``/``matrix``）。

    逐项定标（优先级）：
    1. **盲审项**（``audit_sample.csv`` 命中）→ 双人盲审共识（``audit-human``）；
       双人分歧/未填 → 未决（``audit-unresolved``）。
    2. **LLM 一致项**（``triage.csv`` = consensus）→ LLM 共识（``llm-consensus``）。
    3. **分歧项** → 人工裁决 ``adjudication.csv``（``adjudicated``）；未裁决 → 未决。

    复算时以**盲审双人卡**（``label_audit_<human>.csv``）算 Cohen's κ。
    """
    out_dir = Path(out_dir)
    key_rows = _load_keys(out_dir)
    triage_map = {r["item_id"]: r for r in _load_triage(out_dir)}
    audit_path = out_dir / AUDIT_CSV
    audit_ids = ({r["item_id"] for r in _read_csv(audit_path)}
                 if audit_path.exists() else set())
    adj_map = _read_card_map(out_dir / adj_name)
    audit_humans = _audit_humans(tuple(humans))
    audit_label_maps = {h: _read_label_map(out_dir, h) for h in audit_humans}

    gold_path = out_dir / f"{LABEL_PREFIX}{gold_name}.csv"
    if gold_path.exists() and not force:
        raise SystemExit(f"[x] {gold_path} 已存在（加 --force 覆盖）—— 勿覆盖已组装的金标卡")

    gold_rows: list[dict] = []
    n_llm = n_audit = n_adj = n_unres = 0
    for row in key_rows:
        it = row["item_id"]
        if it in audit_ids:
            labs = [audit_label_maps[h].get(it, "") for h in audit_humans]
            if len(set(labs)) == 1 and labs[0] in M2_LABELS:
                gl, src = labs[0], "audit-human"
                n_audit += 1
            else:
                gl, src = "", "audit-unresolved"
                n_unres += 1
        else:
            t = triage_map.get(it, {})
            if t.get("status") == "consensus" and t.get("llm_label") in M2_LABELS:
                gl, src = t["llm_label"], "llm-consensus"
                n_llm += 1
            else:
                adj = adj_map.get(it, "")
                if adj in ADJUDICABLE:
                    gl, src = adj, "adjudicated"
                    n_adj += 1
                else:
                    gl, src = "", "unresolved"
                    n_unres += 1
        gold_rows.append({"item_id": it, "label": gl, "note": src})

    _write_csv(gold_path, gold_rows)
    res = score(out_dir, humans=audit_humans, gold_name=gold_name)
    mx = matrix(out_dir, humans=audit_humans, gold_name=gold_name)
    return {"dir": out_dir, "n_items": len(gold_rows), "n_llm_consensus": n_llm,
            "n_audit_human": n_audit, "n_adjudicated": n_adj, "n_unresolved": n_unres,
            "gold_source": res["gold_source"], "score": res, "matrix": mx,
            "files": [gold_path, out_dir / SCORE_CSV, out_dir / MATRIX_CSV]}


# --- 路线 2b：加厚重标 + 第 4 家否决式裁定 + 跨层人工盲审 ------------------
# 计划：docs/superpowers/plans/2026-10-10-m2-route2b-thick-adjudication.md
# 生产纪律：薄层（一致项，maxlen=600/max_files=80）**不被重标**；厚层（分歧项，
# maxlen=4000/max_files=200）重标 → 两层**不得合并成一个 κ**，必须分层报告。
# 全部产物落独立目录（隔离 llm_cache.json，避免与 m2 缓存键碰撞）。

def _audit_b_humans(humans: tuple) -> tuple:
    """跨层盲审人工卡名（``label_audit_b_<human>.csv``）。"""
    return tuple(f"{AUDIT_B_PREFIX}{h}" for h in humans)


def _enrich_humans(humans: tuple) -> tuple:
    """C-lite 富集补样盲审人工卡名（``label_enrich_<human>.csv``）。"""
    return tuple(f"{ENRICH_PREFIX}{h}" for h in humans)


def _enrich_supp_humans(humans: tuple) -> tuple:
    """方向 B 缺层补样盲审人工卡名（``label_enrich_supp_<human>.csv``）。"""
    return tuple(f"{ENRICH_SUPP_PREFIX}{h}" for h in humans)


def _disputed_rows(thin_dir: Path) -> list[dict]:
    """薄层分流结果里的分歧项（保持 ``triage.csv`` 顺序，去重）。"""
    seen: set[str] = set()
    rows: list[dict] = []
    for r in _load_triage(thin_dir):
        it = r["item_id"]
        if r.get("status") == "disputed" and it not in seen:
            seen.add(it)
            rows.append(r)
    return rows


def thick_sheet(thin_dir: Path, dual_dir: Path, out_dir: Path,
                maxlen: int = THICK_MAXLEN, max_files: int = THICK_MAX_FILES,
                roster: tuple[str, ...] = ROSTER_DEFAULT, force: bool = False) -> dict:
    """路线 2b 加厚标注包：只对**分歧集 D** 重出加厚材料（``maxlen``/``max_files``）。

    复用 ``thin_dir/key.csv`` 的分层键与 ``thin_dir/triage.csv`` 的分歧判定；
    材料由 ``repo_context(dual_dir/<repo_key>, maxlen, max_files)`` 重取（加厚）。
    **不读 DB**；产 ``sheet.csv``/``key.csv``/``codebook.csv`` + 各标注者空卡。
    防覆盖 ``sheet.csv``——勿覆盖已下发的重标包。
    """
    thin_dir = Path(thin_dir)
    dual_dir = Path(dual_dir)
    out_dir = Path(out_dir)
    disputed = _disputed_rows(thin_dir)
    if not disputed:
        raise SystemExit("[x] 无分歧项（triage.csv 中 status != disputed）—— 无需加厚")
    sheet_path = out_dir / SHEET_CSV
    if sheet_path.exists() and not force:
        raise SystemExit(f"[x] {sheet_path} 已存在（加 --force 覆盖）—— 勿覆盖已下发的重标包")

    key_map = {r["item_id"]: r for r in _load_keys(thin_dir)}
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx_cache: dict[str, tuple[str, str]] = {}
    sheet_rows: list[dict] = []
    key_rows: list[dict] = []
    label_rows: list[dict] = []
    cats: list[str] = []
    for t in disputed:
        it = t["item_id"]
        k = key_map.get(it, {})
        repo_key = t.get("repo_key") or k.get("repo_key", "")
        cat = t.get("category") or k.get("category", "")
        if repo_key not in ctx_cache:
            ctx_cache[repo_key] = repo_context(dual_dir / repo_key, maxlen, max_files)
        file_list, snippet = ctx_cache[repo_key]
        sheet_rows.append({"item_id": it, "repo_key": repo_key, "category": cat,
                           "category_zh": CATEGORY_ZH.get(cat, ""),
                           "file_list": file_list, "snippet": snippet})
        key_rows.append({"item_id": it, "repo_key": repo_key, "category": cat,
                         "stratum": k.get("stratum", ""), "sev_bin": k.get("sev_bin", ""),
                         "repo_type": k.get("repo_type", ""),
                         "lang_group": k.get("lang_group", ""),
                         "source_tier": k.get("source_tier", ""),
                         "agcm_present": k.get("agcm_present", 0),
                         "declared": k.get("declared", ""), "n_danger": k.get("n_danger", 0)})
        label_rows.append({"item_id": it, "label": "", "note": ""})
        if cat not in cats:
            cats.append(cat)

    _write_csv(sheet_path, sheet_rows)
    _write_csv(out_dir / KEY_CSV, key_rows)
    _write_csv(out_dir / CODEBOOK_CSV, [
        {"category": c, "名称": CATEGORY_ZH[c], "定义": CATEGORY_DEF[c],
         "正例": CATEGORY_POS[c], "反例": CATEGORY_NEG[c], "边界": CATEGORY_BOUND[c]}
        for c in DANGER_CATEGORIES if c in cats])

    cards: list[Path] = []
    for name in roster:
        card = out_dir / f"{LABEL_PREFIX}{name}.csv"
        if force or not card.exists():
            _write_csv(card, label_rows)
        cards.append(card)

    return {"dir": out_dir, "n_items": len(sheet_rows), "maxlen": maxlen,
            "max_files": max_files, "roster": list(roster),
            "files": [sheet_path, out_dir / KEY_CSV, out_dir / CODEBOOK_CSV] + cards}


def residual_sheet(out_dir: Path, llms: tuple = LLM_DEFAULT,
                   force: bool = False) -> dict:
    """路线 2b **残余分歧子集**：从厚层 ``sheet.csv`` 剔出三家仍分歧的项（**省 token**）。

    判据与 :func:`adjudicate4` Stage-1 **完全一致**：厚层三家 **3/0 一致**
    （``len(set(v3))==1 且 v3[0]∈M2_LABELS``）→ 第 4 家免疫，**剔除**；否则保留。
    产 ``sheet_residual.csv``（列同 ``sheet.csv``），供 ``llm(sheet_name=...)`` 只跑第 4 家。
    **仅供第 4 家**——对三家使用会把其满量卡缩成残余行数（``_dump_m2_rows`` 按 sheet 全行
    回写）而丢失共识项，故调用方须自行守卫。三家卡缺失 → ``SystemExit``。
    """
    out_dir = Path(out_dir)
    sheet_path = out_dir / SHEET_CSV
    if not sheet_path.exists():
        raise SystemExit(f"[x] 未找到 {sheet_path}（先跑 thick_sheet）")
    llms = tuple(llms)
    missing = [n for n in llms if not (out_dir / f"{LABEL_PREFIX}{n}.csv").exists()]
    if missing:
        raise SystemExit(f"[x] 缺 {'/'.join(missing)} 的厚层卡（先跑三家重标）")
    votes_map = {n: _read_label_map(out_dir, n) for n in llms}
    rows = [r for r in _read_csv(sheet_path) if (r.get("item_id") or "").strip()]

    def _consensus(it: str) -> bool:
        v3 = [votes_map[n].get(it, "") for n in llms]
        return len(set(v3)) == 1 and v3[0] in M2_LABELS

    resid = [r for r in rows if not _consensus(r["item_id"])]
    out_path = out_dir / RESIDUAL_SHEET_CSV
    if out_path.exists() and not force:
        raise SystemExit(f"[x] {out_path.name} 已存在（加 --force 覆盖）")
    _write_csv(out_path, resid)
    return {"dir": out_dir, "total": len(rows), "residual": len(resid),
            "consensus": len(rows) - len(resid), "path": out_path}


def _fmt_num(x: float) -> str:
    """数值格式化：整数去小数、其余保留 4 位（避免浮点噪声写入明细 CSV）。"""
    if abs(x - round(x)) < 1e-9:
        return str(int(round(x)))
    return f"{x:.4f}"


def _parse_weights(spec: str | None) -> dict | None:
    """解析 ``"GLM=3,GPT-4o=3"`` 为 ``{模型: 权重}``；空 → ``None``（等权 ρ≡1）。"""
    if not spec:
        return None
    out: dict = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        name, _, val = part.partition("=")
        out[name.strip()] = float(val)
    return out


def adjudicate4(out_dir: Path, model4: str, llms: tuple = LLM_DEFAULT,
                force: bool = False, mode: str = "coequal", tau: float = 2 / 3,
                beta: float = 0.0, weights: dict | None = None) -> dict:
    """路线 2b 第 4 家裁定（**规则② WVA 加权**，``mode="rule1"`` 为规则① 对照）。

    - Stage-1：厚层三家 **3/0 一致** → 直接锁定（``thick-consensus``），**第 4 家免疫**；
    - Stage-2（``mode="coequal"``，默认）：四票语义权重 ``ρ``（默认等权）下
      ``W+ = Σ_{present} ρ``、``W- = Σ_{absent} ρ``、``D = W+ + W- + β·Σ_{unsure} ρ``；
      唯一 ``argmax`` 且 ``W_{w*} ≥ τ·D`` → 采纳（``adj4-llm``），否则 **未决**
      （``unresolved``，fail-closed，label 留空）。
    - Stage-2（``mode="rule1"``，对照）：规则① 退化角 ``τ=1, β=0, ρ≡1``（最高票 ≥3 且唯一）。

    **性质**：第 4 家**只能否决（2:1 遇反向 → 2:2 未决），不能 overrule 多数**，
    但在 coequal 下**可打破 1:1:1 平局**。产 ``adjudication.csv``（item_id,label,note）
    + 明细 ``adjudication4.csv``（item_id, llm_<各家>, n_max, w_present, w_absent,
    w_denom, ratio, status，保留 ``n_max`` 以兼容规则① 对照）。防覆盖已有裁决表。

    **四家闸（fail-closed）**：Stage-2 任一家票缺失（含第 4 家卡为残余子集时的漏项）
    → 该项判 **未决**（``n_missing_vote`` 计数），**不得以 3 票冒充四家**；第 4 家卡整份
    缺失 → ``SystemExit``。
    """
    out_dir = Path(out_dir)
    adj_path = out_dir / ADJUDICATION_CSV
    adj4_path = out_dir / ADJ4_CSV
    if (adj_path.exists() or adj4_path.exists()) and not force:
        raise SystemExit(f"[x] {adj_path.name}/{adj4_path.name} 已存在（加 --force 覆盖）"
                         "—— 勿覆盖已定边的裁决表")
    if mode not in ("coequal", "rule1"):
        raise SystemExit(f"[x] 未知 mode={mode!r}（可选 coequal|rule1）")

    key_rows = _load_keys(out_dir)
    llms = tuple(llms)
    model_cols = list(llms) + ([model4] if model4 not in llms else [])
    if model4 not in llms and not (out_dir / f"{LABEL_PREFIX}{model4}.csv").exists():
        raise SystemExit(f"[x] 缺第 4 家 {model4} 的卡（{LABEL_PREFIX}{model4}.csv）"
                         "—— 四家闸必须有第 4 家票")
    votes_map = {n: _read_label_map(out_dir, n) for n in model_cols}

    rho = {n: 1.0 for n in model_cols}
    if weights:
        for n, v in weights.items():
            if n in rho:
                rho[n] = float(v)
    if mode == "rule1":  # 规则① 退化角：τ=1, β=0, ρ≡1
        tau, beta, rho = 1.0, 0.0, {n: 1.0 for n in model_cols}

    adj_rows: list[dict] = []
    det_rows: list[dict] = []
    n_cons = n_adj = n_unres = n_missing = 0
    for row in key_rows:
        it = row["item_id"]
        v3 = [votes_map[n].get(it, "") for n in llms]
        votes = v3 + ([votes_map[model4].get(it, "")] if model4 not in llms else [])

        # 统一计算语义加权量（明细列 + coequal 判定复用）
        w_pos = sum(rho[n] for n, vv in zip(model_cols, votes) if vv == "present")
        w_neg = sum(rho[n] for n, vv in zip(model_cols, votes) if vv == "absent")
        w_u = sum(rho[n] for n, vv in zip(model_cols, votes) if vv == "unsure")
        denom = w_pos + w_neg + beta * w_u
        ratio = (max(w_pos, w_neg) / denom) if denom > 0 else 0.0

        if len(set(v3)) == 1 and v3[0] in M2_LABELS:
            label, status, n_max = v3[0], SRC_THICK, len(v3)
            n_cons += 1
        else:
            cnt = Counter(votes)
            n_max = max((cnt.get(l, 0) for l in M2_LABELS), default=0)
            if any(not vv for vv in votes):  # 四家闸缺票 → fail-closed，勿以 3 票冒充四家
                label = ""
                n_missing += 1
            elif mode == "rule1":
                top = [l for l in M2_LABELS if cnt.get(l, 0) == n_max]
                label = top[0] if (n_max >= 3 and len(top) == 1) else ""
            elif w_pos == w_neg or denom <= 0:  # 并列 / 无决定性质量 → 未决
                label = ""
            else:
                win = "present" if w_pos > w_neg else "absent"
                label = win if (max(w_pos, w_neg) >= tau * denom - 1e-9) else ""
            status = SRC_ADJ4 if label else SRC_UNRESOLVED
            if label:
                n_adj += 1
            else:
                n_unres += 1

        adj_rows.append({"item_id": it, "label": label, "note": status})
        det = {"item_id": it}
        for n in model_cols:
            det[f"llm_{n}"] = votes_map[n].get(it, "")
        det["n_max"] = n_max
        det["w_present"] = _fmt_num(w_pos)
        det["w_absent"] = _fmt_num(w_neg)
        det["w_denom"] = _fmt_num(denom)
        det["ratio"] = _fmt_num(ratio)
        det["status"] = status
        det_rows.append(det)

    _write_csv(adj_path, adj_rows)
    _write_csv(adj4_path, det_rows)
    return {"dir": out_dir, "n_thick_consensus": n_cons, "n_adjudicated": n_adj,
            "n_unresolved": n_unres, "n_missing_vote": n_missing, "model4": model4,
            "mode": mode, "tau": tau, "beta": beta, "files": [adj_path, adj4_path]}


def cross_audit(thin_dir: Path, thick_dir: Path, out_dir: Path,
                fraction: float = AUDIT_B_FRACTION_DEFAULT, seed: int = SEED_DEFAULT,
                humans: tuple = HUMANS_DEFAULT, force: bool = False) -> dict:
    """路线 2b 跨层抽样盲审：薄层（一致项）+ 厚层（已采纳分歧项）各按比例抽。

    固定 ``seed`` → 可复现。产 ``audit_b.csv``（含 ``layer`` 列，材料按层取：
    薄层取 ``thin/sheet.csv`` 薄材料、厚层取 ``thick/sheet.csv`` 加厚材料）
    + 各盲审者空卡 ``label_audit_b_<human>.csv``。防覆盖已下发的盲审包。
    """
    thin_dir = Path(thin_dir)
    thick_dir = Path(thick_dir)
    out_dir = Path(out_dir)
    audit_path = out_dir / AUDIT_B_CSV
    if audit_path.exists() and not force:
        raise SystemExit(f"[x] {audit_path} 已存在（加 --force 覆盖）—— 勿覆盖已下发的盲审包")

    thin_pool = sorted(r["item_id"] for r in _load_triage(thin_dir)
                       if r.get("status") == "consensus")
    adj_path = thick_dir / ADJUDICATION_CSV
    adj_map = ({r["item_id"]: (r.get("label") or "").strip() for r in _read_csv(adj_path)}
               if adj_path.exists() else {})
    thick_pool = sorted(it for it, lab in adj_map.items() if lab)

    pools = {"thin": thin_pool, "thick": thick_pool}
    live = {k: len(v) for k, v in pools.items() if v}
    k = max(0, min(int(round(fraction * (len(thin_pool) + len(thick_pool)))),
                   len(thin_pool) + len(thick_pool)))
    if not live or k <= 0:
        alloc: dict = {}
    elif k >= len(live):
        alloc = allocate(live, k)
    else:
        alloc = _allocate_nomin(live, k)

    rnd = random.Random(seed)
    thin_pick = rnd.sample(thin_pool, min(alloc.get("thin", 0), len(thin_pool)))
    thick_pick = rnd.sample(thick_pool, min(alloc.get("thick", 0), len(thick_pool)))

    thin_sheet = {r["item_id"]: r for r in _read_csv(thin_dir / SHEET_CSV)}
    thick_sheet_map = {r["item_id"]: r for r in _read_csv(thick_dir / SHEET_CSV)}
    rows: list[dict] = []
    for layer, picked, src in (("thin", thin_pick, thin_sheet),
                               ("thick", thick_pick, thick_sheet_map)):
        for it in picked:
            s = src.get(it, {})
            rows.append({"item_id": it, "layer": layer,
                         "repo_key": s.get("repo_key", ""), "category": s.get("category", ""),
                         "category_zh": s.get("category_zh", ""),
                         "file_list": s.get("file_list", ""), "snippet": s.get("snippet", "")})

    cards: list[Path] = []
    if rows:
        _write_csv(audit_path, rows)
        for h in _audit_b_humans(humans):
            card = out_dir / f"{LABEL_PREFIX}{h}.csv"
            _write_csv(card, [{"item_id": r["item_id"], "label": "", "note": ""} for r in rows])
            cards.append(card)
    return {"dir": out_dir, "n_thin_pool": len(thin_pool), "n_thick_pool": len(thick_pool),
            "n_sampled": len(rows), "fraction": fraction, "seed": seed,
            "files": [audit_path] + cards}


def _jsonable(x):
    """把 ``NaN`` 等非法 JSON 值规整为 ``None``（递归）。"""
    if isinstance(x, float) and x != x:
        return None
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    return x


def route2b_gold(thin_dir: Path, thick_dir: Path, out_dir: Path,
                 humans: tuple = HUMANS_DEFAULT, gold_name: str = LABEL_GOLD,
                 force: bool = False) -> dict:
    """路线 2b 分层金标组装（``label_gold.csv``）+ 分层报告 + 敏感性分析。

    逐项定标（优先级）：
    1. **跨层盲审命中**（``thick/audit_b.csv``）→ 双人共识（``audit-b-human``）；
       双人分歧 → ``audit-b-unresolved``（label 留空）。
    2. **厚层分歧项**（``thick/key.csv`` 命中）→ ``thick/adjudication.csv`` 的来源
       （``thick-consensus``/``adj4-llm``）；未定 → ``unresolved``。
    3. **薄层一致项** → ``thin/triage.csv`` 的 LLM 共识（``thin-consensus``）。

    产 ``label_gold.csv`` + ``gold_report.json``（分层指标 + 敏感性 S-a/S-b/S-c）
    + ``gold_report.csv``（逐项明细）。防覆盖已组装的金标卡。
    """
    thin_dir = Path(thin_dir)
    thick_dir = Path(thick_dir)
    out_dir = Path(out_dir)
    gold_path = out_dir / f"{LABEL_PREFIX}{gold_name}.csv"
    if gold_path.exists() and not force:
        raise SystemExit(f"[x] {gold_path} 已存在（加 --force 覆盖）—— 勿覆盖已组装的金标卡")

    key_rows = _load_keys(thin_dir)
    key_map = {r["item_id"]: r for r in key_rows}
    triage_map = {r["item_id"]: r for r in _load_triage(thin_dir)}
    d_ids = {r["item_id"] for r in _read_csv(thick_dir / KEY_CSV)} \
        if (thick_dir / KEY_CSV).exists() else set()

    adj_path = thick_dir / ADJUDICATION_CSV
    adj_label: dict[str, str] = {}
    adj_note: dict[str, str] = {}
    if adj_path.exists():
        for r in _read_csv(adj_path):
            adj_label[r["item_id"]] = (r.get("label") or "").strip()
            adj_note[r["item_id"]] = (r.get("note") or "").strip()

    audit_path = thick_dir / AUDIT_B_CSV
    audit_ids = ({r["item_id"] for r in _read_csv(audit_path)}
                 if audit_path.exists() else set())
    audit_humans = _audit_b_humans(tuple(humans))
    audit_maps = {h: _read_label_map(thick_dir, h) for h in audit_humans}

    gold_rows: list[dict] = []
    layer_of: dict[str, str] = {}
    for row in key_rows:
        it = row["item_id"]
        layer_of[it] = "thick" if it in d_ids else "thin"
        if it in audit_ids:
            labs = [audit_maps[h].get(it, "") for h in audit_humans]
            if len(set(labs)) == 1 and labs[0] in M2_LABELS:
                gl, src = labs[0], SRC_AUDIT_HUMAN
            else:
                gl, src = "", SRC_AUDIT_UNRES
        elif it in d_ids:
            note = adj_note.get(it, "")
            lab = adj_label.get(it, "")
            if note in (SRC_THICK, SRC_ADJ4) and lab in M2_LABELS:
                gl, src = lab, note
            else:
                gl, src = "", SRC_UNRESOLVED
        else:
            t = triage_map.get(it, {})
            if t.get("status") == "consensus" and t.get("llm_label") in M2_LABELS:
                gl, src = t["llm_label"], SRC_THIN
            else:
                gl, src = "", SRC_UNRESOLVED
        gold_rows.append({"item_id": it, "label": gl, "note": src})

    _write_csv(gold_path, gold_rows)
    gold_map = {r["item_id"]: r["label"] for r in gold_rows}
    note_map = {r["item_id"]: r["note"] for r in gold_rows}
    by_source = Counter(r["note"] for r in gold_rows)

    def _agcm(it: str) -> bool:
        return str(key_map[it].get("agcm_present")) == "1"

    def _metrics(ids: list[str], incl_unres: bool = False) -> dict:
        tp = fp = fn = tn = 0
        n_gold = n_unsure = n_unres = 0
        for it in ids:
            lab = gold_map.get(it, "")
            if lab in ("present", "absent"):
                eff = lab
            elif lab == "unsure":
                n_unsure += 1
                continue
            else:
                n_unres += 1
                if not incl_unres:
                    continue
                eff = "absent"
            n_gold += 1
            pred, truth = _agcm(it), eff == "present"
            if pred and truth:
                tp += 1
            elif pred and not truth:
                fp += 1
            elif not pred and truth:
                fn += 1
            else:
                tn += 1
        p, r, f1 = _prf(tp, fp, fn)
        return {"n": len(ids), "n_gold": n_gold, "n_unsure": n_unsure,
                "n_unresolved": n_unres, "tp": tp, "fp": fp, "fn": fn, "tn": tn,
                "precision": p, "recall": r, "f1": f1}

    a_map = audit_maps[audit_humans[0]] if audit_humans else {}
    b_map = audit_maps[audit_humans[1]] if len(audit_humans) > 1 else {}

    def _kappa(ids: list[str]) -> float:
        common = [it for it in ids if a_map.get(it) and b_map.get(it)]
        return cohen_kappa_l(a_map, b_map, common)[0]

    all_ids = [r["item_id"] for r in key_rows]
    thin_ids = [it for it in all_ids if layer_of[it] == "thin"]
    thick_ids = [it for it in all_ids if layer_of[it] == "thick"]
    layers = {}
    for name, ids in (("thin", thin_ids), ("thick", thick_ids)):
        m = _metrics(ids)
        m["kappa_hh"] = _kappa(ids)
        layers[name] = m

    overall = _metrics(all_ids)
    overall["kappa_hh"] = _kappa(all_ids)

    def _macro(key: str) -> float:
        vals = [layers[l][key] for l in ("thin", "thick")
                if layers[l]["n_gold"] > 0 and layers[l][key] == layers[l][key]]
        return sum(vals) / len(vals) if vals else float("nan")

    merged = {"precision": overall["precision"], "recall": overall["recall"],
              "f1": overall["f1"]}
    stratified = {"precision": _macro("precision"), "recall": _macro("recall"),
                  "f1": _macro("f1")}
    s_a = {"merged": merged, "stratified": stratified}
    s_b = {"exclude_unresolved": _metrics(all_ids, incl_unres=False),
           "unresolved_as_absent": _metrics(all_ids, incl_unres=True)}
    n_adj4 = sum(1 for it in d_ids if adj_note.get(it) == SRC_ADJ4)
    n_thick_unres = sum(1 for it in d_ids
                        if it not in audit_ids and gold_map.get(it, "") == "")
    s_c = {"n_adj4": n_adj4, "n_thick_unresolved": n_thick_unres,
           "share": (n_adj4 / len(d_ids)) if d_ids else float("nan")}

    report = {"n_items": len(gold_rows), "by_source": dict(by_source),
              "layers": layers, "overall": overall,
              "sensitivity": {"S-a": s_a, "S-b": s_b, "S-c": s_c}}
    (out_dir / GOLD_REPORT_JSON).write_text(
        json.dumps(_jsonable(report), ensure_ascii=False, indent=2), encoding="utf-8")
    _write_csv(out_dir / GOLD_REPORT_CSV, [
        {"item_id": it, "layer": layer_of[it], "source": note_map.get(it, ""),
         "gold": gold_map.get(it, ""), "agcm": "1" if _agcm(it) else "0"}
        for it in all_ids])

    return {"dir": out_dir, "n_items": len(gold_rows), "by_source": dict(by_source),
            "layers": layers, "overall": overall,
            "files": [gold_path, out_dir / GOLD_REPORT_JSON, out_dir / GOLD_REPORT_CSV]}


def route2b_score(thin_dir: Path, thick_dir: Path, out_dir: Path,
                  report_name: str = GOLD_REPORT_CSV,
                  class_name: str = GOLD_CLASS_CSV) -> dict:
    """路线 2b：**分层**逐能力类 P/R/F1 + Wilson CI + gate → ``gold_class_report.csv``。

    读 ``thick_dir/gold_report.csv``（逐项 ``layer/source/gold/agcm``）与
    ``thin_dir/key.csv``（取 ``category``），按 ``(layer, category)`` 分组聚合：
    ``gold ∈ {present,absent}`` 计入点估计分母；``unsure`` / 未决（``gold`` 为空）
    分别计入 ``n_unsure``/``n_unresolved`` 并**剔除**（fail-closed）。
    recall/precision 各配 Wilson 95% CI；recall 按预注册阈值（安全关键类 ≥0.90）判 gate。

    纪律：薄层与厚层**不合并**——每层独立逐类 + 每层 ``__overall__`` 行。
    产 ``gold_class_report.csv``。
    """
    from .stats_tests import wilson_ci

    thin_dir = Path(thin_dir)
    thick_dir = Path(thick_dir)
    out_dir = Path(out_dir)
    rep_path = thick_dir / report_name
    if not rep_path.exists():
        raise SystemExit(f"[x] 分层金标报告不存在：{rep_path}（先跑 route2b-gold）")

    cat_of = {r["item_id"]: r["category"] for r in _load_keys(thin_dir)}

    agg: dict[tuple, dict] = {}
    for r in _read_csv(rep_path):
        layer = (r.get("layer") or "").strip()
        cat = cat_of.get(r["item_id"], "")
        if not layer or not cat:
            continue
        a = agg.setdefault((layer, cat), {"tp": 0, "fp": 0, "fn": 0, "tn": 0,
                                          "n_unsure": 0, "n_unresolved": 0})
        gold = (r.get("gold") or "").strip()
        if gold == "unsure":
            a["n_unsure"] += 1
            continue
        if gold not in ("present", "absent"):
            a["n_unresolved"] += 1
            continue
        pred, truth = str(r.get("agcm")) == "1", gold == "present"
        if pred and truth:
            a["tp"] += 1
        elif pred and not truth:
            a["fp"] += 1
        elif not pred and truth:
            a["fn"] += 1
        else:
            a["tn"] += 1

    layer_order = {"thin": 0, "thick": 1}
    cat_order = {c: i for i, c in enumerate(DANGER_CATEGORIES)}
    layers: dict[str, dict] = {}
    out_rows: list[dict] = []
    fails: list[dict] = []
    for layer in sorted({k[0] for k in agg}, key=lambda l: layer_order.get(l, 9)):
        per_class: dict[str, dict] = {}
        pooled = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
        tot_unsure = tot_unres = 0
        cats = sorted({k[1] for k in agg if k[0] == layer},
                      key=lambda c: cat_order.get(c, 99))
        for cat in cats:
            a = agg[(layer, cat)]
            p, rc, f1 = _prf(a["tp"], a["fp"], a["fn"])
            p_lo, p_hi = wilson_ci(a["tp"], a["tp"] + a["fp"])
            r_lo, r_hi = wilson_ci(a["tp"], a["tp"] + a["fn"])
            thr = RECALL_MIN_CRITICAL if cat in SAFETY_CRITICAL else RECALL_MIN_OTHER
            n_gold = a["tp"] + a["fp"] + a["fn"] + a["tn"]
            if rc != rc:
                gate = "no_denominator"
            elif rc < thr:
                gate = "fail"
            else:
                gate = "pass"
            per_class[cat] = {
                "tp": a["tp"], "fp": a["fp"], "fn": a["fn"], "tn": a["tn"],
                "precision": p, "recall": rc, "f1": f1,
                "precision_lo": p_lo, "precision_hi": p_hi,
                "recall_lo": r_lo, "recall_hi": r_hi,
                "n_gold": n_gold, "n_unsure": a["n_unsure"],
                "n_unresolved": a["n_unresolved"],
                "recall_min": thr, "gate": gate,
            }
            if gate == "no_denominator":
                fails.append({"layer": layer, "category": cat,
                              "reason": "no_denominator", "recall": None,
                              "threshold": thr})
            elif gate == "fail":
                fails.append({"layer": layer, "category": cat,
                              "reason": "below_threshold", "recall": rc,
                              "threshold": thr})
            pooled["tp"] += a["tp"]; pooled["fp"] += a["fp"]
            pooled["fn"] += a["fn"]; pooled["tn"] += a["tn"]
            tot_unsure += a["n_unsure"]; tot_unres += a["n_unresolved"]
            out_rows.append({
                "layer": layer, "category": cat,
                "n_gold": n_gold, "tp": a["tp"], "fp": a["fp"],
                "fn": a["fn"], "tn": a["tn"],
                "precision": _fmt(p), "recall": _fmt(rc), "f1": _fmt(f1),
                "precision_lo": _fmt(p_lo), "precision_hi": _fmt(p_hi),
                "recall_lo": _fmt(r_lo), "recall_hi": _fmt(r_hi),
                "n_unsure": a["n_unsure"], "n_unresolved": a["n_unresolved"],
                "recall_min": _fmt(thr), "gate": gate,
            })

        op, orr, of1 = _prf(pooled["tp"], pooled["fp"], pooled["fn"])
        op_lo, op_hi = wilson_ci(pooled["tp"], pooled["tp"] + pooled["fp"])
        or_lo, or_hi = wilson_ci(pooled["tp"], pooled["tp"] + pooled["fn"])
        overall = {"tp": pooled["tp"], "fp": pooled["fp"], "fn": pooled["fn"],
                   "tn": pooled["tn"], "precision": op, "recall": orr, "f1": of1,
                   "precision_lo": op_lo, "precision_hi": op_hi,
                   "recall_lo": or_lo, "recall_hi": or_hi,
                   "n_gold": pooled["tp"] + pooled["fp"] + pooled["fn"] + pooled["tn"],
                   "n_unsure": tot_unsure, "n_unresolved": tot_unres}
        layers[layer] = {"per_class": per_class, "overall": overall}
        out_rows.append({
            "layer": layer, "category": "__overall__",
            "n_gold": overall["n_gold"], "tp": pooled["tp"], "fp": pooled["fp"],
            "fn": pooled["fn"], "tn": pooled["tn"],
            "precision": _fmt(op), "recall": _fmt(orr), "f1": _fmt(of1),
            "precision_lo": _fmt(op_lo), "precision_hi": _fmt(op_hi),
            "recall_lo": _fmt(or_lo), "recall_hi": _fmt(or_hi),
            "n_unsure": tot_unsure, "n_unresolved": tot_unres,
            "recall_min": "", "gate": "",
        })

    cls_path = out_dir / class_name
    _write_csv(cls_path, out_rows)
    gate = {"verdict": "pass" if not fails else "fail-closed", "fails": fails,
            "recall_min_critical": RECALL_MIN_CRITICAL,
            "recall_min_other": RECALL_MIN_OTHER}
    return {"dir": out_dir, "layers": layers, "gate": gate, "files": [cls_path]}


def route2b_report(thick_dir: Path, out_dir: Path,
                   report_name: str = GOLD_REPORT_JSON,
                   class_name: str = GOLD_CLASS_CSV,
                   out_name: str = ROUTE2B_REPORT_JSON,
                   fail_closed: bool = True) -> dict:
    """路线 2b：把分层金标产物组装成 **M9 Gate-B 判定材料**（分层，不合并两层）。

    读 ``thick_dir/gold_report.json``（分层 κ/overall/敏感性）与
    ``thick_dir/gold_class_report.csv``（逐层逐类 P/R/F1 + Wilson CI + gate），
    组装为 M9 ``judge_gate_b`` 可消费的报告：``layers.<layer>.{kappa_hh,
    per_class, overall}`` + ``safety_critical`` + ``fail_closed_accepted``。

    纪律：薄/厚层**各自成层**、互不合并；未决/unsure 已由上游 fail-closed 剔除，
    ``no_denominator``（本层该类金标无正例）在 M9 侧单列 N/A、不计入不达标项。
    """
    thick_dir = Path(thick_dir)
    out_dir = Path(out_dir)
    rep_path = thick_dir / report_name
    cls_path = thick_dir / class_name
    if not rep_path.exists():
        raise SystemExit(f"[x] 分层金标报告不存在：{rep_path}（先跑 route2b-gold）")
    if not cls_path.exists():
        raise SystemExit(f"[x] 分层逐类报告不存在：{cls_path}（先跑 route2b-score）")
    rep = json.loads(rep_path.read_text(encoding="utf-8"))

    def _num(s) -> "float | None":
        s = (s or "").strip()
        if not s:
            return None
        try:
            return float(s)
        except ValueError:
            return None

    def _int(s) -> int:
        s = (s or "").strip()
        return int(s) if s.lstrip("-").isdigit() else 0

    cls_rows = list(_read_csv(cls_path))
    layers: dict[str, dict] = {}
    for layer in ("thin", "thick"):
        lv = (rep.get("layers") or {}).get(layer) or {}
        per_class: dict[str, dict] = {}
        for r in cls_rows:
            if (r.get("layer") or "").strip() != layer:
                continue
            cat = (r.get("category") or "").strip()
            if not cat or cat == "__overall__":
                continue
            per_class[cat] = {
                "recall": _num(r.get("recall")),
                "recall_lo": _num(r.get("recall_lo")),
                "recall_hi": _num(r.get("recall_hi")),
                "precision": _num(r.get("precision")),
                "n_gold": _int(r.get("n_gold")),
                "n_unsure": _int(r.get("n_unsure")),
                "n_unresolved": _int(r.get("n_unresolved")),
                "recall_min": _num(r.get("recall_min")),
                "gate": (r.get("gate") or "").strip(),
            }
        layers[layer] = {
            "kappa_hh": lv.get("kappa_hh"),
            "n": lv.get("n"), "n_gold": lv.get("n_gold"),
            "n_unsure": lv.get("n_unsure"), "n_unresolved": lv.get("n_unresolved"),
            "overall": {"precision": lv.get("precision"),
                        "recall": lv.get("recall"), "f1": lv.get("f1")},
            "per_class": per_class,
        }

    report = {
        "experiment": "M2", "route": "2b",
        "title": "M2 路线 2b 分层金标 → M9 Gate-B 判定材料",
        "source": {"gold_report": str(rep_path), "gold_class_report": str(cls_path)},
        "layers": layers,
        "safety_critical": list(DANGER_CATEGORIES),
        "fail_closed_accepted": bool(fail_closed),
        "fail_closed_note": (
            "存在 recall 有定义且低于阈值的类；里程碑允许以 fail-closed 表述接受："
            "unsure/未决项不计入点估计分母，AGCM 对未决项按 fail-closed 处理；"
            "no_denominator（本层该类金标无正例）单列 N/A、不计入不达标项。"),
    }
    out_path = out_dir / out_name
    out_path.write_text(json.dumps(_jsonable(report), ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return {"dir": out_dir, "path": out_path, "layers": layers,
            "safety_critical": report["safety_critical"],
            "fail_closed_accepted": report["fail_closed_accepted"],
            "files": [out_path]}


# --- M2-R2：可估计性 + 人锚定补做（A 线零标注 + C-lite 补样）--------------
# 预注册：docs/superpowers/plans/2026-10-11-m2r2-estimability-prereg.md
# 纪律：全部产物落 out_dir（默认 M2R2_DIR）；m2\ / m2b\ 只读；fail-closed；
# 盲审扑克不泄露 LLM/AGCM；θ 门槛须零标注、可复算（票数随明细落盘）。

def _layer_key_rows(d: Path) -> list[dict]:
    p = Path(d) / KEY_CSV
    return list(_read_csv(p)) if p.exists() else []


def _layer_sheet_map(d: Path) -> dict:
    p = Path(d) / SHEET_CSV
    return {r["item_id"]: r for r in _read_csv(p)} if p.exists() else {}


def _votes_map(d: Path, models: tuple) -> dict:
    return {n: _read_label_map(d, n) for n in models}


def _agcm_of(k: dict) -> bool:
    return str(k.get("agcm_present")) == "1"


def gold_error_audit(thin_dir: Path, thick_dir: Path, out_dir: Path,
                     humans: tuple = HUMANS_DEFAULT,
                     name: str = GOLD_ERROR_CSV) -> dict:
    """A1：LLM 侧标签 vs 跨层盲审双人共识的逐(层,类)误差率 + Wilson CI。

    读 ``thick/audit_b.csv``（含 ``layer`` 列）取盲审项；LLM 侧标签 thin 层取
    ``thin/triage.csv`` 的 ``llm_label``（``status=consensus``），thick 层取
    ``thick/adjudication.csv`` 中来源为 ``thick-consensus``/``adj4-llm`` 的 ``label``。
    fail-closed：双人分歧/unsure 或无 LLM 标签 → **不计入误差率分母**，分别计入
    ``n_human_unresolved``/``n_human_unsure``/``n_llm_unresolved``。分母 0 → 留空。
    产 ``gold_error_audit.csv``（逐 (层,类) + 每层 ``__overall__``）。
    """
    from .stats_tests import wilson_ci

    thin_dir = Path(thin_dir)
    thick_dir = Path(thick_dir)
    out_dir = Path(out_dir)
    audit_path = thick_dir / AUDIT_B_CSV
    if not audit_path.exists():
        raise SystemExit(f"[x] 跨层盲审包不存在：{audit_path}（先跑 cross-audit）")

    audit_humans = _audit_b_humans(tuple(humans))
    h_maps = {h: _read_label_map(thick_dir, h) for h in audit_humans}

    triage_map: dict[str, str] = {}
    tri_path = thin_dir / TRIAGE_CSV
    if tri_path.exists():
        for r in _read_csv(tri_path):
            if (r.get("status") or "").strip() == "consensus":
                triage_map[r["item_id"]] = (r.get("llm_label") or "").strip()

    adj_llm: dict[str, str] = {}
    adj_path = thick_dir / ADJUDICATION_CSV
    if adj_path.exists():
        for r in _read_csv(adj_path):
            if (r.get("note") or "").strip() in (SRC_THICK, SRC_ADJ4):
                adj_llm[r["item_id"]] = (r.get("label") or "").strip()

    def _llm_label(layer: str, it: str) -> str:
        return triage_map.get(it, "") if layer == "thin" else adj_llm.get(it, "")

    rows = [r for r in _read_csv(audit_path) if (r.get("item_id") or "").strip()]

    def _agg(items: list[dict]) -> dict:
        n_human_unres = n_human_unsure = n_llm_unres = n_compared = n_mismatch = 0
        for r in items:
            it = r["item_id"]
            layer = (r.get("layer") or "").strip()
            labs = [h_maps[h].get(it, "") for h in audit_humans]
            uniq = set(labs)
            if len(uniq) == 1 and labs and labs[0] in ("present", "absent"):
                gold = labs[0]
            elif len(uniq) == 1 and labs and labs[0] == "unsure":
                gold, _u = None, (n_human_unsure := n_human_unsure + 1)
            else:
                gold, _u = None, (n_human_unres := n_human_unres + 1)
            llm = _llm_label(layer, it)
            if llm not in ("present", "absent"):
                n_llm_unres += 1
            if gold is None or llm not in ("present", "absent"):
                continue
            n_compared += 1
            if llm != gold:
                n_mismatch += 1
        er = (n_mismatch / n_compared) if n_compared else float("nan")
        lo, hi = wilson_ci(n_mismatch, n_compared)
        return {"n_audit": len(items), "n_compared": n_compared,
                "n_mismatch": n_mismatch, "error_rate": er,
                "error_lo": lo, "error_hi": hi,
                "n_human_unresolved": n_human_unres,
                "n_human_unsure": n_human_unsure, "n_llm_unresolved": n_llm_unres}

    layer_order = {"thin": 0, "thick": 1}
    cat_order = {c: i for i, c in enumerate(DANGER_CATEGORIES)}
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        layer = (r.get("layer") or "").strip()
        cat = (r.get("category") or "").strip()
        if layer and cat:
            groups[(layer, cat)].append(r)

    out_rows: list[dict] = []
    by_layer: dict[str, dict] = {}
    for layer in sorted({k[0] for k in groups}, key=lambda l: layer_order.get(l, 9)):
        cats = sorted({k[1] for k in groups if k[0] == layer},
                      key=lambda c: cat_order.get(c, 99))
        for cat in cats:
            a = _agg(groups[(layer, cat)])
            out_rows.append({
                "layer": layer, "category": cat, "n_audit": a["n_audit"],
                "n_compared": a["n_compared"], "n_mismatch": a["n_mismatch"],
                "error_rate": _fmt(a["error_rate"]),
                "error_lo": _fmt(a["error_lo"]), "error_hi": _fmt(a["error_hi"]),
                "n_human_unresolved": a["n_human_unresolved"],
                "n_human_unsure": a["n_human_unsure"],
                "n_llm_unresolved": a["n_llm_unresolved"]})
        ov = _agg([r for k, v in groups.items() if k[0] == layer for r in v])
        by_layer[layer] = ov
        out_rows.append({
            "layer": layer, "category": "__overall__", "n_audit": ov["n_audit"],
            "n_compared": ov["n_compared"], "n_mismatch": ov["n_mismatch"],
            "error_rate": _fmt(ov["error_rate"]),
            "error_lo": _fmt(ov["error_lo"]), "error_hi": _fmt(ov["error_hi"]),
            "n_human_unresolved": ov["n_human_unresolved"],
            "n_human_unsure": ov["n_human_unsure"],
            "n_llm_unresolved": ov["n_llm_unresolved"]})

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / name
    _write_csv(out_path, out_rows)
    return {"dir": out_dir, "n_audit": len(rows),
            "n_compared": sum(a["n_compared"] for a in by_layer.values()),
            "n_mismatch": sum(a["n_mismatch"] for a in by_layer.values()),
            "by_layer": by_layer, "files": [out_path]}


def _freeze_split(repos: list[str], holdout: float, seed: int,
                  out_dir: Path, force: bool) -> dict:
    """按 ``repo_key`` 冻结 60/40 划分（``split.csv`` + ``split.sha256``），可复现。"""
    split_path = out_dir / SPLIT_CSV
    sha_path = out_dir / SPLIT_SHA
    if split_path.exists() and not force:
        rows = list(_read_csv(split_path))
    else:
        rnd = random.Random(seed)
        k = max(0, min(int(round(holdout * len(repos))), len(repos)))
        test = set(rnd.sample(list(repos), k))
        rows = [{"repo_key": r, "split": "test" if r in test else "train"}
                for r in sorted(repos)]
        out_dir.mkdir(parents=True, exist_ok=True)
        _write_csv(split_path, rows)
    sha_path.write_text(f"{sha256_file(split_path)}  {SPLIT_CSV}\n", encoding="utf-8")
    return {r["repo_key"]: (r.get("split") or "").strip() for r in rows}


def operating_points(thick_dir: Path, out_dir: Path, layer: str = FINAL_LAYER,
                     seed: int = SEED_DEFAULT, holdout: float = HOLDOUT_FRACTION,
                     n_points: int = OP_POINTS_DEFAULT,
                     models: tuple = LLM_DEFAULT, force: bool = False) -> dict:
    """A2：AGCM × LLM 一致率 **联合门槛** 的 θ 工作点曲线（零标注、可复算）。

    预测重定义（append-only 修正，预注册 §五）：``pred_θ = agcm_present AND
    (p_llm ≥ θ)``，其中 ``p_llm`` = 三家 present 票数 / ``len(models)`` ∈ [0,1]。
    θ 在 [0,1] 等距取 ``n_points`` 点；按 ``repo_key`` 冻结 60/40 划分（先落
    ``split.sha256``），曲线在 held-out 上复算。工作点标签：θ≤1/3 → recall-first；
    1/3<θ≤2/3 → balanced；θ>2/3 → precision-first。整条曲线落盘（禁只报最优点）。
    产 ``operating_points.csv`` + 伴生明细 ``operating_points_items.csv``（票数可复算）。
    """
    from .stats_tests import wilson_ci

    thick_dir = Path(thick_dir)
    out_dir = Path(out_dir)
    rep_path = thick_dir / GOLD_REPORT_CSV
    if not rep_path.exists():
        raise SystemExit(f"[x] 分层金标明细不存在：{rep_path}（先跑 route2b-gold）")
    key_map = {r["item_id"]: r for r in _layer_key_rows(thick_dir)}
    models = tuple(models)
    vote_maps = _votes_map(thick_dir, models) if models else {}

    items: list[dict] = []
    for r in _read_csv(rep_path):
        if (r.get("layer") or "").strip() != layer:
            continue
        it = r["item_id"]
        k = key_map.get(it, {})
        votes = [vote_maps[n].get(it, "") for n in models]
        n_present = sum(1 for v in votes if v == "present")
        p_llm = (n_present / len(models)) if models else float("nan")
        agcm = str(r.get("agcm")).strip() == "1" if (r.get("agcm") or "").strip() \
            else _agcm_of(k)
        items.append({"item_id": it, "repo_key": k.get("repo_key", ""),
                      "category": k.get("category", ""), "layer": layer,
                      "gold": (r.get("gold") or "").strip(),
                      "agcm": agcm, "n_present": n_present, "p_llm": p_llm})

    repos = sorted({it["repo_key"] for it in items if it["repo_key"]})
    split_of = _freeze_split(repos, holdout, seed, out_dir, force)
    for it in items:
        it["split"] = split_of.get(it["repo_key"], "train")

    test_items = [it for it in items if it["split"] == "test"]
    n_train = len(items) - len(test_items)

    out_rows: list[dict] = []
    for i in range(n_points):
        theta = i / (n_points - 1) if n_points > 1 else 0.0
        tp = fp = fn = tn = 0
        for it in test_items:
            gold = it["gold"]
            if gold not in ("present", "absent"):
                continue
            pred = it["agcm"] and (it["p_llm"] >= theta)
            truth = gold == "present"
            if pred and truth:
                tp += 1
            elif pred and not truth:
                fp += 1
            elif not pred and truth:
                fn += 1
            else:
                tn += 1
        p, rc, f1 = _prf(tp, fp, fn)
        pred_map = {it["item_id"]: ("present" if (it["agcm"] and it["p_llm"] >= theta)
                                    else "absent")
                    for it in test_items if it["gold"] in ("present", "absent")}
        truth_map = {it["item_id"]: it["gold"] for it in test_items
                     if it["gold"] in ("present", "absent")}
        kappa = cohen_kappa_l(pred_map, truth_map, list(pred_map))[0]
        p_lo, p_hi = wilson_ci(tp, tp + fp)
        r_lo, r_hi = wilson_ci(tp, tp + fn)
        if theta <= 1 / 3:
            wp = "recall-first"
        elif theta <= 2 / 3:
            wp = "balanced"
        else:
            wp = "precision-first"
        out_rows.append({
            "theta": _fmt(theta), "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": _fmt(p), "recall": _fmt(rc), "f1": _fmt(f1),
            "kappa": _fmt(kappa),
            "precision_lo": _fmt(p_lo), "precision_hi": _fmt(p_hi),
            "recall_lo": _fmt(r_lo), "recall_hi": _fmt(r_hi), "workpoint": wp})

    op_path = out_dir / OPERATING_POINTS_CSV
    _write_csv(op_path, out_rows)
    items_path = out_dir / OP_ITEMS_CSV
    _write_csv(items_path, [{
        "item_id": it["item_id"], "repo_key": it["repo_key"],
        "category": it["category"], "layer": it["layer"], "gold": it["gold"],
        "agcm": "1" if it["agcm"] else "0", "n_present": it["n_present"],
        "p_llm": _fmt(it["p_llm"]), "split": it["split"]} for it in items])
    return {"dir": out_dir, "layer": layer, "n_test": len(test_items),
            "n_train": n_train, "n_points": n_points,
            "split_path": out_dir / SPLIT_CSV, "path": op_path,
            "files": [op_path, items_path, out_dir / SPLIT_CSV, out_dir / SPLIT_SHA]}


def _adj4_accepted(d: Path) -> set:
    """厚层第 4 家按规则② 采纳的项（``adj4-llm``）。"""
    out: set = set()
    p = Path(d) / ADJUDICATION_CSV
    if p.exists():
        for r in _read_csv(p):
            if (r.get("note") or "").strip() == SRC_ADJ4:
                out.add(r["item_id"])
    return out


def _consensus_gold(labs: list, min_votes: int = 2):
    """多标注者标签聚合（方向 D 口径，预注册 §九）：忽略空值与 ``unsure``，取**严格多数**且票数
    ``≥ min_votes``（默认 2），否则返回 ``None``（fail-closed）。

    双人（``min_votes=2``）时退化为「两人全一致且非-unsure」——与 M2-R2 主口径**逐位一致**；
    三人时即「多数表决 + 无多数 fail-closed」。返回 ``present`` / ``absent`` / ``None``。
    """
    vals = [l for l in labs if l in ("present", "absent")]
    if not vals:
        return None
    top, n = Counter(vals).most_common(1)[0]
    if n >= min_votes and n * 2 > len(vals):
        return top
    return None


def enrich_sample(thin_dir: Path, thick_dir: Path, out_dir: Path,
                  targets=C_LITE_TARGETS, n_cand: int = ENRICH_N_CAND_DEFAULT,
                  seed: int = SEED_DEFAULT, humans: tuple = HUMANS_DEFAULT,
                  force: bool = False) -> dict:
    """C-lite：稀有类**富集抽样包**（候选池 = AGCM 判正 ∪ 任一 LLM present ∪ adj4 采纳）。

    对每个 ``(layer, category)`` 目标类，从该层 ``key.csv`` 取该类总体，按上述并集
    构造稀有正例候选池，确定性抽 ``min(n_cand, |pool|)`` 项（排序后按 ``seed`` 抽样）。
    材料取自该层 ``sheet.csv``；产 ``enrich_sheet.csv``（**不含 label/agcm**，盲审不泄露）
    + 各盲审者空卡 ``label_enrich_<human>.csv``。防覆盖已下发的富集盲审包。
    """
    thin_dir = Path(thin_dir)
    thick_dir = Path(thick_dir)
    out_dir = Path(out_dir)
    sheet_path = out_dir / ENRICH_SHEET_CSV
    if sheet_path.exists() and not force:
        raise SystemExit(f"[x] {sheet_path} 已存在（加 --force 覆盖）—— 勿覆盖已下发的富集包")

    dirs = {"thin": thin_dir, "thick": thick_dir}
    rows: list[dict] = []
    for layer, cat in targets:
        d = dirs.get(layer)
        if d is None or not d.is_dir():
            continue
        key_rows = _layer_key_rows(d)
        sheet_map = _layer_sheet_map(d)
        vote_maps = _votes_map(d, LLM_DEFAULT)
        adj_ok = _adj4_accepted(d)
        pool: list[str] = []
        for k in key_rows:
            if (k.get("category") or "") != cat:
                continue
            it = k["item_id"]
            any_llm = any(vote_maps[n].get(it, "") == "present" for n in LLM_DEFAULT)
            if _agcm_of(k) or any_llm or (it in adj_ok):
                pool.append(it)
        pool = sorted(set(pool))
        picked = sorted(random.Random(seed).sample(pool, min(n_cand, len(pool))))
        for it in picked:
            s = sheet_map.get(it, {})
            rows.append({
                "item_id": it, "layer": layer, "repo_key": s.get("repo_key", ""),
                "category": cat, "category_zh": CATEGORY_ZH.get(cat, ""),
                "file_list": s.get("file_list", ""), "snippet": s.get("snippet", "")})

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(sheet_path, rows)
    cards: list[Path] = []
    for h in _enrich_humans(humans):
        card = out_dir / f"{LABEL_PREFIX}{h}.csv"
        if force or not card.exists():
            _write_csv(card, [{"item_id": r["item_id"], "label": "", "note": ""}
                              for r in rows])
        cards.append(card)
    return {"dir": out_dir, "n_items": len(rows), "n_targets": len(targets),
            "targets": list(targets), "files": [sheet_path] + cards}


def post_strat(thin_dir: Path, thick_dir: Path, out_dir: Path,
               humans: tuple = HUMANS_DEFAULT,
               name: str = POST_STRAT_CSV, min_votes: int = 2,
               supp_sheet: Path | None = None) -> dict:
    """C-lite：逐类 P/R —— **两套口径分列**（依预注册 §4.1，二者不得混算）。

    **主口径 = 条件口径（给定候选池）**：富集样本是对候选池的均匀抽样（``|pool|<=n_cand``
    时即全池），故直接以样本计数即得池内无偏 P/R，**不加总体权重**（列
    ``tp/fp/fn/tn/precision/recall/f1``）。pred=AGCM 判正，金标=双人共识。

    **参考口径 = 全总体加权**（列 ``pop_*``）：以 ``w=N_总体层/n_采样层`` 对
    ``(类×AGCM)`` 事后分层加权。因富集样本并非对总体的随机抽样，该口径**不成立、仅作对照**；
    流行度请以 R1 的 N=500 随机集为准（``estimand`` 列标注 ``cond_pool``）。

    **方向 B（预注册 §十）**：``supp_sheet``（缺层补样包，默认 ``None`` → 行为逐位不变）仅把补样行
    并入**参考口径对应缺层**，消除 ``n_strata_missing``；**主口径逐位不变**（补样行不入主口径）。

    fail-closed：金标为 unsure/未决 → 不计入任何口径；某总体层在富集包中无采样 →
    ``n_strata_missing`` 计数、该类 ``gate=incomplete``（针对参考口径）。产 ``post_strat_report.csv``。
    """
    thin_dir = Path(thin_dir)
    thick_dir = Path(thick_dir)
    out_dir = Path(out_dir)
    sheet_path = out_dir / ENRICH_SHEET_CSV
    if not sheet_path.exists():
        raise SystemExit(f"[x] 富集包不存在：{sheet_path}（先跑 enrich-sample）")

    label_maps = {h: _read_label_map(out_dir, f"{ENRICH_PREFIX}{h}") for h in humans}
    # 方向 B：补样卡标签（缺层补样）；参考口径用合并映射（主卡优先 → 主口径行仍用主卡标签）
    supp_maps = {h: _read_label_map(out_dir, f"{ENRICH_SUPP_PREFIX}{h}") for h in humans}
    ref_maps = {h: {**supp_maps[h], **label_maps[h]} for h in humans}

    def _gold(it: str):
        return _consensus_gold([label_maps[h].get(it, "") for h in humans], min_votes)

    def _gold_ref(it: str):
        return _consensus_gold([ref_maps[h].get(it, "") for h in humans], min_votes)

    dirs = {"thin": thin_dir, "thick": thick_dir}
    sheet_rows = [r for r in _read_csv(sheet_path) if (r.get("item_id") or "").strip()]
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in sheet_rows:
        layer = (r.get("layer") or "").strip()
        cat = (r.get("category") or "").strip()
        if layer and cat:
            groups[(layer, cat)].append(r)

    # 方向 B：缺层补样行（独立文件；仅并入参考口径对应缺层）
    supp_groups: dict[tuple, list[dict]] = defaultdict(list)
    if supp_sheet is not None:
        sp = Path(supp_sheet)
        if not sp.exists():
            raise SystemExit(f"[x] 补样包不存在：{sp}（先跑 enrich-supp）")
        for r in _read_csv(sp):
            if not (r.get("item_id") or "").strip():
                continue
            layer = (r.get("layer") or "").strip()
            cat = (r.get("category") or "").strip()
            if layer and cat:
                supp_groups[(layer, cat)].append(r)

    layer_order = {"thin": 0, "thick": 1}
    cat_order = {c: i for i, c in enumerate(DANGER_CATEGORIES)}
    all_keys = set(groups) | set(supp_groups)
    out_rows: list[dict] = []
    n_incomplete = 0
    for layer in sorted({k[0] for k in all_keys}, key=lambda l: layer_order.get(l, 9)):
        d = dirs.get(layer)
        pop = _layer_key_rows(d) if d and d.is_dir() else []
        pop_agcm = {r["item_id"]: _agcm_of(r) for r in pop}
        pop_cat = {r["item_id"]: (r.get("category") or "") for r in pop}
        cats = sorted({k[1] for k in all_keys if k[0] == layer},
                      key=lambda c: cat_order.get(c, 99))
        for cat in cats:
            sampled = groups.get((layer, cat), [])
            # 方向 B：本格补样行（仅并入参考口径对应缺层）
            supp_cell = supp_groups.get((layer, cat), [])
            n_supp = len(supp_cell)
            n_supp_gold = sum(1 for r in supp_cell if _gold_ref(r["item_id"]) is not None)
            n_pop = sum(1 for r in pop if (r.get("category") or "") == cat)

            # ---- 主口径：条件口径（给定候选池，不加权；补样行**不入**，逐位不变）----
            c_tp = c_fp = c_fn = c_tn = 0
            n_gold = 0
            for r in sampled:
                gold = _gold(r["item_id"])
                if gold is None:
                    continue
                n_gold += 1
                pred, truth = bool(pop_agcm.get(r["item_id"], False)), gold == "present"
                if pred and truth:
                    c_tp += 1
                elif pred and not truth:
                    c_fp += 1
                elif not pred and truth:
                    c_fn += 1
                else:
                    c_tn += 1
            p_c, r_c, f1_c = _prf(c_tp, c_fp, c_fn)

            # ---- 参考口径：全总体加权（不成立，仅对照）；补样行并入对应缺层 ----
            n_strata = 0
            n_missing = 0
            p_tp = p_fp = p_fn = p_tn = 0.0
            ref_pool = sampled + supp_cell
            for av in (True, False):
                pop_s = [it for it, a in pop_agcm.items()
                         if a == av and pop_cat.get(it) == cat]
                if not pop_s:
                    continue
                n_strata += 1
                s = [r for r in ref_pool if pop_agcm.get(r["item_id"]) == av]
                if not s:
                    n_missing += 1
                    continue
                w = len(pop_s) / len(s)
                for r in s:
                    gold = _gold_ref(r["item_id"])
                    if gold is None:
                        continue
                    pred, truth = av, gold == "present"
                    if pred and truth:
                        p_tp += w
                    elif pred and not truth:
                        p_fp += w
                    elif not pred and truth:
                        p_fn += w
                    else:
                        p_tn += w
            p_p, r_p, f1_p = _prf(p_tp, p_fp, p_fn)

            gate = "incomplete" if n_missing > 0 else "complete"
            if gate == "incomplete":
                n_incomplete += 1
            out_rows.append({
                "layer": layer, "category": cat, "n_pop": n_pop,
                "n_sampled": len(sampled), "n_gold": n_gold,
                "n_undecided": len(sampled) - n_gold,
                "tp": c_tp, "fp": c_fp, "fn": c_fn, "tn": c_tn,
                "precision": _fmt(p_c), "recall": _fmt(r_c), "f1": _fmt(f1_c),
                "pop_tp_w": _fmt_num(p_tp), "pop_fp_w": _fmt_num(p_fp),
                "pop_fn_w": _fmt_num(p_fn), "pop_tn_w": _fmt_num(p_tn),
                "pop_precision": _fmt(p_p), "pop_recall": _fmt(r_p), "pop_f1": _fmt(f1_p),
                "n_strata": n_strata, "n_strata_missing": n_missing,
                "n_supp": n_supp, "n_supp_gold": n_supp_gold, "gate": gate,
                "estimand": ESTIMAND_COND_POOL})

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / name
    _write_csv(out_path, out_rows)
    return {"dir": out_dir, "n_incomplete": n_incomplete, "files": [out_path]}


def enrich_supp(thin_dir: Path, thick_dir: Path, out_dir: Path,
                targets=C_LITE_TARGETS, n_cand: int = ENRICH_N_CAND_DEFAULT,
                seed: int = SEED_DEFAULT, humans: tuple = HUMANS_DEFAULT,
                force: bool = False) -> dict:
    """方向 B（预注册 §十）：对**参考口径缺层**（总体非空但富集包 0 采样）做独立补样。

    缺层判定与 ``post_strat`` 参考口径同口径：对每个 ``(layer, category)`` 目标类，按
    ``av = AGCM 判正/判负`` 遍历该层 ``key.csv`` 总体；某层总体非空但现有
    ``enrich_sheet.csv`` 对该层**无采样** → 缺层。对每个缺层从该层总体确定性抽
    ``min(n_cand, |缺层总体|)`` 项（排序后按 ``seed``，同 ``enrich_sample``），材料取自该层
    ``sheet.csv``。产**独立** ``enrich_supp_sheet.csv``（**不含 label/agcm**）+ 补样空卡
    ``label_enrich_supp_<human>.csv``；**不覆盖** ``enrich_sheet.csv`` 与主卡。
    """
    thin_dir = Path(thin_dir)
    thick_dir = Path(thick_dir)
    out_dir = Path(out_dir)
    main_sheet = out_dir / ENRICH_SHEET_CSV
    if not main_sheet.exists():
        raise SystemExit(f"[x] 富集包不存在：{main_sheet}（先跑 enrich-sample）")
    supp_path = out_dir / ENRICH_SUPP_SHEET_CSV
    if supp_path.exists() and not force:
        raise SystemExit(f"[x] {supp_path} 已存在（加 --force 覆盖）—— 勿覆盖已下发的补样包")

    # 现有富集包已采样的 (layer, category) → item_id 集合 + 其 AGCM 层
    sampled: dict[tuple, set] = defaultdict(set)
    for r in _read_csv(main_sheet):
        it = (r.get("item_id") or "").strip()
        layer = (r.get("layer") or "").strip()
        cat = (r.get("category") or "").strip()
        if it and layer and cat:
            sampled[(layer, cat)].add(it)

    dirs = {"thin": thin_dir, "thick": thick_dir}
    rows: list[dict] = []
    n_missing = 0
    for layer, cat in targets:
        d = dirs.get(layer)
        if d is None or not d.is_dir():
            continue
        key_rows = _layer_key_rows(d)
        sheet_map = _layer_sheet_map(d)
        done = sampled.get((layer, cat), set())
        done_agcm = {_agcm_of(k) for k in key_rows if k["item_id"] in done}
        for av in (True, False):
            if av in done_agcm:
                continue  # 该 AGCM 层已有采样 → 非缺层
            pop_s = [k["item_id"] for k in key_rows
                     if (k.get("category") or "") == cat and _agcm_of(k) == av]
            if not pop_s:
                continue
            n_missing += 1
            picked = sorted(random.Random(seed).sample(sorted(pop_s),
                                                       min(n_cand, len(pop_s))))
            for it in picked:
                s = sheet_map.get(it, {})
                rows.append({
                    "item_id": it, "layer": layer, "repo_key": s.get("repo_key", ""),
                    "category": cat, "category_zh": CATEGORY_ZH.get(cat, ""),
                    "file_list": s.get("file_list", ""), "snippet": s.get("snippet", "")})

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(supp_path, rows)
    cards: list[Path] = []
    for h in _enrich_supp_humans(humans):
        card = out_dir / f"{LABEL_PREFIX}{h}.csv"
        if force or not card.exists():
            _write_csv(card, [{"item_id": r["item_id"], "label": "", "note": ""}
                              for r in rows])
        cards.append(card)
    return {"dir": out_dir, "n_items": len(rows), "n_missing": n_missing,
            "targets": list(targets), "files": [supp_path] + cards}


def enrich_disputes(out_dir: Path, humans: tuple = HUMANS_DEFAULT,
                    third: str = ENRICH_THIRD_DEFAULT,
                    min_votes: int = 2, force: bool = False) -> dict:
    """方向 D（预注册 §九）：从富集盲审包中提取**争议项**送第 3 人裁决。

    争议项 = 现有 ``humans`` 依 ``_consensus_gold`` 无共识（即 ``post_strat`` 口径下的
    ``undecided``，含硬冲突与含 ``unsure``）。按 ``item_id`` 去重（跨层重复取 ``thick`` 行材料，
    对齐「系统输出层=thick」），保持首次出现顺序。产 ``enrich_disputes.csv``（盲审包）
    + 第 3 人空卡 ``label_enrich_<third>.csv``。防覆盖已下发的送审包。
    """
    out_dir = Path(out_dir)
    sheet_path = out_dir / ENRICH_SHEET_CSV
    if not sheet_path.exists():
        raise SystemExit(f"[x] 富集包不存在：{sheet_path}（先跑 enrich-sample）")
    dispute_path = out_dir / ENRICH_DISPUTES_CSV
    if dispute_path.exists() and not force:
        raise SystemExit(f"[x] {dispute_path} 已存在（加 --force 覆盖）—— 勿覆盖已下发的送审包")

    label_maps = {h: _read_label_map(out_dir, f"{ENRICH_PREFIX}{h}") for h in humans}

    def _disputed(it: str) -> bool:
        return _consensus_gold([label_maps[h].get(it, "") for h in humans], min_votes) is None

    # 按 item_id 去重：跨层重复取 thick 行的材料（dict 保序，替换不改位置）
    seen: dict[str, dict] = {}
    for r in _read_csv(sheet_path):
        it = (r.get("item_id") or "").strip()
        if not it:
            continue
        layer = (r.get("layer") or "").strip()
        if it in seen:
            if layer == "thick" and (seen[it].get("layer") or "").strip() != "thick":
                seen[it] = r
            continue
        seen[it] = r

    rows = [{"item_id": it, "layer": (r.get("layer") or "").strip(),
             "repo_key": r.get("repo_key", ""), "category": r.get("category", ""),
             "category_zh": r.get("category_zh", ""),
             "file_list": r.get("file_list", ""), "snippet": r.get("snippet", "")}
            for it, r in seen.items() if _disputed(it)]

    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(dispute_path, rows)
    card = out_dir / f"{LABEL_PREFIX}{ENRICH_PREFIX}{third}.csv"
    if force or not card.exists():
        _write_csv(card, [{"item_id": r["item_id"], "label": "", "note": ""} for r in rows])
    return {"dir": out_dir, "n_disputed": len(rows), "n_rows": len(rows),
            "third": third, "files": [dispute_path, card]}


# --- S4 LLM 交叉核验：M2 版运行器（三值 present/absent/unsure）----------
# 复用第一篇 annotation.py 的**工具层**（_llm_call 的退避/正文抽取/限流闸/provider 预设），
# 但判定口径（SYSTEM_PROMPT / prompt / 标签归一 / 落卡）是 M2 专用，故在此新建，
# **不修改**第一篇代码。

M2_SYSTEM_PROMPT = (
    "你是 MCP 服务器静态能力审计员。用户会给你一个仓库的文件清单与关键源码片段，"
    "以及一个待判定的「能力类」。请判断该仓库源码中是否存在该类能力。\n"
    "只从下面三值中选一个：\n"
    "- present：源码中存在该类能力的实际调用/实现（见该类定义与正例）\n"
    "- absent：源码中不存在该类能力（含反例情形）\n"
    "- unsure：证据不足或处于边界模糊地带，无法可靠判定\n"
    "判定顺序：先对照该类定义与边界；命中正例语义 → present；命中反例语义或完全无相关"
    "代码 → absent；证据不足以支撑前两者 → unsure。\n"
    "只输出一个 JSON 对象，不要任何解释、前后缀或代码块："
    '{"label": "<present|absent|unsure>", "note": "<不超过 30 字的中文理由>"}'
)

# 三值别名（容忍模型输出中文/大小写/同义词；仅用于归一，不改变判定口径）
_M2_ALIASES = {
    "present": "present", "有": "present", "存在": "present", "是": "present",
    "yes": "present", "true": "present",
    "absent": "absent", "无": "absent", "不存在": "absent", "否": "absent",
    "no": "absent", "false": "absent", "none": "absent",
    "unsure": "unsure", "不确定": "unsure", "存疑": "unsure", "未知": "unsure",
    "maybe": "unsure", "unclear": "unsure",
}


def _norm_m2_label(raw: str) -> str:
    """把模型/人工原始值归一为三值之一；认不出 → ``""``（不臆造）。"""
    s = (raw or "").strip().strip("`\"' ").lower()
    return _M2_ALIASES.get(s, "")


def _m2_codebook_text(category: str) -> str:
    """单类 codebook 全文（LLM 提示词与 codebook.csv 共用同一份，保证人机同卷）。"""
    if category not in CATEGORY_DEF:
        return f"（未知能力类：{category}）"
    return (f"能力类：{category}（{CATEGORY_ZH[category]}）\n"
            f"定义：{CATEGORY_DEF[category]}\n"
            f"正例：{CATEGORY_POS[category]}\n"
            f"反例：{CATEGORY_NEG[category]}\n"
            f"边界：{CATEGORY_BOUND[category]}")


def _m2_llm_prompt(item: dict) -> str:
    """单条盲标 prompt：只给仓库文件清单 + 源码片段 + 该类 codebook。

    **不给** AGCM 判定 / 官方声明 / 分层键（与真人同卷，防锚定偏差）。
    """
    cat = (item.get("category") or "").strip()
    lines = [
        f"仓库：{item.get('repo_key', '')}",
        f"文件清单：{(item.get('file_list') or '').strip()}",
        "",
        "关键源码片段：",
        (item.get("snippet") or "").strip(),
        "",
        "判定标准：",
        _m2_codebook_text(cat),
        "",
        "请判断该仓库源码中是否存在上述能力，只输出三值之一。",
    ]
    return "\n".join(lines)


def _parse_m2_json(text: str, fallback: str = "") -> tuple[str, str]:
    """从模型输出里抠出 ``{"label","note"}``（容忍 ```json 包裹与前后废话）。

    ``label`` 经 :func:`_norm_m2_label` 归一为三值；认不出则用 ``fallback``（亦归一）。
    """
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return "", ""
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return "", ""
    if not isinstance(obj, dict):
        return "", ""
    lb = _norm_m2_label(str(obj.get("label") or ""))
    if not lb and fallback:
        lb = _norm_m2_label(fallback)
    note = str(obj.get("note") or "").strip()
    return lb, note[:120]


def _dump_m2_rows(path: Path, items: list[dict], state: dict[str, dict]) -> None:
    """按 sheet 行序回写标注卡（未标的留空）——M2 三值卡。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(path, [
        {"item_id": it["item_id"],
         "label": state.get(it["item_id"], {}).get("label", ""),
         "note": state.get(it["item_id"], {}).get("note", "")}
        for it in items])


def _read_m2_runs(path: Path) -> dict[tuple[str, int, str], dict]:
    """读 ``reps/runs.csv`` 长表 → ``{(model, rep, item_id): {label, note}}``。

    与 ``annotation._read_runs`` 的差异：标签按 **M2 三值** 归一（而非 E2 五类），
    否则三值判定会被 E2 归一函数清空而静默丢弃。
    """
    out: dict[tuple[str, int, str], dict] = {}
    if not path.exists():
        return out
    for r in _read_csv(path):
        m = (r.get("model") or "").strip()
        rep = (r.get("rep") or "").strip()
        iid = (r.get("item_id") or "").strip()
        lb = _norm_m2_label((r.get("label") or "").strip())
        if m and rep.isdigit() and iid and lb:
            out[(m, int(rep), iid)] = {"label": lb, "note": (r.get("note") or "").strip()}
    return out


def _write_m2_runs(path: Path, runs: dict[tuple[str, int, str], dict]) -> None:
    """逐条判定落盘为长表：每行一个 ``(model, rep, item_id)`` 判定（只记已判定项）。"""
    rows = [{"model": m, "rep": rep, "item_id": iid,
             "label": rec.get("label", ""), "note": rec.get("note", "")}
            for (m, rep, iid), rec in sorted(runs.items()) if rec.get("label")]
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(path, rows)


def _m2_llm_call(prov: dict, prompt: str, api_key: str, timeout: float,
                 system: str = M2_SYSTEM_PROMPT, minimal: bool = False,
                 retries: int | None = None,
                 on_retry=None, client=None) -> str:
    """OpenAI 兼容 chat/completions 调用，返回正文文本（M2 专用系统提示词）。

    与 ``annotation._llm_call`` 的差异：``system`` 显式传入（默认 :data:`M2_SYSTEM_PROMPT`），
    其余退避/正文抽取逻辑复用 ``annotation`` 的常量与工具（``RETRY_STATUS`` /
    ``_retry_wait`` / ``_extract_content``），保证与第一篇一致的健壮性。
    """
    from . import annotation as A
    import httpx
    import time

    if retries is None:
        retries = A.RETRY_TIMES
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    payload: dict = {
        "model": prov["model"],
        "stream": False,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": prompt}],
    }
    if not minimal:
        payload["temperature"] = prov.get("temp", 0.0)

    resp: httpx.Response | None = None
    for attempt in range(1, max(retries, 1) + 1):
        try:
            if client is not None:
                resp = client.post(prov["url"], json=payload, headers=headers)
            else:
                with httpx.Client(timeout=timeout) as own:
                    resp = own.post(prov["url"], json=payload, headers=headers)
        except httpx.HTTPError as exc:
            if attempt >= retries:
                raise RuntimeError(f"网络异常（已重试 {retries} 次）："
                                   f"{type(exc).__name__} {exc}"[:400]) from exc
            wait = min(A.RETRY_BASE * 2 ** (attempt - 1), A.RETRY_MAX)
            if on_retry:
                on_retry(f"网络异常，{wait:.0f}s 后第 {attempt + 1} 次尝试")
            time.sleep(wait)
            continue
        if resp.status_code < 400:
            break
        if resp.status_code not in A.RETRY_STATUS:
            raise RuntimeError(f"HTTP {resp.status_code}：{resp.text.strip()[:400]}")
        if attempt >= retries:
            raise RuntimeError(f"HTTP {resp.status_code}（已重试 {retries} 次）："
                               f"{resp.text.strip()[:400]}")
        wait = A._retry_wait(resp, attempt)
        if on_retry:
            on_retry(f"HTTP {resp.status_code}，{wait:.0f}s 后第 {attempt + 1} 次尝试")
        time.sleep(wait)
    if resp is None:
        raise RuntimeError("请求没有拿到响应")
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"响应不是 JSON：{resp.text.strip()[:300]}")
    text = A._extract_content(data)
    if not text.strip():
        raise RuntimeError("响应里没有正文：" + json.dumps(data, ensure_ascii=False)[:300])
    return text


def _load_providers(path) -> dict:
    """读 providers JSON（CLI ``--providers``）→ ``{模型: {url/model/key}}``。

    顶层可含 ``"default"`` 键，其 url/model/key **先**合并进每个具名模型（具名优先），
    便于「一个中转站 URL/Key 喂三家」。文件缺失 / JSON 损坏 / 顶层非对象 → SystemExit。
    """
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"[x] 未找到 providers 文件：{p}")
    try:
        raw = json.loads(p.read_text("utf-8"))
    except (ValueError, OSError) as exc:
        raise SystemExit(f"[x] providers 解析失败：{type(exc).__name__} {exc}")
    if not isinstance(raw, dict):
        raise SystemExit("[x] providers 顶层须是对象 {模型: {url/model/key}}")
    base = raw.get("default") or {}
    if not isinstance(base, dict):
        raise SystemExit("[x] providers.default 须是对象 {url/model/key}")
    out: dict = {}
    for name, ov in raw.items():
        if name == "default":
            continue
        if ov is not None and not isinstance(ov, dict):
            raise SystemExit(f"[x] providers.{name} 须是对象 {{url/model/key}}")
        out[name] = {**base, **(ov or {})}
    return out


def llm_providers() -> dict:
    """M2 模型注册表 = 第一篇内置预设（``annotation.LLM_PROVIDERS``）∪ 第 4 家扩展。

    GUI 的模型白名单 / 预设 URL·模型名取此；**不改**第一篇 ``annotation`` 常量。
    键序即界面通道顺序（前三家在前，StepFun 在后）。
    """
    from . import annotation as A
    return {**A.LLM_PROVIDERS, **EXTRA_PROVIDERS}


def llm(out_dir: Path, models: list[str], limit: int | None = None, force: bool = False,
        timeout: float = 60.0, sleep: float = 0.6, providers: dict | None = None,
        on_progress=None, should_stop=None, reps: int = 1, workers: int = 1,
        parallel: bool = False, sheet_name: str = SHEET_CSV) -> dict:
    """S4：三家跨家族 LLM 对 ``(仓,类)`` 盲标填卡（按 model+item_id 缓存 ⇒ 可断点续跑）。

    与真人**同卷**：只喂 sheet.csv 的仓库文件清单 + 源码片段 + 该类 codebook，
    **不喂** AGCM 判定 / 官方声明 / 分层；同一 :data:`M2_SYSTEM_PROMPT` 与
    ``temperature=0`` 保证可复现。逐类判定 ``present``/``absent``/``unsure``，
    落 ``label_<模型>.csv``（第 1 轮）；``reps>1`` 时第 2 轮起写 ``reps/r{k}/label_<模型>.csv``，
    逐条判定累积进 ``reps/runs.csv`` 长表（``model,rep,item_id,label,note``）。

    ``workers`` = 每个模型内部的并发线程数（默认 1 = 串行）。``parallel`` = 各模型之间
    **同时**跑（默认 False = 依次跑）；开并行时共享态（``llm_cache.json`` / ``runs.csv``
    落盘）加锁串行写，各模型仍各挂自己的 ``_Throttle`` 限流闸。
    """
    from . import annotation as A
    import httpx
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed

    reps = max(1, int(reps or 1))
    workers = max(1, min(int(workers or 1), A.WORKERS_MAX))
    run_parallel = bool(parallel and len(models) > 1)
    _shared_lock = threading.Lock()   # 护住共享态：cache / runs / result 的并发写
    out_dir = Path(out_dir)
    sheet_path = out_dir / sheet_name
    if not sheet_path.exists():
        raise SystemExit(f"[x] 未找到 {sheet_path}（先跑 sheet）")
    all_items = [r for r in _read_csv(sheet_path) if (r.get("item_id") or "").strip()]
    todo = all_items[:limit] if (limit and limit > 0) else all_items

    cache_path = out_dir / LLM_CACHE_JSON
    cache: dict[str, dict[str, dict]] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text("utf-8"))
        except (ValueError, OSError):
            cache = {}

    runs_path = out_dir / RERUN_DIR / "runs.csv"
    runs = _read_m2_runs(runs_path)

    result: dict = {"dir": out_dir, "items": len(todo), "total": len(all_items),
                    "models": {}, "stopped": False, "reps": reps, "runs": runs_path,
                    "workers": workers}
    def _one(name: str) -> None:
        """跑单个模型：填卡 + 落卡 + 累积 runs（串行/并行两种调度都走这里）。"""
        prov = A.resolve_provider(name, (providers or {}).get(name))
        api_key = (prov.get("key") or os.environ.get(prov.get("env") or "", "")).strip()
        entry: dict = {"model": prov.get("model", ""), "url": prov.get("url", ""),
                       "env": prov.get("env", ""), "filled": 0, "cached": 0, "errors": 0,
                       "retries": 0, "skipped": "", "last_error": "", "stopped": False,
                       "card": None, "reps": reps, "cards": {}, "workers": workers,
                       "throttle": None}
        with _shared_lock:
            result["models"][name] = entry
        if not api_key:
            entry["skipped"] = (f"未设 {prov['env']}" if prov.get("env")
                                else "未提供 API Key")
            return

        minimal = bool(prov.get("minimal"))
        rep_cards: list[Path] = []
        for rep in range(1, reps + 1):
            if should_stop and should_stop():
                entry["stopped"] = True
                break
            ckey = name if rep == 1 else f"{name}#r{rep}"
            with _shared_lock:
                done = dict(cache.get(ckey) or {})
            card = (out_dir / f"{LABEL_PREFIX}{name}.csv" if rep == 1
                    else out_dir / RERUN_DIR / f"r{rep}" / f"{LABEL_PREFIX}{name}.csv")
            pending: list[tuple[int, dict]] = []
            for i, it in enumerate(todo, 1):
                iid = it["item_id"]
                if iid in done and not force:
                    entry["cached"] += 1
                    if on_progress:
                        on_progress({"name": name, "rep": rep, "reps": reps, "i": i,
                                     "total": len(todo), "item_id": iid,
                                     "status": "cached", "filled": entry["filled"],
                                     "cached": entry["cached"], "errors": entry["errors"],
                                     "workers": workers})
                    continue
                pending.append((i, it))

            if not pending:
                print(f"  [{name} r{rep}] 本轮全部命中缓存（{entry['cached']} 条）")
            else:
                lock = threading.Lock()
                t0 = time.time()
                prog = {"n": 0}
                throttle = A._Throttle(workers)

                def _stats() -> dict:
                    el = max(time.time() - t0, 1e-6)
                    with lock:
                        n_done = prog["n"]
                    snap = throttle.snapshot()
                    rate = n_done / el
                    left = max(len(pending) - n_done, 0)
                    return {"elapsed": round(el, 1), "rate": round(rate, 3),
                            "eta": round(left / rate, 1) if rate > 0 else 0.0,
                            "workers_now": snap["workers"], "gap": snap["gap"],
                            "throttled": snap["throttled"]}

                def _retry_note(msg: str, _i: int, _iid: str, _rep: int = rep) -> None:
                    with lock:
                        entry["retries"] += 1
                        if on_progress:
                            on_progress({"name": name, "rep": _rep, "reps": reps, "i": _i,
                                         "total": len(todo), "item_id": _iid,
                                         "status": "retry", "note": msg,
                                         "filled": entry["filled"], "cached": entry["cached"],
                                         "errors": entry["errors"], "workers": workers})

                def _work(i: int, it: dict, client=None) -> None:
                    if should_stop and should_stop():
                        return
                    iid = it["item_id"]
                    ok_item = False
                    hit429 = {"v": False}
                    info: dict = {}

                    def _retry_cb(msg: str) -> None:
                        if "429" in msg:
                            hit429["v"] = True
                        _retry_note(msg, _i=i, _iid=iid)

                    throttle.acquire()
                    try:
                        text = _m2_llm_call(prov, _m2_llm_prompt(it), api_key, timeout,
                                            minimal=minimal, client=client,
                                            on_retry=_retry_cb)
                        lb, note = _parse_m2_json(text, "")
                        if not lb:
                            with lock:
                                entry["errors"] += 1
                                entry["last_error"] = f"{iid}: 解析不出三值标签"
                        else:
                            with lock:
                                done[iid] = {"label": lb, "note": note}
                                entry["filled"] += 1
                            ok_item = True
                    except (httpx.HTTPError, RuntimeError, KeyError, ValueError,
                            TypeError, OSError) as exc:
                        with lock:
                            entry["errors"] += 1
                            entry["last_error"] = f"{iid}: {type(exc).__name__} {exc}"[:400]
                            first = entry["errors"] <= 3
                        if first:
                            print(f"  [{name} r{rep}] {iid} 调用失败："
                                  f"{type(exc).__name__} {exc}")
                    finally:
                        info = throttle.release(hit429["v"])
                    with lock:
                        prog["n"] += 1
                    if info.get("changed") and on_progress:
                        note = (f"限流降档 → {info['workers']} 线程 / 间隔 {info['gap']:.1f}s"
                                if info.get("down") else
                                f"限流恢复 → {info['workers']} 线程 / 间隔 {info['gap']:.1f}s")
                        on_progress({"name": name, "rep": rep, "reps": reps, "i": i,
                                     "total": len(todo), "item_id": iid,
                                     "status": "throttle", "note": note,
                                     "filled": entry["filled"], "cached": entry["cached"],
                                     "errors": entry["errors"], "workers": workers,
                                     "workers_now": info["workers"], "gap": info["gap"]})
                    stats = _stats()
                    with lock:
                        if on_progress:
                            on_progress({"name": name, "rep": rep, "reps": reps, "i": i,
                                         "total": len(todo), "item_id": iid,
                                         "status": "filled" if ok_item else "error",
                                         "label": done.get(iid, {}).get("label", ""),
                                         "note": "" if ok_item else entry["last_error"][:200],
                                         "filled": entry["filled"], "cached": entry["cached"],
                                         "errors": entry["errors"], "workers": workers,
                                         **stats})
                    if sleep:
                        time.sleep(sleep)

                def _dump_card() -> None:
                    try:
                        _dump_m2_rows(card, all_items, done)
                    except OSError as exc:
                        print(f"  [{name} r{rep}] 卡片暂不可写：{type(exc).__name__} {exc}"
                              f"（结果已进缓存，稍后自动重试）")

                def _flush(n: int, total_n: int) -> None:
                    st = _stats()
                    with lock:
                        print(f"  [{name} r{rep}] {n}/{total_n} 已填 {entry['filled']} "
                              f"失败 {entry['errors']} 退避 {entry['retries']}"
                              + (f" · {workers} 线程" if workers > 1 else "")
                              + f" · {st['rate']:.2f} 条/秒 · 剩 {A._fmt_eta(st['eta'])}"
                              + (f" · 并发 {st['workers_now']}/{workers}"
                                 f" 间隔 {st['gap']:.1f}s"
                                 if st["workers_now"] < workers or st["gap"] > 0 else ""))
                    with _shared_lock:
                        cache[ckey] = dict(done)
                        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
                    _dump_card()

                if workers > 1 and len(pending) > 1:
                    with httpx.Client(timeout=timeout) as client, ThreadPoolExecutor(
                            max_workers=workers,
                            thread_name_prefix=f"llm-{name}") as pool:
                        futs = [pool.submit(_work, i, it, client) for i, it in pending]
                        for n, fut in enumerate(as_completed(futs), 1):
                            try:
                                fut.result()
                            except BaseException as exc:  # noqa: BLE001 —— 兜底防线程静默
                                with lock:
                                    entry["errors"] += 1
                                    entry["last_error"] = (f"{type(exc).__name__} "
                                                           f"{exc}")[:400]
                            if n % 20 == 0 or n == len(pending):
                                _flush(n, len(pending))
                            if should_stop and should_stop():
                                entry["stopped"] = True
                                break
                else:
                    for n, (i, it) in enumerate(pending, 1):
                        if should_stop and should_stop():
                            entry["stopped"] = True
                            break
                        _work(i, it)
                        if n % 20 == 0 or n == len(pending):
                            _flush(n, len(pending))

                entry["throttle"] = throttle.snapshot()

            _dump_m2_rows(card, all_items, done)
            entry["cards"][rep] = card
            rep_cards.append(card)
            with _shared_lock:
                for it in todo:
                    iid = it["item_id"]
                    rec = done.get(iid) or {}
                    if rec.get("label"):
                        runs[(name, rep, iid)] = {"label": rec.get("label", ""),
                                                  "note": rec.get("note", "")}
                _write_m2_runs(runs_path, runs)
                cache[ckey] = dict(done)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                                      encoding="utf-8")

            if entry["stopped"]:
                break

        if rep_cards:
            entry["card"] = rep_cards[0]
        if entry["stopped"]:
            with _shared_lock:
                result["stopped"] = True

    if run_parallel:
        with ThreadPoolExecutor(max_workers=len(models),
                                thread_name_prefix="llm-model") as pool:
            for fut in [pool.submit(_one, n) for n in models]:
                fut.result()
    else:
        for n in models:
            _one(n)

    with _shared_lock:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    return result


def _open_frozen(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"[x] 冻结快照不存在：{path}")
    return sqlite3.connect(str(path))


def main(argv: list[str] | None = None) -> None:
    import argparse

    ap = argparse.ArgumentParser(prog="mcp_audit agcm-audit",
                                 description="M2：AGCM 准确率分层普查")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sample", help="S2：分层抽样，冻结清单")
    p.add_argument("--db", type=Path, default=FROZEN_DB)
    p.add_argument("--dual", type=Path, default=DUAL_DIR)
    p.add_argument("--out", type=Path, default=M2_DIR)
    p.add_argument("--n", type=int, default=N_DEFAULT)
    p.add_argument("--seed", type=int, default=SEED_DEFAULT)
    p.add_argument("--force", action="store_true")

    s = sub.add_parser("sheet", help="S3：标注包生成（盲标卡 + 隐藏键 + codebook + 空卡）")
    s.add_argument("--db", type=Path, default=FROZEN_DB)
    s.add_argument("--dual", type=Path, default=DUAL_DIR)
    s.add_argument("--out", type=Path, default=M2_DIR)
    s.add_argument("--maxlen", type=int, default=DEFAULT_MAXLEN)
    s.add_argument("--max-files", type=int, default=CONTEXT_MAX_FILES,
                   help="每仓文件清单上限（默认 80；厚层材料用 200）")
    s.add_argument("--force", action="store_true")

    sc = sub.add_parser("score", help="S5：统计（per-class κ / P / R / F1 / Wilson CI + gate）")
    sc.add_argument("--out", type=Path, default=M2_DIR)

    mx = sub.add_parser("matrix", help="S5：三方混淆矩阵（官方声明 vs AGCM vs 人工）")
    mx.add_argument("--out", type=Path, default=M2_DIR)

    gd = sub.add_parser("gold", help="S6：争议裁决 → 金标卡 label_gold.csv → 复算")
    gd.add_argument("--out", type=Path, default=M2_DIR)
    gd.add_argument("--force", action="store_true")

    lm = sub.add_parser("llm", help="S4：三家跨家族 LLM 交叉核验（三值判定 → label_<模型>.csv）")
    lm.add_argument("--out", type=Path, default=M2_DIR)
    lm.add_argument("--models", nargs="+", default=list(LLM_DEFAULT))
    lm.add_argument("--limit", type=int, default=None)
    lm.add_argument("--force", action="store_true")
    lm.add_argument("--timeout", type=float, default=60.0)
    lm.add_argument("--sleep", type=float, default=0.6)
    lm.add_argument("--reps", type=int, default=1)
    lm.add_argument("--workers", type=int, default=1)
    lm.add_argument("--parallel", action="store_true",
                    help="各模型之间同时跑（默认依次串行）；共享态加锁串行写")
    lm.add_argument("--providers", type=Path, default=None,
                    help="providers JSON 路径（覆盖各模型 url/model/key；支持 default 共享）")
    lm.add_argument("--sheet", default=SHEET_CSV,
                    help="标注包文件名（默认 sheet.csv；跑第 4 家残余子集用 sheet_residual.csv）")

    tr = sub.add_parser("triage", help="路线2：3-LLM 一致性分流 → triage.csv + 分歧项裁决表")
    tr.add_argument("--out", type=Path, default=M2_DIR)
    tr.add_argument("--models", nargs="+", default=list(LLM_DEFAULT))
    tr.add_argument("--force", action="store_true")

    au = sub.add_parser("audit-sample", help="路线2：从 LLM 一致项随机抽盲审包 audit_sample.csv")
    au.add_argument("--out", type=Path, default=M2_DIR)
    au.add_argument("--fraction", type=float, default=AUDIT_FRACTION_DEFAULT)
    au.add_argument("--seed", type=int, default=SEED_DEFAULT)
    au.add_argument("--force", action="store_true")

    lg = sub.add_parser("llm-gold", help="路线2：组装 LLM 金标 label_gold.csv（一致+盲审+裁决）→ 复算")
    lg.add_argument("--out", type=Path, default=M2_DIR)
    lg.add_argument("--force", action="store_true")

    ts = sub.add_parser("thick-sheet", help="路线2b：对分歧集 D 生成加厚标注包（maxlen 4000 / max_files 200）")
    ts.add_argument("--thin", type=Path, default=M2_DIR,
                    help="薄层目录（含 triage.csv / key.csv）")
    ts.add_argument("--dual", type=Path, default=DUAL_DIR)
    ts.add_argument("--out", type=Path, default=M2B_DIR)
    ts.add_argument("--maxlen", type=int, default=THICK_MAXLEN)
    ts.add_argument("--max-files", type=int, default=THICK_MAX_FILES)
    ts.add_argument("--force", action="store_true")

    rs = sub.add_parser("residual-sheet",
                        help="路线2b：从厚层 sheet.csv 剔出三家仍分歧的残余子集（省 token，仅供第 4 家）")
    rs.add_argument("--out", type=Path, default=M2B_DIR)
    rs.add_argument("--models", nargs="+", default=list(LLM_DEFAULT),
                    help="厚层已标注的三家（用于判定 3/0 一致 → 免疫）")
    rs.add_argument("--force", action="store_true")

    a4 = sub.add_parser("adjudicate4", help="路线2b：第 4 家 LLM 按规则②（WVA 加权，默认 coequal）/规则① 裁定厚层残余分歧")
    a4.add_argument("--out", type=Path, default=M2B_DIR)
    a4.add_argument("--model4", default=MODEL4_DEFAULT)
    a4.add_argument("--models", nargs="+", default=list(LLM_DEFAULT),
                    help="厚层已标注的三家（用于读取 label_<模型>.csv）")
    a4.add_argument("--mode", choices=("coequal", "rule1"), default="coequal",
                    help="coequal=规则② 加权（C0，默认）；rule1=规则① 否决式（对照）")
    a4.add_argument("--tau", type=float, default=2 / 3, help="采纳所需决定性质量比（默认 2/3）")
    a4.add_argument("--beta", type=float, default=0.0, help="unsure 折减系数（默认 0）")
    a4.add_argument("--weights", default=None,
                    help='语义权重，如 "GLM=3,GPT-4o=3"（默认等权 ρ≡1）')
    a4.add_argument("--force", action="store_true")

    ca = sub.add_parser("cross-audit", help="路线2b：跨层（薄层一致项 + 厚层判定项）盲审抽样 audit_b.csv")
    ca.add_argument("--thin", type=Path, default=M2_DIR)
    ca.add_argument("--thick", type=Path, default=M2B_DIR)
    ca.add_argument("--out", type=Path, default=M2B_DIR)
    ca.add_argument("--fraction", type=float, default=AUDIT_B_FRACTION_DEFAULT)
    ca.add_argument("--seed", type=int, default=SEED_DEFAULT)
    ca.add_argument("--force", action="store_true")

    r2 = sub.add_parser("route2b-gold", help="路线2b：组装分层金标 + 分层报告 + 敏感性 S-a/S-b/S-c")
    r2.add_argument("--thin", type=Path, default=M2_DIR)
    r2.add_argument("--thick", type=Path, default=M2B_DIR)
    r2.add_argument("--out", type=Path, default=M2B_DIR)
    r2.add_argument("--force", action="store_true")

    r2s = sub.add_parser("route2b-score",
                         help="路线2b：分层逐能力类 P/R/F1 + Wilson CI + gate → gold_class_report.csv")
    r2s.add_argument("--thin", type=Path, default=M2_DIR)
    r2s.add_argument("--thick", type=Path, default=M2B_DIR)
    r2s.add_argument("--out", type=Path, default=M2B_DIR)

    r2r = sub.add_parser("route2b-report",
                         help="路线2b：组装 M9 Gate-B 判定材料（分层，不合并两层）→ route2b_report.json")
    r2r.add_argument("--thick", type=Path, default=M2B_DIR)
    r2r.add_argument("--out", type=Path, default=M2B_DIR)
    r2r.add_argument("--no-fail-closed", dest="fail_closed", action="store_false",
                     help="不声明接受 fail-closed（默认声明接受，供 Gate-B 判定）")

    gea = sub.add_parser("gold-error-audit",
                         help="R2-A1：LLM 标签 vs 跨层盲审共识 的逐(层,类)误差率 + Wilson CI")
    gea.add_argument("--thin", type=Path, default=M2_DIR)
    gea.add_argument("--thick", type=Path, default=M2B_DIR)
    gea.add_argument("--out", type=Path, default=M2R2_DIR)

    op = sub.add_parser("operating-points",
                        help="R2-A2：θ 门槛工作点扫描（锁厚层，先冻结 split.sha256）")
    op.add_argument("--thick", type=Path, default=M2B_DIR)
    op.add_argument("--out", type=Path, default=M2R2_DIR)
    op.add_argument("--layer", default=FINAL_LAYER)
    op.add_argument("--holdout", type=float, default=HOLDOUT_FRACTION)
    op.add_argument("--seed", type=int, default=SEED_DEFAULT)
    op.add_argument("--points", type=int, default=OP_POINTS_DEFAULT)
    op.add_argument("--force", action="store_true")

    es = sub.add_parser("enrich-sample",
                        help="R2-C-lite：稀有类富集抽样包（候选池盲审卡，不泄露标签）")
    es.add_argument("--thin", type=Path, default=M2_DIR)
    es.add_argument("--thick", type=Path, default=M2B_DIR)
    es.add_argument("--out", type=Path, default=M2R2_DIR)
    es.add_argument("--n-cand", type=int, default=ENRICH_N_CAND_DEFAULT)
    es.add_argument("--seed", type=int, default=SEED_DEFAULT)
    es.add_argument("--force", action="store_true")

    ps = sub.add_parser("post-strat",
                        help="R2-C-lite：条件口径（给定候选池）逐类 P/R 主列 + 全总体加权 pop_* 参考列")
    ps.add_argument("--thin", type=Path, default=M2_DIR)
    ps.add_argument("--thick", type=Path, default=M2B_DIR)
    ps.add_argument("--out", type=Path, default=M2R2_DIR)
    ps.add_argument("--third", default="",
                    help="第 3 位真人标注者名（如 third）→ 纳入多数表决；缺省仅双人")
    ps.add_argument("--min-votes", type=int, default=2,
                    help="共识所需最小票数（默认 2；双人时等价现状）")
    ps.add_argument("--supp", nargs="?", const=True, default=None,
                    help="方向 B：并入缺层补样包（省略路径时取 --out 下 enrich_supp_sheet.csv）")

    ed = sub.add_parser("enrich-disputes",
                        help="R2-D：从富集盲审包提取争议项 → enrich_disputes.csv + 第 3 人空卡")
    ed.add_argument("--out", type=Path, default=M2R2_DIR)
    ed.add_argument("--third", default=ENRICH_THIRD_DEFAULT,
                    help="第 3 位真人标注者名（默认 third）→ 空卡 label_enrich_<third>.csv")
    ed.add_argument("--min-votes", type=int, default=2,
                    help="判定「无共识」所需最小票数（默认 2，与主口径一致）")
    ed.add_argument("--force", action="store_true")

    esp = sub.add_parser("enrich-supp",
                         help="R2-B：参考口径缺层补样（独立包 enrich_supp_sheet.csv + 补样空卡）")
    esp.add_argument("--thin", type=Path, default=M2_DIR)
    esp.add_argument("--thick", type=Path, default=M2B_DIR)
    esp.add_argument("--out", type=Path, default=M2R2_DIR)
    esp.add_argument("--n-cand", type=int, default=ENRICH_N_CAND_DEFAULT)
    esp.add_argument("--seed", type=int, default=SEED_DEFAULT)
    esp.add_argument("--force", action="store_true")

    args = ap.parse_args(argv)
    if args.cmd == "sample":
        conn = _open_frozen(args.db)
        try:
            res = sample(conn, args.dual, args.out, args.n, args.seed, args.force)
        finally:
            conn.close()
        print(f"[ok] 抽样 {res['n']}/{res['pop']} 仓 → {res['dir']}")
        print(f"     SHA-256 {res['sha256']}")
        for k in sorted(res["tab"]):
            print(f"     {k[0]}×{k[1]}: {res['tab'][k]}")
    elif args.cmd == "sheet":
        conn = _open_frozen(args.db)
        try:
            res = sheet(conn, args.dual, args.out, maxlen=args.maxlen, force=args.force)
        finally:
            conn.close()
        print(f"[ok] 标注包 {res['repos']} 仓 × {len(DANGER_CATEGORIES)} 类 "
              f"= {res['items']} 判定 → {res['dir']}")
        print(f"     标注者：{' / '.join(res['roster'])}")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "score":
        res = score(args.out)
        ov, gate = res["overall"], res["gate"]
        print(f"[ok] 统计 → {res['dir']}（金标来源：{res['gold_source']}）")
        print(f"     总体 κ={ov['kappa_hh']:.3f}  P={ov['precision']:.3f} "
              f"R={ov['recall']:.3f} F1={ov['f1']:.3f}  n_gold={ov['n_gold']}")
        print(f"     未决 {ov['n_unresolved']} / unsure {ov['n_unsure']}")
        print(f"     Gate：{gate['verdict']}（不达标 {len(gate['fails'])} 类）")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "matrix":
        res = matrix(args.out)
        print(f"[ok] 三方混淆矩阵 {len(res['rows'])} 格 → {res['dir']}")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "gold":
        res = gold(args.out, force=args.force)
        ov = res["score"]["overall"]
        print(f"[ok] 金标 {res['n_items']} 项 → {res['dir']}")
        print(f"     共识 {res['n_consensus']} / 争议 {res['n_disputed']}"
              f"（已裁决 {res['n_adjudicated']} / 未裁决 {res['n_unresolved']}）")
        print(f"     复算 κ={ov['kappa_hh']:.3f} P={ov['precision']:.3f} "
              f"R={ov['recall']:.3f} 金标来源：{res['gold_source']}")
        print(f"     Gate：{res['score']['gate']['verdict']}")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "thick-sheet":
        res = thick_sheet(args.thin, args.dual, args.out, maxlen=args.maxlen,
                          max_files=args.max_files, force=args.force)
        print(f"[ok] 加厚标注包 {res['n_items']} 项"
              f"（maxlen {res['maxlen']} / max_files {res['max_files']}）→ {res['dir']}")
        print(f"     标注者：{' / '.join(res['roster'])}")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "residual-sheet":
        res = residual_sheet(args.out, llms=tuple(args.models), force=args.force)
        print(f"[ok] 残余分歧子集 {res['residual']} 项"
              f"（厚层共 {res['total']}，三家 3/0 免疫 {res['consensus']}）→ {res['path'].name}")
        print("     下一步：agcm-audit llm --sheet sheet_residual.csv --models <第4家>")
    elif args.cmd == "adjudicate4":
        res = adjudicate4(args.out, args.model4, llms=tuple(args.models),
                          force=args.force, mode=args.mode, tau=args.tau,
                          beta=args.beta, weights=_parse_weights(args.weights))
        print(f"[ok] 第 4 家裁定（{res['model4']}·{res['mode']}）→ {res['dir']}")
        print(f"     厚层 3/0 锁定 {res['n_thick_consensus']} / 第4家采纳 {res['n_adjudicated']}"
              f" / 未决 {res['n_unresolved']}"
              + (f"（其中缺票 {res['n_missing_vote']}）" if res.get("n_missing_vote") else ""))
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "cross-audit":
        res = cross_audit(args.thin, args.thick, args.out, fraction=args.fraction,
                          seed=args.seed, force=args.force)
        print(f"[ok] 跨层盲审 抽样 {res['n_sampled']}"
              f"（薄池 {res['n_thin_pool']} / 厚池 {res['n_thick_pool']}，"
              f"比例 {res['fraction']:.2f}，种子 {res['seed']}）→ {res['dir']}")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "route2b-gold":
        res = route2b_gold(args.thin, args.thick, args.out, force=args.force)
        ov = res["overall"]
        print(f"[ok] 分层金标 {res['n_items']} 项 → {res['dir']}")
        print(f"     来源：{res['by_source']}")
        print(f"     总体 P={ov['precision']:.3f} R={ov['recall']:.3f}"
              f" F1={ov['f1']:.3f} κ={ov['kappa_hh']:.3f}")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "route2b-score":
        res = route2b_score(args.thin, args.thick, args.out)
        for layer, lv in res["layers"].items():
            ov = lv["overall"]
            print(f"[ok] {layer} 层：n_gold={ov['n_gold']}"
                  f"（unsure {ov['n_unsure']} / 未决 {ov['n_unresolved']}）"
                  f" P={ov['precision']:.3f} R={ov['recall']:.3f} F1={ov['f1']:.3f}")
        print(f"     Gate：{res['gate']['verdict']}（不达标 {len(res['gate']['fails'])} 项）")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "route2b-report":
        res = route2b_report(args.thick, args.out, fail_closed=args.fail_closed)
        print(f"[ok] M9 Gate-B 判定材料（分层）→ {res['path']}")
        for layer, lv in res["layers"].items():
            n_na = sum(1 for m in lv["per_class"].values()
                       if m["gate"] == "no_denominator")
            n_fail = sum(1 for m in lv["per_class"].values() if m["gate"] == "fail")
            k = lv["kappa_hh"]
            print(f"     {layer} 层：κ={k:.3f}  不达标 {n_fail} / N/A {n_na}"
                  f"  （金标 {lv['n_gold']} / unsure {lv['n_unsure']}"
                  f" / 未决 {lv['n_unresolved']}）")
        print(f"     fail_closed_accepted={res['fail_closed_accepted']}")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "gold-error-audit":
        res = gold_error_audit(args.thin, args.thick, args.out)
        print(f"[ok] A1 跨层盲审误差率 {res['n_audit']} 项"
              f"（可比 {res['n_compared']} / 不符 {res['n_mismatch']}）→ {res['dir']}")
        for layer, a in res["by_layer"].items():
            er = _fmt(a["error_rate"])
            print(f"     {layer} 层：n_audit={a['n_audit']} n_compared={a['n_compared']}"
                  f" n_mismatch={a['n_mismatch']} error_rate={er or 'N/A'}"
                  f"（人未决 {a['n_human_unresolved']} / 人 unsure {a['n_human_unsure']}"
                  f" / LLM 未决 {a['n_llm_unresolved']}）")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "operating-points":
        res = operating_points(args.thick, args.out, layer=args.layer,
                               seed=args.seed, holdout=args.holdout,
                               n_points=args.points, force=args.force)
        print(f"[ok] A2 工作点扫描 {res['n_points']} 点（层 {res['layer']}）"
              f"：测试 {res['n_test']} / 训练 {res['n_train']} → {res['path']}")
        print(f"     split 已冻结 → {res['split_path']}")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "enrich-sample":
        res = enrich_sample(args.thin, args.thick, args.out, n_cand=args.n_cand,
                            seed=args.seed, force=args.force)
        print(f"[ok] C-lite 富集抽样 {res['n_items']} 项 / {res['n_targets']} 目标类 → {res['dir']}")
        for tar in res["targets"]:
            print(f"     {tar[0]} / {tar[1]}")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "post-strat":
        humans = HUMANS_DEFAULT + ((args.third,) if args.third else ())
        supp = None
        if args.supp is True:
            supp = args.out / ENRICH_SUPP_SHEET_CSV
        elif args.supp:
            supp = Path(args.supp)
        res = post_strat(args.thin, args.thick, args.out, humans=humans,
                         min_votes=args.min_votes, supp_sheet=supp)
        print(f"[ok] C-lite 事后分层加权 → {res['dir']}"
              f"（未完成层 {res['n_incomplete']}；口径 {'+'.join(humans)}"
              f"{'；并入补样 ' + supp.name if supp else ''}）")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "enrich-supp":
        res = enrich_supp(args.thin, args.thick, args.out, n_cand=args.n_cand,
                          seed=args.seed, force=args.force)
        print(f"[ok] R2-B 缺层补样 {res['n_items']} 项（缺层 {res['n_missing']}）→ {res['dir']}")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "enrich-disputes":
        res = enrich_disputes(args.out, third=args.third,
                              min_votes=args.min_votes, force=args.force)
        print(f"[ok] R2-D 争议项送审 {res['n_disputed']} 项 → {res['dir']}"
              f"（第 3 人 {res['third']}）")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "llm":
        def _prog(e: dict) -> None:
            print(f"  [{e.get('model', '')} r{e.get('rep', 1)}] "
                  f"{e.get('done', 0)}/{e.get('total', 0)} "
                  f"已填 {e.get('filled', 0)} 失败 {e.get('errors', 0)} "
                  f"退避 {e.get('retries', 0)} · {e.get('rate', 0.0):.2f} 条/秒 · "
                  f"剩 {int(e.get('eta', 0))}s")
        providers = _load_providers(args.providers) if args.providers else None
        res = llm(args.out, args.models, limit=args.limit, force=args.force,
                  timeout=args.timeout, sleep=args.sleep, reps=args.reps,
                  workers=args.workers, providers=providers, on_progress=_prog,
                  parallel=args.parallel, sheet_name=args.sheet)
        print(f"[ok] LLM 交叉核验 {res['items']} 项 × {len(res['models'])} 模型"
              f"（{res['reps']} 轮，{res['workers']} 并发，"
              f"模型{'并行' if args.parallel else '串行'}）→ {res['dir']}")
        for name, e in res["models"].items():
            if e.get("skipped"):
                print(f"     {name}: 跳过（{e['skipped']}）")
            else:
                print(f"     {name}: 已填 {e.get('filled', 0)} / 失败 {e.get('errors', 0)}")
        if res["models"] and all(e.get("skipped") for e in res["models"].values()):
            print("     [!] 三家全部跳过：未设 API Key，本次未产生任何标注。"
                  "请设 DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / ZHIPU_API_KEY 后重跑。")
        print(f"     停止：{'是' if res['stopped'] else '否'}")
    elif args.cmd == "triage":
        res = triage(args.out, llms=tuple(args.models), force=args.force)
        print(f"[ok] 一致性分流 {res['n_items']} 项 → {res['dir']}")
        print(f"     LLM 一致 {res['n_consensus']} / 分歧 {res['n_disputed']}（分歧进裁决表）")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "audit-sample":
        res = audit_sample(args.out, fraction=args.fraction, seed=args.seed,
                           force=args.force)
        print(f"[ok] 随机盲审 抽样 {res['n_sampled']}/{res['n_pool']}"
              f"（比例 {res['fraction']:.2f}，种子 {res['seed']}）→ {res['dir']}")
        for f in res["files"]:
            print(f"     {f.name}")
    elif args.cmd == "llm-gold":
        res = llm_gold(args.out, force=args.force)
        ov = res["score"]["overall"]
        print(f"[ok] LLM 金标 {res['n_items']} 项 → {res['dir']}")
        print(f"     LLM 共识 {res['n_llm_consensus']} / 盲审覆盖 {res['n_audit_human']}"
              f" / 人工裁决 {res['n_adjudicated']} / 未决 {res['n_unresolved']}")
        print(f"     复算 P={ov['precision']:.3f} R={ov['recall']:.3f} κ={ov['kappa_hh']:.3f}"
              f" 金标来源：{res['gold_source']}")
        print(f"     Gate：{res['score']['gate']['verdict']}")
        for f in res["files"]:
            print(f"     {f.name}")


if __name__ == "__main__":
    main()
