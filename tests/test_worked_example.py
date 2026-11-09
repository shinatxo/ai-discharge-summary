"""Tests for the worked example's exporter and checker (WS1a step build piece 8, 9 Nov 2026).

Fixtures are SYNTHETIC: an offline replay of the committed batch's S1 r1 (no model, no AWS,
$0) wrapped in a fake piece-7 evidence folder with the real schema (manifest attempt with
`input_sha256`; audit row PK USER#<sub> + user_sub + idempotency_key; ResultsTable item with
PK/SK/user_sub/ttl and JSON-string fields; p7 check receipt). Each test binds the checker to
its fixture (job, notes hash, source byte hashes) — the committed example's own binding is
tested in test_worked_example_committed.py.

Covered: the round trip exports and checks MET; every exporter STOP; every checker code by a
mutation; positive controls for every privacy detector, including inside embedded JSON; the
three trail paths (rejected citations kept as diagnostics; producer flags compared by category,
recorded representation and occurrence; boxes; recounts by the producer's definitions); and
the frozen-rules comparison with the current producer (a difference prompts review).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "src" / "generate"))

import check_worked_example as C  # noqa: E402
import export_worked_example as X  # noqa: E402

BATCH = ROOT / "evals" / "runs" / "w6-pipeline-e09-m01-c02-p04-x5"
needs_batch = pytest.mark.skipif(not BATCH.exists(), reason="saved batch not present (e.g. the eval image)")

SUB = "9f8e7d6c-1111-4222-8333-444455556666"
IDEM = "aaaaaaaa-bbbb-4ccc-8ddd-000000000002"
COMMIT = "0" * 40
NOW = "2026-11-09T15:00:00+00:00"
sha = lambda b: hashlib.sha256(b).hexdigest()


# --- fixtures ------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def replay():
    import replay_orchestrate as RO
    class W:
        last = None

        def __init__(self):
            self.items = []
            W.last = self

        def put(self, item):
            self.items.append(json.loads(json.dumps(item)))
    saved = RO.Writer
    RO.Writer = W
    try:
        rec = json.loads((BATCH / "r1" / "S1.json").read_text())
        corpus, _ = RO.rce.load_corpus(RO.rce.DEFAULT_CORPUS_PATH)
        notes = {s.id: s for s in corpus}["S1"].notes
        r = RO.replay_record(rec, notes)
    finally:
        RO.Writer = saved
    items = W.last.items
    assert r["outcome"] == "bundle" and len(items) == 12
    meta = next(i for i in items if i["SK"] == "META")
    items.append({"PK": meta["PK"], "SK": "DELIVERY", "outcome_of": "delivery", "results": "written",
                  "audit": "complete", "at": meta["completed_at"], "ttl": meta["ttl"]})
    return sorted(items, key=lambda i: i["SK"].encode()), notes


def _evidence(root: Path, trace: list, notes: str) -> dict:
    """A fake piece-7 evidence folder for `trace`; returns its binding."""
    s = C._by_sk(trace)
    meta, out8 = s["META"], s["STEP#08b#assemble_patient#A1"]["output"]
    job = meta["PK"].split("#", 1)[1]
    nsha = hashlib.sha256(notes.encode()).hexdigest()
    h = out8["hashes"]
    audit = {"PK": f"USER#{SUB}", "SK": f"GEN#{job}", "generation_id": job, "user_sub": SUB,
             "idempotency_key": IDEM, "status": "complete", "draft": True, "reviewed_at": None,
             "started_at": meta["started_at"], "completed_at": meta["completed_at"], "input_sha256": nsha,
             "output_type": "summary,gp_letter,patient", "request_region": "eu-west-2",
             "inference_profile": "n/a (on-demand)", "schema_version": 2,
             "pipeline_version": meta["pipeline_version"], "model_version": "m (eu-west-2, on-demand)",
             "output_sha256": {"summary": h["part_a"], "gp_letter": h["part_b"], "patient": h["part_c"]},
             "parse_ok": True, "input_tokens": 1, "output_tokens": 1, "model_calls": 4, "safety_net_gate": "pass"}
    results = {"PK": f"USER#{SUB}", "SK": f"RES#{job}", "user_sub": SUB, "job_id": job,
               "summary": out8["part_a"], "gp_letter": out8["part_b"], "patient": out8["part_c"],
               "units_json": json.dumps({"units": [{"id": "u1", "fact_ids": ["treatment.0"]}]}),
               "templates_json": json.dumps({"t": "x"}), "warnings_json": json.dumps(out8.get("warnings") or []),
               "output_sha256_json": json.dumps(dict(h)), "pipeline_version": meta["pipeline_version"],
               "generated_on": meta["generated_on"], "completed_at": meta["completed_at"], "ttl": 1}
    check = {"criteria": {k: True for k in C.RECEIPT_CRITERIA},
             "coverage": {k: True for k in C.RECEIPT_COVERAGE["S1"]}, "met": True, "repeat_eligible": False,
             "hard_stops_clear": True, "recorded": {"items": 13, "pages": 1}, "intervals": {}, "privacy": {"hits": 0}}
    d = root / "evidence" / job / "snap-1"
    d.mkdir(parents=True)
    files = {"trace.json": trace, "audit.json": audit, "results.json": results, "check.json": check,
             "_snapshot.json": {"at": NOW, "unresolved": []}}
    for n, o in files.items():
        (d / n).write_text(json.dumps(o, indent=1))
    (root / "manifest.json").write_text(json.dumps({"attempts": [
        {"n": 1, "scenario": "FAILPROBE", "job_id": "01FAILPROBE", "met": True},
        {"n": 2, "scenario": "S1", "job_id": job, "met": True, "checked_snapshot": "snap-1",
         "input_sha256": nsha, "idempotency_key": IDEM}]}))
    (root / "preflight.json").write_text(json.dumps({"commit": COMMIT}))
    return rebind(root, {"scenario": "S1", "job_id": job, "snapshot": "snap-1", "deployed_commit": COMMIT,
                         "pipeline_version": meta["pipeline_version"], "stack_name": "discharge-eph-test",
                         "notes_sha256": nsha, "source_sha256": {}})


def rebind(root: Path, b: dict) -> dict:
    d = root / "evidence" / b["job_id"] / b["snapshot"]
    b = copy.deepcopy(b)
    b["source_sha256"] = {n: sha((d / n).read_bytes()) for n in X.SOURCE_FILES}
    return b


@pytest.fixture
def ev(tmp_path, replay):
    trace, _ = replay
    root = tmp_path / "eph"
    b = _evidence(root, copy.deepcopy(trace), C.corpus_notes()["S1"])
    return root, b


def _export(root, b, out):
    return X.export(root, out, binding=b, rules_pv=b["pipeline_version"], now=NOW)


def _cso_read(out: Path):
    p = out / "README.md"
    p.write_text(p.read_text().replace(C.README_PENDING, "absent in this run (fixture)"))


@pytest.fixture
def example(tmp_path, ev):
    root, b = ev
    out = tmp_path / "w6-worked-example-S1-test"
    _export(root, b, out)
    _cso_read(out)
    return out, b


def _check(out, b, **kw):
    return C.check(out, binding=b, rules_pv=kw.pop("rules_pv", b["pipeline_version"]), **kw)[0]


# --- the round trip ------------------------------------------------------------------------------

@needs_batch
def test_round_trip_exports_and_checks_met(ev, tmp_path):
    root, b = ev
    out = tmp_path / "ex"
    r = _export(root, b, out)
    assert r["pending"].startswith("readme")
    assert _check(out, b) == ["readme"]                       # waits only for the CSO read
    _cso_read(out)
    assert _check(out, b) == []
    src = root / "evidence" / b["job_id"] / "snap-1"
    assert (out / "trace.json").read_bytes() == (src / "trace.json").read_bytes()   # byte copy
    assert set(json.loads((out / "audit.json").read_text())) == C.AUDIT_FIELDS and len(C.AUDIT_FIELDS) == 19
    assert set(json.loads((out / "release.json").read_text())) == C.RELEASE_FIELDS and len(C.RELEASE_FIELDS) == 11
    rel, res = json.loads((out / "release.json").read_text()), json.loads((src / "results.json").read_text())
    assert all(rel[k] == res[k] for k in C.JSON_FIELDS)        # JSON-string fields: the same strings
    blob = b"".join(p.read_bytes() for p in out.iterdir())
    assert SUB.encode() not in blob and IDEM.encode() not in blob
    assert not out.with_name(out.name + ".partial").exists()
    prov = json.loads((out / "provenance.json").read_text())
    assert prov["source"]["files"]["trace.json"]["sha256"] == prov["export"]["files"]["trace.json"]["sha256"]
    assert prov["source"]["files"]["audit.json"]["sha256"] != prov["export"]["files"]["audit.json"]["sha256"]


@needs_batch
def test_cli_default_binding_refuses_a_fixture(example):
    out, _ = example
    assert C.main([str(out)]) == 1                            # bound to the committed run, not this one


# --- exporter STOPs (nothing written to --out) ---------------------------------------------------

def _mut_json(root, b, name, fn, rebind_after=True):
    p = root / "evidence" / b["job_id"] / b["snapshot"] / name
    o = json.loads(p.read_text())
    fn(o)
    p.write_text(json.dumps(o, indent=1))
    return rebind(root, b) if rebind_after else b


def _mut_manifest(root, fn):
    p = root / "manifest.json"
    m = json.loads(p.read_text())
    fn(next(a for a in m["attempts"] if a["scenario"] == "S1" or a.get("n") == 2))
    p.write_text(json.dumps(m))


STOPS = {
    "bound_job_once": lambda r, b: dict(b, job_id="01OTHERJOB"),
    "scenario": lambda r, b: (_mut_manifest(r, lambda a: a.update(scenario="S13")), b)[1],
    "met": lambda r, b: (_mut_manifest(r, lambda a: a.update(met=False)), b)[1],
    "checked_snapshot": lambda r, b: (_mut_manifest(r, lambda a: a.update(checked_snapshot="snap-2")), b)[1],
    "latest_complete": lambda r, b: (shutil.copytree(r / "evidence" / b["job_id"] / "snap-1",
                                                     r / "evidence" / b["job_id"] / "snap-2"), b)[1],
    "unresolved": lambda r, b: _mut_json(r, b, "_snapshot.json", lambda o: o.update(unresolved=["x"])),
    "preflight_commit": lambda r, b: ((r / "preflight.json").write_text(json.dumps({"commit": "1" * 40})), b)[1],
    "source_hashes": lambda r, b: _mut_json(r, b, "results.json", lambda o: o.update(summary=o["summary"] + "."),
                                            rebind_after=False),
    "notes_sha256": lambda r, b: (_mut_manifest(r, lambda a: a.update(input_sha256="0" * 64)), b)[1],
    "audit_attributes": lambda r, b: _mut_json(r, b, "audit.json", lambda o: o.update(new_attribute=1)),
    "results_attributes": lambda r, b: _mut_json(r, b, "results.json", lambda o: o.pop("units_json")),
    "audit_complete": lambda r, b: _mut_json(r, b, "audit.json", lambda o: o.update(status="pending")),
    "privacy": lambda r, b: _mut_json(r, b, "trace.json", lambda o: o[0].update(note=f"see {SUB}")),
}


@needs_batch
@pytest.mark.parametrize("code", sorted(STOPS))
def test_exporter_stops(ev, tmp_path, code):
    root, b = ev
    b2 = STOPS[code](root, b)
    out = tmp_path / "ex"
    with pytest.raises(X.Stop) as e:
        _export(root, b2, out)
    assert e.value.code == code
    assert not out.exists() and not out.with_name(out.name + ".partial").exists()


@needs_batch
def test_exporter_stops_on_the_actual_identity_values_even_when_no_detector_fires(ev, tmp_path):
    root, b = ev
    sub = "subject-x7"                                         # not UUID-shaped, no USER# prefix
    b = _mut_json(root, b, "audit.json", lambda o: o.update(PK=f"USER#{sub}", user_sub=sub))
    b = _mut_json(root, b, "trace.json", lambda o: o[0].update(note=f"by {sub}"))
    with pytest.raises(X.Stop) as e:
        _export(root, b, tmp_path / "ex")
    assert e.value.code == "identity_absent"


@needs_batch
def test_exporter_refuses_an_existing_out(ev, tmp_path):
    root, b = ev
    out = tmp_path / "ex"
    out.mkdir()
    with pytest.raises(X.Stop) as e:
        _export(root, b, out)
    assert e.value.code == "out_exists"


# --- checker codes, one mutation each ------------------------------------------------------------

def _edit(out, name, fn, text=False):
    p = out / name
    if text:
        p.write_text(fn(p.read_text()))
    else:
        o = json.loads(p.read_text())
        r = fn(o)
        p.write_text(json.dumps(o if r is None else r, indent=1))


def _rel(fn):
    return lambda out: _edit(out, "release.json", fn)


def _units(fn):
    def go(out):
        def f(o):
            u = json.loads(o["units_json"])
            fn(u)
            o["units_json"] = json.dumps(u)
        _edit(out, "release.json", f)
    return go


def _trace(fn):
    return lambda out: _edit(out, "trace.json", fn)


def _prov(fn):
    return lambda out: _edit(out, "provenance.json", fn)


def _sk(o, sk):
    return next(i for i in o if i["SK"] == sk)


MUTATIONS = {
    "files_set": lambda out: (out / "extra.txt").write_text("x"),
    "json_parse": lambda out: (out / "trail.json").write_text("{"),
    "binding": _prov(lambda o: o["binding"].update(stack_name="other")),
    "source_hashes": _prov(lambda o: o["source"]["files"]["audit.json"].update(sha256="0" * 64)),
    "trace_bytes": lambda out: (out / "trace.json").write_bytes((out / "trace.json").read_bytes() + b"\n"),
    "export_hashes": _prov(lambda o: o["export"]["files"]["release.json"].update(sha256="0" * 64)),
    "trace_sks": _trace(lambda o: [o[1], o[0], *o[2:]]),
    "trace_pk": _trace(lambda o: o[3].update(PK="GEN#other")),
    "trace_ttl": _trace(lambda o: o[4].update(ttl=o[4]["ttl"] + 1)),
    "meta_complete": _trace(lambda o: _sk(o, "META").update(status="failed")),
    "pipeline_version": lambda out: _edit(out, "audit.json", lambda o: o.update(pipeline_version="1" * 64)),
    "job_ids": _rel(lambda o: o.update(job_id="01OTHER")),
    "release_truth": lambda out: _edit(out, "audit.json", lambda o: o.update(status="pending")),
    "audit_fields": lambda out: _edit(out, "audit.json", lambda o: o.update(user_sub_hash="x")),
    "release_fields": _rel(lambda o: (o.pop("templates_json"), None)[1]),
    "json_fields": _rel(lambda o: o.update(warnings_json="{bad")),
    "hash_audit": lambda out: _edit(out, "audit.json", lambda o: o["output_sha256"].update(patient="0" * 64)),
    "hash_results": _rel(lambda o: o.update(output_sha256_json=json.dumps(
        dict(json.loads(o["output_sha256_json"]), part_b="0" * 64)))),
    "hash_8b": _trace(lambda o: _sk(o, "STEP#08b#assemble_patient#A1")["output"]["hashes"].update(part_a="0" * 64)),
    "docs_8b": _trace(lambda o: _sk(o, "STEP#08b#assemble_patient#A1")["output"].update(
        part_c=_sk(o, "STEP#08b#assemble_patient#A1")["output"]["part_c"] + ".")),
    "receipt": _prov(lambda o: o["receipt"]["criteria"].update(overlap_5a_6=False)),
    "trail_report": lambda out: _edit(out, "trail.json", lambda o: o["lines"].update(line_ids=0)),
    "readme": lambda out: _edit(out, "README.md", lambda t: t.replace(C.REQUIRED_README[2], ""), text=True),
    "privacy_user_sub_key": _units(lambda u: u.update(user_sub="x")),
    "privacy_user_hash": _units(lambda u: u.update(k="USER#x")),
    "privacy_uuid": _units(lambda u: u.update(k=SUB)),
    "privacy_idempotency_key": _units(lambda u: u.update(idempotency_key="x")),
    "privacy_notes_key": _units(lambda u: u.update(notes="raw notes")),
    "privacy_line_index": _units(lambda u: u.update(idx={"L001": "a", "L002": "b"})),
    "privacy_quote_len": _units(lambda u: u.update(quote="q" * 201)),
    "privacy_notes_run": _units(lambda u: u.update(k=C.corpus_notes()["S1"][:250])),
    "privacy_whole_notes": _units(lambda u: u.update(k=C.corpus_notes()["S1"])),
    "privacy_arn": _units(lambda u: u.update(k="arn:aws:kms:x")),
    "privacy_acct12": _units(lambda u: u.update(k="acct :123456789012:")),
}


@needs_batch
@pytest.mark.parametrize("code", sorted(MUTATIONS))
def test_checker_code(example, code):
    out, b = example
    assert _check(out, b) == []
    MUTATIONS[code](out)
    assert code in _check(out, b)


@needs_batch
def test_trace_mutations_delete_duplicate_substitute(example):
    out, b = example
    orig = (out / "trace.json").read_bytes()
    for fn in (lambda o: o.pop(5) and None, lambda o: o.insert(6, copy.deepcopy(o[5])),
               lambda o: o[6].update(SK="STEP#05b#select_safety_net#A2")):
        (out / "trace.json").write_bytes(orig)
        _edit(out, "trace.json", fn)
        assert "trace_sks" in _check(out, b)


@needs_batch
def test_binding_rejects_consistent_substitution_and_wrong_scenario(example):
    out, b = example
    assert "binding" in _check(out, dict(b, job_id="01M6V35PH1GQBC07TSQNB7XXXX"))
    assert {"binding", "trace_pk", "job_ids"} <= set(_check(out, dict(b, job_id="01M6V35PH1GQBC07TSQNB7XXXX")))
    assert {"binding", "notes_sha256"} <= set(_check(out, dict(b, scenario="S13")))
    assert "binding" in _check(out, dict(b, snapshot="snap-2"))


@needs_batch
def test_rules_frozen_to_the_source_pipeline_version(example):
    out, b = example
    assert _check(out, b, rules_pv="1" * 64) == ["rules_version"]


@needs_batch
def test_readme_placeholder_must_be_resolved(example):
    out, b = example
    _edit(out, "README.md", lambda t: t + "\n" + C.README_PENDING + "\n", text=True)
    assert _check(out, b) == ["readme"]


@needs_batch
def test_negative_controls_hex_and_plain_numbers_do_not_fire(example):
    out, b = example
    _units(lambda u: u.update(h="ab123456789012cd" + "e" * 48, n="ten digits 1234567890", t=12345678901234))(out)
    fails = _check(out, b)
    assert not any(c.startswith("privacy_") for c in fails)


# --- the trail: three paths, the producer's own diagnostics --------------------------------------

def _t(replay):
    return copy.deepcopy(replay[0])


def _steps(trace):
    s = C._by_sk(trace)
    return (s["STEP#01#guard_input#A1"]["output"], s["STEP#03#validate_facts#A1"]["output"],
            s["STEP#06#compose_clinical#A1"], s["STEP#07#compose_patient#A1"])


def _sync6(trace):
    st1, st3, s6, s7 = _steps(trace)
    s6["input"]["facts"] = copy.deepcopy(st3["facts"])


def _a_med(st3):
    meds = st3["facts"]["medications"]
    return (meds["discharge"] or meds["pre_admission"])[0]


@needs_batch
def test_trail_baseline_reports_without_failing(replay):
    t = C.trail_report(_t(replay))
    assert t["rules_pipeline_version"] == C.RULES_PIPELINE_VERSION
    assert t["step6_sections_to_facts"]["boxes"] and t["step7_sections_to_a_sections"]["boxes"]
    assert t["step3_facts_to_lines"]["cites_total"] >= t["step3_facts_to_lines"]["cites_verified"] > 0


@needs_batch
def test_trail_rejected_L999_kept_as_diagnostic_but_not_as_verified(replay):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    it = _a_med(st3)                                           # medications are not given to step 6
    it["cites"] = [{"lines": ["L999"], "quote": "x", "verified": False, "reason": "line_out_of_range"}]
    it["citation_status"], it["reason"] = "citation_unverified", "line_out_of_range"
    _recount(st3)
    _sync6(tr)
    t = C.trail_report(tr)
    assert {"fact_id": it["fact_id"], "lines": ["L999"], "reason": "line_out_of_range"} in t["step3_facts_to_lines"]["unverified_cites"]
    it["cites"][0].update(verified=True, reason=None)          # presented as verified
    it["citation_status"], it["reason"] = "verified", None
    _recount(st3)
    _sync6(tr)
    with pytest.raises(C.TrailError) as e:
        C.trail_report(tr)
    assert e.value.code == "trail_verified_invalid"


def _recount(st3):
    """The producer's definitions, written independently: citations (items + suspicious text)
    vs ITEMS unverified."""
    f = st3["facts"]
    items = [i for fld in f["fields"].values() for i in fld["items"]] + f["medications"]["pre_admission"] \
        + f["medications"]["discharge"] + f["documented_advice"] + f["contradictions"] + [f["resus"]]
    cites = [c for i in items for c in i["cites"]] + f["suspicious_text"]
    st3["counts"].update(cites_total=len(cites), cites_verified=sum(1 for c in cites if c["verified"]),
                         items_unverified=sum(1 for i in items if i["citation_status"] == "citation_unverified"))


@needs_batch
def test_trail_counts_items_not_citations_and_suspicious_text_only_in_cites(replay):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    it = _a_med(st3)
    good = next(c for i in [it] for c in i["cites"] if c["verified"]) if any(c["verified"] for c in it["cites"]) else None
    it["cites"] = [{"lines": ["L999"], "quote": "x", "verified": False, "reason": "line_out_of_range"},
                   {"lines": ["L998"], "quote": "y", "verified": False, "reason": "line_out_of_range"}]
    it["citation_status"], it["reason"] = "citation_unverified", "line_out_of_range"
    if good is not None:
        st3["facts"]["suspicious_text"].append(dict(good, fact_id="suspicious_text.0"))
    _recount(st3)
    _sync6(tr)
    C.trail_report(tr)                                         # producer's definitions: passes
    st3["counts"]["items_unverified"] += 1                     # counting the two citations as two items
    with pytest.raises(C.TrailError) as e:
        C.trail_report(tr)
    assert e.value.code == "trail_counts"


def _first_prose(s6, part="part_a"):
    return next(x for x in s6["output"]["sections"] if x["part"] == part and x["heading"] not in C.STEP6_BOXES and x["fact_ids"])


def _flag6(s6, check, sec, fid):
    s6["output"]["checks"].append({"check": check, "part": sec["part"], "heading": sec["heading"], "fact_id": fid, "token": None})


@needs_batch
@pytest.mark.parametrize("ref,flag,ok", [
    ("BAD ID", ("unknown_fact_id", None), True),               # malformed: recorded as null
    ("BAD ID", ("unknown_fact_id", "BAD ID"), False),          # a recorded ID the producer would not keep
    ("treatment.999", ("unknown_fact_id", "treatment.999"), True),
    ("treatment.999", ("unknown_fact_id", None), False),
    ("treatment.999", None, False),                            # flag missing
    ("medications.discharge.0", ("excluded_fact_id", "medications.discharge.0"), True),
    ("medications.discharge.0", ("unknown_fact_id", "medications.discharge.0"), False),   # wrong category
    ("resus", ("excluded_fact_id", "resus"), True),
])
def test_trail_step6_flags_by_category_representation_and_occurrence(replay, ref, flag, ok):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    sec = _first_prose(s6)
    sec["fact_ids"].append(ref)
    if flag:
        _flag6(s6, flag[0], sec, flag[1])
    if ok:
        t = C.trail_report(tr)
        assert any(x["check"] == flag[0] and x["fact_id"] == flag[1] for x in t["step6_sections_to_facts"]["flags"])
        sec["fact_ids"].append(ref)                            # a second occurrence, one flag
        with pytest.raises(C.TrailError) as e:
            C.trail_report(tr)
        assert e.value.code == "trail_flags_6"
    else:
        with pytest.raises(C.TrailError) as e:
            C.trail_report(tr)
        assert e.value.code == "trail_flags_6"


@needs_batch
def test_trail_extra_producer_flag_fails(replay):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    _flag6(s6, "unknown_fact_id", _first_prose(s6), None)
    with pytest.raises(C.TrailError) as e:
        C.trail_report(tr)
    assert e.value.code == "trail_flags_6"


@needs_batch
def test_trail_held_back_fact_cited_is_an_excluded_diagnostic(replay):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    sec = _first_prose(s6)
    fid = sec["fact_ids"][0]
    field = next(n for n, f in st3["facts"]["fields"].items() if any(i["fact_id"] == fid for i in f["items"]))
    s6["output"]["held_back"].append({"fact_id": fid, "field": field})
    for x in s6["output"]["sections"]:
        for _ in range(x["fact_ids"].count(fid)):
            _flag6(s6, "excluded_fact_id", x, fid)
        x["unverified_fact_ids"] = [f for f in x["unverified_fact_ids"] if f != fid]
    t = C.trail_report(tr)
    assert fid in t["step6_sections_to_facts"]["held_back"]
    s6["output"]["held_back"].append({"fact_id": "contradictions.0", "field": "x"})   # never held
    with pytest.raises(C.TrailError) as e:
        C.trail_report(tr)
    assert e.value.code == "trail_held"


@needs_batch
def test_trail_unverified_fact_ids_must_match(replay):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    sec = _first_prose(s6)
    sec["unverified_fact_ids"] = sec["unverified_fact_ids"] + [sec["fact_ids"][0]] if sec["fact_ids"][0] not in sec["unverified_fact_ids"] else []
    with pytest.raises(C.TrailError) as e:
        C.trail_report(tr)
    assert e.value.code == "trail_unverified_6"


@needs_batch
@pytest.mark.parametrize("box_step", ["6", "7"])
def test_trail_box_with_a_reference_fails_and_empty_box_passes(replay, box_step):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    C.trail_report(tr)                                         # empty boxes: by design
    if box_step == "6":
        box = next(x for x in s6["output"]["sections"] if x["heading"] in C.STEP6_BOXES)
        box["fact_ids"].append("medications.discharge.0")
    else:
        box = next(x for x in s7["output"]["sections"] if x["heading"] in C.STEP7_BOXES)
        box["source_ids"].append("sections.0")
    with pytest.raises(C.TrailError) as e:
        C.trail_report(tr)
    assert e.value.code == f"trail_box_{box_step}"


@needs_batch
@pytest.mark.parametrize("ref,recorded,ok", [
    ("sections.99", "sections.99", True), ("zz", None, True),
    ("sections.99", None, False), ("zz", "zz", False), ("sections.99", "missing", False)])
def test_trail_step7_unknown_source_ids(replay, ref, recorded, ok):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    sec = next(x for x in s7["output"]["sections"] if x["source_ids"])
    sec["source_ids"].append(ref)
    if recorded != "missing":
        s7["output"]["checks"].append({"check": "unknown_source_id", "heading": sec["heading"], "source_id": recorded, "token": None})
    if ok:
        C.trail_report(tr)
    else:
        with pytest.raises(C.TrailError) as e:
            C.trail_report(tr)
        assert e.value.code == "trail_flags_7"


@needs_batch
def test_trail_step7_input_must_be_step6_a_sections(replay):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    s7["input"]["sections"][0]["text"] += "."
    with pytest.raises(C.TrailError) as e:
        C.trail_report(tr)
    assert e.value.code == "trail_step7_input"


@needs_batch
def test_trail_step6_input_must_be_step3_facts(replay):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    s6["input"]["facts"]["age_group"] = "other"
    with pytest.raises(C.TrailError) as e:
        C.trail_report(tr)
    assert e.value.code == "trail_step6_input"


@needs_batch
def test_trail_withheld_line_markers_kept_and_counted(replay):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    st1["lines"]["line_ids"].append("[withheld]")
    st1["line_count"] += 1
    C._by_sk(tr)["META"]["line_count"] += 1
    assert C.trail_report(tr)["lines"]["withheld_markers"] == 1
    st1["line_count"] += 1
    with pytest.raises(C.TrailError) as e:
        C.trail_report(tr)
    assert e.value.code == "trail_line_count"


@needs_batch
def test_trail_unknown_reason_fails(replay):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    it = _a_med(st3)
    it["cites"] = [{"lines": ["L001"], "quote": "x", "verified": False, "reason": "repaired"}]
    it["citation_status"], it["reason"] = "citation_unverified", "repaired"
    _recount(st3)
    _sync6(tr)
    with pytest.raises(C.TrailError) as e:
        C.trail_report(tr)
    assert e.value.code == "trail_reason_unknown"


# --- the frozen rules vs the current producer (a difference prompts review) ----------------------

REVIEW = ("the producer's rules differ from the worked example's frozen copy (pipeline "
          f"{C.RULES_PIPELINE_VERSION[:12]}): review evals/check_worked_example.py — do not update "
          "the frozen rules silently; the committed example keeps the rules of its own pipeline")


def test_frozen_rules_match_the_current_producer():
    from pipeline import compose, compose_patient, orchestrate, registry, validate
    vsrc = Path(validate.__file__).read_text()
    csrc = Path(compose.__file__).read_text()
    assert validate.QUOTE_MAX_CHARS == C.QUOTE_MAX_CHARS and validate.CITE_MAX_LINES == C.CITE_MAX_LINES, REVIEW
    assert validate._LINE_ID.pattern == r"^L\d{3}$", REVIEW
    assert set(re.findall(r'out\["reason"\] = "(\w+)"', vsrc)) == C.CITE_REASONS, REVIEW
    assert set(re.findall(r'\breason = "(\w+)"', vsrc)) == C.ITEM_REASONS - C.CITE_REASONS, REVIEW
    assert set(compose.BOXES) == C.STEP6_BOXES and compose.EXCLUDED_IDS == C.STEP6_EXCLUDED_IDS, REVIEW
    assert compose.EXCLUDED_PREFIXES == C.STEP6_EXCLUDED_PREFIXES, REVIEW
    assert 're.fullmatch(r"[a-z_]+\\.\\d+|resus", fid)' in csrc, REVIEW
    assert set(C.STEP6_FLAGS) <= set(compose.CHECKS), REVIEW
    assert set(compose_patient.BOXES) == C.STEP7_BOXES, REVIEW
    assert compose_patient._SECTION_ID.pattern == C.STEP7_RECORDED_SID.pattern, REVIEW
    assert orchestrate._LINE_ID.pattern == C.RAW_LINE_KEY.pattern and orchestrate.QUOTE_MAX_CHARS == C.QUOTE_MAX_CHARS, REVIEW
    assert orchestrate.TRACE_TTL_S == C.TTL_S, REVIEW
    assert [f"STEP#{s.seq}#{s.name}#A1" for s in registry._ALL_STEPS if s.name != "verify_patient"] == C.STEP_SKS, REVIEW


def test_checker_is_standard_library_only():
    src = Path(C.__file__).read_text()
    imported = set(re.findall(r"^(?:import|from) (\w+)", src, re.M))
    assert imported <= {"__future__", "hashlib", "json", "re", "sys", "collections", "datetime", "pathlib"}


# --- after Codex's 15:35 review: whitespace-tolerant embedded JSON; item status AND reason -------

WS_PLANTS = {
    "privacy_user_sub_key": {"user_sub": "x"},
    "privacy_line_index": {"idx": {"L001": "a", "L002": "b"}},
    "privacy_quote_len": {"quote": "q" * 201},
}


@needs_batch
@pytest.mark.parametrize("code", sorted(WS_PLANTS))
def test_checker_leading_whitespace_json_in_a_declared_field(example, code):
    out, b = example
    _rel(lambda o: o.update(units_json="\n  " + json.dumps(WS_PLANTS[code])))(out)
    assert code in _check(out, b)


@needs_batch
@pytest.mark.parametrize("code", sorted(WS_PLANTS))
def test_checker_leading_whitespace_json_nested_in_a_string(example, code):
    out, b = example
    _units(lambda u: u.update(nested="\t" + json.dumps({"deeper": " " + json.dumps(WS_PLANTS[code])})))(out)
    assert code in _check(out, b)


@needs_batch
def test_checker_declared_field_decoding_to_a_json_string_is_scanned(example):
    out, b = example
    _rel(lambda o: o.update(warnings_json='"\\u0055SER#x"'))(out)        # raw text has no "USER#"
    assert "privacy_user_hash" in _check(out, b)


@needs_batch
@pytest.mark.parametrize("where", ["results_units", "trace_string"])
def test_exporter_stops_on_leading_whitespace_json(ev, tmp_path, where):
    root, b = ev
    plant = "\n " + json.dumps({"x": "\n" + json.dumps({"user_sub": "x"})})
    if where == "results_units":
        b = _mut_json(root, b, "results.json", lambda o: o.update(units_json=plant))
    else:
        b = _mut_json(root, b, "trace.json", lambda o: o[0].update(note=plant))
    with pytest.raises(X.Stop) as e:
        _export(root, b, tmp_path / "ex")
    assert e.value.code == "privacy"


def _item_with(st3, cites, status, reason):
    it = _a_med(st3)
    it["cites"], it["citation_status"], it["reason"] = cites, status, reason
    _recount(st3)
    return it


def _ok_cite(st3):
    return copy.deepcopy(next(c for fld in st3["facts"]["fields"].values() for i in fld["items"]
                              for c in i["cites"] if c["verified"]))


@needs_batch
def test_trail_item_claiming_missing_cites_with_verified_cites_fails(replay):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    _item_with(st3, [_ok_cite(st3)], "citation_unverified", "missing_cites")
    _sync6(tr)
    with pytest.raises(C.TrailError) as e:
        C.trail_report(tr)
    assert e.value.code == "trail_item_status"


@needs_batch
def test_trail_item_reason_is_the_first_rejected_cites(replay):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    cites = [_ok_cite(st3), {"lines": ["L001"], "quote": "x", "verified": False, "reason": "quote_not_found"},
             {"lines": ["L999"], "quote": "y", "verified": False, "reason": "line_out_of_range"}]
    it = _item_with(st3, cites, "citation_unverified", "quote_not_found")
    _sync6(tr)
    t = C.trail_report(tr)                                         # legitimate: kept as diagnostics
    assert {"fact_id": it["fact_id"], "reason": "quote_not_found"} in t["step3_facts_to_lines"]["unverified_items"]
    assert sum(1 for u in t["step3_facts_to_lines"]["unverified_cites"] if u["fact_id"] == it["fact_id"]) == 2
    for status, reason in (("citation_unverified", "line_out_of_range"),   # not the first rejected
                           ("citation_unverified", "repaired"),            # impossible
                           ("verified", None)):                            # rejected cites present
        it["citation_status"], it["reason"] = status, reason
        _recount(st3)
        _sync6(tr)
        with pytest.raises(C.TrailError) as e:
            C.trail_report(tr)
        assert e.value.code == "trail_item_status"


@needs_batch
def test_trail_contradiction_one_side_and_missing_cites(replay):
    tr = _t(replay)
    st1, st3, s6, s7 = _steps(tr)
    c = _ok_cite(st3)
    one_line = dict(c, lines=c["lines"][:1])
    st3["facts"]["contradictions"].append({"value": "v", "cites": [one_line, copy.deepcopy(one_line)],
                                           "fact_id": "contradictions.9", "citation_status": "citation_unverified",
                                           "reason": "contradiction_one_side"})
    _recount(st3)
    _sync6(tr)
    C.trail_report(tr)                                             # as the producer would record it
    st3["facts"]["contradictions"][-1].update(citation_status="verified", reason=None)
    _recount(st3)
    _sync6(tr)
    with pytest.raises(C.TrailError) as e:
        C.trail_report(tr)
    assert e.value.code == "trail_item_status"
    st3["facts"]["contradictions"][-1].update(cites=[], citation_status="citation_unverified", reason="missing_cites")
    _recount(st3)
    _sync6(tr)
    C.trail_report(tr)


@needs_batch
def test_exporter_recheck_failure_keeps_partial_and_blocks_a_rerun(ev, tmp_path, monkeypatch):
    root, b = ev
    out = tmp_path / "ex"
    real = C.check
    monkeypatch.setattr(X.C, "check", lambda *a, **k: (["readme", "hash_audit"], real(*a, **k)[1]))
    with pytest.raises(X.Stop) as e:
        _export(root, b, out)
    assert e.value.code == "recheck"
    assert not out.exists() and out.with_name(out.name + ".partial").is_dir()
    monkeypatch.setattr(X.C, "check", real)
    with pytest.raises(X.Stop) as e:
        _export(root, b, out)
    assert e.value.code == "out_exists"


@needs_batch
def test_exporter_scans_a_declared_field_decoding_to_a_json_string(ev, tmp_path):
    root, b = ev
    b = _mut_json(root, b, "results.json", lambda o: o.update(warnings_json='"\\u0055SER#x"'))
    with pytest.raises(X.Stop) as e:
        _export(root, b, tmp_path / "ex")
    assert e.value.code == "privacy"
