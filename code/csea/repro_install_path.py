"""ANALYSIS A - parallel, timed. Install-path cross of the 14,797 frozen units / 5,407 drifted."""
import sqlite3, json, collections, re, csv, time, sys
from concurrent.futures import ThreadPoolExecutor

DB_URI = r"file:D:\Data\mcp-audit\mcp_audit.db?mode=ro"
PROBE = r"D:\Data\mcp-audit\c1_reclone_probe.json"
BASE = r"C:\Users\Fan\Downloads\1\mcp-audit\paper\csea\out\_exp"
T0 = time.time()

def conn():
    c = sqlite3.connect(DB_URI, uri=True, check_same_thread=False)
    c.row_factory = sqlite3.Row
    for p in ("PRAGMA mmap_size=268435456", "PRAGMA cache_size=-200000", "PRAGMA temp_store=MEMORY"):
        c.execute(p)
    return c

# ---------- step 1: frozen unit set (single cheap query on indexes) ----------
t = time.time()
c0 = conn()
UNIT_SQL = """
WITH p AS (
  SELECT source,name,version FROM snapshots
  WHERE version IS NOT NULL AND version<>'' AND version NOT IN ('latest','unknown')
  GROUP BY source,name,version
  HAVING COUNT(DISTINCT round_id)>=2
     AND COUNT(DISTINCT COALESCE(code_json,''))=1
     AND COUNT(DISTINCT COALESCE(remotes_json,''))=1)
SELECT DISTINCT ri.norm_url AS norm_url, GROUP_CONCAT(DISTINCT p.version) AS versions
FROM p JOIN repo_index ri ON ri.source=p.source AND ri.name=p.name
JOIN clone_log cl ON cl.norm_url=ri.norm_url
WHERE cl.status='ok' AND cl.commit_sha IS NOT NULL AND cl.commit_sha<>''
GROUP BY ri.norm_url"""
units = {r["norm_url"]: r["versions"] for r in c0.execute(UNIT_SQL)}
names = collections.defaultdict(list)
for r in c0.execute("SELECT norm_url,name,source FROM repo_index WHERE norm_url IS NOT NULL"):
    if r["norm_url"] in units:
        names[r["norm_url"]].append((r["source"], r["name"]))
c0.close()
t_units = time.time() - t
print(f"[t] frozen units {len(units)} in {t_units:.1f}s", flush=True)

# ---------- step 2: per-unit package evidence, chunked + threaded ----------
RE_RANGE = re.compile(r"\|\||\s-\s|\*|\.x$|\.X$|^[\^~<>]=?|^=")
RE_CONCRETE = re.compile(r"^\d+(?:\.\d+)+[0-9A-Za-z.\-+]*$")
RE_BARE = re.compile(r"^\d+$")
TAGS = {"latest", "next", "stable", "beta", "alpha", "canary", "main", "master"}

def vclass(s):
    t = (s or "").strip(); low = t.lower()
    if not t: return "absent"
    if low in TAGS: return "tag"
    if RE_RANGE.search(t): return "range"
    if RE_BARE.match(t): return "range"
    if RE_CONCRETE.match(t): return "exact"
    return "other"

def work(chunk):
    c = conn(); out = {}
    for nu in chunk:
        entries = []; remotes = 0; hints = set(); idents = set()
        for src, nm in names.get(nu, []):
            for r in c.execute("SELECT packages_json, remotes_json FROM snapshots WHERE source=? AND name=?", (src, nm)):
                for p in json.loads(r["packages_json"] or "[]"):
                    if not isinstance(p, dict): continue
                    v = (p.get("version") or "").strip()
                    entries.append((str(p.get("registryType") or "").lower(), v,
                                    (p.get("runtimeHint") or "").strip().lower()))
                    h = (p.get("runtimeHint") or "").strip().lower()
                    if h: hints.add(h)
                    i = (p.get("identifier") or "").strip()
                    if i:
                        if "github.com" in i.lower() or i.startswith("github:"): idents.add("git_url")
                        elif i.startswith("http"): idents.add("http")
                        elif p.get("registryType") == "oci": idents.add("oci_ref")
                        else: idents.add("registry_name")
                rj = json.loads(r["remotes_json"] or "[]")
                if rj: remotes = max(remotes, len(rj))
        vers = [e[1] for e in entries if e[1]]
        cls = {vclass(v) for v in vers}
        if not entries: uc = "no_spec"
        elif not vers: uc = "spec_no_version"
        elif cls & {"range", "tag", "other"}: uc = "spec_range"
        else: uc = "spec_exact"
        out[nu] = dict(entries=entries, remotes=remotes, uclass=uc,
                       hints=sorted(hints), idents=sorted(idents))
    c.close()
    return out

t = time.time()
chunks = [list(units)[i:i + 500] for i in range(0, len(units), 500)]
rec = {}
with ThreadPoolExecutor(max_workers=12) as ex:
    for part in ex.map(work, chunks):
        rec.update(part)
t_scan = time.time() - t
print(f"[t] package scan {len(rec)} units in {t_scan:.1f}s (12 threads, {len(chunks)} chunks)", flush=True)

# ---------- step 3: probe verdicts ----------
probe = json.load(open(PROBE, encoding="utf-8"))
sha = {r["norm_url"]: r["commit_sha"] for r in conn().execute(
    "SELECT norm_url, commit_sha FROM clone_log WHERE status='ok' AND commit_sha<>''")}

def pclass(nu):
    p = probe.get(nu) or {}
    if p.get("head_sha"):
        return "unchanged" if p["head_sha"].lower() == (sha.get(nu) or "").lower() else "changed"
    e = p.get("error") or ""
    if "Repository not found" in e: return "not_found"
    if "timeout" in e.lower() or "超时" in e: return "timeout"
    return "other_error"

cls = {nu: pclass(nu) for nu in units}

def rate(sub):
    n = len(sub); m = sum(1 for nu in sub if cls[nu] == "changed")
    res = sum(1 for nu in sub if cls[nu] in ("changed", "unchanged"))
    return dict(n=n, moved=m, rate_all=round(m / n, 4) if n else None,
                resolved=res, rate_resolved=round(m / res, 4) if res else None)

allu = list(units)
spec = [nu for nu in allu if rec[nu]["uclass"] != "no_spec"]
nospec = [nu for nu in allu if rec[nu]["uclass"] == "no_spec"]
advanced = [nu for nu in allu if cls[nu] == "changed"]
gone = [nu for nu in allu if cls[nu] == "not_found"]
drifted = advanced + gone

def crosstab(sub):
    return dict(
        total=len(sub),
        spec=sum(1 for nu in sub if rec[nu]["uclass"] != "no_spec"),
        spec_exact=sum(1 for nu in sub if rec[nu]["uclass"] == "spec_exact"),
        spec_range=sum(1 for nu in sub if rec[nu]["uclass"] == "spec_range"),
        spec_no_version=sum(1 for nu in sub if rec[nu]["uclass"] == "spec_no_version"),
        no_spec=sum(1 for nu in sub if rec[nu]["uclass"] == "no_spec"),
        pkg_only=sum(1 for nu in sub if rec[nu]["uclass"] != "no_spec" and not rec[nu]["remotes"]),
        pkg_remote=sum(1 for nu in sub if rec[nu]["uclass"] != "no_spec" and rec[nu]["remotes"]),
        remote_only=sum(1 for nu in sub if rec[nu]["uclass"] == "no_spec" and rec[nu]["remotes"]),
        repo_only=sum(1 for nu in sub if rec[nu]["uclass"] == "no_spec" and not rec[nu]["remotes"]),
    )

res = {
    "universe": dict(units=len(allu), verdicts=dict(collections.Counter(cls.values())),
                     multi_source=sum(1 for nu in allu if len(names[nu]) > 1 and False)),
    "classes": {k: rate([nu for nu in allu if rec[nu]["uclass"] == k])
                for k in ("no_spec", "spec_no_version", "spec_exact", "spec_range")},
    "all": rate(allu), "spec_any": rate(spec), "no_spec": rate(nospec),
    "advanced": crosstab(advanced), "gone": crosstab(gone), "drifted_5407": crosstab(drifted),
    "advanced_n": len(advanced), "gone_n": len(gone),
    "entry_classes": dict(collections.Counter(vclass(e[1]) if e[1] else "absent"
                                              for nu in allu for e in rec[nu]["entries"])),
    "registry_types": dict(collections.Counter(t for nu in allu for t in
                                               {e[0] for e in rec[nu]["entries"] if e[0]})),
    "runtime_hints": dict(collections.Counter(h for nu in allu for h in rec[nu]["hints"])),
    "git_identifiers": sum(1 for nu in allu if "git_url" in rec[nu]["idents"]),
    "drifted_hints": dict(collections.Counter(h for nu in drifted for h in rec[nu]["hints"])),
    "drifted_registry_types": dict(collections.Counter(t for nu in drifted for t in
                                                       {e[0] for e in rec[nu]["entries"] if e[0]})),
    "drifted_git_idents": sum(1 for nu in drifted if "git_url" in rec[nu]["idents"]),
    "git_ident_drifted": sum(1 for nu in drifted if "git_url" in rec[nu]["idents"] and cls[nu] == "changed"),
    "git_ident_all_rate": rate([nu for nu in allu if "git_url" in rec[nu]["idents"]]),
    "runner_hint_units": sum(1 for nu in allu if rec[nu]["hints"]),
    "runner_hint_drifted": sum(1 for nu in drifted if rec[nu]["hints"]),
}
# package version equal to the frozen declared version, among exact-pinned units
def declared(nu): return {v for v in (units[nu] or "").split(",") if v}
res["exact_equal_declared"] = rate([nu for nu in allu if rec[nu]["uclass"] == "spec_exact"
                                    and any(v in declared(nu) for v in [e[1] for e in rec[nu]["entries"]])])
res["exact_but_differs"] = rate([nu for nu in allu if rec[nu]["uclass"] == "spec_exact"
                                 and not any(v in declared(nu) for v in [e[1] for e in rec[nu]["entries"]])])
res["timing"] = dict(total_s=round(time.time() - T0, 1), units_s=round(t_units, 1),
                     scan_s=round(t_scan, 1))
json.dump(res, open(BASE + r"\A_parallel.json", "w", encoding="utf-8"), indent=2, ensure_ascii=False)

# per-unit csv
with open(BASE + r"\A_units2.csv", "w", encoding="utf-8", newline="") as fh:
    w = csv.writer(fh); w.writerow(["norm_url", "verdict", "uclass", "remotes", "versions"])
    for nu in allu: w.writerow([nu, cls[nu], rec[nu]["uclass"], rec[nu]["remotes"], units[nu]])

print(json.dumps({k: v for k, v in res.items() if k != "registry_types"}, indent=2, ensure_ascii=False))
print("TIMING", res["timing"], "wall", time.time() - T0)
