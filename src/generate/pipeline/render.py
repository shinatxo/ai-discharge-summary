"""PART C rendering — the code that fills step 7's boxes and frames the leaflet
(ADR-009 8b; WS1a step build piece 3, CSO decisions of 1 Nov 2026).

Pure functions, no model, no AWS. Written in piece 3 (R5b) so the box the
batch measures is the box that ships: 8b (item 4) calls these unchanged and
adds the runtime gate, hashes and the audit flip. PART A / B rendering (8a)
is NOT here yet — item 4.

  medicines_box(recon)                    {{MEDICATIONS}} in C, from 5a (D2 M1, R5, R5a)
  translation_header(language, audience)  the fixed C7 header, from 3b (D3 T1, R4)
  assemble_part_c(step7, recon, sn, language)
                                          title + header + step 7's sections in order,
                                          code headings, both boxes filled

Wording here is fixed text, decided by the CSO on 1 Nov 2026 (R4, R5, R5a);
change it only with a new CSO decision. Square-bracketed lines and notes are
addressed to the reviewing clinician, not the patient: each names a gap to
close before the leaflet is handed over.

The title and section headings (titles below) are presentation, chosen by
Claude in round 3 and listed in the round-3 summary for the CSO.
"""

from __future__ import annotations

import re

from .errors import StepError

AUDIENCES = ("adult", "parent_or_carer")

TITLES = {"adult": "Your discharge information",
          "parent_or_carer": "Discharge information for parents and carers"}
HEADINGS = {
    "adult": {"why_in_hospital": "Why you were in hospital", "what_we_did": "What we did",
              "medicines": "Your medicines", "next_steps": "What happens next",
              "safety_net": "Advice and getting help"},
    "parent_or_carer": {"why_in_hospital": "Why your child was in hospital", "what_we_did": "What we did",
                        "medicines": "Your child's medicines", "next_steps": "What happens next",
                        "safety_net": "Advice and getting help"},
}
ORDER = ("why_in_hospital", "what_we_did", "medicines", "next_steps", "safety_net")
BOX_TOKENS = {"medicines": "{{MEDICATIONS}}", "safety_net": "{{SAFETY_NET}}"}

# R4 (H1), CSO 1 Nov 2026. Carries no signposting token (111 / 999 / A&E).
TRANSLATION_HEADER = {
    "adult": ("FOR TRANSLATION — do not hand to the patient untranslated. The notes record an "
              "interpreter or language need. Arrange a translated copy, or go through this leaflet "
              "with an interpreter, before it is given."),
    "parent_or_carer": ("FOR TRANSLATION — do not hand to the parent or carer untranslated. The notes "
                        "record an interpreter or language need. Arrange a translated copy, or go through "
                        "this leaflet with an interpreter, before it is given."),
}

# R5, CSO 1 Nov 2026 — the change wording per 5a tag.
TAG_TEXT = {"new": "new medicine", "continued": "keep taking as before",
            "increased": "dose increased", "decreased": "dose lowered", "changed": "changed",
            "stopped": "stop taking this", "withheld": "do not take this for now"}
WITH_PREVIOUS = ("increased", "decreased", "changed")
NAME_ONLY = ("stopped", "withheld")

NOTE_CONFLICT = "[dose to be confirmed — the notes disagree]"
NOTE_INFERRED = "[to be confirmed — in the drug history, not on the discharge list]"
NOTE_UNVERIFIED = "[to be confirmed — source not verified]"
STATUS_LINES = {
    "not_documented": "[Medicines: not recorded in these notes — add them before this leaflet is given.]",
    "referenced_not_listed": ("[The notes refer to a separate discharge medicines list — add it before this "
                              "leaflet is given.]"),
    "none_required": "The notes say no medicines are needed on discharge.",
}

# R5a, CSO 1 Nov 2026 — shorthand, matched on the WHOLE field (case-insensitive);
# anything else is copied as written.
FREQUENCY = {"od": "once a day", "bd": "twice a day", "tds": "three times a day",
             "qds": "four times a day", "on": "at night", "nocte": "at night",
             "prn": "when needed", "mane": "in the morning"}
_DAYS = re.compile(r"(\d+)/7")
_WEEKS = re.compile(r"(\d+)/52")
ROUTE_OMIT = ("oral", "po", "by mouth")
ROUTE = {"subcut": "injection under the skin", "sc": "injection under the skin",
         "s/c": "injection under the skin", "subcutaneous": "injection under the skin",
         "iv": "into a vein", "intravenous": "into a vein"}


def frequency_text(freq: str | None) -> str | None:
    if not freq or not freq.strip():
        return None
    f = freq.strip()
    if f.casefold() in FREQUENCY:
        return FREQUENCY[f.casefold()]
    m = _DAYS.fullmatch(f)
    if m:
        return f"for {m.group(1)} day" + ("s" if m.group(1) != "1" else "")
    m = _WEEKS.fullmatch(f)
    if m:
        return f"for {m.group(1)} week" + ("s" if m.group(1) != "1" else "")
    return f


def route_text(route: str | None) -> str | None:
    if not route or not route.strip():
        return None
    r = route.strip()
    if r.casefold() in ROUTE_OMIT:
        return None
    return ROUTE.get(r.casefold(), r)


def _name(drug: str) -> str:
    d = (drug or "").strip()
    return d[:1].upper() + d[1:]


def _join(*parts) -> str:
    return " ".join(p for p in parts if p)


def medicine_line(e: dict) -> str:
    """One 5a reconciliation entry -> one line of the C medicines box."""
    tag = e["tag"]
    if tag not in TAG_TEXT:
        raise StepError("bad_input")
    name = _name(e["drug"])
    if tag in NAME_ONLY:
        line = f"{name} — {TAG_TEXT[tag]}"
    else:
        what = _join(name, e.get("dose"), route_text(e.get("route")), frequency_text(e.get("frequency")))
        change = TAG_TEXT[tag]
        prev = e.get("previous") or {}
        was = _join(prev.get("dose"), frequency_text(prev.get("frequency")))
        if tag in WITH_PREVIOUS and was:
            change += f" (was {was})"
        line = f"{what} — {change}"
    notes = []
    if e.get("conflict"):
        notes.append(NOTE_CONFLICT)
    if e.get("basis") == "inferred_from_dh":
        notes.append(NOTE_INFERRED)
    if e.get("citation_status") != "verified":
        notes.append(NOTE_UNVERIFIED)
    return "- " + _join(line, *notes)


def medicines_box(recon: dict) -> str:
    """5a's output -> the text of C's {{MEDICATIONS}} box (D2 M1)."""
    try:
        entries = recon["reconciliation"]
        status = recon["discharge_status"]
        lines = [medicine_line(e) for e in entries]
    except (KeyError, TypeError):
        raise StepError("bad_input") from None
    head = STATUS_LINES.get(status)
    if status == "listed" and not lines:
        head = STATUS_LINES["not_documented"]
    return "\n".join(([head] if head else []) + lines)


def translation_header(language_lines: list, audience: str) -> str | None:
    """The fixed header when step 3b found any language-need line (D3 T1). No
    negation handling, on purpose: a false header costs the clinician a
    deletion; a missed one hands an English-only leaflet over (HAZ-12)."""
    if audience not in AUDIENCES:
        raise StepError("bad_input")
    return TRANSLATION_HEADER[audience] if language_lines else None


def assemble_part_c(step7: dict, recon: dict, sn: dict, language_lines: list) -> str:
    """Step 7's output + 5a + 5b + 3b's language lines -> PART C as text.

    Sections in step 7's order (its order is checked there, not changed here);
    each under its code heading; the two boxes filled by code; an unknown
    {{...}} is left as written, so a stray placeholder stays visible."""
    audience = step7["audience"]
    if audience not in AUDIENCES:
        raise StepError("bad_input")
    fill = {"{{MEDICATIONS}}": medicines_box(recon), "{{SAFETY_NET}}": sn["part_c"]}
    out = [TITLES[audience], ""]
    header = translation_header(language_lines, audience)
    if header:
        out += [header, ""]
    for s in step7["sections"]:
        text = s["text"]
        for token, body in fill.items():
            text = text.replace(token, body)
        out += [HEADINGS[audience][s["heading"]], text, ""]
    return "\n".join(out).rstrip() + "\n"
