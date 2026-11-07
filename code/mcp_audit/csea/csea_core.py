"""CSEA core: properties, evidence, the ordered adjudication, and the ledger.

Two things are structural in this revision.

*The rule is not specialised per property.* A property is a registered object that
supplies its own guard chain and its comparison; the engine walks whatever guards
the property declares and never inspects `claim.property`. Adding a property means
registering one, not editing the engine.

*Evidence is a first-class object with a stable identity.* Every verdict cites the
evidence it consumed by hash, and the ledger stores those records once. A verdict
that cannot be traced to evidence fails invariant I4 rather than going undetected.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from typing import Callable, Iterable, Iterator, Literal

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

Status = Literal[
    "SUPPORTED", "PARTIAL", "UNSUPPORTED", "CONFLICT",
    "STALE", "INSUFFICIENT", "UNRESOLVED",
]

DECLARABLE_CLASSES = frozenset({"env_access", "network_egress", "credential_paths"})
NON_DECLARABLE_CLASSES = frozenset({
    "unsafe_deserialize", "subprocess", "arbitrary_code",
    "fs_destructive", "db_access", "shell_exec",
})
ALL_CLASSES = DECLARABLE_CLASSES | NON_DECLARABLE_CLASSES

DECLARING_MANIFEST = "registry_server"

REASONS = {
    "FULL_MATCH": "declared set equals detected set over declarable classes",
    "DECLARATION_SUBSET": "some declarable detected classes are declared, not all",
    "UNDECLARED_CAPABILITY": "declarable capability present in code, absent from record",
    "GHOST_DECLARATION": "capability declared, not detected in code",
    "SOURCE_DISAGREEMENT": "two sources are mutually exclusive on one property",
    "UNPINNED_VERSION": "displayed version string unchanged while the revision moved",
    "CODE_UNAVAILABLE": "no clone was obtained for this listing",
    "CODE_GONE": "the indexed URL no longer resolves",
    "CODE_TOO_LARGE": "the repository exceeded the clone size cap",
    "CODE_OPAQUE": "a clone was obtained but no analysable surface was found",
    "NO_DECLARATION": "the record declares nothing comparable",
    "NO_DECLARING_CHANNEL": "every detected class lacks a declaring field in the schema",
    "CHANNEL_ABSENT": "the artefact's manifest kind carries no declaring field",
    "SEMANTIC_NOT_CHECKABLE": "no decision procedure exists for this property",
    "IDENTITY_AMBIGUOUS": "one name maps to several listings",
}

#: What would settle an open verdict. Carried per verdict so that an abstention is a
#: work item rather than a full stop.
NEXT_CHECK = {
    "UNDECLARED_CAPABILITY": "request the declaring fields for these classes",
    "DECLARATION_SUBSET": "request the declaring fields for the missing classes",
    "GHOST_DECLARATION": "inspect the implementation for the declared classes",
    "SOURCE_DISAGREEMENT": "confirm which source's value matches the artefact",
    "UNPINNED_VERSION": "re-issue the record with an immutable revision",
    "NO_DECLARING_CHANNEL": "extend the manifest schema, then re-audit",
    "CHANNEL_ABSENT": "publish the record in a format that carries the field",
    "NO_DECLARATION": "ask the publisher for any declaration at all",
    "CODE_OPAQUE": "review the entry point by hand",
    "CODE_GONE": "re-resolve the listing; the URL is dead",
    "CODE_UNAVAILABLE": "retry the clone",
    "CODE_TOO_LARGE": "scan a subdirectory or a shallow clone",
    "IDENTITY_AMBIGUOUS": "resolve the name to a single listing",
    "SEMANTIC_NOT_CHECKABLE": "define a decision procedure for the property",
    "FULL_MATCH": "none",
}


# --------------------------------------------------------------------------
# Evidence and the ledger
# --------------------------------------------------------------------------

def _digest(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(repr(p).encode("utf-8", "replace"))
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class Evidence:
    """One observation. The identity is derived, so the same observation exported
    twice yields the same id and the ledger stores it once."""
    type: str                      # identity | declaration | code | time | consensus
    source: str
    content: tuple
    revision: str | None = None
    timestamp: str | None = None
    evidence_id: str = ""

    def __post_init__(self):
        if not self.evidence_id:
            object.__setattr__(self, "evidence_id", _digest(
                self.type, self.source, self.revision, self.timestamp, self.content))

    def to_json(self) -> dict:
        return {"evidence_id": self.evidence_id, "type": self.type,
                "source": self.source, "revision": self.revision,
                "timestamp": self.timestamp, "content": list(self.content)}


class Ledger:
    """Process-local store of evidence, keyed by hash. Writes JSONL on demand."""

    def __init__(self) -> None:
        self._items: dict[str, Evidence] = {}

    def add(self, ev: Evidence) -> str:
        self._items.setdefault(ev.evidence_id, ev)
        return ev.evidence_id

    def has(self, eid: str) -> bool:
        return eid in self._items

    def get(self, eid: str) -> Evidence | None:
        return self._items.get(eid)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[Evidence]:
        return iter(self._items.values())

    def write(self, path) -> int:
        with open(path, "w", encoding="utf-8") as fh:
            for ev in self._items.values():
                fh.write(json.dumps(ev.to_json(), ensure_ascii=False) + "\n")
        return len(self._items)


# --------------------------------------------------------------------------
# Objects
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Claim:
    claim_id: str
    target: str
    property: str
    value: tuple
    source: str
    observed_at: str


@dataclass
class Verdict:
    claim_id: str
    status: Status
    reason: str
    evidence_ids: list[str]
    window: tuple[str | None, str | None]
    detail: dict = field(default_factory=dict)

    @property
    def next_check(self) -> str:
        return NEXT_CHECK.get(self.reason, "")

    def row(self) -> dict:
        d = asdict(self)
        d["evidence_ids"] = "|".join(self.evidence_ids)
        d["next_check"] = self.next_check
        d["window_from"], d["window_to"] = self.window
        d.pop("window", None)
        return d


class Abstain(Exception):
    def __init__(self, status: Status, reason: str, **detail):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------
# The property registry
# --------------------------------------------------------------------------

class View:
    """A bag of observations. Properties read the keys they need; the engine reads
    none of them, which is what keeps it property-agnostic."""

    def __init__(self, **kw):
        self._d = kw

    def get(self, k, default=None):
        return self._d.get(k, default)

    def __getitem__(self, k):
        return self._d[k]


@dataclass(frozen=True)
class Property:
    name: str
    guards: tuple[Callable[[View], None], ...]
    compare: Callable[[View], tuple[str, str, dict]]
    evidence_types: tuple[str, ...]

    def run(self, view: View) -> tuple[str, str, dict]:
        for g in self.guards:
            g(view)                      # each raises Abstain when unsatisfied
        return self.compare(view)


# -- shared guards ---------------------------------------------------------

def guard_identity(v: View) -> None:
    if v.get("identity_ambiguous"):
        raise Abstain("UNRESOLVED", "IDENTITY_AMBIGUOUS")


def guard_code_collected(v: View) -> None:
    st = v.get("clone_status", "missing")
    if st == "gone":
        raise Abstain("INSUFFICIENT", "CODE_GONE")
    if st in ("oversize", "too_large"):
        raise Abstain("INSUFFICIENT", "CODE_TOO_LARGE")
    if st != "ok":
        raise Abstain("INSUFFICIENT", "CODE_UNAVAILABLE")
    if not v.get("files_scanned"):
        raise Abstain("INSUFFICIENT", "CODE_OPAQUE")


def guard_declaration(v: View) -> None:
    if not v.get("has_declaration"):
        raise Abstain("INSUFFICIENT", "NO_DECLARATION")


def guard_conflict(v: View) -> None:
    if v.get("conflicting_sources"):
        raise Abstain("CONFLICT", "SOURCE_DISAGREEMENT")


def guard_declarable_complete(v: View) -> None:
    detected = v.get("detected") or set()
    if not (detected & DECLARABLE_CLASSES):
        if detected:
            raise Abstain("INSUFFICIENT", "NO_DECLARING_CHANNEL",
                          undecidable_classes=sorted(detected))
        raise Abstain("INSUFFICIENT", "CODE_OPAQUE")
    if not v.get("has_channel"):
        # The artefact never carried the field. Charging this to the publisher reads
        # a format choice as an omission, so the primary verdict is abstention;
        # "refute" is the sensitivity variant.
        status = "UNSUPPORTED" if v.get("channel_policy") == "refute" else "INSUFFICIENT"
        raise Abstain(status, "CHANNEL_ABSENT",
                      declarable=sorted(detected & DECLARABLE_CLASSES))


def guard_declaration_present_sound(v: View) -> None:
    if not (v.get("declared") or set()):
        raise Abstain("INSUFFICIENT", "NO_DECLARATION")


def guard_version_present(v: View) -> None:
    if not v.get("declared_version"):
        raise Abstain("INSUFFICIENT", "NO_DECLARATION")
    if not v.get("stored_sha") or not v.get("head_sha"):
        raise Abstain("INSUFFICIENT", "CODE_UNAVAILABLE")


# -- comparisons ----------------------------------------------------------

def compare_complete(v: View):
    detected = set(v.get("detected") or set()) & ALL_CLASSES
    declared = set(v.get("declared") or set()) & ALL_CLASSES
    missing = (detected & DECLARABLE_CLASSES) - declared
    if not missing:
        return "SUPPORTED", "FULL_MATCH", {}
    if declared & (detected & DECLARABLE_CLASSES):
        return "PARTIAL", "DECLARATION_SUBSET", {"missing": sorted(missing)}
    return "UNSUPPORTED", "UNDECLARED_CAPABILITY", {"missing": sorted(missing)}


def compare_sound(v: View):
    detected = set(v.get("detected") or set()) & ALL_CLASSES
    declared = set(v.get("declared") or set()) & ALL_CLASSES
    ghosts = declared - detected
    if not ghosts:
        return "SUPPORTED", "FULL_MATCH", {}
    if declared & detected:
        return "PARTIAL", "GHOST_DECLARATION", {"ghosts": sorted(ghosts)}
    return "UNSUPPORTED", "GHOST_DECLARATION", {"ghosts": sorted(ghosts)}


def compare_version(v: View):
    if v.get("stored_sha") == v.get("head_sha"):
        return "SUPPORTED", "FULL_MATCH", {"version_changed": v.get("version_changed")}
    return "STALE", "UNPINNED_VERSION", {
        "declared_version": v.get("declared_version"),
        "version_changed": v.get("version_changed")}


PROPERTIES: dict[str, Property] = {}


def register(prop: Property) -> Property:
    PROPERTIES[prop.name] = prop
    return prop


DECLARATION_COMPLETE = register(Property(
    name="DECLARATION_COMPLETE",
    guards=(guard_identity, guard_code_collected, guard_declaration,
            guard_declarable_complete, guard_conflict),
    compare=compare_complete,
    evidence_types=("identity", "code", "declaration"),
))

DECLARATION_SOUND = register(Property(
    name="DECLARATION_SOUND",
    guards=(guard_identity, guard_code_collected, guard_declaration_present_sound,
            guard_conflict),
    compare=compare_sound,
    evidence_types=("identity", "code", "declaration"),
))

VERSION_PIN = register(Property(
    name="VERSION_PIN",
    guards=(guard_identity, guard_version_present),
    compare=compare_version,
    evidence_types=("identity", "time"),
))

CLAIM_TYPES = {
    "DECLARATION_COMPLETE": "every declarable capability detected in code is declared",
    "DECLARATION_SOUND": "every capability declared is detected in code",
    "VERSION_PIN": "the displayed version string identifies the revision it names",
}


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------

def adjudicate_property(prop: Property, claim: Claim, view: View,
                        evidence_ids: Iterable[str],
                        window: tuple[str | None, str | None] = (None, None)) -> Verdict:
    ids = list(evidence_ids)
    try:
        status, reason, detail = prop.run(view)
        return Verdict(claim.claim_id, status, reason, ids, window, detail)
    except Abstain as a:
        return Verdict(claim.claim_id, a.status, a.reason, ids, window, a.detail)


def adjudicate(claim: Claim, evidence_ids: list[str], *,
               identity_ambiguous: bool = False,
               clone_status: str = "ok",
               files_scanned: int = 0,
               detected: Iterable[str] = (),
               declared: Iterable[str] = (),
               has_declaration: bool = True,
               has_channel: bool = True,
               conflicting_sources: bool = False,
               channel_policy: str = "abstain",
               window: tuple[str | None, str | None] = (None, None)) -> Verdict:
    """Builds a view and dispatches through the registry; no per-property branch."""
    prop = PROPERTIES.get(claim.property, DECLARATION_COMPLETE)
    view = View(identity_ambiguous=identity_ambiguous, clone_status=clone_status,
                files_scanned=files_scanned,
                detected=set(detected) & ALL_CLASSES,
                declared=set(declared) & ALL_CLASSES,
                has_declaration=has_declaration, has_channel=has_channel,
                conflicting_sources=conflicting_sources,
                channel_policy=channel_policy)
    return adjudicate_property(prop, claim, view, evidence_ids, window)


def adjudicate_version(claim: Claim, evidence_ids: list[str], *,
                       declared_version: str | None,
                       stored_sha: str | None,
                       head_sha: str | None,
                       probe_ok: bool = True,
                       version_changed: bool = False,
                       window: tuple[str | None, str | None] = (None, None)) -> Verdict:
    if not probe_ok:
        return Verdict(claim.claim_id, "INSUFFICIENT", "CODE_GONE",
                       list(evidence_ids), window)
    view = View(declared_version=declared_version, stored_sha=stored_sha,
                head_sha=head_sha, version_changed=version_changed,
                identity_ambiguous=False)
    return adjudicate_property(PROPERTIES["VERSION_PIN"], claim, view,
                               evidence_ids, window)


# --------------------------------------------------------------------------
# The decomposition
# --------------------------------------------------------------------------

def decompose(detected: Iterable[str], declared: Iterable[str],
              has_channel: bool) -> dict:
    """``|O| = |schema| + |artefact| + |behavioural|`` by construction."""
    detected = set(detected) & ALL_CLASSES
    declared = set(declared) & ALL_CLASSES
    omitted = detected - declared
    schema = omitted & NON_DECLARABLE_CLASSES
    rest = omitted & DECLARABLE_CLASSES
    artefact, behavioural = (set(), rest) if has_channel else (rest, set())
    assert len(omitted) == len(schema) + len(artefact) + len(behavioural)
    return {"n_detected": len(detected), "n_omitted": len(omitted),
            "n_schema": len(schema), "n_artefact": len(artefact),
            "n_behavioural": len(behavioural),
            "schema_classes": sorted(schema), "artefact_classes": sorted(artefact),
            "behavioural_classes": sorted(behavioural)}


def shares(totals: dict) -> dict:
    denom = totals.get("n_omitted", 0)
    if not denom:
        return {}
    s, a, b = totals["n_schema"], totals["n_artefact"], totals["n_behavioural"]
    return {"schema_share": s / denom, "artefact_share": a / denom,
            "behavioural_share": b / denom, "S_schema": s / denom,
            "S_structural": (s + a) / denom}


# --------------------------------------------------------------------------
# Record level
# --------------------------------------------------------------------------

DECL_PRECEDENCE = ("UNRESOLVED", "INSUFFICIENT", "CONFLICT", "UNSUPPORTED",
                   "PARTIAL", "SUPPORTED")
CURRENCY = ("FRESH", "STALE", "UNKNOWN")


def record_verdict(by_claim: dict[str, str]) -> str:
    for s in DECL_PRECEDENCE:
        if s in by_claim.values():
            return s
    return "INSUFFICIENT"


def currency_of(version_pin: str | None) -> str:
    if version_pin is None:
        return "UNKNOWN"
    return {"STALE": "STALE", "SUPPORTED": "FRESH"}.get(version_pin, "UNKNOWN")
