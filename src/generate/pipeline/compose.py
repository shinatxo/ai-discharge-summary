"""Step 6 — compose_clinical (ADR-009 step table, row 6; [A1]–[A5]; Q4).

MODEL 3. One Bedrock Converse call with one strict, forced tool,
`record_composition` (schemas.py). The model writes PART A (the clinical
summary) and PART B (the GP letter) as sections, each listing the fact_ids it
states, from step 3's verified facts ONLY — it never sees the notes (ADR-009:
"Steps 6 and 7 never see the notes"). Medications, resuscitation and
safety-netting are three boxes the model leaves as placeholders and code fills
at 8a ([A1]–[A3]). Code then runs flag-don't-fix checks (fixed words, like
5a): nothing is repaired, nothing is dropped.

Decisions of 27–28 Oct 2026 (step build piece 2; CSO):
  - every contradiction is given to step 6 and must appear in PART A
    (decision 1A — 5a's line-based rule would drop B6's allergy conflict);
  - code renders the PART A header (identifiers, dates, specialty, legal
    status, weight) and B's letterhead at 8a, so those six fields are context
    here, not PART A content (decision 2A, C5);
  - unverified facts are included, unmarked; each section lists the
    unverified fact_ids it uses so 8a can add "(source not verified — check
    the notes)" (decision 3A, [A4]);
  - the input is {fact_id, value} under each field's status — no quotes, no
    line IDs (decision 5).

run_step("compose_clinical", {"facts": <step 3 output facts>}, ctx=StepContext(...)):
  in:  {"facts": ...} and nothing else — a "lines" key (the notes) is refused.
  out: {"sections": [{section_id, part, heading, fact_ids, text,
                      unverified_fact_ids}],
        "checks": [{check, part, heading, fact_id, token}],
        "counts": {...}, "age_group", "call": {...}}
Fails closed with a fixed code: bad_input, no_client (registry), max_tokens,
unexpected_stop_reason, no_tool_call, composition_not_object,
composition_shape_invalid, throttled, read_timeout, request_rejected,
request_invalid, bedrock_error.

What the output may carry: prose built from fact values (the same class of
content as step 3's facts), headings and fact_ids. `checks` carries fixed
words, headings, fact_ids, field names and 4-digit years only — never text.

Honest limit (ADR-009 Q4, again): a fact_id shows the model CLAIMED to use a
fact, not that its sentence states it correctly; and provenance is per
section, not per sentence. Faithfulness is not checked here.

The caller builds the client with read_timeout 90 s and max_attempts 1
(ADR-009 Q3 (2)). Nothing here creates an AWS client (package rule).
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from pathlib import Path

from .context import StepContext
from .converse import call_block, converse_with_throttle_retry
from .errors import StepError
from .retrieve import _RESUS_PATTERNS          # the same terms step 3b retrieves on
from .safety_net import PAEDIATRIC_GROUPS
from .schemas import FIELD_NAMES, record_composition_schema, shape_errors

TOOL_NAME = "record_composition"
TOOL_DESCRIPTION = (
    "Record PART A and PART B as a list of sections, each with the IDs of the "
    "facts it states. Call exactly once."
)

MAX_TOKENS = 6000        # expected ~2-2.5k; truncation fails the step
TEMPERATURE = 0
READ_TIMEOUT_S = 90      # ADR-009 Q3 (2): the caller's client setting, recorded here

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "compose_clinical.md"
PROMPT_MARKER = "## SYSTEM PROMPT"

# The three code-filled boxes ([A1]-[A3]).
BOXES = {"medications": "{{MEDICATIONS}}",
         "resuscitation_status": "{{RESUS}}",
         "safety_net": "{{SAFETY_NET}}"}

# Code renders these in the PART A header and B's letterhead (decision 2A, C5).
HEADER_FIELDS = ("age_sex", "specialty", "legal_status", "weight", "admission_date", "discharge_date")

A_HEADINGS = ("presenting_complaint", "diagnosis", "key_investigations", "treatment",
              "resuscitation_status", "risk_assessment", "medications", "medication_conflicts",
              "allergies", "follow_up", "gp_actions", "safety_net", "vte_assessment")
B_HEADINGS = ("opening", "admission_summary", "diagnosis", "resuscitation_status", "medications",
              "medication_conflicts", "gp_actions", "follow_up", "pending_results", "safety_net")
A_OPTIONAL = ("risk_assessment", "medication_conflicts")
A_REQUIRED = tuple(h for h in A_HEADINGS if h not in A_OPTIONAL)
B_REQUIRED = ("opening", "resuscitation_status", "medications", "gp_actions", "safety_net")
PART_HEADINGS = {"part_a": A_HEADINGS, "part_b": B_HEADINGS}

# PART A heading -> the fields it is built from. With HEADER_FIELDS, covers FIELD_NAMES.
A_FIELDS = {
    "presenting_complaint": ("presenting_complaint",),
    "diagnosis": ("diagnosis_primary", "diagnosis_secondary", "diagnosis_background"),
    "key_investigations": ("key_investigations",),
    "treatment": ("treatment",),
    "risk_assessment": ("risk_assessment",),
    "allergies": ("allergies",),
    "follow_up": ("follow_up",),
    "gp_actions": ("gp_actions",),
    "vte_assessment": ("vte_assessment",),
}
CONTENT_FIELDS = tuple(f for fs in A_FIELDS.values() for f in fs)

NOT_DOCUMENTED = "Not documented"
PAEDIATRIC_VTE = "Not applicable (paediatric)"
EXCLUDED_PREFIXES = ("medications.", "documented_advice.", "suspicious_text.")
EXCLUDED_IDS = ("resus",)

# Fixed check words — safe to trace and log.
CHECKS = (
    "heading_wrong_part", "heading_repeated", "required_heading_missing",
    "unknown_fact_id", "excluded_fact_id",
    "box_not_exact", "stray_placeholder",
    "contradiction_missing", "fact_unused", "field_omitted", "gp_action_missing_in_b",
    "inferred_unflagged", "not_documented_with_content", "conflict_section_uncited",
    "year_added", "resus_text_outside",
)

_PLACEHOLDER = re.compile(r"\{\{[^{}]*\}\}")
_INFERRED = re.compile(r"inferred\W+not documented\W+confirm", re.IGNORECASE)
STATUSES = ("documented", "not_documented", "inferred_flagged")

# Years — the rubric v2 D2 rule (evals/rubric_checks.py, checks v2), copied
# because the package cannot import evals/; a test pins the two together.
_YEAR = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
_SHORT_YEAR_DATE = re.compile(r"(?<!\d)\d{1,2}[/.-]\d{1,2}[/.-](\d{2})(?!\d)")


def documented_years(text: str) -> set[str]:
    """4-digit years `text` gives: in full, or as the 2-digit year of a date
    (26/04/26 -> 1926 and 2026). A bare 2-digit number is not a year."""
    years = set(_YEAR.findall(text))
    for yy in _SHORT_YEAR_DATE.findall(text):
        years |= {"19" + yy, "20" + yy}
    return years


def _in_digit_run(text: str, start: int, end: int) -> bool:
    """'0800 2000 123' — a phone or reference number, not a year."""
    before, after = text[max(0, start - 2):start], text[end:end + 2]
    return bool(re.fullmatch(r"\d[\s-]", before) or re.fullmatch(r"[\s-]\d", after))


def added_years(input_years: set[str], text: str) -> list[str]:
    return [m.group() for m in _YEAR.finditer(text)
            if m.group() not in input_years and not _in_digit_run(text, m.start(), m.end())]


def tool_config() -> dict:
    """One strict tool, forced. Built fresh each call (the schema is a deep copy)."""
    return {
        "tools": [{"toolSpec": {
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "inputSchema": {"json": record_composition_schema()},
            "strict": True,
        }}],
        "toolChoice": {"tool": {"name": TOOL_NAME}},
    }


@lru_cache(maxsize=1)
def system_prompt() -> tuple[str, str]:
    """(text sent, its SHA-256) — only the text after the marker, as steps 2 and 5a."""
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    if PROMPT_MARKER not in raw:
        raise RuntimeError("compose_clinical.md is missing the SYSTEM PROMPT marker")
    text = raw.split(PROMPT_MARKER, 1)[1].strip()
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- input ----------------------------------------------------------------------

def _items_ok(items) -> bool:
    return isinstance(items, list) and all(
        isinstance(i, dict) and isinstance(i.get("fact_id"), str) and isinstance(i.get("value"), str)
        for i in items)


def _check_input(inp) -> dict:
    # Exactly {"facts": ...}: step 6 must never receive the notes (ADR-009 F8 / Q1).
    if not isinstance(inp, dict) or set(inp) != {"facts"}:
        raise StepError("bad_input")
    facts = inp["facts"]
    try:
        fields = facts["fields"]
        if (not isinstance(fields, dict) or set(fields) != set(FIELD_NAMES)
                or any(fields[n].get("status") not in STATUSES or not _items_ok(fields[n].get("items"))
                       for n in FIELD_NAMES)
                or not _items_ok(facts["contradictions"]) or not isinstance(facts["age_group"], str)):
            raise StepError("bad_input")
    except (KeyError, TypeError, AttributeError):
        raise StepError("bad_input") from None
    return facts


def user_text(facts: dict) -> str:
    """age_group, every field with its status and {fact_id, value} items, and
    every contradiction. NOT medications, resus, documented advice or
    suspicious text; no quotes, no line IDs (decision 5); unverified items are
    included unmarked (decision 3A)."""
    parts = [f"age_group: {facts['age_group']}", "", "FACTS"]
    for name in FIELD_NAMES:
        f = facts["fields"][name]
        parts.append(f"{name} ({f['status']})")
        parts += [f"- [{i['fact_id']}] {i['value']}" for i in f["items"]]
    parts += ["", "CONTRADICTIONS"]
    parts += [f"- [{i['fact_id']}] {i['value']}" for i in facts["contradictions"]] or ["none"]
    return "\n".join(parts)


# --- output ---------------------------------------------------------------------

def _norm(text: str) -> str:
    return " ".join(text.split()).rstrip(".").casefold()


def _excluded(fid: str) -> bool:
    return fid in EXCLUDED_IDS or fid.startswith(EXCLUDED_PREFIXES)


def check_composition(wire: dict, facts: dict) -> tuple[list[dict], list[dict], dict]:
    """Wire form -> (sections, checks, counts). Flags; never repairs or drops."""
    fields = facts["fields"]
    given: dict[str, str] = {}                         # fact_id -> field name / "contradictions"
    for name in FIELD_NAMES:
        for i in fields[name]["items"]:
            given[i["fact_id"]] = name
    for i in facts["contradictions"]:
        given[i["fact_id"]] = "contradictions"
    every_item = [i for n in FIELD_NAMES for i in fields[n]["items"]] + list(facts["contradictions"])
    unverified = {i["fact_id"] for i in every_item if i.get("citation_status") != "verified"}
    inferred = {i["fact_id"] for n in FIELD_NAMES if fields[n]["status"] == "inferred_flagged"
                for i in fields[n]["items"]}
    input_years = documented_years("\n".join(i["value"] for i in every_item))
    paediatric = facts["age_group"] in PAEDIATRIC_GROUPS

    checks: list[dict] = []

    def flag(check, part=None, heading=None, fact_id=None, token=None):
        checks.append({"check": check, "part": part, "heading": heading, "fact_id": fact_id, "token": token})

    sections, seen = [], {}
    for idx, s in enumerate(wire["sections"]):
        part, heading, ids, text = s["part"], s["heading"], s["fact_ids"], s["text"]
        if heading not in PART_HEADINGS[part]:
            flag("heading_wrong_part", part, heading)
        if (part, heading) in seen:
            flag("heading_repeated", part, heading)
        seen.setdefault((part, heading), idx)
        for fid in ids:
            if _excluded(fid):
                flag("excluded_fact_id", part, heading, fid)
            elif fid not in given:
                flag("unknown_fact_id", part, heading, fid if re.fullmatch(r"[a-z_]+\.\d+|resus", fid) else None)
        if heading in BOXES:
            if text.strip() != BOXES[heading] or ids:
                flag("box_not_exact", part, heading)
        else:
            if _PLACEHOLDER.search(text):
                flag("stray_placeholder", part, heading)
            if any(p.search(text) for p in _RESUS_PATTERNS):
                flag("resus_text_outside", part, heading)
            if any(f in inferred for f in ids) and not _INFERRED.search(text):
                flag("inferred_unflagged", part, heading)
            for y in added_years(input_years, text):
                flag("year_added", part, heading, token=y)
        if heading == "medication_conflicts" and not any(given.get(f) == "contradictions" for f in ids):
            flag("conflict_section_uncited", part, heading)
        sections.append({"section_id": f"sections.{idx}", "part": part, "heading": heading,
                         "fact_ids": list(ids), "text": text,
                         "unverified_fact_ids": [f for f in ids if f in unverified]})

    for part, required in (("part_a", A_REQUIRED), ("part_b", B_REQUIRED)):
        for h in required:
            if (part, h) not in seen:
                flag("required_heading_missing", part, h)

    a_ids = {f for s in sections if s["part"] == "part_a" for f in s["fact_ids"]}
    b_ids = {f for s in sections if s["part"] == "part_b" for f in s["fact_ids"]}
    for i in facts["contradictions"]:
        if i["fact_id"] not in a_ids:
            flag("contradiction_missing", "part_a", fact_id=i["fact_id"])
    for name in CONTENT_FIELDS:
        items = fields[name]["items"]
        for i in items:
            if i["fact_id"] not in a_ids:
                flag("fact_unused", "part_a", fact_id=i["fact_id"])
        if items and not any(i["fact_id"] in a_ids for i in items):
            flag("field_omitted", "part_a", token=name)
    for i in fields["gp_actions"]["items"]:
        if i["fact_id"] not in b_ids:
            flag("gp_action_missing_in_b", "part_b", fact_id=i["fact_id"])

    # A section whose fields are all not_documented (and carry no items):
    # "Not documented", no fact_ids — or left out (risk_assessment), or the
    # paediatric VTE line. A cited contradiction may sit beside it.
    by_heading = {s["heading"]: s for s in reversed(sections) if s["part"] == "part_a"}
    for heading, names in A_FIELDS.items():
        if any(fields[n]["status"] != "not_documented" or fields[n]["items"] for n in names):
            continue
        sec = by_heading.get(heading)
        if sec is None:
            continue
        if heading == "risk_assessment":
            flag("not_documented_with_content", "part_a", heading)
            continue
        own = [f for f in sec["fact_ids"] if given.get(f) != "contradictions"]
        cites_contradiction = len(own) < len(sec["fact_ids"])
        allowed = {_norm(NOT_DOCUMENTED)}
        if heading == "vte_assessment" and paediatric:
            allowed.add(_norm(PAEDIATRIC_VTE))
        if own or (not cites_contradiction and _norm(sec["text"]) not in allowed):
            flag("not_documented_with_content", "part_a", heading)

    content = [i for n in CONTENT_FIELDS for i in fields[n]["items"]]
    counts = {
        "sections": len(sections),
        "sections_a": sum(s["part"] == "part_a" for s in sections),
        "sections_b": sum(s["part"] == "part_b" for s in sections),
        "facts_given": len(content),
        "facts_used_in_a": sum(i["fact_id"] in a_ids for i in content),
        "header_facts_given": sum(len(fields[n]["items"]) for n in HEADER_FIELDS),
        "contradictions_given": len(facts["contradictions"]),
        "contradictions_in_a": sum(i["fact_id"] in a_ids for i in facts["contradictions"]),
        "gp_actions_given": len(fields["gp_actions"]["items"]),
        "gp_actions_in_b": sum(i["fact_id"] in b_ids for i in fields["gp_actions"]["items"]),
        "unverified_given": len(unverified),
        "unverified_used": len(unverified & (a_ids | b_ids)),
        "checks": len(checks),
    }
    return sections, checks, counts


def compose_clinical(inp: dict, ctx: StepContext) -> dict:
    facts = _check_input(inp)
    prompt, prompt_sha256 = system_prompt()

    system = [{"text": prompt}]
    if ctx.prompt_caching:
        system.append({"cachePoint": {"type": "default"}})   # the facts stay below it, never cached
    request = {
        "modelId": ctx.model_id,
        "system": system,
        "messages": [{"role": "user", "content": [{"text": user_text(facts)}]}],
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
        raise StepError("composition_not_object")
    if shape_errors(wire, record_composition_schema()):
        # Constrained decoding makes this impossible short of truncation; fail closed.
        raise StepError("composition_shape_invalid")

    sections, checks, counts = check_composition(wire, facts)
    return {
        "sections": sections,
        "checks": checks,
        "counts": counts,
        "age_group": facts["age_group"],
        "call": call_block(ctx, prompt_sha256, resp, attempts),
    }
