"""Invariants I1-I4, tested rather than asserted.

I1  no SUPPORTED verdict without evidence of the types the property requires
I2  a missing declaring channel yields an abstention, never a verdict
I3  a frozen snapshot yields identical verdicts on a second pass
I4  every verdict cites evidence that exists in the ledger

    python mcp_audit\\csea\\invariants.py

Exit code is non-zero if any invariant fails, so this can gate a build.
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

from paths import DEFAULT_DB, OUT
import csea_core as C
import dispatch as D

POSITIVE = {"SUPPORTED", "PARTIAL", "UNSUPPORTED"}


def fail(msg: str) -> None:
    print("  FAIL " + msg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--determinism-n", type=int, default=600)
    args = ap.parse_args()
    out = Path(args.out)

    verdicts = list(csv.DictReader((out / "verdicts.csv").open(encoding="utf-8")))
    evidence = {}
    with (out / "evidence.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            e = json.loads(line)
            evidence[e["evidence_id"]] = e

    results = {}

    # ---- I4 first: everything else depends on the ledger being complete -----
    missing = 0
    empty = 0
    for r in verdicts:
        ids = [i for i in (r["evidence_ids"] or "").split("|") if i]
        if not ids:
            empty += 1
        missing += sum(1 for i in ids if i not in evidence)
    ok4 = (missing == 0 and empty == 0)
    print(f"I4 traceability: {len(verdicts):,} verdicts, {len(evidence):,} evidence items, "
          f"{missing} dangling ids, {empty} uncited")
    if not ok4:
        fail(f"I4 — {missing} ids absent from the ledger, {empty} verdicts cite nothing")
    results["I4"] = {"ok": ok4, "verdicts": len(verdicts),
                     "evidence": len(evidence), "dangling": missing, "uncited": empty}

    # ---- I1: SUPPORTED must cite the property's required evidence types -----
    bad1 = []
    for r in verdicts:
        if r["status"] != "SUPPORTED":
            continue
        prop = C.PROPERTIES.get(r["claim_type"])
        if prop is None:
            continue
        have = {evidence[i]["type"] for i in r["evidence_ids"].split("|")
                if i and i in evidence}
        need = set(prop.evidence_types)
        if not need <= have:
            bad1.append((r["claim_id"], sorted(need - have)))
    ok1 = not bad1
    print(f"I1 evidence sufficiency: {len(bad1)} SUPPORTED verdicts lack a required type")
    for cid, lack in bad1[:5]:
        fail(f"I1 — {cid} missing {lack}")
    results["I1"] = {"ok": ok1, "violations": len(bad1),
                     "sample": [{"claim": c, "missing": m} for c, m in bad1[:20]]}

    # ---- I2: no verdict where the declaring channel is absent ---------------
    bad2 = Counter()
    for r in verdicts:
        if r["claim_type"] != "DECLARATION_COMPLETE":
            continue
        if str(r.get("has_channel", "1")) != "0":
            continue
        if r["status"] in POSITIVE:
            bad2[(r["status"], r["reason"])] += 1
    ok2 = not bad2
    print(f"I2 channel guard: {sum(bad2.values())} verdicts returned without a channel")
    for (st, rs), k in bad2.most_common(5):
        fail(f"I2 — {k} x ({st}, {rs}) on channel-absent units")
    results["I2"] = {"ok": ok2, "violations": sum(bad2.values()),
                     "by_reason": {f"{k[0]}|{k[1]}": v for k, v in bad2.items()}}

    # ---- I3: determinism on a frozen snapshot -------------------------------
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ctx = D.load(con)
    units = ctx["units"][: args.determinism_n]
    runs = []
    for _ in range(2):
        ledger = C.Ledger()
        rows = []
        for row in units:
            rows.extend(D.dispatch_one(row, ctx, "abstain", ledger))
        runs.append([(r["claim_id"], r["status"], r["reason"], r["evidence_ids"])
                     for r in rows])
    same = runs[0] == runs[1]
    diff = sum(1 for a, b in zip(*runs) if a != b)
    print(f"I3 determinism: {len(runs[0]):,} verdicts recomputed twice, "
          f"{diff} differ")
    if not same:
        fail(f"I3 — {diff} verdicts changed between identical runs")
    results["I3"] = {"ok": same, "n": len(runs[0]), "differing": diff}

    # ---- report -------------------------------------------------------------
    ok = all(results[k]["ok"] for k in ("I1", "I2", "I3", "I4"))
    lines = ["# Invariants I1-I4", "",
             f"Verdicts: {len(verdicts):,}   Evidence: {len(evidence):,}", "",
             "| invariant | statement | result | detail |", "|---|---|---|---|",
             f"| I1 | no SUPPORTED without the evidence its property requires | "
             f"{'PASS' if results['I1']['ok'] else 'FAIL'} | "
             f"{results['I1']['violations']} violations |",
             f"| I2 | a missing declaring channel never yields a verdict | "
             f"{'PASS' if results['I2']['ok'] else 'FAIL'} | "
             f"{results['I2']['violations']} violations |",
             f"| I3 | a frozen snapshot reproduces itself | "
             f"{'PASS' if results['I3']['ok'] else 'FAIL'} | "
             f"{results['I3']['differing']}/{results['I3']['n']} differ |",
             f"| I4 | every verdict cites evidence in the ledger | "
             f"{'PASS' if results['I4']['ok'] else 'FAIL'} | "
             f"{results['I4']['dangling']} dangling, {results['I4']['uncited']} uncited |",
             "", f"Overall: **{'PASS' if ok else 'FAIL'}**", ""]
    (out / "invariants.md").write_text("\n".join(lines), encoding="utf-8")
    (out / "invariants.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"overall: {'PASS' if ok else 'FAIL'}  -> {out / 'invariants.md'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
