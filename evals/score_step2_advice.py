"""Step-level metric for step 2 (extract_facts): documented advice vs gold (W5).

The W4 pipeline batch failed safety-netting 16/90 times, all on ROUTE — and the
route is decided by what step 2 extracts as `documented_advice`. 5b's score says
THAT the route was wrong; this says WHERE: step 2 never extracted the advice,
step 2 extracted it but step 3 rejected the citation, or step 2 extracted
something that is not advice.

Inputs per record: the gold record, and the facts object after step 3
(`steps.validate_facts.facts`). Step 3 annotates every item step 2 produced and
drops none, so its `documented_advice` IS step 2's output, plus a verdict per
item. Two stages are scored from it:

  extracted   every item step 2 produced                     (the model alone)
  verified    citation_status == "verified" and every cite   (what reaches 5b)
              verified

MATCHING. An item matches a gold advice item when the note lines it cites
overlap that gold item's cited lines. Line IDs, never text: the metric reads no
note text and writes none. Overlap is generous on purpose — an item citing a
three-line span that includes the gold line counts — so precision here is an
upper bound; a wrong VALUE on the right line is the rubric's job (D2), not this.

  recall      gold items matched by >= 1 item / gold items     (None if no gold items)
  precision   items matching >= 1 gold item / items            (None if no items)
  route       "documented" if >= 1 item at that stage, else "fallback" — 5b's
              rule, so the verified route is the route 5b takes

ROUTE ERRORS (verified route != gold), by the stage that caused them:
  not_extracted          gold documented; step 2 produced no item
  lost_at_verification   gold documented; step 2 produced items, step 3 verified none
  non_advice_as_advice   gold fallback; a verified item reached 5b
And one route that is right for the wrong reason:
  right_route_wrong_item gold documented, verified route documented, but no
                         verified item matches a gold item (recall_verified 0)

Not covered: whether the item's VALUE says what the line says (D2, the rubric);
seek-help triggers (score_safety_net (iii)); v1, which has no step 2.

Results carry the scenario ID, counts, line IDs and fixed labels — never note text.
"""

from __future__ import annotations

METRIC_VERSION = 1
ROUTE_ERRORS = ("not_extracted", "lost_at_verification", "non_advice_as_advice")


def _lines(item: dict) -> set[str]:
    return {lid for c in item.get("cites") or [] for lid in c.get("lines") or []}


def _verified(item: dict) -> bool:
    cites = item.get("cites") or []
    return (item.get("citation_status") == "verified" and bool(cites)
            and all(c.get("verified") is True for c in cites))


def _stage(items: list[dict], gold_items: list[set[str]]) -> dict:
    item_lines = [_lines(i) for i in items]
    correct = sum(1 for il in item_lines if any(il & g for g in gold_items))
    matched = sum(1 for g in gold_items if any(il & g for il in item_lines))
    return {
        "n_items": len(items), "n_correct": correct,
        "n_gold": len(gold_items), "n_gold_matched": matched,
        "recall": matched / len(gold_items) if gold_items else None,
        "precision": correct / len(items) if items else None,
        "route": "documented" if items else "fallback",
    }


def score(gold: dict, facts: dict) -> dict:
    sn = gold["safety_net"]
    gold_items = [{lid for c in a["cites"] for lid in c["lines"]} for a in sn["advice"]]
    items = list(facts.get("documented_advice") or [])
    ext = _stage(items, gold_items)
    ver = _stage([i for i in items if _verified(i)], gold_items)

    gold_route = sn["route"]
    error = None
    if ver["route"] != gold_route:
        if gold_route == "fallback":
            error = "non_advice_as_advice"
        elif ext["n_items"] == 0:
            error = "not_extracted"
        else:
            error = "lost_at_verification"
    wrong_item = (gold_route == "documented" and ver["route"] == "documented"
                  and ver["n_gold_matched"] == 0)
    return {
        "metric": "step2_advice", "metric_version": METRIC_VERSION,
        "scenario_id": gold["scenario_id"],
        "gold_route": gold_route,
        "extracted": ext, "verified": ver,
        "route_correct": error is None,
        "route_error": error,
        "right_route_wrong_item": wrong_item,
    }


def score_record(record: dict, gold: dict) -> dict:
    """A saved pipeline run record. No step-3 facts -> `not_scored`, never a pass."""
    facts = ((record.get("steps") or {}).get("validate_facts") or {}).get("facts")
    if not isinstance(facts, dict):
        return {"metric": "step2_advice", "metric_version": METRIC_VERSION,
                "scenario_id": gold["scenario_id"], "not_scored": "no_step_3_facts"}
    return score(gold, facts)


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def summarise(results: list[dict]) -> dict:
    """Pooled over scored results (micro: sum of matches / sum of items)."""
    scored = [r for r in results if "not_scored" not in r]
    out = {"n_records": len(results), "n_scored": len(scored),
           "route_correct": sum(r["route_correct"] for r in scored),
           "route_errors": {e: sum(r["route_error"] == e for r in scored) for e in ROUTE_ERRORS},
           "right_route_wrong_item": sum(r["right_route_wrong_item"] for r in scored)}
    for stage in ("extracted", "verified"):
        s = [r[stage] for r in scored]
        out[stage] = {
            "recall": _ratio(sum(x["n_gold_matched"] for x in s), sum(x["n_gold"] for x in s)),
            "precision": _ratio(sum(x["n_correct"] for x in s), sum(x["n_items"] for x in s)),
            "n_items": sum(x["n_items"] for x in s),
            "n_gold": sum(x["n_gold"] for x in s),
        }
    return out
