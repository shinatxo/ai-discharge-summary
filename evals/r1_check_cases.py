#!/usr/bin/env python3
"""Item 3 R1 step 4b — the case-list validator (the part-3 validation design Rounds 1-2 and Codex
12 Nov 18:58, adopted by Shina (author/CSO) 12 Nov 2026 ~19:00; the readings of part A Rounds 1-2
and Codex 13 Nov 07:05, adopted by Shina 13 Nov 2026 ~07:08).

Proves, BEFORE the freeze and without rendering anything of the 90, that a case list is well-formed
and fits the frozen input — so the one run cannot STOP, crash or be REFUSED because of the list:

    python -B evals/r1_check_cases.py <cases.json> --batch <the source batch> --inputs evals/r1/inputs.json
                                      --located <cases-located.json> --reading-sheet <CASE-READING.md>
                                      [--expect-sha256 <hash>]

Exit 0 = well-formed · 1 = problems (each named: an id + a fixed code) · 2 = refused (it cannot
vouch for anything: a file missing or unreadable, the list not a JSON object, not the expected
hash, a pass file that is not the pass's, inputs that are not the pinned ones or do not match the
batch). It prints the sha256 of the list it checked, counts, ids and codes — never wording.
A field of the wrong type is a named problem before anything reads it, and an error the tool did not
foresee is a named problem too (exit 1) — never a pass and never a traceback (Codex 13 Nov 2026 07:53).

Order: the list's sha256 -> the identities of the pass's two files (both mandatory) -> the inputs
file and the batch -> the fourteen checks and the obligations table -> each synthetic control
screened as invented and only then rendered by the evaluator's own `synthetic_case()`.

The identities are the constant PRODUCTION. THE COMMAND LINE CANNOT CHANGE THEM: there is no
option, environment variable or file for it. `check()` and `main()` take an identity only as a
Python argument (tests pass an invented fixture's own hashes; the identity checks still run).

What it reads of the 90, each time it runs (an input-side read, to be recorded): the records'
bytes once, through the harness's own reader, and from each record ONLY
`patient_input(steps.compose_clinical)` — step 7's A-only input. It hashes lines, tests the list's
own short patterns against a case's own unit, and compares the three invented controls' word runs
with the inputs in memory. It opens nothing of step 7's or 8b's output.

What it never does: it never calls `render_patient.split_lines` (the renderer's first stage, which
refuses forms) and never renders a line of the 90. Units come from the locator's `units()`.
The only rendering is of the invented controls.

What it cannot prove: that an expectation is right (only that it is well-formed and fits the
input); that the list is complete; anything that needs a rendered batch. A well-formed case can
correctly be NOT MET on the run — validity is not the measured outcome.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))

import make_patient_sheets as MS  # noqa: E402
import r1_freeze as FZ  # noqa: E402
import r1_go_stop as GS  # noqa: E402
import r1_locate_cases as LC  # noqa: E402
import render_offline as H  # noqa: E402
import score_patient as SP  # noqa: E402
from pipeline import compose_patient as CP7  # noqa: E402
from pipeline import render_patient as RP  # noqa: E402
from pipeline.errors import StepError  # noqa: E402

CHECKER_VERSION = 1
SCENARIO_DIR = ROOT / "evals" / "scenarios"
RUN_WORDS, SHEET_WORDS = 5, 4            # a shared run: five words (controls), four (the reading sheet)
MAX_WORDS = 3                            # the pattern rule (2f): at most three words per alternative


# =================================================================================================
# identities — the one locating pass (12 Nov 2026 16:03) and the pinned inputs
# =================================================================================================

@dataclass(frozen=True)
class Identity:
    inputs_sha256: str
    located_sha256: str
    reading_sheet_sha256: str
    locator_sha256: str


PRODUCTION = Identity(
    inputs_sha256=FZ.MEASUREMENT_INPUTS_SHA256,
    located_sha256="b502a496e0ff2937ecee57496d77c36989139521e10820c8890b87874423a42f",
    reading_sheet_sha256="fc22d7309a7ecba728f4b454436c2b73cb4df6a6e3d94b2021f913a8dd414279",
    locator_sha256="a4df06525836cd53ebae1cde9757c0872d6ddf441380bcbad043826a2e093dd5",
)


class Refused(Exception):
    """The tool cannot vouch for anything (exit 2). `code` is a fixed code — no wording."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# =================================================================================================
# the adopted obligations (PART3-R2-fixes.md, Fix 1) — written once; NOT derived from a list
# =================================================================================================

WWD = ("key_investigations", "treatment")                 # A headings behind C's what_we_did
WIH = ("presenting_complaint", "diagnosis")               # ... why_in_hospital
NXT = ("follow_up", "gp_actions")                         # ... next_steps
# The two fixed phrases the pattern rule allows by exact text (CASES-REVIEW-R2.md §9, 4): the adopted
# inferred wording — built from the renderer's own constant, not typed again — and its first five words.
INFERRED_EXACT = r"\(" + RP.ADOPTED_INFERRED[1:-1] + r"\)"
INFERRED_HEAD = " ".join(RP.ADOPTED_INFERRED[1:].split()[:5])
FIXED_PHRASES = (INFERRED_EXACT, INFERRED_HEAD)


def _add(token, a_headings=None, *, not_from=None, copy_only=False) -> dict:
    d = {"type": "ADD", "token": token}
    if not_from:
        d["not_from"] = not_from
    if copy_only:
        d["copy_only"] = True
    if a_headings:
        d["a_headings"] = list(a_headings)
    return d


def _keep(term, a_headings) -> dict:
    return {"type": "KEEP", "term": term, "a_headings": list(a_headings)}


def _ruled(kind, *c_headings) -> dict:
    return {"type": "RULED", "kinds": [kind], "c_headings": list(c_headings)}


def _blind(item) -> dict:
    return {"type": "BLIND", "item": item, "dimension": "audience"}


_DIAG, _WITHHELD = {"type": "DIAG"}, {"type": "WITHHELD"}
_TO_HELP = (_add(r"\bto help\b", WWD, copy_only=True), _ruled("cause", "what_we_did"))
_CAUS_WIH = (_add(r"\bcaus\w*", WIH, copy_only=True), _ruled("cause", "why_in_hospital"))

# (known-failure number, scenario, its adopted checks) — 28 entries, 58 checks (design §1.5 as made
# concrete in part 2; adopted by Shina 12 Nov 2026 ~18:26).
KNOWN_FAILURES = (
    (1, "S1", (_add("camera test", WWD, not_from="angiogra"), _keep(r"\bangiogra\w*", WWD),
               _ruled("test_term", "what_we_did"))),
    (2, "S4", (_add(r"\bswabs?\b", copy_only=True), _keep(r"\bNPA\b", WWD), _keep(r"\bnasopharyngeal\b", WWD),
               _ruled("test_term", "what_we_did", "why_in_hospital"))),
    (3, "S12", (_add(r"\bscans?\b", WIH, not_from=r"AXR|CXR|X-?ray"), _ruled("test_term", "why_in_hospital"))),
    (4, "S10", _TO_HELP), (4, "S1", _TO_HELP), (4, "S14", _TO_HELP),
    (5, "S1", (_add(r"\bbecause\b", NXT, copy_only=True), _ruled("cause", "next_steps"))),
    (5, "S10", (_add(r"\bbecause\b", WWD, copy_only=True), _ruled("cause", "what_we_did"))),
    (6, "S16", _CAUS_WIH),
    (7, "S2", (_add(r"\bto keep\b", WWD, copy_only=True), _ruled("cause", "what_we_did"))),
    (7, "S4", _CAUS_WIH), (7, "S18", _CAUS_WIH),
    (8, "S11", (_add(r"\bclosely\b", WWD, copy_only=True), _ruled("certainty", "what_we_did"))),
    (8, "S3", (_add(r"\bcarefully\b", WWD, copy_only=True), _ruled("certainty", "what_we_did"))),
    (9, "S13", (_keep(r"\bimprov\w*", WWD), _ruled("qualifier", "what_we_did"))),
    (10, "S3", (_DIAG, _ruled("diagnosis", "why_in_hospital"))),
    (11, "S16", (_add(r"\bhome\b", NXT, not_from="communit"), _add(r"\bvisit\w*", NXT, copy_only=True),
                 _keep(r"\bcommunity\b", NXT), _ruled("promise", "next_steps"))),
    (12, "S1", (_add(r"\badjust\w*", NXT, copy_only=True), _keep(r"\buptitrat\w*", NXT))),
    (13, "S1", (_DIAG,)),
    (14, "S13", (_add(r"\bon your own\b", WWD, copy_only=True), _keep(r"\bextubat\w*", WWD),
                 _keep(r"\bday \d+\b", WWD))),
    (15, "S16", (_add(r"\bdamage\w*", WWD, copy_only=True), _keep(r"\bischaemi\w*", WWD))),
    (16, "A5", (_add(r"\binfect\w*", WWD, not_from=r"CRP|WCC"), _keep(r"\bCRP\b", WWD), _keep(r"\bWCC\b", WWD))),
    (17, "S3", (_keep(r"\bdose\b", NXT), _keep(r"\breview\w*", NXT), _keep(r"\bsertraline\b", NXT))),
    (18, "S18", (_WITHHELD,)),
    (19, "A5", (_blind("A5"),)), (19, "S10", (_blind("S10"),)), (19, "S13", (_blind("S13"),)),
    (19, "S3", (_blind("S3"),)),
)


@dataclass(frozen=True)
class Obligations:
    """What an adopted list must hold. Tests may pass a smaller table; the command always uses OBLIGATIONS."""
    known_failures: tuple = KNOWN_FAILURES
    runs: tuple = (1, 2, 3, 4, 5)                                  # D2: all five runs
    u1: dict | None = field(default_factory=lambda: {              # S1 "2 weeks", unchanged
        "scenario": "S1", "heading": "presenting_complaint",
        "must_keep": (r"\b2\b", r"\bweeks\b"), "c_must_match": (r"\b2 weeks\b",)})
    u2: dict | None = field(default_factory=lambda: {              # B6 r4: rendered, no conflict wording
        "record": "r4/B6.json", "c_must_not_match": r"notes\s+conflict"})
    kf18: dict | None = field(default_factory=lambda: {            # carried in code (part 2 Round 2)
        "scenario": "S18", "source_id": "sections.3", "line": 1, "sentence": 1,
        "must_keep": r"\bno\b", "c_must_not_match": r"(?i)\brisk\b"})
    omissions: tuple = (("A5", "diagnosis", "withheld", "w_curb65"),)        # D3' boundary: on every run
    inferred: tuple = (("r2/S14.json", "sections.3", 5, None),               # D3' boundary: the two located
                       ("r4/S14.json", "sections.3", 5, None))               # inferred-wording cases
    synthetic: tuple = (("SX1", "refusal", "conflict_in_input"), ("SX2", "refusal", "withheld_not_isolable"),
                        ("SX3", "retained", None))


OBLIGATIONS = Obligations()

# --- the schema ------------------------------------------------------------------------------------
PARTS = ("located", "synthetic", "known_failures", "generations")
TOP_KEYS = ("cases_version", "inputs_sha256", "provenance") + PARTS
LOCATED_KEYS = ("case_id", "category", "record", "source_id", "heading", "line", "sentence", "line_sha256",
                "expected")
PASS_KEYS = LOCATED_KEYS[:-1]                                      # what check 14 compares with the pass
SYNTHETIC_KEYS = ("case_id", "label", "input", "at", "expected")
SYNTHETIC_NOTES = ("shape",)
KF_KEYS = ("kf_id", "scenario_id", "runs", "checks")
KF_NOTES = ("source",)
GENERATION_KEYS = ("case_id", "record", "expected")
GENERATION_OPTIONAL = ("c_must_not_match", "rule")
EXPECTED_NOTES = ("rule", "predicted")
C_PATTERNS = ("c_must_match", "c_must_not_match")
PROSE_PATTERNS = ("prose_must_match", "prose_must_not_match")       # a synthetic control only
OUTCOME_KEYS = {"retained": ("no_drop", "must_keep") + C_PATTERNS,
                "approved_omission": ("drop", "drops", "whole", "must_keep") + C_PATTERNS,
                "refusal": ("code",)}
SYNTHETIC_LABEL = "SYNTHETIC"
KF_ID = re.compile(r"KF-(\d\d)(?:-([A-Za-z0-9]+))?")
CHECK_FIELDS = {          # type -> (required, optional)
    "ADD": (("token",), ("not_from", "copy_only", "a_headings")),
    "KEEP": (("term",), ("a_headings",)),
    "DIAG": ((), ()), "WITHHELD": ((), ()),
    "RULED": (("kinds", "c_headings"), ()),
    "BLIND": (("item", "dimension"), ()),
}
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}")


def sha_text(text: str) -> str:
    return GS.sha_bytes(text.encode("utf-8"))


# =================================================================================================
# the pattern rule (check 7; part A Round 2 Fix 2; Codex 13 Nov 07:05)
# =================================================================================================

def _pieces(pattern: str) -> list[str] | None:
    """The pattern as pieces of the flat grammar, or None if it is outside it. Escapes are read FIRST,
    so an escaped `|` is a sign, never an alternation. Pieces: "w" part of a word · "s" a space or a
    hyphen · "e" an escaped sign · "b" a word edge · "|" an alternation · "q" a quantifier."""
    body = pattern[4:] if pattern.startswith("(?i)") else pattern
    out, i = [], 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            nxt = body[i + 1:i + 2]
            if nxt == "b":
                out.append("b")
            elif nxt in ("w", "d"):
                out.append("w")
            elif nxt == "s":
                out.append("s")
            elif not nxt or nxt.isalnum() or nxt == "_":
                return None                                  # \B \A \Z \1 \n ... — not in the grammar
            else:
                out.append("e")                              # an escaped sign: \? \( \. \| ...
            i += 2
            continue
        if ch == "[":
            j = body.find("]", i + 1)
            inner = body[i + 1:j] if j > 0 else ""
            if not inner or not all(c.isascii() and c.isalnum() for c in inner):
                return None                                  # no range, no ^, no escape inside a class
            out.append("w")
            i = j + 1
            continue
        if ch == "|":
            out.append("|")
        elif ch in "?*+":
            if not out or out[-1] not in ("w", "s", "e"):
                return None                                  # a quantifier only straight after one piece
            out.append("q")
        elif ch.isalnum():
            out.append("w")
        elif ch in " -":
            out.append("s")
        else:
            return None                                      # ( ) . ^ $ { } and any other bare sign
        i += 1
    return out


def pattern_problem(pattern) -> str | None:
    """None, or the fixed code of what is wrong with one pattern."""
    if not isinstance(pattern, str) or not pattern:
        return "pattern:empty"
    try:
        rx = re.compile(pattern)
    except re.error:
        return "pattern:does_not_compile"
    if pattern in FIXED_PHRASES:
        return None
    pieces = _pieces(pattern)
    if pieces is None:
        return "pattern:unsupported_form"
    alternatives, current = [], []
    for p in pieces + ["|"]:
        if p == "|":
            alternatives.append(current)
            current = []
        else:
            current.append(p)
    for alt in alternatives:
        if not any(p in ("w", "e") for p in alt):
            return "pattern:unsupported_form"                # an alternative with no word and no sign
        n_words, inside = 0, False                           # (empty, or only edges / spaces) checks nothing
        for p in alt:
            if p == "w":
                n_words += 0 if inside else 1
                inside = True
            elif p in ("s", "e"):
                inside = False
        if n_words > MAX_WORDS:
            return "pattern:too_long"
    if rx.search("") is not None:
        return "pattern:empty"                               # it matches nothing-at-all: it checks nothing
    return None


# =================================================================================================
# words and runs (checks 10 and 13; part A Round 2 Fix 3)
# =================================================================================================

def words(text: str) -> list[str]:
    """The renderer's own WORD pattern, lower-cased — punctuation, "- " markers and line breaks are not words."""
    return [m.group(0).lower() for m in RP.WORD.finditer(text)]


def runs(tokens: list[str], n: int) -> set[tuple]:
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def _strings(obj):
    """Every string in a JSON value — dictionary KEYS as well as values (Codex 13 Nov 07:53)."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


# =================================================================================================
# the report
# =================================================================================================

class Report:
    def __init__(self, sha256: str):
        self.sha256 = sha256
        self.problems: list[tuple[str, str]] = []
        self.notes: list[tuple[str, str]] = []
        self.counts: dict = {}
        self._seen: set = set()

    def add(self, where: str, code: str) -> None:
        if ("problem", where, code) not in self._seen:
            self._seen.add(("problem", where, code))
            self.problems.append((where, code))

    def note(self, where: str, code: str) -> None:
        if ("note", where, code) not in self._seen:
            self._seen.add(("note", where, code))
            self.notes.append((where, code))

    def codes(self, where: str | None = None) -> list[str]:
        return [c for w, c in self.problems if where is None or w == where]

    @property
    def exit_code(self) -> int:
        return 1 if self.problems else 0


def _tag(part: str, i: int, obj, key: str = "case_id") -> str:
    """A printable name for an entry: its id when that is a plain token, else its position (never wording)."""
    v = obj.get(key) if isinstance(obj, dict) else None
    return v if isinstance(v, str) and _SAFE_ID.fullmatch(v) else f"{part}[{i}]"


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _str_list(v) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _in(v, known) -> bool:
    """`v in known` for a value that came from the list: only a string can be a member (a list or an
    object there is never looked up, so it can never raise)."""
    return isinstance(v, str) and v in known


def _notes(rep: "Report", where: str, obj: dict, keys) -> None:
    for k in keys:
        if k in obj and not isinstance(obj[k], str):
            rep.add(where, f"schema:type:{k}")                       # a note is one string, never a structure


# =================================================================================================
# the 90's step-7 inputs, read once (checks 2, 4, 8 and the control screen)
# =================================================================================================

class Inputs:
    """The batch read ONCE through the harness's own reader. From each record only step 7's A-only input."""

    def __init__(self, batch: Path, inputs_path: Path, identity: Identity):
        try:
            ib = inputs_path.read_bytes()
        except OSError:
            raise Refused("inputs:not_readable") from None
        if GS.sha_bytes(ib) != identity.inputs_sha256:
            raise Refused("inputs:identity_mismatch")
        try:
            manifest, _index, mb, bb, rels, rbs = H._read_source(batch.resolve())
            derived = H._inputs_from(manifest, mb, bb, rels, rbs)
            listed = json.loads(ib)
        except (H.HarnessError, OSError, ValueError, KeyError, TypeError):
            raise Refused("batch:not_readable") from None
        if listed != derived:
            raise Refused("inputs:do_not_match_the_batch")
        self.records = len(rels)
        self.rels = set(rels)
        self.scenarios: set = set()
        self.by_rel: dict = {}
        self.line_hashes: set = set()                    # every non-blank physical line and every unit, by hash
        self.runs: set = set()                           # every run of RUN_WORDS words, per section
        for rel, rb in zip(rels, rbs):
            rec = json.loads(rb)
            self.scenarios.add(rec.get("scenario_id"))
            try:
                inp = GS.step7_input(rec)                # the ONLY part of a record that is read
            except StepError:
                inp = None
            self.by_rel[rel] = inp
            for sec in (inp or {}).get("sections", []):
                self.runs |= runs(words(sec["text"]), RUN_WORDS)
                for _n, _k, line, text in LC.units(sec):
                    self.line_hashes.add(sha_text(line))
                    self.line_hashes.add(sha_text(text))

    def section(self, rel, source_id):
        inp = self.by_rel.get(rel)
        return next((s for s in (inp or {}).get("sections", []) if s["section_id"] == source_id), None)


def location_problem(case: dict, inputs: Inputs) -> str | None:
    """The predicate the evaluator's `located_case()` refuses on, stated here because that function needs
    a rendered batch: the record, the section (by id), the physical line and the line's sha256."""
    rel = case.get("record")
    if not _in(rel, inputs.rels):
        return "located:record_not_in_the_inputs"
    if inputs.by_rel.get(rel) is None:
        return "located:no_step7_input"
    sec = inputs.section(rel, case.get("source_id"))
    if sec is None:
        return "located:no_such_section"
    phys = sec["text"].split("\n")
    n = case.get("line")
    if not isinstance(n, int) or not 1 <= n <= len(phys):
        return "located:line_out_of_range"
    if GS.sha_bytes(phys[n - 1].rstrip().encode("utf-8")) != case.get("line_sha256"):
        return "located:line_sha256_mismatch"
    return None


def unit_text(section: dict, line, sentence) -> str | None:
    """The text of one unit as the evaluator will test it: the locator's `units()` numbering, without
    the "- " marker of an item (the renderer's unit text excludes it). None if there is no such unit."""
    for n, k, _line, text in LC.units(section):
        if (n, k) == (line, sentence):
            return text[2:] if k is None and text.startswith("- ") else text
    return None


# =================================================================================================
# the checks on the list alone (1, 3, 5, 6, 7, 9, 11 and the obligations)
# =================================================================================================

def _patterns(rep: Report, where: str, holder: dict, keys, *, one: bool = False) -> None:
    """Check 7 on the pattern fields `keys`. `one`: the field holds ONE pattern (a known failure's token,
    term, not_from) — a list there is not a pattern (the evaluator would hand it to `re` and crash)."""
    for k in keys:
        if k not in holder:
            continue
        v = holder[k]
        for i, p in enumerate([v] if one or not isinstance(v, list) else v):
            code = pattern_problem(p)
            if code:
                rep.add(where, f"{k}[{i}]:{code}")
        if not one and isinstance(v, list) and not _str_list(v):
            rep.add(where, f"schema:type:{k}")


def _expected(rep: Report, where: str, exp, tables: RP.Tables, synthetic: bool) -> None:
    """Checks 5, 6, 7 and 9 on one located case or synthetic control."""
    if not isinstance(exp, dict):
        rep.add(where, "schema:not_an_object")
        return
    outcome = exp.get("outcome")
    if not _in(outcome, GS.OUTCOMES):
        rep.add(where, "outcome:not_recognised")
        return
    _notes(rep, where, exp, EXPECTED_NOTES)
    allowed = OUTCOME_KEYS[outcome] + EXPECTED_NOTES + ("outcome",)
    if synthetic and outcome != "refusal":
        allowed += PROSE_PATTERNS
    for k in exp:
        if k not in allowed:
            rep.add(where, f"outcome:key_not_allowed:{k}" if _SAFE_ID.fullmatch(str(k)) else "outcome:key_not_allowed")
    lists = [k for k in ("must_keep",) + C_PATTERNS + PROSE_PATTERNS if k in exp]
    for k in lists:
        if not _str_list(exp[k]):
            rep.add(where, f"schema:type:{k}")
    for k in ("no_drop", "whole"):
        if k in exp and not isinstance(exp[k], bool):
            rep.add(where, f"schema:type:{k}")
    _patterns(rep, where, exp, [k for k in lists if _str_list(exp[k])])
    if outcome == "refusal":
        if not _in(exp.get("code"), RP.REFUSALS):
            rep.add(where, "outcome:refusal_code")
    elif outcome == "approved_omission":
        named = ([exp["drop"]] if "drop" in exp else []) + (exp["drops"] if isinstance(exp.get("drops"), list) else [])
        if "drops" in exp and not isinstance(exp["drops"], list):
            rep.add(where, "schema:type:drops")
        if not named:
            rep.add(where, "omission:no_drop_named")
        for d in named:
            if not isinstance(d, dict) or set(d) - {"rule", "entry"}:
                rep.add(where, "schema:type:drop")
                continue
            rule, entry = d.get("rule"), d.get("entry")
            if not _in(rule, RP.DROP_RULES):
                rep.add(where, "omission:rule")
            elif rule == "resus_conflict":
                if entry is not None:
                    rep.add(where, "omission:entry")
            else:
                ids = {e.id for e in RP._table_entries(tables, rule)}       # `withheld` as loaded already holds
                if not isinstance(entry, str) or entry not in ids:         # the built-in entry RP.RESUS_ENTRY
                    rep.add(where, "omission:entry")
    else:                                                            # retained
        checks = any(exp.get(k) for k in ("must_keep",) + C_PATTERNS + (PROSE_PATTERNS if synthetic else ()))
        if not checks and exp.get("no_drop") is not True:
            rep.add(where, "case:checks_nothing")                    # check 9


def _keys(rep: Report, where: str, obj: dict, required, optional=()) -> bool:
    ok = True
    for k in required:
        if k not in obj:
            rep.add(where, f"schema:key_missing:{k}")
            ok = False
    for k in obj:
        if k not in required and k not in optional:
            rep.add(where, "schema:unknown_key")
    return ok


def _check_shape(c: dict) -> tuple:
    """Every meaningful parameter of a known-failure check (Codex 12 Nov 18:58), in a comparable form."""
    def fs(v):
        return frozenset(v) if isinstance(v, list) and all(isinstance(x, str) for x in v) else None
    return (c.get("type"), c.get("token"), c.get("term"), c.get("not_from"), bool(c.get("copy_only")),
            fs(c.get("a_headings")), fs(c.get("kinds")), fs(c.get("c_headings")), c.get("item"), c.get("dimension"))


def _known_failure(rep: Report, where: str, kf: dict) -> None:
    """Check 11 on one entry (without the batch) and the per-entry obligations."""
    if not _keys(rep, where, kf, KF_KEYS, KF_NOTES):
        return
    _notes(rep, where, kf, KF_NOTES)
    m = KF_ID.fullmatch(kf["kf_id"]) if isinstance(kf["kf_id"], str) else None
    sid = kf["scenario_id"]
    if m is None or not isinstance(sid, str) or (m.group(2) and m.group(2) != sid):
        rep.add(where, "kf:id_form")
    runs_ = kf["runs"]
    if runs_ != "all":
        rep.add(where, "obligation:runs_not_all")                    # D2: all five runs, on every entry
        if not (isinstance(runs_, list) and runs_ and all(_is_int(r) and r >= 1 for r in runs_)):
            rep.add(where, "kf:runs_invalid")
    checks = kf["checks"]
    if not isinstance(checks, list) or not checks:
        rep.add(where, "kf:no_checks")
        return
    for i, c in enumerate(checks):
        t = c.get("type") if isinstance(c, dict) else None
        if not _in(t, CHECK_FIELDS):
            rep.add(where, f"kf:check_type[{i}]")
            continue
        required, optional = CHECK_FIELDS[t]
        fields_ok = all(k in c for k in required) and all(k == "type" or k in required + optional for k in c)
        fields_ok &= all(isinstance(c[k], str) for k in ("token", "term", "not_from", "item", "dimension") if k in c)
        if t == "ADD":
            fields_ok &= isinstance(c.get("copy_only", False), bool)
            fields_ok &= not (c.get("copy_only") and "not_from" in c)       # copy_only makes not_from unread
            _patterns(rep, where, c, ("token", "not_from"), one=True)
        if t == "KEEP":
            _patterns(rep, where, c, ("term",), one=True)
        if t in ("ADD", "KEEP") and "a_headings" in c:
            fields_ok &= _str_list(c["a_headings"]) and bool(c["a_headings"]) \
                and set(c["a_headings"]) <= set(CP7.GIVEN_A_HEADINGS)
        if t == "RULED" and fields_ok:
            fields_ok &= _str_list(c["kinds"]) and bool(c["kinds"]) and set(c["kinds"]) <= set(SP.REVIEW_KINDS)
            fields_ok &= _str_list(c["c_headings"]) and bool(c["c_headings"]) \
                and set(c["c_headings"]) <= set(RP.PROSE_C)
        if t == "BLIND" and fields_ok:
            if not _in(c["item"], MS.ITEMS):
                rep.add(where, "obligation:blind_item_not_an_item")
            elif c["item"] != sid:
                rep.add(where, "obligation:blind_item_mismatch")
            if not _in(c["dimension"], MS.DIMENSIONS):
                rep.add(where, f"kf:blind_dimension[{i}]")
        if not fields_ok:
            rep.add(where, f"kf:check_fields[{i}]")


def _kf_obligations(rep: Report, entries: list, ob: Obligations) -> None:
    by_key: dict = {}
    for i, kf in enumerate(entries):
        if not isinstance(kf, dict):
            continue
        m = KF_ID.fullmatch(kf.get("kf_id")) if isinstance(kf.get("kf_id"), str) else None
        if m is not None and isinstance(kf.get("scenario_id"), str):
            where = _tag("known_failures", i, kf, "kf_id")
            by_key.setdefault((int(m.group(1)), kf["scenario_id"]), []).append((where, kf))
    wanted = {(n, sid): checks for n, sid, checks in ob.known_failures}
    for (n, sid), checks in wanted.items():
        name = f"KF-{n:02d}/{sid}"
        mine = by_key.get((n, sid), [])
        if not mine:
            rep.add(name, "obligation:kf_entry_missing")
            continue
        if len(mine) > 1:
            rep.add(name, "obligation:kf_entry_duplicated")
            continue
        where, kf = mine[0]
        have = [_check_shape(c) for c in (kf["checks"] if isinstance(kf.get("checks"), list) else [])
                if isinstance(c, dict)]
        need = [_check_shape(c) for c in checks]
        for i, shape in enumerate(need):
            if shape not in have:
                rep.add(where, f"obligation:kf_check_missing[{i}]")
        if any(shape not in need for shape in have):
            rep.note(where, "note:kf_check_extra")                   # stricter, never looser
    for key, mine in by_key.items():
        if key not in wanted:
            for where, _kf in mine:
                rep.note(where, "note:kf_entry_extra")


def _drops(exp: dict) -> set:
    """The (rule, entry) pairs an approved omission names, as the evaluator reads them (`drop` and `drops`)."""
    more = exp.get("drops") if isinstance(exp.get("drops"), list) else []
    return {(d["rule"], d.get("entry")) for d in [exp.get("drop")] + more
            if isinstance(d, dict) and isinstance(d.get("rule"), str)
            and (d.get("entry") is None or isinstance(d.get("entry"), str))}


def _other_obligations(rep: Report, cases: dict, ob: Obligations) -> None:
    located = [c for c in cases.get("located") or [] if isinstance(c, dict) and isinstance(c.get("expected"), dict)]

    def has(exp, key, pattern):
        return isinstance(exp.get(key), list) and pattern in exp[key]

    if ob.u1:
        for n in ob.runs:
            if not any(c.get("category") == "U1" and c.get("record") == f"r{n}/{ob.u1['scenario']}.json"
                       and c.get("heading") == ob.u1["heading"] and c["expected"].get("outcome") == "retained"
                       and all(has(c["expected"], "must_keep", p) for p in ob.u1["must_keep"])
                       and all(has(c["expected"], "c_must_match", p) for p in ob.u1["c_must_match"])
                       for c in located):
                rep.add(f"U1-r{n}", "obligation:u1_missing")
    if ob.u2:
        if not any(isinstance(g, dict) and g.get("record") == ob.u2["record"] and g.get("expected") == "rendered"
                   and has(g, "c_must_not_match", ob.u2["c_must_not_match"]) for g in cases.get("generations") or []):
            rep.add("U2", "obligation:u2_missing")
    if ob.kf18:
        k = ob.kf18
        for n in ob.runs:
            mine = [c["expected"] for c in located
                    if (c.get("record"), c.get("source_id"), c.get("line"), c.get("sentence"))
                    == (f"r{n}/{k['scenario']}.json", k["source_id"], k["line"], k["sentence"])
                    and c["expected"].get("outcome") == "retained" and has(c["expected"], "must_keep", k["must_keep"])
                    and not c["expected"].get("no_drop")]            # not the adopted case otherwise (C4)
            if not mine:
                rep.add(f"KF-18-r{n}", "obligation:kf18_case_missing")
            elif not any(has(e, "c_must_not_match", k["c_must_not_match"]) for e in mine):
                rep.add(f"KF-18-r{n}", "obligation:kf18_pattern_missing")
    for sid, heading, rule, entry in ob.omissions:
        for n in ob.runs:
            if not any(c.get("record") == f"r{n}/{sid}.json" and c.get("heading") == heading
                       and c["expected"].get("outcome") == "approved_omission"
                       and (rule, entry) in _drops(c["expected"]) for c in located):
                rep.add(f"{sid}-r{n}", "obligation:boundary_case_missing")
    for rel, source_id, line, sentence in ob.inferred:
        at = (rel, source_id, line, sentence)
        if not any((c.get("record"), c.get("source_id"), c.get("line"), c.get("sentence")) == at
                   and c["expected"].get("outcome") == "retained" and has(c["expected"], "c_must_match", INFERRED_EXACT)
                   for c in located):
            rep.add(rel, "obligation:boundary_case_missing")
    synthetic = {s["case_id"]: s["expected"] for s in cases.get("synthetic") or []
                 if isinstance(s, dict) and isinstance(s.get("case_id"), str) and isinstance(s.get("expected"), dict)}
    for cid, outcome, code in ob.synthetic:
        exp = synthetic.get(cid)
        if exp is None or exp.get("outcome") != outcome or (code is not None and exp.get("code") != code):
            rep.add(cid, "obligation:synthetic_missing")


def list_checks(rep: Report, cases: dict, tables: RP.Tables, ob: Obligations) -> None:
    """Checks 1, 3, 5, 6, 7, 9, 11 (the parts that need no batch) and the obligations table: the list alone."""
    for k in cases:
        if k not in TOP_KEYS:
            rep.add("list", "schema:unknown_key")
    if cases.get("cases_version") != GS.CASES_VERSION or isinstance(cases.get("cases_version"), bool):
        rep.add("list", "schema:version")
    seen: dict = {}
    for part in PARTS:
        entries = cases.get(part)
        if not isinstance(entries, list):
            rep.add(part, "schema:part_not_a_list")
            cases[part] = []
            continue
        id_key = "kf_id" if part == "known_failures" else "case_id"
        for i, e in enumerate(entries):
            where = _tag(part, i, e, id_key)
            if not isinstance(e, dict):
                rep.add(where, "schema:not_an_object")
                continue
            cid = e.get(id_key)
            if not isinstance(cid, str) or not cid:
                rep.add(where, "ids:missing")                        # check 3
            elif cid in seen:
                rep.add(where, "ids:duplicate")
            else:
                seen[cid] = part
            if part == "located":
                if _keys(rep, where, e, LOCATED_KEYS):
                    texts = ("category", "record", "source_id", "heading", "line_sha256")
                    if not all(isinstance(e[k], str) for k in texts) or not _is_int(e["line"]) \
                            or not (e["sentence"] is None or _is_int(e["sentence"])):
                        rep.add(where, "schema:type")
                    _expected(rep, where, e["expected"], tables, synthetic=False)
            elif part == "synthetic":
                _notes(rep, where, e, SYNTHETIC_NOTES)
                if _keys(rep, where, e, SYNTHETIC_KEYS, SYNTHETIC_NOTES):
                    _expected(rep, where, e["expected"], tables, synthetic=True)
            elif part == "known_failures":
                _known_failure(rep, where, e)
            else:
                _notes(rep, where, e, ("rule",))
                if _keys(rep, where, e, GENERATION_KEYS, GENERATION_OPTIONAL):
                    if not isinstance(e["record"], str):
                        rep.add(where, "schema:type:record")
                    if e["expected"] != "rendered":
                        rep.add(where, "generation:expected")        # check 12
                    if "c_must_not_match" in e:
                        if not _str_list(e["c_must_not_match"]):
                            rep.add(where, "schema:type:c_must_not_match")
                        else:
                            _patterns(rep, where, e, ("c_must_not_match",))
    _kf_obligations(rep, cases["known_failures"], ob)
    _other_obligations(rep, cases, ob)


# =================================================================================================
# the checks against the pass's two files (13, 14 and the provenance) — identity first
# =================================================================================================

def read_pass_files(located_path: Path, sheet_path: Path, identity: Identity) -> tuple[dict, str]:
    """(the located file, the reading sheet's text). A file that is not the pass's is not read further."""
    try:
        lb = located_path.read_bytes()
    except OSError:
        raise Refused("located:not_readable") from None
    if GS.sha_bytes(lb) != identity.located_sha256:
        raise Refused("located:identity_mismatch")
    try:
        sb = sheet_path.read_bytes()
    except OSError:
        raise Refused("reading_sheet:not_readable") from None
    if GS.sha_bytes(sb) != identity.reading_sheet_sha256:
        raise Refused("reading_sheet:identity_mismatch")
    try:
        located = json.loads(lb)
        sheet = sb.decode("utf-8")
        if not isinstance(located, dict) or not isinstance(located.get("cases"), list):
            raise ValueError
    except ValueError:
        raise Refused("located:not_readable") from None
    if located.get("inputs_sha256") != identity.inputs_sha256:
        raise Refused("located:written_for_other_inputs")
    return located, sheet


def pass_checks(rep: Report, cases: dict, located: dict, sheet: str, identity: Identity) -> None:
    if cases.get("inputs_sha256") != identity.inputs_sha256:
        rep.add("list", "inputs:sha256_mismatch")                    # check 2 (the list's own field)
    prov = cases.get("provenance")
    if not isinstance(prov, dict) or prov.get("located_sha256") != identity.located_sha256 \
            or prov.get("reading_sheet_sha256") != identity.reading_sheet_sha256 \
            or prov.get("locator_sha256") != identity.locator_sha256:
        rep.add("list", "provenance:pass_identity_mismatch")
    sheet_runs = runs(words(sheet), SHEET_WORDS)                     # check 13: the whole sheet
    for part, id_key in (("located", "case_id"), ("known_failures", "kf_id"), ("generations", "case_id")):
        for i, e in enumerate(cases.get(part) or []):
            if any(runs(words(s), SHEET_WORDS) & sheet_runs for s in _strings(e)):
                rep.add(_tag(part, i, e, id_key), "wording:shared_with_the_reading_sheet")

    def canon(c):                                                    # type-strict: true is not 1
        return json.dumps({k: c.get(k) for k in PASS_KEYS}, sort_keys=True)
    def by_id(entries):
        return {c["case_id"]: canon(c) for c in entries if isinstance(c, dict) and isinstance(c.get("case_id"), str)}
    theirs, mine = by_id(located["cases"]), by_id(cases.get("located") or [])
    for cid in sorted(theirs):                                       # check 14
        if cid not in mine:
            rep.add(cid, "located:lost_since_the_pass")
        elif mine[cid] != theirs[cid]:
            rep.add(cid, "located:changed_since_the_pass")
    for i, c in enumerate(cases.get("located") or []):
        if isinstance(c, dict) and not _in(c.get("case_id"), theirs):
            rep.add(_tag("located", i, c), "located:added_since_the_pass")


# =================================================================================================
# the checks against the frozen input (4, 8, 11's scenario, 12's record) and the controls (10)
# =================================================================================================

def input_checks(rep: Report, cases: dict, inputs: Inputs) -> None:
    for i, c in enumerate(cases.get("located") or []):
        if not isinstance(c, dict):
            continue
        where = _tag("located", i, c)
        code = location_problem(c, inputs)                           # check 4: the evaluator's predicate
        if code:
            rep.add(where, code)
            continue
        sec = inputs.section(c["record"], c["source_id"])
        if sec["heading"] != c.get("heading"):
            rep.add(where, "located:heading_mismatch")               # check 4: beyond the evaluator
        text = unit_text(sec, c.get("line"), c.get("sentence"))
        if text is None:
            rep.add(where, "located:no_such_sentence")
            continue
        exp = c.get("expected") if isinstance(c.get("expected"), dict) else {}
        for j, term in enumerate(exp.get("must_keep") if _str_list(exp.get("must_keep")) else []):
            try:
                found = re.search(term, text, re.IGNORECASE) is not None        # as the evaluator tests it
            except re.error:
                continue                                             # reported by check 7
            if not found:
                rep.add(where, f"must_keep[{j}]:term_not_in_its_unit")           # check 8
    for i, kf in enumerate(cases.get("known_failures") or []):
        if isinstance(kf, dict) and not _in(kf.get("scenario_id"), inputs.scenarios):
            rep.add(_tag("known_failures", i, kf, "kf_id"), "kf:scenario_not_in_the_batch")
    for i, g in enumerate(cases.get("generations") or []):
        if isinstance(g, dict) and not _in(g.get("record"), inputs.rels):
            rep.add(_tag("generations", i, g), "generation:record_not_in_the_inputs")


def scenario_text(directory: Path) -> tuple[set, str]:
    """(every run of RUN_WORDS words in a scenario file, the files' text normalised for the whole-line rule)."""
    found, whole = set(), []
    try:
        for p in sorted(directory.glob("*.md")):
            text = p.read_text(encoding="utf-8")
            found |= runs(words(text), RUN_WORDS)
            whole.append(" ".join(text.split()).lower())
    except (OSError, UnicodeDecodeError):
        whole = []
    if not whole:
        raise Refused("scenarios:not_readable")                      # fail closed: no files, no screen
    return found, " ".join(whole)


def control_screen(case: dict, inputs: Inputs, scen_runs: set, scen_whole: str) -> str | None:
    """None if the control may be rendered; else the fixed code. NOTHING here renders (Codex 12 Nov 18:45)."""
    label = case.get("label")
    if not isinstance(label, str) or not label.startswith(SYNTHETIC_LABEL):
        return "synthetic:not_labelled"                              # (i)
    inp = case.get("input")
    secs = inp.get("sections") if isinstance(inp, dict) else None
    if not isinstance(secs, list) or not secs or not all(
            isinstance(s, dict) and all(isinstance(s.get(k), str) for k in ("section_id", "heading", "text"))
            for s in secs):
        return "synthetic:input_shape"
    for s in secs:
        for _n, _k, line, text in LC.units(s):
            if sha_text(line) in inputs.line_hashes or sha_text(text) in inputs.line_hashes:
                return "synthetic:measurement_text"                  # (ii) a line or unit of the 90
            flat = " ".join(line.lstrip("- ").split()).lower()
            if len(line.split()) >= RUN_WORDS and flat in scen_whole:
                return "synthetic:measurement_text"                  # (iv) the existing whole-line rule
        mine = runs(words(s["text"]), RUN_WORDS)                     # across the section's line breaks
        if mine & inputs.runs or mine & scen_runs:
            return "synthetic:measurement_text"                      # (iii) the inputs · (iv) a scenario file
    at = case.get("at")
    sec = next((s for s in secs if isinstance(at, dict) and s["section_id"] == at.get("source_id")), None)
    if sec is None or not _is_int(at.get("line")) or unit_text(sec, at["line"], at.get("sentence")) is None:
        return "synthetic:at"                                        # the line it is about, in its own input
    return None


def control_checks(rep: Report, cases: dict, inputs: Inputs, tables: RP.Tables, scenario: tuple) -> None:
    scen_runs, scen_whole = scenario
    for i, case in enumerate(cases.get("synthetic") or []):
        if not isinstance(case, dict):
            continue
        where = _tag("synthetic", i, case)
        code = control_screen(case, inputs, scen_runs, scen_whole)
        if code:
            rep.add(where, code)
            continue                                                 # NOT passed to synthetic_case()
        try:
            probs = GS.synthetic_case(case, tables)                  # invented text: rendered now
        except Exception as exc:                                     # fail closed
            probs = [f"raised:{type(exc).__name__}"]
        for p in probs:
            rep.add(where, f"synthetic:not_at_its_outcome:{p}")


# =================================================================================================
# the entry points
# =================================================================================================

def read_list(cases_path: Path, expect_sha256: str | None) -> tuple[dict, str]:
    try:
        cb = cases_path.read_bytes()
    except OSError:
        raise Refused("cases:not_readable") from None
    sha = GS.sha_bytes(cb)
    if expect_sha256 is not None and sha != expect_sha256:
        raise Refused("cases:sha256_not_the_expected")
    try:
        cases = json.loads(cb)
    except ValueError:
        raise Refused("cases:not_a_json_object") from None
    if not isinstance(cases, dict):
        raise Refused("cases:not_a_json_object")
    return cases, sha


def run_checks(rep: Report, cases: dict, located: dict, sheet: str, inputs: Inputs, tables: RP.Tables,
               identity: Identity, obligations: Obligations, scenario: tuple) -> None:
    """The four groups of checks on a list already read. FAIL CLOSED: if a group meets something it did
    not foresee and raises, that is a named problem (exit 1) — never a pass, never a traceback."""
    groups = (("list", lambda: list_checks(rep, cases, tables, obligations)),
              ("pass", lambda: pass_checks(rep, cases, located, sheet, identity)),
              ("inputs", lambda: input_checks(rep, cases, inputs)),
              ("controls", lambda: control_checks(rep, cases, inputs, tables, scenario)))
    for name, group in groups:
        try:
            group()
        except Refused:
            raise
        except Exception as exc:
            rep.add("validator", f"internal_error:{name}:{type(exc).__name__}")


def check(cases_path: Path, batch: Path, inputs_path: Path, located_path: Path, sheet_path: Path, *,
          expect_sha256: str | None = None, identity: Identity = PRODUCTION,
          obligations: Obligations = OBLIGATIONS, scenario_dir: Path = SCENARIO_DIR) -> Report:
    """The whole validation. Raises Refused (exit 2); otherwise a Report (exit 0 or 1)."""
    cases, sha = read_list(cases_path, expect_sha256)
    located, sheet = read_pass_files(located_path, sheet_path, identity)        # identity FIRST
    inputs = Inputs(batch, inputs_path, identity)
    try:
        tables = RP.default_tables()                     # the approved tables, checked against their pins
    except RP.RenderRefusal as exc:
        raise Refused(f"tables:{exc.code}") from None
    scenario = scenario_text(scenario_dir)               # read before any check: no files, no verdict
    rep = Report(sha)
    run_checks(rep, cases, located, sheet, inputs, tables, identity, obligations, scenario)
    part = {p: cases[p] if isinstance(cases.get(p), list) else [] for p in PARTS}
    outcomes = [c["expected"].get("outcome") for c in part["located"]
                if isinstance(c, dict) and isinstance(c.get("expected"), dict)]
    rep.counts = {"records": inputs.records, "located": len(part["located"]),
                  **{o: sum(1 for x in outcomes if isinstance(x, str) and x == o) for o in GS.OUTCOMES},
                  "synthetic": len(part["synthetic"]), "known_failure_entries": len(part["known_failures"]),
                  "known_failure_checks": sum(len(k["checks"]) for k in part["known_failures"]
                                              if isinstance(k, dict) and isinstance(k.get("checks"), list)),
                  "generations": len(part["generations"]), "pass_cases": len(located["cases"])}
    return rep


def render_report(rep: Report) -> str:
    c = rep.counts
    out = [f"r1_check_cases v{CHECKER_VERSION} — sha256 of the list checked: {rep.sha256}",
           f"inputs: {c['records']} records; the pass: {c['pass_cases']} located cases",
           f"list: {c['located']} located ({c['retained']} retained, {c['approved_omission']} approved omission, "
           f"{c['refusal']} refusal) · {c['synthetic']} synthetic · {c['known_failure_entries']} known-failure "
           f"entries / {c['known_failure_checks']} checks · {c['generations']} per-generation",
           f"problems: {len(rep.problems)}"]
    out += [f"  {w}: {code}" for w, code in sorted(rep.problems)]
    out += [f"notes: {len(rep.notes)}"] + [f"  {w}: {code}" for w, code in sorted(rep.notes)]
    out.append("WELL-FORMED (exit 0) — not adopted, not frozen: adoption is Shina's, by this sha256"
               if not rep.problems else "PROBLEMS (exit 1) — the list is not edited in place")
    return "\n".join(out) + "\n"


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("cases", type=Path)
    ap.add_argument("--batch", type=Path, required=True)
    ap.add_argument("--inputs", type=Path, required=True)
    ap.add_argument("--located", type=Path, required=True)
    ap.add_argument("--reading-sheet", type=Path, required=True)
    ap.add_argument("--expect-sha256", default=None)
    return ap


def main(argv=None, *, _identity: Identity = PRODUCTION, _obligations: Obligations = OBLIGATIONS) -> int:
    """`_identity` / `_obligations` can be passed only from Python (tests). The command line has no way
    to set them: `__main__` calls main() with neither, so the command always uses the production constants."""
    a = parser().parse_args(argv)
    try:
        rep = check(a.cases, a.batch, a.inputs, a.located, a.reading_sheet, expect_sha256=a.expect_sha256,
                    identity=_identity, obligations=_obligations)
    except Refused as exc:
        print(f"REFUSED (exit 2): {exc.code}", file=sys.stderr)
        return 2
    sys.stdout.write(render_report(rep))
    return rep.exit_code


if __name__ == "__main__":
    sys.exit(main())
