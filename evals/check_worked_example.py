"""Offline checker for the committed worked example (WS1a step build piece 8, DoD 7; 9 Nov 2026).

    python evals/check_worked_example.py [DIR]        # default: the committed S1 example

Standard library only. No AWS, no network, no model, no pipeline import. Reads only the example
folder and src/canary/scenarios.json. Prints fixed check codes and counts — never note,
document or model-output text. Exit 0 = MET (every check passed), 1 = NOT MET (failed codes
listed), 2 = usage error.

WHAT IT VERIFIES: offline CONSISTENCY of the example, its BINDING to one fixed saved run, and
the privacy rules. It does NOT prove origin — coordinated edits to content, hashes and
provenance can stay consistent. Origin rests on the export record: the source files' byte
hashes, taken from the operator's saved evidence by a read-only probe run before export and
pinned below in BINDING (independently of the exporter), with trace.json a byte-identical copy.
Structural consistency does not establish faithful clinical meaning.

FROZEN RULES. The trail rules below are a copy of the producer's rules AS THEY WERE in the
source run's pipeline (RULES_PIPELINE_VERSION). They are not imported, so a later pipeline
change cannot silently rewrite the historical example's rules. tests/test_worked_example.py
compares this copy with the current producer: a difference fails that test and prompts a
review — it never updates the rules here automatically.

Files (exactly these): README.md, trace.json (byte copy of the saved trace), audit.json (allow-
list projection of the saved audit row), release.json (allow-list projection of the saved
ResultsTable item; JSON-string fields unchanged), trail.json (the trail report, recomputed and
compared here), provenance.json.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CORPUS = ROOT / "src" / "canary" / "scenarios.json"
SCHEMA = "w6-worked-example/1"

# --- the binding: one fixed saved run (piece 7), independent of the exporter -----------------
# Source byte hashes: Shina's read-only probe run, 9 Nov 2026 14:22 (docs/_local/p8-worked/
# probe_evidence.py v2.1) over ~/eph-20261107/evidence/<job>/snap-1/.
BINDING = {
    "scenario": "S1",
    "job_id": "01M6V35PH1GQBC07TSQNB7PVKS",
    "snapshot": "snap-1",
    "deployed_commit": "b57f4bce75abe0075d2dc4daf51a2bc72c3c9446",
    "pipeline_version": "d3df2826d8bbbdac21f62bec560889fb28bc4ce05829ba4b5bfb6d6ed940cad1",
    "stack_name": "discharge-eph-20261107",
    "notes_sha256": "deacfb8fe45187fb890a33b6e0c8ad2f5e6902c825879bb20931a69f80969fa3",
    "source_sha256": {
        "trace.json": "046b52dfcdd54aef990fcec00376ba2f5d2d75187ce319401089039d99703a39",
        "audit.json": "36a6623cb73b7d13584298ef858d9716a28e13151a4b14e0fe2273e6dea34ca7",
        "results.json": "99e08e066b02f519c9016e489a97ccbfabdc3d75a0fae02bd0a30acee999d2ac",
        "check.json": "21af29b89180c2164a6c4fad02a021cf41e6f11f1f23c1bebbe58f4d0dd40165",
        "_snapshot.json": "b597a5d2c2caecbd80e4c6990843d21c7edf27f947a92f0de93f9530e299771f",
    },
}
DEFAULT_DIR = ROOT / "evals" / "runs" / f"w6-worked-example-{BINDING['scenario']}-{BINDING['job_id']}"
RULES_PIPELINE_VERSION = BINDING["pipeline_version"]

FILES = {"README.md", "trace.json", "audit.json", "release.json", "trail.json", "provenance.json"}
# The saved audit row minus PK, SK, user_sub, idempotency_key (CSO D2, 9 Nov 2026): 19 fields.
AUDIT_FIELDS = {"completed_at", "draft", "generation_id", "inference_profile", "input_sha256",
                "input_tokens", "model_calls", "model_version", "output_sha256", "output_tokens",
                "output_type", "parse_ok", "pipeline_version", "request_region", "reviewed_at",
                "safety_net_gate", "schema_version", "started_at", "status"}
AUDIT_DROPPED = {"PK", "SK", "user_sub", "idempotency_key"}
# The saved ResultsTable item minus PK, SK, user_sub, ttl (CSO D2): 11 fields.
RELEASE_FIELDS = {"completed_at", "generated_on", "gp_letter", "job_id", "output_sha256_json",
                  "patient", "pipeline_version", "summary", "templates_json", "units_json",
                  "warnings_json"}
RELEASE_DROPPED = {"PK", "SK", "user_sub", "ttl"}
JSON_FIELDS = ("output_sha256_json", "templates_json", "units_json", "warnings_json")
DOCS = (("summary", "part_a"), ("gp_letter", "part_b"), ("patient", "part_c"))
STEP_SKS = ["STEP#01#guard_input#A1", "STEP#02#extract_facts#A1", "STEP#03#validate_facts#A1",
            "STEP#03b#retrieve_evidence#A1", "STEP#04#route_resus#A1",
            "STEP#05a#reconcile_medications#A1", "STEP#05b#select_safety_net#A1",
            "STEP#06#compose_clinical#A1", "STEP#07#compose_patient#A1",
            "STEP#08a#assemble_clinical#A1", "STEP#08b#assemble_patient#A1"]
EXPECTED_SKS = sorted(["DELIVERY", "META", *STEP_SKS], key=str.encode)
TTL_S = 30 * 86400
# p7.py check_attempt, a completed scenario not last in the run order: 17 criteria.
RECEIPT_CRITERIA = {"audit_complete", "results_item", "pipeline_version_audit", "pipeline_version_meta",
                    "no_failed_step_or_code", "status_endpoint_complete", "items_13_expected_sks",
                    "one_page", "sk_byte_order", "overlap_5a_6", "e2e_le_120s",
                    "not_twelve_item_release_shape", "log_evidence_complete", "privacy_zero_hits",
                    "privacy_controls_fire", "privacy_groups_nonempty", "admission_next_ok"}
RECEIPT_COVERAGE = {"S1": {"gate_documented_advice", "resus_box_shown"},
                    "B6": {"gate_documented_advice", "contradictions_block"}}
REQUIRED_README = (
    "This is a synthetic example.",
    "It is not clinical validation",
    "PART C is a draft flagged for clinician review: WS1a checklist item 3 (compose_patient) is NOT MET.",
    "Structural consistency does not establish faithful clinical meaning.",
    "The checker verifies offline consistency, binding to this run and the privacy rules; it does not prove origin.",
)
README_PENDING = "PENDING-CSO-READ"

# --- frozen producer rules (pipeline d3df2826…cad1) -------------------------------------------
QUOTE_MAX_CHARS = 200                                   # validate.QUOTE_MAX_CHARS
CITE_MAX_LINES = 3                                      # validate.CITE_MAX_LINES
VERIFIED_LINE_ID = re.compile(r"L\d{3}")                # validate._LINE_ID (^L\d{3}$), fullmatch
CITE_REASONS = {"missing_lines", "malformed_line_id", "line_out_of_range", "cite_too_many_lines",
                "non_consecutive_lines", "quote_too_long", "empty_quote", "quote_not_found"}
ITEM_REASONS = CITE_REASONS | {"missing_cites", "contradiction_one_side"}   # validate._check_item
STEP6_BOXES = {"medications", "resuscitation_status", "safety_net"}         # compose.BOXES
STEP6_EXCLUDED_IDS = ("resus",)                                             # compose.EXCLUDED_IDS
STEP6_EXCLUDED_PREFIXES = ("medications.", "documented_advice.", "suspicious_text.")
STEP6_RECORDED_FID = re.compile(r"[a-z_]+\.\d+|resus")  # compose.check_composition, fullmatch
STEP7_BOXES = {"medicines", "safety_net"}                                    # compose_patient.BOXES
STEP7_RECORDED_SID = re.compile(r"sections\.\d+")       # compose_patient._SECTION_ID, fullmatch
STEP6_FLAGS = ("excluded_fact_id", "unknown_fact_id")
STEP7_FLAGS = ("unknown_source_id",)

# --- privacy detectors (walk unescaped strings; tests carry a positive control for each) -------
UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
ACCT12 = re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")   # an account ID as in an ARN; not inside hex
RAW_LINE_KEY = re.compile(r"L\d{3,}")                   # orchestrate._LINE_ID (raw index keys)
NOTES_RUN = QUOTE_MAX_CHARS + 1                         # a notes run longer than any allowed quote


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def corpus_notes(path: Path = CORPUS) -> dict[str, str]:
    """Stripped, as the dispatcher strips before hashing (evals/run_cold_eval.load_corpus)."""
    return {s["id"]: (s.get("notes") or "").strip() for s in json.loads(path.read_bytes())["scenarios"]}


def _epoch(s: str) -> float:
    s = s.replace("Z", "+00:00")
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)   # 3.10-safe
    return datetime.fromisoformat(s).timestamp()


# --- decoding + privacy scan --------------------------------------------------------------------

def decoded_views(obj, where: str, out: list):
    """(where, object) for obj and for every JSON object/array embedded in one of its strings,
    recursively (nested strings included). Leading/trailing whitespace is tolerated, as by
    json.loads (Codex review 15:35). The strings themselves are never changed."""
    out.append((where, obj))

    def walk(o, path):
        if isinstance(o, dict):
            for k, v in o.items():
                walk(v, f"{path}.{k}")
        elif isinstance(o, list):
            for i, v in enumerate(o):
                walk(v, f"{path}[{i}]")
        elif isinstance(o, str) and o.lstrip()[:1] in ("{", "["):
            try:
                inner = json.loads(o)
            except ValueError:
                return
            if isinstance(inner, (dict, list)):
                decoded_views(inner, f"{path}<json>", out)
    walk(obj, where)
    return out


def collect_views(objs: dict) -> list:
    """Every view the privacy scan reads, for both the exporter and the checker: each file's
    object with its embedded JSON (decoded_views), plus each DECLARED JSON-string field of the
    release, decoded whatever its shape (a JSON string literal included) and scanned as its own
    view. A declared field that does not decode is skipped here — `json_fields` fails it."""
    views: list = []
    for name, obj in objs.items():
        decoded_views(obj, name, views)
    rel = objs.get("release.json")
    if isinstance(rel, dict):
        for k in JSON_FIELDS:
            if isinstance(rel.get(k), str):
                try:
                    decoded_views(json.loads(rel[k]), f"release.json.{k}<declared>", views)
                except ValueError:
                    pass
    return views


def strings_of(o, out: list) -> list:
    if isinstance(o, dict):
        for k, v in o.items():
            out.append(k)
            strings_of(v, out)
    elif isinstance(o, list):
        for v in o:
            strings_of(v, out)
    elif isinstance(o, str):
        out.append(o)
    return out


def _structure_hits(o, c: Counter) -> Counter:
    if isinstance(o, dict):
        if o and all(isinstance(k, str) and RAW_LINE_KEY.fullmatch(k) and isinstance(v, str) for k, v in o.items()):
            c["privacy_line_index"] += 1
        for k, v in o.items():
            if k == "user_sub":
                c["privacy_user_sub_key"] += 1
            if "idempot" in k.lower():
                c["privacy_idempotency_key"] += 1
            if k == "notes" and isinstance(v, str):
                c["privacy_notes_key"] += 1
            if k == "quote" and isinstance(v, str) and len(v) > QUOTE_MAX_CHARS:
                c["privacy_quote_len"] += 1
            _structure_hits(v, c)
    elif isinstance(o, list):
        for v in o:
            _structure_hits(v, c)
    return c


def _norm(s: str) -> str:
    return " ".join(s.split())


def privacy_scan(views: list, notes: str, extra_text: list[str] = ()) -> Counter:
    """Counts per detector over every decoded view (and extra plain texts, e.g. the README)."""
    c = Counter()
    n = _norm(notes)
    runs = {n[i:i + NOTES_RUN] for i in range(0, max(len(n) - NOTES_RUN + 1, 0))}
    strs = list(extra_text)
    for _, o in views:
        _structure_hits(o, c)
        strings_of(o, strs)
    for s in strs:
        if "USER#" in s:
            c["privacy_user_hash"] += 1
        if UUID.search(s):
            c["privacy_uuid"] += 1
        if "arn:" in s:
            c["privacy_arn"] += 1
        if ACCT12.search(s):
            c["privacy_acct12"] += 1
        t = _norm(s)
        if t and t == n:
            c["privacy_whole_notes"] += 1
        if len(t) >= NOTES_RUN and any(t[i:i + NOTES_RUN] in runs for i in range(len(t) - NOTES_RUN + 1)):
            c["privacy_notes_run"] += 1
    return c


# --- the trail report (three paths; reports the producer's own diagnostics) --------------------

class TrailError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _by_sk(trace: list) -> dict:
    return {i.get("SK"): i for i in trace}


def _step3_items(facts: dict):
    """(fact_id, item, kind) for every item step 3 checked with _check_item."""
    for name, field in facts["fields"].items():
        for it in field["items"]:
            yield it["fact_id"], it, "field"
    for group in ("pre_admission", "discharge"):
        for it in facts["medications"][group]:
            yield it["fact_id"], it, "medication"
    for it in facts["documented_advice"]:
        yield it["fact_id"], it, "documented_advice"
    for it in facts["contradictions"]:
        yield it["fact_id"], it, "contradiction"
    yield "resus", facts["resus"], "resus"


def _cite_valid_as_verified(c: dict, line_set: set) -> bool:
    ids = c.get("lines")
    q = c.get("quote")
    if not isinstance(ids, list) or not ids or c.get("reason") is not None:
        return False
    if not all(isinstance(i, str) and VERIFIED_LINE_ID.fullmatch(i) and i in line_set for i in ids):
        return False
    if len(ids) > CITE_MAX_LINES:
        return False
    nums = [int(i[1:]) for i in ids]
    if any(b != a + 1 for a, b in zip(nums, nums[1:])):
        return False
    return isinstance(q, str) and 0 < len(q) <= QUOTE_MAX_CHARS and bool(q.split())


def _expected_item_outcome(cites: list, kind: str) -> tuple:
    """(citation_status, reason) as validate._check_item derives them from the RECORDED
    citations (frozen copy): no cites -> missing_cites; any rejected cite -> the FIRST rejected
    cite's reason; a contradiction whose verified cites touch < 2 distinct lines ->
    contradiction_one_side; else verified / None. Quote text is not re-checked."""
    good = [c for c in cites if c.get("verified") is True]
    if not cites:
        reason = "missing_cites"
    elif len(good) < len(cites):
        reason = next(c.get("reason") for c in cites if c.get("verified") is not True)
    elif kind == "contradiction" and len({i for c in good for i in c["lines"]}) < 2:
        reason = "contradiction_one_side"
    else:
        reason = None
    return ("verified" if reason is None else "citation_unverified"), reason


def _resus_claims(r: dict) -> bool:
    return bool(r.get("form_or_discussion_documented") or r.get("status_documented") != "not_documented"
                or r.get("changed") != "not_documented")


def trail_report(trace: list) -> dict:
    """Recompute the trail from the trace. Raises TrailError(code) on a structural break;
    returns IDs, reasons and counts only (no text). Rejected citations and flagged references
    are DIAGNOSTICS, reported as the producer saved them — never repaired."""
    s = _by_sk(trace)
    try:
        st1 = s["STEP#01#guard_input#A1"]["output"]
        st3 = s["STEP#03#validate_facts#A1"]["output"]
        in6 = s["STEP#06#compose_clinical#A1"]["input"]
        st6 = s["STEP#06#compose_clinical#A1"]["output"]
        in7 = s["STEP#07#compose_patient#A1"]["input"]
        st7 = s["STEP#07#compose_patient#A1"]["output"]
        meta = s["META"]
    except (KeyError, TypeError):
        raise TrailError("trail_shape") from None
    try:
        return _trail(st1, st3, in6, st6, in7, st7, meta)
    except TrailError:
        raise
    except (KeyError, TypeError, AttributeError, ValueError):
        raise TrailError("trail_shape") from None


def _trail(st1, st3, in6, st6, in7, st7, meta) -> dict:
    # path 0 — the line set, from step 01's saved output.lines.line_ids, kept as saved
    line_ids = st1["lines"]["line_ids"]
    withheld = sum(1 for i in line_ids if i == "[withheld]")
    line_set = {i for i in line_ids if isinstance(i, str) and i != "[withheld]"}
    if len(line_ids) != st1["line_count"] or st1["line_count"] != meta["line_count"]:
        raise TrailError("trail_line_count")

    # path 1 — facts -> note lines (step 3)
    facts = st3["facts"]
    cites_total = cites_verified = items_unverified = 0
    unverified_cites, unverified_items, n_items = [], [], 0
    statuses = {}
    for fid, it, kind in _step3_items(facts):
        cites = it["cites"]
        checked = kind != "resus" or _resus_claims(it) or bool(cites)
        status, reason = it["citation_status"], it["reason"]
        statuses[fid] = status
        if not checked:                                  # resus, nothing claimed: nothing to verify
            if cites or status != "verified" or reason is not None:
                raise TrailError("trail_item_status")
            continue
        n_items += 1
        for c in cites:
            cites_total += 1
            if c.get("verified") is True:
                if not _cite_valid_as_verified(c, line_set):
                    raise TrailError("trail_verified_invalid")
                cites_verified += 1
            elif c.get("verified") is False:
                if c.get("reason") not in CITE_REASONS:
                    raise TrailError("trail_reason_unknown")
                unverified_cites.append({"fact_id": fid, "lines": list(c.get("lines") or []), "reason": c["reason"]})
            else:
                raise TrailError("trail_shape")
        if (status, reason) != _expected_item_outcome(cites, kind):
            raise TrailError("trail_item_status")
        if status == "citation_unverified":
            items_unverified += 1
            unverified_items.append({"fact_id": fid, "reason": reason})
    for c in facts["suspicious_text"]:
        cites_total += 1
        if c.get("verified") is True:
            if not _cite_valid_as_verified(c, line_set):
                raise TrailError("trail_verified_invalid")
            cites_verified += 1
        elif c.get("verified") is False:
            if c.get("reason") not in CITE_REASONS:
                raise TrailError("trail_reason_unknown")
            unverified_cites.append({"fact_id": c.get("fact_id"), "lines": list(c.get("lines") or []), "reason": c["reason"]})
        else:
            raise TrailError("trail_shape")
    rc = st3["counts"]
    if (rc["cites_total"], rc["cites_verified"], rc["items_unverified"]) != (cites_total, cites_verified, items_unverified):
        raise TrailError("trail_counts")

    # path 2 — PART A/B sections -> facts (step 6), against step 6's RECORDED input
    if in6.get("facts") != facts:
        raise TrailError("trail_step6_input")
    f6 = in6["facts"]
    field_ids = {it["fact_id"] for fld in f6["fields"].values() for it in fld["items"]}
    contra_ids = {it["fact_id"] for it in f6["contradictions"]}
    held = [h["fact_id"] for h in st6["held_back"]]
    if not set(held) <= field_ids or len(set(held)) != len(held):
        raise TrailError("trail_held")
    held_set = set(held)
    given = (field_ids - held_set) | contra_ids
    unverified6 = {f for f in given if statuses.get(f) != "verified"}
    expected6, links6, boxes6, empty6, unv_links = Counter(), 0, [], [], []
    for sec in st6["sections"]:
        part, heading, ids = sec["part"], sec["heading"], sec["fact_ids"]
        if not all(isinstance(f, str) for f in ids):
            raise TrailError("trail_shape")
        if heading in STEP6_BOXES:
            if ids:
                raise TrailError("trail_box_6")
            boxes6.append({"part": part, "heading": heading})
        elif not ids:
            empty6.append({"part": part, "heading": heading})
        for f in ids:
            if f in STEP6_EXCLUDED_IDS or f.startswith(STEP6_EXCLUDED_PREFIXES) or f in held_set:
                expected6[("excluded_fact_id", part, heading, f)] += 1
            elif f not in given:
                expected6[("unknown_fact_id", part, heading, f if STEP6_RECORDED_FID.fullmatch(f) else None)] += 1
            else:
                links6 += 1
        want_unv = [f for f in ids if f in unverified6]
        if sec["unverified_fact_ids"] != want_unv:
            raise TrailError("trail_unverified_6")
        unv_links += [{"section_id": sec["section_id"], "fact_id": f} for f in want_unv]
    recorded6 = Counter((c["check"], c["part"], c["heading"], c["fact_id"]) for c in st6["checks"]
                        if c["check"] in STEP6_FLAGS)
    if recorded6 != expected6:
        raise TrailError("trail_flags_6")

    # path 3 — PART C sections -> PART A sections (step 7), against step 7's RECORDED input
    a_secs = {x["section_id"]: x for x in st6["sections"] if x["part"] == "part_a"}
    for x in in7["sections"]:
        a = a_secs.get(x["section_id"])
        if a is None or a["heading"] != x["heading"] or a["text"] != x["text"]:
            raise TrailError("trail_step7_input")
    given7 = {x["section_id"] for x in in7["sections"]}
    expected7, links7, boxes7, empty7 = Counter(), 0, [], []
    for sec in st7["sections"]:
        heading, ids = sec["heading"], sec["source_ids"]
        if not all(isinstance(i, str) for i in ids):
            raise TrailError("trail_shape")
        if heading in STEP7_BOXES:
            if ids:
                raise TrailError("trail_box_7")
            boxes7.append({"heading": heading})
        elif not ids:
            empty7.append({"heading": heading})
        for i in ids:
            if i not in given7:
                expected7[("unknown_source_id", heading, i if STEP7_RECORDED_SID.fullmatch(i) else None)] += 1
            else:
                links7 += 1
    recorded7 = Counter((c["check"], c["heading"], c["source_id"]) for c in st7["checks"] if c["check"] in STEP7_FLAGS)
    if recorded7 != expected7:
        raise TrailError("trail_flags_7")

    def flags(counter, keys):
        return [dict(zip(keys, k), occurrences=n) for k, n in sorted(counter.items(), key=lambda kv: json.dumps(kv[0]))]

    return {
        "rules_pipeline_version": RULES_PIPELINE_VERSION,
        "lines": {"line_ids": len(line_ids), "withheld_markers": withheld},
        "step3_facts_to_lines": {
            "items_checked": n_items, "cites_total": cites_total, "cites_verified": cites_verified,
            "items_unverified": items_unverified, "suspicious_text_cites": len(facts["suspicious_text"]),
            "unverified_cites": unverified_cites, "unverified_items": unverified_items},
        "step6_sections_to_facts": {
            "sections": len(st6["sections"]), "facts_given": len(given), "held_back": held,
            "links_resolved": links6, "flags": flags(expected6, ("check", "part", "heading", "fact_id")),
            "boxes": boxes6, "empty_non_box": empty6, "unverified_links": unv_links},
        "step7_sections_to_a_sections": {
            "sections": len(st7["sections"]), "a_sections_given": len(given7), "links_resolved": links7,
            "flags": flags(expected7, ("check", "heading", "source_id")), "boxes": boxes7, "empty_non_box": empty7},
    }


# --- the checker ---------------------------------------------------------------------------------

def check(d: Path, binding: dict = BINDING, rules_pv: str = RULES_PIPELINE_VERSION,
          corpus_path: Path = CORPUS) -> tuple[list[str], dict]:
    """(failed codes, counts). [] = MET."""
    fails: list[str] = []
    info: dict = {}

    def f(code: str, ok) -> bool:
        if ok is not True:
            fails.append(code)
        return ok is True

    present = {p.name for p in d.iterdir()} if d.is_dir() else set()
    if not f("files_set", present == FILES):
        return fails, {"present": sorted(present)}
    raw = {n: (d / n).read_bytes() for n in FILES}
    try:
        trace = json.loads(raw["trace.json"])
        audit = json.loads(raw["audit.json"])
        rel = json.loads(raw["release.json"])
        trail_saved = json.loads(raw["trail.json"])
        prov = json.loads(raw["provenance.json"])
        readme = raw["README.md"].decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        fails.append("json_parse")
        return fails, info
    if not (isinstance(trace, list) and all(isinstance(i, dict) for i in trace) and isinstance(audit, dict)
            and isinstance(rel, dict) and isinstance(prov, dict) and isinstance(trail_saved, dict)):
        fails.append("json_shape")
        return fails, info
    job = binding["job_id"]
    s = _by_sk(trace)
    meta = s.get("META") or {}

    # binding + provenance (independent anchor: BINDING's source hashes)
    pb = prov.get("binding") or {}
    f("schema", prov.get("schema") == SCHEMA)
    f("binding", {k: pb.get(k) for k in binding if k != "source_sha256"} == {k: v for k, v in binding.items() if k != "source_sha256"})
    src = (prov.get("source") or {}).get("files") or {}
    f("source_hashes", {n: (src.get(n) or {}).get("sha256") for n in binding["source_sha256"]} == binding["source_sha256"])
    f("trace_bytes", sha256_bytes(raw["trace.json"]) == binding["source_sha256"]["trace.json"])
    exp = (prov.get("export") or {}).get("files") or {}
    f("export_hashes", set(exp) == {"trace.json", "audit.json", "release.json", "trail.json"}
      and all((exp.get(n) or {}).get("sha256") == sha256_bytes(raw[n]) for n in exp))

    # trace structure
    sks = [i.get("SK") for i in trace]
    f("trace_sks", sks == EXPECTED_SKS)
    f("trace_pk", all(i.get("PK") == f"GEN#{job}" for i in trace))
    try:
        ttl_ok = {i.get("ttl") for i in trace} == {int(_epoch(meta["started_at"])) + TTL_S}
    except (KeyError, TypeError, ValueError):
        ttl_ok = False
    f("trace_ttl", ttl_ok)
    f("meta_complete", meta.get("status") == "complete" and meta.get("trace_schema_version") == 1
      and meta.get("failed_step") is None and meta.get("error_code") is None)
    f("rules_version", meta.get("pipeline_version") == rules_pv)

    # identity
    pv = binding["pipeline_version"]
    f("pipeline_version", meta.get("pipeline_version") == pv and audit.get("pipeline_version") == pv
      and rel.get("pipeline_version") == pv)
    f("job_ids", audit.get("generation_id") == job and rel.get("job_id") == job)

    # notes, by reference (sha256 of the stripped corpus notes)
    notes = corpus_notes(corpus_path).get(binding["scenario"], "")
    nsha = sha256_text(notes) if notes else None
    st1 = (s.get("STEP#01#guard_input#A1") or {}).get("output") or {}
    f("notes_sha256", nsha == binding["notes_sha256"] and meta.get("notes_sha256") == nsha
      and audit.get("input_sha256") == nsha and st1.get("notes_sha256") == nsha
      and (prov.get("corpus") or {}).get("notes_sha256") == nsha)

    # release truth: the audit row + the ResultsTable item (META alone does not prove release)
    docs = {k: rel.get(k) for k, _ in DOCS}
    f("release_truth", audit.get("status") == "complete" and all(isinstance(v, str) and v for v in docs.values()))

    # projections + embedded JSON
    f("audit_fields", set(audit) == AUDIT_FIELDS)
    f("release_fields", set(rel) == RELEASE_FIELDS)
    decoded = {}
    for k in JSON_FIELDS:
        try:
            decoded[k] = json.loads(rel[k]) if isinstance(rel.get(k), str) else None
        except ValueError:
            decoded[k] = None
    f("json_fields", all(decoded[k] is not None for k in JSON_FIELDS))

    # three independent hash records, each against sha256 of the released documents
    dh = {k: sha256_text(v) if isinstance(v, str) else None for k, v in docs.items()}
    f("hash_audit", audit.get("output_sha256") == dh)
    f("hash_results", decoded.get("output_sha256_json") == {p: dh[k] for k, p in DOCS})
    out8 = (s.get("STEP#08b#assemble_patient#A1") or {}).get("output") or {}
    f("hash_8b", out8.get("hashes") == {p: dh[k] for k, p in DOCS})
    f("docs_8b", all(out8.get(p) == docs[k] for k, p in DOCS))

    # check receipt (recorded in provenance; its source byte hash is pinned in BINDING)
    rc = prov.get("receipt") or {}
    crit, cov = rc.get("criteria"), rc.get("coverage")
    f("receipt", rc.get("check_json_sha256") == binding["source_sha256"]["check.json"]
      and isinstance(crit, dict) and set(crit) == RECEIPT_CRITERIA and all(v is True for v in crit.values())
      and isinstance(cov, dict) and set(cov) == RECEIPT_COVERAGE.get(binding["scenario"], {None})
      and all(v is True for v in cov.values()) and rc.get("met") is True
      and rc.get("hard_stops_clear") is True and rc.get("repeat_eligible") is False)

    # trail (three paths), recomputed and compared with the committed report
    try:
        trail = trail_report(trace)
        f("trail_report", trail == trail_saved)
        info["trail"] = {"cites_total": trail["step3_facts_to_lines"]["cites_total"],
                         "cites_verified": trail["step3_facts_to_lines"]["cites_verified"],
                         "unverified_cites": len(trail["step3_facts_to_lines"]["unverified_cites"]),
                         "links_6": trail["step6_sections_to_facts"]["links_resolved"],
                         "flags_6": len(trail["step6_sections_to_facts"]["flags"]),
                         "links_7": trail["step7_sections_to_a_sections"]["links_resolved"],
                         "flags_7": len(trail["step7_sections_to_a_sections"]["flags"])}
    except TrailError as e:
        fails.append(e.code)

    # README: the fixed statements, and no unresolved CSO placeholder
    f("readme", all(x in readme for x in REQUIRED_README) and README_PENDING not in readme)

    # privacy: every file, every decoded embedded JSON, the README text
    views = collect_views({"trace.json": trace, "audit.json": audit, "release.json": rel,
                           "trail.json": trail_saved, "provenance.json": prov})
    hits = privacy_scan(views, notes or "\0", [readme])
    for code in ("privacy_user_sub_key", "privacy_user_hash", "privacy_uuid", "privacy_idempotency_key",
                 "privacy_notes_key", "privacy_line_index", "privacy_quote_len", "privacy_notes_run",
                 "privacy_whole_notes", "privacy_arn", "privacy_acct12"):
        f(code, hits[code] == 0)
    info["views_scanned"] = len(views)
    info["trace_items"] = len(trace)
    return fails, info


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) > 1:
        print("usage: python evals/check_worked_example.py [DIR]")
        return 2
    d = Path(argv[0]) if argv else DEFAULT_DIR
    fails, info = check(d)
    print(json.dumps({"example": d.name, **info}, sort_keys=True))
    if fails:
        print("NOT MET:", " ".join(fails))
        return 1
    print("MET")
    return 0


if __name__ == "__main__":
    sys.exit(main())
