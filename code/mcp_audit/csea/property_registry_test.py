"""Proof that a property is registered, not branched.

The claim in Section 4 is that adding a property means supplying a guard chain and a
comparison, and that the engine does not change. This test makes that checkable: it
registers a property the core has never heard of, runs it through the same engine,
and asserts that the core module contains no reference to it.

    python mcp_audit\\csea\\property_registry_test.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from paths import CODE
import csea_core as C

NAME = "ENDPOINT_REACHABLE"
failures = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def guard_reachable(v: C.View) -> None:
    if v.get("reachable") is False:
        raise C.Abstain("INSUFFICIENT", "CODE_UNAVAILABLE")


def compare_reachable(v: C.View):
    if v.get("reachable"):
        return "SUPPORTED", "FULL_MATCH", {"probe": v.get("probe")}
    return "UNSUPPORTED", "CODE_OPAQUE", {}


# --- register -----------------------------------------------------------------
print("registering a property the core does not know")
prop = C.register(C.Property(
    name=NAME,
    guards=(C.guard_identity, guard_reachable),
    compare=compare_reachable,
    evidence_types=("identity",),
))
check(NAME in C.PROPERTIES, "the property is in the registry")
check(NAME not in C.CLAIM_TYPES, "it is not part of the built-in vocabulary")

# --- run through the unmodified engine ----------------------------------------
print("running it through the engine")
ledger = C.Ledger()
eid = ledger.add(C.Evidence(type="identity", source="probe", content=("x", 1)))
claim = C.Claim("c1", "target", NAME, ("x",), "probe", "2026-09-21")

v_ok = C.adjudicate_property(prop, claim, C.View(identity_ambiguous=False,
                                                reachable=True, probe="200"),
                             [eid])
check(v_ok.status == "SUPPORTED", "supported when the probe succeeds")
check(v_ok.evidence_ids == [eid], "the verdict cites the evidence it used")
check(v_ok.next_check == "none", "a closed verdict carries no work item")

v_no = C.adjudicate_property(prop, claim, C.View(identity_ambiguous=False,
                                                reachable=False), [eid])
check(v_no.status == "INSUFFICIENT", "abstains when the probe cannot be made")

v_amb = C.adjudicate_property(prop, claim, C.View(identity_ambiguous=True,
                                                 reachable=True), [eid])
check(v_amb.status == "UNRESOLVED", "the shared identity guard still fires first")

# --- the engine was not touched ------------------------------------------------
src = (CODE / "csea_core.py").read_text(encoding="utf-8")
check(src.count(NAME) == 0, "csea_core.py contains no reference to the new property")
check(src.count("claim.property ==") == 0, "the engine has no per-property branch")

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    sys.exit(1)
print("PASS: a new property needs no engine change")
