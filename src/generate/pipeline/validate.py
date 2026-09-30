"""Step 3 — validate_facts: the implementation of the record_facts tool (ADR-009 [A4]).

For every citation in the facts object: resolve its line IDs against the line
index, check the quote appears verbatim in those lines, and cap the quote at
200 characters. A citation that fails is marked, with a fixed reason — it is
NEVER repaired (F4: a repair asks the model to find support for what it already
wrote, HAZ-13's shape). Then list the note lines that no verified citation
covers, so an omission by extraction is visible (F8, HAZ-07).

Honest limit (ADR-009 Q4): this proves a quote EXISTS in the cited lines, not
that the value was read correctly from it. "no penicillin allergy", cited
correctly, with the value "penicillin allergy" passes. HAZ-18 is not closed here.

"Verbatim" means equal after this narrow normalisation, applied identically to
the quote and to the cited text:
  - Unicode NFC
  - curly single/double quotes -> straight
  - every run of whitespace (incl. NBSP, tab, and the join between cited lines) -> one space
  - trim both ends
Case-sensitive. Nothing else — add a rule only when a real run shows a false
flag it would fix. (Not the safety-net gate's _normalise: that lowercases, so
"Mg" (magnesium) would match "mg".)
"""

from __future__ import annotations

import copy
import re
import unicodedata

from .errors import StepError
from .schemas import FIELD_NAMES, shape_errors

QUOTE_MAX_CHARS = 200     # DPIA / ADR-009 (b): trace quotes <= 200 characters, enforced in code
CITE_MAX_LINES = 3        # a quote may cross a wrap; three lines is a wrap, more is a paragraph

_LINE_ID = re.compile(r"^L\d{3}$")
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'",
                         "“": '"', "”": '"', "„": '"', "‟": '"'})
_WS = re.compile(r"\s+")

# Fixed reasons — safe to trace, safe to log.
REASONS = (
    "missing_lines", "malformed_line_id", "line_out_of_range", "cite_too_many_lines",
    "non_consecutive_lines", "quote_too_long", "empty_quote", "quote_not_found",
    "missing_cites", "contradiction_one_side",
)


def normalise(text: str) -> str:
    t = unicodedata.normalize("NFC", text).translate(_QUOTES)
    return _WS.sub(" ", t).strip()


def verify_cite(cite: dict, lines: dict) -> dict:
    """Return a copy of `cite` with `verified` and `reason` added. Public because
    step 5a's citations are re-verified with this same function (ADR-009)."""
    ids = cite.get("lines") or []
    quote = cite.get("quote", "")
    out = {"lines": list(ids), "quote": quote, "verified": False, "reason": None}

    if not ids:
        out["reason"] = "missing_lines"
        return out
    if any(not isinstance(i, str) or not _LINE_ID.match(i) for i in ids):
        out["reason"] = "malformed_line_id"
        return out
    if any(i not in lines for i in ids):
        out["reason"] = "line_out_of_range"
        return out
    if len(ids) > CITE_MAX_LINES:
        out["reason"] = "cite_too_many_lines"
        return out
    nums = [int(i[1:]) for i in ids]
    if any(b != a + 1 for a, b in zip(nums, nums[1:])):
        out["reason"] = "non_consecutive_lines"   # also catches duplicates and reversed order
        return out
    if len(quote) > QUOTE_MAX_CHARS:
        out["quote"] = ""                          # dropped, not truncated: truncating then passing is a repair
        out["reason"] = "quote_too_long"
        return out
    needle = normalise(quote)
    if not needle:
        out["reason"] = "empty_quote"              # "" is a substring of everything
        return out
    haystack = normalise(" ".join(lines[i] for i in ids))
    if needle not in haystack:
        out["reason"] = "quote_not_found"
        return out
    out["verified"] = True
    return out


def _check_item(item: dict, fact_id: str, lines: dict, covered: set, counts: dict,
                need_two_lines: bool = False) -> None:
    """Annotate one {value, cites} item in place (on our own copy)."""
    item["fact_id"] = fact_id
    item["cites"] = [verify_cite(c, lines) for c in item["cites"]]
    counts["cites_total"] += len(item["cites"])
    good = [c for c in item["cites"] if c["verified"]]
    counts["cites_verified"] += len(good)
    for c in good:
        covered.update(c["lines"])

    if not item["cites"]:
        reason = "missing_cites"
    elif len(good) < len(item["cites"]):
        reason = next(c["reason"] for c in item["cites"] if not c["verified"])
    elif need_two_lines and len({i for c in good for i in c["lines"]}) < 2:
        reason = "contradiction_one_side"
    else:
        reason = None
    item["citation_status"] = "verified" if reason is None else "citation_unverified"
    item["reason"] = reason
    if reason:
        counts["items_unverified"] += 1


def validate_facts(inp: dict) -> dict:
    """{"facts": <record_facts object>, "lines": <line index>} ->
    {"facts": annotated copy, "uncited_lines": [...], "counts": {...}}"""
    if not isinstance(inp, dict) or not isinstance(inp.get("lines"), dict) or "facts" not in inp:
        raise StepError("bad_input")
    lines = inp["lines"]
    if shape_errors(inp["facts"]):
        # Live, constrained decoding makes this impossible short of truncation
        # (which fails step 2 first). Here it guards fixture inputs. Fail closed.
        raise StepError("facts_shape_invalid")

    facts = copy.deepcopy(inp["facts"])   # never mutate the caller's object
    covered: set[str] = set()
    counts = {"cites_total": 0, "cites_verified": 0, "items_unverified": 0, "fields_inconsistent": 0}

    for name in FIELD_NAMES:
        field = facts["fields"][name]
        for i, item in enumerate(field["items"]):
            _check_item(item, f"{name}.{i}", lines, covered, counts)
        # Status and items must agree. Flag, don't fix.
        if field["status"] == "not_documented" and field["items"]:
            field["field_check"] = "items_on_not_documented"
        elif field["status"] != "not_documented" and not field["items"]:
            field["field_check"] = "status_without_items"
        else:
            field["field_check"] = "ok"
        if field["field_check"] != "ok":
            counts["fields_inconsistent"] += 1

    meds = facts["medications"]
    for group in ("pre_admission", "discharge"):
        for i, item in enumerate(meds[group]):
            _check_item(item, f"medications.{group}.{i}", lines, covered, counts)

    for i, item in enumerate(facts["documented_advice"]):
        _check_item(item, f"documented_advice.{i}", lines, covered, counts)
    for i, item in enumerate(facts["contradictions"]):
        _check_item(item, f"contradictions.{i}", lines, covered, counts, need_two_lines=True)

    # resus is one object with its own cites. A claimed form, discussion or status
    # needs a verified citation; step 4 forces documented_but_absent otherwise [A2].
    resus = facts["resus"]
    claims = (resus["form_or_discussion_documented"] or resus["status_documented"] != "not_documented"
              or resus["changed"] != "not_documented")
    if claims or resus["cites"]:
        _check_item(resus, "resus", lines, covered, counts)
    else:
        resus["fact_id"] = "resus"
        resus["citation_status"] = "verified"   # nothing claimed, nothing to verify
        resus["reason"] = None

    suspicious = []
    for i, cite in enumerate(facts["suspicious_text"]):
        checked = verify_cite(cite, lines)
        checked["fact_id"] = f"suspicious_text.{i}"
        counts["cites_total"] += 1
        if checked["verified"]:
            counts["cites_verified"] += 1
            covered.update(checked["lines"])
        suspicious.append(checked)
    facts["suspicious_text"] = suspicious

    # Coverage: non-blank lines no VERIFIED citation touches. An unverified cite
    # does not prove the line was read, so it does not count as coverage.
    uncited = [lid for lid, text in lines.items() if text.strip() and lid not in covered]

    return {"facts": facts, "uncited_lines": uncited, "counts": counts}
