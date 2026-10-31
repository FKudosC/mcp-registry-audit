"""描述能力名抽取（E2/E3 共享前置）。

从各源描述文本中抽取归一化"能力名"集合，供：
- E2 声明三角（consistency_analysis._triangle_verdict）改用 desc + remotes
- E3 同码异面（fork_analysis）重定义为声明面分歧

抽取策略（纯标准库，无外部 NLP 依赖，确定性可复现）：
1. 分词：按字母边界切分，保留连字符 / 下划线词
2. 过滤：去停用词、去过短词（<3 字符）、去纯数字
3. 归一化：小写 + 折叠复数（-s → -，但 -ss 保留）
4. 返回：frozenset 能力名集合

同时提供 normalize_tool_name() 把代码侧工具名拆成同口径词集，
使声明侧与代码侧可在同一词集上做集合运算（三角 / Jaccard）。
"""

import json
import re
import sqlite3
from collections import defaultdict

from . import db

FINDING_TYPE = "desc_capability"

_STOP = frozenset({
    "the", "and", "for", "with", "your", "you", "are", "but", "not",
    "this", "that", "from", "they", "will", "can", "all", "any",
    "has", "have", "had", "was", "were", "been", "being", "their",
    "there", "here", "what", "which", "who", "when", "where", "how",
    "then", "than", "so", "if", "or", "as", "by", "on", "in", "at",
    "to", "of", "is", "it", "an", "be", "no", "we", "us", "our",
    "its", "him", "her", "she", "he", "them", "these", "those",
    "about", "into", "over", "after", "before", "between", "under",
    "above", "below", "up", "down", "out", "off", "again", "more",
    "most", "other", "some", "such", "only", "own", "same", "too",
    "very", "just", "now", "also", "get", "got", "via", "per",
    "one", "two", "new", "use", "using", "used", "make", "made",
    "let", "run", "runs", "running", "set", "sets", "based",
    "allow", "allows", "allowed", "help", "helps", "helped",
    "enable", "enables", "support", "supports", "provide", "provides",
    "tool", "tools", "server", "servers", "mcp", "model", "models",
    "protocol", "context", "ai", "app", "application", "data",
    "query", "queries", "result", "results", "return", "returns",
    "build", "built", "create", "creates", "created", "access",
    "simple", "easily", "easy", "like", "need", "needs", "want",
    "want", "wants", "wanted", "every", "each", "both", "either",
    "neither", "nor", "because", "while", "during", "since",
})

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_CAMEL_RE = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _norm_word(w: str) -> str:
    w = w.lower().strip("-_")
    if len(w) >= 4 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return w


def extract(description: str | None) -> frozenset[str]:
    """从描述文本抽取归一化能力名集合。"""
    if not description:
        return frozenset()
    words = (_norm_word(m.group()) for m in _WORD_RE.finditer(description))
    return frozenset(w for w in words if len(w) >= 3 and w not in _STOP)


def normalize_tool_name(name: str) -> frozenset[str]:
    """把代码侧工具名拆成同口径词集（驼峰 / 下划线 / 连字符拆分）。"""
    if not name:
        return frozenset()
    parts = _CAMEL_RE.split(name)
    tokens = []
    for p in parts:
        tokens.extend(re.split(r"[_\-.]+", p))
    words = (_norm_word(t) for t in tokens if t)
    return frozenset(w for w in words if len(w) >= 3 and w not in _STOP)


def jaccard(a: frozenset, b: frozenset) -> float:
    """两集合的 Jaccard 相似度（空集约定 = 1.0）。"""
    if not a and not b:
        return 1.0
    u = len(a | b)
    if not u:
        return 1.0
    return len(a & b) / u


_NAME_KEYS = ("name", "tool", "toolName", "tool_name")


def _loads(text: str | None):
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


def _tool_names_from(tools_json: str | None) -> list[str]:
    """从 tools_json 抽取工具字面名（兼容 str / dict / function.name）。"""
    data = _loads(tools_json)
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for it in data:
        nm = None
        if isinstance(it, str):
            nm = it
        elif isinstance(it, dict):
            for k in _NAME_KEYS:
                v = it.get(k)
                if isinstance(v, str) and v.strip():
                    nm = v
                    break
            if nm is None:
                fn = it.get("function")
                if isinstance(fn, dict) and isinstance(fn.get("name"), str):
                    nm = fn["name"]
        nm = (nm or "").strip()
        if nm:
            out.append(nm)
    return out


def decl_caps(tools_json: str | None = None, remotes_json: str | None = None,
              description: str | None = None) -> frozenset[str]:
    """声明集 D(u)：tools 工具名 ∪ remotes(type/url) 抽词 ∪ 描述抽词。

    与代码侧 **同口径归一化**（同一词集空间），使 `D_s vs A_code` 可直接做
    集合运算（E2 声明三角 / E3 同码异面）。
    """
    caps: set[str] = set()
    for nm in _tool_names_from(tools_json):
        caps |= normalize_tool_name(nm)
    rj = _loads(remotes_json)
    if isinstance(rj, list):
        for it in rj:
            if isinstance(it, dict):
                for k in ("type", "url"):
                    v = it.get(k)
                    if isinstance(v, str):
                        caps |= extract(v)
            elif isinstance(it, str):
                caps |= extract(it)
    caps |= extract(description)
    return frozenset(caps)


def collect(conn: sqlite3.Connection) -> dict[str, dict[str, frozenset[str]]]:
    """逐跨源单元 → {source: 能力名集}（分母同 analysis.collect：≥2 源）。"""
    rows = conn.execute(
        """
        SELECT r.norm_url, r.source, sv.description
        FROM repo_index r
        LEFT JOIN servers sv ON sv.source = r.source AND sv.name = r.name
        WHERE r.norm_url IS NOT NULL
        """
    ).fetchall()
    per: dict[str, dict[str, frozenset[str]]] = defaultdict(dict)
    for r in rows:
        per[r["norm_url"]][r["source"]] = extract(r["description"])
    return {nu: sv for nu, sv in per.items() if len(sv) >= 2}


def run(conn: sqlite3.Connection, apply: bool = False, examples: int = 8,
        limit: int | None = None) -> dict:
    """预演 / 写库。"""
    data = collect(conn)
    counts: dict[str, int] = defaultdict(int)
    nonempty_pairs = 0
    multi_nonempty = 0
    sample: list[dict] = []

    ts = db.now()
    if apply:
        conn.execute("DELETE FROM findings WHERE finding_type = ?", (FINDING_TYPE,))

    items = list(data.items())
    if limit:
        items = items[:limit]

    for nu, by_source in items:
        cap_sources = {s: caps for s, caps in by_source.items() if caps}
        n_cap = len(cap_sources)
        if n_cap >= 2:
            nonempty_pairs += 1
            multi_nonempty += 1
            counts["multi_nonempty"] += 1
        elif n_cap == 1:
            counts["single_nonempty"] += 1
        else:
            counts["all_empty"] += 1

        if len(sample) < examples and n_cap >= 2:
            jacs = []
            srcs = sorted(cap_sources)
            for i, a in enumerate(srcs):
                for b in srcs[i + 1:]:
                    jacs.append(round(jaccard(cap_sources[a], cap_sources[b]), 3))
            sample.append({
                "norm_url": nu,
                "source_count": len(by_source),
                "cap_sources": n_cap,
                "sizes": {s: len(c) for s, c in cap_sources.items()},
                "min_jaccard": min(jacs) if jacs else None,
                "terms": {s: sorted(c)[:8] for s, c in cap_sources.items()},
            })

        if apply:
            payload = {
                "norm_url": nu,
                "source_count": len(by_source),
                "cap_source_count": n_cap,
                "capabilities": {s: sorted(c) for s, c in by_source.items()},
            }
            conn.execute(
                "INSERT INTO findings "
                "(source, name, version, finding_type, data_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                ("+".join(sorted(by_source)), nu,
                 "multi" if n_cap >= 2 else "single",
                 FINDING_TYPE,
                 json.dumps(payload, ensure_ascii=False), ts),
            )

    if apply:
        conn.commit()

    return {
        "total": len(data),
        "counts": dict(counts),
        "multi_nonempty": multi_nonempty,
        "sample": sample,
        "applied": apply,
    }


def show(result: dict, examples: int) -> None:
    print()
    print(f"=== 描述能力名抽取（"
          f"{'写库' if result['applied'] else '预演'}） ===")
    print(f"跨源单元（≥2 源）: {result['total']}")
    c = result["counts"]
    print(f"  ├ ≥2 源有抽名      {c.get('multi_nonempty', 0)}")
    print(f"  ├ 仅 1 源有抽名    {c.get('single_nonempty', 0)}")
    print(f"  └ 全部无抽名       {c.get('all_empty', 0)}")
    print()
    if examples:
        print(f"===== 样例（前 {min(examples, len(result['sample']))} 个） =====")
        for s in result["sample"][:examples]:
            print(f"\n● {s['norm_url'][:60]}  源 {s['source_count']}  "
                  f"抽名源 {s['cap_sources']}  min_J = {s['min_jaccard']}")
            for src, terms in s["terms"].items():
                print(f"    {src:<12} ({s['sizes'][src]:>3} 词) {', '.join(terms)}")


def main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="mcp_audit cap-extract",
        description="描述能力名抽取（E2/E3 共享前置）")
    parser.add_argument("--apply", action="store_true", help="写库")
    parser.add_argument("--examples", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    result = run(conn, apply=args.apply, examples=args.examples,
                 limit=args.limit)
    show(result, args.examples)
    conn.close()


if __name__ == "__main__":
    main()
