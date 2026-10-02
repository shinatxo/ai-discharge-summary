"""Step 4 — route_resus: the resuscitation block, rendered by code (ADR-009 [A2], (e) §2a).

No model writes any part of this block. Code picks one of three routes and
renders fixed wording from the step-3 enums plus quotes found in the notes:

  documented            a status (DNACPR / for resuscitation / other) with verified
                        citations, and every resus line code found is cited
  documented_but_absent a form or discussion is documented but no status is
                        transcribed (S12) — OR the route is FORCED here because
                        the evidence does not hold up (below)
  absent                nothing claimed, nothing found

Forced to documented_but_absent [A2] — failure sends the clinician to the notes,
never to a guessed status:
  resus_citation_unverified  extraction claims something about resus and a
                             citation for it failed step 3
  resus_lines_uncited        code matched a resus line (step 3b) that no
                             verified citation touches
  resus_claim_inconsistent   verified resus citations with no claim at all

Guard-flagged lines (decision 4, 2 Oct 2026) never force the route and are never
quoted: an injection that names DNACPR must not be able to suppress a
documented status, and its text must not be copied into a clinical document.

Quotes are either a verified citation's quote (already checked verbatim in
step 3) or, for a line code found, the line's own text — never model wording.
A quote over 200 characters is not printed (the line ID still is).
"""

from __future__ import annotations

from .errors import StepError
from .validate import QUOTE_MAX_CHARS, normalise

DOCUMENTED = "documented"
DOCUMENTED_BUT_ABSENT = "documented_but_absent"
ABSENT = "absent"

NOT_FORCED = "no"
CITATION_UNVERIFIED = "resus_citation_unverified"
LINES_UNCITED = "resus_lines_uncited"
CLAIM_INCONSISTENT = "resus_claim_inconsistent"

HEADING = "RESUSCITATION STATUS"
CHANGED_MARKER = "*** CHANGED DURING ADMISSION ***"

_STATUS_LABEL = {
    "dnacpr": "DNACPR.",
    "for_resuscitation": "For resuscitation.",
    "other_documented": "Documented (see source).",   # never names a status code cannot map
}
_CHANGE_TEXT = {
    "yes": CHANGED_MARKER,
    "no": "No change during admission.",
    "not_documented": "",              # say nothing rather than "no change"
}

# [A2] — the natural documented_but_absent route (S12: form exists, recommendation
# not transcribed). Fixed wording from ADR-009.
FORM_NOT_TRANSCRIBED = (
    "A resuscitation form or discussion is documented ({sources}). Its recommendation "
    "is not transcribed in these notes. Confirm against the completed form before "
    "relying on any resuscitation decision."
)
# Approved by the author as CSO, 2 Oct 2026 — the FORCED route. [A2]'s sentence "its
# recommendation is not transcribed" is false when the notes do state a status
# but the evidence for it did not verify (S1 with "For resus." uncited), so the
# forced route says what is actually true: the status could not be confirmed.
NOT_CONFIRMED = (
    "Resuscitation is recorded in these notes{sources}, but the status could not be "
    "confirmed from them. Check the notes and any completed form before relying on "
    "any resuscitation decision."
)
ABSENT_TEXT = "Not documented."
FLAGGED_NOT_QUOTED = "(flagged as possible instruction text, not quoted)"


def _span(ids: list[str]) -> str:
    return f"[{ids[0]}]" if len(ids) == 1 else f"[{ids[0]}–{ids[-1]}]"


def _source(ids: list[str], quote: str, flagged: set[str]) -> str:
    if any(i in flagged for i in ids):
        return f"{_span(ids)} {FLAGGED_NOT_QUOTED}"
    q = normalise(quote)
    if not q or len(q) > QUOTE_MAX_CHARS:
        return _span(ids)
    return f'{_span(ids)}: "{q}"'


def route_resus(inp: dict) -> dict:
    """{"facts": step-3 facts, "evidence": step-3b output, "lines": line index}
    -> {"route", "forced", "status", "changed", "cited_lines", "rendered"}"""
    if not isinstance(inp, dict):
        raise StepError("bad_input")
    facts, evidence, lines = inp.get("facts"), inp.get("evidence"), inp.get("lines")
    if (not isinstance(facts, dict) or not isinstance(evidence, dict) or not isinstance(lines, dict)
            or not isinstance(facts.get("resus"), dict) or not isinstance(evidence.get("resus"), list)):
        raise StepError("bad_input")
    try:
        resus = facts["resus"]
        status, changed = resus["status_documented"], resus["changed"]
        form = resus["form_or_discussion_documented"]
        cites = resus["cites"]
        rows = evidence["resus"]
        flagged = {r["line"] for r in rows if r["flagged"]}
        uncited = [r["line"] for r in rows if "cited" not in r["sources"] and not r["flagged"]]
        if status not in _STATUS_LABEL and status != "not_documented":
            raise StepError("bad_input")
        if changed not in _CHANGE_TEXT:
            raise StepError("bad_input")
    except (KeyError, TypeError):
        raise StepError("bad_input") from None

    verified = [c for c in cites if c.get("verified") is True]
    claims = form or status != "not_documented" or changed != "not_documented"

    if claims and (not cites or len(verified) < len(cites)):
        forced = CITATION_UNVERIFIED
    elif uncited:
        forced = LINES_UNCITED
    elif (verified and not claims) or (claims and not form and status == "not_documented"):
        # cites with no claim, or a "changed" with neither a status nor a form
        forced = CLAIM_INCONSISTENT
    else:
        forced = NOT_FORCED

    if forced != NOT_FORCED:
        # Cite what is checkable: verified quotes, then the uncited lines code
        # found, quoted from the notes themselves. Never the claimed status.
        # In note order, so the clinician reads them as the notes run.
        found = [(c["lines"][0], _source(c["lines"], c["quote"], flagged)) for c in verified]
        found += [(lid, _source([lid], lines.get(lid, ""), flagged)) for lid in uncited]
        parts = [text for _, text in sorted(found)]
        cited_lines = sorted({i for c in verified for i in c["lines"]} | set(uncited))
        body = NOT_CONFIRMED.format(sources=f" ({'; '.join(parts)})" if parts else "")
        return _out(DOCUMENTED_BUT_ABSENT, forced, "not_documented", "not_documented", cited_lines, body)

    cited_lines = sorted({i for c in verified for i in c["lines"]})
    sources = "; ".join(_source(c["lines"], c["quote"], flagged) for c in verified)

    if status != "not_documented":
        first = " ".join(t for t in (_STATUS_LABEL[status], _CHANGE_TEXT[changed]) if t)
        return _out(DOCUMENTED, NOT_FORCED, status, changed, cited_lines, f"{first}\nSource: {sources}")
    if claims:
        # form/discussion documented, no status transcribed — the narrowed §2a.
        return _out(DOCUMENTED_BUT_ABSENT, NOT_FORCED, "not_documented", changed, cited_lines,
                    FORM_NOT_TRANSCRIBED.format(sources=sources))
    return _out(ABSENT, NOT_FORCED, "not_documented", "not_documented", [], ABSENT_TEXT)


def _out(route, forced, status, changed, cited_lines, body):
    return {"route": route, "forced": forced, "status": status, "changed": changed,
            "cited_lines": cited_lines, "rendered": f"{HEADING}\n{body}"}
