"""Step 3b — retrieve_evidence: the retrieval step (ADR-009 step table row 3b, [A7]).

Builds two evidence blocks, medication and resuscitation, keyed by line ID:

  1. lines that a VERIFIED citation in the step-3 facts touches ("cited"), and
  2. lines code matches on its own, whatever extraction did — so a line the
     model missed is still retrieved (F8, HAZ-07).

Output is line IDs, the fixed names of the rules that matched, the fact_ids that
cite the line, and whether step 1 flagged the line. NEVER line text: a consumer
(step 4, step 5a) looks the text up in "lines" itself. That keeps this step's
whole output safe to trace.

Pattern rules, decided 2 Oct 2026 (W3):
- Narrow on purpose, and every match on the 18 canary scenarios is pinned in
  tests/test_pipeline_retrieve.py, the guard's approach. A resus match that
  extraction did not cite forces step 4 to documented_but_absent [A2], so a
  loose pattern costs a correct route, not just noise.
- "ReSPECT" is matched case-SENSITIVELY ("ReSPECT" / "RESPECT"): lower-case
  "respect" is ordinary prose ("respect the patient's wishes").
- "resus" followed by bay / room / area is a place in ED, not a status.
- "ceiling of care" is NOT a resus term: it is escalation, not resuscitation
  status, and extraction may correctly leave it uncited.
- DH and TTO/TTA are matched case-sensitively (as written in notes); the
  spelled-out headers case-insensitively.
- A medication header pulls in at most 2 following lines, while the previous
  line does not end in "." and the next is not blank, a "Day N" entry or a
  header of its own — drug lists wrap mid-list (S1, S10, A5). This is a recall
  aid, not completeness: step 5a also receives the full line index.
"""

from __future__ import annotations

import re

from .errors import StepError

# Fixed vocabulary — safe to trace.
CITED = "cited"
RESUS_TERM = "resus_term"
MED_HEADER = "med_header"
MED_CONTINUATION = "med_continuation"

MAX_CONTINUATION = 2

_RESUS_PATTERNS = (
    re.compile(r"\bDNA[ -]?CPR\b|\bDNAR\b|\bCPR\b"
               r"|\bresus(?:citat\w*)?\b(?!\s+(?:bay|room|area)\b)", re.IGNORECASE),
    re.compile(r"\b(?:ReSPECT|RESPECT)\b"),                      # case-sensitive
)

_MED_PATTERNS = (
    re.compile(r"\bDH\b|\bTT[OA]s?\b"),                          # case-sensitive
    re.compile(r"\bdrug history\b"
               r"|\bdischarge (?:medications?|meds)\b"
               r"|\b(?:medications?|meds) on discharge\b"
               r"|\bcontinue (?:all )?(?:regular|usual) (?:medications?|meds)\b"
               r"|\bno (?:meds|medications?)\b",                 # S4: "No meds needed" (none_required)
               re.IGNORECASE),
)

_DAY_ENTRY = re.compile(r"^\s*Day\s+\d", re.IGNORECASE)


def _matches(patterns, text: str) -> bool:
    return any(p.search(text) for p in patterns)


def _verified_lines(items) -> dict[str, list[str]]:
    """line ID -> fact_ids whose VERIFIED citations touch it. An unverified
    citation proves nothing about the line (same rule as step 3's coverage)."""
    out: dict[str, list[str]] = {}
    for item in items:
        for c in item.get("cites", []):
            if c.get("verified") is True:
                for lid in c["lines"]:
                    ids = out.setdefault(lid, [])
                    if item["fact_id"] not in ids:
                        ids.append(item["fact_id"])
    return out


def _block(cited: dict[str, list[str]], matched: dict[str, str], flagged: set[str]) -> list[dict]:
    rows = []
    for lid in sorted(set(cited) | set(matched)):
        sources = ([CITED] if lid in cited else []) + ([matched[lid]] if lid in matched else [])
        rows.append({"line": lid, "sources": sources, "fact_ids": cited.get(lid, []),
                     "flagged": lid in flagged})
    return rows


def _match_medication(lines: dict) -> dict[str, str]:
    ids = sorted(lines)
    found: dict[str, str] = {}
    for i, lid in enumerate(ids):
        if not _matches(_MED_PATTERNS, lines[lid]):
            continue
        found[lid] = MED_HEADER
        prev = lines[lid]
        for nxt in ids[i + 1:i + 1 + MAX_CONTINUATION]:
            text = lines[nxt]
            if (prev.rstrip().endswith(".") or not text.strip() or _DAY_ENTRY.match(text)
                    or _matches(_MED_PATTERNS, text)):
                break
            found.setdefault(nxt, MED_CONTINUATION)
            prev = text
    return found


def retrieve_evidence(inp: dict) -> dict:
    """{"facts": step-3 facts (grouped form), "lines": line index, "flags": step-1 flags}
    -> {"medication": [...], "resus": [...], "counts": {...}}

    Each row: {"line", "sources", "fact_ids", "flagged"}, in line order.
    "flags" is required (an empty list when step 1 flagged nothing) so a caller
    cannot silently drop it — step 4 needs it [decision 2 Oct 2026: guard-flagged
    lines are retrieved but never force the route or get quoted]."""
    if not isinstance(inp, dict):
        raise StepError("bad_input")
    facts, lines, flags = inp.get("facts"), inp.get("lines"), inp.get("flags")
    if (not isinstance(facts, dict) or not isinstance(lines, dict) or not isinstance(flags, list)
            or not isinstance(facts.get("medications"), dict) or not isinstance(facts.get("resus"), dict)):
        raise StepError("bad_input")
    try:
        flagged = {f["line"] for f in flags}
        meds = facts["medications"]
        med_cited = _verified_lines(meds["pre_admission"] + meds["discharge"])
        resus_cited = _verified_lines([facts["resus"]])
    except (KeyError, TypeError):
        raise StepError("bad_input") from None

    resus_matched = {lid: RESUS_TERM for lid, text in lines.items() if _matches(_RESUS_PATTERNS, text)}

    medication = _block(med_cited, _match_medication(lines), flagged)
    resus = _block(resus_cited, resus_matched, flagged)
    return {
        "medication": medication,
        "resus": resus,
        "counts": {
            "medication_lines": len(medication),
            "resus_lines": len(resus),
            # Lines code found that no verified citation touches: the recall signal
            # W5 scores, and (for resus) what step 4 may force on.
            "medication_matched_not_cited": sum(1 for r in medication if CITED not in r["sources"]),
            "resus_matched_not_cited": sum(1 for r in resus if CITED not in r["sources"]),
        },
    }
