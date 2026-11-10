"""Step 7 — compose_patient: step-level scoring for a pipeline batch (WS1a step
build piece 3, 1 Nov 2026). Called by score_batch.py for a batch whose runner
includes step 7; never run on its own.

The pass bar — written 1 Nov 2026 in rounds 1-2 (docs/_local/ADR-009-build-
record-step-build-p3.md), before any step-7 code, CSO-approved (lines 4 and 5
amended in round 2, also before any code) — frozen here before the first live
step-7 call. PART C is ASSEMBLED here as 8b will assemble it (pipeline/render.py,
the shipped code), so the gate, HAZ-03 and the header are checked on the
leaflet a clinician would see:

  1  reached step 7           records with a compose_patient output / all
  2  boxes / source_ids       each C box exactly once and exact; no stray {{...}};
                              unknown source_ids 0
  3  signposting              0 signpost sentences outside {{SAFETY_NET}} (step check,
                              decides) AND the gate on assembled A + C PASS on every
                              generation (advisory pass counts on the documented route)
  4  advice-shaped sentences  flagged, CSO-ruled; 0 defects. Standard (round 2): acceptable
                              only if it restates an instruction its cited PART A section
                              states, with the same force and scope
  5  resus / medicines        0 resus-pattern matches in step-7 text (decides); dose /
                              frequency and unsourced drug names flagged, CSO-ruled, 0
                              defects (acceptable: the cited A section gives the same dose
                              for an in-hospital treatment; defect: tells the patient what
                              to take at home)
  6  years / numbers          years added vs step 7's input 0 (decides); numbers / units
                              added flagged, CSO-ruled, 0 defects
  7  reading age              FK <= 8 on step 7's prose (boxes excluded), every
                              generation (reading_age.py); full-C FK reported
  8  translation              the header on every TRANSLATION_SCENARIOS generation and on
                              no other; 0 non-English sentences in C (rubric_checks
                              non_english, deciding finding)
  9  withholding / coverage   0 sources outside what step 7 was given (risk_assessment
                              never given — by construction); 0 "Not documented" text;
                              every given follow_up / gp_actions section cited; title
                              audience = gold age_group's, every generation; sensitive /
                              conflict flags CSO-ruled, 0 defects
  10 blind read               NOT here: make_patient_sheets.py (8 items, before reports)
  11 no regression            HAZ-03 on ASSEMBLED C here; 5b / D4 / step 2 / step 6 in
                              SAFETY_NET.md, MEDS.md, STEP2_ADVICE.md, COMPOSE_CLINICAL.md
  12 latency                  0 generations where step 7 would start after 200 s
                              (step 2 + max(5a, 6)); full critical path vs 270 s reported
  13 sourceless prose         0 step-7 prose sections citing no section step 7 was given
                              (code decides; computed here from the sections, not from
                              the step's own check, so older batches are measured too)

Metric v2 (2 Nov 2026, piece 3 follow-up, CSO F1-F5; committed before any p0.2 live
call). Lines 1-12 are unchanged. Added: line 13 (F2); a report-only `promise` review
kind (F5: service / availability / visit wording — ruled for the record, no bar); the
`no_sections_given` refusals counted (F1 — a refusal is a step-7 failure, so line 1
counts it, as written 1 Nov); and the written scope of line 9's sensitive standard
(F3, before the batch): the reason for admission stated as step 7's input gives it —
suicidal thoughts included — is NOT withheld-category; withheld-category is risk levels
and scores, safeguarding, police, ceilings of care and resuscitation.

Metric v3 (3 Nov 2026, piece 3 follow-up 2, CSO G2 A; committed before any p0.3 live
call). Lines 1-13 are unchanged. Added: line 14 (G2, stricter, written before the batch):

  14 meaning flags            sentences / sections flagged by pipeline/meaning.py (the same
                              functions the step's checks use, computed here from the
                              saved sections, so older batches are measured too): an added
                              cause or purpose, an added certainty / completion word, a
                              qualifier of the cited section with no equivalent, a test or
                              sample word the cited section does not justify, a primary /
                              secondary diagnosis not reached. CSO-ruled; 0 defects.
                              Standard (written 3 Nov 2026, before the p0.3 batch):
                              acceptable only if the C text says the same thing as its
                              cited PART A section with the same force and scope — a
                              cause or purpose that section states; a certainty word it
                              supports; the qualifier kept in other words; the test named
                              as the section names it, or by an everyday description that
                              is not a different test; the diagnosis stated in other words,
                              or a by-design omission (sheets v2 list) — defect otherwise.
                              The plain-word tables in meaning.py are the pre-ruled part.
                              Correction 4 Nov 2026 (CSO K-A, after the p0.4 batch, disclosed):
                              a diagnosis flag's key includes the leaflet's prose, so each
                              leaflet is ruled on its own; no verdict logic changed.

Metric v4 (9 Nov 2026, item 3 R1 step 1 — decision S7 of 4 Nov as amended by Shina
(author/CSO) on 9 Nov, 2(a); D8 of the step 7 decision round 2; design adopted 9 Nov
~21:15; committed before the R1 development run, one instrument for development and
acceptance). Lines 1-14, their thresholds and their ruling standards are unchanged;
the five S7 fixes are in the shared check functions (pipeline/meaning.py and
compose_patient.py's _SENSITIVE / _PROMISE), so step 7's own checks, 8b's meaning
warnings and this scorer measure the same thing:
  - line 14: "damage" a certainty word; a "label — content" diagnosis line reached
    only when its head phrase AND every segment after a dash are reached (stricter);
    Hb justifies "blood test" (the one change that can remove flags — allowed only
    where each removed flag was ruled acceptable at p0.4, shown by the p0.4 re-score);
  - line 9: any risk word is a sensitive flag (beside v3's four forms);
  - promise (report only): "will be told".
Every retained flag keeps its key, so its ruling attaches as before; added flags are
PENDING until ruled. Any later change is v5, CSO-ruled, before acceptance.

A generation whose C cannot be assembled (5a or 5b missing) fails every line that
needs the assembled leaflet (3, 8, 9's title, 11) — never a pass.

Rulings live in evals/gold/adjudications/<ID>.json under `patient_rulings`.
An unruled flag is PENDING, never a pass. The JSON results hold labels, headings,
section_ids, years, numbers, unit and drug tokens; the review FILE may quote
output sentences. The console shows counts only.
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
import haz03_check as H3  # noqa: E402
import reading_age as RA  # noqa: E402
import rubric_checks as RC  # noqa: E402
from pipeline import safety_net_gate as gate  # noqa: E402
import score_compose as SC  # noqa: E402
from pipeline import compose_patient as CP7  # noqa: E402
from pipeline import meaning as MN  # noqa: E402
from pipeline import render as RN  # noqa: E402

METRIC_VERSION = 4
# Pre-declared from the scenario files (adversarial-scenarios.md C7: Polish-speaking,
# interpreter used): the one scenario of the 18 whose notes record a language need.
TRANSLATION_SCENARIOS = ("C7",)
DEADLINE_WATCH_S = 200      # 270 s − step 7's 60 s read timeout − 10 s (ADR-009 Q3)
DEADLINE_S = 270
RULINGS = ("acceptable", "defect")
# Review kind -> bar line.
MEANING_KINDS = MN.KINDS                      # G2 (3 Nov): bar line 14
REVIEW_KINDS = ("advice", "medication", "drug", "number", "unit", "conflict", "sensitive", "promise") + MEANING_KINDS
BAR_OF = {"advice": 4, "medication": 5, "drug": 5, "number": 6, "unit": 6, "conflict": 9, "sensitive": 9,
          "promise": None} | {k: 14 for k in MEANING_KINDS}    # F5: promise report only
REFUSED = "no_sections_given"                 # F1
STRUCTURE = ("box_not_exact", "stray_placeholder")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def _key(kind: str, heading: str, text: str) -> str:
    norm = " ".join(text.split()).casefold()
    return hashlib.sha256(f"patient|{kind}|{heading}|{norm}".encode("utf-8")).hexdigest()[:16]


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s.strip()]


def given_input(rec: dict) -> dict:
    """Step 7's input, rebuilt from step 6's output by the step's own function."""
    return CP7.patient_input(rec["steps"]["compose_clinical"])


def prose(step7: dict) -> str:
    return "\n".join(s["text"] for s in step7["sections"] if s["heading"] not in CP7.BOXES)


def review_items(rec: dict) -> list[dict]:
    """Every sentence code flags for a CSO ruling, per kind, WITH the sentence (for
    the review file); score_record strips it. Same patterns as the step's checks,
    applied per sentence, plus drug names (the step does not see 5a)."""
    step7 = rec["steps"]["compose_patient"]
    given = {s["section_id"]: s for s in given_input(rec)["sections"]}
    all_text = "\n".join(s["text"] for s in given.values())
    drugs = SC.drug_tokens(rec)
    items = []
    for s in step7["sections"]:
        if s["heading"] in CP7.BOXES:
            continue
        src = "\n".join(given[i]["text"] for i in s["source_ids"] if i in given) or all_text
        src_n, src_u = CP7.numbers(src), CP7.units(src)
        input_years = CP7.documented_years(all_text)
        for sent in _sentences(s["text"]):
            found = []
            if CP7._ADVICE.search(sent):
                found.append(("advice", None))
            if CP7._DOSE.search(sent):
                found.append(("medication", None))
            found += [("drug", d) for d in drugs
                      if re.search(rf"\b{re.escape(d)}\b", sent, re.IGNORECASE)
                      and not re.search(rf"\b{re.escape(d)}\b", src, re.IGNORECASE)]
            found += [("number", n) for n in sorted(CP7.numbers(sent) - src_n)
                      if not CP7.added_years(input_years, n)]
            found += [("unit", u) for u in sorted(CP7.units(sent) - src_u)]
            if CP7._CONFLICT.search(sent):
                found.append(("conflict", None))
            if CP7._SENSITIVE.search(sent):
                found.append(("sensitive", None))
            if CP7._PROMISE.search(sent):
                found.append(("promise", None))
            for kind, token in found:
                items.append({"kind": kind, "heading": s["heading"], "token": token,
                              "key": _key(kind + (f":{token}" if token else ""), s["heading"], sent),
                              "sentence": sent})
    # v3 (G2): meaning flags — the step's own functions, measured against the cited sections.
    for s in step7["sections"]:
        if s["heading"] in CP7.BOXES:
            continue
        cited = [given[i] for i in s["source_ids"] if i in given] or list(given.values())
        for kind, token, shown in MN.section_flags(s["text"], cited):
            items.append({"kind": kind, "heading": s["heading"], "token": token,
                          "key": _key(f"{kind}:{token}", s["heading"], shown), "sentence": shown})
    # Key = the diagnosis line AND this leaflet's prose (correction of 4 Nov 2026, CSO K-A, after the
    # p0.4 batch; disclosed): keyed on the input line alone, one ruling covered different leaflets
    # (S3's line, all 5 runs). Every other kind already keys on step 7's own sentence.
    text = prose(step7)
    for _sid, _n, line in MN.diagnoses_unreached(list(given.values()), text):
        items.append({"kind": "diagnosis", "heading": "why_in_hospital", "token": None,
                      "key": _key("diagnosis", "why_in_hospital", f"{line}\n{text}"),
                      "sentence": f"not reached: {line}"})
    return items


def sourceless(rec: dict) -> list[str]:
    """Headings of step-7 prose sections that cite no section step 7 was given (line 13)."""
    given = {s["section_id"] for s in given_input(rec)["sections"]}
    return [s["heading"] for s in rec["steps"]["compose_patient"]["sections"]
            if s["heading"] not in CP7.BOXES and not set(s["source_ids"]) & given]


def part_a_for_gate(rec: dict) -> str:
    """PART A as text for the gate ONLY (8a renders A for real — item 4): step 6's
    PART A sections under upper-case labels, the SAFETY NET box filled with 5b's
    part_ab so the gate's advice_block finds it; other boxes left as tokens."""
    out = []
    for s in rec["steps"]["compose_clinical"]["sections"]:
        if s["part"] != "part_a":
            continue
        label = "SAFETY NET" if s["heading"] == "safety_net" else s["heading"].replace("_", " ").upper()
        text = rec["steps"]["select_safety_net"]["part_ab"] if s["heading"] == "safety_net" else s["text"]
        out += [f"{label}:", text, ""]
    return "\n".join(out)


def assemble(rec: dict) -> str | None:
    steps = rec["steps"]
    if "reconcile_medications" not in steps or "select_safety_net" not in steps:
        return None
    return RN.assemble_part_c(steps["compose_patient"], steps["reconcile_medications"],
                              steps["select_safety_net"], steps["retrieve_evidence"].get("language") or [])


def _latency(rec: dict) -> dict:
    by = {c["name"]: c.get("latency_s") for c in rec.get("calls") or []}
    l2, l5, l6, l7 = (by.get(k) for k in ("extract_facts", "reconcile_medications",
                                           "compose_clinical", "compose_patient"))
    start7 = None if l2 is None or l6 is None else round(l2 + max(l6, l5 or 0), 3)
    full = None if start7 is None or l7 is None else round(start7 + l7, 3)
    return {"step7_s": l7, "step7_start_s": start7, "critical_s": full}


def score_record(rec: dict, notes: str, gold: dict, rulings: dict) -> dict:
    steps = rec["steps"]
    step7 = steps["compose_patient"]
    checks: dict[str, int] = {}
    for c in step7["checks"]:
        checks[c["check"]] = checks.get(c["check"], 0) + 1
    box_problems = sum(1 for c in step7["checks"]
                       if c["check"] in STRUCTURE
                       or (c["check"] in ("required_heading_missing", "heading_repeated")
                           and c["heading"] in CP7.BOXES))

    review = []
    for it in review_items(rec):
        ruling = rulings.get(it["key"])
        if ruling is not None and ruling not in RULINGS:
            raise ValueError(f"patient ruling {it['key']}: {ruling!r} is not one of {RULINGS}")
        review.append({k: v for k, v in it.items() if k != "sentence"} | {"ruling": ruling})

    text = prose(step7)
    fk = RA.fk_grade(text)
    sid = rec["scenario_id"]
    expected_audience = CP7.audience_for(gold["age_group"]["value"])
    c = assemble(rec)
    out = {"metric": "compose_patient", "metric_version": METRIC_VERSION,
           "checks": checks, "counts": step7["counts"], "box_problems": box_problems,
           "fk_prose": fk, "d5_prose": RA.d5_verdict(fk),
           "years_vs_notes": [f["token"] for f in RC.added_years(notes, text, {})],
           "review": review, "latency": _latency(rec), "sourceless": sourceless(rec),
           "audience": step7["audience"], "expected_audience": expected_audience,
           "risk_sections_in_a": sum(1 for s in steps["compose_clinical"]["sections"]
                                     if s["part"] == "part_a" and s["heading"] == "risk_assessment"),
           "unverified_sources": sum(
               1 for s in step7["sections"]
               for a in steps["compose_clinical"]["sections"]
               if a["section_id"] in s["source_ids"] and a["unverified_fact_ids"]),
           "assembled": c is not None}
    if c is None:
        return out | {"gate": None, "haz03_c": None, "header": None, "non_english": None, "fk_full": None,
                      "title_ok": False}
    g = gate.check(notes, part_a_for_gate(rec), c)
    audience = H3.expected_audience(gold["age_group"]["value"])
    expected_line, other_line = H3.LINES[audience], H3.LINES["adult" if audience == "paediatric" else "paediatric"]
    combined = "PART C\n" + c
    ne = RC.non_english(notes, combined, RC.split_parts(combined))
    has_header = any(h in c for h in RN.TRANSLATION_HEADER.values())
    return out | {
        "gate": {"status": g.status, "ok": g.ok},
        "haz03_c": "pass" if (H3._contains(expected_line, c) and not H3._contains(other_line, c)) else "fail",
        "header": {"present": has_header, "expected": sid in TRANSLATION_SCENARIOS},
        "non_english": {"decided": sum(f["reason"] == "non_english_sentence" for f in ne),
                        "possible": sum(f["reason"] == "possible_non_english" for f in ne)},
        "fk_full": RA.fk_grade(c),
        "title_ok": c.startswith(RN.TITLES[expected_audience]) and step7["audience"] == expected_audience,
    }


def _q(values: list[float], p: float):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, max(0, round(p * (len(v) - 1))))]


def summarise(results: list[dict], skipped: list[dict], n_records: int) -> dict:
    def tot(name):
        return sum(r["checks"].get(name, 0) for r in results)

    def review(kind):
        rows = [x for r in results for x in r["review"] if x["kind"] == kind]
        return {"flagged": len(rows), "defect": sum(x["ruling"] == "defect" for x in rows),
                "acceptable": sum(x["ruling"] == "acceptable" for x in rows),
                "pending": sum(x["ruling"] is None for x in rows)}

    asm = [r for r in results if r["assembled"]]
    fks = [r["fk_prose"] for r in results if r["fk_prose"] is not None]
    fkf = [r["fk_full"] for r in asm if r["fk_full"] is not None]
    starts = [r["latency"]["step7_start_s"] for r in results if r["latency"]["step7_start_s"] is not None]
    crit = [r["latency"]["critical_s"] for r in results if r["latency"]["critical_s"] is not None]
    s7 = [r["latency"]["step7_s"] for r in results if r["latency"]["step7_s"] is not None]
    hdr = [r["header"] for r in asm]
    return {
        "n_records": n_records, "n_scored": len(results), "n_assembled": len(asm), "skipped": skipped,
        "boxes_correct": sum(1 for r in results if r["box_problems"] == 0),
        "unknown_source_ids": tot("unknown_source_id"),
        "signpost_outside_box": tot("signpost_outside_box"),
        "gate": {"pass": sum(1 for r in asm if r["gate"]["ok"]),
                 "statuses": {s: sum(1 for r in asm if r["gate"]["status"] == s)
                              for s in sorted({r["gate"]["status"] for r in asm})}},
        "resus_text": tot("resus_text"),
        "year_added": tot("year_added"),
        "years_vs_notes": sum(len(r["years_vs_notes"]) for r in results),
        "fk": {"prose_pass": sum(1 for x in fks if x <= RA.D5_PASS_MAX), "prose_n": len(fks),
               "prose_p50": _q(fks, 0.5), "prose_max": max(fks, default=None),
               "full_p50": _q(fkf, 0.5), "full_max": max(fkf, default=None)},
        "header": {"expected": sum(1 for h in hdr if h["expected"]),
                   "expected_present": sum(1 for h in hdr if h["expected"] and h["present"]),
                   "unexpected_present": sum(1 for h in hdr if not h["expected"] and h["present"])},
        "header_expected_records": sum(1 for r in results if r.get("scenario_id") in TRANSLATION_SCENARIOS),
        "non_english": {"decided": sum(r["non_english"]["decided"] for r in asm),
                        "possible": sum(r["non_english"]["possible"] for r in asm)},
        "not_documented_text": tot("not_documented_text"),
        "follow_up_missing": tot("follow_up_missing"),
        "title_ok": sum(1 for r in results if r["title_ok"]),
        "haz03_c": sum(1 for r in asm if r["haz03_c"] == "pass"),
        "risk_sections_in_a": sum(r["risk_sections_in_a"] for r in results),
        "unverified_sources": sum(r["unverified_sources"] for r in results),
        "review": {k: review(k) for k in REVIEW_KINDS},
        "prose_without_source": sum(len(r["sourceless"]) for r in results),
        "refused_no_sections": sum(1 for s in skipped if s.get("error_code") == REFUSED),
        "other_checks": {k: tot(k) for k in CP7.CHECKS
                         if k not in ("unknown_source_id", "signpost_outside_box", "resus_text", "year_added",
                                      "not_documented_text", "follow_up_missing",
                                      "prose_without_source", "promise_shaped")
                         and k not in MN.CHECK_OF.values()},   # v2 / v3: scored here
        "words_prose_p50": _q([r["counts"]["words_prose"] for r in results], 0.5),
        "latency": {"step7_p50": _q(s7, 0.5), "step7_max": max(s7, default=None),
                    "start7_max": max(starts, default=None),
                    "start7_over_watch": sum(x > DEADLINE_WATCH_S for x in starts),
                    "critical_p50": _q(crit, 0.5), "critical_max": max(crit, default=None),
                    "critical_over_deadline": sum(x > DEADLINE_S for x in crit),
                    "watch_s": DEADLINE_WATCH_S, "deadline_s": DEADLINE_S,
                    "median_step7": statistics.median(s7) if s7 else None},
    }


def bar(sm: dict) -> list[tuple[str, str, str]]:
    """(bar line, result, verdict) for bar lines 1-9, 11, 12 (frozen 1 Nov), 13 (v2, 2 Nov), 14 (v3, 3 Nov).
    v4 (9 Nov) changes what is flagged, not how a line is decided: a ruled defect gives NOT MET
    whatever else is pending; PENDING REVIEW only when nothing is a defect."""
    n = sm["n_records"]
    full = sm["n_scored"] == n
    asm_full = sm["n_assembled"] == n

    def met(ok, pending=False):
        return "PENDING REVIEW" if pending and ok else ("MET" if ok else "NOT MET")

    def rv(*kinds):
        rows = [sm["review"][k] for k in kinds]
        return {x: sum(r[x] for r in rows) for x in ("flagged", "defect", "pending")}

    def rtxt(r):
        return f"flagged {r['flagged']}, defects {r['defect']}, pending {r['pending']}"

    r4, r5, r6, r9 = rv("advice"), rv("medication", "drug"), rv("number", "unit"), rv("conflict", "sensitive")
    r14 = rv(*MEANING_KINDS)
    h = sm["header"]
    fk = sm["fk"]
    lat = sm["latency"]
    return [
        ("1 Reached step 7 (schema accepted; a failure counts as a fail everywhere)",
         f"{sm['n_scored']}/{n}", met(full)),
        ("2 C boxes exactly once and exact; no stray {{...}}; unknown source_ids",
         f"{sm['boxes_correct']}/{n}; unknown {sm['unknown_source_ids']}",
         met(sm["boxes_correct"] == n and sm["unknown_source_ids"] == 0)),
        ("3 Signpost sentences outside {{SAFETY_NET}}; gate on assembled A + C",
         f"{sm['signpost_outside_box']}; gate pass {sm['gate']['pass']}/{n}",
         met(full and asm_full and sm["signpost_outside_box"] == 0 and sm["gate"]["pass"] == n)),
        ("4 Advice-shaped sentences outside the box (CSO-ruled defects)", rtxt(r4),
         met(full and r4["defect"] == 0, pending=r4["pending"] > 0)),
        ("5 Resus text in step 7 (code) / dose, frequency, unsourced drug (CSO-ruled defects)",
         f"resus {sm['resus_text']}; {rtxt(r5)}",
         met(full and sm["resus_text"] == 0 and r5["defect"] == 0, pending=r5["pending"] > 0)),
        ("6 Years added vs step 7's input (code) / numbers, units added (CSO-ruled defects)",
         f"years {sm['year_added']}; {rtxt(r6)}",
         met(full and sm["year_added"] == 0 and r6["defect"] == 0, pending=r6["pending"] > 0)),
        ("7 FK grade <= 8 on step 7's prose (boxes excluded)",
         f"{fk['prose_pass']}/{n} (p50 {fk['prose_p50']}, max {fk['prose_max']})",
         met(full and fk["prose_pass"] == n)),
        ("8 Translation header where expected / elsewhere; non-English sentences in C",
         f"{h['expected_present']}/{sm['header_expected_records']}; elsewhere {h['unexpected_present']}; "
         f"non-English {sm['non_english']['decided']}",
         met(asm_full and h["expected_present"] == sm["header_expected_records"]
             and h["unexpected_present"] == 0 and sm["non_english"]["decided"] == 0)),
        ("9 Withholding / coverage: unknown sources, 'Not documented', follow-up missing, title audience; "
         "sensitive / conflict (CSO-ruled defects)",
         f"{sm['unknown_source_ids']} / {sm['not_documented_text']} / {sm['follow_up_missing']} / "
         f"title {sm['title_ok']}/{n}; {rtxt(r9)}",
         met(full and asm_full and sm["unknown_source_ids"] == 0 and sm["not_documented_text"] == 0
             and sm["follow_up_missing"] == 0 and sm["title_ok"] == n and r9["defect"] == 0,
             pending=r9["pending"] > 0)),
        ("11 HAZ-03 on assembled C (expected pinned line verbatim, other line absent)",
         f"{sm['haz03_c']}/{n}", met(asm_full and sm["haz03_c"] == n)),
        ("12 Generations where step 7 would start after the 200 s watch line",
         f"{lat['start7_over_watch']} (start max {lat['start7_max']} s; full path max {lat['critical_max']} s "
         f"vs {lat['deadline_s']} s)", met(full and lat["start7_over_watch"] == 0)),
        ("13 Step-7 prose sections citing no section step 7 was given (code)",
         f"{sm['prose_without_source']}", met(full and sm["prose_without_source"] == 0)),
        ("14 Meaning flags: cause, certainty, qualifier, test term, diagnosis (CSO-ruled defects)", rtxt(r14),
         met(full and r14["defect"] == 0, pending=r14["pending"] > 0)),
    ]


def write_reports(out: Path, scored: dict, batch_dir: Path) -> None:
    pv = scored["patient"]
    sm = pv["summary"]
    (out / "patient_version.json").write_text(json.dumps(
        {"batch_id": scored["batch_id"], **pv}, indent=2) + "\n", encoding="utf-8")
    fmt = lambda x: "—" if x is None else f"{x:.1f} s"  # noqa: E731
    lat = sm["latency"]
    lines = [f"# Step 7 compose_patient — batch `{scored['batch_id']}`", "",
             f"Step-level checks, metric v{pv['metric_version']}. Definition: `evals/score_patient.py` "
             "(pass bar written 1 Nov 2026 before any step-7 code; frozen in code before the first live call). "
             "PART C is assembled by `pipeline/render.py`, as 8b will.", "",
             "> **Not measured here:** whether C says the same things as A (HAZ-17) beyond line 14's fixed word "
             "lists, withholding judgement, tone — see the CSO blind read (bar line 10, `make_patient_sheets.py`). Bar line 11's other "
             "measures are in SAFETY_NET.md, MEDS.md, STEP2_ADVICE.md and COMPOSE_CLINICAL.md.", "",
             "| Bar line | Result | Verdict |", "| --- | --- | --- |"]
    lines += [f"| {a} | {b} | **{c}** |" for a, b, c in bar(sm)]
    lines += ["", "## Reported, no bar", "", "| Measure | Result |", "| --- | --- |",
              f"| Assembled C (5a and 5b present) | {sm['n_assembled']}/{sm['n_records']} |",
              f"| Gate statuses | {', '.join(f'{k} {v}' for k, v in sm['gate']['statuses'].items()) or '—'} |",
              f"| FK on the whole assembled C (incl. boxes and verbatim advice) p50 / max | "
              f"{sm['fk']['full_p50']} / {sm['fk']['full_max']} |",
              f"| Possible non-English sentences (one weak signal) | {sm['non_english']['possible']} |",
              f"| Years vs the notes in step-7 prose (rubric D2) | {sm['years_vs_notes']} |",
              f"| PART A risk_assessment sections not given to step 7 (by construction) | {sm['risk_sections_in_a']} |",
              f"| C sections drawing on an A section with unverified facts (8b's warning) | {sm['unverified_sources']} |",
              f"| Words of step-7 prose, p50 | {sm['words_prose_p50']} |",
              f"| Step 7 refused: `{REFUSED}` (F1; counted as not reached in line 1) | {sm['refused_no_sections']} |",
              f"| Promise wording (F5; report only, CSO-ruled for the record) | "
              f"flagged {sm['review']['promise']['flagged']}, defects {sm['review']['promise']['defect']}, "
              f"pending {sm['review']['promise']['pending']} |"]
    lines += [f"| Check `{k}` | {v} |" for k, v in sm["other_checks"].items()]
    lines += ["", "## Latency (270 s deadline watch)", "", "| Measure | p50 | max |", "| --- | --- | --- |",
              f"| Step 7 | {fmt(lat['step7_p50'])} | {fmt(lat['step7_max'])} |",
              f"| Step 2 + max(5a, 6) + 7 | {fmt(lat['critical_p50'])} | {fmt(lat['critical_max'])} |",
              "", f"Step 7 start after {lat['watch_s']} s: **{lat['start7_over_watch']}**; full path over "
              f"{lat['deadline_s']} s: **{lat['critical_over_deadline']}**. Bedrock `latencyMs` per call — a floor "
              "on wall time.", ""]
    if sm["skipped"]:
        lines += ["## Not scored (step 7 failed or did not run)", ""]
        lines += [f"- `{s['generation_id']}`: {s['error_code']}" for s in sm["skipped"]] + [""]
    res = pv["results"]
    lines += ["## Per scenario", "", "| Scenario | Scored | Box problems | FK prose (max) | Gate pass | Header | "
              "Review flags (advice / meds+drug / number+unit / conflict+sensitive) | Promise (no bar) |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for sid in dict.fromkeys(r["scenario_id"] for r in res):
        rows = [r for r in res if r["scenario_id"] == sid]
        rk = lambda *k: sum(1 for r in rows for x in r["review"] if x["kind"] in k)  # noqa: E731
        fkm = max((r["fk_prose"] for r in rows if r["fk_prose"] is not None), default=None)
        lines.append(f"| {sid} | {len(rows)} | {sum(r['box_problems'] for r in rows)} | {fkm} | "
                     f"{sum(1 for r in rows if r['gate'] and r['gate']['ok'])} | "
                     f"{sum(1 for r in rows if r['header'] and r['header']['present'])} | "
                     f"{rk('advice')} / {rk('medication', 'drug')} / {rk('number', 'unit')} / "
                     f"{rk('conflict', 'sensitive')} | {rk('promise')} |")
    lines += ["", f"## Per scenario — meaning flags (bar line 14, v{METRIC_VERSION})", "",
              "| Scenario | " + " | ".join(MEANING_KINDS) + " |", "| --- |" + " --- |" * len(MEANING_KINDS)]
    for sid in dict.fromkeys(r["scenario_id"] for r in res):
        rows = [r for r in res if r["scenario_id"] == sid]
        lines.append(f"| {sid} | " + " | ".join(
            str(sum(1 for r in rows for x in r["review"] if x["kind"] == k)) for k in MEANING_KINDS) + " |")
    (out / "PATIENT_VERSION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    pending: dict[tuple[str, str], dict] = {}
    for r in res:
        rec = json.loads((batch_dir / f"r{r['run']}" / f"{r['scenario_id']}.json").read_text(encoding="utf-8"))
        for it in review_items(rec):
            ruled = next((x["ruling"] for x in r["review"] if x["key"] == it["key"]), None)
            e = pending.setdefault((r["scenario_id"], it["key"]), {**it, "runs": [], "ruling": ruled})
            e["runs"].append(r["run"])
    rv = [f"# Step 7 sentences to rule on — batch `{scored['batch_id']}`", "",
          "Each sentence is step 7's prose (outside both boxes) and matched a pattern. Standards, written "
          "before any code (1 Nov 2026): **advice** — acceptable only if it restates an instruction its cited "
          "PART A section states, same force and scope; **medication / drug** — acceptable if the cited A "
          "section gives the same drug or dose for an in-hospital treatment, defect if it tells the patient "
          "what to take at home; **number / unit** — acceptable if the value is in the cited A section in "
          "another form; **conflict / sensitive** — acceptable only if it discloses no disagreement in the "
          "notes and no withheld-category detail (scope written 2 Nov 2026, before the p0.2 batch: the reason "
          "for admission as step 7's input gives it, suicidal thoughts included, is not withheld-category; "
          "withheld-category is risk levels and scores, safeguarding, police, ceilings of care and "
          "resuscitation); **promise** (report only, no bar) — acceptable only if its cited PART A section "
          "states the service, visit or availability; **cause / certainty / qualifier / test_term / diagnosis** "
          "(bar 14, written 3 Nov 2026 before the p0.3 batch) — acceptable only if the C text says the same "
          "thing as its cited PART A section with the same force and scope (a cause or purpose that section "
          "states; a certainty word it supports; the qualifier kept in other words; the test named as the "
          "section names it, or by an everyday description that is not a different test; the diagnosis stated "
          "in other words, or a by-design omission per sheets v2), defect otherwise (HAZ-17); the plain-word "
          "tables in `pipeline/meaning.py` are pre-ruled. Store rulings in `evals/gold/adjudications/<ID>.json` under "
          "`patient_rulings` as `{key: ruling}`.", ""]
    for (sid, key), e in sorted(pending.items()):
        tok = f" `{e['token']}`" if e["token"] else ""
        bar_txt = f"bar {BAR_OF[e['kind']]}" if BAR_OF[e["kind"]] else "no bar"
        rv.append(f"- **{sid}** `{key}` {e['kind']}{tok} ({bar_txt}) · {e['heading']} "
                  f"(runs {', '.join(map(str, e['runs']))}) · ruling: {e['ruling'] or 'PENDING'}: {e['sentence']}")
    if not pending:
        rv.append("Nothing to rule on.")
    (out / "review_patient.md").write_text("\n".join(rv) + "\n", encoding="utf-8")
