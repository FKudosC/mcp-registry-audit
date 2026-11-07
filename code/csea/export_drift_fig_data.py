"""Export the data behind Figure 7 (drift census).

Two panels, two sources:

    (a) the outcome of the re-probe over the 14,797 frozen units
    (b) commits advanced between the recorded revision and the re-probe, for the units whose
        diff GitHub would return (the top-N diff sample), as `ahead_by`

Usage:
    python paper/csea/out/export_drift_fig_data.py --db D:/Data/mcp-audit/mcp_audit.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from pathlib import Path

OUT = Path(__file__).parent / "fig7_data.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    outcomes = {"advanced": 0, "unchanged": 0, "no_longer_exists": 0, "timeout": 0}
    ahead = []
    for (data,) in con.execute(
            "SELECT data_json FROM findings WHERE finding_type='c1_reclone'"):
        try:
            d = json.loads(data) if data else {}
        except json.JSONDecodeError:
            continue
        verdict = str(d.get("verdict", "")).lower()
        err = str(d.get("error") or "").lower()
        if verdict == "changed":
            outcomes["advanced"] += 1
        elif verdict == "unchanged":
            outcomes["unchanged"] += 1
        elif "not found" in err:
            outcomes["no_longer_exists"] += 1
        elif "timeout" in err or "超时" in err:
            outcomes["timeout"] += 1
        diff = d.get("diff")
        if isinstance(diff, dict) and isinstance(diff.get("ahead_by"), int):
            ahead.append(diff["ahead_by"])

    total = sum(outcomes.values())
    payload = {
        "outcomes": outcomes,
        "total": total,
        "advanced_or_gone": outcomes["advanced"] + outcomes["no_longer_exists"],
        "ahead_by": sorted(ahead),
        "ahead_median": statistics.median(ahead) if ahead else None,
    }
    Path(a.out).write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"units {total} | advanced {outcomes['advanced']} | gone {outcomes['no_longer_exists']}")
    print(f"ahead_by n={len(ahead)} median={payload['ahead_median']}")
    print("wrote", a.out)


if __name__ == "__main__":
    main()
