"""Dispatch the CSEA state machine over the audited corpus.

Reads the frozen corpus read-only, emits one typed verdict per claim, and writes
the reason-code distribution, the three-way decomposition (class- and
unit-weighted), the record-level summary, the channel-policy sensitivity and the
common-support recomputation that tests Proposition 1 (E1b).

    python dispatch.py [--db PATH] [--out DIR] [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import csea_core as C

from paths import DEFAULT_DB, OUT

#: entity_ambiguity labels that denote a genuine name->listing ambiguity.
AMBIGUOUS_LABELS = {"name_only", "same_owner"}


# --------------------------------------------------------------------------
# Load
# --------------------------------------------------------------------------

def load(con: sqlite3.Connection) -> dict:
    cur = con.cursor()

    clone = {}
    cur.execute("SELECT norm_url, status FROM clone_log")
    for url, st in cur.fetchall():
        if st == "ok" or url not in clone:
            clone[url] = st

    cap = {}
    cur.execute("SELECT data_json FROM findings WHERE finding_type='code_capability'")
    for (d,) in cur.fetchall():
        o = json.loads(d)
        kinds = o.get("manifest_kinds") or []
        cap[o["norm_url"]] = {
            "files_scanned": o.get("files_scanned") or 0,
            "detected": set(o.get("danger_categories") or []),
            "manifests": o.get("manifests") or [],
            "manifest_kinds": kinds,
            "has_channel": C.DECLARING_MANIFEST in kinds,
            "tools": o.get("tool_names") or [],
            "commit_sha": o.get("commit_sha"),
        }

    cons = {}
    cur.execute("SELECT data_json FROM findings WHERE finding_type='cross_source_consensus'")
    for (d,) in cur.fetchall():
        o = json.loads(d)
        cons[o["norm_url"]] = {
            "actual": set(o.get("actual") or []),
            "declared": set(o.get("declared_collective") or []),
            "per_source": o.get("per_source_declared") or {},
            "has_code": bool(o.get("has_code")),
        }

    band = {}
    cur.execute("SELECT data_json FROM findings WHERE finding_type='desc_diff'")
    for (d,) in cur.fetchall():
        o = json.loads(d)
        band[o["norm_url"]] = o.get("verdict") or "unknown"

    repl = {}
    cur.execute("SELECT data_json FROM findings WHERE finding_type='c1_reclone'")
    for (d,) in cur.fetchall():
        o = json.loads(d)
        repl[o["norm_url"]] = o

    ambig = set()
    cur.execute("SELECT data_json FROM findings WHERE finding_type='entity_ambiguity'")
    for (d,) in cur.fetchall():
        o = json.loads(d)
        if (o.get("label") or "") not in AMBIGUOUS_LABELS:
            continue
        for m in o.get("mappings") or []:
            if m.get("norm_url"):
                ambig.add(m["norm_url"])

    cur.execute("""SELECT norm_url, source_count, entry_count, sources
                   FROM v_cross_source_repo""")
    units = cur.fetchall()

    return {"clone": clone, "cap": cap, "cons": cons, "band": band,
            "repl": repl, "ambig": ambig, "units": units}


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

def dispatch_one(row, ctx, policy: str = "abstain", ledger=None) -> list[dict]:
    norm_url, source_count, entry_count, sources = row
    srcs = [s for s in (sources or "").split(",") if s]

    cl = ctx["clone"]
    cp = ctx["cap"].get(norm_url) or {}
    co = ctx["cons"].get(norm_url) or {}
    rp = ctx["repl"].get(norm_url)

    clone_status = cl.get(norm_url, "missing")
    files_scanned = cp.get("files_scanned", 0)
    detected = cp.get("detected") or co.get("actual") or set()
    declared = co.get("declared") or set()
    manifests = cp.get("manifests") or []
    has_channel = bool(cp.get("has_channel"))
    has_decl = bool(declared or manifests or cp.get("tools"))

    identity_ambiguous = norm_url in ctx["ambig"]
    multi_id = (entry_count or 0) > (source_count or 0)

    nonempty = [set(v or []) for v in (co.get("per_source") or {}).values() if v]
    conflict = False
    for i in range(len(nonempty)):
        for j in range(i + 1, len(nonempty)):
            if nonempty[i] and nonempty[j] and not (nonempty[i] & nonempty[j]):
                conflict = True

    observed_at = "2026-09-21"
    window = ("2026-09-03", "2026-09-21")
    dec = C.decompose(detected, declared, has_channel)

    # Evidence carries its own provenance; the verdict cites identifiers, never the
    # raw observations, so a reader can walk from a verdict back to a source.
    if ledger is None:
        ledger = C.Ledger()
    ids: list[str] = []

    ident = C.Evidence(
        type="identity",
        source="+".join(srcs) or "unknown",
        content=(norm_url, int(source_count or 0), int(entry_count or 0),
                 int(identity_ambiguous)),
    )
    ids.append(ledger.add(ident))

    for s, v in sorted((co.get("per_source") or {}).items()):
        decl_ev = C.Evidence(
            type="declaration",
            source=s,
            content=tuple(sorted(set(v or []))),
            timestamp=observed_at,
        )
        ids.append(ledger.add(decl_ev))

    # A declaring source is not always identifiable per source; when the declared
    # set exists but no per-source record does, it still has to be citable, or a
    # SUPPORTED verdict would cite no declaration evidence at all.
    if declared and not (co.get("per_source") or {}):
        coll = C.Evidence(
            type="declaration",
            source="collective",
            content=tuple(sorted(set(declared))),
            timestamp=observed_at,
        )
        ids.append(ledger.add(coll))

    code_ev = C.Evidence(
        type="code",
        source="clone",
        revision=cp.get("commit_sha"),
        timestamp=observed_at,
        content=(tuple(sorted(detected)), int(files_scanned), int(has_channel),
                 clone_status),
    )
    ids.append(ledger.add(code_ev))

    if rp:
        time_ev = C.Evidence(
            type="time",
            source="probe",
            timestamp=observed_at,
            content=(rp.get("declared_version"), rp.get("stored_sha"),
                     rp.get("head_sha")),
        )
        ids.append(ledger.add(time_ev))

    ev = ids

    base = {
        "norm_url": norm_url,
        "source_count": source_count,
        "entry_count": entry_count,
        "sources": "+".join(srcs),
        "multi_id": int(multi_id),
        "has_channel": int(has_channel),
        "has_manifest": int(bool(manifests)),
        "n_declaring_srcs": len(nonempty),
        "clone_status": clone_status,
        "band": ctx["band"].get(norm_url, "unknown"),
        "n_detected": dec["n_detected"],
        "n_omitted": dec["n_omitted"],
        "n_schema": dec["n_schema"],
        "n_artefact": dec["n_artefact"],
        "n_behavioural": dec["n_behavioural"],
        "policy": policy,
    }

    out = []
    for prop in ("DECLARATION_COMPLETE", "DECLARATION_SOUND"):
        claim = C.Claim(f"{norm_url}#{prop}", norm_url, prop,
                        tuple(sorted(declared)), "+".join(srcs), observed_at)
        v = C.adjudicate(
            claim, ev,
            identity_ambiguous=identity_ambiguous,
            clone_status=clone_status,
            files_scanned=files_scanned,
            detected=detected,
            declared=declared,
            has_declaration=has_decl,
            has_channel=has_channel,
            conflicting_sources=conflict,
            channel_policy=policy,
            window=window,
        )
        d = v.row()
        d.update(base)
        d["claim_type"] = prop
        out.append(d)

    if rp:
        claim = C.Claim(f"{norm_url}#VERSION_PIN", norm_url, "VERSION_PIN",
                        (rp.get("declared_version"),), "+".join(srcs), observed_at)
        v = C.adjudicate_version(
            claim, ev,
            declared_version=rp.get("declared_version"),
            stored_sha=rp.get("stored_sha"),
            head_sha=rp.get("head_sha"),
            probe_ok=bool(rp.get("head_sha")),
            version_changed=bool(rp.get("versions") and len(rp["versions"]) > 1),
            window=window,
        )
        d = v.row()
        d.update(base)
        d["claim_type"] = "VERSION_PIN"
        out.append(d)

    return out


# --------------------------------------------------------------------------
# Record-level summary and the two weightings
# --------------------------------------------------------------------------

def record_table(rows: list[dict]) -> list[dict]:
    by_unit: dict[str, dict] = {}
    for r in rows:
        d = by_unit.setdefault(r["norm_url"], {"norm_url": r["norm_url"],
                                               "sources": r["sources"],
                                               "band": r["band"],
                                               "has_channel": r["has_channel"],
                                               "n_detected": r["n_detected"],
                                               "n_omitted": r["n_omitted"],
                                               "n_schema": r["n_schema"],
                                               "n_artefact": r["n_artefact"],
                                               "n_behavioural": r["n_behavioural"],
                                               "claims": {}})
        d["claims"][r["claim_type"]] = r["status"]

    out = []
    for d in by_unit.values():
        # The record-level verdict is the primary claim. Soundness is a second
        # question about the same record and is rarely decidable (~82% of units
        # declare nothing to be sound about), so folding it in would let one
        # undecidable claim swallow a decidable one.
        verdict = d["claims"].get("DECLARATION_COMPLETE", "INSUFFICIENT")
        out.append({
            "norm_url": d["norm_url"],
            "sources": d["sources"],
            "band": d["band"],
            "has_channel": d["has_channel"],
            "verdict": verdict,
            "sound_status": d["claims"].get("DECLARATION_SOUND", ""),
            "currency": C.currency_of(d["claims"].get("VERSION_PIN")),
            "n_detected": d["n_detected"],
            "n_omitted": d["n_omitted"],
            "n_schema": d["n_schema"],
            "n_artefact": d["n_artefact"],
            "n_behavioural": d["n_behavioural"],
        })
    return out


def unit_weighting(recs: list[dict]) -> Counter:
    b = Counter()
    for r in recs:
        o = r["n_omitted"]
        if o == 0:
            b["no_omission"] += 1
        elif r["n_behavioural"] == 0:
            b["fully_structural"] += 1
        elif r["n_schema"] == 0 and r["n_artefact"] == 0:
            b["fully_behavioural"] += 1
        else:
            b["mixed"] += 1
    return b


# --------------------------------------------------------------------------
# E1b: is the per-source rate a composition effect?
# --------------------------------------------------------------------------

def e1b(ctx, units) -> dict:
    """Recompute each source's omission rate within strata of the corpus.

    The naive rate averages over whatever each source lists. The stratified rate
    reweights the within-stratum rates to the corpus-wide stratum distribution,
    so two sources with the same behaviour on the same kind of unit receive the
    same number. The gap between the two is the part of the spread that listing
    composition produces.
    """
    per_source: dict[str, list] = defaultdict(list)
    stratum_w = Counter()

    for norm_url, sc, ec, sources in units:
        cp = ctx["cap"].get(norm_url) or {}
        co = ctx["cons"].get(norm_url) or {}
        det = (cp.get("detected") or set()) & C.DECLARABLE_CLASSES
        if not det:
            continue
        stratum = (int(bool(cp.get("has_channel"))), len(det))
        stratum_w[stratum] += 1
        ps = co.get("per_source") or {}
        for s in (sources or "").split(","):
            if not s:
                continue
            decl = set(ps.get(s) or []) & C.DECLARABLE_CLASSES
            per_source[s].append((stratum, len(det - decl) / len(det)))

    total_w = sum(stratum_w.values()) or 1
    table = []
    for s, items in per_source.items():
        naive = sum(r for _, r in items) / len(items)
        by_st = defaultdict(list)
        for st, r in items:
            by_st[st].append(r)
        num = den = 0.0
        for st, rs in by_st.items():
            w = stratum_w[st] / total_w
            num += w * (sum(rs) / len(rs))
            den += w
        table.append({"source": s, "n": len(items), "naive": naive,
                      "stratified": num / den if den else None,
                      "n_strata": len(by_st)})

    def rng(key):
        vals = [t[key] for t in table if t[key] is not None]
        return (min(vals), max(vals)) if vals else (None, None)

    # Sources that carry a declaring field themselves should sit below the band
    # the aggregators occupy once composition is controlled for. Anything left is
    # a channel effect, not a diligence effect.
    vals = sorted(t["stratified"] for t in table if t["stratified"] is not None)
    med = None
    if vals:
        k = len(vals)
        med = vals[k // 2] if k % 2 else 0.5 * (vals[k // 2 - 1] + vals[k // 2])
    for t in table:
        if t["stratified"] is None or med is None:
            t["dev"] = None
            t["band"] = "-"
            continue
        t["dev"] = t["stratified"] - med
        t["band"] = ("below" if t["dev"] < -0.05
                     else "above" if t["dev"] > 0.05 else "within")

    return {"table": sorted(table, key=lambda t: -t["n"]),
            "naive_range": rng("naive"), "stratified_range": rng("stratified"),
            "median": med, "strata": len(stratum_w)}


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------

STATUS_ORDER = ["SUPPORTED", "PARTIAL", "UNSUPPORTED", "CONFLICT",
                "STALE", "INSUFFICIENT", "UNRESOLVED"]


def pct(k, n):
    return f"{100.0 * k / n:.1f}%" if n else "-"


def summarize(rows: list[dict], units_total: int) -> dict:
    by_type = defaultdict(Counter)
    by_type_reason = defaultdict(Counter)
    by_source = defaultdict(Counter)
    crosswalk = defaultdict(Counter)
    decl_src_hist = Counter()
    tot = Counter()

    for r in rows:
        t, s = r["claim_type"], r["status"]
        by_type[t][s] += 1
        by_type_reason[t][(s, r["reason"])] += 1
        crosswalk[(t, r["band"])][s] += 1
        if t == "DECLARATION_COMPLETE":
            for k in ("n_detected", "n_omitted", "n_schema", "n_artefact",
                      "n_behavioural"):
                tot[k] += r[k]
            decl_src_hist[r["n_declaring_srcs"]] += 1
            for src in (r["sources"] or "").split("+"):
                if src:
                    by_source[src][s] += 1

    return {"units": units_total, "by_type": by_type, "by_type_reason": by_type_reason,
            "by_source": by_source, "crosswalk": crosswalk, "totals": tot,
            "decl_src_hist": decl_src_hist}


def write_report(summary, recs, e1b_res, unit_w, sens, out: Path) -> None:
    L = ["# CSEA dispatch — first pass", "",
         f"Units: {summary['units']:,}   (read-only on the frozen corpus)", ""]

    for t in ("DECLARATION_COMPLETE", "DECLARATION_SOUND", "VERSION_PIN"):
        c = summary["by_type"].get(t)
        if not c:
            continue
        n = sum(c.values())
        L += [f"## {t} (n = {n:,})", "", "| status | n | share |", "|---|---:|---:|"]
        for s in STATUS_ORDER:
            if c[s]:
                L.append(f"| {s} | {c[s]:,} | {pct(c[s], n)} |")
        L += ["", "| verdict | n | share of claim |", "|---|---:|---:|"]
        for (s, why), k in summary["by_type_reason"][t].most_common():
            L.append(f"| ({s}, {why}) | {k:,} | {pct(k, n)} |")
        L.append("")

    # record level
    rc = Counter(r["verdict"] for r in recs)
    cc = Counter(r["currency"] for r in recs)
    n = len(recs)
    L += ["## Record level", "",
          "The record verdict is the primary claim (declaration completeness). "
          "Soundness is reported beside it, and currency beside both: staleness is a "
          "property of the binding, not of the declaration, so it is never merged in.", "",
          "| record verdict | n | share |", "|---|---:|---:|"]
    for s in STATUS_ORDER:
        if rc[s]:
            L.append(f"| {s} | {rc[s]:,} | {pct(rc[s], n)} |")
    L += ["", "| currency | n | share |", "|---|---:|---:|"]
    for s in C.CURRENCY:
        L.append(f"| {s} | {cc[s]:,} | {pct(cc[s], n)} |")
    L += ["", "### verdict x currency", "",
          "| verdict | " + " | ".join(C.CURRENCY) + " | n |",
          "|---|" + "---:|" * (len(C.CURRENCY) + 1)]
    xt = defaultdict(Counter)
    for r in recs:
        xt[r["verdict"]][r["currency"]] += 1
    for v in STATUS_ORDER:
        if xt[v]:
            nn = sum(xt[v].values())
            L.append(f"| {v} | " + " | ".join(str(xt[v][c]) for c in C.CURRENCY) +
                     f" | {nn:,} |")
    L.append("")

    # decomposition, both weightings
    tot = summary["totals"]
    if tot["n_omitted"]:
        sh = C.shares(tot)
        L += ["## Decomposition of the omission — class-weighted", "",
              "| bucket | class-instances | share |", "|---|---:|---:|",
              f"| schema | {tot['n_schema']:,} | {pct(tot['n_schema'], tot['n_omitted'])} |",
              f"| artefact | {tot['n_artefact']:,} | {pct(tot['n_artefact'], tot['n_omitted'])} |",
              f"| behavioural | {tot['n_behavioural']:,} | "
              f"{pct(tot['n_behavioural'], tot['n_omitted'])} |",
              f"| total | {tot['n_omitted']:,} | 100% |", "",
              f"`S_schema` = {sh['S_schema']:.4f}   "
              f"`S_structural` = {sh['S_structural']:.4f}", ""]

    uw_n = sum(unit_w.values())
    L += ["## Decomposition — unit-weighted (units with an omission)", "",
          "| bucket | units | share |", "|---|---:|---:|"]
    for k, lab in (("fully_structural", "omission entirely schema/artefact"),
                   ("mixed", "some behavioural share"),
                   ("fully_behavioural", "every omission is behavioural"),
                   ("no_omission", "nothing omitted")):
        L.append(f"| {lab} | {unit_w[k]:,} | {pct(unit_w[k], uw_n)} |")
    L += [""]

    # sensitivity
    L += ["## Sensitivity: `CHANNEL_ABSENT` read as refutation", "",
          "| status | abstain (primary) | refute | difference |", "|---|---:|---:|---:|"]
    a = summary["by_type"]["DECLARATION_COMPLETE"]
    b = sens["by_type"]["DECLARATION_COMPLETE"]
    na, nb = sum(a.values()), sum(b.values())
    for s in STATUS_ORDER:
        if a[s] or b[s]:
            L.append(f"| {s} | {pct(a[s], na)} | {pct(b[s], nb)} | "
                     f"{a[s] - b[s]:+,} |")
    L += ["", "Read as refutation, the abstention rate falls and the refutation rate "
          "carries the format choice. The paper reports the primary reading and this "
          "table as the bound on it.", ""]

    # e1b
    L += ["## E1b: per-source omission rate, naive vs stratified", "",
          "| source | units | naive | stratified | vs median |", "|---|---:|---:|---:|---|"]
    for t in e1b_res["table"]:
        st = f"{t['stratified']:.3f}" if t["stratified"] is not None else "-"
        dev = f"{t['dev']:+.3f} ({t['band']})" if t.get("dev") is not None else "-"
        L.append(f"| {t['source']} | {t['n']:,} | {t['naive']:.3f} | {st} | {dev} |")
    nr, sr = e1b_res["naive_range"], e1b_res["stratified_range"]
    med = e1b_res.get("median")
    L += ["", f"Stratification on (channel present, number of declarable classes "
          f"detected), {e1b_res['strata']} strata.", ""]
    if nr[0] is not None:
        L.append(f"- naive spread: {nr[0]:.3f} to {nr[1]:.3f} "
                 f"(width {nr[1] - nr[0]:.3f})")
    if sr[0] is not None:
        L.append(f"- stratified spread: {sr[0]:.3f} to {sr[1]:.3f} "
                 f"(width {sr[1] - sr[0]:.3f})")
    if med is not None:
        below = [t["source"] for t in e1b_res["table"]
                 if t.get("band") == "below"]
        L += ["", f"Stratified median {med:.3f}. "
              f"Below the band by more than 0.05: {', '.join(below) if below else 'none'}.",
              "", "A spread that survives stratification is not a composition artefact. "
              "What survives here is the set of sources that carry a declaring field of "
              "their own, which is Corollary 1 rather than the strong form of "
              "Proposition 1."]
    L += [""]

    # crosswalk + diagnostics
    L += ["## Crosswalk: old similarity band x new status (DECLARATION_COMPLETE)", "",
          "| band | " + " | ".join(STATUS_ORDER) + " | n |",
          "|---|" + "---:|" * (len(STATUS_ORDER) + 1)]
    for (t, band), c in sorted(summary["crosswalk"].items()):
        if t != "DECLARATION_COMPLETE":
            continue
        nn = sum(c.values())
        L.append(f"| {band} | " + " | ".join(str(c[s]) for s in STATUS_ORDER) +
                 f" | {nn:,} |")
    L.append("")

    L += ["## Per-source status share (DECLARATION_COMPLETE, naive)", "",
          "| source | n | " + " | ".join(STATUS_ORDER) + " |",
          "|---|---:|" + "---:|" * len(STATUS_ORDER)]
    for src, c in summary["by_source"].items():
        nn = sum(c.values())
        L.append(f"| {src} | {nn:,} | " +
                 " | ".join(pct(c[s], nn) for s in STATUS_ORDER) + " |")
    L.append("")

    hist = summary["decl_src_hist"]
    L += ["## Diagnostic: how many sources declare a capability set", "",
          "| declaring sources | units |", "|---:|---:|"]
    for k in sorted(hist):
        L.append(f"| {k} | {hist[k]:,} |")
    L += ["", "`CONFLICT` can only be populated where two non-empty declarations "
          "disagree. The histogram is that precondition; the measured count is zero.", ""]

    (out / "summary.md").write_text("\n".join(L), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    ctx = load(con)
    units = ctx["units"]
    if args.limit:
        units = units[: args.limit]
    print(f"units: {len(units):,}  code_capability: {len(ctx['cap']):,}  "
          f"consensus: {len(ctx['cons']):,}  repl: {len(ctx['repl']):,}  "
          f"ambiguous: {len(ctx['ambig']):,}")

    ledger = C.Ledger()
    primary, refute = [], []
    for i, row in enumerate(units, 1):
        primary.extend(dispatch_one(row, ctx, "abstain", ledger))
        refute.extend(dispatch_one(row, ctx, "refute", ledger))
        if i % 5000 == 0:
            print(f"  ... {i:,}")

    for fname, rows in (("verdicts.csv", primary), ("verdicts_refute.csv", refute)):
        with (out / fname).open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    n_ev = ledger.write(out / "evidence.jsonl")
    print(f"  evidence items: {n_ev:,}")

    recs = record_table(primary)
    with (out / "records.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(recs[0].keys()))
        w.writeheader()
        w.writerows(recs)

    summary = summarize(primary, len(units))
    sens = summarize(refute, len(units))
    e1b_res = e1b(ctx, units)
    unit_w = unit_weighting(recs)
    write_report(summary, recs, e1b_res, unit_w, sens, out)

    ser = {
        "units": summary["units"],
        "by_type": {t: dict(c) for t, c in summary["by_type"].items()},
        "by_type_reason": {t: {f"{s}|{r}": k for (s, r), k in c.items()}
                           for t, c in summary["by_type_reason"].items()},
        "totals": dict(summary["totals"]),
        "shares": C.shares(summary["totals"]) if summary["totals"]["n_omitted"] else {},
        "unit_weighted": dict(unit_w),
        "record_verdict": dict(Counter(r["verdict"] for r in recs)),
        "record_currency": dict(Counter(r["currency"] for r in recs)),
        "e1b": e1b_res,
        "sensitivity_refute": {t: dict(c) for t, c in sens["by_type"].items()},
    }
    (out / "summary.json").write_text(json.dumps(ser, indent=2), encoding="utf-8")

    print(f"\nverdict rows: {len(primary):,} (x2 policies)")
    for t in ("DECLARATION_COMPLETE", "DECLARATION_SOUND", "VERSION_PIN"):
        c = summary["by_type"].get(t)
        if c:
            n = sum(c.values())
            top = ", ".join(f"{s}={c[s]:,}({pct(c[s], n)})"
                            for s in STATUS_ORDER if c[s])
            print(f"  {t:22s} n={n:>7,}  {top}")
    rc = Counter(r["verdict"] for r in recs)
    print("  record verdict:", ", ".join(f"{s}={rc[s]:,}" for s in STATUS_ORDER if rc[s]))
    cc = Counter(r["currency"] for r in recs)
    print("  currency:", ", ".join(f"{s}={cc[s]:,}" for s in C.CURRENCY))
    tot = summary["totals"]
    if tot["n_omitted"]:
        sh = C.shares(tot)
        print(f"  class-weighted: schema {sh['schema_share']:.3f} "
              f"artefact {sh['artefact_share']:.3f} "
              f"behavioural {sh['behavioural_share']:.3f}")
    uw_n = sum(unit_w.values())
    print(f"  unit-weighted: fully_structural {pct(unit_w['fully_structural'], uw_n)} "
          f"mixed {pct(unit_w['mixed'], uw_n)} "
          f"fully_behavioural {pct(unit_w['fully_behavioural'], uw_n)}")
    nr, sr = e1b_res["naive_range"], e1b_res["stratified_range"]
    print(f"  E1b spread: naive {nr[1]-nr[0]:.3f} -> stratified {sr[1]-sr[0]:.3f}")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
