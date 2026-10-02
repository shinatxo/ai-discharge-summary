"""Step 5b — select_safety_net (ADR-009 [A1], (e); decision of 1 Oct 2026, CSO; HAZ-01, HAZ-03).

Code, no model. Returns TWO renderings:

  PART A / B  documented advice verbatim with its citations — or, if none is
              documented, the pinned line. Unchanged from (e).
  PART C      the pinned line ALWAYS, with any documented advice above it,
              verbatim. (1 Oct: a record that advice was given — S8's "Safety-net
              advice to parents re fever/feeding/breathing" — is not something a
              parent can act on, so the leaflet must never lose the signpost.)

"Verbatim" means the citation's QUOTE, which step 3 proved is in the notes —
never the model's `value`, which is its own restatement.

Which advice counts as documented:
- an item whose citations ALL verified (step 3's citation_status "verified");
- and none of whose cited lines step 1 flagged — flagged text is never copied
  into a document (decision 4, 2 Oct 2026, as for step 4).
Anything else is left out and counted (`advice_excluded`), never printed:
copying unverified model text into a patient leaflet is the HAZ-01 failure.

Audience is selected on age_group alone (CSO, 24 Sep / 1 Oct): neonate, infant
or child -> paediatric; adult or not_documented -> adult. A misclassified child
gets the adult line, which is still a correct generic signpost.

The pinned lines are the CSO's wording of 1 Oct 2026, verbatim. They name no
symptom, threshold or diagnosis (WS2a §6 item 3) — do not edit them without a
new CSO decision. evals/safety_net_gate.py carries the same two strings until
the gate moves into this package at 8b; a test pins that they are identical.
"""

from __future__ import annotations

from .errors import StepError
from .validate import normalise

ADULT_LINE = (
    "If you have been given a number to call, use that first. Otherwise, if you are "
    "worried, contact your GP or call NHS 111. Call 999 if it is an emergency."
)
PAEDIATRIC_LINE = (
    "If you have been given a number to call, use that first. Otherwise, if you are "
    "worried about your child, contact your GP or call NHS 111. Call 999 if it is an "
    "emergency."
)

PAEDIATRIC_GROUPS = ("neonate", "infant", "child")
AGE_GROUPS = PAEDIATRIC_GROUPS + ("adult", "not_documented")

DOCUMENTED = "documented"
FALLBACK = "fallback"


def _span(ids: list[str]) -> str:
    return f"[{ids[0]}]" if len(ids) == 1 else f"[{ids[0]}–{ids[-1]}]"


def select_safety_net(inp: dict) -> dict:
    """{"facts": step-3 facts, "flags": step-1 flags}
    -> {"route", "audience", "pinned_line", "advice", "advice_excluded",
        "cited_lines", "part_ab", "part_c"}"""
    if not isinstance(inp, dict):
        raise StepError("bad_input")
    facts, flags = inp.get("facts"), inp.get("flags")
    if (not isinstance(facts, dict) or not isinstance(flags, list)
            or not isinstance(facts.get("documented_advice"), list)
            or facts.get("age_group") not in AGE_GROUPS):
        raise StepError("bad_input")
    try:
        flagged = {f["line"] for f in flags}
        advice, excluded = [], 0
        for item in facts["documented_advice"]:
            cites = item["cites"]
            ok = (item["citation_status"] == "verified" and cites
                  and all(c["verified"] is True for c in cites)
                  and not any(lid in flagged for c in cites for lid in c["lines"]))
            if not ok:
                excluded += 1
                continue
            for c in cites:
                advice.append({"fact_id": item["fact_id"], "lines": list(c["lines"]),
                               "quote": normalise(c["quote"])})
    except (KeyError, TypeError):
        raise StepError("bad_input") from None

    paediatric = facts["age_group"] in PAEDIATRIC_GROUPS
    pinned = PAEDIATRIC_LINE if paediatric else ADULT_LINE
    route = DOCUMENTED if advice else FALLBACK

    if advice:
        part_ab = "\n".join(f'- "{a["quote"]}" {_span(a["lines"])}' for a in advice)
        part_c = "\n".join(a["quote"] for a in advice) + "\n\n" + pinned
    else:
        part_ab = pinned
        part_c = pinned

    return {
        "route": route,
        "audience": "paediatric" if paediatric else "adult",
        "pinned_line": pinned,
        "advice": advice,
        "advice_excluded": excluded,
        "cited_lines": sorted({lid for a in advice for lid in a["lines"]}),
        "part_ab": part_ab,
        "part_c": part_c,
    }
