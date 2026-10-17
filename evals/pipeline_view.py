"""The pipeline's judgeable view (W5): what the rubric v2 judge and checks read
for a pipeline record at steps 1–5b.

EVAL_RESULTS.md section 2, "What each generator is scored on (v2)": until steps
6–7 exist the pipeline has no prose, so it is scored on a narrower surface —
each fact's value against its cites, step 4's resus status, age group, and
5b's `part_ab` / `part_c`. This module renders exactly that surface as one plain
text document with PART A / B / C markers, so the frozen judge and the
deterministic checks read it the same way they read a v1 output:

  preamble  one line saying what this is (no prose yet)
  PART A    steps 2–4: age group, every field (status, values, line IDs),
            medications, resus as step 4 rendered it, documented advice,
            contradictions
  PART B    step 5b `part_ab` — what the clinician's documents would carry
  PART C    step 5b `part_c` — what the patient version would carry

Rendering is deterministic and adds nothing a step did not produce: values are
copied verbatim, a field with no items shows its status, line IDs are the step's
own cites. Pipeline and v1 are still compared per dimension, never as a whole.
"""

from __future__ import annotations

import copy

VIEW_NAME = "pipeline-steps-1-5b"
VIEW_VERSION = 1


class ViewError(ValueError):
    pass


FIELD_LABELS = {
    "age_sex": "Age / sex",
    "specialty": "Specialty",
    "legal_status": "Legal status",
    "weight": "Weight",
    "admission_date": "Admission date",
    "discharge_date": "Discharge date",
    "presenting_complaint": "Presenting complaint",
    "diagnosis_primary": "Primary diagnosis",
    "diagnosis_secondary": "Secondary diagnoses",
    "diagnosis_background": "Background",
    "key_investigations": "Key investigations",
    "treatment": "Treatment",
    "risk_assessment": "Risk assessment",
    "allergies": "Allergies",
    "follow_up": "Follow-up",
    "gp_actions": "GP actions",
    "vte_assessment": "VTE assessment",
}

STATUS_TEXT = {
    "documented": None,                       # values speak for themselves
    "not_documented": "Not documented",
    "inferred_flagged": "inferred, flagged for clinician check",
}

MED_STATUS_TEXT = {
    "listed": None,
    "not_documented": "Not documented",
    "referenced_not_listed": "Referenced in the notes but not listed",
    "none_required": "None required",
}


def _ids(item: dict) -> str:
    ids = [i for c in item.get("cites") or [] for i in c.get("lines") or []]
    tag = f" [{', '.join(ids)}]" if ids else ""
    if item.get("citation_status") not in (None, "verified"):
        tag += " [citation unverified]"
    return tag


def _item_lines(items: list) -> list[str]:
    return [f"- {it['value']}{_ids(it)}" for it in items]


def _field(label: str, entry: dict) -> list[str]:
    status = entry.get("status")
    if status not in STATUS_TEXT:
        raise ViewError(f"unknown field status: {status!r}")
    items = entry.get("items") or []
    note = STATUS_TEXT[status]
    if not items:
        return [f"{label}: {note or 'Not documented'}"]
    head = f"{label}:" + (f" ({note})" if note else "")
    if len(items) == 1 and not note:
        return [f"{label}: {items[0]['value']}{_ids(items[0])}"]
    return [head, *_item_lines(items)]


def _meds(meds: dict) -> list[str]:
    status = meds.get("discharge_status")
    if status not in MED_STATUS_TEXT:
        raise ViewError(f"unknown discharge medication status: {status!r}")
    out = ["Discharge medications:" + (f" {MED_STATUS_TEXT[status]}" if MED_STATUS_TEXT[status] else "")]
    out += _item_lines(meds.get("discharge") or [])
    pre = meds.get("pre_admission") or []
    out.append("Pre-admission medications:" + ("" if pre else " Not documented"))
    out += _item_lines(pre)
    return out


def render(rec: dict) -> str:
    """The judgeable text for one pipeline record. Raises ViewError if the record
    did not get through step 5b."""
    if rec.get("generator") != "pipeline":
        raise ViewError("not a pipeline record")
    if rec.get("error") or rec.get("failed_step"):
        raise ViewError("record stopped before step 5b")
    steps = rec.get("steps") or {}
    try:
        facts = steps["validate_facts"]["facts"]
        resus_text = steps["route_resus"]["rendered"]
        sn = steps["select_safety_net"]
        part_ab, part_c = sn["part_ab"], sn["part_c"]
    except (KeyError, TypeError) as exc:
        raise ViewError(f"record is missing a step 1–5b result: {exc}") from None

    fields = facts.get("fields") or {}
    unknown = set(fields) - set(FIELD_LABELS)
    if unknown:
        raise ViewError(f"unknown fact fields: {sorted(unknown)}")

    a = [f"Age group: {facts.get('age_group')}"]
    for name, label in FIELD_LABELS.items():
        if name in fields:
            a += _field(label, fields[name])
    a += _meds(facts.get("medications") or {})
    a += ["Resuscitation status:", resus_text.strip()]
    advice = facts.get("documented_advice") or []
    a.append("Documented advice:" + ("" if advice else " None documented"))
    a += _item_lines(advice)
    contra = facts.get("contradictions") or []
    if contra:
        a.append("Contradictions flagged for clinician review:")
        a += _item_lines(contra)

    return "\n".join([
        "Pipeline output, steps 1–5b: structured facts and safety-netting only "
        "(no prose until steps 6–7).",
        "",
        "PART A — CLINICAL FACTS (steps 2–4)",
        *a,
        "",
        "PART B — SAFETY-NETTING FOR THE CLINICIAN (step 5b)",
        part_ab.strip(),
        "",
        "PART C — PATIENT SAFETY-NETTING (step 5b)",
        part_c.strip(),
        "",
    ])


def judgeable(rec: dict) -> dict:
    """A copy of the record with `output` set to the rendered view and `view`
    naming the renderer, so judge records say which view they scored."""
    out = copy.deepcopy(rec)
    out["output"] = render(rec)
    out["view"] = {"name": VIEW_NAME, "version": VIEW_VERSION}
    return out
