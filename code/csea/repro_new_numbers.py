"""Regenerate the numbers added after the first deposit.

Every figure printed by this script appears in Section 4.5 or in Table S7, and every one of
them was added to the paper after the original four-command build. Running this file against
the deposited corpus reproduces them; nothing here is hand-entered.

Usage
-----
    python paper/csea/out/repro_new_numbers.py --db D:/Data/mcp-audit/mcp_audit.db

Output is JSON on stdout, so a re-run can be diffed against the published values.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import math
import pathlib
import re
import sqlite3
from collections import defaultdict

DECLARABLE = {"env_access", "network_egress", "credential_paths"}
NON_DECLARABLE = {"unsafe_deserialize", "subprocess", "arbitrary_code",
                  "fs_destructive", "db_access", "shell_exec"}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round((c - h) * 100, 1), round((c + h) * 100, 1)


def connect(db: str) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def frozen_units(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM findings WHERE finding_type='c1_reclone'")}


def probe_outcomes(con: sqlite3.Connection) -> dict:
    """The five-way outcome the paper reports for the 14,797 frozen units."""
    rows = con.execute(
        "SELECT name, data_json FROM findings WHERE finding_type='c1_reclone'").fetchall()
    out = defaultdict(int)
    for name, data in rows:
        try:
            d = json.loads(data) if data else {}
        except json.JSONDecodeError:
            d = {}
        verdict = str(d.get("verdict", d.get("status", ""))).upper()
        if verdict in ("CHANGED", "DRIFTED"):
            out["changed"] += 1
        elif verdict in ("UNCHANGED", "SAME"):
            out["unchanged"] += 1
        elif "NOT FOUND" in str(d.get("error", "")).upper():
            out["not_found"] += 1
        elif "TIMEOUT" in str(d.get("error", "")).upper():
            out["timeout"] += 1
        else:
            out["other"] += 1
    return dict(out)


def install_path(con: sqlite3.Connection, frozen: set[str]) -> dict:
    """Drift rate by the channel a record declares, not by how a consumer installed.

    packages_json holds objects of the form {"registryType": "npm", "identifier": ...,
    "version": ...}. A unit counts as git-identified when its identifier is a repository
    reference rather than a package name.
    """
    # snapshots.name 是记录名，冻结集合的键是 norm_url；经 repo_index 建立映射
    name_by_url: dict[str, str] = {}
    for name, url in con.execute("SELECT name, norm_url FROM repo_index"):
        if url:
            name_by_url[url] = name
    latest: dict[str, tuple[str, str]] = {}
    for name, pkgs, remotes in con.execute(
            "SELECT name, packages_json, remotes_json FROM snapshots"):
        prev = latest.get(name)
        if prev is None or (len(str(pkgs)) + len(str(remotes))) > (len(prev[0]) + len(prev[1])):
            latest[name] = (str(pkgs or ""), str(remotes or ""))

    outcomes = {}
    for name, data in con.execute(
            "SELECT name, data_json FROM findings WHERE finding_type='c1_reclone'"):
        try:
            d = json.loads(data) if data else {}
        except json.JSONDecodeError:
            d = {}
        outcomes[name] = bool(d.get("changed"))

    out = defaultdict(lambda: {"n": 0, "moved": 0})
    for url in frozen:
        pkgs, remotes = latest.get(name_by_url.get(url, ""), ("", ""))
        moved = outcomes.get(url, False)
        has_pkg = pkgs not in ("", "[]", "null", "None")
        git_id = bool(re.search(r"github:|git\+|\"type\":\s*\"git\"", pkgs + remotes))
        exact = bool(re.search(r"\"version\":\s*\"\d+\.\d+\.\d+", pkgs))
        if git_id:
            stratum = "git_identifier"
        elif has_pkg and exact:
            stratum = "package_exact"
        elif has_pkg:
            stratum = "package_loose"
        elif remotes not in ("", "[]", "null", "None"):
            stratum = "remote_only"
        else:
            stratum = "repository_only"
        out[stratum]["n"] += 1
        out[stratum]["moved"] += int(moved)
    return {k: {**v, "rate": round(v["moved"] / v["n"] * 100, 1) if v["n"] else None,
                "ci": wilson(v["moved"], v["n"]) if v["n"] else None}
            for k, v in sorted(out.items())}


def dead_links(con: sqlite3.Connection) -> dict:
    """The cause taxonomy of the non-resolving URLs, read from clone_log."""
    rows = con.execute("SELECT status, error FROM clone_log").fetchall()
    status = defaultdict(int)
    causes = defaultdict(int)
    for st, err in rows:
        status[st] += 1
        if st != "gone":
            continue
        e = (err or "").lower()
        if "repository not found" in e or "not found" in e:
            causes["not_found"] += 1
        elif "超时" in e or "timed out" in e or "timeout" in e:
            causes["timeout"] += 1
        elif "authentication" in e or "permission" in e:
            causes["auth"] += 1
        else:
            causes["other"] += 1
    return {"status": dict(status), "gone_causes": dict(causes)}


def baseline(result_csv: pathlib.Path, con: sqlite3.Connection, probe_day: str) -> dict:
    """The 500-unit control sample. Requires the probe output from the baseline run."""
    cloned = dict(con.execute("SELECT norm_url, cloned_at FROM clone_log").fetchall())
    rows = list(csv.DictReader(open(result_csv, encoding="utf-8")))
    ok = [r for r in rows if r["status"] in ("same", "changed")]
    chg = [r for r in ok if r["status"] == "changed"]
    day = datetime.date.fromisoformat(probe_day)
    windows = []
    for r in ok:
        c = cloned.get(r["norm_url"])
        if not c:
            continue
        try:
            windows.append(max((day - datetime.date.fromisoformat(c[:10])).days, 1))
        except ValueError:
            continue
    tot = sum(windows)
    return {"n_raw": len(rows), "n_usable": len(ok), "changed": len(chg),
            "unreachable": sum(1 for r in rows if r["status"] == "unreachable"),
            "rate": round(len(chg) / len(ok) * 100, 1),
            "median_window_days": sorted(windows)[len(windows) // 2] if windows else None,
            "per_100_unit_days": round(len(chg) / tot * 100, 2) if tot else None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--baseline-csv", default=None,
                    help="output of the 500-unit control probe, if it is to be re-checked")
    ap.add_argument("--probe-day", default="2026-11-06")
    args = ap.parse_args()

    con = connect(args.db)
    frozen = frozen_units(con)
    result = {
        "frozen_units": len(frozen),
        "probe_outcomes": probe_outcomes(con),
        # 安装路径分层由 repro_install_path.py 生成，见该文件
        "dead_links": dead_links(con),
        "declarable_classes": sorted(DECLARABLE),
        "non_declarable_classes": sorted(NON_DECLARABLE),
    }
    if args.baseline_csv:
        result["baseline"] = baseline(pathlib.Path(args.baseline_csv), con, args.probe_day)
    result["generated_by"] = "paper/csea/out/repro_new_numbers.py"
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
