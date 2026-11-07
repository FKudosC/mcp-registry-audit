"""§7.1 Channel ablation: is each evidence channel necessary?

No reference label is needed for this question. The ablation asks what happens to
the verdict when one channel is removed, which is a property of the rule and the
evidence, not of any annotator.

    python mcp_audit\\csea\\ablation.py

Writes paper/csea/out/ablation.md and ablation.json.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

from paths import DEFAULT_DB, OUT
import csea_core as C
import dispatch as D

STATUSES = ["SUPPORTED", "PARTIAL", "UNSUPPORTED", "CONFLICT", "STALE",
            "INSUFFICIENT", "UNRESOLVED"]

#: Removing a precondition collapses every decidable unit at once, which is a fact
#: about the rule but not an informative ablation. The arms below degrade the
#: evidence instead of deleting it, so they separate.
ARMS = [
    ("full", "complete chain"),
    ("channel_assumed", "every artefact assumed to carry the declaring field"),
    ("official_only", "declarations from the official registry only"),
    ("best_single", "largest single declaration set per unit (post-hoc best)"),
]


def evidence(row, ctx) -> dict:
    norm_url, source_count, entry_count, sources = row
    cp = ctx["cap"].get(norm_url) or {}
    co = ctx["cons"].get(norm_url) or {}
    declared = set(co.get("declared") or set())
    manifests = cp.get("manifests") or []
    return {
        "url": norm_url,
        "sources": [s for s in (sources or "").split(",") if s],
        "clone_status": ctx["clone"].get(norm_url, "missing"),
        "files_scanned": cp.get("files_scanned", 0),
        "detected": set(cp.get("detected") or co.get("actual") or set()),
        "declared": declared,
        "per_source": co.get("per_source") or {},
        "has_channel": bool(cp.get("has_channel")),
        "has_declaration": bool(declared or manifests or cp.get("tools")),
        "ambiguous": norm_url in ctx["ambig"],
    }


def apply_arm(ev: dict, arm: str) -> dict:
    e = dict(ev)
    if arm == "no_code":
        e["clone_status"] = "missing"
        e["files_scanned"] = 0
    elif arm == "no_declaration":
        e["has_declaration"] = False
    elif arm == "no_channel":
        e["has_channel"] = False
    elif arm == "channel_assumed":
        e["has_channel"] = True
    elif arm == "official_only":
        off = set(e["per_source"].get("official") or set())
        e["declared"] = off
        e["has_declaration"] = bool(off)
    elif arm == "best_single":
        sets = [set(v) for v in e["per_source"].values() if v]
        best = max(sets, key=len) if sets else set()
        e["declared"] = best
        e["has_declaration"] = bool(best)
    return e


def verdict(ev: dict) -> tuple[str, str]:
    claim = C.Claim("c", ev["url"], "DECLARATION_COMPLETE",
                    tuple(sorted(ev["declared"])), "+".join(ev["sources"]), "")
    v = C.adjudicate(
        claim, [f"code:{ev['url']}"],
        identity_ambiguous=ev["ambiguous"],
        clone_status=ev["clone_status"],
        files_scanned=ev["files_scanned"],
        detected=ev["detected"],
        declared=ev["declared"],
        has_declaration=ev["has_declaration"],
        has_channel=ev["has_channel"],
    )
    return v.status, v.reason


def pct(k, n):
    return f"{100.0 * k / n:.1f}%" if n else "-"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ctx = D.load(con)
    units = ctx["units"]
    print(f"units: {len(units):,}")

    dist = {}
    changed = {}
    examples = defaultdict(Counter)
    per_source = defaultdict(lambda: {"n": 0, "dist": Counter()})

    for row in units:
        ev = evidence(row, ctx)
        base_status, base_reason = verdict(ev)
        dist.setdefault("full", Counter())[base_status] += 1

        for arm, _ in ARMS[1:]:
            st, rs = verdict(apply_arm(ev, arm))
            dist.setdefault(arm, Counter())[st] += 1
            if st != base_status:
                changed[arm] = changed.get(arm, 0) + 1
                examples[arm][f"{base_status} → {st}"] += 1

        # single-registry baseline: every source that lists the unit gets a vote,
        # computed from its own declarations only
        for s in ev["sources"]:
            e1 = dict(ev)
            e1["declared"] = set(ev["per_source"].get(s) or set())
            e1["has_declaration"] = bool(e1["declared"])
            st, _ = verdict(e1)
            per_source[s]["n"] += 1
            per_source[s]["dist"][st] += 1

    n = len(units)
    L = ["# §7.1 Channel ablation", "",
         f"Units: {n:,}   (verdict recomputed under each ablation)", "",
         "## Verdict distribution by arm", "",
         "| arm | " + " | ".join(STATUSES) + " | abstained |",
         "|---|" + "---:|" * len(STATUSES) + "---:|"]
    for arm, label in ARMS:
        c = dist[arm]
        abst = c["INSUFFICIENT"] + c["UNRESOLVED"]
        L.append(f"| {label} | " + " | ".join(pct(c[s], n) for s in STATUSES) +
                 f" | {pct(abst, n)} |")
    L.append("")

    L += ["## What each channel decides", "",
          "| arm removed | verdicts changed | share | most common shift |",
          "|---|---:|---:|---|"]
    for arm, label in ARMS[1:]:
        ch = changed.get(arm, 0)
        top = examples[arm].most_common(1)
        L.append(f"| {label} | {ch:,} | {pct(ch, n)} | "
                 f"{top[0][0] + f' ({top[0][1]:,})' if top else '-'} |")
    L.append("")

    L += ["## Coverage of a single registry", "",
          "| source | units it can address | share of corpus | SUPPORTED | PARTIAL | "
          "UNSUPPORTED | INSUFFICIENT |", "|---|---:|---:|---:|---:|---:|---:|"]
    for s, d in sorted(per_source.items(), key=lambda kv: -kv[1]["n"]):
        c = d["dist"]
        L.append(f"| {s} | {d['n']:,} | {pct(d['n'], n)} | "
                 + " | ".join(pct(c[x], d["n"]) for x in
                              ("SUPPORTED", "PARTIAL", "UNSUPPORTED", "INSUFFICIENT"))
                 + " |")
    L.append("")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "ablation.md").write_text("\n".join(L), encoding="utf-8")
    (out / "ablation.json").write_text(json.dumps({
        "n": n,
        "dist": {a: dict(c) for a, c in dist.items()},
        "changed": changed,
        "per_source": {s: {"n": d["n"], "dist": dict(d["dist"])}
                       for s, d in per_source.items()},
    }, indent=2), encoding="utf-8")

    print("changed:", {a: f"{v} ({pct(v, n)})" for a, v in changed.items()})
    print(f"wrote {out / 'ablation.md'}")


if __name__ == "__main__":
    main()
