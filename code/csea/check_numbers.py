"""Check that compression did not invent or alter a number.

The risk in a 60% cut is not that a sentence disappears — that is intended — but that
a surviving sentence carries a number that was rounded, re-derived or mis-typed. This
script compares the numeric vocabulary of the compressed draft against the source it
was cut from.

    python check_numbers.py v4.docx v5.docx

Exit code is non-zero when the draft contains a number the source never had.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

NUM = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
# Citation groups are numbers too, and "[3,39]" tokenises as 339. Strip them first,
# or the audit reports citation lists as invented statistics.
CITE = re.compile(r"\[\d+(?:[,\-]\d+)*\]")


def body_text(path: Path, stop_at: str = "Declarations") -> str:
    doc = Document(str(path))
    out, stop = [], False
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            p = Paragraph(child, doc)
            text = p.text.strip()
            if text == stop_at:
                stop = True
            if not stop:
                out.append(text)
        elif child.tag == qn("w:tbl") and not stop:
            table = Table(child, doc)
            for row in table.rows:
                out.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(out)


def norm(tok: str) -> str:
    return tok.replace(",", "")


def main(src: Path, dst: Path) -> int:
    a, b = CITE.sub(" ", body_text(src)), CITE.sub(" ", body_text(dst))
    src_nums = {norm(x) for x in NUM.findall(a)}
    dst_tokens = NUM.findall(b)
    dst_nums = {norm(x) for x in dst_tokens}

    invented = sorted(dst_nums - src_nums)
    print(f"source: {len(a.split()):,} words, {len(src_nums):,} distinct numerals")
    print(f"draft : {len(b.split()):,} words, {len(dst_nums):,} distinct numerals")
    print(f"numbers in the draft that the source never had: {len(invented)}")
    for n in invented[:25]:
        ctx = re.search(r".{70}" + re.escape(n) + r".{70}", b, re.S)
        print(f"  {n:>10}  …{ctx.group(0).replace(chr(10), ' ') if ctx else '':<150}")

    # a numeral that survives should keep its unit, not its rounding
    lost = sorted(src_nums - dst_nums, key=lambda s: (len(s), s))
    print(f"\nnumbers dropped with their sentences: {len(lost)} (expected in a 60% cut)")
    print("  largest dropped:", ", ".join(lost[-12:]))

    return 1 if invented else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
