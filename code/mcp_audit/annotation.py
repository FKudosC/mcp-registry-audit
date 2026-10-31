"""E2 跨源描述分歧的人工标注 + 标注者间信度（kappa）。

研究问题
--------
E1（``desc_diff``）用「跨源描述最高两两相似度」把货号自动分成
``identical / near / partial / different`` 四档。但**自动判定不是标注协议**：
相似度阈值（0.9 / 0.5）是否切在人类语义边界上，无人验证。E2 的目标是补齐
测量学效度 —— 用**人工五类标注**给出真值，再回答两件事：

1. **信度（reliability）**：多名标注者之间一致吗？（Cohen / Fleiss kappa）
2. **效度（validity）**：E1 的自动档能不能预测人工语义判决？（混淆矩阵 +
   良性/有害二值化的 precision / recall）

对象（面准群）
------------
``desc_diff`` 中 ``verdict ∈ {near, partial, different}`` 的**跨源描述分歧货号**。
另两档排除：``identical`` 无分歧、``insufficient`` 有描述的源不足 2 个（无对可比）。
实测面准群 = **6,723**（near 162 / partial 990 / different 5,571）。

标注单元 = 货号（与 E1 逐一对应，便于算 criterion validity）；呈现时把**最分歧
的一对**（相似度最低的源对）标为 focal pair，其余源描述作为上下文。

五类标注（codebook，语义层、分歧严重度递增）
------------------------------------------
  1 equivalent            语义等价 —— 仅措辞/语言/标点/语序差异，能力范围相同
  2 subset                单向包含 —— 一方是另一方的真子集，同向、无矛盾
  3 complementary         互补 —— 各述不同侧面、互不矛盾，合起来才完整
  4 capability_conflict   能力冲突 —— 对同一货号的**功能/能力**描述互相矛盾
  5 identity_conflict     身份冲突 —— 指向**不同项目/作者/仓库** ⇒ 跨源对齐错误

1–3 为**良性分歧**（不该报警），4–5 为**有害分歧**（该报警）。这层二值化是
效度检验的落点：若 E1 的 ``different`` 档真有判别力，则
``P(有害 | different)`` 应显著高于 ``P(有害 | near)``。

标注者（本课题定稿：2 名真人 + 3 家跨家族 LLM）
--------------------------------------------
真人层（金标，**全样本盲标**，是唯一能称 human ground truth 的部分）：
  Me / other —— 各自独立盲标全部 600 条，分歧条目由两人合议定 ``label_final``。
LLM 层（扩展器，用于把结论外推到 6,723 母体）：
  DeepSeek / Claude / GLM —— 三家**不同家族**，避免「一个模型投三票」。
  由 ``annotate llm`` 自动填卡；它们**不参与金标**，只接受「与真人的一致率」检验。

抽样（双因素分层，种子固定 ⇒ 可复现）
------------------------------------
层 = 自动档（near/partial/different）× 长度受限（``len_max > 3·len_min``，沿用
E1 §12 判据）。**已定死 N=600**：near 80 / partial 160 / different 360，层内 50/50。
**长度受限这一维是刻意加的**：E1 主张「different 中有 34.4% 只是长度被迫」，
本抽样正是为直接检验该主张 —— 若成立，``different × 长度受限`` 的
真分歧率应显著低于 ``different × 自由``。

产出（``D:/Data/mcp-audit/annotation/``）
---------------------------------------
  sheet.csv             盲标表（**不含**自动档，避免锚定偏差），最多 5 源描述列
  key.csv               隐藏键：item_id → 自动档 / 分层 / 相似度（标注者不看）
  codebook.csv          五类定义（Excel 可读）
  label_Me.csv          真人标注卡（第 1 位标注者）
  label_other.csv       真人标注卡（第 2 位标注者，独立盲标）
  label_DeepSeek.csv    LLM 标注卡（``annotate llm`` 自动填充）
  label_Claude.csv      LLM 标注卡
  label_GLM.csv         LLM 标注卡
  label_gold.csv        金标卡（``annotate gold`` 按预注册规则合成；**派生卡**，不进 kappa）
  disagreements.csv     score 时产出：标注者分歧条目，供仲裁
  llm_cache.json        LLM 判定的断点续跑缓存（model[#r轮次] → item_id → label）
  llm_ui.json           GUI 里填的**非密钥**配置（端点 URL / 模型名；不存 API Key）
  reps/runs.csv         ``--reps`` 逐轮留档长表（model, rep, item_id, label, note）
  reps/r{k}/label_*.csv 第 2 轮起的重复判定卡（第 1 轮仍写主卡，互不覆盖）
  reps/stability.csv    stability 时产出：同模型自重跑的自一致率 / Fleiss κ / 平票

用法：
    python -m mcp_audit annotate sample                 # 抽 N=600 产标注包
    python -m mcp_audit annotate status                 # 看交卷进度
    python -m mcp_audit annotate score                  # 算 kappa / α + 效度
    python -m mcp_audit annotate score --golden Me      # 指定仲裁基准标注者
    python -m mcp_audit annotate llm                    # 三家 LLM 盲标填卡（需 API key）
    python -m mcp_audit annotate llm --reps 3           # 每模型跑 3 次，逐轮留档
    python -m mcp_audit annotate llm --workers 4        # 每模型 4 线程并发标注（提速，更易触限流）
    python -m mcp_audit annotate stability              # 同模型自重跑稳定性
    python -m mcp_audit annotate gold                   # 合成金标卡（人–人一致 + 5 人众数，不覆盖已填值）
    python -m mcp_audit annotate score --golden gold    # 用金标卡算效度（主分析）
    python -m mcp_audit gui                             # 本地 GUI：填 URL/模型名 → 验证 → 判定
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import re
import sqlite3
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

import httpx

from . import config, db

SOURCE_TYPE = "desc_diff"
ANNOTATION_TYPE = "annotation_label"
DIVERGENT = ("near", "partial", "different")

LABELS = ("equivalent", "subset", "complementary",
          "capability_conflict", "identity_conflict")
BENIGN = ("equivalent", "subset", "complementary")
HARMFUL = ("capability_conflict", "identity_conflict")

LABEL_ZH = {
    "equivalent": "语义等价",
    "subset": "单向包含",
    "complementary": "互补",
    "capability_conflict": "能力冲突",
    "identity_conflict": "身份冲突",
}
LABEL_DEF = {
    "equivalent": "两条描述语义等价：仅措辞/语言/标点/语序不同，能力范围相同",
    "subset": "一方是另一方的真子集：同向、信息量不等，无矛盾",
    "complementary": "互补：各述不同侧面、互不矛盾，合起来才完整",
    "capability_conflict": "能力冲突：对同一货号的功能/能力描述互相矛盾",
    "identity_conflict": "身份冲突：指向不同项目/作者/仓库，跨源对齐错误",
}
LABEL_EXPECT = {
    "equivalent": "near / identical",
    "subset": "partial",
    "complementary": "partial",
    "capability_conflict": "different",
    "identity_conflict": "different",
}

QUOTA = {"near": 40, "partial": 80, "different": 180}
QUOTA_BASE = 300
N_DEFAULT = 600
SEED_DEFAULT = 20260917
LEN_RATIO = 3.0
MAX_SOURCE_COLS = 5
DEFAULT_MAXLEN = 600

LABEL_PREFIX = "label_"
RERUN_DIR = "reps"
WORKERS_DEFAULT = 1
WORKERS_MAX = 16
THROTTLE_RECOVER = 10
THROTTLE_SLEEP_MAX = 8.0
THROTTLE_DOWN_GAP = 0.5
ROSTER_DEFAULT = ("Me", "other", "DeepSeek", "Claude", "GLM")
LLM_NAMES = ("DeepSeek", "Claude", "GLM")
# 派生卡：由其他标注者按规则推出（非独立标注），只作仲裁基准，**不进** kappa 计算
DERIVED_NAMES = ("gold", "final", "majority")

LLM_PROVIDERS = {
    "DeepSeek": {
        "env": "DEEPSEEK_API_KEY",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "temp": 0.0,
    },
    "Claude": {
        "env": "ANTHROPIC_API_KEY",
        "url": "https://api.anthropic.com/v1/chat/completions",
        "model": "claude-haiku-5-5",
        "temp": 0.0,
    },
    "GLM": {
        "env": "ZHIPU_API_KEY",
        "url": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-4-plus",
        "temp": 0.0,
    },
}

SYSTEM_PROMPT = (
    "你是跨源软件描述一致性审计员。用户会给你同一个货号（仓库）在多个来源上的描述。\n"
    "请判断这些描述之间的关系，只从下面五类中选一类：\n"
    "- equivalent：语义等价（仅措辞/语言/标点/语序不同，能力范围相同）\n"
    "- subset：一方是另一方的真子集（同向、信息量不等、无矛盾）\n"
    "- complementary：互补（各述不同侧面、互不矛盾，合起来才完整）\n"
    "- capability_conflict：能力冲突（对同一货号的功能/能力描述互相矛盾）\n"
    "- identity_conflict：身份冲突（指向不同项目/作者/仓库，跨源对齐错误）\n"
    "判定顺序：先看是否指向同一项目（否则 identity_conflict）；再看能力描述是否互相"
    "矛盾（否则 capability_conflict）；最后在良性三类中按信息量关系择一。\n"
    "只输出一个 JSON 对象，不要任何解释、前后缀或代码块："
    '{"label": "<五类之一>", "note": "<不超过 30 字的中文理由>"}'
)

_NORM_RE = re.compile(r"\s+")


def _dir(path: str | None) -> Path:
    return Path(path) if path else config.DATA_DIR / "annotation"


def _clip(text: str, maxlen: int) -> str:
    """超长描述截断（面准群里最长 30,189 字符，p95=960，中位 128）。"""
    text = _NORM_RE.sub(" ", text or "").strip()
    if maxlen and len(text) > maxlen:
        return text[:maxlen] + f" …[截断 {len(text) - maxlen} 字]"
    return text


def _len_confounded(len_min: int | None, len_max: int | None) -> bool:
    """长度受限判据（沿用 E1 §12）：len_max > 3·len_min。"""
    return bool(len_min and len_max and len_max > LEN_RATIO * len_min)


def collect(conn: sqlite3.Connection) -> list[dict]:
    """从 ``desc_diff`` 取出跨源分歧货号，附分层所需元数据。"""
    out: list[dict] = []
    rows = conn.execute(
        "SELECT data_json FROM findings WHERE finding_type = ?", (SOURCE_TYPE,)
    ).fetchall()
    for r in rows:
        p = json.loads(r[0])
        verdict = p.get("verdict")
        if verdict not in DIVERGENT:
            continue
        descs = p.get("descriptions") or {}
        reps = {s: (descs.get(s) or [""])[0] for s in (p.get("desc_sources") or [])}
        reps = {s: d for s, d in reps.items() if d}
        if len(reps) < 2:
            continue
        items = sorted(reps.items())
        best, worst = None, None
        for i, (s1, d1) in enumerate(items):
            for s2, d2 in items[i + 1:]:
                sim = SequenceMatcher(None, d1, d2).ratio()
                if best is None or sim > best[0]:
                    best = (sim, s1, s2)
                if worst is None or sim < worst[0]:
                    worst = (sim, s1, s2)
        out.append({
            "norm_url": p["norm_url"],
            "verdict": verdict,
            "similarity": p.get("similarity"),
            "reps": reps,
            "sources": sorted(reps),
            "len_min": p.get("len_min"),
            "len_max": p.get("len_max"),
            "sim_ceiling": p.get("sim_ceiling"),
            "confounded": _len_confounded(p.get("len_min"), p.get("len_max")),
            "best_pair": (best[1], best[2]) if best else None,
            "worst_pair": (worst[1], worst[2]) if worst else None,
            "worst_sim": round(worst[0], 3) if worst else None,
        })
    return out


def _draw(pop: list[dict], n: int, seed: int) -> list[dict]:
    """双因素分层随机抽样：层内 50/50，某层不足时由另一层补足。"""
    scale = n / QUOTA_BASE
    rnd = random.Random(seed)
    drawn: list[dict] = []
    for verdict, base in QUOTA.items():
        quota = max(1, round(base * scale))
        pool = [p for p in pop if p["verdict"] == verdict]
        conf = [p for p in pool if p["confounded"]]
        free = [p for p in pool if not p["confounded"]]
        rnd.shuffle(conf)
        rnd.shuffle(free)
        half = quota // 2
        take_c = conf[:half]
        take_f = free[:quota - len(take_c)]
        if len(take_c) + len(take_f) < quota:
            take_c = conf[:quota - len(take_f)]
        drawn.extend(take_c + take_f)
    drawn.sort(key=lambda x: x["norm_url"])
    return drawn


def sample(conn: sqlite3.Connection, n: int, seed: int, out_dir: Path,
           force: bool, maxlen: int, roster: tuple[str, ...] = ROSTER_DEFAULT) -> dict:
    """抽分层样本并落标注包（盲标表 + 隐藏键 + 各标注者空卡 + codebook）。"""
    pop = collect(conn)
    picked = _draw(pop, n, seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet = out_dir / "sheet.csv"
    if sheet.exists() and not force:
        raise SystemExit(f"[x] {sheet} 已存在（加 --force 覆盖）—— 勿覆盖已标注的包")

    enc = "utf-8-sig"
    line: list[dict] = []
    key: list[dict] = []
    label_rows: list[dict] = []
    for i, p in enumerate(picked, 1):
        item_id = f"E2-{i:04d}"
        order = p["sources"]
        row = {"item_id": item_id, "norm_url": p["norm_url"],
               "n_desc_sources": len(order)}
        for k in range(MAX_SOURCE_COLS):
            row[f"src{k + 1}"] = order[k] if k < len(order) else ""
            row[f"desc{k + 1}"] = _clip(p["reps"][order[k]], maxlen) if k < len(order) else ""
        line.append(row)
        key.append({
            "item_id": item_id, "norm_url": p["norm_url"],
            "verdict": p["verdict"],
            "stratum": f"{p['verdict']}×{'受限' if p['confounded'] else '自由'}",
            "len_confounded": int(p["confounded"]),
            "similarity": p["similarity"], "sim_ceiling": p["sim_ceiling"],
            "len_min": p["len_min"], "len_max": p["len_max"],
            "best_pair": "+".join(p["best_pair"] or []),
            "worst_pair": "+".join(p["worst_pair"] or []),
            "worst_sim": p["worst_sim"],
        })
        label_rows.append({"item_id": item_id, "label": "", "note": ""})

    _write_csv(sheet, line, enc)
    _write_csv(out_dir / "key.csv", key, enc)
    _write_csv(out_dir / "codebook.csv", [
        {"label": lb, "名称": LABEL_ZH[lb], "定义": LABEL_DEF[lb],
         "E1 自动档预期": LABEL_EXPECT[lb],
         "类别": "良性" if lb in BENIGN else "有害"} for lb in LABELS], enc)

    cards: list[Path] = []
    for name in roster:
        card = out_dir / f"{LABEL_PREFIX}{name}.csv"
        if force or not card.exists():
            _write_csv(card, label_rows, enc)
        cards.append(card)

    tab = Counter((p["verdict"], p["confounded"]) for p in picked)
    return {"dir": out_dir, "n": len(picked), "pop": len(pop), "seed": seed,
            "tab": tab, "roster": list(roster),
            "files": [sheet, out_dir / "key.csv", out_dir / "codebook.csv"] + cards}


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


LABEL_ALIASES: dict[str, str] = {}
for _lb in LABELS:
    LABEL_ALIASES[_lb] = _lb
    LABEL_ALIASES[_lb.upper()] = _lb
    LABEL_ALIASES[LABEL_ZH[_lb]] = _lb
LABEL_ALIASES.update({
    "等价": "equivalent", "相同": "equivalent", "一致": "equivalent",
    "语义相同": "equivalent", "同义": "equivalent",
    "包含": "subset", "子集": "subset", "真子集": "subset", "单向": "subset",
    "互补关系": "complementary", "侧重点不同": "complementary",
    "能力矛盾": "capability_conflict", "功能冲突": "capability_conflict",
    "能力不一致": "capability_conflict",
    "身份矛盾": "identity_conflict", "对齐错误": "identity_conflict",
    "不是同一个": "identity_conflict", "不同项目": "identity_conflict",
})


def _norm_label(raw: str) -> str:
    """把人工填写的标签归一到标准 token（容忍大小写 / 中文名 / 常见同义写法）。

    认不出来就返回空串 —— 由调用方**点名报错**，绝不静默当成有效标注。
    """
    s = (raw or "").strip().strip("`\"' ")
    if not s:
        return ""
    if s in LABELS:
        return s
    return LABEL_ALIASES.get(s, "") or LABEL_ALIASES.get(s.lower(), "")


def _read_labels(path: Path, issues: list[dict] | None = None) -> dict[str, str]:
    """标注卡 → {item_id: label}（只取已填写的）。"""
    return {r["item_id"]: r["label"] for r in _read_label_rows(path, issues) if r["label"]}


def _label_files(out_dir: Path) -> list[Path]:
    """标注卡文件：``label_<标注者>.csv``（含真人卡与 LLM 卡）。"""
    return sorted(out_dir.glob(f"{LABEL_PREFIX}*.csv"))


def _label_name(path: Path) -> str:
    return path.stem[len(LABEL_PREFIX):]


def _all_and_independent_labels(out_dir: Path,
                                issues: list[dict]) -> tuple[dict, dict]:
    """读回全部标注卡 → ``(全部标签, 独立标注者标签)``。

    ``label_gold.csv`` 等 **派生卡**（见 :data:`DERIVED_NAMES`）是由独立标注者按规则
    推出、**非独立标注**，只作仲裁基准。若混进 kappa 会自证虚高 —— 故从「独立标注者」
    中剔除，但仍保留在「全部」里，供 ``--golden`` 点名取用。
    """
    files = _label_files(out_dir)
    all_labels = {_label_name(f): _read_labels(f, issues) for f in files}
    all_labels = {k: v for k, v in all_labels.items() if v}
    independent = {k: v for k, v in all_labels.items() if k not in DERIVED_NAMES}
    return all_labels, independent


def _read_label_rows(path: Path, issues: list[dict] | None = None) -> list[dict]:
    """读标注卡为 ``[{'item_id','label','note'}, ...]``，保持原行序。

    ``label`` 一律经 :func:`_norm_label` 归一；填了但认不出来的值记进 ``issues``
    （**点名牌 + 条目号 + 原值**），不静默丢弃 —— 否则 600 行里少了几条没人发现。
    """
    rows: list[dict] = []
    for row in _read_csv(path):
        iid = (row.get("item_id") or "").strip()
        if not iid:
            continue
        raw = (row.get("label") or "").strip()
        lb = _norm_label(raw)
        if raw and not lb and issues is not None:
            issues.append({"file": path.name, "annotator": _label_name(path),
                           "item_id": iid, "raw": raw[:40]})
        rows.append({"item_id": iid,
                     "label": lb,
                     "note": (row.get("note") or "").strip()})
    return rows


def _dump_label_rows(path: Path, items: list[dict], state: dict[str, dict]) -> None:
    """按 sheet 行序回写标注卡（未标的留空）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(path, [
        {"item_id": it["item_id"],
         "label": state.get(it["item_id"], {}).get("label", ""),
         "note": state.get(it["item_id"], {}).get("note", "")}
        for it in items])


def _read_runs(path: Path) -> dict[tuple[str, int, str], dict]:
    """读 ``reps/runs.csv`` 长表 → ``{(model, rep, item_id): {label, note}}``。"""
    out: dict[tuple[str, int, str], dict] = {}
    for r in _read_csv(path):
        m = (r.get("model") or "").strip()
        rep = (r.get("rep") or "").strip()
        iid = (r.get("item_id") or "").strip()
        lb = _norm_label((r.get("label") or "").strip())
        if m and rep.isdigit() and iid and lb:
            out[(m, int(rep), iid)] = {"label": lb, "note": (r.get("note") or "").strip()}
    return out


def _write_runs(path: Path, runs: dict[tuple[str, int, str], dict]) -> None:
    """逐条判定落盘为长表：每行一个 ``(model, rep, item_id)`` 判定（只记已判定项）。"""
    rows = [{"model": m, "rep": rep, "item_id": iid,
             "label": rec.get("label", ""), "note": rec.get("note", "")}
            for (m, rep, iid), rec in sorted(runs.items()) if rec.get("label")]
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(path, rows)



def _read_key(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in _read_csv(path):
        out[row["item_id"]] = row
    return out


def _codebook_text() -> str:
    """五类定义全文（LLM 提示词与 codebook.csv 共用同一份，保证人机同卷）。"""
    return "\n".join(f"- {lb}（{LABEL_ZH[lb]}）：{LABEL_DEF[lb]}" for lb in LABELS)


def _llm_prompt(item: dict) -> str:
    """单条盲标 prompt：只给来源名 + 描述，**不给**自动档（与真人同卷）。"""
    lines = ["同一货号在多个来源上的描述："]
    for k in range(1, MAX_SOURCE_COLS + 1):
        src = (item.get(f"src{k}") or "").strip()
        desc = (item.get(f"desc{k}") or "").strip()
        if src and desc:
            lines.append(f"[{k}] 来源 {src}：{desc}")
    lines.append("")
    lines.append("五类定义：")
    lines.append(_codebook_text())
    lines.append("")
    lines.append("若这些描述指向不同项目，选 identity_conflict；能力描述互相矛盾，选 "
                 "capability_conflict；否则在 equivalent / subset / complementary 中择一。")
    return "\n".join(lines)


def _parse_model_json(text: str, fallback_label: str) -> tuple[str, str]:
    """从模型输出里抠出 ``{"label","note"}``（容忍 ```json 包裹与前后废话）。"""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return "", ""
    try:
        obj = json.loads(m.group(0))
    except (ValueError, TypeError):
        return "", ""
    if not isinstance(obj, dict):
        return "", ""
    lb = str(obj.get("label") or "").strip().strip("`\"' ")
    note = str(obj.get("note") or "").strip()
    if lb not in LABELS:
        if lb.lower() in [x.lower() for x in LABELS]:
            lb = next(x for x in LABELS if x.lower() == lb.lower())
        else:
            lb = fallback_label
    return lb, note[:120]


def _content_text(value: object) -> str:
    """把 ``message.content`` 归一成 str（兼容 str / 分段数组两种形态）。"""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for p in value:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(str(p.get("text") or p.get("content") or ""))
        return "".join(parts)
    return ""


def _extract_content(data: object) -> str:
    """从 OpenAI 兼容响应里取正文；容忍分段 content / 推理字段 / 纯文本兜底。"""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        ch = choices[0] if isinstance(choices[0], dict) else {}
        msg = ch.get("message") if isinstance(ch.get("message"), dict) else {}
        for cand in (msg.get("content"), msg.get("reasoning_content"),
                     ch.get("text"), data.get("output_text"), data.get("response")):
            text = _content_text(cand)
            if text.strip():
                return text
    return ""


RETRY_STATUS = (408, 409, 425, 429, 500, 502, 503, 504)
RETRY_TIMES = 5
RETRY_BASE = 1.0
RETRY_MAX = 30.0


def _retry_wait(resp: httpx.Response, attempt: int) -> float:
    """限流/服务端错误的等待秒数：优先 ``Retry-After``，否则指数退避。"""
    ra = (resp.headers.get("retry-after") or "").strip()
    if ra:
        try:
            return min(max(float(ra), 0.0), RETRY_MAX)
        except ValueError:
            pass
    return min(RETRY_BASE * (2 ** (attempt - 1)), RETRY_MAX)


def _llm_call(prov: dict, prompt: str, api_key: str, timeout: float,
              minimal: bool = False, retries: int = RETRY_TIMES,
              on_retry: Callable[[str], None] | None = None,
              client: httpx.Client | None = None) -> str:
    """OpenAI 兼容 chat/completions 调用，返回正文文本。

    ``minimal=True`` 时只发 ``model + messages``（去掉 ``temperature``）—— 部分
    第三方网关（如商汤统一网关）不吃额外参数，会直接 400。
    限流与 5xx 会自动退避重试：优先用响应里的 ``Retry-After``，否则按
    1/2/4/8s 指数退避（商汤网关会回 ``429 rpm exhausted``，硬冲只会把 600 条
    全撞成失败）。重试耗尽才抛 ``RuntimeError``，**带上响应正文**（否则只看到
    一个干巴巴的状态码，没法判断是 key 错、模型名错还是参数不兼容）。

    ``client`` 传入可复用的 ``httpx.Client``（keep-alive 连接池，httpx 本身线程安全），
    并发时**整池共用一个**，避免每条都新建 TLS 连接；不传则每次自建（单条调用语义不变）。
    """
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    payload: dict = {
        "model": prov["model"],
        "stream": False,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
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
            wait = min(RETRY_BASE * 2 ** (attempt - 1), RETRY_MAX)
            if on_retry:
                on_retry(f"网络异常，{wait:.0f}s 后第 {attempt + 1} 次尝试")
            time.sleep(wait)
            continue
        if resp.status_code < 400:
            break
        if resp.status_code not in RETRY_STATUS:
            raise RuntimeError(f"HTTP {resp.status_code}：{resp.text.strip()[:400]}")
        if attempt >= retries:
            raise RuntimeError(f"HTTP {resp.status_code}（已重试 {retries} 次）："
                               f"{resp.text.strip()[:400]}")
        wait = _retry_wait(resp, attempt)
        if on_retry:
            on_retry(f"HTTP {resp.status_code}，{wait:.0f}s 后第 {attempt + 1} 次尝试")
        time.sleep(wait)
    if resp is None:
        raise RuntimeError("请求没有拿到响应")
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"响应不是 JSON：{resp.text.strip()[:300]}")
    text = _extract_content(data)
    if not text.strip():
        raise RuntimeError("响应里没有正文：" + json.dumps(data, ensure_ascii=False)[:300])
    return text


_PARAM_ERR_RE = re.compile(
    r"temperature|max_tokens|top_p|unsupported|not support|unknown|invalid[_ ]request"
    r"|invalid[_ ]param|参数", re.I)


def _probe_prompt(out_dir: Path) -> str:
    """验证探针用的样例：优先取标注包的第一条真实条目，退化到内置样例。"""
    sheet = out_dir / "sheet.csv"
    if sheet.exists():
        try:
            for row in _read_csv(sheet):
                if (row.get("item_id") or "").strip() and (row.get("desc2") or "").strip():
                    return _llm_prompt(row)
        except OSError:
            pass
    return _llm_prompt({
        "src1": "official",
        "desc1": ("Weather MCP server exposing current conditions and a 7-day forecast "
                  "for any city."),
        "src2": "mcpso",
        "desc2": "查询任意城市实时天气与未来一周预报的 MCP 服务。",
    })


def verify(prov: dict, api_key: str, timeout: float, out_dir: Path) -> dict:
    """链路验证：**真跑一条样例**，看端点/模型名/key 是否可用且能返回可解析的五类 JSON。

    只在「能真的判出一条」时才算通过 —— 单纯 ping 通不算，因为判定要的是结构化输出。
    首次失败若像「参数不兼容」，自动退到最小载荷再试一次（``minimal`` 模式），
    并把生效模式回传，后续 ``llm`` 按同一模式调用（避免每条都重试一遍）。
    """
    if not api_key:
        return {"ok": False, "ms": 0, "mode": "", "minimal": False,
                "label": "", "note": "", "raw": "", "error": "未填 API Key（环境变量也未设置）"}
    if not (prov.get("url") or "").strip():
        return {"ok": False, "ms": 0, "mode": "", "minimal": False,
                "label": "", "note": "", "raw": "", "error": "未填端点 URL"}
    if not (prov.get("model") or "").strip():
        return {"ok": False, "ms": 0, "mode": "", "minimal": False,
                "label": "", "note": "", "raw": "", "error": "未填模型名"}

    prompt = _probe_prompt(out_dir)
    t0 = time.time()
    mode, minimal = "标准载荷", False
    try:
        text = _llm_call(prov, prompt, api_key, timeout)
    except RuntimeError as exc:
        if not _PARAM_ERR_RE.search(str(exc)):
            return {"ok": False, "ms": int((time.time() - t0) * 1000), "mode": mode,
                    "minimal": False, "label": "", "note": "", "raw": "", "error": str(exc)}
        mode, minimal = "兼容模式（省略 temperature）", True
        try:
            text = _llm_call(prov, prompt, api_key, timeout, minimal=True)
        except (RuntimeError, httpx.HTTPError) as exc2:
            return {"ok": False, "ms": int((time.time() - t0) * 1000), "mode": mode,
                    "minimal": True, "label": "", "note": "", "raw": "", "error": str(exc2)}
    except httpx.HTTPError as exc:
        return {"ok": False, "ms": int((time.time() - t0) * 1000), "mode": mode,
                "minimal": False, "label": "", "note": "", "raw": "",
                "error": f"{type(exc).__name__}: {exc}"}

    lb, note = _parse_model_json(text, "")
    return {"ok": bool(lb), "ms": int((time.time() - t0) * 1000), "mode": mode,
            "minimal": minimal, "label": lb, "note": note, "raw": text.strip()[:400],
            "error": "" if lb else "返回内容里解析不出五类标签（检查是否是对话模型）"}


def cohen_kappa(a: dict[str, str], b: dict[str, str],
                common: list[str]) -> tuple[float, float, float]:
    """Cohen's kappa = (Po - Pe) / (1 - Pe)，返回 (kappa, Po, Pe)。"""
    if not common:
        return float("nan"), float("nan"), float("nan")
    n = len(common)
    po = sum(1 for k in common if a[k] == b[k]) / n
    ca, cb = Counter(a[k] for k in common), Counter(b[k] for k in common)
    pe = sum(ca[lb] * cb[lb] for lb in LABELS) / (n * n)
    kappa = (po - pe) / (1 - pe) if pe != 1 else float("nan")
    return kappa, po, pe


def fleiss_kappa(rows: list[list[str]]) -> tuple[float, float, float]:
    """Fleiss' kappa（≥2 名标注者、同一批条目全部标注），返回 (kappa, P̄, Pe)。"""
    if not rows:
        return float("nan"), float("nan"), float("nan")
    n, raters = len(rows), len(rows[0])
    if raters < 2:
        return float("nan"), float("nan"), float("nan")
    p_bar = 0.0
    for row in rows:
        cnt = Counter(row)
        p_bar += (sum(v * v for v in cnt.values()) - raters) / (raters * (raters - 1))
    p_bar /= n
    tot = n * raters
    prop = {lb: sum(r.count(lb) for r in rows) / tot for lb in LABELS}
    pe = sum(v * v for v in prop.values())
    kappa = (p_bar - pe) / (1 - pe) if pe != 1 else float("nan")
    return kappa, p_bar, pe


def krippendorff_alpha(units: list[dict[str, str]]) -> tuple[float, float, float]:
    """名义型 Krippendorff α（**容忍缺失值**），返回 (α, Do, De)。

    比 Fleiss κ 更适合本设计：真人与 LLM 交卷进度天然不同（一人标满、一家模型
    只跑了 200 条），Fleiss 要求「同一批条目所有标注者都标过」，会把大部分数据
    丢掉；α 按逐条可配对值计算，缺谁都不影响。

    ``units`` = 每条目的 ``{标注者: 标签}``（未标者不出现在该条目的字典里）。
    """
    cats = sorted({lb for u in units for lb in u.values() if lb})
    k = len(cats)
    if k < 2:
        return float("nan"), float("nan"), float("nan")
    idx = {c: i for i, c in enumerate(cats)}
    o = [[0.0] * k for _ in range(k)]
    n = 0
    for u in units:
        vals = [v for v in u.values() if v]
        m = len(vals)
        if m < 2:
            continue
        n += m
        for i in range(m):
            for j in range(m):
                if i != j:
                    o[idx[vals[i]]][idx[vals[j]]] += 1.0 / (m - 1)
    if n < 2:
        return float("nan"), float("nan"), float("nan")
    marg = [sum(row) for row in o]
    do = sum(o[i][j] for i in range(k) for j in range(k) if i != j) / n
    de = sum(marg[i] * marg[j] for i in range(k) for j in range(k)
             if i != j) / (n * (n - 1))
    if de == 0:
        return float("nan"), do, de
    return 1 - do / de, do, de


def _label_quality(kappa: float) -> str:
    """Landis & Koch (1977) 经验分级。"""
    if kappa != kappa:
        return "不可算"
    for cut, name in ((0.81, "几乎完全一致"), (0.61, "高度一致"), (0.41, "中等一致"),
                      (0.21, "尚可"), (0.0, "轻微一致")):
        if kappa >= cut:
            return name
    return "负一致（低于随机）"


def score(lim_dir: Path, golden: str | None) -> dict:
    """读回标注 → 信度（kappa）+ 效度（对 E1 自动档）。"""
    key = _read_key(lim_dir / "key.csv")
    issues: list[dict] = []
    all_labels, labels = _all_and_independent_labels(lim_dir, issues)

    res: dict = {"annotators": labels, "key": key, "n_items": len(key),
                 "invalid": issues}
    if not labels:
        return res

    shared = set.intersection(*[set(v) for v in labels.values()])
    res["shared"] = shared
    res["coverage"] = {k: len(v) for k, v in labels.items()}
    res["dist"] = {k: Counter(v.values()) for k, v in labels.items()}

    names = sorted(labels)
    pairs: list[tuple[str, str, float, float, float, int]] = []
    groups: dict[str, list[tuple[str, str, float, int]]] = defaultdict(list)
    for i, x in enumerate(names):
        for y in names[i + 1:]:
            common = sorted(set(labels[x]) & set(labels[y]))
            kappa, po, pe = cohen_kappa(labels[x], labels[y], common)
            pairs.append((x, y, kappa, po, pe, len(common)))
            hx, hy = x in LLM_NAMES, y in LLM_NAMES
            tag = "机–机" if (hx and hy) else ("人–机" if (hx or hy) else "人–人")
            groups[tag].append((x, y, kappa, len(common)))
    res["pairs"] = pairs
    res["groups"] = groups

    if len(names) >= 2:
        complete = sorted(k for k in shared
                          if all(labels[n].get(k) for n in names))
        res["fleiss"] = fleiss_kappa([[labels[n][k] for n in names] for k in complete])
        res["fleiss_n"] = len(complete)
        units = [{n: labels[n][k] for n in names if labels[n].get(k)}
                 for k in sorted(shared)]
        res["alpha"] = krippendorff_alpha(units)
        res["alpha_n"] = sum(1 for u in units if len(u) >= 2)
    else:
        res["fleiss"] = (float("nan"),) * 3
        res["fleiss_n"] = 0
        res["alpha"] = (float("nan"),) * 3
        res["alpha_n"] = 0

    humans = [n for n in names if n not in LLM_NAMES]
    if golden in all_labels:
        base = golden
    elif humans:
        base = "Me" if "Me" in humans else humans[0]
    else:
        base = names[0]
    res["base"] = base
    gold = all_labels[base]
    res["base_dist"] = Counter(gold.values())
    res["items"] = {k: gold[k] for k in gold}

    conf_by_verdict: dict[str, Counter] = defaultdict(Counter)
    strata: dict[tuple[str, int], Counter] = defaultdict(Counter)
    for item, lb in gold.items():
        meta = key.get(item)
        if not meta:
            continue
        conf_by_verdict[meta["verdict"]][lb] += 1
        strata[(meta["verdict"], int(meta["len_confounded"]))][lb] += 1
    res["conf_by_verdict"] = conf_by_verdict
    res["strata"] = strata
    return res


def _rates(counter: Counter) -> tuple[float, float]:
    tot = sum(counter.values())
    if not tot:
        return float("nan"), float("nan")
    benign = sum(counter[lb] for lb in BENIGN)
    return (tot - benign) / tot, tot


def show_sample(result: dict) -> None:
    print()
    print("=== E2 标注包已生成（跨源描述分歧 · 人工五类标注） ===")
    print(f"面准群（near+partial+different）: {result['pop']}")
    print(f"抽样条数                        : {result['n']}（种子 {result['seed']}）")
    print()
    print("双因素分层（自动档 × 长度受限）:")
    print(f"  {'自动档':<11}{'长度受限':>9}{'长度自由':>9}{'小计':>7}")
    for v in DIVERGENT:
        c, f = result["tab"][(v, True)], result["tab"][(v, False)]
        print(f"  {v:<11}{c:>9}{f:>9}{c + f:>7}")
    print()
    print("产出文件:")
    for p in result["files"]:
        print(f"  {p}")
    print()
    print("下一步（代码只代劳 LLM 层，真人盲标必须亲力亲为）:")
    print("  1) 真人：Me / other 各自**独立盲标** label_Me.csv / label_other.csv 的全部条目")
    print("     （按 codebook.csv 的五类定义填 label 列；sheet.csv 为盲标，不含自动档）")
    print("  2) LLM：设好 DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / ZHIPU_API_KEY 后跑")
    print("     python -m mcp_audit annotate llm")
    print("  3) 跑 score 算 Cohen/Fleiss κ、Krippendorff α 与效度（含人–人/人–机/机–机 分组）")
    print("  提示：sheet.csv / codebook.csv / label_*.csv 用 Excel 直接打开即可（已带 BOM）")


def show_score(result: dict) -> None:
    print()
    print("=== E2 标注信度与效度 ===")
    bad = result.get("invalid") or []
    if bad:
        print(f"[!] 有 {len(bad)} 行标签认不出来（已按缺失处理，改正后重跑）：")
        for it in bad[:20]:
            print(f"    {it['file']} {it['item_id']} → {it['raw']!r}")
        if len(bad) > 20:
            print(f"    ...另有 {len(bad) - 20} 行")
        print()
    if not result.get("annotators"):
        print("未发现 label_*.csv（先在 annotation/ 下按标注卡填写，或跑 annotate llm）")
        return
    print(f"条目数 : {result['n_items']}")
    print()
    print("交卷进度（已填条数）:")
    for name, cov in result["coverage"].items():
        print(f"  {name:<16} {cov} / {result['n_items']}")
    print()
    print("标注类别分布:")
    for name, dist in result["dist"].items():
        tot = sum(dist.values()) or 1
        detail = " ".join(f"{lb}={dist[lb]}({dist[lb] * 100 // tot}%)" for lb in LABELS
                          if dist[lb])
        print(f"  {name:<16} {detail}")

    print()
    print("标注者间信度:")
    if len(result["annotators"]) < 2:
        print("  仅 1 名标注者 → 无法算 inter-rater kappa（需 ≥2 名独立标注）")
    for x, y, kappa, po, pe, n in result["pairs"]:
        grade = _label_quality(kappa)
        print(f"  Cohen κ({x}, {y}) = {kappa:.3f}  [{grade}]"
              f"  Po={po:.3f} Pe={pe:.3f}  共同条目={n}")
    fk, fpo, fpe = result["fleiss"]
    if len(result["annotators"]) >= 3:
        print(f"  Fleiss κ({len(result['annotators'])} 名, 完备条目={result['fleiss_n']})"
              f" = {fk:.3f}  [{_label_quality(fk)}]  P̄={fpo:.3f} Pe={fpe:.3f}")
    alpha, do, de_value = result["alpha"]
    if alpha == alpha:
        print(f"  Krippendorff α（{len(result['annotators'])} 名, 可比条目={result['alpha_n']}）"
              f" = {alpha:.3f}  [{_label_quality(alpha)}]  Do={do:.3f} De={de_value:.3f}")

    for tag in ("人–人", "人–机", "机–机"):
        rows = result.get("groups", {}).get(tag) or []
        if not rows:
            continue
        vals = [k for _, _, k, _ in rows if k == k]
        mean = sum(vals) / len(vals) if vals else float("nan")
        spread = f"{min(vals):.3f}~{max(vals):.3f}" if vals else "—"
        print(f"  {tag} κ（{len(rows)} 对，均值 {mean:.3f}，区间 {spread}）")

    if len(result["annotators"]) >= 2:
        human = [k for _, _, k, _ in result.get("groups", {}).get("人–机") or [] if k == k]
        hh = [k for _, _, k, _ in result.get("groups", {}).get("人–人") or [] if k == k]
        if human and hh:
            gap = abs(sum(human) / len(human) - sum(hh) / len(hh))
            verdict = "代理有效（人–机 ≈ 人–人）" if gap <= 0.1 else "代理存疑（落差 > 0.1，需照实报告）"
            print(f"  判决：人–机 与 人–人 κ 落差 {gap:.3f} → {verdict}")

    print()
    print(f"效度：人工标注（基准标注者 = {result['base']}）vs E1 自动档")
    print(f"  {'E1 自动档':<11}" + "".join(f"{lb:>20}" for lb in LABELS)
          + f"{'小计':>7}{'真有害率':>10}")
    for v in DIVERGENT:
        cnt = result["conf_by_verdict"].get(v, Counter())
        tot = sum(cnt.values())
        if not tot:
            continue
        rate, _ = _rates(cnt)
        print(f"  {v:<11}" + "".join(f"{cnt[lb]:>20}" for lb in LABELS)
              + f"{tot:>7}{rate * 100:>9.1f}%")

    print()
    print("分层真有害率（检验 E1「长度被迫」主张）:")
    for v in DIVERGENT:
        row = []
        for flag, name in ((1, "长度受限"), (0, "长度自由")):
            cnt = result["strata"].get((v, flag), Counter())
            if sum(cnt.values()):
                rate, tot = _rates(cnt)
                row.append(f"{name} {rate * 100:.1f}%（n={tot}）")
        if row:
            print(f"  {v:<11} {'   vs   '.join(row)}")

    if len(result["annotators"]) >= 2:
        names = sorted(result["annotators"])
        dis = [k for k in sorted(result["shared"])
               if len({result["annotators"][n][k] for n in names}) > 1]
        print()
        print(f"标注者分歧条目 : {len(dis)}（已写 disagreements.csv，供仲裁）")
        print(f"  {dis[:8]}{' …' if len(dis) > 8 else ''}")


def gold_card(out_dir: Path) -> dict:
    """按**预注册规则**合成金标卡 ``label_gold.csv``（派生卡，不进 kappa）。

    规则**先于效度结果锁定**（避免事后挑基准）：

    1. 真人 ``Me`` 与 ``other`` 均已标且相同 → 取该值（人–人一致，最可信）；
    2. 否则 5 名独立标注者（2 真人 + 3 LLM）众数票 ≥ ``floor`` → 取众数；
    3. 其余无过半众数者 → **留空**，交人工在 ``disagreements.csv`` 基础上终裁。

    **不覆盖** ``label_gold.csv`` 里已填的值 —— 便于逐条仲裁后重跑本命令增量补全。
    """
    floor = len(ROSTER_DEFAULT) // 2 + 1
    issues: list[dict] = []
    _all_labels, labels = _all_and_independent_labels(out_dir, issues)
    key = _read_key(out_dir / "key.csv") if (out_dir / "key.csv").exists() else {}

    items = list(key) if key else sorted(set().union(*[set(v) for v in labels.values()]))
    path = out_dir / f"{LABEL_PREFIX}gold.csv"
    kept = {r["item_id"]: r for r in (_read_label_rows(path) if path.exists() else [])
            if r["label"]}

    rows: list[dict] = []
    stats: Counter = Counter()
    for iid in items:
        if iid in kept:
            rows.append({"item_id": iid, "label": kept[iid]["label"],
                         "note": kept[iid]["note"]})
            stats["已填保留"] += 1
            continue
        mm = labels.get("Me", {}).get(iid)
        oo = labels.get("other", {}).get(iid)
        if mm and oo and mm == oo:
            value, why = mm, "人–人一致"
        else:
            cnt = Counter(v[iid] for v in labels.values() if v.get(iid))
            top = cnt.most_common()
            if top and top[0][1] >= floor:
                value, why = top[0][0], f"众数≥{floor}"
            else:
                value, why = "", "无众数·待终裁"
        rows.append({"item_id": iid, "label": value, "note": why})
        stats[why] += 1

    _write_csv(path, rows)
    return {"path": path, "n_items": len(rows), "floor": floor, "stats": stats,
            "filled": sum(1 for r in rows if r["label"]),
            "blank": sum(1 for r in rows if not r["label"]),
            "kept": len(kept), "invalid": issues}


def show_gold(result: dict) -> None:
    print()
    print("=== E2 金标卡合成（派生卡 · 不进 kappa） ===")
    bad = result.get("invalid") or []
    if bad:
        print(f"[!] 有 {len(bad)} 行独立卡标签认不出来（已按缺失处理）")
        print()
    print(f"条目数     : {result['n_items']}")
    print(f"已填       : {result['filled']}（其中沿用你已裁决 {result['kept']} 条）")
    print(f"留空待终裁 : {result['blank']}（无 ≥{result['floor']} 众数）")
    print()
    print("合成来源:")
    for why, c in result["stats"].most_common():
        print(f"  {why:<14}{c:>4}")
    print()
    print(f"已写 {result['path']}（**不覆盖**你已填的值）")
    print("  留空条目请人工终裁后回填 label 列，再跑 score；")
    print("下一步: python -m mcp_audit annotate score --golden gold")


def resolve_provider(name: str, override: dict | None = None) -> dict:
    """合并「内置预设」与「调用方覆盖」（GUI 里手填的 url / model / key）。"""
    prov = dict(LLM_PROVIDERS.get(name) or {})
    if override:
        prov.update({k: v for k, v in override.items() if v is not None})
    return prov


def _fmt_eta(sec: float) -> str:
    """把剩余秒数格式化成 ``45s`` / ``3m20s`` / ``1h02m``（进度行用）。"""
    sec = max(0, int(round(float(sec or 0.0))))
    if sec >= 3600:
        return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"
    if sec >= 60:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec}s"


class _Throttle:
    """自适应限流闸：连续 429 自动**降并发 + 加间隔**，连续成功后再回升。

    网关限流（``429 rpm exhausted``）时硬冲只会把整批撞成失败。这里用一个线程安全的
    闸门同时管两件事：

    * **并发额度** —— 池子仍按请求的 ``workers`` 开线程，但真正同时在飞的请求数受
      ``acquire``/``release`` 的额度限制；限流时额度先从 ``workers`` 往 1 收（每次 -1）。
    * **间隔** —— 每个请求在拿到额度后再停顿 ``gap`` 秒；限流时 ``gap`` 每次
      ``+THROTTLE_DOWN_GAP``（上限 ``THROTTLE_SLEEP_MAX``）。

    连续 ``recover`` 次成功即回升一档（先回并发、再收间隔）。无 429 时 ``gap`` 恒为 0、
    额度恒等于 ``workers``，因此 **``workers=1`` 的串行行为与接入前完全一致**。
    """

    def __init__(self, workers: int, gap: float = 0.0,
                 max_gap: float = THROTTLE_SLEEP_MAX,
                 recover: int = THROTTLE_RECOVER) -> None:
        self._max_workers = max(1, int(workers or 1))
        self._workers = self._max_workers
        self._gap_max = max(0.0, float(max_gap))
        self._gap = min(max(0.0, float(gap or 0.0)), self._gap_max)
        self._recover = max(1, int(recover or 1))
        self._streak = 0
        self._throttled = 0
        self._inflight = 0
        self._cv = threading.Condition()

    def acquire(self) -> None:
        """占一个并发额度（超限时阻塞），拿到后再按当前间隔停顿。"""
        with self._cv:
            while self._inflight >= self._workers:
                self._cv.wait()
            self._inflight += 1
            gap = self._gap
        if gap > 0:
            time.sleep(gap)

    def release(self, throttled: bool = False) -> dict:
        """交还额度并按本次是否被限流调节档位；返回新档位与是否变化。"""
        with self._cv:
            self._inflight -= 1
            before = (self._workers, round(self._gap, 4))
            if throttled:
                self._throttled += 1
                self._streak = 0
                self._gap = min(self._gap_max, self._gap + THROTTLE_DOWN_GAP)
                if self._workers > 1:
                    self._workers -= 1
            else:
                self._streak += 1
                if self._streak >= self._recover:
                    self._streak = 0
                    if self._workers < self._max_workers:
                        self._workers += 1
                    elif self._gap > 0:
                        self._gap = max(0.0, self._gap - THROTTLE_DOWN_GAP)
            self._cv.notify_all()
            changed = (self._workers, round(self._gap, 4)) != before
            return {"changed": changed, "down": bool(throttled),
                    "workers": self._workers, "gap": round(self._gap, 2),
                    "throttled": self._throttled}

    def snapshot(self) -> dict:
        """当前档位快照（进度行 / 汇总表用）。"""
        with self._cv:
            return {"workers": self._workers, "max": self._max_workers,
                    "gap": round(self._gap, 2), "throttled": self._throttled,
                    "streak": self._streak}


def llm(out_dir: Path, models: list[str], limit: int | None, force: bool,
        timeout: float, sleep: float, providers: dict | None = None,
        on_progress: Callable[[dict], None] | None = None,
        should_stop: Callable[[], bool] | None = None, reps: int = 1,
        workers: int = WORKERS_DEFAULT) -> dict:
    """三家跨家族 LLM 盲标填卡（按 model+item_id 缓存 ⇒ 可断点续跑）。

    与真人**同卷**：只喂 sheet.csv 的来源名 + 描述，不喂自动档；同一 ``SYSTEM_PROMPT``
    与 ``temperature=0`` 保证可复现。

    ``reps`` = 每个模型重复判定轮次（默认 1）。第 1 轮写主卡 ``label_<模型>.csv``（不破坏
    既有流程），第 2 轮起写 ``reps/r{k}/label_<模型>.csv``；缓存键按轮次隔离
    （``<模型>#r{k}``），因此第 2/3 轮**不会**命中第 1 轮缓存、会真正重跑。每轮的逐条判定
    追加累积进 ``reps/runs.csv`` 长表（``model,rep,item_id,label,note``），供
    :func:`stability` 统计同模型自重跑的自一致率。

    ``workers`` = **每个模型内部**的并发标注线程数（默认 1 = 纯串行）。线程池只在
    「当前模型 × 当前轮次」的待判条目上并发；模型之间仍**串行**（不同 key / 不同端点，
    也便于逐模型看进度与限流）。并发下 ``sleep`` 变为**每线程**的节流，实际速率≈
    ``workers / sleep``，故触发 429 的概率随之升高 —— 网关限流时**调小 workers** 比调大
    sleep 更有效。计数、缓存、``on_progress`` 回调整体加锁，保证多线程下不串号。

    并发/串行都挂 :class:`_Throttle` 自适应限流闸：撞到 429 就自动降并发 + 加间隔，连续
    成功再回升（无 429 时零开销、行为与接入前一致）。进度行打印实时 ``条/秒`` 与
    ``预计剩余``。

    ``providers`` 允许逐模型覆盖 ``url`` / ``model`` / ``key``（GUI 走第三方网关时用；
    带 ``key`` 就不再读环境变量）。``on_progress`` 每条回调一次（GUI 进度条），
    ``should_stop`` 返回 True 时当前模型收尾落卡即中止（已标部分不丢）。
    """
    reps = max(1, int(reps or 1))
    workers = max(1, min(int(workers or 1), WORKERS_MAX))
    sheet_path = out_dir / "sheet.csv"
    if not sheet_path.exists():
        raise SystemExit(f"[x] 未找到 {sheet_path}（先跑 annotate sample）")
    all_items = [r for r in _read_csv(sheet_path) if (r.get("item_id") or "").strip()]
    todo = all_items[:limit] if (limit and limit > 0) else all_items

    cache_path = out_dir / "llm_cache.json"
    cache: dict[str, dict[str, dict]] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text("utf-8"))
        except (ValueError, OSError):
            cache = {}

    runs_path = out_dir / RERUN_DIR / "runs.csv"
    runs: dict[tuple[str, int, str], dict] = {}
    if runs_path.exists():
        runs = _read_runs(runs_path)

    result: dict = {"dir": out_dir, "items": len(todo), "total": len(all_items),
                    "models": {}, "stopped": False, "reps": reps, "runs": runs_path,
                    "workers": workers}
    for name in models:
        prov = resolve_provider(name, (providers or {}).get(name))
        api_key = (prov.get("key") or os.environ.get(prov.get("env") or "", "")).strip()
        entry: dict = {"model": prov.get("model", ""), "url": prov.get("url", ""),
                       "env": prov.get("env", ""), "filled": 0, "cached": 0, "errors": 0,
                       "retries": 0, "skipped": "", "last_error": "", "stopped": False,
                       "card": None, "reps": reps, "cards": {}, "workers": workers,
                       "throttle": None}
        result["models"][name] = entry
        if not api_key:
            entry["skipped"] = (f"未设 {prov['env']}" if prov.get("env")
                                else "未提供 API Key")
            continue

        minimal = bool(prov.get("minimal"))
        rep_cards: list[Path] = []
        for rep in range(1, reps + 1):
            if should_stop and should_stop():
                entry["stopped"] = True
                break
            ckey = name if rep == 1 else f"{name}#r{rep}"
            done = cache.setdefault(ckey, {})
            card = (out_dir / f"{LABEL_PREFIX}{name}.csv" if rep == 1
                    else out_dir / RERUN_DIR / f"r{rep}" / f"{LABEL_PREFIX}{name}.csv")
            # —— 本轮待判条目：缓存命中的直接跳过（不计入线程池）——
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
                throttle = _Throttle(workers)

                def _stats() -> dict:
                    """本轮实时吞吐：条/秒、预计剩余，以及当前生效的并发 / 间隔。"""
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
                    """退避重试时给界面发一条 warn 级进度（否则 429 风暴在界面上是哑的）。"""
                    with lock:
                        entry["retries"] += 1
                        if on_progress:
                            on_progress({"name": name, "rep": _rep, "reps": reps, "i": _i,
                                         "total": len(todo), "item_id": _iid,
                                         "status": "retry", "note": msg,
                                         "filled": entry["filled"], "cached": entry["cached"],
                                         "errors": entry["errors"], "workers": workers})

                def _work(i: int, it: dict,
                          client: httpx.Client | None = None) -> None:
                    """判定并登记单条（线程安全：计数 / 缓存 / 回调全部在 ``lock`` 内）。"""
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
                        text = _llm_call(prov, _llm_prompt(it), api_key, timeout,
                                         minimal=minimal, client=client,
                                         on_retry=_retry_cb)
                        lb, note = _parse_model_json(text, "")
                        if not lb:
                            with lock:
                                entry["errors"] += 1
                                entry["last_error"] = f"{iid}: 解析不出五类标签"
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
                    """回写卡片；Excel 占用等 OSError 只告警不中断（结果已在缓存里）。"""
                    try:
                        _dump_label_rows(card, all_items, done)
                    except OSError as exc:
                        print(f"  [{name} r{rep}] 卡片暂不可写：{type(exc).__name__} {exc}"
                              f"（结果已进缓存，稍后自动重试）")

                def _flush(n: int, total_n: int) -> None:
                    st = _stats()
                    with lock:
                        print(f"  [{name} r{rep}] {n}/{total_n} 已填 {entry['filled']} "
                              f"失败 {entry['errors']} 退避 {entry['retries']}"
                              + (f" · {workers} 线程" if workers > 1 else "")
                              + f" · {st['rate']:.2f} 条/秒 · 剩 {_fmt_eta(st['eta'])}"
                              + (f" · 并发 {st['workers_now']}/{workers}"
                                 f" 间隔 {st['gap']:.1f}s"
                                 if st["workers_now"] < workers or st["gap"] > 0 else ""))
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

            _dump_label_rows(card, all_items, done)
            entry["cards"][rep] = card
            rep_cards.append(card)
            for it in todo:
                iid = it["item_id"]
                rec = done.get(iid) or {}
                if rec.get("label"):
                    runs[(name, rep, iid)] = {"label": rec.get("label", ""),
                                              "note": rec.get("note", "")}
            _write_runs(runs_path, runs)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                                  encoding="utf-8")

            if entry["stopped"]:
                break

        if rep_cards:
            entry["card"] = rep_cards[0]
        if entry["stopped"]:
            result["stopped"] = True
            break

    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1),
                          encoding="utf-8")
    return result


def show_llm(result: dict) -> None:
    reps = result.get("reps", 1)
    workers = result.get("workers", 1)
    print()
    print("=== E2 LLM 层盲标（跨家族三家，temperature=0） ===")
    print(f"标注包条目 : {result['total']}（本次处理 {result['items']}）")
    print(f"重复轮次   : 每模型 {reps} 轮")
    print(f"并发线程   : 每模型 {workers} 线程" + ("（串行）" if workers <= 1 else ""))
    print(f"缓存文件   : {result['dir'] / 'llm_cache.json'}")
    if reps > 1:
        print(f"逐轮留档   : {result.get('runs') or (result['dir'] / RERUN_DIR / 'runs.csv')}")
    print()
    print(f"  {'模型':<11}{'本次新增':>9}{'命中缓存':>9}{'失败':>7}{'退避':>7}   标注卡")
    for name, e in result["models"].items():
        if e["skipped"]:
            print(f"  {name:<11}{'—':>9}{'—':>9}{'—':>7}{'—':>7}   跳过（{e['skipped']}）")
            continue
        cards = e.get("cards") or {}
        if cards:
            card_txt = f"r1={Path(cards[1]).name}" if 1 in cards else ""
            extra = [f"r{k}→{RERUN_DIR}/r{k}/" for k in sorted(cards) if k >= 2]
            card_txt = ", ".join([card_txt, *extra]).strip(", ")
        else:
            card_txt = e["card"].name if e["card"] else ""
        print(f"  {name:<11}{e['filled']:>9}{e['cached']:>9}{e['errors']:>7}"
              f"{e.get('retries', 0):>7}   {card_txt}")
    thr = [(n, e["throttle"]) for n, e in result["models"].items()
           if (e.get("throttle") or {}).get("throttled")]
    if thr:
        print()
        print("限流自适应 : 撞到 429 自动降并发 + 加间隔，连续成功后再回升（无 429 时零开销）")
        for n, s in thr:
            print(f"  {n:<11}限流 {s['throttled']} 次 → 最终 {s['workers']}/{s['max']} 线程"
                  f" / 间隔 {s['gap']:.1f}s")
    print()
    hint = "下一步跑 annotate score 看人–人 / 人–机 / 机–机 κ 与 α"
    if reps > 1:
        hint += "；再跑 annotate stability 看同模型自重跑的自一致率"
    print(f"提示：LLM 只做扩展层，**不参与金标**；{hint}")


def stability(out_dir: Path, golden: str | None = None) -> dict:
    """同模型自重跑稳定性（读 ``reps/runs.csv``）。

    对每个模型跨轮次算：轮内自一致率（全轮同标签）、两两轮次一致率、跨轮 Fleiss κ、
    平票条目数，以及「多数票 vs 真人基准」的 Cohen κ —— 这是论文里「LLM 代理是否
    稳定可复现」的直接证据（temperature=0 也不保证逐条稳定，故须实测）。
    """
    runs_path = out_dir / RERUN_DIR / "runs.csv"
    res: dict = {"dir": out_dir, "runs_path": runs_path, "models": {}, "reps": 0,
                 "base": None, "invalid": []}
    if not runs_path.exists():
        res["note"] = f"未找到 {runs_path}（先跑 annotate llm --reps k）"
        return res

    by_model: dict[str, dict[int, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    for (m, rep, iid), rec in _read_runs(runs_path).items():
        by_model[m][rep][iid] = rec["label"]
    res["reps"] = max((len(v) for v in by_model.values()), default=0)

    issues: list[dict] = []
    all_labels, labels = _all_and_independent_labels(out_dir, issues)
    res["invalid"] = issues
    humans = [n for n in labels if n not in LLM_NAMES]
    if golden in all_labels:
        base = golden
    elif humans:
        base = "Me" if "Me" in humans else humans[0]
    else:
        base = None
    res["base"] = base
    gold = all_labels.get(base, {}) if base else {}

    for m in sorted(by_model):
        by_rep = by_model[m]
        reps = sorted(by_rep)
        info: dict = {"reps": len(reps), "reps_list": reps}
        if len(reps) < 2:
            info["note"] = "轮次 < 2，无法算自一致"
            res["models"][m] = info
            continue
        common = sorted(set.intersection(*[set(by_rep[r]) for r in reps]))
        n = len(common)
        info["n_items"] = n
        if not n:
            res["models"][m] = info
            continue
        unanimous = sum(1 for k in common if len({by_rep[r][k] for r in reps}) == 1)
        info["unanimous"] = unanimous
        info["unanimous_rate"] = unanimous / n
        pairwise: list[float] = []
        for a in range(len(reps)):
            for b in range(a + 1, len(reps)):
                agree = sum(1 for k in common if by_rep[reps[a]][k] == by_rep[reps[b]][k])
                pairwise.append(agree / n)
        info["pairwise"] = sum(pairwise) / len(pairwise) if pairwise else float("nan")
        info["fleiss"] = fleiss_kappa([[by_rep[r][k] for r in reps] for k in common])
        majority: dict[str, str] = {}
        tie = 0
        maj_dist = Counter()
        for k in common:
            cnt = Counter(by_rep[r][k] for r in reps)
            top = cnt.most_common()
            majority[k] = top[0][0]
            maj_dist[top[0][0]] += 1
            if len(top) > 1 and top[0][1] == top[1][1]:
                tie += 1
        info["tie"] = tie
        info["tie_rate"] = tie / n
        info["maj_dist"] = maj_dist
        if gold:
            gcommon = sorted(set(majority) & set(gold))
            info["human_items"] = len(gcommon)
            if gcommon:
                info["human_kappa"] = cohen_kappa(majority, gold, gcommon)
        res["models"][m] = info
    return res


def show_stability(result: dict) -> None:
    print()
    print("=== E2 同模型自重跑稳定性（temperature=0 是否可复现） ===")
    print(f"逐轮留档 : {result.get('runs_path')}")
    if result.get("note"):
        print(f"[!] {result['note']}")
        return
    bad = result.get("invalid") or []
    if bad:
        print(f"[!] 人标卡有 {len(bad)} 行标签认不出来（已按缺失处理）")
    base = result.get("base")
    if not result["models"]:
        print("runs.csv 里没有可用判定（先跑 annotate llm --reps ≥2）")
        return
    print(f"人标基准 : {base or '（暂无，跳过 vs 人标 κ）'}")
    print()
    print(f"  {'模型':<11}{'轮次':>5}{'共同条目':>9}{'全轮一致':>9}{'自一致率':>10}"
          f"{'两两一致':>10}{'Fleiss κ':>10}{'平票':>7}{'vs人标 κ':>10}")
    for m, info in result["models"].items():
        if info.get("reps", 0) < 2:
            print(f"  {m:<11}{info.get('reps', 0):>5}   {info.get('note', '')}")
            continue
        n = info.get("n_items", 0)
        fk = info.get("fleiss", (float("nan"),))[0]
        hk = info.get("human_kappa", (float("nan"),))[0]
        print(f"  {m:<11}{info['reps']:>5}{n:>9}{info.get('unanimous', 0):>9}"
              f"{info.get('unanimous_rate', float('nan')) * 100:>9.1f}%"
              f"{info.get('pairwise', float('nan')) * 100:>9.1f}%"
              f"{fk:>10.3f}{info.get('tie', 0):>7}{hk:>10.3f}"
              f"   [{_label_quality(fk)}]")
    if base:
        print()
        hi = [i.get("human_kappa", (float("nan"),))[0] for i in result["models"].values()
              if i.get("human_kappa")]
        hi = [k for k in hi if k == k]
        if hi:
            mean = sum(hi) / len(hi)
            print(f"多数票 vs 人标 κ 均值 = {mean:.3f}  "
                  f"[{'代理稳定可用（均值 ≥ 0.61）' if mean >= 0.61 else '代理偏弱，需照实报告'}]")
    print()
    print("读法：自一致率 = 三轮全同标签占比（越高越可复现）；平票 = 三轮无多数票的条目数")


def dump_stability(result: dict, out_dir: Path) -> Path | None:
    models = result.get("models") or {}
    rows = []
    for m, info in models.items():
        if info.get("reps", 0) < 2:
            continue
        fk = info.get("fleiss", (float("nan"),))[0]
        hk = info.get("human_kappa", (float("nan"),))[0]

        def _f(x: float) -> str:
            return "" if x != x else f"{x:.4f}"

        rows.append({
            "model": m, "reps": info["reps"], "n_items": info.get("n_items", ""),
            "unanimous": info.get("unanimous", ""),
            "unanimous_rate": _f(info.get("unanimous_rate", float("nan"))),
            "pairwise_agree": _f(info.get("pairwise", float("nan"))),
            "fleiss_kappa": _f(fk),
            "tie_items": info.get("tie", ""),
            "tie_rate": _f(info.get("tie_rate", float("nan"))),
            "vs_human": result.get("base") or "",
            "human_items": info.get("human_items", ""),
            "human_kappa": _f(hk),
        })
    if not rows:
        return None
    path = out_dir / RERUN_DIR / "stability.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_csv(path, rows)
    return path



def dump_disagreements(result: dict, out_dir: Path) -> Path | None:
    names = sorted(result.get("annotators") or {})
    if len(names) < 2:
        return None
    rows = []
    for k in sorted(result.get("shared") or []):
        vals = [result["annotators"][n][k] for n in names]
        if len(set(vals)) > 1:
            meta = result["key"].get(k, {})
            rows.append({"item_id": k, **{n: result["annotators"][n][k] for n in names},
                         "verdict": meta.get("verdict", ""),
                         "stratum": meta.get("stratum", ""),
                         "norm_url": meta.get("norm_url", ""),
                         "label_final": ""})
    if not rows:
        return None
    path = out_dir / "disagreements.csv"
    _write_csv(path, rows)
    return path


def status(out_dir: Path) -> dict:
    key_path = out_dir / "key.csv"
    key = _read_key(key_path) if key_path.exists() else {}
    files = _label_files(out_dir)
    issues: list[dict] = []
    return {"dir": out_dir, "n_items": len(key), "files": files,
            "invalid": issues,
            "annotators": {_label_name(f): _read_labels(f, issues) for f in files}}


def show_status(result: dict) -> None:
    print()
    print(f"=== E2 标注进度（{result['dir']}） ===")
    if not result["n_items"]:
        print("尚未抽样（先跑 annotate sample）")
        return
    print(f"标注包条目数 : {result['n_items']}")
    if not result["annotators"]:
        print("已交卷标注者 : 无（label_Me.csv / label_other.csv 填 label 列；LLM 卡跑 annotate llm）")
        return
    total = result["n_items"]
    print(f"已交卷标注者 : {len(result['annotators'])}")
    for name, labels in result["annotators"].items():
        done = len(labels)
        bar = "█" * int(done * 20 / total) + "·" * (20 - int(done * 20 / total))
        print(f"  {name:<16} [{bar}] {done}/{total}（{done * 100 // total}%）")


def main() -> None:
    ap = argparse.ArgumentParser(prog="mcp_audit annotate",
                                 description="E2 跨源描述分歧人工标注 + kappa")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="分层抽样并生成标注包")
    s.add_argument("--n", type=int, default=N_DEFAULT,
                   help=f"样本量（默认 {N_DEFAULT}，即 near 80 / partial 160 / different 360）")
    s.add_argument("--seed", type=int, default=SEED_DEFAULT, help="随机种子")
    s.add_argument("--dir", default=None, help="输出目录（默认 DATA_DIR/annotation）")
    s.add_argument("--maxlen", type=int, default=DEFAULT_MAXLEN,
                   help="每条描述在标注表中的截断长度（默认 600）")
    s.add_argument("--force", action="store_true", help="覆盖已存在的标注包")

    sc = sub.add_parser("score", help="读回标注，算 kappa / α 与效度")
    sc.add_argument("--dir", default=None)
    sc.add_argument("--golden", default=None, help="仲裁基准标注者（默认字典序第一个）")

    gd = sub.add_parser("gold", help="按预注册规则合成金标卡 label_gold.csv（派生卡，不进 kappa）")
    gd.add_argument("--dir", default=None)

    st = sub.add_parser("status", help="看标注交卷进度")
    st.add_argument("--dir", default=None)

    lm = sub.add_parser("llm", help="三家跨家族 LLM 盲标填卡（需 API key）")
    lm.add_argument("--dir", default=None)
    lm.add_argument("--only", default=None,
                    help="只跑指定模型，逗号分隔（默认三家：DeepSeek,Claude,GLM）")
    lm.add_argument("--limit", type=int, default=0, help="只跑前 N 条（0 = 全跑）")
    lm.add_argument("--timeout", type=float, default=60.0, help="单次请求超时秒数")
    lm.add_argument("--sleep", type=float, default=0.0, help="每条之间停顿秒数（限速用）")
    lm.add_argument("--reps", type=int, default=1,
                    help="每模型重复判定轮次（≥1；第 2 轮起写 reps/r{k}/，逐条留档 runs.csv）")
    lm.add_argument("--workers", type=int, default=WORKERS_DEFAULT,
                    help=f"每模型内部的并发标注线程数（默认 {WORKERS_DEFAULT}；1–{WORKERS_MAX}，"
                         ">1 提速但更易触限流）")
    lm.add_argument("--force", action="store_true", help="忽略缓存重标")

    sb = sub.add_parser("stability", help="读 reps/runs.csv 算同模型自重跑的稳定性")
    sb.add_argument("--dir", default=None)
    sb.add_argument("--golden", default=None, help="真人基准标注者（默认 Me / 字典序第一个）")


    args = ap.parse_args()
    out_dir = _dir(args.dir)

    if args.cmd == "sample":
        conn = db.connect()
        try:
            res = sample(conn, args.n, args.seed, out_dir, args.force, args.maxlen)
        finally:
            conn.close()
        show_sample(res)
    elif args.cmd == "llm":
        models = ([m.strip() for m in args.only.split(",") if m.strip()]
                  if args.only else list(LLM_NAMES))
        bad = [m for m in models if m not in LLM_PROVIDERS]
        if bad:
            raise SystemExit(f"[x] 未知模型 {bad}，可选 {list(LLM_PROVIDERS)}")
        show_llm(llm(out_dir, models, args.limit, args.force, args.timeout, args.sleep,
                     reps=args.reps, workers=args.workers))
    elif args.cmd == "score":
        res = score(out_dir, args.golden)
        show_score(res)
        path = dump_disagreements(res, out_dir)
        if path:
            print(f"  已写 {path}")
    elif args.cmd == "gold":
        show_gold(gold_card(out_dir))
    elif args.cmd == "stability":
        res = stability(out_dir, args.golden)
        show_stability(res)
        path = dump_stability(res, out_dir)
        if path:
            print(f"  已写 {path}")
    else:
        show_status(status(out_dir))
