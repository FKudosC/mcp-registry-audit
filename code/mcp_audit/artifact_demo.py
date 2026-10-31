"""A1 跨源对比 demo（产物类，不计创新；论文 §Artifact）。

研究问题
--------
前四线（E1 / C1 / C2 / C3 / C4）各把逐跨源货号的判定落进机器可读的 ``findings``，
但读者**无法一眼看到**「同一货号在各注册表下长什么样、我们的跨源口径如何判它」。
本模块把**同一跨源货号**的各源**声明身份 / 声明描述**、**共享代码面**、**声明-代码
对照（共识超配）** 与**五条跨源口径判定**并排成一张「对照卡」，导出为**自包含 HTML
产物**（离线可开），即 A1「跨源对比 demo」—— 只证明数据集**可用、可比、可复核**，
**不新增任何主张**。

口径（逐字复用，不新造）
----------------------
- 跨源单元 = ``repo_index`` 中 ``HAVING COUNT(DISTINCT source) > 1``（同 I3 / C 线）。
- 五条口径的 finding_type 映射 ``LENS_FT`` 与「分歧 / 异常」判定集 ``DIVERGENT``
  **直接**从 :mod:`mcp_audit.defense_baseline` 复用（同一真值源，避免两处定义漂移）。
- 各源声明身份 / 描述取 ``cross_source_fork.declared`` 与 ``desc_diff``
  （``descriptions`` / ``desc_lens``）。
- **代码面按仓库（跨源单元）给一份**：``code_capability`` 的口径是**逐仓库一行**
  （``source`` 列 = 该仓所属源并集），因为**同一 ``norm_url`` 即同一份代码** ——
  「同码异面」（C2）比的是**声明 vs 代码**，不是「同一份代码算出多份画像」。
  故对照卡把代码面单列一块，与各源声明并置。

产出（只读，不写 findings）
--------------------------
1. 控制台：Top-N 对照卡文本（CJK 定宽对齐）。
2. HTML 产物：``paper/artifact/cross_source_demo.html``（自包含、离线可开）。

安全边界（不可破）
----------------
只读 ``findings`` / ``repo_index``；**不触盘爬取、不执行**任何仓库代码；
**不写 findings**（A1 为产物，不计入 8 类落库口径）。

用法：
    python -m mcp_audit demo                       # 控制台 Top-12 + 写 HTML
    python -m mcp_audit demo --top 20 --limit 5000
    python -m mcp_audit demo --out D:\\tmp\\demo.html
    python -m mcp_audit demo --no-html             # 只打印，不写文件
"""

import html
import json
import sqlite3
from pathlib import Path

from . import config
from . import db
from .defense_baseline import C4_LENSES, DIVERGENT, LENS_FT

LENS_ORDER = ("fidelity", "drift", "fork", "consistency", "consensus")

LENS_CN = {
    "fidelity": "描述保真",
    "drift": "纵向漂移",
    "fork": "跨源分叉",
    "consistency": "声明-行为一致性",
    "consensus": "危险面共识",
}

DEFAULT_OUT = config.PROJECT_ROOT / "paper" / "artifact" / "cross_source_demo.html"
DESC_CLIP = 280


def _index(conn: sqlite3.Connection, ft: str) -> dict[str, dict]:
    """finding_type → {norm_url(name): payload}（缺 payload 的键跳过）。"""
    out: dict[str, dict] = {}
    for name, dj in conn.execute(
            "SELECT name, data_json FROM findings WHERE finding_type = ?", (ft,)):
        try:
            out[name] = json.loads(dj)
        except (TypeError, ValueError):
            continue
    return out


def _cross_sources(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """跨源单元（≥2 源）→ 成员源列表（口径同其余跨源模块）。"""
    cross: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT norm_url, GROUP_CONCAT(DISTINCT source) AS sources FROM repo_index "
        "WHERE norm_url IS NOT NULL GROUP BY norm_url "
        "HAVING COUNT(DISTINCT source) > 1"
    ):
        cross[r["norm_url"]] = sorted(r["sources"].split(","))
    return cross


def _code_face(payload: dict | None) -> dict | None:
    """把 code_capability payload 收敛成对照卡要用的代码面字段。"""
    if not payload:
        return None
    return {
        "files_scanned": payload.get("files_scanned") or 0,
        "langs": payload.get("langs") or [],
        "danger_categories": payload.get("danger_categories") or [],
        "declared_env_vars": payload.get("declared_env_vars") or 0,
        "declared_secrets": payload.get("declared_secrets") or 0,
        "declared_transports": payload.get("declared_transports") or [],
        "tool_names_total": payload.get("tool_names_total") or 0,
        "sources": payload.get("sources") or [],
        "commit_sha": payload.get("commit_sha"),
    }


def collect(conn: sqlite3.Connection) -> dict[str, dict]:
    """逐跨源单元汇总「各源声明 + 共享代码面 + 五口径判定」对照卡。"""
    cross = _cross_sources(conn)
    if not cross:
        return {}
    lens = {l: _index(conn, ft) for l, ft in LENS_FT.items()}
    desc = _index(conn, "desc_diff")
    fork = _index(conn, "cross_source_fork")
    cons = _index(conn, "cross_source_consensus")
    code = _index(conn, "code_capability")

    out: dict[str, dict] = {}
    for nu, sources in cross.items():
        lens_payload = {l: lens[l].get(nu) or {} for l in LENS_ORDER}
        verdicts = {l: lens_payload[l].get("verdict") for l in LENS_ORDER}
        div = [l for l in LENS_ORDER if verdicts[l] in DIVERGENT[l]]
        c4_div = [l for l in div if l in C4_LENSES]

        d = desc.get(nu) or {}
        f = fork.get(nu) or {}
        c = cons.get(nu) or {}

        declared = {
            s: {
                "name": (v or {}).get("name"),
                "title": (v or {}).get("title"),
                "version": (v or {}).get("version"),
                "tools_count": (v or {}).get("tools_count"),
            }
            for s, v in (f.get("declared") or {}).items()
        }
        code_face = _code_face(code.get(nu))

        sim = d.get("similarity")
        score = (len(div), len(sources),
                 -(sim if isinstance(sim, (int, float)) else -1.0))
        out[nu] = {
            "norm_url": nu,
            "sources": sources,
            "source_count": len(sources),
            "verdicts": verdicts,
            "divergent_lenses": div,
            "c4_divergent_lenses": c4_div,
            "tier": ("multi_lens" if len(c4_div) >= 2
                     else "single_lens" if len(c4_div) == 1 else "no_divergence"),
            "desc_verdict": d.get("verdict"),
            "similarity": sim,
            "desc_sources": d.get("desc_sources") or [],
            "descriptions": d.get("descriptions") or {},
            "desc_lens": d.get("desc_lens") or {},
            "declared": declared,
            "code": code_face,
            "consensus_verdict": c.get("verdict"),
            "consensus_declared": c.get("declared_collective") or [],
            "consensus_actual": c.get("actual") or [],
            "consensus_overgrant": c.get("overgrant") or [],
            "consensus_danger_hits": c.get("danger_hits") or {},
            "consensus_declaring_sources": c.get("declaring_sources") or [],
            "score": score,
        }
    return out


def rank(dossiers: dict[str, dict]) -> list[dict]:
    """按「分歧口径数 → 源数 → 描述相似度最低」降序选卡（越分歧越靠前）。"""
    return sorted(dossiers.values(), key=lambda p: p["score"], reverse=True)


def _clip(text, n: int = DESC_CLIP) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _lens_strip(p: dict) -> str:
    parts = []
    for l in LENS_ORDER:
        v = p["verdicts"].get(l) or "-"
        mark = "*" if l in p["divergent_lenses"] else " "
        parts.append(f"{mark}{l}={v}")
    return "  ".join(parts)


def render_cards(cards: list[dict]) -> str:
    """控制台对照卡文本（CJK 定宽对齐）。"""
    lines: list[str] = []
    for i, p in enumerate(cards, 1):
        lines.append("")
        lines.append("=" * 100)
        lines.append(f"[{i}] {p['norm_url']}   源 {p['source_count']}"
                     f"   分层 {p['tier']}   分歧口径 {len(p['divergent_lenses'])}"
                     f"   描述相似度 {p['similarity']}")
        lines.append(f"    口径  {_lens_strip(p)}")
        lines.append("-" * 100)
        lines.append(f"    {db._pad('源', 10)} {db._pad('声明身份 (name@version)', 40)} "
                     f"{db._pad('标题', 26)} {db._pad('工具', 4, right=True)} "
                     f"{db._pad('描述长度', 8, right=True)}")
        for s in p["sources"]:
            dec = p["declared"].get(s) or {}
            ident = f"{dec.get('name') or '-'}@{dec.get('version') or '-'}"
            title = _clip(dec.get("title") or "-", 26)
            tools = dec.get("tools_count")
            tools = "-" if tools is None else str(tools)
            dlen = p["desc_lens"].get(s)
            dlen = "-" if dlen is None else str(dlen)
            lines.append(f"    {db._pad(s, 10)} {db._pad(_clip(ident, 40), 40)} "
                         f"{db._pad(title, 26)} {db._pad(tools, 4, right=True)} "
                         f"{db._pad(dlen, 8, right=True)}")
        for s in p["desc_sources"]:
            descs = p["descriptions"].get(s) or []
            lines.append(f"    · {s} 描述: {_clip(descs[0]) if descs else '-'}")
        cb = p["code"]
        if cb:
            lines.append(
                f"    代码面  语言 {','.join(cb['langs']) or '-'}  文件 {cb['files_scanned']}"
                f"  工具信号 {cb['tool_names_total']}"
                f"  危险面 {len(cb['danger_categories'])} "
                f"[{','.join(cb['danger_categories']) or '-'}]"
                f"  声明 env {cb['declared_env_vars']} / secret {cb['declared_secrets']}"
                f" / transport {','.join(cb['declared_transports']) or '-'}")
        else:
            lines.append("    代码面  —（该仓无代码画像：未克隆 / 超体积闸）")
        if p["consensus_overgrant"]:
            hits = p["consensus_danger_hits"]
            hit_txt = " ".join(f"{k}={v}" for k, v in hits.items())
            lines.append(f"    共识超配  {','.join(p['consensus_overgrant'])}"
                         f"   实际命中  {hit_txt}")
    return "\n".join(lines)


_CSS = """
:root{--bg:#0f1419;--ink:#1a2027;--mut:#5b6875;--line:#e3e8ee;
--hit:#c0392b;--ok:#1f8a4c;--accent:#2f6fb0;}
*{box-sizing:border-box}
body{margin:0;background:#eef2f6;color:var(--ink);
font:15px/1.6 -apple-system,"Segoe UI",Roboto,"Noto Sans CJK SC",sans-serif;}
header{background:var(--bg);color:#fff;padding:26px 32px;}
header h1{margin:0 0 6px;font-size:22px;letter-spacing:.3px;}
header p{margin:0;color:#9fb0c0;font-size:13px;}
.wrap{max-width:1180px;margin:0 auto;padding:24px 20px 60px;}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;
margin:18px 0;padding:18px 20px;box-shadow:0 1px 3px rgba(16,24,40,.06);}
.card h2{margin:0 0 4px;font-size:17px;word-break:break-all;}
.card h2 a{color:var(--accent);text-decoration:none;}
.badges{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 12px;}
.b{font-size:12px;padding:2px 9px;border-radius:999px;border:1px solid var(--line);
background:#f6f9fc;color:var(--mut);white-space:nowrap;}
.b.hit{border-color:#f2c9c4;background:#fdeceb;color:var(--hit);font-weight:600;}
.b.tier{border-color:#cfe0f2;background:#eaf3fc;color:var(--accent);font-weight:600;}
table{width:100%;border-collapse:collapse;margin:6px 0 10px;font-size:13.5px;}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;
vertical-align:top;word-break:break-all;}
th{background:#f6f9fc;color:var(--mut);font-weight:600;}
td.n{text-align:right;white-space:nowrap;}
.face{display:inline-block;background:#fdeceb;color:var(--hit);border-radius:4px;
padding:0 6px;margin:1px 3px 1px 0;font-size:12px;}
.none{color:#9aa7b4;}
.desc{margin:4px 0 0;padding:8px 10px;background:#f8fafc;border-left:3px solid #cfe0f2;
border-radius:0 6px 6px 0;font-size:13px;color:#3b4753;}
.desc b{color:var(--accent);}
.codeface{margin:6px 0;padding:8px 10px;background:#f8fafc;border-radius:6px;
font-size:13px;color:#3b4753;border:1px dashed var(--line);}
.foot{color:var(--mut);font-size:12.5px;text-align:center;margin-top:28px;}
"""


def _faces_html(faces: list[str]) -> str:
    if not faces:
        return '<span class="none">—</span>'
    return "".join(f'<span class="face">{html.escape(f)}</span>' for f in faces)


def _card_html(i: int, p: dict) -> str:
    nu = html.escape(p["norm_url"])
    badges = [f'<span class="b tier">分层 {p["tier"]}</span>',
              f'<span class="b">{p["source_count"]} 源</span>']
    if p["similarity"] is not None:
        badges.append(f'<span class="b">描述相似度 {p["similarity"]}</span>')
    for l in LENS_ORDER:
        v = p["verdicts"].get(l) or "-"
        cls = "b hit" if l in p["divergent_lenses"] else "b"
        badges.append(f'<span class="{cls}">{LENS_CN[l]}：{html.escape(str(v))}</span>')

    rows = []
    for s in p["sources"]:
        dec = p["declared"].get(s) or {}
        ident = f"{dec.get('name') or '—'} @ {dec.get('version') or '—'}"
        title = dec.get("title") or "—"
        tools = dec.get("tools_count")
        tools = "—" if tools is None else str(tools)
        dlen = p["desc_lens"].get(s)
        dlen = "—" if dlen is None else str(dlen)
        rows.append(f"<tr><td>{html.escape(s)}</td><td>{html.escape(ident)}</td>"
                    f"<td>{html.escape(str(title))}</td>"
                    f"<td class='n'>{html.escape(tools)}</td>"
                    f"<td class='n'>{html.escape(dlen)}</td></tr>")

    descs = []
    for s in p["desc_sources"]:
        text = (p["descriptions"].get(s) or ["—"])[0]
        descs.append(f'<p class="desc"><b>{html.escape(s)}</b> · '
                     f'{html.escape(_clip(text))}</p>')

    cb = p["code"]
    if cb:
        code_block = (
            f'<div class="codeface"><b>共享代码面</b>（同 norm_url 即同代码）· '
            f'语言 {html.escape(", ".join(cb["langs"]) or "—")} · '
            f'文件 {cb["files_scanned"]} · 工具信号 {cb["tool_names_total"]} · '
            f'声明 env {cb["declared_env_vars"]} / secret {cb["declared_secrets"]} / '
            f'transport {html.escape(", ".join(cb["declared_transports"]) or "—")}<br>'
            f'代码危险面：{_faces_html(cb["danger_categories"])}</div>')
    else:
        code_block = ('<div class="codeface"><b>共享代码面</b> · '
                      '<span class="none">—（该仓无代码画像：未克隆 / 超体积闸）</span></div>')

    cons = ""
    if p["consensus_overgrant"]:
        hits = " ".join(f"{k}={v}" for k, v in p["consensus_danger_hits"].items())
        cons = (f'<div class="codeface"><b>共识超配</b>（声明 ⊆ 实际 不成立）· '
                f'{_faces_html(p["consensus_overgrant"])}'
                f'<br>实际命中 <span class="none">{html.escape(hits)}</span></div>')

    return f"""<section class="card">
  <h2><a href="https://{nu}" target="_blank" rel="noopener">{nu}</a></h2>
  <div class="badges">{"".join(badges)}</div>
  <table><thead><tr><th>源</th><th>声明身份 (name @ version)</th><th>标题</th>
  <th class="n">声明工具</th><th class="n">描述长度</th></tr></thead>
  <tbody>{"".join(rows)}</tbody></table>
  {code_block}
  {"".join(descs)}{cons}
</section>"""


def render_html(cards: list[dict], total: int) -> str:
    """自包含 HTML 产物（离线可开，无外部资源）。"""
    body = "\n".join(_card_html(i, p) for i, p in enumerate(cards, 1))
    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A1 跨源对比 demo · mcp-audit</title><style>{_CSS}</style></head>
<body>
<header><h1>A1 · 跨源对比 demo（Cross-Source Comparison）</h1>
<p>同一跨源货号的各注册表声明 × 共享代码面 × 五条跨源口径判定 ·
候选 {total} 个 · 展示 Top-{len(cards)} · 只读产物，不写 findings</p></header>
<div class="wrap">
{body}
<p class="foot">mcp-audit · 产物类 demo（不计创新）· 生成器 <code>python -m mcp_audit demo</code></p>
</div></body></html>"""


def run(conn: sqlite3.Connection, top: int = 12, limit: int | None = None,
        out=None, write_html: bool = True) -> dict:
    """只读汇总 + 选卡；write_html 时导出 HTML 产物（不写 findings）。"""
    data = collect(conn)
    ranked = rank(data)
    if limit:
        ranked = ranked[:limit]
    cards = ranked[:top]

    total_div = sum(1 for p in data.values() if p["divergent_lenses"])
    with_code = sum(1 for p in data.values() if p["code"])
    tier_counts: dict[str, int] = {}
    for p in data.values():
        tier_counts[p["tier"]] = tier_counts.get(p["tier"], 0) + 1

    written_path = None
    if write_html:
        out_path = Path(out) if out else DEFAULT_OUT
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(render_html(cards, len(data)), encoding="utf-8")
        written_path = str(out_path)

    return {
        "total": len(data),
        "total_divergent": total_div,
        "with_code": with_code,
        "tier_counts": tier_counts,
        "cards": cards,
        "ranked": ranked,
        "written_path": written_path,
    }


def show(result: dict, table: int = 0) -> None:
    print()
    print("=== A1 跨源对比 demo（产物类，不计创新；只读，不写 findings） ===")
    print(f"跨源单元（≥2 源，22,104 口径） : {result['total']}")
    print(f"  含 ≥1 条跨源分歧口径         : {result['total_divergent']}")
    print(f"  有代码画像（可并列代码面）    : {result['with_code']}")
    for t in ("multi_lens", "single_lens", "no_divergence"):
        print(f"  ├ {t:<14} {result['tier_counts'].get(t, 0)}")
    if result["written_path"]:
        print(f"HTML 产物已导出              : {result['written_path']}")
    else:
        print("HTML 产物                     : 未写（--no-html）")
    print()
    print(f"===== 对照卡 Top-{len(result['cards'])}"
          f"（按分歧口径数 / 源数 / 描述相似度降序） =====")
    print(render_cards(result["cards"]))
    if table and table > len(result["cards"]):
        more = result["ranked"][len(result["cards"]):table]
        if more:
            print(render_cards(more))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="mcp_audit demo",
        description="A1 跨源对比 demo：导出「同一货号 × 各源声明 × 共享代码面 × "
                    "五口径判定」对照卡（只读产物，不写 findings）")
    parser.add_argument("--top", type=int, default=12, help="展示 / 导出卡片数（默认 12）")
    parser.add_argument("--limit", type=int, default=None,
                        help="限制参与排序的跨源单元数（抽样试跑用）")
    parser.add_argument("--table", type=int, default=0,
                        help="控制台额外打印的卡片数（默认 = --top）")
    parser.add_argument("--out", default=None,
                        help=f"HTML 输出路径（默认 {DEFAULT_OUT}）")
    parser.add_argument("--no-html", action="store_true", help="只打印，不写 HTML")
    args = parser.parse_args()

    conn = db.connect()
    db.init_db(conn)
    result = run(conn, top=args.top, limit=args.limit,
                 out=args.out, write_html=not args.no_html)
    show(result, table=args.table or args.top)
    conn.close()


if __name__ == "__main__":
    main()
