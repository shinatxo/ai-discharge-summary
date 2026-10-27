"""Step 5a — reconcile_medications (ADR-009 step table, row 5a; [A1], [A4]).

MODEL 2. One Bedrock Converse call with one strict, forced tool,
`record_reconciliation` (schemas.py). The model reconciles the pre-admission
drug history against the discharge medication, one entry per drug, citing the
note lines for each. Code then:

  1. re-verifies every citation with step 3's verify_cite (ADR-009: "citations
     re-verified by the step-3 validator") — a failure is FLAGGED, never
     repaired and never dropped: dropping a drug is an omission (HAZ-06/07),
     repairing asks the model to find support for what it wrote (F4);
  2. regroups the wire form (one `cites` list with a `role` per cite) into the
     gold / ADR shape: dh_cites, discharge_cites, previous{dose, frequency};
  3. runs flag-don't-fix checks (fixed words, like step 3's field_check);
  4. copies discharge_status from step 3 — 5a does not decide it.

run_step("reconcile_medications", {"lines", "facts", "evidence"}, ctx=StepContext(...)):
  in:  lines    — step 1's line index
       facts    — step 3's output facts (grouped form)
       evidence — step 3b's output (its "medication" block is used)
  out: {"reconciliation": [entry, ...], "discharge_status", "reconciliation_check",
        "dh_lines_unreconciled": [line IDs], "counts": {...}, "call": {...}}
  entry: {entry_id, drug, tag, basis, dose, route, frequency, previous, conflict,
          dh_cites, discharge_cites, citation_status, reason, checks}
         dose / route / frequency: None where the model wrote "" (not written).
Fails closed with a fixed code: bad_input, no_client (registry), max_tokens,
unexpected_stop_reason, no_tool_call, reconciliation_not_object,
reconciliation_shape_invalid, throttled, read_timeout, request_rejected,
request_invalid, bedrock_error.

What the output may carry: drug names, doses and frequencies as the model wrote
them, and quotes of <= 200 characters (verify_cite drops a longer one) — the
same class of content as step 3's facts. Never the line index or a whole note.

The caller builds the client with read_timeout 60 s and max_attempts 1
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
from .schemas import record_reconciliation_schema, shape_errors
from .validate import verify_cite

TOOL_NAME = "record_reconciliation"
TOOL_DESCRIPTION = (
    "Record the medication reconciliation: one entry per drug, each with its "
    "citations. Call exactly once."
)

MAX_TOKENS = 4096        # real outputs expected ~0.5-1.5k; truncation fails the step
TEMPERATURE = 0
READ_TIMEOUT_S = 60      # ADR-009 Q3 (2): the caller's client setting, recorded here

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "reconcile_medications.md"
PROMPT_MARKER = "## SYSTEM PROMPT"
_LINE_ID = re.compile(r"^L\d{3}$")

# Which citations each tag needs (prompt m0.1, "cites"). Checked, not enforced.
NEEDS_DH = {"continued", "increased", "decreased", "changed", "stopped", "withheld"}
CHANGE_TAGS = {"increased", "decreased", "changed"}
NOT_ACTIVE = {"stopped", "withheld"}

# Fixed check words — safe to trace and log.
CHECKS = ("missing_dh_cite", "missing_discharge_cite", "inferred_not_continued",
          "conflict_one_side", "previous_missing", "previous_on_unchanged_tag", "duplicate_drug")
DRUGS_WHERE_NONE_DOCUMENTED = "drugs_where_none_documented"


def tool_config() -> dict:
    """One strict tool, forced. Built fresh each call (the schema is a deep copy)."""
    return {
        "tools": [{"toolSpec": {
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "inputSchema": {"json": record_reconciliation_schema()},
            "strict": True,
        }}],
        "toolChoice": {"tool": {"name": TOOL_NAME}},
    }


@lru_cache(maxsize=1)
def system_prompt() -> tuple[str, str]:
    """(text sent, its SHA-256) — only the text after the marker, as step 2."""
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    if PROMPT_MARKER not in raw:
        raise RuntimeError("reconcile_medications.md is missing the SYSTEM PROMPT marker")
    text = raw.split(PROMPT_MARKER, 1)[1].strip()
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- input ----------------------------------------------------------------------

def _check_input(inp) -> tuple[dict, dict, list]:
    if not isinstance(inp, dict):
        raise StepError("bad_input")
    lines, facts, evidence = inp.get("lines"), inp.get("facts"), inp.get("evidence")
    if (not isinstance(lines, dict) or not lines
            or any(not isinstance(k, str) or not _LINE_ID.match(k) or not isinstance(v, str)
                   for k, v in lines.items())):
        raise StepError("bad_input")
    try:
        meds = facts["medications"]
        if not (isinstance(meds["pre_admission"], list) and isinstance(meds["discharge"], list)
                and isinstance(meds["discharge_status"], str)
                and isinstance(facts["contradictions"], list)):
            raise StepError("bad_input")
        med_rows = evidence["medication"]
        if not isinstance(med_rows, list) or any(r["line"] not in lines for r in med_rows):
            raise StepError("bad_input")
    except (KeyError, TypeError):
        raise StepError("bad_input") from None
    return lines, facts, med_rows


def _is_verified(item: dict) -> bool:
    cites = item.get("cites") or []
    return (item.get("citation_status") == "verified" and bool(cites)
            and all(c.get("verified") is True for c in cites))


def _cited_lines(item: dict) -> list[str]:
    return sorted({lid for c in item.get("cites") or [] if c.get("verified") is True
                   for lid in c.get("lines") or []})


def _fact_lines(items: list) -> list[str]:
    return [f"- {i['value']} [{', '.join(_cited_lines(i))}]" for i in items] or ["- none recorded"]


def user_text(lines: dict, facts: dict, med_rows: list) -> str:
    """NOTES, MEDICATION LINES, EXTRACTED FACTS — the three parts the m0.1 prompt
    describes. Only VERIFIED facts go in (ADR-009: "verified medication facts");
    contradictions only if they cite a medication line (B6)."""
    meds = facts["medications"]
    pre = [i for i in meds["pre_admission"] if _is_verified(i)]
    dis = [i for i in meds["discharge"] if _is_verified(i)]
    med_lines = {r["line"] for r in med_rows}
    med_lines.update(lid for i in pre + dis for lid in _cited_lines(i))
    contra = [i for i in facts["contradictions"]
              if _is_verified(i) and med_lines & set(_cited_lines(i))]
    parts = ["NOTES"]
    parts += [f"{lid}: {lines[lid]}" for lid in sorted(lines)]
    parts += ["", "MEDICATION LINES", ", ".join(sorted(med_lines)) or "none found"]
    parts += ["", "EXTRACTED FACTS", f"discharge_status: {meds['discharge_status']}",
              "pre_admission:", *_fact_lines(pre),
              "discharge:", *_fact_lines(dis),
              "contradictions:", *_fact_lines(contra)]
    return "\n".join(parts)


# --- output ---------------------------------------------------------------------

def _none_if_blank(v: str):
    return v if v.strip() else None


def group_reconciliation(wire: dict, lines: dict) -> tuple[list[dict], dict]:
    """Wire form -> entries (module docstring). Returns (entries, counts)."""
    counts = {"entries": 0, "cites_total": 0, "cites_verified": 0,
              "entries_unverified": 0, "entries_flagged": 0}
    entries, seen = [], set()
    for i, d in enumerate(wire["drugs"]):
        cites = []
        for c in d["cites"]:
            checked = verify_cite({"lines": c["lines"], "quote": c["quote"]}, lines)
            checked["role"] = c["role"]
            cites.append(checked)
        dh = [c for c in cites if c["role"] == "dh"]
        dis = [c for c in cites if c["role"] == "discharge"]
        counts["cites_total"] += len(cites)
        counts["cites_verified"] += sum(1 for c in cites if c["verified"])

        if not cites:
            reason = "missing_cites"
        else:
            reason = next((c["reason"] for c in cites if not c["verified"]), None)
        status = "verified" if reason is None else "citation_unverified"

        tag, basis = d["tag"], d["basis"]
        prev_dose, prev_freq = _none_if_blank(d["previous_dose"]), _none_if_blank(d["previous_frequency"])
        checks = []
        if tag in NEEDS_DH and not dh:
            checks.append("missing_dh_cite")
        if basis == "documented" and not dis:
            checks.append("missing_discharge_cite")
        if basis == "inferred_from_dh" and tag != "continued":
            checks.append("inferred_not_continued")
        if d["conflict"] and len({lid for c in dis for lid in c["lines"]}) < 2:
            checks.append("conflict_one_side")
        if tag in ("increased", "decreased") and not prev_dose:
            checks.append("previous_missing")
        if (prev_dose or prev_freq) and tag not in CHANGE_TAGS:
            checks.append("previous_on_unchanged_tag")
        key = d["drug"].strip().lower()
        if key in seen:
            checks.append("duplicate_drug")
        seen.add(key)

        counts["entries"] += 1
        counts["entries_unverified"] += status != "verified"
        counts["entries_flagged"] += bool(checks)
        entries.append({
            "entry_id": f"reconciliation.{i}",
            "drug": d["drug"],
            "tag": tag,
            "basis": basis,
            "dose": _none_if_blank(d["dose"]),
            "route": _none_if_blank(d["route"]),
            "frequency": _none_if_blank(d["frequency"]),
            "previous": ({"dose": prev_dose, "frequency": prev_freq}
                         if (prev_dose or prev_freq) else None),
            "conflict": d["conflict"],
            "dh_cites": [{k: v for k, v in c.items() if k != "role"} for c in dh],
            "discharge_cites": [{k: v for k, v in c.items() if k != "role"} for c in dis],
            "citation_status": status,
            "reason": reason,
            "checks": checks,
        })
    return entries, counts


def dh_lines_unreconciled(facts: dict, entries: list[dict]) -> list[str]:
    """Lines that a verified pre-admission fact cites and no verified dh cite in
    5a covers — the "silently dropped drug-history drug" signal. Line IDs only."""
    dh_fact_lines = {lid for i in facts["medications"]["pre_admission"] if _is_verified(i)
                     for lid in _cited_lines(i)}
    covered = {lid for e in entries for c in e["dh_cites"] if c["verified"] for lid in c["lines"]}
    return sorted(dh_fact_lines - covered)


def reconcile_medications(inp: dict, ctx: StepContext) -> dict:
    lines, facts, med_rows = _check_input(inp)
    prompt, prompt_sha256 = system_prompt()

    system = [{"text": prompt}]
    if ctx.prompt_caching:
        system.append({"cachePoint": {"type": "default"}})   # notes stay below it, never cached
    request = {
        "modelId": ctx.model_id,
        "system": system,
        "messages": [{"role": "user", "content": [{"text": user_text(lines, facts, med_rows)}]}],
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
        raise StepError("reconciliation_not_object")
    if shape_errors(wire, record_reconciliation_schema()):
        # Constrained decoding makes this impossible short of truncation; fail closed.
        raise StepError("reconciliation_shape_invalid")

    entries, counts = group_reconciliation(wire, lines)
    status = facts["medications"]["discharge_status"]
    check = ("ok" if status not in ("none_required", "not_documented")
             or all(e["tag"] in NOT_ACTIVE for e in entries) else DRUGS_WHERE_NONE_DOCUMENTED)
    unreconciled = dh_lines_unreconciled(facts, entries)
    counts["dh_lines_unreconciled"] = len(unreconciled)
    return {
        "reconciliation": entries,
        "discharge_status": status,
        "reconciliation_check": check,
        "dh_lines_unreconciled": unreconciled,
        "counts": counts,
        "call": call_block(ctx, prompt_sha256, resp, attempts),
    }
