"""Steps 8a and 8b — assemble (ADR-009 step table row 8; [A1]-[A4], [A6]; WS1a step
build piece 4; CSO decisions of 5 Nov 2026, A1-F2 with the 14:10 and 14:16 amendments).

Code, no model, no AWS. Fixed wording below is CSO-approved (5 Nov 2026); change it
only with a new CSO decision.

  8a assemble_clinical  PART A and PART B as INTERNAL candidates: the header and
                        B's letterhead, the contradictions block (S5), the three
                        boxes filled by code ([A1], [A2]), step 6's prose under
                        fixed labels, the fixed lines (held back, language,
                        "(source not verified — check the notes)" [A4]), and
                        reviewer warnings for A and B.
  8b assemble_patient   the RELEASE CHECK. Re-derives 8a from the same inputs (a
                        stale or altered 8a fails), assembles PART C with
                        render.assemble_part_c UNCHANGED (piece 3, R5b) under a
                        draft-for-review banner, runs every bundle check on A, B
                        and C and the safety-net gate on assembled A and C
                        ([A3]), and returns the three documents, the gate status,
                        the warnings and a sha256 per document — or nothing.

Release contract (CSO, 5 Nov 2026): no document bundle is released on failure;
intermediate drafts may remain in the restricted trace. 8a's documents are
candidates, never a result: only a successful 8b returns a bundle. Every check
failure raises a StepError with a fixed code and returns nothing; any other
exception inside 8b is converted to `assembly_check_error` — a check that crashes
must not let a bundle through (fail securely). Item 5 (orchestrator) owns
external release and must test that an 8b failure exposes no draft through
polling, downloads or error payloads (HAZ-19 is the control; those tests are its
evidence).

8a's own structure checks (required headings, boxes) are defence in depth; they are
not a timing guarantee — 8a may run beside step 7. Stopping before step 7 on a step-6
structural failure is item 5's upstream pre-flight.

PART C is a DRAFT FOR CLINICIAN REVIEW (step 7 decision S1, 4 Nov 2026): item 3
is NOT MET and nothing here claims C is faithful to A. The step-7 meaning flags
(pipeline/meaning.py) are shown to the reviewer as warnings, not fixed.

Fail-closed codes: bad_input, required_heading_missing, box_structure,
resus_text_outside, clinical_stale, contradictions_block_mismatch,
placeholder_unfilled, gate_cannot_read_a, translation_marker_mismatch,
safety_net_gate_failed, assembly_check_error.

Warnings carry fixed words, part (A / B / C), heading and a 1-based sentence number
(or null for a section- or document-level flag) — never text.
"""

from __future__ import annotations

import hashlib
import re

from . import meaning as MN
from . import render as RN
from .compose import (A_HEADINGS, B_HEADINGS, BOXES as BOXES_6, HELD_MARK, RESUS_CONFLICT_TEXT,
                      held_back, is_resus)
from .compose_patient import BOXES as BOXES_7, _DOSE, audience_for, patient_input, units
from .errors import StepError
from .guard import RULES as GUARD_RULES
from .route import FLAGGED_NOT_QUOTED
from .safety_net_gate import _ADVICE_HEADING, advice_block, check as gate_check
from .schemas import FIELD_NAMES

# --- fixed wording (CSO, 5 Nov 2026) ----------------------------------------------
TITLE_A = "PART A — CLINICAL SUMMARY"
TITLE_B = "PART B — LETTER TO THE GP"
IDENTIFIERS_LINE = "PATIENT IDENTIFIERS: not extracted — confirm from the patient record"   # ADR-009 W2
NOT_DOCUMENTED = "Not documented"
NOT_EXTRACTED = "Not extracted — check the notes"          # B1: documented, but step 2 gave no item
INFERRED_MARK = "(inferred — not documented, confirm)"
STATUS_INCONSISTENT = "(documentation status inconsistent — check the notes)"   # CSO 5 Nov 14:40
UNVERIFIED_MARK = "(source not verified — check the notes)"                    # [A4]
HELD_BACK_LINE = ("Held back — these facts concern resuscitation and are shown in the "
                  "RESUSCITATION STATUS box.")                                  # B3
LANGUAGE_LINE = ("LANGUAGE: the notes record an interpreter or language need ({ids}) — arrange an "
                 "interpreter; the patient version is marked for translation.")  # B2
CONFLICT_HEAD = "NOTES CONFLICT — requires clarification before relying on these entries:"   # A1
C_UNVERIFIED_NOTE = ("[Check before handing over: part of this section comes from notes whose "
                     "source was not verified.]")                               # B4
C_BANNER = ("[DRAFT FOR CLINICIAN REVIEW — not checked for faithfulness to PART A. Read it against "
            "PART A and correct it before it is given. {n} review warning{s}.]")  # C1
B_DEAR = "Dear Colleague,"
B_RE = "Re: patient identifiers not extracted — confirm from the patient record"
B_SIGN_OFF = ("Yours sincerely,", "[Responsible clinician — name, grade, contact — to be completed]")

# A / B medicines boxes (B6, B7)
TAG_WORD = {"new": "NEW", "continued": "CONTINUED", "increased": "INCREASED", "decreased": "DECREASED",
            "changed": "CHANGED", "stopped": "STOPPED", "withheld": "WITHHELD"}
NOTE_CONFLICT = "[notes disagree — confirm]"
NOTE_INFERRED = "[inferred from drug history — confirm]"
MED_STATUS = {"not_documented": "Discharge medicines: not documented in these notes",
              "referenced_not_listed": "Discharge medicines: the notes refer to a separate list — attach it",
              "none_required": "No medicines required on discharge (as documented)"}
MED_STATUSES = ("listed",) + tuple(MED_STATUS)
B_NO_CHANGES = "No changes to regular medicines."
B_CONTINUED = "Continued unchanged: {n} medicine{s} — full list in the discharge summary."

HEADER_LABELS = (("age_sex", "AGE / SEX"), ("specialty", "SPECIALTY"),
                 ("legal_status", "LEGAL STATUS"), ("weight", "WEIGHT"))

# Kinds of reviewer warning (C1, D2-D4). Fixed words.
WARNING_KINDS = ("unit_added", "dose_outside_box", "drug_not_in_cited_facts") + tuple(MN.CHECK_OF.values())

INPUT_KEYS_8A = {"facts", "flags", "language", "resus", "reconciliation", "safety_net", "composition",
                 "generated_on"}
INPUT_KEYS_8B = INPUT_KEYS_8A | {"lines", "patient", "clinical"}

_PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LINE_ID = re.compile(r"L\d{3,}")
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{3,}")
_CONFLICT_WORDING = re.compile(r"\bnotes conflict\b", re.IGNORECASE)
# c0.2 only (CSO 5 Nov 2026 14:40): set to False when c0.3 goes live.
G3_ALLOWANCE = True


def _bad(cond: bool) -> None:
    if cond:
        raise StepError("bad_input")


def _ws(text: str) -> str:
    return " ".join(text.split())


def label(heading: str) -> str:
    return heading.replace("_", " ").upper() + ":"


# --- input --------------------------------------------------------------------------

def _check_8a_input(inp) -> None:
    _bad(not isinstance(inp, dict) or set(inp) != INPUT_KEYS_8A)
    try:
        facts, comp = inp["facts"], inp["composition"]
        _bad(not isinstance(facts["fields"], dict) or set(facts["fields"]) != set(FIELD_NAMES))
        _bad(not isinstance(facts["contradictions"], list))
        _bad(not isinstance(comp["sections"], list) or not isinstance(comp["checks"], list))
        _bad(not isinstance(inp["language"], list) or not isinstance(inp["flags"], list))
        _bad(not isinstance(inp["resus"].get("rendered"), str) or inp["resus"].get("route") not in
             ("documented", "documented_but_absent", "absent"))
        _bad(not isinstance(inp["safety_net"].get("part_ab"), str))
        _bad(inp["reconciliation"].get("discharge_status") not in MED_STATUSES
             or not isinstance(inp["reconciliation"].get("reconciliation"), list))
        _bad(not isinstance(inp["generated_on"], str) or not _DATE.fullmatch(inp["generated_on"]))
    except (KeyError, TypeError, AttributeError):
        raise StepError("bad_input") from None


def _check_lines(lines) -> str:
    """The notes, as step 1's line index -> the notes text for the gate. Missing or
    malformed notes are bad_input, never an empty set (an empty set would let the
    gate pass on nothing)."""
    _bad(not isinstance(lines, dict) or not lines)
    _bad(any(not isinstance(k, str) or not _LINE_ID.fullmatch(k) or not isinstance(v, str)
             for k, v in lines.items()))
    text = "\n".join(lines[k] for k in sorted(lines, key=lambda k: int(k[1:])))
    _bad(not text.strip())
    return text


# --- structure (fail closed) ----------------------------------------------------------

def _check_step6_structure(comp: dict) -> None:
    if any(c.get("check") == "required_heading_missing" for c in comp["checks"]):
        raise StepError("required_heading_missing")
    for part in ("part_a", "part_b"):
        for heading, token in BOXES_6.items():
            secs = [s for s in comp["sections"] if s["part"] == part and s["heading"] == heading]
            if len(secs) != 1 or secs[0]["text"].strip() != token or secs[0]["fact_ids"]:
                raise StepError("box_structure")


def check_step6_structure(comp: dict) -> None:
    """Public for item 5's upstream PRE-FLIGHT (orchestrate.py): the same rule 8a applies,
    run on step 6's output BEFORE step 7 starts. Raises required_heading_missing or
    box_structure — the two stay distinct (CSO, 5 Nov 2026). Malformed input -> bad_input."""
    try:
        _check_step6_structure(comp)
    except StepError:
        raise
    except (KeyError, TypeError, AttributeError):
        raise StepError("bad_input") from None


def _check_step7_structure(step7: dict) -> None:
    for heading, token in BOXES_7.items():
        secs = [s for s in step7["sections"] if s["heading"] == heading]
        if len(secs) != 1 or secs[0]["text"].strip() != token or secs[0]["source_ids"]:
            raise StepError("box_structure")


def contradictions(facts: dict, flags: list) -> list[dict]:
    """Step 2's recorded contradictions -> one block line each (A1, A2), in order.
    kind: quotes (verified: verbatim quotes with line IDs) | value_unverified (step 2's
    wording + [A4] marker) | resus_pointer (the pre-ruled sentence, no quotes) |
    flagged (a cited line step 1 flagged, or wording / a quote step 1's rules would flag:
    never quoted — route.py's wording). Identity is kept: one row per contradiction."""
    flagged = {f["line"] for f in flags}
    out = []
    for c in facts["contradictions"]:
        cites = c.get("cites") or []
        texts = [c["value"]] + [ci.get("quote") or "" for ci in cites]
        ids = [lid for ci in cites for lid in ci.get("lines") or []]
        if any(is_resus(t) for t in texts):
            kind, line = "resus_pointer", f"- {RESUS_CONFLICT_TEXT}"
        elif any(lid in flagged for lid in ids) or _instruction_like(texts):
            # Flagged text is never copied (decision 4, 2 Oct 2026) — neither as a quote
            # nor through the unverified-wording fallback: step 1's own rules are run on
            # step 2's wording and every quote too (CSO, 5 Nov 2026 14:40).
            kind = "flagged"
            line = f"- [{', '.join(ids)}] {FLAGGED_NOT_QUOTED}" if ids else f"- {FLAGGED_NOT_QUOTED}"
        elif c.get("citation_status") == "verified" and cites and all(ci.get("quote") for ci in cites):
            kind = "quotes"
            line = "- " + " / ".join(f'[{", ".join(ci["lines"])}] "{_ws(ci["quote"])}"' for ci in cites)
        else:
            kind, line = "value_unverified", f"- {_ws(c['value'])} {UNVERIFIED_MARK}"
        out.append({"fact_id": c["fact_id"], "kind": kind, "line": line})
    return out


def _instruction_like(texts: list[str]) -> bool:
    return any(p.search(t) for t in texts for p in GUARD_RULES.values())


def conflict_block(rows: list[dict]) -> str | None:
    return "\n".join([CONFLICT_HEAD] + [r["line"] for r in rows]) if rows else None


def _scan_prose_6(comp: dict, rows: list[dict]) -> int:
    """D1 (CSO 5 Nov, 14:16 and 14:40): every model-written step-6 prose section, through
    the structured fields, before any code block is inserted. The fixed sentences are NOT
    whitelisted. One exception, for c0.2 only (it prescribes the sentence): in PART A
    `treatment`, a sentence that is EXACTLY RESUS_CONFLICT_TEXT, standing alone, in a
    section that cites a contradiction 8a renders as a resus pointer, is set aside; every
    other sentence is scanned. No exception in PART B or C. Removed with c0.3.
    Returns how many sentences the exception set aside (reported, A5 preview)."""
    pointer_ids = {r["fact_id"] for r in rows if r["kind"] == "resus_pointer"}
    used = 0
    for s in comp["sections"]:
        if s["heading"] in BOXES_6:
            continue
        sents = MN.sentences(s["text"])
        if (G3_ALLOWANCE and s["part"] == "part_a" and s["heading"] == "treatment"
                and set(s["fact_ids"]) & pointer_ids):
            kept = [x for x in sents if x != RESUS_CONFLICT_TEXT]
            used += len(sents) - len(kept)
            sents = kept
        if any(is_resus(x) for x in sents):
            raise StepError("resus_text_outside")
    return used


def _scan_prose_7(step7: dict) -> None:
    for s in step7["sections"]:
        if s["heading"] not in BOXES_7 and is_resus(s["text"]):
            raise StepError("resus_text_outside")


# --- boxes ---------------------------------------------------------------------------

def _join(*parts) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


def med_line(e: dict) -> str:
    """One 5a entry in clinician wording (B6)."""
    tag = e.get("tag")
    _bad(tag not in TAG_WORD)
    name = (e.get("drug") or "").strip()
    _bad(not name)
    what = _join(name[:1].upper() + name[1:], e.get("dose"), e.get("route"), e.get("frequency"))
    change = TAG_WORD[tag]
    prev = e.get("previous") or {}
    was = _join(prev.get("dose"), prev.get("frequency"))
    if tag in RN.WITH_PREVIOUS and was:
        change += f" (was {was})"
    notes = []
    if e.get("conflict"):
        notes.append(NOTE_CONFLICT)
    if e.get("basis") == "inferred_from_dh":
        notes.append(NOTE_INFERRED)
    if e.get("citation_status") != "verified":
        notes.append(UNVERIFIED_MARK)
    return "- " + _join(f"{what} — {change}", *notes)


def _status_line(recon: dict) -> str | None:
    status, entries = recon["discharge_status"], recon["reconciliation"]
    if status == "listed" and not entries:
        return MED_STATUS["not_documented"]
    return MED_STATUS.get(status)


def meds_box_a(recon: dict) -> str:
    head = _status_line(recon)
    return "\n".join(([head] if head else []) + [med_line(e) for e in recon["reconciliation"]])


def _clean_continued(e: dict) -> bool:
    return (e.get("tag") == "continued" and not e.get("conflict") and e.get("basis") != "inferred_from_dh"
            and e.get("citation_status") == "verified")


def meds_box_b(recon: dict) -> str:
    """B7 as corrected (CSO 5 Nov): changes only, and only from KNOWN data. A missing or
    referred-out list keeps its status line; the continued count and "No changes" are
    printed only for a listed, non-empty list; an entry with any doubt is always shown."""
    status, entries = recon["discharge_status"], recon["reconciliation"]
    head = _status_line(recon)
    if status != "listed" or not entries:
        return "\n".join(([head] if head else []) + [med_line(e) for e in entries])
    clean = [e for e in entries if _clean_continued(e)]
    shown = [e for e in entries if not _clean_continued(e)]
    out = [med_line(e) for e in shown]
    if not shown:
        out.append(B_NO_CHANGES)
    if clean:
        out.append(B_CONTINUED.format(n=len(clean), s="" if len(clean) == 1 else "s"))
    return "\n".join(out)


# --- header ------------------------------------------------------------------------------

def field_value(field: dict, held: set[str]) -> str:
    """B1. Values as written (no reformatting: a year can never be added here)."""
    items = field["items"]
    kept = [i for i in items if i["fact_id"] not in held]
    if items and not kept:
        return HELD_MARK
    if not kept:
        return NOT_DOCUMENTED if field["status"] == "not_documented" else NOT_EXTRACTED
    vals = [_join(_ws(i["value"]), UNVERIFIED_MARK if i.get("citation_status") != "verified" else "")
            for i in kept]
    out = "; ".join(vals)
    # inferred_flagged -> the inferred marker. Items under a not_documented status are a
    # step-2 inconsistency (step 3's field_check items_on_not_documented), not an
    # inference: shown with their own marker (CSO, 5 Nov 2026 14:40), never as documented.
    if field["status"] == "inferred_flagged":
        out = _join(out, INFERRED_MARK)
    elif field["status"] == "not_documented":
        out = _join(out, STATUS_INCONSISTENT)
    return out


def language_line(language: list) -> str | None:
    ids = [row["line"] for row in language if isinstance(row, dict) and row.get("line")]
    _bad(bool(language) and not ids)
    return LANGUAGE_LINE.format(ids=", ".join(ids)) if ids else None


# --- warnings (C1, D2-D4) ----------------------------------------------------------------

def _drug_keys(recon: dict) -> list[str]:
    keys = []
    for e in recon["reconciliation"]:
        m = _WORD.search(e.get("drug") or "")
        if m and m.group(0).lower() not in keys:
            keys.append(m.group(0).lower())
    return keys


def _names(sentence: str, keys: list[str]) -> list[str]:
    low = sentence.lower()
    return [k for k in keys if re.search(rf"\b{re.escape(k)}\b", low)]


def _warn(part, heading, kind, n):
    return {"part": part, "heading": heading, "kind": kind, "sentence": n}


def warnings_ab(facts: dict, comp: dict, recon: dict) -> list[dict]:
    held = held_back(facts)
    values = {i["fact_id"]: i["value"] for n in FIELD_NAMES for i in facts["fields"][n]["items"]
              if i["fact_id"] not in held}
    values.update({c["fact_id"]: c["value"] for c in facts["contradictions"]})
    input_units = units("\n".join(values.values()))
    keys = _drug_keys(recon)
    out = []
    for s in comp["sections"]:
        if s["heading"] in BOXES_6:
            continue
        part = "A" if s["part"] == "part_a" else "B"
        cited = " ".join(values.get(f, "") for f in s["fact_ids"]).lower()
        for n, sent in enumerate(MN.sentences(s["text"]), start=1):
            if units(sent) - input_units:
                out.append(_warn(part, s["heading"], "unit_added", n))
            named = _names(sent, keys)
            if named and _DOSE.search(sent):
                out.append(_warn(part, s["heading"], "dose_outside_box", n))
            if any(not re.search(rf"\b{re.escape(k)}\b", cited) for k in named):
                out.append(_warn(part, s["heading"], "drug_not_in_cited_facts", n))
    return out


def warnings_c(comp: dict, step7: dict, recon: dict) -> list[dict]:
    given = {s["section_id"]: s for s in patient_input(comp)["sections"]}
    keys = _drug_keys(recon)
    out, prose = [], []
    for s in step7["sections"]:
        if s["heading"] in BOXES_7:
            continue
        prose.append(s["text"])
        sents = MN.sentences(s["text"])
        cited = [given[i] for i in s["source_ids"] if i in given] or list(given.values())
        for kind, _token, shown in MN.section_flags(s["text"], cited):
            n = sents.index(shown) + 1 if kind != "qualifier" and shown in sents else None
            out.append(_warn("C", s["heading"], MN.CHECK_OF[kind], n))
        for n, sent in enumerate(sents, start=1):
            if _names(sent, keys) and _DOSE.search(sent):
                out.append(_warn("C", s["heading"], "dose_outside_box", n))
    for _sid, _n, _line in MN.diagnoses_unreached(list(given.values()), "\n".join(prose)):
        out.append(_warn("C", "why_in_hospital", MN.CHECK_OF["diagnosis"], None))
    return out


# --- 8a ----------------------------------------------------------------------------------

def _resus_in_b(inp: dict, rows: list[dict]) -> dict:
    """B9 (b) + the pointer rule: B shows the RESUS box when the notes document
    anything (route documented / documented_but_absent), when a GP action was held back
    (G2A), or when B's contradictions block points to the box (a pointer needs a
    destination)."""
    reasons = []
    if inp["resus"]["route"] in ("documented", "documented_but_absent"):
        reasons.append("route")
    if any(h["field"] == "gp_actions" for h in inp["composition"].get("held_back") or []):
        reasons.append("gp_action_held_back")
    if any(r["kind"] == "resus_pointer" for r in rows):
        reasons.append("conflict_pointer")
    return {"shown": bool(reasons), "reasons": reasons}


def _render_sections(part: str, headings, comp: dict, held_headings: set, fill: dict) -> list[str]:
    out = []
    for h in headings:
        secs = [s for s in comp["sections"] if s["part"] == part and s["heading"] == h]
        if not secs and h in held_headings:
            out += [label(h), HELD_BACK_LINE, ""]
            continue
        for s in secs:
            if h in BOXES_6:
                if fill.get(h) is None:                 # B's RESUS box not shown (B9)
                    continue
                body = fill[h]
                out += ([body, ""] if h == "resuscitation_status" else [label(h), body, ""])
                continue
            if part == "part_b" and h == "opening":
                text = s["text"].strip()
            else:
                text = label(h) + "\n" + s["text"].strip()
            if s.get("unverified_fact_ids"):
                text += "\n" + UNVERIFIED_MARK
            out += [text, ""]
    return out


def build_clinical(inp: dict) -> dict:
    """8a's pure core (8b calls it again to prove 8a's candidates are current)."""
    facts, comp, recon = inp["facts"], inp["composition"], inp["reconciliation"]
    _check_step6_structure(comp)
    rows = contradictions(facts, inp["flags"])
    g3_used = _scan_prose_6(comp, rows)              # D1, before any code block goes in

    held = set(held_back(facts))
    fields = facts["fields"]
    held_heads = {(c["part"], c["heading"]) for c in comp["checks"] if c.get("check") == "section_held_back"}
    block = conflict_block(rows)
    resus_b = _resus_in_b(inp, rows)
    lang = language_line(inp["language"])

    # PART A
    a = [TITLE_A, "", IDENTIFIERS_LINE]
    v = {n: field_value(fields[n], held) for n in FIELD_NAMES}
    a.append(f"AGE / SEX: {v['age_sex']}")
    a.append(f"ADMITTED: {v['admission_date']}    DISCHARGED: {v['discharge_date']}")
    a += [f"SPECIALTY: {v['specialty']}", f"LEGAL STATUS: {v['legal_status']}", f"WEIGHT: {v['weight']}"]
    if lang:
        a.append(lang)
    a.append("")
    if block:
        a += [block, ""]
    fill_a = {"medications": meds_box_a(recon), "resuscitation_status": inp["resus"]["rendered"],
              "safety_net": inp["safety_net"]["part_ab"]}
    a += _render_sections("part_a", A_HEADINGS, comp, {h for p, h in held_heads if p == "part_a"}, fill_a)

    # PART B
    b = [TITLE_B, "", f"Date: {inp['generated_on']}", B_DEAR, B_RE,
         f"Admitted {v['admission_date']} · Discharged {v['discharge_date']} · {v['specialty']}", ""]
    fill_b = {"medications": meds_box_b(recon),
              "resuscitation_status": inp["resus"]["rendered"] if resus_b["shown"] else None,
              "safety_net": inp["safety_net"]["part_ab"]}
    held_b = {h for p, h in held_heads if p == "part_b"}
    b += _render_sections("part_b", ("opening",), comp, held_b, fill_b)
    if block:
        b += [block, ""]
    b += _render_sections("part_b", tuple(h for h in B_HEADINGS if h != "opening"), comp, held_b, fill_b)
    b += list(B_SIGN_OFF)

    part_a = "\n".join(a).rstrip() + "\n"
    part_b = "\n".join(b).rstrip() + "\n"
    warnings = warnings_ab(facts, comp, recon)
    return {
        "part_a": part_a, "part_b": part_b,
        "contradictions": [{"fact_id": r["fact_id"], "kind": r["kind"]} for r in rows],
        "resus_in_b": resus_b,
        "warnings": warnings,
        "counts": {
            "contradictions": len(rows),
            "contradictions_by_kind": {k: sum(r["kind"] == k for r in rows)
                                       for k in ("quotes", "value_unverified", "resus_pointer", "flagged")},
            "sections_held_back": len(held_heads),
            "sections_unverified": sum(1 for s in comp["sections"]
                                       if s["heading"] not in BOXES_6 and s.get("unverified_fact_ids")),
            "language_line": lang is not None,
            "d1_g3_allowance_used": g3_used,
            "warnings": len(warnings),
        },
    }


def assemble_clinical(inp: dict) -> dict:
    """Step 8a. {facts, flags, language, resus, reconciliation, safety_net, composition,
    generated_on} -> PART A and PART B candidates (never released by themselves)."""
    _check_8a_input(inp)
    try:
        return build_clinical(inp)
    except StepError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError):
        raise StepError("bad_input") from None


# --- 8b ----------------------------------------------------------------------------------

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _bundle(inp: dict) -> dict:
    _bad(not isinstance(inp, dict) or set(inp) != INPUT_KEYS_8B)
    notes = _check_lines(inp["lines"])
    upstream = {k: inp[k] for k in INPUT_KEYS_8A}
    _check_8a_input(upstream)
    clinical, step7 = inp["clinical"], inp["patient"]
    _bad(not isinstance(clinical, dict) or not isinstance(step7, dict)
         or not isinstance(step7.get("sections"), list) or step7.get("audience") not in RN.AUDIENCES)

    # 1. 8a re-derived from EVERY rendering input (generated_on included) and compared
    #    whole — A, B, contradictions, warnings, counts. This proves rendering
    #    consistency; where the inputs came from remains trace evidence.
    expected = build_clinical(upstream)
    if clinical != expected:
        raise StepError("clinical_stale")
    part_a, part_b = clinical["part_a"], clinical["part_b"]

    # 2. Model prose (D1) and box structure, C side.
    _check_step7_structure(step7)
    _scan_prose_7(step7)
    _bad(step7["audience"] != audience_for(upstream["composition"]["age_group"]))

    # 3. Contradictions block: exactly once in A and in B, as rendered from the facts.
    block = conflict_block(contradictions(upstream["facts"], upstream["flags"]))
    for doc in (part_a, part_b):
        if block is None:
            if CONFLICT_HEAD in doc:
                raise StepError("contradictions_block_mismatch")
        elif doc.count(block) != 1 or doc.count(CONFLICT_HEAD) != 1:
            raise StepError("contradictions_block_mismatch")

    # 4. PART C: render.assemble_part_c UNCHANGED, on step 7's sections with the B4 note
    #    appended where a section draws on an A section with unverified facts.
    unverified_a = {s["section_id"] for s in upstream["composition"]["sections"]
                    if s.get("unverified_fact_ids")}
    marked = dict(step7)
    marked["sections"] = [
        dict(s, text=s["text"].rstrip() + "\n" + C_UNVERIFIED_NOTE)
        if s["heading"] not in BOXES_7 and set(s["source_ids"]) & unverified_a else s
        for s in step7["sections"]]
    leaflet = RN.assemble_part_c(marked, upstream["reconciliation"], upstream["safety_net"],
                                 upstream["language"])
    warnings = clinical["warnings"] + warnings_c(upstream["composition"], step7, upstream["reconciliation"])
    n_c = sum(1 for w in warnings if w["part"] == "C")
    part_c = C_BANNER.format(n=n_c, s="" if n_c == 1 else "s") + "\n\n" + leaflet

    # 5. Placeholders: none left anywhere.
    if any(_PLACEHOLDER.search(d) for d in (part_a, part_b, part_c)):
        raise StepError("placeholder_unfilled")

    # 6. The gate must be able to read A's advice field: one SAFETY NET heading, and the
    #    field it reads is exactly 5b's part_ab — else the A check would pass on nothing.
    if (len(_ADVICE_HEADING.findall(part_a)) != 1
            or _ws(advice_block(part_a)) != _ws(upstream["safety_net"]["part_ab"])):
        raise StepError("gate_cannot_read_a")

    # 7. The translation marker promised in A (B2) is present in C, and only then.
    has_lang = bool(upstream["language"])
    in_a = any(line.startswith("LANGUAGE: ") for line in part_a.splitlines())
    in_c = RN.TRANSLATION_HEADER[step7["audience"]] in part_c
    if not (has_lang == in_a == in_c):
        raise StepError("translation_marker_mismatch")

    # 8. The safety-net gate on assembled A and C, anchored to the notes ([A3]).
    g = gate_check(notes, part_a, part_c)
    if not g.ok:
        raise StepError("safety_net_gate_failed")

    return {
        "part_a": part_a, "part_b": part_b, "part_c": part_c,
        "gate": {"status": g.status, "ok": g.ok},
        "warnings": warnings,
        "hashes": {"part_a": _sha(part_a), "part_b": _sha(part_b), "part_c": _sha(part_c)},
        "counts": {"warnings_a": sum(w["part"] == "A" for w in warnings),
                   "warnings_b": sum(w["part"] == "B" for w in warnings),
                   "warnings_c": n_c,
                   "c_sections_unverified": sum(1 for s in marked["sections"]
                                                if s["text"].endswith(C_UNVERIFIED_NOTE))},
    }


def assemble_patient(inp: dict) -> dict:
    """Step 8b. 8a's input + {lines, patient (step 7), clinical (8a)} -> the released
    bundle {part_a, part_b, part_c, gate, warnings, hashes, counts} — or a StepError
    and nothing. Fail securely: an exception inside any check fails the job."""
    try:
        return _bundle(inp)
    except StepError:
        raise
    except Exception:  # noqa: BLE001 — deliberate: a crashing check must not release a bundle
        raise StepError("assembly_check_error") from None
