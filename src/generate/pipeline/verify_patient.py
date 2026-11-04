"""Step 7v — verify_patient (ADR-010, decisions V1–V7 of 4 Nov 2026; HAZ-17).

MODEL 5. One Bedrock Converse call with one strict, forced tool,
`record_verification` (schemas.py). The verifier checks every PART C prose
sentence against the PART A sections that sentence's C section cites (Q4: a
source_id is a claim, and this step tests it), and lists the never-drop items
those sections give that the C section does not reach. It never sees the notes,
the facts, or the code-rendered boxes.

Sentences are numbered by CODE (meaning.sentences, the splitter every step-7
check and the scorer use — one copy), so the model cannot skip one: every
numbered sentence must come back with at least one item, or the step fails
closed. A sentence is either `supported` (its only item) or carries one item per
fault (v0.2, 4 Nov 2026: one sentence can hold two independent defects — S4 r2's
swab and its added cause). An evidence reference to a PART A section that was not
given fails closed; a `supported` verdict that rests on a section its C section
does not cite cannot be accepted — it counts as flagged (CSO audit, 4 Nov). Other
faults in the verifier's own output are flagged, never repaired.

The sequence — verify, regenerate step 7 once with the findings, verify again,
then hold back any section still flagged — is `verified_patient_version()`
(V3). It takes the two step calls as functions, so the eval runner and the
orchestrator (item 5) run the same sequence through run_step.

run_step("verify_patient", verify_input(step7_input, step7_output), ctx=StepContext(...)):
  in:  {"a_sections": [{"section_id", "heading", "text"}],
        "c_sections": [{"section_id", "heading", "source_ids", "sentences": [{"sentence_id", "text"}]}]}
  out: {"verdicts": [{sentence_id, c_section_id, heading, text, verdict, reason, a_section_id, span,
                      evidence_valid}]   (one per supported sentence; one per fault otherwise),
        "unreached": [{c_section_id, heading, a_section_id, reason, span}],
        "flagged": int, "checks": [{check, sentence_id, c_section_id, token}],
        "counts": {...}, "call": {...}}
Fails closed with a fixed code: bad_input, nothing_to_verify, no_client (registry),
max_tokens, unexpected_stop_reason, no_tool_call, verification_not_object,
verification_shape_invalid, verdict_missing, verdict_conflict, unknown_sentence_id,
unknown_section_id, unknown_a_section, throttled, read_timeout, request_rejected, request_invalid,
bedrock_error.

What the output may carry: verdicts, fixed reason words, IDs, and spans — words
copied from PART C or PART A prose (the same class of content as steps 6 and 7's
output; never the notes). `checks` carries fixed words and IDs only.

Honest limit: this is a model judging a model. Its recall and false-flag rate
are what offline calibration measures (ADR-010 V5) — on already-ruled outputs,
against a bar written before calibration ran. Until that bar is met, no live
generation relies on it.

The caller builds the client with read_timeout 60 s and max_attempts 1, as
step 7. Nothing here creates an AWS client (package rule).
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Callable

from . import compose_patient as CP7
from . import meaning as MN
from .context import StepContext
from .converse import call_block, converse_with_throttle_retry
from .errors import StepError
from .schemas import PATIENT_HEADINGS, VERIFY_REASONS, VERIFY_VERDICTS, record_verification_schema, shape_errors

TOOL_NAME = "record_verification"
TOOL_DESCRIPTION = (
    "Record one verdict for every numbered leaflet sentence, and one item for each "
    "must-keep item the leaflet does not reach. Call exactly once."
)

MAX_TOKENS = 4000        # ~20-30 sentences x ~40 tokens + unreached items; truncation fails the step
TEMPERATURE = 0
READ_TIMEOUT_S = 60      # as step 7 (ADR-010 V6)

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "verify_patient.md"
PROMPT_MARKER = "## SYSTEM PROMPT"

SUPPORTED = "supported"
UNREACHED = "unreached"
NEVER_DROP = "never_drop_item"
PROSE_HEADINGS = tuple(h for h in PATIENT_HEADINGS if h not in CP7.BOXES)

# O1 (ADR-010): the fixed line that replaces a C section still flagged after the one
# regeneration. DRAFT — to be approved by the CSO before any batch.
HELD_BACK_TEXT = "Your care team will go through this part with you before you leave."
# (Checked 4 Nov: raises none of step 7's pattern checks — "Please ask them." tripped _ADVICE and was
# dropped. It is still a fixed sentence the scorer must treat as pre-ruled once the CSO approves it, O1.)

CHECKS = ("uncited_source", "span_not_found", "span_on_supported", "item_repeated",
          "reason_mismatch", "unreached_with_sentence", "verdict_without_sentence")


def acceptable(v: dict) -> bool:
    """A sentence item the leaflet may keep: supported, on a source its C section cites."""
    return v["verdict"] == SUPPORTED and v.get("evidence_valid", True)


def tool_config() -> dict:
    """One strict tool, forced. Built fresh each call (the schema is a deep copy)."""
    return {
        "tools": [{"toolSpec": {
            "name": TOOL_NAME,
            "description": TOOL_DESCRIPTION,
            "inputSchema": {"json": record_verification_schema()},
            "strict": True,
        }}],
        "toolChoice": {"tool": {"name": TOOL_NAME}},
    }


@lru_cache(maxsize=1)
def system_prompt() -> tuple[str, str]:
    """(text sent, its SHA-256) — only the text after the marker, as steps 2, 5a, 6 and 7."""
    raw = PROMPT_PATH.read_text(encoding="utf-8")
    if PROMPT_MARKER not in raw:
        raise RuntimeError("verify_patient.md is missing the SYSTEM PROMPT marker")
    text = raw.split(PROMPT_MARKER, 1)[1].strip()
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


# --- input ----------------------------------------------------------------------

def verify_input(step7_input: dict, step7_output: dict) -> dict:
    """Step 7's input and output -> step 7v's input. Code, deterministic: the A
    sections step 7 was given (never a revision block), and each C prose section
    with its sentences numbered by meaning.sentences. Boxes are not verified."""
    try:
        a = [{"section_id": s["section_id"], "heading": s["heading"], "text": s["text"]}
             for s in step7_input["sections"]]
        c = []
        for s in step7_output["sections"]:
            if s["heading"] in CP7.BOXES:
                continue
            sents = MN.sentences(s["text"])
            c.append({"section_id": s["section_id"], "heading": s["heading"],
                      "source_ids": list(s["source_ids"]),
                      "sentences": [{"sentence_id": f"{s['section_id']}.s{i}", "text": t}
                                    for i, t in enumerate(sents, 1)]})
        return {"a_sections": a, "c_sections": c}
    except (KeyError, TypeError):
        raise StepError("bad_input") from None


def _check_input(inp) -> dict:
    # Exactly {"a_sections", "c_sections"}: step 7v never receives the notes, the facts or the boxes.
    if not isinstance(inp, dict) or set(inp) != {"a_sections", "c_sections"}:
        raise StepError("bad_input")
    a, c = inp["a_sections"], inp["c_sections"]
    if not isinstance(a, list) or not isinstance(c, list):
        raise StepError("bad_input")
    a_ids = set()
    for s in a:
        if (not isinstance(s, dict) or set(s) != {"section_id", "heading", "text"}
                or not all(isinstance(s[k], str) for k in s)
                or s["heading"] not in CP7.GIVEN_A_HEADINGS or s["section_id"] in a_ids):
            raise StepError("bad_input")
        a_ids.add(s["section_id"])
    c_ids, sent_ids = set(), set()
    for s in c:
        if (not isinstance(s, dict) or set(s) != {"section_id", "heading", "source_ids", "sentences"}
                or not isinstance(s["section_id"], str) or s["heading"] not in PROSE_HEADINGS
                or not isinstance(s["source_ids"], list) or not all(isinstance(i, str) for i in s["source_ids"])
                or not isinstance(s["sentences"], list) or s["section_id"] in c_ids):
            raise StepError("bad_input")
        c_ids.add(s["section_id"])
        for t in s["sentences"]:
            if (not isinstance(t, dict) or set(t) != {"sentence_id", "text"}
                    or not all(isinstance(t[k], str) for k in t)
                    or not t["sentence_id"].startswith(s["section_id"] + ".s")
                    or t["sentence_id"] in sent_ids):
                raise StepError("bad_input")
            sent_ids.add(t["sentence_id"])
    if not sent_ids:
        raise StepError("nothing_to_verify")
    return inp


def user_text(inp: dict) -> str:
    """Per C section: its ID and heading, the A sections it cites (ID, heading, text),
    then its numbered sentences."""
    a = {s["section_id"]: s for s in inp["a_sections"]}
    parts = ["LEAFLET SECTIONS"]
    for s in inp["c_sections"]:
        parts += ["", f"=== {s['section_id']} {s['heading']}", "PART A sections it says it used:"]
        cited = [a[i] for i in s["source_ids"] if i in a]
        if not cited:
            parts.append("none")
        for x in cited:
            parts += [f"[{x['section_id']}] {x['heading']}", x["text"]]
        parts.append("Leaflet sentences:")
        parts += [f"{t['sentence_id']}: {t['text']}" for t in s["sentences"]]
    return "\n".join(parts)


# --- output ---------------------------------------------------------------------

def check_verification(wire: dict, inp: dict) -> tuple[list, list, list, dict]:
    """Wire form -> (verdicts, unreached, checks, counts). Fails closed on coverage and
    on references to sections that were not given; flags everything else; never repairs."""
    a = {s["section_id"]: s for s in inp["a_sections"]}
    c = {s["section_id"]: s for s in inp["c_sections"]}
    sent_of = {t["sentence_id"]: (s, t) for s in inp["c_sections"] for t in s["sentences"]}
    checks: list[dict] = []

    def flag(check, sentence_id=None, c_section_id=None, token=None):
        checks.append({"check": check, "sentence_id": sentence_id, "c_section_id": c_section_id, "token": token})

    per: dict[str, list[dict]] = {}
    seen: set = set()
    unreached: list[dict] = []
    for it in wire["items"]:
        sid, cid, aid = it["sentence_id"], it["c_section_id"], it["a_section_id"]
        verdict, reason, span = it["verdict"], it["reason"], it["span"]
        if cid not in c:
            raise StepError("unknown_section_id")
        if aid not in a:
            raise StepError("unknown_a_section")       # evidence must be a section step 7 was given
        sec = c[cid]
        cited = aid in sec["source_ids"]
        if not cited:
            flag("uncited_source", sid or None, cid, token=aid)
        if not sid or verdict == UNREACHED:
            if sid:
                flag("unreached_with_sentence", sid, cid)
            if verdict != UNREACHED:
                flag("verdict_without_sentence", None, cid, token=verdict)
            if reason != NEVER_DROP:
                flag("reason_mismatch", None, cid, token=reason)
            if _norm(span) not in _norm(a[aid]["text"]):
                flag("span_not_found", None, cid)
            unreached.append({"c_section_id": cid, "heading": sec["heading"], "a_section_id": aid,
                              "reason": reason, "span": span, "evidence_valid": cited})
            continue
        if sid not in sent_of or sent_of[sid][0]["section_id"] != cid:
            raise StepError("unknown_sentence_id")
        # The identity includes the evidence (a_section_id), so an item that fails evidence
        # validation is never discarded as a "repeat" of a valid one, in either order (CSO
        # audit 2, 4 Nov 2026: a valid supported item followed by the same item on an uncited
        # section must still leave the sentence flagged).
        sig = (sid, verdict, reason, _norm(span), aid)
        if sig in seen:
            flag("item_repeated", sid, cid)
            continue
        seen.add(sig)
        text = sent_of[sid][1]["text"]
        if verdict == SUPPORTED:
            if reason != "none":
                flag("reason_mismatch", sid, cid, token=reason)
            if span.strip():
                flag("span_on_supported", sid, cid)
        else:
            if reason in ("none", NEVER_DROP):
                flag("reason_mismatch", sid, cid, token=reason)
            if _norm(span) not in _norm(text) or not span.strip():
                flag("span_not_found", sid, cid)
        per.setdefault(sid, []).append(
            {"sentence_id": sid, "c_section_id": cid, "heading": sec["heading"], "text": text,
             "verdict": verdict, "reason": reason, "a_section_id": aid, "span": span, "evidence_valid": cited})
    if set(per) != set(sent_of):
        raise StepError("verdict_missing")
    for items in per.values():
        verdicts_here = {v["verdict"] for v in items}
        if SUPPORTED in verdicts_here and len(verdicts_here) > 1:
            raise StepError("verdict_conflict")         # supported AND at fault: not a verdict
        # several supported items (different evidence) are kept: the sentence is acceptable
        # only if every one of them is (acceptable() per item)

    ordered = [v for s in inp["c_sections"] for t in s["sentences"] for v in per[t["sentence_id"]]]
    faults = [v for v in ordered if v["verdict"] != SUPPORTED]
    ok_sentences = sum(1 for items in per.values() if all(acceptable(v) for v in items))
    counts = {
        "sentences": len(sent_of),
        "supported": ok_sentences,
        "faults": len(faults),
        "supported_uncited": sum(1 for v in ordered if v["verdict"] == SUPPORTED and not v["evidence_valid"]),
        "by_verdict": {k: sum(1 for v in faults if v["verdict"] == k) for k in VERIFY_VERDICTS
                       if k not in (SUPPORTED, UNREACHED)},
        "by_reason": {k: sum(1 for v in faults if v["reason"] == k) for k in VERIFY_REASONS
                      if k not in ("none", NEVER_DROP)},
        "unreached": len(unreached),
        "checks": len(checks),
    }
    return ordered, unreached, checks, counts


def verify_patient(inp: dict, ctx: StepContext) -> dict:
    inp = _check_input(inp)
    prompt, prompt_sha256 = system_prompt()

    system = [{"text": prompt}]
    if ctx.prompt_caching:
        system.append({"cachePoint": {"type": "default"}})
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
        raise StepError("verification_not_object")
    if shape_errors(wire, record_verification_schema()):
        raise StepError("verification_shape_invalid")

    verdicts, unreached, checks, counts = check_verification(wire, inp)
    # every fault, every unreached item, and every supported verdict on an uncited source
    flagged = sum(1 for v in verdicts if not acceptable(v)) + len(unreached)
    return {"verdicts": verdicts, "unreached": unreached, "flagged": flagged,
            "checks": checks, "counts": counts, "call": call_block(ctx, prompt_sha256, resp, attempts)}


# --- the V3 sequence ------------------------------------------------------------

def revision_for(step7_output: dict, verification: dict) -> dict:
    """The `revision` block for step 7's one regeneration: its first draft, and each
    finding as {heading, span, issue} (issue = a reason word; step 7 renders the
    plain-word text from CP7.REVISION_ISSUES — code-fixed wording, O3)."""
    findings = [{"heading": v["heading"], "span": v["span"], "issue": v["reason"]}
                for v in verification["verdicts"] if v["verdict"] != SUPPORTED]
    # supported, but only by a section its C section does not cite: the cited sections do not say it
    findings += [{"heading": v["heading"], "span": v["text"], "issue": "new_fact"}
                 for v in verification["verdicts"] if v["verdict"] == SUPPORTED and not acceptable(v)]
    findings += [{"heading": u["heading"], "span": u["span"], "issue": NEVER_DROP}
                 for u in verification["unreached"]]
    previous = [{"heading": s["heading"], "source_ids": list(s["source_ids"]), "text": s["text"]}
                for s in step7_output["sections"]]
    return {"previous": previous, "findings": findings}


def hold_back(step7_output: dict, verification: dict, step7_input: dict) -> tuple[dict, list[dict]]:
    """Replace the prose of every C section that still holds a non-supported sentence
    with HELD_BACK_TEXT (fail closed); re-run step 7's code checks on the result, so
    the output's checks describe the text that is kept. Unreached items are left
    for the reviewer (8b warning); code adds no prose."""
    bad = {v["c_section_id"] for v in verification["verdicts"] if not acceptable(v)}
    held = [{"section_id": s["section_id"], "heading": s["heading"]}
            for s in step7_output["sections"] if s["section_id"] in bad]
    if not held:
        return step7_output, []
    wire = {"sections": [{"heading": s["heading"], "source_ids": list(s["source_ids"]),
                          "text": HELD_BACK_TEXT if s["section_id"] in bad else s["text"]}
                         for s in step7_output["sections"]]}
    sections, checks, counts = CP7.check_patient_version(wire, step7_input)
    out = dict(step7_output, sections=sections, checks=checks, counts=counts)
    return out, held


def verified_patient_version(step7_input: dict, run7: Callable[[dict], dict],
                             run7v: Callable[[dict], dict]) -> dict:
    """ADR-010 V3: 7 -> 7v; if anything is flagged, one regeneration of 7 with the
    findings -> 7v again -> hold back sections still flagged. Returns the step
    outputs to record: compose_patient (the FINAL leaflet), verify_patient, and —
    when the regeneration ran — compose_patient_first, verify_patient_2 and
    patient_held_back. A StepError from any call propagates (fail closed)."""
    first = run7(step7_input)
    v1 = run7v(verify_input(step7_input, first))
    if not v1["flagged"]:
        return {"compose_patient": first, "verify_patient": v1}
    second = run7(dict(step7_input, revision=revision_for(first, v1)))
    v2 = run7v(verify_input(step7_input, second))
    final, held = hold_back(second, v2, step7_input)
    return {"compose_patient_first": first, "verify_patient": v1, "compose_patient": final,
            "verify_patient_2": v2, "patient_held_back": held}
