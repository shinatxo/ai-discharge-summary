"""Offline calibration of step 7v `verify_patient` (ADR-010 V5, 4 Nov 2026).

"Offline" = no new patient-facing generation: the verifier runs, live, on step-7
leaflets already saved in a batch, and its verdicts are compared with labels the
CSO has ALREADY made — the step-7 rulings (`patient_rulings` in
evals/gold/adjudications/*.json, joined through score_patient.review_items) and
the blind-read findings, mapped to sentences in a file the CSO approves before
any held-out run (evals/calibration/verifier/blind_read_map.json, O4).

  python evals/calibrate_verifier.py run   <batch_dir> [--repeats N] [--yes] [--dry-run] [--held-out --frozen <sha12>]
  python evals/calibrate_verifier.py score --dev <batch_id> --held-out <batch_id> [<batch_id> ...]

`run` writes evals/calibration/verifier/<prompt_sha12>/<batch_id>/rep<k>/r<run>/<ID>.json
(never overwrites). A held-out batch runs only with --held-out and --frozen = the
verifier prompt's body sha (first 12) — the frozen, committed prompt; `score`
refuses a held-out set produced by a different prompt than the dev set.

The bar (ADR-010 V5, CSO 4 Nov 2026 14:34), written before calibration ran:
  1. missed defects 0 (held-out): every CSO-ruled defect row and every mapped
     blind-read finding gets a non-supported verdict (or, for an omission, an
     unreached item);
  2. false flags <= 25% of CSO-ruled acceptable rows (held-out);
  3. self-agreement >= 90% of sentence verdicts (supported vs not) across the
     repeats (on the dev set — O2, recommended; --agreement-on held-out to change);
  4. precision on NEW flags (sentences no label covers) reported — grouped in
     NEW_FLAGS.md for a CSO ruling; no threshold; never changes items 1-3.

How a label is matched — COUNTING RULE v3 (v2 + eligibility, audit 2), frozen before any measured run (CSO audit of
4 Nov 2026 ~15:36, which found that rule v1 — C1 as first approved — could report a
pass without the required defects being caught):
  A verifier "unit" is one non-supported item (sentence, reason, span) or one
  unreached item. A sentence may carry several units (prompt v0.2).
  - Sentence rows (cause, certainty, test_term, advice, medication, drug, number,
    unit, conflict, sensitive, promise; and blind-read sentence rows): a unit on the
    SAME sentence whose reason is in the row's family (FAMILY) and whose span is at
    the row's place — it contains the row's token (stem match), or, for a map row,
    shares a content word with the map's words. A row with no token (advice,
    medication, sensitive, promise, conflict) is sentence-wide: its family alone
    decides, and generic reasons (new_fact, other) do not count for it.
  - Qualifier rows (recorded per C section): a unit in that section with verdict
    qualifier_dropped or reason qualifier / certainty_or_completion. Never "any flag
    in the section".
  - Omission rows (diagnosis rows, blind-read unreached rows): an unreached unit in
    the same C section heading, on the A section that holds the words, whose span
    covers at least half the row's content words. Never one shared word.
  - ONE-TO-ONE: a unit is credited to at most one defect row (maximum matching).
  - A unit credited to a defect row is never also a false flag. A false flag is an
    acceptable row matched (same rule) by a unit credited to no defect row — so a
    correct cause flag is not a false positive against an acceptable medication
    ruling on the same sentence.
  - A blind-read row is not counted twice only when a CSO-ruled defect row on the
    SAME sentence (or, for an omission, the same A words) is the same fault — its
    token lies in the map's words. Never section-wide.
  - Blind-read rows marked category "policy_test" (S1's Background diagnoses under
    S6) are reported on their own line, never as historical defects.
Required for a pass, beside bar items 1-3 (stricter, added before any held-out run):
  - 5: verifier failures 0 — every leaflet of every judged set has an output; a
    missing or failed output counts its defect rows as missed AND blocks the pass;
  - 6: supported verdicts resting on an uncited source 0;
  - item 3 needs exactly PROTOCOL_REPEATS repeats, every leaflet present in each.
Leaflets today's step 7 would refuse (F1: p0.1 r5 S12, no labels) are excluded and listed.
ELIGIBILITY (audit 2): a row no verifier unit could match — decided from the saved A and C
text, never from verifier results (eligible()) — is excluded from the false-flag denominator
if acceptable, and reported; a defect row with no place stays a defect (missed) and is reported.
Rows are counted per generation, and also reported per unique key; held-out rows
whose key also occurs in dev are reported separately.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "generate"))
sys.path.insert(0, str(REPO / "evals"))

import run_cold_eval as rce  # noqa: E402
import score_batch as SB  # noqa: E402
import score_patient as SP  # noqa: E402
from pipeline import StepContext, StepError, run_step  # noqa: E402
from pipeline import compose_patient as CP7  # noqa: E402
from pipeline import meaning as MN  # noqa: E402
from pipeline import verify_patient as V  # noqa: E402

CAL_DIR = REPO / "evals" / "calibration" / "verifier"
MAP_PATH = CAL_DIR / "blind_read_map.json"
NEW_FLAG_RULINGS = CAL_DIR / "new_flag_rulings.json"
ADJ_DIR = REPO / "evals" / "gold" / "adjudications"
RUNS_DIR = rce.DEFAULT_RUNS_DIR

BAR = {"missed_max": 0, "false_flag_max": 0.25, "self_agreement_min": 0.90}
SECTION_KINDS = ("qualifier",)
UNREACHED_KINDS = ("diagnosis",)
# Per verifier call, an UPPER bound. Measured on the v0.1 pilot (4 Nov, 270 calls): output p50 1,279,
# mean 1,319, p90 1,838; input ~1.3k + ~2.3k cache read; actual $0.0335 per call. The first estimate
# (3,500 / 900 -> $0.026, printed as "<= $7.13") was exceeded by 27% ($9.06) — corrected here.
EST_TOKENS = {"input": 3600, "output": 1700}
EST_SECONDS = 12


def _norm(t: str) -> str:
    return " ".join(t.split()).casefold()


def prompt_sha12() -> str:
    return V.system_prompt()[1][:12]


# --- run ----------------------------------------------------------------------

def _step7_would_refuse(rec: dict) -> bool:
    """A leaflet today's step 7 would refuse (F1, 2 Nov: no presenting_complaint or
    diagnosis given — p0.1 r5 S12). 7v never sees such a leaflet live, and no valid
    verifier output exists for it (evidence must be a given section)."""
    try:
        CP7._check_input(CP7.patient_input(rec["steps"]["compose_clinical"]))
        return False
    except StepError:
        return True


def records(batch_dir: Path, *, excluded: list | None = None) -> list[dict]:
    out = []
    for p in sorted(batch_dir.glob("r*/*.json")):
        rec = json.loads(p.read_text(encoding="utf-8"))
        if not (rec.get("steps") or {}).get("compose_patient"):
            continue
        if _step7_would_refuse(rec):
            if excluded is not None:
                excluded.append(rec["generation_id"])
            continue
        out.append(rec)
    return out


def verify_record(rec: dict, ctx: StepContext) -> dict:
    inp7 = CP7.patient_input(rec["steps"]["compose_clinical"])
    vin = V.verify_input(inp7, rec["steps"]["compose_patient"])
    try:
        return {"verify_patient": run_step("verify_patient", vin, ctx=ctx), "error": None}
    except StepError as exc:
        return {"verify_patient": None, "error": exc.code}


class DryVerifier:
    """Every sentence supported, nothing unreached — for --dry-run and tests."""
    def __init__(self, read_timeout=None):
        self.read_timeout = read_timeout

    def converse(self, **kw):
        text = kw["messages"][0]["content"][0]["text"]
        items, cid, first_a = [], None, ""
        for line in text.splitlines():
            if line.startswith("=== "):
                cid, first_a = line.split()[1], ""
            elif line.startswith("[sections.") and not first_a:
                first_a = line[1:].split("]", 1)[0]
            elif cid and line.startswith(f"{cid}.s") and ": " in line:
                items.append({"c_section_id": cid, "sentence_id": line.split(": ", 1)[0],
                              "a_section_id": first_a, "span": "", "reason": "none", "verdict": "supported"})
        return {"output": {"message": {"role": "assistant", "content": [
                    {"toolUse": {"toolUseId": "dry", "name": V.TOOL_NAME, "input": {"items": items}}}]}},
                "stopReason": "tool_use", "usage": {"inputTokens": 0, "outputTokens": 0}, "metrics": {"latencyMs": 0}}


def cmd_run(a, *, client_factory=None, now=None, confirm=input, sleep=time.sleep, cal_dir: Path = CAL_DIR) -> int:
    batch_dir = Path(a.batch_dir)
    recs = records(batch_dir)
    if not recs:
        print(f"error: no step-7 records in {batch_dir}", file=sys.stderr)
        return 2
    sha = prompt_sha12()
    if a.held_out and a.frozen != sha:
        print(f"refused: a held-out run needs --frozen {sha} (the verifier prompt's body sha) — "
              f"freeze and commit the prompt first.", file=sys.stderr)
        return 2
    batch_id = recs[0]["batch_id"]
    out_dir = cal_dir / sha / batch_id
    n = len(recs) * a.repeats
    est = rce.cost_usd([{"input_tokens": EST_TOKENS["input"] * n, "output_tokens": EST_TOKENS["output"] * n}])
    start = (now or (lambda: dt.datetime.now(rce.LONDON)))()
    end = start + dt.timedelta(seconds=n * EST_SECONDS)
    print(f"Verifier:  prompt_sha256 {sha}… · model {rce.MODEL_ID} ({rce.REGION})")
    print(f"Batch:     {batch_id} ({len(recs)} leaflets x {a.repeats} repeats = {n} calls) · "
          f"{'HELD-OUT' if a.held_out else 'dev'}")
    print(f"Estimate:  <= ${est:.2f}; ~{n * EST_SECONDS / 60:.0f} min, finishing ~{end:%H:%M} London")
    clash = rce.canary_window_clash(start, end)
    if clash:
        print(f"refused: overlaps a canary window ({clash[0]:%a %H:%M}–{clash[1]:%H:%M} London).", file=sys.stderr)
        return 3
    if out_dir.exists() and not a.dry_run:
        print(f"refused: {rce._rel(out_dir)} already exists — never overwritten.", file=sys.stderr)
        return 2
    if not a.dry_run and not a.yes:
        if confirm(f"Run {n} live verifier calls (est. <= ${est:.2f})? Type yes: ").strip().lower() != "yes":
            print("Not run.")
            return 1
    factory = DryVerifier if a.dry_run else (client_factory or (lambda t: __import__("run_pipeline_eval").live_client(t)))
    ctx = StepContext(bedrock=factory(V.READ_TIMEOUT_S), model_id=rce.MODEL_ID, prompt_caching=True, sleep=sleep)
    total, errors = 0.0, 0
    if not a.dry_run:
        out_dir.mkdir(parents=True)
        (out_dir / "manifest.json").write_text(json.dumps({
            "batch_id": batch_id, "prompt_sha256": V.system_prompt()[1], "prompt_path": rce._rel(V.PROMPT_PATH),
            "model_id": rce.MODEL_ID, "region": rce.REGION, "repeats": a.repeats, "held_out": a.held_out,
            "created_at": start.isoformat(timespec="seconds")}, indent=2) + "\n", encoding="utf-8")
    for k in range(1, a.repeats + 1):
        for rec in recs:
            t = (now or (lambda: dt.datetime.now(rce.LONDON)))()
            if not a.dry_run and rce.canary_window_clash(t, t + dt.timedelta(seconds=EST_SECONDS)):
                print("  stopping: a canary window is about to open.")
                return 1
            res = verify_record(rec, ctx)
            vp = res["verify_patient"]
            if vp:
                total += rce.cost_usd([{"input_tokens": (vp["call"]["input_tokens"] or 0)
                                        + (vp["call"]["cache_read_tokens"] or 0) + (vp["call"]["cache_write_tokens"] or 0),
                                        "output_tokens": vp["call"]["output_tokens"] or 0}])
            else:
                errors += 1
            print(f"  rep{k} r{rec['run']} {rec['scenario_id']:<4} "
                  + (f"flagged={vp['flagged']} sentences={vp['counts']['sentences']} checks={vp['counts']['checks']}"
                     if vp else f"FAILED: {res['error']}"))
            if not a.dry_run:
                d = out_dir / f"rep{k}" / f"r{rec['run']}"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{rec['scenario_id']}.json").write_text(json.dumps(
                    {"generation_id": rec["generation_id"], **res}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nEst. cost ${total:.4f} · failures {errors}" + (" · dry run, nothing written" if a.dry_run else ""))
    return 0 if not errors else 1


# --- labels -------------------------------------------------------------------

PROTOCOL_REPEATS = 3
GENERIC = {"new_fact", "other"}
FAMILY = {
    "cause": {"cause_or_purpose"},
    "certainty": {"certainty_or_completion"},
    "test_term": {"term_changed"},
    "qualifier": {"qualifier", "certainty_or_completion"},
    "advice": {"instruction_or_promise"},
    "promise": {"instruction_or_promise"},
    "sensitive": {"withheld_category"},
    "conflict": {"withheld_category"},
    "medication": {"instruction_or_promise", "new_fact", "term_changed"},
    "drug": {"new_fact", "term_changed"},
    "number": {"new_fact", "certainty_or_completion"},
    "unit": {"new_fact"},
    "withheld": {"withheld_category"},          # blind-read dimension
}
TOKENLESS = ("advice", "medication", "sensitive", "promise", "conflict")


def rulings_for(sid: str) -> dict:
    return SB.load_adjudications(sid, ADJ_DIR, "patient_rulings")


def labels_for(rec: dict) -> list[dict]:
    """The CSO-ruled step-7 rows of one record: {key, kind, heading, sentence, token, ruling}."""
    rul = rulings_for(rec["scenario_id"])
    return [{"key": it["key"], "kind": it["kind"], "heading": it["heading"], "sentence": it["sentence"],
             "token": it["token"], "ruling": rul[it["key"]], "source": "ruling", "category": "historical"}
            for it in SP.review_items(rec) if it["key"] in rul]


def map_doc() -> dict:
    return json.loads(MAP_PATH.read_text(encoding="utf-8")) if MAP_PATH.exists() else {"findings": []}


def blind_read_map() -> list[dict]:
    return map_doc()["findings"]


def map_labels(rec: dict) -> list[dict]:
    out = []
    for i, f in enumerate(blind_read_map()):
        if f["generation_id"] != rec["generation_id"]:
            continue
        out.append(dict(f, key=f"map:{i}", kind=f["dimension"], sentence=f.get("text", ""), token=None,
                        ruling=f.get("ruling", "defect"), source="map", category=f.get("category", "historical")))
    return out


def _content(text: str) -> set[str]:
    return {MN.stem(w) for w in MN._WORD.findall(text) if w.lower() not in MN._STOP and len(w) >= 3} or MN.stems(text)


def _located(lab: dict, span: str) -> bool:
    if lab["source"] == "map":
        a, b = _norm(lab["sentence"]), _norm(span)
        return bool(b) and (a in b or b in a or bool(_content(lab["sentence"]) & _content(span)))
    if lab["kind"] in TOKENLESS or not lab.get("token"):
        return True
    tok = str(lab["token"]).replace("_", " ")
    return _norm(tok) in _norm(span) or MN.stems(tok) <= MN.stems(span)


def _typed(lab: dict, unit: dict) -> bool:
    if lab["source"] == "map":
        fam = FAMILY.get(lab["kind"])
        return unit["reason"] in fam if fam else unit["reason"] not in ("none", V.NEVER_DROP)
    if lab["kind"] == "qualifier":
        return unit["verdict"] == "qualifier_dropped" or unit["reason"] in FAMILY["qualifier"]
    fam = FAMILY.get(lab["kind"], set())
    return unit["reason"] in fam or (lab["kind"] not in TOKENLESS and unit["reason"] in GENERIC)


def _a_section_of(words: str, given: list[dict], heading: str = "diagnosis") -> str | None:
    w = _norm(words)
    for s in given:
        if w in _norm(s["text"]):
            return s["section_id"]
    return next((s["section_id"] for s in given if s["heading"] == heading), None)


def _row_matches(lab: dict, unit: dict, vin: dict, given: list[dict]) -> bool:
    """COUNTING RULE v2 — one label vs one verifier unit."""
    omission = lab.get("unit") == "unreached" or lab["kind"] == "diagnosis"
    if omission:
        if unit["kind"] != "unreached" or unit["heading"] != lab["heading"]:
            return False
        words = lab.get("a_text") or lab["sentence"].removeprefix("not reached: ")
        if unit["a_section_id"] != _a_section_of(words, given):
            return False
        want = set(MN.head_words(words)) if lab["kind"] == "diagnosis" else _content(words)
        want = want or _content(words)
        return len(want & MN.stems(unit["span"])) * 2 >= len(want)
    if unit["kind"] != "sentence" or unit["heading"] != lab["heading"]:
        return False
    if lab["kind"] == "qualifier" and lab["source"] == "ruling":
        # section-level row: a qualifier-type unit on a sentence that re-expresses the PART A
        # sentence carrying the qualifier (shares a content word with it) — never any flag in
        # the section (audit 2), and never an unrelated certainty flag (S13 "closed").
        a_sentence = lab["sentence"].split(" → C: ", 1)[0].removeprefix("input: ")
        return _typed(lab, unit) and bool(_content(a_sentence) & _content(unit["text"]))
    target = _norm(lab["sentence"])
    on_sentence = (target in _norm(unit["text"])) if lab["source"] == "map" else (_norm(unit["text"]) == target)
    return on_sentence and _typed(lab, unit) and _located(lab, unit["span"])


def eligible(lab: dict, vin: dict, given: list[dict]) -> bool:
    """Can ANY verifier output match this row? Decided from the saved A and C text only —
    never from verifier results (CSO audit 2, 4 Nov 2026). A row no unit could match is
    excluded from the false-flag denominator (acceptable) and reported; a defect row with
    no place would be uncatchable and is reported as such (it stays a defect, so it is missed)."""
    omission = lab.get("unit") == "unreached" or lab["kind"] == "diagnosis"
    secs = [s for s in vin["c_sections"] if s["heading"] == lab["heading"]]
    if omission:
        words = lab.get("a_text") or lab["sentence"].removeprefix("not reached: ")
        return bool(secs) and _a_section_of(words, given) is not None
    if lab["kind"] == "qualifier" and lab["source"] == "ruling":
        a_sentence = lab["sentence"].split(" → C: ", 1)[0].removeprefix("input: ")
        return any(_content(a_sentence) & _content(t["text"]) for s in secs for t in s["sentences"])
    target = _norm(lab["sentence"])
    return any((target in _norm(t["text"])) if lab["source"] == "map" else (_norm(t["text"]) == target)
               for s in secs for t in s["sentences"])


def _units(vp: dict) -> list[dict]:
    us = [dict(v, kind="sentence") for v in vp["verdicts"] if v["verdict"] != V.SUPPORTED]
    us += [dict(u, kind="unreached", text="", verdict=V.UNREACHED) for u in vp["unreached"]]
    return us


def _max_matching(rows: list[dict], units: list[dict], ok) -> dict[int, int]:
    """Maximum bipartite matching rows -> units (augmenting paths); {row index: unit index}."""
    adj = {i: [j for j, u in enumerate(units) if ok(r, u)] for i, r in enumerate(rows)}
    owner: dict[int, int] = {}

    def try_row(i, seen):
        for j in adj[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in owner or try_row(owner[j], seen):
                owner[j] = i
                return True
        return False
    for i in adj:
        try_row(i, set())
    return {i: j for j, i in owner.items()}


def _same_fault(m: dict, r: dict, given: list[dict]) -> bool:
    """A blind-read row duplicates a CSO-ruled defect row: same sentence (or same A words) and same fault."""
    if m.get("unit") == "unreached":
        if r["kind"] != "diagnosis":
            return False
        line = r["sentence"].removeprefix("not reached: ")
        # the WHOLE line, not its head phrase: "label — content" lines (S3's triggers) are judged
        # on their content too (the head-phrase limit is a known line-14 limitation, S7)
        return (_a_section_of(m["a_text"], given) == _a_section_of(line, given)
                and bool(_content(m["a_text"]) & _content(line)))
    if r["kind"] == "qualifier" or r["kind"] == "diagnosis" or r["heading"] != m["heading"]:
        return False
    if _norm(m["sentence"]) not in _norm(r["sentence"]):
        return False
    if r["kind"] in TOKENLESS:
        return True        # a token-less ruling covers its whole sentence: the same fault
    tok = r.get("token")
    return bool(tok) and (_norm(str(tok)) in _norm(m["sentence"]) or MN.stems(str(tok)) <= MN.stems(m["sentence"]))


def _valid_output(vp, vin) -> bool:
    if vp is None:
        return False
    a_ids = {s["section_id"] for s in vin["a_sections"]}
    return all(v.get("a_section_id") in a_ids for v in vp["verdicts"]) and \
        all(u.get("a_section_id") in a_ids for u in vp["unreached"])


def _uncited_supported(vp, vin) -> int:
    cites = {s["section_id"]: set(s["source_ids"]) for s in vin["c_sections"]}
    return sum(1 for v in vp["verdicts"] if v["verdict"] == V.SUPPORTED
               and v.get("a_section_id") not in cites.get(v["c_section_id"], set()))


def _with_text(vp: dict, vin: dict) -> dict:
    """v0.1 pilot outputs carry no sentence text: add it from the input."""
    text = {t["sentence_id"]: t["text"] for s in vin["c_sections"] for t in s["sentences"]}
    return dict(vp, verdicts=[dict(v, text=v.get("text") or text.get(v["sentence_id"], "")) for v in vp["verdicts"]])


# --- score --------------------------------------------------------------------

def load_outputs(sha_dir: Path, batch_id: str) -> dict[int, dict[str, dict]]:
    """{repeat: {generation_id: verify_patient output or None}}"""
    reps = {}
    for rd in sorted((sha_dir / batch_id).glob("rep*")):
        k = int(rd.name[3:])
        reps[k] = {}
        for p in rd.glob("r*/*.json"):
            d = json.loads(p.read_text(encoding="utf-8"))
            reps[k][d["generation_id"]] = d["verify_patient"]
    return reps


def evaluate(batch_ids: list[str], sha_dir: Path, repeat: int = 1, exclude_keys: set[str] = frozenset()) -> dict:
    rows, unlabelled, refused, ineligible_acc, ineligible_def = [], [], [], [], []
    errors = uncited = 0
    for bid in batch_ids:
        outs = load_outputs(sha_dir, bid).get(repeat, {})
        for rec in records(RUNS_DIR / bid, excluded=refused):
            given = CP7.patient_input(rec["steps"]["compose_clinical"])["sections"]
            vin = V.verify_input({"audience": "adult", "sections": given}, rec["steps"]["compose_patient"])
            ruled, mapped = labels_for(rec), map_labels(rec)
            mapped = [m for m in mapped if not any(r["ruling"] == "defect" and _same_fault(m, r, given) for r in ruled)]
            labs = []
            for lab in ruled + mapped:
                if eligible(lab, vin, given):
                    labs.append(lab)
                elif lab["ruling"] == "acceptable":
                    ineligible_acc.append(dict(lab, generation_id=rec["generation_id"]))
                else:
                    ineligible_def.append(dict(lab, generation_id=rec["generation_id"]))
                    labs.append(lab)                      # still a defect: uncatchable, so missed
            vp = outs.get(rec["generation_id"])
            ok = _valid_output(vp, vin)
            if not ok:
                errors += 1
                rows += [dict(lab, generation_id=rec["generation_id"], caught=False, error=True,
                              leaked=lab["key"] in exclude_keys) for lab in labs]
                continue
            vp = _with_text(vp, vin)
            uncited += _uncited_supported(vp, vin)
            units = _units(vp)
            match = lambda lab, u: _row_matches(lab, u, vin, given)   # noqa: E731
            defects = [l for l in labs if l["ruling"] == "defect"]
            credited = _max_matching(defects, units, match)
            used = set(credited.values())
            for i, lab in enumerate(defects):
                rows.append(dict(lab, generation_id=rec["generation_id"], caught=i in credited, error=False,
                                 leaked=lab["key"] in exclude_keys))
            for lab in (l for l in labs if l["ruling"] == "acceptable"):
                hit = any(match(lab, u) for j, u in enumerate(units) if j not in used)
                rows.append(dict(lab, generation_id=rec["generation_id"], caught=hit, error=False,
                                 leaked=lab["key"] in exclude_keys))
            covered = {j for j, u in enumerate(units) if j in used or any(match(l, u) for l in labs)}
            for j, u in enumerate(units):
                if j not in covered:
                    key = hashlib.sha256(f"verifier|{u['heading']}|{u['kind']}|{_norm(u['text'] or u['span'])}|"
                                         f"{u['reason']}".encode()).hexdigest()[:16]
                    unlabelled.append({"generation_id": rec["generation_id"], "heading": u["heading"],
                                       "sentence": u["text"] or f"not reached: {u['span']}", "verdict": u["verdict"],
                                       "reason": u["reason"], "span": u["span"], "key": key})
    hist = [r for r in rows if r["category"] == "historical"]
    defects = [r for r in hist if r["ruling"] == "defect"]
    accept = [r for r in hist if r["ruling"] == "acceptable"]
    missed = [r for r in defects if not r["caught"]]
    false = [r for r in accept if r["caught"]]
    policy = [r for r in rows if r["category"] == "policy_test"]
    return {"rows": rows, "defects": len(defects), "missed": missed, "acceptable": len(accept),
            "false_flags": len(false), "false_rate": (len(false) / len(accept)) if accept else None,
            "errors": errors, "uncited_supported": uncited, "unlabelled": unlabelled,
            "policy": {"rows": len(policy), "caught": sum(1 for r in policy if r["caught"])},
            "excluded_refused_by_step7": refused,
            "acceptable_no_opportunity": len([r for r in ineligible_acc if r["category"] == "historical"]),
            "acceptable_no_opportunity_by_kind": {k: sum(1 for r in ineligible_acc if r["kind"] == k)
                                                  for k in sorted({r["kind"] for r in ineligible_acc})},
            "defects_no_place": [{"generation_id": r["generation_id"], "kind": r["kind"], "key": r["key"]}
                                 for r in ineligible_def],
            "missed_unique": sorted({r["key"] for r in missed}),
            "missed_not_leaked": [r for r in missed if not r.get("leaked")]}


def self_agreement(batch_id: str, sha_dir: Path) -> float | None:
    """Sentences whose verdict (acceptable vs not) is the same in all PROTOCOL_REPEATS repeats.
    None — and so NOT MET — unless exactly that many repeats exist and every leaflet has a
    valid output in each."""
    reps = load_outputs(sha_dir, batch_id)
    if sorted(reps) != list(range(1, PROTOCOL_REPEATS + 1)):
        return None
    recs = records(RUNS_DIR / batch_id)
    same = total = 0
    for rec in recs:
        given = CP7.patient_input(rec["steps"]["compose_clinical"])["sections"]
        vin = V.verify_input({"audience": "adult", "sections": given}, rec["steps"]["compose_patient"])
        outs = [reps[k].get(rec["generation_id"]) for k in sorted(reps)]
        if not all(_valid_output(o, vin) for o in outs):
            return None
        per = []
        for o in outs:
            ok: dict[str, bool] = {}
            for v in o["verdicts"]:
                ok[v["sentence_id"]] = ok.get(v["sentence_id"], True) and V.acceptable(
                    dict(v, evidence_valid=v.get("a_section_id") in next(
                        set(s["source_ids"]) for s in vin["c_sections"] if s["section_id"] == v["c_section_id"])))
            per.append(ok)
        for sid in per[0]:
            total += 1
            same += all(p.get(sid) == per[0][sid] for p in per)
    return same / total if total else None


def cmd_score(a, *, sha_dir: Path | None = None) -> int:
    sha_dir = sha_dir or (CAL_DIR / (a.sha or prompt_sha12()))
    shas = {json.loads((sha_dir / b / "manifest.json").read_text())["prompt_sha256"][:12]
            for b in [a.dev, *a.held_out] if (sha_dir / b / "manifest.json").exists()}
    if len(shas) > 1:
        print("refused: dev and held-out were produced by different verifier prompts.", file=sys.stderr)
        return 2
    dev = evaluate([a.dev], sha_dir)
    dev_keys = {r["key"] for r in dev["rows"] if r.get("key")}
    held = evaluate(a.held_out, sha_dir, exclude_keys=dev_keys) if a.held_out else None
    agree_on = a.dev if a.agreement_on == "dev" else (a.held_out[0] if a.held_out else a.dev)
    agree = self_agreement(agree_on, sha_dir)
    judged = held or dev
    errs = judged["errors"] + (dev["errors"] if held else 0)
    verdict = {
        "1 missed defects": (len(judged["missed"]), len(judged["missed"]) <= BAR["missed_max"]),
        "2 false-flag rate": (judged["false_rate"], judged["false_rate"] is not None
                              and judged["false_rate"] <= BAR["false_flag_max"]),
        "3 self-agreement": (agree, agree is not None and agree >= BAR["self_agreement_min"]),
        "5 verifier failures": (errs, errs == 0),
        "6 supported on an uncited source": (judged["uncited_supported"], judged["uncited_supported"] == 0),
    }
    rulings = json.loads(NEW_FLAG_RULINGS.read_text()) if NEW_FLAG_RULINGS.exists() else {}
    new = judged["unlabelled"]
    ruled = [u for u in new if u["key"] in rulings]
    prec = (sum(1 for u in ruled if rulings[u["key"]] == "defect") / len(ruled)) if ruled else None
    lines = [f"# Step 7v calibration — prompt `{sha_dir.name}` (counting rule v3)", "",
             f"Dev: `{a.dev}` · held-out: {', '.join(f'`{b}`' for b in a.held_out) or '(none yet)'} · "
             f"judged on: {'held-out' if held else '**dev only — not the bar**'}", "",
             "| Bar item | Value | Bar | Verdict |", "|---|---|---|---|"]
    fmt = {"1 missed defects": lambda v: f"{v} of {judged['defects']}",
           "2 false-flag rate": lambda v: "—" if v is None else f"{v:.1%} ({judged['false_flags']} of {judged['acceptable']})",
           "3 self-agreement": lambda v: "— (needs 3 complete repeats)" if v is None else f"{v:.1%} (on {agree_on})",
           "5 verifier failures": lambda v: str(v), "6 supported on an uncited source": lambda v: str(v)}
    bars = {"1 missed defects": "0", "2 false-flag rate": "≤ 25%", "3 self-agreement": "≥ 90%",
            "5 verifier failures": "0", "6 supported on an uncited source": "0"}
    for k, (val, ok) in verdict.items():
        lines.append(f"| {k} | {fmt[k](val)} | {bars[k]} | {'MET' if ok else '**NOT MET**'} |")
    lines.append(f"| 4 new-flag precision | {'—' if prec is None else f'{prec:.0%}'} ({len(ruled)} of {len(new)} ruled) | report | — |")
    pol = judged["policy"] if not held else dev["policy"]
    lines += ["", f"S6 policy tests (prospective, not historical defects; dev): {pol['caught']} of {pol['rows']} caught.",
              f"Excluded — leaflets today's step 7 would refuse (F1): "
              f"{', '.join(judged['excluded_refused_by_step7']) or 'none'}.",
              f"Acceptable rows with no opportunity to be flagged (from the saved A/C text; outside the "
              f"false-flag denominator): {judged['acceptable_no_opportunity']} "
              f"{judged['acceptable_no_opportunity_by_kind'] or ''}.",
              f"Defect rows with no place (uncatchable — counted missed): {len(judged['defects_no_place'])}.",
              f"Missed (unique keys): {len(judged['missed_unique'])}; missed excluding keys also in dev: "
              f"{len(judged['missed_not_leaked'])}.", "", "## Missed defects", ""]
    lines += [f"- {r['generation_id']} · {r['kind']} · {r['heading']} · {r['source']}"
              + (" · NO OUTPUT" if r.get("error") else "") for r in judged["missed"]] or ["- none"]
    out = sha_dir / "scores"
    out.mkdir(exist_ok=True)
    (out / "CALIBRATION.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    groups: dict = {}
    for u in new:
        groups.setdefault(u["key"], []).append(u)
    nf = ["# New verifier flags — for a CSO ruling (bar item 4; never changes items 1–3)", "",
          "Rule each key `acceptable` (the flag was wrong) or `defect` in "
          f"`{rce._rel(NEW_FLAG_RULINGS)}`.", ""]
    for key, us in sorted(groups.items(), key=lambda kv: (kv[1][0]["generation_id"], kv[1][0]["heading"])):
        u = us[0]
        nf.append(f"- `{key}` ×{len(us)} · {u['heading']} · {u['verdict']}/{u['reason']} · "
                  f"\"{u['sentence']}\" — span \"{u['span']}\"" + (f" — ruled {rulings[key]}" if key in rulings else ""))
    (out / "NEW_FLAGS.md").write_text("\n".join(nf) + "\n", encoding="utf-8")
    (out / "calibration.json").write_text(json.dumps({
        "prompt_sha12": sha_dir.name, "counting_rule": 3, "dev": a.dev, "held_out": a.held_out,
        "bar": BAR, "verdict": {k: {"value": v, "met": ok} for k, (v, ok) in verdict.items()},
        "new_flag_precision": prec, "policy_tests": pol,
        "missed": [{k: r.get(k) for k in ("generation_id", "kind", "heading", "source", "key", "error")}
                   for r in judged["missed"]]}, indent=2, default=str) + "\n", encoding="utf-8")
    print("\n".join(lines[:14]))
    return 0 if all(ok for _v, ok in verdict.values()) else 1


def parse_args(argv):
    p = argparse.ArgumentParser(description="Offline calibration of step 7v (ADR-010 V5).")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("batch_dir")
    r.add_argument("--repeats", type=int, default=1)
    r.add_argument("--held-out", action="store_true")
    r.add_argument("--frozen", default=None)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--yes", action="store_true")
    s = sub.add_parser("score")
    s.add_argument("--dev", required=True)
    s.add_argument("--held-out", nargs="*", default=[])
    s.add_argument("--agreement-on", choices=("dev", "held-out"), default="dev")
    s.add_argument("--sha", default=None)
    a = p.parse_args(argv)
    if a.cmd == "run" and a.repeats < 1:
        p.error("--repeats must be at least 1")
    return a


def main(argv=None, **kw) -> int:
    a = parse_args(argv)
    if a.cmd == "run":
        return cmd_run(a, **kw)
    return cmd_score(a, **{k: v for k, v in kw.items() if k == "sha_dir"})


if __name__ == "__main__":
    sys.exit(main())
