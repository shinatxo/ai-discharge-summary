"""evals/calibrate_verifier.py — offline calibration of step 7v (ADR-010 V5) under
COUNTING RULE v2 (CSO audit, 4 Nov 2026). Fake Bedrock; no AWS. Tests that read the
saved step-7 batches skip when they are not mounted.

The audit found four ways rule v1 could report a pass without the required defects
being caught; each has a regression test below (marked AUDIT n)."""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "generate"))
sys.path.insert(0, str(ROOT / "evals"))

import calibrate_verifier as C  # noqa: E402
import run_cold_eval as rce  # noqa: E402
from pipeline import verify_patient as V  # noqa: E402

QUIET = dt.datetime(2026, 11, 4, 15, 0, tzinfo=rce.LONDON)
DEV = "w6-pipeline-e09-m01-c02-p04-x5"
HELD = ("w6-pipeline-e09-m01-c02-p02-x5", "w6-pipeline-e09-m01-c02-p01-x5")
HAVE_RUNS = all((C.RUNS_DIR / b).is_dir() for b in (DEV, *HELD))
needs_runs = pytest.mark.skipif(not HAVE_RUNS, reason="saved step-7 batches not mounted")


def _args(*argv):
    return C.parse_args(list(argv))


def _rec(gid):
    bid, run, sid = gid.split("/")
    return json.loads((C.RUNS_DIR / bid / run / f"{sid}.json").read_text())


def _ctx(rec):
    given = C.CP7.patient_input(rec["steps"]["compose_clinical"])["sections"]
    return V.verify_input({"audience": "adult", "sections": given}, rec["steps"]["compose_patient"]), given


def _supported_all(vin):
    return [{"sentence_id": t["sentence_id"], "c_section_id": s["section_id"], "heading": s["heading"],
             "text": t["text"], "verdict": "supported", "reason": "none",
             "a_section_id": s["source_ids"][0] if s["source_ids"] else "", "span": "", "evidence_valid": True}
            for s in vin["c_sections"] for t in s["sentences"]]


def _unit_for(lab, vin, given):
    """The verifier unit that the counting rule says catches `lab` — a perfect verifier."""
    omission = lab.get("unit") == "unreached" or lab["kind"] == "diagnosis"
    if omission:
        words = lab.get("a_text") or lab["sentence"].removeprefix("not reached: ")
        return "unreached", {"c_section_id": next(s["section_id"] for s in vin["c_sections"] if s["heading"] == lab["heading"]),
                             "heading": lab["heading"], "a_section_id": C._a_section_of(words, given),
                             "reason": "never_drop_item", "span": words, "evidence_valid": True}
    secs = [s for s in vin["c_sections"] if s["heading"] == lab["heading"]]
    if lab["kind"] == "qualifier" and lab["source"] == "ruling":
        a_sentence = lab["sentence"].split(" → C: ", 1)[0].removeprefix("input: ")
        s, t = max(((s, t) for s in secs for t in s["sentences"]),
                   key=lambda st: len(C._content(a_sentence) & C._content(st[1]["text"])))
        if not C._content(a_sentence) & C._content(t["text"]):
            # the leaflet never re-expresses the A sentence: possible only for an ACCEPTABLE
            # row (a test value left out); no unit can — or should — match it
            assert lab["ruling"] == "acceptable", "a defect qualifier row with no re-expressing sentence"
            return None, None
        reason, span, verdict = "qualifier", t["text"], "qualifier_dropped"
    else:
        target = C._norm(lab["sentence"])
        s, t = next((s, t) for s in secs for t in s["sentences"]
                    if (target in C._norm(t["text"]) if lab["source"] == "map" else C._norm(t["text"]) == target))
        fam = C.FAMILY.get(lab["kind"])
        reason = sorted(fam)[0] if fam else "new_fact"
        span = lab["sentence"] if lab["source"] == "map" else (str(lab["token"]) if lab.get("token") else t["text"])
        if span not in t["text"]:
            span = t["text"]
        verdict = "added"
    return "sentence", {"sentence_id": t["sentence_id"], "c_section_id": s["section_id"], "heading": s["heading"],
                        "text": t["text"], "verdict": verdict, "reason": reason,
                        "a_section_id": s["source_ids"][0], "span": span, "evidence_valid": True}


def _oracle(rec, which=("defect",)):
    vin, given = _ctx(rec)
    verdicts, unreached = _supported_all(vin), []
    labs = [l for l in C.labels_for(rec) + C.map_labels(rec) if l["ruling"] in which]
    for lab in labs:
        kind, u = _unit_for(lab, vin, given)
        if kind is None:
            continue
        if kind == "unreached":
            unreached.append(u)
        else:
            verdicts = [v for v in verdicts if not (v["sentence_id"] == u["sentence_id"] and v["verdict"] == "supported")]
            verdicts.append(u)
    return {"verdicts": verdicts, "unreached": unreached, "flagged": 0, "checks": [], "counts": {}, "call": {}}


def _write(tmp, bid, make, repeats=1, skip=()):
    for k in range(1, repeats + 1):
        for rec in C.records(C.RUNS_DIR / bid):
            if rec["generation_id"] in skip:
                continue
            d = tmp / bid / f"rep{k}" / f"r{rec['run']}"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{rec['scenario_id']}.json").write_text(json.dumps(
                {"generation_id": rec["generation_id"], "verify_patient": make(rec), "error": None}))
    (tmp / bid / "manifest.json").write_text(json.dumps({"prompt_sha256": V.system_prompt()[1]}))


# --- the bar and the run ----------------------------------------------------------

def test_bar_and_protocol_are_the_ones_written_before_calibration():
    assert C.BAR == {"missed_max": 0, "false_flag_max": 0.25, "self_agreement_min": 0.90}
    assert C.PROTOCOL_REPEATS == 3


@needs_runs
def test_a_held_out_run_needs_the_frozen_prompt_sha(tmp_path):
    batch = C.RUNS_DIR / DEV
    assert C.cmd_run(_args("run", str(batch), "--held-out", "--yes"), client_factory=C.DryVerifier,
                     now=lambda: QUIET, cal_dir=tmp_path) == 2
    assert C.cmd_run(_args("run", str(batch), "--held-out", "--frozen", "000000000000", "--yes"),
                     client_factory=C.DryVerifier, now=lambda: QUIET, cal_dir=tmp_path) == 2
    assert not any(tmp_path.iterdir())


@needs_runs
def test_run_writes_one_file_per_leaflet_and_repeat_and_never_overwrites(tmp_path):
    batch = C.RUNS_DIR / DEV
    a = _args("run", str(batch), "--repeats", "2", "--yes")
    assert C.cmd_run(a, client_factory=C.DryVerifier, now=lambda: QUIET, cal_dir=tmp_path, sleep=lambda s: None) == 0
    out = tmp_path / C.prompt_sha12() / DEV
    assert len(list(out.glob("rep*/r*/*.json"))) == 2 * len(C.records(batch))
    one = json.loads(next(out.glob("rep1/r1/*.json")).read_text())
    assert one["error"] is None and one["verify_patient"]["checks"] == []
    assert C.cmd_run(a, client_factory=C.DryVerifier, now=lambda: QUIET, cal_dir=tmp_path) == 2


@needs_runs
def test_a_canary_window_is_refused(tmp_path):
    night = dt.datetime(2026, 11, 5, 1, 50, tzinfo=rce.LONDON)
    assert C.cmd_run(_args("run", str(C.RUNS_DIR / DEV), "--yes"), client_factory=C.DryVerifier,
                     now=lambda: night, cal_dir=tmp_path) == 3


# --- labels -------------------------------------------------------------------------

@needs_runs
@pytest.mark.parametrize("bid", (DEV, *HELD))
def test_every_label_has_a_place(bid):
    """Instrument self-test: every ruled and mapped row lands on its sentence, its
    section, or (omissions) on the A section that holds its words."""
    n = 0
    for rec in C.records(C.RUNS_DIR / bid):
        vin, given = _ctx(rec)
        for lab in C.labels_for(rec) + C.map_labels(rec):
            n += 1
            kind, u = _unit_for(lab, vin, given)          # raises StopIteration if a sentence is missing
            if kind == "unreached":
                assert u["a_section_id"] is not None, (rec["generation_id"], lab["key"])
    assert n > 0


@needs_runs
def test_map_amendments_of_the_audit():
    doc = C.map_doc()
    by = {(f["generation_id"].split("/")[0][-6:-3], f["generation_id"].split("/")[-1], f.get("text") or f.get("a_text")): f
          for f in doc["findings"]}
    assert by[("p02", "C7", "half-plaster support")]["ruling"] == "acceptable"
    assert not any(k[:2] == ("p01", "S13") and "closed" in (k[2] or "") for k in by)
    bg = [f for f in doc["findings"] if f["generation_id"].endswith("p04-x5/r1/S1") and f.get("unit") == "unreached"]
    assert len(bg) == 5 and all(f["category"] == "policy_test" for f in bg)
    s18 = by[("p04", "S18", "the immediate risk of another one is low")]
    assert (s18["dimension"], s18["ruling"]) == ("withheld", "defect")
    assert "next_steps" in by[("p02", "S13", "New atrial fibrillation")]["note"]


# --- counting rule v2: a perfect verifier passes, a silent one fails ----------------

@needs_runs
def test_a_perfect_verifier_misses_nothing_and_false_flags_nothing(tmp_path):
    _write(tmp_path, DEV, _oracle)
    ev = C.evaluate([DEV], tmp_path)
    assert ev["errors"] == 0 and ev["missed"] == [] and ev["false_flags"] == 0
    assert ev["policy"] == {"rows": 5, "caught": 5}
    assert ev["defects"] == len([r for r in ev["rows"] if r["ruling"] == "defect" and r["category"] == "historical"])


@needs_runs
def test_a_silent_verifier_misses_every_defect(tmp_path):
    _write(tmp_path, DEV, lambda rec: {"verdicts": _supported_all(_ctx(rec)[0]), "unreached": []})
    ev = C.evaluate([DEV], tmp_path)
    assert len(ev["missed"]) == ev["defects"] > 0 and ev["false_flags"] == 0 and ev["policy"]["caught"] == 0


@needs_runs
def test_flagging_everything_acceptable_is_counted_as_false_flags(tmp_path):
    _write(tmp_path, DEV, lambda rec: _oracle(rec, which=("acceptable",)))
    ev = C.evaluate([DEV], tmp_path)
    # every acceptable row in the denominator had an opportunity to be flagged, so flagging
    # all of them gives a 100% false-flag rate (audit 2: ineligible rows are outside it)
    assert ev["false_flags"] == ev["acceptable"] > 0 and ev["false_rate"] == 1.0


# --- AUDIT 1: failed or missing calls block a pass -----------------------------------

@needs_runs
def test_audit1_a_missing_output_counts_its_defects_missed_and_blocks_the_pass(tmp_path):
    gid = f"{DEV}/r1/S4"
    _write(tmp_path, DEV, _oracle, repeats=3, skip={gid})
    ev = C.evaluate([DEV], tmp_path)
    assert ev["errors"] == 1 and ev["missed"] and all(r["generation_id"] == gid and r["error"] for r in ev["missed"])
    assert C.self_agreement(DEV, tmp_path) is None             # a leaflet missing in a repeat
    assert C.cmd_score(_args("score", "--dev", DEV), sha_dir=tmp_path) == 1
    assert "| 5 verifier failures | 1 | 0 | **NOT MET** |" in (tmp_path / "scores" / "CALIBRATION.md").read_text()


@needs_runs
def test_audit1_agreement_needs_exactly_three_complete_repeats(tmp_path):
    _write(tmp_path, DEV, _oracle, repeats=2)
    assert C.self_agreement(DEV, tmp_path) is None
    _write(tmp_path, DEV, _oracle, repeats=3)
    assert C.self_agreement(DEV, tmp_path) == 1.0


@needs_runs
def test_audit1_an_output_citing_a_section_not_given_is_a_failure(tmp_path):
    def bad(rec):
        o = _oracle(rec)
        o["verdicts"][0] = dict(o["verdicts"][0], a_section_id="sections.99")
        return o
    _write(tmp_path, DEV, bad)
    assert C.evaluate([DEV], tmp_path)["errors"] == len(C.records(C.RUNS_DIR / DEV))


# --- AUDIT 2: match the actual finding ------------------------------------------------

def _units_on(rec, pred):
    vin, given = _ctx(rec)
    return vin, given


@needs_runs
def test_audit2_a_flag_of_another_kind_in_the_section_does_not_catch_a_qualifier_row():
    rec = next(r for r in C.records(C.RUNS_DIR / DEV)
               if any(l["kind"] == "qualifier" and l["ruling"] == "defect" for l in C.labels_for(r)))
    vin, given = _ctx(rec)
    lab = next(l for l in C.labels_for(rec) if l["kind"] == "qualifier" and l["ruling"] == "defect")
    sec = next(s for s in vin["c_sections"] if s["heading"] == lab["heading"])
    a_sentence = lab["sentence"].split(" → C: ", 1)[0].removeprefix("input: ")
    t = max(sec["sentences"], key=lambda x: len(C._content(a_sentence) & C._content(x["text"])))
    other = {"kind": "sentence", "sentence_id": t["sentence_id"], "c_section_id": sec["section_id"],
             "heading": sec["heading"], "text": t["text"], "verdict": "added", "reason": "cause_or_purpose",
             "a_section_id": sec["source_ids"][0], "span": t["text"]}
    assert not C._row_matches(lab, other, vin, given)
    assert C._row_matches(lab, dict(other, verdict="qualifier_dropped", reason="qualifier"), vin, given)


@needs_runs
def test_audit2_an_omission_needs_its_own_a_section_and_most_of_its_words():
    gid = f"{HELD[0]}/r1/S13"
    rec = _rec(gid)
    vin, given = _ctx(rec)
    lab = next(l for l in C.map_labels(rec) if l.get("a_text") == "Line sepsis day 14")
    base = {"kind": "unreached", "c_section_id": "c_sections.0", "heading": "why_in_hospital",
            "a_section_id": C._a_section_of("Line sepsis day 14", given), "reason": "never_drop_item",
            "text": "", "verdict": "unreached"}
    assert C._row_matches(lab, dict(base, span="Line sepsis day 14"), vin, given)
    assert not C._row_matches(lab, dict(base, span="ICU-acquired weakness"), vin, given)       # another omission
    assert not C._row_matches(lab, dict(base, span="sepsis"), vin, given) or \
        len(C._content("Line sepsis day 14")) <= 2                                             # one shared word
    assert not C._row_matches(lab, dict(base, span="Line sepsis day 14", heading="next_steps"), vin, given)


@needs_runs
def test_audit2_one_unreached_item_credits_one_omission_only(tmp_path):
    gid = f"{HELD[0]}/r1/S13"

    def one_for_all(rec):
        o = {"verdicts": _supported_all(_ctx(rec)[0]), "unreached": []}
        if rec["generation_id"] == gid:
            _, given = _ctx(rec)
            o["unreached"] = [{"c_section_id": "c_sections.0", "heading": "why_in_hospital",
                               "a_section_id": C._a_section_of("ICU-acquired weakness", given), "reason": "never_drop_item",
                               "span": "Line sepsis day 14; ICU-acquired weakness; New atrial fibrillation"}]
        return o
    _write(tmp_path, HELD[0], one_for_all)
    rows = [r for r in C.evaluate([HELD[0]], tmp_path)["rows"] if r["generation_id"] == gid and r.get("unit") == "unreached"]
    assert len(rows) == 3 and sum(r["caught"] for r in rows) == 1


@needs_runs
def test_audit2_s13_breathing_is_not_deduplicated_by_the_feeding_qualifier_row():
    rec = _rec(f"{DEV}/r1/S13")
    _, given = _ctx(rec)
    m = next(l for l in C.map_labels(rec) if "breathing on your own" in l["sentence"])
    assert not any(r["ruling"] == "defect" and C._same_fault(m, r, given) for r in C.labels_for(rec))


@needs_runs
def test_audit2_a_true_duplicate_is_not_counted_twice():
    rec = _rec(f"{DEV}/r1/S4")
    _, given = _ctx(rec)
    m = next(l for l in C.map_labels(rec) if "swab" in l["sentence"])
    assert any(r["ruling"] == "defect" and C._same_fault(m, r, given) for r in C.labels_for(rec))


# --- AUDIT 3: dimension-specific rulings on one sentence -----------------------------

@needs_runs
def test_audit3_a_correct_cause_flag_is_not_a_false_flag_against_an_acceptable_medication_ruling(tmp_path):
    gid = f"{DEV}/r2/S1"
    rec = _rec(gid)
    labs = [l for l in C.labels_for(rec) if "isosorbide mononitrate" in l["sentence"]]
    assert {(l["kind"], l["ruling"]) for l in labs} >= {("medication", "acceptable"), ("cause", "defect")}
    _write(tmp_path, DEV, _oracle)
    rows = [r for r in C.evaluate([DEV], tmp_path)["rows"]
            if r["generation_id"] == gid and "isosorbide mononitrate" in (r.get("sentence") or "")]
    assert any(r["kind"] == "cause" and r["caught"] for r in rows)
    assert not any(r["kind"] == "medication" and r["caught"] for r in rows)


@needs_runs
def test_audit3_two_defects_in_one_sentence_need_two_units(tmp_path):
    gid = f"{DEV}/r2/S4"

    def one_unit(rec):
        o = _oracle(rec)
        if rec["generation_id"] == gid:                   # keep only the first fault on the swab sentence
            seen, keep = set(), []
            for v in o["verdicts"]:
                if v["verdict"] != "supported" and "nasopharyngeal aspirate" in v["text"]:
                    if v["sentence_id"] in seen:
                        continue
                    seen.add(v["sentence_id"])
                keep.append(v)
            o["verdicts"] = keep
        return o
    _write(tmp_path, DEV, one_unit)
    missed = [r for r in C.evaluate([DEV], tmp_path)["missed"] if r["generation_id"] == gid]
    assert len(missed) == 1


@needs_runs
def test_audit3_s18_withheld_flag_is_credited_and_not_a_false_flag_against_its_cause_ruling(tmp_path):
    gid = f"{DEV}/r1/S18"
    _write(tmp_path, DEV, _oracle)
    rows = [r for r in C.evaluate([DEV], tmp_path)["rows"] if r["generation_id"] == gid and "immediate risk" in (r.get("sentence") or "")]
    assert any(r["kind"] == "withheld" and r["ruling"] == "defect" and r["caught"] for r in rows)
    assert not any(r["ruling"] == "acceptable" and r["caught"] for r in rows)


# --- AUDIT 4: a supported verdict on an uncited source blocks acceptance -------------

@needs_runs
def test_audit4_supported_on_an_uncited_source_blocks_the_pass(tmp_path):
    def uncited(rec):
        o = _oracle(rec)
        vin, _ = _ctx(rec)
        for v in o["verdicts"]:
            sec = next(s for s in vin["c_sections"] if s["section_id"] == v["c_section_id"])
            other = [a["section_id"] for a in vin["a_sections"] if a["section_id"] not in sec["source_ids"]]
            if v["verdict"] == "supported" and other:
                v["a_section_id"] = other[0]
                break
        return o
    _write(tmp_path, DEV, uncited, repeats=3)
    ev = C.evaluate([DEV], tmp_path)
    assert ev["uncited_supported"] > 0 and ev["errors"] == 0
    assert C.cmd_score(_args("score", "--dev", DEV), sha_dir=tmp_path) == 1


@needs_runs
def test_score_refuses_mixed_prompts(tmp_path):
    _write(tmp_path, DEV, _oracle, repeats=3)
    _write(tmp_path, HELD[0], _oracle)
    (tmp_path / HELD[0] / "manifest.json").write_text(json.dumps({"prompt_sha256": "f" * 64}))
    assert C.cmd_score(_args("score", "--dev", DEV, "--held-out", HELD[0]), sha_dir=tmp_path) == 2


@needs_runs
def test_a_perfect_verifier_passes_the_whole_bar_on_held_out(tmp_path):
    _write(tmp_path, DEV, _oracle, repeats=3)
    for b in HELD:
        _write(tmp_path, b, _oracle)
    assert C.cmd_score(_args("score", "--dev", DEV, "--held-out", *HELD), sha_dir=tmp_path) == 0
    md = (tmp_path / "scores" / "CALIBRATION.md").read_text()
    assert "judged on: held-out" in md and "**NOT MET**" not in md and "counting rule v3" in md


# --- CSO audit 2 (4 Nov 2026): eligibility from the saved text, both sets -------------

def _independent_ineligible(bid):
    """Acceptable rows no unit could match, counted here WITHOUT C.eligible: qualifier rows
    whose A sentence shares no content word with any sentence of their C section."""
    n = 0
    for rec in C.records(C.RUNS_DIR / bid):
        vin, given = _ctx(rec)
        for lab in C.labels_for(rec):
            if lab["kind"] != "qualifier" or lab["ruling"] != "acceptable":
                continue
            a = lab["sentence"].split(" → C: ", 1)[0].removeprefix("input: ")
            words = {w for s in vin["c_sections"] if s["heading"] == lab["heading"]
                     for t in s["sentences"] for w in C._content(t["text"])}
            n += not (C._content(a) & words)
    return n


@needs_runs
@pytest.mark.parametrize("bid", (DEV, *HELD))
def test_audit2_ineligible_acceptable_rows_are_outside_the_denominator_and_reported(tmp_path, bid):
    _write(tmp_path, bid, lambda rec: {"verdicts": _supported_all(_ctx(rec)[0]), "unreached": []})
    ev = C.evaluate([bid], tmp_path)
    assert ev["acceptable_no_opportunity"] == _independent_ineligible(bid)
    n_acc_all = sum(1 for rec in C.records(C.RUNS_DIR / bid)
                    for l in C.labels_for(rec) + C.map_labels(rec)
                    if l["ruling"] == "acceptable" and l["category"] == "historical")
    assert ev["acceptable"] + ev["acceptable_no_opportunity"] == n_acc_all


@needs_runs
@pytest.mark.parametrize("bid", (DEV, *HELD))
def test_audit2_eligibility_does_not_depend_on_verifier_output(tmp_path, bid):
    _write(tmp_path / "a", bid, lambda rec: {"verdicts": _supported_all(_ctx(rec)[0]), "unreached": []})
    _write(tmp_path / "b", bid, _oracle)
    ea, eb = C.evaluate([bid], tmp_path / "a"), C.evaluate([bid], tmp_path / "b")
    assert (ea["acceptable"], ea["acceptable_no_opportunity"], ea["defects"]) == \
        (eb["acceptable"], eb["acceptable_no_opportunity"], eb["defects"])


@needs_runs
@pytest.mark.parametrize("bid", (DEV, *HELD))
def test_audit2_no_defect_row_is_without_a_place(tmp_path, bid):
    _write(tmp_path, bid, _oracle)
    assert C.evaluate([bid], tmp_path)["defects_no_place"] == []



@needs_runs
@pytest.mark.parametrize("gid,words", [
    (f"{HELD[1]}/r1/C7", "Please make sure you attend this."),            # ruled advice (token-less)
    (f"{HELD[1]}/r1/S4", "will be there if you have any concerns"),       # ruled advice (token-less)
    (f"{DEV}/r1/S16", "A community stoma nurse will follow you up at home."),   # ruled promise
    (f"{DEV}/r1/S3", "recent relationship breakdown and work stress"),    # ruled diagnosis, "label — content"
])
def test_audit2_the_same_fault_is_not_counted_twice(gid, words):
    rec = _rec(gid)
    _, given = _ctx(rec)
    m = next(l for l in C.map_labels(rec) if words in (l.get("text") or l.get("a_text") or ""))
    assert any(r["ruling"] == "defect" and C._same_fault(m, r, given) for r in C.labels_for(rec))
