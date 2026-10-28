"""Step 6 — compose_clinical: step-level scoring for a pipeline batch
(WS1a step build piece 2, 28 Oct 2026). Called by score_batch.py for a batch
whose runner includes step 6; never run on its own.

What it scores — the pass bar written on 27 Oct 2026, before any step-6 code
(docs/_local/ADR-009-build-record-step-build-p2.md), frozen here before the
first live step-6 call:

  1  reached step 6                 records with a compose_clinical output / all
  2  years added by step 6          the step's own `year_added` (vs its input values)
                                    + years vs the NOTES (rubric v2 D2, rubric_checks),
                                    each attributed: in step 6's input -> step 2, else step 6
  3  boxes / fact_ids               each box exactly once and exact in A and B, no stray
                                    {{...}}; unknown or excluded fact_ids
  4  contradictions in PART A       contradictions_in_a / contradictions_given
  5  resus / advice outside boxes   flagged by code, RULED BY THE CSO (review file)
  6  omission                       field_omitted, gp_action_missing_in_b = 0;
                                    fact coverage k/n reported (no target)
  7  medication text outside box    flagged by code, RULED BY THE CSO
  8  no regression                  NOT here: SAFETY_NET.md, MEDS.md, haz03_check.py

Not measured, reported as such: faithfulness (D2 beyond years, D6, D7 in prose)
and narrative omission — the judge is not validated on the pipeline view
(spot-check κ 0.29, NOT MET). Evidence instead: the CSO blind read
(make_compose_sheets.py), which must be done BEFORE COMPOSE_CLINICAL.md is read.

Why 5 and 7 are code-flagged and CSO-ruled rather than code-decided (stated
27-28 Oct, before the batch; the bar itself is unchanged at 0): the patterns
cannot tell "presented to A&E" from "go to A&E", or "fluid resuscitation" from a
resus status, or an in-hospital drug from a discharge one. A flagged sentence
is a defect only if the CSO rules it so; an unruled one is PENDING, never a pass.
Rulings live in evals/gold/adjudications/<ID>.json under `compose_rulings`.

The JSON results hold labels, headings, fact_ids, years and drug-name tokens;
the review FILE may quote output sentences (as review_safety_net.md does). The
console shows counts only.
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "generate"))
import rubric_checks as RC  # noqa: E402
from pipeline import compose as CMP  # noqa: E402

METRIC_VERSION = 1
DEADLINE_WATCH_S = 200      # 270 s internal deadline − step 7's 60 s read timeout − 10 s (ADR-009 Q3)
RULINGS = ("acceptable", "defect")

# Seek-help / re-presentation / urgency language in a step-6 SENTENCE. Broad on
# purpose — a false flag costs one CSO ruling; a miss is HAZ-01's laundering path.
_ADVICE = re.compile(
    r"(nhs\s*111|\b111\b|\b999\b|\ba\s*&\s*e\b|accident and emergency|emergency department|\bED\b|"
    r"seek (?:help|advice|medical|urgent|review)|come back|return(?:s)? (?:to [a-z ]+ )?if|"
    r"\bre-present\w*|\brepresent(?:s)? (?:if|to|with)\b|red flag|safety[- ]?net|"
    r"if (?:symptoms |it |things |pain )?(?:gets? |get )?worse|\bif [^.]{0,40}\bworsens?\b|"
    r"if (?:you|he|she|they|the patient) (?:develops?|notices?|experiences?|feels?|becomes?)|"
    r"should (?:he|she|they|you|the patient) (?:develop|notice|experience|become))",
    re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_DRUG_TOKEN = re.compile(r"[A-Za-z][A-Za-z\-]{3,}")
_NOT_DRUGS = {"none", "nil", "regular", "usual", "medication", "medications", "meds", "continue",
              "continued", "stop", "stopped", "tablets", "tablet", "dose", "daily", "required",
              "discharge", "home", "known", "drug", "drugs", "history", "listed", "with", "from"}
MED_EXEMPT_HEADINGS = ("treatment", "medication_conflicts")
BOX_HEADINGS = tuple(CMP.BOXES)


def _key(kind: str, part: str, heading: str, text: str) -> str:
    norm = " ".join(text.split()).casefold()
    return hashlib.sha256(f"{kind}|{part}|{heading}|{norm}".encode("utf-8")).hexdigest()[:16]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s.strip()]


def drug_tokens(rec: dict) -> list[str]:
    """Drug names the record knows from steps 3 and 5a — the first word of each
    medication fact value and each 5a `drug` — lowercased, de-duplicated."""
    steps = rec["steps"]
    meds = steps["validate_facts"]["facts"]["medications"]
    words = [i["value"] for g in ("pre_admission", "discharge") for i in meds[g]]
    words += [e["drug"] for e in (steps.get("reconcile_medications") or {}).get("reconciliation", [])]
    out = []
    for w in words:
        m = _DRUG_TOKEN.search(w or "")
        if m and m.group().lower() not in _NOT_DRUGS and m.group().lower() not in out:
            out.append(m.group().lower())
    return out


def review_items(rec: dict) -> list[dict]:
    """Every sentence code flags for a CSO ruling: advice outside {{SAFETY_NET}},
    resus text outside {{RESUS}}, a drug name outside {{MEDICATIONS}} (TREATMENT
    and contradiction sections exempt). Returns items WITH the sentence (for the
    review file); score_record strips it."""
    step = rec["steps"]["compose_clinical"]
    contra = {i["fact_id"] for i in rec["steps"]["validate_facts"]["facts"]["contradictions"]}
    drugs = drug_tokens(rec)
    items = []
    for s in step["sections"]:
        if s["heading"] in BOX_HEADINGS:
            continue
        med_exempt = s["heading"] in MED_EXEMPT_HEADINGS or bool(contra & set(s["fact_ids"]))
        for sent in _sentences(s["text"]):
            found = []
            if _ADVICE.search(sent):
                found.append(("advice", None))
            if any(p.search(sent) for p in CMP._RESUS_PATTERNS):
                found.append(("resus", None))
            if not med_exempt:
                found += [("medication", d) for d in drugs
                          if re.search(rf"\b{re.escape(d)}\b", sent, re.IGNORECASE)]
            for kind, token in found:
                items.append({"kind": kind, "part": s["part"], "heading": s["heading"], "token": token,
                              "key": _key(kind + (f":{token}" if token else ""), s["part"], s["heading"], sent),
                              "sentence": sent})
    return items


def _latency(rec: dict) -> dict:
    by = {c["name"]: c.get("latency_s") for c in rec.get("calls") or []}
    l2, l5, l6 = by.get("extract_facts"), by.get("reconcile_medications"), by.get("compose_clinical")
    crit = None if l2 is None or l6 is None else round(l2 + max(l6, l5 or 0), 3)
    return {"step2_s": l2, "step5a_s": l5, "step6_s": l6, "critical_s": crit}


def score_record(rec: dict, notes: str, rulings: dict) -> dict:
    step = rec["steps"]["compose_clinical"]
    facts = rec["steps"]["validate_facts"]["facts"]
    checks: dict[str, int] = {}
    for c in step["checks"]:
        checks[c["check"]] = checks.get(c["check"], 0) + 1
    box_problems = sum(1 for c in step["checks"]
                       if c["check"] in ("box_not_exact", "stray_placeholder")
                       or (c["check"] in ("required_heading_missing", "heading_repeated")
                           and c["heading"] in BOX_HEADINGS))

    # Years vs the NOTES, with attribution (the rubric's D2 definition).
    given = [i["value"] for n in facts["fields"] for i in facts["fields"][n]["items"]]
    given += [i["value"] for i in facts["contradictions"]]
    input_years = CMP.documented_years("\n".join(given))
    vs_notes = []
    for s in step["sections"]:
        for f in RC.added_years(notes, s["text"], {}):
            vs_notes.append({"part": s["part"], "heading": s["heading"], "token": f["token"],
                             "source": "step_2" if f["token"] in input_years else "step_6"})

    review = []
    for it in review_items(rec):
        ruling = rulings.get(it["key"])
        if ruling is not None and ruling not in RULINGS:
            raise ValueError(f"compose ruling {it['key']}: {ruling!r} is not one of {RULINGS}")
        review.append({k: v for k, v in it.items() if k != "sentence"} | {"ruling": ruling})
    return {"metric": "compose_clinical", "metric_version": METRIC_VERSION,
            "checks": checks, "counts": step["counts"], "box_problems": box_problems,
            "years_vs_notes": vs_notes, "review": review, "latency": _latency(rec)}


def _q(values: list[float], p: float):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, round(p * (len(v) - 1))))]


def summarise(results: list[dict], skipped: list[dict], n_records: int) -> dict:
    def tot(name):
        return sum(r["checks"].get(name, 0) for r in results)

    def gens(name):
        return sum(1 for r in results if r["checks"].get(name, 0))

    def review(kind):
        rows = [x for r in results for x in r["review"] if x["kind"] == kind]
        return {"flagged": len(rows), "defect": sum(x["ruling"] == "defect" for x in rows),
                "acceptable": sum(x["ruling"] == "acceptable" for x in rows),
                "pending": sum(x["ruling"] is None for x in rows)}

    c = lambda k: sum(r["counts"][k] for r in results)  # noqa: E731
    crit = [r["latency"]["critical_s"] for r in results if r["latency"]["critical_s"] is not None]
    s6 = [r["latency"]["step6_s"] for r in results if r["latency"]["step6_s"] is not None]
    years_notes = [y for r in results for y in r["years_vs_notes"]]
    return {
        "n_records": n_records, "n_scored": len(results), "skipped": skipped,
        "year_added_step6": {"findings": tot("year_added"), "generations": gens("year_added")},
        "years_vs_notes": {"step_6": sum(y["source"] == "step_6" for y in years_notes),
                           "step_2": sum(y["source"] == "step_2" for y in years_notes),
                           "generations": sum(1 for r in results if r["years_vs_notes"])},
        "boxes_correct": sum(1 for r in results if r["box_problems"] == 0),
        "unknown_fact_ids": tot("unknown_fact_id"), "excluded_fact_ids": tot("excluded_fact_id"),
        "contradictions": {"given": c("contradictions_given"), "in_a": c("contradictions_in_a")},
        "field_omitted": tot("field_omitted"), "gp_action_missing_in_b": tot("gp_action_missing_in_b"),
        "facts": {"given": c("facts_given"), "used_in_a": c("facts_used_in_a")},
        "unverified": {"given": c("unverified_given"), "used": c("unverified_used")},
        "review": {k: review(k) for k in ("advice", "resus", "medication")},
        "other_checks": {k: tot(k) for k in CMP.CHECKS
                         if k not in ("year_added", "unknown_fact_id", "excluded_fact_id", "field_omitted",
                                      "gp_action_missing_in_b", "contradiction_missing")},
        "latency": {"step6_p50": _q(s6, 0.5), "step6_p95": _q(s6, 0.95), "step6_max": max(s6, default=None),
                    "critical_p50": _q(crit, 0.5), "critical_p95": _q(crit, 0.95),
                    "critical_max": max(crit, default=None),
                    "critical_over_watch": sum(x > DEADLINE_WATCH_S for x in crit),
                    "watch_s": DEADLINE_WATCH_S, "median_step6": statistics.median(s6) if s6 else None},
    }


def bar(sm: dict) -> list[tuple[str, str, str]]:
    """(bar line, result, verdict) for bar lines 1-7 — the pre-declared bar, frozen."""
    n = sm["n_records"]

    def met(ok, pending=False):
        return "PENDING REVIEW" if pending and ok else ("MET" if ok else "NOT MET")

    rv = sm["review"]
    ra = {"flagged": rv["advice"]["flagged"] + rv["resus"]["flagged"],
          "defect": rv["advice"]["defect"] + rv["resus"]["defect"],
          "pending": rv["advice"]["pending"] + rv["resus"]["pending"]}
    rm = rv["medication"]
    ct = sm["contradictions"]
    return [
        ("1 Reached step 6 (schema accepted; a failure counts as a fail everywhere)",
         f"{sm['n_scored']}/{n}", met(sm["n_scored"] == n)),
        ("2 Years added by step 6 (its own check, vs its input)",
         f"{sm['year_added_step6']['findings']} in {sm['year_added_step6']['generations']} generations",
         met(sm["year_added_step6"]["findings"] == 0 and sm["n_scored"] == n)),
        ("3a Boxes exactly once and exact in A and B; no stray {{...}}",
         f"{sm['boxes_correct']}/{n}", met(sm["boxes_correct"] == n)),
        ("3b Unknown / excluded fact_ids", f"{sm['unknown_fact_ids']} / {sm['excluded_fact_ids']}",
         met(sm["unknown_fact_ids"] == 0 and sm["excluded_fact_ids"] == 0)),
        ("4 Contradictions given to step 6 that appear in PART A", f"{ct['in_a']}/{ct['given']}",
         met(ct["in_a"] == ct["given"] and sm["n_scored"] == n)),
        ("5 Resus / advice text outside its box (CSO-ruled defects)",
         f"flagged {ra['flagged']}, defects {ra['defect']}, pending {ra['pending']}",
         met(ra["defect"] == 0, pending=ra["pending"] > 0)),
        ("6 Field-level omission in A / GP actions missing from B",
         f"{sm['field_omitted']} / {sm['gp_action_missing_in_b']}",
         met(sm["field_omitted"] == 0 and sm["gp_action_missing_in_b"] == 0 and sm["n_scored"] == n)),
        ("7 Medication text outside the box (CSO-ruled defects)",
         f"flagged {rm['flagged']}, defects {rm['defect']}, pending {rm['pending']}",
         met(rm["defect"] == 0, pending=rm["pending"] > 0)),
    ]


def write_reports(out: Path, scored: dict, batch_dir: Path, corpus: dict) -> None:
    cp = scored["compose"]
    sm = cp["summary"]
    (out / "compose_clinical.json").write_text(json.dumps(
        {"batch_id": scored["batch_id"], **cp}, indent=2) + "\n", encoding="utf-8")
    lat = sm["latency"]
    fmt = lambda x: "—" if x is None else f"{x:.1f} s"  # noqa: E731
    lines = [f"# Step 6 compose_clinical — batch `{scored['batch_id']}`", "",
             f"Step-level checks, metric v{cp['metric_version']}. Definition: `evals/score_compose.py` "
             "(pass bar written 27 Oct 2026, before any step-6 code; frozen in code before the first live call).",
             "", "> **Not measured here:** faithfulness of the prose to its facts (D2 beyond years, D6, D7) "
             "and narrative omission — see the CSO blind read. Bar line 8 (no regression) is read from "
             "SAFETY_NET.md, MEDS.md and `haz03_check.py`.", "",
             "| Bar line | Result | Verdict |", "| --- | --- | --- |"]
    lines += [f"| {a} | {b} | **{c}** |" for a, b, c in bar(sm)]
    yn = sm["years_vs_notes"]
    lines += ["", "## Reported, no bar", "", "| Measure | Result |", "| --- | --- |",
              f"| Fact coverage in PART A (k/n) | {sm['facts']['used_in_a']}/{sm['facts']['given']} |",
              f"| Unverified facts given / used (8a adds the warning) | {sm['unverified']['given']} / {sm['unverified']['used']} |",
              f"| Years vs the notes (rubric D2) — added by step 6 / by step 2 | {yn['step_6']} / {yn['step_2']} "
              f"(in {yn['generations']} generations) |"]
    lines += [f"| Check `{k}` | {v} |" for k, v in sm["other_checks"].items()]
    lines += ["", "## Latency (270 s deadline watch)", "", "| Measure | p50 | p95 | max |", "| --- | --- | --- | --- |",
              f"| Step 6 | {fmt(lat['step6_p50'])} | {fmt(lat['step6_p95'])} | {fmt(lat['step6_max'])} |",
              f"| Step 2 + max(5a, 6) | {fmt(lat['critical_p50'])} | {fmt(lat['critical_p95'])} | {fmt(lat['critical_max'])} |",
              "", f"Generations over the {lat['watch_s']} s watch line (270 − step 7's 60 s read timeout − 10): "
              f"**{lat['critical_over_watch']}**. Latency is Bedrock's reported `latencyMs` per call: it "
              "excludes network time and any throttle wait, so it is a floor on wall time, not a ceiling.", ""]
    if sm["skipped"]:
        lines += ["## Not scored (step 6 failed or did not run)", ""]
        lines += [f"- `{s['generation_id']}`: {s['error_code']}" for s in sm["skipped"]] + [""]
    lines += ["## Per scenario", "", "| Scenario | Scored | Box problems | Years (step 6) | Contradictions in A | "
              "Field omitted | Review flags (advice / resus / medication) |", "| --- | --- | --- | --- | --- | --- | --- |"]
    res = cp["results"]
    for sid in dict.fromkeys(r["scenario_id"] for r in res):
        rows = [r for r in res if r["scenario_id"] == sid]
        rk = lambda k: sum(1 for r in rows for x in r["review"] if x["kind"] == k)  # noqa: E731
        lines.append(f"| {sid} | {len(rows)} | {sum(r['box_problems'] for r in rows)} | "
                     f"{sum(r['checks'].get('year_added', 0) for r in rows)} | "
                     f"{sum(r['counts']['contradictions_in_a'] for r in rows)}/"
                     f"{sum(r['counts']['contradictions_given'] for r in rows)} | "
                     f"{sum(r['checks'].get('field_omitted', 0) for r in rows)} | "
                     f"{rk('advice')} / {rk('resus')} / {rk('medication')} |")
    (out / "COMPOSE_CLINICAL.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # The review sheet: one entry per distinct flagged sentence. A FILE, so it may quote output.
    pending: dict[tuple[str, str], dict] = {}
    for r in res:
        rec = json.loads((batch_dir / f"r{r['run']}" / f"{r['scenario_id']}.json").read_text(encoding="utf-8"))
        for it in review_items(rec):
            ruled = next((x["ruling"] for x in r["review"] if x["key"] == it["key"]), None)
            e = pending.setdefault((r["scenario_id"], it["key"]), {**it, "runs": [], "ruling": ruled})
            e["runs"].append(r["run"])
    rv = [f"# Step 6 sentences to rule on — batch `{scored['batch_id']}`", "",
          "Each sentence sits OUTSIDE a box and matched a pattern. Rule on each: **acceptable** (it is not "
          "advice / a resus status / a discharge medicine — e.g. \"presented to A&E\", \"fluid resuscitation\", "
          "an in-hospital drug) or **defect** (it is). Store rulings in `evals/gold/adjudications/<ID>.json` "
          "under `compose_rulings` as `{key: ruling}`; they are reused across runs.", ""]
    for (sid, key), e in sorted(pending.items()):
        tok = f" `{e['token']}`" if e["token"] else ""
        rv.append(f"- **{sid}** `{key}` {e['kind']}{tok} · {e['part']} / {e['heading']} "
                  f"(runs {', '.join(map(str, e['runs']))}) · ruling: {e['ruling'] or 'PENDING'}: {e['sentence']}")
    if not pending:
        rv.append("Nothing to rule on.")
    (out / "review_compose.md").write_text("\n".join(rv) + "\n", encoding="utf-8")
