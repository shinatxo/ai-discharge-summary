"""Gold records for the step-level metrics — safety-netting correctness (DoD 5)
and D4 medication reconciliation — scored identically on v1 and the pipeline
(ADR-009 (e); "Build record — W4").

One JSON file per scenario in evals/gold/<ID>.json. The format is described in
evals/gold/README.md. In short: what the NOTES document, never what any
generator wrote, with every claim cited by line ID + verbatim quote in the same
form step 3 verifies — so this module checks every gold citation with the
pipeline's own `verify_cite`, against the pipeline's own line index.

A gold record is pinned to the exact notes by `notes_sha256`: if a scenario's
notes change, its gold stops validating rather than silently scoring against
text it was not written for.

Errors name scenario IDs, field paths and fixed reasons — never note text.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src" / "generate"))
from pipeline import run_step  # noqa: E402
from pipeline.validate import verify_cite  # noqa: E402

GOLD_DIR = REPO / "evals" / "gold"
GOLD_VERSION = 1

ROUTES = ("documented", "fallback")
AGE_GROUPS = ("neonate", "infant", "child", "adult", "not_documented")
DISCHARGE_STATUSES = ("listed", "referenced_not_listed", "none_required", "not_documented")
TAGS = ("new", "continued", "increased", "decreased", "stopped", "withheld", "changed")
BASES = ("documented", "inferred_from_dh")
MUST_NOT_REASONS = ("inpatient_only", "stopped", "not_prescribed")
STATUSES = ("draft", "approved")
ADVICE_KINDS = ("instruction", "record")


class GoldError(ValueError):
    pass


def line_index(notes: str) -> dict:
    """The pipeline's line index for these notes (step 1), so gold line IDs are
    the IDs every pipeline step uses."""
    return run_step("guard_input", {"notes": notes})["lines"]


def _cites(value, path: str, lines: dict, errors: list, *, required: bool) -> None:
    if not isinstance(value, list):
        errors.append(f"{path}: must be a list")
        return
    if required and not value:
        errors.append(f"{path}: at least one citation required")
    for i, c in enumerate(value):
        if not isinstance(c, dict) or set(c) != {"lines", "quote"}:
            errors.append(f"{path}[{i}]: a citation is exactly {{lines, quote}}")
            continue
        v = verify_cite(c, lines)
        if not v["verified"]:
            errors.append(f"{path}[{i}]: {v['reason']}")


def _enum(value, allowed, path, errors) -> None:
    if value not in allowed:
        errors.append(f"{path}: must be one of {', '.join(map(str, allowed))}")


def _keys(obj, required: set, optional: set, path: str, errors: list) -> bool:
    if not isinstance(obj, dict):
        errors.append(f"{path}: must be an object")
        return False
    missing, extra = required - obj.keys(), obj.keys() - required - optional
    if missing:
        errors.append(f"{path}: missing {', '.join(sorted(missing))}")
    if extra:
        errors.append(f"{path}: unknown {', '.join(sorted(extra))}")
    return not missing


def validate(gold: dict, notes: str, notes_sha256: str) -> list[str]:
    """Every problem with one gold record, as fixed-wording messages. [] = valid."""
    errors: list[str] = []
    top = {"gold_version", "scenario_id", "notes_sha256", "status", "provenance",
           "age_group", "safety_net", "medications"}
    if not _keys(gold, top, {"review_notes"}, "$", errors):
        return errors
    if gold["gold_version"] != GOLD_VERSION:
        errors.append(f"$.gold_version: must be {GOLD_VERSION}")
    if gold["notes_sha256"] != notes_sha256:
        errors.append("$.notes_sha256: does not match the corpus notes — re-derive this gold")
        return errors
    lines = line_index(notes)

    _enum(gold["status"], STATUSES, "$.status", errors)
    prov = gold["provenance"]
    if _keys(prov, {"drafted_by", "sources", "approved_by", "approved_on"}, set(), "$.provenance", errors):
        if gold["status"] == "approved" and not (prov["approved_by"] and prov["approved_on"]):
            errors.append("$.provenance: an approved record names approved_by and approved_on")
        if gold["status"] == "draft" and (prov["approved_by"] or prov["approved_on"]):
            errors.append("$.provenance: a draft carries no approval")

    ag = gold["age_group"]
    if _keys(ag, {"value", "cites"}, set(), "$.age_group", errors):
        _enum(ag["value"], AGE_GROUPS, "$.age_group.value", errors)
        _cites(ag["cites"], "$.age_group.cites", lines, errors,
               required=ag["value"] != "not_documented")

    sn = gold["safety_net"]
    if _keys(sn, {"route", "advice", "seek_help"}, {"rationale"}, "$.safety_net", errors):
        # CSO, 6 Oct 2026: documented advice and safety-netting are distinct. seek_help
        # records whether the notes document a seek-help trigger (when and where to get
        # help). Without one, no output may carry a trigger beyond the pinned line —
        # even where other advice is documented (S15, S18).
        sh = sn["seek_help"]
        if _keys(sh, {"documented", "cites"}, set(), "$.safety_net.seek_help", errors):
            if not isinstance(sh["documented"], bool):
                errors.append("$.safety_net.seek_help.documented: must be true or false")
            else:
                _cites(sh["cites"], "$.safety_net.seek_help.cites", lines, errors,
                       required=sh["documented"])
                if not sh["documented"] and sh["cites"]:
                    errors.append("$.safety_net.seek_help: no citations unless documented")
                if sh["documented"] and sn.get("route") != "documented":
                    errors.append("$.safety_net: a documented seek-help trigger is documented advice")
        _enum(sn["route"], ROUTES, "$.safety_net.route", errors)
        if not isinstance(sn["advice"], list):
            errors.append("$.safety_net.advice: must be a list")
        else:
            if (sn["route"] == "documented") != bool(sn["advice"]):
                errors.append("$.safety_net: route 'documented' if and only if advice is non-empty")
            for i, a in enumerate(sn["advice"]):
                if _keys(a, {"cites", "kind"}, {"note"}, f"$.safety_net.advice[{i}]", errors):
                    # instruction: the advice itself is written ("No driving."). record: the
                    # notes record that advice was given and what it covered — documented
                    # advice only since the CSO ruling of 1 Oct 2026, after v0.7 was written.
                    _enum(a["kind"], ADVICE_KINDS, f"$.safety_net.advice[{i}].kind", errors)
                    _cites(a["cites"], f"$.safety_net.advice[{i}].cites", lines, errors, required=True)

    meds = gold["medications"]
    if _keys(meds, {"discharge_status", "status_cites", "drugs", "must_not_appear"}, set(),
             "$.medications", errors):
        status = meds["discharge_status"]
        _enum(status, DISCHARGE_STATUSES, "$.medications.discharge_status", errors)
        _cites(meds["status_cites"], "$.medications.status_cites", lines, errors,
               required=status in ("referenced_not_listed", "none_required"))
        drugs = meds["drugs"] if isinstance(meds["drugs"], list) else []
        if status in ("none_required", "not_documented") and any(
                d.get("tag") != "stopped" for d in drugs if isinstance(d, dict)):
            errors.append("$.medications: only STOPPED pre-admission drugs may sit beside "
                          f"discharge_status {status}")
        names: set[str] = set()
        for i, d in enumerate(drugs):
            p = f"$.medications.drugs[{i}]"
            if not _keys(d, {"drug", "aliases", "dose", "route", "frequency", "tag", "basis",
                             "previous", "dh_cites", "discharge_cites"}, {"note", "conflict"}, p, errors):
                continue
            if "conflict" in d:
                # The notes give two or more values for this drug (B6's warfarin). The
                # correct output flags the conflict; it never picks one. dose stays null.
                cf = d["conflict"]
                if _keys(cf, {"field", "values", "cites"}, set(), f"{p}.conflict", errors):
                    _enum(cf["field"], ("dose", "frequency"), f"{p}.conflict.field", errors)
                    if not (isinstance(cf["values"], list) and len(set(cf["values"])) >= 2):
                        errors.append(f"{p}.conflict.values: at least two different values")
                    _cites(cf["cites"], f"{p}.conflict.cites", lines, errors, required=True)
                    if isinstance(cf["cites"], list) and len(cf["cites"]) < 2:
                        errors.append(f"{p}.conflict.cites: one citation per side")
                    if cf["field"] in ("dose", "frequency") and d.get(cf["field"]) is not None:
                        errors.append(f"{p}: a conflicting {cf['field']} is null, never resolved")
            all_names = {d["drug"].lower(), *(a.lower() for a in d["aliases"])}
            if all_names & names:
                errors.append(f"{p}: a drug name or alias is used twice")
            names |= all_names
            _enum(d["tag"], TAGS, f"{p}.tag", errors)
            _enum(d["basis"], BASES, f"{p}.basis", errors)
            if d["basis"] == "inferred_from_dh" and d["tag"] != "continued":
                errors.append(f"{p}: only a 'continued' drug can be inferred from the DH")
            if d["tag"] in ("increased", "decreased", "changed"):
                if not isinstance(d["previous"], dict) or set(d["previous"]) != {"dose", "frequency"}:
                    errors.append(f"{p}.previous: {{dose, frequency}} required for tag {d['tag']}")
            elif d["previous"] is not None:
                errors.append(f"{p}.previous: null unless the tag is a change")
            needs_dh = d["tag"] in ("continued", "increased", "decreased", "stopped",
                                    "withheld", "changed")
            _cites(d["dh_cites"], f"{p}.dh_cites", lines, errors, required=needs_dh)
            _cites(d["discharge_cites"], f"{p}.discharge_cites", lines, errors,
                   required=d["basis"] == "documented")
            if d["tag"] == "new" and d["dh_cites"]:
                errors.append(f"{p}: a NEW drug has no DH citation")
        for i, m in enumerate(meds["must_not_appear"]):
            p = f"$.medications.must_not_appear[{i}]"
            if _keys(m, {"drug", "aliases", "reason", "cites"}, {"note"}, p, errors):
                _enum(m["reason"], MUST_NOT_REASONS, f"{p}.reason", errors)
                _cites(m["cites"], f"{p}.cites", lines, errors, required=True)
                if {m["drug"].lower(), *(a.lower() for a in m["aliases"])} & names:
                    errors.append(f"{p}: also listed as a discharge drug")
    return errors


def load(scenario_id: str, gold_dir: Path = GOLD_DIR) -> dict:
    path = gold_dir / f"{scenario_id}.json"
    gold = json.loads(path.read_text(encoding="utf-8"))
    if gold.get("scenario_id") != scenario_id:
        raise GoldError(f"{path.name}: scenario_id does not match the file name")
    return gold


def load_valid(scenario_id: str, notes: str, notes_sha256: str,
               gold_dir: Path = GOLD_DIR) -> dict:
    gold = load(scenario_id, gold_dir)
    errors = validate(gold, notes, notes_sha256)
    if errors:
        raise GoldError(f"{scenario_id}: {len(errors)} gold error(s): " + "; ".join(errors))
    return gold
