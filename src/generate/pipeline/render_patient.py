"""Item 3 R1 candidate — the constrained PART C renderer (ADR-009 step 7; WS1a step build,
item 3; the R1 experiment's step 2 — decision-2 record Round 3 (D6, D7), the item-3 R1 build
record session 3 Rounds 1-2, adopted by Shina (author/CSO) 10 Nov 2026 07:54).

Pure and UNWIRED: not in registry._ALL_STEPS, not called by the orchestrator (D6). No model
call, no client, no network, no writes; it reads this file and its tables once. It is a
hypothesis under test, not "faithful by construction".

  in:  exactly model step 7's input — {"audience", "sections": [{section_id, heading, text}]}
       from compose_patient.patient_input(), validated by compose_patient's own _check_input
       (the A-only contract; F1 -> no_sections_given). A "revision" key -> bad_input.
  out: model step 7's contract — {"sections", "checks", "counts", "audience", "revised": False,
       "renderer", "ledger"}: sections / checks / counts are compose_patient.check_patient_version's
       own output on the renderer's wire form (flags, never blocks); "renderer" replaces "call";
       "ledger" holds the renderer's own record (lines, ops, drops, unknown words, counts).

One PART A line in -> at most one PART C line out, same order (D7.1). Four transformations
only (D7.2): (a) glossary / drug class "plain (term)"; (b) the phrase table (label, inferred,
query, person, literal); (c) a fixed lead-in per C heading and audience; (d) drops by rule.
Every other word is copied exactly as written. A drop is AUTHORISED only for its whole span
(Codex 07:38:27, Round 2 fix 1): one unit, fully covered by approved entries of one drop table
(+ fixed fillers), no required-content word, no instruction marker for the box-owned tables,
the position rule, the rest keeping a content word. Pattern matches (resus, signpost,
sensitive, medicine) only SCREEN: a resus or signpost hit with no authorised drop refuses;
any other is copied and the unchanged step-7 checks flag it for the CSO's ruling.

Each line is self-checked, fail closed (D7.3, D7.8): partition, reproduction and no loss of a
protected token (verify_line), and every op authorised against the frozen tables
(authorise_ops). audit() re-runs all of it from a saved output — positions and source text
rebuilt from the input, never trusted from the ledger — and re-renders the record.

Refusals raise RenderRefusal (a StepError: a fixed code + where = {source_id, line, sentence},
positions only, never text).

Tables live in render_tables/ (JSON + general_words.txt); their sha256 are pinned below, so a
table change also changes pipeline_version (which hashes this package's .py bytes). Step 2
ships STUBS (status "stub"); step 3 fills them for the CSO's approval (D4). The offline harness
(evals/render_offline.py) refuses stub tables unless --dev, and --dev on the measurement batch.

Implementation choices beyond the adopted text (10 Nov 2026; disclosed in the build record, each
stricter or narrower, none looser): the case rule applies at a line's start only; a box-owned
rule (seek_help, home_medicine) never drops from a LINE that carries an instruction marker
(Round 2 said "the unit"); a drop-table example may hold no protected token other than a number
in a number slot, and none may match a required-content pattern; on a label line the label is
structure, never part of a unit; every resus match is a hit of the built-in withheld entry
(dropped by an authorised unit or refused), and authorise_ops' copy check is the backstop.
After Codex's 09:04:06 review (Round 4, 10 Nov 2026): F3 words (suicid*, self-harm*, overdos*) are
checked on the actual removed span of every drop — builder and authorise_ops — not only by the table
validation's probes; protected tokens are located on the complete source line (protected_spans), so a
phrase split across ops must survive as written.

Shared rule definitions, stated honestly: authorise_ops uses the same unit enumeration,
coverage test and case rule as the builder — it is independent of the RECORD (it re-derives
every claim from the source line and the pinned tables) and of the builder's choices, not of
the rule definitions themselves. The record-level re-render in audit() checks the builder.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from . import compose_patient as CP7
from . import meaning as MN
from .compose import RESUS_CONFLICT_TEXT, _INFERRED
from .errors import StepError
from .retrieve import _RESUS_PATTERNS
from .safety_net_gate import _SIGNPOST
from .schemas import record_patient_version_schema, shape_errors

RENDERER_NAME = "constrained_renderer"
RENDERER_VERSION = 1
MODULE_PATH = Path(__file__).resolve()
TABLES_DIR = MODULE_PATH.parent / "render_tables"

JSON_TABLES = ("glossary", "drug_classes", "phrases", "withheld", "seek_help", "home_medicine",
               "required_content", "lead_ins")
WORDS_FILE = "general_words.txt"
TABLE_FILES = tuple(f"{t}.json" for t in JSON_TABLES) + (WORDS_FILE,)
# sha256 of each table file — the pins (Round 1). Stubs in step 2; the CSO-approved tables at step 3/5.
TABLE_SHA256 = {
    "glossary.json": "f6df976f2ce481bbb471326a584cae5586dacba8b320397da61339f1e9d021c1",
    "drug_classes.json": "8ca20dfe16ef2c099a7ef1e7f2139c650366aae5b77697be108ce141921b5abe",
    "phrases.json": "037ff80f9959cab53dd720e64b6553c04c758ff09113666261d4a400d2d17224",
    "withheld.json": "34f06e449a5540e641d62316ab201c2acaaf81de9b031710a086b93975fea6a1",
    "seek_help.json": "ddc49c507b01ae972c5ed6d4935cac442b08806a75d8cdeb6797fa51907fd8bc",
    "home_medicine.json": "f62efda9a7a20cc119cf94c7525c5e5b5f9010f24c5dc9e5d49c091b1ffeafe3",
    "required_content.json": "32f58db91081316ef5ced766de7669421d78c733c2fc0529671c00474f88714a",
    "lead_ins.json": "d01448f09bfa8aa8a6a670f55539f3beab220cf625e27d3e4633d784c0000ed0",
    "general_words.txt": "eafd434fcae9451aa9e01615c4c3cec3d49c550b924419bb29f2d4f2fe2475f0",
}

# The inferred-suffix wording adopted by Shina (author/CSO), 9 Nov 2026 ~20:00 (1(a)). The phrase
# table's `inferred` entry must give exactly this (validation).
ADOPTED_INFERRED = "(the notes point to this but do not say it; the team must check it)"

AUDIENCES = CP7.AUDIENCES
C_OF = {"presenting_complaint": "why_in_hospital", "diagnosis": "why_in_hospital",
        "key_investigations": "what_we_did", "treatment": "what_we_did",
        "follow_up": "next_steps", "gp_actions": "next_steps"}
PROSE_C = ("why_in_hospital", "what_we_did", "next_steps")
NEVER_WHOLE = ("presenting_complaint", "diagnosis")      # sub-line drops only (D7.5, Round 2 (iv))

REFUSALS = ("bad_input", "no_sections_given", "unsupported_form", "conflict_in_input",
            "resus_not_isolable", "signpost_not_isolable", "withheld_not_isolable",
            "instruction_overlap", "reason_dropped", "accounting_failed", "op_not_authorised",
            "meaning_token_lost", "patient_version_shape_invalid", "tables_invalid", "tables_changed")
DROP_RULES = ("withheld", "seek_help", "home_medicine", "resus_conflict")
SUB_TABLES = ("glossary", "drug_classes", "phrases")
PHRASE_KINDS = ("label", "inferred", "query", "person", "literal")
UNIT_KINDS = ("aside", "dash", "semicolon", "comma", "item", "sentence", "line")
RESUS_ENTRY = "resus_patterns"            # the built-in withheld entry: retrieve._RESUS_PATTERNS, one copy

# --- the line grammar (D7.4; decision 1 (b)) ----------------------------------------------------
LABEL = re.compile(r"(Primary|Secondary|Background)(?: diagnosis| diagnoses)?:", re.IGNORECASE)
_UNSUPPORTED_START = re.compile(r"(?:#|\*|•|\||\d+[.)])")
_SENT = re.compile(r"(?<=[.!?])\s+")
_PLACEHOLDER = re.compile(r"\{\{|\}\}")
INFERRED_BRACKET = re.compile(r"\(\s*inferred\W+not documented\W+confirm\s*\)", re.IGNORECASE)
_NOTES_CONFLICT = re.compile(r"\bnotes\s+conflict\b", re.IGNORECASE)

# --- words, fillers, instruction markers ---------------------------------------------------------
WORD = re.compile(r"[A-Za-z0-9]+(?:['’./-][A-Za-z0-9]+)*")
_WORDCH = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'’-")
FILLERS = frozenset("a an the of and or with is was".split())
INSTRUCTION = re.compile(r"\b(?:advised|asked|told|instructed|planned)\b", re.IGNORECASE)
_SEPARATOR_DASH = re.compile(r"\s[-–—]\s|—")

# --- protected tokens (D7.3; Round 1's classes, adopted 07:54) ------------------------------------
NEGATION = frozenset("no not nil none never without negative neither nor nothing denies denied absent".split())
UNCERTAINTY = frozenset("likely unlikely query may might could presumed impression".split())
CHANGE = frozenset("partly until unless ongoing awaited pending still stable".split())
TIMING = frozenset("""day days week weeks month months year years hour hours hrs overnight yesterday
today tomorrow admission discharge before after since prior previous previously recent recently
current currently now later ago then during daily weekly""".split())
SCOPE = frozenset("only all both each every some any either left right bilateral unilateral whole entire "
                  "single multiple several".split())
PREFIXES = ("possib", "probabl", "suspect", "improv", "worsen", "initial", "partial", "mild", "resolv")
PHRASES = ("ruled out", "rule out", "at first", "treated as", "treat as", "treating as", "concerning for",
           "only if", "for now", "for the time being", "not yet")
_PHRASE_RE = re.compile(r"\b(?:" + "|".join(re.escape(p) for p in sorted(PHRASES, key=len, reverse=True))
                        + r")\b", re.IGNORECASE)
_DIGIT_TOKEN = re.compile(r"[^\s]*\d[^\s]*")
_ALPHA = re.compile(r"[A-Za-z][A-Za-z'’-]*")
_EDGE_PUNCT = "()[]{},;:.!?\"“”"

# --- step-7 check patterns a table text must not trip (Round 1, D4) -------------------------------
CHECK_PATTERNS = (("advice", CP7._ADVICE), ("promise", CP7._PROMISE), ("sensitive", CP7._SENSITIVE),
                  ("conflict", CP7._CONFLICT), ("dose", CP7._DOSE), ("signpost", _SIGNPOST),
                  ("not_documented", CP7._NOT_DOCUMENTED), ("cause", MN._CAUSE), ("certainty", MN._CERTAINTY)) \
    + tuple((f"resus{i}", p) for i, p in enumerate(_RESUS_PATTERNS)) \
    + tuple((f"test:{n}", in_c) for n, in_c, _j in MN._TEST_TERMS)
F3_PROBES = ("suicidal thoughts", "suicide", "self-harm", "self harm", "overdose")
# F3 (decision-2 record Round 3, D7.5): the reason for admission as given, suicidal thoughts and self-harm
# included, is never dropped. Checked on the ACTUAL removed span of every drop, in the builder and in
# authorise_ops (Codex 09:04:06 P1) — the table validation's probe test alone cannot see a broader pattern.
F3_NEVER_DROP = re.compile(r"(?<![\w'’-])(?:suicid\w*|self[\s-]*harm\w*|overdos\w*)", re.IGNORECASE)


class RenderRefusal(StepError):
    """A fail-closed refusal: a fixed code and where it happened (positions only, never text)."""

    def __init__(self, code: str, where: dict | None = None):
        super().__init__(code)
        self.where = where


# =================================================================================================
# protected tokens
# =================================================================================================

def _canon_word(w: str) -> str | None:
    w = w.lower().strip("'’-")
    if not w:
        return None
    if w.endswith("n't") or w.endswith("n’t"):
        return "n't"
    if w in NEGATION or w in UNCERTAINTY or w in CHANGE or w in TIMING or w in SCOPE:
        return w
    for p in PREFIXES:
        if w.startswith(p):
            return p
    return None


def canon(token: str) -> str | None:
    """The protected-token key of a single word or symbol (used for keep-maps)."""
    if token == "?":
        return "?"
    if any(ch.isdigit() for ch in token):
        return "#" + token.strip(_EDGE_PUNCT)
    return _canon_word(token)


def protected_spans(text: str) -> list[tuple[int, int, str]]:
    """Every protected token of `text` with its position: (start, end, key). Multiword phrases and
    digit tokens are located as whole spans of the COMPLETE line, so an op boundary that splits one
    cannot hide its loss (Codex 09:04:06 P2). protected() is the multiset of these keys."""
    out: list[tuple[int, int, str]] = []
    for m in _DIGIT_TOKEN.finditer(text):
        raw = m.group(0)
        tok = raw.strip(_EDGE_PUNCT)
        if tok:
            s = m.start() + len(raw) - len(raw.lstrip(_EDGE_PUNCT))
            out.append((s, s + len(tok), "#" + tok))
    for m in CP7._UNIT.finditer(text):
        out.append((m.start(1), m.end(1), "unit:" + m.group(1).lower()))
    stripped = _DIGIT_TOKEN.sub(lambda m: " " * len(m.group(0)), text)      # same length: positions hold
    for m in _ALPHA.finditer(stripped):
        k = _canon_word(m.group(0))
        if k:
            out.append((m.start(), m.end(), k))
    for m in _PHRASE_RE.finditer(text):
        out.append((m.start(), m.end(), "phrase:" + " ".join(m.group(0).lower().split())))
    out += [(i, i + 1, "?") for i, ch in enumerate(text) if ch == "?"]
    return out


def protected(text: str) -> Counter:
    """Multiset of protected tokens in `text` (Round 1's classes): digit tokens (exact string),
    units, negation, uncertainty and change, timing, scope, the fixed phrases and "?"."""
    return Counter(k for _s, _e, k in protected_spans(text))


# =================================================================================================
# tables
# =================================================================================================

@dataclass(frozen=True)
class Entry:
    table: str
    id: str
    raw: dict
    regexes: tuple = ()


@dataclass(frozen=True)
class Tables:
    status: str
    sha256: dict
    glossary: tuple
    drug_classes: tuple
    phrases: tuple
    withheld: tuple
    seek_help: tuple
    home_medicine: tuple
    required_content: tuple
    lead_ins: dict
    general_words: frozenset
    by_id: dict = field(default_factory=dict)

    def entry(self, table: str, entry_id):
        return self.by_id.get((table, entry_id))


def _form_regex(form: str, case_sensitive: bool) -> re.Pattern:
    return re.compile(r"(?<![\w'’-])" + re.escape(form) + r"(?![\w'’-])", 0 if case_sensitive else re.IGNORECASE)


def _drop_regex(pattern: str) -> re.Pattern:
    return re.compile(r"(?<![\w'’-])(?:" + pattern + r")(?![\w'’-])", re.IGNORECASE)


_TABLE_KEYS = {"table", "version", "status", "approved_by", "approved_at", "entries"}
_ENTRY_KEYS = {
    "glossary": {"id", "forms", "case_sensitive", "plain", "category", "origin", "flag_words", "keeps"},
    "drug_classes": {"id", "forms", "case_sensitive", "plain", "origin", "flag_words"},
    "phrases": {"id", "kind", "forms", "plain", "keeps", "flag_words"},
    "withheld": {"id", "category", "pattern", "number_slot", "examples"},
    "seek_help": {"id", "category", "pattern", "number_slot", "examples"},
    "home_medicine": {"id", "category", "pattern", "number_slot", "examples"},
    "required_content": {"id", "pattern"},
    "lead_ins": {"heading", "audience", "text"},
}


def _check_hits(text: str, allowed: set) -> list[str]:
    """Words of `text` that trip a step-7 check pattern and are not in `allowed` (lower case)."""
    out = []
    for name, pat in CHECK_PATTERNS:
        for m in pat.finditer(text):
            w = " ".join(m.group(0).lower().split())
            if w not in allowed:
                out.append(f"{name}:{w}")
    return out


def validate_tables(raw: dict, words_text: str) -> list[str]:
    """Problems (fixed words + entry ids, never table text) in a set of tables; [] = valid.
    `raw` maps each JSON table name to its parsed content."""
    probs: list[str] = []
    statuses = []
    forms_seen: dict[str, str] = {}
    required = ((raw.get("required_content") or {}).get("entries") or []) if isinstance(raw.get("required_content"), dict) else []
    for t in JSON_TABLES:
        d = raw.get(t)
        if not isinstance(d, dict) or set(d) != _TABLE_KEYS or d.get("table") != t:
            probs.append(f"{t}:shape")
            continue
        if d["status"] not in ("stub", "approved"):
            probs.append(f"{t}:status")
        if (d["status"] == "approved") != (bool(d["approved_by"]) and bool(d["approved_at"])):
            probs.append(f"{t}:approval_fields")
        statuses.append(d["status"])
        if not isinstance(d["entries"], list):
            probs.append(f"{t}:entries")
            continue
        ids = set()
        for e in d["entries"]:
            if not isinstance(e, dict) or set(e) != _ENTRY_KEYS[t]:
                probs.append(f"{t}:entry_shape")
                continue
            eid = e.get("id", f"{e.get('heading')}/{e.get('audience')}")
            if eid in ids:
                probs.append(f"{t}:{eid}:duplicate_id")
            ids.add(eid)
            probs += _validate_entry(t, e, eid, forms_seen, required)
    lead = (raw.get("lead_ins") or {}).get("entries") or []
    have = {(e.get("heading"), e.get("audience")) for e in lead if isinstance(e, dict)}
    for h in PROSE_C:
        for a in AUDIENCES:
            if (h, a) not in have:
                probs.append(f"lead_ins:{h}/{a}:missing")
    lines = words_text.splitlines()
    if not lines or not re.fullmatch(r"# status: (stub|approved)", lines[0].strip()):
        probs.append("general_words:header")
    else:
        statuses.append(lines[0].split(":", 1)[1].strip())
        for w in lines[1:]:
            if w.strip() and not re.fullmatch(r"[a-z][a-z'-]*", w.strip()):
                probs.append("general_words:word")
                break
    kinds = Counter(e.get("kind") for e in (raw.get("phrases") or {}).get("entries") or [] if isinstance(e, dict))
    if kinds.get("inferred", 0) > 1:
        probs.append("phrases:inferred:more_than_one")
    return probs


def _compiles(pattern: str) -> bool:
    try:
        _drop_regex(pattern)
        return True
    except re.error:
        return False


def _validate_entry(t: str, e: dict, eid: str, forms_seen: dict, required: list) -> list[str]:
    p: list[str] = []
    if t in ("glossary", "drug_classes", "phrases"):
        forms = e["forms"]
        if not isinstance(forms, list) or not forms or not all(isinstance(f, str) and f for f in forms):
            return [f"{t}:{eid}:forms"]
        kind = e.get("kind") if t == "phrases" else None
        if t == "phrases" and kind not in PHRASE_KINDS:
            return [f"phrases:{eid}:kind"]
        for f in forms:
            if kind in (None, "person", "literal"):
                if not (f[0].isalnum() and f[-1].isalnum()):
                    p.append(f"{t}:{eid}:form_edges")
                key = " ".join(f.lower().split())
                if key in forms_seen and forms_seen[key] != f"{t}:{eid}":
                    p.append(f"{t}:{eid}:form_in_two_entries")
                forms_seen[key] = f"{t}:{eid}"
        flag_words = e["flag_words"]
        if not isinstance(flag_words, list) or not all(isinstance(w, str) for w in flag_words):
            return p + [f"{t}:{eid}:flag_words"]
        allowed = {" ".join(w.lower().split()) for w in flag_words}
        keeps = e.get("keeps", {})
        if not isinstance(keeps, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in keeps.items()):
            return p + [f"{t}:{eid}:keeps"]
        plains = e["plain"]
        if t == "phrases":
            if not isinstance(plains, dict) or set(plains) != set(AUDIENCES) or not all(isinstance(v, str) for v in plains.values()):
                return p + [f"phrases:{eid}:plain"]
            plain_list = list(plains.values())
        else:
            if not isinstance(plains, str) or not plains.strip():
                return p + [f"{t}:{eid}:plain"]
            plain_list = [plains]
            if not isinstance(e["case_sensitive"], bool):
                p.append(f"{t}:{eid}:case_sensitive")
            if e["origin"] not in ("general", "added_after_inputs"):
                p.append(f"{t}:{eid}:origin")
        if kind == "label":
            if len(forms) != 1 or forms[0].lower() not in ("primary", "secondary", "background"):
                p.append(f"phrases:{eid}:label_form")
        if kind == "inferred":
            if any(v != ADOPTED_INFERRED for v in plain_list):
                p.append("phrases:inferred:not_the_adopted_wording")
        if kind == "query":
            if sorted(f.lower() for f in forms) not in (["?"], ["query"], ["?", "query"]):
                p.append(f"phrases:{eid}:query_forms")
        # no addition of a protected token the matched forms do not carry (other than keep-map images)
        carried = Counter()
        for f in forms:
            carried |= protected(f)
        for v in keeps.values():
            k = canon(v)
            if k:
                carried[k] += 99
        for v in plain_list:
            extra = protected(v) - carried
            if extra:
                p.append(f"{t}:{eid}:adds_protected_token")
            if _check_hits(v, allowed):
                p.append(f"{t}:{eid}:check_word_undeclared")
        for w in sorted(allowed):                 # a declaration must be used (no stale pre-approvals)
            if not any(" ".join(m.group(0).lower().split()) == w
                       for v in plain_list for _n, pat in CHECK_PATTERNS for m in pat.finditer(v)):
                p.append(f"{t}:{eid}:flag_word_not_in_plain")
        for k, v in keeps.items():
            if canon(k) is None or canon(v) is None:
                p.append(f"{t}:{eid}:keep_not_protected")
    elif t in ("withheld", "seek_help", "home_medicine"):
        try:
            rx = _drop_regex(e["pattern"])
        except (re.error, TypeError):
            return [f"{t}:{eid}:pattern"]
        if rx.fullmatch(""):
            p.append(f"{t}:{eid}:matches_empty")
        if not isinstance(e["number_slot"], bool):
            p.append(f"{t}:{eid}:number_slot")
        ex = e["examples"]
        if not isinstance(ex, list) or not ex or not all(isinstance(s, str) and s for s in ex):
            return p + [f"{t}:{eid}:examples"]
        for s in ex:
            if not rx.fullmatch(s):
                p.append(f"{t}:{eid}:example_not_matched")
            if any(ch.isdigit() for ch in s) and not e["number_slot"]:
                p.append(f"{t}:{eid}:digit_without_number_slot")
            if t != "withheld" and (CP7._ADVICE.search(s) or INSTRUCTION.search(s)):
                p.append(f"{t}:{eid}:instruction_marker")
            if any(_drop_regex(r["pattern"]).search(s) for r in required if isinstance(r, dict)
                   and isinstance(r.get("pattern"), str) and _compiles(r["pattern"])):
                p.append(f"{t}:{eid}:required_content")
            if any(not k.startswith("#") for k in protected(s)):
                p.append(f"{t}:{eid}:protected_token")     # only a number (in a number slot) may leave
            if F3_NEVER_DROP.search(s):
                p.append(f"{t}:{eid}:example_holds_f3_word")
        if t == "withheld" and any(rx.search(probe) for probe in F3_PROBES):
            p.append("withheld:" + eid + ":matches_f3_word")
    elif t == "required_content":
        try:
            _drop_regex(e["pattern"])
        except (re.error, TypeError):
            p.append(f"required_content:{eid}:pattern")
    elif t == "lead_ins":
        if e["heading"] not in PROSE_C or e["audience"] not in AUDIENCES or not isinstance(e["text"], str):
            return [f"lead_ins:{eid}:shape"]
        if protected(e["text"]):
            p.append(f"lead_ins:{eid}:protected_token")
        if _check_hits(e["text"], set()):
            p.append(f"lead_ins:{eid}:check_word")
        if "\n" in e["text"]:
            p.append(f"lead_ins:{eid}:newline")
    return p


def build_tables(raw: dict, words_text: str, sha256: dict | None = None) -> Tables:
    """Validated Tables from parsed content (tests pass fixture tables here). Invalid -> tables_invalid."""
    if validate_tables(raw, words_text):
        raise RenderRefusal("tables_invalid")
    by_id: dict = {}
    tabs: dict = {}
    for t in JSON_TABLES:
        if t == "lead_ins":
            continue
        out = []
        for e in raw[t]["entries"]:
            if t in ("glossary", "drug_classes"):
                rx = tuple(_form_regex(f, e["case_sensitive"]) for f in e["forms"])
            elif t == "phrases":
                rx = tuple(_form_regex(f, False) for f in e["forms"]) if e["kind"] in ("person", "literal") else ()
            else:
                rx = (_drop_regex(e["pattern"]),)
            ent = Entry(t, e["id"], e, rx)
            out.append(ent)
            by_id[(t, e["id"])] = ent
        tabs[t] = tuple(out)
    resus = Entry("withheld", RESUS_ENTRY, {"id": RESUS_ENTRY, "category": "resuscitation", "number_slot": False},
                  tuple(_RESUS_PATTERNS))
    by_id[("withheld", RESUS_ENTRY)] = resus
    lead = {(e["heading"], e["audience"]): e["text"] for e in raw["lead_ins"]["entries"]}
    words = frozenset(w.strip() for w in words_text.splitlines()[1:] if w.strip())
    statuses = {raw[t]["status"] for t in JSON_TABLES} | {words_text.splitlines()[0].split(":", 1)[1].strip()}
    return Tables(status="approved" if statuses == {"approved"} else "stub", sha256=dict(sha256 or {}),
                  glossary=tabs["glossary"], drug_classes=tabs["drug_classes"], phrases=tabs["phrases"],
                  withheld=tabs["withheld"] + (resus,), seek_help=tabs["seek_help"],
                  home_medicine=tabs["home_medicine"], required_content=tabs["required_content"],
                  lead_ins=lead, general_words=words, by_id=by_id)


def load_tables(directory: Path = TABLES_DIR, pins: dict | None = TABLE_SHA256) -> Tables:
    """The tables in `directory`, checked against the pins (None: no pin check — tests only)."""
    blobs, shas = {}, {}
    for name in TABLE_FILES:
        try:
            b = (directory / name).read_bytes()
        except OSError:
            raise RenderRefusal("tables_invalid") from None
        blobs[name] = b
        shas[name] = hashlib.sha256(b).hexdigest()
    if pins is not None and shas != pins:
        raise RenderRefusal("tables_changed")
    try:
        raw = {t: json.loads(blobs[f"{t}.json"].decode("utf-8")) for t in JSON_TABLES}
        words = blobs[WORDS_FILE].decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        raise RenderRefusal("tables_invalid") from None
    return build_tables(raw, words, shas)


@lru_cache(maxsize=1)
def default_tables() -> Tables:
    return load_tables()


def module_sha256() -> str:
    return hashlib.sha256(MODULE_PATH.read_bytes()).hexdigest()


# =================================================================================================
# splitting A into lines (the grammar)
# =================================================================================================

def _norm(s: str) -> str:
    return " ".join(s.split())


def split_lines(section: dict) -> list[dict]:
    """A given section -> its lines: {source_id, line, sentence, form, label, marker, text}.
    `line` is the 1-based physical line; `sentence` the 1-based sentence of a prose line, else None;
    `text` excludes the "- " marker. Anything outside the grammar -> unsupported_form."""
    sid, heading, text = section["section_id"], section["heading"], section["text"]
    out: list[dict] = []
    current_label = None
    for n, raw in enumerate(text.split("\n"), 1):
        where = {"source_id": sid, "line": n, "sentence": None}
        s = raw.rstrip()
        if not s.strip():
            continue
        if s != s.lstrip() or _UNSUPPORTED_START.match(s) or _PLACEHOLDER.search(s):
            raise RenderRefusal("unsupported_form", where)
        if len(_INFERRED.findall(s)) != len(INFERRED_BRACKET.findall(s)):
            raise RenderRefusal("unsupported_form", where)
        if s.startswith("- "):
            body = s[2:]
            if not body.strip() or body != body.lstrip():
                raise RenderRefusal("unsupported_form", where)
            if heading == "diagnosis":
                if current_label is None:
                    raise RenderRefusal("unsupported_form", where)
                out.append({"source_id": sid, "line": n, "sentence": None, "form": "label_item",
                            "label": current_label, "marker": "- ", "text": body})
            else:
                out.append({"source_id": sid, "line": n, "sentence": None, "form": "item", "label": None,
                            "marker": "- ", "text": body})
            continue
        if s.startswith("-"):
            raise RenderRefusal("unsupported_form", where)
        if heading == "diagnosis":
            m = LABEL.match(s)
            if not m:
                raise RenderRefusal("unsupported_form", where)
            current_label = m.group(1).lower()
            out.append({"source_id": sid, "line": n, "sentence": None, "form": "label", "label": current_label,
                        "marker": "", "text": s})
            continue
        for k, sent in enumerate((x for x in _SENT.split(s) if x.strip()), 1):
            out.append({"source_id": sid, "line": n, "sentence": k, "form": "prose", "label": None,
                        "marker": "", "text": sent.strip()})
    return out


def _where(u: dict) -> dict:
    return {"source_id": u["source_id"], "line": u["line"], "sentence": u["sentence"]}


# =================================================================================================
# units and drop authorisation (Round 2 fix 1)
# =================================================================================================

def _paren_pairs(text: str) -> list[tuple[int, int]]:
    stack, pairs = [], []
    for i, ch in enumerate(text):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            pairs.append((stack.pop(), i + 1))
    return pairs


def _depth_map(text: str, lo: int, hi: int) -> list[int]:
    d, out = 0, []
    for i in range(lo, hi):
        if text[i] == "(":
            d += 1
        out.append(d)
        if text[i] == ")":
            d = max(0, d - 1)
    return out


def _split_top(text: str, lo: int, hi: int, kind: str) -> list[tuple[int, int, int, int]]:
    """Top-level segments of text[lo:hi] split by `kind`'s separator, ignoring nested brackets:
    (seg_start, seg_end, sep_before_start, sep_after_end) with the separators' extents."""
    depth = _depth_map(text, lo, hi)
    seps: list[tuple[int, int]] = []
    if kind == "dash":
        for m in _SEPARATOR_DASH.finditer(text, lo, hi):
            if depth[m.start() - lo] == 0 and (m.end() - 1 - lo < len(depth) and depth[m.end() - 1 - lo] == 0):
                seps.append((m.start(), m.end()))
    else:
        ch = ";" if kind == "semicolon" else ","
        for i in range(lo, hi):
            if text[i] == ch and depth[i - lo] == 0:
                j = i + 1
                while j < hi and text[j] == " ":
                    j += 1
                seps.append((i, j))
    if not seps:
        return []
    bounds = [lo] + [x for s in seps for x in s] + [hi]
    segs = []
    for k in range(len(seps) + 1):
        a, b = bounds[2 * k], bounds[2 * k + 1]
        before = seps[k - 1][0] if k > 0 else None
        after = seps[k][1] if k < len(seps) else None
        segs.append((a, b, before, after, k))
    return [(a, b, bf, af) for a, b, bf, af, k in segs if not (kind == "dash" and k == 0)]


def content_start(text: str, form: str) -> int:
    """Where a line's content starts: after the label on a diagnosis label line, else 0. The label
    is structure, never part of a unit."""
    if form == "label":
        m = LABEL.match(text)
        if m:
            i = m.end()
            while i < len(text) and text[i] == " ":
                i += 1
            return i
    return 0


def candidate_units(text: str, start: int = 0) -> list[tuple[int, int, str]]:
    """Every unit of a line's content (text[start:]), as the DROP SPAN (unit + one adjacent
    separator) and its kind: bracketed asides (with the space before them), dash / ; / , segments
    of each container (the content, and each aside's interior), and the whole content ("line")."""
    out = []
    containers = [(start, len(text))]
    for a, b in _paren_pairs(text):
        if a < start:
            continue
        span_a = a - 1 if a > 0 and text[a - 1] == " " else a
        out.append((span_a, b, "aside"))
        containers.append((a + 1, b - 1))
    for lo, hi in containers:
        for kind in ("dash", "semicolon", "comma"):
            for a, b, before, after in _split_top(text, lo, hi, kind):
                if a >= b:
                    continue
                if before is not None:
                    out.append((before, b, kind))
                elif after is not None:
                    out.append((a, after, kind))
    out.append((start, len(text), "line"))
    seen, uniq = set(), []
    for u in sorted(out, key=lambda x: (x[1] - x[0], x[0], UNIT_KINDS.index(x[2]))):
        if (u[0], u[1]) not in seen:
            seen.add((u[0], u[1]))
            uniq.append(u)
    return uniq


def _words(text: str, lo: int, hi: int):
    for m in WORD.finditer(text, lo, hi):
        yield m.start(), m.end(), m.group(0)


def _table_entries(tables: Tables, rule: str) -> tuple:
    return {"withheld": tables.withheld, "seek_help": tables.seek_help,
            "home_medicine": tables.home_medicine}[rule]


def _entry_matches(entry: Entry, text: str) -> list[tuple[int, int]]:
    return [(m.start(), m.end()) for rx in entry.regexes for m in rx.finditer(text) if m.end() > m.start()]


def drop_problem(text: str, a: int, b: int, kind: str, rule: str, entry_id, tables: Tables,
                 heading: str, form: str) -> str | None:
    """None if dropping text[a:b] (a unit of `kind`) under `rule` / `entry_id` is authorised, else the
    reason. Checks (i)-(v) of Round 2 on the WHOLE span, from the source text and the tables."""
    if rule not in ("withheld", "seek_help", "home_medicine"):
        return "rule"
    ent = tables.entry(rule, entry_id)
    if ent is None:
        return "entry"
    if (a, b, kind) not in candidate_units(text, content_start(text, form)):          # (i)
        return "not_a_unit"
    if not any(a <= s and e <= b for s, e in _entry_matches(ent, text)):
        return "entry_not_in_span"
    # (iii), the instruction part — checked on the WHOLE LINE, before coverage: a box-owned rule never
    # drops from a line that carries a team instruction ("Advised to rest, attend ..." must not lose a
    # clause to a comma). Stricter than Round 2's "the unit holds no marker" (disclosed).
    if rule in ("seek_help", "home_medicine") and (CP7._ADVICE.search(text) or INSTRUCTION.search(text)):
        return "instruction"
    entries = _table_entries(tables, rule)
    matches = [(s, e, en) for en in entries for s, e in _entry_matches(en, text) if a <= s and e <= b]
    for ws, we, w in _words(text, a, b):                                           # (ii) full coverage
        inside = [en for s, e, en in matches if s <= ws and we <= e]
        if any(ch.isdigit() for ch in w):
            if not any(en.raw.get("number_slot") for en in inside):
                return "number_outside_slot"
        elif not inside and w.lower() not in FILLERS:
            return "not_covered"
    span = text[a:b]
    if F3_NEVER_DROP.search(span):                                                 # F3, on the real span
        return "f3_content"
    if any(r.regexes[0].search(span) for r in tables.required_content):            # (iii)
        return "required_content"
    whole = kind == "line"
    if whole and (heading in NEVER_WHOLE or form in ("label", "label_item")):        # (iv)
        return "position"
    if not whole:
        rest = text[:a] + " " + text[b:]
        if not any(w.isalpha() and w.lower() not in MN._STOP and w.lower() not in FILLERS
                   for _s, _e, w in _words(rest, 0, len(rest))):                   # (v)
            return "rest_empty"
    return None


# =================================================================================================
# substitutions
# =================================================================================================

def case_rule(out: str, text: str, a: int) -> str:
    """At the start of a line (a == 0), a substitution's output starts with a capital when the
    first letter of the source from there on is a capital ("IV ..." -> "Into a vein (IV) ...",
    "?quinsy" -> "possible ...", "?Quinsy" -> "Possible ..."); elsewhere the output is as the table
    gives it. (Narrowed from Round 1's "takes the case of the term's first letter" to the line
    start, so a mid-line abbreviation does not capitalise the plain words — disclosed.)"""
    first = next((ch for ch in text[a:] if ch.isalpha()), "")
    if a == 0 and out and first.isupper():
        return out[0].upper() + out[1:]
    return out


def sub_out(table: str, ent: Entry, span: str, a: int, audience: str, text: str) -> str | None:
    """The output an entry gives for `span` at `a`, or None if the entry does not apply there."""
    e = ent.raw
    if table in ("glossary", "drug_classes"):
        if not any(rx.fullmatch(span) for rx in ent.regexes) or not _word_safe(text, a, a + len(span)):
            return None
        return case_rule(f"{e['plain']} ({span})", text, a)
    kind = e["kind"]
    plain = e["plain"][audience]
    if kind == "label":
        m = LABEL.match(text)
        if a != 0 or not m or m.end() != len(span) or m.group(1).lower() != e["forms"][0].lower():
            return None
        return plain
    if kind == "inferred":
        return plain if INFERRED_BRACKET.fullmatch(span) else None
    if kind == "query":
        nxt = text[a + len(span): a + len(span) + 1]
        if span == "?" and "?" in e["forms"] and nxt.isalpha() and (a == 0 or not text[a - 1].isalnum()):
            return case_rule(plain + " ", text, a)
        if span.lower() == "query" and "query" in [f.lower() for f in e["forms"]] \
                and re.match(r"\s+[A-Za-z]", text[a + len(span):]) and _word_safe(text, a, a + len(span)):
            return case_rule(plain, text, a)
        return None
    if not any(rx.fullmatch(span) for rx in _phrase_regexes(ent)) or not _word_safe(text, a, a + len(span)):
        return None
    return case_rule(plain, text, a)


def _phrase_regexes(ent: Entry):
    return ent.regexes


def _word_safe(text: str, a: int, b: int) -> bool:
    left = a == 0 or text[a - 1] not in _WORDCH or text[a] not in _WORDCH
    right = b == len(text) or text[b] not in _WORDCH or text[b - 1] not in _WORDCH
    return left and right


def _phrase_candidates(text: str, tables: Tables, form: str) -> list[tuple[int, int, Entry]]:
    cands = []
    for ent in tables.phrases:
        kind = ent.raw["kind"]
        if kind == "label":
            m = LABEL.match(text)
            if form == "label" and m and m.group(1).lower() == ent.raw["forms"][0].lower():
                cands.append((0, m.end(), ent))
        elif kind == "inferred":
            cands += [(m.start(), m.end(), ent) for m in INFERRED_BRACKET.finditer(text)]
        elif kind == "query":
            forms = [f.lower() for f in ent.raw["forms"]]
            if "?" in forms:
                cands += [(m.start(), m.end(), ent) for m in re.finditer(r"(?<![A-Za-z0-9])\?(?=[A-Za-z])", text)]
            if "query" in forms:
                cands += [(m.start(), m.end(), ent) for m in re.finditer(r"(?<![\w'’-])query(?=\s+[A-Za-z])", text,
                                                                         re.IGNORECASE)]
        else:
            cands += [(m.start(), m.end(), ent) for rx in ent.regexes for m in rx.finditer(text)]
    return cands


def _claim(cands, taken: list[tuple[int, int]]) -> list[tuple[int, int, Entry]]:
    chosen = []
    for s, e, ent in sorted(cands, key=lambda x: (x[0], -(x[1] - x[0]), x[2].table, x[2].id)):
        if any(not (e <= ts or s >= te) for ts, te in taken):
            continue
        taken.append((s, e))
        chosen.append((s, e, ent))
    return chosen


# =================================================================================================
# rendering one line
# =================================================================================================

def _normalise(s: str) -> str:
    s = " ".join(s.split())
    s = re.sub(r"\s+([,;:.)])", r"\1", s)
    s = re.sub(r"\(\s+", "(", s)
    return s.strip()


def reproduce(text: str, ops: list[dict]) -> str:
    parts = []
    for op in ops:
        if op["op"] == "copy":
            parts.append(text[op["a"]:op["b"]])
        elif op["op"] == "sub":
            parts.append(op["out"])
    return _normalise("".join(parts))


def _plan_drops(u: dict, heading: str, tables: Tables) -> list[dict]:
    text = u["text"]
    if heading == "treatment" and _norm(text) == RESUS_CONFLICT_TEXT and u["form"] in ("item", "prose"):
        return [{"op": "drop", "a": 0, "b": len(text), "rule": "resus_conflict", "entry": None, "unit": "line"}]
    if _NOTES_CONFLICT.search(text):
        raise RenderRefusal("conflict_in_input", _where(u))
    drops: list[dict] = []
    hits = []
    for rule in ("withheld", "seek_help", "home_medicine"):
        for ent in _table_entries(tables, rule):
            for s, e in _entry_matches(ent, text):
                hits.append((s, e, rule, ent))
    for s, e, rule, ent in sorted(hits, key=lambda x: (x[0], x[1], DROP_RULES.index(x[2]), x[3].id)):
        if any(d["a"] <= s and e <= d["b"] for d in drops):
            continue
        chosen, reasons = None, set()
        for a, b, kind in candidate_units(text, content_start(text, u["form"])):
            if not (a <= s and e <= b):
                continue
            if any(not (b <= d["a"] or a >= d["b"]) for d in drops):
                continue
            why = drop_problem(text, a, b, kind, rule, ent.id, tables, heading, u["form"])
            if why is None:
                chosen = {"op": "drop", "a": a, "b": b, "rule": rule, "entry": ent.id, "unit": kind}
                break
            reasons.add(why)
        if chosen:
            drops.append(chosen)
            continue
        if rule == "withheld":
            raise RenderRefusal("resus_not_isolable" if ent.id == RESUS_ENTRY else "withheld_not_isolable", _where(u))
        if "instruction" in reasons:
            raise RenderRefusal("instruction_overlap", _where(u))
        if rule == "seek_help":
            raise RenderRefusal("signpost_not_isolable", _where(u))
        # home_medicine with no authorised drop: copied as written; the step-7 checks flag it
    kept = [(0, len(text))]
    for d in sorted(drops, key=lambda d: d["a"]):
        kept = [(x, y) for x0, y0 in kept for x, y in ((x0, min(y0, d["a"])), (max(x0, d["b"]), y0)) if x < y]
    # Every resus match is a hit of the built-in withheld entry above (dropped or refused); a signpost
    # match outside an authorised drop refuses here — copying it would fail bar line 3.
    for x, y in kept:
        seg = text[x:y]
        if _SIGNPOST.search(seg):
            raise RenderRefusal("signpost_not_isolable", _where(u))
    return sorted(drops, key=lambda d: d["a"])


def render_line(u: dict, heading: str, audience: str, tables: Tables) -> tuple[list[dict], str | None]:
    """(ops, C line or None if dropped whole). Raises RenderRefusal."""
    text = u["text"]
    drops = _plan_drops(u, heading, tables)
    taken = [(d["a"], d["b"]) for d in drops]
    subs = []
    # Phrase entries first (D7.2 (b) before (a)), then glossary + drug classes; each candidate kept only
    # where its entry applies there; longest first, left to right, never overlapping a drop or a sub.
    pc = [(s, e, ent) for s, e, ent in _phrase_candidates(text, tables, u["form"])
          if sub_out("phrases", ent, text[s:e], s, audience, text) is not None]
    gc = [(m.start(), m.end(), ent) for ent in tables.glossary + tables.drug_classes
          for rx in ent.regexes for m in rx.finditer(text)]
    gc = [(s, e, ent) for s, e, ent in gc if sub_out(ent.table, ent, text[s:e], s, audience, text) is not None]
    for cands in (pc, gc):
        for s, e, ent in _claim(cands, taken):
            subs.append({"op": "sub", "a": s, "b": e, "table": ent.table, "entry": ent.id,
                         "out": sub_out(ent.table, ent, text[s:e], s, audience, text)})
    spans = sorted(drops + subs, key=lambda o: o["a"])
    ops, pos = [], 0
    for o in spans:
        if o["a"] > pos:
            ops.append({"op": "copy", "a": pos, "b": o["a"]})
        ops.append(o)
        pos = o["b"]
    if pos < len(text):
        ops.append({"op": "copy", "a": pos, "b": len(text)})
    body = reproduce(text, ops)
    return ops, (u["marker"] + body) if body else None


# =================================================================================================
# the self-checks
# =================================================================================================

def verify_line(u: dict, c_line: str | None, ops: list[dict], tables: Tables) -> list[str]:
    """Partition, reproduction, no loss of a protected token (Round 1). [] = clean."""
    text = u["text"]
    probs = []
    pos = 0
    for op in ops:
        if not isinstance(op, dict) or op.get("op") not in ("copy", "sub", "drop") \
                or not isinstance(op.get("a"), int) or not isinstance(op.get("b"), int) \
                or op["a"] != pos or op["b"] <= op["a"]:
            probs.append("partition")
            break
        if 0 < op["a"] < len(text) and text[op["a"] - 1] in _WORDCH and text[op["a"]] in _WORDCH:
            probs.append("partition_inside_word")
            break
        pos = op["b"]
    if not probs and pos != len(text):
        probs.append("partition")
    if probs:
        return probs
    body = reproduce(text, ops)
    expected = (u["marker"] + body) if body else None
    if expected != c_line:
        probs.append("reproduction")
    # Protected tokens are found on the complete source line (Codex 09:04:06 P2), then each is placed:
    # wholly inside a copy -> must appear as written; wholly inside a substitution -> must appear as
    # written or as that entry's keep-map image (the query / inferred transformations); wholly inside a
    # drop -> leaves with the drop; across an op boundary -> no single op accounts for it, so it must
    # appear as written.
    kept: Counter = Counter()
    for s, e, key in protected_spans(text):
        op = next((o for o in ops if o["a"] <= s and e <= o["b"]), None)
        if op is None or op["op"] == "copy":
            kept[key] += 1
        elif op["op"] == "sub":
            ent = tables.entry(op.get("table"), op.get("entry"))
            keeps = (ent.raw.get("keeps") or {}) if ent else {}
            image = next((canon(v) for k, v in keeps.items() if canon(k) == key), None)
            kept[image or key] += 1
    if kept - protected(c_line or ""):
        probs.append("protected_lost")
    return probs


def authorise_ops(u: dict, heading: str, audience: str, ops: list[dict], tables: Tables) -> list[str]:
    """Every op re-derived from the source line and the pinned tables (Round 2 fix 2). Saved
    `out` text and rule labels are claims to check, never trusted. [] = all authorised."""
    text = u["text"]
    probs = []
    for i, op in enumerate(ops):
        span = text[op["a"]:op["b"]]
        if op["op"] == "copy":
            if any(p.search(span) for p in _RESUS_PATTERNS) or _SIGNPOST.search(span):
                probs.append(f"op{i}:copy_holds_resus_or_signpost")
        elif op["op"] == "sub":
            ent = tables.entry(op.get("table"), op.get("entry"))
            if op.get("table") not in SUB_TABLES or ent is None:
                probs.append(f"op{i}:sub_entry")
                continue
            if ent.table == "phrases" and ent.raw["kind"] == "label" and (heading != "diagnosis" or u["form"] != "label"):
                probs.append(f"op{i}:label_position")
                continue
            want = sub_out(ent.table, ent, span, op["a"], audience, text)
            if want is None:
                probs.append(f"op{i}:sub_not_an_entry_form")
            elif want != op.get("out"):
                probs.append(f"op{i}:sub_out_differs")
        elif op["op"] == "drop":
            rule = op.get("rule")
            if rule == "resus_conflict":
                if not (heading == "treatment" and op.get("entry") is None and op["a"] == 0 and op["b"] == len(text)
                        and _norm(text) == RESUS_CONFLICT_TEXT and u["form"] in ("item", "prose")):
                    probs.append(f"op{i}:resus_conflict")
                continue
            why = drop_problem(text, op["a"], op["b"], op.get("unit"), rule, op.get("entry"), tables,
                               heading, u["form"])
            if why:
                probs.append(f"op{i}:drop_{why}")
        else:
            probs.append(f"op{i}:kind")
    return probs


# =================================================================================================
# the step
# =================================================================================================

def _check_input(inp) -> dict:
    if not isinstance(inp, dict) or "revision" in inp:
        raise RenderRefusal("bad_input")
    try:
        return CP7._check_input(inp)
    except StepError as exc:
        raise RenderRefusal(exc.code) from None


def _render(inp: dict, tables: Tables) -> tuple[dict, list[dict], dict]:
    audience = inp["audience"]
    lines_out: dict[str, list[str]] = {h: [] for h in PROSE_C}
    sources: dict[str, list[str]] = {h: [] for h in PROSE_C}
    ledger_lines, drops, unknown = [], [], []
    subs_by: Counter = Counter()
    drops_by: Counter = Counter()
    for sec in inp["sections"]:
        heading = sec["heading"]
        ch = C_OF[heading]
        for u in split_lines(sec):
            ops, c_line = render_line(u, heading, audience, tables)
            v = verify_line(u, c_line, ops, tables)
            if v:
                raise RenderRefusal("meaning_token_lost" if v == ["protected_lost"] else "accounting_failed", _where(u))
            if authorise_ops(u, heading, audience, ops, tables):
                raise RenderRefusal("op_not_authorised", _where(u))
            idx = None
            if c_line is not None:
                idx = len(lines_out[ch])
                lines_out[ch].append(c_line)
                if sec["section_id"] not in sources[ch]:
                    sources[ch].append(sec["section_id"])
            for op in ops:
                if op["op"] == "drop":
                    drops_by[op["rule"]] += 1
                    drops.append({"rule": op["rule"], "entry": op["entry"], **_where(u),
                                  "text": u["text"][op["a"]:op["b"]]})
                elif op["op"] == "sub":
                    subs_by[op["table"]] += 1
                elif op["op"] == "copy":
                    for _s, _e, w in _words(u["text"], op["a"], op["b"]):
                        lw = w.lower()
                        if w.isalpha() and lw not in tables.general_words and lw not in MN._STOP \
                                and lw not in FILLERS:
                            unknown.append({**_where(u), "word": w})
            ledger_lines.append({**_where(u), "form": u["form"], "label": u["label"], "marker": u["marker"],
                                 "c_heading": ch, "c_line": idx, "ops": ops})
    if not lines_out["why_in_hospital"]:
        raise RenderRefusal("reason_dropped")
    wire_sections = []
    for h in ("why_in_hospital", "what_we_did", "medicines", "next_steps", "safety_net"):
        if h in CP7.BOXES:
            wire_sections.append({"heading": h, "source_ids": [], "text": CP7.BOXES[h]})
        elif lines_out[h]:
            lead = tables.lead_ins[(h, audience)]
            body = ([lead] if lead else []) + lines_out[h]
            wire_sections.append({"heading": h, "source_ids": list(sources[h]), "text": "\n".join(body)})
    wire = {"sections": wire_sections}
    ledger = {"lines": ledger_lines, "drops": drops, "unknown_terms": unknown,
              "counts": {"lines": len(ledger_lines),
                         "rendered": sum(1 for x in ledger_lines if x["c_line"] is not None),
                         "dropped_whole": sum(1 for x in ledger_lines if x["c_line"] is None),
                         "drops_by_rule": {r: drops_by[r] for r in DROP_RULES if drops_by[r]},
                         "subs_by_table": {t: subs_by[t] for t in SUB_TABLES if subs_by[t]},
                         "unknown_terms": len(unknown)}}
    return wire, ledger_lines, ledger


def render_patient(inp: dict, tables: Tables | None = None) -> dict:
    """Step 7's input -> PART C in step 7's output contract (+ renderer, ledger). Raises RenderRefusal."""
    inp = _check_input(inp)
    tables = tables if tables is not None else default_tables()
    wire, _lines, ledger = _render(inp, tables)
    if shape_errors(wire, record_patient_version_schema()):
        raise RenderRefusal("patient_version_shape_invalid")
    sections, checks, counts = CP7.check_patient_version(wire, inp)
    return {
        "sections": sections,
        "checks": checks,
        "counts": counts,
        "audience": inp["audience"],
        "revised": False,
        "renderer": {"name": RENDERER_NAME, "version": RENDERER_VERSION, "module_sha256": module_sha256(),
                     "tables": dict(sorted(tables.sha256.items())), "tables_status": tables.status},
        "ledger": ledger,
    }


def audit(inp: dict, output: dict, tables: Tables) -> list[str]:
    """Re-check a SAVED renderer output against its input and the frozen tables, trusting nothing in
    the output: lines rebuilt from the input; each ledger line matched to them; partition,
    reproduction, protected tokens and op authorisation per line; each C line equal to the saved C
    section's line; the C sections rebuilt from the ledger; then the whole output re-rendered and
    compared. [] = clean. Used by the step-4 go/stop evaluator."""
    probs: list[str] = []
    try:
        inp = _check_input(inp)
    except RenderRefusal as exc:
        return [f"input:{exc.code}"]
    audience = inp["audience"]
    try:
        led = output["ledger"]["lines"]
        saved = {s["heading"]: s for s in output["sections"]}
    except (KeyError, TypeError):
        return ["output_shape"]
    rebuilt = []
    try:
        for sec in inp["sections"]:
            rebuilt += [(sec["heading"], u) for u in split_lines(sec)]
    except RenderRefusal as exc:
        return [f"input:{exc.code}"]
    if len(rebuilt) != len(led):
        return ["ledger_line_count"]
    c_lines: dict[str, list[str]] = {h: [] for h in PROSE_C}
    for (heading, u), x in zip(rebuilt, led):
        tag = f"{u['source_id']}:{u['line']}:{u['sentence']}"
        if not isinstance(x, dict) or any(x.get(k) != u[k] for k in ("source_id", "line", "sentence", "form", "marker")):
            probs.append(f"{tag}:ledger_line_mismatch")
            continue
        ops = x.get("ops") or []
        sec = saved.get(C_OF[heading])
        sec_lines = sec["text"].split("\n") if sec else []
        lead = tables.lead_ins.get((C_OF[heading], audience)) or ""
        body = sec_lines[1:] if (lead and sec_lines and sec_lines[0] == lead) else sec_lines
        idx = x.get("c_line")
        c_line = None
        if idx is not None:
            if not isinstance(idx, int) or not 0 <= idx < len(body):
                probs.append(f"{tag}:c_line_index")
                continue
            c_line = body[idx]
            c_lines[C_OF[heading]].append(c_line)
        for p in verify_line(u, c_line, ops, tables):
            probs.append(f"{tag}:{p}")
        try:
            for p in authorise_ops(u, heading, audience, ops, tables):
                probs.append(f"{tag}:{p}")
        except (KeyError, TypeError, IndexError):
            probs.append(f"{tag}:op_shape")
    for h in PROSE_C:
        sec = saved.get(h)
        lead = tables.lead_ins.get((h, audience)) or ""
        want = "\n".join(([lead] if lead else []) + c_lines[h]) if c_lines[h] else None
        have = sec["text"] if sec else None
        if want != have:
            probs.append(f"{h}:section_text_differs")
    try:
        again = render_patient(inp, tables)
    except RenderRefusal as exc:
        probs.append(f"rerender_refused:{exc.code}")
    else:
        if json.dumps(again, sort_keys=True) != json.dumps(output, sort_keys=True):
            probs.append("rerender_differs")
    return probs
