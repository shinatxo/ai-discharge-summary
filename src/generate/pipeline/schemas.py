"""The record_facts input schema — the facts object step 2 must produce (ADR-009 (a)).

Step 2 is a Bedrock tool call with `strict: true` and toolChoice forced, so
Bedrock compiles this schema into a grammar and the model cannot emit anything
outside it. Two sets of rules shape the schema:

1. Bedrock structured-output limits: additionalProperties false on every object,
   no minLength/maxLength/minimum/maximum, no recursion. We go further and use
   no optional properties and no union/nullable types (Anthropic's compiler
   caps both, and "not documented" is expressed with a status enum instead).
2. DPIA v2.6 Annex C.2: the compiled grammar is cached for 24 h under
   AWS-managed keys, not our CMK — acceptable only because a schema carries no
   patient data. So a schema holds FIELD NAMES AND FIXED ENUMS ONLY: no
   description, no title, no examples, no default, nothing drawn from notes.
   Field meanings live in the step-2 prompt, which is not grammar-cached.

tests/test_pipeline_schema.py enforces both, independently of the helpers
below (the helpers make a violation hard to write; the test catches a hand edit).

Every property is required. Absence is data: a field the notes do not document
comes back with status "not_documented" and no items, never as a missing key.

WIRE FORM vs GROUPED FORM. What the model emits (the "wire form", this schema):
    field_status: [{field, status}]            one per FIELD_NAMES name
    facts:        [{section, value, cites}]    every extracted statement, tagged
    discharge_status, resus, age_group, suspicious_text
validate_facts (step 3) regroups it into the "grouped form" every later step
and every fixture uses — fields{name: {status, items}}, medications{...},
documented_advice[], contradictions[] — exactly the ADR-009 step-table shape.
wire_facts() below converts grouped -> wire, for fixtures and tests.

Why the wire form is flat — measured, 30-01 Oct 2026 (docs/_local/schema_ladder.py):
Bedrock rejected the grouped schema ("The compiled grammar is too large", 13.5 KB)
and a 3.9 KB list-of-fields version of it. Each part compiled alone (fields list
3.8 s; everything else 12.5 s); together they did not, nor with one citation per
item. The grammar appears to grow with the number of PLACES the item shape
occurs (five in the grouped form). The wire form has it once and compiled in
7.4 s (2.6 KB). What the grammar no longer enforces — each field exactly once,
and a statement in the right group — step 3 checks (fields_incomplete) or the
prompt asks for (section). Keep the schema under ~3 KB: test_pipeline_schema
guards it, and growing past it needs a real call to prove it still compiles.
"""

from __future__ import annotations

import copy

# --- builders: closed objects, fully required, one type per node ------------

def _obj(props: dict) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def _arr(items: dict) -> dict:
    return {"type": "array", "items": items}


def _str() -> dict:
    return {"type": "string"}


def _bool() -> dict:
    return {"type": "boolean"}


def _enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


# --- building blocks ---------------------------------------------------------

# A citation: one or more CONSECUTIVE line IDs (a quote may run across a line
# wrap) and the verbatim quote. "Consecutive", "at most 3 lines" and
# "quote <= 200 characters" cannot be expressed in the schema — step 3 enforces
# them in code and marks failures citation_unverified.
CITE = _obj({
    "lines": _arr(_str()),
    "quote": _str(),
})

STATUS = _enum("documented", "not_documented", "inferred_flagged")


# PART A fields other than medications, resuscitation and advice, which have
# their own objects below because code renders them (ADR-009 [A1]-[A3]).
# Patient identifiers (name, DOB, NHS/hospital number) are deliberately absent —
# see the note in the W2 session: they would put identifiers into every trace.
FIELD_NAMES = (
    "age_sex",       # first: the model fills the schema in order (30 Sep, W2 part 2)
    "specialty",
    "legal_status",
    "weight",
    "admission_date",
    "discharge_date",
    "presenting_complaint",
    "diagnosis_primary",
    "diagnosis_secondary",
    "diagnosis_background",
    "key_investigations",
    "treatment",
    "risk_assessment",
    "allergies",
    "follow_up",
    "gp_actions",
    "vte_assessment",
)

# The groups a statement can belong to: a PART A field, or one of the four
# lists that code renders or checks separately (ADR-009 [A1]-[A3]).
LIST_SECTIONS = ("pre_admission", "discharge", "documented_advice", "contradictions")
SECTIONS = FIELD_NAMES + LIST_SECTIONS

# A field's status. documented -> statements with cites; not_documented -> none;
# inferred_flagged -> the low-stakes contextual inference v0.7 permits (e.g.
# specialty), still citing the lines it was inferred from.
FIELD_STATUS = _obj({
    "field": _enum(*FIELD_NAMES),
    "status": STATUS,
})

# One extracted statement, tagged with its section, and its evidence. Step 3
# gives every statement a fact_id from its grouped path ("diagnosis_primary.0",
# "medications.discharge.2") — the model does not choose IDs.
FACT = _obj({
    "section": _enum(*SECTIONS),
    "value": _str(),
    "cites": _arr(CITE),
})

    # listed: the discharge drugs are written out; referenced_not_listed: the
    # notes say e.g. "continue regular medications" without listing them (A5);
    # none_required: the notes say no medication is needed on discharge (v0.7's
    # "None", distinct from "Not documented"); not_documented: nothing about
    # discharge medication. For referenced_not_listed and none_required the
    # step-2 prompt puts the statement itself in `discharge` as one cited item,
    # so the status has evidence code can verify (no separate cites field).
DISCHARGE_STATUS = _enum("listed", "referenced_not_listed", "none_required", "not_documented")

RESUS = _obj({
    "form_or_discussion_documented": _bool(),
    "status_documented": _enum("dnacpr", "for_resuscitation", "other_documented", "not_documented"),
    "changed": _enum("yes", "no", "not_documented"),
    "cites": _arr(CITE),
})

AGE_GROUP = _enum("neonate", "infant", "child", "adult", "not_documented")


def _build() -> dict:
    return _obj({
        "field_status": _arr(FIELD_STATUS),    # one per FIELD_NAMES name; checked in step 3
        "facts": _arr(FACT),                   # every statement, tagged with its section
        "discharge_status": DISCHARGE_STATUS,
        "resus": RESUS,
        "age_group": AGE_GROUP,                # selects the pinned paediatric fall-back in 5b
        "suspicious_text": _arr(CITE),         # model-reported instruction-like text (HAZ-08)
    })


def record_facts_schema() -> dict:
    """A fresh copy each call, so no caller can mutate the schema another caller sees."""
    return copy.deepcopy(_build())


def wire_facts(grouped: dict) -> dict:
    """Grouped form -> wire form, for fixtures and tests. Statements keep their
    order within each section; sections are emitted in SECTIONS order."""
    facts = []
    for name in FIELD_NAMES:
        facts += [{"section": name, **i} for i in grouped["fields"][name]["items"]]
    for sec in ("pre_admission", "discharge"):
        facts += [{"section": sec, **i} for i in grouped["medications"][sec]]
    for sec in ("documented_advice", "contradictions"):
        facts += [{"section": sec, **i} for i in grouped[sec]]
    return {
        "field_status": [{"field": n, "status": grouped["fields"][n]["status"]} for n in FIELD_NAMES],
        "facts": facts,
        "discharge_status": grouped["medications"]["discharge_status"],
        "resus": grouped["resus"],
        "age_group": grouped["age_group"],
        "suspicious_text": grouped["suspicious_text"],
    }


# --- structural check used by validate_facts (step 3) on fixture inputs -------

def shape_errors(instance, schema: dict | None = None, path: str = "$") -> list[str]:
    """Where `instance` departs from the schema, as a list of '<path>: <problem>'.

    Live, constrained decoding makes a mismatch impossible short of truncation;
    this exists for fixture inputs in the W4-W5 harness and for fail-closed
    input checking. Messages hold paths and fixed problem words ONLY — never an
    instance value, which could be note text.
    """
    if schema is None:
        schema = _build()
    kind = schema["type"]
    if kind == "object":
        if not isinstance(instance, dict):
            return [f"{path}: expected object"]
        errors = []
        props = schema["properties"]
        for key in props:
            if key not in instance:
                errors.append(f"{path}.{key}: missing")
        for key in instance:
            if key not in props:
                errors.append(f"{path}: unexpected key")  # key name could be anything; not echoed
        for key, sub in props.items():
            if key in instance:
                errors.extend(shape_errors(instance[key], sub, f"{path}.{key}"))
        return errors
    if kind == "array":
        if not isinstance(instance, list):
            return [f"{path}: expected array"]
        errors = []
        for i, item in enumerate(instance):
            errors.extend(shape_errors(item, schema["items"], f"{path}[{i}]"))
        return errors
    if kind == "string":
        if not isinstance(instance, str):
            return [f"{path}: expected string"]
        if "enum" in schema and instance not in schema["enum"]:
            return [f"{path}: not in enum"]
        return []
    if kind == "boolean":
        return [] if isinstance(instance, bool) else [f"{path}: expected boolean"]
    return [f"{path}: unsupported schema type"]
