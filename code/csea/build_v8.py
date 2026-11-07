"""Assemble v5: the article compressed to the journal's word budget.

Structure of the build:
  * titles, abstract and highlights come from constants here;
  * the eight body sections come from the compressed files in v5_work/;
  * section and subsection numbers are regenerated from position, so no source file
    can carry a stale number into the manuscript;
  * tables and figures are inserted by caption lookup, and only the kept ones are
    emitted, so dropping one cannot leave an orphan caption.

    python build_v5.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt, RGBColor

sys.path.insert(0, str(Path(__file__).parent))
from build_manuscript import ( # noqa: E402
    CSEA, parse_blocks, add_runs, add_runs_with_cites, heading_text, INLINE_RE,
)
from build_v4 import three_line, add_questions_caption # noqa: E402

OUT = Path(r"C:\Users\Fan\Downloads\1\mcp-audit\paper\投稿_20261031\Codex_初稿_投稿_v8.docx")
WORK = CSEA / "v8_work"
SUMMARY = json.loads((CSEA / "out" / "summary.json").read_text(encoding="utf-8"))
V1 = json.loads((CSEA / "v1_body.json").read_text(encoding="utf-8"))

TITLE = ("Assertion without establishment: a cross-source evidence audit of "
         "Model Context Protocol registry records")
AUTHORS = "Kecai Fan^a, Jialiang Peng^a,*"
AFFIL = ("^a Heilongjiang University, 74 Xuefu Road, Nangang District, "
         "Harbin 150080, China")
CORR = ("* Corresponding author: Jialiang Peng (pengjialiang@hlju.edu.cn), "
        "Heilongjiang University, 74 Xuefu Road, Nangang District, Harbin 150080, China")

ABSTRACT = (
    "Registry records mediate installation decisions for Model Context Protocol servers, "
    "yet nothing in the protocol requires a record to be bound to a revision. What do such "
    "records actually establish? Do registries agree about the same repository, does the "
    "declaration match the code, can a shortfall be attributed to a party who could repair "
    "it, and does a version string still identify what it names? In this paper we present "
    "CSEA, a cross-source evidence audit framework that asks what a record establishes, and "
    "apply it to seven public registries. A record becomes a claim over a normalized "
    "repository listing; a declaration plane and a code plane are read independently on the "
    "same unit; and an ordered series of guards returns a typed verdict with a reason code, "
    "abstaining where no decision procedure exists rather than averaging it away. Over eight "
    "fetch days the audit resolved 158,631 records to 100,688 listings and cloned 86,983 of "
    "them. We find that 58.6% of claims cannot be decided, and that the omission partitions "
    "into 50.4% forced by the schema, 22.9% forced by the package format and 26.7% left to "
    "the publisher. That 81.5% of comparable units differ textually while none of 686 "
    "divergent declaration pairs is mutually exclusive. That 88.9% of 77,313 regex-hit "
    "repositories are listed by exactly one registry. And that 35.1% of declared-frozen "
    "records moved while their version string stayed unchanged. Together these give an "
    "evidence-based account of what MCP records establish, and of which field would let "
    "them establish more."
)

HIGHLIGHTS = [
    "A framework that asks what a registry record establishes, not what it asserts",
    "Cross-source audit of seven public registries on one normalized listing",
    "Registries diverge in wording yet never contradict one another",
    "Most of the disclosure gap belongs to the format, not to the publisher",
    "A version string can stay still while the code moves, and no field says so",
]

KEYWORDS = ("Model Context Protocol; registry ecosystem; evidence audit; metadata "
            "integrity; software supply chain provenance; abstention")

# figure number -> keep, and the v1 block that carries the image
KEEP_FIGURES = {1, 3, 4, 7, 8, 11}
KEEP_TABLES = {1, 2, 3}

F57 = """
### 5.7 What the corpus can and cannot decide

Of the 22,104 aligned units, 9,156 (41.4%) can be decided: 623 (6.8% of those) are
supported, 3,198 (34.9%) partial and 5,335 (58.3%) unsupported. The remaining 12,948 cannot be decided, 12,835 for want of evidence and 113 for want of attribution, and the reason
codes name four different repairers: the package format, the publisher, the schema and
the collection pipeline.

[[TABLE:verdicts]]

The omission partitions exactly. [[TABLE:decomp]] The sum is an identity, not an
estimate, and `S_structural` = 0.733.

Two robustness checks bound the claim. Resampling the 16,494 units with a detection 400
times gives 95% intervals of [0.500, 0.507], [0.224, 0.235] and [0.263, 0.272]: the
shares are stable far beyond the precision at which they are reported. The weighting is
not covered by that interval. Counting every detected instance alike ignores that the
nine detectors differ in hand-adjudicated precision by a factor of forty, and that the
weakest of them sit in the schema bucket; weighting each instance by its class precision
moves the structural share from 0.733 to 0.639 and makes the behavioural bucket the
largest, at 0.360 against a schema share of 0.315. We report the unweighted figures as
primary, because a reader is harmed by a capability rather than by a detector, and the
weighted figures as the lower bound on the structural claim.

On the binding ladder, [[TABLE:rbl]] Every RBL2-grade judgement in this paper comes from
the audit's own clone, not from the record.
"""


def figure_map() -> dict[int, str]:
    """Map a figure number to the image v1 embedded before its caption."""
    out = {}
    for i, b in enumerate(V1):
        if not b.get("img"):
            continue
        for nxt in V1[i + 1:i + 3]:
            t = (nxt.get("text") or "").strip()
            m = re.match(r"^Figure (\d+)\.", t)
            if m:
                out[int(m.group(1))] = b["img"]
                break
    return out


FIGS = figure_map()
TABLES = {b["idx"]: b["rows"] for b in V1 if b["kind"] == "t"}


def new_table(doc: Document, kind: str) -> None:
    if kind == "verdicts":
        rows = [["verdict", "n", "share", "who can repair it"]]
        owner = {"CHANNEL_ABSENT": "package format", "UNDECLARED_CAPABILITY": "publisher",
                 "DECLARATION_SUBSET": "publisher", "CODE_OPAQUE": "tooling",
                 "CODE_UNAVAILABLE": "collection", "CODE_GONE": "collection",
                 "NO_DECLARATION": "publisher", "NO_DECLARING_CHANNEL": "schema",
                 "FULL_MATCH": "—", "IDENTITY_AMBIGUOUS": "alignment"}
        total = sum(SUMMARY["by_type_reason"]["DECLARATION_COMPLETE"].values())
        for key, n in sorted(SUMMARY["by_type_reason"]["DECLARATION_COMPLETE"].items(),
                             key=lambda kv: -kv[1]):
            status, reason = key.split("|")
            rows.append([f"{status} · {reason}", f"{n:,}",
                         f"{100 * n / total:.1f}%", owner.get(reason, "—")])
        add_table(doc, rows)
    elif kind == "decomp":
        t = SUMMARY["totals"]
        rows = [["bucket", "class instances", "share", "owner"],
                ["schema: no declaring field in any format", f"{t['n_schema']:,}",
                 f"{100 * t['n_schema'] / t['n_omitted']:.1f}%", "schema designer"],
                ["artefact: manifest kind carries no field", f"{t['n_artefact']:,}",
                 f"{100 * t['n_artefact'] / t['n_omitted']:.1f}%", "package format"],
                ["behavioural: field present, left empty", f"{t['n_behavioural']:,}",
                 f"{100 * t['n_behavioural'] / t['n_omitted']:.1f}%", "publisher"]]
        add_table(doc, rows)
    elif kind == "rbl":
        add_table(doc, [["level", "what the record carries", "records", "share"],
                        ["RBL0", "no repository URL and no digest", "27,605", "17.4%"],
                        ["RBL1", "URL and a version string", "130,171", "82.1%"],
                        ["RBL2", "a content digest", "855", "0.54%"],
                        ["RBL3", "RBL2 and an attestation", "0", "0.00%"]])


def add_table(doc: Document, rows: list[list[str]]) -> None:
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    table.style = "Table Grid"
    for ri, row in enumerate(rows):
        for ci, value in enumerate(row):
            cell = table.rows[ri].cells[ci]
            cell.text = ""
            para = cell.paragraphs[0]
            run = para.add_run(value)
            run.font.size = Pt(8.5)
            if ri == 0:
                run.bold = True




def parse_md(text: str):
    """A parser written for these files, not a general one.

    Captions arrive in two shapes ("**Table 5.** …" from the drafted sections,
    "Table 3. …" from the exported v1 text), headings arrive with or without a
    trailing dot, and marker lines must survive as markers. A general parser gets
    one of those wrong and silently drops a table.
    """
    blocks = []
    buf: list[str] = []
    allow_cont = False

    def flush():
        if buf:
            blocks.append(("p", " ".join(x.strip() for x in buf)))
            buf.clear()

    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            flush(); allow_cont = False; continue
        if re.fullmatch(r"\[\[FIG\d\]\]|\[FIGURE\]|\[TABLE \d+\]", s):
            continue
        if re.fullmatch(r"-{3,}", s):
            flush(); continue
        if s.startswith(">"):
            continue
        # Level comes from the number, not from the number of # marks: the drafted
        # sections mark every heading with ##, the exported text has no marks at all.
        stripped = re.sub(r"^#{1,4}\s*", "", s)
        num = re.match(r"^(\d+(?:\.\d+)*)\.?\s+[A-Z]", stripped)
        if num and len(stripped) < 95 and not stripped.endswith("."):
            depth = num.group(1).count(".") + 1
            flush()
            blocks.append((f"h{min(depth, 3)}", stripped))
            continue
        if s.startswith("### "):
            flush(); blocks.append(("h2", s[4:])); continue
        if s.startswith("## "):
            flush(); blocks.append(("h1", s[3:])); continue
        if s.startswith("# "):
            flush(); blocks.append(("h1", s[2:])); continue
        m = re.match(r"^\*{0,2}(Table \d+\.|Figure \d+\.)", s)
        if m:
            flush(); blocks.append(("caption", s.replace("**", ""))); continue
        if s.startswith("|"):
            continue
        if s.startswith("- "):
            flush(); blocks.append(("li", s[2:])); allow_cont = True; continue
        if allow_cont and not buf and blocks and blocks[-1][0] == "li":
            blocks[-1] = ("li", blocks[-1][1] + " " + s)
        else:
            buf.append(s)
        allow_cont = True
    flush()
    return blocks


class Counter:
    def __init__(self) -> None:
        self.h1 = self.h2 = self.h3 = 0
        self.table = 0
        self.figure = 0

    def title(self, level: int, text: str) -> str:
        text = re.sub(r"^§\s*", "", heading_text(text))
        text = re.sub(r"^\d+(\.\d+)*[.)]?\s+", "", text)
        if level == 1:
            self.h1 += 1
            self.h2 = self.h3 = 0
            return f"{self.h1}. {text}"
        if level == 2:
            self.h2 += 1
            self.h3 = 0
            return f"{self.h1}.{self.h2} {text}"
        self.h3 += 1
        return f"{self.h1}.{self.h2}.{self.h3} {text}"


def emit_caption(doc: Document, text: str, cnt: Counter, refs) -> None:
    m = re.match(r"^Table (\d+)\.", text.strip())
    if m:
        n = int(m.group(1))
        if n not in KEEP_TABLES:
            return
        cnt.table += 1
        para = doc.add_paragraph()
        para.paragraph_format.space_before = Pt(8)
        run = para.add_run(re.sub(r"^Table \d+\.", f"Table {cnt.table}.", text.strip()))
        run.font.size = Pt(8.5)
        # caption number is not the v1 table index
        source = {1: 0, 2: 1, 3: 2}.get(n)
        if source is not None and source in TABLES:
            add_table(doc, TABLES[source])
        return

    m = re.match(r"^Figure (\d+)\.", text.strip())
    if m:
        n = int(m.group(1))
        if n not in KEEP_FIGURES or n not in FIGS:
            return
        cnt.figure += 1
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(FIGS[n], width=Cm(16.0))
        cap = doc.add_paragraph()
        cap.paragraph_format.space_after = Pt(10)
        run = cap.add_run(re.sub(r"^Figure \d+\.", f"Figure {cnt.figure}.", text.strip()))
        run.font.size = Pt(8.5)


def emit_md(doc: Document, path: Path, cnt: Counter, refs: list[str]) -> None:
    in_refs = False
    # utf-8-sig: one of the compressed files was written with a BOM, and a BOM turns
    # "4. Results" into an unrecognised line and buries the whole section.
    for kind, payload in parse_md(path.read_text(encoding="utf-8-sig")):
        text = str(payload).strip() if payload is not None else ""
        if in_refs:
            m = re.match(r"^(\d+)\.\s+(.*)$", text)
            if m:
                para = doc.add_paragraph()
                para.paragraph_format.left_indent = Cm(0.9)
                para.paragraph_format.first_line_indent = Cm(-0.9)
                para.add_run(f"{m.group(1)}. ").bold = True
                para.add_run(m.group(2)).font.size = Pt(9)
            continue
        if kind in ("h1", "h2", "h3") and re.sub(r"^[\d.\s]+", "", text).strip().lower() == "references":
            doc.add_heading("References", level=1)
            in_refs = True
            continue
        if kind in ("h1", "h2", "h3"):
            level = {"h1": 1, "h2": 2, "h3": 3}[kind]
            doc.add_heading(cnt.title(level, text), level=level)
        elif kind == "table":
            pass # source tables are handled by caption
        elif kind == "caption":
            emit_caption(doc, text, cnt, refs)
        elif kind == "li":
            p = doc.add_paragraph(style="List Bullet")
            add_runs_with_cites(p, text, refs)
            add_new_tables(doc, p, cnt)
        elif text == "[[FIG:framework]]":
            cnt.figure += 1
            pic = doc.add_paragraph()
            pic.alignment = WD_ALIGN_PARAGRAPH.CENTER
            pic.add_run().add_picture(str(CSEA / "figures" / "fig_framework_v2.png"),
                                      width=Cm(16.0))
            cap = doc.add_paragraph()
            cap.paragraph_format.space_after = Pt(10)
            cap.add_run(
                f"Figure {cnt.figure}. The adjudication order. Each guard asks one question and "
                "the first that fails returns the status shown; comparison is reached only after "
                "identity, evidence, analysability, declaration, a decision procedure and "
                "exclusion have all passed.").font.size = Pt(8.5)
        elif text:
            p = doc.add_paragraph()
            p.paragraph_format.first_line_indent = Cm(0.75)
            add_runs_with_cites(p, text, refs)
            add_new_tables(doc, p, cnt)


def add_new_tables(doc: Document, para, cnt: Counter) -> None:
    """Handle [[TABLE:kind]] markers that ride at the end of a paragraph."""
    text = para.text
    for kind in ("verdicts", "decomp", "rbl"):
        marker = f"[[TABLE:{kind}]]"
        if marker not in text:
            continue
        for run in list(para.runs):
            if marker in run.text:
                run.text = run.text.replace(marker, "")
        cnt.table += 1
        new_table(doc, kind)
        cap = doc.add_paragraph()
        cap.add_run(
            f"Table {cnt.table}. " + {
                "verdicts": "Verdicts and reason codes over the aligned units.",
                "decomp": "Where the omission comes from.",
                "rbl": "Records on the binding ladder.",
            }[kind]).font.size = Pt(8.5)


def build() -> Document:
    doc = Document()
    st = doc.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(10.5)
    st.paragraph_format.space_after = Pt(6)
    st.paragraph_format.line_spacing = 1.15
    for name, size in (("Heading 1", 14), ("Heading 2", 12), ("Heading 3", 11)):
        s = doc.styles[name]
        s.font.name = "Times New Roman"
        s.font.size = Pt(size)
        s.font.bold = True
        s.font.color.rgb = RGBColor(0, 0, 0)

    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(TITLE); r.bold = True; r.font.size = Pt(16)
    for text, size, italic in ((AUTHORS, 10.5, False), (AFFIL, 9.5, True),
                               (CORR, 9, True)):
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(text); run.font.size = Pt(size); run.italic = italic

    doc.add_heading("Abstract", level=1)
    p = doc.add_paragraph(); p.paragraph_format.first_line_indent = Cm(0.75)
    add_runs(p, ABSTRACT)
    p = doc.add_paragraph(); p.add_run("Keywords: ").bold = True; p.add_run(KEYWORDS)
    doc.add_heading("Highlights", level=1)
    for h in HIGHLIGHTS:
        doc.add_paragraph(h, style="List Bullet")

    cnt = Counter()
    refs: list[str] = []
    for name in ("S1_S2_out.md", "S3_rq.md", "S4_framework.md",
                 "S5_design.md", "S6_results.md"):
        emit_md(doc, WORK / name, cnt, refs)

    doc.add_heading(cnt.title(2, "What the corpus can and cannot decide"), level=2)
    for block in parse_md(F57):
        kind, payload = block
        text = str(payload).strip()
        if kind in ("h1", "h2", "h3"):
            continue
        if kind == "p" or kind == "li":
            p = doc.add_paragraph()
            p.paragraph_format.first_line_indent = Cm(0.75)
            add_runs_with_cites(p, text, refs)
            add_new_tables(doc, p, cnt)

    emit_md(doc, WORK / "S789_out.md", cnt, refs)

    for name in ("declarations_en.md", "availability_en.md"):
        body = (CSEA / name).read_text(encoding="utf-8")
        i = body.find("## 作者备注")
        if i > 0:
            body = body[:i]
        doc.add_page_break()
        for kind, payload in parse_blocks(body):
            text = str(payload).strip() if payload is not None else ""
            if kind == "h1":
                doc.add_heading(heading_text(text), level=1)
            elif kind == "h2":
                # Declaration subheads carry one or two lines of boilerplate each.
                # As headings they would break the five-line rule; as bold lead-ins
                # they read the same and leave a single well-filled section.
                para = doc.add_paragraph()
                para.paragraph_format.space_before = Pt(8)
                para.add_run(heading_text(text) + ".").bold = True
            elif kind == "li":
                doc.add_paragraph(text, style="List Bullet")
            elif kind == "table":
                pass
            elif text:
                p = doc.add_paragraph(); add_runs(p, text)
    for para in doc.paragraphs:
        body = para.text.strip()
        if para.style.name.startswith("Heading") or not body:
            continue
        if re.match(r"^(Table|Figure|Note)\.?\s", body) or len(body.split()) < 12:
            continue
        para.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    return doc


if __name__ == "__main__":
    d = build()
    for t in d.tables:
        three_line(t)
    d.save(str(OUT))
    print(f"saved: {OUT}")
    print(f"paragraphs {len(d.paragraphs)} tables {len(d.tables)} images {len(d.inline_shapes)}")