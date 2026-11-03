"""Step 7 — compose_patient (ADR-009 step table, row 7; [A1]-[A4]; Q4; HAZ-01, -12, -16, -17, -21).

MODEL 4. One Bedrock Converse call with one strict, forced tool,
`record_patient_version` (schemas.py). The model writes PART C, the patient
leaflet, as sections, each listing the PART A section_ids it re-expresses. It
never sees the notes or the facts object — only PART A's prose sections that
code passes it. Medicines and safety-netting are two boxes the model leaves as
placeholders; code fills them at 8b (render.py) from 5a and 5b, as it does in
PART A and B ([A1]-[A3]). Code then runs flag-don't-fix checks (fixed words):
nothing is repaired, nothing is dropped.

Decisions of 1 Nov 2026 (step build piece 3; CSO, all as recommended):
  - D1 I3: the input is step 6's PART A prose sections {section_id, heading,
    text} and the audience — "assembled PART A" read as "PART A as 8a
    assembles it, WITHOUT the code-rendered boxes, which 8b renders into C
    itself". Fed the whole of A, the model could copy the RESUS box's quotes or
    rephrase A's advice outside C's box. `patient_input()` builds it; the step
    refuses anything else (a "lines" or "facts" key, a box heading, an excluded
    heading, a "Not documented" section);
  - D2 M1: C carries a code-rendered {{MEDICATIONS}} box (a recorded departure
    from the ADR row's one placeholder — it keeps [A1]'s "an edit propagates
    into all three documents deterministically" true for C);
  - D3 T1: the translation header is code (render.py, from step 3b's
    language_lines); this step writes English only and is not told about it;
  - D4: risk_assessment is not given (HAZ-16, withheld by construction);
    R1 gp_actions given (next steps); R2 allergies not given;
  - R3: an instruction PART A reports may be passed on as what the team said,
    same force and limits — flagged here (`advice_shaped`), ruled by the CSO.

Decisions of 2 Nov 2026 (piece 3 follow-up; CSO, all as recommended):
  - F1: the step REFUSES (`no_sections_given`, before any Bedrock call) when it is
    given neither a presenting_complaint nor a diagnosis section: why_in_hospital
    is always required and cannot be written honestly without one (S12 r5, 1 Nov:
    step 6 returned 2 sections, step 7 was given none and wrote an unsourced
    sentence that raised no check). The orchestrator (item 5) is to fail the job on
    any step-6 required_heading_missing before step 7 is reached;
  - F2: `prose_without_source` — a prose section citing no section it was given;
  - F5: `promise_shaped` — a service / availability / visit wording, flagged for
    the record (report only, no bar), CSO-ruled.

run_step("compose_patient", {"audience": ..., "sections": [...]}, ctx=StepContext(...)):
  in:  {"audience": "adult" | "parent_or_carer",
        "sections": [{"section_id", "heading", "text"}]}   (patient_input() builds it)
  out: {"sections": [{section_id, heading, source_ids, text}],
        "checks": [{check, heading, source_id, token}],
        "counts": {...}, "audience", "call": {...}}
Fails closed with a fixed code: bad_input, no_sections_given, no_client (registry), max_tokens,
unexpected_stop_reason, no_tool_call, patient_version_not_object,
patient_version_shape_invalid, throttled, read_timeout, request_rejected,
request_invalid, bedrock_error.

What the output may carry: prose re-expressed from PART A (the same class of
content as step 6's sections), headings and section_ids. `checks` carries
fixed words, headings, section_ids, 4-digit years, numbers and unit tokens only
— never text. (G2, 3 Nov: the meaning checks' tokens are fixed list words, a
qualifier group name or "line <n>".)

Honest limit (ADR-009 Q4): a source_id shows the model CLAIMED to use a PART A
section, not that its sentence says the same thing. Withholding judgement is
not checked here, and meaning preservation (HAZ-17) only by fixed word lists
(pipeline/meaning.py, decision G2 of 3 Nov 2026: added cause / certainty words,
dropped qualifiers, test words the cited text does not justify, unreached
diagnoses) — the CSO blind read stays the evidence. The pattern checks are
floors: they flag, the CSO rules.

The caller builds the client with read_timeout 60 s and max_attempts 1
(ADR-009 Q3 (2)). Nothing here creates an AWS client (package rule).
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

from .compose import documented_years, added_years
from . import meaning as MN                  # G2 (3 Nov 2026): meaning flags
from .context import StepContext
from .converse import call_block, converse_with_throttle_retry
from .errors import StepError
from .retrieve import _RESUS_PATTERNS          # the same terms as 3b, the guard and step 6's check
from .safety_net import PAEDIATRIC_GROUPS
from .schemas import PATIENT_HEADINGS, record_patient_version_schema, shape_errors

TOOL_NAME = "record_patient_version"
TOOL_DESCRIPTION = (
    "Record PART C, the patient version, as a list of sections, each with the IDs "
    "of the PART A sections it uses. Call exactly once."
)

MAX_TOKENS = 3000        # expected ~0.6-1k; truncation fails the step
TEMPERATURE = 0
READ_TIMEOUT_S = 60      # ADR-009 Q3 (2): the caller's client setting, recorded here

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "compose_patient.md"
PROMPT_MARKER = "## SYSTEM PROMPT"

AUDIENCES = ("adult", "parent_or_carer")

# The two code-filled boxes in PART C (D2 M1; [A1]-[A3]). PART C has no RESUS box.
BOXES = {"medicines": "{{MEDICATIONS}}", "safety_net": "{{SAFETY_NET}}"}

# PART A headings given to step 7 (D1 I3, D4, R1, R2). Everything else in A is
# not given: the three boxes, risk_assessment, allergies, vte_assessment,
# medication_conflicts — and any section whose text is "Not documented".
GIVEN_A_HEADINGS = ("presenting_complaint", "diagnosis", "key_investigations", "treatment",
                    "follow_up", "gp_actions")
NOT_GIVEN_A_HEADINGS = ("resuscitation_status", "medications", "safety_net", "risk_assessment",
                        "allergies", "vte_assessment", "medication_conflicts")
# PART C section -> the given PART A headings that make it required (and without
# which it must not be written).
SOURCES = {"what_we_did": ("key_investigations", "treatment"),
           "next_steps": ("follow_up", "gp_actions")}
ALWAYS = ("why_in_hospital", "medicines", "safety_net")
# Given sections whose every appointment must reach C (bar line 9).
MUST_REACH_C = ("follow_up", "gp_actions")
# F1 (2 Nov): at least one of these must be given, or the step refuses.
REASON_HEADINGS = ("presenting_complaint", "diagnosis")

CHECKS = (
    "heading_repeated", "heading_out_of_order", "required_heading_missing", "heading_without_source",
    "box_not_exact", "stray_placeholder", "unknown_source_id", "follow_up_missing", "source_unused",
    "signpost_outside_box", "resus_text", "year_added", "not_documented_text",
    "advice_shaped", "dose_or_frequency", "number_added", "unit_added", "conflict_text", "sensitive_text",
    "prose_without_source", "promise_shaped",
) + tuple(MN.CHECK_OF.values())         # G2 (3 Nov 2026): meaning flags, pipeline/meaning.py

_PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")
_NOT_DOCUMENTED = re.compile(r"\bnot\s+(?:documented|recorded)\b", re.IGNORECASE)
_SECTION_ID = re.compile(r"sections\.\d+")

# Urgency tokens — evals/safety_net_gate.py's _SIGNPOST, copied because the package
# cannot import evals/ (the gate moves into the package at 8b, item 4); a test pins
# the two together. Bare "emergency" is deliberately not a token ("an emergency
# operation").
_SIGNPOST = re.compile(
    r"(nhs\s*111|call\s*111|dial\s*111|\b999\b|\ba\s*&\s*e\b|"
    r"accident and emergency|emergency department)",
    re.I,
)

# Flag patterns. Broad on purpose: a false flag costs one CSO ruling; a miss is
# HAZ-01's path (advice), [A1]'s (doses), HAZ-16's (sensitive), Q4's (numbers).
_ADVICE = re.compile(
    r"\bif (?:you|your child|he|she|they)\b|\b(?:avoid|don't|do not|should|must|make sure|try to|"
    r"remember to|call|contact|ring|phone|go to|seek|come back|return)\b|"
    r"\b(?:advised|asked you|told you|you need to|please)\b",
    re.IGNORECASE)
_DOSE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:mg|mcg|micrograms?|g|ml|units?)\b"
    r"|\b(?:once|twice|three times|four times) (?:a|per|each) day\b|\bevery \d+ hours\b"
    r"|(?-i:\b(?:OD|BD|TDS|QDS|PRN|ON)\b)|\b(?:nocte|mane)\b",   # abbreviations case-SENSITIVE:
    re.IGNORECASE)                                                  # "on" is a word, "ON" a dose time
_CONFLICT = re.compile(
    r"\b(?:conflict\w*|disagree\w*|discrepan\w*|contradict\w*|inconsisten\w*|unclear|not clear)\b",
    re.IGNORECASE)
_SENSITIVE = re.compile(
    r"safeguard\w*|social services|social worker|\bpolice\b|self[- ]harm|overdose|suicid\w*|"
    r"risk (?:score|level|assessment|rating)|ceiling of care|escalat\w*|palliat\w*|end of life|"
    r"\bNEWS2?\b|\bPHQ-?9\b|\bGAD-?7\b",
    re.IGNORECASE)
# F5 (2 Nov): a service, availability or visit the input may not state (S4 "will be
# there", S10 a home visit, S12 r5 "will be able to tell you"). Report only; broad on
# purpose — "the team will be in touch" is acceptable when follow_up says so.
_PROMISE = re.compile(
    r"\bwill be (?:there|available|able to|on hand|around|in touch)\b|\bavailable\b|"
    r"\b(?:there|here) for you\b|\bvisits?\b|\bvisit(?:ed|ing)? you\b|\bcome (?:and|to) see you\b|"
    r"\bcheck on you\b|\blook after you\b|\bkeep an eye on\b|\bsupport you\b|\bfollow you up\b",
    re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.]*\d)")
_UNIT = re.compile(
    r"(?<=\d)\s*(mg|mcg|micrograms?|kg|g/L|g/dL|g|ml|mL|L|mmol/L|mmol|ng/L|units?|mmHg|bpm|%)"
    r"(?![A-Za-z])")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def tool_config() -> dict:
    """One strict tool, forced. Built fresh each call (the schema is a deep copy)."""
    return {
        "tools": [{"toolSpec": {
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "inputSchema": {"json": record_patient_version_schema()},
            "strict": True,
        }}],
        "toolChoice": {"tool": {"name": TOOL_NAME}},
    }


@lru_cache(maxsize=1)
def system_prompt() -> tuple[str, str]:
    """(text sent, its SHA-256) — only the text after the marker, as steps 2, 5a and 6."""
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    if PROMPT_MARKER not in raw:
        raise RuntimeError("compose_patient.md is missing the SYSTEM PROMPT marker")
    text = raw.split(PROMPT_MARKER, 1)[1].strip()
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- input ----------------------------------------------------------------------

def _is_not_documented(text: str) -> bool:
    return " ".join(text.split()).rstrip(".").casefold() == "not documented"


def audience_for(age_group: str) -> str:
    return "parent_or_carer" if age_group in PAEDIATRIC_GROUPS else "adult"


def patient_input(compose_output: dict) -> dict:
    """Step 6's output -> step 7's input (D1 I3). Code, deterministic: PART A
    sections whose heading is in GIVEN_A_HEADINGS and whose text is not
    "Not documented", in step 6's order, plus the audience from step 6's
    age_group. The orchestrator (item 5) and the harness both call this."""
    try:
        sections = [{"section_id": s["section_id"], "heading": s["heading"], "text": s["text"]}
                    for s in compose_output["sections"]
                    if s["part"] == "part_a" and s["heading"] in GIVEN_A_HEADINGS
                    and not _is_not_documented(s["text"])]
        return {"audience": audience_for(compose_output["age_group"]), "sections": sections}
    except (KeyError, TypeError):
        raise StepError("bad_input") from None


def _check_input(inp) -> dict:
    # Exactly {"audience", "sections"}: step 7 never receives the notes or the facts.
    if not isinstance(inp, dict) or set(inp) != {"audience", "sections"} or inp["audience"] not in AUDIENCES:
        raise StepError("bad_input")
    secs = inp["sections"]
    if not isinstance(secs, list):
        raise StepError("bad_input")
    ids = set()
    for s in secs:
        if (not isinstance(s, dict) or set(s) != {"section_id", "heading", "text"}
                or not all(isinstance(s[k], str) for k in s)
                or s["heading"] not in GIVEN_A_HEADINGS or _is_not_documented(s["text"])
                or not _SECTION_ID.fullmatch(s["section_id"]) or s["section_id"] in ids):
            raise StepError("bad_input")
        ids.add(s["section_id"])
    if not {s["heading"] for s in secs} & set(REASON_HEADINGS):
        raise StepError("no_sections_given")       # F1: nothing to say why the patient was in
    return inp


def user_text(inp: dict) -> str:
    """The audience, then each given PART A section as its ID, heading and text."""
    parts = [f"audience: {inp['audience']}", "", "PART A SECTIONS"]
    for s in inp["sections"]:
        parts += ["", f"[{s['section_id']}] {s['heading']}", s["text"]]
    if not inp["sections"]:
        parts.append("none")
    return "\n".join(parts)


# --- output ---------------------------------------------------------------------

def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s.strip()]


def numbers(text: str) -> set[str]:
    return set(_NUMBER.findall(text))


def units(text: str) -> set[str]:
    return {u.lower() for u in _UNIT.findall(text)}


def check_patient_version(wire: dict, inp: dict) -> tuple[list[dict], list[dict], dict]:
    """Wire form -> (sections, checks, counts). Flags; never repairs or drops.
    Measured against what step 7 was GIVEN."""
    given = {s["section_id"]: s for s in inp["sections"]}
    given_headings = {s["heading"] for s in inp["sections"]}
    input_text = "\n".join(s["text"] for s in inp["sections"])
    input_years = documented_years(input_text)

    checks: list[dict] = []

    def flag(check, heading=None, source_id=None, token=None):
        checks.append({"check": check, "heading": heading, "source_id": source_id, "token": token})

    sections, seen, last = [], set(), -1
    for idx, s in enumerate(wire["sections"]):
        heading, ids, text = s["heading"], s["source_ids"], s["text"]
        pos = PATIENT_HEADINGS.index(heading)
        if heading in seen:
            flag("heading_repeated", heading)
        elif pos < last:
            flag("heading_out_of_order", heading)
        seen.add(heading)
        last = max(last, pos)
        for sid in ids:
            if sid not in given:
                flag("unknown_source_id", heading, sid if _SECTION_ID.fullmatch(sid) else None)
        if heading in BOXES:
            if text.strip() != BOXES[heading] or ids:
                flag("box_not_exact", heading)
        else:
            if heading in SOURCES and not (given_headings & set(SOURCES[heading])):
                flag("heading_without_source", heading)
            if not set(ids) & set(given):
                flag("prose_without_source", heading)          # F2: no given section behind it
            if _PLACEHOLDER.search(text):
                flag("stray_placeholder", heading)
            if _NOT_DOCUMENTED.search(text):
                flag("not_documented_text", heading)
            for y in added_years(input_years, text):
                flag("year_added", heading, token=y)
            src = "\n".join(given[i]["text"] for i in ids if i in given) or input_text
            src_numbers, src_units = numbers(src), units(src)
            cited = [given[i] for i in ids if i in given] or list(given.values())
            for kind, token, _shown in MN.section_flags(text, cited):
                flag(MN.CHECK_OF[kind], heading, token=token)
            for sent in _sentences(text):
                if _SIGNPOST.search(sent):
                    flag("signpost_outside_box", heading)
                if any(p.search(sent) for p in _RESUS_PATTERNS):
                    flag("resus_text", heading)
                if _ADVICE.search(sent):
                    flag("advice_shaped", heading)
                if _DOSE.search(sent):
                    flag("dose_or_frequency", heading)
                if _CONFLICT.search(sent):
                    flag("conflict_text", heading)
                if _SENSITIVE.search(sent):
                    flag("sensitive_text", heading)
                if _PROMISE.search(sent):
                    flag("promise_shaped", heading)
            for n in sorted(numbers(text) - src_numbers):
                if not added_years(input_years, n):          # a year is year_added's job
                    flag("number_added", heading, token=n)
            for u in sorted(units(text) - src_units):
                flag("unit_added", heading, token=u)
        sections.append({"section_id": f"c_sections.{idx}", "heading": heading,
                         "source_ids": list(ids), "text": text})

    required = list(ALWAYS) + [h for h, srcs in SOURCES.items() if given_headings & set(srcs)]
    for h in PATIENT_HEADINGS:
        if h in required and h not in seen:
            flag("required_heading_missing", h)

    cited = {i for s in sections for i in s["source_ids"]}
    for sid, s in given.items():
        if sid not in cited:
            flag("follow_up_missing" if s["heading"] in MUST_REACH_C else "source_unused",
                 source_id=sid, token=s["heading"])

    prose = [s["text"] for s in sections if s["heading"] not in BOXES]
    for sid, n, _line in MN.diagnoses_unreached(list(given.values()), "\n".join(prose)):
        flag("diagnosis_unreached", "why_in_hospital", sid, token=f"line {n}")
    counts = {
        "sections": len(sections),
        "sections_given": len(given),
        "sources_cited": len(cited & set(given)),
        "words_prose": sum(len(t.split()) for t in prose),
        "checks": len(checks),
    }
    return sections, checks, counts


def compose_patient(inp: dict, ctx: StepContext) -> dict:
    inp = _check_input(inp)
    prompt, prompt_sha256 = system_prompt()

    system = [{"text": prompt}]
    if ctx.prompt_caching:
        system.append({"cachePoint": {"type": "default"}})   # the sections stay below it, never cached
    request = {
        "modelId": ctx.model_id,
        "system": system,
        "messages": [{"role": "user", "content": [{"text": user_text(inp)}]}],
        "inferenceConfig": {"maxTokens": MAX_TOKENS, "temperature": TEMPERATURE},
        "toolConfig": tool_config(),
    }
    resp, attempts = converse_with_throttle_retry(ctx, request)

    stop = resp.get("stopReason")
    if stop == "max_tokens":
        raise StepError("max_tokens")
    uses = [b["toolUse"] for b in resp.get("output", {}).get("message", {}).get("content", [])
            if isinstance(b, dict) and "toolUse" in b]
    if len(uses) != 1 or uses[0].get("name") != TOOL_NAME:
        raise StepError("no_tool_call")
    if stop != "tool_use":
        raise StepError("unexpected_stop_reason")
    wire = uses[0].get("input")
    if not isinstance(wire, dict):
        raise StepError("patient_version_not_object")
    if shape_errors(wire, record_patient_version_schema()):
        # Constrained decoding makes this impossible short of truncation; fail closed.
        raise StepError("patient_version_shape_invalid")

    sections, checks, counts = check_patient_version(wire, inp)
    return {
        "sections": sections,
        "checks": checks,
        "counts": counts,
        "audience": inp["audience"],
        "call": call_block(ctx, prompt_sha256, resp, attempts),
    }
